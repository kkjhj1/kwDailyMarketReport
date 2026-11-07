"""[TG-05] Rate-limited dispatcher for YOLO Bot weekly & weather briefings.
- Token bucket: global 25 msg/s, 1.0s per chat
- 429: honours retry_after, otherwise exponential backoff
- Network timeout: logged as UNKNOWN and never retried (prevents duplicate sends)
"""
from __future__ import annotations

import hashlib
import logging
import re
import socket
import time
from typing import Any, Callable, Optional

from yolo_brief.storage.db import YoloDatabase

logger = logging.getLogger(__name__)


class TelegramRateLimiter:
    """Token-bucket style rate limiter enforcing global 25 msg/s and 1.0s per chat."""

    def __init__(
        self,
        max_global_rate: float = 25.0,
        min_chat_interval: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
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
            wait_chat = self.min_chat_interval - (time.monotonic() - last_chat)
            if wait_chat > 0:
                self._sleep(wait_chat)
        now = time.monotonic()
        self._last_global_send = now
        self._last_chat_send[chat_id] = now


class SafeTelegramDispatcher:
    """[TG-05] Safe dispatcher with rate limiting, 429 backoff, and DB deduplication."""

    def __init__(
        self,
        rate_limiter: Optional[TelegramRateLimiter] = None,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.limiter = rate_limiter or TelegramRateLimiter(sleep=sleep)
        self.max_retries = max_retries
        self._sleep = sleep

    def send_one(
        self,
        channel: Any,
        chat_id: str,
        text: str,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> tuple[str, Optional[str]]:
        """Returns (status: SENT | FAILED | UNKNOWN, error_detail)."""
        for attempt in range(1, self.max_retries + 1):
            self.limiter.wait_before_send(chat_id)
            try:
                channel.send_message(chat_id, text, reply_markup=reply_markup)
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
                    delay = float(match.group(1)) + 0.5 if match else float(2**attempt)
                    if attempt < self.max_retries:
                        logger.warning(
                            "[TG-05] 429 rate limit for %s, sleeping %.1fs (attempt %d)",
                            chat_id,
                            delay,
                            attempt,
                        )
                        self._sleep(delay)
                        continue
                return "FAILED", err
        return "FAILED", "max retries exceeded"

    def dispatch_batch(
        self,
        channel: Any,
        db: YoloDatabase,
        week_key: str,
        dispatch_type: str,
        payloads: list[tuple[str, str, str, Optional[dict[str, Any]]]],
    ) -> dict[str, Any]:
        """Dispatches [(chat_id, archetype, html_text, reply_markup)] with DB reservation."""
        stats: dict[str, Any] = {
            "total": len(payloads),
            "sent": 0,
            "skipped": 0,
            "failed": 0,
            "unknown": 0,
            "errors": {},
        }
        for chat_id, archetype, html_text, reply_markup in payloads:
            cid = str(chat_id).strip()
            content_hash = hashlib.sha256(html_text.encode("utf-8")).hexdigest()[:16]
            dispatch_id = db.reserve_dispatch(
                chat_id=cid,
                week_key=week_key,
                archetype=archetype,
                dispatch_type=dispatch_type,
                content_hash=content_hash,
            )
            if dispatch_id is None:
                stats["skipped"] += 1
                continue

            status, err = self.send_one(channel, cid, html_text, reply_markup=reply_markup)
            if status == "SENT":
                db.mark_dispatch_sent(dispatch_id)
                stats["sent"] += 1
            elif status == "UNKNOWN":
                db.mark_dispatch_failed(dispatch_id, err or "timeout", status="UNKNOWN")
                stats["unknown"] += 1
                if err:
                    stats["errors"][cid] = err
            else:
                db.mark_dispatch_failed(dispatch_id, err or "send failed", status="FAILED")
                stats["failed"] += 1
                if err:
                    stats["errors"][cid] = err
        return stats
