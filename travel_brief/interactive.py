"""Interactive On-Demand Handler & Background Dispatcher for Travel Pathfinder Bot (v2.3).
Equipped with:
1. 0.1s Pre-emptive Callback Acknowledgement (TG-04) to prevent spinner timeout.
2. Per-User 3s Debounce Cooldown + In-Progress Flight Lock + per-scope refresh cooldown.
3. Decoupled ThreadPoolExecutor worker pool to prevent poller loop blocking (C3).
4. Scope-isolated refresh: `trv:r:CTS` only invalidates the CTS cache scope, `trv:r:ALL` only curated scopes.
5. Zero-Stuck Loading: per-message `MessageState` (generation guard, active-worker-aware LRU), edit →
   HTML send → plain-text send fallback chain, immediate error card on submit failure, 45s watchdog card.
6. Conversation state machine: commands first, `trv:c:guide` → AWAITING_CITY, free-text city search
   through `CityCatalog` (unknown 3-letter codes rejected), unsupported text → HELP card.
7. Worker-side FX pre-fetch (Fail-Open) injected into the formatter (no network I/O in formatting).
"""
from __future__ import annotations

import concurrent.futures
import html
import io
import json
import logging
import os
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.request
from collections import OrderedDict
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

from travel_brief.channels.telegram import TelegramChannel
from travel_brief.channels.telegram_limiter import SafeTelegramDispatcher
from travel_brief.city_catalog import (
    REGION_CODES,
    REGION_LABELS,
    THEME_LABELS,
    CityCatalog,
    get_watch_country_meta,
    is_country_watch_code,
    is_region_code,
    region_label,
)
from travel_brief.flight_engine import FlightEngine, kst_today, validate_fixed_dates
from travel_brief.budget_calculator import BudgetCalculator
from travel_brief.fx_client import FXClient
from travel_brief.destination_photos import DestinationPhoto, resolve_destination_photo
from travel_brief.models import TravelRecommendation
from travel_brief.naver_live import NaverFlightLiveClient

logger = logging.getLogger(__name__)

_ORIGINAL_URLOPEN = urllib.request.urlopen  # identity check: patched urlopen -> legacy urllib transport

USER_COOLDOWN_SECONDS = 3.0
GLOBAL_REFRESH_COOLDOWN_SECONDS = 30.0  # fast on-demand refresh (~5s) -> short per-scope guard
MAX_TRACKED_MESSAGES = 5000  # LRU bound for per-message state registry
MAX_TRACKED_CHATS = 5000  # LRU bound for conversation states
SSE_RERANK_ROUNDS = 2
WATCHDOG_SECONDS = 45.0

STATE_IDLE = "IDLE"
STATE_AWAITING_CITY = "AWAITING_CITY"
STATE_AWAITING_WATCH_PRICE = "AWAITING_WATCH_PRICE"
CHAT_STATE_TTL_SECONDS = 300.0
MAX_CHAT_STATES = MAX_TRACKED_CHATS
MAX_TRACKED_WATCH_CARDS = 2000

WATCH_PRICE_MIN_KRW = 50_000
WATCH_PRICE_MAX_KRW = 5_000_000
WATCH_QUICK_ADD_CODES = ("NRT", "KIX", "FUK", "TPE", "DAD", "BKK")
WATCH_DAY_LABELS = {"ANY": "전체 요일", "FRI_MON": "금~월", "THU_SUN": "목~일"}
WATCH_PRESET_LABELS = {"DEAL": "실속 특가", "FLOOR": "바닥가", "CUSTOM": "직접 입력", "BASELINE": "평균가 기준"}
CANCEL_COMMANDS = {"/cancel", "/취소"}

# V2 watch wizard (country / horizon / weekday / fixed dates / cadence)
STATE_AWAITING_WATCH_DATES = "AWAITING_WATCH_DATES"
WATCH_DRAFT_TTL_SECONDS = 1800.0
MAX_WATCH_DRAFTS = 2000
WATCH_DEFAULT_DRAFT = {"watch_mode": "FLEX", "horizon_months": 3, "weekday_option": "ANY", "notify_mode": "DROP"}
WATCH_HORIZON_CHOICES = (1, 3, 6, 12)
WATCH_MODE_CODES = {"F": "FLEX", "X": "FIXED"}
WATCH_WEEKDAY_CODES = {"W": "WEEKEND", "D": "WEEKDAY", "A": "ANY"}
WATCH_NOTIFY_CODES = {"D": "DROP", "Y": "DAILY"}
WATCH_WEEKDAY_LABELS = {"WEEKEND": "주말(목~일·금~월)", "WEEKDAY": "평일(월~목·화~금)", "ANY": "주말+평일 전체"}
WATCH_WEEKDAY_SHORT = {"WEEKEND": "주말", "WEEKDAY": "평일", "ANY": "전체"}
WATCH_NOTIFY_LABELS = {"DROP": "목표가 도달·추가 하락 시 즉시", "DAILY": "매일 아침 7:30 브리핑 + 급락 시"}
WATCH_PICKER_CITIES = ("NRT", "KIX", "FUK", "TPE", "DAD", "BKK")
_DOW_KO = ["월", "화", "수", "목", "금", "토", "일"]

# Shared 8-region (4 rows x 2) -> city (3x3 = 9 per page) drilldown for search, watch add and Track A prefs.
STATE_AWAITING_WATCH_CITY = "AWAITING_WATCH_CITY"
REGION_GRID: tuple[tuple[str, str], ...] = (("JP", "TWHK"), ("CNMN", "SEASIA"), ("PAC", "OCE"), ("NA", "EU"))
CITY_PAGE_COLS = 3
CITY_PAGE_SIZE = 9
WATCH_LIMIT_NOTICE = "기존 알림 유지 중 — 새 알림 추가는 4개 미만일 때 가능"
COUNTRY_WATCH_RETIRED_NOTICE = "ℹ️ 국가 전체 알림은 신규 등록이 종료됐어요. 권역에서 도시를 골라 주세요. (기존 알림은 그대로 유지)"
DORMANT_BANNER = "💤 모든 자동 발송 꺼짐 (검색 전용 모드)"
DORMANT_NOTICE = "💤 이제 자동 발송이 모두 꺼졌어요. 도시 검색은 언제든 가능하고, ⚙️ /settings 에서 다시 켤 수 있어요."
WEEKLY_FREQ_CODES = {"T": "THU_1X", "B": "TUE_THU_2X", "O": "OFF"}
WEEKLY_FREQ_BUTTONS = {"THU_1X": "목 주1회", "TUE_THU_2X": "화·목 주2회", "OFF": "끄기"}
WEEKLY_FREQ_STATUS = {"THU_1X": "주1회(목) 12:00", "TUE_THU_2X": "주2회(화·목) 12:00", "OFF": "꺼짐"}
THEME_CB_CODES = {"A": "ALL", "R": "RESORT", "C": "CITY"}
UI_SHUTDOWN_GRACE_SECONDS = 1.5  # shutdown waits this long for in-flight UI edits before closing HTTP sessions
WATCH_CHECK_SCHEDULE = "07:30·12:30·18:30"
WATCH_STEP1_HEADER = "🔔 <b>[맞춤발송 등록 · 1/2단계: 도시 선택]</b>"
WATCH_STEP1_GUIDE = "권역 → 도시를 누르거나, 채팅창에 도시 이름(예: 오사카)을 바로 입력하세요."
WATCH_STEP2_GUIDE = "원하는 목표가를 누르거나 [✍️ 목표가 직접 입력] 후 채팅창에 숫자를 입력하면 저장돼요."

_DATE_TOKEN_RE = re.compile(
    r"(?P<y>\d{4})\s*[-./년]\s*(?P<m>\d{1,2})\s*[-./월]\s*(?P<d>\d{1,2})\s*일?"
    r"|(?P<c8>\d{8})"
    r"|(?P<c6>\d{6})"
    r"|(?P<m2>\d{1,2})\s*[./월]\s*(?P<d2>\d{1,2})\s*일?"
)
_DATE_LEFTOVER_RE = re.compile(r"^[\s~〜\-–—→>,부터까지]*$")
_HAS_DIGIT_RE = re.compile(r"\d")


def _token_to_date(m: re.Match) -> tuple[Optional[tuple[int, int, int]], bool]:
    """(y, m, d) with y=0 when the year was omitted; bool = explicit year."""
    if m.group("y"):
        return (int(m.group("y")), int(m.group("m")), int(m.group("d"))), True
    if m.group("c8"):
        s = m.group("c8")
        return (int(s[:4]), int(s[4:6]), int(s[6:])), True
    if m.group("c6"):
        s = m.group("c6")
        return (2000 + int(s[:2]), int(s[2:4]), int(s[4:])), True
    return (0, int(m.group("m2")), int(m.group("d2"))), False


def parse_watch_date_range(text: str, today: Optional[Any] = None) -> tuple[Optional[Any], Optional[Any], Optional[str]]:
    """Parses `YYYY-MM-DD ~ YYYY-MM-DD`, `YYYY.MM.DD-YYYY.MM.DD`, `YYMMDD YYYYMMDD`, `MM.DD~MM.DD`, `5/1~5/4`.
    A year-less departure is the nearest future date (KST); a year-less return before the departure rolls
    into the next year (`12/30~1/3`). Explicit past dates are rejected, never silently corrected.
    Returns (dep, ret, None) or (None, None, reason)."""
    from datetime import date as _date

    base = today or kst_today()
    raw = unicodedata.normalize("NFKC", text or "").strip()
    tokens = list(_DATE_TOKEN_RE.finditer(raw))
    leftover = _DATE_TOKEN_RE.sub(" ", raw)
    if len(tokens) != 2 or not _DATE_LEFTOVER_RE.match(leftover):
        return None, None, "출발일과 귀국일 두 날짜를 입력해 주세요"
    (dep_t, dep_explicit), (ret_t, ret_explicit) = (_token_to_date(t) for t in tokens)
    try:
        if dep_explicit:
            dep = _date(*dep_t)
        else:
            dep = _date(base.year, dep_t[1], dep_t[2])
            if dep < base:
                dep = _date(base.year + 1, dep_t[1], dep_t[2])
        if ret_explicit:
            ret = _date(*ret_t)
        else:
            ret = _date(dep.year, ret_t[1], ret_t[2])
            if ret < dep:
                ret = _date(dep.year + 1, ret_t[1], ret_t[2])
    except ValueError:
        return None, None, "존재하지 않는 날짜예요"
    reason = validate_fixed_dates(dep, ret, base)
    if reason:
        return None, None, reason
    return dep, ret, None


def _short_iso(value: Optional[str]) -> str:
    from datetime import date as _date

    try:
        d = _date.fromisoformat(value or "")
    except ValueError:
        return value or ""
    return f"{d.month:02d}/{d.day:02d}({_DOW_KO[d.weekday()]})"


def _trip_nights_iso(dep: Optional[str], ret: Optional[str]) -> int:
    from datetime import date as _date

    try:
        return (_date.fromisoformat(ret or "") - _date.fromisoformat(dep or "")).days
    except ValueError:
        return 0


@dataclass
class WatchDraft:
    """Per-(chat, user, scope) wizard choices; saved to the DB only on an explicit confirm."""
    scope: str
    watch_mode: str = "FLEX"
    horizon_months: int = 3
    weekday_option: str = "ANY"
    notify_mode: str = "DROP"
    fixed_dep_date: str = ""
    fixed_ret_date: str = ""
    expires_at: float = 0.0


_PRICE_TEXT_RE = re.compile(r"^(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d{1,4}))?(만)?(?:원)?(?:이하|미만)?$")
_PRICE_LIKE_RE = re.compile(r"^[-+]?[\d,.]+(?:만)?(?:원)?(?:이하|미만)?$")

CITY_CODE_ALIASES: dict[str, str] = {
    "TYO": "NRT",
    "OSA": "KIX",
}

CITY_LABELS: dict[str, str] = {
    "NRT": "🇯🇵 도쿄",
    "KIX": "🇯🇵 오사카",
    "FUK": "🇯🇵 후쿠오카",
    "TPE": "🇹🇼 타이베이",
    "DAD": "🇻🇳 다낭",
    "BKK": "🇹🇭 방콕",
    "ALL": "✈️ 전체 주말 특가 TOP 3",
}

START_COMMANDS = {"/start", "/구독"}
START_WORDS = {"안녕", "시작"}
STOP_COMMANDS = {"/stop", "/unsubscribe", "/구독취소"}
SEARCH_ALL_COMMANDS = {"/여행", "/today", "/특가", "/all"}
HELP_COMMANDS = {"/help", "/도움말"}
GUIDE_COMMANDS = {"/검색", "/search", "/guide"}
MENU_COMMANDS = {"/menu", "/메뉴"}
WATCH_COMMANDS = {"/watch", "/알림"}
SETTINGS_COMMANDS = {"/settings", "/설정"}
# Main-menu navigation callbacks (`trv:m:<sub>`); anything else is rejected, never routed to search.
MENU_SUBS = frozenset({"home", "search", "watch", "settings", "popular", "help"})
POPULAR_CITY_ROWS = (("NRT", "KIX", "FUK"), ("TPE", "DAD", "BKK"))
ALL_WORDS = {"전체", "all", "전체특가", "top3"}

_TAG_RE = re.compile(r"<[^>]+>")
_MENTION_RE = re.compile(r"(^|\s)@[A-Za-z0-9_]{3,}")


def html_to_plain(text: str) -> str:
    """Plain-text rendering for the last-resort `parse_mode=""` fallback."""
    return html.unescape(_TAG_RE.sub("", text or ""))


def _compact_price_text(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text or ""))


def looks_like_price_text(text: str) -> bool:
    """True for number-ish input (`25`, `25만원`, `-3`, `2,500`) that must be answered by the price parser."""
    return bool(_PRICE_LIKE_RE.match(_compact_price_text(text)))


def parse_watch_price_krw(text: str) -> Optional[int]:
    """Parses a target fare. `25` / `25만` / `25.5만원` (<= 999, 만원 unit) and `250000` / `250,000원`
    (>= 10,000 won) are accepted; ambiguous 1,000~9,999 values, negatives, zero, and anything outside
    [50,000, 5,000,000] KRW return None.
    """
    m = _PRICE_TEXT_RE.match(_compact_price_text(text))
    if not m:
        return None
    int_part, frac, man = m.group(1), m.group(2), m.group(3)
    has_comma = "," in int_part
    try:
        number = Decimal(int_part.replace(",", "") + (f".{frac}" if frac else ""))
    except InvalidOperation:
        return None
    if man:
        value = number * 10_000
    elif number <= 999 and not has_comma:
        value = number * 10_000
    elif number >= 10_000 and not frac:
        value = number
    else:
        return None  # e.g. "2,500" / "1000": unit is ambiguous
    if value != value.to_integral_value():
        return None
    won = int(value)
    if won < WATCH_PRICE_MIN_KRW or won > WATCH_PRICE_MAX_KRW:
        return None
    return won


def _man_label(krw: int) -> str:
    man = krw / 10_000
    return f"{man:,.0f}만 원" if man == int(man) else f"{man:,.1f}만 원"


def callback_fits(data: str) -> bool:
    return len(data.encode("utf-8")) <= 64


@dataclass
class ChatState:
    state: str
    expires_at: float
    dest_code: str = ""


@dataclass
class MessageState:
    generation: int = 0
    phase: str = "LOADING"  # LOADING | DONE | TIMEOUT
    lock: threading.Lock = field(default_factory=threading.Lock)
    active_workers: int = 0
    deadline: float = 0.0
    done_generation: int = 0
    watchdog: Optional[threading.Timer] = None


class UserCooldownManager:
    """Thread-safe cooldown and in-progress tracker using monotonic clocks."""

    def __init__(self, cooldown_sec: float = USER_COOLDOWN_SECONDS):
        self.cooldown_sec = cooldown_sec
        self._lock = threading.Lock()
        self._last_click: dict[str, float] = {}
        self._in_progress_users: set[str] = set()
        self._last_refresh: dict[str, float] = {}

    def check_and_acquire(
        self,
        user_id: str,
        is_force_refresh: bool = False,
        is_admin: bool = False,
        scope: str = "ALL",
    ) -> tuple[bool, str]:
        """Checks if request is allowed. Returns (allowed, toast_message)."""
        now = time.monotonic()
        scope = (scope or "ALL").upper()
        with self._lock:
            # 1. Check if user already has an active query in progress
            if user_id in self._in_progress_users:
                return False, "이미 최신 정보를 수집 중입니다. 잠시만 기다려주세요."

            # 2. Check per-user 3s cooldown
            last_ts = self._last_click.get(user_id, 0.0)
            if (now - last_ts) < self.cooldown_sec:
                remain = int(self.cooldown_sec - (now - last_ts)) + 1
                return False, f"방금 조회되었습니다. {remain}초 후 다시 시도해주세요."

            # 3. Per-scope force-refresh cooldown (admins bypass)
            if is_force_refresh and not is_admin:
                elapsed = now - self._last_refresh.get(scope, float("-inf"))
                if elapsed < GLOBAL_REFRESH_COOLDOWN_SECONDS:
                    wait_sec = int(GLOBAL_REFRESH_COOLDOWN_SECONDS - elapsed) + 1
                    label = "전체" if scope == "ALL" else scope
                    return False, f"{label} 새로고침은 {wait_sec}초 후에 가능합니다 (기존 캐시 제공)."

            # Acquire state
            self._last_click[user_id] = now
            self._in_progress_users.add(user_id)
            if is_force_refresh:
                self._last_refresh[scope] = now

            return True, "✈️ 최신 실시간 항공권을 조회 중입니다..."

    def release(self, user_id: str) -> None:
        """Releases the in-progress lock for the user."""
        with self._lock:
            self._in_progress_users.discard(user_id)


def parse_text_update(msg: dict[str, Any]) -> tuple[str, int, str, str, dict[str, Any], str]:
    """Shared extraction for standalone & manager routing: (chat_id, message_id, user_id, text, from_user, name)."""
    chat = msg.get("chat", {}) or {}
    sender = msg.get("from", {}) or chat
    chat_id = str(chat.get("id") or sender.get("id") or "")
    user_id = str(sender.get("id") or chat_id)
    text = (msg.get("text") or "").strip()
    name = chat.get("title") or chat.get("first_name") or sender.get("first_name") or f"사용자_{chat_id[-4:]}"
    return chat_id, int(msg.get("message_id") or 0), user_id, text, sender, name


def dispatch_travel_update(
    handler: "TravelInteractiveHandler",
    item: dict[str, Any],
    on_subscribe: Optional[Callable[[str, str], Any]] = None,
    on_unsubscribe: Optional[Callable[[str], Any]] = None,
) -> tuple[bool, str]:
    """Single routing entry used by both `travel_bot_runner --poll` and `DynamicBotManager`."""
    if "callback_query" in item:
        cq = item["callback_query"]
        cb_data = str(cq.get("data") or "")
        if not cb_data.startswith("trv:"):
            return False, "not_travel_callback"
        sender = cq.get("from", {}) or {}
        msg = cq.get("message", {}) or {}
        chat = msg.get("chat", {}) or sender
        handler.handle_callback_query(
            cq_id=str(cq.get("id", "")),
            chat_id=str(chat.get("id") or sender.get("id") or ""),
            message_id=int(msg.get("message_id") or 0),
            callback_data=cb_data,
            from_user=sender,
        )
        return True, "travel_callback_handled"

    if "message" in item:
        chat_id, message_id, user_id, text, sender, name = parse_text_update(item["message"] or {})
        if not chat_id or not text:
            return False, "empty_message"
        handled, reason = handler.handle_text_message(
            chat_id,
            message_id,
            user_id,
            text,
            sender,
            sender_name=name,
            on_subscribe=on_subscribe,
            on_unsubscribe=on_unsubscribe,
        )
        return (True, "travel_message_handled") if handled else (False, reason)

    return False, "not_travel_interactive_event"


class TravelInteractiveHandler:
    """Manages Telegram callback queries & text messages for the Travel Bot without blocking polling."""

    def __init__(
        self,
        bot_token: str,
        engine: Optional[FlightEngine] = None,
        max_workers: int = 2,
        report_url: str = "http://161.33.196.211:8002/reports/travel_latest.html",
        fx_client: Optional[FXClient] = None,
        fx_enabled: Optional[bool] = None,
        db: Any = None,
        db_path: Optional[str] = None,
    ):
        self.bot_token = bot_token
        if db is None and db_path:
            from travel_brief.storage.db import TravelBriefDatabase
            db = TravelBriefDatabase(db_path=str(db_path))
        self.db = db  # TravelBriefDatabase for price watches (None -> watch menu answers "unavailable")
        self.engine = engine or FlightEngine()
        self.report_url = report_url
        self.cooldown = UserCooldownManager()
        self.worker_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="TravelWorker"
        )
        # Separate tiny pool for lightweight UI edits (loading cards) so they never queue behind scrapes.
        self.ui_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=4, thread_name_prefix="TravelUI"
        )
        # Lazily-created keep-alive HTTP session for editMessageText (reuses TLS to api.telegram.org).
        self._http_session: Any = None
        self._http_session_lock = threading.Lock()
        self.tg = TelegramChannel(bot_token=bot_token)
        self.dispatcher = SafeTelegramDispatcher()
        self._admin_ids = self._load_admin_ids()
        self.catalog = CityCatalog(getattr(self.engine, "destinations", {}) or {})
        self.fx_client = fx_client or FXClient()
        # FX is only fetched in live mode (offline demo/tests never touch the network).
        self.fx_enabled = bool(getattr(self.engine, "live_naver", False)) if fx_enabled is None else bool(fx_enabled)
        self.watchdog_seconds = WATCHDOG_SECONDS
        self.max_tracked_messages = MAX_TRACKED_MESSAGES
        # Per-message state: (chat_id, message_id) -> MessageState (generation guard + edit lock).
        self._seq_lock = threading.Lock()
        self._msg_states: OrderedDict[tuple[str, int], MessageState] = OrderedDict()
        # Conversation state: (chat_id, user_id) -> ChatState (TTL, bounded LRU)
        self._chat_states: OrderedDict[tuple[str, str], ChatState] = OrderedDict()
        self.chat_state_ttl = CHAT_STATE_TTL_SECONDS
        self._watch_cards: OrderedDict[tuple[str, int], None] = OrderedDict()
        # Watch wizard drafts: (chat_id, user_id, scope) -> WatchDraft (TTL, bounded LRU)
        self._watch_drafts: OrderedDict[tuple[str, str, str], WatchDraft] = OrderedDict()
        self.watch_draft_ttl = WATCH_DRAFT_TTL_SECONDS
        # Best-effort city detail photo only with a real-looking bot token (`<id>:<secret>`), so dummy-token
        # tests / offline tools never upload anything.
        self._photo_enabled = bool(re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", str(bot_token or "").strip()))
        self._closed = False

    def _load_admin_ids(self) -> set[str]:
        raw = os.environ.get("ADMIN_TELEGRAM_ID", "") or os.environ.get("TELEGRAM_CHAT_ID", "")
        return {r.strip() for r in raw.split(",") if r.strip()}

    # ------------------------------------------------------------------
    # Labels & keyboards
    # ------------------------------------------------------------------
    def normalize_filter(self, raw: Optional[str]) -> Optional[str]:
        """Returns "ALL", a catalog code, or None for anything unsupported (unknown codes rejected)."""
        token = (raw or "").strip()
        if not token or token.upper() == "ALL":
            return "ALL"
        res = self.catalog.resolve(CITY_CODE_ALIASES.get(token.upper(), token))
        return res.code if res else None

    def label_for(self, code: str) -> str:
        country = get_watch_country_meta(code)
        if country is not None:
            return country["label"]
        if code in CITY_LABELS:
            return CITY_LABELS[code]
        res = self.catalog.get(code)
        return res.display_name if res else code

    def build_inline_keyboard(self, current_filter: str = "ALL", chat_id: Optional[str] = None) -> dict[str, Any]:
        """Constructs the slim 4-row result keyboard within 64-byte callback_data limits.
        Contract: rows[0][0] = `trv:r:<scope>`, rows[-3][0] = watch entry, rows[-2][0] = `trv:c:guide`,
        rows[-1][0] = web report URL. City shortcuts live in the `trv:m:popular` submenu.
        `chat_id` (optional) only personalises the watch button badge.
        """
        scope = self.normalize_filter(current_filter) or "ALL"
        return {
            "inline_keyboard": [
                [
                    {"text": "🔄 최신 특가 새로고침", "callback_data": f"trv:r:{scope}"},
                ],
                [
                    self._watch_entry_button(scope, chat_id),
                    {"text": "⚙️ 정기발송 설정", "callback_data": "trv:m:settings"},
                ],
                [
                    {"text": "🔍 권역·도시 찾기", "callback_data": "trv:c:guide"},
                    {"text": "🏠 메뉴", "callback_data": "trv:m:home"},
                ],
                [
                    {"text": "📊 6개월 특가 웹 리포트 ↗", "url": self.report_url},
                ],
            ]
        }

    # ------------------------------------------------------------------
    # Main menu hub & submenus (`trv:m:*`)
    # ------------------------------------------------------------------
    @staticmethod
    def _norm_region_theme(prefs: dict[str, Any]) -> tuple[str, str]:
        region = str(prefs.get("weekly_brief_region") or "ALL").upper()
        theme = str(prefs.get("weekly_brief_theme") or "ALL").upper()
        return (region if region in REGION_LABELS else "ALL"), (theme if theme in THEME_LABELS else "ALL")

    def _format_status_badge(self, chat_id: Optional[str]) -> list[str]:
        """2-line live status (📰 정기발송 / 🔔 맞춤발송) for /start and /menu; [] without a chat or DB.
        0 active watches always reads `미등록` (never `일시중지`/`받는 중`), even when the paused flag is set."""
        if not chat_id or self.db is None:
            return []
        from travel_brief.storage.db import WATCH_MAX_ACTIVE_PER_CHAT

        cid = str(chat_id)
        prefs = self._get_prefs(cid)
        freq = self._freq_of(prefs)
        if freq != "OFF":
            region, theme = self._norm_region_theme(prefs)
            weekly = (
                f"🟢 받는 중 ({WEEKLY_FREQ_STATUS[freq]} · {html.escape(region_label(region))} · "
                f"{THEME_LABELS[theme]})"
            )
        else:
            weekly = "🔴 꺼짐"
        n_watch = self._active_watch_count(cid)
        if n_watch == 0:
            watch = f"⚪ 미등록 (도시 0/{WATCH_MAX_ACTIVE_PER_CHAT})"
        elif prefs.get("watches_paused", False):
            watch = f"⏸️ 일시중지 ({n_watch}/{WATCH_MAX_ACTIVE_PER_CHAT})"
        else:
            watch = f"🟢 받는 중 ({n_watch}/{WATCH_MAX_ACTIVE_PER_CHAT})"
        return [f"📰 정기발송: <b>{weekly}</b>", f"🔔 맞춤발송: <b>{watch}</b>"]

    def build_menu_hub_card(self, chat_id: Optional[str] = None) -> tuple[str, dict[str, Any]]:
        lines = ["🧭 <b>[트래블 패스파인더 메인 메뉴]</b>", "━━━━━━━━━━━━━━━━━━"]
        badge = self._format_status_badge(chat_id)
        if badge:
            lines.extend(badge)
            lines.append("━━━━━━━━━━━━━━━━━━")
        lines.extend([
            "⚙️ <b>정기발송 설정</b>: 발송 주기·관심 권역·여행 스타일 변경",
            "🔔 <b>맞춤발송 설정</b>: 인천 직항 116개 도시 · 8대 권역 목표가 알림 관리",
            "✈️ <b>지금 특가 보기</b>: 전체 주말 특가 TOP 3",
            "🔍 <b>권역·도시 찾기</b>: 8대 권역별 직항 도시 탐색",
            "🏙️ <b>인기 도시</b>: 도쿄·오사카·후쿠오카·타이베이·다낭·방콕",
            "💡 도시 이름을 채팅창에 바로 입력해도 검색돼요.",
        ])
        markup = {
            "inline_keyboard": [
                [
                    {"text": "⚙️ 정기발송 설정", "callback_data": "trv:m:settings"},
                    {"text": "🔔 맞춤발송 설정", "callback_data": "trv:m:watch"},
                ],
                [
                    {"text": "✈️ 지금 특가 보기", "callback_data": "trv:m:search"},
                    {"text": "🔍 권역·도시 찾기", "callback_data": "trv:c:guide"},
                ],
                [
                    {"text": "🏙️ 인기 도시", "callback_data": "trv:m:popular"},
                    {"text": "📊 웹 리포트 ↗", "url": self.report_url},
                ],
            ]
        }
        return "\n".join(lines), markup

    def build_popular_cities_card(self) -> tuple[str, dict[str, Any]]:
        text = (
            "🏙️ <b>[인기 도시 실시간 특가]</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "도시를 누르면 6개월 주말 최저가·예산을 바로 조회해요.\n"
            "🔍 목록에 없는 곳은 [권역·도시 찾기]로 찾아보세요."
        )
        rows: list[list[dict[str, Any]]] = [
            [{"text": self.label_for(code), "callback_data": f"trv:q:{code}"} for code in row]
            for row in POPULAR_CITY_ROWS
        ]
        rows.append([
            {"text": "🔍 권역·도시 찾기", "callback_data": "trv:c:guide"},
            {"text": "🏠 메뉴", "callback_data": "trv:m:home"},
        ])
        return text, {"inline_keyboard": rows}

    def build_delivery_settings_card(self, chat_id: str, notice: str = "") -> tuple[str, dict[str, Any]]:
        """Track A only (1:1 with 🔔 맞춤발송 설정): 📰 주기 / 🧭 관심권역 / 🎯 여행스타일, one button row each."""
        cid = str(chat_id)
        prefs = self._get_prefs(cid)
        freq = self._freq_of(prefs)
        region, theme = self._norm_region_theme(prefs)
        lines = [DORMANT_BANNER] if self._is_dormant(cid, prefs) else []
        if notice:
            lines.append(notice)
        status = "🟢 받는 중" if freq != "OFF" else "🔴"
        lines += [
            "📰 <b>[정기발송 설정]</b>",
            "━━━━━━━━━━━━━━━━━━",
            "원하는 주기·권역·스타일을 누르면 바로 저장돼요.",
            f"📰 정기발송: <b>{WEEKLY_FREQ_STATUS[freq]}</b> {status}",
            f"🧭 관심권역: <b>{html.escape(region_label(region))}</b>",
            f"🎯 여행스타일: <b>{THEME_LABELS[theme]}</b>",
        ]

        def mark(selected: bool, text: str) -> str:
            return f"✅ {text}" if selected else text

        region_btn = "🧭 권역 고르기" if region == "ALL" else f"✅ {REGION_LABELS[region]} (변경)"
        markup = {
            "inline_keyboard": [
                [
                    {"text": mark(freq == f, WEEKLY_FREQ_BUTTONS[f]), "callback_data": f"trv:w:fq:{c}"}
                    for c, f in WEEKLY_FREQ_CODES.items()
                ],
                [
                    {"text": mark(region == "ALL", "🌏 전체"), "callback_data": "trv:w:rgn:ALL"},
                    {"text": region_btn, "callback_data": "trv:w:rgp"},
                ],
                [
                    {"text": mark(theme == t, THEME_LABELS[t]), "callback_data": f"trv:w:th:{c}"}
                    for c, t in THEME_CB_CODES.items()
                ],
                [
                    {"text": "🔔 맞춤발송 설정", "callback_data": "trv:m:watch"},
                    {"text": "🏠 메뉴", "callback_data": "trv:m:home"},
                ],
            ]
        }
        return "\n".join(lines), markup

    def build_pref_region_card(self, chat_id: str) -> tuple[str, dict[str, Any]]:
        """Track A interest-region picker (same 4x2 grid as search / watch)."""
        prefs = self._get_prefs(str(chat_id))
        current = str(prefs.get("weekly_brief_region") or "ALL").upper()
        text = (
            "🧭 <b>[관심권역 선택]</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"현재: <b>{html.escape(region_label(current))}</b>\n"
            "정기발송 TOP 3를 고른 권역 기준으로 추천해 드려요."
        )
        rows = self.build_region_grid("p")
        rows.append([
            {"text": "🌏 전체", "callback_data": "trv:w:rgn:ALL"},
            {"text": "↩️ 정기발송 설정", "callback_data": "trv:m:settings"},
        ])
        return text, {"inline_keyboard": rows}

    # ------------------------------------------------------------------
    # Shared 4x2 region grid -> 3x3 paginated city pages
    # ------------------------------------------------------------------
    @staticmethod
    def build_region_grid(mode: str) -> list[list[dict[str, Any]]]:
        """`s` search (`trv:srg:<REG>:0`), `w` watch add (`trv:w:rg:<REG>:0`), `p` Track A pref (`trv:w:rgn:<REG>`)."""
        def cb(reg: str) -> str:
            if mode == "s":
                return f"trv:srg:{reg}:0"
            if mode == "w":
                return f"trv:w:rg:{reg}:0"
            return f"trv:w:rgn:{reg}"

        return [[{"text": REGION_LABELS[r], "callback_data": cb(r)} for r in pair] for pair in REGION_GRID]

    def build_city_page_card(
        self, mode: str, region: str, page: int, chat_id: Optional[str] = None
    ) -> tuple[str, dict[str, Any]]:
        """3x3 city page of one region (popularity first, then 가나다). `s` -> `trv:q:<CODE>` search;
        `w` -> `trv:w:ct:<REG>:<PAGE>:<CODE>` watch setup. Nav: ◀ 이전 / 다음 ▶ / ↩️ 돌아가기."""
        codes = self.catalog.cities_in_region(region)
        pages = max(1, -(-len(codes) // CITY_PAGE_SIZE))
        page = min(max(0, int(page)), pages - 1)
        chunk = codes[page * CITY_PAGE_SIZE:(page + 1) * CITY_PAGE_SIZE]
        watched: set[str] = set()
        if mode == "w" and chat_id and self.db is not None:
            try:
                watched = {w["dest_code"] for w in self.db.list_travel_watches(chat_id=str(chat_id), active_only=True)}
            except Exception:
                watched = set()

        def city_btn(code: str) -> dict[str, Any]:
            res = self.catalog.get(code)
            name = res.meta.name if res else code
            if mode == "w":
                text = f"🔔 {name}" if code in watched else name
                return {"text": text, "callback_data": f"trv:w:ct:{region}:{page}:{code}"}
            return {"text": name, "callback_data": f"trv:q:{code}"}

        buttons = [city_btn(c) for c in chunk]
        rows: list[list[dict[str, Any]]] = [
            buttons[i:i + CITY_PAGE_COLS] for i in range(0, len(buttons), CITY_PAGE_COLS)
        ]
        page_cb = (lambda p: f"trv:srg:{region}:{p}") if mode == "s" else (lambda p: f"trv:w:rg:{region}:{p}")
        nav: list[dict[str, Any]] = []
        if page > 0:
            nav.append({"text": "◀ 이전", "callback_data": page_cb(page - 1)})
        if page < pages - 1:
            nav.append({"text": "다음 ▶", "callback_data": page_cb(page + 1)})
        if nav:
            rows.append(nav)
        rows.append([{"text": "↩️ 돌아가기", "callback_data": "trv:srg" if mode == "s" else "trv:w:rg"}])
        region_name = html.escape(REGION_LABELS.get(region, region))
        if mode == "s":
            text = (
                f"🗺️ <b>[{region_name} 도시 선택]</b> ({page + 1}/{pages})\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "도시를 누르면 실시간 특가를 바로 조회해요. (인기순 · 가나다순)"
            )
        else:
            text = (
                f"{WATCH_STEP1_HEADER}\n"
                f"🗺️ {region_name} 도시 선택 ({page + 1}/{pages})\n"
                "━━━━━━━━━━━━━━━━━━\n"
                "알림 받을 도시를 누르면 다음 단계에서 목표가를 정해요. (인기순 · 가나다순)"
            )
        return text, {"inline_keyboard": rows}

    def _handle_menu_callback(self, cq_id: str, chat_id: str, message_id: int, sub: str) -> None:
        """Non-search `trv:m:*` navigation (read-only): UI edit submitted first, ack in parallel (TG-04)."""
        if sub in ("watch", "settings") and self.db is None:
            self._answer(cq_id, "⚠️ 특가 알림 기능을 일시적으로 사용할 수 없습니다.")
            return
        self._submit_nav_then_ack(cq_id, "", self._run_menu_action, chat_id, message_id, sub)

    def _run_menu_action(self, chat_id: str, message_id: int, sub: str) -> None:
        in_place = self._is_watch_card(chat_id, message_id)
        try:
            if sub == "home":
                text, markup = self.build_menu_hub_card(chat_id)
            elif sub == "popular":
                text, markup = self.build_popular_cities_card()
            elif sub == "help":
                text, markup = self.build_help_card(), self.build_inline_keyboard("ALL", chat_id=chat_id)
            elif sub == "watch":
                text, markup = self.build_watch_list_card(chat_id)
            elif sub == "settings":
                text, markup = self.build_delivery_settings_card(chat_id)
            else:
                return
        except Exception as exc:
            logger.error("Menu action %s failed for %s: %s", sub, chat_id, exc, exc_info=True)
            text = "⚠️ 메뉴를 불러오는 중 일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
            markup = {"inline_keyboard": [[{"text": "🏠 메뉴", "callback_data": "trv:m:home"}]]}
        self._render_watch_card(chat_id, message_id, text, markup, edit=in_place)

    def _watch_entry_button(self, scope: str, chat_id: Optional[str] = None) -> dict[str, Any]:
        """ALL never registers a watch: it opens the per-city list. A city opens its own watch card."""
        if scope == "ALL":
            return {"text": "🔔 맞춤발송 설정", "callback_data": "trv:w:l"}
        watch = self._get_watch(chat_id, scope) if chat_id else None
        if watch and watch.get("is_active"):
            text = f"🔔 알림 설정중 ({_man_label(int(watch['target_price_krw']))})"
        else:
            text = "🔔 이 도시 특가 알림 설정"
        return {"text": text, "callback_data": f"trv:w:m:{scope}"}

    def _get_watch(self, chat_id: Optional[str], code: str) -> Optional[dict[str, Any]]:
        if self.db is None or not chat_id:
            return None
        try:
            return self.db.get_travel_watch(str(chat_id), code)
        except Exception as exc:
            logger.warning("Watch lookup failed for %s/%s: %s", chat_id, code, exc)
            return None

    def build_onboarding_keyboard(self) -> dict[str, Any]:
        """/start onboarding keyboard (3 rows / 6 buttons), aligned with the /menu hub.
        Legacy `trv:s:<CITY>` callbacks from old onboarding messages are still handled in `handle_callback`.
        """
        return {
            "inline_keyboard": [
                [
                    {"text": "⚙️ 정기발송 설정", "callback_data": "trv:m:settings"},
                    {"text": "🔔 맞춤발송 설정", "callback_data": "trv:m:watch"},
                ],
                [
                    {"text": "✈️ 지금 특가 보기", "callback_data": "trv:q:ALL"},
                    {"text": "🔍 권역·도시 찾기", "callback_data": "trv:c:guide"},
                ],
                [
                    {"text": "📊 웹 리포트 ↗", "url": self.report_url},
                    {"text": "🏠 통합 메뉴봇 ↗", "url": "https://t.me/KwMenu_bot?start=from_sub"},
                ],
            ]
        }

    def build_selected_target_keyboard(self, target_city: str = "ALL") -> dict[str, Any]:
        """Step 2 keyboard shown right after target selection, exposing [📋 직전 발송 특가 정보 보기]."""
        city_norm = self.normalize_filter(target_city) or "ALL"
        label = self.label_for(city_norm)
        return {
            "inline_keyboard": [
                [
                    {
                        "text": f"📋 [{label}] 직전 발송 특가 정보 보기",
                        "callback_data": f"trv:q:{city_norm}"[:64],
                    }
                ],
                [
                    {"text": "🇯🇵 도쿄", "callback_data": "trv:s:NRT"},
                    {"text": "🇯🇵 오사카", "callback_data": "trv:s:KIX"},
                    {"text": "🇯🇵 후쿠오카", "callback_data": "trv:s:FUK"},
                ],
                [
                    {"text": "🇹🇼 타이베이", "callback_data": "trv:s:TPE"},
                    {"text": "🇻🇳 다낭", "callback_data": "trv:s:DAD"},
                    {"text": "🇹🇭 방콕", "callback_data": "trv:s:BKK"},
                ],
                [
                    {"text": "📊 6개월 특가 웹 리포트 ↗", "url": self.report_url},
                ],
                [
                    {"text": "🏠 통합 메뉴봇 바로가기", "url": "https://t.me/KwMenu_bot?start=from_sub"},
                ],
            ]
        }

    # ------------------------------------------------------------------
    # Per-message state (generation guard, active-worker-aware LRU)
    # ------------------------------------------------------------------
    def _state_locked(self, key: tuple[str, int]) -> MessageState:
        st = self._msg_states.get(key)
        if st is None:
            st = MessageState()
            self._msg_states[key] = st
            self._evict_locked(keep=key)
        return st

    def _evict_locked(self, keep: Optional[tuple[str, int]] = None) -> None:
        """LRU eviction that never drops a message with an active worker (its lock must stay unique)."""
        if len(self._msg_states) <= self.max_tracked_messages:
            return
        for k in list(self._msg_states.keys()):
            if len(self._msg_states) <= self.max_tracked_messages:
                break
            st = self._msg_states[k]
            if st.active_workers > 0 or k == keep:
                continue
            if st.watchdog is not None:
                st.watchdog.cancel()
            del self._msg_states[k]

    def _next_request_seq(self, chat_id: str, message_id: int) -> int:
        """Registers a new accepted request for this message and returns its generation (O(1))."""
        key = (str(chat_id), int(message_id or 0))
        with self._seq_lock:
            st = self._state_locked(key)
            st.generation += 1
            st.phase = "LOADING"
            st.deadline = time.monotonic() + self.watchdog_seconds
            if st.watchdog is not None:
                st.watchdog.cancel()
                st.watchdog = None
            self._msg_states.move_to_end(key)
            self._evict_locked(keep=key)
            return st.generation

    def _is_latest_seq(self, chat_id: str, message_id: int, seq: Optional[int]) -> bool:
        if seq is None:
            return True
        with self._seq_lock:
            st = self._msg_states.get((str(chat_id), int(message_id or 0)))
            return st is not None and st.generation == seq

    def _get_msg_lock(self, chat_id: str, message_id: int) -> threading.Lock:
        key = (str(chat_id), int(message_id or 0))
        with self._seq_lock:
            return self._state_locked(key).lock

    def _worker_enter(self, key: tuple[str, int]) -> None:
        with self._seq_lock:
            self._state_locked(key).active_workers += 1

    def _worker_exit(self, key: tuple[str, int]) -> None:
        with self._seq_lock:
            st = self._msg_states.get(key)
            if st is not None and st.active_workers > 0:
                st.active_workers -= 1
            self._evict_locked()

    def _edit_if_latest(
        self,
        chat_id: str,
        message_id: int,
        request_seq: Optional[int],
        text: str,
        reply_markup: Optional[dict[str, Any]] = None,
        is_loading: bool = False,
    ) -> bool:
        """Edits the message only if `request_seq` is still the newest request for it.
        "Check generation then edit" runs under the per-message lock, so an older result can never land
        after a newer one, and a loading card never overwrites the final result of the same request.
        Returns True when the edit (or its send fallback) was delivered.
        """
        if request_seq is None:
            if is_loading:
                return self._edit_in_place_only(chat_id, message_id, text, reply_markup) is not False
            return self._safe_edit_message(
                chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup
            ) is not False

        key = (str(chat_id), int(message_id or 0))
        with self._get_msg_lock(chat_id, message_id):
            if not self._is_latest_seq(chat_id, message_id, request_seq):
                logger.info("Skipping superseded edit for %s (seq=%s, loading=%s).", key, request_seq, is_loading)
                return False
            if is_loading:
                with self._seq_lock:
                    st = self._msg_states.get(key)
                    if st is not None and st.done_generation >= request_seq:
                        return False
                return self._edit_in_place_only(chat_id, message_id, text, reply_markup) is not False
            ok = self._safe_edit_message(
                chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup
            ) is not False
            if ok:
                with self._seq_lock:
                    st = self._msg_states.get(key)
                    if st is not None:
                        st.done_generation = max(st.done_generation, request_seq)
                        st.phase = "DONE"
            return ok

    # ------------------------------------------------------------------
    # Watchdog
    # ------------------------------------------------------------------
    def _arm_watchdog(self, chat_id: str, message_id: int, seq: int, target: str, user_id: str) -> None:
        if self._closed or self.watchdog_seconds <= 0:
            return
        key = (str(chat_id), int(message_id or 0))
        timer = threading.Timer(
            self.watchdog_seconds, self._watchdog_fire, args=(chat_id, message_id, seq, target, user_id)
        )
        timer.daemon = True
        with self._seq_lock:
            st = self._state_locked(key)
            if st.generation != seq:
                return
            if st.watchdog is not None:
                st.watchdog.cancel()
            st.watchdog = timer
        timer.start()

    def _disarm_watchdog(self, key: tuple[str, int], seq: Optional[int]) -> None:
        with self._seq_lock:
            st = self._msg_states.get(key)
            if st is None or (seq is not None and st.generation != seq):
                return
            if st.phase == "DONE" and st.watchdog is not None:
                st.watchdog.cancel()
                st.watchdog = None

    def _watchdog_fire(self, chat_id: str, message_id: int, seq: int, target: str, user_id: str) -> None:
        key = (str(chat_id), int(message_id or 0))
        with self._seq_lock:
            st = self._msg_states.get(key)
            if st is None or st.generation != seq or st.phase == "DONE":
                return
        logger.warning("Travel watchdog fired for %s (seq=%s, target=%s).", key, seq, target)
        self.cooldown.release(user_id)
        delivered = self._edit_if_latest(
            chat_id, message_id, seq, self.build_timeout_card(target), self.build_inline_keyboard(target, chat_id=chat_id)
        )
        if delivered:
            with self._seq_lock:
                st = self._msg_states.get(key)
                if st is not None and st.generation == seq:
                    st.phase = "TIMEOUT"
                    st.watchdog = None

    # ------------------------------------------------------------------
    # Transient cards (TG-01/TG-02: short, fully escaped)
    # ------------------------------------------------------------------
    def build_loading_card(self, target_city: str, is_refresh: bool = False) -> str:
        if is_refresh:
            label = "🔄 전체 최신 특가" if target_city == "ALL" else f"🔄 {self.label_for(target_city)}"
        else:
            label = self.label_for(target_city)
        return (
            f"✈️ <b>[{html.escape(label)} 실시간 특가 검색 중...]</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⏳ 네이버 항공권 실시간 최저가를 조회하고 있습니다.\n"
            f"💡 조회가 끝나면 최신 요금과 예약 링크로 자동 갱신됩니다."
        )

    def build_error_card(self, target_city: str, reason: str) -> str:
        label = self.label_for(target_city)
        return (
            f"⚠️ <b>[{html.escape(label)} 특가 조회 실패]</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{html.escape(reason)}\n"
            f"🔄 잠시 후 아래 버튼으로 다시 시도해 주세요."
        )

    def build_timeout_card(self, target_city: str) -> str:
        label = self.label_for(target_city)
        return (
            f"⌛ <b>[{html.escape(label)} 조회 시간 초과]</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"네이버 항공권 응답이 지연되고 있습니다 ({int(self.watchdog_seconds)}초 초과).\n"
            f"🔄 잠시 후 아래 버튼으로 다시 시도해 주세요."
        )

    def build_city_guide_card(self) -> str:
        names = " · ".join(self.catalog.suggestion_names(8))
        return (
            "🔍 <b>[기타 지역 직접 검색]</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "가고 싶은 <b>도시 이름</b>을 채팅창에 그대로 입력해 주세요.\n"
            f"예) {html.escape(names)}\n"
            "🗺️ 아래 8대 권역 버튼으로 직항 도시 목록에서 골라도 돼요.\n"
            "💡 기본 6개 도시 외 지역은 보수적 기본 예산으로 안내되며, 실시간 항공권 링크를 함께 드립니다."
        )

    def build_city_guide_markup(self) -> dict[str, Any]:
        """Guide card keyboard: the shared 4x2 region grid (search mode) + popular-city shortcut."""
        rows = self.build_region_grid("s")
        rows.append([
            {"text": "🏙️ 인기 도시", "callback_data": "trv:m:popular"},
            {"text": "🏠 메뉴", "callback_data": "trv:m:home"},
        ])
        return {"inline_keyboard": rows}

    def _send_guide_card(self, chat_id: str) -> None:
        """`/search`: fresh guide card (remembered so region/page taps edit it in place)."""
        self._remember_watch_card(chat_id, self._send_card(chat_id, self.build_city_guide_card(), self.build_city_guide_markup()))

    def _run_guide_action(self, chat_id: str, message_id: int) -> None:
        """`trv:c:guide`: edits a tracked navigation (menu/watch/guide) card in place; any other message
        (search result, alert, onboarding) keeps its content and gets a fresh guide card."""
        in_place = self._is_watch_card(chat_id, message_id)
        self._render_watch_card(
            chat_id, message_id, self.build_city_guide_card(), self.build_city_guide_markup(), edit=in_place
        )

    def build_help_card(self, unknown_text: str = "", suggestions: Optional[list[Any]] = None) -> str:
        names = " · ".join(self.catalog.suggestion_names(12))
        lines = ["🧭 <b>[트래블 패스파인더 지역 검색 도움말]</b>", "━━━━━━━━━━━━━━━━━━"]
        if unknown_text:
            shown = unknown_text if len(unknown_text) <= 20 else unknown_text[:19] + "…"
            lines.append(f"❓ '<b>{html.escape(shown)}</b>'은(는) 아직 지원하지 않는 지역이에요.")
        if suggestions:
            picks = ", ".join(f"<b>{html.escape(s.display_name)}</b>" for s in suggestions)
            lines.append(f"💡 혹시 아래 도시를 찾으셨나요? {picks}")
        lines.extend([
            "✍️ 도시 이름을 그대로 입력해 보세요. 예) <code>삿포로</code>, <code>세부</code>, <code>호치민</code>",
            f"🔎 추천 검색 도시: {html.escape(names)}",
            "📌 /menu 메인 메뉴 · /여행 전체 TOP 3 · /help 도움말 · /stop 구독 해지",
        ])
        return "\n".join(lines)

    def build_country_city_picker_card(self, country_code: str) -> tuple[str, dict[str, Any]]:
        """Country text (`일본`) -> member-city picker: 1 tap = live search for that city."""
        meta = get_watch_country_meta(country_code)
        if meta is None:
            return self.build_help_card(), self.build_inline_keyboard("ALL")
        text = (
            f"{meta['flag']} <b>[{html.escape(meta['name'])} 주요 도시 선택]</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "어느 도시의 실시간 항공권을 조회할까요? 도시를 누르면 바로 최저가·예산을 알려드려요."
        )
        city_buttons = [
            {"text": self.label_for(code), "callback_data": f"trv:q:{code}"[:64]}
            for code in self.catalog.watch_target_codes(meta["code"])
        ]
        rows: list[list[dict[str, str]]] = [city_buttons[i:i + 3] for i in range(0, len(city_buttons), 3)]
        # Legacy `@JP`-style country watches are read-only compatible: no new-registration button here.
        rows.append([
            {"text": "🗺️ 권역별 도시", "callback_data": "trv:srg"},
            {"text": "🌏 전체 TOP 3 보기", "callback_data": "trv:q:ALL"},
        ])
        return text, {"inline_keyboard": rows}

    def build_onboarding_card(self, chat_id: Optional[str] = None) -> str:
        badge = self._format_status_badge(chat_id)
        status = ("\n".join(["📍 <b>내 발송 상태</b>", *badge]) + "\n━━━━━━━━━━━━━━━━━━\n") if badge else ""
        return (
            "🎉 <b>[✈️ 트래블 패스파인더] 구독이 시작되었습니다!</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"{status}"
            "1️⃣ 📰 <b>정기발송 (매주 목 12:00 기본)</b>\n"
            "관심 권역·여행 스타일(휴양/관광)에 맞춘 TOP 3 브리핑\n"
            "2️⃣ 🔔 <b>맞춤발송 (목표가 도달 시)</b>\n"
            "원하는 직항 도시·일정·가격 등록 시 하루 3회 체크 후 알림 (최대 4개)\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "💬 채팅창에 도시 이름(예: 오사카, 밴쿠버)을 바로 입력하면 실시간 특가를 즉시 조회할 수 있어요.\n"
            "⚙️ 설정은 /settings 에서 언제든 바꿀 수 있어요 👇"
        )

    # ------------------------------------------------------------------
    # Conversation state
    # ------------------------------------------------------------------
    @staticmethod
    def _state_key(chat_id: str, user_id: Optional[str]) -> tuple[str, str]:
        cid = str(chat_id)
        return cid, str(user_id or cid)

    def get_chat_context(self, chat_id: str, user_id: Optional[str] = None) -> Optional[ChatState]:
        """Live (non-expired) state for `(chat_id, user_id)`; expired entries are dropped on read."""
        key = self._state_key(chat_id, user_id)
        with self._seq_lock:
            st = self._chat_states.get(key)
            if st is None:
                return None
            if st.expires_at <= time.monotonic():
                self._chat_states.pop(key, None)
                return None
            return st

    def get_chat_state(self, chat_id: str, user_id: Optional[str] = None) -> str:
        """State for `(chat_id, user_id)`; without `user_id`, the most recent live state of any chat member."""
        if user_id is not None:
            st = self.get_chat_context(chat_id, user_id)
            return st.state if st else STATE_IDLE
        cid = str(chat_id)
        now = time.monotonic()
        with self._seq_lock:
            for (c, _u), st in reversed(self._chat_states.items()):
                if c == cid and st.expires_at > now:
                    return st.state
        return STATE_IDLE

    def _set_chat_state(
        self,
        chat_id: str,
        state: str,
        user_id: Optional[str] = None,
        dest_code: str = "",
        ttl: Optional[float] = None,
    ) -> None:
        """IDLE with `user_id=None` clears every member state of the chat (e.g. /stop)."""
        with self._seq_lock:
            if state == STATE_IDLE:
                if user_id is None:
                    cid = str(chat_id)
                    for k in [k for k in self._chat_states if k[0] == cid]:
                        self._chat_states.pop(k, None)
                    for k in [k for k in self._watch_drafts if k[0] == cid]:
                        self._watch_drafts.pop(k, None)
                else:
                    self._chat_states.pop(self._state_key(chat_id, user_id), None)
                return
            key = self._state_key(chat_id, user_id)
            expires = time.monotonic() + (self.chat_state_ttl if ttl is None else ttl)
            self._chat_states[key] = ChatState(state=state, expires_at=expires, dest_code=dest_code)
            self._chat_states.move_to_end(key)
            while len(self._chat_states) > MAX_CHAT_STATES:
                self._chat_states.popitem(last=False)

    # ------------------------------------------------------------------
    # Entry points
    # ------------------------------------------------------------------
    def handle_callback_query(
        self,
        cq_id: str,
        chat_id: str,
        message_id: int,
        callback_data: str,
        from_user: dict[str, Any],
    ) -> None:
        """Entry point for incoming callback queries.
        CRITICAL ARCHITECTURE GUARANTEES:
        1. Evaluates cooldown in <0.001s.
        2. Dispatches answerCallbackQuery IMMEDIATELY (TG-04) - 0.1s target.
        3. Hands heavy scraping to background worker pool so the polling loop is NEVER blocked (C3).
        """
        user_id = str(from_user.get("id") or chat_id).strip()
        is_admin = user_id in self._admin_ids

        # Parse command: e.g. "trv:s:NRT", "trv:q:NRT", "trv:r:ALL", "trv:c:guide"
        parts = callback_data.split(":")
        action = parts[1] if len(parts) > 1 else "q"
        target = parts[2] if len(parts) > 2 else "ALL"

        if action == "c":
            self._set_chat_state(chat_id, STATE_AWAITING_CITY, user_id=user_id)
            self._submit_nav_then_ack(
                cq_id, "🔍 가고 싶은 도시 이름을 입력해 주세요!",
                self._run_guide_action, str(chat_id), int(message_id or 0),
            )
            return

        # Search drilldown: `trv:srg` (4x2 regions) / `trv:srg:<REG>:<PAGE>` (3x3 city page). Never a search.
        if action == "srg":
            region: Optional[str] = None
            page = 0
            if len(parts) != 2:
                page_raw = parts[3] if len(parts) == 4 else ""
                if len(parts) != 4 or not is_region_code(parts[2]) or not page_raw.isdigit() or len(page_raw) > 3:
                    self._answer(cq_id, "지원하지 않는 메뉴입니다. /search 로 다시 열어 주세요.")
                    return
                region, page = parts[2].upper(), int(page_raw)
            self._submit_nav_then_ack(
                cq_id, "", self._run_search_region_action, str(chat_id), int(message_id or 0), region, page
            )
            return

        # Price watch flows (`trv:w:*`) never fall into the search/stale path; answered immediately.
        if action == "w":
            self._handle_watch_callback(cq_id, str(chat_id), int(message_id or 0), user_id, parts)
            return

        # Main menu (`trv:m:<sub>`): strict allowlist; unknown subs are rejected (never fall into search).
        if action == "m":
            sub = parts[2] if len(parts) == 3 else ""
            if sub not in MENU_SUBS:
                self._answer(cq_id, "지원하지 않는 메뉴입니다. /menu 로 다시 열어 주세요.")
                return
            # Navigation abandons only this user's pending input (other group members untouched).
            self._set_chat_state(chat_id, STATE_IDLE, user_id=user_id)
            if sub != "search":
                self._handle_menu_callback(cq_id, str(chat_id), int(message_id or 0), sub)
                return
            action, target = "q", "ALL"  # same path as `trv:q:ALL`

        target_norm = self.normalize_filter(target)
        if target_norm is None:
            self._answer(cq_id, "지원하지 않는 지역입니다. 도시 이름을 직접 입력해 주세요.")
            return

        if action in ("q", "r"):
            self._set_chat_state(chat_id, STATE_IDLE, user_id=user_id)

        # Target-selection step (`trv:s:<CITY>`): acknowledge immediately & show [📋 직전 발송 특가 정보 보기] button
        if action == "s":
            city_label = self.label_for(target_norm)
            self._answer(cq_id, f"🎯 {city_label} 선택 완료! [직전 발송 특가 정보 보기] 버튼을 눌러주세요.")
            confirm_html = (
                f"✅ <b>[트래블 패스파인더 맞춤 여행지 선택 완료]</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🎯 <b>선택하신 여행지:</b> <b>{html.escape(city_label)}</b>\n\n"
                f"👇 아래 <b>[📋 직전 발송 특가 정보 보기]</b> 버튼을 누르시면 "
                f"구독 직전에 발송되었던 최신 주말 항공·숙박 특가 브리핑을 즉시 확인하실 수 있습니다!"
            )
            markup = self.build_selected_target_keyboard(target_norm)
            s_seq = self._next_request_seq(chat_id, message_id)
            try:
                self.worker_pool.submit(
                    self._edit_if_latest,
                    chat_id=chat_id,
                    message_id=message_id,
                    request_seq=s_seq,
                    text=confirm_html,
                    reply_markup=markup,
                )
            except Exception as exc:
                logger.error("Target-selection edit submission failed: %s", exc)
                self._edit_if_latest(chat_id, message_id, s_seq, confirm_html, markup)
            return

        is_refresh = (action == "r")

        # 1. Evaluate cooldown & in-progress state
        allowed, toast = self.cooldown.check_and_acquire(
            user_id=user_id, is_force_refresh=is_refresh, is_admin=is_admin, scope=target_norm
        )

        # 2. Immediate single callback acknowledgement (TG-04 / M4)
        self._answer(cq_id, toast)

        if not allowed:
            # Blocked by cooldown or already in-progress; no background task submitted
            return

        # Scope-isolated refresh: ALL -> curated scopes only, city -> that scope only
        if is_refresh:
            self._invalidate_scope(target_norm)

        self._launch_search(chat_id, message_id, target_norm, user_id, is_refresh=is_refresh, show_loading=True)

    def _run_search_region_action(self, chat_id: str, message_id: int, region: Optional[str], page: int) -> None:
        in_place = self._is_watch_card(chat_id, message_id)
        try:
            if region is None:
                text, markup = self.build_city_guide_card(), self.build_city_guide_markup()
            else:
                text, markup = self.build_city_page_card("s", region, page)
        except Exception as exc:
            logger.error("Region page %s/%s failed for %s: %s", region, page, chat_id, exc, exc_info=True)
            text = "⚠️ 도시 목록을 불러오는 중 일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요."
            markup = {"inline_keyboard": [[{"text": "🏠 메뉴", "callback_data": "trv:m:home"}]]}
        self._render_watch_card(chat_id, message_id, text, markup, edit=in_place)

    def handle_text_message(
        self,
        chat_id: str,
        message_id: int,
        user_id: str,
        text: str,
        from_user: Optional[dict[str, Any]] = None,
        *,
        sender_name: str = "",
        on_subscribe: Optional[Callable[[str, str], Any]] = None,
        on_unsubscribe: Optional[Callable[[str], Any]] = None,
    ) -> tuple[bool, str]:
        """Common text entry for standalone poller and multi-bot manager.
        Returns (handled, reason); `handled=False` delegates to the caller (e.g. manager /stop handling).
        """
        chat_id = str(chat_id)
        user_id = str(user_id or chat_id)
        raw = (text or "").strip()
        if not chat_id or not raw:
            return False, "empty_message"

        first = raw.split()[0]
        if first.startswith("/"):
            cmd = first.split("@", 1)[0].casefold()
            # Any command first clears this user's pending input (AWAITING_CITY / AWAITING_WATCH_PRICE).
            pending = self.get_chat_context(chat_id, user_id)
            self._set_chat_state(chat_id, STATE_IDLE, user_id=user_id)
            if cmd in CANCEL_COMMANDS:
                notice = "✅ 입력을 취소했어요." if pending else "ℹ️ 진행 중인 입력이 없어요."
                self._send_card(chat_id, notice, self.build_inline_keyboard("ALL", chat_id=chat_id))
                return True, "travel_cancel"
            if cmd in START_COMMANDS:
                return self._handle_start(chat_id, sender_name, on_subscribe)
            if cmd in STOP_COMMANDS:
                if on_unsubscribe is None:
                    return False, "travel_stop_delegated"
                self._set_chat_state(chat_id, STATE_IDLE)
                try:
                    on_unsubscribe(chat_id)
                except Exception as exc:
                    logger.warning("Travel unsubscribe hook failed for %s: %s", chat_id, exc)
                self._send_card(
                    chat_id,
                    "👋 여행 특가 구독이 해지되었습니다. 언제든 <code>/start</code>를 누르면 다시 받아보실 수 있습니다.",
                )
                return True, "travel_stop"
            if cmd in SEARCH_ALL_COMMANDS:
                if on_subscribe is not None:
                    self._call_subscribe(on_subscribe, chat_id, sender_name)
                self._set_chat_state(chat_id, STATE_IDLE, user_id=user_id)
                return self._start_text_search(chat_id, user_id, "ALL")
            if cmd in GUIDE_COMMANDS:
                self._set_chat_state(chat_id, STATE_AWAITING_CITY, user_id=user_id)
                self._send_guide_card(chat_id)
                return True, "travel_guide"
            if cmd in MENU_COMMANDS:
                text, markup = self.build_menu_hub_card(chat_id)
                self._render_watch_card(chat_id, 0, text, markup, edit=False)
                return True, "travel_menu"
            if cmd in WATCH_COMMANDS or cmd in SETTINGS_COMMANDS:
                if self.db is None:
                    self._send_card(chat_id, "⚠️ 특가 알림 기능을 일시적으로 사용할 수 없습니다.")
                    return True, "travel_watch_unavailable"
                is_watch = cmd in WATCH_COMMANDS
                try:
                    text, markup = (
                        self.build_watch_list_card(chat_id) if is_watch else self.build_delivery_settings_card(chat_id)
                    )
                except Exception as exc:
                    logger.error("Travel %s command failed for %s: %s", cmd, chat_id, exc, exc_info=True)
                    self._send_card(chat_id, "⚠️ 일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.")
                    return True, "travel_command_failed"
                self._render_watch_card(chat_id, 0, text, markup, edit=False)
                return True, "travel_watch_list" if is_watch else "travel_settings"
            if cmd in HELP_COMMANDS:
                self._send_card(chat_id, self.build_help_card(), self.build_inline_keyboard("ALL", chat_id=chat_id))
                return True, "travel_help"
            self._send_card(chat_id, self.build_help_card(), self.build_inline_keyboard("ALL"))
            return True, "travel_help"

        query = _MENTION_RE.sub(" ", raw).strip()
        if not query:
            self._send_card(chat_id, self.build_help_card(), self.build_inline_keyboard("ALL"))
            return True, "travel_help"

        ctx = self.get_chat_context(chat_id, user_id)
        if ctx is not None and ctx.state == STATE_AWAITING_WATCH_CITY:
            handled = self._handle_watch_city_input(chat_id, user_id, query)
            if handled is not None:
                return handled
            # The ALL keyword escapes the watch-city prompt into the normal search (never traps the user).
            self._set_chat_state(chat_id, STATE_IDLE, user_id=user_id)
            ctx = None
        if ctx is not None and ctx.state == STATE_AWAITING_WATCH_DATES:
            handled = self._handle_watch_dates_input(chat_id, user_id, query, ctx)
            if handled is not None:
                return handled
            # A recognised city / country / ALL keyword escapes the date prompt (never traps the user).
            self._set_chat_state(chat_id, STATE_IDLE, user_id=user_id)
        if ctx is not None and ctx.state == STATE_AWAITING_WATCH_PRICE:
            handled = self._handle_watch_price_input(chat_id, user_id, query, ctx)
            if handled is not None:
                return handled
            # A recognised city / ALL keyword escapes the price prompt into the normal search below.
            self._set_chat_state(chat_id, STATE_IDLE, user_id=user_id)
        if query in START_WORDS:
            return self._handle_start(chat_id, sender_name, on_subscribe)
        if query.casefold().replace(" ", "") in ALL_WORDS:
            self._set_chat_state(chat_id, STATE_IDLE, user_id=user_id)
            return self._start_text_search(chat_id, user_id, "ALL")

        res = self.catalog.resolve(query)
        if res is None:
            country = self.catalog.resolve_country(query)
            if country:
                # Countries have no briefing search; they open a member-city picker (+ country watch entry).
                self._set_chat_state(chat_id, STATE_IDLE, user_id=user_id)
                text, markup = self.build_country_city_picker_card(country)
                self._send_card(chat_id, text, markup)
                return True, "travel_country_city_picker"
            suggestions = self.catalog.suggest_cities(query, limit=3)
            markup = self.build_inline_keyboard("ALL")
            if suggestions:
                suggest_row = [
                    {"text": s.display_name, "callback_data": f"trv:q:{s.code}"[:64]} for s in suggestions
                ]
                markup = {"inline_keyboard": [suggest_row] + markup["inline_keyboard"]}
            self._send_card(chat_id, self.build_help_card(query, suggestions), markup)
            return True, "travel_help_unknown_city"
        self._set_chat_state(chat_id, STATE_IDLE, user_id=user_id)
        return self._start_text_search(chat_id, user_id, res.code)

    def _handle_start(
        self, chat_id: str, sender_name: str, on_subscribe: Optional[Callable[[str, str], Any]]
    ) -> tuple[bool, str]:
        if on_subscribe is not None:
            self._call_subscribe(on_subscribe, chat_id, sender_name)
        self._set_chat_state(chat_id, STATE_IDLE)
        self._send_card(chat_id, self.build_onboarding_card(chat_id=chat_id), self.build_onboarding_keyboard())
        return True, "travel_start"

    @staticmethod
    def _call_subscribe(on_subscribe: Callable[[str, str], Any], chat_id: str, sender_name: str) -> None:
        try:
            on_subscribe(chat_id, sender_name)
        except Exception as exc:
            logger.warning("Travel subscribe hook failed for %s: %s", chat_id, exc)

    def _start_text_search(self, chat_id: str, user_id: str, code: str) -> tuple[bool, str]:
        allowed, toast = self.cooldown.check_and_acquire(user_id=user_id, scope=code)
        if not allowed:
            self._send_card(chat_id, f"⏳ {html.escape(toast)}")
            return True, "travel_search_throttled"
        loading_id = self._send_card(
            chat_id, self.build_loading_card(code), self.build_inline_keyboard(current_filter=code, chat_id=chat_id)
        )
        try:
            msg_id = int(loading_id) if loading_id is not None else 0
        except (TypeError, ValueError):
            msg_id = 0
        if msg_id <= 0:
            self.cooldown.release(user_id)
            return True, "travel_loading_send_failed"
        self._launch_search(chat_id, msg_id, code, user_id, is_refresh=False, show_loading=False)
        return True, "travel_search_started"

    # ------------------------------------------------------------------
    # Price watches (`trv:w:*`)
    # ------------------------------------------------------------------
    _WATCH_TOASTS = {
        "m": "🔔 알림 설정을 불러옵니다.",
        "p": "✅ 알림을 저장하는 중입니다...",
        "t": "⚙️ 알림 옵션을 변경합니다.",
        "i": "✍️ 목표가를 채팅창에 입력해 주세요.",
        "l": "📋 내 알림 목록을 불러옵니다.",
        "x": "🗑️ 삭제 여부를 확인해 주세요.",
        "X": "🗑️ 알림을 삭제했습니다.",
        # V2 wizard
        "g": "🌏 알림 대상을 골라 주세요.",
        "s": "🔔 알림 조건을 불러옵니다.",
        "e": "⚙️ 저장된 조건을 불러옵니다.",
        # Draft check toggles ack silently (no toast banner per checkmark tap).
        "m2": "",
        "h": "",
        "d": "",
        "f": "🗓️ 여행 날짜를 채팅창에 입력해 주세요.",
        "n": "",
        "k": "✅ 알림을 저장하는 중입니다...",
        # Chat-level delivery controls (Track A opt-out / Track B pause / 2-step reset)
        "wb": "📰 정기발송 설정을 변경합니다.",
        "wp": "🔔 맞춤발송 설정을 변경합니다.",
        "z": "🧹 초기화 여부를 확인해 주세요.",
        "Z": "🧹 알림을 초기화합니다...",
        # 4x2 region -> 3x3 city watch add, direct key-in, and the 4-line /settings controls
        "rg": "🗺️ 권역·도시를 골라 주세요.",
        "ct": "🔔 알림 조건을 불러옵니다.",
        "ki": "⌨️ 알림 받을 도시 이름을 채팅창에 입력해 주세요.",
        "fq": "📰 정기발송 주기를 변경합니다.",
        "rgp": "🧭 관심권역을 골라 주세요.",
        "rgn": "🧭 관심권역을 변경합니다.",
        "th": "🎯 여행스타일을 변경합니다.",
    }
    # Sub-actions with their own strict argument layout (parsed by `_parse_nav_watch`).
    _NAV_WATCH_SUBS = frozenset({"rg", "ct", "ki", "fq", "rgp", "rgn", "th"})
    # Sub-actions that take no city/country scope argument.
    _CHAT_LEVEL_WATCH_SUBS = frozenset({"l", "g", "wb", "wp", "z", "Z"})
    _PREFS_WATCH_SUBS = frozenset({"wb", "wp"})
    _WATCH_ARGS = {
        "p": ("D", "F"),
        "t": ("day", "pk"),
        "m2": tuple(WATCH_MODE_CODES),
        "h": tuple(str(h) for h in WATCH_HORIZON_CHOICES),
        "d": tuple(WATCH_WEEKDAY_CODES),
        "n": tuple(WATCH_NOTIFY_CODES),
        "k": ("1", "W"),
    }

    _FAST_WATCH_TOGGLES = frozenset({"m2", "h", "d", "n"})
    # Pure read/navigation subs (no DB write): UI edit submitted first, ack in parallel. Every DB-mutating
    # sub (wb / wp / Z / k / p / t / X / fq / rgn / th ...) keeps the ack-before-work ordering.
    _READ_NAV_WATCH_SUBS = frozenset({"rg", "rgp", "ki"})
    _READ_WATCH_SUBS = frozenset({"l", "g", "s", "e", "m"})

    def normalize_watch_scope(self, raw: Optional[str]) -> Optional[str]:
        """`@JP`-style country scopes or a catalog city code; ALL / unknown -> None."""
        token = (raw or "").strip()
        if is_country_watch_code(token):
            return token.upper()
        norm = self.normalize_filter(token) if token else None
        return None if (not norm or norm == "ALL") else norm

    def _handle_watch_callback(
        self, cq_id: str, chat_id: str, message_id: int, user_id: str, parts: list[str]
    ) -> None:
        sub = parts[2] if len(parts) > 2 else ""
        if sub not in self._WATCH_TOASTS:
            self._answer(cq_id, "지원하지 않는 알림 메뉴입니다.")
            return
        if self.db is None:
            self._answer(cq_id, "⚠️ 특가 알림 기능을 일시적으로 사용할 수 없습니다.")
            return
        if sub in self._NAV_WATCH_SUBS:
            parsed = self._parse_nav_watch(sub, parts)
            if parsed is None:
                self._answer(cq_id, "지원하지 않는 알림 메뉴입니다.")
                return
            read_only = sub in self._READ_NAV_WATCH_SUBS
            if not read_only:
                self._answer(cq_id, self._WATCH_TOASTS[sub])  # TG-04: ack before any DB / network work
            if sub in ("ki", "rg"):
                self._set_chat_state(chat_id, STATE_AWAITING_WATCH_CITY, user_id=user_id)
            else:
                st = self.get_chat_context(chat_id, user_id)
                if st is not None and st.state in (
                    STATE_AWAITING_WATCH_PRICE, STATE_AWAITING_WATCH_DATES, STATE_AWAITING_WATCH_CITY
                ):
                    self._set_chat_state(chat_id, STATE_IDLE, user_id=user_id)
            if read_only:
                # Pure navigation: start the edit first, ack in parallel (still exactly one ack).
                self._submit_nav_then_ack(
                    cq_id, self._WATCH_TOASTS[sub], self._run_watch_nav_action, chat_id, message_id, sub, parsed, user_id
                )
            else:
                self._submit_ui(self._run_watch_nav_action, chat_id, message_id, sub, parsed, user_id)
            return
        code = ""
        arg = parts[4] if len(parts) > 4 else ""
        if sub in self._PREFS_WATCH_SUBS:
            # Legacy `trv:w:wb[:s]` / `trv:w:wp[:s]` (older messages) stay valid; nothing else is accepted.
            # Result card follows the track: wb -> 📰 정기발송 설정, wp -> 🔔 맞춤발송 설정.
            arg = parts[3] if len(parts) > 3 else ""
            if len(parts) > 4 or arg not in ("", "s"):
                self._answer(cq_id, "지원하지 않는 알림 메뉴입니다.")
                return
        elif sub not in self._CHAT_LEVEL_WATCH_SUBS:
            raw = parts[3] if len(parts) > 3 else ""
            norm = self.normalize_watch_scope(raw)
            if not norm:
                self._answer(cq_id, "지원하지 않는 지역입니다. 도시를 먼저 선택해 주세요.")
                return
            code = norm
        allowed = self._WATCH_ARGS.get(sub)
        if allowed is not None and arg not in allowed:
            self._answer(cq_id, "지원하지 않는 알림 메뉴입니다.")
            return

        if sub in self._FAST_WATCH_TOGGLES:
            # Latency path: clear stale input state, start the editMessageText on ui_pool first, then
            # ack (silently) so answerCallbackQuery runs in parallel. Still acked before returning (TG-04).
            st = self.get_chat_context(chat_id, user_id)
            if st is not None and st.state in (
                STATE_AWAITING_WATCH_PRICE, STATE_AWAITING_WATCH_DATES, STATE_AWAITING_WATCH_CITY
            ):
                self._set_chat_state(chat_id, STATE_IDLE, user_id=user_id)
            self._submit_ui(self._run_watch_action, chat_id, message_id, sub, code, arg, user_id)
            self._answer(cq_id, self._WATCH_TOASTS[sub])
            return

        read_only = sub in self._READ_WATCH_SUBS
        if not read_only:
            self._answer(cq_id, self._WATCH_TOASTS[sub])  # TG-04: ack before any DB / network work
        if sub == "i":
            self._set_chat_state(chat_id, STATE_AWAITING_WATCH_PRICE, user_id=user_id, dest_code=code)
        elif sub == "f":
            self._set_chat_state(chat_id, STATE_AWAITING_WATCH_DATES, user_id=user_id, dest_code=code)
        elif sub == "g":
            # No DB read before the ack: the worker drops this back to IDLE when the chat is at the cap.
            self._set_chat_state(chat_id, STATE_AWAITING_WATCH_CITY, user_id=user_id)
        else:
            st = self.get_chat_context(chat_id, user_id)
            if st is not None and st.state in (
                STATE_AWAITING_WATCH_PRICE, STATE_AWAITING_WATCH_DATES, STATE_AWAITING_WATCH_CITY
            ):
                self._set_chat_state(chat_id, STATE_IDLE, user_id=user_id)
        if read_only:
            self._submit_nav_then_ack(
                cq_id, self._WATCH_TOASTS[sub], self._run_watch_action, chat_id, message_id, sub, code, arg, user_id
            )
        else:
            self._submit_ui(self._run_watch_action, chat_id, message_id, sub, code, arg, user_id)

    def _parse_nav_watch(self, sub: str, parts: list[str]) -> Optional[dict[str, Any]]:
        """Strict allowlist parsing for the region/city/settings watch callbacks (anything else -> None)."""
        n = len(parts)
        if sub in ("ki", "rgp"):
            return {} if n == 3 else None
        if sub == "rg":
            if n == 3:
                return {"region": None, "page": 0}
            if n == 5 and is_region_code(parts[3]) and parts[4].isdigit() and len(parts[4]) <= 3:
                return {"region": parts[3].upper(), "page": int(parts[4])}
            return None
        if sub == "ct":
            if n != 6 or not is_region_code(parts[3]) or not parts[4].isdigit() or len(parts[4]) > 3:
                return None
            code = self.normalize_filter(parts[5])
            if not code or code == "ALL" or is_country_watch_code(parts[5]):
                return None
            return {"region": parts[3].upper(), "page": int(parts[4]), "code": code}
        if n != 4:
            return None
        if sub == "fq":
            return {"freq": WEEKLY_FREQ_CODES[parts[3]]} if parts[3] in WEEKLY_FREQ_CODES else None
        if sub == "rgn":
            reg = parts[3].upper()
            return {"region": reg} if (reg == "ALL" or is_region_code(reg)) else None
        if sub == "th":
            return {"theme": THEME_CB_CODES[parts[3]]} if parts[3] in THEME_CB_CODES else None
        return None

    def _run_watch_nav_action(
        self, chat_id: str, message_id: int, sub: str, p: dict[str, Any], user_id: str = ""
    ) -> None:
        uid = str(user_id or chat_id)
        in_place = self._is_watch_card(chat_id, message_id)
        try:
            if sub == "rg":
                if self._at_watch_limit(chat_id):
                    self._set_chat_state(chat_id, STATE_IDLE, user_id=uid)
                    text, markup = self.build_watch_list_card(chat_id, notice=f"⚠️ {WATCH_LIMIT_NOTICE}")
                elif p["region"] is None:
                    text, markup = self.build_watch_target_picker_card()
                else:
                    text, markup = self.build_city_page_card("w", p["region"], p["page"], chat_id=chat_id)
            elif sub == "ct":
                code = p["code"]
                if self._at_watch_limit(chat_id, code):
                    text, markup = self.build_watch_list_card(chat_id, notice=f"⚠️ {WATCH_LIMIT_NOTICE}")
                else:
                    self._get_draft(chat_id, uid, code)
                    text, markup = self.build_watch_setup_card(chat_id, uid, code)
            elif sub == "ki":
                if self._at_watch_limit(chat_id):
                    self._set_chat_state(chat_id, STATE_IDLE, user_id=uid)
                    text, markup = self.build_watch_list_card(chat_id, notice=f"⚠️ {WATCH_LIMIT_NOTICE}")
                else:
                    text, markup = self.build_watch_city_prompt_card()
            elif sub == "rgp":
                text, markup = self.build_pref_region_card(chat_id)
            else:
                before = self._is_dormant(chat_id)
                if sub == "fq":
                    prefs = self.db.set_weekly_brief_freq(chat_id, p["freq"])
                    notice = (
                        "📰 정기 여행정보 발송을 껐어요. 맞춤 특가 알림과 도시 조회는 그대로 이용할 수 있어요."
                        if prefs["weekly_brief_freq"] == "OFF"
                        else f"📰 정기발송을 {WEEKLY_FREQ_STATUS[prefs['weekly_brief_freq']]}로 설정했어요."
                    )
                elif sub == "rgn":
                    prefs = self.db.set_weekly_brief_region(chat_id, p["region"])
                    notice = f"🧭 관심권역을 {html.escape(region_label(prefs['weekly_brief_region']))}(으)로 바꿨어요."
                else:
                    prefs = self.db.set_weekly_brief_theme(chat_id, p["theme"])
                    notice = f"🎯 여행스타일을 {THEME_LABELS[prefs['weekly_brief_theme']]}(으)로 바꿨어요."
                notice = self._with_dormant_notice(chat_id, before, notice)
                text, markup = self.build_delivery_settings_card(chat_id, notice=notice)
            self._render_watch_card(chat_id, message_id, text, markup, edit=in_place)
        except Exception as exc:
            logger.error("Watch nav %s failed for %s: %s", sub, chat_id, exc, exc_info=True)
            self._render_watch_card(
                chat_id, message_id,
                "⚠️ 알림 설정 처리 중 일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
                {"inline_keyboard": [[{"text": "📋 내 알림 목록", "callback_data": "trv:w:l"}]]},
            )

    # ------------------------------------------------------------------
    # Watch limit / dormant-mode helpers
    # ------------------------------------------------------------------
    def _active_watch_count(self, chat_id: str) -> int:
        if self.db is None:
            return 0
        try:
            return len(self.db.list_travel_watches(chat_id=str(chat_id), active_only=True))
        except Exception as exc:
            logger.warning("Active watch count failed for %s: %s", chat_id, exc)
            return 0

    def _at_watch_limit(self, chat_id: str, code: Optional[str] = None) -> bool:
        """True when a NEW watch can't be added (active >= 4). Editing an already-active watch never counts."""
        from travel_brief.storage.db import WATCH_MAX_ACTIVE_PER_CHAT

        if code:
            w = self._get_watch(chat_id, code)
            if w and w.get("is_active"):
                return False
        return self._active_watch_count(chat_id) >= WATCH_MAX_ACTIVE_PER_CHAT

    @staticmethod
    def _freq_of(prefs: dict[str, Any]) -> str:
        freq = str(prefs.get("weekly_brief_freq") or "").upper()
        if freq not in WEEKLY_FREQ_STATUS:
            freq = "THU_1X" if prefs.get("weekly_brief_enabled", True) else "OFF"
        if not prefs.get("weekly_brief_enabled", True):
            freq = "OFF"
        return freq

    def _is_dormant(
        self, chat_id: str, prefs: Optional[dict[str, Any]] = None, n_watches: Optional[int] = None
    ) -> bool:
        """Dormant = Track A OFF and Track B silent (paused or no active watch): search-only mode."""
        prefs = prefs if prefs is not None else self._get_prefs(str(chat_id))
        if self._freq_of(prefs) != "OFF":
            return False
        if prefs.get("watches_paused", False):
            return True
        count = self._active_watch_count(chat_id) if n_watches is None else int(n_watches)
        return count == 0

    def _with_dormant_notice(self, chat_id: str, was_dormant: bool, notice: str) -> str:
        """One-time friendly notice on the transition into dormant mode (never repeated while dormant)."""
        if not was_dormant and self._is_dormant(chat_id):
            return f"{notice}\n{DORMANT_NOTICE}" if notice else DORMANT_NOTICE
        return notice

    def build_watch_city_prompt_card(self, notice: str = "") -> tuple[str, dict[str, Any]]:
        minutes = max(1, int(self.chat_state_ttl // 60))
        names = " · ".join(self.catalog.suggestion_names(6))
        lines = [notice] if notice else []
        lines += [
            WATCH_STEP1_HEADER,
            "━━━━━━━━━━━━━━━━━━",
            f"⌨️ 알림 받을 도시 이름을 채팅창에 입력해 주세요 ({minutes}분 안에).",
            f"예) {html.escape(names)}",
            "• 국가 전체 알림은 지원하지 않아요 · 취소: /cancel",
        ]
        rows = self.build_region_grid("w")
        rows.append([{"text": "📋 내 알림", "callback_data": "trv:w:l"}])
        return "\n".join(lines), {"inline_keyboard": rows}

    def _handle_watch_city_input(self, chat_id: str, user_id: str, query: str) -> Optional[tuple[bool, str]]:
        """STATE_AWAITING_WATCH_CITY: a catalog city -> setup card + STATE_AWAITING_WATCH_PRICE (type a price or
        pick options). Unknown text / countries keep the prompt; the ALL keyword escapes (returns None)."""
        if query.casefold().replace(" ", "") in ALL_WORDS:
            return None
        res = self.catalog.resolve(query)
        if res is None:
            if self.catalog.resolve_country(query):
                notice = "⚠️ 국가 전체 알림은 지원하지 않아요. 도시 이름을 입력하거나 권역에서 골라 주세요."
            else:
                shown = query if len(query) <= 20 else query[:19] + "…"
                notice = f"⚠️ '<b>{html.escape(shown)}</b>'은(는) 알림 가능한 직항 도시가 아니에요."
            text, markup = self.build_watch_city_prompt_card(notice=notice)
            suggestions = self.catalog.suggest_cities(query, limit=3)
            if suggestions:
                markup["inline_keyboard"].insert(
                    0, [{"text": s.display_name, "callback_data": f"trv:w:s:{s.code}"} for s in suggestions]
                )
            self._send_card(chat_id, text, markup)
            return True, "travel_watch_city_invalid"
        if self.db is None:
            self._set_chat_state(chat_id, STATE_IDLE, user_id=user_id)
            self._send_card(chat_id, "⚠️ 특가 알림 기능을 일시적으로 사용할 수 없습니다.")
            return True, "travel_watch_unavailable"
        if self._at_watch_limit(chat_id, res.code):
            self._set_chat_state(chat_id, STATE_IDLE, user_id=user_id)
            text, markup = self.build_watch_list_card(chat_id, notice=f"⚠️ {WATCH_LIMIT_NOTICE}")
            self._render_watch_card(chat_id, 0, text, markup, edit=False)
            return True, "travel_watch_limit"
        self._set_chat_state(chat_id, STATE_AWAITING_WATCH_PRICE, user_id=user_id, dest_code=res.code)
        self._get_draft(chat_id, user_id, res.code)
        text, markup = self.build_watch_setup_card(
            chat_id, user_id, res.code,
            notice=f"✅ {html.escape(res.display_name)} 선택! 목표가를 채팅창에 바로 입력하거나(예: 25만) 아래에서 골라 저장해 주세요.",
        )
        self._render_watch_card(chat_id, 0, text, markup, edit=False)
        return True, "travel_watch_city_selected"

    def _run_watch_action(
        self, chat_id: str, message_id: int, sub: str, code: str, arg: str, user_id: str = ""
    ) -> None:
        uid = str(user_id or chat_id)
        in_place = self._is_watch_card(chat_id, message_id)
        try:
            if code and is_country_watch_code(code) and sub not in ("x", "X"):
                # Legacy `@JP` country scopes: read-only compatible (existing active watches stay editable /
                # deletable); a chat without an active one can never start a new country registration.
                existing = self._get_watch(chat_id, code)
                if not (existing and existing.get("is_active")):
                    self._drop_draft(chat_id, uid, code)
                    self._set_chat_state(chat_id, STATE_IDLE, user_id=uid)
                    text, markup = self.build_watch_target_picker_card(notice=COUNTRY_WATCH_RETIRED_NOTICE)
                    self._render_watch_card(chat_id, message_id, text, markup, edit=in_place)
                    return
            if sub == "m" and is_country_watch_code(code):
                sub = "s"  # countries only have the V2 setup hub
            if sub == "m":
                text, markup = self.build_watch_manage_card(chat_id, code)
                self._render_watch_card(chat_id, message_id, text, markup, edit=in_place)
            elif sub == "l":
                text, markup = self.build_watch_list_card(chat_id)
                self._render_watch_card(chat_id, message_id, text, markup, edit=in_place)
            elif sub == "p":
                text, markup = self._save_preset_watch(chat_id, code, arg, user_id=uid)
                self._render_watch_card(chat_id, message_id, text, markup)
            elif sub == "t":
                watch = self.db.toggle_travel_watch_option(chat_id, code, arg)
                if watch is None:
                    text, markup = self.build_watch_manage_card(chat_id, code, notice="ℹ️ 활성화된 알림이 없어 설정 화면으로 이동했어요.")
                else:
                    text, markup = self.build_watch_saved_card(watch, title="⚙️ 알림 옵션이 변경되었습니다")
                self._render_watch_card(chat_id, message_id, text, markup)
            elif sub == "i":
                text, markup = self.build_watch_input_card(code)
                self._render_watch_card(chat_id, message_id, text, markup)
            elif sub == "x":
                text, markup = self.build_watch_delete_confirm_card(code)
                self._render_watch_card(chat_id, message_id, text, markup)
            elif sub == "X":
                before = self._is_dormant(chat_id)
                removed = self.db.deactivate_travel_watch(chat_id, code)
                self._drop_draft(chat_id, uid, code)
                notice = (
                    f"🗑️ {html.escape(self.label_for(code))} 알림을 삭제했어요."
                    if removed else "ℹ️ 이미 삭제되었거나 활성화된 알림이 없어요."
                )
                notice = self._with_dormant_notice(chat_id, before, notice)
                text, markup = self.build_watch_list_card(chat_id, notice=notice)
                self._render_watch_card(chat_id, message_id, text, markup)
            elif sub == "g":
                if self._at_watch_limit(chat_id):
                    self._set_chat_state(chat_id, STATE_IDLE, user_id=uid)
                    text, markup = self.build_watch_list_card(chat_id, notice=f"⚠️ {WATCH_LIMIT_NOTICE}")
                else:
                    text, markup = self.build_watch_target_picker_card()
                self._render_watch_card(chat_id, message_id, text, markup, edit=in_place)
            elif sub in ("s", "e"):
                self._get_draft(chat_id, uid, code, reset=(sub == "e"))
                text, markup = self.build_watch_setup_card(chat_id, uid, code)
                self._render_watch_card(chat_id, message_id, text, markup, edit=in_place)
            elif sub == "f":
                text, markup = self.build_watch_dates_prompt_card(code)
                self._render_watch_card(chat_id, message_id, text, markup)
            elif sub in ("m2", "h", "d", "n"):
                text, markup = self._apply_draft_option(chat_id, uid, code, sub, arg)
                self._render_watch_card(chat_id, message_id, text, markup)
            elif sub == "k":
                text, markup = self._confirm_draft_watch(chat_id, uid, code, arg)
                self._render_watch_card(chat_id, message_id, text, markup)
            elif sub == "wb":
                before = self._is_dormant(chat_id)
                prefs = self.db.set_weekly_brief_enabled(chat_id)
                notice = (
                    f"📰 정기 여행정보 발송을 다시 켰어요 ({WEEKLY_FREQ_STATUS[self._freq_of(prefs)]})."
                    if prefs["weekly_brief_enabled"] else
                    "📰 정기 여행정보 발송을 껐어요. 맞춤 특가 알림과 도시 조회는 그대로 이용할 수 있어요."
                )
                notice = self._with_dormant_notice(chat_id, before, notice)
                text, markup = self._prefs_result_card(chat_id, "wb", notice)
                self._render_watch_card(chat_id, message_id, text, markup, edit=in_place)
            elif sub == "wp":
                before = self._is_dormant(chat_id)
                prefs = self.db.set_watches_paused(chat_id)
                notice = (
                    "⏸️ 맞춤 특가 알림을 일시중지했어요. 설정은 그대로 보관돼요."
                    if prefs["watches_paused"] else
                    "▶️ 맞춤 특가 알림을 다시 시작했어요."
                )
                notice = self._with_dormant_notice(chat_id, before, notice)
                text, markup = self._prefs_result_card(chat_id, "wp", notice)
                self._render_watch_card(chat_id, message_id, text, markup, edit=in_place)
            elif sub == "z":
                text, markup = self.build_watch_reset_confirm_card(chat_id)
                self._render_watch_card(chat_id, message_id, text, markup, edit=in_place)
            elif sub == "Z":
                before = self._is_dormant(chat_id)
                removed = self.db.reset_travel_watches(chat_id)
                self._clear_user_watch_state(chat_id, uid)
                notice = self._with_dormant_notice(
                    chat_id, before, f"🧹 맞춤 특가 알림 {removed}개를 초기화했어요. (정기발송 설정은 유지)"
                )
                text, markup = self.build_watch_list_card(chat_id, notice=notice)
                self._render_watch_card(chat_id, message_id, text, markup, edit=in_place)
        except Exception as exc:
            logger.error("Watch action %s/%s failed for %s: %s", sub, code, chat_id, exc, exc_info=True)
            self._render_watch_card(
                chat_id, message_id,
                "⚠️ 알림 설정 처리 중 일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
                {"inline_keyboard": [[{"text": "📋 내 알림 목록", "callback_data": "trv:w:l"}]]},
            )

    def _prefs_result_card(self, chat_id: str, track: str, notice: str) -> tuple[str, dict[str, Any]]:
        """Legacy toggles (`trv:w:wb[:s]` / `trv:w:wp[:s]` from older messages) land on the card that owns the
        track (1:1 split): Track A `wb` -> 📰 정기발송 설정, Track B `wp` -> 🔔 맞춤발송 설정."""
        if track == "wb":
            return self.build_delivery_settings_card(chat_id, notice=notice)
        return self.build_watch_list_card(chat_id, notice=notice)

    def _save_preset_watch(
        self, chat_id: str, code: str, arg: str, user_id: Optional[str] = None
    ) -> tuple[str, dict[str, Any]]:
        presets = self.watch_reference_prices(code)
        kind = "DEAL" if arg == "D" else "FLOOR"
        target = presets.get(kind)
        if not target:
            return self.build_watch_manage_card(
                chat_id, code, notice="ℹ️ 이 도시는 기준가 정보가 없어 목표가를 직접 입력해 주세요."
            )
        return self._save_watch(chat_id, code, target, kind, user_id=user_id)

    def _save_watch(
        self, chat_id: str, code: str, target: int, kind: str, user_id: Optional[str] = None
    ) -> tuple[str, dict[str, Any]]:
        """Saves the watch. A live wizard draft of this user/scope (V2 options) is applied atomically in the
        same upsert; without a draft the legacy upsert keeps every stored option untouched."""
        from travel_brief.storage.db import WatchLimitError

        if is_country_watch_code(code):
            existing = self._get_watch(chat_id, code)
            if not (existing and existing.get("is_active")):
                # Legacy country scopes are never newly registered (read-only compatibility only).
                if user_id:
                    self._drop_draft(chat_id, user_id, code)
                return self.build_watch_target_picker_card(notice=COUNTRY_WATCH_RETIRED_NOTICE)
        draft = self._peek_draft(chat_id, user_id, code) if user_id else None
        v2: dict[str, Any] = {}
        if draft is not None:
            if draft.watch_mode == "FIXED":
                reason = self._draft_dates_problem(draft)
                if reason:
                    self._set_chat_state(chat_id, STATE_AWAITING_WATCH_DATES, user_id=user_id, dest_code=code)
                    return self.build_watch_dates_prompt_card(code, notice=f"⚠️ {reason}. 날짜를 다시 입력해 주세요.")
                v2 = {"watch_mode": "FIXED", "fixed_dep_date": draft.fixed_dep_date,
                      "fixed_ret_date": draft.fixed_ret_date, "notify_mode": draft.notify_mode}
            else:
                v2 = {"watch_mode": "FLEX", "horizon_months": draft.horizon_months,
                      "weekday_option": draft.weekday_option, "notify_mode": draft.notify_mode}
        try:
            watch = self.db.upsert_travel_watch(chat_id, code, target, preset_kind=kind, **v2)
        except WatchLimitError as exc:
            text, markup = self.build_watch_list_card(
                chat_id,
                notice=f"⚠️ 특가 알림은 최대 {exc.limit}개까지 설정할 수 있어요. {WATCH_LIMIT_NOTICE}",
            )
            return text, markup
        if draft is not None:
            self._drop_draft(chat_id, user_id, code)
        return self.build_watch_saved_card(watch, title="✅ 특가 알림이 설정되었습니다")

    # ------------------------------------------------------------------
    # Wizard drafts
    # ------------------------------------------------------------------
    def _draft_key(self, chat_id: str, user_id: Optional[str], scope: str) -> tuple[str, str, str]:
        cid = str(chat_id)
        return cid, str(user_id or cid), scope.upper()

    def _peek_draft(self, chat_id: str, user_id: Optional[str], scope: str) -> Optional[WatchDraft]:
        key = self._draft_key(chat_id, user_id, scope)
        with self._seq_lock:
            d = self._watch_drafts.get(key)
            if d is None:
                return None
            if d.expires_at <= time.monotonic():
                self._watch_drafts.pop(key, None)
                return None
            return d

    def _drop_draft(self, chat_id: str, user_id: Optional[str], scope: str) -> None:
        with self._seq_lock:
            self._watch_drafts.pop(self._draft_key(chat_id, user_id, scope), None)

    def _clear_user_watch_state(self, chat_id: str, user_id: Optional[str]) -> int:
        """Reset helper: drops every wizard draft and the pending input state of ONE `(chat_id, user_id)`.
        Unlike `_set_chat_state(..., user_id=None)` other members of a group chat are left untouched."""
        cid = str(chat_id)
        uid = str(user_id or cid)
        with self._seq_lock:
            keys = [k for k in self._watch_drafts if k[0] == cid and k[1] == uid]
            for k in keys:
                self._watch_drafts.pop(k, None)
            self._chat_states.pop((cid, uid), None)
        return len(keys)

    def _get_draft(self, chat_id: str, user_id: Optional[str], scope: str, reset: bool = False) -> WatchDraft:
        """Live draft for (chat, user, scope); seeded from the stored watch (or defaults) when absent/reset."""
        draft = None if reset else self._peek_draft(chat_id, user_id, scope)
        if draft is None:
            draft = WatchDraft(scope=scope.upper(), **WATCH_DEFAULT_DRAFT)
            watch = self._get_watch(chat_id, scope)
            if watch and watch.get("is_active"):
                draft.watch_mode = str(watch.get("watch_mode") or "FLEX").upper()
                draft.horizon_months = int(watch.get("horizon_months") or 3)
                draft.weekday_option = str(watch.get("weekday_option") or "ANY").upper()
                draft.notify_mode = str(watch.get("notify_mode") or "DROP").upper()
                draft.fixed_dep_date = watch.get("fixed_dep_date") or ""
                draft.fixed_ret_date = watch.get("fixed_ret_date") or ""
        key = self._draft_key(chat_id, user_id, scope)
        with self._seq_lock:
            draft.expires_at = time.monotonic() + self.watch_draft_ttl
            self._watch_drafts[key] = draft
            self._watch_drafts.move_to_end(key)
            while len(self._watch_drafts) > MAX_WATCH_DRAFTS:
                self._watch_drafts.popitem(last=False)
        return draft

    @staticmethod
    def _draft_dates_problem(draft: WatchDraft) -> Optional[str]:
        from datetime import date as _date

        if not draft.fixed_dep_date or not draft.fixed_ret_date:
            return "여행 날짜가 아직 없어요"
        try:
            dep, ret = _date.fromisoformat(draft.fixed_dep_date), _date.fromisoformat(draft.fixed_ret_date)
        except ValueError:
            return "날짜 형식이 올바르지 않아요"
        return validate_fixed_dates(dep, ret, kst_today())

    def _apply_draft_option(
        self, chat_id: str, user_id: str, code: str, sub: str, arg: str
    ) -> tuple[str, dict[str, Any]]:
        draft = self._get_draft(chat_id, user_id, code)
        notice = ""
        if sub == "m2":
            draft.watch_mode = WATCH_MODE_CODES[arg]
            if draft.watch_mode == "FIXED":
                if self._draft_dates_problem(draft):
                    self._set_chat_state(chat_id, STATE_AWAITING_WATCH_DATES, user_id=user_id, dest_code=code)
                    return self.build_watch_dates_prompt_card(
                        code, notice="📌 날짜 고정: 정한 날짜의 실시간 요금만 추적해요 (±1일 요금도 함께 비교)."
                    )
                notice = "📌 날짜 고정: 정한 날짜의 실시간 요금만 추적해요 (±1일 요금도 함께 비교)."
            else:
                notice = "📅 기간 탐색: 정한 기간 안에서 가장 싼 날짜를 찾아드려요."
        elif sub == "h":
            draft.watch_mode = "FLEX"
            draft.horizon_months = int(arg)
        elif sub == "d":
            draft.watch_mode = "FLEX"
            draft.weekday_option = WATCH_WEEKDAY_CODES[arg]
        elif sub == "n":
            draft.notify_mode = WATCH_NOTIFY_CODES[arg]
        return self.build_watch_setup_card(chat_id, user_id, code, notice=notice)

    def _confirm_draft_watch(self, chat_id: str, user_id: str, code: str, arg: str) -> tuple[str, dict[str, Any]]:
        refs = self.watch_reference_prices(code)
        kind, target = ("DEAL", refs.get("DEAL")) if arg == "1" else ("BASELINE", refs.get("BASELINE"))
        self._get_draft(chat_id, user_id, code)  # keep (or seed) the draft the user is looking at
        if not target:
            return self.build_watch_setup_card(
                chat_id, user_id, code, notice="ℹ️ 기준가 정보가 없어 목표가를 직접 입력해 주세요."
            )
        return self._save_watch(chat_id, code, int(target), kind, user_id=user_id)

    def _handle_watch_dates_input(
        self, chat_id: str, user_id: str, query: str, ctx: ChatState
    ) -> Optional[tuple[bool, str]]:
        """Consumes date text for the FIXED wizard; returns None to fall through (city / country / ALL)."""
        code = ctx.dest_code
        if _HAS_DIGIT_RE.search(query):
            dep, ret, reason = parse_watch_date_range(query)
            if reason:
                self._send_card(chat_id, self.build_watch_dates_error_card(code, query, reason))
                return True, "travel_watch_dates_invalid"
            draft = self._get_draft(chat_id, user_id, code)
            draft.watch_mode = "FIXED"
            draft.fixed_dep_date, draft.fixed_ret_date = dep.isoformat(), ret.isoformat()
            self._set_chat_state(chat_id, STATE_IDLE, user_id=user_id)
            if self.db is None:
                self._send_card(chat_id, "⚠️ 특가 알림 기능을 일시적으로 사용할 수 없습니다.")
                return True, "travel_watch_unavailable"
            text, markup = self.build_watch_setup_card(
                chat_id, user_id, code, notice="✅ 여행 날짜를 반영했어요. 목표가를 골라 저장해 주세요."
            )
            self._render_watch_card(chat_id, 0, text, markup, edit=False)
            return True, "travel_watch_dates_set"
        folded = query.casefold().replace(" ", "")
        if folded in ALL_WORDS or self.catalog.resolve(query) is not None or self.catalog.resolve_country(query):
            return None
        self._send_card(chat_id, self.build_watch_dates_error_card(code, query, "날짜로 인식할 수 없어요"))
        return True, "travel_watch_dates_invalid"

    def _handle_watch_price_input(
        self, chat_id: str, user_id: str, query: str, ctx: ChatState
    ) -> Optional[tuple[bool, str]]:
        """Returns a result when the text was consumed by the price prompt, or None to fall through
        to the normal search (a recognised city / ALL keyword escapes the prompt)."""
        code = ctx.dest_code
        if looks_like_price_text(query):
            target = parse_watch_price_krw(query)
            if target is None:
                self._send_card(chat_id, self.build_watch_price_error_card(code, query))
                return True, "travel_watch_price_invalid"
            self._set_chat_state(chat_id, STATE_IDLE, user_id=user_id)
            if self.db is None:
                self._send_card(chat_id, "⚠️ 특가 알림 기능을 일시적으로 사용할 수 없습니다.")
                return True, "travel_watch_unavailable"
            text, markup = self._save_watch(chat_id, code, target, "CUSTOM", user_id=user_id)
            self._render_watch_card(chat_id, 0, text, markup, edit=False)
            return True, "travel_watch_saved"
        if query.casefold().replace(" ", "") in ALL_WORDS or self.catalog.resolve(query) is not None:
            return None
        self._send_card(chat_id, self.build_watch_price_error_card(code, query))
        return True, "travel_watch_price_invalid"

    def watch_reference_prices(self, code: str) -> dict[str, int]:
        """FLOOR (바닥가) / DEAL (실속 특가) / BASELINE (평균가) for a curated or fallback city.
        Country scopes use the cheapest member-city benchmark per key (the watch alerts on the cheapest
        member airport). Values outside the allowed watch range are omitted (manual input only)."""
        if is_country_watch_code(code):
            agg: dict[str, int] = {}
            for member in self.catalog.watch_target_codes(code):
                for k, v in self.watch_reference_prices(member).items():
                    agg[k] = min(v, agg.get(k, v))
            return agg
        res = self.catalog.get(code)
        meta = res.meta if res else None
        if meta is None:
            return {}
        baseline = int(meta.flight_baseline_krw or 0)
        floor = int(meta.flight_min_floor_krw or 0) or (round(int(baseline * 0.80), -3) if baseline > 0 else 0)
        deal = int(meta.flight_deal_threshold_krw or 0)
        out: dict[str, int] = {}
        for k, v in (("FLOOR", floor), ("DEAL", deal), ("BASELINE", baseline)):
            if WATCH_PRICE_MIN_KRW <= v <= WATCH_PRICE_MAX_KRW:
                out[k] = v
        return out

    def _watch_options_line(self, watch: dict[str, Any]) -> str:
        day = WATCH_DAY_LABELS.get(watch.get("day_pattern") or "ANY", "전체 요일")
        peak = "연휴 포함" if watch.get("include_peak") else "연휴 제외"
        daily = " · 매일 브리핑" if str(watch.get("notify_mode") or "") == "DAILY" else ""
        if str(watch.get("watch_mode") or "") == "FIXED" and watch.get("fixed_dep_date"):
            return f"📌 {_short_iso(watch.get('fixed_dep_date'))}~{_short_iso(watch.get('fixed_ret_date'))}{daily}"
        horizon = watch.get("horizon_months")
        span = ""
        if horizon:
            wopt = WATCH_WEEKDAY_SHORT.get(str(watch.get("weekday_option") or "ANY"), "전체")
            span = f"{int(horizon)}개월·{wopt} · "
        return f"{span}{day} · {peak}{daily}"

    def _watch_schedule_lines(self, watch: dict[str, Any]) -> list[str]:
        if str(watch.get("watch_mode") or "") == "FIXED" and watch.get("fixed_dep_date"):
            dep, ret = watch.get("fixed_dep_date"), watch.get("fixed_ret_date")
            return [f"📌 날짜 고정: {_short_iso(dep)}~{_short_iso(ret)} · {_trip_nights_iso(dep, ret)}박 (±1일 비교)"]
        horizon = int(watch.get("horizon_months") or 6)
        wopt = WATCH_WEEKDAY_LABELS.get(str(watch.get("weekday_option") or "ANY"), "주말+평일 전체")
        return [f"📅 기간 탐색: 앞으로 {horizon}개월 · {wopt}"]

    def build_watch_manage_card(
        self, chat_id: str, code: str, notice: str = ""
    ) -> tuple[str, dict[str, Any]]:
        label = self.label_for(code)
        refs = self.watch_reference_prices(code)
        res = self.catalog.get(code)
        watch = self._get_watch(chat_id, code)
        active = bool(watch and watch.get("is_active"))
        lines = []
        if notice:
            lines.append(notice)
        lines += [f"🔔 <b>[{html.escape(label)} 특가 알림 설정]</b>", "━━━━━━━━━━━━━━━━━━"]
        if refs:
            basis = "" if (res and res.metadata_quality == "CURATED") else " · 기본 메타 기준 추정"
            lines.append(f"📊 <b>참고 시세</b> (왕복 직항 1인{basis})")
            if "FLOOR" in refs:
                lines.append(f"• 바닥가: {_man_label(refs['FLOOR'])}")
            if "DEAL" in refs:
                lines.append(f"• 실속 특가: {_man_label(refs['DEAL'])}")
            if "BASELINE" in refs:
                lines.append(f"• 평균가: {_man_label(refs['BASELINE'])}")
        else:
            lines.append("📊 이 도시는 참고 시세가 없어요. 목표가를 직접 입력해 주세요.")
        if active:
            lines.append(
                f"✅ 현재 알림: <b>{int(watch['target_price_krw']):,}원 이하</b> · {self._watch_options_line(watch)}"
            )
        lines.append("👇 네이버 실시간 최저가가 목표가 이하로 떨어지면 알려드려요.")
        lines.append("💡 실시간 관측 요금만 알림 대상이며, 결제 단계에서 변동될 수 있어요.")

        rows: list[list[dict[str, Any]]] = []
        preset_row = []
        if "DEAL" in refs:
            preset_row.append({"text": f"🎯 실속 특가 {_man_label(refs['DEAL'])} 이하", "callback_data": f"trv:w:p:{code}:D"})
        if "FLOOR" in refs:
            preset_row.append({"text": f"🪙 바닥가 {_man_label(refs['FLOOR'])} 이하", "callback_data": f"trv:w:p:{code}:F"})
        if preset_row:
            rows.append(preset_row)
        rows.append([{"text": "✍️ 목표가 직접 입력", "callback_data": f"trv:w:i:{code}"}])
        rows.append([{"text": "⚙️ 기간·평일/주말·날짜 고정·매일 브리핑", "callback_data": f"trv:w:s:{code}"}])
        if active:
            day = WATCH_DAY_LABELS.get(watch.get("day_pattern") or "ANY", "전체 요일")
            peak = "포함" if watch.get("include_peak") else "제외"
            rows.append([
                {"text": f"🗓️ 요일: {day}", "callback_data": f"trv:w:t:{code}:day"},
                {"text": f"🎉 연휴: {peak}", "callback_data": f"trv:w:t:{code}:pk"},
            ])
            rows.append([{"text": "🔕 알림 끄기", "callback_data": f"trv:w:x:{code}"}])
        rows.append([{"text": "📋 내 알림 목록", "callback_data": "trv:w:l"}])
        return "\n".join(lines), {"inline_keyboard": rows}

    def build_watch_saved_card(self, watch: dict[str, Any], title: str) -> tuple[str, dict[str, Any]]:
        code = watch["dest_code"]
        label = self.label_for(code)
        kind = WATCH_PRESET_LABELS.get(watch.get("preset_kind") or "", "")
        day = WATCH_DAY_LABELS.get(watch.get("day_pattern") or "ANY", "전체 요일")
        peak = "포함" if watch.get("include_peak") else "제외"
        fixed = str(watch.get("watch_mode") or "") == "FIXED" and bool(watch.get("fixed_dep_date"))
        daily = str(watch.get("notify_mode") or "") == "DAILY"
        lines = [
            f"{title}",
            f"🔔 <b>[{html.escape(label)}]</b>",
            "━━━━━━━━━━━━━━━━━━",
            f"🎯 목표가: <b>{int(watch['target_price_krw']):,}원 이하</b>" + (f" ({kind})" if kind else ""),
        ]
        lines += self._watch_schedule_lines(watch)
        if not fixed:
            lines += [f"🗓️ 요일: {day}", f"🎉 연휴·성수기: {peak}"]
        lines.append(f"🔔 알림: {WATCH_NOTIFY_LABELS['DAILY' if daily else 'DROP']}")
        if daily:
            lines.append("⏰ 매일 아침 7:30 실시간 최저가 브리핑 (놓친 날은 보충 발송하지 않아요).")
        elif fixed:
            lines.append("⏰ 목표가 이하이거나 직전 알림보다 1만 원 이상 더 내리면 알려드려요.")
        else:
            lines.append("⏰ 실시간 관측 최저가가 목표가 이하가 되면 알려드려요 (같은 일정은 7일에 1번).")
        if is_country_watch_code(code):
            lines.append("🌏 국가 전체: 여러 공항 중 가장 싼 실시간 요금으로 알려드려요.")
        rows: list[list[dict[str, Any]]] = []
        if not fixed:
            rows.append([{"text": f"🗓️ 요일: {day} (변경)", "callback_data": f"trv:w:t:{code}:day"}])
            rows.append([{"text": f"🎉 연휴·성수기: {peak} (변경)", "callback_data": f"trv:w:t:{code}:pk"}])
        rows += [
            [
                {"text": "✍️ 목표가 변경", "callback_data": f"trv:w:i:{code}"},
                {"text": "🔕 알림 끄기", "callback_data": f"trv:w:x:{code}"},
            ],
            [{"text": "⚙️ 기간·날짜·알림 방식 변경", "callback_data": f"trv:w:e:{code}"}],
            [{"text": "📋 내 알림 목록", "callback_data": "trv:w:l"}],
        ]
        return "\n".join(lines), {"inline_keyboard": rows}

    def build_watch_list_card(self, chat_id: str, notice: str = "") -> tuple[str, dict[str, Any]]:
        from travel_brief.storage.db import WATCH_MAX_ACTIVE_PER_CHAT

        # Grandfathering: never sliced — chats holding more than the cap still see (and can edit/delete) all.
        watches = self.db.list_travel_watches(chat_id=chat_id, active_only=True)
        prefs = self._get_prefs(chat_id)
        paused = bool(prefs.get("watches_paused", False))
        lines = [DORMANT_BANNER] if self._is_dormant(chat_id, prefs, len(watches)) else []
        if notice:
            lines.append(notice)
        lines += [
            f"🔔 <b>[맞춤발송 설정]</b> ({len(watches)}/{WATCH_MAX_ACTIVE_PER_CHAT})",
            "━━━━━━━━━━━━━━━━━━",
        ]
        if not watches:
            # Never "일시중지"/"켜짐" with 0 watches, even if the paused flag is still set.
            lines.append("⚪ 등록된 도시가 없어 맞춤발송이 나가지 않아요. 아래 [➕ 도시 추가하기]로 등록해 보세요!")
        elif paused:
            lines.append("⏸️ 맞춤발송이 일시중지 중이에요. [▶️ 다시 받기]를 누르면 재개돼요.")
        else:
            lines.append(f"🟢 아래 도시의 목표가 도달 시 매일 3회({WATCH_CHECK_SCHEDULE}) 체크해 알려드려요.")
        if watches:
            lines.append("━━━━━━━━━━━━━━━━━━")
        rows: list[list[dict[str, Any]]] = []
        for i, w in enumerate(watches, 1):
            label = self.label_for(w["dest_code"])
            seen = w.get("last_seen_price_krw")
            seen_txt = f" · 최근 {_man_label(int(seen))}" if seen else ""
            lines.append(
                f"{i}. {html.escape(label)} · {_man_label(int(w['target_price_krw']))} 이하 · "
                f"{self._watch_options_line(w)}{seen_txt}"
            )
            settings_sub = "s" if is_country_watch_code(w["dest_code"]) else "m"
            rows.append([
                {"text": f"⚙️ {label}", "callback_data": f"trv:w:{settings_sub}:{w['dest_code']}"},
                {"text": "🗑️", "callback_data": f"trv:w:x:{w['dest_code']}"},
            ])
        if len(watches) >= WATCH_MAX_ACTIVE_PER_CHAT and WATCH_LIMIT_NOTICE not in notice:
            lines.append(f"ℹ️ {WATCH_LIMIT_NOTICE}")
        if len(watches) < 3:
            watched = {w["dest_code"] for w in watches}
            quick = [c for c in WATCH_QUICK_ADD_CODES if c not in watched and self.catalog.get(c)][:3]
            if quick:
                rows.append([{"text": f"➕ {self.label_for(c)}", "callback_data": f"trv:w:m:{c}"} for c in quick])
        if len(watches) < WATCH_MAX_ACTIVE_PER_CHAT:
            rows.append([{"text": "➕ 도시 추가하기", "callback_data": "trv:w:g"}])
        if watches:  # pause / reset only make sense with >= 1 watch (grandfathered > cap included)
            rows.append([
                {"text": "▶️ 다시 받기" if paused else "⏸️ 일시중지", "callback_data": "trv:w:wp"},
                {"text": "🧹 초기화", "callback_data": "trv:w:z"},
            ])
        rows.append([
            {"text": "📰 정기발송 설정", "callback_data": "trv:m:settings"},
            {"text": "🏠 메뉴", "callback_data": "trv:m:home"},
        ])
        return "\n".join(lines), {"inline_keyboard": rows}

    def _get_prefs(self, chat_id: str) -> dict[str, Any]:
        defaults = {
            "weekly_brief_enabled": True, "watches_paused": False, "weekly_brief_freq": "THU_1X",
            "weekly_brief_region": "ALL", "weekly_brief_theme": "ALL",
        }
        getter = getattr(self.db, "get_travel_user_prefs", None)
        if getter is None:
            return defaults
        try:
            return getter(chat_id)
        except Exception as exc:
            logger.warning("Travel prefs lookup failed for %s: %s", chat_id, exc)
            return defaults

    def build_watch_reset_confirm_card(self, chat_id: str) -> tuple[str, dict[str, Any]]:
        """[TG-04] In-place 2-step confirm for the destructive reset (`trv:w:z` -> `trv:w:Z`)."""
        count = len(self.db.list_travel_watches(chat_id=chat_id, active_only=True))
        text = (
            "🧹 <b>[맞춤 특가 알림 초기화]</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"설정된 알림 <b>{count}개</b>를 모두 끄고 일시중지 상태도 풀어요.\n"
            "📰 정기 여행정보 발송 설정(주기·권역·스타일)은 그대로 유지돼요.\n"
            "정말 초기화할까요?"
        )
        return text, {
            "inline_keyboard": [[
                {"text": "✅ 네, 초기화", "callback_data": "trv:w:Z"},
                {"text": "↩️ 취소/목록", "callback_data": "trv:w:l"},
            ]]
        }

    def build_watch_target_picker_card(self, notice: str = "") -> tuple[str, dict[str, Any]]:
        """Watch add entry: shared 4x2 region grid -> 3x3 city pages, plus direct city key-in.
        Legacy `@JP`-style country scopes are intentionally NOT offered for new registration."""
        lines = [notice] if notice else []
        lines += [
            WATCH_STEP1_HEADER,
            "━━━━━━━━━━━━━━━━━━",
            WATCH_STEP1_GUIDE,
            "🗺️ 권역을 고르면 인천 직항 도시 목록이 나와요.",
        ]
        rows = self.build_region_grid("w")
        rows.append([
            {"text": "📋 내 알림", "callback_data": "trv:w:l"},
            {"text": "🏠 메뉴", "callback_data": "trv:m:home"},
        ])
        return "\n".join(lines), {"inline_keyboard": rows}

    def build_watch_setup_card(
        self, chat_id: str, user_id: Optional[str], code: str, notice: str = ""
    ) -> tuple[str, dict[str, Any]]:
        """V2 setup hub: scope, baseline context, current choices and one-tap options (mobile-first)."""
        draft = self._get_draft(chat_id, user_id, code)
        label = self.label_for(code)
        refs = self.watch_reference_prices(code)
        watch = self._get_watch(chat_id, code)
        active = bool(watch and watch.get("is_active"))
        country = is_country_watch_code(code)
        lines = [notice] if notice else []
        if active:
            lines += [f"🔔 <b>[맞춤발송 수정 · {html.escape(label)}]</b>", "━━━━━━━━━━━━━━━━━━"]
        else:
            lines += [
                f"🔔 <b>[맞춤발송 등록 · 2/2단계: {html.escape(label)} 목표가 입력]</b>",
                "━━━━━━━━━━━━━━━━━━",
                WATCH_STEP2_GUIDE,
            ]
        if country:
            names = [self.label_for(c).split(" ", 1)[-1] for c in self.catalog.watch_target_codes(code)]
            lines.append(f"📍 대상: {html.escape(' · '.join(names))} ({len(names)}곳 중 최저가)")
        if refs:
            parts = []
            for key, name in (("FLOOR", "바닥"), ("DEAL", "실속"), ("BASELINE", "평균")):
                if key in refs:
                    parts.append(f"{name} {_man_label(refs[key])}")
            basis = " · 국가 내 최저 기준" if country else ""
            lines.append(f"📊 참고 시세(왕복 직항 1인{basis}): {' · '.join(parts)}")
        else:
            lines.append("📊 참고 시세가 없어요. 목표가를 직접 입력해 주세요.")
        if active:
            lines.append(f"✅ 현재 알림: <b>{int(watch['target_price_krw']):,}원 이하</b>")
            seen = watch.get("last_seen_price_krw")
            if seen:
                lines.append(f"👀 최근 실시간 관측 최저가: {int(seen):,}원")
        lines.append("━━ 선택한 조건 ━━")
        if draft.watch_mode == "FIXED":
            if draft.fixed_dep_date and draft.fixed_ret_date:
                nights = _trip_nights_iso(draft.fixed_dep_date, draft.fixed_ret_date)
                lines.append(
                    f"📌 날짜 고정: {_short_iso(draft.fixed_dep_date)}~{_short_iso(draft.fixed_ret_date)} · {nights}박"
                )
            else:
                lines.append("📌 날짜 고정: 날짜 입력 전")
            lines.append("💡 정한 날짜의 실시간 요금과 ±1일 요금을 함께 비교해 드려요.")
        else:
            wlabel = WATCH_WEEKDAY_LABELS.get(draft.weekday_option, "주말+평일 전체")
            lines.append(f"📅 기간 탐색: 앞으로 {draft.horizon_months}개월 · {wlabel} (3박)")
            if draft.horizon_months > 6:
                lines.append("ℹ️ 9~12개월 먼 일정은 LCC 특가가 아직 미오픈일 수 있어요.")
        lines.append(f"🔔 알림: {WATCH_NOTIFY_LABELS.get(draft.notify_mode, WATCH_NOTIFY_LABELS['DROP'])}")
        lines.append("👇 조건을 고른 뒤 [💾 저장] 버튼을 눌러 주세요. 실시간 검증 요금만 알려드려요.")

        def mark(selected: bool, text: str) -> str:
            return f"✅ {text}" if selected else text

        flex = draft.watch_mode != "FIXED"
        rows: list[list[dict[str, Any]]] = [[
            {"text": mark(flex, "📅 기간 탐색"), "callback_data": f"trv:w:m2:{code}:F"},
            {"text": mark(not flex, "📌 날짜 고정"), "callback_data": f"trv:w:m2:{code}:X"},
        ]]
        if flex:
            rows.append([
                {"text": mark(draft.horizon_months == h, f"{h}개월"), "callback_data": f"trv:w:h:{code}:{h}"}
                for h in WATCH_HORIZON_CHOICES
            ])
            rows.append([
                {"text": mark(draft.weekday_option == opt, WATCH_WEEKDAY_SHORT[opt]),
                 "callback_data": f"trv:w:d:{code}:{c}"}
                for c, opt in WATCH_WEEKDAY_CODES.items()
            ])
        else:
            rows.append([{"text": "🗓️ 여행 날짜 입력·변경", "callback_data": f"trv:w:f:{code}"}])
        rows.append([
            {"text": mark(draft.notify_mode == "DROP", "🔔 하락 시 알림"), "callback_data": f"trv:w:n:{code}:D"},
            {"text": mark(draft.notify_mode == "DAILY", "📰 매일 아침"), "callback_data": f"trv:w:n:{code}:Y"},
        ])
        # Compact 2-column save / input rows (mobile: fewer rows, same callbacks).
        save_row: list[dict[str, Any]] = []
        if "DEAL" in refs:
            save_row.append({"text": f"💾 실속 {_man_label(refs['DEAL'])} 이하",
                             "callback_data": f"trv:w:k:{code}:1"})
        if "BASELINE" in refs:
            save_row.append({"text": f"💾 평균 {_man_label(refs['BASELINE'])} 이하",
                             "callback_data": f"trv:w:k:{code}:W"})
        if save_row:
            rows.append(save_row)
        input_row = [{"text": "✍️ 목표가 직접 입력", "callback_data": f"trv:w:i:{code}"}]
        if active:
            input_row.append({"text": "🔕 알림 끄기", "callback_data": f"trv:w:x:{code}"})
        rows.append(input_row)
        rows.append([
            {"text": "📋 내 알림", "callback_data": "trv:w:l"},
            {"text": "🌏 대상 바꾸기", "callback_data": "trv:w:g"},
        ])
        return "\n".join(lines), {"inline_keyboard": rows}

    def build_watch_dates_prompt_card(self, code: str, notice: str = "") -> tuple[str, dict[str, Any]]:
        label = self.label_for(code)
        minutes = max(1, int(self.chat_state_ttl // 60))
        lines = [notice] if notice else []
        lines += [
            f"🗓️ <b>[{html.escape(label)} 여행 날짜 입력]</b>",
            "━━━━━━━━━━━━━━━━━━",
            f"출발일~귀국일을 채팅창에 입력해 주세요 ({minutes}분 안에).",
            "예) <code>2027-05-01~2027-05-04</code> · <code>5.1~5.4</code> · <code>270501 270504</code>",
            "• 오늘 이후 출발 · 1~30박 · 연도를 생략하면 가장 가까운 미래 날짜",
            "• 취소: /cancel",
        ]
        return "\n".join(lines), {"inline_keyboard": [[{"text": "↩️ 돌아가기", "callback_data": f"trv:w:s:{code}"}]]}

    def build_watch_dates_error_card(self, code: str, raw: str, reason: str) -> str:
        shown = raw if len(raw) <= 30 else raw[:29] + "…"
        label = self.label_for(code) if code else "선택한 지역"
        return (
            f"⚠️ '<b>{html.escape(shown)}</b>' — {html.escape(reason)}.\n"
            f"{html.escape(label)} 날짜를 <code>2027-05-01~2027-05-04</code> 또는 <code>5.1~5.4</code> 처럼 입력해 주세요.\n"
            "• 오늘 이후 출발 · 1~30박 · 취소: /cancel"
        )

    def build_watch_input_card(self, code: str) -> tuple[str, dict[str, Any]]:
        label = self.label_for(code)
        minutes = max(1, int(self.chat_state_ttl // 60))
        text = (
            f"✍️ <b>[{html.escape(label)} 목표가 직접 입력]</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            f"왕복 항공권 목표가를 채팅창에 입력해 주세요 ({minutes}분 안에).\n"
            "예) <code>25</code> · <code>25만</code> · <code>250000</code>\n"
            "• 허용 범위: 5만 ~ 500만 원\n"
            "• 취소: /cancel"
        )
        return text, {"inline_keyboard": [[{"text": "↩️ 취소", "callback_data": f"trv:w:m:{code}"}]]}

    def build_watch_price_error_card(self, code: str, raw: str) -> str:
        shown = raw if len(raw) <= 20 else raw[:19] + "…"
        label = self.label_for(code) if code else "선택한 도시"
        return (
            f"⚠️ '<b>{html.escape(shown)}</b>'은(는) 목표가로 인식할 수 없어요.\n"
            f"{html.escape(label)} 목표가를 <code>25</code>, <code>25만</code>, <code>250000</code> 처럼 입력해 주세요.\n"
            "• 허용 범위: 5만 ~ 500만 원 · 취소: /cancel"
        )

    def build_watch_delete_confirm_card(self, code: str) -> tuple[str, dict[str, Any]]:
        label = self.label_for(code)
        text = f"🗑️ 정말 <b>{html.escape(label)}</b> 알림을 삭제할까요?"
        return text, {
            "inline_keyboard": [[
                {"text": "✅ 네, 삭제", "callback_data": f"trv:w:X:{code}"},
                {"text": "↩️ 취소/목록", "callback_data": "trv:w:l"},
            ]]
        }

    def _is_watch_card(self, chat_id: str, message_id: int) -> bool:
        with self._seq_lock:
            return (str(chat_id), int(message_id or 0)) in self._watch_cards

    def _remember_watch_card(self, chat_id: str, message_id: Any) -> None:
        try:
            mid = int(message_id)
        except (TypeError, ValueError):
            return
        if mid <= 0:
            return
        key = (str(chat_id), mid)
        with self._seq_lock:
            self._watch_cards[key] = None
            self._watch_cards.move_to_end(key)
            while len(self._watch_cards) > MAX_TRACKED_WATCH_CARDS:
                self._watch_cards.popitem(last=False)

    def _render_watch_card(
        self, chat_id: str, message_id: int, text: str, markup: dict[str, Any], edit: bool = True
    ) -> None:
        """Edits watch cards in place; entry points from search results / alerts open a fresh card so the
        original briefing or alert stays readable."""
        if edit and message_id and message_id > 0:
            seq = self._next_request_seq(chat_id, message_id)
            if self._edit_if_latest(chat_id, message_id, seq, text, markup):
                self._remember_watch_card(chat_id, message_id)
            return
        self._remember_watch_card(chat_id, self._send_card(chat_id, text, markup))

    # ------------------------------------------------------------------
    # Search pipeline
    # ------------------------------------------------------------------
    def _invalidate_scope(self, scope: str) -> None:
        codes = self.catalog.curated_codes if scope == "ALL" else {scope}
        try:
            if hasattr(self.engine, "invalidate_scope"):
                self.engine.invalidate_scope(codes)
            elif getattr(self.engine, "_live_client", None):
                self.engine._live_client.invalidate_cache()
            else:
                NaverFlightLiveClient.invalidate_global_cache()
        except Exception as exc:
            logger.warning("Scope cache invalidation failed for %s: %s", scope, exc)

    def _launch_search(
        self,
        chat_id: str,
        message_id: int,
        target: str,
        user_id: str,
        is_refresh: bool = False,
        show_loading: bool = True,
    ) -> None:
        key = (str(chat_id), int(message_id or 0))
        # A menu/watch card turned into a search result is no longer a navigation card: later menu taps on it
        # open a fresh card so the result stays readable.
        with self._seq_lock:
            self._watch_cards.pop(key, None)
        request_seq = self._next_request_seq(chat_id, message_id)
        markup = self.build_inline_keyboard(current_filter=target, chat_id=chat_id)

        loading_future: Optional[concurrent.futures.Future] = None
        if show_loading:
            try:
                loading_future = self.ui_pool.submit(
                    self._edit_if_latest,
                    chat_id=chat_id,
                    message_id=message_id,
                    request_seq=request_seq,
                    text=self.build_loading_card(target, is_refresh=is_refresh),
                    reply_markup=markup,
                    is_loading=True,
                )
            except Exception as exc:
                logger.warning("Loading card submission skipped: %s", exc)

        self._worker_enter(key)
        try:
            self.worker_pool.submit(
                self._async_execute_and_update,
                chat_id=chat_id,
                message_id=message_id,
                target_city=target,
                user_id=user_id,
                request_seq=request_seq,
                loading_future=loading_future,
                _tracked=True,
                send_photo=(target != "ALL" and not is_refresh),
            )
        except Exception as exc:
            logger.error("Worker submission failed for %s: %s", user_id, exc)
            self._worker_exit(key)
            self.cooldown.release(user_id)
            if loading_future is not None:
                loading_future.cancel()
            self._edit_if_latest(
                chat_id, message_id, request_seq,
                self.build_error_card(target, "조회 작업을 시작하지 못했습니다."),
                markup,
            )
            return
        self._arm_watchdog(chat_id, message_id, request_seq, target, user_id)

    def _fetch_fx_map(self, recs: list[TravelRecommendation]) -> dict:
        if not self.fx_enabled or not recs:
            return {}
        try:
            return self.fx_client.get_many([r.destination.currency for r in recs[:3]])
        except Exception as exc:
            logger.info("FX pre-fetch skipped (fail-open): %s", exc)
            return {}

    def _async_execute_and_update(
        self,
        chat_id: str,
        message_id: int,
        target_city: str,
        user_id: str,
        request_seq: Optional[int] = None,
        loading_future: Optional[concurrent.futures.Future] = None,
        _tracked: bool = False,
        send_photo: bool = False,
    ) -> None:
        """Runs in background worker thread to collect deals and edit Telegram message in-place.
        Always ends with either the briefing or an error card (never a stuck loading card).
        `send_photo` (fresh single-city search only) adds a best-effort reference photo AFTER the briefing.
        """
        key = (str(chat_id), int(message_id or 0))
        markup = self.build_inline_keyboard(current_filter=target_city, chat_id=chat_id)
        try:
            target_dest = None if target_city == "ALL" else target_city
            custom = self.catalog.custom_destinations_for(target_dest) if target_dest else None
            search_kwargs: dict[str, Any] = {"dest_code": target_dest, "months_ahead": 6, "fast_live": True}
            if custom:
                search_kwargs["custom_destinations"] = custom
            all_deals = self.engine.search_deals(**search_kwargs)
            dest_map = {**custom, **self.engine.destinations} if custom else self.engine.destinations
            recs = BudgetCalculator.rank_recommendations(
                destinations=dest_map,
                deals=all_deals,
                top_n=3,
            )

            # SSE-verify the hero deal and re-rank (a price surge may promote another date)
            if recs and getattr(self.engine, "live_naver", False):
                recs = self._verify_hero_with_rerank(all_deals, recs, custom_destinations=custom)

            # M-D fix: never overwrite with blank text, but never leave the loading card stuck either
            if not recs:
                logger.warning("On-demand query yielded empty recommendations for target %s.", target_city)
                self._finish_with_card(
                    chat_id, message_id, request_seq, loading_future,
                    self.build_error_card(target_city, "현재 조회 가능한 직항 특가가 없습니다."),
                    markup,
                )
                return

            fx_map = self._fetch_fx_map(recs)

            from travel_bot_runner import format_telegram_briefing
            format_kwargs: dict[str, Any] = {
                "recommendations": recs[:3],
                "web_report_url": self.report_url,
                "engine": self.engine,
                "target_city": target_city,
            }
            if fx_map:
                format_kwargs["fx_map"] = fx_map
            new_text = format_telegram_briefing(**format_kwargs)

            # Safe in-place edit (M-E), generation-guarded
            edited = self._finish_with_card(chat_id, message_id, request_seq, loading_future, new_text, markup)
            if send_photo and target_dest and edited is not False:
                self._submit_city_photo(chat_id, target_dest, recs[0])
        except Exception as exc:
            logger.error("Background on-demand update failed for user %s: %s", user_id, exc, exc_info=True)
            try:
                self._finish_with_card(
                    chat_id, message_id, request_seq, loading_future,
                    self.build_error_card(target_city, "네이버 항공권 조회 중 일시적인 오류가 발생했습니다."),
                    markup,
                )
            except Exception as card_exc:
                logger.error("Error card edit failed for user %s: %s", user_id, card_exc)
        finally:
            self.cooldown.release(user_id)
            if _tracked:
                self._worker_exit(key)
            self._disarm_watchdog(key, request_seq)

    def _finish_with_card(
        self,
        chat_id: str,
        message_id: int,
        request_seq: Optional[int],
        loading_future: Optional[concurrent.futures.Future],
        text: str,
        markup: dict[str, Any],
    ) -> None:
        # Cancel a not-yet-started loading edit; if it is already running, the per-message lock and
        # done-generation marker in `_edit_if_latest` guarantee it can never overwrite this result.
        if loading_future is not None:
            loading_future.cancel()
        return self._edit_if_latest(
            chat_id=chat_id,
            message_id=message_id,
            request_seq=request_seq,
            text=text,
            reply_markup=markup,
        )

    # ------------------------------------------------------------------
    # Best-effort city detail photo (never blocks / fails the text briefing)
    # ------------------------------------------------------------------
    def resolve_city_photo(self, target_code: str, rec: Optional[TravelRecommendation] = None) -> Optional[DestinationPhoto]:
        """Curated season-matched reference photo for `target_code` (fallback cities -> None, never another
        city's photo). Season/date come from the TOP recommendation only when it is the same city."""
        same_city = rec is not None and getattr(rec.destination, "code", None) == target_code
        try:
            return resolve_destination_photo(
                target_code,
                season_status=rec.season_status if same_city else "NORMAL",
                dep_date=rec.best_deal.departure_date if same_city else None,
                dest_name=rec.destination.name if same_city else "",
            )
        except Exception as exc:
            logger.info("City photo resolve skipped for %s: %s", target_code, exc)
            return None

    def _submit_city_photo(self, chat_id: str, target_code: str, rec: Optional[TravelRecommendation]) -> bool:
        if not self._photo_enabled or self._closed:
            return False
        photo = self.resolve_city_photo(target_code, rec)
        if photo is None:
            return False
        caption = f"{html.escape(photo.season_tag)} · <i>{html.escape(photo.caption)}</i>"
        try:
            # Separate UI task: no generation guard / watchdog / worker accounting is attached to it, and it
            # is never run inline (a saturated/closed pool just skips the photo).
            self.ui_pool.submit(self._send_city_photo, str(chat_id), photo, caption)
            return True
        except Exception as exc:
            logger.info("City photo task skipped for %s: %s", target_code, exc)
            return False

    def _send_city_photo(self, chat_id: str, photo: DestinationPhoto, caption: str) -> Optional[str]:
        try:
            status, _msg_id, err = self.dispatcher.send_photo_to_recipient(
                self.tg, chat_id, photo.path, caption=caption, parse_mode="HTML"
            )
        except Exception as exc:
            logger.info("City photo send failed for %s (best-effort): %s", chat_id, exc)
            return "ERROR"
        if status != "SENT":
            logger.info("City photo %s for %s (not retried): %s", status, chat_id, err)
        return status

    def _verify_hero_with_rerank(
        self,
        deals: list[Any],
        recs: list[TravelRecommendation],
        custom_destinations: Optional[dict] = None,
    ) -> list[TravelRecommendation]:
        """SSE-verifies the #1 deal, re-ranks, and repeats if a different unverified deal becomes #1.
        On SSE timeout/empty the deal stays LIVE_PG and is labelled as price-graph data by the formatter.
        """
        dest_map = (
            {**custom_destinations, **self.engine.destinations} if custom_destinations else self.engine.destinations
        )
        current = list(deals)
        attempted: set[tuple[str, str, str]] = set()
        for _ in range(SSE_RERANK_ROUNDS):
            if not recs:
                break
            hero = recs[0].best_deal
            k = (hero.destination_code, hero.departure_date, hero.return_date)
            if hero.data_source != "LIVE_PG" or k in attempted:
                break
            attempted.add(k)
            try:
                if custom_destinations:
                    verified = self.engine.ensure_deals_sse_verified([hero], custom_destinations=custom_destinations)
                else:
                    verified = self.engine.ensure_deals_sse_verified([hero])
            except Exception as exc:
                logger.warning("Hero SSE verification failed for %s: %s", k, exc)
                break
            if not verified or verified[0].data_source != "LIVE_SSE":
                break
            upgraded = verified[0]
            current = [
                upgraded if (d.destination_code, d.departure_date, d.return_date) == k else d
                for d in current
            ]
            recs = BudgetCalculator.rank_recommendations(
                destinations=dest_map,
                deals=current,
                top_n=3,
            )
        return recs

    # ------------------------------------------------------------------
    # Telegram I/O
    # ------------------------------------------------------------------
    def _answer(self, cq_id: str, text: str) -> None:
        try:
            self.tg.answer_callback(cq_id, text=text, show_alert=False)
        except Exception as exc:
            logger.warning("answerCallbackQuery failed for %s: %s", cq_id, exc)

    def _submit_ui(self, fn: Callable[..., Any], *args: Any) -> None:
        try:
            self.ui_pool.submit(fn, *args)
        except Exception as exc:
            logger.warning("UI task submission failed, running inline: %s", exc)
            fn(*args)

    def _submit_nav_then_ack(self, cq_id: str, toast: str, fn: Callable[..., Any], *args: Any) -> None:
        """Read/navigation callbacks: submit the UI edit FIRST, then answerCallbackQuery so both HTTP calls
        run in parallel. If the task ends up running inline on this thread (pool closed/full -> `_submit_ui`
        fallback), the ack is sent BEFORE the inline work, so the spinner is never held by the edit.
        Exactly one ack either way."""
        caller = threading.get_ident()
        ack_lock = threading.Lock()
        acked = [False]

        def ack_once() -> None:
            with ack_lock:
                if acked[0]:
                    return
                acked[0] = True
            self._answer(cq_id, toast)

        def task(*a: Any) -> None:
            if threading.get_ident() == caller:
                ack_once()  # inline fallback: ack first (TG-04)
            fn(*a)

        try:
            self._submit_ui(task, *args)
        finally:
            ack_once()

    def _send_card(
        self, chat_id: str, text: str, reply_markup: Optional[dict[str, Any]] = None
    ) -> Optional[str]:
        """sendMessage with HTML → plain-text fallback. Returns the new message_id or None."""
        try:
            return self.tg.send_text(chat_id, text, parse_mode="HTML", reply_markup=reply_markup)
        except Exception as exc:
            logger.warning("HTML send_text failed for %s: %s; retrying as plain text.", chat_id, exc)
        try:
            return self.tg.send_text(chat_id, html_to_plain(text), parse_mode="", reply_markup=reply_markup)
        except Exception as exc:
            logger.error("Plain send_text fallback failed for %s: %s", chat_id, exc)
            return None

    def _safe_edit_message(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Edit with retry-after handling, not-modified idempotency, and HTML → plain send fallback (M-E)."""
        return self._edit_message_core(chat_id, message_id, text, reply_markup, allow_send_fallback=True)

    def _edit_in_place_only(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> bool:
        """Edit-only variant for transient loading cards: never sends a new message on failure."""
        return self._edit_message_core(chat_id, message_id, text, reply_markup, allow_send_fallback=False)

    def _get_http_session(self) -> Any:
        """Thread-safe lazy keep-alive session; None when `requests` is unavailable or shut down."""
        if self._closed:
            return None
        with self._http_session_lock:
            if self._http_session is None:
                try:
                    import requests
                    from requests.adapters import HTTPAdapter
                except ImportError:
                    return None
                sess = requests.Session()
                adapter = HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=0)
                sess.mount("https://", adapter)
                sess.headers.update({"Content-Type": "application/json"})
                self._http_session = sess
            return self._http_session

    def _post_edit(self, url: str, payload: bytes) -> dict[str, Any]:
        """POST editMessageText; HTTP errors are raised as urllib.error.HTTPError for one error path."""
        session = None
        if urllib.request.urlopen is _ORIGINAL_URLOPEN:  # tests that patch urlopen keep the urllib path
            session = self._get_http_session()
        if session is None:
            req = urllib.request.Request(
                url, data=payload, headers={"Content-Type": "application/json"}, method="POST"
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                return json.load(resp)

        resp = session.post(url, data=payload, timeout=10)
        if resp.status_code >= 400:
            raise urllib.error.HTTPError(
                url, resp.status_code, resp.reason or "", resp.headers, io.BytesIO(resp.content or b"")
            )
        return resp.json()

    def _edit_message_core(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: Optional[dict[str, Any]] = None,
        allow_send_fallback: bool = True,
    ) -> bool:
        if not chat_id:
            return False

        body: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            body["reply_markup"] = reply_markup

        if message_id and message_id > 0:
            body["message_id"] = message_id
            edit_url = f"https://api.telegram.org/bot{self.bot_token}/editMessageText"
            payload = json.dumps(body).encode("utf-8")
            for attempt in range(2):
                try:
                    data = self._post_edit(edit_url, payload)
                    if data.get("ok"):
                        return True
                    logger.warning("editMessageText rejected: %s", data.get("description", ""))
                    break
                except urllib.error.HTTPError as http_err:
                    err_desc = ""
                    try:
                        err_json = json.loads(http_err.read().decode("utf-8", errors="ignore"))
                        err_desc = err_json.get("description", "")
                    except Exception:
                        err_desc = str(http_err)

                    if "message is not modified" in err_desc.lower():
                        logger.debug("Message text and keyboard are already up-to-date.")
                        return True

                    if http_err.code == 429 and attempt == 0:
                        m = re.search(r"retry after (\d+)", err_desc.lower())
                        sec = int(m.group(1)) if m else 2
                        time.sleep(min(sec, 10) + 0.5)
                        continue

                    logger.warning("editMessageText failed HTTP %s: %s", http_err.code, err_desc)
                    break
                except Exception as exc:
                    logger.warning("editMessageText network exception: %s", exc)
                    break

        if not allow_send_fallback:
            return False

        # Fallback chain: fresh HTML message → plain-text message
        return self._send_card(chat_id, text, reply_markup) is not None

    def shutdown(self) -> None:
        """Shuts down background workers and watchdog timers cleanly."""
        self._closed = True
        with self._seq_lock:
            for st in self._msg_states.values():
                if st.watchdog is not None:
                    st.watchdog.cancel()
                    st.watchdog = None
        self.worker_pool.shutdown(wait=False)
        try:
            self.ui_pool.shutdown(wait=False, cancel_futures=True)  # queued (not started) UI edits are dropped
        except TypeError:  # Python < 3.9
            self.ui_pool.shutdown(wait=False)
        self._drain_ui_pool(UI_SHUTDOWN_GRACE_SECONDS)
        with self._http_session_lock:
            sess, self._http_session = self._http_session, None
        if sess is not None:
            try:
                sess.close()
            except Exception as exc:
                logger.debug("HTTP session close failed: %s", exc)
        close_tg = getattr(self.tg, "close", None)
        if callable(close_tg):
            try:
                close_tg()
            except Exception as exc:
                logger.debug("Telegram channel close failed: %s", exc)

    def _drain_ui_pool(self, grace: float) -> None:
        """Bounded wait for in-flight UI edits so their HTTP session is not closed under them."""
        deadline = time.monotonic() + max(0.0, grace)
        current = threading.current_thread()
        for t in list(getattr(self.ui_pool, "_threads", ()) or ()):
            if t is current:
                continue
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            t.join(remaining)
