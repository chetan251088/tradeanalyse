"""Live state of the NIFTY / SENSEX option chains and everything derived from it, for the flow dashboard.

The engine is single-threaded: sources push ticks into it, the server calls ``analyze`` once a second and
``frame`` ten times a second. Interpretations (the JARVIS feed) are rule-based and explained in their text.
"""

from __future__ import annotations

import math
from collections import deque
from datetime import date, datetime, time

import numpy as np

from scalper.clock import IST, to_dt
from scalper.config import FlowCfg
from scalper.flow import model as M
from scalper.flow.bounce import BounceWatch
from scalper.flow.tape import Tape

MIN_MS = 60_000
TAGS = {"": 0, M.LONG: 1, M.WRITE: 2, M.COVER: 3, M.UNWIND: 4}
OPT_FIELDS = ["ltp", "chg", "oi", "doi", "vol", "act", "iv", "delta", "gamma", "theta", "vega", "imb", "tag",
              "tag_doi", "fbuy", "fsell", "bshare", "gex", "bid", "ask", "b1", "s1"]
ROW_FIELDS = ["strike"] + [f"ce_{f}" for f in OPT_FIELDS] + [f"pe_{f}" for f in OPT_FIELDS]


def _r(x, n: int = 2):
    try:
        x = float(x)
    except (TypeError, ValueError):
        return None
    return round(x, n) if math.isfinite(x) else None


def fmt_qty(q: float) -> str:
    a = abs(q)
    for div, suf in ((1e7, "Cr"), (1e5, "L"), (1e3, "K")):
        if a >= div:
            return f"{q / div:.1f}{suf}"
    return f"{q:.0f}"


def sq(q: float) -> str:
    return ("+" if q > 0 else "") + fmt_qty(q)


class Opt:
    __slots__ = ("symbol", "exchange", "strike", "is_call", "lot", "ltp", "bid", "ask", "bq", "aq", "vol", "oi", "oi0",
                 "prev_close", "buy", "sell", "fbuy", "fsell", "last_side", "fast", "slow", "br", "sr", "rate_ts",
                 "depth", "depth20", "d20_ts", "mins", "iv", "delta", "gamma", "theta", "vega", "gex", "tag", "tag_doi",
                 "tag_dp", "wall", "bo", "bh", "bl")

    def __init__(self, symbol: str, exchange: str, strike: float, is_call: bool, lot: int):
        self.symbol, self.exchange, self.strike, self.is_call, self.lot = symbol, exchange, strike, is_call, lot
        self.ltp = self.bid = self.ask = self.bq = self.aq = self.vol = self.oi = self.prev_close = 0.0
        self.oi0 = None
        self.buy = self.sell = self.fbuy = self.fsell = 0.0
        self.last_side = 0
        self.fast = self.slow = self.br = self.sr = 0.0
        self.rate_ts = 0
        self.depth = self.depth20 = None
        self.d20_ts = 0
        self.mins: deque = deque(maxlen=420)  # (minute_ms, ltp, oi, buy, sell, vol, open, high, low)
        self.bo = self.bh = self.bl = 0.0
        self.iv = self.delta = self.gamma = self.theta = self.vega = self.gex = math.nan
        self.tag, self.tag_doi, self.tag_dp = "", 0.0, 0.0
        self.wall = None  # (side, price, qty)

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask >= self.bid and self.ask - self.bid <= max(0.3 * self.ask, 1.0):
            return 0.5 * (self.bid + self.ask)
        return self.ltp

    def rates(self, now: int) -> tuple[float, float]:
        dt = max(now - self.rate_ts, 0) / 1000
        return self.fast * math.exp(-dt / 20), self.slow * math.exp(-dt / 900)

    def flow1(self, now: int) -> tuple[float, float]:
        """Bought / sold quantity per minute, decaying with a one-minute time constant."""
        k = math.exp(-max(now - self.rate_ts, 0) / 1000 / 60) * 60
        return self.br * k, self.sr * k

    def trade(self, ts: int, dv: float, buy: float) -> None:
        dt = max(ts - self.rate_ts, 0) / 1000
        k20, k60, k900 = math.exp(-dt / 20), math.exp(-dt / 60), math.exp(-dt / 900)
        self.fast, self.slow = self.fast * k20 + dv / 20, self.slow * k900 + dv / 900
        self.br, self.sr = self.br * k60 + buy / 60, self.sr * k60 + (dv - buy) / 60
        self.rate_ts = ts

    def ago(self, minutes: int):
        return self.mins[-minutes - 1] if len(self.mins) > minutes else (self.mins[0] if self.mins else None)


class Chain:
    def __init__(self, expiry: date):
        self.expiry = expiry
        self.expiry_ms = int(datetime.combine(expiry, time(15, 30), IST).timestamp() * 1000)
        self.opts: dict[tuple[float, bool], Opt] = {}
        self.strikes: list[float] = []
        self.forward = self.atm = self.atm_iv = math.nan
        self.smile: list = []
        self.smile_open: list | None = None

    def add(self, o: Opt) -> None:
        self.opts[(o.strike, o.is_call)] = o
        if o.strike not in self.strikes:
            self.strikes.append(o.strike)
            self.strikes.sort()

    def t_years(self, now: int) -> float:
        return max(self.expiry_ms - now, 30_000) / 1000 / M.YEAR_S

    def window(self, n: int) -> list[float]:
        live = [k for k in self.strikes
                if any((o := self.opts.get((k, c))) is not None and o.ltp > 0 for c in (True, False))]
        ks = live or self.strikes
        if not ks:
            return []
        c = self.atm if math.isfinite(self.atm) else ks[len(ks) // 2]
        near = sorted(ks, key=lambda k: abs(k - c))[: 2 * n + 1]
        return sorted(near)


class Book:
    def __init__(self, name: str, step: float):
        self.name, self.step = name, step
        self.spot = self.open = self.prev_close = 0.0
        self.chains: list[Chain] = []
        self.candles: list[list] = []
        self.events: deque = deque(maxlen=300)
        self.anchor: dict | None = None
        self.decay: dict = {}
        self.decay_series: list = []
        self.iv_series: list = []
        self.heat: deque = deque(maxlen=400)
        self.grid: list = []
        self.curve: list = []
        self.flip = self.max_pain = self.call_wall = self.put_wall = None
        self.net_gex = self.pcr_oi = self.pcr_vol = self.d_ce_oi = self.d_pe_oi = math.nan
        self.rv = self.straddle = self.imb = math.nan
        self.agg = {"cb": 0.0, "cs": 0.0, "pb": 0.0, "ps": 0.0}
        self.agg_ts = 0
        self.flip_side = 0
        self.em_hit = 0
        self.decay_side = 0
        self.cool: dict[str, int] = {}
        self.minute_seq = 0
        self.read: dict | None = None
        self.tape: Tape | None = None
        self.liq: dict = {"n": 0}

    def flow(self, now: int) -> dict[str, float]:
        k = math.exp(-max(now - self.agg_ts, 0) / 1000 / 300)
        return {a: v * k for a, v in self.agg.items()}


class FlowEngine:
    def __init__(self, cfg: FlowCfg, mode: str = "live"):
        self.cfg, self.mode = cfg, mode
        self.books: dict[str, Book] = {}
        self.route: dict[tuple[str, str], tuple] = {}
        self.vix = self.vix_prev = 0.0
        self.now = 0
        self.minute: int | None = None
        self.seq = 0
        self.status = "starting"
        self.bounce = BounceWatch(cfg.bounce_lots)
        self.breadth: dict | None = None

    # ---- setup ----------------------------------------------------------------------
    def add_book(self, name: str, index_symbol: str, index_exchange: str, step: float) -> Book:
        b = self.books.setdefault(name, Book(name, step))
        self.route[(index_symbol, index_exchange)] = ("index", b)
        return b

    def set_vix(self, symbol: str, exchange: str) -> None:
        self.route[(symbol, exchange)] = ("vix", None)

    def add_future(self, name: str, symbol: str, exchange: str, lot: int, tick: float) -> Tape:
        b = self.books[name]
        b.tape = Tape(name, symbol, exchange, lot, tick, self.cfg)
        self.route[(symbol, exchange)] = ("fut", b, b.tape)
        return b.tape

    def add_chain(self, name: str, expiry: date, rows: list[dict]) -> Chain:
        """rows: symbol, exchange, strike, is_call, lot and optionally ltp, bid, ask, oi, volume, prev_close."""
        b = self.books[name]
        ch = next((c for c in b.chains if c.expiry == expiry), None)
        if ch is None:
            ch = Chain(expiry)
            b.chains.append(ch)
            b.chains.sort(key=lambda c: c.expiry)
        for r in rows:
            key = (r["symbol"], r["exchange"])
            if key in self.route:
                continue
            o = Opt(r["symbol"], r["exchange"], float(r["strike"]), bool(r["is_call"]), int(r.get("lot") or 1))
            o.ltp = float(r.get("ltp") or 0)
            o.bid, o.ask = float(r.get("bid") or 0), float(r.get("ask") or 0)
            o.prev_close = float(r.get("prev_close") or 0)
            if r.get("oi"):
                o.oi = float(r["oi"])
                o.oi0 = o.oi
            if r.get("volume"):
                o.vol = float(r["volume"])
            ch.add(o)
            self.route[key] = ("opt", b, ch, o)
        if len(ch.strikes) > 1:
            b.step = min(np.diff(ch.strikes)) if b.step <= 0 else b.step
        return ch

    def instruments(self) -> list[tuple[str, str, str]]:
        return [(s, x, t[0]) for (s, x), t in self.route.items()]

    # ---- ticks ------------------------------------------------------------------------
    def advance(self, ts: int) -> None:
        if ts <= 0:
            return
        self.now = max(self.now, ts)
        m = self.now // MIN_MS * MIN_MS
        if self.minute is None:
            self.minute = m
        elif m > self.minute:
            closed = self.minute
            self.minute = m
            self.analyze()
            self._on_minute(closed)

    def on_tick(self, ts: int, symbol: str, exchange: str, d: dict) -> None:
        t = self.route.get((symbol, exchange))
        if t is None:
            return
        self.advance(ts)
        if t[0] == "opt":
            self._opt_tick(t[1], t[3], ts, d)
        elif t[0] == "index":
            self._index_tick(t[1], ts, d)
        elif t[0] == "fut":
            for key, sev, text, level, cool in t[2].on_tick(ts, d):
                self._event(t[1], key, sev, text, level, cool=cool)
        else:
            ltp = float(d.get("ltp") or 0)
            if ltp > 0:
                self.vix = ltp
            if d.get("close"):
                self.vix_prev = float(d["close"])

    def _index_tick(self, b: Book, ts: int, d: dict) -> None:
        ltp = float(d.get("ltp") or 0)
        if ltp <= 0:
            return
        b.spot = ltp
        if d.get("close") and not b.prev_close:
            b.prev_close = float(d["close"])
        if not b.open:
            b.open = float(d.get("open") or 0) or ltp
        m = ts // MIN_MS * MIN_MS
        if b.candles and b.candles[-1][0] == m:
            c = b.candles[-1]
            c[2], c[3], c[4] = max(c[2], ltp), min(c[3], ltp), ltp
        elif not b.candles or m > b.candles[-1][0]:
            b.candles.append([m, ltp, ltp, ltp, ltp])
            del b.candles[:-420]

    def _opt_tick(self, b: Book, o: Opt, ts: int, d: dict) -> None:
        dep = d.get("depth") or {}
        if d.get("depth_level") == 20 or len(dep.get("buy") or []) > 5:
            o.depth20, o.d20_ts = dep, ts
            self._walls(b, o, dep)
            return
        ltp = float(d.get("ltp") or 0)
        vol = float(d.get("volume") or 0)
        if vol > o.vol > 0 and ltp > 0:
            dv = vol - o.vol
            side = M.trade_side(ltp, o.bid, o.ask, o.ltp, o.last_side)
            o.last_side = side or o.last_side
            buy = dv if side > 0 else 0.0 if side < 0 else dv / 2
            o.buy += buy
            o.sell += dv - buy
            o.fbuy += buy
            o.fsell += dv - buy
            o.trade(ts, dv, buy)
            k = math.exp(-max(ts - b.agg_ts, 0) / 1000 / 300)
            for a in b.agg:
                b.agg[a] *= k
            b.agg_ts = ts
            pre = "c" if o.is_call else "p"
            b.agg[pre + "b"] += buy
            b.agg[pre + "s"] += dv - buy
        if vol > 0:
            o.vol = vol
        if ltp > 0:
            o.ltp = ltp
            if o.bo > 0:
                o.bh, o.bl = max(o.bh, ltp), min(o.bl, ltp)
            else:
                o.bo = o.bh = o.bl = ltp
        if d.get("close") and not o.prev_close:
            o.prev_close = float(d["close"])
        oi = float(d.get("oi") or 0)
        if oi > 0:
            o.oi = oi
            if o.oi0 is None:
                o.oi0 = oi
        bids, asks = dep.get("buy") or [], dep.get("sell") or []
        if bids or asks:
            o.depth = dep
            o.bid = float(bids[0].get("price") or 0) if bids else 0.0
            o.ask = float(asks[0].get("price") or 0) if asks else 0.0
            o.bq = sum(float(x.get("quantity") or 0) for x in bids)
            o.aq = sum(float(x.get("quantity") or 0) for x in asks)
        elif d.get("bid") is not None:
            o.bid, o.ask = float(d.get("bid") or 0), float(d.get("ask") or 0)

    def _walls(self, b: Book, o: Opt, dep: dict) -> None:
        best = None
        for side, levels in (("bid", dep.get("buy") or []), ("offer", dep.get("sell") or [])):
            px, qty, ratio = M.biggest_level(levels)
            if ratio >= self.cfg.wall_mult and qty >= 10 * o.lot and (best is None or qty > best[2]):
                best = (side, px, qty)
        prev = o.wall
        if best and (prev is None or prev[1] != best[1] or prev[0] != best[0]):
            self._event(b, f"wall:{o.symbol}", "low",
                        f"{o.strike:g} {'CE' if o.is_call else 'PE'}: {fmt_qty(best[2] / o.lot)} lots resting on the "
                        f"{best[0]} at {best[1]:.2f} ({self.cfg.wall_mult:g}x+ the other levels)", o.strike, cool=5)
        elif best is None and prev is not None:
            self._event(b, f"pull:{o.symbol}", "med",
                        f"{o.strike:g} {'CE' if o.is_call else 'PE'}: the {prev[0]} wall at {prev[1]:.2f} "
                        f"({fmt_qty(prev[2] / o.lot)} lots) is gone", o.strike, cool=5)
        o.wall = best

    # ---- analytics (once a second) ---------------------------------------------------
    def analyze(self) -> None:
        for b in self.books.values():
            for i, ch in enumerate(b.chains[: self.cfg.expiries]):
                self._chain(b, ch, primary=i == 0)
            self._book(b)
            if b.tape:
                b.tape.sample(self.now)
            self._liquidity(b)

    def _liquidity(self, b: Book) -> None:
        """Median spread and resting depth of the ATM+-3 options against their own 30-minute baseline."""
        ch = b.chains[0] if b.chains else None
        if ch is None or not math.isfinite(ch.atm):
            return
        step, spreads, depth = b.step or 50.0, [], 0.0
        for k in ch.strikes:
            if abs(k - ch.atm) > 3 * step + 1e-9:
                continue
            for is_call in (True, False):
                o = ch.opts.get((k, is_call))
                if o and o.bid > 0 and o.ask > o.bid:
                    spreads.append((o.ask - o.bid) / (0.5 * (o.ask + o.bid)))
                    depth += (o.bq + o.aq) / max(o.lot, 1)
        if len(spreads) < 6 or depth <= 0:
            return
        s, L = float(np.median(spreads)), b.liq
        if L["n"] == 0:
            L.update(s=s, d=depth, sb=s, db=depth)
        else:
            L["s"] += (s - L["s"]) / 15
            L["d"] += (depth - L["d"]) / 15
            L["sb"] += (s - L["sb"]) / 1800
            L["db"] += (depth - L["db"]) / 1800
        L["n"] += 1
        L["sx"], L["dx"] = L["s"] / L["sb"], L["d"] / L["db"]
        if L["n"] >= 600 and L["sx"] >= self.cfg.liq_spread_mult and L["dx"] <= self.cfg.liq_depth_frac:
            self._event(b, "liq", "hi",
                        f"{b.name} options: market makers pulling quotes near the money (spreads {L['sx']:.1f}x, depth "
                        f"{L['dx']:.1f}x the last 30 min) → thin book, fast-move risk", b.spot, cool=10)

    def _chain(self, b: Book, ch: Chain, primary: bool) -> None:
        if not ch.strikes or b.spot <= 0:
            return
        now = self.now
        ks = np.array(ch.strikes)
        ce = [ch.opts.get((k, True)) for k in ch.strikes]
        pe = [ch.opts.get((k, False)) for k in ch.strikes]
        live = [i for i, (c, p) in enumerate(zip(ce, pe)) if (c and c.ltp > 0) or (p and p.ltp > 0)]
        if len(live) < 3:
            return
        ks, ce, pe = ks[live], [ce[i] for i in live], [pe[i] for i in live]
        cm = np.array([o.mid if o else 0.0 for o in ce])
        pm = np.array([o.mid if o else 0.0 for o in pe])
        f = M.synthetic_forward(ks, cm, pm, b.spot)
        t = ch.t_years(now)
        ch.forward = f
        ch.atm = float(ks[np.argmin(np.abs(ks - f))])
        k2 = np.concatenate([ks, ks])
        px = np.concatenate([cm, pm])
        is_call = np.concatenate([np.ones(len(ks), bool), np.zeros(len(ks), bool)])
        iv = M.b76_iv(np.where(px > 0, px, np.nan), f, k2, t, is_call)
        g = M.b76_greeks(f, k2, t, np.where(np.isfinite(iv), iv, 0.2), is_call)
        oi = np.array([o.oi if o else 0.0 for o in ce + pe])
        gx = M.gex(np.where(np.isfinite(iv), g["gamma"], 0.0), oi, b.spot, is_call)
        for j, o in enumerate(ce + pe):
            if o is None:
                continue
            ok = math.isfinite(iv[j])
            o.iv = iv[j] * 100 if ok else math.nan
            o.delta, o.gamma = (g["delta"][j], g["gamma"][j]) if ok else (math.nan, math.nan)
            o.theta, o.vega = (g["theta"][j], g["vega"][j]) if ok else (math.nan, math.nan)
            o.gex = gx[j]
        n = len(ks)
        a = int(np.argmin(np.abs(ks - f)))
        ch.atm_iv = float(np.nanmean([iv[a], iv[n + a]]) * 100) if np.isfinite([iv[a], iv[n + a]]).any() else math.nan
        otm = np.where(ks >= f, iv[:n], iv[n:]) * 100
        ch.smile = [[float(k), _r(v)] for k, v in zip(ks, otm)]
        if ch.smile_open is None and np.isfinite(otm).sum() >= 5 and to_dt(now).strftime("%H:%M") >= "09:20":
            ch.smile_open = ch.smile
        if not primary:
            return
        b.straddle = float(cm[a] + pm[a]) if cm[a] > 0 and pm[a] > 0 else math.nan
        coi, poi = oi[:n], oi[n:]
        b.max_pain = M.max_pain(ks, coi, poi)
        b.call_wall = float(ks[int(np.argmax(gx[:n]))]) if gx[:n].max() > 0 else None
        b.put_wall = float(ks[int(np.argmin(gx[n:]))]) if gx[n:].min() < 0 else None
        b.net_gex = float(gx.sum())
        step = b.step or float(np.min(np.diff(ks))) if n > 1 else 50.0
        grid = np.arange(ks[0], ks[-1] + step / 8, step / 4)
        sig = np.where(np.isfinite(iv), iv, np.nan)
        b.grid, b.curve = grid.tolist(), M.gamma_profile(k2, is_call, sig, oi, t, grid).tolist()
        b.flip = M.flip_level(grid, b.curve, b.spot)
        b.pcr_oi = poi.sum() / coi.sum() if coi.sum() > 0 else math.nan
        cv = sum(o.vol for o in ce if o)
        pv = sum(o.vol for o in pe if o)
        b.pcr_vol = pv / cv if cv > 0 else math.nan
        b.d_ce_oi = sum(o.oi - (o.oi0 or o.oi) for o in ce if o)
        b.d_pe_oi = sum(o.oi - (o.oi0 or o.oi) for o in pe if o)
        bq = sum(o.bq for o in ce + pe if o)
        aq = sum(o.aq for o in ce + pe if o)
        b.imb = M.imbalance(bq, aq)
        self._decay(b, ch, f, t, a, cm, pm, iv, n, [float(k) for k in ks])
        closes = np.array([c[4] for c in b.candles], dtype=float)
        if len(closes) >= 21:
            r = np.diff(np.log(closes))
            b.rv = float(np.std(r) * math.sqrt(252 * 375) * 100)

    def _decay(self, b: Book, ch: Chain, f: float, t: float, a: int, cm, pm, iv, n: int, ks: list[float]) -> None:
        if b.anchor is None:
            if to_dt(self.now).strftime("%H:%M") < "09:20":
                return
            if cm[a] > 0 and pm[a] > 0 and np.isfinite([iv[a], iv[n + a]]).all():
                b.anchor = {"k": ks[a], "f": f, "s": float((iv[a] + iv[n + a]) / 2), "t": t,
                            "p": float(cm[a] + pm[a]), "ts": self.now}
            return
        k = b.anchor["k"]
        try:
            j = ks.index(k)
        except ValueError:
            return
        c, p = ch.opts.get((k, True)), ch.opts.get((k, False))
        if not (c and p and c.mid > 0 and p.mid > 0) or not np.isfinite([iv[j], iv[n + j]]).any():
            return
        s_now = np.nanmean([iv[j], iv[n + j]])
        split = M.decay_split(b.anchor["f"], b.anchor["s"], b.anchor["t"], f, float(s_now), t, k)
        b.decay = {**split, "actual": c.mid + p.mid - b.anchor["p"], "k": k, "p0": b.anchor["p"], "since": b.anchor["ts"],
                   "iv0": b.anchor["s"] * 100, "iv": float(s_now) * 100}

    def _book(self, b: Book) -> None:
        ch = b.chains[0] if b.chains else None
        if ch is None or b.spot <= 0:
            return
        if b.flip is not None:
            side = 1 if b.spot > b.flip else -1
            if b.flip_side and side != b.flip_side:
                txt = (f"{b.name} crossed BELOW the gamma flip {b.flip:,.0f}: hedging flows can now amplify moves"
                       if side < 0 else
                       f"{b.name} back ABOVE the gamma flip {b.flip:,.0f}: hedging flows tend to dampen moves (pinning)")
                self._event(b, "flip", "hi", txt, b.flip)
            b.flip_side = side
        b.read = self._read(b, ch)

    def iv_ref(self, b: Book) -> tuple[float, str]:
        """ATM IV to compare with realised vol; on expiry day the next expiry's (hours-to-expiry IV is distorted)."""
        c0 = b.chains[0] if b.chains else None
        if c0 is None:
            return math.nan, ""
        if c0.expiry == to_dt(self.now).date() and len(b.chains) > 1 and math.isfinite(b.chains[1].atm_iv):
            return b.chains[1].atm_iv, "next expiry (0DTE today)"
        return c0.atm_iv, "weekly"

    def _read(self, b: Book, ch: Chain) -> dict | None:
        """Rule-based summary of the chain for a human; context, not a trade signal."""
        step = b.step or 50.0
        spot, lines, score = b.spot, [], 0
        near = [k for k in ch.strikes if abs(k - spot) <= 6 * step]

        def d15(o: Opt | None) -> float:
            old = o.ago(15) if o else None
            return (o.oi - old[2]) if old and old[2] > 0 and o.oi > 0 else 0.0

        puts = [o for k in ch.strikes if k <= spot and (o := ch.opts.get((k, False))) and o.oi > 0]
        calls = [o for k in ch.strikes if k >= spot and (o := ch.opts.get((k, True))) and o.oi > 0]
        if puts:
            s = max(puts, key=lambda o: o.oi)
            d = d15(s)
            lines.append({"l": "SUPPORT", "t": f"{s.strike:,.0f} · PE OI {fmt_qty(s.oi)} · {sq(d)}/15m · "
                          + ("writers adding" if d > 0 else "covering" if d < 0 else "steady"),
                          "tone": 1 if d > 0 else -1 if d < 0 else 0})
        if calls:
            r = max(calls, key=lambda o: o.oi)
            d = d15(r)
            lines.append({"l": "RESIST", "t": f"{r.strike:,.0f} · CE OI {fmt_qty(r.oi)} · {sq(d)}/15m · "
                          + ("writers adding" if d > 0 else "covering" if d < 0 else "steady"),
                          "tone": -1 if d > 0 else 1 if d < 0 else 0})
        if math.isfinite(b.straddle):
            lines.append({"l": "RANGE", "t": f"{spot - b.straddle:,.0f} – {spot + b.straddle:,.0f} priced to expiry "
                          f"(±{b.straddle:.0f})", "tone": 0})
        dpe = sum(d15(ch.opts.get((k, False))) for k in near)
        dce = sum(d15(ch.opts.get((k, True))) for k in near)
        tot = abs(dpe) + abs(dce)
        tone = (1 if dpe - dce > 0.15 * tot else -1 if dce - dpe > 0.15 * tot else 0) if tot > 0 else 0
        score += tone
        lines.append({"l": "WRITERS", "t": f"15m near ATM: puts {sq(dpe)} · calls {sq(dce)} → "
                      + ("put writers lead" if tone > 0 else "call writers lead" if tone < 0 else "balanced"),
                      "tone": tone})
        fl = b.flow(self.now)
        cb, pb = fl["cb"] + fl["cs"], fl["pb"] + fl["ps"]
        if cb > 0 and pb > 0:
            c_buy, p_buy = fl["cb"] / cb, fl["pb"] / pb
            net = (c_buy - 0.5) - (p_buy - 0.5)
            tone = 1 if net > 0.08 else -1 if net < -0.08 else 0
            score += tone
            lines.append({"l": "FLOW", "t": f"5m: calls {c_buy:.0%} bought · puts {p_buy:.0%} bought → "
                          + ("bullish aggression" if tone > 0 else "bearish aggression" if tone < 0 else "two-way"),
                          "tone": tone})
        if math.isfinite(b.d_ce_oi) and math.isfinite(b.d_pe_oi):
            tot = abs(b.d_pe_oi) + abs(b.d_ce_oi)
            tone = (1 if b.d_pe_oi - b.d_ce_oi > 0.15 * tot else -1 if b.d_ce_oi - b.d_pe_oi > 0.15 * tot else 0) if tot else 0
            score += tone
            lines.append({"l": "DAY OI", "t": f"puts {sq(b.d_pe_oi)} vs calls {sq(b.d_ce_oi)} since the open", "tone": tone})
        pin = False
        if b.flip is not None:
            pin = spot > b.flip and b.net_gex > 0
            lines.append({"l": "GAMMA", "t": (f"above flip {b.flip:,.0f} → PINNING, moves fade toward walls" if pin
                                             else f"{'below' if spot < b.flip else 'near'} flip {b.flip:,.0f} → TRENDING, "
                                                  "moves can extend"), "tone": 0})
        iv, lbl = self.iv_ref(b)
        if math.isfinite(iv):
            s = b.iv_series
            d = (s[-1][1] - s[-16][1]) if len(s) > 15 and s[-1][1] is not None and s[-16][1] is not None else None
            rich = iv - b.rv if math.isfinite(b.rv) else None
            lines.append({"l": "VOL", "t": f"IV {iv:.1f}%" + (f" ({d:+.1f}/15m)" if d is not None else "")
                          + (f" vs RV {b.rv:.1f}% → {'rich' if rich > 0 else 'cheap'}" if rich is not None else "")
                          + (" [next exp]" if lbl != "weekly" else ""), "tone": 0})
        if b.decay:
            d = b.decay
            lines.append({"l": "DECAY", "t": f"{'sellers' if d['actual'] < 0 else 'buyers'} ahead {d['actual']:+.1f} on "
                          f"{d['k']:,.0f} straddle since {to_dt(int(d['since'])).strftime('%H:%M')}",
                          "tone": 0})
        tp = b.tape.view(self.now, spot) if b.tape and b.tape.secs else None
        if tp:
            c15, z = tp["cvd15"] or 0, tp["ofi_z"]
            tone = 1 if c15 > 0 and (tp["ofi5"] or 0) > 0 else -1 if c15 < 0 and (tp["ofi5"] or 0) < 0 else 0
            liq = f" · MM depth {b.liq['dx']:.1f}x" if b.liq.get("dx") and b.liq["n"] >= 600 else ""
            lines.append({"l": "BOOK", "t": f"fut aggressors {c15:+,.0f} lots/15m · queues "
                          + (f"{z:+.1f}σ/1m" if z is not None else "warming up") + liq + " (untested, not scored)",
                          "tone": tone})
        br = self.breadth
        if br:
            up, flat, down = br["fno"]
            u50, _, d50 = br["n50"]
            share = up / (up + down) if up + down else 0.5
            lead = f" · lead {br['lead'][0]} {br['lead'][1]:+.1f}%" if br.get("lead") else ""
            lag = f" · lag {br['lag'][0]} {br['lag'][1]:+.1f}%" if br.get("lag") else ""
            lines.append({"l": "BREADTH", "t": f"F&O {up}↑ {down}↓ ({flat} flat) · N50 {u50}↑ {d50}↓{lead}{lag}",
                          "tone": 1 if share >= 0.6 else -1 if share <= 0.4 else 0})
        bias = ("BULLISH TILT" if score >= 2 else "BEARISH TILT" if score <= -2 else "RANGE / PIN" if pin else
                "LEANING BULLISH" if score == 1 else "LEANING BEARISH" if score == -1 else "MIXED")
        return {"bias": bias, "score": score, "max": 3, "lines": lines}

    # ---- minute roll ----------------------------------------------------------------
    def _on_minute(self, m: int) -> None:
        cfg = self.cfg
        for b in self.books.values():
            for i, ch in enumerate(b.chains[: cfg.expiries]):
                for o in ch.opts.values():
                    bar = (o.bo, o.bh, o.bl) if o.bo > 0 else (o.ltp, o.ltp, o.ltp)
                    o.mins.append((m, o.ltp, o.oi, o.buy, o.sell, o.vol, *bar))
                    o.bo = 0.0
                    old = o.ago(cfg.buildup_min)
                    if old and old[1] > 0 and o.ltp > 0:
                        o.tag_dp, o.tag_doi = (o.ltp - old[1]) / old[1] * 100, o.oi - old[2]
                        clear = abs(o.tag_dp) >= cfg.buildup_min_dp_pct / 2 and abs(o.tag_doi) >= 0.005 * max(o.oi, 1)
                        o.tag = M.buildup(o.tag_dp, o.tag_doi) if clear else ""
                if i == 0:
                    self._minute_events(b, ch)
                    self._bounce(b, ch, m, live=True)
            if b.chains:
                ch = b.chains[0]
                col = []
                for k in ch.strikes:
                    c, p = ch.opts.get((k, True)), ch.opts.get((k, False))
                    dc = c.mins[-1][2] - c.mins[-2][2] if c and len(c.mins) > 1 else 0.0
                    dp = p.mins[-1][2] - p.mins[-2][2] if p and len(p.mins) > 1 else 0.0
                    col.append([k, dc, dp])
                b.heat.append([m, col])
                b.iv_series.append([m, _r(ch.atm_iv), _r(b.chains[1].atm_iv) if len(b.chains) > 1 else None,
                                    _r(self._skew(ch))])
                del b.iv_series[:-420]
                if b.decay:
                    d = b.decay
                    b.decay_series.append([m, _r(d["actual"]), _r(d["theta"]), _r(d["move"]), _r(d["vol"])])
                    del b.decay_series[:-420]
                self._regime_events(b, ch)
            if b.tape:
                for key, sev, text, level, cool in b.tape.minute_events(self.now):
                    self._event(b, key, sev, text, level, cool=cool)
            if b.name == "NIFTY":
                self._breadth_event(b)
            b.minute_seq += 1

    def _breadth_event(self, b: Book) -> None:
        """NIFTY moving one way while most of its constituents go the other: a move carried by a few heavyweights."""
        br = self.breadth
        if not br or b.spot <= 0 or b.prev_close <= 0:
            return
        move = b.spot / b.prev_close - 1
        up, _, down = br["n50"]
        if up + down < 10:
            return
        share = up / (up + down)
        if move >= 0.003 and share <= 0.4:
            self._event(b, "breadth", "med", f"NIFTY {move:+.2%} but only {up} of the NIFTY 50 are up ({down} down) → a narrow "
                        "rally carried by a few heavyweights; such moves fade more easily (untested here)", b.spot, cool=30)
        elif move <= -0.003 and share >= 0.6:
            self._event(b, "breadth", "med", f"NIFTY {move:+.2%} while {up} of the NIFTY 50 are up ({down} down) → a narrow "
                        "sell-off in a few heavyweights, most stocks holding (untested here)", b.spot, cool=30)

    def _skew(self, ch: Chain) -> float:
        if not math.isfinite(ch.atm) or len(ch.strikes) < 11:
            return math.nan
        a = ch.strikes.index(ch.atm)
        lo, hi = max(a - 5, 0), min(a + 5, len(ch.strikes) - 1)
        p, c = ch.opts.get((ch.strikes[lo], False)), ch.opts.get((ch.strikes[hi], True))
        return (p.iv - c.iv) if p and c else math.nan

    def _minute_events(self, b: Book, ch: Chain) -> None:
        cfg = self.cfg
        opts = [o for o in ch.opts.values() if o.tag_doi]
        if not opts:
            return
        med = float(np.median([abs(o.tag_doi) for o in opts])) or 1.0
        budget = cfg.max_events_per_min
        for o in sorted(opts, key=lambda x: -abs(x.tag_doi)):
            if budget <= 0:
                break
            if not o.tag or abs(o.tag_dp) < cfg.buildup_min_dp_pct:
                continue
            if abs(o.tag_doi) < max(cfg.oi_event_mult * med, cfg.oi_event_min_pct / 100 * max(o.oi, 1)):
                continue
            above = o.strike > b.spot
            atm = abs(o.strike - b.spot) < (b.step or 50.0)
            name = f"{o.strike:g} {'CE' if o.is_call else 'PE'}"
            move = f"OI {'+' if o.tag_doi > 0 else ''}{fmt_qty(o.tag_doi)} in {cfg.buildup_min}m, price {o.tag_dp:+.0f}%"
            meaning = {
                (M.WRITE, True): (f"ATM call writing: capping the upside near {o.strike:g}" if atm else
                                  f"call writing: resistance building at {o.strike:g}" if above else "ITM calls being written"),
                (M.WRITE, False): (f"ATM put writing: a floor near {o.strike:g}" if atm else
                                   f"put writing: support building at {o.strike:g}" if not above else "ITM puts being written"),
                (M.COVER, True): "call writers covering: resistance giving way (bullish)",
                (M.COVER, False): "put writers covering: support giving way (bearish)",
                (M.LONG, True): "call buyers adding: bullish bets",
                (M.LONG, False): "put buyers adding: bearish bets or hedges",
                (M.UNWIND, True): "call longs exiting: bullish bets fading",
                (M.UNWIND, False): "put longs exiting: hedges coming off",
            }.get((o.tag, o.is_call), "")
            if meaning:
                sev = "hi" if abs(o.tag_doi) >= 2 * cfg.oi_event_mult * med else "med"
                if self._event(b, f"oi:{o.symbol}:{o.tag}", sev, f"{name}: {move} -> {meaning}", o.strike):
                    budget -= 1
        bursts = []
        step = b.step or 50.0
        for o in ch.opts.values():
            if len(o.mins) < 2 or abs(o.strike - b.spot) > 6 * step:
                continue
            dv = o.mins[-1][5] - o.mins[-2][5]
            db, ds = o.mins[-1][3] - o.mins[-2][3], o.mins[-1][4] - o.mins[-2][4]
            n = min(30, len(o.mins) - 2)
            norm = (o.mins[-2][5] - o.mins[-2 - n][5]) / n if n >= 10 else o.rates(self.now)[1] * 60
            if dv >= max(cfg.burst_mult * norm, cfg.burst_min_lots * o.lot) and norm > 0 and db + ds > 0:
                share = db / (db + ds)
                if share >= cfg.burst_side_share or share <= 1 - cfg.burst_side_share:
                    bursts.append((dv / norm, share, o, dv))
        if bursts:
            top = sorted(bursts, key=lambda x: -x[0])[:3]
            parts = [f"{o.strike:g} {'CE' if o.is_call else 'PE'} {'buyers' if sh >= 0.5 else 'sellers'} "
                     f"{ratio:.1f}x ({max(sh, 1 - sh) * 100:.0f}%)" for ratio, sh, o, dv in top]
            self._event(b, "burst", "low", "Volume bursts near the money: " + " · ".join(parts), top[0][2].strike, cool=0)

    def _regime_events(self, b: Book, ch: Chain) -> None:
        cfg = self.cfg
        s = b.iv_series
        if len(s) > 15 and s[-1][1] is not None and s[-16][1] is not None:
            d = s[-1][1] - s[-16][1]
            if abs(d) >= cfg.iv_event_pts:
                txt = (f"ATM IV {d:+.1f} pts in 15m ({s[-16][1]:.1f}% -> {s[-1][1]:.1f}%): "
                       + ("premiums getting bid, buyers paying up for protection/moves" if d > 0
                          else "IV crush, premiums deflating: sellers' tailwind"))
                self._event(b, "iv", "hi" if abs(d) >= 2 * cfg.iv_event_pts else "med", txt, ch.atm, cool=15)
        if math.isfinite(ch.atm_iv) and b.open > 0:
            sd = b.open * ch.atm_iv / 100 / math.sqrt(365)
            hit = int((b.spot - b.open) / sd) if sd > 0 else 0
            if abs(hit) > abs(b.em_hit):
                self._event(b, f"em{hit}", "hi" if abs(hit) >= 2 else "med",
                            f"{b.name} {abs(hit)} sigma {'UP' if hit > 0 else 'DOWN'} from the open ({b.spot - b.open:+,.0f} "
                            f"pts vs 1 sigma {sd:,.0f}): a trend day so far, straddle sellers under pressure", b.spot, cool=0)
                b.em_hit = hit
        if b.decay:
            d = b.decay
            side = -1 if d["actual"] < 0 else 1
            if side != b.decay_side:
                if b.decay_side:
                    self._event(b, "decay", "med",
                                f"ATM {d['k']:g} straddle {d['actual']:+.1f} since the anchor (decay {d['theta']:+.1f}, "
                                f"move {d['move']:+.1f}, IV {d['vol']:+.1f}): "
                                + ("SELLERS ahead, decay winning" if side < 0 else "BUYERS ahead, movement/IV beating decay"),
                                d["k"], cool=30)
                b.decay_side = side

    def _bounce(self, b: Book, ch: Chain, upto: int, live: bool) -> None:
        for key, sev, text, k in self.bounce.scan(b.name, b.step or 50.0, ch.opts, b.candles, b.spot, upto,
                                                   live_book=live):
            self._event(b, key, sev, text, k, cool=0)

    def _event(self, b: Book, key: str, sev: str, text: str, level, cool: int | None = None) -> bool:
        m = self.now // MIN_MS
        c = self.cfg.event_cooldown_min if cool is None else cool
        if key in b.cool and m - b.cool[key] < c:
            return False
        b.cool[key] = m
        self.seq += 1
        b.events.append({"id": self.seq, "ts": self.now, "sev": sev, "text": text, "k": _r(level)})
        return True

    # ---- history backfill (live start after the open) -------------------------------
    def backfill(self, bars: dict[tuple[str, str], list[tuple]]) -> None:
        """Merge today's 1-minute bars (minute_ms, close, oi, cumulative volume[, open, high, low]) in front of the
        live histories."""
        for key, rows in bars.items():
            t = self.route.get(key)
            if not t or t[0] != "opt" or not rows:
                continue
            o = t[3]
            first_live = o.mins[0][0] if o.mins else self.now // MIN_MS * MIN_MS
            hist = [(r[0], r[1], r[2], 0.0, 0.0, r[3], *(r[4:7] if len(r) >= 7 else (r[1],) * 3))
                    for r in rows if r[0] < first_live]
            o.mins = deque(hist + list(o.mins), maxlen=420)
            if rows[0][2] > 0:
                o.oi0 = rows[0][2]

    def rebuild(self) -> None:
        """Recompute the OI heatmap, ATM IV path, 09:20 straddle anchor and decay path from the minute histories."""
        for b in self.books.values():
            if not b.chains:
                continue
            ch = b.chains[0]
            nxt = b.chains[1] if len(b.chains) > 1 else None
            ser = {key: {x[0]: (x[1], x[2]) for x in o.mins} for key, o in ch.opts.items()}
            nser = {key: {x[0]: (x[1], x[2]) for x in o.mins} for key, o in nxt.opts.items()} if nxt else {}
            minutes = sorted({m for s in ser.values() for m in s})
            heat, last = deque(maxlen=400), {}
            for m in minutes:
                col = []
                for k in ch.strikes:
                    d = []
                    for is_call in (True, False):
                        v = ser.get((k, is_call), {}).get(m)
                        p = last.get((k, is_call))
                        d.append(v[1] - p if v and v[1] > 0 and p else 0.0)
                        if v and v[1] > 0:
                            last[(k, is_call)] = v[1]
                    col.append([k, d[0], d[1]])
                heat.append([m, col])
            b.heat = heat
            spot_at = {c[0]: c[4] for c in b.candles}
            iv_ser, dec_ser, anchor = [], [], None
            for m in minutes:
                s = spot_at.get(m)
                if not s:
                    continue
                tm = m + MIN_MS
                iv, f = self._atm_iv_at(ch, ser, m, s, tm)
                niv = self._atm_iv_at(nxt, nser, m, s, tm)[0] if nxt else math.nan
                iv_ser.append([m, _r(iv), _r(niv), None])
                if anchor is None and to_dt(m).strftime("%H:%M") >= "09:19" and math.isfinite(iv):
                    k = min(ch.strikes, key=lambda x: abs(x - f))
                    c, p = ser.get((k, True), {}).get(m), ser.get((k, False), {}).get(m)
                    sig = self._iv_pair(k, c[0], p[0], f, ch.t_years(tm)) if c and p and c[0] > 0 and p[0] > 0 else math.nan
                    if math.isfinite(sig):
                        anchor = {"k": k, "f": f, "s": sig, "t": ch.t_years(tm), "p": c[0] + p[0], "ts": tm}
                if anchor:
                    k = anchor["k"]
                    c, p = ser.get((k, True), {}).get(m), ser.get((k, False), {}).get(m)
                    if c and p and c[0] > 0 and p[0] > 0 and math.isfinite(f):
                        sig = self._iv_pair(k, c[0], p[0], f, ch.t_years(tm))
                        if math.isfinite(sig):
                            sp = M.decay_split(anchor["f"], anchor["s"], anchor["t"], f, sig, ch.t_years(tm), k)
                            dec_ser.append([m, _r(c[0] + p[0] - anchor["p"]), _r(sp["theta"]), _r(sp["move"]), _r(sp["vol"])])
            if iv_ser:
                b.iv_series = iv_ser[-420:]
            if anchor:
                b.anchor, b.decay_series, b.decay_side = anchor, dec_ser[-420:], 0
            if minutes:
                self.bounce.reset(b.name)
                self._bounce(b, ch, minutes[-1], live=False)
            b.minute_seq += 1

    @staticmethod
    def _iv_pair(k: float, c: float, p: float, f: float, t: float) -> float:
        iv = M.b76_iv(np.array([c, p]), f, np.array([k, k]), t, np.array([True, False]))
        return float(np.nanmean(iv)) if np.isfinite(iv).any() else math.nan

    def _atm_iv_at(self, ch: Chain | None, ser: dict, m: int, spot: float, tm: int) -> tuple[float, float]:
        if ch is None or not ch.strikes:
            return math.nan, spot
        k = min(ch.strikes, key=lambda x: abs(x - spot))
        c, p = ser.get((k, True), {}).get(m), ser.get((k, False), {}).get(m)
        if not (c and p and c[0] > 0 and p[0] > 0):
            return math.nan, spot
        f = k + c[0] - p[0]
        return self._iv_pair(k, c[0], p[0], f, ch.t_years(tm)) * 100, f

    # ---- frames -----------------------------------------------------------------------
    def _opt_vals(self, o: Opt | None, now: int) -> list:
        if o is None:
            return [None] * len(OPT_FIELDS)
        fast, slow = o.rates(now)
        act = min(fast / max(slow, 1.0), 10.0) if fast > 0 else 0.0
        old = o.ago(5)
        db, ds = (o.buy - old[3], o.sell - old[4]) if old else (o.buy, o.sell)
        share = db / (db + ds) if db + ds > 0 else None
        chg = (o.ltp / o.prev_close - 1) * 100 if o.prev_close > 0 and o.ltp > 0 else None
        b1, s1 = o.flow1(now)
        return [_r(o.ltp), _r(chg, 1), _r(o.oi, 0), _r(o.oi - (o.oi0 or o.oi), 0), _r(o.vol, 0), _r(act),
                _r(o.iv), _r(o.delta, 3), _r(o.gamma, 6), _r(o.theta), _r(o.vega), _r(M.imbalance(o.bq, o.aq)),
                TAGS.get(o.tag, 0), _r(o.tag_doi, 0), _r(o.fbuy, 0), _r(o.fsell, 0), _r(share), _r(o.gex / 1e7, 3),
                _r(o.bid), _r(o.ask), _r(b1, 0), _r(s1, 0)]

    def frame(self, name: str, e: int = 0, sel: float | None = None) -> dict:
        b = self.books.get(name)
        if b is None:
            return {"t": "frame", "u": name, "ok": False}
        now = self.now
        ch = b.chains[min(e, len(b.chains) - 1)] if b.chains else None
        rows = []
        if ch:
            for k in ch.window(self.cfg.strikes):
                rows.append([k] + self._opt_vals(ch.opts.get((k, True)), now) + self._opt_vals(ch.opts.get((k, False)), now))
        c0 = b.chains[0] if b.chains else None
        flow = b.flow(now)
        hud = {
            "spot": _r(b.spot), "open": _r(b.open), "prev": _r(b.prev_close),
            "fwd": _r(ch.forward if ch else None), "atm": _r(ch.atm if ch else None),
            "vix": _r(self.vix), "vix_prev": _r(self.vix_prev),
            "exp": ch.expiry.isoformat() if ch else None, "tte": _r((ch.expiry_ms - now) / 1000 if ch else None, 0),
            "exps": [c.expiry.isoformat() for c in b.chains[: self.cfg.expiries]],
            "pcr_oi": _r(b.pcr_oi), "pcr_vol": _r(b.pcr_vol), "d_ce_oi": _r(b.d_ce_oi, 0), "d_pe_oi": _r(b.d_pe_oi, 0),
            "max_pain": _r(b.max_pain), "flip": _r(b.flip), "net_gex": _r(b.net_gex / 1e7, 2),
            "call_wall": _r(b.call_wall), "put_wall": _r(b.put_wall),
            "atm_iv": _r(c0.atm_iv if c0 else None), "atm_iv_next": _r(b.chains[1].atm_iv if len(b.chains) > 1 else None),
            "skew": _r(self._skew(c0)) if c0 else None, "rv": _r(b.rv), "straddle": _r(b.straddle), "imb": _r(b.imb),
            "decay": {k: (_r(v) if not isinstance(v, str) else v) for k, v in b.decay.items()} if b.decay else None,
            "flow": {k: _r(v, 0) for k, v in flow.items()},
            "lot": next(iter(c0.opts.values())).lot if c0 and c0.opts else None,
            "step": _r(b.step),
            "dte0": bool(c0 and c0.expiry == to_dt(now).date()),
            "iv_ref": _r(self.iv_ref(b)[0]), "iv_ref_lbl": self.iv_ref(b)[1],
            "read": b.read,
            "liq": {"spread_x": _r(b.liq.get("sx")), "depth_x": _r(b.liq.get("dx")), "warm": b.liq["n"] >= 600},
        }
        out = {"t": "frame", "u": name, "e": e, "ts": now, "mode": self.mode, "status": self.status, "ok": True,
               "hud": hud, "rows": rows, "gex": {"grid": [_r(x) for x in b.grid], "net": [_r(x / 1e7, 3) for x in b.curve]},
               "smile": ch.smile if ch else [], "smile_open": ch.smile_open if ch else None,
               "minute_seq": b.minute_seq, "last_event": self.seq,
               "bounce": self.bounce.view(name, c0.opts if c0 else {}, self.mode == "live"),
               "tape": b.tape.view(now, b.spot) if b.tape and b.tape.ltp > 0 else None}
        if sel is not None and ch:
            out["depth"] = {side: self._depth(ch.opts.get((float(sel), side == "ce")), now) for side in ("ce", "pe")}
            out["depth"]["k"] = sel
        return out

    def _depth(self, o: Opt | None, now: int) -> dict | None:
        if o is None:
            return None
        fresh20 = o.depth20 is not None and now - o.d20_ts < 15_000
        dep = o.depth20 if fresh20 else (o.depth or {})
        lv = lambda xs: [[_r(x.get("price")), _r(x.get("quantity"), 0), x.get("orders")] for x in (xs or [])]  # noqa: E731
        return {"symbol": o.symbol, "levels": 20 if fresh20 else 5, "bids": lv(dep.get("buy")), "asks": lv(dep.get("sell")),
                "ltp": _r(o.ltp), "iv": _r(o.iv), "delta": _r(o.delta, 3), "gamma": _r(o.gamma, 6), "theta": _r(o.theta),
                "vega": _r(o.vega), "oi": _r(o.oi, 0), "doi": _r(o.oi - (o.oi0 or o.oi), 0), "vol": _r(o.vol, 0),
                "buy": _r(o.buy, 0), "sell": _r(o.sell, 0), "lot": o.lot}

    def reset_flows(self) -> None:
        for t in self.route.values():
            if t[0] == "opt":
                t[3].fbuy = t[3].fsell = 0.0

    def events_after(self, name: str, last: int, limit: int = 30) -> list[dict]:
        b = self.books.get(name)
        return [e for e in b.events if e["id"] > last][-limit:] if b else []

    def note(self, text: str, sev: str = "med", ts: int | None = None) -> None:
        """A system message (e.g. the data update) in every book's feed."""
        for b in self.books.values():
            self.seq += 1
            b.events.append({"id": self.seq, "ts": ts or self.now, "sev": sev, "text": text, "k": None})

    def tape_cols(self, name: str, after: int) -> list[list]:
        b = self.books.get(name)
        return b.tape.cols_after(after) if b and b.tape else []

    def history(self, name: str) -> dict:
        b = self.books.get(name)
        if b is None:
            return {"t": "history", "u": name}
        return {"t": "history", "u": name, "candles": [[c[0], *(_r(x) for x in c[1:])] for c in b.candles],
                "decay": b.decay_series, "iv": b.iv_series,
                "heat": [[m, [[k, _r(dc, 0), _r(dp, 0)] for k, dc, dp in col]] for m, col in b.heat],
                "events": list(b.events)[-60:], "minute_seq": b.minute_seq, "fields": ROW_FIELDS,
                "tape": b.tape.series if b.tape else []}
