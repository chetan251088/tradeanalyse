"""Telegram remote control for the scanner. Uses its own bot (TELEGRAM_BOT_TOKEN), never OpenAlgo's bot token:
two programs polling one token make Telegram reject both (HTTP 409).

  .venv/bin/python -m scalper.control --config config/vps.yaml --env-file env/dhan.env
  .venv/bin/python -m scalper.control --whoami --env-file env/dhan.env     (prints your chat id; send the bot a message first)

Only chats listed in TELEGRAM_CHAT_ID are answered. Commands take no free text, so nothing from Telegram reaches a shell.
"""

from __future__ import annotations

import argparse
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from scalper.clock import now_ms, to_dt
from scalper.config import Config, load_config, load_env_file
from scalper.notify import Notifier, TelegramBot
from scalper.store import harvest_paused, read_heartbeat, set_harvest_paused

log = logging.getLogger("scalper.control")

UNITS = {"scanner": "tradeanalyse-scanner", "openalgo": "openalgo"}

HELP = """Scanner control
/status  - scanner, broker login, harvest positions
/report  - harvest journal: positions, P&L, why it did / did not trigger
/check   - dry run of today's harvest entry with live quotes
/pause   - stop new harvest entries (open positions are still marked and closed)
/resume  - allow new harvest entries again
/logs [n] - last n lines of today's scanner log (default 20, max 60)
/login   - link to log in to OpenAlgo and connect Dhan
/restart scanner|openalgo - restart a service (Linux server only)"""


class Controller:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _rest(self):
        from openalgo import api

        from scalper.universe import Rest

        return Rest(api(api_key=self.cfg.api_key(), host=self.cfg.openalgo.host), self.cfg.api_key())

    def _journal(self) -> sqlite3.Connection:
        return sqlite3.connect(self.cfg.paths.journal, timeout=10)

    def handle(self, text: str) -> tuple[str, bool]:
        """Reply text and whether to send it as fixed-width."""
        parts = text.strip().split()
        cmd, args = parts[0].split("@")[0].lower(), parts[1:]
        fn = {"/status": self.status, "/report": self.report, "/check": self.check, "/pause": self.pause,
              "/resume": self.resume, "/logs": self.logs, "/login": self.login, "/restart": self.restart}.get(cmd)
        if fn is None:
            return HELP, False
        try:
            return fn(args)
        except Exception as e:
            log.exception("Command %s failed", cmd)
            return f"{cmd} failed: {e}", False

    def status(self, args: list[str]) -> tuple[str, bool]:
        lines = []
        hb = read_heartbeat(self.cfg.paths.data_dir)
        if hb is None:
            lines.append("Scanner: no heartbeat yet (not started?)")
        else:
            age = (now_ms() - int(hb["ts"])) / 1000
            lines.append(f"Scanner: {'ALIVE' if age < 120 else 'NOT RESPONDING'} ({hb['phase']}; beat {age:.0f}s ago)")
        try:
            rest = self._rest()
            ok, info = rest.ping()
            lines.append(f"Broker: {'logged in (' + info + ')' if ok else 'NOT logged in: ' + info}")
            if ok:
                lines.append(f"OpenAlgo mode: {rest.analyzer_mode()}")
        except Exception as e:
            lines.append(f"OpenAlgo: unreachable ({e})")
        lines.append(f"Harvest entries: {'PAUSED' if harvest_paused(self.cfg.paths.data_dir) else 'on'}")
        from scalper.harvest import SCHEMA

        con = self._journal()
        try:
            con.executescript(SCHEMA)
            eq = self.cfg.harvest.capital_rs + (con.execute(
                "SELECT coalesce(sum(pnl_rs), 0) FROM harvest_positions WHERE status='closed'").fetchone()[0])
            lines.append(f"Paper equity: Rs {eq:,.0f}")
            for pid, name, exp, lots, credit in con.execute(
                    "SELECT id, underlying, expiry, lots, credit FROM harvest_positions WHERE status='open'"):
                mark = con.execute("SELECT day, pnl_r, pnl_rs FROM harvest_marks WHERE position_id=? "
                                   "ORDER BY day DESC LIMIT 1", (pid,)).fetchone()
                lines.append(f"Open #{pid} {name} exp {exp}, {lots} lot(s), credit {credit:.1f}"
                             + (f", mark {mark[0]}: {mark[1]:+.2f}R Rs {mark[2]:+,.0f}" if mark else ""))
            today = to_dt(now_ms()).date().isoformat()
            for name, decision, reason in con.execute(
                    "SELECT underlying, decision, reason FROM harvest_checks WHERE day=? ORDER BY time", (today,)):
                lines.append(f"Today {name}: {decision} - {reason}")
        finally:
            con.close()
        return "\n".join(lines), False

    def report(self, args: list[str]) -> tuple[str, bool]:
        from scalper.harvest import SCHEMA, report

        con = self._journal()
        try:
            con.executescript(SCHEMA)
            text = report(con, self.cfg.harvest, last=10)
        finally:
            con.close()
        return (text if len(text) <= 3800 else "...\n" + text[-3800:]), True

    def check(self, args: list[str]) -> tuple[str, bool]:
        from scalper.harvest import Harvester

        con = self._journal()
        try:
            hv = Harvester(self.cfg, self._rest(), con, Notifier(bot=TelegramBot.from_env()))
            dt = to_dt(now_ms())
            out = [f"{name}: {decision} - {reason}" for name, decision, reason in hv.check_entries(dt, dry=True, force=True)]
            out += hv.mark_positions(dt, dry=True)
        finally:
            con.close()
        return "Dry run (plans arrive as separate messages):\n" + "\n".join(out), False

    def pause(self, args: list[str]) -> tuple[str, bool]:
        set_harvest_paused(self.cfg.paths.data_dir, True)
        return "Harvest entries PAUSED. Open positions are still marked and closed at expiry. /resume to undo.", False

    def resume(self, args: list[str]) -> tuple[str, bool]:
        set_harvest_paused(self.cfg.paths.data_dir, False)
        return "Harvest entries resumed.", False

    def logs(self, args: list[str]) -> tuple[str, bool]:
        n = min(max(int(args[0]) if args and args[0].isdigit() else 20, 1), 60)
        path = Path(self.cfg.paths.data_dir) / "logs" / f"scalper-{to_dt(now_ms()).date()}.log"
        if not path.exists():
            return f"No log yet for today ({path.name})", False
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-n:]
        return "\n".join(line[:200] for line in lines), True

    def login(self, args: list[str]) -> tuple[str, bool]:
        return (f"1. Open {self.cfg.openalgo.host} and sign in to OpenAlgo\n2. Connect Dhan (OTP / PIN)\n"
                "3. Send /status to confirm. The Dhan session ends at 03:00 IST every day."), False

    def restart(self, args: list[str]) -> tuple[str, bool]:
        unit = UNITS.get(args[0].lower()) if args else None
        if unit is None:
            return "Usage: /restart scanner  or  /restart openalgo", False
        if not sys.platform.startswith("linux") or shutil.which("systemctl") is None:
            return "Restart works only on the Linux server (systemd).", False
        res = subprocess.run(["sudo", "-n", "systemctl", "restart", unit], capture_output=True, text=True, timeout=90)
        if res.returncode:
            return f"Restart of {unit} failed: {(res.stderr or res.stdout).strip()[:300]}", False
        return f"{unit} restarted.", False


def whoami(bot_token: str) -> int:
    bot = TelegramBot(bot_token, [])
    chats = {(u["message"]["chat"]["id"], u["message"]["chat"].get("username") or u["message"]["chat"].get("title"))
             for u in bot.updates(0, timeout=0) if "message" in u}
    if not chats:
        print("No messages yet: send any message to the bot in Telegram, then run this again.")
    for chat_id, name in chats:
        print(f"chat id {chat_id}  ({name})  -> TELEGRAM_CHAT_ID={chat_id}")
    return 0


def serve(cfg: Config, bot: TelegramBot) -> None:
    ctl = Controller(cfg)
    pending = bot.updates(-1, timeout=0)
    offset = pending[-1]["update_id"] + 1 if pending else 0  # ignore commands sent while the bot was down
    bot.send("Scanner control online. /help for commands.")
    log.info("Listening for commands from chats %s", bot.chat_ids)
    while True:
        try:
            for u in bot.updates(offset):
                offset = u["update_id"] + 1
                msg = u.get("message") or {}
                chat, text = (msg.get("chat") or {}).get("id"), (msg.get("text") or "").strip()
                if chat not in bot.chat_ids:
                    log.warning("Ignored a message from chat %s (not in TELEGRAM_CHAT_ID)", chat)
                    continue
                if not text.startswith("/"):
                    continue
                log.info("Command from %s: %s", chat, text[:60])
                reply, pre = ctl.handle(text)
                bot.send(reply, chat_id=chat, pre=pre)
        except Exception as e:
            log.warning("Telegram polling error: %s", e)
            time.sleep(10)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="scalper.control", description=__doc__.split("\n")[0])
    p.add_argument("--config", default="config/dhan.yaml")
    p.add_argument("--env-file")
    p.add_argument("--whoami", action="store_true", help="print the chat ids that messaged the bot, then exit")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", stream=sys.stdout)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if args.env_file:
        load_env_file(args.env_file)
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        log.error("Set TELEGRAM_BOT_TOKEN (from @BotFather) in the env file")
        return 2
    if args.whoami:
        return whoami(token)
    bot = TelegramBot.from_env()
    if bot is None:
        log.error("Set TELEGRAM_CHAT_ID (run with --whoami to find it)")
        return 2
    serve(load_config(args.config), bot)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
