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
from travel_brief.city_catalog import CityCatalog
from travel_brief.flight_engine import FlightEngine
from travel_brief.budget_calculator import BudgetCalculator
from travel_brief.fx_client import FXClient
from travel_brief.models import TravelRecommendation
from travel_brief.naver_live import NaverFlightLiveClient

logger = logging.getLogger(__name__)

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
WATCH_PRESET_LABELS = {"DEAL": "실속 특가", "FLOOR": "바닥가", "CUSTOM": "직접 입력"}
CANCEL_COMMANDS = {"/cancel", "/취소"}

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
SEARCH_ALL_COMMANDS = {"/여행", "/today", "/특가"}
HELP_COMMANDS = {"/help", "/도움말"}
GUIDE_COMMANDS = {"/검색", "/search"}
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
            max_workers=2, thread_name_prefix="TravelUI"
        )
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
        if code in CITY_LABELS:
            return CITY_LABELS[code]
        res = self.catalog.get(code)
        return res.display_name if res else code

    def build_inline_keyboard(self, current_filter: str = "ALL", chat_id: Optional[str] = None) -> dict[str, Any]:
        """Constructs mobile-optimized slim keyboard within 64-byte callback_data limits.
        `chat_id` (optional) only personalises the watch button badge.
        """
        scope = self.normalize_filter(current_filter) or "ALL"
        return {
            "inline_keyboard": [
                [
                    {"text": "🔄 최신 특가 새로고침", "callback_data": f"trv:r:{scope}"},
                ],
                [
                    {"text": "🇯🇵 도쿄", "callback_data": "trv:q:NRT"},
                    {"text": "🇯🇵 오사카", "callback_data": "trv:q:KIX"},
                    {"text": "🇯🇵 후쿠오카", "callback_data": "trv:q:FUK"},
                ],
                [
                    {"text": "🇹🇼 타이베이", "callback_data": "trv:q:TPE"},
                    {"text": "🇻🇳 다낭", "callback_data": "trv:q:DAD"},
                    {"text": "🇹🇭 방콕", "callback_data": "trv:q:BKK"},
                ],
                [self._watch_entry_button(scope, chat_id)],
                [
                    {"text": "🔍 기타 지역 직접 검색", "callback_data": "trv:c:guide"},
                ],
                [
                    {"text": "📊 6개월 특가 웹 리포트 ↗", "url": self.report_url},
                ],
            ]
        }

    def _watch_entry_button(self, scope: str, chat_id: Optional[str] = None) -> dict[str, Any]:
        """ALL never registers a watch: it opens the per-city list. A city opens its own watch card."""
        if scope == "ALL":
            return {"text": "🔔 내 알림 / 도시별 설정", "callback_data": "trv:w:l"}
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
        """Step 1 onboarding target-selection keyboard for new subscribers (64-byte safe `trv:s:<CITY>`)."""
        return {
            "inline_keyboard": [
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
                    {"text": "✈️ 전체 주말 특가 TOP 3 선택", "callback_data": "trv:s:ALL"},
                ],
                [
                    {"text": "🔍 기타 지역 직접 검색", "callback_data": "trv:c:guide"},
                ],
                [
                    {"text": "📊 6개월 특가 웹 리포트 ↗", "url": self.report_url},
                ],
                [
                    {"text": "🏠 통합 메뉴봇 바로가기", "url": "https://t.me/KwMenu_bot?start=from_sub"},
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
            "💡 기본 6개 도시 외 지역은 보수적 기본 예산으로 안내되며, 실시간 항공권 링크를 함께 드립니다."
        )

    def build_help_card(self, unknown_text: str = "") -> str:
        names = " · ".join(self.catalog.suggestion_names(12))
        lines = ["🧭 <b>[트래블 패스파인더 지역 검색 도움말]</b>", "━━━━━━━━━━━━━━━━━━"]
        if unknown_text:
            shown = unknown_text if len(unknown_text) <= 20 else unknown_text[:19] + "…"
            lines.append(f"❓ '<b>{html.escape(shown)}</b>'은(는) 아직 지원하지 않는 지역이에요.")
        lines.extend([
            "✍️ 도시 이름을 그대로 입력해 보세요. 예) <code>삿포로</code>, <code>세부</code>, <code>호치민</code>",
            f"🔎 추천 검색 도시: {html.escape(names)}",
            "📌 /여행 전체 TOP 3 · /help 도움말 · /stop 구독 해지",
        ])
        return "\n".join(lines)

    def build_onboarding_card(self) -> str:
        return (
            "🎉 <b>[✈️ 트래블 패스파인더] 구독이 시작되었습니다!</b>\n"
            "━━━━━━━━━━━━━━━━━━\n"
            "🎯 <b>[1단계: 관심 여행지 선택]</b>\n"
            "아래 버튼에서 원하시는 여행지를 선택하신 후 <b>[📋 직전 발송 특가 정보 보기]</b> 버튼을 누르면 "
            "최신 주말 항공·숙박 특가를 즉시 확인하실 수 있습니다 👇\n"
            "🔍 목록에 없는 도시는 이름을 바로 입력하세요 (예: 삿포로, 세부)."
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
            self._answer(cq_id, "🔍 가고 싶은 도시 이름을 입력해 주세요!")
            self._set_chat_state(chat_id, STATE_AWAITING_CITY, user_id=user_id)
            self._submit_ui(self._send_card, chat_id, self.build_city_guide_card())
            return

        # Price watch flows (`trv:w:*`) never fall into the search/stale path; answered immediately.
        if action == "w":
            self._handle_watch_callback(cq_id, str(chat_id), int(message_id or 0), user_id, parts)
            return

        target_norm = self.normalize_filter(target)
        if target_norm is None:
            self._answer(cq_id, "지원하지 않는 지역입니다. 도시 이름을 직접 입력해 주세요.")
            return

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
                self._send_card(chat_id, self.build_city_guide_card())
                return True, "travel_guide"
            self._send_card(chat_id, self.build_help_card(), self.build_inline_keyboard("ALL"))
            return True, "travel_help"

        query = _MENTION_RE.sub(" ", raw).strip()
        if not query:
            self._send_card(chat_id, self.build_help_card(), self.build_inline_keyboard("ALL"))
            return True, "travel_help"

        ctx = self.get_chat_context(chat_id, user_id)
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
            self._send_card(chat_id, self.build_help_card(query), self.build_inline_keyboard("ALL"))
            return True, "travel_help_unknown_city"
        self._set_chat_state(chat_id, STATE_IDLE, user_id=user_id)
        return self._start_text_search(chat_id, user_id, res.code)

    def _handle_start(
        self, chat_id: str, sender_name: str, on_subscribe: Optional[Callable[[str, str], Any]]
    ) -> tuple[bool, str]:
        if on_subscribe is not None:
            self._call_subscribe(on_subscribe, chat_id, sender_name)
        self._set_chat_state(chat_id, STATE_IDLE)
        self._send_card(chat_id, self.build_onboarding_card(), self.build_onboarding_keyboard())
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
    }

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
        code = ""
        if sub != "l":
            raw = parts[3] if len(parts) > 3 else ""
            norm = self.normalize_filter(raw) if raw else None
            if not norm or norm == "ALL":
                self._answer(cq_id, "지원하지 않는 지역입니다. 도시를 먼저 선택해 주세요.")
                return
            code = norm
        arg = parts[4] if len(parts) > 4 else ""
        if sub == "p" and arg not in ("D", "F"):
            self._answer(cq_id, "지원하지 않는 알림 메뉴입니다.")
            return
        if sub == "t" and arg not in ("day", "pk"):
            self._answer(cq_id, "지원하지 않는 알림 메뉴입니다.")
            return

        self._answer(cq_id, self._WATCH_TOASTS[sub])  # TG-04: ack before any DB / network work
        if sub == "i":
            self._set_chat_state(chat_id, STATE_AWAITING_WATCH_PRICE, user_id=user_id, dest_code=code)
        else:
            st = self.get_chat_context(chat_id, user_id)
            if st is not None and st.state == STATE_AWAITING_WATCH_PRICE:
                self._set_chat_state(chat_id, STATE_IDLE, user_id=user_id)
        self._submit_ui(self._run_watch_action, chat_id, message_id, sub, code, arg)

    def _run_watch_action(self, chat_id: str, message_id: int, sub: str, code: str, arg: str) -> None:
        try:
            if sub == "m":
                text, markup = self.build_watch_manage_card(chat_id, code)
                self._render_watch_card(chat_id, message_id, text, markup, edit=self._is_watch_card(chat_id, message_id))
            elif sub == "l":
                text, markup = self.build_watch_list_card(chat_id)
                self._render_watch_card(chat_id, message_id, text, markup, edit=self._is_watch_card(chat_id, message_id))
            elif sub == "p":
                text, markup = self._save_preset_watch(chat_id, code, arg)
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
                removed = self.db.deactivate_travel_watch(chat_id, code)
                notice = (
                    f"🗑️ {html.escape(self.label_for(code))} 알림을 삭제했어요."
                    if removed else "ℹ️ 이미 삭제되었거나 활성화된 알림이 없어요."
                )
                text, markup = self.build_watch_list_card(chat_id, notice=notice)
                self._render_watch_card(chat_id, message_id, text, markup)
        except Exception as exc:
            logger.error("Watch action %s/%s failed for %s: %s", sub, code, chat_id, exc, exc_info=True)
            self._render_watch_card(
                chat_id, message_id,
                "⚠️ 알림 설정 처리 중 일시적인 오류가 발생했습니다. 잠시 후 다시 시도해 주세요.",
                {"inline_keyboard": [[{"text": "📋 내 알림 목록", "callback_data": "trv:w:l"}]]},
            )

    def _save_preset_watch(self, chat_id: str, code: str, arg: str) -> tuple[str, dict[str, Any]]:
        presets = self.watch_reference_prices(code)
        kind = "DEAL" if arg == "D" else "FLOOR"
        target = presets.get(kind)
        if not target:
            return self.build_watch_manage_card(
                chat_id, code, notice="ℹ️ 이 도시는 기준가 정보가 없어 목표가를 직접 입력해 주세요."
            )
        return self._save_watch(chat_id, code, target, kind)

    def _save_watch(self, chat_id: str, code: str, target: int, kind: str) -> tuple[str, dict[str, Any]]:
        from travel_brief.storage.db import WatchLimitError

        try:
            watch = self.db.upsert_travel_watch(chat_id, code, target, preset_kind=kind)
        except WatchLimitError as exc:
            text, markup = self.build_watch_list_card(
                chat_id,
                notice=f"⚠️ 특가 알림은 최대 {exc.limit}개까지 설정할 수 있어요. 기존 알림을 정리한 뒤 다시 시도해 주세요.",
            )
            return text, markup
        return self.build_watch_saved_card(watch, title="✅ 특가 알림이 설정되었습니다")

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
            text, markup = self._save_watch(chat_id, code, target, "CUSTOM")
            self._render_watch_card(chat_id, 0, text, markup, edit=False)
            return True, "travel_watch_saved"
        if query.casefold().replace(" ", "") in ALL_WORDS or self.catalog.resolve(query) is not None:
            return None
        self._send_card(chat_id, self.build_watch_price_error_card(code, query))
        return True, "travel_watch_price_invalid"

    def watch_reference_prices(self, code: str) -> dict[str, int]:
        """FLOOR (바닥가) / DEAL (실속 특가) / BASELINE (평균가) for a curated or fallback city.
        Values outside the allowed watch range are omitted (manual input only)."""
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
        return f"{day} · {peak}"

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
        lines = [
            f"{title}",
            f"🔔 <b>[{html.escape(label)}]</b>",
            "━━━━━━━━━━━━━━━━━━",
            f"🎯 목표가: <b>{int(watch['target_price_krw']):,}원 이하</b>" + (f" ({kind})" if kind else ""),
            f"🗓️ 요일: {day}",
            f"🎉 연휴·성수기: {peak}",
            "⏰ 실시간 관측 최저가가 목표가 이하가 되면 알려드려요 (같은 일정은 7일에 1번).",
        ]
        rows = [
            [{"text": f"🗓️ 요일: {day} (변경)", "callback_data": f"trv:w:t:{code}:day"}],
            [{"text": f"🎉 연휴·성수기: {peak} (변경)", "callback_data": f"trv:w:t:{code}:pk"}],
            [
                {"text": "✍️ 목표가 변경", "callback_data": f"trv:w:i:{code}"},
                {"text": "🔕 알림 끄기", "callback_data": f"trv:w:x:{code}"},
            ],
            [{"text": "📋 내 알림 목록", "callback_data": "trv:w:l"}],
        ]
        return "\n".join(lines), {"inline_keyboard": rows}

    def build_watch_list_card(self, chat_id: str, notice: str = "") -> tuple[str, dict[str, Any]]:
        from travel_brief.storage.db import WATCH_MAX_ACTIVE_PER_CHAT

        watches = self.db.list_travel_watches(chat_id=chat_id, active_only=True)[:WATCH_MAX_ACTIVE_PER_CHAT]
        lines = []
        if notice:
            lines.append(notice)
        lines += [
            f"📋 <b>[내 특가 알림]</b> ({len(watches)}/{WATCH_MAX_ACTIVE_PER_CHAT})",
            "━━━━━━━━━━━━━━━━━━",
        ]
        rows: list[list[dict[str, Any]]] = []
        if not watches:
            lines.append("아직 설정한 알림이 없어요. 아래 도시를 눌러 바로 시작해 보세요 👇")
        for i, w in enumerate(watches, 1):
            label = self.label_for(w["dest_code"])
            seen = w.get("last_seen_price_krw")
            seen_txt = f" · 최근 {_man_label(int(seen))}" if seen else ""
            lines.append(
                f"{i}. {html.escape(label)} · {_man_label(int(w['target_price_krw']))} 이하 · "
                f"{self._watch_options_line(w)}{seen_txt}"
            )
            rows.append([
                {"text": f"⚙️ {label}", "callback_data": f"trv:w:m:{w['dest_code']}"},
                {"text": "🗑️ 삭제", "callback_data": f"trv:w:x:{w['dest_code']}"},
            ])
        if len(watches) < 3:
            watched = {w["dest_code"] for w in watches}
            quick = [c for c in WATCH_QUICK_ADD_CODES if c not in watched and self.catalog.get(c)][:3]
            if quick:
                rows.append([{"text": f"➕ {self.label_for(c)}", "callback_data": f"trv:w:m:{c}"} for c in quick])
        rows.append([{"text": "🔍 다른 도시 검색", "callback_data": "trv:c:guide"}])
        return "\n".join(lines), {"inline_keyboard": rows}

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
    ) -> None:
        """Runs in background worker thread to collect deals and edit Telegram message in-place.
        Always ends with either the briefing or an error card (never a stuck loading card).
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
            self._finish_with_card(chat_id, message_id, request_seq, loading_future, new_text, markup)
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
        self._edit_if_latest(
            chat_id=chat_id,
            message_id=message_id,
            request_seq=request_seq,
            text=text,
            reply_markup=markup,
        )

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
            for attempt in range(2):
                try:
                    req = urllib.request.Request(
                        edit_url,
                        data=json.dumps(body).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = json.load(resp)
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
        self.ui_pool.shutdown(wait=False)
