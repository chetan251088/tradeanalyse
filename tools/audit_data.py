"""Sanity-check the downloaded Dhan data before backtesting.

  .venv\\Scripts\\python.exe tools\\audit_data.py

Index/VIX bars: session coverage, duplicates, impossible OHLC, frozen days, extreme jumps.
Option chains (one file per session): minute coverage, duplicates, impossible OHLC, filler bars
(no volume and no range), chain spot vs index close, strike grid, ATM/offset alignment, IV/OI
sanity, VIX present. Expiry calendar: weekday pattern and weeks without an expiry.
Writes one row per chain day to data/market/audit.csv; prints a summary and every flagged day.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path("data/market")
IST = "Asia/Kolkata"
STEP = {"NIFTY": 50, "SENSEX": 100}


def load_index(name: str) -> pd.DataFrame:
    files = sorted((ROOT / "index" / name).glob("*.parquet"))
    df = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
    dt = pd.to_datetime(df.ts, unit="s", utc=True).dt.tz_convert(IST)
    return df.assign(day=dt.dt.date, hm=dt.dt.strftime("%H:%M"))


def audit_index(name: str, df: pd.DataFrame) -> dict:
    s = df[(df.hm >= "09:15") & (df.hm <= "15:29")]
    per_day = s.groupby("day").agg(bars=("ts", "size"), hi=("high", "max"), lo=("low", "min"))
    bad = (s.low > s[["open", "close"]].min(axis=1) + 1e-6) | (s.high < s[["open", "close"]].max(axis=1) - 1e-6) | (s.close <= 0)
    ret = s.sort_values("ts").groupby("day").close.pct_change().abs()
    return {
        "series": name, "days": len(per_day), "first": per_day.index.min(), "last": per_day.index.max(),
        "short_days(<370 bars)": int((per_day.bars < 370).sum()), "dup_ts": int(df.ts.duplicated().sum()),
        "bad_ohlc": int(bad.sum()), "frozen_days": int((per_day.hi == per_day.lo).sum()),
        "max_1m_move_%": round(float(ret.max()) * 100, 2),
    }


def audit_chain(name: str, day, df: pd.DataFrame, idx: pd.DataFrame, vix_days: set) -> dict:
    step = STEP[name]
    dt = pd.to_datetime(df.ts, unit="s", utc=True).dt.tz_convert(IST)
    hm = dt.dt.strftime("%H:%M")
    r = {"underlying": name, "day": day, "rows": len(df), "minutes": df.ts.nunique(),
         "first": hm.min(), "last": hm.max(), "strikes": df.strike.nunique(),
         "outside_session": int(((hm < "09:15") | (hm > "15:30")).sum()),
         "dup_rows": int(df.duplicated(["ts", "opt", "offset"]).sum()), "vix": day in vix_days}
    lo_oc, hi_oc = df[["open", "close"]].min(axis=1), df[["open", "close"]].max(axis=1)
    r["bad_ohlc"] = int(((df.close <= 0) | (df.low > lo_oc + 1e-6) | (df.high < hi_oc - 1e-6)).sum())
    r["filler_pct"] = round(float(((df.volume.fillna(0) == 0) & (df.high == df.low)).mean()) * 100, 2)
    r["iv_bad_pct"] = round(float(((df.iv <= 0) | (df.iv > 300)).mean()) * 100, 2)
    r["oi_zero_pct"] = round(float((df.oi.fillna(0) <= 0).mean()) * 100, 2)
    spot = df.groupby("ts").spot.agg(["min", "max"])
    r["spot_ce_pe_diff"] = round(float((spot["max"] - spot["min"]).max()), 2)
    m = spot["min"].rename("spot").reset_index().merge(idx[["ts", "close"]], on="ts")
    bps = (m.spot - m.close).abs() / m.close * 1e4
    r["spot_vs_index_bps_med"] = round(float(bps.median()), 2) if len(m) else np.nan
    r["spot_vs_index_bps_max"] = round(float(bps.max()), 2) if len(m) else np.nan
    r["off_grid"] = int((df.strike % step != 0).sum())
    atm = df[df.offset == 0].set_index(["ts", "opt"]).strike.rename("atm")
    j = df.join(atm, on=["ts", "opt"])
    r["offset_mismatch"] = int(((j.strike - j.atm) != j.offset * step).sum())
    a0 = df[df.offset == 0]
    r["atm_far_steps"] = round(float(((a0.strike - a0.spot).abs() / step).max()), 2)
    return r


def flag(r: pd.Series) -> list[str]:
    out = []
    if r.minutes < 370:
        out.append(f"only {r.minutes} minutes")
    if r.outside_session:
        out.append(f"{r.outside_session} rows outside 09:15-15:30")
    if r.dup_rows:
        out.append(f"{r.dup_rows} duplicate rows")
    if r.bad_ohlc:
        out.append(f"{r.bad_ohlc} impossible OHLC")
    if r.filler_pct > 10:
        out.append(f"{r.filler_pct}% filler bars")
    if not r.vix:
        out.append("no VIX bars")
    if r.spot_vs_index_bps_max > 25 or np.isnan(r.spot_vs_index_bps_max):
        out.append(f"spot vs index up to {r.spot_vs_index_bps_max} bps")
    if r.spot_ce_pe_diff > 0.5:
        out.append(f"CE/PE spot differ by {r.spot_ce_pe_diff}")
    if r.off_grid:
        out.append(f"{r.off_grid} strikes off the {STEP[r.underlying]} grid")
    if r.offset_mismatch:
        out.append(f"{r.offset_mismatch} rows with strike != ATM + offset*step")
    if r.atm_far_steps > 1:
        out.append(f"ATM strike {r.atm_far_steps} steps from spot")
    return out


def audit_calendar(name: str) -> pd.DataFrame:
    t = pd.read_csv(ROOT / "options" / name / "days.csv", parse_dates=["day"])
    t = t[t.ratio.notna()]
    iso = t.day.dt.isocalendar()
    t = t.assign(week=iso.year.astype(str) + "-W" + iso.week.astype(str).str.zfill(2), year=t.day.dt.year,
                 wd=t.day.dt.day_name().str[:3], exp=t.is_expiry.astype(str) == "True")
    no_exp = t.groupby("week").exp.sum()
    weekdays = t[t.exp].groupby(["year", "wd"]).size().unstack(fill_value=0)
    print(f"\n{name} expiry weekdays by year:\n{weekdays.to_string()}")
    missing = no_exp[no_exp == 0].index.tolist()
    two = no_exp[no_exp > 1].index.tolist()
    print(f"{name}: weeks with option data but no expiry found: {missing or 'none'}; weeks with two: {two or 'none'}")
    worst = t[t.exp].ratio.max()
    print(f"{name}: highest 15:25 ATM straddle/spot on an expiry day: {worst:.4f} (expect well below 0.006)")
    return t


def main() -> int:
    pd.set_option("display.width", 200)
    idx = {n: load_index(n) for n in ("NIFTY", "SENSEX", "INDIAVIX")}
    print(pd.DataFrame([audit_index(n, d) for n, d in idx.items()]).to_string(index=False))
    vix_days = set(idx["INDIAVIX"][(idx["INDIAVIX"].hm >= "09:15") & (idx["INDIAVIX"].hm <= "15:29")]
                   .groupby("day").size().loc[lambda s: s >= 300].index)

    rows = []
    for name in STEP:
        index = idx[name]
        by_day = dict(tuple(index.groupby("day")))
        files = sorted((ROOT / "options" / name / "chain").glob("*.parquet"))
        for f in files:
            day = pd.Timestamp(f.stem).date()
            rows.append(audit_chain(name, day, pd.read_parquet(f), by_day.get(day, index.iloc[:0]), vix_days))
        print(f"{name}: audited {len(files)} chain files")
        audit_calendar(name)
    df = pd.DataFrame(rows)
    df.to_csv(ROOT / "audit.csv", index=False)

    df["year"] = pd.to_datetime(df.day).dt.year
    summary = df.groupby(["underlying", "year"]).agg(
        days=("day", "size"), rows_med=("rows", "median"), min_minutes=("minutes", "min"),
        strikes_med=("strikes", "median"), bad_ohlc=("bad_ohlc", "sum"), dup_rows=("dup_rows", "sum"),
        filler_pct=("filler_pct", "mean"), iv_bad_pct=("iv_bad_pct", "mean"), oi_zero_pct=("oi_zero_pct", "mean"),
        spot_bps_med=("spot_vs_index_bps_med", "median"), spot_bps_max=("spot_vs_index_bps_max", "max"),
        offset_mismatch=("offset_mismatch", "sum"), no_vix=("vix", lambda s: int((~s).sum())),
    ).round(2)
    print("\nOption chains by year:\n" + summary.to_string())

    flagged = [(r.underlying, r.day, "; ".join(f)) for r in df.itertuples() if (f := flag(r))]
    print(f"\nFlagged days: {len(flagged)} of {len(df)}")
    for u, d, why in flagged:
        print(f"  {u} {d}: {why}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
