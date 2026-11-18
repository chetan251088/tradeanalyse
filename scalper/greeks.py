"""Black-Scholes greeks for downloaded chains (Dhan's expired-options API has no greeks).

  from scalper.greeks import add_greeks
  chain = add_greeks(pd.read_parquet(path), expiry)   # expiry: scalper.backtest.next_expiry(...)

Adds iv_calc (implied vol solved from each bar's close, in %), delta, gamma, theta (Rs per calendar
day) and vega (Rs per 1 vol point). Uses spot, r = 0 and calendar time to 15:30 IST on expiry,
the same convention as scalper.sim. Dhan's own `iv` column is kept as-is; it has outliers
(0 and >1000%) on deep in-the-money strikes, so the greeks use iv_calc.
"""

from __future__ import annotations

from datetime import date, datetime, time
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd

_YEAR_S = 365 * 24 * 3600
_IST = ZoneInfo("Asia/Kolkata")


def _ncdf(x: np.ndarray) -> np.ndarray:
    # Abramowitz-Stegun 7.1.26 (|error| < 1.5e-7), vectorised without scipy
    z = np.abs(x) / np.sqrt(2.0)
    t = 1.0 / (1.0 + 0.3275911 * z)
    poly = t * (0.254829592 + t * (-0.284496736 + t * (1.421413741 + t * (-1.453152027 + t * 1.061405429))))
    erf = 1.0 - poly * np.exp(-z * z)
    return 0.5 * (1.0 + np.sign(x) * erf)


def _npdf(x: np.ndarray) -> np.ndarray:
    return np.exp(-0.5 * x * x) / np.sqrt(2.0 * np.pi)


def _price(spot, strike, t, sigma, is_call):
    sd = sigma * np.sqrt(t)
    d1 = (np.log(spot / strike) + 0.5 * sd * sd) / sd
    call = spot * _ncdf(d1) - strike * _ncdf(d1 - sd)
    return np.where(is_call, call, call - spot + strike)


def implied_vol(price, spot, strike, t, is_call, lo=0.005, hi=5.0, steps=60) -> np.ndarray:
    """Bisection; NaN where the price is at/below intrinsic or above the hi-vol price."""
    price, spot, strike, t = (np.asarray(a, dtype=float) for a in (price, spot, strike, t))
    is_call = np.asarray(is_call, dtype=bool)
    a, b = np.full_like(price, lo), np.full_like(price, hi)
    for _ in range(steps):
        mid = 0.5 * (a + b)
        high = _price(spot, strike, t, mid, is_call) > price
        b, a = np.where(high, mid, b), np.where(high, a, mid)
    iv = 0.5 * (a + b)
    intrinsic = np.where(is_call, np.maximum(spot - strike, 0), np.maximum(strike - spot, 0))
    bad = (price <= intrinsic + 1e-9) | (price >= _price(spot, strike, t, np.full_like(price, hi), is_call))
    return np.where(bad, np.nan, iv)


def add_greeks(chain: pd.DataFrame, expiry: date) -> pd.DataFrame:
    expiry_ts = datetime.combine(expiry, time(15, 30), _IST).timestamp()
    spot, strike = chain.spot.to_numpy(float), chain.strike.to_numpy(float)
    is_call = (chain.opt == "CE").to_numpy()
    # bar timestamps are the minute's start; greeks are for its close
    t = np.maximum(expiry_ts - (chain.ts.to_numpy(float) + 60), 30) / _YEAR_S
    iv = implied_vol(chain.close.to_numpy(float), spot, strike, t, is_call)
    sqrt_t = np.sqrt(t)
    d1 = (np.log(spot / strike) + 0.5 * iv * iv * t) / (iv * sqrt_t)
    pdf = _npdf(d1)
    return chain.assign(
        iv_calc=iv * 100,
        delta=np.where(is_call, _ncdf(d1), _ncdf(d1) - 1.0),
        gamma=pdf / (spot * iv * sqrt_t),
        theta=-spot * pdf * iv / (2 * sqrt_t) / 365,
        vega=spot * pdf * sqrt_t / 100,
    )
