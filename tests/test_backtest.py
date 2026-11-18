from datetime import date

import pandas as pd

from scalper.backtest import apply_overrides, day_events, next_expiry
from scalper.config import load_config
from scalper.notify import Notifier
from scalper.outcomes import FlyOutcome
from scalper.run import run_offline, sim_events

DAY = date(2026, 11, 5)


def _ts(hhmm: str) -> int:
    return int(pd.Timestamp(f"{DAY} {hhmm}", tz="Asia/Kolkata").timestamp())


def test_day_events_builds_contracts_from_rolling_rows():
    index = pd.DataFrame({"timestamp": [_ts("09:15"), _ts("09:16")], "open": [100.0, 101.0], "high": [101.0, 102.0],
                          "low": [99.0, 100.5], "close": [101.0, 101.5], "volume": [0, 0], "day": [DAY, DAY]})
    chain = pd.DataFrame({
        "ts": [_ts("09:15"), _ts("09:16"), _ts("09:15"), _ts("09:16")],
        "opt": ["CE", "CE", "PE", "PE"], "offset": [0, 1, 0, 0],
        "open": [10.0, 11.0, 9.0, 8.0], "high": [11.0, 12.0, 9.5, 8.5], "low": [9.5, 10.5, 8.5, 7.5],
        "close": [10.5, 11.5, 9.0, 8.0], "volume": [100, 200, 50, 60], "oi": [1000, 1100, 900, 950],
        "iv": [15.0] * 4, "strike": [100.0, 100.0, 100.0, 100.0], "spot": [101.0, 101.5, 101.0, 101.5],
    })
    meta, events = day_events("NIFTY", DAY, chain, index, None)
    symbols = {o["symbol"] for o in meta["underlyings"][0]["options"]}
    assert symbols == {"NIFTY05NOV26100CE", "NIFTY05NOV26100PE"}
    ce_ticks = [e for e in events if e[1] == "NIFTY05NOV26100CE"]
    assert len(ce_ticks) == 8 and ce_ticks[-1][3]["ltp"] == 11.5 and ce_ticks[0][3]["bid"] == 10.0 - 0.05
    assert [e[0] for e in events] == sorted(e[0] for e in events)

    meta, _ = day_events("NIFTY", DAY, chain, index, None, expiry=date(2026, 11, 10))
    u = meta["underlyings"][0]
    assert u["expiry"] == "2026-11-10" and u["is_expiry_day"] is False
    assert u["options"][0]["symbol"].startswith("NIFTY10NOV26")


def test_next_expiry_follows_calendar_then_weekday():
    cal = [date(2026, 10, 19), date(2026, 10, 27)]
    assert next_expiry(cal, date(2026, 10, 16)) == date(2026, 10, 19)
    assert next_expiry(cal, date(2026, 10, 27)) == date(2026, 10, 27)
    assert next_expiry(cal, date(2026, 10, 30)) == date(2026, 11, 3)


def test_greeks_recover_black_scholes_inputs():
    from scalper.greeks import add_greeks
    from scalper.sim import bs_price

    expiry = date(2026, 11, 10)
    ts = _ts("10:00") - 60  # bar closing at 10:00, 5 days + 5.5 h before expiry
    minutes_left = (5 * 24 + 5.5) * 60
    rows = []
    for opt in ("CE", "PE"):
        for strike in (24800.0, 25000.0, 25200.0):
            rows.append({"ts": ts, "opt": opt, "strike": strike, "spot": 25010.0,
                         "close": bs_price(25010.0, strike, minutes_left, 0.14, opt)})
    g = add_greeks(pd.DataFrame(rows), expiry)
    assert (g.iv_calc - 14).abs().max() < 0.01
    ce, pe = g[g.opt == "CE"].reset_index(), g[g.opt == "PE"].reset_index()
    assert ((ce.delta - pe.delta) - 1).abs().max() < 1e-6  # put-call parity
    assert ce.delta.is_monotonic_decreasing and (g.gamma > 0).all() and (g.theta < 0).all() and (g.vega > 0).all()
    atm = ce[ce.strike == 25000.0].iloc[0]
    assert 0.5 < atm.delta < 0.56 and -40 < atm.theta < -10


def test_greeks_mark_prices_below_intrinsic_as_nan():
    from scalper.greeks import add_greeks

    row = pd.DataFrame([{"ts": _ts("10:00"), "opt": "CE", "strike": 24000.0, "spot": 25000.0, "close": 990.0}])
    assert add_greeks(row, date(2026, 11, 10)).iv_calc.isna().all()


def test_overrides_are_validated():
    cfg = apply_overrides(load_config("config/dhan.yaml"), ["s1.min_drop_pct=60", "s1.window=['10:00','12:00']"])
    assert cfg.s1.min_drop_pct == 60 and cfg.s1.window == ("10:00", "12:00")


def test_sim_grades_s3_iron_fly():
    from scalper import sim

    meta, ticks = sim.generate(day=DAY)
    engine = run_offline(load_config("config/dhan.yaml"), sim_events(meta, ticks), Notifier())
    flies = [o for o in engine.outcomes if isinstance(o, FlyOutcome)]
    assert len(flies) == 1 and flies[0].credit > 0
    fly = flies[0]
    assert fly.result in ("open", "leg stop", "both stops")
    assert -1.05 <= fly.realized() <= fly.credit / fly.risk
    assert set(fly.stops) == {fly.legs[2][0], fly.legs[3][0]}
