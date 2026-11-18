"""Flow dashboard server: serves web/flow over HTTP and streams engine frames over /ws (one asyncio loop)."""

from __future__ import annotations

import asyncio
import json
import logging
import mimetypes
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path

from websockets.asyncio.server import ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Response

from scalper.clock import now_ms
from scalper.flow.engine import ROW_FIELDS, FlowEngine

log = logging.getLogger("scalper.flow")

WEB = Path(__file__).resolve().parents[2] / "web" / "flow"
TYPES = {".js": "text/javascript", ".css": "text/css", ".html": "text/html; charset=utf-8", ".svg": "image/svg+xml"}


class Client:
    def __init__(self, ws: ServerConnection, u: str):
        self.ws, self.u, self.e, self.sel = ws, u, 0, None
        self.last_event = 0
        self.last_col = 0
        self.minute_seq = -1


class FlowServer:
    def __init__(self, engine: FlowEngine, source, frame_hz: float, rebuild: Callable[[str], tuple] | None = None,
                 replay_days: list[str] | None = None, updater=None, breadth=None, order_flow_url: str = ""):
        self.engine, self.source = engine, source
        self.period = 1.0 / frame_hz
        self.rebuild = rebuild
        self.replay_days = replay_days or []
        self.updater = updater
        self.breadth = breadth
        self.order_flow_url = order_flow_url
        self.clients: set[Client] = set()

    def hello(self) -> dict:
        return {"t": "hello", "mode": self.engine.mode, "books": list(self.engine.books), "fields": ROW_FIELDS,
                "status": self.engine.status, "replay_days": self.replay_days,
                "day": getattr(self.source, "day", None) and self.source.day.isoformat(),
                "speed": getattr(self.source, "speed", None), "order_flow": self.order_flow_url}

    def process_request(self, connection: ServerConnection, request):
        path = request.path.split("?")[0]
        if path == "/ws":
            return None
        f = (WEB / ("index.html" if path in ("", "/") else path.lstrip("/"))).resolve()
        if WEB not in f.parents or not f.is_file():
            return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")
        body = f.read_bytes()
        ctype = TYPES.get(f.suffix) or mimetypes.guess_type(f.name)[0] or "application/octet-stream"
        headers = Headers([("Content-Type", ctype), ("Content-Length", str(len(body))), ("Cache-Control", "no-store")])
        return Response(200, "OK", headers, body)

    async def handler(self, ws: ServerConnection) -> None:
        c = Client(ws, next(iter(self.engine.books), ""))
        self.clients.add(c)
        try:
            await ws.send(json.dumps(self.hello()))
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                except (TypeError, ValueError):
                    continue
                self.on_message(c, msg)
        except ConnectionClosed:
            pass
        finally:
            self.clients.discard(c)

    def on_message(self, c: Client, msg: dict) -> None:
        if msg.get("t") == "view":
            u = msg.get("u")
            if u in self.engine.books and u != c.u:
                c.u, c.minute_seq, c.last_event, c.last_col = u, -1, 0, 0
            c.e = 1 if msg.get("e") == 1 else 0
            sel = msg.get("sel")
            c.sel = float(sel) if isinstance(sel, (int, float)) else None
        elif msg.get("t") == "update":
            if self.updater is not None:
                self.updater.start()
        elif msg.get("t") == "replay":
            if msg.get("day") and self.rebuild is not None and msg["day"] in self.replay_days:
                self.engine, self.source = self.rebuild(msg["day"])
                for cl in self.clients:
                    cl.minute_seq, cl.last_event, cl.last_col = -1, 0, 0
                    if cl.u not in self.engine.books:
                        cl.u = next(iter(self.engine.books), "")
                asyncio.ensure_future(self.broadcast(self.hello()))
            else:
                self.source.control(msg)

    async def broadcast(self, msg: dict) -> None:
        raw = json.dumps(msg)
        for c in list(self.clients):
            await self._send(c, raw)

    async def _send(self, c: Client, raw: str) -> None:
        try:
            await asyncio.wait_for(c.ws.send(raw), timeout=1.0)
        except (ConnectionClosed, asyncio.TimeoutError):
            self.clients.discard(c)

    async def run_loop(self) -> None:
        loop = asyncio.get_running_loop()
        next_analysis = 0.0
        while True:
            t0 = loop.time()
            eng = self.engine
            try:
                self.source.pump()
                if self.updater is not None:
                    for sev, text in self.updater.drain():
                        eng.note(text, sev, now_ms())
                data = self.updater.view() if self.updater is not None else None
                # Order Flow's board is today's market: never mix it into a replayed day
                eng.breadth = self.breadth.current(now_ms()) if self.breadth is not None and eng.mode == "live" else None
                of = {"up": bool(self.breadth and self.breadth.up), "breadth": eng.breadth} if self.order_flow_url else None
                if t0 >= next_analysis:
                    eng.analyze()
                    next_analysis = t0 + 1.0
                frames: dict[tuple, dict] = {}
                for c in list(self.clients):
                    b = eng.books.get(c.u)
                    if b is not None and b.minute_seq != c.minute_seq:
                        c.minute_seq = b.minute_seq
                        hist = eng.history(c.u)
                        c.last_event = max([e["id"] for e in hist["events"]] + [c.last_event])
                        await self._send(c, json.dumps(hist))
                    key = (c.u, c.e, c.sel)
                    if key not in frames:
                        frames[key] = eng.frame(c.u, c.e, c.sel)
                    events = eng.events_after(c.u, c.last_event)
                    if events:
                        c.last_event = events[-1]["id"]
                    cols = eng.tape_cols(c.u, c.last_col)
                    if cols:
                        c.last_col = cols[-1][0]
                    await self._send(c, json.dumps({**frames[key], "events": events, "tcols": cols, "data": data, "of": of},
                                                   separators=(",", ":")))
                eng.reset_flows()
            except Exception:
                log.exception("Frame loop error")
            await asyncio.sleep(max(0.0, self.period - (loop.time() - t0)))

    async def serve(self, host: str, port: int) -> None:
        async with serve(self.handler, host, port, process_request=self.process_request, compression=None,
                         max_size=2**16, ping_interval=20):
            log.info("Flow dashboard on http://%s:%d", host, port)
            await self.run_loop()
