"""Interactive Telegram Bot Handler for Admission Briefing.
Handles /start, persistent reply keyboards, inline grade/region selection, /todo, /recap, /region,
and natural language keyword matching for effortless mobile touch UX.
"""
from __future__ import annotations
from market_brief.channels.tg_ui_style import NAV_HOME, NAV_PORTAL, TG_DIVIDER
import html
import logging
import time
from datetime import datetime
from typing import Any, Iterable, Optional

from market_brief.channels.http_keepalive import answer_callback_once, callback_ack_scope

from admission_brief.ai_curator import AICurator
from admission_brief.catalog import KnowledgeCatalog, KnowledgeCard
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
    EducationStage,
    GradeLevel,
    ParentUserProfile,
    TargetGrade,
    TargetRegion,
)
from admission_brief.push_engine import ACK_OWNERSHIP_STREAMS
from admission_brief.storage.db import AdmissionDatabase

logger = logging.getLogger(__name__)

EVENTS_PAGE_SIZE = 3
GLOSSARY_COLUMNS = 3
CALLBACK_MAX_BYTES = 64
GUIDE_MENU_TEXT = "📚 학습 가이드"
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


def cb(raw: str) -> str:
    """Validate Telegram callback_data without silently truncating it."""
    if not isinstance(raw, str) or not raw or not raw.isascii() or any(ord(char) < 0x21 or ord(char) >= 0x7F for char in raw):
        raise ValueError("callback_data must be non-empty ASCII")
    if len(raw.encode("utf-8")) > CALLBACK_MAX_BYTES:
        raise ValueError("callback_data must be at most 64 UTF-8 bytes")
    return raw


# Legacy bottom reply-keyboard texts; still matched as text aliases by the message handler.
LEGACY_REPLY_KEYBOARD_TEXTS: tuple[str, ...] = (
    "📋 이번 주 필수 To-Do",
    "⚡ 긴급 속보 & 핫 행사 TOP 3",
    "💡 1분 입시 팩트체크 (So-What)",
    "🎪 설명회·박람회 전체보기",
    "🎓 학년/지역 설정",
    "❓ 이용 도움말",
    GLOSSARY_MENU_TEXT,
    GUIDE_MENU_TEXT,
    SETTINGS_MENU_TEXT,
)


def build_remove_reply_keyboard() -> dict[str, Any]:
    """Removes any legacy bottom reply keyboard; menus are served as inline cards + /commands."""
    return {"remove_keyboard": True}


def build_persistent_reply_keyboard() -> dict[str, Any]:
    """Deprecated: kept for un-updated callers; now returns the remove-keyboard payload."""
    return build_remove_reply_keyboard()


ESSENTIALS_MENU_TEXT = "📅 지금 우리 아이 필수"
EVENTS_MENU_TEXT = "🎪 설명회·모집일정"
KNOWLEDGE_MENU_TEXT = "📖 용어·팩트체크"


def build_main_menu_inline_keyboard() -> dict[str, Any]:
    """3-pillar inline main menu (필수 / 일정 / 지식) + settings & help.

    Legacy callbacks (adm:todo, adm:hot, adm:fact, adm:gl:menu, adm:guide:menu)
    stay routable and are reachable from inside each pillar.
    """
    return {
        "inline_keyboard": [
            [
                {"text": ESSENTIALS_MENU_TEXT, "callback_data": cb("adm:essentials")},
                {"text": EVENTS_MENU_TEXT, "callback_data": cb("adm:menu")},
            ],
            [{"text": KNOWLEDGE_MENU_TEXT, "callback_data": cb("adm:knowledge")}],
            [
                {"text": "⚙️ 내 설정", "callback_data": cb("adm:settings")},
                {"text": "❓ 도움말", "callback_data": cb("adm:help")},
            ],
        ]
    }


def build_essentials_keyboard() -> dict[str, Any]:
    """Quick actions under the active child's To-Do (adm:essentials)."""
    return {
        "inline_keyboard": [
            [
                {"text": "📚 학년별 학습 가이드", "callback_data": cb("adm:guide:menu")},
                {"text": "🔄 학년 변경", "callback_data": cb("menu:grade")},
            ],
            [
                {"text": "📲 카톡 공유", "callback_data": cb("adm:sh:todo")},
                {"text": NAV_HOME, "callback_data": cb("adm:main")},
            ],
        ]
    }


def build_knowledge_keyboard() -> dict[str, Any]:
    """Quick actions under the latest factcheck (adm:knowledge)."""
    return {
        "inline_keyboard": [
            [{"text": GLOSSARY_MENU_TEXT, "callback_data": cb("adm:gl:menu")}],
            [{"text": NAV_HOME, "callback_data": cb("adm:main")}],
        ]
    }


def build_events_category_keyboard(include_back: bool = False) -> dict[str, Any]:
    """Build inline category buttons, optionally including a callback to the main menu."""
    rows = [
            [{"text": f"{EventCategory.EXPO.icon} {EventCategory.EXPO.title}", "callback_data": cb("adm:cat:EXPO:0")}],
            [{"text": f"{EventCategory.BRIEFING.icon} {EventCategory.BRIEFING.title}", "callback_data": cb("adm:cat:BRIEF:0")}],
            [{"text": f"{EventCategory.SEMINAR.icon} {EventCategory.SEMINAR.title}", "callback_data": cb("adm:cat:SEMI:0")}],
            [{"text": "⚡ 핫 행사 TOP 3", "callback_data": cb("adm:hot")}],
    ]
    rows.append(
        [{"text": NAV_HOME, "callback_data": cb("adm:main")}]
        if include_back
        else [{"text": NAV_PORTAL, "url": "https://t.me/KwMenu_bot?start=from_sub"}]
    )
    return {
        "inline_keyboard": rows
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
                rows.append([{"text": f"📲 {idx}번 카톡 공유", "callback_data": cb(share_cb)}])
    nav_row = []
    if page > 0:
        nav_row.append({"text": "◀ 이전", "callback_data": cb(f"adm:cat:{code}:{page - 1}")})
    if page < last_page:
        nav_row.append({"text": "다음 ▶", "callback_data": cb(f"adm:cat:{code}:{page + 1}")})
    if nav_row:
        rows.append(nav_row)
    rows.append([{"text": "🏛 카테고리 목록", "callback_data": cb("adm:menu")}])
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
        TG_DIVIDER,
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
    lines.append(TG_DIVIDER)
    lines.append(f"<i>총 {total}건 · 지난 행사는 자동 제외됩니다</i>")
    return "\n".join(lines)


def build_glossary_menu_keyboard() -> dict[str, Any]:
    """15 terms as a 3-column grid (adm:gl:<CODE>) plus a back-to-categories row."""
    entries = list_glossary_entries()
    rows: list[list[dict[str, Any]]] = [
        [{"text": e.short_name, "callback_data": cb(f"adm:gl:{e.code}")} for e in entries[i:i + GLOSSARY_COLUMNS]]
        for i in range(0, len(entries), GLOSSARY_COLUMNS)
    ]
    rows.append([{"text": "🏛 카테고리 목록", "callback_data": cb("adm:menu")}])
    return {"inline_keyboard": rows}


def build_glossary_detail_keyboard(entry: GlossaryEntry) -> dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "📲 카톡 공유용 요약", "callback_data": cb(f"adm:sh:gl:{entry.code}")}],
            [
                {"text": "📚 다른 용어 보기", "callback_data": cb("adm:gl:menu")},
                {"text": "🏛 카테고리 목록", "callback_data": cb("adm:menu")},
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
        TG_DIVIDER,
        "💡 <b>핵심 개념</b>",
        html.escape(entry.definition),
        "",
        "🎯 <b>학부모 실전 득실</b>",
        html.escape(entry.impact),
        "",
        "⚠️ <b>주의사항 & 꿀팁</b>",
        html.escape(entry.caution),
        TG_DIVIDER,
    ])


def build_grade_inline_keyboard(include_back: bool = False) -> dict[str, Any]:
    """Builds inline keyboard markup for grade selection."""
    rows = [
        [
            {"text": "📘 중등 / 예비고1", "callback_data": cb("grade:MIDDLE")},
        ],
        [
            {"text": "📗 고1 ~ 고2", "callback_data": cb("grade:HIGH_1_2")},
        ],
        [
            {"text": "📕 고3 / N수생", "callback_data": cb("grade:HIGH_3_N")},
        ],
        [
            {"text": "🌐 전학년 공통 알림", "callback_data": cb("grade:ALL")},
        ],
    ]
    if include_back:
        rows.append([{"text": "⚙️ 설정으로 돌아가기", "callback_data": cb("adm:settings")}])
    return {"inline_keyboard": rows}


def build_stage_inline_keyboard(include_back: bool = False) -> dict[str, Any]:
    """First step of the additive stage -> exact grade picker."""
    rows = [
        [{"text": "🌱 초등", "callback_data": cb("adm:stage:ELEMENTARY")}],
        [{"text": "📘 중등", "callback_data": cb("adm:stage:MIDDLE")}],
        [{"text": "📗 고등", "callback_data": cb("adm:stage:HIGH")}],
        [{"text": "🎓 고등 이후 / N수", "callback_data": cb("adm:stage:POST_HIGH")}],
        [{"text": "🌐 전 학년 공통", "callback_data": cb("adm:stage:ALL")}],
        # The legacy choices remain visible and routable for existing subscribers.
        [
            {"text": "기존 분류: 중등", "callback_data": cb("grade:MIDDLE")},
            {"text": "고1~고2", "callback_data": cb("grade:HIGH_1_2")},
            {"text": "고3/N수", "callback_data": cb("grade:HIGH_3_N")},
        ],
        [{"text": "기존 분류: 전체", "callback_data": cb("grade:ALL")}],
    ]
    if include_back:
        rows.append([{"text": "⚙️ 설정으로 돌아가기", "callback_data": cb("adm:settings")}])
    return {"inline_keyboard": rows}


def build_level_inline_keyboard(stage: EducationStage, include_back: bool = False) -> dict[str, Any]:
    """Second step of stage selection; values are short ASCII callback tokens."""
    stage = stage if isinstance(stage, EducationStage) else EducationStage(stage)
    levels = {
        EducationStage.ELEMENTARY: [GradeLevel.E1, GradeLevel.E2, GradeLevel.E3, GradeLevel.E4, GradeLevel.E5, GradeLevel.E6],
        EducationStage.MIDDLE: [GradeLevel.M1, GradeLevel.M2, GradeLevel.M3],
        EducationStage.HIGH: [GradeLevel.H1, GradeLevel.H2, GradeLevel.H3, GradeLevel.N],
        EducationStage.POST_HIGH: [GradeLevel.N],
        EducationStage.ALL: [GradeLevel.ALL],
    }[stage]
    rows = [[{"text": level.label, "callback_data": cb(f"adm:level:{level.value}")}] for level in levels]
    if include_back:
        rows.append([{"text": "◀ 단계 다시 선택", "callback_data": cb("menu:grade")}])
    return {"inline_keyboard": rows}


def build_guide_menu_keyboard(cards: Iterable[KnowledgeCard]) -> dict[str, Any]:
    rows = [[{"text": card.headline[:40], "callback_data": cb(f"adm:guide:{card.content_id}")}] for card in cards]
    rows.append([{"text": NAV_HOME, "callback_data": cb("adm:main")}])
    return {"inline_keyboard": rows}


def format_guide_card_html(card: KnowledgeCard) -> str:
    lines = [
        f"📚 <b>{html.escape(card.headline)}</b>",
        f"<i>{html.escape(card.topic)}</i>",
        TG_DIVIDER,
        *[f"• {html.escape(line)}" for line in card.summary_3lines],
        "",
        f"🎯 <b>그래서 중요한 점</b>\n{html.escape(card.so_what)}",
        "",
        "✅ <b>이번 주 해볼 일</b>",
        *[f"• {html.escape(todo)}" for todo in card.action_todos],
        "",
        f"출처: <a href=\"{html.escape(card.source_url)}\">{html.escape(card.source_name)}</a>",
    ]
    return "\n".join(lines)


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
            [{"text": "📘 중등 / 예비고1", "callback_data": cb(f"adm:cadd:MIDDLE:{slot}")}],
            [{"text": "📗 고1 ~ 고2", "callback_data": cb(f"adm:cadd:HIGH_1_2:{slot}")}],
            [{"text": "📕 고3 / N수생", "callback_data": cb(f"adm:cadd:HIGH_3_N:{slot}")}],
            [{"text": "⚙️ 설정으로 돌아가기", "callback_data": cb("adm:settings")}],
        ]
    }


def build_region_inline_keyboard(include_back: bool = False) -> dict[str, Any]:
    """Builds inline keyboard markup for region selection."""
    rows = [
        [
            {"text": "🌐 온라인/유튜브 라이브만", "callback_data": cb("region:ONLINE")},
        ],
        [
            {"text": "📍 서울 (대치·목동·중계)", "callback_data": cb("region:SEOUL")},
        ],
        [
            {"text": "📍 경기·인천 (분당·일산·평촌)", "callback_data": cb("region:GYEONGGI")},
        ],
        [
            {"text": "📍 비수도권 / 지방 거점", "callback_data": cb("region:LOCAL")},
        ],
        [
            {"text": "🏢 전국 전체 (온·오프라인)", "callback_data": cb("region:ALL")},
        ],
    ]
    if include_back:
        rows.append([{"text": "⚙️ 설정으로 돌아가기", "callback_data": cb("adm:settings")}])
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
        self._pending_stages: dict[str, EducationStage] = {}

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

        # Create once, then refresh metadata without overwriting preferences.
        profile = self.db.get_subscriber(chat_id)
        if not profile:
            profile = ParentUserProfile(
                chat_id=chat_id,
                username=username,
                first_name=first_name,
                registered_at=datetime.now(),
                last_active_at=datetime.now(),
            )
            self.db.create_subscriber_if_absent(profile)
        else:
            self.db.touch_subscriber_metadata(chat_id, username=username, first_name=first_name)

        # Route commands & friendly Korean natural text touch buttons
        clean_text = text.strip()
        if clean_text.startswith("/start"):
            self._cmd_start(chat_id, first_name)
        elif clean_text.startswith("/"):
            self._route_slash_command(chat_id, clean_text, first_name)
        elif clean_text == ESSENTIALS_MENU_TEXT:
            self._cmd_essentials(chat_id)
        elif clean_text == KNOWLEDGE_MENU_TEXT:
            self._cmd_knowledge(chat_id)
        elif clean_text == GLOSSARY_MENU_TEXT or clean_text.startswith("/glossary") or "용어" in clean_text:
            self._cmd_glossary_menu(chat_id)
        elif clean_text == GUIDE_MENU_TEXT or clean_text.startswith("/guide") or "학습가이드" in clean_text or clean_text == "가이드":
            self._cmd_guide_menu(chat_id)
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
            self._send_main_menu_fallback(chat_id)

    def _send_main_menu_fallback(self, chat_id: str) -> None:
        """Fallback friendly response with the inline main menu card."""
        self.channel.send_message(
            chat_id,
            "안녕하세요! 에듀브리프(EduBrief) 입시 비서입니다.\n"
            "타자를 치실 필요 없이, 아래 메뉴 버튼을 손가락으로 탭해 주세요! 👇",
            reply_markup=build_main_menu_inline_keyboard(),
        )

    @staticmethod
    def _parse_command(text: str) -> str:
        """'/Todo@EduBot arg' -> 'todo' (exact token; @BotName suffix and args stripped)."""
        token = text.strip().split(maxsplit=1)[0] if text.strip() else ""
        if not token.startswith("/"):
            return ""
        return token[1:].split("@", 1)[0].lower()

    def _route_slash_command(self, chat_id: str, text: str, first_name: str) -> None:
        """Routes an exact command token; unknown commands get the main menu card."""
        command = self._parse_command(text)
        if command == "start":
            self._cmd_start(chat_id, first_name)
            return
        handlers = {
            "todo": self._cmd_todo,
            # Retired /briefing (still cached in some Telegram clients) -> essentials pillar.
            "briefing": self._cmd_essentials,
            "essentials": self._cmd_essentials,
            "knowledge": self._cmd_knowledge,
            "events": self._cmd_events_menu,
            "guide": self._cmd_guide_menu,
            "glossary": self._cmd_glossary_menu,
            "settings": self._cmd_settings,
            "help": self._cmd_help,
            "hot": self._cmd_hot_events,
            "recap": self._cmd_hot_events,
            "fact": self._cmd_factcheck,
            "grade": self._cmd_grade,
            "region": self._cmd_region,
        }
        handler = handlers.get(command)
        if handler is None:
            self._send_main_menu_fallback(chat_id)
        else:
            handler(chat_id)

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
        """Presents additive stage -> grade selection, with legacy choices retained."""
        text = "변경하실 <b>자녀분의 현재 학년</b>을 선택해 주세요:"
        markup = build_stage_inline_keyboard(include_back=bool(message_id))
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
            f"• <b>주간 가이드:</b> {'켜짐' if profile is None or profile.weekly_guide_enabled else '꺼짐'}",
            f"• <b>행사 D-Day:</b> {'켜짐' if profile is None or profile.event_dday_enabled else '꺼짐'}",
            f"• <b>정책·긴급:</b> {'켜짐' if profile is None or profile.policy_breaking_enabled else '꺼짐'}",
            "",
            "변경을 원하시는 항목의 버튼을 선택해 주세요:",
        ])
        rows: list[list[dict[str, Any]]] = [
            [
                {"text": "🎓 학년 변경하기", "callback_data": cb("menu:grade")},
                {"text": "📍 지역/온라인 변경하기", "callback_data": cb("menu:region")},
            ],
            [
                {"text": "📋 주간 가이드 켜기/끄기", "callback_data": cb("adm:pref:weekly")},
                {"text": "⏰ 행사 D-Day 켜기/끄기", "callback_data": cb("adm:pref:dday")},
            ],
            [{"text": "🚨 정책·긴급 알림 켜기/끄기", "callback_data": cb("adm:pref:policy")}],
        ]
        children = profile.children if profile else []
        if len(children) >= 2:
            nxt_idx = (profile.active_child_index + 1) % len(children)
            rows.append([{
                "text": f"👦 {active.alias} ⇄ 👧 {children[nxt_idx].alias} 전환",
                "callback_data": cb(f"adm:child:toggle:{nxt_idx}:{children[nxt_idx].grade.value}"),
            }])
        if len(children) < MAX_CHILDREN:
            rows.append([{"text": "➕ 자녀 추가하기", "callback_data": cb("adm:child:add")}])
        markup = {"inline_keyboard": rows}
        text = "\n".join(lines)
        if message_id:
            self._safe_edit_or_send(chat_id, message_id, text, markup)
        else:
            self.channel.send_message(chat_id, text, reply_markup=markup)

    def _cmd_guide_menu(self, chat_id: str, message_id: Optional[int] = None) -> None:
        """Shows pull-based, profile-matched guide cards from the read-only catalog."""
        profile = self.db.get_subscriber(chat_id)
        active = profile.get_active_child() if profile else ChildProfile()
        try:
            cards = self.curator.catalog.filter(
                active.education_stage, active.grade_level, card_type="STUDY_GUIDE"
            )
        except Exception as exc:
            logger.warning("Guide catalog unavailable: %s", exc)
            cards = ()
        if not cards:
            text = "📚 <b>학습 가이드</b>\n\n현재 자녀 설정에 맞는 가이드가 없습니다. 설정에서 학년을 확인해 주세요."
            markup = {"inline_keyboard": [[{"text": "⚙️ 자녀·맞춤 설정", "callback_data": cb("adm:settings")}]]}
        else:
            text = (
                "📚 <b>학습 가이드</b>\n\n"
                f"현재 설정: <b>{html.escape(active.education_stage.label)} · {html.escape(active.grade_level.label)}</b>\n"
                "광고 없이, 지금 시기에 필요한 카드만 골라 보세요."
            )
            markup = build_guide_menu_keyboard(cards)
        if message_id:
            self._safe_edit_or_send(chat_id, message_id, text, markup)
        else:
            self.channel.send_message(chat_id, text, reply_markup=markup)

    def _show_guide_card(self, chat_id: str, message_id: Optional[int], content_id: str) -> None:
        card = self.curator.catalog.get(content_id)
        profile = self.db.get_subscriber(chat_id)
        active = profile.get_active_child() if profile else ChildProfile()
        try:
            visible = card is not None and any(
                item.content_id == content_id for item in self.curator.catalog.filter(
                    active.education_stage, active.grade_level, card_type=card.card_type if card else None
                )
            )
        except Exception:
            visible = False
        if card is None or card.status != "PUBLISHED" or not visible:
            self._cmd_guide_menu(chat_id, message_id)
            return
        self._safe_edit_or_send(
            chat_id, message_id, format_guide_card_html(card),
            {"inline_keyboard": [
                [{"text": "📚 다른 가이드 보기", "callback_data": cb("adm:guide:menu")}],
                [{"text": NAV_HOME, "callback_data": cb("adm:main")}],
            ]},
        )

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

    def _active_todo_briefing(self, chat_id: str) -> AdmissionBriefing:
        """The active child's grade-specific weekly To-Do briefing (silent)."""
        profile = self.db.get_subscriber(chat_id)
        grade = profile.get_active_child().grade if profile else TargetGrade.MIDDLE
        active = profile.get_active_child() if profile else ChildProfile(grade=grade)
        briefing = self.curator.generate_weekly_todo_briefing(
            grade, education_stage=active.education_stage, grade_level=active.grade_level
        )
        if profile and len(profile.children) >= 2:
            briefing.headline = f"[{profile.get_active_child().alias}] {briefing.headline}"
        briefing.silent = True  # Silent push for regular digests
        return briefing

    def _cmd_todo(self, chat_id: str) -> None:
        """Sends the active child's grade-specific weekly action To-Do list."""
        share_markup = {"inline_keyboard": [[{"text": "📲 카톡 공유", "callback_data": cb("adm:sh:todo")}]]}
        self.channel.send(chat_id, self._active_todo_briefing(chat_id), reply_markup=share_markup)

    def _cmd_essentials(self, chat_id: str) -> None:
        """Pillar 1 (adm:essentials): active child's To-Do + guide / grade-change shortcuts."""
        self.channel.send(chat_id, self._active_todo_briefing(chat_id), reply_markup=build_essentials_keyboard())

    def _cmd_knowledge(self, chat_id: str) -> None:
        """Pillar 3 (adm:knowledge): latest verified factcheck + glossary shortcut."""
        self.channel.send(chat_id, self._factcheck_briefing(chat_id), reply_markup=build_knowledge_keyboard())

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
                    {"text": "⚡ 핫 행사 TOP 3", "callback_data": cb("adm:hot")},
                    {"text": "🎪 설명회·박람회 전체보기", "callback_data": cb("adm:menu")},
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

    def _factcheck_briefing(self, chat_id: str) -> AdmissionBriefing:
        profile = self.db.get_subscriber(chat_id)
        grade = profile.get_active_child().grade if profile else TargetGrade.ALL
        active = profile.get_active_child() if profile else ChildProfile(grade=grade)
        briefing = self.curator.get_latest_factcheck(
            grade, education_stage=active.education_stage, grade_level=active.grade_level
        )
        briefing.silent = True
        return briefing

    def _cmd_factcheck(self, chat_id: str) -> None:
        """Presents the latest verified 1-minute admission policy factcheck."""
        self.channel.send(chat_id, self._factcheck_briefing(chat_id))

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
            self.channel.send_message(
                chat_id,
                "📭 현재 등록된 최신 설명회·티켓팅 정보가 없습니다.\n"
                "공식 행사 정보가 수집되면 이 메뉴에서 바로 확인하실 수 있습니다.",
                reply_markup=build_main_menu_inline_keyboard(),
            )

    def _cmd_help(self, chat_id: str) -> None:
        """Presents help and official channel links with the inline main menu card."""
        help_text = (
            "📌 <b>에듀브리프 (EduBrief) 간편 사용 안내</b>\n\n"
            "타자를 치실 필요 없이, 아래 메뉴 버튼을 손가락으로 누르시면 됩니다!\n\n"
            "▪ <b>📅 지금 우리 아이 필수</b>: 자녀 학년별 이번 주 To-Do + 학년별 학습 가이드\n"
            "▪ <b>🎪 설명회·모집일정</b>: 박람회·대형 설명회·테마 세미나 일정 + 핫 행사 TOP 3\n"
            "▪ <b>📖 용어·팩트체크</b>: 최신 입시 팩트체크(So-What) + 1초 입시용어 사전\n"
            "▪ <b>⚙️ 내 설정</b>: 자녀 학년·지역, 알림 켜기/끄기, 자녀 추가·전환\n\n"
            "🏛 <i>모든 정보는 교육부, 대교협, 교육청 등 100% 공인 출처에 기반합니다.</i>"
        )
        self.channel.send_message(
            chat_id,
            help_text,
            reply_markup=build_main_menu_inline_keyboard(),
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
        markup = build_events_category_keyboard(include_back=True)
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
            if data.startswith("adm:stage:"):
                return "🎓 학년 단계를 선택했습니다. 세부 학년을 골라 주세요."
            if data.startswith("adm:level:"):
                return "✅ 세부 학년을 선택했습니다. 지역을 골라 주세요."
            if data == "adm:essentials":
                return "📅 우리 아이 필수 To-Do를 불러옵니다."
            if data == "adm:knowledge":
                return "📖 최신 입시 팩트체크를 불러옵니다."
            if data == "adm:guide:menu":
                return "📚 학습 가이드를 불러옵니다."
            if data.startswith("adm:guide:"):
                return "📚 학습 가이드를 엽니다."
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
            if data.startswith("adm:pref:"):
                return "✅ 맞춤 알림 설정을 변경했습니다."
            if data.startswith("adm:ack:"):
                return "✅ 확인했습니다."
        except (ValueError, IndexError):
            return "⚠️ 알 수 없는 선택입니다."
        return ""

    def _handle_callback_query(self, cb: dict[str, Any]) -> None:
        """Processes inline button clicks (grade/region selection, event category browsing).
        Single-ack scope: joins the manager's scope when dispatched by `DynamicBotManager`."""
        with callback_ack_scope(cb.get("id", "")):
            self._handle_callback_query_scoped(cb)

    def _handle_callback_query_scoped(self, callback: dict[str, Any]) -> None:
        cb_id = callback["id"]
        data = callback.get("data", "")

        # [TG-04] Acknowledge first to clear the loading spinner before any DB work (exactly once)
        toast = self._callback_toast(data)
        if toast:
            answer_callback_once(cb_id, lambda: self.channel.answer_callback_query(cb_id, text=toast))
        else:
            answer_callback_once(cb_id, lambda: self.channel.answer_callback_query(cb_id))

        msg = callback.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id") or callback.get("from", {}).get("id") or "")
        message_id = msg.get("message_id")

        # A callback received from a different Telegram user must never be able
        # to mutate the owner's acknowledgement or preferences.
        from_id = str((callback.get("from") or {}).get("id") or "")
        if from_id and chat.get("id") is not None and from_id != str(chat.get("id")):
            return

        # Ensure subscriber profile exists in DB before updating preferences
        profile = self.db.get_subscriber(chat_id)
        if not profile:
            from_u = callback.get("from") or {}
            profile = ParentUserProfile(
                chat_id=chat_id,
                username=from_u.get("username", "") or chat.get("username", ""),
                first_name=from_u.get("first_name", "") or chat.get("first_name", ""),
                registered_at=datetime.now(),
                last_active_at=datetime.now(),
            )
            self.db.create_subscriber_if_absent(profile)

        # 1. Additive stage -> exact grade selection. Pending stage is local and
        # short-lived; level callbacks also infer their stage for stateless clients.
        if data.startswith("adm:stage:"):
            try:
                stage = EducationStage(data.split(":", 2)[2])
                self._pending_stages[chat_id] = stage
                self._safe_edit_or_send(
                    chat_id, message_id,
                    f"🎓 <b>{html.escape(stage.label)}</b> 세부 학년을 선택해 주세요:",
                    build_level_inline_keyboard(stage, include_back=True),
                )
            except (ValueError, IndexError):
                self._cmd_grade(chat_id, message_id)

        elif data.startswith("adm:level:"):
            try:
                level = GradeLevel(data.split(":", 2)[2])
                stage = self._pending_stages.pop(chat_id, None)
                if stage is None:
                    stage = (
                        EducationStage.ELEMENTARY if level.value.startswith("E")
                        else EducationStage.MIDDLE if level.value.startswith("M")
                        else EducationStage.HIGH if level in (GradeLevel.H1, GradeLevel.H2, GradeLevel.H3)
                        else EducationStage.POST_HIGH if level == GradeLevel.N
                        else EducationStage.ALL
                    )
                if not self.db.update_subscriber_stage_level(chat_id, stage, level):
                    raise ValueError("subscriber stage update failed")
                next_step_text = (
                    f"✅ <b>{html.escape(stage.label)} · {html.escape(level.label)}</b> 설정 완료!\n\n"
                    "👇 희망하시는 설명회 지역 또는 온라인 전용 여부를 선택해 주세요:"
                )
                markup = build_region_inline_keyboard()
                markup["inline_keyboard"].append([{"text": "📋 변경된 학년 To-Do 바로보기", "callback_data": cb("adm:todo")}])
                self._safe_edit_or_send(chat_id, message_id, next_step_text, markup)
            except (ValueError, IndexError):
                self._cmd_grade(chat_id, message_id)

        # 2. Legacy grade selection
        elif data.startswith("grade:"):
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
                    [{"text": "📋 변경된 학년 To-Do 바로보기", "callback_data": cb("adm:todo")}]
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

        # 3. Region selection
        elif data.startswith("region:"):
            region_val = data.split(":", 1)[1]
            try:
                target_region = TargetRegion(region_val)
                self.db.update_subscriber_region(chat_id, target_region)

                profile = self.db.get_subscriber(chat_id)
                grade = profile.get_active_child().grade if profile else TargetGrade.ALL
                final_text = (
                    "🎉 <b>모든 맞춤 설정이 완료되었습니다!</b>\n\n"
                    f"• <b>자녀 학년</b>: {html.escape(grade.label)}\n"
                    f"• <b>희망 지역</b>: {html.escape(target_region.label)}\n\n"
                    "📋 아래 <b>[정보보기]</b> 버튼 및 자녀 맞춤 <b>이번 주 To-Do</b>를 바로 확인해 보세요 👇"
                )
                region_done_markup = {
                    "inline_keyboard": [
                        [{"text": "📋 직전 발송 맞춤 브리핑(To-Do) 정보보기", "callback_data": cb("adm:todo")}],
                        [
                            {"text": "⚡ 핫 행사 TOP 3 보기", "callback_data": cb("adm:hot")},
                            {"text": "🎪 설명회·박람회 전체보기", "callback_data": cb("adm:menu")},
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
                    "💡 언제든지 /help 를 입력하시면 메인 메뉴 카드를 다시 보실 수 있습니다.",
                    reply_markup=build_remove_reply_keyboard(),
                )

        # 4. Settings Menu Navigation
        elif data == "menu:grade" or data.startswith("info:retarget"):
            self._cmd_grade(chat_id, message_id)
        elif data == "menu:region":
            self._cmd_region(chat_id, message_id)
        elif data == "adm:settings":
            self._cmd_settings(chat_id, message_id)

        # 4-0. Personalized alert preferences (ACK-first above, DB write here)
        elif data.startswith("adm:pref:"):
            preference = data.split(":", 2)[2]
            profile = self.db.get_subscriber(chat_id)
            if profile is None or preference not in {"weekly", "dday", "policy"}:
                self._cmd_settings(chat_id, message_id, notice="⚠️ 설정을 찾을 수 없습니다.")
            else:
                kwargs = {
                    "weekly": {"weekly": not profile.weekly_guide_enabled},
                    "dday": {"dday": not profile.event_dday_enabled},
                    "policy": {"policy": not profile.policy_breaking_enabled},
                }[preference]
                self.db.update_alert_preferences(chat_id, **kwargs)
                self._cmd_settings(chat_id, message_id, notice="✅ 맞춤 알림 설정이 변경되었습니다.")

        # 4-1. Multi-child switch / add (adm:child:toggle[:<IDX>:<GRADE>], adm:child:add, adm:cadd:<GRADE>[:<SLOT>])
        elif data.startswith("adm:child:toggle"):
            self._toggle_child(chat_id, message_id, data)
        elif data == "adm:child:add":
            self._child_add_menu(chat_id, message_id)
        elif data.startswith("adm:cadd:"):
            self._add_child(chat_id, message_id, data)

        # 5. Welcome / preview shortcuts (manual requests are not logged as dispatches)
        elif data == "adm:essentials":
            self._cmd_essentials(chat_id)
        elif data == "adm:knowledge":
            self._cmd_knowledge(chat_id)
        elif data == "adm:hot":
            self._cmd_hot_events(chat_id)
        elif data == "adm:todo":
            self._cmd_todo(chat_id)
        elif data == "adm:fact":
            self._cmd_factcheck(chat_id)
        elif data == "adm:help":
            self._cmd_help(chat_id)
        elif data.startswith("adm:ack:"):
            content_id = data.split(":", 2)[2]
            valid_id = bool(content_id) and content_id.isascii() and len(content_id) <= 24 and all(
                char.isalnum() or char == "-" for char in content_id
            )
            # Strict (no cooldown) history across every stream incl. dday:D3/dday:D1.
            owned = any(
                self.db.has_content_been_sent(chat_id, content_id, stream)
                for stream in ACK_OWNERSHIP_STREAMS
            ) if valid_id else False
            card = self.curator.catalog.get(content_id) if owned else None
            version = getattr(card, "version", 1) if card is not None else 1
            if owned and self.db.record_card_ack(chat_id, content_id, version=version):
                self._safe_edit_or_send(
                    chat_id, message_id,
                    "✅ <b>확인 완료</b>\n\n이 맞춤 알림을 확인 목록에 기록했습니다.",
                    {"inline_keyboard": [[{"text": NAV_HOME, "callback_data": cb("adm:main")}]]},
                )
            else:
                self._safe_edit_or_send(
                    chat_id, message_id,
                    "⚠️ 이 알림은 이미 처리되었거나 확인 권한이 없습니다.",
                    {"inline_keyboard": [[{"text": NAV_HOME, "callback_data": cb("adm:main")}]]},
                )
        elif data == "adm:guide:menu":
            self._cmd_guide_menu(chat_id, message_id)
        elif data.startswith("adm:guide:"):
            self._show_guide_card(chat_id, message_id, data.split(":", 2)[2])

        # 6. Event category browsing (adm:menu / adm:cat:<CODE>:<PAGE>)
        elif data == "adm:menu":
            self._cmd_events_menu(chat_id, message_id)
        elif data == "adm:main":
            self._safe_edit_or_send(chat_id, message_id, "🎓 에듀브리프 메인 메뉴입니다.", build_main_menu_inline_keyboard())
        elif data.startswith("adm:cat:"):
            parts = data.split(":")
            code = parts[2] if len(parts) > 2 else ""
            page_str = parts[3] if len(parts) > 3 else "0"
            self._show_events_page(chat_id, message_id, code, page_str)

        # 7. Glossary (adm:gl:menu / adm:gl:<CODE>) and one-tap Kakao share cards (adm:sh:...)
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
