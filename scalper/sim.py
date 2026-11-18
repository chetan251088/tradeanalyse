"""Synthetic expiry day for offline testing when the market is closed.

SENSEX path: morning rally that crushes in-the-money puts, a pull-back (bounce),
a pinned midday range between OI walls, then a late breakout that makes cheap
out-of-the-money calls explode. Option prices come from Black-Scholes with the
clock running down to 15:30, so time decay and gamma behave like a real expiry.
"""

from __future__ import annotations

import math
import random
from collections.abc import Iterator
from datetime import date

from scalper.clock import MINUTE_MS, at

_SQRT2 = math.sqrt(2.0)
_YEAR_MIN = 365 * 24 * 60

# (time, index level) anchors; the path is interpolated between them.
PATH = [
    ("09:15", 75000), ("10:40", 75430), ("11:05", 75300), ("11:20", 75370),
    ("14:30", 75370), ("14:50", 75650), ("15:30", 75620),
]
RANGE_FROM, RANGE_TO, RANGE_AMP = "11:20", "14:30", 15.0


def _ncdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / _SQRT2))


def bs_price(spot: float, strike: float, minutes_left: float, iv: float, opt: str) -> float:
    t = max(minutes_left, 0.5) / _YEAR_MIN
    sd = iv * math.sqrt(t)
    d1 = (math.log(spot / strike) + 0.5 * sd * sd) / sd
    d2 = d1 - sd
    call = spot * _ncdf(d1) - strike * _ncdf(d2)
    return call if opt == "CE" else call - spot + strike


def _anchor_level(minute: float, anchors: list[tuple[float, float]]) -> float:
    for (m0, v0), (m1, v1) in zip(anchors, anchors[1:]):
        if m0 <= minute <= m1:
            return v0 + (v1 - v0) * (minute - m0) / (m1 - m0) if m1 > m0 else v1
    return anchors[-1][1]


def _tick_round(x: float, tick: float = 0.05) -> float:
    return round(max(round(x / tick) * tick, tick), 2)


def generate(day: date = date(2026, 11, 5), seed: int = 7, step_sec: int = 5) -> tuple[dict, Iterator[tuple]]:
    """Return (meta, ticks) for one synthetic SENSEX expiry day."""
    rnd = random.Random(seed)
    lot, step, center = 20, 100, 75000
    strikes = [center + i * step for i in range(-10, 11)]
    opts = []
    for k in strikes:
        for opt in ("CE", "PE"):
            opts.append({
                "symbol": f"SENSEX{day.strftime('%d%b%y').upper()}{k}{opt}", "exchange": "BFO", "kind": "option",
                "underlying": "SENSEX", "strike": float(k), "opt": opt, "lotsize": lot, "tick_size": 0.05,
            })
    meta = {
        "day": day.isoformat(),
        "underlyings": [{
            "name": "SENSEX", "index_exchange": "BSE_INDEX", "fo_exchange": "BFO",
            "expiry": day.isoformat(), "is_expiry_day": True,
            "index": {"symbol": "SENSEX", "exchange": "BSE_INDEX", "kind": "index", "underlying": "SENSEX"},
            "options": opts,
        }],
        "vix": {"symbol": "INDIAVIX", "exchange": "NSE_INDEX", "kind": "vix"},
    }
    start, close = at(day, "09:15"), at(day, "15:30")

    def mins(text: str) -> float:
        return (at(day, text) - start) / MINUTE_MS

    anchors = [(mins(t), float(v)) for t, v in PATH]
    r0, r1 = mins(RANGE_FROM), mins(RANGE_TO)

    def ticks() -> Iterator[tuple]:
        noise = 0.0
        ohlc: dict[str, list[float]] = {}
        cum_vol: dict[str, float] = {o["symbol"]: 0.0 for o in opts}
        spot_hist: list[float] = []
        oi = {
            o["symbol"]: 3e5 + 2e6 * math.exp(-(((o["strike"] - (75600 if o["opt"] == "CE" else 75200)) / 250) ** 2))
            for o in opts
        }
        ts = start

        def bar(sym: str, px: float) -> dict:
            o = ohlc.setdefault(sym, [px, px, px])
            o[1], o[2] = max(o[1], px), min(o[2], px)
            return {"open": o[0], "high": o[1], "low": o[2]}

        while ts <= close:
            minute = (ts - start) / MINUTE_MS
            noise = 0.8 * noise + rnd.gauss(0, 1.5)
            level = _anchor_level(minute, anchors) + noise
            if r0 <= minute <= r1:
                level += RANGE_AMP * math.sin(2 * math.pi * (minute - r0) / 20)
            spot = round(level, 2)
            minutes_left = (close - ts) / MINUTE_MS
            # traded volume grows with the square of the index's 1-minute speed, like real breakouts
            spot_hist.append(spot)
            speed = abs(spot - spot_hist[-13]) if len(spot_hist) > 12 else 0.0
            intensity = 1 + (speed / 10) ** 2

            yield ts, "SENSEX", "BSE_INDEX", {"ltp": spot, **bar("SENSEX", spot)}
            vix = round(13.0 + rnd.gauss(0, 0.03), 2)
            yield ts, "INDIAVIX", "NSE_INDEX", {"ltp": vix, **bar("INDIAVIX", vix)}
            for o in opts:
                sym = o["symbol"]
                px = _tick_round(bs_price(spot, o["strike"], minutes_left, 0.14, o["opt"]))
                half = max(0.05, _tick_round(px * 0.0015))
                near = math.exp(-(((o["strike"] - spot) / 300) ** 2))
                cum_vol[sym] += lot * intensity * (0.2 + near) * (5 + rnd.random())
                oi[sym] *= 1 + rnd.gauss(0, 0.0005)
                yield ts, sym, "BFO", {
                    "ltp": px, **bar(sym, px), "volume": round(cum_vol[sym]), "oi": round(oi[sym]),
                    "bid": _tick_round(px - half), "ask": _tick_round(px + half),
                }
            ts += step_sec * 1000

    return meta, ticks()
