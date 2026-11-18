from __future__ import annotations

from collections import deque
from datetime import date
from statistics import median

from scalper.bars import Bar, BarSeries
from scalper.clock import MINUTE_MS
from scalper.models import Instrument


def _f(value) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


class InstrumentState:
    """Live view of one instrument: last price, book top, day OHLC, OI and 1m/5m bars."""

    def __init__(self, inst: Instrument, session_start_ms: int = 0):
        self.inst = inst
        self.session_start = session_start_ms
        self.ltp = 0.0
        self.bid = 0.0
        self.ask = 0.0
        self.day_open = 0.0
        self.day_high = 0.0
        self.day_low = 0.0
        self.low_ts = 0
        self.volume: float | None = None
        self.oi = 0.0
        self.last_ts = 0
        self.open_label = ""
        self.m1 = BarSeries(MINUTE_MS, 400)
        self.m5 = BarSeries(5 * MINUTE_MS, 100)

    @property
    def symbol(self) -> str:
        return self.inst.symbol

    def seed(self, q: dict) -> None:
        """Prime day OHLC/OI from an option-chain quote so a late start still knows the morning."""
        self.ltp = _f(q.get("ltp")) or self.ltp
        self.bid = _f(q.get("bid")) or self.bid
        self.ask = _f(q.get("ask")) or self.ask
        self.day_open = _f(q.get("open")) or self.day_open
        self.day_high = _f(q.get("high")) or self.day_high
        self.day_low = _f(q.get("low")) or self.day_low
        self.oi = _f(q.get("oi")) or self.oi

    def on_tick(self, ts: int, d: dict) -> bool:
        ltp = _f(d.get("ltp"))
        if ltp <= 0:
            return False
        self.ltp = ltp
        self.last_ts = ts
        o, h, lo = _f(d.get("open")), _f(d.get("high")), _f(d.get("low"))
        if o > 0:
            self.day_open = o
        elif self.day_open <= 0:
            self.day_open = ltp
        self.day_high = max(self.day_high, h, ltp)
        low = min(lo, ltp) if lo > 0 else ltp
        if self.day_low <= 0 or low < self.day_low:
            self.day_low = low
            self.low_ts = ts
        depth = d.get("depth")
        if depth:
            buy, sell = depth.get("buy") or [], depth.get("sell") or []
            self.bid = _f(buy[0].get("price")) if buy else 0.0
            self.ask = _f(sell[0].get("price")) if sell else 0.0
        else:
            if "bid" in d:
                self.bid = _f(d.get("bid"))
            if "ask" in d:
                self.ask = _f(d.get("ask"))
        vol = d.get("volume")
        if vol is not None:
            self.volume = _f(vol)
        if d.get("oi"):
            self.oi = _f(d.get("oi"))
        if ts >= self.session_start:
            self.m1.update(ts, ltp, self.volume, self.oi)
            self.m5.update(ts, ltp, self.volume, self.oi)
        return True

    def roll(self, ts: int) -> None:
        self.m1.roll(ts)
        self.m5.roll(ts)

    # ---- features -------------------------------------------------------
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return (self.bid + self.ask) / 2
        return self.ltp

    def spread_pct(self) -> float | None:
        if self.bid <= 0 or self.ask <= 0 or self.ask < self.bid:
            return None
        return (self.ask - self.bid) / self.mid() * 100

    def drop_from_open_pct(self) -> float:
        if self.day_open <= 0:
            return 0.0
        return (self.ltp / self.day_open - 1) * 100

    def bounce_from_low_pct(self) -> float:
        if self.day_low <= 0:
            return 0.0
        return (self.ltp / self.day_low - 1) * 100

    def red_streak_5m(self, bars: list[Bar] | None = None) -> int:
        n = 0
        for bar in reversed(bars if bars is not None else list(self.m5.closed)):
            if not bar.red:
                break
            n += 1
        return n

    def vol_ratio(self, n: int = 20) -> float:
        bars = self.m1.last(n + 1)
        if len(bars) < 3:
            return 0.0
        base = [b.volume for b in bars[:-1]]
        avg = sum(base) / len(base)
        return bars[-1].volume / avg if avg > 0 else 0.0

    def window_bars(self, minutes: int) -> list[Bar]:
        bars = self.m1.last(minutes - 1 if self.m1.current else minutes)
        if self.m1.current:
            bars.append(self.m1.current)
        return bars

    def min_low(self, minutes: int) -> float:
        bars = self.window_bars(minutes)
        return min(b.low for b in bars) if bars else self.ltp

    def oi_change_pct(self, minutes: int) -> float:
        bars = self.m1.last(minutes)
        if not bars or bars[0].oi <= 0 or self.oi <= 0:
            return 0.0
        return (self.oi / bars[0].oi - 1) * 100

    def snapshot(self, now_ts: int) -> dict:
        sp = self.spread_pct()
        return {
            "symbol": self.symbol,
            "open_label": self.open_label,
            "open": round(self.day_open, 2),
            "ltp": round(self.ltp, 2),
            "day_low": round(self.day_low, 2),
            "day_high": round(self.day_high, 2),
            "drop_from_open_pct": round(self.drop_from_open_pct(), 1),
            "red_5m_streak": self.red_streak_5m(),
            "mins_since_low": round((now_ts - self.low_ts) / MINUTE_MS, 1) if self.low_ts else None,
            "bounce_from_low_pct": round(self.bounce_from_low_pct(), 1),
            "vol_ratio": round(self.vol_ratio(), 2),
            "oi": self.oi,
            "oi_chg_15m_pct": round(self.oi_change_pct(15), 1),
            "bid": self.bid,
            "ask": self.ask,
            "spread_pct": round(sp, 2) if sp is not None else None,
        }


class UnderlyingState:
    """Index plus its option chain for one expiry."""

    def __init__(
        self,
        name: str,
        index_exchange: str,
        fo_exchange: str,
        expiry: date,
        is_expiry_day: bool,
        index_inst: Instrument,
        session_start_ms: int,
    ):
        self.name = name
        self.index_exchange = index_exchange
        self.fo_exchange = fo_exchange
        self.expiry = expiry
        self.is_expiry_day = is_expiry_day
        self.session_start = session_start_ms
        self.index = InstrumentState(index_inst, session_start_ms)
        self.options: dict[str, InstrumentState] = {}
        self.chain: dict[float, dict[str, InstrumentState]] = {}
        self.step = 0.0
        self.expected_move: float | None = None
        # Straddle prices the move to expiry; on earlier days scale it to roughly one day's move.
        self.em_scale = 1.0
        self.straddle_hist: deque[tuple[int, float]] = deque(maxlen=400)
        self.open_spot: float | None = None
        self.center_strike: float | None = None
        self.scan_enabled = True

    def add_option(self, inst: Instrument) -> InstrumentState:
        st = self.options.get(inst.symbol)
        if st is not None:
            return st
        st = InstrumentState(inst, self.session_start)
        self.options[inst.symbol] = st
        self.chain.setdefault(inst.strike, {})[inst.opt] = st
        strikes = sorted(self.chain)
        diffs = [b - a for a, b in zip(strikes, strikes[1:]) if b > a]
        self.step = min(diffs) if diffs else 0.0
        if self.open_spot is not None:
            st.open_label = self.label(inst.strike, inst.opt, self.open_spot)
        return st

    def atm(self, spot: float) -> float | None:
        if not self.chain or spot <= 0:
            return None
        return min(self.chain, key=lambda k: abs(k - spot))

    def moneyness(self, strike: float, opt: str, spot: float) -> int:
        """Signed strike steps from ATM: negative = in the money, positive = out of the money."""
        atm = self.atm(spot)
        if atm is None or self.step <= 0:
            return 0
        k = round((strike - atm) / self.step)
        return k if opt == "CE" else -k

    def label(self, strike: float, opt: str, spot: float) -> str:
        m = self.moneyness(strike, opt, spot)
        return "ATM" if m == 0 else (f"ITM{-m}" if m < 0 else f"OTM{m}")

    def assign_open_labels(self, spot: float) -> None:
        self.open_spot = spot
        for st in self.options.values():
            st.open_label = self.label(st.inst.strike, st.inst.opt, spot)

    def leg(self, strike: float, opt: str) -> InstrumentState | None:
        return self.chain.get(strike, {}).get(opt)

    def straddle(self, strike: float) -> float | None:
        ce, pe = self.leg(strike, "CE"), self.leg(strike, "PE")
        if not ce or not pe or ce.ltp <= 0 or pe.ltp <= 0:
            return None
        return ce.mid() + pe.mid()

    def straddle_change_pct(self, minutes: int) -> float | None:
        if len(self.straddle_hist) < 2:
            return None
        now_ts, now_val = self.straddle_hist[-1]
        cutoff = now_ts - minutes * MINUTE_MS
        past = [v for t, v in self.straddle_hist if t <= cutoff]
        if not past:
            return None
        return (now_val / past[-1] - 1) * 100

    def on_minute(self, minute_ts: int, expected_move_ms: int) -> None:
        spot = self.index.ltp
        atm = self.atm(spot)
        if atm is None:
            return
        value = self.straddle(atm)
        if value:
            self.straddle_hist.append((minute_ts, value))
            if self.expected_move is None and minute_ts >= expected_move_ms:
                self.expected_move = value * self.em_scale

    def oi_walls(self, spot: float) -> tuple[float | None, float | None]:
        calls = [(st.oi, k) for k, legs in self.chain.items() if k > spot and (st := legs.get("CE")) and st.oi > 0]
        puts = [(st.oi, k) for k, legs in self.chain.items() if k < spot and (st := legs.get("PE")) and st.oi > 0]
        return (max(calls)[1] if calls else None, max(puts)[1] if puts else None)

    def pcr(self) -> float | None:
        ce = sum(st.oi for st in self.options.values() if st.inst.opt == "CE")
        pe = sum(st.oi for st in self.options.values() if st.inst.opt == "PE")
        return pe / ce if ce > 0 else None

    def atm_spread_pct(self, spot: float, width: int = 2, min_price: float = 10.0) -> float | None:
        """Median bid/ask spread near ATM; near-worthless options (one tick is a big %) are ignored."""
        atm = self.atm(spot)
        if atm is None:
            return None
        spreads = []
        for i in range(-width, width + 1):
            for st in self.chain.get(atm + i * self.step, {}).values():
                sp = st.spread_pct()
                if sp is not None and st.mid() >= min_price:
                    spreads.append(sp)
        return median(spreads) if spreads else None
