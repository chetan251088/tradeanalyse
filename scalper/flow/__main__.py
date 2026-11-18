"""Options flow dashboard ("JARVIS") for NIFTY / SENSEX.

  .venv\\Scripts\\python.exe -m scalper.flow                                  # replay the latest stored day
  .venv\\Scripts\\python.exe -m scalper.flow --replay 2026-10-29 --speed 30
  .venv\\Scripts\\python.exe -m scalper.flow --live --env-file env\\dhan.env   # live (OpenAlgo running, Dhan connected)

Then open http://127.0.0.1:8787 in a browser.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import socket
import sys
import webbrowser

from scalper.config import load_config, load_env_file
from scalper.flow.breadth import BreadthFeed, OrderFlowServer
from scalper.flow.engine import FlowEngine
from scalper.flow.server import FlowServer
from scalper.flow.sources import LiveSource, ReplaySource, parse_day, replay_days
from scalper.flow.updater import DataUpdater


def _port_free(host: str, port: int) -> bool:
    with socket.socket() as s:
        try:
            s.bind((host, port))
            return True
        except OSError:
            return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="scalper.flow", description=__doc__.split("\n")[0])
    p.add_argument("--config", default="config/dhan.yaml")
    p.add_argument("--env-file")
    p.add_argument("--live", action="store_true", help="stream live data through OpenAlgo")
    p.add_argument("--replay", metavar="YYYY-MM-DD", help="replay a stored session (default: the latest)")
    p.add_argument("--speed", type=float, default=30.0, help="replay speed, x real time")
    p.add_argument("--start", default="09:15", help="replay start time (IST)")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--open", action="store_true", help="open the dashboard in the default browser")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", stream=sys.stdout)
    for noisy in ("httpx", "websockets", "websocket"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    os.environ["NO_PROXY"] = ",".join(filter(None, ["127.0.0.1", "localhost", os.environ.get("NO_PROXY", "")]))
    if args.env_file:
        load_env_file(args.env_file)
    cfg = load_config(args.config)
    host, port = args.host or cfg.flow.host, args.port or cfg.flow.port
    if not _port_free(host, port):
        logging.getLogger("scalper.flow").error(
            "Port %d is already in use, probably by another dashboard still running. Close that window "
            "(Ctrl+C) or start this one with --port %d.", port, port + 1)
        return 1

    days = replay_days("NIFTY", 30)
    of_url = cfg.flow.order_flow_url
    of_server, breadth = None, None
    if of_url and cfg.flow.breadth:
        breadth = BreadthFeed(of_url)
        breadth.start()
    if args.live:
        if of_url and cfg.flow.order_flow_autostart:
            of_server = OrderFlowServer(of_url)
            logging.getLogger("scalper.flow").info(of_server.start())
        engine = FlowEngine(cfg.flow, "live")
        source = LiveSource(cfg, engine)
        source.start()
        rebuild = None
    else:
        def rebuild(day: str):
            eng = FlowEngine(cfg.flow, "replay")
            return eng, ReplaySource(cfg, eng, parse_day(day), args.speed, args.start)

        engine, source = rebuild(args.replay or days[-1])

    server = FlowServer(engine, source, cfg.flow.frame_hz, rebuild, days,
                        DataUpdater(args.config, args.env_file, cfg.notify.telegram_username), breadth, of_url)
    if args.open:
        webbrowser.open(f"http://{host}:{port}")
    try:
        asyncio.run(server.serve(host, port))
    except KeyboardInterrupt:
        pass
    finally:
        server.source.stop()
        if of_server is not None:
            of_server.stop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
