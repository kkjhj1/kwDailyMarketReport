"""Telegram Bot API transport + [TG-05] rate-limited dispatcher for SilverCare.
HTTP errors surface as TelegramAPIError (code / retry_after preserved) so 429 back-off actually works.
"""
from __future__ import annotations
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Iterable, Optional

logger = logging.getLogger(__name__)

SILVER_COMMANDS = [
    {"command": "start", "description": "실버케어 메인 메뉴"},
    {"command": "parents", "description": "부모님 등록·혜택 찾기"},
    {"command": "bookmarks", "description": "찜한 혜택 보기"},
]


class TelegramAPIError(RuntimeError):
    def __init__(self, method: str, code: int, description: str, retry_after: float = 0.0):
        super().__init__(f"Telegram API {method} HTTP {code}: {description}")
        self.code = code
        self.description = description
        self.retry_after = retry_after


class SilverTelegramChannel:
    """Minimal Telegram Bot API client (stdlib only)."""

    def __init__(self, bot_token: str, opener: Optional[Callable[..., Any]] = None):
        self.bot_token = (bot_token or "").strip()
        self._opener = opener or urllib.request.urlopen
        if not self.bot_token:
            logger.warning("SilverTelegramChannel initialized with empty bot_token")

    def _api_call(self, method: str, payload: Optional[dict[str, Any]] = None, timeout: int = 15) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        body = json.dumps(payload or {}).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json", "User-Agent": "SilverCareBot/1.0"},
        )
        try:
            with self._opener(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            desc, retry_after = str(exc), 0.0
            try:
                err = json.loads(exc.read().decode("utf-8", errors="ignore"))
                desc = err.get("description", desc)
                retry_after = float((err.get("parameters") or {}).get("retry_after", 0) or 0)
            except Exception:
                pass
            raise TelegramAPIError(method, exc.code, desc, retry_after) from exc
        if not data.get("ok"):
            raise TelegramAPIError(method, 0, data.get("description", "unknown"))
        return data

    def set_my_commands(self, commands: Optional[list[dict[str, str]]] = None) -> bool:
        return bool(self._api_call("setMyCommands", {"commands": commands or SILVER_COMMANDS}).get("ok"))

    def send_message(
        self, chat_id: int | str, text: str, reply_markup: Optional[dict[str, Any]] = None,
        disable_notification: bool = False,
    ) -> int:
        body: dict[str, Any] = {
            "chat_id": str(chat_id), "text": text, "parse_mode": "HTML", "disable_web_page_preview": True,
        }
        if disable_notification:
            body["disable_notification"] = True
        if reply_markup:
            body["reply_markup"] = reply_markup
        return int(self._api_call("sendMessage", body)["result"]["message_id"])

    def edit_message_text(
        self, chat_id: int | str, message_id: int, text: str, reply_markup: Optional[dict[str, Any]] = None
    ) -> bool:
        body: dict[str, Any] = {
            "chat_id": str(chat_id), "message_id": message_id, "text": text,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        try:
            return bool(self._api_call("editMessageText", body).get("ok"))
        except TelegramAPIError as exc:
            if "message is not modified" in exc.description:
                return True
            logger.warning("edit_message_text failed: %s", exc)
            return False

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> bool:
        try:
            body = {"callback_query_id": callback_query_id, "text": text, "show_alert": False}
            return bool(self._api_call("answerCallbackQuery", body, timeout=5).get("ok"))
        except Exception as exc:
            logger.debug("answerCallbackQuery failed: %s", exc)
            return False

    def get_updates(self, offset: int = 0, timeout: int = 25) -> list[dict[str, Any]]:
        res = self._api_call(
            "getUpdates", {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
            timeout=timeout + 10,
        )
        return res.get("result", [])


class TelegramRateLimiter:
    """Global <= 25 msg/s and >= 1.0s per chat ([TG-05])."""

    def __init__(self, max_global_rate: float = 25.0, min_chat_interval: float = 1.0,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic):
        self.min_global_interval = 1.0 / max_global_rate if max_global_rate > 0 else 0.04
        self.min_chat_interval = min_chat_interval
        self._sleep = sleep
        self._clock = clock
        self._last_global = float("-inf")
        self._last_chat: dict[str, float] = {}

    def wait_before_send(self, chat_id: int | str) -> None:
        key = str(chat_id)
        now = self._clock()
        wait = max(
            self.min_global_interval - (now - self._last_global),
            self.min_chat_interval - (now - self._last_chat.get(key, float("-inf"))),
            0.0,
        )
        if wait > 0:
            self._sleep(wait)
        stamp = self._clock()
        self._last_global = stamp
        self._last_chat[key] = stamp


class SafeTelegramDispatcher:
    """Sends one personalised message per chat with idempotency log + 429 exponential back-off.

    `db` must expose has_dispatched(key, chat_id) / log_dispatch(key, chat_id, status).
    """

    def __init__(self, rate_limiter: Optional[TelegramRateLimiter] = None, max_retries: int = 3,
                 sleep: Callable[[float], None] = time.sleep):
        self.limiter = rate_limiter or TelegramRateLimiter(sleep=sleep)
        self.max_retries = max_retries
        self._sleep = sleep

    def dispatch(
        self,
        channel: Any,
        db: Any,
        dispatch_key: str,
        messages: Iterable[tuple[int, str]],
        disable_notification: bool = False,
    ) -> dict[str, Any]:
        stats: dict[str, Any] = {"total": 0, "success": 0, "skipped": 0, "failed": 0, "blocked": 0, "errors": {}}
        for chat_id, text in messages:
            stats["total"] += 1
            if db.has_dispatched(dispatch_key, chat_id):
                stats["skipped"] += 1
                continue
            status = self._send_with_retry(channel, chat_id, text, disable_notification)
            db.log_dispatch(dispatch_key, chat_id, status)
            if status == "SUCCESS":
                stats["success"] += 1
            elif status in ("HTTP_400", "HTTP_403"):
                stats["blocked"] += 1
            else:
                stats["failed"] += 1
                stats["errors"][str(chat_id)] = status
        return stats

    def _send_with_retry(self, channel: Any, chat_id: int, text: str, silent: bool) -> str:
        for attempt in range(self.max_retries + 1):
            self.limiter.wait_before_send(chat_id)
            try:
                channel.send_message(chat_id, text, disable_notification=silent)
                return "SUCCESS"
            except TelegramAPIError as exc:
                if exc.code == 429 and attempt < self.max_retries:
                    self._sleep(max(exc.retry_after, 2.0 ** attempt))
                    continue
                if exc.code in (400, 403):
                    return f"HTTP_{exc.code}"
                return "FAILED_429" if exc.code == 429 else f"HTTP_{exc.code}"
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                # Network timeout: the message may already be delivered — do not resend (no duplicate push)
                logger.warning("send to %s network error (not retried): %s", chat_id, exc)
                return "FAILED_NETWORK"
            except Exception as exc:  # noqa: BLE001
                logger.warning("send to %s failed: %s", chat_id, exc)
                return "FAILED_ERROR"
        return "FAILED_429"
