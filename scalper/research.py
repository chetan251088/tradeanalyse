"""Strategy research on 1-minute bars of the downloaded expiry days.

  .venv\\Scripts\\python.exe -m scalper.research buy            # ETS baseline, train/test/stress
  .venv\\Scripts\\python.exe -m scalper.research sell --grid    # ETH small grid on the train period

ETS  Expiry Trend Scalper (option buying): on expiry days, trade with the index trend, never against it.
     Trend = index beyond the 09:15-09:29 opening range and on the right side of a rising/falling 20-bar EMA.
     Entry = a pullback that touched the EMA zone, then a bar closing beyond the previous bar (resumption).
     Buys the ATM (or 1 ITM) option in the trend direction. Premium stop, 1R break-even, 2R limit target,
     time stop. Variant mode=breakout: after 13:30, a close beyond the last 60 minutes' range on an
     expanding bar (the gamma burst S2 was chasing), same exits.
ETH  Expiry Theta Harvester (option selling): on expiry days sell the ATM straddle with wings (iron fly)
     at a fixed time, stop each short leg at +sl% of its premium (buy it back with its wing), move the
     other leg's stop to cost, close everything at 15:15.

Execution: decisions on bar closes; orders fill at the next bar's open (about a minute of latency) at the
ask/bid (half-spread model of scalper.backtest) plus one tick. Premium stops are stop-market orders
(fill at the stop, or at the open if the bar gapped through it); targets are limits that need the bid to
reach them; when a bar touches both, the stop is assumed first. Charges from scalper.execution.
Days pass the same cleaning and quality gate as scalper.backtest. R = planned rupee risk per trade.
"""

from __future__ import annotations

import argparse
import io
import itertools
import pickle
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

from scalper.backtest import EXCHANGES, HALF_SPREAD, LOT, ROOT, STEP, _index, clean_chain, expiry_calendar
from scalper.execution import charges

OUT = Path("data/research")
TICK = 0.05
IST = ZoneInfo("Asia/Kolkata")
TRAIN_END = date(2024, 7, 1)


@dataclass
class Day:
    name: str
    day: date
    ts: np.ndarray
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    vix: np.ndarray
    opt: dict

    @property
    def step(self) -> int:
        return STEP[self.name]

    def minute(self, hhmm: str) -> int:
        hh, mm = map(int, hhmm.split(":"))
        t = datetime.combine(self.day, time(hh, mm), IST).timestamp()
        return int(np.searchsorted(self.ts, t))


class _CacheReader(pickle.Unpickler):
    """Our own cache file; Day may have been pickled from __main__ (python -m scalper.research)."""

    def find_class(self, module: str, name: str):
        return Day if name == "Day" else super().find_class(module, name)


def load_days(name: str) -> list[Day]:
    """Expiry days that pass the backtest's cleaning and quality gate (cached)."""
    cache = OUT / f"cache_{name}_expiry.pkl"
    if cache.exists():
        return _CacheReader(io.BytesIO(cache.read_bytes())).load()
    index = _index(name)
    by_day = dict(tuple(index.groupby("day")))
    vix = _index("INDIAVIX")
    vix_by_day = dict(tuple(vix.groupby("day")))
    expiries = set(expiry_calendar(name))
    days = []
    for f in sorted((ROOT / "options" / name / "chain").glob("*.parquet")):
        d = date.fromisoformat(f.stem)
        ix = by_day.get(d)
        if d not in expiries or ix is None or len(ix) < 300:
            continue
        good, q = clean_chain(name, pd.read_parquet(f), ix)
        if q["atm_cov"] < 0.9 or q["oi_cov"] < 0.5:
            continue
        ts = ix.timestamp.to_numpy(np.int64)
        v = np.full(len(ts), np.nan)
        vx = vix_by_day.get(d)
        if vx is not None:
            pos = np.searchsorted(ts, vx.timestamp.to_numpy())
            ok = pos < len(ts)
            v[pos[ok]] = vx.close.to_numpy()[ok]
            v = pd.Series(v).ffill().to_numpy()
        opt = {}
        for (side, strike), g in good.groupby(["opt", "strike"]):
            a = np.full((len(ts), 4), np.nan)
            gts = g.ts.to_numpy()
            pos = np.searchsorted(ts, gts)
            ok = (pos < len(ts)) & (ts[np.minimum(pos, len(ts) - 1)] == gts)
            a[pos[ok]] = g[["open", "high", "low", "close"]].to_numpy(float)[ok]
            opt[(side, float(strike))] = a
        days.append(Day(name, d, ts, *(ix[k].to_numpy(float) for k in ("open", "high", "low", "close")), v, opt))
    OUT.mkdir(parents=True, exist_ok=True)
    cache.write_bytes(pickle.dumps(days))
    return days


@dataclass(frozen=True)
class Cost:
    spread_mult: float = 1.0
    slip_ticks: int = 1
    delay: int = 1  # bars from the decision close to the fill bar's open

    def hs(self, name: str, px: float) -> float:
        return max(TICK, px * HALF_SPREAD[name] * self.spread_mult)

    @property
    def slip(self) -> float:
        return self.slip_ticks * TICK


def _first_open(a: np.ndarray, i: int, look: int = 3) -> tuple[int, float] | None:
    for j in range(i, min(i + look, len(a))):
        if not np.isnan(a[j, 0]):
            return j, float(a[j, 0])
    return None


def _exit_px(a: np.ndarray, k: int) -> float:
    """Price for a market exit decided at the close of bar k: next bar's open, else the last known close."""
    nxt = _first_open(a, k + 1, 2)
    if nxt:
        return nxt[1]
    closes = a[: k + 1, 3]
    closes = closes[~np.isnan(closes)]
    return float(closes[-1]) if len(closes) else np.nan


def _context(day: Day, i: int) -> dict:
    v0 = day.vix[~np.isnan(day.vix)][:1]
    vix = float(day.vix[i]) if not np.isnan(day.vix[i]) else np.nan
    return {"idx_move_pct": round((day.c[i] / day.o[0] - 1) * 100, 3), "vix": round(vix, 2),
            "vix_chg_pct": round((vix / v0[0] - 1) * 100, 2) if len(v0) else np.nan}


# ---------------------------------------------------------------- ETS: option buying ----------
@dataclass(frozen=True)
class BuyP:
    mode: str = "pullback"  # pullback | breakout
    start: str = "09:30"
    last_entry: str = "14:45"
    exit: str = "15:15"
    or_min: int = 15
    ema: int = 20
    pull_bars: int = 5
    touch_pct: float = 0.03
    lookback: int = 60
    expand: float = 1.5
    itm: int = 0
    stop_pct: float = 20.0
    target_r: float = 2.0
    be_at_r: float = 1.0
    max_hold: int = 20
    max_trades: int = 3
    max_losses: int = 2
    cooldown: int = 5
    R: float = 1500.0


def _buy_signal(day: Day, p: BuyP, i: int, ema: np.ndarray, orh: float, orl: float) -> int:
    c, h, l = day.c, day.h, day.l
    if p.mode == "breakout":
        if i < p.lookback + 1:
            return 0
        hi, lo = h[i - p.lookback:i].max(), l[i - p.lookback:i].min()
        avg = (h[i - 30:i] - l[i - 30:i]).mean()
        if avg <= 0 or h[i] - l[i] < p.expand * avg:
            return 0
        return 1 if c[i] > hi else -1 if c[i] < lo else 0
    a, b = i - p.pull_bars, i
    zone = ema[a:b] * p.touch_pct / 100
    if c[i] > orh and c[i] > ema[i] > ema[i - 5] and (l[a:b] <= ema[a:b] + zone).any() and c[i] > h[i - 1]:
        return 1
    if c[i] < orl and c[i] < ema[i] < ema[i - 5] and (h[a:b] >= ema[a:b] - zone).any() and c[i] < l[i - 1]:
        return -1
    return 0


def _buy_trade(day: Day, p: BuyP, cost: Cost, sig: int, i: int, end_entry: int, xe: int) -> dict | None:
    step = day.step
    atm = round(day.c[i] / step) * step
    strike = atm - p.itm * step if sig > 0 else atm + p.itm * step
    side = "CE" if sig > 0 else "PE"
    a = day.opt.get((side, float(strike)))
    if a is None:
        return None
    f = _first_open(a, i + cost.delay)
    if f is None or f[0] > end_entry + 3:
        return None
    j, px = f
    hs = lambda x: cost.hs(day.name, x)  # noqa: E731
    entry = px + hs(px) + cost.slip
    stop = entry * (1 - p.stop_pct / 100)
    risk = entry - stop
    target = entry + p.target_r * risk
    be = entry + p.be_at_r * risk if p.be_at_r else None
    end = min(j + p.max_hold, xe)
    exit_px, reason, k = None, "", j
    peak = entry
    for k in range(j, len(a)):
        o, hi, lo, cl = a[k]
        if not np.isnan(o):
            if lo <= stop:
                trig = stop if k == j or o > stop else o
                exit_px, reason = trig - hs(trig) - cost.slip, "breakeven" if stop >= entry else "stop"
                break
            if hi - hs(hi) >= target:
                exit_px, reason = target, "target"
                break
            peak = max(peak, hi - hs(hi))
            if be and peak >= be:
                stop = max(stop, entry)
        if k >= end:
            px_out = _exit_px(a, k)
            exit_px, reason = px_out - hs(px_out) - cost.slip, "time" if k < xe else "15:15"
            break
    if exit_px is None or np.isnan(exit_px):
        return None
    exit_px = max(exit_px, TICK)
    qty = p.R / risk
    gross = qty * (exit_px - entry)
    cst = charges(EXCHANGES[day.name][1], day.day, qty * entry, qty * exit_px, 2)
    return {"underlying": day.name, "day": day.day, "time": _hhmm(day.ts[j]), "side": side, "strike": strike,
            "entry": round(entry, 2), "exit": round(exit_px, 2), "reason": reason, "held": k - j,
            "gross_r": gross / p.R, "cost_r": cst / p.R, "net_r": (gross - cst) / p.R,
            "mfe_r": (peak - entry) / risk, "_exit_i": k, **_context(day, i)}


def run_buy(day: Day, p: BuyP, cost: Cost) -> list[dict]:
    ema = pd.Series(day.c).ewm(span=p.ema, adjust=False).mean().to_numpy()
    orh, orl = day.h[:p.or_min].max(), day.l[:p.or_min].min()
    i = max(day.minute(p.start), p.ema, p.pull_bars + 5)
    le, xe = day.minute(p.last_entry), min(day.minute(p.exit), len(day.ts) - 2)
    trades, losses = [], 0
    while i < le and len(trades) < p.max_trades and losses < p.max_losses:
        sig = _buy_signal(day, p, i, ema, orh, orl)
        t = _buy_trade(day, p, cost, sig, i, le, xe) if sig else None
        if t is None:
            i += 1
            continue
        trades.append(t)
        losses += t["net_r"] < 0
        i = t.pop("_exit_i") + p.cooldown
    return trades


# ---------------------------------------------------------------- ETH: option selling ---------
@dataclass(frozen=True)
class SellP:
    entry: str = "10:00"
    exit: str = "15:15"
    offset: int = 0
    wing: int = 4
    mode: str = "legs"  # legs: per-leg stops | fly: combined take-profit / stop on the whole position
    sl_pct: float = 40.0
    sl_to_cost: bool = True
    close_wing: bool = True  # sell the wing with its stopped short leg; False keeps it as a hedge to the exit
    tp_pct: float = 50.0
    stop_x: float = 1.0
    max_move_pct: float = 99.0
    max_vix_chg: float = 99.0
    last_entry: str = ""  # if set, enter at the first minute from `entry` to here where the filters pass
    range_mult: float = 99.0  # last 60 minutes' index range <= range_mult x ATM straddle
    decay_pct: float = 99.0  # ATM straddle change over the last 30 minutes <= decay_pct (%)
    R: float = 5000.0


def _sell_ready(day: Day, p: SellP, e: int) -> bool:
    ctx = _context(day, e - 1)
    if abs(ctx["idx_move_pct"]) > p.max_move_pct or (ctx["vix_chg_pct"] or 0) > p.max_vix_chg:
        return False
    if p.range_mult >= 99 and p.decay_pct >= 99:
        return True
    if e < 62:
        return False
    atm = round(day.c[e - 1] / day.step) * day.step
    ce, pe = day.opt.get(("CE", float(atm))), day.opt.get(("PE", float(atm)))
    if ce is None or pe is None:
        return False
    now, before = ce[e - 1, 3] + pe[e - 1, 3], ce[e - 31, 3] + pe[e - 31, 3]
    if np.isnan(now) or np.isnan(before) or now <= 0:
        return False
    rng = day.h[e - 61:e - 1].max() - day.l[e - 61:e - 1].min()
    return rng <= p.range_mult * now and (now / before - 1) * 100 <= p.decay_pct


def run_sell(day: Day, p: SellP, cost: Cost) -> list[dict]:
    step, name = day.step, day.name
    e, x = day.minute(p.entry), min(day.minute(p.exit), len(day.ts) - 1)
    last = day.minute(p.last_entry) if p.last_entry else e
    while e <= last and not _sell_ready(day, p, e):
        e += 1
    if e > last or e < 1 or e >= x:
        return []
    ctx = _context(day, e - 1)
    atm = round(day.c[e - 1] / step) * step
    legs = {  # name: (key, +1 long / -1 short)
        "sc": (("CE", float(atm + p.offset * step)), -1), "sp": (("PE", float(atm - p.offset * step)), -1),
        "wc": (("CE", float(atm + (p.offset + p.wing) * step)), 1),
        "wp": (("PE", float(atm - (p.offset + p.wing) * step)), 1),
    }
    arr, fill = {}, {}
    for leg, (key, sign) in legs.items():
        a = day.opt.get(key)
        f = _first_open(a, e) if a is not None else None
        if f is None:
            return []
        arr[leg] = a
        px = f[1]
        fill[leg] = px - cost.hs(name, px) - cost.slip if sign < 0 else px + cost.hs(name, px) + cost.slip
    credit = fill["sc"] + fill["sp"] - fill["wc"] - fill["wp"]
    width = p.wing * step
    if credit <= 0 or credit >= width:
        return []
    risk = width - credit
    out: dict[str, float] = {}
    stopped = []
    if p.mode == "fly":
        last = {leg: fill_px for leg, fill_px in ((g, arr[g][_first_open(arr[g], e)[0], 0]) for g in legs)}
        for k in range(e, x):
            for leg in legs:
                if not np.isnan(arr[leg][k, 3]):
                    last[leg] = arr[leg][k, 3]
            close_cost = sum(last[g] + cost.hs(name, last[g]) if legs[g][1] < 0 else -(last[g] - cost.hs(name, last[g]))
                             for g in legs)
            pnl_now = credit - close_cost
            if close_cost <= credit * (1 - p.tp_pct / 100) or pnl_now <= -p.stop_x * credit:
                stopped.append("target" if pnl_now > 0 else "stop")
                x = k + 1
                break
    else:
        stop = {s: fill[s] * (1 + p.sl_pct / 100) for s in ("sc", "sp")}
        wing_of = {"sc": "wc", "sp": "wp"}
        for k in range(e, x):
            for s in ("sc", "sp"):
                if s in out:
                    continue
                o, hi, lo, cl = arr[s][k]
                if np.isnan(o) or hi < stop[s]:
                    continue
                trig = stop[s] if k == e or o < stop[s] else o
                out[s] = trig + cost.hs(name, trig) + cost.slip
                w = wing_of[s]
                if p.close_wing:
                    # the wing goes out at its price when the stop bar opened; that bar's close can be a bad print
                    wpx = arr[w][k, 0] if not np.isnan(arr[w][k, 0]) else _exit_px(arr[w], k - 1)
                    out[w] = max(wpx - cost.hs(name, wpx) - cost.slip, 0.0)
                stopped.append(s)
                other = "sp" if s == "sc" else "sc"
                if p.sl_to_cost and other not in out:
                    stop[other] = min(stop[other], fill[other])
            if len(stopped) == 2:
                break
    for leg, (_, sign) in legs.items():
        if leg not in out:
            px = arr[leg][x, 0] if x < len(day.ts) and not np.isnan(arr[leg][x, 0]) else _exit_px(arr[leg], x - 1)
            out[leg] = px + cost.hs(name, px) + cost.slip if sign < 0 else max(px - cost.hs(name, px) - cost.slip, 0.0)
    pnl = sum((fill[s] - out[s]) for s in ("sc", "sp")) + sum((out[w] - fill[w]) for w in ("wc", "wp"))
    qty = p.R / risk
    buys = fill["wc"] + fill["wp"] + out["sc"] + out["sp"]
    sells = fill["sc"] + fill["sp"] + out["wc"] + out["wp"]
    cst = charges(EXCHANGES[name][1], day.day, qty * buys, qty * sells, 8)
    lot = LOT[name]
    lot_cost = charges(EXCHANGES[name][1], day.day, lot * buys, lot * sells, 8)
    return [{"underlying": name, "day": day.day, "time": _hhmm(day.ts[e]), "atm": atm, "credit": round(credit, 2),
             "width": width, "stops": "+".join(stopped) or "none", "gross_r": qty * pnl / p.R,
             "cost_r": cst / p.R, "net_r": (qty * pnl - cst) / p.R, "rs_per_lot": round(lot * pnl - lot_cost),
             **ctx}]


# ---------------------------------------------------------------- evaluation ------------------
def _hhmm(ts: int) -> str:
    return datetime.fromtimestamp(int(ts), IST).strftime("%H:%M")


def run_all(days: list[Day], runner, p, cost: Cost = Cost()) -> pd.DataFrame:
    rows = [t for d in days for t in runner(d, p, cost)]
    df = pd.DataFrame(rows)
    if not df.empty:
        df["period"] = np.where(pd.to_datetime(df.day) < pd.Timestamp(TRAIN_END), "train", "test")
        df["year"] = pd.to_datetime(df.day).dt.year
    return df


def stats(r: pd.Series) -> dict:
    r = r.dropna()
    if r.empty:
        return {"n": 0}
    eq = r.cumsum()
    loss = -r[r < 0].sum()
    return {"n": len(r), "win%": round((r > 0).mean() * 100), "avg_R": round(r.mean(), 3),
            "total_R": round(r.sum(), 1), "PF": round(r[r > 0].sum() / loss, 2) if loss > 0 else np.inf,
            "t": round(r.mean() / (r.std(ddof=1) / np.sqrt(len(r))), 2) if len(r) > 2 else np.nan,
            "maxDD_R": round(float((eq.cummax().clip(lower=0) - eq).max()), 1)}


def table(df: pd.DataFrame, by: list[str]) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    return pd.DataFrame({k if isinstance(k, tuple) else (k,): stats(g.sort_values(["day", "time"]).net_r)
                         for k, g in df.groupby(by)}).T


def grid(days: list[Day], runner, base, space: dict, cost: Cost = Cost()) -> pd.DataFrame:
    rows = []
    for values in itertools.product(*space.values()):
        p = replace(base, **dict(zip(space, values)))
        df = run_all(days, runner, p, cost)
        for period in ("train", "test"):
            s = stats(df[df.period == period].sort_values(["day", "time"]).net_r) if not df.empty else {"n": 0}
            rows.append({**dict(zip(space, values)), "period": period, **s})
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("which", choices=["buy", "sell", "build"])
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE")
    args = ap.parse_args()
    days = [d for n in ("NIFTY", "SENSEX") for d in load_days(n)]
    print(f"{len(days)} expiry days loaded "
          f"({sum(d.day < TRAIN_END for d in days)} train < {TRAIN_END}, {sum(d.day >= TRAIN_END for d in days)} test)")
    if args.which == "build":
        return 0
    runner, base = (run_buy, BuyP()) if args.which == "buy" else (run_sell, SellP())
    over = {k: type(getattr(base, k))(v) if not isinstance(getattr(base, k), bool) else v.lower() == "true"
            for k, v in (s.split("=", 1) for s in args.set)}
    p = replace(base, **over)
    print(asdict(p))
    for label, cost in (("base", Cost()), ("stress 2x spread, 2-bar delay, 2 ticks", Cost(2.0, 2, 2))):
        df = run_all(days, runner, p, cost)
        print(f"\n== {label}\n" + table(df, ["period"]).to_string())
        if label == "base" and not df.empty:
            print("\n" + table(df, ["period", "underlying"]).to_string())
            print("\n" + table(df, ["year"]).to_string())
            OUT.mkdir(parents=True, exist_ok=True)
            df.to_csv(OUT / f"{args.which}_trades.csv", index=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
