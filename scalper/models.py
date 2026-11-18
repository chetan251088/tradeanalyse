from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class Instrument:
    symbol: str
    exchange: str
    kind: str  # index | option | vix
    underlying: str = ""
    strike: float = 0.0
    opt: str = ""  # CE | PE
    lotsize: int = 1
    tick_size: float = 0.05

    @property
    def key(self) -> tuple[str, str]:
        return (self.symbol, self.exchange)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> Instrument:
        return cls(
            symbol=d["symbol"],
            exchange=d["exchange"],
            kind=d["kind"],
            underlying=d.get("underlying", ""),
            strike=float(d.get("strike", 0) or 0),
            opt=d.get("opt", ""),
            lotsize=int(d.get("lotsize", 1) or 1),
            tick_size=float(d.get("tick_size", 0.05) or 0.05),
        )


@dataclass
class Signal:
    ts: int
    underlying: str
    strategy: str  # S1 | S2 | S3
    title: str
    side: str  # BUY | SELL
    legs: list[dict]
    score: float
    regime: str
    plan: dict = field(default_factory=dict)
    features: dict = field(default_factory=dict)
