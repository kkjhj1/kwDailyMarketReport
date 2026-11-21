"""Interactive UI Renderer & State Handler for Portal Hub Bot (v2.2).
Equipped with:
1. In-Place Accidental Click Prevention (Inline 2-Step Confirmation without losing context).
2. Category-based 2-Tap Discovery (Finance, Edu, Life, Biz).
3. Instant Keyword Search Engine.
4. Immediate Callback Acknowledgement to eliminate button spinner lag.
"""
from __future__ import annotations
from datetime import datetime
import html
import logging
import os
import re
from typing import Any, Optional

from market_brief.channels.http_keepalive import AckOnceClient, callback_ack_scope
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from portal_hub.db import PortalDatabase
from portal_hub.models import BotCategory, BotServiceType, UserProfile, UserTier
from portal_hub.telegram import TelegramPortalClient

logger = logging.getLogger(__name__)


# Legacy bottom reply-keyboard texts; still matched as text aliases by match_menu_action.
LEGACY_REPLY_KEYBOARD_TEXTS: tuple[str, ...] = (
    "📋 내 구독 관리",
    "📂 봇 둘러보기",
    "💎 내 등급 혜택",
    "❓ 이용 도움말",
)


def build_remove_reply_keyboard() -> dict[str, Any]:
    """Removes any legacy bottom reply keyboard; menus are served as inline cards + /commands."""
    return {"remove_keyboard": True}


def build_persistent_reply_keyboard() -> dict[str, Any]:
    """Deprecated: kept for un-updated callers; now returns the remove-keyboard payload."""
    return build_remove_reply_keyboard()


def build_slot_gauge(active_count: int, tier: UserTier) -> str:
    """Builds graphical slot progress gauge with tier color accents."""
    if tier == UserTier.BLACK:
        return f"[ ⬛ ⬛ ⬛ ★ ] {active_count}개 활성화 (무제한)"

    max_slots = tier.max_slots
    filled_char = tier.gauge_char
    empty_char = "◽"

    filled_len = min(active_count, max_slots)
    empty_len = max(0, max_slots - filled_len)

    gauge_str = " ".join([filled_char] * filled_len + [empty_char] * empty_len)
    status_suffix = " (FULL)" if filled_len >= max_slots else ""
    return f"[ {gauge_str} ] {filled_len}/{max_slots}개 사용 중{status_suffix}"


# Alert for legacy `sub:locked:` / stale `gate:` buttons still present in old chat messages.
SUB_LOCKED_ALERT = "🔒 구독 중인 봇만 대화방에 입장할 수 있습니다.\n먼저 [➕ 구독] 버튼을 눌러 구독을 완료해 주세요."


def _bot_deep_link(bot: BotServiceType) -> str:
    """Direct chat-room URL for a bot (start link, else handle fallback)."""
    return bot.get_start_link() or f"https://t.me/{bot.telegram_handle}?start=portal"


def _short_chip(bot: BotServiceType) -> str:
    """Compact bot label for 360px 2-column buttons (e.g. `시황`, `여행`, `키즈`)."""
    return bot.short_name.split()[0]


def _bot_action_row(bot: BotServiceType, is_sub: bool, context_str: str) -> list[dict[str, str]]:
    """Subscribed: [🚀 <칩> 입장](url) + [<칩> 해지]; un-subscribed: single [➕ <봇> 구독하기] (no URL/gate/lock)."""
    if is_sub:
        chip = _short_chip(bot)
        return [
            {"text": f"🚀 {chip} 입장", "url": _bot_deep_link(bot)},
            {"text": f"{chip} 해지", "callback_data": f"sub:ask_unsub:{bot.value}:{context_str}"},
        ]
    return [{"text": f"➕ {bot.short_name} 구독하기", "callback_data": f"sub:ask_sub:{bot.value}:{context_str}"}]


# Tier rank for upgrade validation (WHITE and FREE are equivalent base tiers).
_TIER_RANK: dict[UserTier, int] = {
    UserTier.WHITE: 0,
    UserTier.FREE: 0,
    UserTier.GREEN: 1,
    UserTier.RED: 2,
    UserTier.BLACK: 3,
}


def _sync_domain_alerts(db: PortalDatabase, user_id: Any, bot_id: str, active: bool) -> None:
    """Best-effort platform-engine hook: swap-out / unsubscribe deactivates domain watches & alerts;
    a new subscription restores the admission alert switch. Never raises."""
    if str(getattr(db, "db_path", ":memory:")) == ":memory:":
        return
    try:
        from market_brief.storage.db import MarketBriefDatabase
        from market_brief.subscription_engine import SubscriptionEngine

        engine = SubscriptionEngine(MarketBriefDatabase(db_path=str(db.db_path)))
        if active:
            engine.restore_domain_alerts(str(user_id), bot_id)
        else:
            engine.deactivate_domain_alerts(str(user_id), bot_id)
    except Exception as exc:
        logger.warning("domain alert sync failed for %s/%s: %s", user_id, bot_id, exc)


def _gate_target_bot(data: str) -> Optional[BotServiceType]:
    """Maps `gate:<action>:bot_<name>` back to its BotServiceType (None for non-bot gate tokens)."""
    parts = data.split(":")
    token = parts[2] if len(parts) >= 3 else ""
    for b in BotServiceType:
        if token == f"bot_{b.value.replace('_bot', '')}":
            return b
    return None


HIDDEN_BOT_ALERT = "🚧 현재 서비스 준비 중인 봇입니다."

# New-subscription entry points: bot id at parts[2] (subscribe) or parts[3] (swap target).
_NEW_SUB_PREFIXES = ("sub:ask_sub:", "sub:do_sub:", "menu:sub:", "info:sub:")
_SWAP_PREFIXES = ("sub:swap:", "menu:swap:", "info:swap:")


def _hidden_new_sub_target(data: str) -> Optional[BotServiceType]:
    """Returns the hidden (non-public) bot a callback tries to newly subscribe/swap into, else None.
    Unknown bot ids return None so the existing ValueError handling of each branch still applies."""
    parts = data.split(":")
    if data.startswith(_NEW_SUB_PREFIXES):
        token = parts[2] if len(parts) >= 3 else ""
    elif data.startswith(_SWAP_PREFIXES):
        token = parts[3] if len(parts) >= 4 else ""
    else:
        return None
    try:
        bot_type = BotServiceType(token)
    except ValueError:
        return None
    return None if bot_type.is_public else bot_type


def render_hidden_unsub_confirm_view(
    bot_type: BotServiceType, context_str: str = "subs"
) -> tuple[str, dict[str, Any]]:
    """Standalone 2-button unsubscribe confirm card for a hidden bot (it is not listed in any public view)."""
    text = (
        f"⚠️ <b>[{html.escape(bot_type.short_name)}] 구독을 정말 해지하시겠습니까?</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>아래 [🔴 해지 확정] 또는 [↩️ 취소]를 선택해 주세요.</i>"
    )
    keyboard = [[
        {"text": "🔴 해지 확정", "callback_data": f"sub:do_unsub:{bot_type.value}:{context_str}"},
        {"text": "↩️ 취소", "callback_data": f"sub:cancel_unsub:{context_str}"},
    ]]
    return text, {"inline_keyboard": keyboard}


def _safe_edit_or_send(
    client: TelegramPortalClient,
    chat_id: int | str,
    message_id: Optional[int],
    text: str,
    reply_markup: Optional[dict[str, Any]] = None,
) -> None:
    """Edits message in-place; if Telegram edit fails (e.g. outdated message), sends fresh message as fallback."""
    ok = False
    if message_id is not None:
        ok = client.edit_message_text(chat_id, message_id, text, reply_markup=reply_markup)
    if not ok:
        logger.info("edit_message_text fallback to send_message for chat %s", chat_id)
        client.send_message(chat_id, text, reply_markup=reply_markup)


# ============================================================
# Screen Renderers
# ============================================================

def render_main_menu(
    user: UserProfile, subs: Optional[dict[BotServiceType, bool]] = None
) -> tuple[str, dict[str, Any]]:
    """Renders Welcome & Main Hub Dashboard with streamlined 3-row layout and active subscriptions chip."""
    name = html.escape(user.first_name or user.username or "회원")
    active_line = ""
    if subs is not None:
        active_bots = [b.short_name for b, act in subs.items() if act and b.is_public]
        if active_bots:
            chips = " · ".join(active_bots)
            active_line = f"📌 <b>내 활성 구독 ({len(active_bots)}개)</b>: {chips}\n"
        else:
            active_line = "📌 <b>내 활성 구독</b>: 아직 구독 중인 봇이 없습니다.\n"

    text = (
        f"👋 안녕하세요, <b>{name}</b>님!\n"
        f"<b>통합 인텔리전스 허브(Hub)</b>에 오신 것을 환영합니다.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏅 <b>나의 회원 등급</b>: {user.tier.badge}\n"
        f"📦 <b>구독 가능 슬롯</b>: <b>{user.tier.max_slots if user.tier != UserTier.BLACK else '무제한'}개</b>\n"
        f"{active_line}"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"💡 <i>전체 봇을 둘러보시려면 아래 버튼을 누르거나, 찾으시는 단어(예: '시황', '여행', '키즈')를 입력하세요!</i>"
    )

    keyboard: list[list[dict[str, str]]] = []
    active_public = [b for b in BotServiceType if b.is_public and (subs or {}).get(b, False)]
    if len(active_public) == 1:
        b = active_public[0]
        keyboard.append([{"text": f"🚀 {b.short_name} 바로가기", "url": _bot_deep_link(b)}])
    elif active_public:
        entry_btns = [{"text": f"🚀 {_short_chip(b)} 입장", "url": _bot_deep_link(b)} for b in active_public]
        keyboard.extend(entry_btns[i:i + 2] for i in range(0, len(entry_btns), 2))

    keyboard.extend([
        [{"text": "📋 봇 둘러보기 · 구독", "callback_data": "nav:subs"}],
        [{"text": "💎 등급 혜택", "callback_data": "nav:tiers"}],
        [
            {"text": "💬 고객센터", "callback_data": "support:home"},
            {"text": "❓ 이용안내", "callback_data": "nav:help"},
        ],
    ])
    return text, {"inline_keyboard": keyboard}


def render_subscription_view(
    user: UserProfile,
    subs: dict[BotServiceType, bool],
    confirming_bot_id: Optional[str] = None,
    confirming_sub_bot_id: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
    """Renders Subscription Dashboard with In-Place Subscribe/Unsubscribe Confirmation."""
    name = html.escape(user.first_name or user.username or "회원")
    active_count = sum(1 for v in subs.values() if v)
    gauge_display = build_slot_gauge(active_count, user.tier)

    notice_part = ""
    if confirming_bot_id:
        target_bot = next((b for b in BotServiceType if b.value == confirming_bot_id), None)
        target_name = html.escape(target_bot.short_name if target_bot else confirming_bot_id)
        notice_part = (
            f"\n⚠️ <b>[{target_name}] 구독을 정말 해지하시겠습니까?</b>\n"
            f"<i>아래 [🔴 해지 확정] 또는 [↩️ 취소]를 선택해 주세요.</i>\n"
        )
    elif confirming_sub_bot_id:
        target_bot = next((b for b in BotServiceType if b.value == confirming_sub_bot_id), None)
        target_name = html.escape(target_bot.short_name if target_bot else confirming_sub_bot_id)
        notice_part = (
            f"\n⚠️ <b>[{target_name}] 알림을 구독하시겠습니까?</b>\n"
            f"<i>아래 [✅ 구독 확정] 또는 [↩️ 취소]를 선택해 주세요.</i>\n"
        )

    public_bots = [b for b in BotServiceType if b.is_public]
    bot_lines = []
    for i, b in enumerate(public_bots, 1):
        status_chip = "[🟢 구독중]" if subs.get(b, False) else "[⚪ 미구독]"
        bot_lines.append(f"<b>{i}. {b.icon} {html.escape(b.short_name)}</b> {status_chip}")
        bot_lines.append(f"   • {html.escape(b.one_line_desc)}")
    bot_block = "\n".join(bot_lines)

    text = (
        f"📋 <b>[{name}] 님의 구독 센터</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏅 <b>회원 등급</b>: {user.tier.badge}\n"
        f"📊 <b>구독 슬롯</b>: <b>{gauge_display}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{bot_block}\n"
        f"━━━━━━━━━━━━━━━━━━━━"
        f"{notice_part}\n"
        f"💡 <i>버튼을 누르면 2단계 확인 후 구독 또는 해지가 진행됩니다.</i>"
    )

    keyboard = []
    for bot in public_bots:
        is_subbed = subs.get(bot, False)

        if confirming_bot_id == bot.value:
            keyboard.append([
                {"text": "🔴 해지 확정", "callback_data": f"sub:do_unsub:{bot.value}:subs"},
                {"text": "↩️ 취소", "callback_data": "sub:cancel_unsub:subs"},
            ])
        elif confirming_sub_bot_id == bot.value:
            keyboard.append([
                {"text": "✅ 구독 확정", "callback_data": f"sub:do_sub:{bot.value}:subs"},
                {"text": "↩️ 취소", "callback_data": "sub:cancel_sub:subs"},
            ])
        else:
            keyboard.append(_bot_action_row(bot, is_subbed, "subs"))

    keyboard.extend([
        [
            {"text": "📋 전체 봇", "callback_data": "cat:all"},
            {"text": "💎 등급 혜택", "callback_data": "nav:tiers"},
        ],
        [
            {"text": "🏠 홈으로", "callback_data": "nav:main"},
        ],
    ])

    return text, {"inline_keyboard": keyboard}


def render_category_view(
    category: BotCategory,
    user: UserProfile,
    subs: dict[BotServiceType, bool],
    confirming_bot_id: Optional[str] = None,
    confirming_sub_bot_id: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
    """Renders 3-item Compact Chip View for selected category with In-Place Confirmation."""
    bots = [b for b in BotServiceType if b.is_public and b.category == category]

    lines = [
        f"<b>{html.escape(category.title)} 봇 카탈로그</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    if confirming_bot_id:
        target_bot = next((b for b in BotServiceType if b.value == confirming_bot_id), None)
        target_name = html.escape(target_bot.short_name if target_bot else confirming_bot_id)
        lines.append(f"⚠️ <b>[{target_name}] 구독을 정말 해지하시겠습니까?</b>")
        lines.append("<i>아래 [🔴 해지 확정] 또는 [↩️ 취소]를 터치하세요.</i>\n")
    elif confirming_sub_bot_id:
        target_bot = next((b for b in BotServiceType if b.value == confirming_sub_bot_id), None)
        target_name = html.escape(target_bot.short_name if target_bot else confirming_sub_bot_id)
        lines.append(f"⚠️ <b>[{target_name}] 알림을 구독하시겠습니까?</b>")
        lines.append("<i>아래 [✅ 구독 확정] 또는 [↩️ 취소]를 터치하세요.</i>\n")

    if not bots:
        lines.append("<i>현재 준비 중인 카테고리입니다. 신규 봇이 곧 출시됩니다!</i>")
    else:
        lines.append(f"총 {len(bots)}개의 전문 봇이 제공되고 있습니다.\n")
        for i, b in enumerate(bots, 1):
            is_sub = subs.get(b, False)
            status_chip = "🟢 [구독중]" if is_sub else "⚪ [미구독]"
            lines.append(f"<b>{i}. {b.icon} {html.escape(b.short_name)}</b> {status_chip}")
            lines.append(f"   • {html.escape(b.one_line_desc)}")

    lines.append("━━━━━━━━━━━━━━━━━━━━")

    keyboard = []
    for b in bots:
        is_sub = subs.get(b, False)

        if confirming_bot_id == b.value:
            keyboard.append([
                {"text": "🔴 해지 확정", "callback_data": f"sub:do_unsub:{b.value}:cat:{category.value}"},
                {"text": "↩️ 취소", "callback_data": f"sub:cancel_unsub:cat:{category.value}"},
            ])
        elif confirming_sub_bot_id == b.value:
            keyboard.append([
                {"text": "✅ 구독 확정", "callback_data": f"sub:do_sub:{b.value}:cat:{category.value}"},
                {"text": "↩️ 취소", "callback_data": f"sub:cancel_sub:cat:{category.value}"},
            ])
        else:
            keyboard.append(_bot_action_row(b, is_sub, f"cat:{category.value}"))

    keyboard.extend([
        [
            {"text": "📂 카테고리", "callback_data": "cat:home"},
            {"text": "📋 구독 관리", "callback_data": "nav:subs"},
        ],
        [
            {"text": "🏠 홈으로", "callback_data": "nav:main"},
        ],
    ])

    return "\n".join(lines), {"inline_keyboard": keyboard}


def render_category_home_view() -> tuple[str, dict[str, Any]]:
    """Renders all categories overview."""
    text = (
        f"📂 <b>전체 카테고리 탐색</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"원하시는 분야를 선택하시면 세부 봇 목록을 확인하실 수 있습니다.\n\n"
        f"• 💰 <b>금융 / 재테크</b>: 글로벌 시황, 증시, 환율 등 (시황 브리핑)\n"
        f"• 🎓 <b>교육 / 입시</b>: 신규 교육 봇 출시 예정 (준비중)\n"
        f"• ✈️ <b>라이프 / 여가</b>: 특가 항공권, 주말 나들이·육아 등 (여행, 키즈)\n"
        f"• 💼 <b>업무 / 생산성</b>: 뉴스 클리핑, 생산성 도구 등 (준비중)\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    keyboard = [
        [
            {"text": "💰 금융/재테크", "callback_data": "cat:view:FINANCE"},
            {"text": "🎓 교육/입시", "callback_data": "cat:view:EDU"},
        ],
        [
            {"text": "✈️ 라이프/여가", "callback_data": "cat:view:LIFE"},
            {"text": "💼 업무/생산성", "callback_data": "cat:view:BIZ"},
        ],
        [
            {"text": "📋 전체 봇 둘러보기", "callback_data": "cat:all"},
        ],
        [
            {"text": "🏠 홈으로", "callback_data": "nav:main"},
        ],
    ]
    return text, {"inline_keyboard": keyboard}


CATALOG_ALL_ORDER = (
    BotServiceType.BRIEF,
    BotServiceType.TRAVEL,
    BotServiceType.KIDS,
)

CALLBACK_DATA_MAX_BYTES = 64
# Longest search-context prefix: `sub:ask_unsub:<longest bot_id>:search:`
_SEARCH_CB_PREFIX_MAX = len("sub:ask_unsub:") + max(len(b.value) for b in BotServiceType) + len(":search:")


def fit_search_query_for_callback(query: str) -> str:
    """Normalizes the `search:<q>` context so EVERY search button stays <= 64 bytes ([TG-04]).
    One shared byte budget (not per-button) keeps all buttons of a view on the same query; cut on a
    character boundary so multi-byte Korean text never splits. Raw text (no HTML escape) is stored."""
    budget = CALLBACK_DATA_MAX_BYTES - _SEARCH_CB_PREFIX_MAX
    q = " ".join((query or "").split())
    while q and len(q.encode("utf-8")) > budget:
        q = q[:-1]
    return q.rstrip()


def render_catalog_all_view(
    user: UserProfile,
    subs: dict[BotServiceType, bool],
    confirming_bot_id: Optional[str] = None,
    confirming_sub_bot_id: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
    """Renders all public bots on one screen with subscription badges and 2-step confirmation buttons."""
    active_count = sum(1 for v in subs.values() if v)
    lines = [
        "📋 <b>전체 봇 한눈에 보기</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📊 <b>구독 슬롯</b>: <b>{build_slot_gauge(active_count, user.tier)}</b>",
    ]

    if confirming_bot_id:
        target_bot = next((b for b in BotServiceType if b.value == confirming_bot_id), None)
        target_name = html.escape(target_bot.short_name if target_bot else confirming_bot_id)
        lines.append(f"\n⚠️ <b>[{target_name}] 구독을 정말 해지하시겠습니까?</b>")
        lines.append("<i>아래 [🔴 해지 확정] 또는 [↩️ 취소]를 터치하세요.</i>")
    elif confirming_sub_bot_id:
        target_bot = next((b for b in BotServiceType if b.value == confirming_sub_bot_id), None)
        target_name = html.escape(target_bot.short_name if target_bot else confirming_sub_bot_id)
        lines.append(f"\n⚠️ <b>[{target_name}] 알림을 구독하시겠습니까?</b>")
        lines.append("<i>아래 [✅ 구독 확정] 또는 [↩️ 취소]를 터치하세요.</i>")

    lines.append("")
    for i, b in enumerate(CATALOG_ALL_ORDER, 1):
        status_chip = "[🟢 구독중]" if subs.get(b, False) else "[⚪ 미구독]"
        lines.append(f"<b>{i}. {b.icon} {html.escape(b.short_name)}</b> {status_chip}")
        lines.append(f"   • {html.escape(b.one_line_desc)}")
    lines.append("━━━━━━━━━━━━━━━━━━━━")

    keyboard = []
    for b in CATALOG_ALL_ORDER:
        if confirming_bot_id == b.value:
            keyboard.append([
                {"text": "🔴 해지 확정", "callback_data": f"sub:do_unsub:{b.value}:all"},
                {"text": "↩️ 취소", "callback_data": "sub:cancel_unsub:all"},
            ])
            continue
        elif confirming_sub_bot_id == b.value:
            keyboard.append([
                {"text": "✅ 구독 확정", "callback_data": f"sub:do_sub:{b.value}:all"},
                {"text": "↩️ 취소", "callback_data": "sub:cancel_sub:all"},
            ])
            continue

        keyboard.append(_bot_action_row(b, subs.get(b, False), "all"))

    keyboard.extend([
        [
            {"text": "📂 카테고리", "callback_data": "cat:home"},
            {"text": "📋 구독 관리", "callback_data": "nav:subs"},
        ],
        [
            {"text": "🏠 홈으로", "callback_data": "nav:main"},
        ],
    ])
    return "\n".join(lines), {"inline_keyboard": keyboard}


def _context_return_buttons(bot_type: BotServiceType, context_str: str) -> list[dict[str, Any]]:
    """Returns a 'back to where you came from' button for catalog/category contexts."""
    if context_str == "all":
        return [{"text": "📋 전체 봇 목록", "callback_data": "cat:all"}]
    if context_str.startswith("cat:"):
        return [{"text": "📂 카테고리", "callback_data": f"cat:view:{bot_type.category.value}"}]
    return []


def _onboarding_keyboard(bot_type: BotServiceType, context_str: str) -> dict[str, Any]:
    """Deep link into the bot's chat room + subs/home (+ optional context return) buttons."""
    keyboard = []
    deep_link = bot_type.get_start_link()
    if deep_link:
        keyboard.append([{"text": f"🚀 {bot_type.short_name} 대화방 바로가기", "url": deep_link}])
    keyboard.append([
        {"text": "📋 구독 관리", "callback_data": "nav:subs"},
        {"text": "🏠 홈으로", "callback_data": "nav:main"},
    ])
    back_row = _context_return_buttons(bot_type, context_str)
    if back_row:
        keyboard.append(back_row)
    return {"inline_keyboard": keyboard}


def render_subscribe_onboarding_view(
    bot_type: BotServiceType, context_str: str = "subs"
) -> tuple[str, dict[str, Any]]:
    """Renders subscription-complete card with schedule guidance and 1-tap deep link."""
    schedule_map = {
        BotServiceType.BRIEF: "매 영업일 아침 07:50 (장전 핵심 요약)",
        BotServiceType.ADMISSION: "매주 월요일 아침 08:00 (대입 To-Do & 설명회)",
        BotServiceType.TRAVEL: "매주 화·목 오전 10:00 (최저가 특가 딜)",
        BotServiceType.SILVER: "매주 일요일 오전 09:00 (부모님 복지 브리핑)",
        BotServiceType.KIDS: "매주 금요일 오전 10:00 (주말 나들이 & 육아 대처)",
        BotServiceType.YOLO: "매주 금요일 오후 14:00 (주말 1인 취미 & 힐링)",
    }
    sched = schedule_map.get(bot_type, "정기 발행 스케줄에 맞춰 발송")
    text = (
        f"🎉 <b>{html.escape(bot_type.title)} 구독 완료!</b>\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⏰ <b>정규 발송 시점</b>: {sched}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 <i>대화방으로 이동하신 후, 화면 하단의 <b>[시작(START)]</b> 버튼을 꼭 눌러주세요. 그래야 알림이 정상 도착합니다!</i>"
    )
    return text, _onboarding_keyboard(bot_type, context_str)


def build_swap_callback_data(old_bot: BotServiceType, new_bot: BotServiceType, context_str: str) -> str:
    """Builds `sub:swap:<old>:<new>:<ctx>`, falling back to ctx `subs` if it would exceed 64 bytes."""
    data = f"sub:swap:{old_bot.value}:{new_bot.value}:{context_str}"
    if len(data.encode("utf-8")) > CALLBACK_DATA_MAX_BYTES:
        data = f"sub:swap:{old_bot.value}:{new_bot.value}:subs"
    return data


def render_quota_swap_view(
    bot_type: BotServiceType,
    swappable_bots: list[BotServiceType],
    reason: str,
    context_str: str = "subs",
) -> tuple[str, dict[str, Any]]:
    """Renders over-quota notice with 1-click swap buttons for each currently active public bot."""
    swappable_bots = [b for b in swappable_bots if b.is_public]
    text = html.escape(reason)
    if swappable_bots:
        text += (
            f"\n\n🔄 <b>기존 구독을 [{html.escape(bot_type.short_name)}](으)로 바로 교체할 수 있습니다.</b>\n"
            f"<i>교체할 봇을 선택해 주세요.</i>"
        )
    keyboard = [
        [{
            "text": f"🔄 {old_b.short_name} ➔ {bot_type.short_name}",
            "callback_data": build_swap_callback_data(old_b, bot_type, context_str),
        }]
        for old_b in swappable_bots
    ]
    keyboard.append([{"text": "💎 등급 업그레이드", "callback_data": "support:upgrade_menu"}])
    keyboard.append([{"text": "↩️ 돌아가기", "callback_data": "nav:subs"}])
    return text, {"inline_keyboard": keyboard}


def render_swap_result_view(
    ok: bool, msg: str, new_bot: BotServiceType, context_str: str = "subs"
) -> tuple[str, dict[str, Any]]:
    """Renders swap success (with deep link onboarding) or the failure reason."""
    if not ok:
        text = f"⚠️ <b>구독 교체 실패</b>\n\n{html.escape(msg)}"
        return text, {"inline_keyboard": [[{"text": "📋 구독 관리", "callback_data": "nav:subs"}]]}

    text = (
        f"🔄 <b>{html.escape(msg)}</b>\n\n"
        f"지금 바로 대화방으로 이동하여 알림을 시작해보세요."
    )
    return text, _onboarding_keyboard(new_bot, context_str)


def render_search_results_view(
    query: str,
    matched_bots: list[BotServiceType],
    user: UserProfile,
    subs: dict[BotServiceType, bool],
    confirming_bot_id: Optional[str] = None,
    confirming_sub_bot_id: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
    """Renders instant keyword search results with In-Place Subscription/Unsubscription Confirmation."""
    matched_bots = [b for b in matched_bots if b.is_public]
    clean_q = html.escape(query.strip())
    cb_q = fit_search_query_for_callback(query)
    lines = [
        f"🔍 <b>'{clean_q}' 검색 결과 (총 {len(matched_bots)}건)</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    if confirming_bot_id:
        target_bot = next((b for b in BotServiceType if b.value == confirming_bot_id), None)
        target_name = html.escape(target_bot.short_name if target_bot else confirming_bot_id)
        lines.append(f"⚠️ <b>[{target_name}] 구독을 정말 해지하시겠습니까?</b>")
        lines.append("<i>아래 [🔴 해지 확정] 또는 [↩️ 취소]를 터치하세요.</i>\n")
    elif confirming_sub_bot_id:
        target_bot = next((b for b in BotServiceType if b.value == confirming_sub_bot_id), None)
        target_name = html.escape(target_bot.short_name if target_bot else confirming_sub_bot_id)
        lines.append(f"⚠️ <b>[{target_name}] 알림을 구독하시겠습니까?</b>")
        lines.append("<i>아래 [✅ 구독 확정] 또는 [↩️ 취소]를 터치하세요.</i>\n")

    if not matched_bots:
        lines.append(f"'{clean_q}'와(과) 일치하는 봇을 찾지 못했습니다. 😢\n")
        lines.append("💡 <i>추천 검색어: '환율', '시황', '여행', '특가', '키즈', '해열제'</i>")
    else:
        for i, b in enumerate(matched_bots, 1):
            is_sub = subs.get(b, False)
            status_chip = "🟢 [구독중]" if is_sub else "⚪ [미구독]"
            lines.append(f"<b>{i}. {b.icon} {html.escape(b.title)}</b> {status_chip}")
            lines.append(f"   • {html.escape(b.one_line_desc)}")

    lines.append("━━━━━━━━━━━━━━━━━━━━")

    keyboard = []
    for b in matched_bots:
        is_sub = subs.get(b, False)

        if confirming_bot_id == b.value:
            keyboard.append([
                {"text": "🔴 해지 확정", "callback_data": f"sub:do_unsub:{b.value}:search:{cb_q}"},
                {"text": "↩️ 취소", "callback_data": f"sub:cancel_unsub:search:{cb_q}"},
            ])
        elif confirming_sub_bot_id == b.value:
            keyboard.append([
                {"text": "✅ 구독 확정", "callback_data": f"sub:do_sub:{b.value}:search:{cb_q}"},
                {"text": "↩️ 취소", "callback_data": f"sub:cancel_sub:search:{cb_q}"},
            ])
        else:
            keyboard.append(_bot_action_row(b, is_sub, f"search:{cb_q}"))

    keyboard.extend([
        [
            {"text": "📂 카테고리", "callback_data": "cat:home"},
            {"text": "📋 구독 관리", "callback_data": "nav:subs"},
        ],
        [
            {"text": "🏠 홈으로", "callback_data": "nav:main"},
        ],
    ])

    return "\n".join(lines), {"inline_keyboard": keyboard}


USER_STATES: dict[int, str] = {}


def render_tier_benefits_view(user: UserProfile) -> tuple[str, dict[str, Any]]:
    """Renders tier level comparison table and upgrade guidance."""
    text = (
        f"💎 <b>멤버십 등급별 혜택 및 슬롯 안내</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"현재 고객님의 등급은 <b>{user.tier.display_name}</b> 입니다.\n\n"
        f"⚪ <b>프리 (FREE)</b>\n"
        f"• 구독 슬롯: <b>최대 1개</b>\n"
        f"• 기본 텍스트 브리핑 제공\n\n"
        f"🟢 <b>그린 (GREEN)</b>\n"
        f"• 구독 슬롯: <b>최대 3개</b>\n"
        f"• 정기 주간 리포트 및 키워드 검색 지원\n\n"
        f"🔴 <b>레드 (RED)</b>\n"
        f"• 구독 슬롯: <b>최대 5개</b>\n"
        f"• 장중 긴급 급변동 경보 & 항공 특가 즉시 푸시\n\n"
        f"⚫ <b>블랙 (BLACK VIP)</b>\n"
        f"• 구독 슬롯: <b>무제한 (All-Access)</b>\n"
        f"• 신규 베타 봇 우선 초대 & 1:1 전담 문의\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"💡 등급 변경 및 문의는 아래 버튼을 이용해 주세요."
    )

    keyboard = [
        [
            {"text": "💎 등급 업그레이드 신청", "callback_data": "support:upgrade_menu"},
        ],
        [
            {"text": "💬 고객센터", "callback_data": "support:home"},
            {"text": "📋 구독 관리", "callback_data": "nav:subs"},
        ],
        [
            {"text": "🏠 홈으로", "callback_data": "nav:main"},
        ],
    ]
    return text, {"inline_keyboard": keyboard}


def render_support_center_view(user: UserProfile) -> tuple[str, dict[str, Any]]:
    """Renders In-App Support & Customer Inquiry Hub."""
    name = html.escape(user.first_name or user.username or "회원")
    text = (
        f"💬 <b>고객센터 & 관리자 문의 창구</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"안녕하세요, <b>{name}</b>님! 무엇을 도와드릴까요?\n\n"
        f"• 💎 <b>등급 업그레이드 신청</b>: 원클릭으로 상향 접수\n"
        f"• ✉️ <b>1:1 문의사항 작성</b>: 운영팀에 직접 메시지 전송\n"
        f"• ❓ <b>자주 묻는 질문</b>: 이용 방법 및 FAQ\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"원하시는 메뉴를 선택해 주세요."
    )
    keyboard = [
        [
            {"text": "💎 등급 업그레이드 신청", "callback_data": "support:upgrade_menu"},
        ],
        [
            {"text": "✉️ 1:1 문의사항 작성", "callback_data": "support:write_inquiry"},
        ],
        [
            {"text": "❓ 이용안내", "callback_data": "nav:help"},
            {"text": "🏠 홈으로", "callback_data": "nav:main"},
        ],
    ]
    return text, {"inline_keyboard": keyboard}


_UPGRADE_OPTIONS: tuple[tuple[UserTier, str], ...] = (
    (UserTier.GREEN, "🟢 그린 등급 신청 (최대 3개)"),
    (UserTier.RED, "🔴 레드 등급 신청 (최대 5개)"),
    (UserTier.BLACK, "⚫ 블랙 VIP 신청 (무제한)"),
)


def render_upgrade_request_view(user: UserProfile) -> tuple[str, dict[str, Any]]:
    """Renders 1-click tier upgrade request form (only tiers strictly above the current one)."""
    current_rank = _TIER_RANK.get(user.tier, 0)
    if user.tier == UserTier.BLACK:
        guide = (
            "👑 <b>최고 등급(BLACK VIP)을 이용 중입니다.</b>\n"
            "더 이상 상향 가능한 등급이 없습니다. 문의는 고객센터를 이용해 주세요.\n"
        )
    else:
        guide = (
            "원하시는 희망 등급을 선택해 주세요.\n"
            "터치 즉시 운영팀에 신청서가 전송되며, 확인 후 신속히 승인해 드립니다!\n"
        )
    text = (
        f"💎 <b>등급 업그레이드 신청</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ 현재 이용 중인 등급: {user.tier.badge}\n\n"
        f"{guide}"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    keyboard = [
        [{"text": label, "callback_data": f"support:req_tier:{tier.value}"}]
        for tier, label in _UPGRADE_OPTIONS
        if _TIER_RANK[tier] > current_rank
    ]
    keyboard.extend([
        [
            {"text": "💬 고객센터", "callback_data": "support:home"},
            {"text": "🏠 홈으로", "callback_data": "nav:main"},
        ],
    ])
    return text, {"inline_keyboard": keyboard}


def render_inquiry_prompt_view(user: UserProfile) -> tuple[str, dict[str, Any]]:
    """Prompts user to type inquiry message."""
    text = (
        f"✉️ <b>1:1 문의사항 작성</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"운영팀에 전달하실 <b>문의 내용이나 제휴/건의사항</b>을\n"
        f"채팅창에 메시지로 자유롭게 입력해 주세요. ✍️\n\n"
        f"입력하신 즉시 운영팀 텔레그램으로 안전하게 전달됩니다.\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    keyboard = [
        [
            {"text": "↩️ 작성 취소", "callback_data": "support:home"},
        ]
    ]
    return text, {"inline_keyboard": keyboard}


def render_help_view() -> tuple[str, dict[str, Any]]:
    """Renders FAQ and help tips."""
    text = (
        f"❓ <b>포털 봇 이용 가이드</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Q. 원하는 봇을 가장 빠르게 찾는 방법은?</b>\n"
        f"A. 채팅창에 '환율', '여행', '키즈' 등 찾으시는 단어를 그냥 입력하시면 0.1초 만에 봇을 찾아드립니다!\n\n"
        f"<b>Q. 실수로 해지 버튼을 눌렀는데 바로 해지되나요?</b>\n"
        f"A. 아닙니다! 실수 방지를 위해 [해지 확정] / [취소] 버튼이 펼쳐지며, [취소]를 누르면 안전하게 구독이 유지됩니다.\n\n"
        f"<b>Q. 알림은 어디로 오나요?</b>\n"
        f"A. 구독을 켜시면 지정된 발송 시간에 각 전문 봇 채널을 통해 고품질 브리핑이 발송됩니다.\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    keyboard = [
        [
            {"text": "💬 고객센터", "callback_data": "support:home"},
            {"text": "📋 구독 관리", "callback_data": "nav:subs"},
        ],
        [
            {"text": "🏠 홈으로", "callback_data": "nav:main"},
        ],
    ]
    return text, {"inline_keyboard": keyboard}


# ============================================================
# Real-time Admin Alerts (New Signup / Tier Upgrade Request) — Fail-Open
# ============================================================

ADMIN_ALERT_MAX_VISIBLE = 1800
ADMIN_ALERT_MAX_HTML = 3500


def _get_admin_chat_id() -> str:
    """Resolves admin alert chat: PORTAL_ADMIN_CHAT_ID -> PORTAL_TELEGRAM_CHAT_ID -> TELEGRAM_CHAT_ID."""
    for key in ("PORTAL_ADMIN_CHAT_ID", "PORTAL_TELEGRAM_CHAT_ID", "TELEGRAM_CHAT_ID"):
        value = os.environ.get(key, "").strip()
        if value:
            return value
    return ""


def _get_admin_console_url(tab: str = "", req_id: Optional[int] = None) -> str:
    """Builds the admin console URL, optionally deep-linking to a tab and upgrade request card."""
    base = os.environ.get("ADMIN_CONSOLE_URL", "http://161.33.196.211:8002/").strip()
    params: dict[str, str] = {}
    if tab:
        params["tab"] = str(tab)
    if req_id is not None:
        params["req_id"] = str(int(req_id))
    if not params:
        return base
    parts = urlsplit(base)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query = [(k, v) for k, v in query if k not in params] + list(params.items())
    return urlunsplit((parts.scheme, parts.netloc, parts.path or "/", urlencode(query), parts.fragment))


def _admin_user_labels(user: UserProfile) -> tuple[str, str]:
    """Returns (escaped name, escaped @handle) truncated to keep alerts within [TG-01]."""
    name = html.escape((user.first_name or user.username or "회원")[:40])
    handle = html.escape(f"@{user.username[:40]}") if user.username else "핸들 없음"
    return name, handle


def _within_tg_limits(text: str) -> bool:
    visible = re.sub(r"<[^>]+>", "", text)
    return len(visible) <= ADMIN_ALERT_MAX_VISIBLE and len(text) <= ADMIN_ALERT_MAX_HTML


def send_admin_new_user_alert(
    client: TelegramPortalClient, user: UserProfile, source: str = "🧭 통합 안내 메뉴봇"
) -> bool:
    """Notifies the admin chat of a first-time signup. Never raises (Fail-Open)."""
    admin_chat = _get_admin_chat_id()
    if not admin_chat:
        return False
    try:
        name, handle = _admin_user_labels(user)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        text = (
            f"👋 <b>[신규 회원 가입 알림]</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"• 회원명: <b>{name}</b> ({handle})\n"
            f"• 유저 ID: <code>{html.escape(str(user.user_id))}</code>\n"
            f"• 기본 등급: {html.escape(user.tier.badge)}\n"
            f"• 유입 채널: {html.escape(source[:60])}\n"
            f"• 가입 일시: <code>{now_str}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        if not _within_tg_limits(text):
            logger.warning("New-user admin alert exceeds TG limits for %s; skipped", user.user_id)
            return False
        client.send_message(admin_chat, text)
        return True
    except Exception as err:
        logger.warning("Failed to send new-user admin alert for %s: %s", user.user_id, err)
        return False


def send_admin_tier_upgrade_alert(
    client: TelegramPortalClient,
    user: UserProfile,
    requested_tier: str,
    status_code: str,
    req_id: Optional[int] = None,
) -> bool:
    """Notifies the admin chat of a CREATED/CHANGED upgrade request. DUPLICATE is suppressed. Never raises."""
    if status_code not in ("CREATED", "CHANGED"):
        return False
    admin_chat = _get_admin_chat_id()
    if not admin_chat:
        return False
    try:
        name, handle = _admin_user_labels(user)
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        title = "💎 [회원 등업 신청 접수]" if status_code == "CREATED" else "🔄 [회원 등업 신청 변경]"
        text = (
            f"<b>{title}</b>\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"• 신청자: <b>{name}</b> ({handle})\n"
            f"• 유저 ID: <code>{html.escape(str(user.user_id))}</code>\n"
            f"• 현재 등급: {html.escape(user.tier.badge)}\n"
            f"• 희망 등급: <b>{html.escape(str(requested_tier)[:20])}</b>\n"
            f"• 접수 시각: <code>{now_str}</code>\n"
            f"━━━━━━━━━━━━━━━━━━━━"
        )
        if not _within_tg_limits(text):
            logger.warning("Upgrade admin alert exceeds TG limits for %s; skipped", user.user_id)
            return False
        markup = {"inline_keyboard": [[{"text": "🌐 관리자 콘솔 바로가기", "url": _get_admin_console_url(tab="users", req_id=req_id)}]]}
        client.send_message(admin_chat, text, reply_markup=markup)
        return True
    except Exception as err:
        logger.warning("Failed to send upgrade admin alert for %s: %s", user.user_id, err)
        return False


# ============================================================
# Callback Query Router (Instant Spinner Stop + In-Place Safety)
# ============================================================

def _is_portal_admin(user_id: Any, chat_id: Any) -> bool:
    """Checks whether the callback query sender is an authorized administrator."""
    uid = str(user_id).strip()
    cid = str(chat_id).strip()
    admin_chat = os.environ.get("PORTAL_ADMIN_CHAT_ID") or os.environ.get("PORTAL_TELEGRAM_CHAT_ID", "")
    admin_users_str = os.environ.get("PORTAL_ADMIN_USER_IDS", "")
    allowed_ids = {x.strip() for x in admin_users_str.split(",") if x.strip()}
    if admin_chat:
        allowed_ids.add(admin_chat.strip())
    # If no admin credentials set in env, restrict Telegram inline admin actions (Web Console only)
    if not allowed_ids:
        return False
    return uid in allowed_ids or cid in allowed_ids


def handle_callback_query(
    cq: dict[str, Any], db: PortalDatabase, client: TelegramPortalClient
) -> None:
    """Dispatches inline button clicks with zero lag and in-place confirmation.

    Single-ack ownership: several branches below may try to acknowledge; inside the scope only the FIRST
    `answer_callback_query` reaches Telegram (joins the manager's scope when dispatched by the manager)."""
    with callback_ack_scope(cq.get("id", "")):
        _handle_callback_query_scoped(cq, db, AckOnceClient(client))


def _handle_callback_query_scoped(
    cq: dict[str, Any], db: PortalDatabase, client: TelegramPortalClient
) -> None:
    cq_id = cq["id"]
    from_user = cq.get("from") or {}
    user_id = from_user.get("id")
    msg = cq.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id") or user_id
    message_id = msg.get("message_id")
    data = cq.get("data", "")

    # 0. Hidden (WIP) bot guard: MUST run before any silent ACK — AckOnceClient delivers only the first
    #    answer, so a prior silent ACK would swallow this show_alert. No DB mutation on block.
    if _hidden_new_sub_target(data) is not None:
        client.answer_callback_query(cq_id, HIDDEN_BOT_ALERT, show_alert=True)
        return

    # 1. Stop button loading spinner immediately (except modal alerts: sub:toggle, adm, sub:do_sub, sub:do_unsub, sub:swap, support:req_tier)
    MODAL_ACTIONS = (
        "sub:toggle:", "adm:", "sub:do_sub:", "sub:do_unsub:", "sub:swap:", "support:req_tier:",
        "sub:locked:", "gate:",
    )
    if not any(data.startswith(prefix) for prefix in MODAL_ACTIONS):
        client.answer_callback_query(cq_id)

    # 0-A. Locked chat-room button of an un-subscribed bot: modal alert only (no URL exposure)
    if data.startswith("sub:locked:"):
        client.answer_callback_query(cq_id, SUB_LOCKED_ALERT, show_alert=True)
        return

    # 0-B. Link Confirmation Gate Intercept (answered here: modal alert when the target bot is locked)
    if data.startswith("gate:"):
        from market_brief.link_gate import LinkGateRouter, GATE_REGISTRY
        user = db.get_or_create_user(
            user_id=user_id,
            chat_id=chat_id,
            username=from_user.get("username"),
            first_name=from_user.get("first_name"),
            last_name=from_user.get("last_name"),
        )
        subs = db.get_user_subscriptions(user_id)
        target_bot = _gate_target_bot(data)
        if target_bot is not None and not subs.get(target_bot, False):
            # Stale `🚀 대화방` gate button for a bot the user no longer subscribes to -> locked menu card
            client.answer_callback_query(cq_id, SUB_LOCKED_ALERT, show_alert=True)
            text, markup = render_subscription_view(user, subs)
            _safe_edit_or_send(client, chat_id, message_id, text, markup)
            return
        client.answer_callback_query(cq_id)
        gate_router = LinkGateRouter(GATE_REGISTRY)
        gate_res = gate_router.handle_callback(data, "menu_bot", str(chat_id), int(message_id or 0))
        if gate_res.get("handled"):
            if gate_res.get("action") == "CONFIRM_CARD":
                _safe_edit_or_send(client, chat_id, message_id, gate_res["html"], {"inline_keyboard": gate_res["inline_keyboard"]})
                return
            # CANCEL / EXPIRED: back to the subscription card (locked bots carry no room link)
            text, markup = render_subscription_view(user, subs)
            _safe_edit_or_send(client, chat_id, message_id, text, markup)
            return
        return

    if data == "noop":
        return

    if data.startswith("sub:toggle:"):
        bot_raw = data.split(":", 2)[2]
        try:
            bot_type = BotServiceType(bot_raw)
        except ValueError:
            client.answer_callback_query(cq_id)
            return

        # Toggle on a hidden bot is allowed only as an unsubscribe of an existing subscription
        if not bot_type.is_public and not db.get_user_subscriptions(user_id).get(bot_type, False):
            client.answer_callback_query(cq_id, HIDDEN_BOT_ALERT, show_alert=True)
            return

        ok, is_active, msg = db.toggle_subscription(user_id, bot_type)
        client.answer_callback_query(cq_id, msg, show_alert=not ok)
        if ok:
            _sync_domain_alerts(db, user_id, bot_type.value, active=is_active)
        return

    user = db.get_or_create_user(
        user_id=user_id,
        chat_id=chat_id,
        username=from_user.get("username"),
        first_name=from_user.get("first_name"),
        last_name=from_user.get("last_name"),
    )
    if getattr(user, "is_new", False) is True:
        send_admin_new_user_alert(client, user)

    # 2. Main Navigation (with full backwards compatibility for menu:home, menu:my_subs, menu:tiers)
    if data in ("nav:main", "menu:home", "home"):
        subs = db.get_user_subscriptions(user_id)
        text, markup = render_main_menu(user, subs)
        _safe_edit_or_send(client, chat_id, message_id, text, markup)
        return

    if data in ("nav:subs", "menu:my_subs"):
        subs = db.get_user_subscriptions(user_id)
        text, markup = render_subscription_view(user, subs)
        _safe_edit_or_send(client, chat_id, message_id, text, markup)
        return

    if data in ("nav:tiers", "menu:tiers"):
        text, markup = render_tier_benefits_view(user)
        _safe_edit_or_send(client, chat_id, message_id, text, markup)
        return

    if data in ("cat:home", "menu:cats", "menu:categories"):
        text, markup = render_category_home_view()
        _safe_edit_or_send(client, chat_id, message_id, text, markup)
        return

    if data in ("cat:all", "nav:catalog"):
        subs = db.get_user_subscriptions(user_id)
        text, markup = render_catalog_all_view(user, subs)
        _safe_edit_or_send(client, chat_id, message_id, text, markup)
        return

    # Compatibility: menu:bot:<bot_id>
    if data.startswith("menu:bot:"):
        bot_raw = data.split(":", 2)[2]
        try:
            bot_type = BotServiceType(bot_raw)
            category = bot_type.category
            subs = db.get_user_subscriptions(user_id)
            text, markup = render_category_view(category, user, subs)
        except ValueError:
            text, markup = render_main_menu(user)
        _safe_edit_or_send(client, chat_id, message_id, text, markup)
        return

    # Compatibility: menu:sub:<bot_id> & info:sub:<bot_id>
    if data.startswith("menu:sub:") or data.startswith("info:sub:"):
        bot_raw = data.split(":", 2)[2]
        try:
            bot_type = BotServiceType(bot_raw)
        except ValueError:
            text, markup = render_main_menu(user)
            _safe_edit_or_send(client, chat_id, message_id, text, markup)
            return

        ok, msg = db.subscribe(user_id, bot_type)
        if not ok:
            quota_markup = {
                "inline_keyboard": [
                    [
                        {"text": "💎 업그레이드", "callback_data": "support:upgrade_menu"},
                        {"text": "📋 구독 관리", "callback_data": "nav:subs"},
                    ]
                ]
            }
            client.send_message(chat_id, msg, reply_markup=quota_markup)
            return

        _sync_domain_alerts(db, user_id, bot_type.value, active=True)
        text, markup = render_subscribe_onboarding_view(bot_type, f"cat:{bot_type.category.value}")
        _safe_edit_or_send(client, chat_id, message_id, text, markup)
        return

    # Compatibility: menu:unsub:<bot_id> & info:unsub:<bot_id>
    if data.startswith("menu:unsub:") or data.startswith("info:unsub:"):
        bot_raw = data.split(":", 2)[2]
        try:
            bot_type = BotServiceType(bot_raw)
        except ValueError:
            text, markup = render_main_menu(user)
            _safe_edit_or_send(client, chat_id, message_id, text, markup)
            return

        ok, msg = db.unsubscribe(user_id, bot_type)
        if ok:
            _sync_domain_alerts(db, user_id, bot_type.value, active=False)
        subs = db.get_user_subscriptions(user_id)
        text, markup = render_category_view(bot_type.category, user, subs)
        _safe_edit_or_send(client, chat_id, message_id, text, markup)
        return

    # Compatibility: menu:swap:<cancel_id>:<new_id> & info:swap:<cancel_id>:<new_id>
    if data.startswith("menu:swap:") or data.startswith("info:swap:"):
        parts = data.split(":")
        if len(parts) == 4:
            cancel_raw, new_raw = parts[2], parts[3]
            try:
                cancel_type = BotServiceType(cancel_raw)
                new_type = BotServiceType(new_raw)
                db.unsubscribe(user_id, cancel_type)
                sub_ok, _ = db.subscribe(user_id, new_type)
                if not sub_ok:
                    db.subscribe(user_id, cancel_type)  # rollback if new sub failed
                else:
                    _sync_domain_alerts(db, user_id, cancel_type.value, active=False)
                    _sync_domain_alerts(db, user_id, new_type.value, active=True)
            except ValueError:
                pass
        subs = db.get_user_subscriptions(user_id)
        text, markup = render_subscription_view(user, subs)
        _safe_edit_or_send(client, chat_id, message_id, text, markup)
        return

    if data == "nav:help":
        text, markup = render_help_view()
        _safe_edit_or_send(client, chat_id, message_id, text, markup)
        return

    # Support Center & Inquiry Actions
    if data == "support:home":
        USER_STATES.pop(user_id, None)
        text, markup = render_support_center_view(user)
        _safe_edit_or_send(client, chat_id, message_id, text, markup)
        return

    if data == "support:upgrade_menu":
        USER_STATES.pop(user_id, None)
        text, markup = render_upgrade_request_view(user)
        _safe_edit_or_send(client, chat_id, message_id, text, markup)
        return

    if data == "support:write_inquiry":
        USER_STATES[user_id] = "AWAITING_INQUIRY"
        text, markup = render_inquiry_prompt_view(user)
        _safe_edit_or_send(client, chat_id, message_id, text, markup)
        return

    if data.startswith("support:req_tier:"):
        USER_STATES.pop(user_id, None)
        raw_tier = data.split(":", 2)[2].upper()
        if raw_tier not in ("GREEN", "RED", "BLACK"):
            client.answer_callback_query(cq_id, "유효하지 않은 등급입니다.", show_alert=True)
            return

        # Stale/forged lower-or-same tier request: alert only, never touch the DB
        if _TIER_RANK[UserTier(raw_tier)] <= _TIER_RANK.get(user.tier, 0):
            client.answer_callback_query(cq_id, "ℹ️ 이미 이용 중이거나 하위 등급입니다.", show_alert=True)
            return

        req_tier = raw_tier
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1. Store upgrade request in alerts.sqlite3 database (viewed and approved in Admin Web Console)
        try:
            req_id, status_code = db.request_tier_upgrade(user_id, req_tier)
        except ValueError as ve:
            client.answer_callback_query(cq_id, str(ve), show_alert=True)
            return
        except Exception as e:
            logger.warning("Failed to store upgrade request for %s: %s", user_id, e)
            client.answer_callback_query(cq_id, "신청 처리 중 오류가 발생했습니다.", show_alert=True)
            return

        # 2. Reply to user with modal popup + clean in-place notice
        if status_code == "DUPLICATE":
            client.answer_callback_query(
                cq_id, f"ℹ️ 이미 [{req_tier}] 등급 신청이 접수되어 심사 대기 중입니다.", show_alert=True
            )
            done_text = (
                f"🫡 <b>이미 신청이 접수되어 운영팀 심사 대기 중입니다!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"• 희망 등급: <b>{html.escape(req_tier)}</b>\n\n"
                f"관리자 승인 즉시 봇을 통해 승인 완료 알림을 보내드리겠습니다.\n"
                f"━━━━━━━━━━━━━━━━━━━━"
            )
        else:
            client.answer_callback_query(
                cq_id, f"🎉 [{req_tier}] 등급 업그레이드 신청이 접수되었습니다!", show_alert=True
            )
            done_text = (
                f"🎉 <b>등급 업그레이드 신청 완료!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"• 희망 등급: <b>{html.escape(req_tier)}</b>\n"
                f"• 접수 일시: <code>{now_str}</code>\n\n"
                f"운영팀 관리자 페이지로 신청서가 안전하게 접수되었습니다. 🫡\n"
                f"관리자 승인 즉시 봇을 통해 승인 완료 알림을 보내드리겠습니다.\n"
                f"━━━━━━━━━━━━━━━━━━━━"
            )
        done_markup = {
            "inline_keyboard": [
                [
                    {"text": "📋 구독 관리", "callback_data": "nav:subs"},
                    {"text": "🏠 홈으로", "callback_data": "nav:main"},
                ]
            ]
        }
        _safe_edit_or_send(client, chat_id, message_id, done_text, done_markup)

        # 3. Notify admin after the user's screen is updated (DUPLICATE suppressed, Fail-Open)
        send_admin_tier_upgrade_alert(client, user, req_tier, status_code, req_id=req_id)
        return

    # Legacy Telegram admin button handler: defuse and direct to Admin Web Console
    if data.startswith("adm:"):
        client.answer_callback_query(
            cq_id, "등급 업그레이드 심사는 관리자 웹 콘솔(http://127.0.0.1:8002)에서 처리됩니다.", show_alert=True
        )
        _safe_edit_or_send(
            client,
            chat_id,
            message_id,
            "ℹ️ <b>[안내]</b> 등급 업그레이드 심사는 <b>관리자 웹 콘솔</b>(http://127.0.0.1:8002)에서만 진행됩니다.",
            {"inline_keyboard": []},
        )
        return

    # 3. Category Views
    if data.startswith("cat:view:"):
        cat_raw = data.split(":", 2)[2]
        try:
            category = BotCategory(cat_raw)
        except ValueError:
            category = BotCategory.FINANCE
        subs = db.get_user_subscriptions(user_id)
        text, markup = render_category_view(category, user, subs)
        _safe_edit_or_send(client, chat_id, message_id, text, markup)
        return

    # 4-A. In-Place Accidental Click Prevention - Step 1 for Subscribe: Expand Confirmation Buttons
    # Format: `sub:ask_sub:<bot_id>:<context>`
    if data.startswith("sub:ask_sub:"):
        parts = data.split(":")
        bot_raw = parts[2]
        context_str = ":".join(parts[3:]) if len(parts) > 3 else "subs"

        subs = db.get_user_subscriptions(user_id)
        if context_str.startswith("cat:"):
            cat_code = context_str.split(":", 1)[1]
            try:
                cat_enum = BotCategory(cat_code)
            except ValueError:
                cat_enum = BotCategory.FINANCE
            text, markup = render_category_view(cat_enum, user, subs, confirming_sub_bot_id=bot_raw)
        elif context_str.startswith("search:"):
            query = context_str.split(":", 1)[1]
            matched = db.search_bots(query)
            text, markup = render_search_results_view(query, matched, user, subs, confirming_sub_bot_id=bot_raw)
        elif context_str == "all":
            text, markup = render_catalog_all_view(user, subs, confirming_sub_bot_id=bot_raw)
        else:
            text, markup = render_subscription_view(user, subs, confirming_sub_bot_id=bot_raw)

        _safe_edit_or_send(client, chat_id, message_id, text, markup)
        return

    # 4-B. In-Place Accidental Click Prevention - Cancel Subscribe
    # Format: `sub:cancel_sub:<context>`
    if data.startswith("sub:cancel_sub:"):
        context_str = data.split(":", 2)[2] if len(data.split(":")) > 2 else "subs"
        subs = db.get_user_subscriptions(user_id)
        if context_str.startswith("cat:"):
            cat_code = context_str.split(":", 1)[1]
            try:
                cat_enum = BotCategory(cat_code)
            except ValueError:
                cat_enum = BotCategory.FINANCE
            text, markup = render_category_view(cat_enum, user, subs)
        elif context_str.startswith("search:"):
            query = context_str.split(":", 1)[1]
            matched = db.search_bots(query)
            text, markup = render_search_results_view(query, matched, user, subs)
        elif context_str == "all":
            text, markup = render_catalog_all_view(user, subs)
        else:
            text, markup = render_subscription_view(user, subs)

        _safe_edit_or_send(client, chat_id, message_id, text, markup)
        return

    # 4-C. In-Place Accidental Click Prevention - Step 1 for Unsubscribe: Expand Confirmation Buttons
    # Format: `sub:ask_unsub:<bot_id>:<context>`
    if data.startswith("sub:ask_unsub:"):
        parts = data.split(":")
        bot_raw = parts[2]
        context_str = ":".join(parts[3:]) if len(parts) > 3 else "subs"

        hidden_bot = next((b for b in BotServiceType if b.value == bot_raw and not b.is_public), None)
        if hidden_bot is not None:
            # Stale unsubscribe of a hidden bot: public views no longer list it, so show a standalone card
            text, markup = render_hidden_unsub_confirm_view(hidden_bot, context_str)
            _safe_edit_or_send(client, chat_id, message_id, text, markup)
            return

        subs = db.get_user_subscriptions(user_id)
        if context_str.startswith("cat:"):
            cat_code = context_str.split(":", 1)[1]
            try:
                cat_enum = BotCategory(cat_code)
            except ValueError:
                cat_enum = BotCategory.FINANCE
            text, markup = render_category_view(cat_enum, user, subs, confirming_bot_id=bot_raw)
        elif context_str.startswith("search:"):
            query = context_str.split(":", 1)[1]
            matched = db.search_bots(query)
            text, markup = render_search_results_view(query, matched, user, subs, confirming_bot_id=bot_raw)
        elif context_str == "all":
            text, markup = render_catalog_all_view(user, subs, confirming_bot_id=bot_raw)
        else:
            text, markup = render_subscription_view(user, subs, confirming_bot_id=bot_raw)

        _safe_edit_or_send(client, chat_id, message_id, text, markup)
        return

    # 5. In-Place Accidental Click Prevention - Cancel Unsubscribe
    # Format: `sub:cancel_unsub:<context>`
    if data.startswith("sub:cancel_unsub:"):
        context_str = data.split(":", 2)[2] if len(data.split(":")) > 2 else "subs"
        subs = db.get_user_subscriptions(user_id)
        if context_str.startswith("cat:"):
            cat_code = context_str.split(":", 1)[1]
            try:
                cat_enum = BotCategory(cat_code)
            except ValueError:
                cat_enum = BotCategory.FINANCE
            text, markup = render_category_view(cat_enum, user, subs)
        elif context_str.startswith("search:"):
            query = context_str.split(":", 1)[1]
            matched = db.search_bots(query)
            text, markup = render_search_results_view(query, matched, user, subs)
        elif context_str == "all":
            text, markup = render_catalog_all_view(user, subs)
        else:
            text, markup = render_subscription_view(user, subs)

        _safe_edit_or_send(client, chat_id, message_id, text, markup)
        return

    # 6. In-Place Accidental Click Prevention - Step 2: Final Execute Unsubscribe
    # Format: `sub:do_unsub:<bot_id>:<context>`
    if data.startswith("sub:do_unsub:"):
        parts = data.split(":")
        bot_raw = parts[2]
        context_str = ":".join(parts[3:]) if len(parts) > 3 else "subs"
        try:
            bot_type = BotServiceType(bot_raw)
        except ValueError:
            client.answer_callback_query(cq_id)
            return

        ok, msg = db.unsubscribe(user_id, bot_type)
        if ok:
            _sync_domain_alerts(db, user_id, bot_type.value, active=False)
        subs = db.get_user_subscriptions(user_id)

        client.answer_callback_query(
            cq_id,
            f"⚪ [{bot_type.short_name}] 구독이 정상적으로 해지되었습니다.",
            show_alert=True,
        )

        if context_str.startswith("cat:"):
            cat_code = context_str.split(":", 1)[1]
            try:
                cat_enum = BotCategory(cat_code)
            except ValueError:
                cat_enum = bot_type.category
            text, markup = render_category_view(cat_enum, user, subs)
        elif context_str.startswith("search:"):
            query = context_str.split(":", 1)[1]
            matched = db.search_bots(query)
            text, markup = render_search_results_view(query, matched, user, subs)
        elif context_str == "all":
            text, markup = render_catalog_all_view(user, subs)
        else:
            text, markup = render_subscription_view(user, subs)

        _safe_edit_or_send(client, chat_id, message_id, text, markup)
        return

    # 7. Execute Subscribe
    # Format: `sub:do_sub:<bot_id>:<context>`
    if data.startswith("sub:do_sub:"):
        parts = data.split(":")
        bot_raw = parts[2]
        context_str = ":".join(parts[3:]) if len(parts) > 3 else "subs"
        try:
            bot_type = BotServiceType(bot_raw)
        except ValueError:
            client.answer_callback_query(cq_id)
            return

        ok, msg = db.subscribe(user_id, bot_type)
        if not ok:
            client.answer_callback_query(
                cq_id,
                "⚠️ 구독 슬롯 초과!\n기존 구독을 교체하거나 등급을 업그레이드해 주세요.",
                show_alert=True,
            )
            # Over quota prompt with 1-click Swap / Upgrade / Back buttons
            subs = db.get_user_subscriptions(user_id)
            swappable_bots = [b for b in BotServiceType if subs.get(b, False) and b != bot_type]
            text, markup = render_quota_swap_view(bot_type, swappable_bots, msg, context_str)
            client.send_message(chat_id, text, reply_markup=markup)
            return

        _sync_domain_alerts(db, user_id, bot_type.value, active=True)
        client.answer_callback_query(
            cq_id,
            f"🎉 [{bot_type.short_name}] 알림 구독이 완료되었습니다!",
            show_alert=True,
        )

        text, markup = render_subscribe_onboarding_view(bot_type, context_str)
        _safe_edit_or_send(client, chat_id, message_id, text, markup)
        return

    # 8. 1-Click Atomic Swap (over-quota)
    # Format: `sub:swap:<old_bot_id>:<new_bot_id>:<context>`
    if data.startswith("sub:swap:"):
        parts = data.split(":")
        if len(parts) < 4:
            client.answer_callback_query(cq_id)
            return
        context_str = ":".join(parts[4:]) if len(parts) > 4 else "subs"
        try:
            old_bot = BotServiceType(parts[2])
            new_bot = BotServiceType(parts[3])
        except ValueError:
            client.answer_callback_query(cq_id)
            return

        ok, msg = db.atomic_swap_subscription(user_id, old_bot, new_bot)
        if ok:
            _sync_domain_alerts(db, user_id, old_bot.value, active=False)
            _sync_domain_alerts(db, user_id, new_bot.value, active=True)
            client.answer_callback_query(
                cq_id,
                f"✅ [{old_bot.short_name}] ➔ [{new_bot.short_name}] 교체가 완료되었습니다!",
                show_alert=True,
            )
        else:
            client.answer_callback_query(
                cq_id,
                f"⚠️ 교체 실패: {msg}",
                show_alert=True,
            )

        text, markup = render_swap_result_view(ok, msg, new_bot, context_str)
        _safe_edit_or_send(client, chat_id, message_id, text, markup)
        return
