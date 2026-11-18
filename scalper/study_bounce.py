"""Expiry-day "put sellers won't win without a fight" test: do puts that collapsed during a rally bounce late in the day?

  .venv\\Scripts\\python.exe -m scalper.study_bounce [--t0 13:30] [--drop 0.6] [--up 0.003]

For every stored expiry day (NIFTY all, SENSEX from 2024-04 where its chains are clean):
  A. the index after t0 on days it is already up >= `up` from the open: reversal or continuation until 15:25?
  B. every put that lost >= `drop` of its 09:20 price by t0 (and still costs >= Rs 1): buy at the ask at t0 and
     - hold to 15:20, sell at the bid, or
     - plan: target 2x / stop -50% (stop checked first when both happen in one minute).
Grouped by distance from spot at t0 (OTM, near the money, in the money); train before 2024-07-01, test after.
Each day counts once (strikes on the same day are averaged), so one big day cannot carry the result.
"""

from __future__ import annotations

import argparse
from datetime import date

import numpy as np
import pandas as pd

from scalper.backtest import HALF_SPREAD, ROOT, STEP, TICK, _index, clean_chain, expiry_calendar

SPLIT = date(2024, 7, 1)


def _hm(ts: pd.Series) -> pd.Series:
    return pd.to_datetime(ts, unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.strftime("%H:%M")


def _sell(name: str, px: float) -> float:
    return max(px - max(TICK, px * HALF_SPREAD[name]) - TICK, 0.0)


def study(name: str, t0: str, drop_min: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    ix = _index(name)
    ix["hm"] = _hm(ix.timestamp)
    days = [d for d in expiry_calendar(name) if name != "SENSEX" or d >= date(2024, 4, 1)]
    irows, prows = [], []
    for d in days:
        f = ROOT / "options" / name / "chain" / f"{d}.parquet"
        ixd = ix[ix.day == d]
        a, e = ixd[ixd.hm == t0], ixd[ixd.hm == "15:25"]
        if not f.exists() or len(ixd) < 300 or a.empty or e.empty:
            continue
        s0 = float(a.close.iloc[0])
        rest = ixd[(ixd.hm > t0) & (ixd.hm <= "15:25")]
        move = s0 / float(ixd.open.iloc[0]) - 1
        irows.append({"day": d, "move": move, "end": float(e.close.iloc[0]) / s0 - 1,
                      "pullback": float(rest.low.min()) / s0 - 1, "further": float(rest.high.max()) / s0 - 1})
        ch, q = clean_chain(name, pd.read_parquet(f), ixd)
        if q["atm_cov"] < 0.9:
            continue
        ch = ch[ch.opt == "PE"].assign(hm=lambda x: _hm(x.ts))
        for k, g in ch.groupby("strike"):
            pa, pb = g[g.hm == "09:20"].close, g[g.hm == t0].close
            if pa.empty or pb.empty or float(pb.iloc[0]) < 1 or float(pa.iloc[0]) <= 0:
                continue
            pa, pb = float(pa.iloc[0]), float(pb.iloc[0])
            if 1 - pb / pa < drop_min:
                continue
            path = g[(g.hm > t0) & (g.hm <= "15:20")].sort_values("ts")
            if len(path) < 60:
                continue
            entry = pb + max(TICK, pb * HALF_SPREAD[name]) + TICK
            hold = _sell(name, float(path.close.iloc[-1])) / entry - 1
            plan = hold
            for r in path.itertuples():
                if r.low <= 0.5 * entry:
                    plan = _sell(name, min(r.open, 0.5 * entry)) / entry - 1
                    break
                if r.high >= 2 * entry:
                    plan = _sell(name, 2 * entry) / entry - 1
                    break
            prows.append({"day": d, "strike": k, "mny": (k - s0) / STEP[name], "drop": 1 - pb / pa, "p920": pa,
                          "entry": entry, "max_mult": float(path.high.max()) / entry, "hold": hold, "plan": plan,
                          "up_day": move})
    return pd.DataFrame(irows), pd.DataFrame(prows)


def _t(x: pd.Series) -> float:
    return float(x.mean() / (x.std() / np.sqrt(len(x)))) if len(x) > 2 and x.std() > 0 else float("nan")


def report(name: str, ir: pd.DataFrame, pr: pd.DataFrame, up: float, t0: str, drop: float) -> str:
    out = [f"\n######## {name}: expiry days, t0 {t0}, puts down >= {drop:.0%} since 09:20"]
    for label, sub in (("all expiry days", ir), (f"days up >= {up:.1%} at {t0}", ir[ir.move >= up])):
        if sub.empty:
            continue
        out.append(f"A. index after {t0} | {label}: n {len(sub)} | lower at 15:25 {100 * (sub.end < 0).mean():.0f}% | "
                   f"avg rest-of-day {sub.end.mean() * 1e4:+.1f} bps | median pullback {sub.pullback.median() * 1e4:.0f} bps | "
                   f"median further rise {sub.further.median() * 1e4:.0f} bps")
    if pr.empty:
        out.append("B. no puts matched")
        return "\n".join(out)
    pr = pr.assign(period=np.where(pr.day < SPLIT, "train", "test"),
                   zone=pd.cut(pr.mny, [-1e9, -2.5, 2.5, 1e9], labels=["OTM", "near ATM", "ITM"]))
    out.append("B. buy the collapsed put at the ask at t0 (each day counted once):")
    for filt, sub in (("all days", pr), ("rally days only", pr[pr.up_day >= up])):
        for (zone, per), g in sub.groupby(["zone", "period"], observed=True):
            daily = g.groupby("day")[["hold", "plan", "max_mult"]].mean()
            out.append(f"   {filt:15s} {zone:8s} {per:5s} days {len(daily):3d} puts {len(g):4d} | "
                       f"reached 1.5x {100 * (g.max_mult >= 1.5).mean():3.0f}% 2x {100 * (g.max_mult >= 2).mean():3.0f}% | "
                       f"hold to 15:20 {daily.hold.mean():+.0%} | 2x/-50% plan {daily.plan.mean():+.0%} "
                       f"(t {_t(daily.plan):+.1f}, win {100 * (g.plan > 0).mean():.0f}%) | median entry Rs {g.entry.median():.1f}")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="scalper.study_bounce", description=__doc__.split("\n")[0])
    p.add_argument("--t0", default="13:30")
    p.add_argument("--drop", type=float, default=0.6)
    p.add_argument("--up", type=float, default=0.003)
    args = p.parse_args(argv)
    for name in ("NIFTY", "SENSEX"):
        ir, pr = study(name, args.t0, args.drop)
        print(report(name, ir, pr, args.up, args.t0, args.drop))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
