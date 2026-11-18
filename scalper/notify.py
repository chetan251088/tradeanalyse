from __future__ import annotations

import html
import logging
import os
import queue
import threading
import time

log = logging.getLogger("scalper.alert")


class TelegramBot:
    """The scanner's own Telegram bot (alerts + commands). Keep OpenAlgo's bot on a different token."""

    def __init__(self, token: str, chat_ids: list[int]):
        import httpx

        self.token, self.chat_ids = token, chat_ids
        self._http = httpx.Client(timeout=70)

    @classmethod
    def from_env(cls) -> TelegramBot | None:
        token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        chats = [int(x) for x in os.environ.get("TELEGRAM_CHAT_ID", "").replace(" ", "").split(",") if x]
        return cls(token, chats) if token and chats else None

    def call(self, method: str, **params) -> dict:
        return self._http.post(f"https://api.telegram.org/bot{self.token}/{method}", json=params).json()

    def send(self, text: str, chat_id: int | None = None, pre: bool = False) -> bool:
        body = {"text": f"<pre>{html.escape(text[:3900])}</pre>", "parse_mode": "HTML"} if pre else {"text": text[:4000]}
        ok = True
        for chat in [chat_id] if chat_id is not None else self.chat_ids:
            res = self.call("sendMessage", chat_id=chat, disable_web_page_preview=True, **body)
            if not res.get("ok"):
                ok = False
                log.warning("Telegram send failed: %s", res.get("description"))
        return ok

    def updates(self, offset: int, timeout: int = 50) -> list[dict]:
        res = self.call("getUpdates", offset=offset, timeout=timeout, allowed_updates=["message"])
        return res.get("result", []) if res.get("ok") else []


class Notifier:
    """Logs every alert; forwards to Telegram through the scanner's own bot, else through OpenAlgo's bot."""

    def __init__(self, client=None, username: str = "", min_interval_sec: float = 2.0, bot: TelegramBot | None = None):
        self.client = client
        self.username = username
        self.bot = bot
        self.min_interval = min_interval_sec
        self._q: queue.Queue | None = None
        if bot is not None or (client is not None and username):
            self._q = queue.Queue(maxsize=200)
            threading.Thread(target=self._worker, name="telegram-notify", daemon=True).start()

    def send(self, text: str, priority: int = 5) -> None:
        log.info("\n%s", text)
        if self._q is None:
            return
        try:
            self._q.put_nowait((text, priority))
        except queue.Full:
            log.warning("Telegram queue full, dropping message")

    def _worker(self) -> None:
        while True:
            text, priority = self._q.get()
            try:
                if self.bot is not None:
                    self.bot.send(text)
                else:
                    res = self.client.telegram(username=self.username, message=text[:4000], priority=priority)
                    if not isinstance(res, dict) or res.get("status") != "success":
                        log.warning("Telegram send failed: %s", res)
            except Exception as e:  # network errors must never reach the decision loop
                log.warning("Telegram send error: %s", e)
            time.sleep(self.min_interval)
