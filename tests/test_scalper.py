from datetime import date

import pytest

from scalper import sim
from scalper.bars import BarSeries
from scalper.clock import MINUTE_MS, at, hhmm
from scalper.config import load_config
from scalper.models import Instrument
from scalper.notify import Notifier
from scalper.run import recording_events, run_offline, sim_events
from scalper.state import UnderlyingState
from scalper.store import Journal, TickRecorder

DAY = date(2026, 11, 5)
CFG = "config/dhan.yaml"


def _cfg():
    cfg = load_config(CFG)
    cfg.s1.enabled = cfg.s2.enabled = True  # off in the shipped config, still exercised here
    return cfg


def _sim_engine(any_day=False, expiry=True, journal=None, recorder=None):
    cfg = _cfg()
    meta, ticks = sim.generate(day=DAY)
    meta["underlyings"][0]["is_expiry_day"] = expiry
    return run_offline(cfg, sim_events(meta, ticks), Notifier(), journal, any_day, recorder)


@pytest.fixture(scope="module")
def sim_engine():
    return _sim_engine()


def test_bars_roll_and_fill_quiet_minutes():
    s = BarSeries(MINUTE_MS)
    t0 = at(DAY, "09:15")
    s.update(t0 + 1_000, 100.0, cum_volume=1000)
    s.update(t0 + 30_000, 104.0, cum_volume=1500)
    s.update(t0 + 50_000, 99.0, cum_volume=1600)
    s.update(t0 + 3 * MINUTE_MS + 5_000, 101.0, cum_volume=1700)
    bars = list(s.closed)
    assert [hhmm(b.start) for b in bars] == ["09:15", "09:16", "09:17"]
    first = bars[0]
    assert (first.open, first.high, first.low, first.close, first.volume) == (100.0, 104.0, 99.0, 99.0, 600.0)
    assert bars[1].open == bars[1].close == 99.0 and bars[1].volume == 0
    assert s.current.volume == 100.0


def test_out_of_order_tick_is_ignored():
    s = BarSeries(MINUTE_MS)
    t0 = at(DAY, "09:15")
    s.update(t0 + MINUTE_MS + 1_000, 50.0)
    s.update(t0 + 2 * MINUTE_MS + 1_000, 51.0)
    s.update(t0 + MINUTE_MS + 5_000, 10.0)
    assert s.closed[-1].low == 50.0


def test_config_rejects_unknown_keys(tmp_path):
    cfg = load_config(CFG)
    assert cfg.s1.window == ("09:30", "12:30") and cfg.underlyings[1].name == "SENSEX"
    bad = tmp_path / "bad.yaml"
    bad.write_text("s1:\n  min_drop: 40\n", encoding="utf-8")
    with pytest.raises(ValueError, match="min_drop"):
        load_config(bad)


def test_moneyness_labels():
    u = UnderlyingState("NIFTY", "NSE_INDEX", "NFO", DAY, True, Instrument("NIFTY", "NSE_INDEX", "index"), 0)
    for k in (24900, 24950, 25000, 25050, 25100):
        for opt in ("CE", "PE"):
            u.add_option(Instrument(f"N{k}{opt}", "NFO", "option", "NIFTY", k, opt, 65))
    assert u.label(24900, "CE", 25010) == "ITM2"
    assert u.label(24900, "PE", 25010) == "OTM2"
    assert u.label(25000, "PE", 25010) == "ATM"
    assert u.label(25100, "PE", 25010) == "ITM2"


def test_sim_day_fires_each_setup_in_its_window(sim_engine):
    by = {s.strategy: s for s in reversed(sim_engine.signals)}
    assert set(by) == {"S1", "S2", "S3"}
    s1, s2, s3 = by["S1"], by["S2"], by["S3"]
    assert "10:38" <= hhmm(s1.ts) <= "11:05" and s1.legs[0]["symbol"].endswith("PE")
    assert s1.plan["stop"] < s1.plan["entry"] < s1.plan["t1"] and s1.plan["risk_rs"] <= 1500
    assert "14:30" <= hhmm(s2.ts) <= "14:50" and s2.legs[0]["symbol"].endswith("CE")
    assert "12:00" <= hhmm(s3.ts) <= "14:30" and [l["action"] for l in s3.legs] == ["BUY", "BUY", "SELL", "SELL"]
    assert all(s.ts < at(DAY, "15:20") for s in sim_engine.signals)


def test_regime_story(sim_engine):
    changes = dict(sim_engine.regime_log["SENSEX"])
    regimes = list(changes.values())
    assert "TREND_UP" in regimes and "RANGE" in regimes and "BREAKOUT_UP" in regimes
    assert regimes.index("RANGE") < regimes.index("BREAKOUT_UP")


def test_scanners_stay_off_on_non_expiry_day_unless_forced():
    assert _sim_engine(expiry=False).signals == []
    assert {s.strategy for s in _sim_engine(any_day=True, expiry=False).signals} == {"S1", "S2", "S3"}


def test_recording_replays_to_identical_alerts(tmp_path, sim_engine):
    path = tmp_path / "day.jsonl.gz"
    recorder = TickRecorder(path)
    first = _sim_engine(recorder=recorder)
    recorder.close()
    journal = Journal(tmp_path / "j.sqlite")
    replayed = run_offline(_cfg(), recording_events(path), Notifier(), journal)
    assert [(s.strategy, s.ts, s.legs[0]["symbol"]) for s in replayed.signals] == [
        (s.strategy, s.ts, s.legs[0]["symbol"]) for s in first.signals
    ]
    rows = journal.con.execute("SELECT strategy, COUNT(*) FROM signals GROUP BY strategy").fetchall()
    assert dict(rows) == {"S1": 1, "S2": 1, "S3": 1}
    journal.close()
