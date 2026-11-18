"""Index-level test of the S1 premise on years of 1-minute data.

Premise: after a strong one-way morning move (which crushes the options that were in the
money at the open), the index snaps back at least briefly instead of running further.

For every day it finds the S1 pattern on the index itself - move from the open of at least
``min_move`` x the expected move, ``min_bars`` consecutive 5-minute candles in that direction,
then the first 5-minute candle that closes against it beyond the previous candle's midpoint -
and grades the bounce exactly like S1's paper outcome: stop just beyond the extreme, target
at +1R, which comes first. An option that was crushed moves roughly delta x the index, so
the R outcome carries over; theta and IV are ignored, which flatters long options late in
expiry day.

  .venv\\Scripts\\python.exe -m scalper.study            (NIFTY and SENSEX, all downloaded years)
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import date, time, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path("data/market")
SESSION = (time(9, 15), time(15, 29))


def load_index(name: str) -> pd.DataFrame:
    files = sorted((ROOT / "index" / name).glob("*.parquet"))
    if not files:
        raise SystemExit(f"No data in {ROOT / 'index' / name}; run tools/dhan_download.py first")
    df = pd.concat(pd.read_parquet(f) for f in files).drop_duplicates("ts").sort_values("ts")
    dt = pd.to_datetime(df["ts"], unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    df = df.assign(dt=dt, day=dt.dt.date, t=dt.dt.time)
    df = df[(df.t >= SESSION[0]) & (df.t <= SESSION[1])]
    counts = df.groupby("day").size()
    return df[df.day.isin(counts[counts >= 300].index)].reset_index(drop=True)


def expiry_weekday(name: str, d: date) -> int | None:
    """Weekly expiry weekday by era (Mon=0); None when that index had no liquid weekly."""
    if name == "NIFTY":
        return 3 if d < date(2025, 9, 1) else 1
    if name == "SENSEX":
        if d < date(2023, 5, 15):
            return None
        if d < date(2025, 1, 1):
            return 4
        return 1 if d < date(2025, 9, 1) else 3
    return None


def expiry_days(name: str, trading_days: list[date]) -> set[date]:
    """Scheduled weekday, moved to the previous trading day when it was a holiday."""
    days = set(trading_days)
    out, d, last = set(), min(trading_days), max(trading_days)
    while d <= last:
        wd = expiry_weekday(name, d)
        if wd is not None and d.weekday() == wd:
            e = d
            while e not in days and e > d - timedelta(days=5):
                e -= timedelta(days=1)
            if e in days:
                out.add(e)
        d += timedelta(days=1)
    return out


@dataclass
class Event:
    underlying: str
    day: date
    expiry_day: bool
    direction: str  # DOWN: index fell, ITM calls crushed -> CE bounce; UP: index rose -> PE bounce
    arm_time: str
    trigger_time: str
    move_em: float
    entry: float
    extreme: float
    stop: float
    risk_pts: float
    result: str
    resolve_time: str
    mfe_r_30m: float
    mfe_r_60m: float
    mae_r_30m: float
    retrace_frac_60m: float
    new_extreme_60m: bool
    vix: float | None


def _five_min(day_df: pd.DataFrame) -> pd.DataFrame:
    g = day_df.set_index("dt").resample("5min", label="left", closed="left")
    bars = g.agg({"open": "first", "high": "max", "low": "min", "close": "last"}).dropna()
    bars["end"] = bars.index + pd.Timedelta(minutes=5)
    return bars


def scan_day(name: str, day_df: pd.DataFrame, em: float, is_expiry: bool, vix_by_min: dict, args) -> list[Event]:
    m1 = day_df.set_index("dt")
    b5 = _five_min(day_df)
    day_open = float(day_df.iloc[0]["open"])
    events: list[Event] = []
    armed: tuple | None = None  # (direction, arm_end_time, extreme)
    cooldown_until = None
    win_lo, win_hi = args.window
    for i in range(1, len(b5)):
        bar, prev = b5.iloc[i], b5.iloc[i - 1]
        end = bar["end"]
        if end.time() > win_hi or len(events) >= args.max_per_day:
            break
        if end.time() <= win_lo:
            continue
        seen = m1[m1.index < end]
        if armed is None and (cooldown_until is None or end > cooldown_until):
            move = float(bar["close"]) - day_open
            recent = b5.iloc[max(0, i - args.min_bars + 1): i + 1]
            if abs(move) >= args.min_move * em and len(recent) == args.min_bars:
                if move < 0 and (recent.close < recent.open).all():
                    armed = ("DOWN", end, float(seen.low.min()))
                elif move > 0 and (recent.close > recent.open).all():
                    armed = ("UP", end, float(seen.high.max()))
            continue
        if armed is None:
            continue
        direction, arm_end, _ = armed
        extreme = float(seen.low.min()) if direction == "DOWN" else float(seen.high.max())
        ext_time = (seen.low.idxmin() if direction == "DOWN" else seen.high.idxmax())
        if end - max(arm_end, ext_time + pd.Timedelta(minutes=1)) > pd.Timedelta(minutes=args.arm_valid):
            armed = None
            continue
        against = bar["close"] > bar["open"] if direction == "DOWN" else bar["close"] < bar["open"]
        beyond_mid = bar["close"] > (prev.high + prev.low) / 2 if direction == "DOWN" else bar["close"] < (prev.high + prev.low) / 2
        if not (against and beyond_mid):
            continue
        atr = float((seen.high - seen.low).tail(10).mean())
        buffer = max(atr * args.stop_atr, extreme * 0.0002)
        entry = float(bar["close"])
        sign = 1 if direction == "DOWN" else -1  # bounce direction of the index
        stop = extreme - buffer if direction == "DOWN" else extreme + buffer
        risk = abs(entry - stop)
        after = m1[(m1.index >= end) & (m1.index.time <= time(15, 20))]
        result, resolve = "open", ""
        for ts, row in after.iterrows():
            adverse = row.low <= stop if sign > 0 else row.high >= stop
            favour = row.high >= entry + risk if sign > 0 else row.low <= entry - risk
            if adverse:
                result, resolve = "stop", ts.strftime("%H:%M")
                break
            if favour:
                result, resolve = "target", ts.strftime("%H:%M")
                break

        def excursions(minutes: int) -> tuple[float, float, float, bool]:
            w = after[after.index < end + pd.Timedelta(minutes=minutes)]
            if w.empty:
                return 0.0, 0.0, 0.0, False
            best = (w.high.max() - entry) if sign > 0 else (entry - w.low.min())
            worst = (w.low.min() - entry) if sign > 0 else (entry - w.high.max())
            move_from_open = abs(extreme - day_open)
            back = ((w.high.max() - extreme) if sign > 0 else (extreme - w.low.min())) / move_from_open if move_from_open else 0
            new_ext = (w.low.min() < extreme) if sign > 0 else (w.high.max() > extreme)
            return best / risk, worst / risk, back, bool(new_ext)

        mfe30, mae30, _, _ = excursions(30)
        mfe60, _, back60, newext60 = excursions(60)
        events.append(Event(
            name, day_df.iloc[0]["day"], is_expiry, direction, arm_end.strftime("%H:%M"), end.strftime("%H:%M"),
            round(abs(extreme - day_open) / em, 2), round(entry, 2), round(extreme, 2), round(stop, 2), round(risk, 2),
            result, resolve, round(mfe30, 2), round(mfe60, 2), round(mae30, 2), round(back60, 3), newext60,
            vix_by_min.get(end.floor("min") - pd.Timedelta(minutes=1)),
        ))
        armed, cooldown_until = None, end + pd.Timedelta(minutes=args.cooldown)
    return events


def run(args, quiet: bool = False) -> pd.DataFrame:
    vix = load_index("INDIAVIX")
    vix_by_min = dict(zip(vix.dt.dt.floor("min"), vix.close))
    rows = []
    for name in args.underlyings:
        df = load_index(name)
        days = sorted(df.day.unique())
        expiries = expiry_days(name, days)
        daily = df.groupby("day").agg(high=("high", "max"), low=("low", "min"))
        em_by_day = (daily.high - daily.low).rolling(args.em_days).mean().shift(1)
        for day, day_df in df.groupby("day", sort=True):
            em = em_by_day.get(day)
            if pd.isna(em) or em <= 0:
                continue
            is_exp = day in expiries
            if args.expiry_only and not is_exp:
                continue
            rows.extend(asdict(e) for e in scan_day(name, day_df, float(em), is_exp, vix_by_min, args))
        if not quiet:
            print(f"{name}: {len(days)} trading days, {len(expiries)} expiry days scanned "
                  f"({min(days)} .. {max(days)})")
    return pd.DataFrame(rows)


SWEEP = {
    "current S1": {},
    "wider stop 2x": {"stop_atr": 2.0},
    "wider stop 3x": {"stop_atr": 3.0},
    "bigger move 1.0 EM": {"min_move": 1.0},
    "4 trend candles": {"min_bars": 4},
    "5 trend candles": {"min_bars": 5},
    "one per day": {"max_per_day": 1},
    "until 11:30": {"window": (time(9, 30), time(11, 30))},
    "1.0 EM + 4 bars + 2x stop": {"min_move": 1.0, "min_bars": 4, "stop_atr": 2.0},
}


def sweep(args) -> None:
    print(f"{'variant':28s} {'events':>6s} {'target%':>7s} {'stop%':>5s} {'edge_R':>7s} {'mfe60>=2R%':>10s} {'new_ext60%':>10s}")
    for name, over in SWEEP.items():
        ev = run(argparse.Namespace(**{**vars(args), **over}), quiet=True)
        if ev.empty:
            print(f"{name:28s} no events")
            continue
        t, s = (ev.result == "target").mean() * 100, (ev.result == "stop").mean() * 100
        print(f"{name:28s} {len(ev):6d} {t:7.0f} {s:5.0f} {(t - s) / 100:+7.2f} "
              f"{(ev.mfe_r_60m >= 2).mean() * 100:10.0f} {ev.new_extreme_60m.mean() * 100:10.0f}")


def _table(ev: pd.DataFrame, by) -> str:
    g = ev.groupby(by, observed=True).agg(
        events=("hit", "size"), target_first_pct=("hit", lambda s: round(s.mean() * 100)),
        stop_first_pct=("stopped", lambda s: round(s.mean() * 100)),
        mfe60_R_median=("mfe_r_60m", "median"), mfe60_ge2R_pct=("mfe_r_60m", lambda s: round((s >= 2).mean() * 100)),
        retrace60_median=("retrace_frac_60m", "median"),
        new_extreme60_pct=("new_extreme_60m", lambda s: round(s.mean() * 100)),
    )
    g["edge_R(1:1)"] = ((g.target_first_pct - g.stop_first_pct) / 100).round(2)
    return g.to_string()


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--underlyings", nargs="+", default=["NIFTY", "SENSEX"])
    p.add_argument("--expiry-only", action="store_true", help="skip non-expiry days (faster)")
    p.add_argument("--min-move", type=float, default=0.6, help="move from open, in expected moves, to arm")
    p.add_argument("--min-bars", type=int, default=3, help="consecutive 5m candles in the move's direction")
    p.add_argument("--arm-valid", type=int, default=25, help="minutes an arm stays valid after the last extreme")
    p.add_argument("--stop-atr", type=float, default=1.0, help="stop buffer beyond the extreme, x avg 1m range")
    p.add_argument("--cooldown", type=int, default=10)
    p.add_argument("--max-per-day", type=int, default=4)
    p.add_argument("--em-days", type=int, default=10, help="expected move = avg daily range of the previous N days")
    p.add_argument("--window", nargs=2, default=["09:30", "12:30"])
    p.add_argument("--out", default="data/market/study/s1_index_events.csv")
    p.add_argument("--sweep", action="store_true", help="compare rule variants (expiry days) instead of a full report")
    args = p.parse_args()
    args.window = tuple(time.fromisoformat(t) for t in args.window)
    if args.sweep:
        args.expiry_only = True
        sweep(args)
        return 0

    ev = run(args)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    ev.to_csv(out, index=False)
    print(f"\n{len(ev)} events -> {out}\n")
    if not ev.empty:
        ev["year"] = pd.to_datetime(ev.day).dt.year
        ev["hit"] = (ev.result == "target").astype(int)
        ev["stopped"] = (ev.result == "stop").astype(int)
        mins = pd.to_datetime(ev.trigger_time, format="%H:%M")
        ev["slot"] = pd.cut(mins.dt.hour * 60 + mins.dt.minute, [0, 630, 690, 751],
                            labels=["09:30-10:30", "10:30-11:30", "11:30-12:30"], right=False)
        ev["move"] = pd.cut(ev.move_em, [0, 0.8, 1.2, 99], labels=["<0.8 EM", "0.8-1.2 EM", ">1.2 EM"])
        ev["vix_band"] = pd.cut(ev.vix.astype(float), [0, 13, 16, 20, 99], labels=["VIX<13", "13-16", "16-20", ">20"])
        print("target_first = +1R before stop; edge_R(1:1) = (target% - stop%)/100 per trade, before costs/theta")
        print("\n== ALL DAYS by underlying / expiry day\n" + _table(ev, ["underlying", "expiry_day"]))
        exp = ev[ev.expiry_day]
        for title, by in (("year", ["underlying", "year"]), ("trigger time", ["slot"]),
                          ("size of morning move", ["move"]), ("VIX", ["vix_band"]), ("direction", ["direction"])):
            print(f"\n== EXPIRY DAYS by {title}\n" + _table(exp, by))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
