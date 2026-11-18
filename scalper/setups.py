from __future__ import annotations

import logging
import math
from dataclasses import dataclass

from scalper.clock import MINUTE_MS, hhmm, in_window
from scalper.config import RiskCfg, S1Cfg, S2Cfg, S3Cfg
from scalper.models import Signal
from scalper.regime import Regime, RegimeView
from scalper.state import InstrumentState, UnderlyingState

log = logging.getLogger("scalper.setups")


@dataclass
class Ctx:
    now_ts: int
    u: UnderlyingState
    regime: RegimeView
    risk: RiskCfg
    any_day: bool = False

    @property
    def expiry_ok(self) -> bool:
        return self.u.is_expiry_day or self.any_day


def _round_tick(price: float, tick: float) -> float:
    return round(round(price / tick) * tick, 2) if tick > 0 else round(price, 2)


def _itm_steps(label: str) -> int:
    return int(label[3:]) if label.startswith("ITM") else 0


@dataclass
class _Arm:
    armed_ts: int = 0
    low: float = 0.0


class FallBounce:
    """S1: an option that was in the money at the open has collapsed; buy the first confirmed bounce."""

    name = "S1"

    def __init__(self, cfg: S1Cfg):
        self.cfg = cfg
        self.arms: dict[str, _Arm] = {}
        self.alerts = 0
        self.last_alert_ts = 0

    def evaluate(self, ctx: Ctx) -> list[Signal]:
        c, u = self.cfg, ctx.u
        if not c.enabled or not in_window(ctx.now_ts, c.window) or ctx.regime.regime in (Regime.CHAOS, Regime.WARMUP):
            return []
        cooling = ctx.now_ts - self.last_alert_ts < c.cooldown_min * MINUTE_MS
        best: Signal | None = None
        for st in u.options.values():
            if not 1 <= _itm_steps(st.open_label) <= c.max_itm_at_open:
                continue
            arm = self._update_arm(st, ctx.now_ts)
            if arm is None or cooling or self.alerts >= c.max_alerts_per_day:
                continue
            sig = self._trigger(st, arm, ctx)
            if sig and (best is None or sig.score > best.score):
                best = sig
        if best:
            self.alerts += 1
            self.last_alert_ts = ctx.now_ts
            self.arms.pop(best.legs[0]["symbol"], None)
            return [best]
        return []

    def _update_arm(self, st: InstrumentState, now_ts: int) -> _Arm | None:
        c = self.cfg
        arm = self.arms.get(st.symbol)
        fallen = st.drop_from_open_pct() <= -c.min_drop_pct
        fresh_low = now_ts - st.low_ts <= c.arm_valid_min * MINUTE_MS
        if fallen and fresh_low and st.red_streak_5m() >= c.min_red_5m:
            if arm is None:
                arm = self.arms[st.symbol] = _Arm(now_ts, st.day_low)
                log.info(
                    "%s S1 watching %s (%s at open): %g -> %g (%.0f%%), %d red 5m, waiting for bounce trigger",
                    hhmm(now_ts), st.symbol, st.open_label, st.day_open, st.ltp,
                    st.drop_from_open_pct(), st.red_streak_5m(),
                )
        if arm is None:
            return None
        if st.day_low < arm.low:
            arm.low = st.day_low
            arm.armed_ts = now_ts
        if now_ts - max(arm.armed_ts, st.low_ts) > c.arm_valid_min * MINUTE_MS:
            del self.arms[st.symbol]
            return None
        return arm

    def _trigger(self, st: InstrumentState, arm: _Arm, ctx: Ctx) -> Signal | None:
        c, u = self.cfg, ctx.u
        if st.ltp < c.min_premium:
            return None
        bounce = (st.ltp / arm.low - 1) * 100 if arm.low > 0 else 0.0
        if bounce < c.min_bounce_pct:
            return None
        sp = st.spread_pct()
        if sp is None or sp > c.max_spread_pct:
            return None

        trig_5m = False
        b5 = st.m5.last(2)
        if len(b5) == 2:
            last5, prev5 = b5
            trig_5m = last5.green and last5.close > prev5.mid and last5.start + st.m5.period > arm.armed_ts
        trig_1m = False
        b1 = st.m1.last(2)
        if len(b1) == 2:
            prev1, last1 = b1
            trig_1m = prev1.low <= arm.low * 1.005 and last1.low > prev1.low and st.ltp > last1.high
        if not (trig_5m or trig_1m):
            return None

        if c.require_index_turn:
            sign = 1 if st.inst.opt == "CE" else -1
            ib = u.index.m1.last(1)
            if not ib:
                return None
            turning = (u.index.ltp - ib[-1].close) * sign > 0 or (ib[-1].close - ib[-1].open) * sign > 0
            if not turning:
                return None

        entry = st.ask or st.ltp
        recent = st.m1.last(10)
        atr = sum(b.range for b in recent) / len(recent) if recent else 0.0
        buffer = max(arm.low * c.stop_buffer_pct / 100, atr * c.stop_atr_mult)
        stop = _round_tick(arm.low - buffer, st.inst.tick_size)
        risk_unit = entry - stop
        if risk_unit <= 0:
            return None
        lot = st.inst.lotsize
        lots = int(ctx.risk.R // (risk_unit * lot))
        initial = math.ceil(lots * c.initial_pct / 100) if lots else 0
        fall = st.day_open - arm.low
        t1 = entry + risk_unit
        t2 = max(arm.low + 0.236 * fall, entry + 2 * risk_unit)
        t3 = max(arm.low + 0.382 * fall, t2 + risk_unit)
        tick = st.inst.tick_size
        plan = {
            "entry": entry,
            "stop": stop,
            "risk_per_unit": round(risk_unit, 2),
            "lot": lot,
            "lots": lots,
            "initial_lots": initial,
            "add_lots": lots - initial,
            "risk_rs": round(lots * lot * risk_unit, 0),
            "risk_per_lot": round(lot * risk_unit, 0),
            "t1": _round_tick(t1, tick),
            "t2": _round_tick(t2, tick),
            "t3": _round_tick(t3, tick),
            "time_stop": "exit if not +0.5R after 2 closed 5m candles",
            "trigger": "+".join(n for n, ok in (("5m-green", trig_5m), ("1m-higher-low", trig_1m)) if ok),
        }
        snap = st.snapshot(ctx.now_ts)
        score = (
            min(abs(snap["drop_from_open_pct"]) / 100, 1.0)
            + 0.1 * min(snap["red_5m_streak"], 6)
            + 0.2 * min(snap["vol_ratio"], 5) / 5
            + (0.2 if trig_5m and trig_1m else 0.0)
        )
        title = (
            f"[S1 FALL-BOUNCE] {u.name} {st.inst.strike:g} {st.inst.opt} ({st.open_label} at open) {hhmm(ctx.now_ts)}\n"
            f"regime {ctx.regime.regime} | open {st.day_open:g} -> low {arm.low:g} ({snap['drop_from_open_pct']}%) "
            f"| now {st.ltp:g} (+{bounce:.1f}% off low) | trigger {plan['trigger']}\n"
            f"plan: BUY {initial} lots now (+{lots - initial} add) x{lot} @ {entry:g} | stop {stop:g} "
            f"| T1 {plan['t1']:g} T2 {plan['t2']:g} T3 {plan['t3']:g} | risk Rs {plan['risk_rs']:g} (R {ctx.risk.R:g})"
        )
        if lots == 0:
            title += f"\nNOTE: 1 lot risks Rs {plan['risk_per_lot']:g} > R, would skip"
        return Signal(
            ts=ctx.now_ts,
            underlying=u.name,
            strategy=self.name,
            title=title,
            side="BUY",
            legs=[{"symbol": st.symbol, "exchange": st.inst.exchange, "action": "BUY", "price": entry}],
            score=round(score, 3),
            regime=str(ctx.regime.regime),
            plan=plan,
            features=snap,
        )


class ExpiryBurst:
    """S2: late on expiry, a cheap out-of-the-money option explodes as the index breaks out."""

    name = "S2"

    def __init__(self, cfg: S2Cfg):
        self.cfg = cfg
        self.tries = 0
        self.last_alert_ts = 0

    def evaluate(self, ctx: Ctx) -> list[Signal]:
        c, u = self.cfg, ctx.u
        if not c.enabled or not ctx.expiry_ok or not in_window(ctx.now_ts, c.window):
            return []
        if ctx.regime.regime not in (Regime.BREAKOUT_UP, Regime.BREAKOUT_DOWN):
            return []
        if self.tries >= c.max_tries or ctx.now_ts - self.last_alert_ts < c.cooldown_min * MINUTE_MS:
            return []
        opt = "CE" if ctx.regime.direction > 0 else "PE"
        spot = u.index.ltp
        best: tuple[float, InstrumentState, float] | None = None
        for st in u.options.values():
            if st.inst.opt != opt or st.ltp <= 0:
                continue
            m = u.moneyness(st.inst.strike, opt, spot)
            if not 1 <= m <= c.max_otm:
                continue
            base = st.min_low(c.rise_lookback_min)
            if not c.premium_min <= base <= c.premium_max:
                continue
            rise = (st.ltp / base - 1) * 100
            sp = st.spread_pct()
            if rise < c.min_rise_pct or st.vol_ratio() < c.min_vol_ratio or sp is None or sp > c.max_spread_pct:
                continue
            if best is None or rise > best[0]:
                best = (rise, st, base)
        if best is None:
            return []
        rise, st, base = best
        self.tries += 1
        self.last_alert_ts = ctx.now_ts
        entry = st.ask or st.ltp
        lot = st.inst.lotsize
        lots = int(c.budget_per_try // (entry * lot))
        tick = st.inst.tick_size
        plan = {
            "entry": entry,
            "lot": lot,
            "lots": lots,
            "premium_at_risk": round(lots * lot * entry, 0),
            "sell_50pct_at": _round_tick(entry * 2, tick),
            "sell_25pct_at": _round_tick(entry * 4, tick),
            "runner_trail_pct": 30,
            "hard_exit": "15:20",
        }
        label = u.label(st.inst.strike, opt, spot)
        title = (
            f"[S2 EXPIRY-BURST] {u.name} {st.inst.strike:g} {opt} ({label}) {hhmm(ctx.now_ts)}\n"
            f"regime {ctx.regime.regime} | {base:g} -> {st.ltp:g} (+{rise:.0f}% in {c.rise_lookback_min}m) "
            f"| vol x{st.vol_ratio():.1f} | spot {spot:g}\n"
            f"plan: BUY {lots} lots x{lot} @ {entry:g} (Rs {plan['premium_at_risk']:g} at risk) "
            f"| sell 50% @ {plan['sell_50pct_at']:g}, 25% @ {plan['sell_25pct_at']:g}, trail rest 30% | out by 15:20"
        )
        return [
            Signal(
                ts=ctx.now_ts,
                underlying=u.name,
                strategy=self.name,
                title=title,
                side="BUY",
                legs=[{"symbol": st.symbol, "exchange": st.inst.exchange, "action": "BUY", "price": entry}],
                score=round(rise / 100, 3),
                regime=str(ctx.regime.regime),
                plan=plan,
                features=st.snapshot(ctx.now_ts),
            )
        ]


class ThetaSell:
    """S3 quiet-decay iron fly (scalper/research.py, 2021-2026 expiry days, out-of-sample positive).

    On expiry, once the index has been quiet for an hour (range <= range_mult x ATM straddle) and the
    straddle is visibly decaying, sell the ATM straddle with wings. Each short leg has its own stop at
    +leg_stop_pct of its premium (buy it back and sell its wing); then the other leg's stop moves to its
    sell price. Everything is closed at `exit`.
    """

    name = "S3"

    def __init__(self, cfg: S3Cfg):
        self.cfg = cfg
        self.alerts = 0

    def evaluate(self, ctx: Ctx) -> list[Signal]:
        c, u = self.cfg, ctx.u
        # decide on completed 1-minute bars only, as in the research backtest
        if ctx.now_ts % MINUTE_MS or not c.enabled or not ctx.expiry_ok or not in_window(ctx.now_ts, c.window):
            return []
        if self.alerts >= c.max_alerts_per_day or ctx.regime.regime == Regime.CHAOS:
            return []
        spot = u.index.ltp
        atm = u.atm(spot)
        if atm is None or u.step <= 0:
            return []
        wing = c.wing_strikes * u.step
        sc, sp_ = u.leg(atm, "CE"), u.leg(atm, "PE")
        wc, wp = u.leg(atm + wing, "CE"), u.leg(atm - wing, "PE")
        legs = [sc, sp_, wc, wp]
        if any(leg is None or leg.bid <= 0 or leg.ask <= 0 for leg in legs):
            return []
        idx_bars = u.index.m1.last(c.range_lookback_min)
        past = [st.m1.last(c.decay_lookback_min + 1) for st in (sc, sp_)]
        if len(idx_bars) < c.range_lookback_min or any(len(b) <= c.decay_lookback_min for b in past):
            return []
        straddle, before = past[0][-1].close + past[1][-1].close, past[0][0].close + past[1][0].close
        if straddle <= 0 or before <= 0:
            return []
        decay = (straddle / before - 1) * 100
        rng = max(b.high for b in idx_bars) - min(b.low for b in idx_bars)
        if rng > c.range_mult * straddle or decay > -c.min_decay_pct:
            return []
        if any((leg.spread_pct() or 99) > c.max_spread_pct for leg in (sc, sp_)):
            return []
        credit = sc.bid + sp_.bid - wc.ask - wp.ask
        if credit <= 0 or credit >= wing:
            return []
        max_loss_unit = wing - credit
        lot = sc.inst.lotsize
        lots = int(c.max_loss_rs // (max_loss_unit * lot))
        self.alerts += 1
        tick = sc.inst.tick_size
        stop_ce = _round_tick(sc.bid * (1 + c.leg_stop_pct / 100), tick)
        stop_pe = _round_tick(sp_.bid * (1 + c.leg_stop_pct / 100), tick)
        plan = {
            "credit": round(credit, 2),
            "width": wing,
            "max_loss_per_unit": round(max_loss_unit, 2),
            "lot": lot,
            "lots": lots,
            "max_loss_rs": round(lots * lot * max_loss_unit, 0),
            "max_loss_per_lot": round(lot * max_loss_unit, 0),
            "risk_budget_rs": c.max_loss_rs,
            "leg_stop_pct": c.leg_stop_pct,
            "stop_ce": stop_ce,
            "stop_pe": stop_pe,
            "exit": c.exit,
            "range_60m": round(rng, 2),
            "straddle": round(straddle, 2),
            "straddle_chg_pct": round(decay, 2),
            "order": "buy both wings first, then sell the ATM call and put",
            "manage": "short leg at/above its stop: buy it back and sell its wing; other short's stop -> its sell price",
        }
        vix = f" | VIX chg {ctx.regime.metrics.get('vix_jump_pct')}%" if ctx.regime.metrics.get("vix_jump_pct") is not None else ""
        title = (
            f"[S3 {'PAPER ' if c.paper else ''}QUIET-DECAY FLY] {u.name} {atm:g} +/-{wing:g} {hhmm(ctx.now_ts)}\n"
            f"60m range {rng:.1f} = {rng / straddle:.2f}x straddle {straddle:.1f} | straddle {decay:+.1f}% /"
            f"{c.decay_lookback_min}m | spot {spot:g}{vix}\n"
            f"plan: BUY {atm + wing:g}CE @ {wc.ask:g} + {atm - wing:g}PE @ {wp.ask:g}, then SELL {atm:g}CE @ {sc.bid:g} "
            f"+ {atm:g}PE @ {sp_.bid:g} | credit {credit:.2f} | max loss/lot Rs {plan['max_loss_per_lot']:g} | lots {lots}\n"
            f"stops: CE {stop_ce:g}, PE {stop_pe:g} (+{c.leg_stop_pct:g}%): buy that leg back + sell its wing, "
            f"then move the other leg's stop to its sell price | exit everything {c.exit}"
        )
        if lots == 0:
            title += f"\nNOTE: 1 lot max loss Rs {plan['max_loss_per_lot']:g} > s3.max_loss_rs {c.max_loss_rs:g}, would skip"
        return [
            Signal(
                ts=ctx.now_ts,
                underlying=u.name,
                strategy=self.name,
                title=title,
                side="SELL",
                legs=[
                    {"symbol": wc.symbol, "exchange": wc.inst.exchange, "action": "BUY", "price": wc.ask},
                    {"symbol": wp.symbol, "exchange": wp.inst.exchange, "action": "BUY", "price": wp.ask},
                    {"symbol": sc.symbol, "exchange": sc.inst.exchange, "action": "SELL", "price": sc.bid},
                    {"symbol": sp_.symbol, "exchange": sp_.inst.exchange, "action": "SELL", "price": sp_.bid},
                ],
                score=round(-decay / 10 + (1 - rng / straddle), 3),
                regime=str(ctx.regime.regime),
                plan=plan,
                features=dict(ctx.regime.metrics),
            )
        ]


def build_detectors(s1: S1Cfg, s2: S2Cfg, s3: S3Cfg) -> list:
    return [FallBounce(s1), ExpiryBurst(s2), ThetaSell(s3)]
