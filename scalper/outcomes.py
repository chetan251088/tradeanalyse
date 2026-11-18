from __future__ import annotations

from dataclasses import dataclass, field

from scalper.clock import at, hhmm, to_dt
from scalper.models import Signal

# S2 has no hard stop in its plan; grade it against losing this share of the premium.
S2_GRADE_STOP = 0.5


@dataclass
class Outcome:
    """Paper result of a BUY alert, filled at the alert price: which came first, target or stop."""

    sig: Signal
    symbol: str
    entry: float
    stop: float
    target: float
    peak: float
    trough: float
    last: float
    result: str = "open"
    resolved_ts: int = 0

    @property
    def symbols(self) -> list[str]:
        return [self.symbol]

    def update(self, ts: int, symbol: str, ltp: float) -> None:
        self.last = ltp
        if self.result != "open":
            return
        self.peak = max(self.peak, ltp)
        self.trough = min(self.trough, ltp)
        if ltp <= self.stop:
            self.result, self.resolved_ts = "stop", ts
        elif ltp >= self.target:
            self.result, self.resolved_ts = "target", ts

    @property
    def risk(self) -> float:
        return max(self.entry - self.stop, 1e-9)

    def r(self, price: float) -> float:
        return (price - self.entry) / self.risk

    def realized(self) -> float:
        """R banked by the simple rule: exit all at target or stop, else at the 15:20 price."""
        price = {"target": self.target, "stop": self.stop}.get(self.result, self.last)
        return self.r(price)

    def line(self) -> str:
        head = self.sig.title.splitlines()[0].split("] ", 1)[-1]
        res = (f"OPEN at 15:20, last {self.last:g} ({self.r(self.last):+.1f}R)" if self.result == "open"
               else f"{self.result.upper()} first at {hhmm(self.resolved_ts)}")
        return (f"  {self.sig.strategy} {head}: entry {self.entry:g} stop {self.stop:g} target {self.target:g} -> {res}"
                f" | best {self.r(self.peak):+.1f}R ({self.peak:g}) worst {self.r(self.trough):+.1f}R ({self.trough:g})")

    def row(self) -> tuple:
        return (self.sig.ts, self.sig.strategy, self.symbol, self.entry, self.stop, self.target, self.result,
                self.resolved_ts, round(self.r(self.peak), 2), round(self.r(self.trough), 2), round(self.r(self.last), 2),
                round(self.realized(), 3))


@dataclass
class FlyOutcome:
    """Paper result of the S3 iron fly managed by its plan; P&L per unit in R = wing width - credit.

    Legs are (symbol, +1 bought / -1 sold, price) in the signal's order: CE wing, PE wing, CE short, PE short.
    A short leg trading at/above its stop is bought back with its wing; the other short's stop then moves
    to its sell price; whatever is left is valued at the plan's exit time.
    """

    sig: Signal
    legs: list[tuple[str, int, float]]
    credit: float
    width: float
    exit_ts: int
    stops: dict[str, float]
    prices: dict[str, float] = field(default_factory=dict)
    closed: dict[str, float] = field(default_factory=dict)
    best: float = 0.0
    worst: float = 0.0
    last_pnl: float = 0.0
    result: str = "open"
    resolved_ts: int = 0

    @property
    def symbols(self) -> list[str]:
        return [s for s, _, _ in self.legs]

    @property
    def symbol(self) -> str:
        return self.legs[2][0]

    @property
    def risk(self) -> float:
        return max(self.width - self.credit, 1e-9)

    def pnl(self) -> float:
        return sum(sign * (self.closed.get(s, self.prices.get(s, p)) - p) for s, sign, p in self.legs)

    def update(self, ts: int, symbol: str, ltp: float) -> None:
        if ts >= self.exit_ts or symbol in self.closed:
            return
        self.prices[symbol] = ltp
        stop = self.stops.get(symbol)
        if stop is not None and ltp >= stop:
            (wc, _, _), (wp, _, _), (sc, _, sell_c), (sp, _, sell_p) = self.legs
            wing, other, other_sell = (wc, sp, sell_p) if symbol == sc else (wp, sc, sell_c)
            self.closed[symbol] = ltp
            self.closed[wing] = self.prices.get(wing, 0.0)
            if other not in self.closed:
                self.stops[other] = min(self.stops[other], other_sell)
            self.result = "both stops" if other in self.closed else "leg stop"
            self.resolved_ts = ts
        self.last_pnl = self.pnl() / self.risk
        self.best, self.worst = max(self.best, self.last_pnl), min(self.worst, self.last_pnl)

    def line(self) -> str:
        head = self.sig.title.splitlines()[0].split("] ", 1)[-1]
        stops = "no stop" if self.result == "open" else f"{self.result} at {hhmm(self.resolved_ts)}"
        return (f"  S3 {head}: credit {self.credit:.2f} of width {self.width:g}, {stops} -> {self.last_pnl:+.2f}R "
                f"at {hhmm(self.exit_ts)} | best {self.best:+.2f}R worst {self.worst:+.2f}R")

    def realized(self) -> float:
        return self.last_pnl

    def row(self) -> tuple:
        return (self.sig.ts, "S3", self.symbol, self.credit, self.width, self.credit, self.result, self.resolved_ts,
                round(self.best, 2), round(self.worst, 2), round(self.last_pnl, 2), round(self.realized(), 3))


def outcome_for(sig: Signal) -> Outcome | FlyOutcome | None:
    if not sig.legs:
        return None
    p = sig.plan
    if sig.strategy == "S3":
        legs = [(leg["symbol"], 1 if leg["action"] == "BUY" else -1, float(leg["price"])) for leg in sig.legs]
        credit = float(p["credit"])
        if credit <= 0:
            return None
        stops = {legs[2][0]: float(p["stop_ce"]), legs[3][0]: float(p["stop_pe"])}
        exit_ts = at(to_dt(sig.ts).date(), p.get("exit", "15:15"))
        return FlyOutcome(sig, legs, credit, float(p["width"]), exit_ts, stops, {s: price for s, _, price in legs})
    entry = float(sig.legs[0]["price"])
    if sig.strategy == "S1":
        stop, target = float(p["stop"]), float(p["t1"])
    elif sig.strategy == "S2":
        stop, target = entry * S2_GRADE_STOP, float(p["sell_50pct_at"])
    else:
        return None
    return Outcome(sig, sig.legs[0]["symbol"], entry, stop, target, entry, entry, entry)
