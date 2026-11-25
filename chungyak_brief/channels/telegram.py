"""Telegram channel + fixed slim templates for chungyak_bot.

- [TG-01] visible <= 1,800 chars / HTML <= 3,500 chars; multi-notice bundles are split per notice block
  (never trimmed mid-block) by `split_blocks`.
- [TG-02] every dynamic string is `html.escape()`d; only <b>, <i>, <code>, <a href> tags are emitted.
- [TG-04] `answer_callback` goes through `answer_callback_once` (one ACK per callback id).
- [TG-06] dry-run / token-less mode records calls in `call_log` and never touches the network.
Token lookup: APTAPP_TELEGRAM_BOT_TOKEN -> CHUNGYAK_TELEGRAM_BOT_TOKEN -> TELEGRAM_BOT_TOKEN (never logged).
"""
from __future__ import annotations

from datetime import date
import html
import json
import logging
import os
import re
from typing import Any, Optional
import urllib.error
import urllib.request

from chungyak_brief.models import ChungyakNotice, EligibilityResult, decimal_text
from market_brief.channels.http_keepalive import answer_callback_once, open_request

logger = logging.getLogger(__name__)

MAX_VISIBLE_CHARS = 1800
MAX_HTML_CHARS = 3500
MAX_CALLBACK_BYTES = 64
TOKEN_ENV_ORDER: tuple[str, ...] = (
    "APTAPP_TELEGRAM_BOT_TOKEN",
    "CHUNGYAK_TELEGRAM_BOT_TOKEN",
    "TELEGRAM_BOT_TOKEN",
)
CHAT_ID_ENV = "APTAPP_TELEGRAM_CHAT_ID"

_TAG_RE = re.compile(r"<[^>]+>")


def resolve_bot_token(environ: Optional[dict[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    for name in TOKEN_ENV_ORDER:
        value = str(env.get(name) or "").strip()
        if value:
            return value
    return ""


def resolve_default_chat_id(environ: Optional[dict[str, str]] = None) -> str:
    env = os.environ if environ is None else environ
    return str(env.get(CHAT_ID_ENV) or "").strip()


def esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def measure_message(html_text: str) -> tuple[int, int]:
    """(visible chars without tags, raw HTML length)."""
    visible = html.unescape(_TAG_RE.sub("", html_text))
    return len(visible), len(html_text)


def within_limits(html_text: str) -> bool:
    v, h = measure_message(html_text)
    return v <= MAX_VISIBLE_CHARS and h <= MAX_HTML_CHARS


def callback_ok(data: str) -> bool:
    try:
        raw = data.encode("ascii")
    except UnicodeEncodeError:
        return False
    return 0 < len(raw) <= MAX_CALLBACK_BYTES


def _clip(text: Any, limit: int) -> str:
    s = "" if text is None else str(text).strip()
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _md(d: Optional[date]) -> str:
    return d.strftime("%m.%d") if d else "미정"


def _safe_link(url: str) -> Optional[str]:
    u = (url or "").strip()
    if not u.lower().startswith("https://"):
        return None
    return u


def render_notice_block(
    notice: ChungyakNotice,
    result: Optional[EligibilityResult] = None,
    *,
    amended: bool = False,
) -> str:
    """Fixed slim per-notice block (1행 1정보). Bounded size regardless of upstream text length."""
    tag = " · ✏️ 정정" if (amended or notice.revision > 1) else ""
    lines = [f"🏢 <b>{esc(_clip(notice.house_name, 40))}</b> <i>{esc(notice.notice_type.label)}</i>{tag}"]
    loc = " ".join(x for x in (notice.region_sido, notice.region_sigungu) if x) or "지역 미상"
    hh = f" · <b>{esc(notice.supply_households)}</b>세대" if notice.supply_households else ""
    lines.append(f"📍 {esc(_clip(loc, 24))}{hh}")
    lines.append(f"🗓 접수 <b>{_md(notice.receipt_start)}~{_md(notice.receipt_end)}</b> · 발표 {_md(notice.winner_date)}")
    price = decimal_text(notice.min_price_eok)
    area = decimal_text(notice.min_area_m2)
    money = []
    if price:
        money.append(f"최저 <b>{esc(price)}억</b>")
    if area:
        money.append(f"{esc(area)}㎡~")
    if money:
        lines.append("💰 " + " · ".join(money))
    if result is not None:
        why = " · ".join(_clip(r, 28) for r in result.reasons[:2])
        lines.append(f"{esc(result.status.badge)}" + (f" · {esc(why)}" if why else ""))
        if result.unknowns:
            lines.append("❔ 미확인: " + esc(", ".join(_clip(u, 16) for u in result.unknowns[:3])))
    link = _safe_link(notice.homepage_url)
    if link:
        lines.append(f'🔗 <a href="{esc(link)}">청약홈 공고 보기</a>')
    return "\n".join(lines)


def split_blocks(
    header: str,
    blocks: list[str],
    footer: str = "",
    *,
    max_visible: int = MAX_VISIBLE_CHARS,
    max_html: int = MAX_HTML_CHARS,
) -> list[tuple[str, list[int]]]:
    """Packs blocks into messages under [TG-01] limits. Returns [(html, [block indexes])].
    Blocks are never cut; every block index appears in exactly one message."""
    sep = "\n\n"
    out: list[tuple[str, list[int]]] = []
    cur: list[int] = []

    def compose(idxs: list[int], part: int) -> str:
        head = header if part == 0 else f"{header} <i>(이어서)</i>"
        body = sep.join(blocks[i] for i in idxs)
        return sep.join(x for x in (head, body, footer) if x)

    for i in range(len(blocks)):
        trial = cur + [i]
        text = compose(trial, len(out))
        v, h = measure_message(text)
        if (v > max_visible or h > max_html) and cur:
            out.append((compose(cur, len(out)), cur))
            cur = [i]
        else:
            cur = trial
    if cur:
        out.append((compose(cur, len(out)), cur))
    return out


class ChungyakTelegramChannel:
    def __init__(
        self,
        token: Optional[str] = None,
        dry_run: bool = False,
        timeout: float = 10.0,
        opener: Optional[Any] = None,
    ) -> None:
        self.token = (token if token is not None else resolve_bot_token()).strip()
        self.dry_run = bool(dry_run) or not self.token
        self.timeout = timeout
        self._opener = opener
        self.call_log: list[tuple[str, dict[str, Any]]] = []
        self._dry_msg_id = 1000

    def _api_call(self, method: str, payload: dict[str, Any], timeout: Optional[float] = None) -> dict[str, Any]:
        self.call_log.append((method, dict(payload)))
        if self.dry_run:
            self._dry_msg_id += 1
            return {"ok": True, "result": {"message_id": self._dry_msg_id, "dry_run": True}}
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with open_request(req, timeout or self.timeout, opener=self._opener) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_body = exc.read().decode("utf-8", errors="replace")
            except Exception:
                pass
            raise RuntimeError(f"Telegram HTTP {exc.code}: {err_body[:300]}") from exc
        if not body.get("ok", False):
            raise RuntimeError(f"Telegram API error: {str(body.get('description') or '')[:300]}")
        return body

    def answer_callback(self, callback_query_id: str, text: Optional[str] = None) -> bool:
        payload: dict[str, Any] = {"callback_query_id": str(callback_query_id)}
        if text:
            payload["text"] = text[:180]

        def _send() -> bool:
            try:
                self._api_call("answerCallbackQuery", payload)
                return True
            except Exception as exc:
                logger.warning("answerCallbackQuery non-fatal error: %s", type(exc).__name__)
                return False

        return bool(answer_callback_once(callback_query_id, _send))

    def send_message(self, chat_id: str, text: str, reply_markup: Optional[dict[str, Any]] = None) -> dict[str, Any]:
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
                low = str(exc).lower()
                if "message is not modified" in low:
                    return {"ok": True, "not_modified": True}
                if "timed out" in low or "timeout" in low:
                    raise
                logger.info("editMessageText failed, falling back to sendMessage")
        return self.send_message(chat_id, text, reply_markup=reply_markup)

    def get_updates(self, offset: Optional[int] = None, timeout: int = 20) -> list[dict[str, Any]]:
        if self.dry_run:
            return []
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        res = self._api_call("getUpdates", payload, timeout=float(timeout) + 10.0)
        return list(res.get("result", []))
