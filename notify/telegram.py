"""Send messages to a Telegram chat through the Bot API.

The bot token is a secret: it comes from the environment (TELEGRAM_BOT_TOKEN)
and never appears in a log line — a failed send reports the error type only,
since an httpx error message can carry the request URL, token included.
"""

from __future__ import annotations

import html
import sys
from collections.abc import Mapping
from typing import Any

import httpx

API_URL = "https://api.telegram.org"
# Telegram refuses messages over 4096 characters; keep a margin.
MAX_MESSAGE_CHARS = 4000

LEVEL_ICON = {"info": "ℹ️", "alert": "🔔", "error": "🚨"}


def split_message(text: str, limit: int = MAX_MESSAGE_CHARS) -> list[str]:
    """`text` cut into chunks of at most `limit` characters, on line boundaries
    where possible (an HTML tag never spans two lines in our messages)."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        while len(line) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) > limit:
            chunks.append(current)
            current = line
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str, http: httpx.Client | None = None) -> None:
        if not token or not chat_id:
            raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are both required")
        self._url = f"{API_URL}/bot{token}/sendMessage"
        self._chat_id = chat_id
        self._http = http or httpx.Client(timeout=15.0)

    def send(self, text: str) -> bool:
        """Send `text` (Telegram HTML); True when every chunk was accepted. Never raises."""
        ok = True
        for chunk in split_message(text):
            try:
                resp = self._http.post(
                    self._url,
                    json={
                        "chat_id": self._chat_id,
                        "text": chunk,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                )
            except httpx.HTTPError as exc:
                print(f"telegram: send failed ({type(exc).__name__})", file=sys.stderr)
                ok = False
                continue
            if resp.status_code != 200:
                print(f"telegram: send refused (HTTP {resp.status_code})", file=sys.stderr)
                ok = False
        return ok


def format_alert(event: Mapping[str, Any], mode: str) -> str:
    """One bot event as an instant Telegram message."""
    icon = LEVEL_ICON.get(event.get("level", "info"), "ℹ️")
    return f"{icon} <b>[{html.escape(mode.upper())}]</b> {html.escape(event['message'])}"
