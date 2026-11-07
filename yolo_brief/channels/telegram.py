"""Telegram HTTP API Channel for YOLO Bot (욜로봇).
Supports:
- Immediate `answer_callback` ([TG-04])
- `safe_edit_or_send` ignoring 'message is not modified' ([TG-04])
- Dry-run / recording mode for tests and `--demo` ([TG-06])
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)


class YoloTelegramChannel:
    """Telegram Bot API wrapper for YOLO Bot."""

    def __init__(
        self,
        token: Optional[str] = None,
        dry_run: bool = False,
        timeout: float = 10.0,
    ) -> None:
        self.token = (
            token
            or os.environ.get("YOLO_TELEGRAM_BOT_TOKEN")
            or os.environ.get("YOLO_BOT_TOKEN")
            or ""
        ).strip()
        self.dry_run = dry_run or not bool(self.token)
        self.timeout = timeout
        self.call_log: list[tuple[str, dict[str, Any]]] = []

    def _api_call(self, method: str, payload: dict[str, Any]) -> dict[str, Any]:
        self.call_log.append((method, dict(payload)))
        if self.dry_run:
            return {"ok": True, "result": {"message_id": 1, "dry_run": True}}

        url = f"https://api.telegram.org/bot{self.token}/{method}"
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8")
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise RuntimeError(f"Telegram HTTP {exc.code}: {err_body}") from exc

    def answer_callback(
        self, callback_query_id: str, text: Optional[str] = None
    ) -> bool:
        """[TG-04] Must be called first upon receiving a callback_query."""
        payload: dict[str, Any] = {"callback_query_id": str(callback_query_id)}
        if text:
            payload["text"] = text[:180]
        try:
            self._api_call("answerCallbackQuery", payload)
            return True
        except Exception as exc:
            logger.warning("answerCallbackQuery non-fatal error: %s", exc)
            return False

    def send_message(
        self,
        chat_id: str,
        text: str,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": str(chat_id),
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self._api_call("sendMessage", payload)

    def safe_edit_or_send(
        self,
        chat_id: str,
        message_id: Optional[int],
        text: str,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """[TG-04] Edits message in-place if message_id is given; ignores 'message is not modified',
        and falls back to `send_message` only when editing is unsupported/missing."""
        if message_id is not None:
            payload: dict[str, Any] = {
                "chat_id": str(chat_id),
                "message_id": int(message_id),
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            }
            if reply_markup is not None:
                payload["reply_markup"] = reply_markup
            try:
                return self._api_call("editMessageText", payload)
            except Exception as exc:
                err_lower = str(exc).lower()
                if "message is not modified" in err_lower:
                    return {"ok": True, "not_modified": True}
                if "timed out" in err_lower or "timeout" in err_lower:
                    # Do not fallback-send on timeout to avoid duplicate messages
                    raise
                logger.info("editMessageText failed (%s), falling back to sendMessage", exc)

        return self.send_message(chat_id, text, reply_markup=reply_markup)

    def get_updates(self, offset: Optional[int] = None, timeout: int = 20) -> list[dict[str, Any]]:
        if self.dry_run:
            return []
        payload: dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            payload["offset"] = offset
        res = self._api_call("getUpdates", payload)
        return list(res.get("result", []))
