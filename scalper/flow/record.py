"""Records live order-book packets so the book & tape readings can be tested later (no historical depth exists).

One gzip JSON-lines file per dashboard run under data/flow/depth/ (<day>_<HHMMSS>.jsonl.gz, so a restart never appends
to a file a crash left truncated). Each line is either a meta record ({"meta": {...}}) or a packet: t (ms), s (symbol),
x (exchange), p (ltp), v (cumulative volume), oi, l (depth levels), b / a ([price, qty, orders] per level). Futures
are kept packet by packet; options and indices at most once a second each.
"""

from __future__ import annotations

import gzip
import json
from collections.abc import Iterator
from pathlib import Path


def _lv(xs) -> list:
    return [[x.get("price"), x.get("quantity"), x.get("orders")] for x in xs or []]


class DepthRecorder:
    def __init__(self, path: str | Path, flush_sec: float = 30.0):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.f = gzip.open(self.path, "wt", compresslevel=5, encoding="utf-8")
        self.want: dict[tuple[str, str], int] = {}  # (symbol, exchange) -> min ms between records (0 = every packet)
        self.last: dict[tuple[str, str], int] = {}
        self.flush_ms, self.flushed, self.lines = int(flush_sec * 1000), 0, 0

    def meta(self, info: dict) -> None:
        self.f.write(json.dumps({"meta": info}, separators=(",", ":")) + "\n")

    def add(self, ts: int, symbol: str, exchange: str, d: dict) -> None:
        key = (symbol, exchange)
        gap = self.want.get(key)
        if gap is None:
            return
        slot = (symbol, exchange, d.get("depth_level") or 5)
        if gap and ts // gap == self.last.get(slot, -1) // gap:
            return
        self.last[slot] = ts
        dep = d.get("depth") or {}
        rec = {"t": ts, "s": symbol, "x": exchange, "p": d.get("ltp"), "v": d.get("volume"), "oi": d.get("oi"),
               "l": d.get("depth_level") or (5 if dep else None), "b": _lv(dep.get("buy")), "a": _lv(dep.get("sell"))}
        self.f.write(json.dumps(rec, separators=(",", ":")) + "\n")
        self.lines += 1
        if ts - self.flushed >= self.flush_ms:
            self.f.flush()
            self.flushed = ts

    def close(self) -> None:
        self.f.close()


def read_depth(path: str | Path) -> Iterator[dict]:
    with gzip.open(path, "rt", encoding="utf-8") as f:
        try:
            for line in f:
                yield json.loads(line)
        except (EOFError, gzip.BadGzipFile, json.JSONDecodeError):
            return  # the file of a run that crashed ends mid-record
