"""Realistic execution of backtest alerts: latency, bid/ask, slippage, plan exits and F&O charges.

The engine's paper grading fills at the alert price. Here each plan is replayed against the
session's ticks the way real orders would fill:
  * the order goes in `latency` after the alert: BUY at the ask, SELL at the bid, plus `slip` ticks
  * limit targets fill only once the bid (ask for a short) reaches them
  * stops fill at the stop less half-spread and slippage, or at the market if price gapped through it
  * anything still open at 15:20 is closed at the bid/ask
  * charges: Dhan Rs 20 per order, STT, exchange, SEBI, stamp duty and GST (approximate rates)
Positions are sized so the planned risk is R rupees (fractional lots), so results compare in R:
  S1  R = entry - stop          S2  R = premium paid          S3  R = wing width - credit
"""

from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date

from scalper.clock import MINUTE_MS, at

TICK = 0.05
_GAP_MS = 70_000


@dataclass(frozen=True)
class Market:
    half_spread_pct: dict[str, float]
    latency_ms: int = 15_000
    slip_ticks: int = 1
    R: float = 1500.0

    def hs(self, underlying: str, price: float) -> float:
        return max(TICK, price * self.half_spread_pct.get(underlying, 0.002))

    @property
    def slip(self) -> float:
        return self.slip_ticks * TICK


def charges(exchange: str, day: date, buy_value: float, sell_value: float, orders: int) -> float:
    """Rupees for one round trip in index options (Oct-2024 STT/exchange revision applied by date)."""
    new = day >= date(2024, 10, 1)
    turnover = buy_value + sell_value
    stt = (0.001 if new else 0.000625) * sell_value
    txn = (0.000325 if exchange == "BFO" else (0.0003503 if new else 0.000495)) * turnover
    sebi = 1e-6 * turnover
    stamp = 0.00003 * buy_value
    brokerage = 20.0 * orders
    return stt + txn + sebi + stamp + brokerage + 0.18 * (brokerage + txn + sebi)


class Paths:
    """Per-symbol tick prices of one replayed session."""

    def __init__(self, events: list[tuple]):
        self.ts: dict[str, list[int]] = {}
        self.px: dict[str, list[float]] = {}
        for ts, sym, _, d in events:
            self.ts.setdefault(sym, []).append(ts)
            self.px.setdefault(sym, []).append(float(d["ltp"]))

    def between(self, sym: str, start: int, end: int) -> list[tuple[int, float]]:
        ts = self.ts.get(sym, [])
        i, j = bisect_left(ts, start), bisect_left(ts, end)
        return list(zip(ts[i:j], self.px[sym][i:j]))

    def last(self, sym: str, t: int) -> tuple[int, float] | None:
        ts = self.ts.get(sym, [])
        i = bisect_left(ts, t + 1) - 1
        return (ts[i], self.px[sym][i]) if i >= 0 else None

    def first(self, sym: str) -> float | None:
        px = self.px.get(sym)
        return px[0] if px else None

    def extremes(self, sym: str, t: int) -> tuple[float, float] | None:
        ts = self.ts.get(sym, [])
        j = bisect_left(ts, t + 1)
        return (max(self.px[sym][:j]), min(self.px[sym][:j])) if j else None


def _result(policy: str, reason: str, **kw) -> dict:
    return {"policy": policy, "exit_reason": reason, **kw}


def _finish(policy, exch, day, mkt, entry, qty, risk_rs, sold, t0, mfe, mae, gap) -> dict:
    exit_value = sum(frac * px for frac, px, _, _ in sold)
    gross = qty * (exit_value - entry)
    orders = 1 + len(sold)
    cost = charges(exch, day, qty * entry, qty * exit_value, orders)
    last_ts = max(ts for _, _, ts, _ in sold)
    reasons = "+".join(dict.fromkeys(r for _, _, _, r in sold))
    return _result(policy, reasons, entry_fill=round(entry, 2), exit_avg=round(exit_value, 2), qty=round(qty, 2),
                   entry_ts=t0, exit_ts=last_ts, held_min=round((last_ts - t0) / MINUTE_MS, 1),
                   gross_r=gross / risk_rs, cost_r=cost / risk_rs, net_r=(gross - cost) / risk_rs,
                   mfe_r=mfe, mae_r=mae, max_gap_min=round(gap / MINUTE_MS, 1), orders=orders)


def buy_option(sig, paths: Paths, mkt: Market, day: date, end_ms: int, policy: str) -> dict:
    """S1 (policies t1 / plan) and S2 (policies plan / stop50) long single-option trades."""
    leg = sig.legs[0]
    sym, exch, und, p = leg["symbol"], leg["exchange"], sig.underlying, sig.plan
    ticks = paths.between(sym, sig.ts + mkt.latency_ms, end_ms)
    if not ticks:
        return _result(policy, "no fill (no trades after alert)")
    t0, px0 = ticks[0]
    entry = px0 + mkt.hs(und, px0) + mkt.slip
    if sig.strategy == "S1":
        stop = float(p["stop"])
        if px0 <= stop:
            return _result(policy, "no fill (stop already hit)")
        risk = entry - stop
        steps = [(float(p["t1"]), 1.0)] if policy == "t1" else [(float(p[k]), 1 / 3) for k in ("t1", "t2", "t3")]
        # time stop: not +0.5R after two closed 5m candles
        deadline = (t0 // (5 * MINUTE_MS) + 2) * 5 * MINUTE_MS if policy == "plan" else None
        trail = None
    else:
        stop = entry * 0.5 if policy == "stop50" else 0.0
        risk = entry
        steps = [(float(p["sell_50pct_at"]), 0.5), (float(p["sell_25pct_at"]), 0.25)]
        deadline, trail = None, float(p.get("runner_trail_pct", 30)) / 100
    qty = mkt.R / risk
    remaining, sold = 1.0, []
    peak = trough = px0 - mkt.hs(und, px0)
    prev_ts, prev_px, gap, hit_first = t0, px0, 0, False
    for ts, px in ticks[1:]:
        gap = max(gap, ts - prev_ts)
        bid = px - mkt.hs(und, px)
        peak, trough = max(peak, bid), min(trough, bid)
        if stop > 0 and px <= stop:
            continuous = prev_px > stop and ts - prev_ts <= _GAP_MS
            fill = (stop - mkt.hs(und, stop) if continuous else bid) - mkt.slip
            sold.append((remaining, max(fill, TICK), ts, "breakeven" if hit_first and sig.strategy == "S1" else "stop"))
            remaining = 0.0
            break
        while steps and bid >= steps[0][0]:
            price, frac = steps.pop(0)
            sold.append((frac, price, ts, "target"))
            remaining -= frac
            if not hit_first:
                hit_first = True
                if sig.strategy == "S1" and policy == "plan":
                    stop = max(stop, entry)
                if sig.strategy == "S2":
                    stop = 0.0
        if remaining <= 1e-9:
            break
        if trail and hit_first and bid <= peak * (1 - trail):
            sold.append((remaining, max(bid - mkt.slip, TICK), ts, "trail"))
            remaining = 0.0
            break
        if deadline and ts >= deadline:
            if peak < entry + 0.5 * risk:
                sold.append((remaining, max(bid - mkt.slip, TICK), ts, "time stop"))
                remaining = 0.0
                break
            deadline = None
        prev_ts, prev_px = ts, px
    if remaining > 1e-9:
        ts, px = ticks[-1]
        sold.append((remaining, max(px - mkt.hs(und, px), TICK), ts, "15:20"))
    return _finish(policy, exch, day, mkt, entry, qty, qty * risk, sold, t0,
                   (peak - entry) / risk, (trough - entry) / risk, gap)


def iron_fly(sig, paths: Paths, mkt: Market, day: date, end_ms: int) -> dict:
    """S3: per-leg stops at +leg_stop_pct (buy back with its wing), other leg's stop to cost, exit at plan time."""
    und, p = sig.underlying, sig.plan
    exch = sig.legs[0]["exchange"]
    legs = [(leg["symbol"], -1 if leg["action"] == "SELL" else 1) for leg in sig.legs]
    (wc, _), (wp, _), (sc, _), (sp, _) = legs
    t0 = sig.ts + mkt.latency_ms
    last = {}
    for sym, _ in legs:
        hit = paths.last(sym, t0)
        if hit is None or t0 - hit[0] > 2 * MINUTE_MS:
            return _result("plan", "no fill (stale leg)")
        last[sym] = hit[1]
    hs = lambda x: mkt.hs(und, x)  # noqa: E731
    fill = {s: last[s] - hs(last[s]) - mkt.slip if side < 0 else last[s] + hs(last[s]) + mkt.slip for s, side in legs}
    credit = fill[sc] + fill[sp] - fill[wc] - fill[wp]
    width = float(p["width"])
    if credit <= 0 or credit >= width:
        return _result("plan", "no fill (no credit)")
    risk = width - credit
    qty = float(p.get("risk_budget_rs", mkt.R)) / risk
    exit_ms = min(end_ms, at(day, p.get("exit", "15:15")))
    stop = {s: fill[s] * (1 + float(p.get("leg_stop_pct", 50)) / 100) for s in (sc, sp)}
    pair = {sc: (wc, sp), sp: (wp, sc)}
    out: dict[str, float] = {}
    stopped: list[str] = []
    prev = {s: (t0, last[s]) for s, _ in legs}
    best = worst = 0.0
    stream = sorted((ts, sym, px) for sym, _ in legs for ts, px in paths.between(sym, t0 + 1, exit_ms))
    gap = 0
    for ts, sym, px in stream:
        gap = max(gap, ts - prev[sym][0])
        if sym in stop and sym not in out and px >= stop[sym]:
            continuous = prev[sym][1] < stop[sym] and ts - prev[sym][0] <= _GAP_MS
            trig = stop[sym] if continuous else px
            out[sym] = trig + hs(trig) + mkt.slip
            wing, other = pair[sym]
            out[wing] = max(last[wing] - hs(last[wing]) - mkt.slip, 0.0)
            stopped.append("CE" if sym == sc else "PE")
            if other not in out:
                stop[other] = min(stop[other], fill[other])
        last[sym] = px
        prev[sym] = (ts, px)
        mark = sum((fill[s] - out.get(s, last[s] + hs(last[s]))) if side < 0
                   else (out.get(s, last[s] - hs(last[s])) - fill[s]) for s, side in legs)
        best, worst = max(best, mark), min(worst, mark)
    for s, side in legs:
        if s not in out:
            out[s] = last[s] + hs(last[s]) + mkt.slip if side < 0 else max(last[s] - hs(last[s]) - mkt.slip, 0.0)
    pnl = fill[sc] - out[sc] + fill[sp] - out[sp] + out[wc] - fill[wc] + out[wp] - fill[wp]
    gross = qty * pnl
    cost_rs = charges(exch, day, qty * (fill[wc] + fill[wp] + out[sc] + out[sp]),
                      qty * (fill[sc] + fill[sp] + out[wc] + out[wp]), 8)
    risk_rs = qty * risk
    exit_ts = stream[-1][0] if stream else t0
    return _result("plan", " then ".join(stopped) + " stop" if stopped else "no stop", entry_fill=round(credit, 2),
                   exit_avg=round(credit - pnl, 2), qty=round(qty, 2), entry_ts=t0, exit_ts=exit_ts,
                   held_min=round((exit_ts - t0) / MINUTE_MS, 1), gross_r=gross / risk_rs, cost_r=cost_rs / risk_rs,
                   net_r=(gross - cost_rs) / risk_rs, mfe_r=best / risk, mae_r=worst / risk,
                   max_gap_min=round(gap / MINUTE_MS, 1), orders=8)


POLICIES = {"S1": ("t1", "plan"), "S2": ("plan", "stop50"), "S3": ("plan",)}


def execute(sig, paths: Paths, mkt: Market, day: date, end_ms: int) -> list[dict]:
    if sig.strategy == "S3":
        return [iron_fly(sig, paths, mkt, day, end_ms)]
    return [buy_option(sig, paths, mkt, day, end_ms, pol) for pol in POLICIES.get(sig.strategy, ())]
