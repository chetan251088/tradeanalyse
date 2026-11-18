from __future__ import annotations

import gzip
import json
import sqlite3
import threading
import time
from collections.abc import Iterator
from pathlib import Path

from scalper.clock import now_ms, to_dt
from scalper.models import Signal

# Compact on-disk tick keys -> the field names the live feed uses.
_FIELDS = {"l": "ltp", "o": "open", "h": "high", "lo": "low", "v": "volume", "oi": "oi", "b": "bid", "a": "ask"}


# ---- state shared between the scanner and the Telegram control process ----------
def state_dir(data_dir: str | Path) -> Path:
    return Path(data_dir) / "state"


def harvest_paused(data_dir: str | Path) -> bool:
    return (state_dir(data_dir) / "harvest.paused").exists()


def set_harvest_paused(data_dir: str | Path, paused: bool) -> None:
    flag = state_dir(data_dir) / "harvest.paused"
    if paused:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(to_dt(now_ms()).isoformat(timespec="seconds"), encoding="utf-8")
    else:
        flag.unlink(missing_ok=True)


def read_heartbeat(data_dir: str | Path) -> dict | None:
    try:
        return json.loads((state_dir(data_dir) / "scanner.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


class Heartbeat:
    """Writes {ts, phase} every 30 s so /status can tell a sleeping scanner from a dead one."""

    def __init__(self, data_dir: str | Path, every_sec: float = 30.0):
        self.path = state_dir(data_dir) / "scanner.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.phase, self.every = "starting", every_sec
        threading.Thread(target=self._run, name="heartbeat", daemon=True).start()

    def _run(self) -> None:
        while True:
            try:
                self.path.write_text(json.dumps({"ts": now_ms(), "phase": self.phase}), encoding="utf-8")
            except OSError:
                pass
            time.sleep(self.every)


class TickRecorder:
    """Append-only gzip JSON-lines file per day; replayable with :func:`read_recording`."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = gzip.open(self.path, "at", encoding="utf-8", compresslevel=5)
        self._n = 0

    def meta(self, meta: dict) -> None:
        self._write({"type": "meta", **meta})

    def add(self, underlying: str, instruments: list[dict]) -> None:
        self._write({"type": "add", "underlying": underlying, "instruments": instruments})

    def tick(self, ts: int, symbol: str, exchange: str, d: dict) -> None:
        rec: dict = {"t": ts, "s": symbol, "x": exchange}
        for short, name in _FIELDS.items():
            value = d.get(name)
            if value:
                rec[short] = value
        depth = d.get("depth")
        if depth:
            buy, sell = depth.get("buy") or [], depth.get("sell") or []
            if buy and buy[0].get("price"):
                rec["b"] = buy[0]["price"]
            if sell and sell[0].get("price"):
                rec["a"] = sell[0]["price"]
        self._write(rec)

    def _write(self, obj: dict) -> None:
        self._f.write(json.dumps(obj, separators=(",", ":")) + "\n")
        self._n += 1
        if self._n % 5000 == 0:
            self._f.flush()

    def close(self) -> None:
        self._f.close()


def read_recording(path: str | Path) -> Iterator[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def expand_tick(rec: dict) -> tuple[int, str, str, dict]:
    return rec["t"], rec["s"], rec["x"], {name: rec[short] for short, name in _FIELDS.items() if short in rec}


class Journal:
    """SQLite log of every alert and a once-a-minute regime snapshot per underlying (insert-only)."""

    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(str(path))
        self.con.executescript(
            """
            CREATE TABLE IF NOT EXISTS signals (
                ts INTEGER, day TEXT, time TEXT, underlying TEXT, strategy TEXT, side TEXT,
                score REAL, regime TEXT, legs TEXT, plan TEXT, features TEXT, title TEXT
            );
            CREATE TABLE IF NOT EXISTS minutes (
                ts INTEGER, day TEXT, time TEXT, underlying TEXT, regime TEXT, metrics TEXT
            );
            CREATE TABLE IF NOT EXISTS outcomes (
                ts INTEGER, strategy TEXT, symbol TEXT, entry REAL, stop REAL, target REAL, result TEXT,
                resolved_ts INTEGER, best_r REAL, worst_r REAL, last_r REAL, realized_r REAL
            );
            """
        )

    def outcomes(self, rows: list[tuple]) -> None:
        self.con.executemany("INSERT INTO outcomes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        self.con.commit()

    def signal(self, s: Signal) -> None:
        dt = to_dt(s.ts)
        self.con.execute(
            "INSERT INTO signals VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                s.ts, dt.date().isoformat(), dt.strftime("%H:%M:%S"), s.underlying, s.strategy, s.side,
                s.score, s.regime, json.dumps(s.legs), json.dumps(s.plan), json.dumps(s.features), s.title,
            ),
        )
        self.con.commit()

    def minute(self, ts: int, underlying: str, regime: str, metrics: dict) -> None:
        dt = to_dt(ts)
        self.con.execute(
            "INSERT INTO minutes VALUES (?,?,?,?,?,?)",
            (ts, dt.date().isoformat(), dt.strftime("%H:%M"), underlying, regime, json.dumps(metrics, default=str)),
        )
        self.con.commit()

    def close(self) -> None:
        self.con.close()
