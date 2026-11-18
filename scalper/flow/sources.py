"""Data sources for the flow engine: live (OpenAlgo REST snapshot + WebSocket ticks) and replay (stored minute data)."""

from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd

from scalper.clock import at, now_ms, to_dt
from scalper.config import Config, UnderlyingCfg
from scalper.feed import MODE_DEPTH, MODE_QUOTE, LiveFeed
from scalper.flow.engine import FlowEngine

log = logging.getLogger("scalper.flow")

MARKET = Path("data/market")


class LiveSource:
    """Snapshots each chain over REST, then streams full packets (5-level book) for every strike plus a 20-level
    book for the near-the-money strikes of one underlying's current expiry (Dhan: NFO only, at most 50)."""

    def __init__(self, cfg: Config, engine: FlowEngine):
        from openalgo import api

        from scalper.universe import Rest

        self.cfg, self.engine, self.fcfg = cfg, engine, cfg.flow
        self.api_key = cfg.api_key()
        if not self.api_key:
            raise RuntimeError(f"set {cfg.openalgo.api_key_env} (env var or --env-file)")
        self.rest = Rest(api(api_key=self.api_key, host=cfg.openalgo.host), self.api_key)
        self.q: queue.SimpleQueue = queue.SimpleQueue()
        self.feed = LiveFeed(cfg.openalgo.ws_url, self.api_key, self.q.put)
        self.ucfg: dict[str, UnderlyingCfg] = {}
        self.d20: set[tuple[str, str]] = set()
        self.futs: list[tuple[str, str]] = []
        self.rec = None
        self.next_recenter = 0
        self.shift = 0
        self.loaded = 0

    def start(self) -> None:
        ok, info = self.rest.ping()
        if not ok:
            raise RuntimeError(f"OpenAlgo/broker not ready: {info}. Log in to OpenAlgo and connect Dhan first.")
        today = to_dt(now_ms()).date()
        for u in self.cfg.underlyings:
            if not u.enabled:
                continue
            self.ucfg[u.name] = u
            self.engine.add_book(u.name, u.name, u.index_exchange, 0)
            for i, exp in enumerate([d for d in self.rest.expiries(u) if d >= today][: self.fcfg.expiries]):
                self._load_chain(u, exp)
            self._candles(u, today)
            if self.fcfg.futures:
                self._future(u, today)
        self.engine.set_vix(self.cfg.universe.vix_symbol, self.cfg.universe.vix_exchange)
        self.feed.start()
        if not self.feed.wait_ready(20):
            log.warning("WebSocket not authenticated yet; it keeps retrying")
        idx = [(u.name, u.index_exchange) for u in self.ucfg.values()]
        idx.append((self.cfg.universe.vix_symbol, self.cfg.universe.vix_exchange))
        self.feed.subscribe(idx, MODE_QUOTE)
        opts = [(s, x) for s, x, kind in self.engine.instruments() if kind == "opt"]
        self.feed.subscribe(opts, MODE_DEPTH)
        if self.futs:
            self.feed.subscribe(self.futs, MODE_DEPTH)
            deep = [k for k in self.futs if k[1] == "NFO"]
            if self.fcfg.depth20 and deep:
                self.feed.subscribe(deep, MODE_DEPTH, depth=20)
                self.d20.update(deep)
        self._depth20()
        if self.fcfg.record_depth:
            self._start_recorder(today)
        self.engine.status = (f"live: {len(opts)} options, {len(self.futs)} futures, {len(self.d20)} with 20-level depth"
                              + (", recording the book" if self.rec else ""))
        log.info(self.engine.status)
        self.next_recenter = now_ms() + 60_000
        if to_dt(now_ms()).strftime("%H:%M") > "09:16":
            threading.Thread(target=self._backfill, args=(today,), name="flow-backfill", daemon=True).start()

    def _future(self, u: UnderlyingCfg, today: date) -> None:
        try:
            f = self.rest.future(u, today)
        except Exception as e:
            log.warning("No futures for %s: %s", u.name, e)
            return
        if f:
            self.engine.add_future(u.name, f["symbol"], u.fo_exchange, f["lot"], f["tick"])
            self.futs.append((f["symbol"], u.fo_exchange))
            log.info("%s futures: %s (lot %d, tick %g)", u.name, f["symbol"], f["lot"], f["tick"])

    def _start_recorder(self, today: date) -> None:
        from scalper.flow.record import DepthRecorder

        path = Path(self.cfg.paths.data_dir) / "flow" / "depth" / f"{today.isoformat()}_{to_dt(now_ms()):%H%M%S}.jsonl.gz"
        self.rec = DepthRecorder(path)
        self.rec.meta({
            "day": today.isoformat(), "started": now_ms(), "record_strikes": self.fcfg.record_strikes,
            "futures": {b.name: {"symbol": b.tape.symbol, "exchange": b.tape.exchange, "lot": b.tape.lot,
                                 "tick": b.tape.tick} for b in self.engine.books.values() if b.tape},
            "books": {b.name: {"index": [b.name, self.ucfg[b.name].index_exchange], "step": b.step,
                               "expiries": [c.expiry.isoformat() for c in b.chains]} for b in self.engine.books.values()},
        })
        self._record_set()
        log.info("Recording the order book to %s", path)

    def _record_set(self) -> None:
        """Futures every packet; indices, VIX and the current expiry's ATM +-N strikes once a second."""
        if self.rec is None:
            return
        want = {k: 0 for k in self.futs}
        want[(self.cfg.universe.vix_symbol, self.cfg.universe.vix_exchange)] = 1000
        for name, u in self.ucfg.items():
            want[(u.name, u.index_exchange)] = 1000
            b = self.engine.books[name]
            if not b.chains or b.spot <= 0:
                continue
            ch = b.chains[0]
            for k in sorted(ch.strikes, key=lambda k: abs(k - b.spot))[: 2 * self.fcfg.record_strikes + 1]:
                for is_call in (True, False):
                    o = ch.opts.get((k, is_call))
                    if o:
                        want[(o.symbol, o.exchange)] = 1000
        self.rec.want = want

    def _backfill(self, day: date) -> None:
        """Today's 1-minute close/OI/volume for every strike, so OI change, heatmap and decay start at the open."""
        from scalper.history import fetch_bars

        todo = []
        for b in self.engine.books.values():
            for i, ch in enumerate(b.chains):
                for o in ch.opts.values():
                    todo.append((i, abs(o.strike - b.spot), o.symbol, o.exchange))
        todo.sort()
        chunk: dict = {}
        for n, (_, _, sym, exch) in enumerate(todo, 1):
            try:
                rows = fetch_bars(self.rest, sym, exch, day)
            except Exception as e:
                log.warning("History for %s failed: %s", sym, e)
                rows = []
            bars, cum = [], 0.0
            for r in rows:
                ts = int(r["timestamp"]) * 1000 - self.shift
                dt = to_dt(ts)
                if dt.date() == day and "09:15" <= dt.strftime("%H:%M") <= "15:29":
                    cum += float(r.get("volume") or 0)
                    bars.append((ts // 60_000 * 60_000, float(r["close"]), float(r.get("oi") or 0), cum,
                                 float(r.get("open") or r["close"]), float(r.get("high") or r["close"]),
                                 float(r.get("low") or r["close"])))
            chunk[(sym, exch)] = bars
            self.loaded = n
            if len(chunk) >= 25 or n == len(todo):
                self.q.put(("backfill", chunk, n, len(todo)))
                chunk = {}
        log.info("Backfilled today's history for %d contracts", len(todo))

    def _load_chain(self, u: UnderlyingCfg, exp: date) -> list[str]:
        chain = self.rest.option_chain(u, exp, self.fcfg.strikes)
        rows = []
        for r in chain.get("chain", []):
            for side in ("ce", "pe"):
                q = r.get(side) or {}
                if q.get("symbol"):
                    rows.append({"symbol": q["symbol"], "exchange": u.fo_exchange, "strike": float(r["strike"]),
                                 "is_call": side == "ce", "lot": q.get("lotsize"), "ltp": q.get("ltp"), "bid": q.get("bid"),
                                 "ask": q.get("ask"), "oi": q.get("oi"), "volume": q.get("volume"),
                                 "prev_close": q.get("prev_close")})
        new = [r["symbol"] for r in rows if (r["symbol"], r["exchange"]) not in self.engine.route]
        self.engine.add_chain(u.name, exp, rows)
        b = self.engine.books[u.name]
        if not b.spot and chain.get("underlying_ltp"):
            b.spot = float(chain["underlying_ltp"])
        if not b.prev_close and chain.get("underlying_prev_close"):
            b.prev_close = float(chain["underlying_prev_close"])
        log.info("%s %s: %d contracts (%d new)", u.name, exp, len(rows), len(new))
        return new

    def _candles(self, u: UnderlyingCfg, day: date) -> None:
        from scalper.history import fetch_bars, session_shift_ms

        try:
            rows = fetch_bars(self.rest, u.name, u.index_exchange, day)
        except Exception as e:
            log.warning("No intraday history for %s: %s", u.name, e)
            return
        if not rows:
            return
        shift = session_shift_ms(rows, day, "09:15")
        self.shift = shift
        b = self.engine.books[u.name]
        for r in rows:
            ts = int(r["timestamp"]) * 1000 - shift
            dt = to_dt(ts)
            if dt.date() == day and "09:15" <= dt.strftime("%H:%M") <= "15:29":
                b.candles.append([ts // 60_000 * 60_000, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])])
        if b.candles:
            b.open = b.candles[0][1]

    def _depth20(self) -> None:
        name = self.fcfg.depth20_underlying
        b = self.engine.books.get(name)
        if not self.fcfg.depth20 or b is None or not b.chains or self.ucfg[name].fo_exchange != "NFO":
            return
        ch = b.chains[0]
        centre = b.spot or ch.strikes[len(ch.strikes) // 2]
        want = []
        for k in sorted(ch.strikes, key=lambda k: abs(k - centre)):
            for is_call in (True, False):
                o = ch.opts.get((k, is_call))
                if o and (o.symbol, o.exchange) not in self.d20:
                    want.append((o.symbol, o.exchange))
        want = want[: max(50 - len(self.d20), 0)]
        if want:
            self.feed.subscribe(want, MODE_DEPTH, depth=20)
            self.d20.update(want)

    def pump(self, limit: int = 50_000) -> None:
        eng = self.engine
        for _ in range(limit):
            try:
                ev = self.q.get_nowait()
            except queue.Empty:
                break
            if ev[0] == "tick":
                if self.rec is not None:
                    try:
                        self.rec.add(ev[1], ev[2], ev[3], ev[4])
                    except OSError as e:
                        log.error("Order-book recording stopped: %s", e)
                        self.rec = None
                eng.on_tick(ev[1], ev[2], ev[3], ev[4])
            elif ev[0] == "backfill":
                eng.backfill(ev[1])
                done, total = ev[2], ev[3]
                eng.status = (f"live: loading today's history {done}/{total}" if done < total
                              else f"live: {total} contracts, history since 09:15 loaded")
                if done >= total:
                    eng.rebuild()
        eng.advance(now_ms())
        if now_ms() >= self.next_recenter:
            self.next_recenter = now_ms() + 60_000
            self._recenter()

    def _recenter(self) -> None:
        for name, u in self.ucfg.items():
            b = self.engine.books[name]
            for ch in b.chains:
                if not ch.strikes or b.spot <= 0 or not b.step:
                    continue
                inside = [k for k in ch.strikes if abs(k - b.spot) <= self.fcfg.strikes * b.step]
                if len(inside) >= 2 * self.fcfg.strikes + 1 - self.fcfg.recenter_strikes:
                    continue
                try:
                    new = self._load_chain(u, ch.expiry)
                except Exception as e:
                    log.warning("Recenter of %s %s failed: %s", name, ch.expiry, e)
                    continue
                if new:
                    self.feed.subscribe([(s, u.fo_exchange) for s in new], MODE_DEPTH)
        self._depth20()
        self._record_set()

    def control(self, msg: dict) -> None:
        pass

    def stop(self) -> None:
        self.feed.stop()
        if self.rec is not None:
            self.rec.close()


class ReplaySource:
    """Plays a stored session (1-minute index, VIX and option-chain bars) as ticks, `speed` x real time.

    Each minute is split into sub-steps so prices glide and volume flows continuously. There is no order book in
    the stored data, so bid/ask are synthetic and the buyer/seller split follows the price direction.
    """

    STEPS = 6

    def __init__(self, cfg: Config, engine: FlowEngine, day: date, speed: float = 30.0, start: str = "09:15"):
        from scalper.backtest import expiry_calendar, next_expiry
        from scalper.vrp_harvest import lot_size

        self.cfg, self.engine, self.day = cfg, engine, day
        self.speed, self.paused = speed, False
        self.t_start, self.t_end = at(day, start), at(day, "15:30")
        self.cursor = self.t_start
        self.wall0, self.t0 = time.monotonic(), self.t_start
        self.minutes: dict[int, list] = {}
        names = []
        for u in cfg.underlyings:
            f = MARKET / "options" / u.name / "chain" / f"{day.isoformat()}.parquet"
            if not u.enabled or not f.exists():
                continue
            exp = next_expiry(expiry_calendar(u.name), day)
            lot = lot_size(u.name, day)
            ch = pd.read_parquet(f)
            ch["minute"] = ch.ts.astype("int64") * 1000
            ch = ch.drop_duplicates(["minute", "opt", "strike"]).sort_values("minute")
            tag = f"{u.name}{exp.strftime('%d%b%y').upper()}"
            ch["symbol"] = tag + ch.strike.astype(int).astype(str) + ch.opt
            b = engine.add_book(u.name, u.name, u.index_exchange, 0)
            first = ch.groupby("symbol").first()
            engine.add_chain(u.name, exp, [{"symbol": s, "exchange": u.fo_exchange, "strike": r.strike, "is_call": r.opt == "CE",
                                            "lot": lot, "prev_close": r.open} for s, r in first.iterrows()])
            ix = _bars(u.name, day)
            b.prev_close = _prev_close(u.name, day)
            self._add(ix, u.name, u.index_exchange, kind="index")
            self._add_chain(ch, u.fo_exchange)
            names.append(u.name)
        if not names:
            raise RuntimeError(f"no stored option chains for {day} (data/market/options/<NAME>/chain/{day}.parquet)")
        vix = _bars(cfg.universe.vix_symbol, day)
        engine.set_vix(cfg.universe.vix_symbol, cfg.universe.vix_exchange)
        engine.vix_prev = _prev_close(cfg.universe.vix_symbol, day)
        self._add(vix, cfg.universe.vix_symbol, cfg.universe.vix_exchange, kind="index")
        self.order = sorted(self.minutes)
        self.pos = 0
        engine.status = f"replay {day} ({', '.join(names)}) at {speed:g}x"

    def _add(self, bars: pd.DataFrame, symbol: str, exchange: str, kind: str) -> None:
        for r in bars.itertuples(index=False):
            m = int(r.ts) * 1000
            path = (r.open, r.low, r.high, r.close) if r.close >= r.open else (r.open, r.high, r.low, r.close)
            self.minutes.setdefault(m, []).append((symbol, exchange, "ix", path, 0.0, 0.0))

    def _add_chain(self, ch: pd.DataFrame, exchange: str) -> None:
        ch = ch.fillna({"volume": 0, "oi": 0})
        for r in ch.itertuples(index=False):
            path = (r.open, r.low, r.high, r.close) if r.close >= r.open else (r.open, r.high, r.low, r.close)
            self.minutes.setdefault(int(r.minute), []).append((r.symbol, exchange, "opt", path, float(r.volume), float(r.oi)))

    def _emit_minute(self, m: int) -> None:
        eng, n = self.engine, self.STEPS
        for s in range(n):
            ts = m + int((s + 1) * 60_000 / n) - 1
            frac = (s + 1) / n
            for sym, exch, kind, path, vol, oi in self.minutes[m]:
                if kind == "ix":
                    px = float(np.interp(frac, np.linspace(0, 1, len(path)), path))
                    eng.on_tick(ts, sym, exch, {"ltp": px, "open": path[0]})
                    continue
                o = eng.route[(sym, exch)][3]
                px = float(np.interp(frac, np.linspace(0, 1, len(path)), path))
                if px <= 0:
                    continue
                hs = max(0.05, round(px * 0.0005 / 0.05) * 0.05)
                d = {"ltp": px, "volume": (o.vol or 1.0) + vol / n,
                     "depth": {"buy": [{"price": px - hs, "quantity": 0}], "sell": [{"price": px + hs, "quantity": 0}]}}
                if s == n - 1 and oi > 0:
                    d["oi"] = oi
                eng.on_tick(ts, sym, exch, d)

    def pump(self, limit: int = 0) -> None:
        if not self.paused:
            target = self.t0 + (time.monotonic() - self.wall0) * 1000 * self.speed
        else:
            target = self.cursor
        while self.pos < len(self.order) and self.order[self.pos] + 60_000 <= target:
            self._emit_minute(self.order[self.pos])
            self.pos += 1
        self.cursor = min(max(self.cursor, target), self.t_end)
        self.engine.advance(int(self.cursor))
        if self.pos >= len(self.order):
            self.engine.status = f"replay {self.day} finished"

    def control(self, msg: dict) -> None:
        if "speed" in msg:
            self.t0, self.wall0 = self.cursor, time.monotonic()
            self.speed = float(min(max(msg["speed"], 1), 600))
        if "pause" in msg:
            self.paused = bool(msg["pause"])
            self.t0, self.wall0 = self.cursor, time.monotonic()
        self.engine.status = f"replay {self.day} at {self.speed:g}x{' (paused)' if self.paused else ''}"

    def stop(self) -> None:
        pass


def _year_bars(name: str, year: int) -> pd.DataFrame:
    f = MARKET / "index" / name / f"{year}.parquet"
    if not f.exists():
        return pd.DataFrame(columns=["ts", "open", "high", "low", "close"])
    df = pd.read_parquet(f)
    dt = pd.to_datetime(df.ts, unit="s", utc=True).dt.tz_convert("Asia/Kolkata")
    hm = dt.dt.strftime("%H:%M")
    return df.assign(day=dt.dt.date)[(hm >= "09:15") & (hm <= "15:29")]


def _bars(name: str, day: date) -> pd.DataFrame:
    df = _year_bars(name, day.year)
    return df[df.day == day].sort_values("ts")


def _prev_close(name: str, day: date) -> float:
    df = pd.concat([_year_bars(name, day.year - 1), _year_bars(name, day.year)])
    prev = df[df.day < day]
    return float(prev.sort_values("ts").close.iloc[-1]) if len(prev) else 0.0


def replay_days(name: str = "NIFTY", n: int = 15) -> list[str]:
    files = sorted((MARKET / "options" / name / "chain").glob("*.parquet"))
    return [f.stem for f in files[-n:]]


def parse_day(text: str) -> date:
    return datetime.strptime(text, "%Y-%m-%d").date()
