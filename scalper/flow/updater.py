"""The dashboard's DATA button: tops up everything the research, scanner and harvester read, in child processes.

  1. index & VIX   scalper.index_update (OpenAlgo history; complete sessions only)
  2. option chains tools/dhan_download.py with OpenAlgo's interpreter (also days.csv / expiry calendar). Skipped
                   09:00-15:35 IST on weekdays: it shares Dhan's request limit with live trading.
  3. Order Flow    order-flow-main's `npm run fetch:nse` (NSE/BSE end-of-day F&O files: the previous day's OI
                   its board measures from). Optional: a failure here is reported but does not fail the update.
Output goes to data/logs/update-<day>.log; progress and the result are posted to the JARVIS feed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import date, timedelta
from pathlib import Path

from scalper.clock import at, now_ms, to_dt
from scalper.index_update import last_stored_day

ROOT = Path(__file__).resolve().parents[2]
OPENALGO_PY = ROOT / "openalgo-main" / ".venv" / "Scripts" / "python.exe"
if not OPENALGO_PY.exists():
    OPENALGO_PY = ROOT / "openalgo-main" / ".venv" / "bin" / "python"


def last_session(now: int) -> date:
    """The latest weekday session that has closed (exchange holidays are not known here)."""
    d = to_dt(now).date()
    if d.weekday() >= 5 or now < at(d, "15:31"):
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def market_hours(now: int) -> bool:
    d = to_dt(now).date()
    return d.weekday() < 5 and at(d, "09:00") <= now < at(d, "15:35")


def freshness(root: Path | None = None) -> dict[str, str | None]:
    root = root or ROOT
    out: dict[str, str | None] = {}
    day = last_stored_day("NIFTY", root / "data" / "market" / "index")
    out["index"] = day.isoformat() if day else None
    for name in ("NIFTY", "SENSEX"):
        files = sorted((root / "data" / "market" / "options" / name / "chain").glob("*.parquet"))
        out[name] = files[-1].stem if files else None
    return out


def archive_day(root: Path | None = None) -> str | None:
    files = sorted(((root or ROOT) / "order-flow-main" / "data" / "archive").glob("????-??-??.json.gz"))
    return files[-1].name[:10] if files else None


class DataUpdater:
    def __init__(self, config: str, env_file: str | None, user: str):
        self.config, self.env_file, self.user = config, env_file, user
        self.state, self.step, self.result = "idle", "", ""
        self.complete = False
        self.lines: deque = deque(maxlen=4)
        self.started = self.ended = 0
        self.notes: deque = deque()
        self.fresh = freshness()
        self.thread: threading.Thread | None = None

    def start(self) -> bool:
        if self.state == "running":
            return False
        self.state, self.started, self.ended, self.result, self.complete = "running", now_ms(), 0, "", False
        self.lines.clear()
        self.thread = threading.Thread(target=self._run, name="data-update", daemon=True)
        self.thread.start()
        return True

    def drain(self) -> list[tuple[str, str]]:
        out = []
        while self.notes:
            out.append(self.notes.popleft())
        return out

    def view(self) -> dict:
        due = last_session(now_ms())
        # after a full run past the close nothing more can be fetched (e.g. an exchange holiday), so it counts as current
        done_after_close = self.complete and self.state == "done" and self.ended >= at(due, "15:31")
        stale = not done_after_close and any((v or "") < due.isoformat() for v in self.fresh.values())
        return {"state": self.state, "step": self.step, "line": self.lines[-1] if self.lines else "",
                "result": self.result, "fresh": self.fresh, "due": due.isoformat(), "stale": bool(stale),
                "market": market_hours(now_ms()), "archive": archive_day()}

    def _note(self, text: str, sev: str = "med") -> None:
        self.notes.append((sev, text))

    def _run(self) -> None:
        env_args = ["--env-file", self.env_file] if self.env_file else []
        steps = [("index & VIX", [sys.executable, "-m", "scalper.index_update", "--config", self.config, *env_args,
                                  "--days", "30"], ROOT, True)]
        skipped = ""
        if market_hours(now_ms()):
            skipped = " Option chains skipped: market open (they share Dhan's request limit); click again after 15:35 IST."
        elif not self.user:
            skipped = " Option chains skipped: set notify.telegram_username (your OpenAlgo login) in the config."
        elif not OPENALGO_PY.exists():
            skipped = f" Option chains skipped: OpenAlgo's interpreter not found at {OPENALGO_PY}."
        else:
            steps.append(("option chains", [str(OPENALGO_PY), str(ROOT / "tools" / "dhan_download.py"), "--user", self.user,
                                            "--years", "5", "--chains", "all"], ROOT, True))
        of_dir, node = ROOT / "order-flow-main", shutil.which("node")
        if node and (of_dir / "scripts" / "fetch-nse-archive.mjs").exists():
            steps.append(("Order Flow archive", [node, "--no-warnings", "scripts/fetch-nse-archive.mjs"], of_dir, False))
        self._note("Data update started: " + " → ".join(s[0] for s in steps) + "." + skipped, "low")
        log = ROOT / "data" / "logs" / f"update-{to_dt(now_ms()).date().isoformat()}.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        soft = ""
        for name, cmd, cwd, required in steps:
            self.step = name
            try:
                rc = self._exec(cmd, log, cwd)
            except OSError as e:
                rc, self.lines = -1, deque([str(e)], maxlen=4)
            if rc != 0 and not required:
                soft = f" · {name} failed: {(self.lines[-1] if self.lines else '')[:100]}"
                continue
            if rc != 0:
                self.state, self.ended = "failed", now_ms()
                last = self.lines[-1] if self.lines else ""
                self.result = f"{name} failed (exit {rc}): {last}"
                self._note(f"Data update FAILED at {name} (exit {rc}): {last[:160]} · details in {log.name}. "
                           "Is OpenAlgo running and Dhan connected?", "hi")
                self.fresh = freshness()
                return
        self.fresh = freshness()
        f = self.fresh
        self.result = (f"index & VIX to {f['index']}, chains NIFTY {f['NIFTY']} · SENSEX {f['SENSEX']}, "
                       f"Order Flow archive {archive_day() or '—'} ({time.monotonic() - t0:.0f}s)"
                       + (" · chains skipped" if skipped else "") + soft)
        self.state, self.step, self.ended = "done", "", now_ms()
        self.complete = not skipped
        self._note(f"Data updated: {self.result}.", "med")

    def _exec(self, cmd: list[str], log: Path, cwd: Path = ROOT) -> int:
        env = {**os.environ, "PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1",
               "NO_PROXY": ",".join(filter(None, ["127.0.0.1", "localhost", os.environ.get("NO_PROXY", "")]))}
        if os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy"):
            env["NODE_USE_ENV_PROXY"] = "1"  # Node's fetch ignores HTTPS_PROXY unless asked
        with log.open("a", encoding="utf-8") as lf:
            lf.write(f"\n=== {to_dt(now_ms()):%Y-%m-%d %H:%M:%S} {' '.join(cmd)}\n")
            p = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                 encoding="utf-8", errors="replace")
            for line in p.stdout:
                lf.write(line)
                if line.strip():
                    self.lines.append(line.strip()[-200:])
            return p.wait()
