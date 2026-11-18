"""Futures book & tape: who is crossing the spread, how the best queues change and what large resting orders do.

Fed by the full packets of the near-month index future (Dhan: a 5-level book with each trade update, plus a 20-level
book on NFO). Readings are rule-based and short-horizon (seconds to minutes). The book is anonymous, so "a passive
buyer absorbing" means the visible prints fit that pattern, not that anyone was identified. None of this has been
tested yet; the depth recorder keeps the raw packets so it can be.
"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import deque

import numpy as np

from scalper.config import FlowCfg
from scalper.flow import model as M

Event = tuple[str, str, str, float, int]  # key, severity, text, price level, cooldown minutes
STALE20_MS = 15_000


def _levels(xs) -> list[tuple[float, float]]:
    out = []
    for x in xs or []:
        p, q = float(x.get("price") or 0), float(x.get("quantity") or 0)
        if p > 0 and q > 0:
            out.append((p, q))
    return out


def _r(x, n: int = 2):
    return round(float(x), n) if x is not None and math.isfinite(float(x)) else None


class Tape:
    def __init__(self, name: str, symbol: str, exchange: str, lot: int, tick: float, cfg: FlowCfg):
        self.name, self.symbol, self.exchange = name, symbol, exchange
        self.lot, self.tick, self.cfg = max(int(lot or 1), 1), tick or 0.05, cfg
        self.ltp = self.vol = self.oi = 0.0
        self.bid = self.ask = self.bq = self.aq = 0.0
        self.book5: tuple[list, list] = ([], [])
        self.book20: tuple[list, list] = ([], [])
        self.d20_ts = 0
        self.last_side = 0
        self.buy = self.sell = self.ofi = 0.0
        self.prev_best: tuple[float, float, float, float] | None = None
        self.secs: deque = deque(maxlen=1800)  # per second: ts, ltp, cvd, ofi, buy, sell
        self.series: list[list] = []  # every 15 s: ts, ltp, cvd in lots
        self.cols: deque = deque(maxlen=cfg.tape_cols)
        self.col_seq = self.col_ts = 0
        self.cb = self.cs = self.chi = self.clo = 0.0
        self.trades: deque = deque(maxlen=500)  # (ts, price)
        self.walls: dict[tuple[str, float], list] = {}  # (side, price) -> [qty, since, max qty]
        self.fates: deque = deque(maxlen=300)  # (ts, side, price, lots, "pulled" | "filled")
        self.absorb: dict[str, dict | None] = {"bid": None, "offer": None}
        self.sweeps: deque = deque(maxlen=50)  # (ts, side, levels, lots, price)
        self.z: float | None = None

    @property
    def cvd(self) -> float:
        return self.buy - self.sell

    def book(self, ts: int) -> tuple[list, list]:
        return self.book20 if self.d20_ts and ts - self.d20_ts < STALE20_MS else self.book5

    # ---- packets ----------------------------------------------------------------------
    def on_tick(self, ts: int, d: dict) -> list[Event]:
        dep = d.get("depth") or {}
        bids, asks = _levels(dep.get("buy")), _levels(dep.get("sell"))
        deep = d.get("depth_level") == 20 or len(bids) > 5 or len(asks) > 5
        ev: list[Event] = []
        if not deep:
            ltp, vol = float(d.get("ltp") or 0), float(d.get("volume") or 0)
            if vol > self.vol > 0 and ltp > 0:
                ev += self._trade(ts, ltp, vol - self.vol)
            if vol > 0:
                self.vol = vol
            if ltp > 0:
                self.ltp = ltp
                self.chi = max(self.chi, ltp) if self.chi else ltp
                self.clo = min(self.clo, ltp) if self.clo else ltp
            if float(d.get("oi") or 0) > 0:
                self.oi = float(d["oi"])
        if bids and asks:
            self._ofi(bids[0], asks[0])
            (self.bid, self.bq), (self.ask, self.aq) = bids[0], asks[0]
            if deep:
                self.book20, self.d20_ts = (bids, asks), ts
            else:
                self.book5 = (bids, asks)
            if deep or not self.d20_ts or ts - self.d20_ts >= STALE20_MS:
                ev += self._walls(ts, bids, asks)
            ev += self._absorb_book(ts)
        return ev

    def _trade(self, ts: int, ltp: float, dv: float) -> list[Event]:
        bids, asks = self.book(ts)
        side = M.trade_side(ltp, self.bid, self.ask, self.ltp, self.last_side)
        self.last_side = side or self.last_side
        buy = dv if side > 0 else 0.0 if side < 0 else dv / 2
        self.buy += buy
        self.sell += dv - buy
        self.cb += buy
        self.cs += dv - buy
        self.trades.append((ts, ltp))
        for s, hit in (("bid", dv - buy), ("offer", buy)):
            a = self.absorb[s]
            if a and hit > 0 and (ltp >= a["px"] if s == "bid" else ltp <= a["px"]):
                a["hit"] += hit
        ev: list[Event] = []
        if side and dv >= self.cfg.sweep_min_lots * self.lot:
            ladder = asks if side > 0 else bids
            n = sum(1 for p, _ in ladder if (p <= ltp if side > 0 else p >= ltp))
            if n >= self.cfg.sweep_levels:
                lots = dv / self.lot
                self.sweeps.append((ts, side, n, lots, ltp))
                who = "buyer" if side > 0 else "seller"
                ev.append((f"sweep:{self.name}:{side}", "med",
                           f"{self.name} FUT {'BUY' if side > 0 else 'SELL'} sweep: {n} levels {'lifted' if side > 0 else 'hit'} "
                           f"to {ltp:,.1f} in one burst ({lots:,.0f} lots) → an urgent {who}", ltp, 2))
        return ev

    def _ofi(self, b: tuple[float, float], a: tuple[float, float]) -> None:
        """Order-flow imbalance (Cont, Kukanov & Stoikov 2014): net growth of the bid queue minus the offer queue."""
        (bp, bq), (ap, aq) = b, a
        if self.prev_best:
            pbp, pbq, pap, paq = self.prev_best
            self.ofi += ((bq if bp >= pbp else 0.0) - (pbq if bp <= pbp else 0.0)
                         - (aq if ap <= pap else 0.0) + (paq if ap >= pap else 0.0))
        self.prev_best = (bp, bq, ap, aq)

    def _absorb_book(self, ts: int) -> list[Event]:
        """Aggressive volume traded into a best level that keeps holding (refilling) is someone absorbing it."""
        ev: list[Event] = []
        for s, px, q in (("bid", self.bid, self.bq), ("offer", self.ask, self.aq)):
            a = self.absorb[s]
            if a is None or px != a["px"]:
                self.absorb[s] = {"px": px, "since": ts, "hit": 0.0, "shown": q, "n": 1, "done": False}
                continue
            a["shown"] += q
            a["n"] += 1
            avg = a["shown"] / a["n"]
            lots = a["hit"] / self.lot
            if (not a["done"] and a["hit"] >= self.cfg.absorb_mult * max(avg, 1.0) and lots >= self.cfg.absorb_min_lots
                    and ts - a["since"] >= 5_000):
                a["done"] = True
                secs = (ts - a["since"]) / 1000
                if s == "bid":
                    txt = (f"{self.name} FUT: {lots:,.0f} lots sold into the {px:,.1f} bid in {secs:.0f}s and it held "
                           f"(~{avg / self.lot:,.0f} lots showing at a time) → a large passive BUYER absorbing; support while it holds")
                else:
                    txt = (f"{self.name} FUT: {lots:,.0f} lots bought into the {px:,.1f} offer in {secs:.0f}s and it held "
                           f"(~{avg / self.lot:,.0f} lots showing at a time) → a large passive SELLER absorbing; resistance while it holds")
                ev.append((f"absorb:{self.name}:{s}:{px:g}", "hi", txt, px, 15))
        return ev

    def _walls(self, ts: int, bids: list, asks: list) -> list[Event]:
        cfg, ev, now_w = self.cfg, [], {}
        for side, lv in (("bid", bids), ("offer", asks)):
            if len(lv) < 4:
                continue
            med = float(np.median([q for _, q in lv]))
            for p, q in lv:
                if q >= cfg.fut_wall_mult * med and q >= cfg.fut_wall_min_lots * self.lot:
                    now_w[(side, p)] = q
        lo_bid = min(p for p, _ in bids) if bids else 0.0
        hi_ask = max(p for p, _ in asks) if asks else 0.0
        for key in [k for k in self.walls if k not in now_w]:
            side, p = key
            q, since, qmax = self.walls.pop(key)
            traded = any(t >= ts - 5_000 and (px <= p if side == "bid" else px >= p) for t, px in reversed(self.trades))
            visible = p >= lo_bid if side == "bid" else p <= hi_ask
            if not traded and not visible:
                continue
            lots = qmax / self.lot
            fate = "filled" if traded else "pulled"
            self.fates.append((ts, side, p, lots, fate))
            if ts - since < 3_000:
                continue
            dist = abs(p - (self.bid if side == "bid" else self.ask))
            if fate == "filled":
                ev.append((f"wall:{self.name}:{side}:{p:g}", "med",
                           f"{self.name} FUT: {lots:,.0f}-lot {side} wall at {p:,.1f} taken out → "
                           + ("sellers broke through the floor" if side == "bid" else "buyers broke through the ceiling"), p, 5))
            elif dist <= 0.0005 * p:
                ev.append((f"wall:{self.name}:{side}:{p:g}", "med",
                           f"{self.name} FUT: {lots:,.0f}-lot {side} wall at {p:,.1f} PULLED {dist:,.1f} pts from the price "
                           "without trading → likely not real (spoof) or a market maker stepping back", p, 5))
        for key, q in now_w.items():
            w = self.walls.get(key)
            if w is None:
                self.walls[key] = [q, ts, q]
            else:
                w[0], w[2] = q, max(w[2], q)
        return ev

    # ---- once a second ------------------------------------------------------------------
    def sample(self, now: int) -> None:
        if self.ltp <= 0:
            return
        self.secs.append((now, self.ltp, self.cvd, self.ofi, self.buy, self.sell))
        self.z = self._ofi_z()
        if not self.series or now - self.series[-1][0] >= 15_000:
            self.series.append([now, _r(self.ltp), round(self.cvd / self.lot)])
            del self.series[:-2000]
        if now - self.col_ts >= self.cfg.tape_col_ms:
            bids, asks = self.book(now)
            L = self.lot
            self.col_seq += 1
            self.cols.append([self.col_seq, now, _r(self.ltp), _r(self.chi or self.ltp), _r(self.clo or self.ltp),
                              round(self.cb / L, 1), round(self.cs / L, 1), round(self.cvd / L, 1),
                              [_r(p) for p, _ in bids], [round(q / L, 1) for _, q in bids],
                              [_r(p) for p, _ in asks], [round(q / L, 1) for _, q in asks]])
            self.col_ts, self.cb, self.cs, self.chi, self.clo = now, 0.0, 0.0, 0.0, 0.0

    def cols_after(self, seq: int) -> list[list]:
        if not self.cols or self.cols[-1][0] <= seq:
            return []
        return [c for c in self.cols if c[0] > seq]

    def _at(self, now: int, sec: int, partial: bool = True):
        """The per-second sample `sec` seconds ago (the oldest one if there is less history and partial=True)."""
        if not self.secs:
            return None
        i = bisect_right([s[0] for s in self.secs], now - sec * 1000) - 1
        if i < 0:
            return self.secs[0] if partial else None
        return self.secs[i]

    def _ofi_z(self) -> float | None:
        if len(self.secs) < 300:
            return None
        ts = np.array([s[0] for s in self.secs], dtype=float)
        of = np.array([s[3] for s in self.secs], dtype=float)
        j = np.searchsorted(ts, ts - 60_000)
        d = (of - of[j])[ts - ts[0] >= 60_000]
        sd = float(np.std(d)) if len(d) > 30 else 0.0
        return float(d[-1] / sd) if sd > 0 else None

    def minute_events(self, now: int) -> list[Event]:
        """Price and aggressive flow disagreeing over 15 minutes."""
        s0 = self._at(now, 900, partial=False)
        if not s0 or not self.secs or s0[1] <= 0:
            return []
        s1 = self.secs[-1]
        dp = s1[1] / s0[1] - 1
        b, s = s1[4] - s0[4], s1[5] - s0[5]
        if b + s < 200 * self.lot:
            return []
        share = b / (b + s)
        if dp >= 0.001 and share <= 0.45:
            return [(f"div:{self.name}", "med",
                     f"{self.name} FUT up {dp:+.2%} in 15m while aggressive SELLERS did {1 - share:.0%} of the volume → "
                     "the rise rides on passive bids / short covering, not buyers chasing; fragile", s1[1], 15)]
        if dp <= -0.001 and share >= 0.55:
            return [(f"div:{self.name}", "med",
                     f"{self.name} FUT down {dp:+.2%} in 15m while aggressive BUYERS did {share:.0%} of the volume → "
                     "the drop rides on passive offers, buyers are stepping in; fragile", s1[1], 15)]
        return []

    def view(self, now: int, spot: float) -> dict:
        L = self.lot
        last = self.secs[-1] if self.secs else None

        def delta(sec: int, j: int):
            s0 = self._at(now, sec)
            return (last[j] - s0[j]) / L if last and s0 else None

        b1, s1 = delta(60, 4), delta(60, 5)
        bq5, aq5 = sum(q for _, q in self.book5[0]), sum(q for _, q in self.book5[1])
        deep = bool(self.d20_ts) and now - self.d20_ts < STALE20_MS
        imb20 = M.imbalance(sum(q for _, q in self.book20[0]), sum(q for _, q in self.book20[1])) if deep else None
        mid = 0.5 * (self.bid + self.ask)
        micro = ((self.bid * self.aq + self.ask * self.bq) / (self.bq + self.aq) - mid) if self.bq + self.aq > 0 else None
        recent = [f for f in self.fates if now - f[0] <= 900_000]
        absorbing = [[s, _r(a["px"]), round(a["hit"] / L), round(a["shown"] / a["n"] / L), round((now - a["since"]) / 1000)]
                     for s, a in self.absorb.items() if a and a["hit"] >= 0.5 * self.cfg.absorb_min_lots * L]
        return {
            "sym": self.symbol, "ltp": _r(self.ltp), "bid": _r(self.bid), "ask": _r(self.ask), "spread": _r(self.ask - self.bid),
            "basis": _r(self.ltp - spot) if spot > 0 and self.ltp > 0 else None, "lot": L,
            "cvd": round(self.cvd / L), "cvd15": _r(delta(900, 2), 0), "buy1": _r(b1, 0), "sell1": _r(s1, 0),
            "ofi1": _r(delta(60, 3), 0), "ofi5": _r(delta(300, 3), 0), "ofi_z": _r(self.z, 1),
            "micro": _r(micro), "imb5": _r(M.imbalance(bq5, aq5)), "imb20": _r(imb20), "levels": 20 if deep else 5,
            "walls": sorted(([s, _r(p), round(w[0] / L), round((now - w[1]) / 1000)] for (s, p), w in self.walls.items()),
                            key=lambda x: -x[2])[:6],
            "pulled": {s: sum(1 for f in recent if f[1] == s and f[4] == "pulled") for s in ("bid", "offer")},
            "filled": {s: sum(1 for f in recent if f[1] == s and f[4] == "filled") for s in ("bid", "offer")},
            "absorb": absorbing,
            "sweeps": [[t, sd, n, round(lots), _r(px)] for t, sd, n, lots, px in self.sweeps if now - t <= 900_000][-5:],
            "col": self.col_seq,
        }
