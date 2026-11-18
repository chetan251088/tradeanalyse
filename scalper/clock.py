from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from functools import lru_cache

IST = timezone(timedelta(hours=5, minutes=30))
MINUTE_MS = 60_000


def now_ms() -> int:
    return int(datetime.now(IST).timestamp() * 1000)


def to_dt(ts_ms: int) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000, IST)


def hhmm(ts_ms: int) -> str:
    return to_dt(ts_ms).strftime("%H:%M")


@lru_cache(maxsize=256)
def parse_hhmm(text: str) -> time:
    h, m = text.split(":")
    return time(int(h), int(m))


def at(day: date, text: str) -> int:
    return int(datetime.combine(day, parse_hhmm(text), IST).timestamp() * 1000)


def floor_ms(ts_ms: int, period_ms: int) -> int:
    return ts_ms - ts_ms % period_ms


def in_window(ts_ms: int, window: tuple[str, str]) -> bool:
    t = to_dt(ts_ms).time()
    return parse_hhmm(window[0]) <= t < parse_hhmm(window[1])


def after(ts_ms: int, text: str) -> bool:
    return to_dt(ts_ms).time() >= parse_hhmm(text)
