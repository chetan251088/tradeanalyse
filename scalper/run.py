from __future__ import annotations

import argparse
import logging
import queue
import sys
import time
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

from scalper import index_update
from scalper.clock import MINUTE_MS, at, now_ms, to_dt
from scalper.config import Config, load_config, load_env_file
from scalper.engine import Engine
from scalper.feed import MODE_DEPTH, LiveFeed
from scalper.harvest import Harvester
from scalper.notify import Notifier, TelegramBot
from scalper.store import Heartbeat, Journal, TickRecorder, expand_tick, read_recording
from scalper.universe import Rest, build_meta, chain_instruments

log = logging.getLogger("scalper")


def _setup_logging(verbose: bool, log_file: Path | None) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    logging.getLogger("websocket").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)


# ---- offline: simulator and recordings --------------------------------------
def sim_events(meta: dict, ticks: Iterator[tuple]) -> Iterator[tuple]:
    yield ("meta", meta)
    for t in ticks:
        yield ("tick", *t)


def recording_events(path: str | Path) -> Iterator[tuple]:
    for rec in read_recording(path):
        kind = rec.get("type")
        if kind == "meta":
            yield ("meta", {k: v for k, v in rec.items() if k != "type"})
        elif kind == "add":
            yield ("add", rec["underlying"], rec["instruments"])
        else:
            yield ("tick", *expand_tick(rec))


def run_offline(
    cfg: Config,
    events: Iterator[tuple],
    notifier: Notifier,
    journal: Journal | None = None,
    any_day: bool = False,
    recorder: TickRecorder | None = None,
) -> Engine | None:
    engine: Engine | None = None
    last_ts: int | None = None
    for ev in events:
        if ev[0] == "meta":
            if engine is None:
                engine = Engine(cfg, ev[1], notifier, journal, recorder, live=False, any_day=any_day)
                if recorder is not None:
                    recorder.meta(ev[1])
            else:
                # a restarted live session writes a second meta line; merge its strikes
                for um in ev[1]["underlyings"]:
                    if um["name"] in engine.underlyings:
                        engine.add_options(um["name"], um["options"])
        elif ev[0] == "add":
            if engine is not None:
                engine.add_options(ev[1], ev[2])
                if recorder is not None:
                    recorder.add(ev[1], ev[2])
        elif engine is not None:
            engine.on_tick(*ev[1:])
            last_ts = ev[1]
    if engine is not None and last_ts is not None:
        engine.advance(last_ts + MINUTE_MS)
    if engine is not None:
        engine.finish()
    return engine


# ---- live -------------------------------------------------------------------
def _sleep_until(ts_ms: int) -> None:
    while True:
        left = (ts_ms - now_ms()) / 1000
        if left <= 0:
            return
        time.sleep(min(left, 60))


def _wait_for_broker(rest: Rest, notifier: Notifier, until_ms: int, login_url: str = "") -> bool:
    told = False
    while now_ms() < until_ms:
        try:
            ok, info = rest.ping()
        except Exception as e:
            ok, info = False, str(e)
        if ok:
            log.info("Broker session OK (%s)", info)
            return True
        if not told:
            notifier.send(f"Scanner waiting for broker login / OpenAlgo: {info}\nLog in to OpenAlgo and connect Dhan: {login_url}")
            told = True
        time.sleep(30)
    return False


def _recenter(engine: Engine, rest: Rest, cfg: Config, feed: LiveFeed, recorder: TickRecorder) -> None:
    for ucfg in cfg.underlyings:
        u = engine.underlyings.get(ucfg.name)
        if u is None or u.center_strike is None or u.step <= 0 or u.index.ltp <= 0:
            continue
        if abs(u.index.ltp - u.center_strike) < cfg.universe.recenter_strikes * u.step:
            continue
        try:
            chain = rest.option_chain(ucfg, u.expiry, cfg.universe.strike_count)
        except Exception as e:
            log.warning("Recenter of %s failed: %s", ucfg.name, e)
            continue
        options = chain_instruments(ucfg, chain)
        added = engine.add_options(ucfg.name, options)
        u.center_strike = float(chain.get("atm_strike") or u.atm(u.index.ltp) or u.center_strike)
        if added:
            names = {i.symbol for i in added}
            feed.subscribe([i.key for i in added], MODE_DEPTH)
            recorder.add(ucfg.name, [o for o in options if o["symbol"] in names])
            log.info("%s recentred on %s, +%d strikes", ucfg.name, u.center_strike, len(added))


def run_day(cfg: Config, rest: Rest, api_key: str, notifier: Notifier, journal: Journal, any_day: bool) -> None:
    day = to_dt(now_ms()).date()
    end_ms = at(day, cfg.session.end)
    meta = build_meta(rest, cfg, day)
    try:
        mode = rest.analyzer_mode()
    except Exception as e:
        mode = f"unknown ({e})"
    recorder = TickRecorder(Path(cfg.paths.data_dir) / "ticks" / f"{day.isoformat()}.jsonl.gz")
    recorder.meta(meta)
    engine = Engine(cfg, meta, notifier, journal, recorder, live=True, any_day=any_day)
    status = ", ".join(
        f"{u.name} {'EXPIRY' if u.is_expiry_day else 'no expiry'} ({u.expiry}, scan {'on' if u.scan_enabled else 'off'})"
        for u in engine.underlyings.values()
    )
    notifier.send(f"Scanner started {day} | OpenAlgo mode {mode} | {status}")

    events: queue.Queue = queue.Queue(maxsize=500_000)
    dropped = 0

    def sink(ev: tuple) -> None:
        nonlocal dropped
        try:
            events.put_nowait(ev)
        except queue.Full:
            dropped += 1

    feed = LiveFeed(cfg.openalgo.ws_url, api_key, sink)
    feed.start()
    if not feed.wait_ready(30):
        log.warning("Feed not authenticated yet; it keeps retrying in the background")
    for mode_id, keys in engine.subscriptions().items():
        feed.subscribe(keys, mode_id)
    log.info("Subscribed %d instruments", sum(len(k) for k in engine.subscriptions().values()))

    recenter_every = cfg.universe.recenter_minutes * MINUTE_MS
    next_recenter = now_ms() + recenter_every
    harvester = Harvester(cfg, rest, journal.con, notifier)
    next_harvest = 0
    try:
        while True:
            now = now_ms()
            if now >= end_ms:
                break
            if now >= next_harvest:
                harvester.poll(now)
                next_harvest = now + 5_000
            try:
                ev = events.get(timeout=1.0)
            except queue.Empty:
                engine.advance(now)
                continue
            if ev[0] == "tick":
                engine.on_tick(*ev[1:])
            if now >= next_recenter:
                _recenter(engine, rest, cfg, feed, recorder)
                next_recenter = now + recenter_every
    finally:
        feed.stop()
        recorder.close()
    if dropped:
        log.warning("Dropped %d events because the queue was full", dropped)
    engine.finish()
    notifier.send(engine.summary(), priority=4)
    try:
        index_update.update(rest, cfg)
    except Exception as e:
        log.warning("Index/VIX top-up failed: %s", e)


def run_live(cfg: Config, any_day: bool, telegram: bool) -> int:
    from openalgo import api

    api_key = cfg.api_key()
    if not api_key:
        log.error("Set %s (env var or --env-file) to your OpenAlgo API key", cfg.openalgo.api_key_env)
        return 2
    client = api(api_key=api_key, host=cfg.openalgo.host)
    rest = Rest(client, api_key)
    notifier = Notifier(client if telegram else None, cfg.notify.telegram_username, cfg.notify.min_interval_sec,
                        TelegramBot.from_env() if telegram else None)
    journal = Journal(cfg.paths.journal)
    beat = Heartbeat(cfg.paths.data_dir)
    while True:
        day = to_dt(now_ms()).date()
        next_wake = at(day + timedelta(days=1), cfg.session.wake)
        end_ms = at(day, cfg.session.end)
        if day.weekday() >= 5 or now_ms() >= end_ms:
            log.info("Outside trading hours; sleeping until %s", to_dt(next_wake))
            beat.phase = f"sleeping until {to_dt(next_wake):%a %H:%M} IST"
            _sleep_until(next_wake)
            continue
        beat.phase = f"sleeping until {cfg.session.wake} IST"
        _sleep_until(at(day, cfg.session.wake))
        beat.phase = "waiting for broker login"
        if not _wait_for_broker(rest, notifier, end_ms, cfg.openalgo.host):
            continue
        try:
            beat.phase = "in session"
            run_day(cfg, rest, api_key, notifier, journal, any_day)
        except Exception as e:
            log.exception("Session failed")
            beat.phase = f"error, restarting: {str(e)[:80]}"
            notifier.send(f"Scanner error, restarting in 30s: {e}", priority=9)
            time.sleep(30)


def run_check(cfg: Config, telegram: bool) -> int:
    """One-shot connectivity test; works on weekends (data may be the last close)."""
    from openalgo import api

    api_key = cfg.api_key()
    if not api_key:
        log.error("FAIL api key: set %s (env var or --env-file)", cfg.openalgo.api_key_env)
        return 2
    client = api(api_key=api_key, host=cfg.openalgo.host)
    rest = Rest(client, api_key)
    ok, info = rest.ping()
    if not ok:
        log.error("FAIL broker session: %s (log in to OpenAlgo and connect the broker)", info)
        return 1
    log.info("OK   broker session: %s", info)
    log.info("OK   OpenAlgo mode: %s", rest.analyzer_mode())
    day = to_dt(now_ms()).date()
    meta = build_meta(rest, cfg, day)
    for um in meta["underlyings"]:
        opts = um["options"]
        sample = opts[len(opts) // 2] if opts else {}
        log.info(
            "OK   %s: next expiry %s (expiry today: %s), %d contracts, lot %s, e.g. %s ltp %s oi %s",
            um["name"], um["expiry"], um["is_expiry_day"], len(opts), sample.get("lotsize"),
            sample.get("symbol"), (sample.get("seed") or {}).get("ltp"), (sample.get("seed") or {}).get("oi"),
        )

    got: list[tuple] = []
    feed = LiveFeed(cfg.openalgo.ws_url, api_key, got.append)
    feed.start()
    if not feed.wait_ready(15):
        feed.stop()
        log.error("FAIL WebSocket login at %s", cfg.openalgo.ws_url)
        return 1
    log.info("OK   WebSocket login")
    engine = Engine(cfg, meta, Notifier(), live=True)
    for mode_id, keys in engine.subscriptions().items():
        feed.subscribe(keys, mode_id)
    deadline = time.time() + 15
    while time.time() < deadline and len(got) < 20:
        time.sleep(0.5)
    feed.stop()
    ticks = [e for e in got if e[0] == "tick"]
    if ticks:
        _, ts, sym, exch, data = ticks[-1]
        log.info("OK   live ticks: %d in 15s, e.g. %s %s %s", len(ticks), exch, sym,
                 {k: data.get(k) for k in ("ltp", "volume", "oi") if k in data})
    else:
        log.warning("--   no ticks in 15s (normal when the market is closed)")

    if telegram and (bot := TelegramBot.from_env()) is not None:
        log.info("%s Telegram (scanner bot)", "OK  " if bot.send("Scanner check: connection OK") else "FAIL")
    elif telegram and cfg.notify.telegram_username:
        res = client.telegram(username=cfg.notify.telegram_username, message="Scanner check: connection OK")
        log.info("%s Telegram: %s", "OK  " if res.get("status") == "success" else "FAIL", res.get("message", res))
    else:
        log.info("--   Telegram not configured (TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID or notify.telegram_username)")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="scalper", description="Expiry-day options scanner on OpenAlgo (alerts only)")
    p.add_argument("--config", default="config/dhan.yaml")
    p.add_argument("--env-file", help="KEY=VALUE file with OPENALGO_API_KEY")
    src = p.add_mutually_exclusive_group()
    src.add_argument("--sim", action="store_true", help="run a synthetic SENSEX expiry day")
    src.add_argument("--replay", metavar="FILE", help="replay a recorded data/ticks/<day>.jsonl.gz")
    src.add_argument("--check", action="store_true", help="test the OpenAlgo/broker/WebSocket/Telegram connection and exit")
    src.add_argument("--history", metavar="YYYY-MM-DD", help="replay a past session rebuilt from Dhan 1-minute bars")
    p.add_argument("--refresh", action="store_true", help="with --history: download again even if cached")
    p.add_argument("--seed", type=int, default=7, help="simulator random seed")
    p.add_argument("--record", metavar="FILE", help="offline only: also write the ticks to this recording")
    p.add_argument("--any-day", action="store_true", help="scan even when today is not an expiry day")
    p.add_argument("--telegram", choices=["auto", "on", "off"], default="auto",
                   help="auto = on for live (if a username is configured), off for sim/replay")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args(argv)

    if args.env_file:
        load_env_file(args.env_file)
    cfg = load_config(args.config)
    offline = bool(args.sim or args.replay or args.history)
    log_file = None if offline else Path(cfg.paths.data_dir) / "logs" / f"scalper-{to_dt(now_ms()).date()}.log"
    _setup_logging(args.verbose, log_file)
    telegram = args.telegram == "on" or (args.telegram == "auto" and not offline)

    if args.check:
        return run_check(cfg, telegram)
    if not offline:
        return run_live(cfg, args.any_day, telegram)

    client = None
    if telegram:
        from openalgo import api

        client = api(api_key=cfg.api_key(), host=cfg.openalgo.host)
    notifier = Notifier(client, cfg.notify.telegram_username, cfg.notify.min_interval_sec,
                        TelegramBot.from_env() if telegram else None)
    journal_path = Path(cfg.paths.data_dir) / "offline_journal.sqlite"
    journal_path.unlink(missing_ok=True)
    journal = Journal(journal_path)
    recorder = TickRecorder(args.record) if args.record else None
    if args.sim:
        from scalper import sim

        meta, ticks = sim.generate(seed=args.seed)
        events = sim_events(meta, ticks)
    elif args.history:
        from datetime import date as _date

        from openalgo import api

        from scalper.history import build_history_recording

        day = _date.fromisoformat(args.history)
        cached = Path(cfg.paths.data_dir) / "history" / f"{day.isoformat()}.jsonl.gz"
        if args.refresh or not cached.exists():
            if not cfg.api_key():
                log.error("Set %s (env var or --env-file) to fetch history", cfg.openalgo.api_key_env)
                return 2
            log.info("Downloading %s 1-minute data from OpenAlgo (about a minute)...", day)
            hist_client = api(api_key=cfg.api_key(), host=cfg.openalgo.host)
            build_history_recording(Rest(hist_client, cfg.api_key()), cfg, day, cached)
        events = recording_events(cached)
    else:
        events = recording_events(args.replay)
    started = time.time()
    engine = run_offline(cfg, events, notifier, journal, args.any_day, recorder)
    if recorder is not None:
        recorder.close()
    if engine is None:
        log.error("No meta record found; nothing to run")
        return 1
    log.info("Offline run finished in %.1fs; journal %s\n%s", time.time() - started, journal_path, engine.summary())
    if telegram:
        time.sleep(cfg.notify.min_interval_sec * (len(engine.signals) + 1))
    journal.close()
    return 0
