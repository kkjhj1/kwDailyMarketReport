"""Telegram transport for Admission Briefing.
Supports mobile-optimized HTML formatting, inline keyboards, PDF attachments, and callback queries.
"""
from __future__ import annotations
import html
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from admission_brief.models import (
    AdmissionBriefing,
    AdmissionEvent,
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


GCAL_BASE_URL = "https://calendar.google.com/calendar/render"
GCAL_MAX_URL_BYTES = 1800
GCAL_DEFAULT_DURATION = timedelta(hours=2)
KST_OFFSET = timedelta(hours=9)


def build_google_calendar_url(event: AdmissionEvent, now: Optional[datetime] = None) -> Optional[str]:
    """Builds a Google Calendar 'add event' web deeplink for an admission event.

    event_date_str is KST ("YYYY-MM-DD HH:MM" → 2-hour slot in UTC; "YYYY-MM-DD" → all-day).
    Returns None for unparseable (TBA) or past dates. now is naive KST (defaults to datetime.now()).
    """
    raw = (event.event_date_str or "").strip()
    now = now or datetime.now()
    start_kst: Optional[datetime] = None
    all_day = False
    try:
        start_kst = datetime.strptime(raw[:16], "%Y-%m-%d %H:%M")
    except ValueError:
        try:
            start_kst = datetime.strptime(raw[:10], "%Y-%m-%d")
            all_day = True
        except ValueError:
            return None

    if all_day:
        if start_kst.date() < now.date():
            return None
        dates_str = f"{start_kst:%Y%m%d}/{start_kst + timedelta(days=1):%Y%m%d}"
    else:
        if start_kst < now:
            return None
        start_utc = start_kst - KST_OFFSET
        end_utc = start_utc + GCAL_DEFAULT_DURATION
        dates_str = f"{start_utc:%Y%m%dT%H%M%SZ}/{end_utc:%Y%m%dT%H%M%SZ}"

    def _build(details: str) -> str:
        query = urllib.parse.urlencode({
            "action": "TEMPLATE",
            "text": f"[{event.organizer}] {event.title}",
            "dates": dates_str,
            "details": details,
            "location": event.target_region.label,
        })
        return f"{GCAL_BASE_URL}?{query}"

    details = f"주최: {event.organizer}\n원문/신청: {event.booking_url or ''}\n\n{event.so_what}"
    url = _build(details)
    # Shrink details until the URL fits (percent-encoded URL is ASCII, so len == bytes)
    while len(url) > GCAL_MAX_URL_BYTES and details:
        cut = max(0, len(details) - max(10, (len(url) - GCAL_MAX_URL_BYTES) // 9 + 1))
        details = details[:cut].rstrip() + "…" if cut else ""
        url = _build(details)
    return url if len(url) <= GCAL_MAX_URL_BYTES else None


TG_MAX_HTML_LEN = 3500       # [TG-01] HTML 원문 상한
TG_MAX_VISIBLE_LEN = 1800    # [TG-01] 태그 제외 가시 텍스트 상한

_TAG_RE = re.compile(r"<[^>]+>")


def measure_message(html_text: str) -> tuple[int, int]:
    """Returns (raw HTML length, visible text length after stripping tags and unescaping entities)."""
    visible = html.unescape(_TAG_RE.sub("", html_text))
    return len(html_text), len(visible)


def format_admission_html(briefing: AdmissionBriefing) -> str:
    """Formats an AdmissionBriefing into a high-clarity HTML message for parents."""
    icon = briefing.briefing_type.icon
    type_title = briefing.briefing_type.title
    grade_badge = briefing.target_grade.badge
    region_badge = " 💻 [온라인]" if briefing.is_online_live else (f" {briefing.target_region.badge}" if briefing.target_region != TargetRegion.ALL else "")
    urgency_badge = " 🚨 <b>[실시간 긴급 속보]</b>" if (briefing.urgency == "BREAKING" or briefing.briefing_type == BriefingType.BREAKING_NEWS) else ""
    category_badge = f" {briefing.category.badge}" if briefing.category else ""

    lines = [
        f"{icon} <b>[{html.escape(type_title)}]</b>{urgency_badge}{category_badge} {grade_badge}{region_badge}",
        f"🕒 기준: <code>{html.escape(briefing.as_of)}</code>",
        f"📌 <b>{html.escape(briefing.headline)}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    # 1. 3-line summary
    if briefing.summary_3lines:
        lines.append("<b>🔍 핵심 요약 3가지</b>")
        for i, pt in enumerate(briefing.summary_3lines, 1):
            lines.append(f"<b>{i}.</b> {html.escape(pt)}")
        lines.append("")

    # 2. So What (학부모 기준 득실 해석)
    if briefing.so_what:
        lines.extend([
            "<b>💡 학부모 득실 분석 및 핵심 인사이트</b>",
            f"<i>{html.escape(briefing.so_what)}</i>",
            "",
        ])

    # 3. Weekly Action To-Dos
    if briefing.action_todos:
        lines.append("<b>📋 이번 주 필수 행동 지침 (To-Do)</b>")
        for item in briefing.action_todos:
            lines.append(f"• {html.escape(item)}")
        lines.append("")

    # 4. Source Grounding (100% Official Verifiability)
    if briefing.source_name:
        if briefing.source_url and _is_valid_url(briefing.source_url):
            lines.append(f"🏛 <b>공식 출처:</b> <a href=\"{html.escape(briefing.source_url)}\">{html.escape(briefing.source_name)}</a>")
        else:
            lines.append(f"🏛 <b>공식 출처:</b> {html.escape(briefing.source_name)}")
        lines.append("")

    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append("<i>에듀브리프 (EduBrief) 입시 인텔리전스</i>")

    text = "\n".join(lines)
    html_len, visible_len = measure_message(text)
    if html_len > TG_MAX_HTML_LEN or visible_len > TG_MAX_VISIBLE_LEN:
        logger.warning(
            "[TG-01] Admission message over limit (html=%d, visible=%d): %s",
            html_len, visible_len, briefing.briefing_id,
        )
    return text


SHARE_BOT_HANDLE = "@KwEduInfo_bot"
SHARE_FIELD_MAX = 160  # 고정 슬림 템플릿: 필드별 상한 (항목 삭제 없이 길이만 고정)


def _clip(text: str, limit: int = SHARE_FIELD_MAX) -> str:
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_kakao_share_html(plain_text: str) -> str:
    """Wraps plain share text in a one-tap-copy <code> block (Telegram copies <code> on tap)."""
    text = (
        "📲 <b>카카오톡 원터치 복사 카드</b>\n"
        "<i>👇 회색 박스를 <b>한 번 탭</b>하면 텍스트가 자동 복사됩니다:</i>\n\n"
        f"<code>{html.escape(plain_text.strip())}\n\n출처: 에듀브리프 봇 ({SHARE_BOT_HANDLE})</code>"
    )
    html_len, visible_len = measure_message(text)
    if html_len > TG_MAX_HTML_LEN or visible_len > TG_MAX_VISIBLE_LEN:
        logger.warning("[TG-01] Share card over limit (html=%d, visible=%d)", html_len, visible_len)
    return text


def build_glossary_share_text(entry: Any) -> str:
    """Plain-text share body for a GlossaryEntry."""
    return "\n".join([
        f"[에듀브리프 입시사전: {entry.term}]",
        f"💡 개념: {entry.definition}",
        f"🎯 학부모 득실: {entry.impact}",
        f"⚠️ 주의사항: {entry.caution}",
    ])


def build_briefing_share_text(briefing: AdmissionBriefing) -> str:
    """Plain-text share body for a briefing (e.g. weekly To-Do): headline, 3 points, so-what, to-dos."""
    lines = [f"[에듀브리프 {_clip(briefing.briefing_type.title, 30)}] {_clip(briefing.headline)}"]
    for i, pt in enumerate(briefing.summary_3lines[:3], 1):
        lines.append(f"{i}. {_clip(pt)}")
    if briefing.so_what:
        lines.append(f"💡 {_clip(briefing.so_what, 240)}")
    for item in briefing.action_todos[:3]:
        lines.append(f"✅ {_clip(item)}")
    return "\n".join(lines)


def build_event_share_text(event: AdmissionEvent) -> str:
    """Plain-text share body for an event card: title, date, organizer/place, link."""
    place = "온라인 생중계" if event.is_online_live else event.target_region.label
    lines = [
        f"[에듀브리프 행사] {_clip(event.title)}",
        f"🗓 일시: {_clip(event.event_date_str, 40)}",
        f"🏢 주최: {_clip(event.organizer, 60)} · {_clip(place, 40)}",
    ]
    if event.is_first_come:
        lines.append("⚡ 선착순 마감")
    if event.so_what:
        lines.append(f"💡 {_clip(event.so_what, 240)}")
    link = event.booking_url or event.source_url
    if link and _is_valid_url(link):
        lines.append(f"🔗 {_clip(link, 300)}")
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
                {"command": "todo", "description": "📋 이번 주 필수 To-Do"},
                {"command": "hot", "description": "⚡ 긴급 속보 & 핫 행사 TOP 3"},
                {"command": "fact", "description": "💡 1분 입시 팩트체크 (So-What)"},
                {"command": "events", "description": "🎪 설명회·박람회 전체보기"},
                {"command": "settings", "description": "⚙️ 학년 및 관심지역 설정"},
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
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> str:
        """Sends an AdmissionBriefing to recipient with optional ticketing inline button.

        An explicit reply_markup overrides the default booking_url button.
        """
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

        if reply_markup is not None:
            body["reply_markup"] = reply_markup
        # Attach ticketing button if valid URL is present
        elif briefing.booking_url and _is_valid_url(briefing.booking_url):
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
            # Same content re-tapped: the screen is already correct, so treat as success
            if "message is not modified" in str(e):
                return True
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
