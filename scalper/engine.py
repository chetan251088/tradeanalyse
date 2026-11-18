from __future__ import annotations

import logging
import math
from collections import Counter
from datetime import date, timedelta

from scalper.clock import MINUTE_MS, at, floor_ms, hhmm, to_dt
from scalper.config import Config
from scalper.models import Instrument, Signal
from scalper.notify import Notifier
from scalper.outcomes import FlyOutcome, Outcome, outcome_for
from scalper.regime import Regime, RegimeView, classify
from scalper.setups import Ctx, build_detectors
from scalper.state import InstrumentState, UnderlyingState
from scalper.store import Journal, TickRecorder

log = logging.getLogger("scalper.engine")

SESSION_MIN = 375
_URGENT = (Regime.CHAOS, Regime.BREAKOUT_UP, Regime.BREAKOUT_DOWN)


class Engine:
    """Single-threaded decision core: ticks in, state/regime/setups updated, alerts out.

    The same object runs live, on recordings and on the simulator, because time only
    ever comes from event timestamps passed in.
    """

    def __init__(
        self,
        cfg: Config,
        meta: dict,
        notifier: Notifier,
        journal: Journal | None = None,
        recorder: TickRecorder | None = None,
        live: bool = False,
        any_day: bool = False,
    ):
        self.cfg = cfg
        self.notifier = notifier
        self.journal = journal
        self.recorder = recorder
        self.live = live
        self.any_day = any_day
        self.day = date.fromisoformat(meta["day"])
        s = cfg.session
        self.start_ms = at(self.day, s.start)
        self.flatten_ms = at(self.day, s.flatten)
        self.end_ms = at(self.day, s.end)
        self.em_ms = at(self.day, s.expected_move_at)
        self.eval_ms = s.eval_interval_sec * 1000

        self.underlyings: dict[str, UnderlyingState] = {}
        self.lookup: dict[tuple[str, str], tuple[UnderlyingState | None, InstrumentState]] = {}
        self.detectors: dict[str, list] = {}
        self.regimes: dict[str, RegimeView] = {}
        self.regime_log: dict[str, list[tuple[str, str]]] = {}
        self.pending: dict[str, tuple[Regime, int]] = {}
        self.signals: list[Signal] = []
        self.outcomes: list[Outcome | FlyOutcome] = []
        self._tracking: dict[tuple[str, str], list[Outcome | FlyOutcome]] = {}
        self.cur_minute: int | None = None
        self.last_eval = 0

        for um in meta["underlyings"]:
            u = UnderlyingState(
                um["name"], um["index_exchange"], um["fo_exchange"], date.fromisoformat(um["expiry"]),
                bool(um["is_expiry_day"]), Instrument.from_dict(um["index"]), self.start_ms,
            )
            u.scan_enabled = u.is_expiry_day or any_day or not cfg.universe.expiry_only
            u.em_scale = _one_day_scale(self.day, u.expiry)
            self.underlyings[u.name] = u
            self.lookup[u.index.inst.key] = (u, u.index)
            self.detectors[u.name] = build_detectors(cfg.s1, cfg.s2, cfg.s3)
            self.regimes[u.name] = RegimeView(Regime.WARMUP)
            self.regime_log[u.name] = []
            self.add_options(u.name, um["options"])
        self.vix: InstrumentState | None = None
        if meta.get("vix"):
            self.vix = InstrumentState(Instrument.from_dict(meta["vix"]), self.start_ms)
            self.lookup[self.vix.inst.key] = (None, self.vix)

    # ---- universe ----------------------------------------------------------
    def add_options(self, underlying: str, options: list[dict]) -> list[Instrument]:
        u = self.underlyings[underlying]
        added = []
        for o in options:
            inst = Instrument.from_dict(o)
            if inst.symbol in u.options:
                continue
            st = u.add_option(inst)
            if o.get("seed"):
                st.seed(o["seed"])
            self.lookup[inst.key] = (u, st)
            added.append(inst)
        if added and u.center_strike is None and u.chain:
            strikes = sorted(u.chain)
            u.center_strike = strikes[len(strikes) // 2]
        return added

    def subscriptions(self) -> dict[int, list[tuple[str, str]]]:
        quote = [u.index.inst.key for u in self.underlyings.values()]
        if self.vix:
            quote.append(self.vix.inst.key)
        depth = [st.inst.key for u in self.underlyings.values() for st in u.options.values()]
        return {2: quote, 3: depth}

    # ---- event entry points -------------------------------------------------
    def on_tick(self, ts: int, symbol: str, exchange: str, data: dict) -> None:
        if ts >= self.end_ms:
            return
        self.advance(ts)
        hit = self.lookup.get((symbol, exchange))
        if hit is None:
            return
        u, st = hit
        if not st.on_tick(ts, data):
            return
        if self.recorder is not None:
            self.recorder.tick(ts, symbol, exchange, data)
        if ts < self.flatten_ms:
            for o in self._tracking.get((symbol, exchange), ()):
                o.update(ts, symbol, st.ltp)
        if u is not None and st is u.index and u.open_spot is None and ts >= self.start_ms:
            u.assign_open_labels(st.day_open or st.ltp)
            log.info("%s open %.2f, ITM-at-open labels assigned", u.name, u.open_spot)
        if ts - self.last_eval >= self.eval_ms:
            self.evaluate(ts)

    def advance(self, ts: int) -> None:
        """Move the engine clock; closes 1m/5m bars and runs the minute routine on each new minute."""
        minute = floor_ms(ts, MINUTE_MS)
        if self.cur_minute is None:
            self.cur_minute = minute
            return
        if minute <= self.cur_minute:
            return
        for _, st in self.lookup.values():
            st.roll(minute)
        self.cur_minute = minute
        self.on_minute(minute)

    def on_minute(self, minute: int) -> None:
        if minute < self.start_ms:
            return
        for name, u in self.underlyings.items():
            u.on_minute(minute, self.em_ms)
            view = self._confirm(name, classify(u, self.vix, minute, self.cfg.regime, self.live))
            prev = self.regimes[name].regime
            self.regimes[name] = view
            if view.regime != prev:
                self.regime_log[name].append((hhmm(minute), str(view.regime)))
                log.info("%s %s regime %s -> %s %s", hhmm(minute), name, prev, view.regime, _brief(view.metrics))
            if self.journal is not None:
                self.journal.minute(minute, name, str(view.regime), view.metrics)
        self.evaluate(minute)

    def _confirm(self, name: str, view: RegimeView) -> RegimeView:
        """Debounce regime changes; chaos and breakouts are urgent and switch immediately."""
        current = self.regimes[name].regime
        if view.regime == current or view.regime in _URGENT or current == Regime.WARMUP:
            self.pending[name] = (view.regime, 0)
            return view
        candidate, count = self.pending.get(name, (view.regime, 0))
        count = count + 1 if candidate == view.regime else 1
        self.pending[name] = (view.regime, count)
        if count >= self.cfg.regime.confirm_min:
            return view
        return RegimeView(current, view.metrics)

    def evaluate(self, ts: int) -> None:
        self.last_eval = ts
        if not self.start_ms <= ts < self.flatten_ms:
            return
        for name, u in self.underlyings.items():
            if not u.scan_enabled:
                continue
            ctx = Ctx(ts, u, self.regimes[name], self.cfg.risk, self.any_day)
            for det in self.detectors[name]:
                for sig in det.evaluate(ctx):
                    self.emit(sig)

    def emit(self, sig: Signal) -> None:
        self.signals.append(sig)
        outcome = outcome_for(sig)
        if outcome is not None:
            self.outcomes.append(outcome)
            exchange = sig.legs[0]["exchange"]
            for symbol in outcome.symbols:
                self._tracking.setdefault((symbol, exchange), []).append(outcome)
        if self.journal is not None:
            self.journal.signal(sig)
        self.notifier.send(sig.title, priority=7)

    # ---- reporting --------------------------------------------------------
    def summary(self) -> str:
        lines = [f"Day summary {self.day.isoformat()}"]
        for name, u in self.underlyings.items():
            tag = "expiry day" if u.is_expiry_day else "not expiry"
            lines.append(f"{name} ({tag}, expiry {u.expiry}): open {u.open_spot}, last {u.index.ltp:g}, EM {u.expected_move and round(u.expected_move, 1)}")
            changes = self.regime_log[name]
            if changes:
                lines.append("  regimes: " + ", ".join(f"{t} {r}" for t, r in changes))
        counts = Counter(s.strategy for s in self.signals)
        lines.append("alerts: " + (", ".join(f"{k} x{v}" for k, v in sorted(counts.items())) or "none"))
        for s in self.signals:
            lines.append(f"  {s.title.splitlines()[0]}  score {s.score}")
        if self.outcomes:
            tally = Counter(o.result for o in self.outcomes)
            lines.append(
                "paper outcomes (filled at alert price; S1 target T1, S2 2x, S3 take-profit vs loss = credit): "
                + ", ".join(f"{k} {v}" for k, v in sorted(tally.items()))
            )
            lines.extend(o.line() for o in self.outcomes)
        return "\n".join(lines)

    def finish(self) -> None:
        if self.journal is not None and self.outcomes:
            self.journal.outcomes([o.row() for o in self.outcomes])


def _one_day_scale(day: date, expiry: date) -> float:
    """sqrt(today's minutes / minutes to expiry), counting weekdays only (holidays ignored)."""
    later_days = sum(1 for i in range(1, (expiry - day).days + 1) if (day + timedelta(days=i)).weekday() < 5)
    return math.sqrt(SESSION_MIN / (SESSION_MIN * (1 + later_days)))


def _brief(m: dict) -> str:
    keys = ("move_frac", "range_frac", "straddle_chg_pct", "put_wall", "call_wall", "expansion", "reason")
    return " ".join(f"{k}={m[k]}" for k in keys if m.get(k) is not None)
