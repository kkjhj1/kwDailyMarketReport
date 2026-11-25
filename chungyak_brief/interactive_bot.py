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
import logging
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
    SIDO_CODES,
    SPECIAL_SUPPLY_TYPES,
    ChannelType,
    ChungyakNotice,
    DeliveryStatus,
    EligibilityStatus,
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
        notices = self._open_notices(now_kst.date())
        for chat_id in self.db.list_regular_sub_chat_ids():
            if chat_id not in active:
                continue
            profile = self.db.get_profile(chat_id) or UserProfile(chat_id=chat_id)
            picked: list[tuple[ChungyakNotice, Any]] = []
            for notice in notices:
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
    ) -> None:
        self.db = db
        self.channel = channel
        self.engine = engine or EligibilityEngine()
        self.access_checker = access_checker
        self.include_samples = include_samples
        self.today_provider = today_provider or (lambda: to_kst().date())

    # ------------------------------------------------------------------ routing
    def handle_update(self, update: dict[str, Any]) -> Optional[str]:
        if "callback_query" in update:
            return self._handle_callback(update["callback_query"])
        msg = update.get("message") or {}
        text = str(msg.get("text") or "").strip()
        chat_id = str((msg.get("chat") or {}).get("id") or "")
        if not chat_id or not text:
            return None
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
        elif action == "ask":
            view = self.render_question(profile, arg)
        elif action == "sido":
            profile = self.db.update_profile(chat_id, residence_sido=SIDO_CODES.get(arg))
            view = self.render_home(profile)
        elif action == "ry":
            profile = self.db.update_profile(chat_id, residence_years=self._int_or_none(arg))
            view = self.render_home(profile)
        elif action == "hl":
            profile = self.db.update_profile(chat_id, is_homeless=self._bool_or_none(arg))
            view = self.render_home(profile)
        elif action == "hh":
            profile = self.db.update_profile(chat_id, is_household_head=self._bool_or_none(arg))
            view = self.render_home(profile)
        elif action == "acct":
            profile = self.db.update_profile(chat_id, account_months=self._int_or_none(arg))
            view = self.render_home(profile)
        elif action == "dep":
            profile = self.db.update_profile(chat_id, deposit_manwon=self._int_or_none(arg))
            view = self.render_home(profile)
        elif action == "pay":
            profile = self.db.update_profile(chat_id, payment_count=self._int_or_none(arg))
            view = self.render_home(profile)
        elif action == "price":
            dec = to_decimal(arg) if arg not in ("", "U") else None
            profile = self.db.update_profile(chat_id, max_price_eok=dec)
            view = self.render_home(profile)
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

    @staticmethod
    def _int_or_none(arg: str) -> Optional[int]:
        return int(arg) if arg.isdigit() else None

    @staticmethod
    def _bool_or_none(arg: str) -> Optional[bool]:
        return {"Y": True, "N": False}.get(arg.upper())

    # ------------------------------------------------------------------ views
    def render_home(self, profile: UserProfile) -> tuple[str, dict[str, Any]]:
        p = profile
        regions = ", ".join(p.preferred_regions) if p.preferred_regions else "전국"
        specials = ", ".join(SPECIAL_LABELS.get(s, s) for s in (p.special_types or [])) or "없음/미입력"
        lines = [
            "🏠 <b>내 청약 조건</b>",
            f"📍 거주지 <b>{esc(p.residence_sido or '미입력')}</b> · 거주 {esc('미입력' if p.residence_years is None else str(p.residence_years) + '년+')}",
            f"🏚 무주택 <b>{esc(_yn(p.is_homeless))}</b> · 세대주 <b>{esc(_yn(p.is_household_head))}</b>",
            f"📒 통장 <b>{esc('미입력' if p.account_months is None else str(p.account_months) + '개월+')}</b>"
            f" · 예치금 <b>{esc('미입력' if p.deposit_manwon is None else str(p.deposit_manwon) + '만')}</b>",
            f"🧾 납입 <b>{esc('미입력' if p.payment_count is None else str(p.payment_count) + '회+')}</b>"
            f" · 예산 <b>{esc('제한없음' if p.max_price_eok is None else decimal_text(p.max_price_eok) + '억')}</b>",
            f"🎁 특공 <b>{esc(specials)}</b>",
            f"🗺 관심지역 <b>{esc(regions)}</b>",
        ]
        if p.is_empty():
            lines.append("🟡 조건을 입력할수록 자격 판정이 정확해져요.")
        kb = [
            [_btn("📍 거주지", "cy:ask:sido"), _btn("⏳ 거주기간", "cy:ask:ry")],
            [_btn("🏚 무주택", "cy:ask:hl"), _btn("👤 세대주", "cy:ask:hh")],
            [_btn("📒 통장기간", "cy:ask:acct"), _btn("💵 예치금", "cy:ask:dep")],
            [_btn("🧾 납입횟수", "cy:ask:pay"), _btn("💰 예산", "cy:ask:price")],
            [_btn("🎁 특별공급", "cy:ask:sp"), _btn("🗺 관심지역", "cy:ask:rg")],
            [_btn("📋 진행중 공고", "cy:list"), _btn("🔔 알림 설정", "cy:set")],
            [_btn("🧹 조건 초기화", "cy:reset:ask")],
        ]
        return "\n".join(lines), {"inline_keyboard": kb}

    def render_question(self, profile: UserProfile, field: str) -> tuple[str, dict[str, Any]]:
        back = [_btn("⬅️ 내 조건", "cy:home")]
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
            [_btn("⬅️ 내 조건", "cy:home")],
        ]
        return text, {"inline_keyboard": kb}

    def render_regular(self, profile: UserProfile) -> tuple[str, dict[str, Any]]:
        reg_on = self.db.get_regular_sub(profile.chat_id) is True
        text = "\n".join([
            "📰 <b>08:00 정기 청약 다이제스트</b>",
            f"📬 현재 상태 <b>{'ON' if reg_on else 'OFF'}</b>",
            f"🗂 매일 08:00 진행중 공고 최대 <b>{DAILY_DIGEST_LIMIT}</b>건 (자격 미달 제외)",
            "🔁 맞춤 알림으로 이미 받은 공고는 다시 보내지 않아요.",
        ])
        kb = [
            [_btn(f"📰 정기 {'끄기' if reg_on else '켜기'}", f"cy:reg:{'off' if reg_on else 'on'}")],
            [_btn("📋 오늘 공고", "cy:list"), _btn("⬅️ 내 조건", "cy:home")],
        ]
        return text, {"inline_keyboard": kb}


def build_demo_dispatcher() -> SafeTelegramDispatcher:
    limiter = TelegramRateLimiter(max_global_rate=1000.0, min_chat_interval=0.0, sleep=lambda _s: None)
    return SafeTelegramDispatcher(rate_limiter=limiter, sleep=lambda _s: None)


__all__ = [
    "COMMANDS_ALL",
    "ChungyakAlertService",
    "ChungyakInteractiveBot",
    "build_demo_dispatcher",
    "in_morning_window",
    "is_quiet_hours",
    "next_morning_slot",
]
