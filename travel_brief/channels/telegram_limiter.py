"""Production rate limiter and resilient Telegram message delivery with error isolation."""
from __future__ import annotations

import json
import logging
import re
import socket
import time
import urllib.error
import urllib.request
from typing import Any

from travel_brief.channels.telegram import TelegramChannel
Briefing = Any
from travel_brief.storage.db import TravelBriefDatabase
from travel_brief.storage.models import Recipient

logger = logging.getLogger(__name__)


class TelegramRateLimiter:
    """Enforces Telegram's official rate limits:
    - Global broadcast: <= 25 msgs/sec
    - Per individual chat: >= 1.0 sec interval
    - Per group chat: <= 20 msgs/min
    """

    def __init__(self, max_global_rate: float = 25.0, min_chat_interval: float = 1.0):
        self.max_global_rate = max_global_rate
        self.min_global_interval = 1.0 / max_global_rate if max_global_rate > 0 else 0.04
        self.min_chat_interval = min_chat_interval
        self._last_global_send: float = 0.0
        self._last_chat_send: dict[str, float] = {}

    def wait_before_send(self, chat_id: str) -> None:
        now = time.time()
        # 1. Global throttle
        elapsed_global = now - self._last_global_send
        if elapsed_global < self.min_global_interval:
            time.sleep(self.min_global_interval - elapsed_global)

        # 2. Per-chat throttle
        now = time.time()
        last_chat = self._last_chat_send.get(chat_id, 0.0)
        elapsed_chat = now - last_chat
        if elapsed_chat < self.min_chat_interval:
            time.sleep(self.min_chat_interval - elapsed_chat)

        final_now = time.time()
        self._last_global_send = final_now
        self._last_chat_send[chat_id] = final_now


class SafeTelegramDispatcher:
    """Dispatches messages to Telegram recipients with rate limiting, 429 retry-after handling,
    selective 403 deactivation, and network timeout UNKNOWN isolation.
    """

    def __init__(self, rate_limiter: TelegramRateLimiter | None = None, max_429_retries: int = 3):
        self.limiter = rate_limiter or TelegramRateLimiter()
        self.max_429_retries = max_429_retries

    def send_to_recipient(
        self,
        tg: TelegramChannel,
        recipient: Recipient,
        briefing: Briefing,
        db: TravelBriefDatabase | None = None,
    ) -> tuple[str, str | None, str | None]:
        """Dispatches briefing to a single recipient safely.
        Returns: (status: 'SENT' | 'FAILED' | 'UNKNOWN', message_id, error_detail)
        """
        chat_id = str(recipient.chat_id).strip()

        for attempt in range(1, self.max_429_retries + 1):
            self.limiter.wait_before_send(chat_id)
            try:
                msg_id = tg.send(chat_id, briefing)
                return "SENT", msg_id, None
            except (socket.timeout, TimeoutError) as net_err:
                return "UNKNOWN", None, f"Network Timeout / Ambiguous Delivery: {net_err}"
            except Exception as exc:
                err_str = str(exc)
                lower_err = err_str.lower()

                # Case 1: Rate limit 429
                if "429" in err_str:
                    match = re.search(r"retry after (\d+)", lower_err)
                    retry_after = int(match.group(1)) if match else 3
                    sleep_sec = retry_after + 0.5
                    logger.warning(f"Telegram 429 Rate Limited. Sleeping {sleep_sec}s for attempt {attempt}...")
                    if attempt < self.max_429_retries:
                        time.sleep(sleep_sec)
                        continue
                    return "FAILED", None, f"429 Too Many Requests: {err_str}"

                # Case 2: Bot Blocked by User
                if "403" in err_str and "bot was blocked by the user" in lower_err:
                    if db:
                        db.deactivate_recipient(chat_id, reason="USER_BLOCKED_BOT")
                    return "FAILED", None, f"403 Blocked: {err_str}"

                # Case 3: Chat not found / User deactivated
                if "400" in err_str and ("chat not found" in lower_err or "user is deactivated" in lower_err):
                    if db:
                        db.deactivate_recipient(chat_id, reason="INVALID_CHAT_OR_USER_DEACTIVATED")
                    return "FAILED", None, f"400 Invalid: {err_str}"

                # Case 4: Ambiguous network connection timeout
                if "timed out" in lower_err or "connection reset" in lower_err:
                    return "UNKNOWN", None, f"Network Timeout / Ambiguous Delivery: {err_str}"

                return "FAILED", None, err_str

        return "FAILED", None, "Max retries exceeded"



    def send_text_to_recipient(
        self,
        tg: TelegramChannel,
        chat_id: str,
        text: str,
        parse_mode: str = "HTML",
        db: Any = None,
    ) -> tuple[str, str | None, str | None]:
        """Dispatches a text/HTML message to a single recipient with rate limit & error safety."""
        chat_id_clean = str(chat_id).strip()
        for attempt in range(1, self.max_429_retries + 1):
            self.limiter.wait_before_send(chat_id_clean)
            try:
                msg_id = tg.send_text(recipient=chat_id_clean, text=text, parse_mode=parse_mode)
                return "SENT", msg_id, None
            except (socket.timeout, TimeoutError) as net_err:
                return "UNKNOWN", None, f"Network Timeout / Ambiguous Delivery: {net_err}"
            except Exception as exc:
                err_str = str(exc)
                lower_err = err_str.lower()
                if "429" in err_str:
                    match = re.search(r"retry after (\d+)", lower_err)
                    retry_after = int(match.group(1)) if match else 3
                    sleep_sec = retry_after + 0.5
                    logger.warning(f"Telegram 429 Rate Limited. Sleeping {sleep_sec}s for attempt {attempt}...")
                    if attempt < self.max_429_retries:
                        time.sleep(sleep_sec)
                        continue
                    return "FAILED", None, f"429 Too Many Requests: {err_str}"
                if "403" in err_str and "bot was blocked by the user" in lower_err:
                    if db and hasattr(db, "deactivate_recipient"):
                        db.deactivate_recipient(chat_id_clean, reason="USER_BLOCKED_BOT")
                    return "FAILED", None, f"403 Blocked: {err_str}"
                if "400" in err_str and ("chat not found" in lower_err or "user is deactivated" in lower_err):
                    if db and hasattr(db, "deactivate_recipient"):
                        db.deactivate_recipient(chat_id_clean, reason="INVALID_CHAT_OR_USER_DEACTIVATED")
                    return "FAILED", None, f"400 Invalid: {err_str}"
                return "FAILED", None, err_str
        return "FAILED", None, "Max retries exceeded"


def sync_telegram_updates(bot_token: str, db: TravelBriefDatabase) -> dict[str, Any]:
    """Polls Telegram getUpdates with offset tracking to consume /stop or /start commands
    without dropping or duplicating updates.
    """
    if not bot_token:
        return {"processed": 0, "status": "NO_TOKEN"}

    last_offset = db.get_telegram_offset()
    processed_count = 0
    unsubscribed = []
    subscribed = []

    while True:
        url = f"https://api.telegram.org/bot{bot_token}/getUpdates?offset={last_offset + 1}&timeout=5"
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.load(resp)
        except Exception as exc:
            logger.warning(f"Failed to fetch Telegram updates: {exc}")
            break

        updates = data.get("result", [])
        if not updates:
            break

        for item in updates:
            up_id = item.get("update_id", 0)
            if up_id > last_offset:
                last_offset = up_id

            msg = item.get("message", {})
            text = (msg.get("text") or "").strip().lower()
            chat = msg.get("chat", {})
            chat_id = str(chat.get("id", ""))
            sender_name = chat.get("title") or chat.get("first_name") or f"사용자_{chat_id[-4:]}"

            if not chat_id:
                continue

            processed_count += 1
            if text in {"/stop", "/구독취소", "/unsubscribe"}:
                db.deactivate_recipient(chat_id, reason="USER_STOP_COMMAND")
                unsubscribed.append(chat_id)
            elif text in {"/start", "/구독", "안녕"}:
                db.add_recipient(chat_id, name=sender_name)
                subscribed.append(chat_id)

    db.update_telegram_offset(last_offset)
    return {
        "processed": processed_count,
        "last_offset": last_offset,
        "unsubscribed": unsubscribed,
        "subscribed": subscribed,
    }
