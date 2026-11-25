"""[TG-05] Rate-limited safe dispatcher for chungyak_bot.

- Token bucket: global 25 msg/s, 1.0 s per chat.
- 429: honours `retry after N`, otherwise exponential backoff (bounded retries) -> FAILED when exhausted.
- Network timeout / connection reset mid-request: UNKNOWN, never retried (duplicate-send prevention).
"""
from __future__ import annotations

import logging
import re
import socket
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class TelegramRateLimiter:
    def __init__(
        self,
        max_global_rate: float = 25.0,
        min_chat_interval: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.min_global_interval = 1.0 / max_global_rate if max_global_rate > 0 else 0.0
        self.min_chat_interval = min_chat_interval
        self._sleep = sleep
        self._clock = clock
        self._lock = threading.Lock()
        self._last_global: Optional[float] = None
        self._last_chat: dict[str, float] = {}

    def wait_before_send(self, chat_id: str) -> None:
        with self._lock:
            now = self._clock()
            waits = [0.0]
            if self._last_global is not None:
                waits.append(self.min_global_interval - (now - self._last_global))
            last = self._last_chat.get(chat_id)
            if last is not None:
                waits.append(self.min_chat_interval - (now - last))
            wait = max(waits)
            if wait > 0:
                self._sleep(wait)
            stamp = self._clock()
            self._last_global = stamp
            self._last_chat[chat_id] = stamp


class SafeTelegramDispatcher:
    def __init__(
        self,
        rate_limiter: Optional[TelegramRateLimiter] = None,
        max_retries: int = 3,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.limiter = rate_limiter or TelegramRateLimiter(sleep=sleep)
        self.max_retries = max(1, int(max_retries))
        self._sleep = sleep

    @staticmethod
    def _message_id(res: Any) -> Optional[str]:
        if isinstance(res, dict):
            result = res.get("result")
            if isinstance(result, dict) and result.get("message_id") is not None:
                return str(result["message_id"])
        return None

    def send_one(
        self,
        channel: Any,
        chat_id: str,
        text: str,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> tuple[str, Optional[str], Optional[str]]:
        """Returns (SENT | FAILED | UNKNOWN, provider_msg_id, error_detail)."""
        cid = str(chat_id)
        for attempt in range(1, self.max_retries + 1):
            self.limiter.wait_before_send(cid)
            try:
                res = channel.send_message(cid, text, reply_markup=reply_markup)
                return "SENT", self._message_id(res), None
            except (socket.timeout, TimeoutError) as exc:
                return "UNKNOWN", None, f"timeout: {type(exc).__name__}"
            except Exception as exc:
                err = str(exc)
                low = err.lower()
                if "timed out" in low or "timeout" in low or "connection reset" in low:
                    return "UNKNOWN", None, err[:300]
                if "429" in err or "too many requests" in low:
                    match = re.search(r"retry[ _]after[\"':\s]*(\d+)", low)
                    delay = float(match.group(1)) + 0.5 if match else float(2 ** attempt)
                    if attempt < self.max_retries:
                        logger.warning("[TG-05] 429 for chat, backoff %.1fs (attempt %d)", delay, attempt)
                        self._sleep(delay)
                        continue
                return "FAILED", None, err[:300]
        return "FAILED", None, "max retries exceeded"
