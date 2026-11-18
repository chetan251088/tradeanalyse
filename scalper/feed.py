from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable

import websocket

log = logging.getLogger("scalper.feed")

MODE_LTP, MODE_QUOTE, MODE_DEPTH = 1, 2, 3
_BACKOFF = [1, 2, 5, 10, 30]
_CHUNK = 200


class LiveFeed:
    """OpenAlgo WebSocket client that passes the full tick payload (OI, volume, depth) through.

    The openalgo SDK's depth callback keeps only ltp+depth, so OI and volume would be lost;
    this client forwards ``data`` untouched and re-subscribes after every reconnect.
    """

    def __init__(self, url: str, api_key: str, sink: Callable[[tuple], None]):
        self.url = url
        self.api_key = api_key
        self.sink = sink
        self._subs: dict[tuple[int, int], dict[tuple[str, str], dict]] = {
            (MODE_LTP, 5): {}, (MODE_QUOTE, 5): {}, (MODE_DEPTH, 5): {}}
        self._lock = threading.Lock()
        self._ws: websocket.WebSocketApp | None = None
        self._authed = threading.Event()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._attempt = 0

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="openalgo-feed", daemon=True)
        self._thread.start()

    def wait_ready(self, timeout: float) -> bool:
        return self._authed.wait(timeout)

    def stop(self) -> None:
        self._stop.set()
        if self._ws is not None:
            try:
                self._ws.close()
            except Exception:
                pass
        if self._thread is not None:
            self._thread.join(timeout=5)

    def subscribe(self, instruments: list[tuple[str, str]], mode: int, depth: int = 5) -> None:
        """depth=20 (NFO only) adds a 20-level book on top of an existing depth-5 subscription."""
        entries = [{"symbol": s, "exchange": x} for s, x in instruments]
        with self._lock:
            subs = self._subs.setdefault((mode, depth), {})
            for e in entries:
                subs[(e["symbol"], e["exchange"])] = e
        if self._authed.is_set():
            self._send_subscribe(mode, entries, depth)

    def _run(self) -> None:
        while not self._stop.is_set():
            self._authed.clear()
            self._ws = websocket.WebSocketApp(
                self.url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=lambda ws, err: log.warning("WebSocket error: %s", err),
                on_close=self._on_close,
            )
            try:
                self._ws.run_forever(ping_interval=20, ping_timeout=10)
            except Exception as e:
                log.warning("WebSocket loop error: %s", e)
            if self._stop.is_set():
                break
            delay = _BACKOFF[min(self._attempt, len(_BACKOFF) - 1)]
            self._attempt += 1
            log.info("Reconnecting in %ss", delay)
            self._stop.wait(delay)

    def _on_open(self, ws) -> None:
        ws.send(json.dumps({"action": "authenticate", "api_key": self.api_key}))

    def _on_close(self, ws, code, reason) -> None:
        if self._stop.is_set():
            log.debug("WebSocket closed on shutdown")
        else:
            log.warning("WebSocket closed unexpectedly (%s %s)", code, reason)

    def _on_message(self, ws, raw: str) -> None:
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            return
        kind = msg.get("type")
        if kind == "market_data":
            data = msg.get("data") or {}
            ts = int(data.get("timestamp") or time.time() * 1000)
            self.sink(("tick", ts, msg.get("symbol"), msg.get("exchange"), data))
        elif kind == "auth":
            if msg.get("status") == "success":
                log.info("Feed authenticated (broker %s)", msg.get("broker"))
                self._attempt = 0
                self._authed.set()
                with self._lock:
                    pending = {key: list(subs.values()) for key, subs in self._subs.items()}
                for (mode, depth), entries in pending.items():
                    if entries:
                        self._send_subscribe(mode, entries, depth)
            else:
                log.error("Feed authentication failed: %s", msg.get("message"))
        elif kind == "subscribe":
            failed = [s for s in msg.get("subscriptions", []) if s.get("status") not in ("success", None)]
            if failed:
                log.warning("Subscription problems: %s", failed[:5])
        elif kind == "order_update":
            self.sink(("order", msg))
        elif kind == "error":
            log.warning("Feed error message: %s", msg.get("message"))

    def _send_subscribe(self, mode: int, entries: list[dict], depth: int = 5) -> None:
        ws = self._ws
        if ws is None:
            return
        for i in range(0, len(entries), _CHUNK):
            msg = {"action": "subscribe", "symbols": entries[i:i + _CHUNK], "mode": mode, "depth": depth}
            try:
                ws.send(json.dumps(msg))
            except Exception as e:
                log.warning("Subscribe send failed: %s", e)
                return
