"""Replay downloaded sessions (Dhan expired-options chains) through the scanner and trade every alert realistically.

  .venv\\Scripts\\python.exe -m scalper.backtest
  .venv\\Scripts\\python.exe -m scalper.backtest --underlyings SENSEX --from 2025-01-01 --set s1.min_drop_pct=60
  .venv\\Scripts\\python.exe -m scalper.backtest --days other --any-day     (non-expiry sessions, setups unlocked)

Data: index and VIX bars are limited to 09:15-15:29. Each chain is cleaned against the real index
(rows whose strike does not match ATM+offset, minutes with no trade, duplicate contracts are dropped)
and days without full ATM coverage or without open interest are skipped (see the data section).
Replay: each 1m bar becomes four ticks; bid/ask = price -/+ a half-spread model (NIFTY 0.10%,
SENSEX 0.25%, at least one tick), which the setups' spread filters also see.
Execution (scalper/execution.py): order `latency` after the alert at the ask/bid plus slippage,
plan exits (targets as limits, stops with gap fills, 15:20 exit) and Dhan/exchange charges.
Results are in R, where R is the planned risk in rupees (S1 entry-stop, S2 premium, S3 width-credit).
The engine's own paper grade (fill at alert price, no costs) is kept as paper_r for comparison.
"""

from __future__ import annotations

import argparse
import logging
import math
import time
from bisect import bisect_left
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from scalper.clock import to_dt
from scalper.config import Config, _build, load_config
from scalper.execution import Market, Paths, execute
from scalper.history import bar_ticks
from scalper.notify import Notifier
from scalper.run import run_offline, sim_events

ROOT = Path("data/market")
EXCHANGES = {"NIFTY": ("NSE_INDEX", "NFO"), "SENSEX": ("BSE_INDEX", "BFO")}
LOT = {"NIFTY": 65, "SENSEX": 20}
STEP = {"NIFTY": 50, "SENSEX": 100}
HALF_SPREAD = {"NIFTY": 0.001, "SENSEX": 0.0025}
TICK = 0.05
IST = "Asia/Kolkata"


def _index(name: str) -> pd.DataFrame:
    files = sorted((ROOT / "index" / name).glob("*.parquet"))
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    dt = pd.to_datetime(df.ts, unit="s", utc=True).dt.tz_convert(IST)
    hm = dt.dt.strftime("%H:%M")
    keep = (hm >= "09:15") & (hm <= "15:29")
    df = df[keep].assign(day=dt[keep].dt.date)
    return df.rename(columns={"ts": "timestamp"}).reset_index(drop=True)


def _rows(df: pd.DataFrame) -> list[dict]:
    return df.rename(columns={"ts": "timestamp"}).to_dict("records")


def expiry_calendar(name: str) -> list[date]:
    path = ROOT / "options" / name / "days.csv"
    if not path.exists():
        return []
    t = pd.read_csv(path, parse_dates=["day"])
    return sorted(d.date() for d, e in zip(t.day, t.is_expiry) if str(e) == "True")


def next_expiry(calendar: list[date], day: date) -> date:
    """The weekly contract Dhan's expiryCode 1 rolls to on this session."""
    i = bisect_left(calendar, day)
    if i < len(calendar):
        return calendar[i]
    # current week, expiry not traded yet: assume the usual weekday
    e = calendar[-1] if calendar else day
    while e < day:
        e += timedelta(days=7)
    return e


def clean_chain(name: str, chain: pd.DataFrame, index_day: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Keep rows whose strike matches the real index's ATM + offset, that traded, one row per contract-minute."""
    step = STEP[name]
    ix = index_day[["timestamp", "close"]].rename(columns={"timestamp": "ts", "close": "ix"})
    d = chain.dropna(subset=["strike", "close"]).merge(ix, on="ts", how="inner")
    atm = (d.ix / step).round() * step
    ok = (d.strike - (atm + d.offset * step)).abs() <= step
    filler = (d.volume.fillna(0) == 0) & (d.high == d.low)
    good = (d[ok & ~filler].sort_values("volume", ascending=False)
            .drop_duplicates(["ts", "opt", "strike"]).sort_values(["ts", "opt", "strike"]).drop(columns="ix"))
    minutes = max(ix.ts.nunique(), 1)
    core = good[good.offset.between(-2, 2)].groupby(["opt", "offset"]).ts.nunique()
    core = core.reindex(pd.MultiIndex.from_product([["CE", "PE"], range(-2, 3)]), fill_value=0)
    quality = {
        "rows": len(chain), "wrong_strike": int((~ok).sum()), "no_trade": int(filler.sum()), "kept": len(good),
        "atm_cov": round(float(core.min()) / minutes, 3),
        "oi_cov": round(float((good.oi.fillna(0) > 0).mean()), 3) if len(good) else 0.0,
    }
    return good, quality


def _book(px: float, hs: float) -> tuple[float, float]:
    bid = math.floor((px - hs) / TICK + 1e-9) * TICK
    ask = math.ceil((px + hs) / TICK - 1e-9) * TICK
    return round(max(bid, TICK), 2), round(ask, 2)


def day_events(name: str, day: date, chain: pd.DataFrame, index: pd.DataFrame, vix: pd.DataFrame | None,
               expiry: date | None = None, mkt: Market | None = None):
    mkt = mkt or Market(HALF_SPREAD)
    index_exchange, fo_exchange = EXCHANGES[name]
    expiry = expiry or day
    code = expiry.strftime("%d%b%y").upper()
    events: list[tuple] = []
    for ts, d in bar_ticks(index[index.day == day].to_dict("records"), None, 0, day):
        events.append((ts, name, index_exchange, d))
    if vix is not None:
        for ts, d in bar_ticks(vix[vix.day == day].to_dict("records"), None, 0, day):
            events.append((ts, "INDIAVIX", "NSE_INDEX", d))
    options = []
    chain = chain.dropna(subset=["strike", "close"])
    for (opt, strike), g in chain.groupby(["opt", "strike"]):
        g = g.drop_duplicates("ts").sort_values("ts")
        symbol = f"{name}{code}{float(strike):g}{opt}"
        options.append({"symbol": symbol, "exchange": fo_exchange, "kind": "option", "underlying": name,
                        "strike": float(strike), "opt": opt, "lotsize": LOT[name], "tick_size": TICK})
        for ts, d in bar_ticks(_rows(g[["ts", "open", "high", "low", "close", "volume", "oi"]]), None, 0, day):
            d["bid"], d["ask"] = _book(d["ltp"], mkt.hs(name, d["ltp"]))
            events.append((ts, symbol, fo_exchange, d))
    events.sort(key=lambda e: e[0])
    meta = {
        "day": day.isoformat(),
        "underlyings": [{
            "name": name, "index_exchange": index_exchange, "fo_exchange": fo_exchange,
            "expiry": expiry.isoformat(), "is_expiry_day": expiry == day,
            "index": {"symbol": name, "exchange": index_exchange, "kind": "index", "underlying": name},
            "options": options,
        }],
        "vix": {"symbol": "INDIAVIX", "exchange": "NSE_INDEX", "kind": "vix"} if vix is not None else None,
    }
    return meta, events


def apply_overrides(cfg: Config, sets: list[str]) -> Config:
    """--set s1.min_drop_pct=60 style overrides, validated through the normal config builder."""
    data = yaml.safe_load(yaml.safe_dump(_to_dict(cfg)))
    for item in sets:
        key, value = item.split("=", 1)
        node = data
        parts = key.split(".")
        for p in parts[:-1]:
            node = node[p]
        if parts[-1] not in node:
            raise SystemExit(f"Unknown setting {key}")
        node[parts[-1]] = yaml.safe_load(value)
    return _build(Config, data, "")


def _to_dict(obj):
    import dataclasses

    if dataclasses.is_dataclass(obj):
        return {f.name: _to_dict(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, (list, tuple)):
        return [_to_dict(v) for v in obj]
    return obj


def _context(ts_ms: int, ix: pd.DataFrame, vx: pd.DataFrame | None) -> dict:
    """Market state from bars closed before the alert (no look-ahead)."""
    t = ts_ms // 1000 - 60
    seen = ix[ix.timestamp <= t]
    o = float(ix.open.iloc[0])
    now = float(seen.close.iloc[-1]) if len(seen) else o
    hi = float(seen.high.max()) if len(seen) else o
    lo = float(seen.low.min()) if len(seen) else o
    c = {"idx_move_pct": round((now / o - 1) * 100, 3), "idx_range_pct": round((hi - lo) / o * 100, 3)}
    if vx is not None and len(vx):
        vs = vx[vx.timestamp <= t]
        v0 = float(vx.open.iloc[0])
        v = float(vs.close.iloc[-1]) if len(vs) else v0
        c.update(vix=round(v, 2), vix_chg_pct=round((v / v0 - 1) * 100, 2))
    return c


def run(cfg: Config, underlyings: list[str], start: date | None, end: date | None, days: str = "expiry",
        any_day: bool = False, mkt: Market | None = None, min_cov: float = 0.9,
        require_oi: bool = True) -> tuple[pd.DataFrame, pd.DataFrame]:
    mkt = mkt or Market(HALF_SPREAD, R=cfg.risk.R)
    vix = _index("INDIAVIX") if (ROOT / "index" / "INDIAVIX").exists() else None
    vix_by_day = dict(tuple(vix.groupby("day"))) if vix is not None else {}
    rows, day_rows = [], []
    for name in underlyings:
        calendar = expiry_calendar(name)
        expiries = set(calendar)
        files = sorted((ROOT / "options" / name / "chain").glob("*.parquet"))
        picked = [(d, f) for d, f in ((date.fromisoformat(f.stem), f) for f in files)
                  if (start is None or d >= start) and (end is None or d <= end)
                  and (days == "all" or (d in expiries) == (days == "expiry"))]
        if not picked:
            print(f"{name}: no downloaded {days} sessions in range")
            continue
        index = _index(name)
        index_by_day = dict(tuple(index.groupby("day")))
        started = time.time()
        for i, (day, path) in enumerate(picked, 1):
            ix = index_by_day.get(day)
            info = {"underlying": name, "day": day, "expiry_day": day in expiries}
            if ix is None or len(ix) < 300:
                day_rows.append({**info, "status": "skipped: index bars missing"})
                continue
            good, q = clean_chain(name, pd.read_parquet(path), ix)
            info.update(q)
            if q["atm_cov"] < min_cov:
                day_rows.append({**info, "status": f"skipped: ATM coverage <{min_cov:.0%}"})
                continue
            if require_oi and q["oi_cov"] < 0.5:
                day_rows.append({**info, "status": "skipped: no open interest"})
                continue
            expiry = next_expiry(calendar, day)
            meta, events = day_events(name, day, good, index, vix, expiry, mkt)
            engine = run_offline(cfg, sim_events(meta, iter(events)), Notifier(), any_day=any_day)
            day_rows.append({**info, "status": "used", "alerts": len(engine.signals) if engine else 0})
            if engine is None or not engine.signals:
                continue
            paths = Paths(events)
            paper = {id(o.sig): o for o in engine.outcomes}
            vx = vix_by_day.get(day)
            for sig in engine.signals:
                ctx = _context(sig.ts, ix, vx)
                o = paper.get(id(sig))
                base = {
                    "underlying": name, "day": day, "year": day.year, "weekday": day.strftime("%a"),
                    "expiry_day": day in expiries, "dte": (expiry - day).days,
                    "time": to_dt(sig.ts).strftime("%H:%M"), "strategy": sig.strategy, "regime": sig.regime,
                    "symbol": sig.legs[0]["symbol"] if sig.strategy != "S3" else sig.legs[2]["symbol"],
                    "score": sig.score, "plan_lots": sig.plan.get("lots"),
                    "paper_r": round(o.realized(), 3) if o else None, **ctx,
                }
                for res in execute(sig, paths, mkt, day, engine.flatten_ms):
                    rows.append({**base, **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in res.items()}})
            if i % 25 == 0 or i == len(picked):
                print(f"{name}: {i}/{len(picked)} sessions ({time.time() - started:.0f}s), "
                      f"{sum(1 for r in rows if r['underlying'] == name)} trades so far")
    return pd.DataFrame(rows), pd.DataFrame(day_rows)


def _max_dd(r: pd.Series) -> float:
    eq = r.cumsum()
    return round(float((eq.cummax().clip(lower=0) - eq).max()), 1) if len(eq) else 0.0


def summarize(df: pd.DataFrame, by: list[str]) -> str:
    def agg(g: pd.DataFrame) -> pd.Series:
        f = g[g.net_r.notna()].sort_values(["day", "time"])
        wins, losses = f.net_r[f.net_r > 0].sum(), -f.net_r[f.net_r < 0].sum()
        return pd.Series({
            "alerts": len(g), "trades": len(f), "days": f.day.nunique(),
            "win%": round((f.net_r > 0).mean() * 100) if len(f) else np.nan,
            "avg_net_R": round(f.net_r.mean(), 3), "total_net_R": round(f.net_r.sum(), 1),
            "avg_gross_R": round(f.gross_r.mean(), 3), "avg_cost_R": round(f.cost_r.mean(), 3),
            "paper_R": round(f.paper_r.mean(), 3), "PF": round(wins / losses, 2) if losses > 0 else np.nan,
            "maxDD_R": _max_dd(f.net_r), "held_min": round(f.held_min.median(), 1),
        })

    return df.groupby(by, observed=True)[list(df.columns)].apply(agg).to_string()


def report(df: pd.DataFrame, days: pd.DataFrame) -> str:
    parts = []
    if not days.empty:
        d = days.assign(year=pd.to_datetime(days.day).dt.year)
        parts.append("== data used per underlying / year (status)\n"
                     + d.groupby(["underlying", "year", "status"]).size().unstack(fill_value=0).to_string())
    if df.empty:
        return "\n".join(parts + ["No alerts."])
    mins = pd.to_datetime(df.time, format="%H:%M")
    dates = pd.to_datetime(df.day)
    split = dates.sort_values().iloc[len(dates) // 2]
    df = df.assign(
        slot=pd.cut(mins.dt.hour * 60 + mins.dt.minute, [0, 600, 660, 720, 780, 840, 900, 960],
                    labels=["<10:00", "10-11", "11-12", "12-13", "13-14", "14-15", "15+"], right=False),
        half=np.where(dates < split, f"A <{split:%Y-%m-%d}", f"B >={split:%Y-%m-%d}"),
        vix_band=pd.cut(df.get("vix", pd.Series(np.nan, index=df.index)), [0, 12, 15, 20, 100],
                        labels=["<12", "12-15", "15-20", "20+"]),
        vix_move=pd.cut(df.get("vix_chg_pct", pd.Series(np.nan, index=df.index)), [-100, -3, 0, 3, 100],
                        labels=["<-3%", "-3..0%", "0..3%", ">3%"]),
        idx_move=pd.cut(df.idx_move_pct.abs(), [-0.01, 0.25, 0.5, 1.0, 100],
                        labels=["<0.25%", "0.25-0.5%", "0.5-1%", ">1%"]),
    )
    k = ["strategy", "policy"]
    parts += [
        "\nR = planned rupee risk (S1 entry-stop, S2 premium paid, S3 width-credit). net = after spread, slippage,"
        " latency and charges; paper_R = engine grade at alert price without costs. maxDD in R.",
        "\n== overall\n" + summarize(df, k),
        "\n== by underlying\n" + summarize(df, k + ["underlying"]),
        "\n== by year\n" + summarize(df, k + ["year"]),
        "\n== first half vs second half of the sample (out-of-sample check)\n" + summarize(df, k + ["half"]),
        "\n== by time of alert\n" + summarize(df, k + ["slot"]),
        "\n== by regime at alert\n" + summarize(df, k + ["regime"]),
        "\n== by India VIX level at alert\n" + summarize(df, k + ["vix_band"]),
        "\n== by India VIX change since open\n" + summarize(df, k + ["vix_move"]),
        "\n== by index move since open (abs)\n" + summarize(df, k + ["idx_move"]),
        "\n== exit reasons\n" + df.groupby(k + ["exit_reason"]).size().unstack(fill_value=0).to_string(),
    ]
    if df.expiry_day.nunique() > 1:
        parts.insert(4, "\n== expiry day vs other days\n" + summarize(df, k + ["expiry_day"]))
    return "\n".join(parts)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config/dhan.yaml")
    p.add_argument("--underlyings", nargs="+", default=list(EXCHANGES))
    p.add_argument("--from", dest="start", type=date.fromisoformat)
    p.add_argument("--to", dest="end", type=date.fromisoformat)
    p.add_argument("--days", choices=["expiry", "other", "all"], default="expiry",
                   help="which downloaded sessions to replay (expiry calendar from days.csv)")
    p.add_argument("--any-day", action="store_true", help="let expiry-day setups fire on non-expiry sessions")
    p.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                   help="override a setting for this run, e.g. --set s1.min_drop_pct=60 (repeatable)")
    p.add_argument("--spread-mult", type=float, default=1.0, help="scale the half-spread model (stress test)")
    p.add_argument("--latency-sec", type=float, default=15, help="seconds from alert to order")
    p.add_argument("--slip-ticks", type=int, default=1, help="extra ticks paid per order")
    p.add_argument("--min-coverage", type=float, default=0.9, help="skip days with less ATM+-2 minute coverage")
    p.add_argument("--allow-no-oi", action="store_true", help="also replay days without open interest")
    p.add_argument("--out", default=None, help="CSV of every trade (default data/backtest/<time>.csv)")
    args = p.parse_args()

    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    cfg = apply_overrides(load_config(args.config), args.set)
    mkt = Market({k: v * args.spread_mult for k, v in HALF_SPREAD.items()}, int(args.latency_sec * 1000),
                 args.slip_ticks, cfg.risk.R)
    df, days = run(cfg, args.underlyings, args.start, args.end, args.days, args.any_day, mkt,
                   args.min_coverage, not args.allow_no_oi)
    out = Path(args.out or f"data/backtest/{time.strftime('%Y%m%d-%H%M%S')}.csv")
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False)
    days.to_csv(out.with_name(out.stem + "_days.csv"), index=False)
    text = report(df, days)
    head = (f"{len(df)} trade rows -> {out}\nsettings: spread x{args.spread_mult}, latency {args.latency_sec}s, "
            f"slip {args.slip_ticks} tick, R Rs {cfg.risk.R:g}, days={args.days}, any_day={args.any_day}"
            + (f"\noverrides: {', '.join(args.set)}" if args.set else ""))
    out.with_suffix(".txt").write_text(head + "\n\n" + text, encoding="utf-8")
    print(head)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
