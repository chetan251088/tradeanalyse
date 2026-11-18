"""Weekend VRP Harvester, live and PAPER: the one rule set that survived the 2021-2026 tests (scalper.vrp_harvest).

Runs inside the scanner (scalper.run.run_day) on every trading day, per index:
  15:22 IST  entry check: exactly `dte_entry` sessions left to the next weekly expiry (NSE/BSE holiday calendar
             from OpenAlgo) and India VIX >= the median of its previous 250 daily 15:25 values -> sell the ATM
             straddle, buy wings 2x the straddle away (4-10 strikes), size to `risk_pct` of paper equity.
             Risk gates first (ported from the day-trader-skill trader-risk / trader-budget skills): one position
             per index, total open risk, a drawdown pause and a spread-cost cap. Telegram [HARVEST PAPER] alert.
  15:25 IST  open positions: on expiry day close at the ask (shorts) / bid (wings) and book P&L after charges;
             on other days journal a mark-to-market line. No stop: the tested rule holds to expiry.
Every check is journalled with its reason, so you can see what it collects and why it did or did not trigger:

  .venv\\Scripts\\python.exe -m scalper.harvest --report
  .venv\\Scripts\\python.exe -m scalper.harvest --check-now --env-file env\\dhan.env   (dry run, any time)

Paper only: no order is ever placed.
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from scalper.clock import now_ms, to_dt
from scalper.config import Config, HarvestCfg, UnderlyingCfg, load_config, load_env_file
from scalper.execution import charges
from scalper.notify import Notifier, TelegramBot
from scalper.store import harvest_paused
from scalper.universe import Rest

log = logging.getLogger("scalper.harvest")

INDEX_ROOT = Path("data/market/index")
TICK = 0.05

SCHEMA = """
CREATE TABLE IF NOT EXISTS harvest_checks (
    day TEXT, time TEXT, underlying TEXT, decision TEXT, reason TEXT, details TEXT
);
CREATE TABLE IF NOT EXISTS harvest_positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, underlying TEXT, exchange TEXT, opened TEXT, expiry TEXT, status TEXT,
    lots INTEGER, lot_size INTEGER, legs TEXT, credit REAL, width REAL, max_loss_unit REAL, max_loss_rs REAL,
    vix REAL, vix_median REAL, spot REAL, closed TEXT, exit_value REAL, charges_rs REAL, pnl_unit REAL,
    pnl_r REAL, pnl_rs REAL, note TEXT
);
CREATE TABLE IF NOT EXISTS harvest_marks (
    day TEXT, position_id INTEGER, value REAL, pnl_unit REAL, pnl_r REAL, pnl_rs REAL
);
"""


# ---- pure rules (unit-tested) -----------------------------------------------
def closed_days(holidays: list[dict], exchange: str) -> set[date]:
    """Weekdays the exchange is shut (TRADING_HOLIDAY rows of OpenAlgo's market calendar)."""
    out = set()
    for h in holidays:
        if h.get("holiday_type", "TRADING_HOLIDAY") == "TRADING_HOLIDAY" and exchange in (h.get("closed_exchanges") or []):
            out.add(date.fromisoformat(str(h["date"])[:10]))
    return out


def sessions_left(today: date, expiry: date, closed: set[date]) -> int:
    """Trading sessions after today up to and including expiry day."""
    n, d = 0, today + timedelta(days=1)
    while d <= expiry:
        n += d.weekday() < 5 and d not in closed
        d += timedelta(days=1)
    return n


def daily_vix(bar: str = "15:25", root: Path = INDEX_ROOT) -> pd.Series:
    """India VIX at `bar` IST (or the last bar before it) for every stored session."""
    files = sorted((root / "INDIAVIX").glob("*.parquet"))
    if not files:
        return pd.Series(dtype=float)
    df = pd.concat([pd.read_parquet(f, columns=["ts", "close"]) for f in files], ignore_index=True)
    dt = pd.to_datetime(df.ts, unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    hm = dt.dt.strftime("%H:%M")
    df = df.assign(day=dt.dt.date)[(hm >= "09:15") & (hm <= bar)]
    return df.sort_values("ts").groupby("day").close.last()


def vix_median(daily: pd.Series, today: date, lookback: int, min_days: int) -> tuple[float, date | None, int]:
    """Median of the last `lookback` daily values strictly before today; (median, last day used, days used)."""
    past = daily[[d < today for d in daily.index]].tail(lookback)
    last = past.index[-1] if len(past) else None
    return (float(past.median()) if len(past) >= min_days else float("nan")), last, len(past)


def wing_strikes(straddle: float, step: float, h: HarvestCfg) -> int:
    return int(min(max(h.min_wing, round(h.wing_mult * straddle / step)), h.max_wing))


def size_lots(equity: float, risk_pct: float, max_loss_unit: float, lot: int) -> int:
    return int(equity * risk_pct / 100 // (max_loss_unit * lot))


def build_plan(chain: dict, h: HarvestCfg, equity: float, allow_ltp: bool = False) -> tuple[dict | None, str]:
    """Short ATM straddle at the bid, wings at the ask, from an OpenAlgo option chain response."""
    rows = {float(r["strike"]): r for r in chain.get("chain", []) if r.get("strike") is not None}
    strikes = sorted(rows)
    if len(strikes) < 3:
        return None, "option chain empty"
    step = min(b - a for a, b in zip(strikes, strikes[1:]))
    spot = float(chain.get("underlying_ltp") or 0)
    if spot <= 0:
        return None, "no index price in the option chain"
    k = round(spot / step) * step

    def leg(opt: str, strike: float, side: int) -> dict | None:
        q = (rows.get(strike) or {}).get(opt.lower()) or {}
        if not q.get("symbol"):
            return None
        bid, ask, ltp = (float(q.get(x) or 0) for x in ("bid", "ask", "ltp"))
        if bid <= 0 or ask < bid:
            if not allow_ltp or ltp <= 0:
                return None
            bid = ask = ltp
        return {"symbol": q["symbol"], "opt": opt, "strike": strike, "side": side, "bid": bid, "ask": ask,
                "lotsize": int(q.get("lotsize") or 0)}

    ce, pe = leg("CE", k, -1), leg("PE", k, -1)
    if not (ce and pe):
        return None, f"no two-sided quote at the ATM strike {k:g}"
    straddle = (ce["bid"] + ce["ask"]) / 2 + (pe["bid"] + pe["ask"]) / 2
    w = wing_strikes(straddle, step, h)
    wce, wpe = leg("CE", k + w * step, 1), leg("PE", k - w * step, 1)
    if not (wce and wpe):
        return None, f"no two-sided quote {w} strikes out (wings)"
    lot = ce["lotsize"]
    if lot <= 0:
        return None, "lot size missing from the option chain"
    legs = [ce, pe, wce, wpe]
    credit = round(ce["bid"] + pe["bid"] - wce["ask"] - wpe["ask"], 2)
    width = w * step
    if credit <= 0 or credit >= width:
        return None, f"credit {credit:.2f} outside (0, {width:g})"
    max_loss_unit = width - credit
    lots = size_lots(equity, h.risk_pct, max_loss_unit, lot)
    return {
        "spot": spot, "atm": k, "step": step, "straddle": round(straddle, 2), "wing_strikes": w, "width": width,
        "credit": credit, "max_loss_unit": round(max_loss_unit, 2), "lot": lot, "lots": lots,
        "max_loss_rs": round(max_loss_unit * lot * lots, 0), "lot_risk_rs": round(max_loss_unit * lot, 0),
        "spread_cost": round(sum(x["ask"] - x["bid"] for x in legs), 2),
        "legs": [{k2: x[k2] for k2 in ("symbol", "opt", "strike", "side", "bid", "ask")} for x in legs],
    }, ""


def gates(plan: dict, h: HarvestCfg, equity: float, peak: float, open_risk_rs: float, holding: bool) -> list[str]:
    """trader-risk style hard checks; any failure blocks the entry."""
    fails = []
    if holding:
        fails.append("already holding a position on this index")
    dd = (1 - equity / peak) * 100 if peak > 0 else 0.0
    if dd >= h.pause_drawdown_pct:
        fails.append(f"paused: paper drawdown {dd:.1f}% >= {h.pause_drawdown_pct:g}% (review, then raise the limit)")
    if plan["spread_cost"] > h.max_spread_cost_pct / 100 * plan["credit"]:
        fails.append(f"bid/ask spreads {plan['spread_cost']:.2f} > {h.max_spread_cost_pct:g}% of the credit")
    if plan["lots"] and open_risk_rs + plan["max_loss_rs"] > h.max_open_risk_pct / 100 * equity:
        fails.append(f"open risk Rs {open_risk_rs + plan['max_loss_rs']:,.0f} > {h.max_open_risk_pct:g}% of equity")
    return fails


def events_between(events: list[str], start: date, end: date) -> list[str]:
    """Config lines like '2026-06-05 RBI policy' that fall inside the holding window."""
    out = []
    for e in events:
        try:
            d = date.fromisoformat(e.strip()[:10])
        except ValueError:
            continue
        if start <= d <= end:
            out.append(e.strip())
    return out


def close_value(legs: list[dict], quotes: dict[str, dict], at_touch: bool) -> float | None:
    """Per-unit cost to close: shorts bought at the ask, wings sold at the bid (at_touch) or all at the mid."""
    v = 0.0
    for x in legs:
        q = quotes.get(x["symbol"]) or {}
        bid, ask, ltp = (float(q.get(k) or 0) for k in ("bid", "ask", "ltp"))
        if bid <= 0 or ask < bid:
            if ltp <= 0:
                return None
            bid = ask = ltp
        px = (ask if x["side"] < 0 else bid) if at_touch else (bid + ask) / 2
        x["exit"] = px
        v -= x["side"] * px
    return v


def fly_intrinsic(spot: float, atm: float, width: float) -> float:
    return min(abs(spot - atm), width)


# ---- live job -----------------------------------------------------------------
class Harvester:
    def __init__(self, cfg: Config, rest: Rest, con: sqlite3.Connection, notifier: Notifier,
                 vix_history=None, index_close=None):
        self.cfg, self.h, self.rest, self.con, self.notifier = cfg, cfg.harvest, rest, con, notifier
        self.vix_history = vix_history or (lambda: daily_vix(self.h.vix_bar))
        self.index_close = index_close or _index_close
        con.executescript(SCHEMA)
        self._holidays: dict[int, list[dict]] = {}
        self._ran: set[tuple[date, str]] = set()

    # -- state
    def equity(self) -> tuple[float, float]:
        eq = peak = self.h.capital_rs
        for (pnl,) in self.con.execute("SELECT pnl_rs FROM harvest_positions WHERE status='closed' ORDER BY closed, id"):
            eq += pnl or 0.0
            peak = max(peak, eq)
        return eq, peak

    def positions(self, status: str | None = "open") -> list[dict]:
        sql = "SELECT * FROM harvest_positions" + (" WHERE status=?" if status else "") + " ORDER BY id"
        cur = self.con.execute(sql, (status,) if status else ())
        cols = [c[0] for c in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]

    def closed(self, exchange: str, start: date, end: date) -> set[date]:
        out: set[date] = set()
        for y in range(start.year, end.year + 1):
            if y not in self._holidays:
                self._holidays[y] = self.rest.holidays(y)
            out |= closed_days(self._holidays[y], exchange)
        return out

    def _record(self, dt: datetime, name: str, decision: str, reason: str, details: dict, dry: bool) -> None:
        log.info("Harvest %s %s: %s", name, decision, reason)
        if dry:
            return
        self.con.execute("INSERT INTO harvest_checks VALUES (?,?,?,?,?,?)", (
            dt.date().isoformat(), dt.strftime("%H:%M:%S"), name, decision, reason, json.dumps(details, default=str)))
        self.con.commit()

    # -- scheduler hook (called from the scanner loop)
    def poll(self, now: int) -> None:
        if not self.h.enabled:
            return
        dt = to_dt(now)
        day, hm = dt.date(), dt.strftime("%H:%M")
        if day.weekday() >= 5 or not (self.h.entry_time <= hm < self.h.deadline):
            return
        for job, start, fn in (("entry", self.h.entry_time, self.check_entries),
                               ("mark", self.h.mark_time, self.mark_positions)):
            if hm >= start and (day, job) not in self._ran:
                self._ran.add((day, job))
                try:
                    fn(dt)
                except Exception as e:
                    log.exception("Harvest %s job failed", job)
                    self.notifier.send(f"[HARVEST] {job} job failed: {e}", priority=8)

    # -- 15:22 entry check
    def check_entries(self, dt: datetime, dry: bool = False, force: bool = False) -> list[tuple[str, str, str]]:
        out, ctx = [], {}
        for u in self.cfg.underlyings:
            if not u.enabled:
                continue
            if not dry and self.con.execute(
                    "SELECT 1 FROM harvest_checks WHERE day=? AND underlying=? AND decision!='error' LIMIT 1",
                    (dt.date().isoformat(), u.name)).fetchone():
                continue  # restarted scanner: already decided today
            try:
                decision, reason, det = self._check(u, dt, ctx, dry, force)
            except Exception as e:
                decision, reason, det = "error", str(e)[:300], {}
            self._record(dt, u.name, decision, reason, det, dry)
            out.append((u.name, decision, reason))
        return out

    def _check(self, u: UnderlyingCfg, dt: datetime, ctx: dict, dry: bool, force: bool) -> tuple[str, str, dict]:
        h, day = self.h, dt.date()
        expiries = self.rest.expiries(u)
        upcoming = [d for d in expiries if d > day]
        if not upcoming:
            return "skip", "no upcoming expiry", {}
        exp = upcoming[0]
        closed = self.closed(u.fo_exchange, day, exp)
        n = sessions_left(day, exp, closed)
        det: dict = {"expiry": exp.isoformat(), "sessions_left": n}
        would_skip = []
        if day in closed:
            would_skip.append("exchange holiday today")
        elif day in expiries:
            would_skip.append("today is an expiry day (the tested rule never enters on one)")
        elif n != h.dte_entry:
            would_skip.append(f"{n} sessions to the {exp} expiry (enters at {h.dte_entry})")
        if would_skip and not force:
            return "skip", would_skip[0], det
        if harvest_paused(self.cfg.paths.data_dir):
            would_skip.append("paused from Telegram (/resume to re-enable)")
            if not force:
                return "skip", would_skip[-1], det
        if "vix" not in ctx:
            ctx["vix"] = float(self.rest.quote(self.cfg.universe.vix_symbol, self.cfg.universe.vix_exchange).get("ltp") or 0)
            ctx["daily"] = self.vix_history()
        vix = ctx["vix"]
        med, last, nd = vix_median(ctx["daily"], day, h.vix_lookback, h.vix_min_days)
        det.update(vix=vix, vix_median=round(med, 2), vix_days=nd, vix_last=str(last))
        if vix <= 0:
            return "error", "no live India VIX quote", det
        if last is None or nd < h.vix_min_days:
            return "skip", f"only {nd} days of VIX history (needs {h.vix_min_days}); run the data update", det
        if (day - last).days > h.max_vix_age_days:
            return "skip", f"VIX history ends {last}; run tools\\run_data_update.ps1", det
        if vix < med:
            would_skip.append(f"VIX {vix:.2f} < {h.vix_lookback}-day median {med:.2f} (volatility not rich)")
            if not force:
                return "skip", would_skip[-1], det
        chain = self.rest.option_chain(u, exp, h.max_wing + 3)
        equity, peak = self.equity()
        plan, why = build_plan(chain, h, equity, allow_ltp=dry)
        if plan is None:
            return "skip", why, det
        det["plan"] = plan
        opens = self.positions("open")
        fails = gates(plan, h, equity, peak, sum(p["max_loss_rs"] or 0 for p in opens),
                      any(p["underlying"] == u.name for p in opens))
        if fails and not force:
            return "skip", "; ".join(fails), det
        evs = events_between(h.events, day, exp)
        text = entry_text(u.name, exp, n, plan, vix, med, equity, h, evs, would_skip + fails)
        if not dry:
            self.con.execute(
                "INSERT INTO harvest_positions (underlying, exchange, opened, expiry, status, lots, lot_size, legs, credit,"
                " width, max_loss_unit, max_loss_rs, vix, vix_median, spot, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (u.name, u.fo_exchange, dt.strftime("%Y-%m-%d %H:%M"), exp.isoformat(), "open", plan["lots"], plan["lot"],
                 json.dumps(plan["legs"]), plan["credit"], plan["width"], plan["max_loss_unit"], plan["max_loss_rs"],
                 vix, med, plan["spot"], "; ".join(evs)))
            self.con.commit()
        self.notifier.send(text, priority=7)
        if would_skip or fails:
            return "dry", "; ".join(would_skip + fails), det
        return "entry", "shadow (0 lots)" if plan["lots"] == 0 else f"{plan['lots']} lots", det

    # -- 15:25 marks and expiry closes
    def mark_positions(self, dt: datetime, dry: bool = False) -> list[str]:
        day, lines = dt.date(), []
        for p in self.positions("open"):
            exp, legs = date.fromisoformat(p["expiry"]), json.loads(p["legs"])
            if exp < day:
                lines.append(self._settle_missed(p, legs, exp, dry))
                continue
            quotes = self.rest.multiquotes([{"symbol": x["symbol"], "exchange": p["exchange"]} for x in legs])
            value = close_value(legs, quotes, at_touch=exp == day)
            if value is None:
                lines.append(f"{p['underlying']} #{p['id']}: no quotes for every leg, not marked")
                continue
            value = min(max(value, 0.0), p["width"])
            if exp == day:
                lines.append(self._close(p, legs, value, day, "closed 15:25 at the touch", dry))
            else:
                qty = p["lots"] * p["lot_size"]
                pnl_unit = p["credit"] - value
                if not dry:
                    self.con.execute("INSERT INTO harvest_marks VALUES (?,?,?,?,?,?)", (
                        day.isoformat(), p["id"], value, pnl_unit, pnl_unit / p["max_loss_unit"], pnl_unit * qty))
                    self.con.commit()
                lines.append(f"{p['underlying']} #{p['id']} exp {p['expiry']}: value {value:.1f} vs credit {p['credit']:.1f}"
                             f" = {pnl_unit:+.1f}/unit ({pnl_unit / p['max_loss_unit']:+.2f}R, Rs {pnl_unit * qty:+,.0f})")
        if lines:
            eq, _ = self.equity()
            self.notifier.send(f"[HARVEST PAPER] {day} 15:25\n" + "\n".join(lines) + f"\nPaper equity Rs {eq:,.0f}",
                               priority=5)
        return lines

    def _close(self, p: dict, legs: list[dict], value: float, day: date, how: str, dry: bool) -> str:
        qty_c = max(p["lots"], 1) * p["lot_size"]  # shadow positions: charges per unit from one lot
        buys = sum(x["ask"] for x in legs if x["side"] > 0) + sum(x["exit"] for x in legs if x["side"] < 0)
        sells = sum(x["bid"] for x in legs if x["side"] < 0) + sum(x["exit"] for x in legs if x["side"] > 0)
        ch_unit = charges(p["exchange"], day, buys * qty_c, sells * qty_c, 8) / qty_c
        pnl_unit = p["credit"] - value
        pnl_r = (pnl_unit - ch_unit) / p["max_loss_unit"]
        qty = p["lots"] * p["lot_size"]
        pnl_rs = (pnl_unit - ch_unit) * qty
        if not dry:
            self.con.execute(
                "UPDATE harvest_positions SET status='closed', closed=?, exit_value=?, charges_rs=?, pnl_unit=?, pnl_r=?,"
                " pnl_rs=?, legs=?, note=trim(coalesce(note,'') || ' ' || ?) WHERE id=?",
                (day.isoformat(), value, ch_unit * qty, pnl_unit, pnl_r, pnl_rs, json.dumps(legs), how, p["id"]))
            self.con.commit()
        return (f"CLOSED {p['underlying']} #{p['id']} exp {p['expiry']} ({how}): credit {p['credit']:.1f} -> exit "
                f"{value:.1f}, {pnl_unit - ch_unit:+.1f}/unit after charges = {pnl_r:+.2f}R"
                + (f", Rs {pnl_rs:+,.0f}" if qty else " (shadow, 0 lots)"))

    def _settle_missed(self, p: dict, legs: list[dict], exp: date, dry: bool) -> str:
        spot = self.index_close(p["underlying"], exp, self.h.mark_time)
        if spot is None:
            return f"{p['underlying']} #{p['id']} expired {exp} but no stored index price; run the data update"
        atm = next(x["strike"] for x in legs if x["side"] < 0)
        for x in legs:
            x["exit"] = max(0.0, (spot - x["strike"]) if x["opt"] == "CE" else (x["strike"] - spot))
        return self._close(p, legs, fly_intrinsic(spot, atm, p["width"]), exp,
                           f"scanner was off at expiry: settled at intrinsic, index {spot:.1f}", dry)


def _index_close(name: str, day: date, bar: str, root: Path = INDEX_ROOT) -> float | None:
    f = root / name / f"{day.year}.parquet"
    if not f.exists():
        return None
    df = pd.read_parquet(f, columns=["ts", "close"])
    dt = pd.to_datetime(df.ts, unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    m = (dt.dt.date == day) & (dt.dt.strftime("%H:%M") <= bar)
    return float(df.close[m].iloc[-1]) if m.any() else None


def entry_text(name: str, exp: date, n: int, plan: dict, vix: float, med: float, equity: float, h: HarvestCfg,
               events: list[str], dry_notes: list[str]) -> str:
    tag = "DRY RUN" if dry_notes else ("PAPER" if h.paper else "LIVE")
    lines = [f"[HARVEST {tag}] {name} weekly {exp} ({n} sessions left)",
             f"VIX {vix:.2f} vs {h.vix_lookback}-day median {med:.2f} | spot {plan['spot']:,.1f}"]
    for x in plan["legs"]:
        side, px = ("SELL", x["bid"]) if x["side"] < 0 else ("BUY ", x["ask"])
        lines.append(f"{side} {x['symbol']} @ {px:.2f}")
    lines.append(f"Credit {plan['credit']:.2f} | wings {plan['wing_strikes']} strikes ({plan['width']:g} pts) | "
                 f"max loss {plan['max_loss_unit']:.2f}/unit")
    if plan["lots"]:
        lines.append(f"Size {plan['lots']} lot(s) x {plan['lot']} | max loss Rs {plan['max_loss_rs']:,.0f} = "
                     f"{plan['max_loss_rs'] / equity * 100:.2f}% of paper equity Rs {equity:,.0f}")
    else:
        lines.append(f"SHADOW: 1 lot risks Rs {plan['lot_risk_rs']:,.0f} > {h.risk_pct:g}% of Rs {equity:,.0f}; "
                     f"tracked per unit only (raise harvest.capital_rs / risk_pct)")
    lines.append("Hold to expiry 15:25 IST, no stop (the tested rule). Paper: no order placed.")
    if events:
        lines.append("Events in the window (not a filter): " + ", ".join(events))
    if dry_notes:
        lines.append("Would NOT trigger: " + "; ".join(dry_notes))
    return "\n".join(lines)


# ---- CLI ------------------------------------------------------------------------
def report(con: sqlite3.Connection, h: HarvestCfg, last: int = 15) -> str:
    out = []
    pos = pd.read_sql("SELECT * FROM harvest_positions ORDER BY id", con)
    eq = h.capital_rs + pos.loc[pos.status == "closed", "pnl_rs"].fillna(0).sum()
    out.append(f"Paper capital Rs {h.capital_rs:,.0f} -> equity Rs {eq:,.0f}")
    if pos.empty:
        out.append("No positions yet.")
    else:
        cols = ["id", "underlying", "opened", "expiry", "status", "lots", "credit", "width", "max_loss_unit",
                "vix", "vix_median", "exit_value", "pnl_r", "pnl_rs"]
        out.append(pos[cols].round(2).to_string(index=False))
        done = pos[pos.status == "closed"]
        if len(done):
            out.append(f"Closed {len(done)}: avg {done.pnl_r.mean():+.2f}R, win {100 * (done.pnl_r > 0).mean():.0f}%, "
                       f"total Rs {done.pnl_rs.sum():+,.0f}  (backtest test period: +0.31R per trade)")
        marks = pd.read_sql("SELECT * FROM harvest_marks ORDER BY day", con)
        if len(marks):
            out.append("Latest marks:\n" + marks.groupby("position_id").tail(1).round(2).to_string(index=False))
    chk = pd.read_sql("SELECT * FROM harvest_checks ORDER BY day, time", con)
    if len(chk):
        out.append(f"\nLast {last} checks:\n" + chk.tail(last)[["day", "time", "underlying", "decision", "reason"]].to_string(index=False))
        reason = chk.reason.str.replace(r"[\d.\-]+", "#", regex=True)
        out.append("\nWhy it did / did not trigger (all days):\n" + chk.assign(reason=reason).groupby(
            ["underlying", "decision", "reason"]).size().rename("days").to_string())
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="scalper.harvest", description=__doc__.split("\n")[0])
    p.add_argument("--config", default="config/dhan.yaml")
    p.add_argument("--env-file")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--report", action="store_true", help="positions, P&L and every journalled check")
    g.add_argument("--check-now", action="store_true", help="dry run of the entry check and marks with live quotes")
    p.add_argument("--telegram", action="store_true", help="with --check-now: also send the dry-run alert")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", stream=sys.stdout)
    if args.env_file:
        load_env_file(args.env_file)
    cfg = load_config(args.config)
    con = sqlite3.connect(cfg.paths.journal)
    con.executescript(SCHEMA)
    if args.report:
        print(report(con, cfg.harvest))
        return 0
    from openalgo import api

    client = api(api_key=cfg.api_key(), host=cfg.openalgo.host)
    notifier = Notifier(client if args.telegram else None, cfg.notify.telegram_username, cfg.notify.min_interval_sec,
                        TelegramBot.from_env() if args.telegram else None)
    hv = Harvester(cfg, Rest(client, cfg.api_key()), con, notifier)
    dt = to_dt(now_ms())
    for name, decision, reason in hv.check_entries(dt, dry=True, force=True):
        print(f"{name}: {decision} - {reason}")
    hv.mark_positions(dt, dry=True)
    if args.telegram:
        import time

        time.sleep(cfg.notify.min_interval_sec * 3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
