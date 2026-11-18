"""Options-flow analytics on numpy arrays: Black-76 IV and greeks, gamma exposure, max pain, OI build-up, trade side.

Everything here is pure (no I/O) so the engine can call it every second for a whole chain in a few milliseconds.
Prices are per unit, OI and volume in contracts' underlying quantity (Dhan reports quantity, not lots).
"""

from __future__ import annotations

import math

import numpy as np

from scalper.greeks import _ncdf, _npdf

YEAR_S = 365 * 24 * 3600

LONG, WRITE, COVER, UNWIND = "LONG", "WRITE", "COVER", "UNWIND"


def b76_price(f, k, t, sigma, is_call):
    sd = sigma * np.sqrt(t)
    d1 = (np.log(f / k) + 0.5 * sd * sd) / sd
    call = f * _ncdf(d1) - k * _ncdf(d1 - sd)
    return np.where(is_call, call, call - f + k)


def b76_iv(price, f, k, t, is_call, lo: float = 0.01, hi: float = 3.0, steps: int = 40) -> np.ndarray:
    """Implied vol by bisection (r = 0); NaN at or below intrinsic and above the hi-vol price."""
    price, f, k, t = (np.asarray(a, dtype=float) for a in (price, f, k, t))
    is_call = np.asarray(is_call, dtype=bool)
    a, b = np.full_like(price, lo), np.full_like(price, hi)
    for _ in range(steps):
        mid = 0.5 * (a + b)
        high = b76_price(f, k, t, mid, is_call) > price
        b, a = np.where(high, mid, b), np.where(high, a, mid)
    intrinsic = np.where(is_call, np.maximum(f - k, 0), np.maximum(k - f, 0))
    bad = ~(price > intrinsic + 1e-6) | (price >= b76_price(f, k, t, np.full_like(price, hi), is_call))
    return np.where(bad, np.nan, 0.5 * (a + b))


def b76_greeks(f, k, t, iv, is_call) -> dict[str, np.ndarray]:
    """delta, gamma (per point), theta (Rs per calendar day per unit), vega (Rs per vol point per unit)."""
    sq = np.sqrt(t)
    sd = iv * sq
    d1 = (np.log(f / k) + 0.5 * sd * sd) / sd
    pdf = _npdf(d1)
    return {
        "delta": np.where(is_call, _ncdf(d1), _ncdf(d1) - 1.0),
        "gamma": pdf / (f * sd),
        "theta": -f * pdf * iv / (2 * sq) / 365,
        "vega": f * pdf * sq / 100,
    }


def synthetic_forward(strikes, ce_mid, pe_mid, spot: float) -> float:
    """Put-call parity F = K + C - P, median over the three strikes nearest spot; spot if no clean pair."""
    strikes, ce_mid, pe_mid = (np.asarray(a, dtype=float) for a in (strikes, ce_mid, pe_mid))
    ok = (ce_mid > 0) & (pe_mid > 0)
    if not ok.any():
        return spot
    k, c, p = strikes[ok], ce_mid[ok], pe_mid[ok]
    near = np.argsort(np.abs(k - spot))[:3]
    return float(np.median(k[near] + c[near] - p[near]))


def gex(gamma, oi, spot: float, is_call) -> np.ndarray:
    """Rs of delta to hedge per 1% index move; calls positive, puts negative (the usual GEX sign convention)."""
    return np.where(is_call, 1.0, -1.0) * np.nan_to_num(gamma) * np.asarray(oi, dtype=float) * spot * spot * 0.01


def gamma_profile(strikes, is_call, iv, oi, t: float, grid) -> np.ndarray:
    """Net GEX if the index were at each grid level (vols and OI held fixed)."""
    k = np.asarray(strikes, dtype=float)[None, :]
    sig = np.nan_to_num(np.asarray(iv, dtype=float), nan=0.0)[None, :]
    ok = sig > 0
    s = np.asarray(grid, dtype=float)[:, None]
    sd = np.where(ok, sig, 1.0) * math.sqrt(t)
    d1 = (np.log(s / k) + 0.5 * sd * sd) / sd
    gam = np.where(ok, _npdf(d1) / (s * sd), 0.0)
    sign = np.where(np.asarray(is_call, dtype=bool), 1.0, -1.0)[None, :]
    return (sign * gam * np.asarray(oi, dtype=float)[None, :]).sum(axis=1) * s[:, 0] ** 2 * 0.01


def flip_level(grid, net, spot: float) -> float | None:
    """Index level where net GEX changes sign, nearest to spot (linear interpolation)."""
    grid, net = np.asarray(grid, dtype=float), np.asarray(net, dtype=float)
    idx = np.nonzero(np.sign(net[:-1]) * np.sign(net[1:]) < 0)[0]
    if not len(idx):
        return None
    lv = grid[idx] - net[idx] * (grid[idx + 1] - grid[idx]) / (net[idx + 1] - net[idx])
    return float(lv[np.argmin(np.abs(lv - spot))])


def max_pain(strikes, ce_oi, pe_oi) -> float | None:
    """Settlement strike that pays option holders least (over the strikes we watch)."""
    k = np.asarray(strikes, dtype=float)
    if not len(k):
        return None
    c, p = np.asarray(ce_oi, dtype=float), np.asarray(pe_oi, dtype=float)
    s = k[:, None]
    pay = (np.maximum(s - k[None, :], 0) * c[None, :] + np.maximum(k[None, :] - s, 0) * p[None, :]).sum(axis=1)
    return float(k[int(np.argmin(pay))])


def buildup(dp: float, doi: float) -> str:
    """Price/OI quadrant: LONG build-up, WRITE (short build-up), short COVER(ing), long UNWIND(ing)."""
    if doi > 0:
        return LONG if dp > 0 else WRITE if dp < 0 else ""
    if doi < 0:
        return COVER if dp > 0 else UNWIND if dp < 0 else ""
    return ""


def trade_side(ltp: float, bid: float, ask: float, last_ltp: float, last_side: int) -> int:
    """+1 buyer lifted the offer, -1 seller hit the bid (quote rule, tick rule when the quote is unusable)."""
    if bid > 0 and ask >= bid:
        if ltp >= ask:
            return 1
        if ltp <= bid:
            return -1
        mid = 0.5 * (bid + ask)
        if ltp != mid:
            return 1 if ltp > mid else -1
    if last_ltp > 0 and ltp != last_ltp:
        return 1 if ltp > last_ltp else -1
    return last_side


def straddle_value(f: float, k: float, t: float, sigma: float) -> float:
    c = b76_price(np.array([f]), np.array([k]), np.array([t]), np.array([sigma]), np.array([True]))[0]
    return float(2 * c - f + k)


def decay_split(f0: float, s0: float, t0: float, f1: float, s1: float, t1: float, k: float) -> dict[str, float]:
    """Model change of the k-straddle split into time decay, index move and vol change (sums to the model total)."""
    m = straddle_value
    base = m(f0, k, t0, s0)
    theta = m(f0, k, t1, s0) - base
    move = m(f1, k, t1, s0) - m(f0, k, t1, s0)
    vol = m(f1, k, t1, s1) - m(f1, k, t1, s0)
    return {"theta": theta, "move": move, "vol": vol, "model": theta + move + vol}


def imbalance(bid_qty: float, ask_qty: float) -> float:
    tot = bid_qty + ask_qty
    return (bid_qty - ask_qty) / tot if tot > 0 else 0.0


def biggest_level(levels: list[dict]) -> tuple[float, float, float]:
    """(price, quantity, quantity / median level quantity) of the largest resting level."""
    qty = [float(x.get("quantity") or 0) for x in levels if x.get("price")]
    if not qty:
        return 0.0, 0.0, 0.0
    i = int(np.argmax(qty))
    med = float(np.median(qty)) or 1.0
    px = [float(x.get("price") or 0) for x in levels if x.get("price")]
    return px[i], qty[i], qty[i] / med
