from datetime import date, datetime

import pandas as pd

from scalper.clock import IST
from scalper.config import Config
from scalper.control import HELP, Controller
from scalper.index_update import append, last_stored_day, session_frame
from scalper.notify import Notifier, TelegramBot
from scalper.store import harvest_paused


def _cfg(tmp_path) -> Config:
    cfg = Config()
    cfg.paths.data_dir = str(tmp_path)
    cfg.paths.journal = str(tmp_path / "journal.sqlite")
    return cfg


def test_bot_from_env(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    assert TelegramBot.from_env() is None
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:abc")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "111, 222")
    bot = TelegramBot.from_env()
    assert bot is not None and bot.chat_ids == [111, 222]


def test_notifier_uses_the_scanner_bot():
    sent = []

    class FakeBot:
        def send(self, text, chat_id=None, pre=False):
            sent.append(text)
            return True

    n = Notifier(bot=FakeBot(), min_interval_sec=0)
    n.send("hello")
    for _ in range(100):
        if sent:
            break
        import time

        time.sleep(0.02)
    assert sent == ["hello"]


def test_controller_pause_resume_logs_help(tmp_path):
    cfg = _cfg(tmp_path)
    ctl = Controller(cfg)
    assert ctl.handle("/pause")[0].startswith("Harvest entries PAUSED")
    assert harvest_paused(cfg.paths.data_dir)
    ctl.handle("/resume@my_scanner_bot")
    assert not harvest_paused(cfg.paths.data_dir)
    assert ctl.handle("/nonsense")[0] == HELP
    assert "No log yet" in ctl.handle("/logs 5")[0]
    assert "Usage" in ctl.handle("/restart everything")[0]
    report, pre = ctl.handle("/report")
    assert pre and "No positions yet" in report


def test_session_frame_realigns_and_trims(tmp_path):
    day = date(2026, 10, 30)
    open_ist = int(datetime(2026, 10, 30, 9, 15, tzinfo=IST).timestamp())
    shift = 4 * 3600 + 1800  # OpenAlgo on a CET host returns IST times shifted by +4.5 h
    rows = [{"timestamp": open_ist + shift + 60 * i, "open": 1, "high": 2, "low": 0.5, "close": 1.5, "volume": 0}
            for i in range(380)]
    df = session_frame(rows, day, shift * 1000)
    assert len(df) == 375 and df.ts.iloc[0] == open_ist
    append("INDIAVIX", df, tmp_path)
    append("INDIAVIX", df, tmp_path)  # idempotent
    stored = pd.read_parquet(tmp_path / "INDIAVIX" / "2026.parquet")
    assert len(stored) == 375
    assert last_stored_day("INDIAVIX", tmp_path) == day
