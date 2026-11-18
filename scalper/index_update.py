"""Top up data/market/index/{NIFTY,SENSEX,INDIAVIX} with 1-minute bars through OpenAlgo's history API.

The harvester's VIX median and its missed-expiry settlement read these files. The scanner runs this after every
session; run it by hand to bootstrap a new server (about 3 requests per session, a few minutes for a year):

  .venv/bin/python -m scalper.index_update --config config/vps.yaml --env-file env/dhan.env --days 400
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from scalper.clock import at, now_ms, to_dt
from scalper.config import Config, load_config, load_env_file
from scalper.history import fetch_bars, session_shift_ms
from scalper.universe import Rest

log = logging.getLogger("scalper.index_update")

INDEX_ROOT = Path("data/market/index")


def last_stored_day(name: str, root: Path = INDEX_ROOT) -> date | None:
    for f in sorted((root / name).glob("*.parquet"), reverse=True):
        ts = pd.read_parquet(f, columns=["ts"]).ts
        if len(ts):
            return to_dt(int(ts.max()) * 1000).date()
    return None


def session_frame(rows: list[dict], day: date, shift_ms: int) -> pd.DataFrame:
    """Bars of `day` inside 09:15-15:29 IST, times corrected by `shift_ms`, in the downloader's column layout."""
    if not rows:
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close", "volume"])
    df = pd.DataFrame(rows)
    df["ts"] = df.timestamp.astype("int64") - shift_ms // 1000
    dt = pd.to_datetime(df.ts, unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    hm = dt.dt.strftime("%H:%M")
    df = df[(dt.dt.date == day) & (hm >= "09:15") & (hm <= "15:29")]
    if "volume" not in df:
        df = df.assign(volume=0)
    return df[["ts", "open", "high", "low", "close", "volume"]].astype(
        {"open": float, "high": float, "low": float, "close": float, "volume": float})


def append(name: str, df: pd.DataFrame, root: Path = INDEX_ROOT) -> None:
    years = pd.to_datetime(df.ts, unit="s", utc=True).dt.tz_convert("Asia/Kolkata").dt.year
    for year, g in df.groupby(years):
        path = root / name / f"{year}.parquet"
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            g = pd.concat([pd.read_parquet(path), g], ignore_index=True)
        g = g.drop_duplicates("ts", keep="last").sort_values("ts")
        tmp = path.with_suffix(".partial")
        g.to_parquet(tmp, index=False)
        tmp.replace(path)


def update(rest: Rest, cfg: Config, days: int = 30, root: Path = INDEX_ROOT) -> dict[str, int]:
    """Fetch every session after the last stored one (at most `days` back); returns bars added per symbol."""
    now = now_ms()
    today = to_dt(now).date()
    last_ok = today if now >= at(today, "15:31") else today - timedelta(days=1)
    symbols = [(u.name, u.index_exchange) for u in cfg.underlyings if u.enabled]
    symbols.append((cfg.universe.vix_symbol, cfg.universe.vix_exchange))
    floor = last_ok - timedelta(days=days)
    start = {name: max(last_stored_day(name, root) or floor, floor) for name, _ in symbols}
    added = {name: 0 for name, _ in symbols}
    d = min(start.values()) + timedelta(days=1)
    while d <= last_ok:
        if d.weekday() < 5:
            fetched = {name: fetch_bars(rest, name, exch, d) for name, exch in symbols if d > start[name]}
            ref = next((rows for rows in fetched.values() if rows), None)
            if ref is not None:
                shift = session_shift_ms(ref, d, cfg.session.start)
                for name, rows in fetched.items():
                    df = session_frame(rows, d, shift)
                    if len(df):
                        append(name, df, root)
                        added[name] += len(df)
        d += timedelta(days=1)
    log.info("Index top-up to %s: %s", last_ok, added)
    return added


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="scalper.index_update", description=__doc__.split("\n")[0])
    p.add_argument("--config", default="config/dhan.yaml")
    p.add_argument("--env-file")
    p.add_argument("--days", type=int, default=30, help="look back at most this many calendar days")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.env_file:
        load_env_file(args.env_file)
    cfg = load_config(args.config)
    from openalgo import api

    update(Rest(api(api_key=cfg.api_key(), host=cfg.openalgo.host), cfg.api_key()), cfg, args.days)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
