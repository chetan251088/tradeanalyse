"""The day-trader-skill pack's rules, tested on Indian index data (vendor/day-trader-skill, MIT).

  .venv\\Scripts\\python.exe -m scalper.skill_tests

1. trader-scanner + trader-entry + trader-exit, intraday: 15-minute opening range (09:15-09:29), entry on the
   first 5-minute bar that CLOSES beyond it, stop at the range midpoint, target 1R, flat by 15:20 IST.
   Graded in index R (an option on it cannot fix a rule with no edge on the index).
2. trader-scanner daily setups on index closes: 20-day closing-high breakout, bull flag, reclaim of the
   20-day average, RSI(14) < 30, oversold bounce; forward 1- and 5-session returns vs all days.
3. trader-timemachine: before each opening-range trade, replay the rest of the day 2,000 times from the
   index's own same-time-of-day 5-minute bars of the previous 60 sessions; does a high "target first"
   probability pick better trades than a low one?
"""

from __future__ import annotations

import random

import numpy as np
import pandas as pd

from scalper.backtest import _index

SPLIT = pd.Timestamp("2024-07-01")


def five_min(name: str) -> dict:
    ix = _index(name)
    ix["dt"] = pd.to_datetime(ix.timestamp, unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    out = {}
    for day, g in ix.groupby("day"):
        if len(g) < 370:
            continue
        b = g.set_index("dt")[["open", "high", "low", "close"]].resample("5min", label="left", closed="left").agg(
            {"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
        if len(b) >= 74:
            out[day] = b.iloc[:75]
    return out


def orb_trade(b: pd.DataFrame, stop_rule: str = "mid", target_r: float = 1.0) -> dict | None:
    o, h, l, c = (b[k].to_numpy() for k in ("open", "high", "low", "close"))
    hi, lo = h[:3].max(), l[:3].min()
    mid = (hi + lo) / 2
    last_entry = 69  # 15:00
    for i in range(3, last_entry):
        side = 1 if c[i] > hi else -1 if c[i] < lo else 0
        if not side:
            continue
        entry = c[i]
        stop = mid if stop_rule == "mid" else (lo if side > 0 else hi)
        risk = (entry - stop) * side
        if risk <= 0:
            return None
        target = entry + side * target_r * risk
        for k in range(i + 1, 73):  # bar 73 starts 15:20
            if (l[k] <= stop) if side > 0 else (h[k] >= stop):
                return {"i": i, "side": side, "r": -1.0, "res": "stop", "risk_bps": risk / entry * 1e4}
            if (h[k] >= target) if side > 0 else (l[k] <= target):
                return {"i": i, "side": side, "r": target_r, "res": "target", "risk_bps": risk / entry * 1e4}
        return {"i": i, "side": side, "r": side * (c[72] - entry) / risk, "res": "flat 15:20", "risk_bps": risk / entry * 1e4}
    return None


def timemachine(days: list, bars: dict, day, i: int, side: int, entry: float, stop: float, target: float,
                n: int = 2000, seed: int = 19) -> float:
    """Share of replays that hit the target before the stop (same-time-of-day bars, previous 60 sessions)."""
    past = [d for d in days if d < day][-60:]
    if len(past) < 20:
        return np.nan
    ratios = []
    for d in past:
        b = bars[d]
        prev = b.close.shift(1).to_numpy()
        ratios.append(np.stack([b.high.to_numpy() / prev, b.low.to_numpy() / prev, b.close.to_numpy() / prev], 1))
    rng = random.Random(seed)
    first = 0
    for _ in range(n):
        px = entry
        for k in range(i + 1, 73):
            r = ratios[rng.randrange(len(ratios))][min(max(k + rng.randint(-2, 2), 1), 74)]
            hi, lo, px_new = px * r[0], px * r[1], px * r[2]
            if (lo <= stop) if side > 0 else (hi >= stop):
                break
            if (hi >= target) if side > 0 else (lo <= target):
                first += 1
                break
            px = px_new
    return first / n


def stats(r: pd.Series) -> str:
    r = r.dropna()
    t = r.mean() / (r.std() / np.sqrt(len(r))) if len(r) > 2 else np.nan
    return f"n {len(r):4d}  avg {r.mean():+.3f}  t {t:+.2f}  win {100 * (r > 0).mean():.0f}%"


def main() -> int:
    for name in ("NIFTY", "SENSEX"):
        bars = five_min(name)
        days = sorted(bars)
        print(f"\n######## {name}: {len(days)} full sessions")
        rows = []
        for d in days:
            for stop_rule, tr in (("mid", 1.0), ("mid", 2.0), ("range", 1.0)):
                t = orb_trade(bars[d], stop_rule, tr)
                if t:
                    rows.append({"day": d, "rule": f"stop {stop_rule} target {tr:g}R", **t})
        orb = pd.DataFrame(rows)
        orb["period"] = np.where(pd.to_datetime(orb.day) < SPLIT, "train", "test")
        print("1. Opening-range breakout (index R, flat 15:20):")
        for (rule, per), g in orb.groupby(["rule", "period"]):
            print(f"   {rule:22s} {per:5s} {stats(g.r)}  stop {100 * (g.res == 'stop').mean():.0f}% target {100 * (g.res == 'target').mean():.0f}%")

        daily = pd.Series({d: bars[d].close.iloc[-1] for d in days})
        ret = np.log(daily).diff()
        delta = daily.diff()
        rs = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean() / (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
        rsi = 100 - 100 / (1 + rs)
        ma20 = daily.rolling(20).mean()
        setups = pd.DataFrame({
            "breakout_20d": daily > daily.shift(1).rolling(20).max(),
            "bull_flag": (daily.shift(5) / daily.shift(15) - 1 > 0.04) & (daily.rolling(5).max() / daily.rolling(5).min() - 1 < 0.02),
            "reclaim_ma20": (daily.shift(1) < ma20.shift(1)) & (daily > ma20),
            "oversold_rsi30": rsi < 30,
            "oversold_bounce": (rsi.shift(1) < 30) & (daily > daily.shift(1)),
        })
        fwd1, fwd5 = ret.shift(-1) * 1e4, (np.log(daily.shift(-5)) - np.log(daily)) * 1e4
        per = pd.Series(np.where(pd.to_datetime(daily.index) < SPLIT, "train", "test"), index=daily.index)
        print("2. Daily setups (bull flag uses 4% / 2%: index-sized, not the stock pack's 10% / 5%), forward bps:")
        for p in ("train", "test"):
            m = per == p
            print(f"   {'all days':16s} {p:5s} next day {fwd1[m].mean():+6.1f}  next 5 days {fwd5[m].mean():+6.1f}  (n {int(m.sum())})")
        for s in setups:
            for p in ("train", "test"):
                m = setups[s] & (per == p)
                f5 = fwd5[m].dropna()
                t5 = f5.mean() / (f5.std() / np.sqrt(len(f5))) if len(f5) > 2 else np.nan
                print(f"   {s:16s} {p:5s} next day {fwd1[m].mean():+6.1f}  next 5 days {f5.mean():+6.1f} (t {t5:+.2f})  n {int(m.sum())}")

        base = orb[orb.rule == "stop mid target 1R"].copy()
        probs = []
        for _, t in base.iterrows():
            b = bars[t.day]
            entry = b.close.iloc[int(t.i)]
            hi, lo = b.high.iloc[:3].max(), b.low.iloc[:3].min()
            stop = (hi + lo) / 2
            target = entry + t.side * abs(entry - stop)
            probs.append(timemachine(days, bars, t.day, int(t.i), int(t.side), entry, stop, target))
        base["tm_target_first"] = probs
        base = base.dropna(subset=["tm_target_first"])
        base["tm_band"] = pd.qcut(base.tm_target_first, 3, labels=["low", "mid", "high"])
        print("3. Time machine 'target first' odds (2,000 replays, seed 19) vs what the trade really did:")
        for (band, p), g in base.groupby(["tm_band", "period"], observed=True):
            print(f"   odds {band:4s} ({g.tm_target_first.mean():.2f}) {p:5s} {stats(g.r)}")
        corr = base[["tm_target_first", "r"]].corr().iloc[0, 1]
        print(f"   correlation between the odds and the realised R: {corr:+.3f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
