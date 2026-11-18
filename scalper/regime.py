from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from scalper.clock import after
from scalper.config import RegimeCfg
from scalper.state import InstrumentState, UnderlyingState


class Regime(StrEnum):
    WARMUP = "WARMUP"
    NORMAL = "NORMAL"
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGE = "RANGE"
    BREAKOUT_UP = "BREAKOUT_UP"
    BREAKOUT_DOWN = "BREAKOUT_DOWN"
    CHAOS = "CHAOS"


@dataclass
class RegimeView:
    regime: Regime
    metrics: dict = field(default_factory=dict)

    @property
    def direction(self) -> int:
        if self.regime in (Regime.TREND_UP, Regime.BREAKOUT_UP):
            return 1
        if self.regime in (Regime.TREND_DOWN, Regime.BREAKOUT_DOWN):
            return -1
        return 0


def _r(x: float | None, nd: int = 3) -> float | None:
    return round(x, nd) if x is not None else None


def classify(u: UnderlyingState, vix: InstrumentState | None, now_ts: int, cfg: RegimeCfg, live: bool) -> RegimeView:
    idx = u.index
    spot = idx.ltp
    bars = list(idx.m1.closed)
    m: dict = {"spot": spot, "bars": len(bars)}
    em = u.expected_move
    if spot <= 0 or not em or len(bars) < cfg.min_bars or idx.day_open <= 0:
        return RegimeView(Regime.WARMUP, m)

    move_frac = (spot - idx.day_open) / em
    look = bars[-cfg.range_lookback_min:]
    range_frac = (max(b.high for b in look) - min(b.low for b in look)) / em
    straddle_chg = u.straddle_change_pct(cfg.straddle_lookback_min)
    call_wall, put_wall = u.oi_walls(spot)
    band = spot * cfg.wall_band_pct / 100
    inside_walls = bool(
        call_wall and put_wall and put_wall < spot < call_wall
        and call_wall - spot <= band and spot - put_wall <= band
    )
    vix_jump = (vix.ltp / vix.day_open - 1) * 100 if vix and vix.ltp > 0 and vix.day_open > 0 else 0.0
    atm_spread = u.atm_spread_pct(spot)
    stale = live and idx.last_ts > 0 and now_ts - idx.last_ts > cfg.stale_sec * 1000
    m.update(
        em=round(em, 2),
        move_frac=_r(move_frac),
        range_frac=_r(range_frac),
        straddle_chg_pct=_r(straddle_chg, 2),
        call_wall=call_wall,
        put_wall=put_wall,
        inside_walls=inside_walls,
        pcr=_r(u.pcr()),
        vix_jump_pct=_r(vix_jump, 2),
        atm_spread_pct=_r(atm_spread, 2),
    )

    if vix_jump >= cfg.chaos_vix_jump_pct or (atm_spread or 0) >= cfg.chaos_spread_pct or stale:
        m["reason"] = "stale feed" if stale else ("vix spike" if vix_jump >= cfg.chaos_vix_jump_pct else "wide spreads")
        return RegimeView(Regime.CHAOS, m)

    lb = cfg.breakout_lookback_min
    if after(now_ts, cfg.breakout_after) and len(bars) >= lb + 3:
        prior, recent = bars[-(lb + 3):-3], bars[-3:]
        hi, lo = max(b.high for b in prior), min(b.low for b in prior)
        prior_rng = sum(b.range for b in prior) / len(prior)
        expansion = (sum(b.range for b in recent) / len(recent)) / prior_rng if prior_rng > 0 else 0.0
        m.update(breakout_hi=hi, breakout_lo=lo, expansion=_r(expansion, 2))
        if spot > hi and (expansion >= cfg.breakout_expansion or (call_wall is None or spot > call_wall)):
            return RegimeView(Regime.BREAKOUT_UP, m)
        if spot < lo and (expansion >= cfg.breakout_expansion or (put_wall is None or spot < put_wall)):
            return RegimeView(Regime.BREAKOUT_DOWN, m)

    decaying = straddle_chg is not None and straddle_chg <= -cfg.straddle_decay_pct
    if len(bars) >= cfg.range_lookback_min and range_frac <= cfg.range_frac and decaying and inside_walls:
        return RegimeView(Regime.RANGE, m)

    sma_bars = bars[-cfg.trend_sma_bars:]
    sma = sum(b.close for b in sma_bars) / len(sma_bars)
    if move_frac >= cfg.trend_frac and spot >= sma:
        return RegimeView(Regime.TREND_UP, m)
    if move_frac <= -cfg.trend_frac and spot <= sma:
        return RegimeView(Regime.TREND_DOWN, m)
    return RegimeView(Regime.NORMAL, m)
