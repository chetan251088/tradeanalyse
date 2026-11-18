from __future__ import annotations

from collections import deque
from dataclasses import dataclass

# Quiet gaps longer than this (e.g. overnight) are not back-filled with flat bars.
MAX_GAP_FILL = 120


@dataclass
class Bar:
    start: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0
    oi: float = 0.0

    @property
    def green(self) -> bool:
        return self.close > self.open

    @property
    def red(self) -> bool:
        return self.close < self.open

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2

    @property
    def range(self) -> float:
        return self.high - self.low


class BarSeries:
    """Fixed-period OHLCV bars built from ticks; volume comes from the broker's cumulative day volume."""

    def __init__(self, period_ms: int, maxlen: int = 400):
        self.period = period_ms
        self.closed: deque[Bar] = deque(maxlen=maxlen)
        self.current: Bar | None = None
        self._last_close: float | None = None
        self._last_oi = 0.0
        self._last_cum_vol: float | None = None
        self._bar_vol_base: float | None = None

    def roll(self, ts: int) -> None:
        """Close bars that ended at or before ``ts``; minutes without ticks become flat bars."""
        boundary = ts - ts % self.period
        if self.current is not None and self.current.start + self.period <= boundary:
            self.closed.append(self.current)
            self._last_close = self.current.close
            self.current = None
        if self.current is not None or not self.closed or self._last_close is None:
            return
        start = self.closed[-1].start + self.period
        if (boundary - start) // self.period > MAX_GAP_FILL:
            return
        c = self._last_close
        while start < boundary:
            self.closed.append(Bar(start, c, c, c, c, 0.0, self._last_oi))
            start += self.period

    def update(self, ts: int, price: float, cum_volume: float | None = None, oi: float | None = None) -> None:
        self.roll(ts)
        start = ts - ts % self.period
        if self.current is not None and start < self.current.start:
            return
        if self.current is None and self.closed and start < self.closed[-1].start + self.period:
            return
        if oi:
            self._last_oi = float(oi)
        bar = self.current
        if bar is None:
            bar = self.current = Bar(start, price, price, price, price, 0.0, self._last_oi)
            self._bar_vol_base = self._last_cum_vol
        else:
            if price > bar.high:
                bar.high = price
            if price < bar.low:
                bar.low = price
            bar.close = price
        bar.oi = self._last_oi
        if cum_volume is not None:
            cum_volume = float(cum_volume)
            if self._bar_vol_base is None:
                self._bar_vol_base = cum_volume
            bar.volume = max(cum_volume - self._bar_vol_base, 0.0)
            self._last_cum_vol = cum_volume

    def last(self, n: int) -> list[Bar]:
        if n <= 0:
            return []
        return list(self.closed)[-n:]
