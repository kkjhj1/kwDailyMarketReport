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
from admission_brief.channels.telegram import TelegramChannel, _is_valid_url
from admission_brief.models import (
    AdmissionBriefing,
    AdmissionEvent,
    BriefingType,
    EventCategory,
    ParentUserProfile,
    TargetGrade,
    TargetRegion,
)
from admission_brief.storage.db import AdmissionDatabase

logger = logging.getLogger(__name__)

EVENTS_PAGE_SIZE = 3

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
        ]
    }


def build_events_page_keyboard(category: EventCategory, page: int, total: int) -> dict[str, Any]:
    """Builds prev/next pager and back-to-categories buttons for an event list page."""
    code = CATEGORY_TO_CODE[category]
    last_page = max(0, (total - 1) // EVENTS_PAGE_SIZE)
    nav_row = []
    if page > 0:
        nav_row.append({"text": "◀ 이전", "callback_data": f"adm:cat:{code}:{page - 1}"})
    if page < last_page:
        nav_row.append({"text": "다음 ▶", "callback_data": f"adm:cat:{code}:{page + 1}"})
    rows = [nav_row] if nav_row else []
    rows.append([{"text": "🏛 카테고리 목록", "callback_data": "adm:menu"}])
    return {"inline_keyboard": rows}


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
        lines.append(f"🗓 <b>{html.escape(ev.event_date_str)}</b>")
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


def build_grade_inline_keyboard() -> dict[str, Any]:
    """Builds inline keyboard markup for grade selection."""
    return {
        "inline_keyboard": [
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
    }


def build_region_inline_keyboard() -> dict[str, Any]:
    """Builds inline keyboard markup for region selection."""
    return {
        "inline_keyboard": [
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
    }


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

    def _cmd_grade(self, chat_id: str) -> None:
        """Presents grade selection inline keyboard."""
        self.channel.send_message(
            chat_id,
            "변경하실 <b>자녀분의 현재 학년</b>을 선택해 주세요:",
            reply_markup=build_grade_inline_keyboard(),
        )

    def _cmd_region(self, chat_id: str) -> None:
        """Presents region selection inline keyboard."""
        self.channel.send_message(
            chat_id,
            "희망하시는 <b>설명회 참여 지역 또는 온라인 전용 여부</b>를 선택해 주세요:",
            reply_markup=build_region_inline_keyboard(),
        )

    def _cmd_settings(self, chat_id: str) -> None:
        """Presents grade and region configuration menu."""
        profile = self.db.get_subscriber(chat_id)
        current_grade = profile.target_grade.label if profile else "미설정"
        current_region = profile.target_region.label if profile else "전국 전체"
        text = (
            f"⚙️ <b>맞춤 알림 설정 현황</b>\n\n"
            f"• <b>자녀 학년:</b> {current_grade}\n"
            f"• <b>관심 지역:</b> {current_region}\n\n"
            f"변경을 원하시는 항목의 버튼을 선택해 주세요:"
        )
        markup = {
            "inline_keyboard": [
                [
                    {"text": "🎓 학년 변경하기", "callback_data": "menu:grade"},
                    {"text": "📍 지역/온라인 변경하기", "callback_data": "menu:region"},
                ]
            ]
        }
        self.channel.send_message(chat_id, text, reply_markup=markup)

    def _cmd_todo(self, chat_id: str) -> None:
        """Sends grade-specific weekly action To-Do list."""
        profile = self.db.get_subscriber(chat_id)
        grade = profile.target_grade if profile else TargetGrade.MIDDLE
        briefing = self.curator.generate_weekly_todo_briefing(grade)
        briefing.silent = True  # Silent push for regular digests
        self.channel.send(chat_id, briefing)

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
        grade = profile.target_grade if profile else TargetGrade.ALL
        region = profile.target_region if profile else TargetRegion.ALL

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
        grade = profile.target_grade if profile else TargetGrade.ALL
        briefing = self.curator.get_latest_factcheck(grade)
        briefing.silent = True
        self.channel.send(chat_id, briefing)

    def _cmd_recap(self, chat_id: str) -> None:
        """Presents the latest admission seminar recap."""
        profile = self.db.get_subscriber(chat_id)
        grade = profile.target_grade if profile else TargetGrade.ALL

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
        grade = profile.target_grade if profile else TargetGrade.ALL
        region = profile.target_region if profile else TargetRegion.ALL
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
        grade = profile.target_grade if profile else TargetGrade.ALL
        region = profile.target_region if profile else TargetRegion.ALL

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
        self._safe_edit_or_send(chat_id, message_id, text, build_events_page_keyboard(category, page, total))

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
        except ValueError:
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
                grade = profile.target_grade if profile else TargetGrade.ALL
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
        elif data == "menu:grade":
            self._cmd_grade(chat_id)
        elif data == "menu:region":
            self._cmd_region(chat_id)

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
