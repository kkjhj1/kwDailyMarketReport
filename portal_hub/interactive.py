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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from portal_hub.db import PortalDatabase
from portal_hub.models import BotCategory, BotServiceType, UserProfile, UserTier
from portal_hub.telegram import TelegramPortalClient
from market_brief.link_gate import GATE_REGISTRY, build_gate_button

logger = logging.getLogger(__name__)


def build_persistent_reply_keyboard() -> dict[str, Any]:
    """Builds persistent 2x2 bottom touch buttons pinned to mobile Telegram screen."""
    return {
        "keyboard": [
            [
                {"text": "📋 내 구독 관리"},
                {"text": "📂 봇 둘러보기"},
            ],
            [
                {"text": "💎 내 등급 혜택"},
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
        active_bots = [b.short_name for b, act in subs.items() if act]
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
        f"💡 <i>전체 봇을 둘러보시려면 아래 버튼을 누르거나, 찾으시는 단어(예: '시황', '여행', '입시', '실버', '키즈', '욜로')를 입력하세요!</i>"
    )

    markup = {
        "inline_keyboard": [
            [
                {"text": "📋 전체 봇 둘러보고 구독하기", "callback_data": "cat:all"},
            ],
            [
                {"text": "⚙️ 내 구독 관리", "callback_data": "nav:subs"},
                {"text": "💎 내 등급 혜택", "callback_data": "nav:tiers"},
            ],
            [
                {"text": "💬 고객센터 / 문의", "callback_data": "support:home"},
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
            deep_link_url = f"https://t.me/{bot.telegram_handle}?start=portal"
            tok = f"bot_{bot.value.replace('_bot', '')}"
            GATE_REGISTRY.update_preset_url(tok, deep_link_url)
            move_btn = build_gate_button(tok, f"{bot.short_name} 이동 🔗")
            if is_subbed:
                btn_text = f"🟢 구독중  │  {bot.icon} {bot.short_name}"
                keyboard.append([
                    {"text": btn_text, "callback_data": f"sub:ask_unsub:{bot.value}:subs"},
                    move_btn,
                ])
            else:
                btn_text = f"⚪ 미구독  │  {bot.icon} {bot.short_name}"
                keyboard.append([
                    {"text": btn_text, "callback_data": f"sub:do_sub:{bot.value}:subs"},
                    move_btn,
                ])

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

            tok = f"bot_{b.value.replace('_bot', '')}"
            GATE_REGISTRY.update_preset_url(tok, deep_link_url)
            move_btn = build_gate_button(tok, f"{b.short_name} 이동 🔗")

            keyboard.append([
                action_btn,
                move_btn,
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
        f"• 💰 <b>금융 / 재테크</b>: 글로벌 시황, 증시, 환율 등 (시황 브리핑)\n"
        f"• 🎓 <b>교육 / 입시</b>: 대입/수시 To-Do, 설명회 요약 등 (입시 나침반)\n"
        f"• ✈️ <b>라이프 / 여가</b>: 특가 항공권(여행), 부모님 복지(실버케어), 주말 나들이·육아(키즈), 1인 취미·힐링(욜로)\n"
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
            {"text": "📋 6개 봇 한눈에 보기", "callback_data": "cat:all"},
        ],
        [
            {"text": "🏠 메인 홈으로", "callback_data": "nav:main"},
        ],
    ]
    return text, {"inline_keyboard": keyboard}


CATALOG_ALL_ORDER = (
    BotServiceType.BRIEF,
    BotServiceType.ADMISSION,
    BotServiceType.TRAVEL,
    BotServiceType.SILVER,
    BotServiceType.KIDS,
    BotServiceType.YOLO,
)

CALLBACK_DATA_MAX_BYTES = 64


def render_catalog_all_view(
    user: UserProfile,
    subs: dict[BotServiceType, bool],
    confirming_bot_id: Optional[str] = None,
) -> tuple[str, dict[str, Any]]:
    """Renders all 6 bots on one screen with subscription badges and 1-tap subscribe/manage buttons."""
    active_count = sum(1 for v in subs.values() if v)
    lines = [
        "📋 <b>6개 봇 한눈에 보기</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        f"📊 <b>구독 슬롯</b>: <b>{build_slot_gauge(active_count, user.tier)}</b>",
    ]

    if confirming_bot_id:
        target_bot = next((b for b in BotServiceType if b.value == confirming_bot_id), None)
        target_name = html.escape(target_bot.short_name if target_bot else confirming_bot_id)
        lines.append(f"\n⚠️ <b>[{target_name}] 구독을 정말 해지하시겠습니까?</b>")
        lines.append("<i>아래 [🔴 해지 확정] 또는 [🔙 취소]를 터치하세요.</i>")

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
                {"text": "🔴 네, 해지 확정", "callback_data": f"sub:do_unsub:{b.value}:all"},
                {"text": "🔙 취소 (구독 유지)", "callback_data": "sub:cancel_unsub:all"},
            ])
            continue

        if subs.get(b, False):
            row = [{"text": "🟢 구독중 (해지)", "callback_data": f"sub:ask_unsub:{b.value}:all"}]
        else:
            row = [{"text": "➕ 구독하기", "callback_data": f"sub:do_sub:{b.value}:all"}]
        deep_link = b.get_start_link()
        if deep_link:
            row.append({"text": f"{b.short_name} 이동 🔗", "url": deep_link})
        keyboard.append(row)

    keyboard.extend([
        [
            {"text": "📂 카테고리별 보기", "callback_data": "cat:home"},
            {"text": "📋 내 구독 센터", "callback_data": "nav:subs"},
        ],
        [
            {"text": "🏠 메인 홈으로", "callback_data": "nav:main"},
        ],
    ])
    return "\n".join(lines), {"inline_keyboard": keyboard}


def _context_return_buttons(bot_type: BotServiceType, context_str: str) -> list[dict[str, Any]]:
    """Returns a 'back to where you came from' button for catalog/category contexts."""
    if context_str == "all":
        return [{"text": "📋 6개 봇 한눈에 보기", "callback_data": "cat:all"}]
    if context_str.startswith("cat:"):
        return [{"text": "📂 카테고리 보기", "callback_data": f"cat:view:{bot_type.category.value}"}]
    return []


def _onboarding_keyboard(bot_type: BotServiceType, context_str: str) -> dict[str, Any]:
    """Deep link into the bot's chat room + subs/home (+ optional context return) buttons."""
    keyboard = []
    deep_link = bot_type.get_start_link()
    if deep_link:
        keyboard.append([{"text": f"🚀 {bot_type.short_name} 대화방 바로가기", "url": deep_link}])
    keyboard.append([
        {"text": "📋 내 구독 센터", "callback_data": "nav:subs"},
        {"text": "🏠 메인 홈으로", "callback_data": "nav:main"},
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
        BotServiceType.BRIEF: "매 영업일 아침 06:30 (장전 핵심 요약)",
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
    """Renders over-quota notice with 1-click swap buttons for each currently active bot."""
    text = html.escape(reason)
    if swappable_bots:
        text += (
            f"\n\n🔄 <b>기존 구독을 [{html.escape(bot_type.short_name)}](으)로 바로 교체할 수 있습니다.</b>\n"
            f"<i>교체할 봇을 선택해 주세요.</i>"
        )
    keyboard = [
        [{
            "text": f"🔄 {old_b.short_name} ➔ {bot_type.short_name} 교체",
            "callback_data": build_swap_callback_data(old_b, bot_type, context_str),
        }]
        for old_b in swappable_bots
    ]
    keyboard.append([{"text": "💎 등급 업그레이드 신청", "callback_data": "support:upgrade_menu"}])
    keyboard.append([{"text": "🔙 돌아가기", "callback_data": "nav:subs"}])
    return text, {"inline_keyboard": keyboard}


def render_swap_result_view(
    ok: bool, msg: str, new_bot: BotServiceType, context_str: str = "subs"
) -> tuple[str, dict[str, Any]]:
    """Renders swap success (with deep link onboarding) or the failure reason."""
    if not ok:
        text = f"⚠️ <b>구독 교체 실패</b>\n\n{html.escape(msg)}"
        return text, {"inline_keyboard": [[{"text": "📋 내 구독 센터", "callback_data": "nav:subs"}]]}

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
        lines.append("💡 <i>추천 검색어: '환율', '시황', '입시', '수능', '여행', '특가', '실버', '효도', '키즈', '해열제', '욜로', '혼놀'</i>")
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

            tok = f"bot_{b.value.replace('_bot', '')}"
            GATE_REGISTRY.update_preset_url(tok, deep_link_url)
            move_btn = build_gate_button(tok, f"{b.short_name} 이동 🔗")

            keyboard.append([
                action_btn,
                move_btn,
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
            {"text": "💬 관리자 문의 & 등급 신청", "callback_data": "support:home"},
        ],
        [
            {"text": "📋 내 구독 센터", "callback_data": "nav:subs"},
            {"text": "🏠 메인 홈으로", "callback_data": "nav:main"},
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
            {"text": "💎 등급 업그레이드 신청 (원클릭)", "callback_data": "support:upgrade_menu"},
        ],
        [
            {"text": "✉️ 1:1 문의사항 작성하기", "callback_data": "support:write_inquiry"},
        ],
        [
            {"text": "❓ 자주 묻는 질문(FAQ)", "callback_data": "nav:help"},
            {"text": "🏠 메인 홈으로", "callback_data": "nav:main"},
        ],
    ]
    return text, {"inline_keyboard": keyboard}


def render_upgrade_request_view(user: UserProfile) -> tuple[str, dict[str, Any]]:
    """Renders 1-click tier upgrade request form."""
    text = (
        f"💎 <b>등급 업그레이드 신청</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"현재 회원님의 등급: {user.tier.badge}\n\n"
        f"원하시는 희망 등급을 선택해 주세요.\n"
        f"터치 즉시 운영팀에 신청서가 전송되며, 확인 후 신속히 승인해 드립니다!\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    keyboard = [
        [
            {"text": "🟢 그린 등급 신청 (최대 3개)", "callback_data": "support:req_tier:GREEN"},
        ],
        [
            {"text": "🔴 레드 등급 신청 (최대 5개)", "callback_data": "support:req_tier:RED"},
        ],
        [
            {"text": "⚫ 블랙 VIP 신청 (무제한)", "callback_data": "support:req_tier:BLACK"},
        ],
        [
            {"text": "🔙 고객센터 홈", "callback_data": "support:home"},
            {"text": "🏠 메인 홈", "callback_data": "nav:main"},
        ],
    ]
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
            {"text": "🔙 작성 취소 (고객센터 홈)", "callback_data": "support:home"},
        ]
    ]
    return text, {"inline_keyboard": keyboard}


def render_help_view() -> tuple[str, dict[str, Any]]:
    """Renders FAQ and help tips."""
    text = (
        f"❓ <b>포털 봇 이용 가이드</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"<b>Q. 원하는 봇을 가장 빠르게 찾는 방법은?</b>\n"
        f"A. 채팅창에 '환율', '수능', '항공', '실버', '키즈', '욜로' 등 찾으시는 단어를 그냥 입력하시면 0.1초 만에 봇을 찾아드립니다!\n\n"
        f"<b>Q. 실수로 해지 버튼을 눌렀는데 바로 해지되나요?</b>\n"
        f"A. 아닙니다! 실수 방지를 위해 [해지 확정] / [취소] 버튼이 펼쳐지며, [취소]를 누르면 안전하게 구독이 유지됩니다.\n\n"
        f"<b>Q. 알림은 어디로 오나요?</b>\n"
        f"A. 구독을 켜시면 지정된 발송 시간에 각 전문 봇 채널을 통해 고품질 브리핑이 발송됩니다.\n"
        f"━━━━━━━━━━━━━━━━━━━━"
    )
    keyboard = [
        [
            {"text": "💬 1:1 고객센터 문의", "callback_data": "support:home"},
            {"text": "📋 내 구독 센터", "callback_data": "nav:subs"},
        ],
        [
            {"text": "🏠 메인 홈으로", "callback_data": "nav:main"},
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
    """Dispatches inline button clicks with zero lag and in-place confirmation."""
    cq_id = cq["id"]
    from_user = cq.get("from") or {}
    user_id = from_user.get("id")
    msg = cq.get("message") or {}
    chat = msg.get("chat") or {}
    chat_id = chat.get("id") or user_id
    message_id = msg.get("message_id")
    data = cq.get("data", "")

    # 1. Stop button loading spinner immediately (except sub:toggle and adm which send popup text or verify auth)
    if not data.startswith("sub:toggle:") and not data.startswith("adm:"):
        client.answer_callback_query(cq_id)

    # 0. Link Confirmation Gate Intercept
    if data.startswith("gate:"):
        from market_brief.link_gate import LinkGateRouter, GATE_REGISTRY
        gate_router = LinkGateRouter(GATE_REGISTRY)
        gate_res = gate_router.handle_callback(data, "menu_bot", str(chat_id), int(message_id or 0))
        if gate_res.get("handled"):
            if gate_res.get("action") == "CONFIRM_CARD":
                _safe_edit_or_send(client, chat_id, message_id, gate_res["html"], {"inline_keyboard": gate_res["inline_keyboard"]})
                return
            elif gate_res.get("action") == "CANCEL":
                subs = db.get_user_subscriptions(user_id)
                text, markup = render_subscription_view(user, subs)
                _safe_edit_or_send(client, chat_id, message_id, text, markup)
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

        ok, is_active, msg = db.toggle_subscription(user_id, bot_type)
        client.answer_callback_query(cq_id, msg, show_alert=not ok)
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
                        {"text": "💎 등급 업그레이드 신청", "callback_data": "support:upgrade_menu"},
                        {"text": "📋 내 구독 관리", "callback_data": "nav:subs"},
                    ]
                ]
            }
            client.send_message(chat_id, msg, reply_markup=quota_markup)
            return

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

        # 2. Reply to user with clean in-place notice
        if status_code == "DUPLICATE":
            done_text = (
                f"🫡 <b>이미 신청이 접수되어 운영팀 심사 대기 중입니다!</b>\n"
                f"━━━━━━━━━━━━━━━━━━━━\n"
                f"• 희망 등급: <b>{html.escape(req_tier)}</b>\n\n"
                f"관리자 승인 즉시 봇을 통해 승인 완료 알림을 보내드리겠습니다.\n"
                f"━━━━━━━━━━━━━━━━━━━━"
            )
        else:
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
                    {"text": "📋 내 구독 센터", "callback_data": "nav:subs"},
                    {"text": "🏠 메인 홈으로", "callback_data": "nav:main"},
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
            return

        ok, msg = db.subscribe(user_id, bot_type)
        if not ok:
            # Over quota prompt with 1-click Swap / Upgrade / Back buttons
            subs = db.get_user_subscriptions(user_id)
            swappable_bots = [b for b in BotServiceType if subs.get(b, False) and b != bot_type]
            text, markup = render_quota_swap_view(bot_type, swappable_bots, msg, context_str)
            client.send_message(chat_id, text, reply_markup=markup)
            return

        text, markup = render_subscribe_onboarding_view(bot_type, context_str)
        _safe_edit_or_send(client, chat_id, message_id, text, markup)
        return

    # 8. 1-Click Atomic Swap (over-quota)
    # Format: `sub:swap:<old_bot_id>:<new_bot_id>:<context>`
    if data.startswith("sub:swap:"):
        parts = data.split(":")
        if len(parts) < 4:
            return
        context_str = ":".join(parts[4:]) if len(parts) > 4 else "subs"
        try:
            old_bot = BotServiceType(parts[2])
            new_bot = BotServiceType(parts[3])
        except ValueError:
            return

        ok, msg = db.atomic_swap_subscription(user_id, old_bot, new_bot)
        text, markup = render_swap_result_view(ok, msg, new_bot, context_str)
        _safe_edit_or_send(client, chat_id, message_id, text, markup)
        return
