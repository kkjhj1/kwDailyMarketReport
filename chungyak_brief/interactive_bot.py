"""chungyak_bot interactive UX + alert service.

Interactive ([TG-04]): every callback is ACKed first (once), `callback_data` is ASCII `cy:...` <= 64 bytes,
screens are edited in place, and the destructive profile reset uses an in-place 2-step confirm.

Alert service ([PROD-LIVE]):
- CUSTOM_MATCH: profile x open notice -> 3-state eligibility -> `should_notify`; sent immediately, except
  in quiet hours (22:00~07:30 KST) where it is ledgered as QUEUED_MORNING (scheduled 08:00 KST).
- QUEUED_MORNING bundle + REGULAR_DAILY digest are sent only by CRON_TICK inside 08:00 <= now < 08:15 KST.
- STARTUP_INIT never sends (no compensatory redelivery); due QUEUED_MORNING rows whose window passed are
  closed as FAILED(WINDOW_MISSED).
- Every live send requires the alerts.sqlite3 active-subscription cross-check; failure -> BLOCKED (0 sends).
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import logging
import re
import secrets
import threading
import time as _time
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

from chungyak_brief.channels.telegram import (
    callback_ok,
    esc,
    render_notice_block,
    split_blocks,
)
from chungyak_brief.channels.telegram_limiter import SafeTelegramDispatcher, TelegramRateLimiter
from chungyak_brief.eligibility import EligibilityEngine, should_notify
from chungyak_brief.models import (
    _SIDO_ALIASES,
    AREA_BUCKET_LABELS,
    AREA_BUCKETS,
    SIDO_CODES,
    SIDO_NAME_TO_CODE,
    SPECIAL_SUPPLY_TYPES,
    ChannelType,
    ChungyakNotice,
    DeliveryStatus,
    EligibilityStatus,
    RegularSubPrefs,
    TriggerContext,
    UserProfile,
    decimal_text,
    to_decimal,
)
from chungyak_brief.storage.db import ChungyakDatabase, SubscriptionGateBlocked

logger = logging.getLogger(__name__)

KST = ZoneInfo("Asia/Seoul")
QUIET_START = time(22, 0)
QUIET_END = time(7, 30)
MORNING_SLOT = time(8, 0)
MORNING_WINDOW_MINUTES = 15
KST_FMT = "%Y-%m-%d %H:%M:%S"
DISCLAIMER = "ℹ️ 최종 자격은 입주자모집공고 원문으로 확인하세요."
DAILY_DIGEST_LIMIT = 8
LIST_VIEW_LIMIT = 5


# =========================================================================================================
# Time policy helpers
# =========================================================================================================
def to_kst(now: Optional[datetime] = None) -> datetime:
    moment = now or datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(KST)


def is_quiet_hours(now_kst: datetime) -> bool:
    t = now_kst.timetz().replace(tzinfo=None)
    return t >= QUIET_START or t < QUIET_END


def next_morning_slot(now_kst: datetime) -> datetime:
    t = now_kst.timetz().replace(tzinfo=None)
    day = now_kst.date() + (timedelta(days=1) if t >= QUIET_START else timedelta(days=0))
    return datetime.combine(day, MORNING_SLOT, tzinfo=KST)


def in_morning_window(now_kst: datetime) -> bool:
    start = datetime.combine(now_kst.date(), MORNING_SLOT, tzinfo=KST)
    return start <= now_kst < start + timedelta(minutes=MORNING_WINDOW_MINUTES)


def is_deadline_d1(notice: ChungyakNotice, today: date) -> bool:
    return notice.receipt_end is not None and notice.receipt_end == today + timedelta(days=1)


def filter_regular_notices(
    notices: list[ChungyakNotice], prefs: RegularSubPrefs, today: date
) -> list[ChungyakNotice]:
    """REGULAR_DAILY filter (eligibility is applied afterwards by the caller).

    - Category: MOOSUNWI -> `moosunwi`, PUBLIC -> `public`, APT_GENERAL/IMUI/CANCEL_RE -> `general`.
    - Area: `areas=None` -> all; otherwise the notice's min~max area span must touch a selected bucket
      (~59: <60, 84: 60~85, 85+: >85). Unknown area is never excluded (unknown != false).
    - D-1 (`deadline_d1`) is additive, never a narrowing filter on top of categories:
        * with >= 1 category ON  -> category AND area match; D-1 notices are moved to the front.
        * with every category OFF -> only notices whose receipt ends tomorrow (any category) AND area match.
        * every category OFF and D-1 OFF -> nothing.
    """
    any_category = prefs.moosunwi or prefs.public or prefs.general
    picked = []
    for n in notices:
        if not prefs.wants_area(n):
            continue
        if any_category:
            if prefs.wants_type(n.notice_type):
                picked.append(n)
        elif prefs.deadline_d1 and is_deadline_d1(n, today):
            picked.append(n)
    if prefs.deadline_d1:
        picked.sort(key=lambda n: 0 if is_deadline_d1(n, today) else 1)  # stable: keeps receipt order otherwise
    return picked


# =========================================================================================================
# One-line condition parser (pure; never touches the DB)
# =========================================================================================================
_SIDO_TERMS: tuple[tuple[str, str], ...] = tuple(sorted(
    [(name, name) for name in SIDO_NAME_TO_CODE] + list(_SIDO_ALIASES.items()),
    key=lambda kv: -len(kv[0]),
))
_SPECIAL_TERMS: tuple[tuple[str, str], ...] = (
    ("신혼", "NEWLYWED"), ("생애최초", "FIRST_LIFE"), ("생초", "FIRST_LIFE"), ("다자녀", "MULTI_CHILD"),
    ("신생아", "NEWBORN"), ("청년", "YOUTH"), ("노부모", "ELDERLY_PARENT"),
)
_RE_PRICE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,2})?)\s*억")
_RE_MONTHS = re.compile(r"(?<![\d.])(\d{1,3})\s*개월")
_RE_YEARS = re.compile(r"(?<![\d.])(\d{1,2})\s*년")
_RE_PYEONG = re.compile(r"(?<![\d.])(\d{1,3})\s*평")
_RE_AREA = re.compile(r"(?<![\d.])(\d{2,3})(?:\.\d+)?\s*(?:㎡|m²|m2|제곱미터|형|타입)?\s*(\+|이상|초과)?(?![\d.])")


def _area_to_bucket(value: int, plus: bool = False) -> Optional[str]:
    if not 20 <= value <= 300:
        return None
    if plus and value >= 85:
        return "85+"
    if value < 60:
        return "59"
    return "84" if value <= 85 else "85+"


def _pyeong_to_bucket(value: int) -> Optional[str]:
    """Market convention (공급면적 평형): 24평형≈59㎡, 34평형≈84㎡, 그 이상 = 85㎡+."""
    if not 5 <= value <= 100:
        return None
    if value <= 28:
        return "59"
    return "84" if value <= 36 else "85+"


def parse_condition_text(text: str) -> dict[str, Any]:
    """Free text ("서울 84 9억 무주택 24개월 신혼") -> recognized conditions. Pure function, no DB access.

    Keys (present only when recognized): preferred_regions (list[str], [] = 전국), areas (list[str] buckets),
    max_price_eok (Decimal), is_homeless, is_household_head (bool), account_months (int),
    special_types (list[str]), moosunwi_only (True). Unrecognized text -> {}.
    """
    raw = str(text or "").strip()
    if not raw or raw.startswith("/") or len(raw) > 300:
        return {}
    work = raw.lower()
    out: dict[str, Any] = {}

    regions: list[str] = []
    for term, sido in _SIDO_TERMS:
        if term in work:
            if sido not in regions:
                regions.append(sido)
            work = work.replace(term, " ")
    if regions:
        out["preferred_regions"] = sorted(regions, key=lambda s: list(SIDO_NAME_TO_CODE).index(s))
    elif "전국" in work:
        out["preferred_regions"] = []

    def _take(pattern: re.Pattern[str], fn: Callable[[re.Match[str]], None]) -> None:
        nonlocal work
        for m in list(pattern.finditer(work)):
            fn(m)
        work = pattern.sub(" ", work)

    def _price(m: re.Match[str]) -> None:
        dec = to_decimal(m.group(1))
        if dec is not None and Decimal("0") < dec <= Decimal("100"):
            out["max_price_eok"] = dec

    def _months(m: re.Match[str]) -> None:
        v = int(m.group(1))
        if 1 <= v <= 600:
            out["account_months"] = v

    def _years(m: re.Match[str]) -> None:
        v = int(m.group(1))
        if 1 <= v <= 30 and "account_months" not in out:
            out["account_months"] = v * 12

    areas: set[str] = set()

    def _pyeong(m: re.Match[str]) -> None:
        b = _pyeong_to_bucket(int(m.group(1)))
        if b:
            areas.add(b)

    def _area(m: re.Match[str]) -> None:
        b = _area_to_bucket(int(m.group(1)), plus=bool(m.group(2)))
        if b:
            areas.add(b)

    _take(_RE_PRICE, _price)
    _take(_RE_MONTHS, _months)
    _take(_RE_YEARS, _years)
    _take(_RE_PYEONG, _pyeong)
    _take(_RE_AREA, _area)
    if areas:
        out["areas"] = [b for b in AREA_BUCKETS if b in areas]

    if "무주택" in work:
        out["is_homeless"] = True
    elif "유주택" in work:
        out["is_homeless"] = False
    if "세대주" in work:
        out["is_household_head"] = True
    elif "세대원" in work:
        out["is_household_head"] = False
    specials = sorted({code for term, code in _SPECIAL_TERMS if term in work})
    if specials:
        out["special_types"] = specials
    if "줍줍" in work or "무순위" in work:
        out["moosunwi_only"] = True
    return out


# In-memory draft store for the one-line parser. Module-level because the platform manager builds a fresh
# ChungyakInteractiveBot per update. Drafts are never persisted; they expire after DRAFT_TTL_SECONDS.
DRAFT_TTL_SECONDS = 600
DRAFT_MAX_ENTRIES = 2000
_DRAFTS: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}
_DRAFTS_LOCK = threading.Lock()


def is_private_chat(chat: dict[str, Any]) -> bool:
    """`chat.type == "private"`; when the type is absent (tests/legacy), a non-negative chat id counts as private.
    Groups / supergroups / channels never reach the free-text parser."""
    ctype = str((chat or {}).get("type") or "").strip().lower()
    if ctype:
        return ctype == "private"
    cid = str((chat or {}).get("id") or "").strip()
    return bool(cid) and not cid.startswith("-")


def _draft_put(chat_id: str, parsed: dict[str, Any], now: float) -> str:
    nonce = secrets.token_hex(4)  # 8 ASCII chars
    with _DRAFTS_LOCK:
        for key in [k for k, (exp, _) in _DRAFTS.items() if exp <= now or k[0] == chat_id]:
            _DRAFTS.pop(key, None)  # expired, or superseded by this chat's newer draft
        while len(_DRAFTS) >= DRAFT_MAX_ENTRIES:
            _DRAFTS.pop(next(iter(_DRAFTS)))
        _DRAFTS[(chat_id, nonce)] = (now + DRAFT_TTL_SECONDS, dict(parsed))
    return nonce


def _draft_pop(chat_id: str, nonce: str, now: float) -> Optional[dict[str, Any]]:
    """Single-use: removes the draft; returns None for unknown/foreign/expired nonces."""
    with _DRAFTS_LOCK:
        entry = _DRAFTS.pop((chat_id, nonce), None)
    if entry is None or entry[0] <= now:
        return None
    return entry[1]


# =========================================================================================================
# Alert service
# =========================================================================================================
class ChungyakAlertService:
    def __init__(
        self,
        db: ChungyakDatabase,
        channel: Any,
        dispatcher: Optional[SafeTelegramDispatcher] = None,
        engine: Optional[EligibilityEngine] = None,
        active_chat_ids_provider: Optional[Callable[[], set[str]]] = None,
        include_samples: bool = False,
        dry_run: bool = False,
    ) -> None:
        self.db = db
        self.channel = channel
        self.dispatcher = dispatcher or SafeTelegramDispatcher()
        self.engine = engine or EligibilityEngine()
        self.active_chat_ids_provider = active_chat_ids_provider
        self.include_samples = include_samples
        self.dry_run = dry_run

    # ------------------------------------------------------------------ public entry
    def run_tick(self, trigger: TriggerContext | str = TriggerContext.CRON_TICK, now: Optional[datetime] = None) -> dict[str, Any]:
        trig = TriggerContext(trigger)
        now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        now_kst = to_kst(now_utc)
        stats: dict[str, Any] = {
            "trigger": trig.value,
            "now_kst": now_kst.strftime(KST_FMT),
            "dry_run": self.dry_run,
            "blocked": False,
            "blocked_reason": None,
            "lease_expired_to_unknown": 0,
            "window_missed": 0,
            "morning_bundle": {"chats": 0, "sent": 0, "unknown": 0, "failed": 0},
            "regular_daily": {"chats": 0, "sent": 0, "unknown": 0, "failed": 0, "notices": 0},
            "custom": {"matched": 0, "queued_morning": 0, "sent": 0, "unknown": 0, "failed": 0},
            "planned": [],
        }

        if not self.dry_run:
            stats["lease_expired_to_unknown"] = self.db.sweep_expired_leases(now=now_utc)
            cutoff = (now_kst - timedelta(minutes=MORNING_WINDOW_MINUTES)).strftime(KST_FMT)
            stats["window_missed"] = self.db.expire_missed_morning(cutoff, now=now_utc)

        if trig == TriggerContext.STARTUP_INIT:
            return stats  # [PROD-LIVE] §2: restart/deploy never triggers sends.

        try:
            active = self._active_chat_ids()
        except SubscriptionGateBlocked as exc:
            stats["blocked"] = True
            stats["blocked_reason"] = str(exc)
            logger.warning("[PROD-LIVE] chungyak dispatch BLOCKED: %s", exc)
            return stats

        if trig == TriggerContext.CRON_TICK and in_morning_window(now_kst):
            self._send_morning_bundles(active, now_utc, now_kst, stats)
            self._send_regular_daily(active, now_utc, now_kst, stats)

        self._process_custom_matches(active, trig, now_utc, now_kst, stats)
        return stats

    # ------------------------------------------------------------------ internals
    def _active_chat_ids(self) -> set[str]:
        if self.active_chat_ids_provider is None:
            raise SubscriptionGateBlocked("active subscription provider not configured")
        return {str(c) for c in self.active_chat_ids_provider()}

    def _open_notices(self, today: date) -> list[ChungyakNotice]:
        return self.db.list_open_notices(today, include_samples=self.include_samples)

    def _keyboard(self) -> dict[str, Any]:
        return {"inline_keyboard": [[
            {"text": "⚙️ 내 조건", "callback_data": "cy:home"},
            {"text": "📋 진행중 공고", "callback_data": "cy:list"},
        ]]}

    def _send_bundle(
        self,
        chat_id: str,
        header: str,
        items: list[tuple[ChungyakNotice, Any, dict[str, Any]]],
        now_utc: datetime,
        bucket: dict[str, Any],
    ) -> None:
        """items: [(notice, eligibility_result|None, ledger_row{delivery_key, lease_token})]."""
        blocks = [render_notice_block(n, r) for n, r, _ in items]
        for html_text, idxs in split_blocks(header, blocks, DISCLAIMER):
            status, msg_id, err = self.dispatcher.send_one(self.channel, chat_id, html_text, reply_markup=self._keyboard())
            for i in idxs:
                row = items[i][2]
                self.db.finalize_delivery(
                    row["delivery_key"], row["lease_token"], status,
                    provider_msg_id=msg_id, error_detail=err, now=now_utc,
                )
            key = {"SENT": "sent", "UNKNOWN": "unknown"}.get(status, "failed")
            bucket[key] += 1

    def _send_morning_bundles(self, active: set[str], now_utc: datetime, now_kst: datetime, stats: dict[str, Any]) -> None:
        bucket = stats["morning_bundle"]
        if self.dry_run:
            for row in self.db.list_deliveries(status=DeliveryStatus.QUEUED_MORNING.value):
                if str(row.get("scheduled_at_kst") or "") <= now_kst.strftime(KST_FMT):
                    stats["planned"].append(("QUEUED_MORNING", row["chat_id"], row["notice_key"]))
            return
        claimed = self.db.claim_queued_morning(now_kst.strftime(KST_FMT), now=now_utc, trigger_context="CRON_TICK")
        by_chat: dict[str, list[dict[str, Any]]] = {}
        for row in claimed:
            by_chat.setdefault(str(row["chat_id"]), []).append(row)
        for chat_id, rows in by_chat.items():
            if chat_id not in active:
                for row in rows:
                    self.db.finalize_delivery(
                        row["delivery_key"], row["lease_token"], DeliveryStatus.FAILED,
                        error_detail="SUBSCRIPTION_INACTIVE: 구독 해지/비활성", now=now_utc,
                    )
                continue
            profile = self.db.get_profile(chat_id) or UserProfile(chat_id=chat_id)
            items = []
            for row in rows:
                notice = self.db.get_notice(row["notice_key"])
                if notice is None:
                    self.db.finalize_delivery(
                        row["delivery_key"], row["lease_token"], DeliveryStatus.FAILED,
                        error_detail="NOTICE_MISSING", now=now_utc,
                    )
                    continue
                items.append((notice, self.engine.evaluate(notice, profile), row))
            if not items:
                continue
            bucket["chats"] += 1
            header = f"🌅 <b>밤사이 맞춤 청약 알림</b> · <b>{len(items)}</b>건"
            self._send_bundle(chat_id, header, items, now_utc, bucket)

    def _send_regular_daily(self, active: set[str], now_utc: datetime, now_kst: datetime, stats: dict[str, Any]) -> None:
        bucket = stats["regular_daily"]
        today = now_kst.date()
        notices = self._open_notices(today)
        for chat_id in self.db.list_regular_sub_chat_ids():
            if chat_id not in active:
                continue
            profile = self.db.get_profile(chat_id) or UserProfile(chat_id=chat_id)
            prefs = self.db.get_regular_prefs(chat_id)
            candidates = filter_regular_notices(notices, prefs, today)
            picked: list[tuple[ChungyakNotice, Any]] = []
            for notice in candidates:
                result = self.engine.evaluate(notice, profile)
                if result.status == EligibilityStatus.INELIGIBLE:
                    continue
                if self.db.is_notice_delivered(chat_id, notice.notice_key, notice.content_hash):
                    continue  # CUSTOM_MATCH (or earlier digest) already SENT/UNKNOWN/queued -> suppress
                picked.append((notice, result))
                if len(picked) >= DAILY_DIGEST_LIMIT:
                    break
            if not picked:
                continue
            if self.dry_run:
                stats["planned"].extend(("REGULAR_DAILY", chat_id, n.notice_key) for n, _ in picked)
                continue
            items = []
            for notice, result in picked:
                lease = self.db.reserve_delivery(
                    chat_id=chat_id, notice_key=notice.notice_key, content_hash=notice.content_hash,
                    channel=ChannelType.REGULAR_DAILY, trigger_context="CRON_TICK", now=now_utc,
                )
                if lease is not None:
                    items.append((notice, result, lease))
            if not items:
                continue
            bucket["chats"] += 1
            bucket["notices"] += len(items)
            header = f"📰 <b>오늘의 청약 공고</b> · {now_kst.strftime('%m.%d')} · <b>{len(items)}</b>건"
            d1_count = sum(1 for n, _, _ in items if is_deadline_d1(n, today))
            if prefs.deadline_d1 and d1_count:
                header += f"\n⏰ 내일 마감 <b>{d1_count}</b>건 먼저 보여드려요"
            self._send_bundle(chat_id, header, items, now_utc, bucket)

    def _process_custom_matches(
        self, active: set[str], trig: TriggerContext, now_utc: datetime, now_kst: datetime, stats: dict[str, Any]
    ) -> None:
        bucket = stats["custom"]
        notices = self._open_notices(now_kst.date())
        if not notices:
            return
        quiet = is_quiet_hours(now_kst)
        queue_until = next_morning_slot(now_kst).strftime(KST_FMT) if quiet else None
        for profile in self.db.list_profiles():
            chat_id = profile.chat_id
            if chat_id not in active or not profile.alerts_on:
                continue
            items = []
            for notice in notices:
                result = self.engine.evaluate(notice, profile)
                if not should_notify(result, profile):
                    continue
                if self.db.is_notice_delivered(chat_id, notice.notice_key, notice.content_hash):
                    continue
                bucket["matched"] += 1
                if self.dry_run:
                    stats["planned"].append(("QUEUE" if quiet else "CUSTOM_MATCH", chat_id, notice.notice_key))
                    continue
                lease = self.db.reserve_delivery(
                    chat_id=chat_id, notice_key=notice.notice_key, content_hash=notice.content_hash,
                    channel=ChannelType.CUSTOM_MATCH, trigger_context=trig.value, now=now_utc,
                    queue_until_kst=queue_until,
                )
                if lease is None:
                    continue
                if lease["status"] == DeliveryStatus.QUEUED_MORNING.value:
                    bucket["queued_morning"] += 1
                    continue
                items.append((notice, result, lease))
            if items:
                header = f"🔔 <b>맞춤 청약 알림</b> · <b>{len(items)}</b>건"
                self._send_bundle(chat_id, header, items, now_utc, bucket)


# =========================================================================================================
# Interactive bot
# =========================================================================================================
SPECIAL_LABELS = {
    "NEWLYWED": "신혼부부", "FIRST_LIFE": "생애최초", "MULTI_CHILD": "다자녀",
    "ELDERLY_PARENT": "노부모부양", "NEWBORN": "신생아", "YOUTH": "청년",
}
SPECIAL_CODES = {"NW": "NEWLYWED", "FL": "FIRST_LIFE", "MC": "MULTI_CHILD", "EP": "ELDERLY_PARENT", "NB": "NEWBORN", "YT": "YOUTH"}
SPECIAL_CODES_REV = {v: k for k, v in SPECIAL_CODES.items()}
ACCOUNT_CHOICES = [("6개월 미만", "0"), ("6~11개월", "6"), ("12~23개월", "12"), ("24개월+", "24")]
DEPOSIT_CHOICES = [("200만", "200"), ("300만", "300"), ("600만", "600"), ("1000만", "1000"), ("1500만+", "1500")]
PAYMENT_CHOICES = [("6회 미만", "0"), ("6~11회", "6"), ("12~23회", "12"), ("24회+", "24")]
PRICE_CHOICES = [("3억", "3"), ("5억", "5"), ("7억", "7"), ("10억", "10"), ("15억", "15")]
YEARS_CHOICES = [("1년 미만", "0"), ("1~2년", "1"), ("2년+", "2")]
PRESETS: dict[str, tuple[str, str]] = {
    "mo": ("🎰 줍줍·무순위만", "08:00 정기 알림을 무순위(줍줍)만 받도록 켜요."),
    "nw": ("💍 신혼·생애최초", "무주택 + 신혼부부·생애최초 특공을 추가해요."),
    "g1": ("🥇 무주택 1순위", "무주택·세대주·통장 24개월+로 맞춰요."),
}
REGULAR_TYPE_TOGGLES: dict[str, str] = {"m": "moosunwi", "p": "public", "g": "general", "d": "deadline_d1"}
HELP_TEXT = (
    "🏠 <b>청약 알림봇 안내</b>\n"
    "⚙️ /start · /custom — 내 맞춤 조건 설정\n"
    "📋 /today — 오늘 진행중 공고\n"
    "📰 /regular — 08:00 정기 다이제스트\n"
    "🔔 /settings — 알림 설정\n"
    "🌙 22:00~07:30 맞춤 알림은 08:00에 모아 보내요.\n"
    + DISCLAIMER
)
LOCKED_TEXT = "🔒 청약 알림봇 구독 후 이용할 수 있어요. 메뉴봇에서 구독해 주세요."
COMMANDS_HOME = ("/start", "/custom", "/profile", "/프로필", "/조건", "/맞춤")
COMMANDS_TODAY = ("/today", "/list", "/공고", "/오늘")
COMMANDS_REGULAR = ("/regular", "/정기")
COMMANDS_ALL = COMMANDS_HOME + COMMANDS_TODAY + COMMANDS_REGULAR + ("/settings", "/설정", "/help", "/도움말")


def _btn(text: str, data: str) -> dict[str, str]:
    if not callback_ok(data):
        raise ValueError(f"callback_data must be ASCII <= 64 bytes: {data!r}")
    return {"text": text, "callback_data": data}


def _yn(value: Optional[bool], yes: str = "예", no: str = "아니오") -> str:
    return "미입력" if value is None else (yes if value else no)


class ChungyakInteractiveBot:
    def __init__(
        self,
        db: ChungyakDatabase,
        channel: Any,
        engine: Optional[EligibilityEngine] = None,
        access_checker: Optional[Callable[[str], bool]] = None,
        include_samples: bool = False,
        today_provider: Optional[Callable[[], date]] = None,
        draft_clock: Optional[Callable[[], float]] = None,
    ) -> None:
        self.db = db
        self.channel = channel
        self.engine = engine or EligibilityEngine()
        self.access_checker = access_checker
        self.include_samples = include_samples
        self.today_provider = today_provider or (lambda: to_kst().date())
        self.draft_clock = draft_clock or _time.monotonic

    # ------------------------------------------------------------------ routing
    def handle_update(self, update: dict[str, Any]) -> Optional[str]:
        if "callback_query" in update:
            return self._handle_callback(update["callback_query"])
        msg = update.get("message") or {}
        text = str(msg.get("text") or "").strip()
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        if not chat_id or not text:
            return None
        if not text.startswith("/"):
            # One-line condition parser: private chats only, and only when >= 1 condition is recognized.
            if not is_private_chat(chat):
                return None
            parsed = parse_condition_text(text)
            if not parsed:
                return None
            if self.access_checker is not None and not self.access_checker(chat_id):
                self.channel.send_message(chat_id, esc(LOCKED_TEXT))
                return "locked"
            nonce = _draft_put(chat_id, parsed, self.draft_clock())  # no DB write until "이대로 저장"
            html_text, markup = self.render_draft_preview(text, parsed, nonce)
            self.channel.send_message(chat_id, html_text, reply_markup=markup)
            return "draft"
        if self.access_checker is not None and not self.access_checker(chat_id):
            self.channel.send_message(chat_id, esc(LOCKED_TEXT))
            return "locked"
        cmd = text.split()[0].lower().split("@")[0]
        if cmd in COMMANDS_HOME:
            profile = self.db.ensure_profile(chat_id)
            html_text, markup = self.render_home(profile)
        elif cmd in COMMANDS_TODAY:
            html_text, markup = self.render_list(self.db.get_profile(chat_id) or UserProfile(chat_id=chat_id))
        elif cmd in COMMANDS_REGULAR:
            html_text, markup = self.render_regular(self.db.ensure_profile(chat_id))
        elif cmd in ("/settings", "/설정"):
            html_text, markup = self.render_settings(self.db.ensure_profile(chat_id))
        elif cmd in ("/help", "/도움말"):
            html_text, markup = HELP_TEXT, {"inline_keyboard": [[_btn("⚙️ 내 조건", "cy:home")]]}
        else:
            return None
        self.channel.send_message(chat_id, html_text, reply_markup=markup)
        return cmd

    def _handle_callback(self, cq: dict[str, Any]) -> Optional[str]:
        cq_id = str(cq.get("id") or "")
        data = str(cq.get("data") or "")
        msg = cq.get("message") or {}
        chat_id = str((msg.get("chat") or {}).get("id") or (cq.get("from") or {}).get("id") or "")
        message_id = msg.get("message_id")
        # [TG-04] ACK first (unconditionally, exactly once) before any DB / rendering work.
        if cq_id:
            self.channel.answer_callback(cq_id)
        if not chat_id or not data.startswith("cy:"):
            return None
        if self.access_checker is not None and not self.access_checker(chat_id):
            self.channel.send_message(chat_id, esc(LOCKED_TEXT))
            return "locked"
        parts = data.split(":")
        action = parts[1] if len(parts) > 1 else "home"
        arg = parts[2] if len(parts) > 2 else ""
        profile = self.db.ensure_profile(chat_id)
        view: Optional[tuple[str, dict[str, Any]]] = None

        if action == "home":
            view = self.render_home(profile)
        elif action == "adv":
            view = self.render_advanced(profile)
        elif action == "ask":
            view = self.render_question(profile, arg)
        elif action == "sido":
            profile = self.db.update_profile(chat_id, residence_sido=SIDO_CODES.get(arg))
            view = self.render_advanced(profile)
        elif action == "ry":
            profile = self.db.update_profile(chat_id, residence_years=self._int_or_none(arg))
            view = self.render_advanced(profile)
        elif action == "hl":
            profile = self.db.update_profile(chat_id, is_homeless=self._bool_or_none(arg))
            view = self.render_advanced(profile)
        elif action == "hh":
            profile = self.db.update_profile(chat_id, is_household_head=self._bool_or_none(arg))
            view = self.render_advanced(profile)
        elif action == "acct":
            profile = self.db.update_profile(chat_id, account_months=self._int_or_none(arg))
            view = self.render_advanced(profile)
        elif action == "dep":
            profile = self.db.update_profile(chat_id, deposit_manwon=self._int_or_none(arg))
            view = self.render_advanced(profile)
        elif action == "pay":
            profile = self.db.update_profile(chat_id, payment_count=self._int_or_none(arg))
            view = self.render_advanced(profile)
        elif action == "price":
            dec = to_decimal(arg) if arg not in ("", "U") else None
            profile = self.db.update_profile(chat_id, max_price_eok=dec)
            view = self.render_advanced(profile)
        elif action == "wiz":
            view = self.render_wizard(profile, arg if arg in ("1", "2", "3") else "1")
        elif action == "w1":
            sido = SIDO_CODES.get(arg)
            if sido:
                cur = set(profile.preferred_regions or [])
                cur.symmetric_difference_update({sido})
                profile = self.db.update_profile(chat_id, preferred_regions=sorted(cur) or None)
            elif arg == "X":
                profile = self.db.update_profile(chat_id, preferred_regions=None)
            view = self.render_wizard(profile, "1")
        elif action == "w2":
            if arg == "U" or arg in {v for _, v in PRICE_CHOICES}:
                dec = None if arg == "U" else to_decimal(arg)
                profile = self.db.update_profile(chat_id, max_price_eok=dec)
                view = self.render_wizard(profile, "3")  # 1-tap auto-advance
            else:
                view = self.render_wizard(profile, "2")
        elif action == "w3":
            if arg == "U" or arg in SIDO_CODES:
                profile = self.db.update_profile(chat_id, residence_sido=SIDO_CODES.get(arg))
                home_text, home_kb = self.render_home(profile)
                view = ("✅ <b>빠른 설정 완료!</b> 맞는 공고가 나오면 알려드릴게요.\n\n" + home_text, home_kb)
            else:
                view = self.render_wizard(profile, "3")
        elif action == "pre":
            if arg in PRESETS:
                profile = self._apply_preset(chat_id, profile, arg)
                home_text, home_kb = self.render_home(profile)
                view = (f"✅ <b>{esc(PRESETS[arg][0])}</b> 프리셋을 적용했어요.\n\n" + home_text, home_kb)
            else:
                view = self.render_presets()
        elif action == "save":
            parsed = _draft_pop(chat_id, arg, self.draft_clock())
            if parsed is None:
                home_text, home_kb = self.render_home(profile)
                view = ("⌛ <b>저장할 수 없는 요청이에요.</b> (만료되었거나 이미 처리됨) 다시 입력해 주세요.\n\n"
                        + home_text, home_kb)
            else:
                profile = self._apply_parsed(chat_id, profile, parsed)
                home_text, home_kb = self.render_home(profile)
                view = ("✅ <b>조건을 저장했어요.</b>\n\n" + home_text, home_kb)
        elif action == "cancel":
            _draft_pop(chat_id, arg, self.draft_clock())
            home_text, home_kb = self.render_home(profile)
            view = ("↩️ 입력을 취소했어요. 저장된 조건은 그대로예요.\n\n" + home_text, home_kb)
        elif action == "rt":
            field_name = REGULAR_TYPE_TOGGLES.get(arg)
            if field_name:
                cur = self.db.get_regular_prefs(chat_id)
                self.db.update_regular_prefs(chat_id, **{field_name: not getattr(cur, field_name)})
            view = self.render_regular(profile)
        elif action == "ra":
            if arg == "X":
                self.db.update_regular_prefs(chat_id, areas=None)
            elif arg in AREA_BUCKETS:
                cur_areas = set(self.db.get_regular_prefs(chat_id).areas or [])
                cur_areas.symmetric_difference_update({arg})
                self.db.update_regular_prefs(chat_id, areas=sorted(cur_areas) or None)
            view = self.render_regular(profile)
        elif action == "sp":
            code = SPECIAL_CODES.get(arg)
            if code:
                cur = set(profile.special_types or [])
                cur.symmetric_difference_update({code})
                profile = self.db.update_profile(chat_id, special_types=sorted(cur) or None)
            elif arg == "X":
                profile = self.db.update_profile(chat_id, special_types=None)
            view = self.render_question(profile, "sp")
        elif action == "rg":
            sido = SIDO_CODES.get(arg)
            if sido:
                cur = set(profile.preferred_regions or [])
                cur.symmetric_difference_update({sido})
                profile = self.db.update_profile(chat_id, preferred_regions=sorted(cur) or None)
            elif arg == "X":
                profile = self.db.update_profile(chat_id, preferred_regions=None)
            view = self.render_question(profile, "rg")
        elif action == "list":
            view = self.render_list(profile)
        elif action == "set":
            view = self.render_settings(profile)
        elif action == "regv":
            view = self.render_regular(profile)
        elif action == "reg":
            if arg == "on":
                self.db.set_regular_sub(chat_id, True)
                view = self.render_settings(profile)
            elif arg == "onv":  # turned on from the regular-digest screen -> stay there
                self.db.set_regular_sub(chat_id, True)
                view = self.render_regular(profile)
            elif arg == "offok":
                self.db.set_regular_sub(chat_id, False)
                view = self.render_settings(profile)
            else:
                # [TG-04] In-place 2-step confirm before cancelling the regular digest (구독 해지류).
                view = (
                    "⚠️ <b>08:00 정기 다이제스트를 끌까요?</b>\n맞춤 조건 알림은 그대로 유지됩니다.",
                    {"inline_keyboard": [[_btn("✅ 끄기", "cy:reg:offok"), _btn("↩️ 취소", "cy:set")]]},
                )
        elif action == "alert":
            profile = self.db.update_profile(chat_id, custom_alert_enabled=(arg == "on"))
            view = self.render_settings(profile)
        elif action == "ofc":
            profile = self.db.update_profile(chat_id, include_officetel=(arg == "on"))
            view = self.render_settings(profile)
        elif action == "reset":
            if arg == "do":
                self.db.delete_profile(chat_id)
                profile = self.db.ensure_profile(chat_id)
                home_text, home_kb = self.render_home(profile)
                view = ("🧹 <b>조건이 초기화되었어요.</b>\n" + home_text, home_kb)
            else:
                view = (
                    "⚠️ <b>내 청약 조건을 모두 삭제할까요?</b>\n정기·맞춤 알림 설정도 함께 초기화됩니다.",
                    {"inline_keyboard": [[_btn("✅ 삭제", "cy:reset:do"), _btn("↩️ 취소", "cy:home")]]},
                )
        if view is None:
            view = self.render_home(profile)
        self.channel.safe_edit_or_send(chat_id, message_id, view[0], reply_markup=view[1])
        return action

    def _apply_preset(self, chat_id: str, profile: UserProfile, key: str) -> UserProfile:
        """Merges preset fields into the profile; unrelated answers are never wiped."""
        if key == "mo":
            self.db.update_regular_prefs(chat_id, enabled=True, moosunwi=True, public=False, general=False)
            return self.db.update_profile(chat_id, custom_alert_enabled=True)
        if key == "nw":
            specials = sorted(set(profile.special_types or []) | {"NEWLYWED", "FIRST_LIFE"})
            return self.db.update_profile(chat_id, is_homeless=True, special_types=specials)
        if key == "g1":
            fields: dict[str, Any] = {"is_homeless": True, "is_household_head": True}
            if profile.account_months is None or profile.account_months < 24:
                fields["account_months"] = 24
            return self.db.update_profile(chat_id, **fields)
        return profile

    def _apply_parsed(self, chat_id: str, profile: UserProfile, parsed: dict[str, Any]) -> UserProfile:
        """Saves a confirmed one-line draft. Regions replace (전국 clears), special types merge."""
        fields: dict[str, Any] = {}
        if "preferred_regions" in parsed:
            fields["preferred_regions"] = list(parsed["preferred_regions"]) or None
        for key in ("max_price_eok", "is_homeless", "is_household_head", "account_months"):
            if key in parsed:
                fields[key] = parsed[key]
        if parsed.get("special_types"):
            fields["special_types"] = sorted(set(profile.special_types or []) | set(parsed["special_types"]))
        if "areas" in parsed:
            self.db.update_regular_prefs(chat_id, areas=parsed["areas"])
        if parsed.get("moosunwi_only"):
            self.db.update_regular_prefs(chat_id, enabled=True, moosunwi=True, public=False, general=False)
        return self.db.update_profile(chat_id, **fields) if fields else profile

    @staticmethod
    def _int_or_none(arg: str) -> Optional[int]:
        return int(arg) if arg.isdigit() else None

    @staticmethod
    def _bool_or_none(arg: str) -> Optional[bool]:
        return {"Y": True, "N": False}.get(arg.upper())

    # ------------------------------------------------------------------ views
    @staticmethod
    def _profile_lines(p: UserProfile) -> list[str]:
        regions = ", ".join(p.preferred_regions) if p.preferred_regions else "전국"
        specials = ", ".join(SPECIAL_LABELS.get(s, s) for s in (p.special_types or [])) or "없음/미입력"
        return [
            f"📍 거주지 <b>{esc(p.residence_sido or '미입력')}</b> · 거주 {esc('미입력' if p.residence_years is None else str(p.residence_years) + '년+')}",
            f"🏚 무주택 <b>{esc(_yn(p.is_homeless))}</b> · 세대주 <b>{esc(_yn(p.is_household_head))}</b>",
            f"📒 통장 <b>{esc('미입력' if p.account_months is None else str(p.account_months) + '개월+')}</b>"
            f" · 예치금 <b>{esc('미입력' if p.deposit_manwon is None else str(p.deposit_manwon) + '만')}</b>",
            f"🧾 납입 <b>{esc('미입력' if p.payment_count is None else str(p.payment_count) + '회+')}</b>"
            f" · 예산 <b>{esc('제한없음' if p.max_price_eok is None else decimal_text(p.max_price_eok) + '억')}</b>",
            f"🎁 특공 <b>{esc(specials)}</b>",
            f"🗺 관심지역 <b>{esc(regions)}</b>",
        ]

    def render_home(self, profile: UserProfile) -> tuple[str, dict[str, Any]]:
        p = profile
        regions = ", ".join(p.preferred_regions) if p.preferred_regions else "전국"
        budget = "제한없음" if p.max_price_eok is None else decimal_text(p.max_price_eok) + "억 이하"
        reg_on = self.db.get_regular_sub(p.chat_id) is True
        lines = [
            "🏠 <b>내 청약 조건</b>",
            f"🗺 관심 <b>{esc(regions)}</b> · 💰 <b>{esc(budget)}</b>",
            f"📍 거주 <b>{esc(p.residence_sido or '미입력')}</b> · 🏚 무주택 <b>{esc(_yn(p.is_homeless))}</b>",
            f"🔔 맞춤 <b>{'ON' if p.alerts_on else 'OFF'}</b> · 📰 정기 <b>{'ON' if reg_on else 'OFF'}</b>",
        ]
        if p.is_empty():
            lines.append("🟡 <b>30초 빠른 설정</b>부터 해보세요. 조건이 많을수록 판정이 정확해져요.")
        lines.append("💬 채팅으로 <i>서울 84 9억 무주택</i> 처럼 보내도 돼요.")
        kb = [
            [_btn("⚡ 30초 빠른 설정", "cy:wiz:1"), _btn("🎯 원클릭 프리셋", "cy:pre:menu")],
            [_btn("📰 정기 알림 설정", "cy:regv"), _btn("📋 진행중 공고", "cy:list")],
            [_btn("⚙️ 상세 조건 더 입력", "cy:adv"), _btn("🔔 알림·초기화", "cy:set")],
        ]
        return "\n".join(lines), {"inline_keyboard": kb}

    def render_advanced(self, profile: UserProfile) -> tuple[str, dict[str, Any]]:
        lines = ["⚙️ <b>상세 조건</b> · 항목을 눌러 바꾸세요"] + self._profile_lines(profile)
        kb = [
            [_btn("📍 거주지", "cy:ask:sido"), _btn("⏳ 거주기간", "cy:ask:ry")],
            [_btn("🏚 무주택", "cy:ask:hl"), _btn("👤 세대주", "cy:ask:hh")],
            [_btn("📒 통장기간", "cy:ask:acct"), _btn("💵 예치금", "cy:ask:dep")],
            [_btn("🧾 납입횟수", "cy:ask:pay"), _btn("💰 예산", "cy:ask:price")],
            [_btn("🎁 특별공급", "cy:ask:sp"), _btn("🗺 관심지역", "cy:ask:rg")],
            [_btn("⬅️ 내 조건", "cy:home")],
        ]
        return "\n".join(lines), {"inline_keyboard": kb}

    def render_presets(self) -> tuple[str, dict[str, Any]]:
        lines = ["🎯 <b>원클릭 프리셋</b> · 기존 조건은 지우지 않고 더해요"]
        lines += [f"{esc(label)} — {esc(desc)}" for label, desc in PRESETS.values()]
        kb = [[_btn(label, f"cy:pre:{key}")] for key, (label, _desc) in PRESETS.items()]
        kb.append([_btn("⬅️ 내 조건", "cy:home")])
        return "\n".join(lines), {"inline_keyboard": kb}

    def render_wizard(self, profile: UserProfile, step: str) -> tuple[str, dict[str, Any]]:
        codes = list(SIDO_CODES.items())
        if step == "2":
            cur = "제한없음" if profile.max_price_eok is None else decimal_text(profile.max_price_eok) + "억"
            opts = PRICE_CHOICES + [("♾ 제한없음", "U")]
            rows = [[_btn(label, f"cy:w2:{val}") for label, val in opts[i:i + 3]] for i in range(0, len(opts), 3)]
            rows.append([_btn("⬅️ 이전", "cy:wiz:1"), _btn("다음 →", "cy:wiz:3")])
            return (f"⚡ <b>빠른 설정 2/3</b> · 💰 최대 분양가 예산\n지금: <b>{esc(cur)}</b> · 누르면 다음 단계로 넘어가요.",
                    {"inline_keyboard": rows})
        if step == "3":
            rows = [[_btn(name, f"cy:w3:{code}") for code, name in codes[i:i + 4]] for i in range(0, len(codes), 4)]
            rows.append([_btn("❔ 모름", "cy:w3:U"), _btn("⬅️ 이전", "cy:wiz:2")])
            return ("⚡ <b>빠른 설정 3/3</b> · 📍 주민등록상 거주지\n누르면 바로 완료돼요.",
                    {"inline_keyboard": rows})
        chosen = set(profile.preferred_regions or [])
        rows = [
            [_btn(("✅" if name in chosen else "") + name, f"cy:w1:{code}") for code, name in codes[i:i + 4]]
            for i in range(0, len(codes), 4)
        ]
        rows.append([_btn("🌏 전국", "cy:w1:X"), _btn("다음 →", "cy:wiz:2")])
        rows.append([_btn("⬅️ 내 조건", "cy:home")])
        picked = ", ".join(sorted(chosen, key=lambda s: list(SIDO_NAME_TO_CODE).index(s) if s in SIDO_NAME_TO_CODE else 99))
        return (f"⚡ <b>빠른 설정 1/3</b> · 🗺 관심 지역 (여러 개 선택)\n선택: <b>{esc(picked or '전국')}</b>",
                {"inline_keyboard": rows})

    def render_draft_preview(self, text: str, parsed: dict[str, Any], nonce: str) -> tuple[str, dict[str, Any]]:
        lines = ["📝 <b>이렇게 이해했어요</b>", f"💬 {esc(text[:80] + ('…' if len(text) > 80 else ''))}"]
        if "preferred_regions" in parsed:
            lines.append(f"🗺 관심지역 <b>{esc(', '.join(parsed['preferred_regions']) or '전국')}</b>")
        if "areas" in parsed:
            lines.append(f"📐 면적(정기) <b>{esc(', '.join(AREA_BUCKET_LABELS[a] for a in parsed['areas']))}</b>")
        if "max_price_eok" in parsed:
            lines.append(f"💰 예산 <b>{esc(decimal_text(parsed['max_price_eok']))}억</b> 이하")
        if "is_homeless" in parsed:
            lines.append(f"🏚 무주택 <b>{esc(_yn(parsed['is_homeless']))}</b>")
        if "is_household_head" in parsed:
            lines.append(f"👤 세대주 <b>{esc(_yn(parsed['is_household_head']))}</b>")
        if "account_months" in parsed:
            lines.append(f"📒 통장 <b>{esc(parsed['account_months'])}개월+</b>")
        if parsed.get("special_types"):
            lines.append(f"🎁 특공 추가 <b>{esc(', '.join(SPECIAL_LABELS.get(s, s) for s in parsed['special_types']))}</b>")
        if parsed.get("moosunwi_only"):
            lines.append("🎰 정기 알림 <b>무순위(줍줍)만</b> 켜기")
        lines.append(f"⏳ 아직 저장 전이에요. {DRAFT_TTL_SECONDS // 60}분 안에 눌러주세요.")
        kb = [[_btn("✅ 이대로 저장", f"cy:save:{nonce}"), _btn("↩️ 취소", f"cy:cancel:{nonce}")]]
        return "\n".join(lines), {"inline_keyboard": kb}

    def render_question(self, profile: UserProfile, field: str) -> tuple[str, dict[str, Any]]:
        back = [_btn("⬅️ 상세 조건", "cy:adv")]
        if field == "sido":
            codes = list(SIDO_CODES.items())
            rows = [[_btn(name, f"cy:sido:{code}") for code, name in codes[i:i + 3]] for i in range(0, len(codes), 3)]
            rows.append([_btn("❔ 모름", "cy:sido:U")] + back)
            return "📍 <b>현재 주민등록상 거주지</b>를 골라주세요.", {"inline_keyboard": rows}
        if field == "rg":
            chosen = set(profile.preferred_regions or [])
            codes = list(SIDO_CODES.items())
            rows = [
                [_btn(("✅ " if name in chosen else "") + name, f"cy:rg:{code}") for code, name in codes[i:i + 3]]
                for i in range(0, len(codes), 3)
            ]
            rows.append([_btn("🌏 전국(해제)", "cy:rg:X")] + back)
            return "🗺 <b>관심 지역</b>을 눌러 켜고 끄세요. (미선택 = 전국)", {"inline_keyboard": rows}
        if field == "sp":
            chosen = set(profile.special_types or [])
            items = [(SPECIAL_LABELS[c], SPECIAL_CODES_REV[c]) for c in SPECIAL_SUPPLY_TYPES]
            rows = [
                [_btn(("✅ " if SPECIAL_CODES[k] in chosen else "") + label, f"cy:sp:{k}") for label, k in items[i:i + 2]]
                for i in range(0, len(items), 2)
            ]
            rows.append([_btn("🚫 해당 없음", "cy:sp:X")] + back)
            return "🎁 <b>해당하는 특별공급</b>을 눌러 켜고 끄세요.", {"inline_keyboard": rows}
        choices: dict[str, tuple[str, str, list[tuple[str, str]]]] = {
            "ry": ("⏳ <b>현 거주지 연속 거주기간</b>", "ry", YEARS_CHOICES),
            "hl": ("🏚 <b>세대원 전원 무주택</b>인가요?", "hl", [("예", "Y"), ("아니오", "N")]),
            "hh": ("👤 <b>세대주</b>인가요?", "hh", [("예", "Y"), ("아니오", "N")]),
            "acct": ("📒 <b>청약통장 가입기간</b>", "acct", ACCOUNT_CHOICES),
            "dep": ("💵 <b>청약통장 예치금</b>", "dep", DEPOSIT_CHOICES),
            "pay": ("🧾 <b>청약통장 납입 인정 횟수</b>", "pay", PAYMENT_CHOICES),
            "price": ("💰 <b>최대 분양가 예산</b>", "price", PRICE_CHOICES),
        }
        if field not in choices:
            return self.render_home(profile)
        title, prefix, opts = choices[field]
        rows = [[_btn(label, f"cy:{prefix}:{val}") for label, val in opts[i:i + 3]] for i in range(0, len(opts), 3)]
        unknown_label = "♾ 제한없음" if field == "price" else "❔ 모름"
        rows.append([_btn(unknown_label, f"cy:{prefix}:U")] + back)
        return title + " 를 골라주세요.", {"inline_keyboard": rows}

    def render_list(self, profile: UserProfile) -> tuple[str, dict[str, Any]]:
        notices = self.db.list_open_notices(self.today_provider(), include_samples=self.include_samples)
        scored = [(n, self.engine.evaluate(n, profile)) for n in notices]
        order = {EligibilityStatus.ELIGIBLE: 0, EligibilityStatus.PARTIAL_UNKNOWN: 1, EligibilityStatus.INELIGIBLE: 2}
        scored.sort(key=lambda x: (order[x[1].status], x[0].receipt_start or date.max))
        shown = [x for x in scored if x[1].status != EligibilityStatus.INELIGIBLE][:LIST_VIEW_LIMIT]
        kb = {"inline_keyboard": [[_btn("⬅️ 내 조건", "cy:home"), _btn("🔄 새로고침", "cy:list")]]}
        if not shown:
            return "📋 <b>진행중 공고</b>\n지금 조건에 맞는 공고가 없어요.", kb
        blocks = [render_notice_block(n, r) for n, r in shown]
        header = f"📋 <b>진행중 공고</b> · <b>{len(shown)}</b>건"
        return split_blocks(header, blocks, DISCLAIMER)[0][0], kb

    def render_settings(self, profile: UserProfile) -> tuple[str, dict[str, Any]]:
        reg_on = self.db.get_regular_sub(profile.chat_id) is True
        alert_on = profile.alerts_on
        ofc_on = profile.include_officetel is True
        text = "\n".join([
            "🔔 <b>알림 설정</b>",
            f"📰 08:00 정기 다이제스트 <b>{'ON' if reg_on else 'OFF'}</b>",
            f"🎯 맞춤 조건 알림 <b>{'ON' if alert_on else 'OFF'}</b>",
            f"🏢 오피스텔·도시형 포함 <b>{'ON' if ofc_on else 'OFF'}</b>",
            "🌙 22:00~07:30 맞춤 알림은 08:00에 모아 보내요.",
        ])
        kb = [
            [_btn(f"📰 정기 {'끄기' if reg_on else '켜기'}", f"cy:reg:{'off' if reg_on else 'on'}")],
            [_btn(f"🎯 맞춤 {'끄기' if alert_on else '켜기'}", f"cy:alert:{'off' if alert_on else 'on'}")],
            [_btn(f"🏢 오피스텔 {'제외' if ofc_on else '포함'}", f"cy:ofc:{'off' if ofc_on else 'on'}")],
            [_btn("🧹 조건 초기화", "cy:reset:ask"), _btn("⬅️ 내 조건", "cy:home")],
        ]
        return text, {"inline_keyboard": kb}

    def render_regular(self, profile: UserProfile) -> tuple[str, dict[str, Any]]:
        prefs = self.db.get_regular_prefs(profile.chat_id)
        reg_on = prefs.is_enabled
        types = [label for label, on in (("무순위", prefs.moosunwi), ("공공", prefs.public), ("일반APT", prefs.general)) if on]
        areas = ", ".join(AREA_BUCKET_LABELS[a] for a in prefs.areas) if prefs.areas else "전체"
        lines = [
            "📰 <b>08:00 정기 청약 다이제스트</b>",
            f"📬 현재 상태 <b>{'ON' if reg_on else 'OFF'}</b>",
            f"🗂 유형 <b>{esc(', '.join(types) or '없음')}</b> · 면적 <b>{esc(areas)}</b>"
            + (" · ⏰ <b>마감 D-1</b>" if prefs.deadline_d1 else ""),
            f"🗓 매일 08:00 최대 <b>{DAILY_DIGEST_LIMIT}</b>건 (자격 미달 제외, 이미 받은 공고 제외)",
        ]
        if not types:
            lines.append("⏰ 유형을 모두 끄면 <b>내일 마감</b> 공고만 보내요." if prefs.deadline_d1
                         else "⚠️ 유형을 하나 이상 켜거나 마감 D-1을 켜야 받아볼 수 있어요.")
        elif prefs.deadline_d1:
            lines.append("⏰ 내일 마감 공고를 맨 앞에 보여드려요.")

        def mark(on: bool) -> str:
            return "✅" if on else "⬜"

        chosen = set(prefs.areas or [])
        kb = [
            [_btn(f"{mark(prefs.moosunwi)} 무순위(줍줍)", "cy:rt:m"), _btn(f"{mark(prefs.public)} 공공분양", "cy:rt:p")],
            [_btn(f"{mark(prefs.general)} 일반APT", "cy:rt:g"), _btn(f"{mark(prefs.deadline_d1)} 마감 D-1", "cy:rt:d")],
            [_btn(f"{mark(a in chosen)} {AREA_BUCKET_LABELS[a]}", f"cy:ra:{a}") for a in AREA_BUCKETS],
            [_btn(f"{mark(not chosen)} 전체 면적", "cy:ra:X")],
            [_btn(f"📰 정기 {'끄기' if reg_on else '켜기'}", f"cy:reg:{'off' if reg_on else 'onv'}")],
            [_btn("📋 오늘 공고", "cy:list"), _btn("⬅️ 내 조건", "cy:home")],
        ]
        return "\n".join(lines), {"inline_keyboard": kb}


def build_demo_dispatcher() -> SafeTelegramDispatcher:
    limiter = TelegramRateLimiter(max_global_rate=1000.0, min_chat_interval=0.0, sleep=lambda _s: None)
    return SafeTelegramDispatcher(rate_limiter=limiter, sleep=lambda _s: None)


__all__ = [
    "COMMANDS_ALL",
    "ChungyakAlertService",
    "ChungyakInteractiveBot",
    "build_demo_dispatcher",
    "filter_regular_notices",
    "in_morning_window",
    "is_private_chat",
    "parse_condition_text",
    "is_quiet_hours",
    "next_morning_slot",
]
