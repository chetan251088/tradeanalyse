"""Bounce watch: paper-tracks the fake-bounce triggers of scalper/study_minibounce.py on the dashboard's chain.

Display only, nothing is ever ordered. A contract qualifies when it trades at <= half its 09:20 price and still
costs >= Rs 5, 09:45-15:00 IST. On each closed minute the current expiry is checked; per side the qualifying
contract nearest the money that fires becomes a paper ticket (one per side per 20 minutes), bought at the ask
+ 1 tick and graded on 1-minute bars like the study (stop first when both hit in one bar, exits at the bid,
charges for `lots` lots). The backtest column comes from data/research/minibounce_<NAME>.csv.
"""

from __future__ import annotations

import csv
import math
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np

from scalper.backtest import HALF_SPREAD, TICK
from scalper.clock import MINUTE_MS, at, to_dt
from scalper.execution import charges

RESEARCH = Path("data/research")
TRIGGERS = ("flush", "turn", "flush+turn", "buy burst", "writers cover", "turn+cover")
PLANS = (("5 min", None, None, 5), ("10 min", None, None, 10), ("+10/-10 10m", 0.10, 0.10, 10),
         ("+20/-10 15m", 0.20, 0.10, 15))
MAIN = "+10/-10 10m"
FALL, MIN_PX, COOLDOWN = 0.5, 5.0, 20


def _hs(name: str, px: float) -> float:
    return max(TICK, px * HALF_SPREAD.get(name, 0.002))


def _sell(name: str, px: float) -> float:
    return max(px - _hs(name, px) - TICK, 0.0)


class Bars:
    """One contract's 1-minute bars for the session, oldest first."""

    __slots__ = ("m", "o", "h", "l", "c", "v", "oi", "lo", "ref", "pos")

    def __init__(self, mins, t0: int, t1: int, t_ref: int):
        rows = [(x[0], *(x[6:9] if len(x) >= 9 and x[6] > 0 else (x[1], x[1], x[1])), x[1], x[5], x[2])
                for x in mins if t0 <= x[0] < t1 and x[1] > 0]
        a = np.array(rows, float).reshape(-1, 7)
        self.m = a[:, 0].astype(np.int64)
        self.o, self.h, self.l, self.c, self.v, self.oi = (a[:, j] for j in range(1, 7))
        self.lo = np.minimum.accumulate(self.l) if len(a) else self.l
        j = int(np.searchsorted(self.m, t_ref))
        self.ref = float(self.c[j]) if j < len(self.m) and self.m[j] < t_ref + 5 * MINUTE_MS else 0.0
        self.pos = {int(m): i for i, m in enumerate(self.m)}


def fired(b: Bars, i: int, cover: bool) -> list[str]:
    """Study triggers that fire on bar i (empty unless the contract is down to <= half its 09:20 price)."""
    c, o, h, l = b.c, b.o, b.h, b.l
    if not (b.ref > 0 and MIN_PX <= c[i] <= FALL * b.ref):
        return []
    green = c[i] > o[i]

    def flush_at(j: int) -> bool:
        return j >= 10 and c[j - 10] > 0 and c[j] <= 0.75 * c[j - 10]

    turn = bool(i >= 4 and green and c[i] > h[i - 1] and l[i - 3:i].min() <= b.lo[i - 1])
    flushed = any(flush_at(j) for j in range(max(i - 5, 0), i + 1))
    dv = np.clip(np.diff(b.v[max(i - 31, 0):i + 1]), 0, None)
    burst = bool(green and len(dv) >= 11 and dv[-1] > 0 and dv[-1] >= 3 * dv[:-1].mean())
    hits = {"flush": flush_at(i), "turn": turn, "flush+turn": turn and flushed, "buy burst": burst,
            "writers cover": cover and green, "turn+cover": turn and cover}
    return [t for t in TRIGGERS if hits[t]]


def covering(side: dict[float, Bars], spot: float, step: float, m: int) -> bool:
    """Writers of this side buying back: OI of the ATM+-2 strikes down >= 1% over 5 minutes."""
    atm = round(spot / step) * step
    now = before = 0.0
    n = 0
    for k in atm + step * np.arange(-2, 3):
        b = side.get(float(k))
        i = b.pos.get(m) if b else None
        if i is None or i < 5 or b.oi[i] <= 0 or b.oi[i - 5] <= 0:
            continue
        now, before, n = now + b.oi[i], before + b.oi[i - 5], n + 1
    return n >= 3 and now / before - 1 <= -0.01


def label(trig: list[str]) -> str:
    parts = [p for p, keys in (("flush", ("flush", "flush+turn")), ("turn", ("turn",)), ("burst", ("buy burst",)),
                               ("cover", ("writers cover", "turn+cover"))) if any(t in trig for t in keys)]
    return "+".join(parts)


def expectations(name: str) -> dict[str, dict[str, tuple[int, float, float]]]:
    """Test-period backtest per trigger (plus 'any' pooled over triggers): plan -> (trades, win rate, day-weighted avg)."""
    f = RESEARCH / f"minibounce_{name}.csv"
    if not f.exists():
        return {}
    per: dict = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    with f.open(newline="") as fh:
        for r in csv.DictReader(fh):
            if r["day"] < "2024-07-01":
                continue
            groups = ["any", r["trigger"]] if r["trigger"] != "every 5 min" else ["every 5 min"]
            for g in groups:
                for plan, *_ in PLANS:
                    if r.get(plan):
                        per[g][plan][r["day"]].append(float(r[plan]))
    out: dict = {}
    for g, plans in per.items():
        out[g] = {}
        for plan, days in plans.items():
            rets = [x for d in days.values() for x in d]
            out[g][plan] = (len(rets), sum(x > 0 for x in rets) / len(rets),
                            float(np.mean([np.mean(d) for d in days.values()])))
    return out


@dataclass
class Ticket:
    id: int
    m: int
    key: tuple
    exchange: str
    strike: float
    is_call: bool
    trig: list
    fall: float
    px: float
    entry: float
    qty: int
    best: float = 0.0
    res: dict = field(default_factory=dict)

    @property
    def done(self) -> bool:
        return len(self.res) == len(PLANS)


class _Book:
    def __init__(self, day: date):
        self.day, self.tickets, self.done = day, [], 0
        self.next_ok = {True: 0, False: 0}


class BounceWatch:
    def __init__(self, lots: int = 5):
        self.lots = lots
        self.books: dict[str, _Book] = {}
        self.expect: dict[str, dict] = {}
        self.seq = 0

    def reset(self, name: str) -> None:
        self.books.pop(name, None)

    def scan(self, name: str, step: float, opts: dict, candles: list, spot_now: float, upto: int,
             live_book: bool) -> list[tuple[str, str, str, float]]:
        """Process every closed minute up to `upto`; returns feed events (key, severity, text, strike)."""
        day = to_dt(upto).date()
        st = self.books.get(name)
        if st is None or st.day != day:
            st = self.books[name] = _Book(day)
        if name not in self.expect:
            self.expect[name] = expectations(name)
        t0, t1 = at(day, "09:15"), at(day, "15:30")
        start, last, close = at(day, "09:45"), at(day, "15:00"), at(day, "15:20")
        bars = {key: Bars(o.mins, t0, t1, at(day, "09:20")) for key, o in opts.items()}
        minutes = sorted({int(m) for b in bars.values() for m in b.m if st.done < m <= upto})
        spot_at = {c[0]: c[4] for c in candles}
        events = []
        for m in minutes:
            for t in st.tickets:
                b = bars.get(t.key)
                j = b.pos.get(m) if b and not t.done and m > t.m else None
                if j is not None and self._grade(name, t, b, j, m, close) and live_book:
                    events.append(self._result_event(name, st, t))
            if start <= m <= last:
                spot = spot_at.get(m) or spot_now
                for is_call in (True, False):
                    t = self._signal(name, st, bars, opts, is_call, spot, step, m, live_book and m == upto)
                    if t and live_book:
                        events.append(self._new_event(name, t))
            st.done = m
        return events

    def _signal(self, name, st, bars, opts, is_call, spot, step, m, use_book) -> Ticket | None:
        if m < st.next_ok[is_call] or spot <= 0:
            return None
        side = {k: b for (k, c), b in bars.items() if c == is_call and len(b.m)}
        cover = covering(side, spot, step, m)
        best = None
        for k, b in side.items():
            i = b.pos.get(m)
            trig = fired(b, i, cover) if i is not None else []
            if trig and (best is None or abs(k - spot) < best[0]):
                best = (abs(k - spot), k, b, i, trig)
        if best is None:
            return None
        _, k, b, i, trig = best
        o = opts[(k, is_call)]
        px = float(b.c[i])
        ask = o.ask if use_book and o.bid > 0 and o.ask >= o.bid and o.ask - o.bid <= max(0.3 * o.ask, 1.0) else px + _hs(name, px)
        self.seq += 1
        t = Ticket(self.seq, m, (k, is_call), o.exchange, k, is_call, trig, 1 - px / b.ref, px, ask + TICK,
                   self.lots * max(o.lot, 1))
        st.tickets.append(t)
        st.next_ok[is_call] = m + COOLDOWN * MINUTE_MS
        return t

    def _grade(self, name: str, t: Ticket, b: Bars, j: int, m: int, close: int) -> bool:
        """Apply bar j to the ticket's open plans; True when the main plan just closed."""
        o, h, l, c = (float(x[j]) for x in (b.o, b.h, b.l, b.c))
        t.best = max(t.best, (h - _hs(name, h)) / t.entry - 1)
        el = (m - t.m) // MINUTE_MS
        main_open = MAIN not in t.res
        for plan, tgt, stp, mins in PLANS:
            if plan in t.res:
                continue
            if stp and l <= t.entry * (1 - stp):
                out, why = _sell(name, min(o, t.entry * (1 - stp))), "stop"
            elif tgt and h - _hs(name, h) >= t.entry * (1 + tgt):
                out, why = t.entry * (1 + tgt), "target"
            elif el >= mins or m >= close:
                out, why = _sell(name, c), "time"
            else:
                continue
            fee = charges(t.exchange, to_dt(m).date(), t.entry * t.qty, out * t.qty, 2)
            t.res[plan] = (out / t.entry - 1 - fee / (t.entry * t.qty), why, int(el))
        return main_open and MAIN in t.res

    def _bt(self, name: str, trig: list[str]) -> tuple[str, tuple | None]:
        ex = self.expect.get(name) or {}
        for g in reversed(TRIGGERS):
            if g in trig and MAIN in ex.get(g, {}):
                return g, ex[g][MAIN]
        return "", None

    def _new_event(self, name: str, t: Ticket) -> tuple[str, str, str, float]:
        g, bt = self._bt(name, t.trig)
        hist = (f" Backtest of '{g}' ({MAIN}): wins {bt[1]:.0%}, avg {bt[2]:+.1%}." if bt else "")
        return (f"bounce:{t.id}", "med",
                f"BOUNCE WATCH (paper): {t.strike:g} {'CE' if t.is_call else 'PE'} down {t.fall:.0%} since 09:20, "
                f"{label(t.trig)} -> paper buy at {t.entry:.2f}.{hist}", t.strike)

    def _result_event(self, name: str, st: _Book, t: Ticket) -> tuple[str, str, str, float]:
        ret, why, el = t.res[MAIN]
        done = [x.res[MAIN][0] for x in st.tickets if MAIN in x.res]
        return (f"bounce-r:{t.id}", "low",
                f"BOUNCE WATCH result {t.strike:g} {'CE' if t.is_call else 'PE'}: {why} after {el}m, {ret:+.1%} after "
                f"costs ({MAIN}). Today {sum(x > 0 for x in done)}/{len(done)} wins, avg {np.mean(done):+.1%}", t.strike)

    def view(self, name: str, opts: dict, mode_live: bool, limit: int = 12) -> dict:
        st = self.books.get(name)
        ex = self.expect.get(name) or {}
        tickets = st.tickets if st else []
        today = []
        for plan, *_ in PLANS:
            r = [t.res[plan][0] for t in tickets if plan in t.res]
            today.append([plan, len(r), _r(sum(x > 0 for x in r) / len(r)) if r else None, _r(np.mean(r)) if r else None])
        bt = [[plan, *(list(map(_r, ex["any"][plan])) if plan in ex.get("any", {}) else [None] * 3)] for plan, *_ in PLANS]
        base = ex.get("every 5 min", {}).get(MAIN)
        rows = []
        for t in reversed(tickets[-limit:]):
            o = opts.get(t.key)
            mark = None
            if o is not None and not t.done:
                bid = o.bid if mode_live and o.bid > 0 else _sell(name, o.ltp) if o.ltp > 0 else 0.0
                mark = _r(bid / t.entry - 1) if bid > 0 else None
            rows.append({"id": t.id, "ts": t.m, "k": t.strike, "side": "CE" if t.is_call else "PE", "lbl": label(t.trig),
                         "fall": _r(t.fall), "entry": _r(t.entry), "best": _r(t.best), "mark": mark,
                         "res": {p: [_r(v[0]), v[1], v[2]] for p, v in t.res.items()}})
        return {"lots": self.lots, "main": MAIN, "plans": [p for p, *_ in PLANS], "today": today, "bt": bt,
                "base": _r(base[2]) if base else None, "tickets": rows, "n": len(tickets),
                "open": sum(not t.done for t in tickets)}


def _r(x, n: int = 4):
    x = float(x)
    return round(x, n) if math.isfinite(x) else None
