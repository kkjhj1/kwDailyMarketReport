"""Lightweight Telegram Bot API Client for Portal Hub.
Zero external dependencies (pure Python urllib) with automatic retry on WinError 10054.
"""
from __future__ import annotations
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

logger = logging.getLogger(__name__)


class TelegramPortalClient:
    """Zero-dependency HTTP client for Telegram Bot API with retry guarantees."""

    def __init__(self, bot_token: str, opener=None):
        self.bot_token = bot_token.strip()
        self._opener = opener or urllib.request.urlopen
        if not self.bot_token:
            logger.warning("TelegramPortalClient initialized with empty bot_token")

    def _api_call(
        self, method: str, payload: Optional[dict[str, Any]] = None, timeout: int = 15, retries: int = 2
    ) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        headers = {
            "User-Agent": "PortalHubBot/2.1",
            "Connection": "close",
        }

        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        last_exc: Optional[Exception] = None
        for attempt in range(retries + 1):
            req = urllib.request.Request(
                url, data=body, headers=headers, method="POST" if payload is not None else "GET"
            )
            try:
                with self._opener(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode("utf-8"))
                if not data.get("ok"):
                    raise RuntimeError(f"Telegram API error ({method}): {data.get('description', 'unknown')}")
                return data
            except urllib.error.HTTPError as exc:
                err_body = ""
                try:
                    err_json = json.loads(exc.read().decode("utf-8", errors="ignore"))
                    err_body = err_json.get("description", "")
                except Exception:
                    err_body = str(exc)
                raise RuntimeError(f"Telegram API {method} HTTP {exc.code}: {err_body}") from exc
            except Exception as exc:
                last_exc = exc
                if attempt < retries:
                    time.sleep(0.15)
                    continue
                raise RuntimeError(f"Telegram API {method} failed after {retries} retries: {exc}") from exc

        raise RuntimeError(f"Telegram API {method} failed: {last_exc}")

    def send_message(
        self,
        chat_id: int | str,
        text: str,
        reply_markup: Optional[dict[str, Any]] = None,
        parse_mode: str = "HTML",
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        """Sends a message with optional reply_markup."""
        body: dict[str, Any] = {
            "chat_id": str(chat_id).strip(),
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        return self._api_call("sendMessage", body)

    def edit_message_text(
        self,
        chat_id: int | str,
        message_id: int,
        text: str,
        reply_markup: Optional[dict[str, Any]] = None,
        parse_mode: str = "HTML",
        disable_web_page_preview: bool = True,
    ) -> bool:
        """In-place message edit for instantaneous, flicker-free UI transitions."""
        body: dict[str, Any] = {
            "chat_id": str(chat_id).strip(),
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        try:
            res = self._api_call("editMessageText", body)
            return bool(res.get("ok"))
        except Exception as e:
            # Check if error is 'message is not modified'
            err_msg = str(e)
            if "message is not modified" in err_msg.lower():
                return True
            logger.warning("editMessageText failed: %s", e)
            return False

    def answer_callback_query(
        self, callback_query_id: str, text: str = "", show_alert: bool = False
    ) -> bool:
        """Acknowledges callback query from inline button with optional modal popup."""
        body: dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": show_alert,
        }
        try:
            res = self._api_call("answerCallbackQuery", body, timeout=5, retries=1)
            return bool(res.get("ok"))
        except Exception as e:
            logger.debug("answerCallbackQuery failed: %s", e)
            return False

    def get_updates(self, offset: int = 0, timeout: int = 3) -> list[dict[str, Any]]:
        """Long-polling update retrieval with tight timeout to prevent stale TCP drops."""
        body = {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        try:
            res = self._api_call("getUpdates", body, timeout=timeout + 5, retries=1)
            return res.get("result", [])
        except Exception as e:
            logger.debug("getUpdates retry: %s", e)
            return []

    def set_my_commands(self, commands: list[dict[str, str]]) -> bool:
        """Sets bot command menu."""
        try:
            res = self._api_call("setMyCommands", {"commands": commands})
            return bool(res.get("ok"))
        except Exception as e:
            logger.warning("setMyCommands failed: %s", e)
            return False
