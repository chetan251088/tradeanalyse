"""Download Dhan 1-minute history into Parquet for backtesting.

  * index + India VIX bars:   data/market/index/<NAME>/<YYYY>.parquet
  * expiry-day calendar:      data/market/options/<NAME>/days.csv
      (pass 1: ATM CE+PE every session; a near-zero 15:25 straddle marks the expiry day)
  * expired weekly options:   data/market/options/<NAME>/chain/<YYYY-MM-DD>.parquet
      (pass 2: ATM-10..ATM+10, CE and PE, OHLC, volume, OI, IV, actual strike and spot,
       nearest weekly expiry; --chains all adds every other session after the expiry days, newest first)

Dhan's expired-options API now serves at most 1-2 sessions per request (forum, 29 Oct 2026),
so everything is fetched one session at a time, 4 requests in flight under OpenAlgo's 5 req/s limiter.
Dhan documents 100k data requests a day, but throttled this account (DH-904) after ~60k in one day;
the run stops at --max-requests (default 40k) and resumes on the next run.

Run it with OpenAlgo's own interpreter while OpenAlgo is logged in to Dhan; it reuses that
session, its proxy-aware HTTP client and its Dhan rate limiter, so no token is copied anywhere:

  openalgo-main\\.venv\\Scripts\\python.exe tools\\dhan_download.py --user chetan --years 5

It resumes: finished years, scanned sessions and saved chains are skipped.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import threading
import time
from datetime import date, timedelta
from datetime import time as time_
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OPENALGO = ROOT / "openalgo-main"
OUT = ROOT / "data" / "market"

UNDERLYINGS = {"NIFTY": ("NSE_INDEX", "NSE_FNO"), "SENSEX": ("BSE_INDEX", "BSE_FNO")}
VIX = ("INDIAVIX", "NSE_INDEX")
FIELDS = ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"]

log = logging.getLogger("dhan_download")


def bootstrap():
    os.chdir(OPENALGO)
    sys.path.insert(0, str(OPENALGO))
    from dotenv import load_dotenv

    load_dotenv(OPENALGO / ".env")
    from broker.dhan.api.data import get_api_response
    from database.auth_db import get_auth_token
    from database.token_db import get_token

    return get_auth_token, get_token, get_api_response


class DhanRejected(RuntimeError):
    pass


class BudgetSpent(RuntimeError):
    pass


class Budget:
    def __init__(self, limit: int) -> None:
        self.limit, self.spent, self._lock = limit, 0, threading.Lock()

    def take(self) -> None:
        with self._lock:
            self.spent += 1

    def left(self) -> int:
        return self.limit - self.spent


class Pacer:
    """Spaces request starts across threads; widens on DH-904, narrows again while requests succeed."""

    def __init__(self, floor: float = 0.0, ceiling: float = 5.0) -> None:
        self.floor, self.ceiling, self._next, self._lock = floor, ceiling, 0.0, threading.Lock()
        self.gap = floor

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.gap
        time.sleep(start - now)

    def throttled(self) -> None:
        with self._lock:
            self.gap = min(self.ceiling, max(self.gap * 1.5, 0.3))

    def ok(self) -> None:
        with self._lock:
            self.gap = max(self.floor, self.gap * 0.99)


BUDGET = Budget(40_000)
PACER = Pacer()
LAST_ERROR = ""
PAUSES = (120, 300, 600, 900)


def call(api, token: str, endpoint: str, payload: dict, attempts: int = 4) -> dict | None:
    global LAST_ERROR
    for i in range(attempts):
        PACER.wait()
        BUDGET.take()
        try:
            res = api(endpoint, token, "POST", json.dumps(payload))
        except Exception as e:
            msg = str(e)
            if "subscri" in msg.lower() or "access token" in msg.lower() or "client ID" in msg:
                raise
            LAST_ERROR = msg[:100]
            log.warning("  %s failed (%s), retry %d", endpoint, msg[:100], i + 1)
            time.sleep(2 * (i + 1))
            continue
        code = res.get("errorCode") if isinstance(res, dict) else None
        if code == "DH-904":
            PACER.throttled()
            LAST_ERROR = f"DH-904 {res.get('errorMessage')}"
            log.warning("  %s rate-limited by Dhan, retry %d; pacing at %.1f req/s", endpoint, i + 1,
                        1 / PACER.gap)
            time.sleep(3 * (i + 1))
            continue
        if code:
            raise DhanRejected(f"{endpoint}: {code} {res.get('errorMessage')}")
        PACER.ok()
        return res
    return None


def _write(df, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".partial")
    df.to_parquet(tmp, index=False)
    tmp.replace(path)


def download_index(api, token, sec_id, name: str, start: date, end: date, refresh: bool) -> None:
    import pandas as pd

    for year in range(start.year, end.year + 1):
        path = OUT / "index" / name / f"{year}.parquet"
        complete = date(year, 12, 31) < end
        if path.exists() and complete and not refresh:
            continue
        frames, a = [], max(start, date(year, 1, 1))
        last = min(end, date(year, 12, 31))
        while a <= last:
            b = min(a + timedelta(days=85), last + timedelta(days=1))
            res = call(api, token, "/v2/charts/intraday", {
                "securityId": str(sec_id), "exchangeSegment": "IDX_I", "instrument": "INDEX", "interval": "1",
                "oi": False, "fromDate": a.isoformat(), "toDate": b.isoformat(),
            })
            if res and res.get("timestamp"):
                frames.append(pd.DataFrame({
                    "ts": res["timestamp"], "open": res["open"], "high": res["high"], "low": res["low"],
                    "close": res["close"], "volume": res.get("volume") or [0] * len(res["timestamp"]),
                }))
            a = b
        if frames:
            df = pd.concat(frames).drop_duplicates("ts").sort_values("ts")
            _write(df, path)
            log.info("%s index %d: %d bars", name, year, len(df))
        else:
            log.info("%s index %d: no data", name, year)


def trading_days(name: str, start: date, end: date) -> list[date]:
    """Sessions that have a full day of index bars (holidays and half days drop out)."""
    import pandas as pd

    files = sorted((OUT / "index" / name).glob("*.parquet"))
    if not files:
        return []
    ts = pd.concat(pd.read_parquet(f, columns=["ts"]) for f in files)["ts"]
    dt = pd.to_datetime(ts, unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    in_session = dt[(dt.dt.time >= time_(9, 15)) & (dt.dt.time <= time_(15, 29))]
    counts = in_session.dt.date.value_counts()
    return sorted(d for d, n in counts.items() if n >= 300 and start <= d <= end)


def fetch_leg(api, token, sec_id, segment: str, day: date, opt: str, offset: int, fields: list[str]):
    """One strike offset, one option type, one session (Dhan allows at most 1-2 days per request)."""
    import pandas as pd

    strike = "ATM" if offset == 0 else f"ATM{offset:+d}"
    res = call(api, token, "/v2/charts/rollingoption", {
        "exchangeSegment": segment, "interval": "1", "securityId": int(sec_id), "instrument": "OPTIDX",
        "expiryFlag": "WEEK", "expiryCode": 1, "strike": strike, "drvOptionType": opt,
        "requiredData": fields, "fromDate": day.isoformat(), "toDate": day.isoformat(),
    })
    if res is None:
        return None
    key = "ce" if opt == "CALL" else "pe"
    d = (res.get("data") or {}).get(key) or {}
    if not d.get("timestamp"):
        return pd.DataFrame()
    n = len(d["timestamp"])
    df = pd.DataFrame({"ts": d["timestamp"], **{f: d.get(f) or [None] * n for f in fields}})
    dt = pd.to_datetime(df.ts, unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    df = df[(dt.dt.date == day) & (dt.dt.time <= time_(15, 30))]
    return df.assign(opt=key.upper(), offset=offset)


def _fetch_many(api, token, sec_id, segment, day, legs, fields, workers: int):
    """Fetch several (opt, offset) legs for one day in parallel; OpenAlgo's limiter keeps 5 req/s."""
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(fetch_leg, api, token, sec_id, segment, day, opt, k, fields) for opt, k in legs]
        return [f.result() for f in futures]


def classify_expiries(table, max_ratio: float):
    """One weekly expiry per calendar week: the session with the cheapest 15:25 ATM straddle vs spot."""
    import pandas as pd

    t = table.copy()
    t["ratio"] = pd.to_numeric(t["ratio"], errors="coerce")
    week = pd.to_datetime(t.day).dt.isocalendar()
    t["wk"] = week.year.astype(str) + "-" + week.week.astype(str)
    lowest = t.groupby("wk")["ratio"].transform("min")
    t["is_expiry"] = (t.ratio == lowest) & (t.ratio < max_ratio)
    return t.drop(columns="wk")


def scan_expiries(api, token, sec_id, name: str, segment: str, days: list[date], refresh: bool,
                  workers: int, max_ratio: float) -> tuple[list[date], list[date]]:
    """Pass 1: ATM CE+PE for every session; the week's near-zero 15:25 straddle marks its expiry day.

    Returns (expiry days, sessions that have option data).
    """
    import pandas as pd

    path = OUT / "options" / name / "days.csv"
    known = pd.read_csv(path, parse_dates=["day"]) if path.exists() and not refresh else pd.DataFrame()
    done = set(known.day.dt.date) if not known.empty else set()
    rows, todo = [], [d for d in days if d not in done or d >= date.today() - timedelta(days=3)]
    log.info("%s: scanning %d sessions for expiry days (%d already known)", name, len(todo), len(done))
    for i, day in enumerate(todo, 1):
        ce, pe = _fetch_many(api, token, sec_id, segment, day, [("CALL", 0), ("PUT", 0)], ["close", "spot"], workers)
        if ce is None or pe is None:
            log.warning("%s %s: scan request failed (%s); left for the next run", name, day, LAST_ERROR)
        else:
            row = {"day": day, "straddle_1525": None, "spot": None, "ratio": None, "is_expiry": None}
            if not ce.empty and not pe.empty:
                m = ce.merge(pe, on="ts", suffixes=("_ce", "_pe"))
                dt = pd.to_datetime(m.ts, unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
                m = m[dt.dt.time <= time_(15, 25)]
                if not m.empty:
                    last = m.iloc[-1]
                    straddle, spot = float(last.close_ce) + float(last.close_pe), float(last.spot_ce)
                    row.update(straddle_1525=straddle, spot=spot, ratio=straddle / spot)
            rows.append(row)
        if rows and (i % 50 == 0 or i == len(todo)):
            merged = (pd.concat([known, pd.DataFrame(rows).assign(day=lambda x: pd.to_datetime(x.day))])
                      .drop_duplicates("day", keep="last").sort_values("day"))
            merged = classify_expiries(merged, max_ratio)
            path.parent.mkdir(parents=True, exist_ok=True)
            merged.to_csv(path.with_suffix(".partial"), index=False)
            path.with_suffix(".partial").replace(path)
            log.info("%s: %d/%d sessions scanned, %d expiry days so far", name, i, len(todo),
                     int(merged.is_expiry.fillna(False).astype(bool).sum()))
    table = pd.read_csv(path, parse_dates=["day"]) if path.exists() else pd.DataFrame(columns=["day", "is_expiry", "ratio"])
    expiries = sorted(d.date() for d, e in zip(table.day, table.is_expiry) if str(e) == "True")
    with_data = sorted(d.date() for d, r in zip(table.day, table.ratio) if pd.notna(r))
    return expiries, with_data


def download_chains(api, token, jobs: list[tuple[str, str, str, date]], width: int, refresh: bool,
                    workers: int, label: str) -> None:
    """Pass 2: ATM-width..ATM+width, CE and PE, full fields, one file per (underlying, session)."""
    import pandas as pd

    legs = [(opt, k) for opt in ("CALL", "PUT") for k in range(-width, width + 1)]
    cols = ["ts", "opt", "offset", *FIELDS]
    todo = [j for j in jobs if refresh or not (OUT / "options" / j[0] / "chain" / f"{j[3].isoformat()}.parquet").exists()]
    log.info("%s: downloading option chains for %d sessions (%d requests each)", label, len(todo), len(legs))
    for i, (name, sec_id, segment, day) in enumerate(todo, 1):
        if BUDGET.left() < len(legs):
            raise BudgetSpent(f"{BUDGET.spent} requests used, {len(todo) - i + 1} sessions left for {label}")
        started = time.time()
        frames = [None] * len(legs)
        for pause in (*PAUSES, None):
            missing = [k for k, f in enumerate(frames) if f is None]
            got = _fetch_many(api, token, sec_id, segment, day, [legs[k] for k in missing], FIELDS, workers)
            for k, f in zip(missing, got):
                frames[k] = f
            if all(f is not None for f in frames):
                break
            if pause is None:
                raise DhanRejected(f"{name} {day} still failing after {sum(PAUSES) // 60} min of pauses ({LAST_ERROR})")
            log.warning("%s %s: %d of %d requests failed (%s); pausing %d min, then retrying those", name, day,
                        sum(f is None for f in frames), len(legs), LAST_ERROR, pause // 60)
            time.sleep(pause)
        good = [f for f in frames if not f.empty]
        df = (pd.concat(good)[cols].sort_values(["ts", "opt", "offset"]) if good else pd.DataFrame(columns=cols))
        _write(df, OUT / "options" / name / "chain" / f"{day.isoformat()}.parquet")
        log.info("%s %s: %d rows, %d strikes in %.0fs (%d/%d, %d requests used, gap %.2fs)", name, day, len(df),
                 df.strike.nunique(), time.time() - started, i, len(todo), BUDGET.spent, PACER.gap)


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--user", required=True, help="your OpenAlgo login username")
    p.add_argument("--years", type=float, default=5, help="how far back (Dhan keeps about 5 years)")
    p.add_argument("--underlyings", nargs="+", default=list(UNDERLYINGS), choices=list(UNDERLYINGS))
    p.add_argument("--width", type=int, default=10, help="strikes each side of ATM (max 10)")
    p.add_argument("--skip-options", action="store_true", help="index and VIX only")
    p.add_argument("--chains", choices=["expiry", "all", "none"], default="expiry",
                   help="which sessions get the full ATM+-width chain (all = ~42 requests per session; "
                        "expiry days first, then the rest newest first)")
    p.add_argument("--max-requests", type=int, default=40_000,
                   help="stop after this many Dhan requests (Dhan throttled at ~60k/day in practice)")
    p.add_argument("--max-rate", type=float, default=2.0,
                   help="requests per second ceiling (Dhan blocked the account after ~10k requests at 3.5/s)")
    p.add_argument("--workers", type=int, default=4, help="parallel requests (OpenAlgo still caps at 5/s)")
    p.add_argument("--expiry-max-ratio", type=float, default=0.006,
                   help="the week's lowest 15:25 ATM straddle / spot must also be below this to count as expiry")
    p.add_argument("--refresh", action="store_true", help="re-download what is already saved")
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s", force=True)
    get_auth_token, get_token, api = bootstrap()
    # OpenAlgo's imports reset the root logger, so the file handler goes on after them
    OUT.mkdir(parents=True, exist_ok=True)
    file_log = logging.FileHandler(OUT / "download.log", encoding="utf-8")
    file_log.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
    log.addHandler(file_log)
    log.setLevel(logging.INFO)
    # A self-generated web.dhan.co token can be supplied when the OAuth login token lacks an entitlement.
    token = os.environ.get("DHAN_ACCESS_TOKEN") or get_auth_token(args.user)
    if not token:
        log.error("No Dhan session for '%s'. Start OpenAlgo, log in and connect Dhan first.", args.user)
        return 1
    end = date.today()
    start = end - timedelta(days=int(args.years * 365))
    BUDGET.limit = args.max_requests
    PACER.floor = PACER.gap = 1 / args.max_rate
    log.info("Downloading %s .. %s into %s", start, end, OUT)

    vix_id = get_token(*VIX)
    if vix_id:
        download_index(api, token, vix_id, VIX[0], start, end, args.refresh)
    targets = []
    for name in args.underlyings:
        index_exchange, segment = UNDERLYINGS[name]
        sec_id = get_token(name, index_exchange)
        if not sec_id:
            log.error("No security id for %s in the master contract; skipping", name)
            continue
        download_index(api, token, sec_id, name, start, end, args.refresh)
        targets.append((name, sec_id, segment))
    width = min(args.width, 10)
    try:
        others = []
        for name, sec_id, segment in [] if args.skip_options else targets:
            days = trading_days(name, start, end)
            expiries, with_data = scan_expiries(api, token, sec_id, name, segment, days, args.refresh,
                                                args.workers, args.expiry_max_ratio)
            log.info("%s: %d expiry days and %d sessions with option data between %s and %s",
                     name, len(expiries), len(with_data), start, end)
            if args.chains != "none":
                download_chains(api, token, [(name, sec_id, segment, d) for d in expiries], width,
                                args.refresh, args.workers, f"{name} expiry days")
            skip = set(expiries)
            others += [(name, sec_id, segment, d) for d in with_data if d not in skip]
        if args.chains == "all":
            others.sort(key=lambda j: (j[3], j[0]), reverse=True)
            download_chains(api, token, others, width, args.refresh, args.workers, "non-expiry days")
    except DhanRejected as e:
        log.error("Dhan refused the expired-options request (%s). Index data is saved; options stopped. "
                  "Check the Dhan connection in OpenAlgo and rerun; it resumes.", e)
    except BudgetSpent as e:
        log.warning("Stopped at the request budget (%s). Rerun after midnight IST; it resumes.", e)
    log.info("Done (%d Dhan requests this run)", BUDGET.spent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
