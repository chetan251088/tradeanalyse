"""Market breadth from the Order Flow board (order-flow-main), and starting that board with the live dashboard.

Order Flow reads every F&O underlying's spot and chain each minute (its `openalgo` provider). BreadthFeed polls its
market file once a minute and boils it down for MARKET READ: names up / flat / down on the day (the 0.5% floor
Order Flow draws the day with), the same for the NIFTY 50, and the strongest and weakest sector by median move.
Moves are spot vs previous close; the board itself ranks by futures, so its counts can differ by a name or two.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from statistics import median
from urllib.parse import urlparse

log = logging.getLogger("scalper.flow")

ROOT = Path(__file__).resolve().parents[2]
OF_DIR = ROOT / "order-flow-main"
FLOOR = 0.005
STALE_MS = 5 * 60_000


def load_universe(of_dir: Path = OF_DIR) -> tuple[dict[str, str], set[str]]:
    """symbol -> sector label for the F&O stocks, and the NIFTY 50 constituents."""
    data = json.loads((of_dir / "public" / "underlyings.json").read_text(encoding="utf-8"))
    labels = {k: v.get("label", k) for k, v in (data.get("themes", {}).get("sectors") or {}).items()}
    sectors = {u["symbol"]: labels.get(u["sector"], u["sector"]) for u in data["underlyings"]
               if u.get("kind") == "stock" and u.get("sector")}
    text = (of_dir / "src" / "features" / "market-flow" / "nifty50.ts").read_text(encoding="utf-8")
    block = re.search(r"NIFTY_50\s*=\s*\[(.*?)\]", text, re.S)
    n50 = set(re.findall(r"'([^']+)'", block.group(1))) if block else set()
    return sectors, n50


def _split(moves) -> list[int]:
    moves = list(moves)
    up = sum(m >= FLOOR for m in moves)
    down = sum(m <= -FLOOR for m in moves)
    return [up, len(moves) - up - down, down]


def summarize(market: dict, sectors: dict[str, str], n50: set[str]) -> dict | None:
    moves = {}
    for s in market.get("series") or []:
        sp, pc = s.get("sp"), s.get("pc")
        if sp and pc and pc > 0:
            moves[s["s"]] = sp / pc - 1
    stocks = {k: v for k, v in moves.items() if k in sectors}
    if len(stocks) < 20:
        return None
    groups: dict[str, list[float]] = {}
    for k, v in stocks.items():
        groups.setdefault(sectors[k], []).append(v)
    med = sorted(((median(v), name) for name, v in groups.items() if len(v) >= 3), reverse=True)
    return {"at": int(market.get("t") or 0), "date": market.get("date"), "n": len(stocks),
            "fno": _split(stocks.values()), "n50": _split(v for k, v in stocks.items() if k in n50),
            "lead": [med[0][1], round(med[0][0] * 100, 2)] if med else None,
            "lag": [med[-1][1], round(med[-1][0] * 100, 2)] if med else None,
            "nifty": round(moves["NIFTY"] * 100, 2) if "NIFTY" in moves else None}


class BreadthFeed:
    """Polls Order Flow's /api/market about 20 s past each minute (it builds at +3 s and reads for ~15 s)."""

    def __init__(self, url: str, of_dir: Path = OF_DIR):
        self.url = url.rstrip("/")
        self.latest: dict | None = None
        self.up = False
        try:
            self.sectors, self.n50 = load_universe(of_dir)
        except (OSError, ValueError) as e:
            log.warning("Breadth off: cannot read Order Flow's universe (%s)", e)
            self.sectors, self.n50 = {}, set()
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.thread = threading.Thread(target=self._run, name="breadth", daemon=True)

    def start(self) -> None:
        if self.sectors:
            self.thread.start()

    def current(self, now_ms: int) -> dict | None:
        b = self.latest
        return b if b and now_ms - b["at"] <= STALE_MS else None

    def poll(self) -> None:
        try:
            with self.opener.open(f"{self.url}/api/market", timeout=20) as r:
                market = json.loads(r.read())
            self.up = True
        except (OSError, ValueError):
            self.up = False
            return
        s = summarize(market, self.sectors, self.n50)
        if s:
            self.latest = s

    def _run(self) -> None:
        while True:
            self.poll()
            time.sleep(60 - (time.time() % 60) + 20)


def port_open(url: str) -> bool:
    u = urlparse(url)
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((u.hostname or "127.0.0.1", u.port or 80)) == 0


class OrderFlowServer:
    """Runs order-flow-main's server (node server/index.mjs) beside the live dashboard, unless one is already up."""

    def __init__(self, url: str, of_dir: Path = OF_DIR):
        self.url, self.of_dir, self.proc = url, of_dir, None

    def start(self) -> str:
        if port_open(self.url):
            return f"Order Flow already running on {self.url}"
        node = shutil.which("node")
        if not node or not (self.of_dir / "server" / "index.mjs").exists():
            return "Order Flow not started: node or order-flow-main not found"
        logs = ROOT / "data" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        out = (logs / f"order-flow-{time.strftime('%Y-%m-%d')}.log").open("a", encoding="utf-8")
        flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        self.proc = subprocess.Popen([node, "--no-warnings", "server/index.mjs"], cwd=self.of_dir, stdout=out,
                                     stderr=subprocess.STDOUT, creationflags=flags)
        return f"Order Flow started on {self.url} (log data/logs/{Path(out.name).name})"

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
