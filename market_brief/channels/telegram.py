"""Telegram transport; supports rich HTML formatting, clickable hyperlinks, inline buttons, and file attachment."""
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from market_brief.core import Briefing, format_briefing, AssetSummary


def _is_public_button_url(url: str) -> bool:
    """Telegram inline_keyboard and clickable links require a public http/https host (not localhost or .invalid)."""
    if not url or not url.startswith(("http://", "https://")):
        return False
    parsed = urllib.parse.urlparse(url)
    host = (parsed.hostname or "").lower()
    if not host or host in {"localhost", "127.0.0.1", "0.0.0.0"} or host.endswith(".invalid") or host.endswith(".local"):
        return False
    return "." in host


TG_MAX_VISIBLE_CHARS = 1800
TG_MAX_HTML_CHARS = 3500
_SEP = "━━━━━━━━━━━━━━━━━━"
_TAG_RE = re.compile(r"<[^>]+>")


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _clip(text: str, limit: int) -> str:
    text = str(text or "").replace("\n", " ").strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def telegram_visible_len(html_text: str) -> int:
    """Approximate Telegram-rendered length: strip tags, unescape entities."""
    return len(html.unescape(_TAG_RE.sub("", html_text)))


def _within_tg_limits(html_text: str) -> bool:
    return len(html_text) <= TG_MAX_HTML_CHARS and telegram_visible_len(html_text) <= TG_MAX_VISIBLE_CHARS


def format_telegram_html(briefing: Briefing) -> str:
    """Formats a Briefing into a mobile-optimized Telegram HTML message within [TG-01] limits.

    Bounded degradation (only when limits are exceeded): drop news → drop extras
    (events / investor trend / sectors) → truncate one-line summary → fixed slim fallback.
    """
    attempts = (
        dict(include_news=True, include_extras=True, summary_limit=None),
        dict(include_news=False, include_extras=True, summary_limit=None),
        dict(include_news=False, include_extras=False, summary_limit=None),
        dict(include_news=False, include_extras=False, summary_limit=60),
    )
    for opts in attempts:
        text = _render_telegram_html(briefing, **opts)
        if _within_tg_limits(text):
            return text
    return _render_slim_fallback(briefing)


def _kind_meta(briefing: Briefing) -> tuple[str, str]:
    kind = str(briefing.kind or "").lower()
    return {
        "morning": ("🌅", "07:50 장전 모닝 브리핑"),
        "closing": ("📊", "장마감 시황 브리핑"),
        "urgent": ("🚨", "±3% 급변동 긴급 경보"),
    }.get(kind, ("📢", f"{_clip(str(briefing.kind).upper(), 20)} 시황 브리핑"))


def _render_slim_fallback(briefing: Briefing) -> str:
    """Fixed slim template guaranteed to fit: header + clipped summary + report link."""
    icon, title = _kind_meta(briefing)
    lines = [
        f"{icon} <b>[{_esc(title)}]</b>",
        f"🕒 기준: <code>{_esc(_clip(briefing.as_of, 40))}</code>",
    ]
    if briefing.one_line_summary:
        lines.append(f"<i>{_esc(_clip(briefing.one_line_summary, 120))}</i>")
    lines.append(_SEP)
    if _is_public_button_url(briefing.report_url) and len(briefing.report_url) <= 1000:
        lines.append(f"👉 <a href=\"{_esc(briefing.report_url)}\">[📊 일일 상세 웹 리포트 보기]</a>")
    else:
        lines.append("💡 <i>상세 지표는 웹 리포트에서 확인하세요.</i>")
    return "\n".join(lines)


def _render_telegram_html(
    briefing: Briefing,
    *,
    include_news: bool,
    include_extras: bool,
    summary_limit: int | None,
) -> str:
    kind = str(briefing.kind or "").lower()
    kind_meta = _kind_meta(briefing)

    lines = [
        f"{kind_meta[0]} <b>[{_esc(kind_meta[1])}]</b>",
        f"🕒 기준: <code>{_esc(_clip(briefing.as_of, 40))}</code> (전 지표 종가 기준)",
    ]

    session_notice = getattr(briefing, "session_notice", "") or ""
    if session_notice:
        lines.append(f"<b>{_esc(_clip(session_notice, 80))}</b>")

    # 1. One-line executive summary
    if briefing.one_line_summary:
        summary = briefing.one_line_summary if summary_limit is None else _clip(briefing.one_line_summary, summary_limit)
        lines.extend([
            "",
            "📌 <b>【오늘의 한 줄 요약】</b>",
            f"<i>{_esc(summary)}</i>",
        ])

    lines.append(_SEP)

    # 1-1. Morning: today's economic events
    events = tuple(getattr(briefing, "economic_events", ()) or ())
    if include_extras and kind == "morning" and events:
        lines.append("📅 <b>【오늘의 주요 일정 & 변동성 이벤트】</b>")
        for ev in events[:5]:
            lines.append(f"• {_esc(_clip(ev, 60))}")
        lines.append(_SEP)

    # 2. Group assets into 4 primary investor categories
    kr_equities = [a for a in briefing.assets if a.category == "KR_EQUITY"]
    us_equities = [a for a in briefing.assets if a.category == "US_EQUITY"]
    bonds = [a for a in briefing.assets if a.category == "BOND"]
    alternatives = [a for a in briefing.assets if a.category == "ALTERNATIVE"]

    def _fmt_quote_line(a: AssetSummary) -> str:
        chg = a.change.strip()
        is_up = chg.startswith("+") and chg not in {"+0.00%", "+0.0bp"}
        is_down = chg.startswith("-") and chg not in {"-0.00%", "-0.0bp"}
        icon = "🔴" if is_up else ("🔵" if is_down else "⚪")

        ind_name = _esc(_clip(a.indicator, 24))
        chg = _clip(chg, 16)

        if a.unit == "PERCENT_YIELD":
            val_txt = f"<b>{a.value:.3f}%</b>" if a.value is not None else "-"
            return f"  {icon} {ind_name} : {val_txt} ({html.escape(chg)})"
        elif a.unit == "FX":
            val_txt = f"<b>{a.value:,.1f}원</b>" if a.value is not None else "-"
            diff_txt = f"{a.diff:+,.1f}원, " if a.diff is not None else ""
            return f"  {icon} {ind_name} : {val_txt} ({html.escape(diff_txt)}{html.escape(chg)})"
        elif a.unit == "PRICE":
            val_txt = f"<b>${a.value:,.2f}</b>" if a.value is not None else "-"
            return f"  {icon} {ind_name} : {val_txt} ({html.escape(chg)})"
        else: # INDEX
            val_txt = f"<b>{a.value:,.2f}</b>" if a.value is not None else "-"
            diff_txt = f"{a.diff:+,.2f}pt, " if a.diff is not None else ""
            return f"  {icon} {ind_name} : {val_txt} ({html.escape(diff_txt)}{html.escape(chg)})"

    if kr_equities:
        lines.append("<b>■ 🇰🇷 국내 증시 (종가)</b>")
        for a in kr_equities:
            lines.append(_fmt_quote_line(a))
        lines.append("")

    if us_equities:
        lines.append("<b>■ 🇺🇸 글로벌 증시 (종가)</b>")
        for a in us_equities:
            lines.append(_fmt_quote_line(a))
        lines.append("")

    if bonds:
        lines.append("<b>■ 🏦 금리 & 채권 (종가)</b>")
        for a in bonds:
            lines.append(_fmt_quote_line(a))
        lines.append("")

    if alternatives:
        lines.append("<b>■ 💵 환율 & 원자재 (종가/고시)</b>")
        for a in alternatives:
            lines.append(_fmt_quote_line(a))

    # 2-1. Closing: investor flows & leading sectors
    trend = getattr(briefing, "investor_trend", None)
    sectors = tuple(getattr(briefing, "leading_sectors", ()) or ())[:3]
    if include_extras and kind == "closing" and (trend or sectors):
        lines.append(_SEP)
        lines.append("📊 <b>【오늘의 수급 & 주도 업종】</b>")
        if trend:
            lines.append(
                "• <b>코스피 수급</b>: "
                f"개인 {_esc(_clip(trend.personal, 16))} | "
                f"외인 {_esc(_clip(trend.foreign, 16))} | "
                f"기관 {_esc(_clip(trend.institutional, 16))}"
            )
        if sectors:
            sector_txt = ", ".join(
                f"{_esc(_clip(s.name, 20))} ({_esc(_clip(s.change_rate, 10))})" for s in sectors
            )
            lines.append(f"• <b>주도 업종</b>: {sector_txt}")

    # 3. Live headlines with direct clickable article hyperlinks
    if include_news and briefing.news:
        lines.append(_SEP)
        lines.append("📰 <b>[주요 시황 뉴스 & 변동 요인]</b>")
        for idx, n in enumerate(briefing.news[:3], start=1):
            title_escaped = _esc(_clip(getattr(n, "title", str(n)), 80))
            src_escaped = _esc(_clip(getattr(n, "source", "네이버금융"), 20))
            url = getattr(n, "url", "")
            if url and url.startswith(("http://", "https://")) and len(url) <= 500:
                safe_url = html.escape(url, quote=True)
                lines.append(f"{idx}. <a href=\"{safe_url}\">{title_escaped}</a> ({src_escaped})")
            else:
                lines.append(f"{idx}. {title_escaped} ({src_escaped})")

    # 4. Standalone Report Link
    lines.append(_SEP)
    if _is_public_button_url(briefing.report_url) and len(briefing.report_url) <= 1000:
        safe_url = html.escape(briefing.report_url, quote=True)
        lines.append(f"👉 <a href=\"{safe_url}\">[📊 일일 상세 웹 리포트 보기]</a>")
    else:
        lines.append("💡 <i>상세 리포트: GitHub Pages 연결 시 클릭 가능한 웹 링크 버튼이 활성화됩니다. (로컬 실행 시 아래 첨부된 HTML 파일을 눌러 바로 여세요)</i>")

    return "\n".join(lines)


class TelegramChannel:
    def __init__(
        self,
        bot_token: str,
        *,
        include_inline_button: bool = False,
        use_html: bool = False,
        opener=None,
    ):
        if not bot_token:
            raise ValueError("Bot token required")
        self.bot_token = bot_token.strip()
        self.include_inline_button = include_inline_button
        self.use_html = use_html
        self._opener = opener or urllib.request.urlopen

    def _api_call(self, method: str, payload: dict | None = None, timeout: int = 10) -> dict:
        url = f"https://api.telegram.org/bot{self.bot_token}/{method}"
        if payload is None:
            req = urllib.request.Request(url, method="GET")
        else:
            data = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url, data=data, headers={"Content-Type": "application/json"}, method="POST"
            )
        try:
            with self._opener(req, timeout=timeout) as response:
                res = json.load(response)
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_json = json.loads(exc.read().decode("utf-8", errors="ignore"))
                err_body = err_json.get("description", "")
            except Exception:
                err_body = str(exc)
            raise RuntimeError(f"Telegram HTTP {exc.code} ({method}): {err_body}") from exc

        if not res.get("ok"):
            desc = res.get("description", "Telegram API rejected request")
            raise RuntimeError(f"Telegram API error ({method}): {desc}")
        return res

    def get_me(self) -> dict:
        res = self._api_call("getMe")
        return res.get("result", {})

    def discover_chat_ids(self) -> list[dict]:
        res = self._api_call("getUpdates")
        chats: dict[str, dict] = {}
        for item in res.get("result", []):
            msg = item.get("message") or item.get("channel_post") or item.get("my_chat_member", {}).get("chat")
            if not isinstance(msg, dict):
                continue
            chat = msg.get("chat") if "chat" in msg else msg
            if not isinstance(chat, dict) or "id" not in chat:
                continue
            cid = str(chat["id"])
            title = (
                chat.get("title")
                or " ".join(filter(None, [chat.get("first_name"), chat.get("last_name")]))
                or chat.get("username")
                or cid
            )
            chats[cid] = {
                "chat_id": cid,
                "type": chat.get("type", "private"),
                "title": title,
                "username": chat.get("username", ""),
                "last_text": msg.get("text", "") if "chat" in msg else "",
            }
        return list(chats.values())

    def send(self, recipient: str, briefing: Briefing) -> str:
        text = format_telegram_html(briefing) if self.use_html else format_briefing(briefing)
        body: dict = {
            "chat_id": str(recipient).strip(),
            "text": text,
            "disable_web_page_preview": True,
        }
        if self.use_html:
            body["parse_mode"] = "HTML"
        if self.include_inline_button and _is_public_button_url(briefing.report_url):
            body["reply_markup"] = {
                "inline_keyboard": [
                    [{"text": "📊 상세 시황 리포트 웹페이지 열기", "url": briefing.report_url}]
                ]
            }
        data = self._api_call("sendMessage", body)
        return str(data["result"]["message_id"])

    def send_html_Attachment(self, recipient: str, html_path: str | Path, caption: str = "📊 상세 시황 리포트 파일 (터치하여 브라우저로 열기)") -> str:
        """Uploads the standalone HTML report file directly to Telegram chat when no public web URL is configured."""
        path = Path(html_path)
        boundary = f"----MarketBriefBoundary{uuid.uuid4().hex}"
        file_bytes = path.read_bytes()

        parts = []
        # chat_id field
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
                f"{str(recipient).strip()}\r\n"
            ).encode("utf-8")
        )
        # caption field
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="caption"\r\n\r\n'
                f"{caption}\r\n"
            ).encode("utf-8")
        )
        # document field
        parts.append(
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="document"; filename="{path.name}"\r\n'
                f"Content-Type: text/html; charset=utf-8\r\n\r\n"
            ).encode("utf-8")
            + file_bytes
            + b"\r\n"
        )
        parts.append(f"--{boundary}--\r\n".encode("utf-8"))
        body = b"".join(parts)

        url = f"https://api.telegram.org/bot{self.bot_token}/sendDocument"
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
            method="POST",
        )
        try:
            with self._opener(req, timeout=15) as response:
                res = json.load(response)
        except urllib.error.HTTPError as exc:
            err_body = ""
            try:
                err_json = json.loads(exc.read().decode("utf-8", errors="ignore"))
                err_body = err_json.get("description", "")
            except Exception:
                err_body = str(exc)
            raise RuntimeError(f"Telegram sendDocument HTTP {exc.code}: {err_body}") from exc

        if not res.get("ok"):
            raise RuntimeError("Telegram sendDocument failed")
        return str(res["result"]["message_id"])
