"""Telegram transport for Admission Briefing.
Supports mobile-optimized HTML formatting, inline keyboards, PDF attachments, and callback queries.
"""
from __future__ import annotations
import html
import json
import logging
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional

from admission_brief.models import (
    AdmissionBriefing,
    BriefingType,
    TargetGrade,
    TargetRegion,
)

logger = logging.getLogger(__name__)


def _is_valid_url(url: str | None) -> bool:
    """Verifies public web URL for Telegram inline buttons."""
    if not url or not url.startswith(("http://", "https://")):
        return False
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    return bool(host and "." in host and host not in {"localhost", "127.0.0.1"})


def format_admission_html(briefing: AdmissionBriefing) -> str:
    """Formats an AdmissionBriefing into a high-clarity HTML message for parents."""
    icon = briefing.briefing_type.icon
    type_title = briefing.briefing_type.title
    grade_badge = briefing.target_grade.badge
    region_badge = " 💻 [온라인]" if briefing.is_online_live else (f" {briefing.target_region.badge}" if briefing.target_region != TargetRegion.ALL else "")

    lines = [
        f"{icon} <b>[{html.escape(type_title)}]</b> {grade_badge}{region_badge}",
        f"🕒 기준: <code>{html.escape(briefing.as_of)}</code>",
        "",
        f"📌 <b>【{html.escape(briefing.headline)}】</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    # 1. 3-line summary
    if briefing.summary_3lines:
        lines.append("<b>■ 🔍 핵심 요약 3가지</b>")
        for i, pt in enumerate(briefing.summary_3lines, 1):
            lines.append(f"<b>{i}.</b> {html.escape(pt)}")
        lines.append("")

    # 2. So What (학부모 기준 득실 해석)
    if briefing.so_what:
        lines.extend([
            "<b>■ 💡 학부모 득실 분석 (So What?)</b>",
            f"<i>{html.escape(briefing.so_what)}</i>",
            "",
        ])

    # 3. Weekly Action To-Dos
    if briefing.action_todos:
        lines.append("<b>■ 📋 이번 주 필수 행동 지침 (To-Do)</b>")
        for item in briefing.action_todos:
            lines.append(f"  ▪ {html.escape(item)}")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("<i>에듀브리프 (EduBrief) 입시 인텔리전스</i>")

    return "\n".join(lines)


class TelegramChannel:
    """Telegram Bot API communication channel."""

    def __init__(self, bot_token: str, opener=None):
        self.bot_token = bot_token.strip()
        self._opener = opener or urllib.request.urlopen
        if not self.bot_token:
            logger.warning("TelegramChannel initialized with empty bot_token")

    def _api_call(self, method: str, payload: Optional[dict[str, Any]] = None, timeout: int = 15) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        if payload is None:
            req = urllib.request.Request(url, method="GET", headers={"User-Agent": "EduBriefBot/1.0"})
        else:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json", "User-Agent": "EduBriefBot/1.0"},
                method="POST",
            )
        try:
            with self._opener(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_json = json.loads(exc.read().decode("utf-8", errors="ignore"))
                err_body = err_json.get("description", "")
            except Exception:
                err_body = str(exc)
            raise RuntimeError(f"Telegram API {method} HTTP {exc.code}: {err_body}") from exc

        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error ({method}): {data.get('description', 'unknown')}")
        return data

    def set_my_commands(self, commands: Optional[list[dict[str, str]]] = None) -> bool:
        """Registers official command menu into Telegram app (left-side blue button)."""
        if commands is None:
            commands = [
                {"command": "todo", "description": "📋 이번 주 우리 아이 필수 To-Do"},
                {"command": "recap", "description": "📢 설명회 3줄 요약 모아보기"},
                {"command": "grade", "description": "🎓 자녀 학년 변경"},
                {"command": "region", "description": "📍 지역/온라인 설정"},
                {"command": "help", "description": "❓ 이용 도움말"},
            ]
        try:
            res = self._api_call("setMyCommands", {"commands": commands})
            return bool(res.get("ok"))
        except Exception as e:
            logger.warning("Failed to setMyCommands: %s", e)
            return False

    def send(
        self,
        recipient: str,
        briefing: AdmissionBriefing,
        disable_notification: Optional[bool] = None,
    ) -> str:
        """Sends an AdmissionBriefing to recipient with optional ticketing inline button."""
        text = format_admission_html(briefing)
        is_silent = briefing.silent if disable_notification is None else disable_notification
        body: dict[str, Any] = {
            "chat_id": str(recipient).strip(),
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if is_silent:
            body["disable_notification"] = True

        # Attach ticketing button if valid URL is present
        if briefing.booking_url and _is_valid_url(briefing.booking_url):
            body["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": briefing.booking_button_text, "url": briefing.booking_url}]
                ]
            }

        res = self._api_call("sendMessage", body)
        return str(res["result"]["message_id"])

    def send_message(
        self,
        recipient: str,
        text: str,
        reply_markup: Optional[dict[str, Any]] = None,
        parse_mode: str = "HTML",
        disable_notification: bool = False,
    ) -> str:
        """Sends arbitrary text message with optional reply_markup."""
        body: dict[str, Any] = {
            "chat_id": str(recipient).strip(),
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if disable_notification:
            body["disable_notification"] = True
        if reply_markup:
            body["reply_markup"] = reply_markup
        res = self._api_call("sendMessage", body)
        return str(res["result"]["message_id"])

    def edit_message_text(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: Optional[dict[str, Any]] = None,
        parse_mode: str = "HTML",
    ) -> bool:
        """Edits an existing message (used for inline button callbacks)."""
        body: dict[str, Any] = {
            "chat_id": str(chat_id).strip(),
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode,
        }
        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        try:
            res = self._api_call("editMessageText", body)
            return bool(res.get("ok"))
        except Exception as e:
            logger.warning("edit_message_text failed: %s", e)
            return False

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> bool:
        """Acknowledges callback query from inline button."""
        body: dict[str, Any] = {
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": False,
        }
        try:
            res = self._api_call("answerCallbackQuery", body)
            return bool(res.get("ok"))
        except Exception as e:
            logger.warning("answer_callback_query failed: %s", e)
            return False

    def send_document(
        self,
        recipient: str,
        document_path: str | Path,
        caption: str = "📄 에듀브리프 입시 자료집 파일",
    ) -> str:
        """Uploads PDF/document directly to Telegram chat via multipart/form-data."""
        path = Path(document_path)
        if not path.exists():
            raise FileNotFoundError(f"Document file not found: {path}")

        boundary = f"----EduBriefBoundary{uuid.uuid4().hex}"
        file_bytes = path.read_bytes()

        parts = [
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
                f"{str(recipient).strip()}\r\n"
            ).encode("utf-8"),
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="caption"\r\n\r\n'
                f"{caption}\r\n"
            ).encode("utf-8"),
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="document"; filename="{path.name}"\r\n'
                f"Content-Type: application/pdf\r\n\r\n"
            ).encode("utf-8")
            + file_bytes
            + b"\r\n",
            f"--{boundary}--\r\n".encode("utf-8"),
        ]
        body = b"".join(parts)

        url = f"https://api.telegram.org/bot{self.bot_token}/sendDocument"
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        with self._opener(req, timeout=30) as resp:
            res = json.loads(resp.read().decode("utf-8"))

        if not res.get("ok"):
            raise RuntimeError(f"Telegram sendDocument failed: {res}")
        return str(res["result"]["message_id"])

    def get_updates(self, offset: int = 0, timeout: int = 5) -> list[dict[str, Any]]:
        """Long-polling fetch updates."""
        res = self._api_call("getUpdates", {"offset": offset, "timeout": timeout}, timeout=timeout + 5)
        return res.get("result", [])
