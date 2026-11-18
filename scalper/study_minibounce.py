"""Expiry-day fake bounces: can a 1-minute trigger catch the 5-20 minute pops in an option that collapsed all day?

  .venv\\Scripts\\python.exe -m scalper.study_minibounce [--lots 5]

Universe: every stored expiry day (NIFTY 2021-26, SENSEX from 2024-04), puts and calls alike: any contract
trading at <= half its 09:20 price with premium >= Rs 5, triggers from 09:45 to 15:00.
Triggers (on the close of a 1-minute bar):
  every 5 min    baseline - buy the fallen option at any time
  flush          down >= 25% in the last 10 minutes
  turn           fresh day low in the last 3 bars, then a green bar closing above the previous bar's high
  flush+turn     turn within 5 minutes of a flush
  buy burst      green bar on >= 3x the contract's 30-minute average volume
  writers cover  green bar while near-the-money OI of that side (ATM+-2, same strikes) fell >= 1% in 5 min
  turn+cover     turn while writers cover
Entry on the NEXT bar's open at the ask (+1 tick); exits on 1-minute bars: limit target sold once the bid
(high - half-spread) reaches it, stop at the bar open if it gapped (stop first if both in one bar), time exit
at the bid. Charges for `lots` lots. Per side one trade at a time (20-min cooldown), the triggered contract
nearest the money. Each day counts once; train before 2024-07-01, test after.
"""

from __future__ import annotations

import argparse
from datetime import date

import numpy as np
import pandas as pd

from scalper.backtest import EXCHANGES, HALF_SPREAD, LOT, ROOT, STEP, TICK, _index, clean_chain, expiry_calendar
from scalper.execution import charges

SPLIT = date(2024, 7, 1)
N, START, LAST, CLOSE = 375, 30, 345, 365          # minutes after 09:15: 15:29, 09:45, 15:00, 15:20
EXITS = [("5 min", None, None, 5), ("10 min", None, None, 10), ("15 min", None, None, 15),
         ("+10/-10 10m", .10, .10, 10), ("+20/-10 15m", .20, .10, 15), ("+20/-15 20m", .20, .15, 20)]
TRIGGERS = ("every 5 min", "flush", "turn", "flush+turn", "buy burst", "writers cover", "turn+cover")
COOLDOWN = 20


def _hs(name: str, px: float) -> float:
    return max(TICK, px * HALF_SPREAD[name])


def _shift(a: np.ndarray, k: int) -> np.ndarray:
    out = np.full_like(a, np.nan)
    out[k:] = a[:-k]
    return out


def triggers(o, h, l, c, v, oi, ks: list[float], spot: np.ndarray, step: int) -> dict[str, np.ndarray]:
    with np.errstate(invalid="ignore", divide="ignore"):
        fallen = (c <= 0.5 * c[5]) & (c >= 5)
        fallen[:START] = False
        fallen[LAST + 1:] = False
        green = c > o
        flush = c / _shift(c, 10) <= 0.75
        runmin = pd.DataFrame(l).cummin().to_numpy()
        recent_low = np.fmin(np.fmin(_shift(l, 1), _shift(l, 2)), _shift(l, 3))
        turn = (recent_low <= _shift(runmin, 1)) & (c > _shift(h, 1)) & green
        flushed = pd.DataFrame(flush.astype(float)).rolling(6, min_periods=1).max().to_numpy() > 0
        vavg = pd.DataFrame(v).shift(1).rolling(30, min_periods=10).mean().to_numpy()
        burst = (v >= 3 * vavg) & (v > 0) & green
    col = {k: j for j, k in enumerate(ks)}
    cover = np.zeros(N, bool)
    for i in range(5, N):
        if np.isnan(spot[i]):
            continue
        atm = round(spot[i] / step) * step
        js = [col[k] for k in atm + step * np.arange(-2, 3) if k in col]
        now, before = oi[i, js], oi[i - 5, js]
        ok = ~(np.isnan(now) | np.isnan(before))
        if ok.sum() >= 3 and before[ok].sum() > 0:
            cover[i] = now[ok].sum() / before[ok].sum() - 1 <= -0.01
    every = np.zeros_like(fallen)
    every[::5] = True
    raw = {"every 5 min": every, "flush": flush, "turn": turn, "flush+turn": turn & flushed, "buy burst": burst,
           "writers cover": cover[:, None] & green, "turn+cover": turn & cover[:, None]}
    return {k: fallen & x for k, x in raw.items()}


def simulate(name: str, day: date, o, h, l, c, i: int, j: int, exit_: tuple, qty: int) -> float | None:
    _, tgt, stp, mins = exit_
    if i + 1 > CLOSE or np.isnan(o[i + 1, j]):
        return None
    entry = o[i + 1, j] + _hs(name, o[i + 1, j]) + TICK
    out = last = None
    for b in range(i + 1, min(i + mins, CLOSE) + 1):
        if np.isnan(o[b, j]):
            continue
        if stp and l[b, j] <= entry * (1 - stp):
            px = min(o[b, j], entry * (1 - stp))
            out = max(px - _hs(name, px) - TICK, 0.0)
            break
        if tgt and h[b, j] - _hs(name, h[b, j]) >= entry * (1 + tgt):
            out = entry * (1 + tgt)
            break
        last = c[b, j]
    if out is None:
        if last is None:
            return None
        out = max(last - _hs(name, last) - TICK, 0.0)
    fee = charges(EXCHANGES[name][1], day, entry * qty, out * qty, 2)
    return out / entry - 1 - fee / (entry * qty)


def _pop(name: str, o, h, i: int, j: int, pct: float, mins: int) -> bool:
    entry = o[i + 1, j] + _hs(name, o[i + 1, j]) + TICK
    hi = h[i + 1:min(i + mins, CLOSE) + 1, j]
    hi = hi[~np.isnan(hi)]
    return bool(len(hi)) and float(np.max(hi - np.maximum(TICK, hi * HALF_SPREAD[name]))) >= entry * (1 + pct)


def study(name: str, lots: int) -> pd.DataFrame:
    ix = _index(name)
    days = [d for d in expiry_calendar(name) if name != "SENSEX" or d >= date(2024, 4, 1)]
    qty, step, rows = lots * LOT[name], STEP[name], []
    for d in days:
        f = ROOT / "options" / name / "chain" / f"{d}.parquet"
        ixd = ix[ix.day == d]
        if not f.exists() or len(ixd) < 300:
            continue
        ch, q = clean_chain(name, pd.read_parquet(f), ixd)
        if q["atm_cov"] < 0.9:
            continue
        ch = ch.assign(m=((ch.ts + 19800) % 86400) // 60 - 555)
        ch = ch[(ch.m >= 0) & (ch.m < N)]
        spot = ch.groupby("m").spot.first().reindex(range(N)).ffill().to_numpy(float)
        for side in ("PE", "CE"):
            s = ch[ch.opt == side]
            ks = sorted(s.strike.unique())
            g = {k: s.pivot(index="m", columns="strike", values=k).reindex(index=range(N), columns=ks).to_numpy(float)
                 for k in ("open", "high", "low", "close", "volume", "oi")}
            o, h, l, c = g["open"], g["high"], g["low"], g["close"]
            karr = np.array(ks)
            for tname, mask in triggers(o, h, l, c, g["volume"], g["oi"], ks, spot, step).items():
                nxt = 0
                for i in np.flatnonzero(mask.any(axis=1)):
                    if i < nxt:
                        continue
                    js = np.flatnonzero(mask[i])
                    j = int(js[np.argmin(np.abs(karr[js] - spot[i]))])
                    res = [simulate(name, d, o, h, l, c, i, j, e, qty) for e in EXITS]
                    if res[0] is None:
                        continue
                    rows.append({"day": d, "side": side, "trigger": tname, "minute": i, "px": c[i, j],
                                 "pop15": _pop(name, o, h, i, j, .15, 20),
                                 **{e[0]: r for e, r in zip(EXITS, res)}})
                    nxt = i + COOLDOWN
    return pd.DataFrame(rows)


def _t(x: pd.Series) -> float:
    return float(x.mean() / (x.std() / np.sqrt(len(x)))) if len(x) > 2 and x.std() > 0 else float("nan")


def report(name: str, df: pd.DataFrame, lots: int) -> str:
    out = [f"\n######## {name}: fallen options on expiry days, {lots} lots, avg return per trade (day-weighted)"]
    if df.empty:
        return out[0] + "\nno trades"
    df = df.assign(period=np.where(df.day < SPLIT, "train", "test"))
    for t in TRIGGERS:
        sub = df[df.trigger == t]
        tr, te = sub[sub.period == "train"], sub[sub.period == "test"]
        out.append(f"{t}: trades train {len(tr)} / test {len(te)} | median premium Rs {sub.px.median():.1f} | "
                   f"bid reached +15% within 20 min: {100 * sub.pop15.mean():.0f}%")
        for e, *_ in EXITS:
            cells = []
            for x in (tr, te):
                daily = x.groupby("day")[e].mean()
                cells.append(f"{daily.mean():+6.1%} t{_t(daily):+5.1f} win {100 * (x[e] > 0).mean():3.0f}%")
            out.append(f"    {e:12s} train {cells[0]} | test {cells[1]}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="scalper.study_minibounce", description=__doc__.split("\n")[0])
    p.add_argument("--lots", type=int, default=5)
    args = p.parse_args(argv)
    for name in ("NIFTY", "SENSEX"):
        df = study(name, args.lots)
        df.to_csv(f"data/research/minibounce_{name}.csv", index=False)
        print(report(name, df, args.lots))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
