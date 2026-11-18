from datetime import date

import pytest

from scalper.execution import Market, Paths, buy_option, charges
from scalper.models import Signal

DAY = date(2026, 11, 5)
T0 = 1_762_316_100_000  # any ms timestamp; only differences matter
MKT = Market({"NIFTY": 0.001}, latency_ms=15_000, slip_ticks=1, R=1500)


def _sig(plan: dict, strategy: str = "S1") -> Signal:
    return Signal(ts=T0, underlying="NIFTY", strategy=strategy, title="t", side="BUY",
                  legs=[{"symbol": "X", "exchange": "NFO", "action": "BUY", "price": 100.0}],
                  score=0, regime="NORMAL", plan=plan)


def _paths(prices: list[float], step_ms: int = 15_000) -> Paths:
    return Paths([(T0 + 15_000 + i * step_ms, "X", "NFO", {"ltp": p}) for i, p in enumerate(prices)])


S1_PLAN = {"stop": 90.0, "t1": 110.0, "t2": 120.0, "t3": 130.0}


def test_entry_pays_spread_and_slippage_after_latency():
    res = buy_option(_sig(S1_PLAN), _paths([100, 101, 102]), MKT, DAY, T0 + 3_600_000, "t1")
    assert res["entry_fill"] == pytest.approx(100 + 0.1 + 0.05)
    assert res["exit_reason"] == "15:20"


def test_target_fills_only_when_bid_reaches_limit():
    res = buy_option(_sig(S1_PLAN), _paths([100, 110.05, 110.2]), MKT, DAY, T0 + 3_600_000, "t1")
    assert res["exit_reason"] == "target" and res["exit_avg"] == 110.0
    assert res["gross_r"] == pytest.approx((110 - 100.15) / (100.15 - 90))


def test_stop_fills_at_stop_when_crossed_and_at_market_on_a_gap():
    crossed = buy_option(_sig(S1_PLAN), _paths([100, 95, 85]), MKT, DAY, T0 + 3_600_000, "t1")
    assert crossed["exit_reason"] == "stop"
    assert crossed["exit_avg"] == pytest.approx(90 - 0.09 - 0.05)
    gapped = buy_option(_sig(S1_PLAN), _paths([100, 95, 80], step_ms=120_000), MKT, DAY, T0 + 3_600_000, "t1")
    assert gapped["exit_avg"] == pytest.approx(80 - 0.08 - 0.05)
    assert gapped["net_r"] < crossed["net_r"] < -1


def test_plan_moves_stop_to_breakeven_after_t1():
    res = buy_option(_sig(S1_PLAN), _paths([100, 110.3, 100.5, 99]), MKT, DAY, T0 + 3_600_000, "plan")
    assert res["exit_reason"] == "target+breakeven"
    assert 0 < res["gross_r"] < 0.5


def test_s2_plan_scales_out_and_trails():
    plan = {"sell_50pct_at": 20.0, "sell_25pct_at": 40.0, "runner_trail_pct": 30}
    res = buy_option(_sig(plan, "S2"), _paths([10, 20.1, 30, 27, 20]), MKT, DAY, T0 + 3_600_000, "plan")
    assert res["exit_reason"] == "target+trail"
    assert res["gross_r"] > 0.4


def test_charges_include_brokerage_stt_and_gst():
    c = charges("NFO", DAY, 10_000, 11_000, 2)
    assert 40 < c < 70
    assert charges("NFO", date(2024, 1, 1), 10_000, 11_000, 2) < c  # lower STT before Oct 2024
