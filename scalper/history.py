"""Rebuild a past session from Dhan 1-minute bars (through OpenAlgo) so the scanner can run on real data.

Each 1m bar becomes four ticks (open, then the nearer extreme, the other extreme, close),
volume is spread evenly and bid/ask is set one tick either side, so results are an
approximation of the live feed. Only contracts that are still listed can be fetched,
so days whose weekly contract has already expired replay against the next expiry.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from scalper.clock import MINUTE_MS, at, to_dt
from scalper.config import Config, UnderlyingCfg
from scalper.store import TickRecorder
from scalper.universe import Rest, _expiry_code, chain_instruments, with_retry

log = logging.getLogger("scalper.history")

_OFFSETS_MS = (5_000, 20_000, 35_000, 55_000)
_HALF_HOUR_MS = 30 * MINUTE_MS


def session_shift_ms(index_rows: list[dict], day: date, session_start: str) -> int:
    """OpenAlgo's Dhan history converts times with the host's local zone; realign the first bar to the open."""
    first = int(index_rows[0]["timestamp"]) * 1000
    shift = round((first - at(day, session_start)) / _HALF_HOUR_MS) * _HALF_HOUR_MS
    if shift:
        log.warning("History timestamps off by %+.1fh (first bar at %s); correcting", shift / 3_600_000,
                    to_dt(first).strftime("%H:%M"))
    return shift


def fetch_bars(rest: Rest, symbol: str, exchange: str, day: date) -> list[dict]:
    res = with_retry(lambda: rest.c._post("history", {
        "apikey": rest.api_key, "symbol": symbol, "exchange": exchange, "interval": "1m",
        "start_date": day.isoformat(), "end_date": day.isoformat(), "source": "api",
    }))
    if not isinstance(res, dict) or res.get("status") != "success":
        log.warning("No history for %s %s: %s", exchange, symbol, (res or {}).get("message") if isinstance(res, dict) else res)
        return []
    return sorted(res.get("data") or [], key=lambda r: r["timestamp"])


def bar_ticks(rows: list[dict], tick: float | None, shift_ms: int = 0, day: date | None = None) -> list[tuple[int, dict]]:
    out: list[tuple[int, dict]] = []
    if day is not None:
        # Dhan returns the following session too when start_date == end_date
        rows = [r for r in rows if to_dt(int(r["timestamp"]) * 1000 - shift_ms).date() == day]
    if not rows:
        return out
    day_open, hi, lo, cum = float(rows[0]["open"]), float("-inf"), float("inf"), 0.0
    for r in rows:
        t0 = int(r["timestamp"]) * 1000 - shift_ms
        o, h, l, c = (float(r[k]) for k in ("open", "high", "low", "close"))
        path = (o, l, h, c) if c >= o else (o, h, l, c)
        vol, oi = float(r.get("volume") or 0), r.get("oi")
        for off, px in zip(_OFFSETS_MS, path):
            cum += vol / 4
            hi, lo = max(hi, px), min(lo, px)
            d = {"ltp": px, "open": day_open, "high": hi, "low": lo, "volume": round(cum)}
            if oi:
                d["oi"] = oi
            if tick:
                d["bid"] = round(max(px - tick, tick), 2)
                d["ask"] = round(px + tick, 2)
            out.append((t0 + off, d))
    return out


def _underlying(rest: Rest, u: UnderlyingCfg, day: date, cfg: Config, events: list) -> tuple[dict | None, int]:
    strike_count = cfg.universe.strike_count
    upcoming = [d for d in rest.expiries(u) if d >= day]
    if not upcoming:
        return None, 0
    expiry = upcoming[0]
    current = chain_instruments(u, rest.option_chain(u, expiry, strike_count))
    strikes = sorted({o["strike"] for o in current})
    diffs = [b - a for a, b in zip(strikes, strikes[1:]) if b > a]
    if not diffs:
        return None, 0
    step, lot, tick = min(diffs), current[0]["lotsize"], current[0]["tick_size"]

    index_rows = fetch_bars(rest, u.name, u.index_exchange, day)
    if not index_rows:
        log.warning("No %s index data for %s (holiday or not available)", u.name, day)
        return None, 0
    shift = session_shift_ms(index_rows, day, cfg.session.start)
    for ts, d in bar_ticks(index_rows, None, shift, day):
        events.append((ts, u.name, u.index_exchange, d))

    atm = round(float(index_rows[0]["open"]) / step) * step
    code = _expiry_code(expiry)
    options, missing = [], 0
    for i in range(-strike_count, strike_count + 1):
        strike = atm + i * step
        for opt in ("CE", "PE"):
            symbol = f"{u.name}{code}{strike:g}{opt}"
            rows = fetch_bars(rest, symbol, u.fo_exchange, day)
            if not rows:
                missing += 1
                continue
            options.append({
                "symbol": symbol, "exchange": u.fo_exchange, "kind": "option", "underlying": u.name,
                "strike": float(strike), "opt": opt, "lotsize": lot, "tick_size": tick,
            })
            for ts, d in bar_ticks(rows, tick, shift, day):
                events.append((ts, symbol, u.fo_exchange, d))
    log.info("%s %s: expiry %s, %d option contracts (%d unavailable)", u.name, day, expiry, len(options), missing)
    return {
        "name": u.name, "index_exchange": u.index_exchange, "fo_exchange": u.fo_exchange,
        "expiry": expiry.isoformat(), "is_expiry_day": expiry == day,
        "index": {"symbol": u.name, "exchange": u.index_exchange, "kind": "index", "underlying": u.name},
        "options": options,
    }, shift


def build_history_recording(rest: Rest, cfg: Config, day: date, path: Path) -> Path:
    """Fetch the day for every enabled underlying and write it as a normal tick recording."""
    events: list[tuple[int, str, str, dict]] = []
    underlyings = []
    shift = 0
    for u in cfg.underlyings:
        if u.enabled:
            um, s = _underlying(rest, u, day, cfg, events)
            if um:
                underlyings.append(um)
                shift = s
    if not underlyings:
        raise RuntimeError(f"No data fetched for {day}")
    vix = {"symbol": cfg.universe.vix_symbol, "exchange": cfg.universe.vix_exchange, "kind": "vix"}
    for ts, d in bar_ticks(fetch_bars(rest, vix["symbol"], vix["exchange"], day), None, shift, day):
        events.append((ts, vix["symbol"], vix["exchange"], d))
    events.sort(key=lambda e: e[0])
    tmp = path.with_suffix(".partial")
    recorder = TickRecorder(tmp)
    recorder.meta({"day": day.isoformat(), "underlyings": underlyings, "vix": vix, "source": "history-1m"})
    for ts, symbol, exchange, d in events:
        recorder.tick(ts, symbol, exchange, d)
    recorder.close()
    tmp.replace(path)
    log.info("Saved %d ticks to %s", len(events), path)
    return path
