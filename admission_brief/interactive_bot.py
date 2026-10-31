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
from admission_brief.channels.telegram import TelegramChannel
from admission_brief.models import (
    AdmissionBriefing,
    ParentUserProfile,
    TargetGrade,
    TargetRegion,
)
from admission_brief.storage.db import AdmissionDatabase

logger = logging.getLogger(__name__)


def build_persistent_reply_keyboard() -> dict[str, Any]:
    """Builds persistent bottom touch keyboard buttons that stay on parent's phone screen."""
    return {
        "keyboard": [
            [
                {"text": "📋 이번 주 필수 To-Do"},
                {"text": "📢 설명회 3줄 요약"},
            ],
            [
                {"text": "🎓 학년 변경"},
                {"text": "📍 지역/온라인 설정"},
            ],
            [
                {"text": "❓ 이용 도움말"},
            ],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


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
        elif clean_text.startswith("/grade") or "학년" in clean_text:
            self._cmd_grade(chat_id)
        elif clean_text.startswith("/region") or "지역" in clean_text or "온라인" in clean_text:
            self._cmd_region(chat_id)
        elif clean_text.startswith("/todo") or "할일" in clean_text or "투두" in clean_text or "이번 주" in clean_text or "체크리스트" in clean_text:
            self._cmd_todo(chat_id)
        elif clean_text.startswith("/recap") or "요약" in clean_text or "설명회" in clean_text or "리캡" in clean_text:
            self._cmd_recap(chat_id)
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
            "<b>내 아이에게 딱 맞는 핵심 팩트와 선착순 티켓팅 알림</b>만 엄선해 드립니다.\n\n"
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

    def _cmd_todo(self, chat_id: str) -> None:
        """Sends grade-specific weekly action To-Do list."""
        profile = self.db.get_subscriber(chat_id)
        grade = profile.target_grade if profile else TargetGrade.MIDDLE
        briefing = self.curator.generate_weekly_todo_briefing(grade)
        briefing.silent = True  # Silent push for regular digests
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
                briefing_type=self.curator.generate_weekly_todo_briefing(grade).briefing_type,
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
                silent=True,
            )
            self.channel.send(chat_id, sample_briefing)

    def _cmd_help(self, chat_id: str) -> None:
        """Presents help and official channel links with persistent touch menu."""
        help_text = (
            "📌 <b>에듀브리프 (EduBrief) 간편 사용 안내</b>\n\n"
            "타자를 치실 필요 없이, **화면 하단의 메뉴 버튼**을 손가락으로 누르시면 됩니다!\n\n"
            "▪ <b>📋 이번 주 필수 To-Do</b>: 우리 아이 학년의 이번 주 꼭 해야 할 일 3가지\n"
            "▪ <b>📢 설명회 3줄 요약</b>: 어제 열린 대형 설명회 핵심 요약 모아보기\n"
            "▪ <b>🎓 학년 변경</b>: 중등/고1·2/고3 설정 변경\n"
            "▪ <b>📍 지역/온라인 설정</b>: 온라인 라이브만 받기 / 서울·경기 설정\n\n"
            "📢 <i>공식 채널: @EduBrief_Official</i>"
        )
        self.channel.send_message(
            chat_id,
            help_text,
            reply_markup=build_persistent_reply_keyboard(),
        )

    def _handle_callback_query(self, cb: dict[str, Any]) -> None:
        """Processes inline button clicks (grade and region selection)."""
        cb_id = cb["id"]
        data = cb.get("data", "")
        msg = cb.get("message", {})
        chat = msg.get("chat", {})
        chat_id = str(chat.get("id"))
        message_id = msg.get("message_id")

        # 1. Grade selection
        if data.startswith("grade:"):
            grade_val = data.split(":", 1)[1]
            try:
                target_grade = TargetGrade(grade_val)
                self.db.update_subscriber_grade(chat_id, target_grade)

                self.channel.answer_callback_query(cb_id, text=f"✅ {target_grade.label} 설정 완료!")

                next_step_text = (
                    f"✅ <b>[{target_grade.badge}] 학년 설정 완료!</b>\n\n"
                    "👇 <b>[2단계] 희망하시는 설명회 지역 또는 온라인 전용 여부</b>를 선택해 주세요:"
                )
                if message_id:
                    self.channel.edit_message_text(
                        chat_id,
                        message_id,
                        next_step_text,
                        reply_markup=build_region_inline_keyboard(),
                    )
            except Exception as e:
                logger.error("Failed to update grade: %s", e)
                self.channel.answer_callback_query(cb_id, text="설정 처리 중 오류가 발생했습니다.")

        # 2. Region selection
        elif data.startswith("region:"):
            region_val = data.split(":", 1)[1]
            try:
                target_region = TargetRegion(region_val)
                self.db.update_subscriber_region(chat_id, target_region)

                self.channel.answer_callback_query(cb_id, text=f"✅ {target_region.label} 설정 완료!")

                profile = self.db.get_subscriber(chat_id)
                grade_label = profile.target_grade.label if profile else "전학년"
                final_text = (
                    "🎉 <b>모든 맞춤 설정이 완료되었습니다!</b>\n\n"
                    f"▪ <b>자녀 학년</b>: {grade_label}\n"
                    f"▪ <b>희망 지역</b>: {target_region.label}\n\n"
                    "이제 화면 아래에 나타난 <b>[📋 이번 주 필수 To-Do]</b> 버튼을 탭해 보세요! 👇"
                )
                if message_id:
                    self.channel.edit_message_text(chat_id, message_id, final_text)

                # Send persistent bottom menu to user's chat!
                self.channel.send_message(
                    chat_id,
                    "💡 화면 아래의 고정 메뉴 버튼을 탭하시면いつでも 편하게 이용하실 수 있습니다:",
                    reply_markup=build_persistent_reply_keyboard(),
                )
            except Exception as e:
                logger.error("Failed to update region: %s", e)
                self.channel.answer_callback_query(cb_id, text="지역 설정 중 오류가 발생했습니다.")

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
