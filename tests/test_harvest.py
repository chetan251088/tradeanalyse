import json
import math
import sqlite3
from datetime import date, datetime

import pandas as pd

from scalper.clock import IST
from scalper.config import Config, HarvestCfg
from scalper.harvest import (
    Harvester, build_plan, close_value, closed_days, gates, report, sessions_left, size_lots, vix_median, wing_strikes,
)
from scalper.notify import Notifier

HOLIDAYS = [
    {"date": "2026-05-01", "holiday_type": "TRADING_HOLIDAY", "closed_exchanges": ["NSE", "BSE", "NFO", "BFO"]},
    {"date": "2026-05-04", "holiday_type": "SETTLEMENT_HOLIDAY", "closed_exchanges": []},
    {"date": "2026-05-06", "holiday_type": "TRADING_HOLIDAY", "closed_exchanges": ["MCX"]},
]


def test_sessions_left_skips_weekends_and_fno_holidays():
    closed = closed_days(HOLIDAYS, "NFO")
    assert closed == {date(2026, 5, 1)}
    # Wed 29-Apr -> Tue 5-May: Thu 30, (Fri 1 holiday), Mon 4, Tue 5
    assert sessions_left(date(2026, 4, 29), date(2026, 5, 5), closed) == 3
    assert sessions_left(date(2026, 4, 28), date(2026, 5, 5), closed) == 4
    assert sessions_left(date(2026, 5, 5), date(2026, 5, 5), closed) == 0


def test_vix_median_uses_only_previous_days():
    days = pd.bdate_range("2025-01-01", periods=300).date
    s = pd.Series(range(300), index=days, dtype=float)
    today = days[-1]
    med, last, n = vix_median(s, today, 250, 120)
    assert last == days[-2] and n == 250
    assert med == s.iloc[-251:-1].median()
    assert math.isnan(vix_median(s.iloc[:50], days[50], 250, 120)[0])


def test_wings_and_sizing():
    h = HarvestCfg()
    assert wing_strikes(100, 50, h) == 4      # 2 x 100 / 50 = 4
    assert wing_strikes(400, 50, h) == 10     # capped
    assert wing_strikes(40, 50, h) == 4       # floor
    assert size_lots(1_000_000, 1.0, 150, 65) == 1
    assert size_lots(1_000_000, 1.0, 160, 65) == 0


def _chain(spot=24_010.0, atm_px=150.0, lot=65):
    rows = []
    for k in range(23_000, 25_050, 50):
        d = abs(k - 24_000)
        px = max(atm_px - d * 0.3, 1.0)
        q = lambda o, k=k, px=px: {"symbol": f"NIFTY{k}{o}", "bid": px - 0.5, "ask": px + 0.5, "ltp": px,  # noqa: E731
                                   "lotsize": lot}
        rows.append({"strike": k, "ce": q("CE"), "pe": q("PE")})
    return {"status": "success", "underlying_ltp": spot, "chain": rows}


def test_build_plan_prices_at_the_touch():
    plan, why = build_plan(_chain(), HarvestCfg(), 1_000_000)
    assert why == ""
    assert plan["atm"] == 24_000 and plan["step"] == 50
    assert plan["wing_strikes"] == 10 and plan["width"] == 500          # straddle 300 -> 2 x 300 / 50 = 12 -> 10
    sells = [x for x in plan["legs"] if x["side"] < 0]
    buys = [x for x in plan["legs"] if x["side"] > 0]
    assert {x["strike"] for x in sells} == {24_000} and {x["strike"] for x in buys} == {23_500, 24_500}
    assert plan["credit"] == round(2 * 149.5 - 2 * 1.5, 2)            # wings 500 out are floored at 1.0 (ask 1.5)
    assert plan["max_loss_unit"] == round(500 - plan["credit"], 2)
    assert plan["lots"] == size_lots(1_000_000, 1.0, plan["max_loss_unit"], 65)


def test_gates():
    h = HarvestCfg()
    plan = {"spread_cost": 4.0, "credit": 290.0, "lots": 1, "max_loss_rs": 13_000}
    assert gates(plan, h, 1_000_000, 1_000_000, 0, False) == []
    assert "already holding" in gates(plan, h, 1_000_000, 1_000_000, 0, True)[0]
    assert "paused" in gates(plan, h, 930_000, 1_000_000, 0, False)[0]
    assert "open risk" in gates(plan, h, 1_000_000, 1_000_000, 15_000, False)[0]
    assert "spreads" in gates({**plan, "spread_cost": 20.0}, h, 1_000_000, 1_000_000, 0, False)[0]


def test_close_value_buys_shorts_at_ask_sells_wings_at_bid():
    legs = [{"symbol": "A", "side": -1}, {"symbol": "B", "side": 1}]
    q = {"A": {"bid": 10, "ask": 11}, "B": {"bid": 1, "ask": 2}}
    assert close_value(legs, q, at_touch=True) == 11 - 1
    assert close_value(legs, q, at_touch=False) == 10.5 - 1.5
    assert close_value(legs, {"A": q["A"]}, at_touch=True) is None


class FakeRest:
    def __init__(self, expiry: date, vix: float = 15.0, chain=None):
        self.expiry, self.vix, self.chain = expiry, vix, chain or _chain()
        self.quotes: dict[str, dict] = {}

    def expiries(self, u):
        return [date(2026, 4, 21), self.expiry]

    def holidays(self, year):
        return HOLIDAYS

    def quote(self, symbol, exchange):
        return {"ltp": self.vix}

    def option_chain(self, u, expiry, strike_count):
        return self.chain

    def multiquotes(self, items):
        return {i["symbol"]: self.quotes[i["symbol"]] for i in items if i["symbol"] in self.quotes}


def _harvester(rest, vix_days=None):
    cfg = Config()
    cfg.underlyings = [u for u in cfg.underlyings if u.name == "NIFTY"]
    cfg.harvest.capital_rs = 2_000_000                                   # 1% = Rs 20,000 -> 1 lot of Rs 13,260
    days = pd.bdate_range(end="2026-04-27", periods=260).date
    hist = pd.Series(14.0, index=days) if vix_days is None else vix_days
    return Harvester(cfg, rest, sqlite3.connect(":memory:"), Notifier(), vix_history=lambda: hist,
                     index_close=lambda name, day, bar: 24_100.0)


def test_entry_mark_and_expiry_close():
    rest = FakeRest(expiry=date(2026, 5, 5))
    hv = _harvester(rest)
    # Tue 28-Apr: 5 sessions to 5-May minus the 1-May holiday = 4 -> enter (VIX 15 >= median 14)
    out = hv.check_entries(datetime(2026, 4, 28, 15, 22, tzinfo=IST))
    assert out[0][1] == "entry" and out[0][2] == "1 lots", out
    pos = hv.positions("open")
    assert len(pos) == 1 and pos[0]["expiry"] == "2026-05-05"
    assert hv.check_entries(datetime(2026, 4, 28, 15, 23, tzinfo=IST)) == []          # once per day
    # next day: not 4 sessions -> skip, and already holding anyway
    assert hv.check_entries(datetime(2026, 4, 29, 15, 22, tzinfo=IST))[0][1] == "skip"

    legs = json.loads(pos[0]["legs"])
    rest.quotes = {x["symbol"]: {"bid": 50.0 if x["side"] < 0 else 0.5, "ask": 51.0 if x["side"] < 0 else 0.6}
                   for x in legs}
    hv.mark_positions(datetime(2026, 4, 30, 15, 25, tzinfo=IST))
    assert hv.con.execute("SELECT count(*) FROM harvest_marks").fetchone()[0] == 1
    hv.mark_positions(datetime(2026, 5, 5, 15, 25, tzinfo=IST))
    done = hv.positions("closed")[0]
    assert done["exit_value"] == 2 * 51.0 - 2 * 0.5
    assert done["pnl_unit"] == done["credit"] - done["exit_value"]
    assert 0 < done["pnl_r"] < done["pnl_unit"] / done["max_loss_unit"]                  # charges taken off
    eq, _ = hv.equity()
    assert eq == hv.h.capital_rs + done["pnl_rs"]
    assert "Closed 1" in report(hv.con, hv.h)


def test_low_vix_skips_and_missed_expiry_settles_at_intrinsic():
    rest = FakeRest(expiry=date(2026, 5, 5), vix=12.0)
    hv = _harvester(rest)
    out = hv.check_entries(datetime(2026, 4, 28, 15, 22, tzinfo=IST))
    assert out[0][1] == "skip" and "not rich" in out[0][2]
    rest.vix = 15.0
    hv.con.execute("DELETE FROM harvest_checks")
    hv.check_entries(datetime(2026, 4, 28, 15, 22, tzinfo=IST))
    hv.mark_positions(datetime(2026, 5, 6, 15, 25, tzinfo=IST))                          # scanner was off on 5-May
    done = hv.positions("closed")[0]
    assert done["exit_value"] == 100.0                                                   # |24,100 - 24,000|
    assert "intrinsic" in done["note"]


def test_never_enters_on_an_expiry_day():
    rest = FakeRest(expiry=date(2026, 5, 5))
    rest.expiries = lambda u: [date(2026, 4, 28), date(2026, 5, 4)]                     # Mon expiry: 4 sessions from Tue
    hv = _harvester(rest)
    out = hv.check_entries(datetime(2026, 4, 28, 15, 22, tzinfo=IST))
    assert out[0][1] == "skip" and "expiry day" in out[0][2]
