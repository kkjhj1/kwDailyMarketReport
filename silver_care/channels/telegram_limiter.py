"""[TG-05] Rate-limited dispatcher for SilverCare weekly briefings.
- Token bucket: global 25 msg/s, 1.0s per chat
- 429: honours retry_after, otherwise exponential backoff
- Network timeout: delivery is ambiguous → logged as UNKNOWN and never retried (no duplicates)
- Already SENT/UNKNOWN for the same briefing_id → skipped
"""
from __future__ import annotations
import logging
import re
import socket
import time
from typing import Any, Callable, Optional

from silver_care.storage.db import SilverCareDatabase

logger = logging.getLogger(__name__)


class TelegramRateLimiter:
    def __init__(self, max_global_rate: float = 25.0, min_chat_interval: float = 1.0, sleep: Callable[[float], None] = time.sleep):
        self.min_global_interval = 1.0 / max_global_rate if max_global_rate > 0 else 0.0
        self.min_chat_interval = min_chat_interval
        self._sleep = sleep
        self._last_global_send = 0.0
        self._last_chat_send: dict[str, float] = {}

    def wait_before_send(self, chat_id: str) -> None:
        wait = self.min_global_interval - (time.monotonic() - self._last_global_send)
        if wait > 0:
            self._sleep(wait)
        last_chat = self._last_chat_send.get(chat_id)
        if last_chat is not None:
            wait = self.min_chat_interval - (time.monotonic() - last_chat)
            if wait > 0:
                self._sleep(wait)
        now = time.monotonic()
        self._last_global_send = now
        self._last_chat_send[chat_id] = now


class SafeTelegramDispatcher:
    def __init__(
        self,
        rate_limiter: Optional[TelegramRateLimiter] = None,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.limiter = rate_limiter or TelegramRateLimiter()
        self.max_retries = max_retries
        self._sleep = sleep

    def send_one(self, channel: Any, chat_id: str, text: str) -> tuple[str, Optional[str]]:
        """Returns (status: SENT | FAILED | UNKNOWN, error_detail)."""
        for attempt in range(1, self.max_retries + 1):
            self.limiter.wait_before_send(chat_id)
            try:
                channel.send_message(chat_id, text)
                return "SENT", None
            except (socket.timeout, TimeoutError) as exc:
                return "UNKNOWN", f"timeout: {exc}"
            except Exception as exc:
                err = str(exc)
                lower = err.lower()
                if "timed out" in lower or "connection reset" in lower:
                    return "UNKNOWN", err
                if "429" in err:
                    match = re.search(r"retry after (\d+)", lower)
                    delay = float(match.group(1)) + 0.5 if match else float(2 ** attempt)
                    if attempt < self.max_retries:
                        logger.warning("[TG-05] 429 for %s, sleeping %.1fs (attempt %d)", chat_id, delay, attempt)
                        self._sleep(delay)
                        continue
                return "FAILED", err
        return "FAILED", "max retries exceeded"

    def dispatch(
        self,
        channel: Any,
        db: SilverCareDatabase,
        briefing_id: str,
        messages: list[tuple[str, int, str]],
    ) -> dict[str, Any]:
        """messages: [(chat_id, parent_id, html_text)]. One chat may get one message per parent,
        so the dedupe log is keyed by 'chat_id#parent_id'."""
        stats: dict[str, Any] = {"total": len(messages), "sent": 0, "skipped": 0, "failed": 0, "unknown": 0, "errors": {}}
        for chat_id, parent_id, text in messages:
            chat_id = str(chat_id).strip()
            log_key = f"{chat_id}#{parent_id}"
            if db.has_dispatched(briefing_id, log_key):
                stats["skipped"] += 1
                continue
            status, err = self.send_one(channel, chat_id, text)
            db.log_dispatch(briefing_id, log_key, status)
            stats[status.lower()] += 1
            if err:
                stats["errors"][log_key] = err
        return stats
