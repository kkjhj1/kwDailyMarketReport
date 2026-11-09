"""Interactive Telegram Bot Handler for Admission Briefing.
Handles /start, persistent reply keyboards, inline grade/region selection, /todo, /recap, /region,
and natural language keyword matching for effortless mobile touch UX.
"""
from __future__ import annotations
import html
import logging
import time
from datetime import datetime
from typing import Any, Optional

from admission_brief.ai_curator import AICurator
from admission_brief.channels.telegram import (
    TelegramChannel,
    _is_valid_url,
    build_briefing_share_text,
    build_event_share_text,
    build_glossary_share_text,
    build_google_calendar_url,
    format_kakao_share_html,
)
from admission_brief.fixtures.glossary import GlossaryEntry, get_glossary_entry, list_glossary_entries
from admission_brief.models import (
    CHILD_ALIASES,
    MAX_CHILDREN,
    AdmissionBriefing,
    AdmissionEvent,
    BriefingType,
    ChildProfile,
    EventCategory,
    ParentUserProfile,
    TargetGrade,
    TargetRegion,
)
from admission_brief.storage.db import AdmissionDatabase

logger = logging.getLogger(__name__)

EVENTS_PAGE_SIZE = 3
GLOSSARY_COLUMNS = 3
CALLBACK_MAX_BYTES = 64
GLOSSARY_MENU_TEXT = "📖 1초 입시용어 사전"
SETTINGS_MENU_TEXT = "⚙️ 자녀 및 맞춤 설정"
SHARE_TOAST = "📋 아래 박스를 터치하면 복사됩니다!"

# callback_data short codes (adm:cat:<CODE>:<PAGE>) — ASCII only, well under 64 bytes
CATEGORY_CODES: dict[str, EventCategory] = {
    "EXPO": EventCategory.EXPO,
    "BRIEF": EventCategory.BRIEFING,
    "SEMI": EventCategory.SEMINAR,
}
CATEGORY_TO_CODE: dict[EventCategory, str] = {v: k for k, v in CATEGORY_CODES.items()}


def build_persistent_reply_keyboard() -> dict[str, Any]:
    """Builds persistent bottom touch keyboard buttons that stay on parent's phone screen."""
    return {
        "keyboard": [
            [
                {"text": "📋 이번 주 필수 To-Do"},
                {"text": "⚡ 긴급 속보 & 핫 행사 TOP 3"},
            ],
            [
                {"text": "💡 1분 입시 팩트체크 (So-What)"},
                {"text": "🎪 설명회·박람회 전체보기"},
            ],
            [
                {"text": "🎓 학년/지역 설정"},
                {"text": "❓ 이용 도움말"},
            ],
            [
                {"text": GLOSSARY_MENU_TEXT},
                {"text": SETTINGS_MENU_TEXT},
            ],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def build_events_category_keyboard() -> dict[str, Any]:
    """Builds inline keyboard for the three event categories."""
    return {
        "inline_keyboard": [
            [{"text": f"{EventCategory.EXPO.icon} {EventCategory.EXPO.title}", "callback_data": "adm:cat:EXPO:0"}],
            [{"text": f"{EventCategory.BRIEFING.icon} {EventCategory.BRIEFING.title}", "callback_data": "adm:cat:BRIEF:0"}],
            [{"text": f"{EventCategory.SEMINAR.icon} {EventCategory.SEMINAR.title}", "callback_data": "adm:cat:SEMI:0"}],
            [{"text": "🏠 통합 메뉴봇 바로가기", "url": "https://t.me/KwMenu_bot?start=from_sub"}],
        ]
    }


def build_events_page_keyboard(
    category: EventCategory,
    page: int,
    total: int,
    events: Optional[list[AdmissionEvent]] = None,
    include_share: bool = False,
) -> dict[str, Any]:
    """Builds per-card Google Calendar url buttons, prev/next pager and back-to-categories buttons."""
    code = CATEGORY_TO_CODE[category]
    last_page = max(0, (total - 1) // EVENTS_PAGE_SIZE)
    rows: list[list[dict[str, Any]]] = []
    for idx, ev in enumerate(events or [], page * EVENTS_PAGE_SIZE + 1):
        cal_url = build_google_calendar_url(ev)
        if cal_url:
            rows.append([{"text": f"📅 {idx}번 구글 캘린더에 담기", "url": cal_url}])
        if include_share:
            share_cb = f"adm:sh:ev:{ev.event_id}"
            if len(share_cb.encode("utf-8")) <= CALLBACK_MAX_BYTES:
                rows.append([{"text": f"📲 {idx}번 카톡 공유", "callback_data": share_cb}])
    nav_row = []
    if page > 0:
        nav_row.append({"text": "◀ 이전", "callback_data": f"adm:cat:{code}:{page - 1}"})
    if page < last_page:
        nav_row.append({"text": "다음 ▶", "callback_data": f"adm:cat:{code}:{page + 1}"})
    if nav_row:
        rows.append(nav_row)
    rows.append([{"text": "🏛 카테고리 목록", "callback_data": "adm:menu"}])
    return {"inline_keyboard": rows}


def _dday_label(event_date_str: str, now: Optional[datetime] = None) -> str:
    """Returns ' · D-n' / ' · D-Day' for dated upcoming events, '' otherwise."""
    try:
        ev_date = datetime.strptime((event_date_str or "").strip()[:10], "%Y-%m-%d").date()
    except ValueError:
        return ""
    days = (ev_date - (now or datetime.now()).date()).days
    if days < 0:
        return ""
    return " · <b>D-Day</b>" if days == 0 else f" · D-{days}"


def format_events_page_html(
    category: EventCategory,
    events: list[AdmissionEvent],
    page: int,
    total: int,
    grade: TargetGrade,
    region: TargetRegion,
) -> str:
    """Formats one page of event cards (1 line = 1 fact; details live behind the link)."""
    total_pages = max(1, (total + EVENTS_PAGE_SIZE - 1) // EVENTS_PAGE_SIZE)
    lines = [
        f"{category.icon} <b>{html.escape(category.title)}</b> · {page + 1}/{total_pages}쪽",
        f"🎯 필터: <b>{html.escape(grade.label)}</b> · {html.escape(region.label)}",
        "━━━━━━━━━━━━━━━━━━━━",
    ]
    if not events:
        lines.append("현재 조건에 맞는 예정 행사가 없습니다.")
        lines.append("🎓 학년/지역 설정을 바꾸거나 다른 카테고리를 확인해 주세요.")
    for idx, ev in enumerate(events, page * EVENTS_PAGE_SIZE + 1):
        place = "💻 온라인 생중계" if ev.is_online_live else ev.target_region.badge
        lines.append(f"<b>{idx}. {html.escape(ev.title)}</b>")
        lines.append(f"🗓 <b>{html.escape(ev.event_date_str)}</b>{_dday_label(ev.event_date_str)}")
        lines.append(f"🏢 {html.escape(ev.organizer)} · {html.escape(place)}")
        if ev.is_first_come:
            lines.append("⚡ <b>선착순 마감</b>")
        link = ev.booking_url or ev.source_url
        if link and _is_valid_url(link):
            lines.append(f"🔗 <a href=\"{html.escape(link)}\">신청·상세 보기</a>")
        lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"<i>총 {total}건 · 지난 행사는 자동 제외됩니다</i>")
    return "\n".join(lines)


def build_glossary_menu_keyboard() -> dict[str, Any]:
    """15 terms as a 3-column grid (adm:gl:<CODE>) plus a back-to-categories row."""
    entries = list_glossary_entries()
    rows: list[list[dict[str, Any]]] = [
        [{"text": e.short_name, "callback_data": f"adm:gl:{e.code}"} for e in entries[i:i + GLOSSARY_COLUMNS]]
        for i in range(0, len(entries), GLOSSARY_COLUMNS)
    ]
    rows.append([{"text": "🏛 카테고리 목록", "callback_data": "adm:menu"}])
    return {"inline_keyboard": rows}


def build_glossary_detail_keyboard(entry: GlossaryEntry) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "📲 카톡 공유용 요약", "callback_data": f"adm:sh:gl:{entry.code}"}],
            [
                {"text": "📚 다른 용어 보기", "callback_data": "adm:gl:menu"},
                {"text": "🏛 카테고리 목록", "callback_data": "adm:menu"},
            ],
        ]
    }


def format_glossary_menu_html() -> str:
    return (
        "📖 <b>1초 입시 은어·용어 사전</b>\n\n"
        "설명회에서 들은 낯선 용어, 학부모 눈높이로 3줄 풀이해 드립니다.\n"
        "👇 궁금한 용어를 탭해 주세요:"
    )


def format_glossary_entry_html(entry: GlossaryEntry) -> str:
    """Term detail card: 개념 / 학부모 득실 / 주의사항."""
    return "\n".join([
        f"📖 <b>[1초 입시사전] {html.escape(entry.term)}</b>",
        "━━━━━━━━━━━━━━━━━━━━",
        "💡 <b>핵심 개념</b>",
        html.escape(entry.definition),
        "",
        "🎯 <b>학부모 실전 득실</b>",
        html.escape(entry.impact),
        "",
        "⚠️ <b>주의사항 & 꿀팁</b>",
        html.escape(entry.caution),
        "━━━━━━━━━━━━━━━━━━━━",
    ])


def build_grade_inline_keyboard(include_back: bool = False) -> dict[str, Any]:
    """Builds inline keyboard markup for grade selection."""
    rows = [
        [
            {"text": "📘 중등 / 예비고1", "callback_data": "grade:MIDDLE"},
        ],
        [
            {"text": "📗 고1 ~ 고2", "callback_data": "grade:HIGH_1_2"},
        ],
        [
            {"text": "📕 고3 / N수생", "callback_data": "grade:HIGH_3_N"},
        ],
        [
            {"text": "🌐 전학년 공통 알림", "callback_data": "grade:ALL"},
        ],
    ]
    if include_back:
        rows.append([{"text": "⚙️ 설정으로 돌아가기", "callback_data": "adm:settings"}])
    return {"inline_keyboard": rows}


REGION_SHORT: dict[TargetRegion, str] = {
    TargetRegion.ALL: "전국",
    TargetRegion.ONLINE: "온라인",
    TargetRegion.SEOUL: "서울",
    TargetRegion.GYEONGGI: "경기·인천",
    TargetRegion.LOCAL: "지방",
}


def format_child_line(child: ChildProfile, active: bool = False) -> str:
    """'• 첫째: 📕 고3/수험생 (서울)' — one child per line, active child marked."""
    grade_txt = child.grade.badge.replace("[", "").replace("]", "")
    mark = " ✅" if active else ""
    return f"• <b>{html.escape(child.alias)}</b>: {html.escape(grade_txt)} ({REGION_SHORT.get(child.region, '전국')}){mark}"


def build_child_add_keyboard(slot: int) -> dict[str, Any]:
    """Grade picker for a new child; slot (new child's index) guards against double-tap duplicates."""
    return {
        "inline_keyboard": [
            [{"text": "📘 중등 / 예비고1", "callback_data": f"adm:cadd:MIDDLE:{slot}"}],
            [{"text": "📗 고1 ~ 고2", "callback_data": f"adm:cadd:HIGH_1_2:{slot}"}],
            [{"text": "📕 고3 / N수생", "callback_data": f"adm:cadd:HIGH_3_N:{slot}"}],
            [{"text": "⚙️ 설정으로 돌아가기", "callback_data": "adm:settings"}],
        ]
    }


def build_region_inline_keyboard(include_back: bool = False) -> dict[str, Any]:
    """Builds inline keyboard markup for region selection."""
    rows = [
        [
            {"text": "🌐 온라인/유튜브 라이브만", "callback_data": "region:ONLINE"},
        ],
        [
            {"text": "📍 서울 (대치·목동·중계)", "callback_data": "region:SEOUL"},
        ],
        [
            {"text": "📍 경기·인천 (분당·일산·평촌)", "callback_data": "region:GYEONGGI"},
        ],
        [
            {"text": "📍 비수도권 / 지방 거점", "callback_data": "region:LOCAL"},
        ],
        [
            {"text": "🏢 전국 전체 (온·오프라인)", "callback_data": "region:ALL"},
        ],
    ]
    if include_back:
        rows.append([{"text": "⚙️ 설정으로 돌아가기", "callback_data": "adm:settings"}])
    return {"inline_keyboard": rows}


class AdmissionInteractiveBot:
    """Handles commands, callback queries, and grade/region personalization for parents."""

    def __init__(
        self,
        channel: TelegramChannel,
        db: AdmissionDatabase,
        curator: Optional[AICurator] = None,
    ):
        self.channel = channel
        self.db = db
        self.curator = curator or AICurator()

    def handle_update(self, update: dict[str, Any]) -> None:
        """Processes a single incoming Telegram update (Message or CallbackQuery)."""
        # 1. Callback Query (Inline Button Click)
        if "callback_query" in update:
            self._handle_callback_query(update["callback_query"])
            return

        # 2. Text Message
        msg = update.get("message")
        if not msg or "text" not in msg or "chat" not in msg:
            return

        chat = msg["chat"]
        chat_id = str(chat["id"])
        text = msg["text"].strip()
        username = chat.get("username", "")
        first_name = chat.get("first_name", "")

        # Upsert subscriber on any interaction
        profile = self.db.get_subscriber(chat_id)
        if not profile:
            profile = ParentUserProfile(
                chat_id=chat_id,
                username=username,
                first_name=first_name,
                registered_at=datetime.now(),
                last_active_at=datetime.now(),
            )
            self.db.upsert_subscriber(profile)

        # Route commands & friendly Korean natural text touch buttons
        clean_text = text.strip()
        if clean_text.startswith("/start"):
            self._cmd_start(chat_id, first_name)
        elif clean_text == GLOSSARY_MENU_TEXT or clean_text.startswith("/glossary") or "용어" in clean_text:
            self._cmd_glossary_menu(chat_id)
        elif clean_text == SETTINGS_MENU_TEXT:
            self._cmd_settings(chat_id)
        elif (
            clean_text.startswith("/events") or "박람회" in clean_text or "설명회" in clean_text
            or "세미나" in clean_text or "행사 전체" in clean_text
        ):
            self._cmd_events_menu(chat_id)
        elif clean_text.startswith("/todo") or "할일" in clean_text or "투두" in clean_text or "이번 주" in clean_text or "체크리스트" in clean_text:
            self._cmd_todo(chat_id)
        elif clean_text.startswith("/hot") or "속보" in clean_text or "핫 행사" in clean_text or "행사" in clean_text or "요약" in clean_text or clean_text.startswith("/recap"):
            self._cmd_hot_events(chat_id)
        elif clean_text.startswith("/fact") or "팩트" in clean_text or "소왓" in clean_text or "so-what" in clean_text.lower() or "인사이트" in clean_text:
            self._cmd_factcheck(chat_id)
        elif clean_text.startswith("/settings") or "설정" in clean_text:
            self._cmd_settings(chat_id)
        elif clean_text.startswith("/grade") or "학년" in clean_text:
            self._cmd_grade(chat_id)
        elif clean_text.startswith("/region") or "지역" in clean_text or "온라인" in clean_text:
            self._cmd_region(chat_id)
        elif clean_text.startswith("/help") or "도움말" in clean_text or "안내" in clean_text or "메뉴" in clean_text:
            self._cmd_help(chat_id)
        else:
            # Fallback friendly response with persistent keyboard
            self.channel.send_message(
                chat_id,
                "안녕하세요! 에듀브리프(EduBrief) 입시 비서입니다.\n"
                "타자를 치실 필요 없이, **아래 화면의 메뉴 버튼**을 손가락으로 탭해 주세요! 👇",
                reply_markup=build_persistent_reply_keyboard(),
            )

    def _cmd_start(self, chat_id: str, first_name: str) -> None:
        """Sends welcome greeting with grade selection buttons and persistent bottom menu."""
        name_str = f" {html.escape(first_name)}님" if first_name else " 학부모님"
        welcome_text = (
            f"반갑습니다{name_str}! 🎓\n"
            "<b>대치동 상위권 입시 인텔리전스, 에듀브리프 (EduBrief)</b>입니다.\n\n"
            "복잡한 입시 설명회와 넘쳐나는 학원 광고 속에서,\n"
            "<b>내 아이에게 딱 맞는 100% 공인 팩트와 긴급 속보 & 핫 행사</b>만 엄선해 드립니다.\n\n"
            "👇 <b>[1단계] 자녀분의 현재 학년을 탭해 주세요:</b>"
        )
        self.channel.send_message(
            chat_id,
            welcome_text,
            reply_markup=build_grade_inline_keyboard(),
        )

    def _cmd_grade(self, chat_id: str, message_id: Optional[int] = None) -> None:
        """Presents grade selection inline keyboard."""
        text = "변경하실 <b>자녀분의 현재 학년</b>을 선택해 주세요:"
        markup = build_grade_inline_keyboard(include_back=bool(message_id))
        if message_id:
            self._safe_edit_or_send(chat_id, message_id, text, markup)
        else:
            self.channel.send_message(chat_id, text, reply_markup=markup)

    def _cmd_region(self, chat_id: str, message_id: Optional[int] = None) -> None:
        """Presents region selection inline keyboard."""
        text = "희망하시는 <b>설명회 참여 지역 또는 온라인 전용 여부</b>를 선택해 주세요:"
        markup = build_region_inline_keyboard(include_back=bool(message_id))
        if message_id:
            self._safe_edit_or_send(chat_id, message_id, text, markup)
        else:
            self.channel.send_message(chat_id, text, reply_markup=markup)

    def _cmd_settings(self, chat_id: str, message_id: Optional[int] = None, notice: str = "") -> None:
        """Presents grade/region configuration plus the registered children (multi-child switch)."""
        profile = self.db.get_subscriber(chat_id)
        active = profile.get_active_child() if profile else None
        current_grade = active.grade.label if active else "미설정"
        current_region = active.region.label if active else "전국 전체"
        lines = [f"⚙️ <b>맞춤 알림 설정 현황</b>", ""]
        if notice:
            lines.extend([notice, ""])
        if profile and profile.children:
            lines.append(f"👨‍👩‍👧 <b>등록 자녀</b> (현재: {html.escape(active.alias)})")
            for i, child in enumerate(profile.children):
                lines.append(format_child_line(child, active=(i == profile.active_child_index)))
            lines.append("")
        lines.extend([
            f"• <b>자녀 학년:</b> {html.escape(current_grade)}",
            f"• <b>관심 지역:</b> {html.escape(current_region)}",
            "",
            "변경을 원하시는 항목의 버튼을 선택해 주세요:",
        ])
        rows: list[list[dict[str, Any]]] = [
            [
                {"text": "🎓 학년 변경하기", "callback_data": "menu:grade"},
                {"text": "📍 지역/온라인 변경하기", "callback_data": "menu:region"},
            ]
        ]
        children = profile.children if profile else []
        if len(children) >= 2:
            nxt_idx = (profile.active_child_index + 1) % len(children)
            rows.append([{
                "text": f"👦 {active.alias} ⇄ 👧 {children[nxt_idx].alias} 전환",
                "callback_data": f"adm:child:toggle:{nxt_idx}:{children[nxt_idx].grade.value}",
            }])
        if len(children) < MAX_CHILDREN:
            rows.append([{"text": "➕ 자녀 추가하기", "callback_data": "adm:child:add"}])
        markup = {"inline_keyboard": rows}
        text = "\n".join(lines)
        if message_id:
            self._safe_edit_or_send(chat_id, message_id, text, markup)
        else:
            self.channel.send_message(chat_id, text, reply_markup=markup)

    def _toggle_child(self, chat_id: str, message_id: Optional[int], data: str) -> None:
        """adm:child:toggle[:<IDX>:<GRADE>] — switches to the encoded child (idempotent on double-tap),
        or to the next child for the bare form; then refreshes the settings message in place."""
        profile = self.db.get_subscriber(chat_id)
        children = profile.children if profile else []
        if len(children) < 2:
            self._cmd_settings(chat_id, message_id, notice="⚠️ 전환할 자녀가 없습니다. 먼저 자녀를 추가해 주세요.")
            return
        parts = data.split(":")
        try:
            target = int(parts[3]) if len(parts) > 3 else (profile.active_child_index + 1) % len(children)
        except ValueError:
            target = (profile.active_child_index + 1) % len(children)
        if not (0 <= target < len(children)):
            target = (profile.active_child_index + 1) % len(children)
        self.db.set_active_child(chat_id, target)
        child = children[target]
        notice = f"✅ <b>{html.escape(child.alias)} ({html.escape(child.grade.label)})</b> 모드로 전환되었습니다!"
        self._cmd_settings(chat_id, message_id, notice=notice)

    def _child_add_menu(self, chat_id: str, message_id: Optional[int]) -> None:
        """Presents the grade picker for a new child (max MAX_CHILDREN)."""
        profile = self.db.get_subscriber(chat_id)
        count = max(1, len(profile.children)) if profile else 1
        if count >= MAX_CHILDREN:
            self._cmd_settings(chat_id, message_id, notice=f"⚠️ 자녀는 최대 {MAX_CHILDREN}명까지 등록할 수 있습니다.")
            return
        alias = CHILD_ALIASES[count]
        text = (
            f"➕ <b>{alias} 자녀 추가</b>\n\n"
            f"👇 {alias} 자녀의 현재 학년을 탭해 주세요:\n"
            "<i>정기 브리핑은 등록된 모든 자녀의 학년으로 받아보실 수 있습니다.</i>"
        )
        self._safe_edit_or_send(chat_id, message_id, text, build_child_add_keyboard(count))

    def _add_child(self, chat_id: str, message_id: Optional[int], data: str) -> None:
        """adm:cadd:<GRADE>[:<SLOT>] — creates 둘째/셋째 with the chosen grade and the active child's region."""
        parts = data.split(":")
        try:
            grade = TargetGrade(parts[2] if len(parts) > 2 else "")
        except ValueError:
            self._cmd_settings(chat_id, message_id, notice="⚠️ 알 수 없는 학년입니다. 다시 선택해 주세요.")
            return
        profile = self.db.get_subscriber(chat_id)
        if not profile:
            return
        count = max(1, len(profile.children))
        if len(parts) > 3 and parts[3] != str(count):
            # Stale/double-tapped picker: that slot is already filled
            self._cmd_settings(chat_id, message_id, notice="ℹ️ 이미 처리된 요청입니다.")
            return
        if count >= MAX_CHILDREN:
            self._cmd_settings(chat_id, message_id, notice=f"⚠️ 자녀는 최대 {MAX_CHILDREN}명까지 등록할 수 있습니다.")
            return
        alias = CHILD_ALIASES[count]
        child = ChildProfile(alias=alias, grade=grade, region=profile.get_active_child().region)
        if self.db.add_child(chat_id, child):
            notice = f"🎉 <b>{alias} ({html.escape(grade.label)})</b> 자녀가 추가되었습니다!"
        else:
            notice = "⚠️ 자녀 추가에 실패했습니다. 다시 시도해 주세요."
        self._cmd_settings(chat_id, message_id, notice=notice)

    def _cmd_todo(self, chat_id: str) -> None:
        """Sends the active child's grade-specific weekly action To-Do list."""
        profile = self.db.get_subscriber(chat_id)
        grade = profile.get_active_child().grade if profile else TargetGrade.MIDDLE
        briefing = self.curator.generate_weekly_todo_briefing(grade)
        if profile and len(profile.children) >= 2:
            briefing.headline = f"[{profile.get_active_child().alias}] {briefing.headline}"
        briefing.silent = True  # Silent push for regular digests
        share_markup = {"inline_keyboard": [[{"text": "📲 카톡 공유", "callback_data": "adm:sh:todo"}]]}
        self.channel.send(chat_id, briefing, reply_markup=share_markup)

    def _cmd_glossary_menu(self, chat_id: str, message_id: Optional[int] = None) -> None:
        """Presents the 15-term glossary grid (in place when navigated back from a term card)."""
        text = format_glossary_menu_html()
        markup = build_glossary_menu_keyboard()
        if message_id:
            self._safe_edit_or_send(chat_id, message_id, text, markup)
        else:
            self.channel.send_message(chat_id, text, reply_markup=markup)

    def _show_glossary_entry(self, chat_id: str, message_id: Optional[int], code: str) -> None:
        """adm:gl:<CODE> — renders one term card in place; unknown codes fall back to the grid."""
        entry = get_glossary_entry(code)
        if entry is None:
            self._cmd_glossary_menu(chat_id, message_id)
            return
        self._safe_edit_or_send(chat_id, message_id, format_glossary_entry_html(entry), build_glossary_detail_keyboard(entry))

    def _send_share_card(self, chat_id: str, data: str) -> None:
        """adm:sh:gl:<CODE> / adm:sh:todo / adm:sh:ev:<EVENT_ID> — sends a separate one-tap-copy <code> card."""
        parts = data.split(":", 3)
        kind = parts[2] if len(parts) > 2 else ""
        ref = parts[3] if len(parts) > 3 else ""
        plain = ""
        if kind == "gl":
            entry = get_glossary_entry(ref)
            if entry:
                plain = build_glossary_share_text(entry)
        elif kind == "todo":
            profile = self.db.get_subscriber(chat_id)
            grade = profile.get_active_child().grade if profile else TargetGrade.MIDDLE
            plain = build_briefing_share_text(self.curator.generate_weekly_todo_briefing(grade))
        elif kind == "ev" and ref:
            event = self.db.get_event(ref)
            if event:
                plain = build_event_share_text(event)
        if not plain:
            self.channel.send_message(chat_id, "⚠️ 공유할 내용을 찾을 수 없습니다. 메뉴에서 다시 열어 주세요.")
            return
        self.channel.send_message(chat_id, format_kakao_share_html(plain), disable_notification=True)

    def _send_welcome_todo(self, chat_id: str, grade: TargetGrade) -> None:
        """Sends this ISO week's To-Do right after onboarding and logs it so the Sunday batch skips it."""
        briefing = self.curator.generate_weekly_todo_briefing(grade)
        if self.db.has_dispatched(briefing.briefing_id, chat_id):
            return
        briefing.silent = True
        welcome_markup = {
            "inline_keyboard": [
                [
                    {"text": "⚡ 핫 행사 TOP 3", "callback_data": "adm:hot"},
                    {"text": "🎪 설명회·박람회 전체보기", "callback_data": "adm:menu"},
                ]
            ]
        }
        try:
            self.channel.send(chat_id, briefing, reply_markup=welcome_markup)
        except Exception as e:
            logger.warning("Welcome To-Do dispatch failed for %s: %s", chat_id, e)
            return
        self.db.log_dispatch(briefing.briefing_id, chat_id, "SUCCESS")

    def _cmd_hot_events(self, chat_id: str) -> None:
        """Presents top 3 hot events & breaking notices matching subscriber's profile."""
        profile = self.db.get_subscriber(chat_id)
        grade = profile.get_active_child().grade if profile else TargetGrade.ALL
        region = profile.get_active_child().region if profile else TargetRegion.ALL

        briefings = self.curator.get_top3_hot_events(grade=grade, region=region, db=self.db)
        if not briefings:
            self._cmd_recap(chat_id)
            return

        for idx, b in enumerate(briefings, 1):
            b.silent = True
            self.channel.send(chat_id, b)
            if idx < len(briefings):
                time.sleep(0.3)

    def _cmd_factcheck(self, chat_id: str) -> None:
        """Presents the latest verified 1-minute admission policy factcheck."""
        profile = self.db.get_subscriber(chat_id)
        grade = profile.get_active_child().grade if profile else TargetGrade.ALL
        briefing = self.curator.get_latest_factcheck(grade)
        briefing.silent = True
        self.channel.send(chat_id, briefing)

    def _cmd_recap(self, chat_id: str) -> None:
        """Presents the latest admission seminar recap."""
        profile = self.db.get_subscriber(chat_id)
        grade = profile.get_active_child().grade if profile else TargetGrade.ALL

        events = self.db.list_upcoming_ticketing()
        if events:
            briefing = self.curator.curate_event_briefing(events[0])
            briefing.silent = True
            self.channel.send(chat_id, briefing)
        else:
            sample_briefing = AdmissionBriefing(
                briefing_id="recap_sample",
                briefing_type=BriefingType.BRIEFING_RECAP,
                target_grade=grade,
                headline="2028 대입 개편 대비 대치동 주요 설명회 핵심 총정리",
                summary_3lines=[
                    "고교학점제 시행 후 상위권 대학은 '세특' 실질 반영률 40% 이상 확대",
                    "일반고 1등급보다 주요 전국 자사고 2등급이 서울대/연고대 지원에 유리",
                    "이번 겨울방학 '통합사회·통합과학' 1회독 안 끝내면 고1 내신 방어 불가",
                ],
                so_what="내신 등급제가 9등급에서 5등급으로 완화되면서, 심화과목 이수와 세특 영향력이 절대적입니다.",
                action_todos=[
                    "이번 주말 관내 고등학교 진학 실적 및 교육과정 편제표 비교",
                    "대치동 예비고1 단과반 레벨테스트 신청 마감 확인",
                ],
                source_name="대치동 주요 입시센터 분석 자료",
                source_url="https://www.adiga.kr",
                silent=True,
            )
            self.channel.send(chat_id, sample_briefing)

    def _cmd_help(self, chat_id: str) -> None:
        """Presents help and official channel links with persistent touch menu."""
        help_text = (
            "📌 <b>에듀브리프 (EduBrief) 간편 사용 안내</b>\n\n"
            "타자를 치실 필요 없이, **화면 하단의 메뉴 버튼**을 손가락으로 누르시면 됩니다!\n\n"
            "▪ <b>📋 이번 주 필수 To-Do</b>: 내 아이 학년별 이번 주 꼭 실천해야 할 3가지\n"
            "▪ <b>⚡ 긴급 속보 & 핫 행사 TOP 3</b>: 실시간 정책 발표 및 선착순 마감 임박 행사\n"
            "▪ <b>💡 1분 입시 팩트체크</b>: 교육부 발표 및 대입 개편안의 실질 득실 (So-What)\n"
            "▪ <b>🎪 설명회·박람회 전체보기</b>: 박람회·대형 설명회·테마 세미나 카테고리별 일정\n"
            "▪ <b>🎓 학년/지역 설정</b>: 중등/고1·2/고3 및 서울·경기·온라인 맞춤 필터링\n\n"
            "🏛 <i>모든 정보는 교육부, 대교협, 교육청 등 100% 공인 출처에 기반합니다.</i>"
        )
        self.channel.send_message(
            chat_id,
            help_text,
            reply_markup=build_persistent_reply_keyboard(),
        )

    def _cmd_events_menu(self, chat_id: str, message_id: Optional[int] = None) -> None:
        """Presents the three event categories (박람회 / 대형설명회 / 테마세미나)."""
        profile = self.db.get_subscriber(chat_id)
        grade = profile.get_active_child().grade if profile else TargetGrade.ALL
        region = profile.get_active_child().region if profile else TargetRegion.ALL
        text = (
            "🎪 <b>설명회·박람회 전체보기</b>\n\n"
            f"🎯 맞춤 필터: <b>{html.escape(grade.label)}</b> · {html.escape(region.label)}\n\n"
            "👇 보고 싶은 행사 유형을 탭해 주세요:"
        )
        markup = build_events_category_keyboard()
        if message_id:
            self._safe_edit_or_send(chat_id, message_id, text, markup)
        else:
            self.channel.send_message(chat_id, text, reply_markup=markup)

    def _show_events_page(self, chat_id: str, message_id: Optional[int], code: str, page_str: str) -> None:
        """Renders one page (3 cards) of a category's upcoming events in place."""
        category = CATEGORY_CODES.get(code)
        if category is None:
            self._cmd_events_menu(chat_id, message_id)
            return
        try:
            page = max(0, int(page_str))
        except ValueError:
            page = 0

        profile = self.db.get_subscriber(chat_id)
        grade = profile.get_active_child().grade if profile else TargetGrade.ALL
        region = profile.get_active_child().region if profile else TargetRegion.ALL

        events, total = self.db.get_events_by_category(
            category, grade, region, page=page, page_size=EVENTS_PAGE_SIZE
        )
        last_page = max(0, (total - 1) // EVENTS_PAGE_SIZE)
        if page > last_page:
            # Stale pager button (events expired since render) → clamp to last page
            page = last_page
            events, total = self.db.get_events_by_category(
                category, grade, region, page=page, page_size=EVENTS_PAGE_SIZE
            )

        text = format_events_page_html(category, events, page, total, grade, region)
        self._safe_edit_or_send(chat_id, message_id, text, build_events_page_keyboard(category, page, total, events))

    def _safe_edit_or_send(
        self,
        chat_id: str,
        message_id: Optional[int],
        text: str,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> None:
        """Edits the message in place; falls back to a new message when editing is impossible.
        ('message is not modified' is treated as success by TelegramChannel.edit_message_text.)
        """
        if message_id and self.channel.edit_message_text(chat_id, message_id, text, reply_markup=reply_markup):
            return
        self.channel.send_message(chat_id, text, reply_markup=reply_markup)

    @staticmethod
    def _callback_toast(data: str) -> str:
        """Computes the callback toast text without touching the DB (so the ack can go out first)."""
        try:
            if data.startswith("grade:"):
                return f"✅ {TargetGrade(data.split(':', 1)[1]).label} 설정 완료!"
            if data.startswith("region:"):
                return f"✅ {TargetRegion(data.split(':', 1)[1]).label} 설정 완료!"
            if data.startswith("adm:sh:"):
                return SHARE_TOAST
            if data.startswith("adm:child:toggle:"):
                # Target child is encoded in the button so the toast needs no DB read
                parts = data.split(":")
                idx, grade = int(parts[3]), TargetGrade(parts[4])
                if 0 <= idx < len(CHILD_ALIASES):
                    return f"✅ {CHILD_ALIASES[idx]} ({grade.label}) 모드로 전환되었습니다!"
            if data == "menu:grade" or data.startswith("info:retarget"):
                return "🎓 학년 선택 메뉴를 불러옵니다."
            if data == "menu:region":
                return "📍 지역 선택 메뉴를 불러옵니다."
            if data == "adm:settings":
                return "⚙️ 맞춤 알림 설정을 불러옵니다."
        except (ValueError, IndexError):
            return "⚠️ 알 수 없는 선택입니다."
        return ""

    def _handle_callback_query(self, cb: dict[str, Any]) -> None:
        """Processes inline button clicks (grade/region selection, event category browsing)."""
        cb_id = cb["id"]
        data = cb.get("data", "")

        # [TG-04] Acknowledge first to clear the loading spinner before any DB work
        toast = self._callback_toast(data)
        if toast:
            self.channel.answer_callback_query(cb_id, text=toast)
        else:
            self.channel.answer_callback_query(cb_id)

        msg = cb.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or cb.get("from", {}).get("id") or "")
        message_id = msg.get("message_id")

        # Ensure subscriber profile exists in DB before updating preferences
        profile = self.db.get_subscriber(chat_id)
        if not profile:
            from_u = cb.get("from") or {}
            profile = ParentUserProfile(
                chat_id=chat_id,
                username=from_u.get("username", "") or chat.get("username", ""),
                first_name=from_u.get("first_name", "") or chat.get("first_name", ""),
                registered_at=datetime.now(),
                last_active_at=datetime.now(),
            )
            self.db.upsert_subscriber(profile)

        # 1. Grade selection
        if data.startswith("grade:"):
            grade_val = data.split(":", 1)[1]
            try:
                target_grade = TargetGrade(grade_val)
                self.db.update_subscriber_grade(chat_id, target_grade)

                next_step_text = (
                    f"✅ <b>[{target_grade.badge}] 학년 설정 완료!</b>\n\n"
                    "👇 <b>[2단계] 희망하시는 설명회 지역 또는 온라인 전용 여부</b>를 선택해 주세요:"
                )
                markup = build_region_inline_keyboard()
                markup["inline_keyboard"].append(
                    [{"text": "📋 변경된 학년 To-Do 바로보기", "callback_data": "adm:todo"}]
                )
                if message_id:
                    self.channel.edit_message_text(
                        chat_id,
                        message_id,
                        next_step_text,
                        reply_markup=markup,
                    )
            except Exception as e:
                logger.error("Failed to update grade: %s", e)
                self.channel.send_message(chat_id, "⚠️ 학년 설정 처리 중 오류가 발생했습니다. 다시 시도해 주세요.")

        # 2. Region selection
        elif data.startswith("region:"):
            region_val = data.split(":", 1)[1]
            try:
                target_region = TargetRegion(region_val)
                self.db.update_subscriber_region(chat_id, target_region)

                profile = self.db.get_subscriber(chat_id)
                grade = profile.get_active_child().grade if profile else TargetGrade.ALL
                final_text = (
                    "🎉 <b>모든 맞춤 설정이 완료되었습니다!</b>\n\n"
                    f"▪ <b>자녀 학년</b>: {html.escape(grade.label)}\n"
                    f"▪ <b>희망 지역</b>: {html.escape(target_region.label)}\n\n"
                    "📋 아래 <b>[정보보기]</b> 버튼 및 자녀 맞춤 <b>이번 주 To-Do</b>를 바로 확인해 보세요 👇"
                )
                region_done_markup = {
                    "inline_keyboard": [
                        [{"text": "📋 직전 발송 맞춤 브리핑(To-Do) 정보보기", "callback_data": "adm:todo"}],
                        [
                            {"text": "⚡ 핫 행사 TOP 3 보기", "callback_data": "adm:hot"},
                            {"text": "🎪 설명회·박람회 전체보기", "callback_data": "adm:menu"},
                        ],
                    ]
                }
                if message_id:
                    self.channel.edit_message_text(
                        chat_id,
                        message_id,
                        final_text,
                        reply_markup=region_done_markup,
                    )
            except Exception as e:
                logger.error("Failed to update region: %s", e)
                self.channel.send_message(chat_id, "⚠️ 지역 설정 처리 중 오류가 발생했습니다. 다시 시도해 주세요.")
            else:
                self._send_welcome_todo(chat_id, grade)
                self.channel.send_message(
                    chat_id,
                    "💡 언제든지 화면 아래의 고정 메뉴 버튼을 탭하여 다른 정보도 확인하실 수 있습니다:",
                    reply_markup=build_persistent_reply_keyboard(),
                )

        # 3. Settings Menu Navigation
        elif data == "menu:grade" or data.startswith("info:retarget"):
            self._cmd_grade(chat_id, message_id)
        elif data == "menu:region":
            self._cmd_region(chat_id, message_id)
        elif data == "adm:settings":
            self._cmd_settings(chat_id, message_id)

        # 3-1. Multi-child switch / add (adm:child:toggle[:<IDX>:<GRADE>], adm:child:add, adm:cadd:<GRADE>[:<SLOT>])
        elif data.startswith("adm:child:toggle"):
            self._toggle_child(chat_id, message_id, data)
        elif data == "adm:child:add":
            self._child_add_menu(chat_id, message_id)
        elif data.startswith("adm:cadd:"):
            self._add_child(chat_id, message_id, data)

        # 4. Welcome / preview shortcuts (manual requests are not logged as dispatches)
        elif data == "adm:hot":
            self._cmd_hot_events(chat_id)
        elif data == "adm:todo":
            self._cmd_todo(chat_id)

        # 5. Event category browsing (adm:menu / adm:cat:<CODE>:<PAGE>)
        elif data == "adm:menu":
            self._cmd_events_menu(chat_id, message_id)
        elif data.startswith("adm:cat:"):
            parts = data.split(":")
            code = parts[2] if len(parts) > 2 else ""
            page_str = parts[3] if len(parts) > 3 else "0"
            self._show_events_page(chat_id, message_id, code, page_str)

        # 6. Glossary (adm:gl:menu / adm:gl:<CODE>) and one-tap Kakao share cards (adm:sh:...)
        elif data == "adm:gl:menu":
            self._cmd_glossary_menu(chat_id, message_id)
        elif data.startswith("adm:gl:"):
            self._show_glossary_entry(chat_id, message_id, data.split(":")[2])
        elif data.startswith("adm:sh:"):
            self._send_share_card(chat_id, data)

    def poll_once(self, offset: int = 0) -> int:
        """Polls updates once and returns new offset."""
        try:
            updates = self.channel.get_updates(offset=offset, timeout=5)
            for upd in updates:
                offset = max(offset, upd["update_id"] + 1)
                self.handle_update(upd)
        except Exception as e:
            logger.warning("Telegram polling error: %s", e)
            time.sleep(1.0)
        return offset
