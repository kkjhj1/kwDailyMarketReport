"""Interactive UI Renderer & State Handler for Portal Hub Bot (v2.2).
Equipped with:
1. In-Place Accidental Click Prevention (Inline 2-Step Confirmation without losing context).
2. Category-based 2-Tap Discovery (Finance, Edu, Life, Biz).
3. Instant Keyword Search Engine.
4. Immediate Callback Acknowledgement to eliminate button spinner lag.
"""
from __future__ import annotations
import html
import logging
import os
from typing import Any, Optional

from portal_hub.db import PortalDatabase
from portal_hub.models import BotCategory, BotServiceType, UserProfile, UserTier
from portal_hub.telegram import TelegramPortalClient

logger = logging.getLogger(__name__)


def build_persistent_reply_keyboard() -> dict[str, Any]:
    """Builds persistent 2x2 bottom touch buttons pinned to mobile Telegram screen."""
    return {
        "keyboard": [
            [
                {"text": "📋 내 구독 관리"},
                {"text": "📂 카테고리 둘러보기"},
            ],
            [
                {"text": "💎 내 등급 & 혜택"},
                {"text": "❓ 이용 도움말"},
            ],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


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


# ============================================================
# Screen Renderers
# ============================================================

def render_main_menu(user: UserProfile) -> tuple[str, dict[str, Any]]:
    """Renders Welcome & Main Hub Dashboard with 4-Category Grid & Popular Top 3."""
    name = html.escape(user.first_name or user.username or "회원")
    text = (
        f"👋 안녕하세요, <b>{name}</b>님!\n"
        f"<b>통합 인텔리전스 허브(Hub)</b>에 오신 것을 환영합니다.\n\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏅 <b>나의 회원 등급</b>: {user.tier.badge}\n"
        f"📦 <b>구독 가능 슬롯</b>: <b>{user.tier.max_slots if user.tier != UserTier.BLACK else '무제한'}개</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n\n"
        f"🔥 <b>[이번 주 인기 봇 Top 3]</b>\n"
        f"• 📈 <b>시황 브리핑</b>: 매일 06:30 글로벌 증시/환율\n"
        f"• 🎓 <b>입시 나침반</b>: 주간 수험생 To-Do & 설명회\n"
        f"• ✈️ <b>여행 특가</b>: 주간 최저가 항공권 딜\n\n"
        f"💡 <i>관심 카테고리를 터치하거나, 찾으시는 단어(예: '환율', '수시')를 채팅창에 바로 입력하세요!</i>"
    )

    markup = {
        "inline_keyboard": [
            [
                {"text": "💰 금융 / 재테크", "callback_data": "cat:view:FINANCE"},
                {"text": "🎓 교육 / 입시", "callback_data": "cat:view:EDU"},
            ],
            [
                {"text": "✈️ 라이프 / 여가", "callback_data": "cat:view:LIFE"},
                {"text": "💼 업무 / 생산성", "callback_data": "cat:view:BIZ"},
            ],
            [
                {"text": "📋 내 구독 센터 (원클릭 설정)", "callback_data": "nav:subs"},
            ],
            [
                {"text": "💎 내 등급 & 혜택", "callback_data": "nav:tiers"},
                {"text": "❓ 이용 도움말", "callback_data": "nav:help"},
            ],
        ]
    }
    return text, markup


def render_subscription_view(
    user: UserProfile, subs: dict[BotServiceType, bool], confirming_bot_id: Optional[str] = None
) -> tuple[str, dict[str, Any]]:
    """Renders Subscription Dashboard with In-Place Unsubscribe Confirmation."""
    name = html.escape(user.first_name or user.username or "회원")
    active_count = sum(1 for v in subs.values() if v)
    gauge_display = build_slot_gauge(active_count, user.tier)

    notice_part = ""
    if confirming_bot_id:
        target_bot = next((b for b in BotServiceType if b.value == confirming_bot_id), None)
        target_name = target_bot.short_name if target_bot else confirming_bot_id
        notice_part = (
            f"\n⚠️ <b>[{target_name}] 구독을 정말 해지하시겠습니까?</b>\n"
            f"<i>아래의 [🔴 해지 확정] 또는 [🔙 취소]를 선택해 주세요.</i>\n"
        )

    text = (
        f"📋 <b>[{name}] 님의 구독 센터</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"🏅 <b>회원 등급</b>: {user.tier.badge}\n"
        f"📊 <b>구독 슬롯</b>: <b>{gauge_display}</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━"
        f"{notice_part}\n"
        f"💡 <i>버튼을 누르면 즉시 구독 신청되거나 해지 확인 단계가 진행됩니다.</i>"
    )

    keyboard = []
    for bot in BotServiceType:
        is_subbed = subs.get(bot, False)

        if confirming_bot_id == bot.value:
            # Expanded In-Place Confirmation Buttons
            keyboard.append([
                {"text": f"⚠️ [{bot.short_name}] 해지 재확인", "callback_data": "noop"},
            ])
            keyboard.append([
                {"text": "🔴 네, 해지 확정", "callback_data": f"sub:do_unsub:{bot.value}:subs"},
                {"text": "🔙 취소 (구독 유지)", "callback_data": f"sub:cancel_unsub:subs"},
            ])
        else:
            if is_subbed:
                btn_text = f"🟢 구독중  │  {bot.icon} {bot.short_name}"
                keyboard.append([{"text": btn_text, "callback_data": f"sub:ask_unsub:{bot.value}:subs"}])
            else:
                btn_text = f"⚪ 미구독  │  {bot.icon} {bot.short_name}"
                keyboard.append([{"text": btn_text, "callback_data": f"sub:do_sub:{bot.value}:subs"}])

    keyboard.extend([
        [
            {"text": "📂 카테고리 둘러보기", "callback_data": "cat:home"},
            {"text": "💎 등급 혜택", "callback_data": "nav:tiers"},
        ],
        [
            {"text": "🏠 메인 홈으로", "callback_data": "nav:main"},
        ],
    ])

    return text, {"inline_keyboard": keyboard}


def render_category_view(
    category: BotCategory,
    user: UserProfile,
    subs: dict[BotServiceType, bool],
    confirming_bot_id: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
    """Renders 3-item Compact Chip View for selected category with In-Place Unsubscribe Confirmation."""
    bots = [b for b in BotServiceType if b.category == category]

    lines = [
        f"<b>{html.escape(category.title)} 봇 카탈로그</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    if confirming_bot_id:
        target_bot = next((b for b in BotServiceType if b.value == confirming_bot_id), None)
        target_name = target_bot.short_name if target_bot else confirming_bot_id
        lines.append(f"⚠️ <b>[{target_name}] 구독을 정말 해지하시겠습니까?</b>")
        lines.append(f"<i>아래 [🔴 해지 확정] 또는 [🔙 취소]를 터치하세요.</i>\n")

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
        deep_link_url = f"https://t.me/{b.telegram_handle}?start=portal"

        if confirming_bot_id == b.value:
            keyboard.append([
                {"text": "🔴 네, 해지 확정", "callback_data": f"sub:do_unsub:{b.value}:cat:{category.value}"},
                {"text": "🔙 취소 (구독 유지)", "callback_data": f"sub:cancel_unsub:cat:{category.value}"},
            ])
        else:
            if is_sub:
                action_btn = {"text": f"🟢 구독중 (해지)", "callback_data": f"sub:ask_unsub:{b.value}:cat:{category.value}"}
            else:
                action_btn = {"text": f"➕ 구독하기", "callback_data": f"sub:do_sub:{b.value}:cat:{category.value}"}

            keyboard.append([
                action_btn,
                {"text": f"{b.short_name} 이동 🔗", "url": deep_link_url},
            ])

    keyboard.extend([
        [
            {"text": "📂 다른 카테고리", "callback_data": "cat:home"},
            {"text": "📋 내 구독 센터", "callback_data": "nav:subs"},
        ],
        [
            {"text": "🏠 메인 홈으로", "callback_data": "nav:main"},
        ],
    ])

    return "\n".join(lines), {"inline_keyboard": keyboard}


def render_category_home_view() -> tuple[str, dict[str, Any]]:
    """Renders all categories overview."""
    text = (
        f"📂 <b>전체 카테고리 탐색</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"원하시는 분야를 선택하시면 세부 봇 목록을 확인하실 수 있습니다.\n\n"
        f"• 💰 <b>금융 / 재테크</b>: 글로벌 시황, 증시, 환율 등\n"
        f"• 🎓 <b>교육 / 입시</b>: 대입/수시 To-Do, 설명회 요약 등\n"
        f"• ✈️ <b>라이프 / 여가</b>: 주간 특가 항공권, 여행지 큐레이션 등\n"
        f"• 💼 <b>업무 / 생산성</b>: 뉴스 클리핑, 생산성 도구 등 (준비중)\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    keyboard = [
        [
            {"text": "💰 금융 / 재테크", "callback_data": "cat:view:FINANCE"},
            {"text": "🎓 교육 / 입시", "callback_data": "cat:view:EDU"},
        ],
        [
            {"text": "✈️ 라이프 / 여가", "callback_data": "cat:view:LIFE"},
            {"text": "💼 업무 / 생산성", "callback_data": "cat:view:BIZ"},
        ],
        [
            {"text": "🏠 메인 홈으로", "callback_data": "nav:main"},
        ],
    ]
    return text, {"inline_keyboard": keyboard}


def render_search_results_view(
    query: str,
    matched_bots: list[BotServiceType],
    user: UserProfile,
    subs: dict[BotServiceType, bool],
    confirming_bot_id: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
    """Renders instant keyword search results with In-Place Unsubscribe Confirmation."""
    clean_q = html.escape(query.strip())
    lines = [
        f"🔍 <b>'{clean_q}' 검색 결과 (총 {len(matched_bots)}건)</b>",
        "━━━━━━━━━━━━━━━━━━━━",
    ]

    if confirming_bot_id:
        target_bot = next((b for b in BotServiceType if b.value == confirming_bot_id), None)
        target_name = target_bot.short_name if target_bot else confirming_bot_id
        lines.append(f"⚠️ <b>[{target_name}] 구독을 정말 해지하시겠습니까?</b>")
        lines.append(f"<i>아래 [🔴 해지 확정] 또는 [🔙 취소]를 터치하세요.</i>\n")

    if not matched_bots:
        lines.append(f"'{clean_q}'와(과) 일치하는 봇을 찾지 못했습니다. 😢\n")
        lines.append("💡 <i>추천 검색어: '환율', '시황', '입시', '수능', '여행', '특가'</i>")
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
        deep_link_url = f"https://t.me/{b.telegram_handle}?start=portal"

        if confirming_bot_id == b.value:
            keyboard.append([
                {"text": "🔴 네, 해지 확정", "callback_data": f"sub:do_unsub:{b.value}:search:{clean_q}"},
                {"text": "🔙 취소 (구독 유지)", "callback_data": f"sub:cancel_unsub:search:{clean_q}"},
            ])
        else:
            if is_sub:
                action_btn = {"text": f"🟢 구독중 (해지)", "callback_data": f"sub:ask_unsub:{b.value}:search:{clean_q}"}
            else:
                action_btn = {"text": f"➕ 구독하기", "callback_data": f"sub:do_sub:{b.value}:search:{clean_q}"}

            keyboard.append([
                action_btn,
                {"text": f"{b.short_name} 이동 🔗", "url": deep_link_url},
            ])

    keyboard.extend([
        [
            {"text": "📂 카테고리 둘러보기", "callback_data": "cat:home"},
            {"text": "📋 내 구독 센터", "callback_data": "nav:subs"},
        ],
        [
            {"text": "🏠 메인 홈으로", "callback_data": "nav:main"},
        ],
    ])

    return "\n".join(lines), {"inline_keyboard": keyboard}


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
        f"💡 등급 변경은 관리자에게 문의해 주세요."
    )

    admin_handle = os.environ.get("ADMIN_TELEGRAM_HANDLE", "admin")
    keyboard = [
        [
            {"text": "💬 관리자 문의하기", "url": f"https://t.me/{admin_handle}"},
        ],
        [
            {"text": "📋 내 구독 센터", "callback_data": "nav:subs"},
            {"text": "🏠 메인 홈으로", "callback_data": "nav:main"},
        ],
    ]
    return text, {"inline_keyboard": keyboard}


def render_help_view() -> tuple[str, dict[str, Any]]:
    """Renders FAQ and help tips."""
    text = (
        f"❓ <b>포털 봇 이용 가이드</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Q. 원하는 봇을 가장 빠르게 찾는 방법은?</b>\n"
        f"A. 채팅창에 '환율', '수능', '항공' 등 찾으시는 단어를 그냥 입력하시면 0.1초 만에 봇을 찾아드립니다!\n\n"
        f"<b>Q. 실수로 해지 버튼을 눌렀는데 바로 해지되나요?</b>\n"
        f"A. 아닙니다! 실수 방지를 위해 [해지 확정] / [취소] 버튼이 펼쳐지며, [취소]를 누르면 안전하게 구독이 유지됩니다.\n\n"
        f"<b>Q. 알림은 어디로 오나요?</b>\n"
        f"A. 구독을 켜시면 지정된 발송 시간에 각 전문 봇 채널을 통해 고품질 브리핑이 발송됩니다.\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    keyboard = [
        [
            {"text": "📋 내 구독 센터", "callback_data": "nav:subs"},
            {"text": "🏠 메인 홈으로", "callback_data": "nav:main"},
        ]
    ]
    return text, {"inline_keyboard": keyboard}


# ============================================================
# Callback Query Router (Instant Spinner Stop + In-Place Safety)
# ============================================================

def handle_callback_query(
    cq: dict[str, Any], db: PortalDatabase, client: TelegramPortalClient
) -> None:
    """Dispatches inline button clicks with zero lag and in-place confirmation."""
    cq_id = cq["id"]
    from_user = cq["from"]
    user_id = from_user["id"]
    chat_id = cq["message"]["chat"]["id"]
    message_id = cq["message"]["message_id"]
    data = cq.get("data", "")

    # 1. Stop button loading spinner immediately (except sub:toggle which sends alert/popup text)
    if not data.startswith("sub:toggle:"):
        client.answer_callback_query(cq_id)

    if data == "noop":
        return

    if data.startswith("sub:toggle:"):
        bot_raw = data.split(":", 2)[2]
        try:
            bot_type = BotServiceType(bot_raw)
        except ValueError:
            client.answer_callback_query(cq_id)
            return

        ok, is_active, msg = db.toggle_subscription(user_id, bot_type)
        client.answer_callback_query(cq_id, msg, show_alert=not ok)
        return

    user = db.get_or_create_user(
        user_id=user_id,
        chat_id=chat_id,
        username=from_user.get("username"),
        first_name=from_user.get("first_name"),
    )

    # 2. Main Navigation
    if data == "nav:main":
        text, markup = render_main_menu(user)
        client.edit_message_text(chat_id, message_id, text, markup)
        return

    if data == "nav:subs":
        subs = db.get_user_subscriptions(user_id)
        text, markup = render_subscription_view(user, subs)
        client.edit_message_text(chat_id, message_id, text, markup)
        return

    if data == "nav:tiers":
        text, markup = render_tier_benefits_view(user)
        client.edit_message_text(chat_id, message_id, text, markup)
        return

    if data == "nav:help":
        text, markup = render_help_view()
        client.edit_message_text(chat_id, message_id, text, markup)
        return

    # 3. Category Views
    if data == "cat:home":
        text, markup = render_category_home_view()
        client.edit_message_text(chat_id, message_id, text, markup)
        return

    if data.startswith("cat:view:"):
        cat_raw = data.split(":", 2)[2]
        try:
            category = BotCategory(cat_raw)
        except ValueError:
            category = BotCategory.FINANCE
        subs = db.get_user_subscriptions(user_id)
        text, markup = render_category_view(category, user, subs)
        client.edit_message_text(chat_id, message_id, text, markup)
        return

    # 4. In-Place Accidental Click Prevention - Step 1: Expand Confirmation Buttons
    # Format: `sub:ask_unsub:<bot_id>:<context>`
    if data.startswith("sub:ask_unsub:"):
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
            text, markup = render_category_view(cat_enum, user, subs, confirming_bot_id=bot_raw)
        elif context_str.startswith("search:"):
            query = context_str.split(":", 1)[1]
            matched = db.search_bots(query)
            text, markup = render_search_results_view(query, matched, user, subs, confirming_bot_id=bot_raw)
        else:
            text, markup = render_subscription_view(user, subs, confirming_bot_id=bot_raw)

        client.edit_message_text(chat_id, message_id, text, markup)
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
        else:
            text, markup = render_subscription_view(user, subs)

        client.edit_message_text(chat_id, message_id, text, markup)
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
            return

        ok, msg = db.unsubscribe(user_id, bot_type)
        subs = db.get_user_subscriptions(user_id)

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
        else:
            text, markup = render_subscription_view(user, subs)

        client.edit_message_text(chat_id, message_id, text, markup)
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
            return

        ok, msg = db.subscribe(user_id, bot_type)
        if not ok:
            # Over quota modal popup
            client.send_message(chat_id, msg)
            return

        subs = db.get_user_subscriptions(user_id)
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
        else:
            text, markup = render_subscription_view(user, subs)

        client.edit_message_text(chat_id, message_id, text, markup)
        return
