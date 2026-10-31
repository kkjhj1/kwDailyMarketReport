"""Domain models for Admission Briefing (입시정보 브리핑).
Handles target grades, regions, briefing types, event metadata, and parent subscriptions.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Optional


class TargetGrade(str, Enum):
    """Target student grades for parents."""
    MIDDLE = "MIDDLE"           # 중1 ~ 중3 (특목·자사고 / 고교학점제)
    HIGH_1_2 = "HIGH_1_2"       # 고1 ~ 고2 (내신 방어 / 세특 / 모의고사)
    HIGH_3_N = "HIGH_3_N"       # 고3 / N수생 (수시 6장 / 정시 배치표 실전)
    ALL = "ALL"                 # 전 학년 공통 (교육 정책 긴급 발표 등)

    @property
    def label(self) -> str:
        labels = {
            "MIDDLE": "중등 / 예비고1",
            "HIGH_1_2": "고1 ~ 고2",
            "HIGH_3_N": "고3 / N수생",
            "ALL": "전 학년 공통",
        }
        return labels.get(self.value, self.value)

    @property
    def badge(self) -> str:
        badges = {
            "MIDDLE": "📘 [중등/예비고1]",
            "HIGH_1_2": "📗 [고1~고2]",
            "HIGH_3_N": "📕 [고3/수험생]",
            "ALL": "🌐 [전학년 공통]",
        }
        return badges.get(self.value, "📌")


class TargetRegion(str, Enum):
    """Target region for admission briefings."""
    ALL = "ALL"              # 전국 전체 (지역 무관)
    ONLINE = "ONLINE"        # 온라인 / 유튜브 라이브 전용 (비대면)
    SEOUL = "SEOUL"          # 서울 (대치·목동·중계 등)
    GYEONGGI = "GYEONGGI"    # 경기·인천 (분당·일산·평촌 등)
    LOCAL = "LOCAL"          # 비수도권 / 지방 거점

    @property
    def label(self) -> str:
        labels = {
            "ALL": "전국 (온·오프라인 전체)",
            "ONLINE": "🌐 온라인/유튜브 라이브만",
            "SEOUL": "📍 서울 (대치·목동·중계)",
            "GYEONGGI": "📍 경기·인천 (분당·일산·평촌)",
            "LOCAL": "📍 비수도권 / 지방",
        }
        return labels.get(self.value, self.value)

    @property
    def badge(self) -> str:
        badges = {
            "ALL": "🌐 [전국]",
            "ONLINE": "💻 [온라인 라이브]",
            "SEOUL": "🏙️ [서울/대치]",
            "GYEONGGI": "🏢 [경기/분당]",
            "LOCAL": "🌄 [지방 거점]",
        }
        return badges.get(self.value, "📍")


class BriefingType(str, Enum):
    """Categorization of admission briefing content."""
    WEEKLY_TODO = "WEEKLY_TODO"           # 위클리 액션플랜 & 캘린더
    BRIEFING_RECAP = "BRIEFING_RECAP"     # 설명회 대리참석 3줄 요약
    TICKETING_ALERT = "TICKETING_ALERT"   # 선착순 예약 마감 5분 전 긴급 직링크
    POLICY_TRANSLATE = "POLICY_TRANSLATE" # 교육부 정책 팩트 번역 (득실 분석)

    @property
    def icon(self) -> str:
        icons = {
            "WEEKLY_TODO": "📋",
            "BRIEFING_RECAP": "📢",
            "TICKETING_ALERT": "⚡",
            "POLICY_TRANSLATE": "💡",
        }
        return icons.get(self.value, "📌")

    @property
    def title(self) -> str:
        titles = {
            "WEEKLY_TODO": "위클리 입시 To-Do 체크리스트",
            "BRIEFING_RECAP": "설명회 대리참석 3줄 요약",
            "TICKETING_ALERT": "선착순 예약 마감 임박 알람",
            "POLICY_TRANSLATE": "교육 정책 팩트 번역기",
        }
        return titles.get(self.value, self.value)


@dataclass
class AdmissionEvent:
    """Metadata for an admission seminar, fair, or test event."""
    event_id: str
    title: str
    organizer: str                          # 주최 (예: 시대인재, 종로학원, 서울시교육청, 서울대)
    target_grade: TargetGrade
    event_type: BriefingType
    event_date_str: str                     # 행사 일시 (예: "2026-11-05 19:00")
    target_region: TargetRegion = TargetRegion.ALL
    is_online_live: bool = False            # 유튜브/온라인 동시 송출 여부
    booking_opens_at: Optional[datetime] = None  # 예약 오픈 일시
    is_first_come: bool = False             # 선착순 여부
    booking_url: Optional[str] = None       # 신청 페이지 URL
    summary_3lines: list[str] = field(default_factory=list) # 3줄 핵심 요약
    so_what: str = ""                       # 학부모 기준 득실 해석 (So What)
    action_todos: list[str] = field(default_factory=list)   # 이번 주 할 일
    pdf_attachment_path: Optional[str] = None # 첨부 자료집 PDF 파일 경로
    credibility_score: int = 80             # 신뢰도 점수 (0~100)
    created_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "title": self.title,
            "organizer": self.organizer,
            "target_grade": self.target_grade.value,
            "target_region": self.target_region.value,
            "is_online_live": self.is_online_live,
            "event_type": self.event_type.value,
            "event_date_str": self.event_date_str,
            "booking_opens_at": self.booking_opens_at.isoformat() if self.booking_opens_at else None,
            "is_first_come": self.is_first_come,
            "booking_url": self.booking_url,
            "summary_3lines": self.summary_3lines,
            "so_what": self.so_what,
            "action_todos": self.action_todos,
            "pdf_attachment_path": self.pdf_attachment_path,
            "credibility_score": self.credibility_score,
            "created_at": self.created_at.isoformat(),
        }


@dataclass
class AdmissionBriefing:
    """Consolidated message payload to be dispatched to parents."""
    briefing_id: str
    briefing_type: BriefingType
    target_grade: TargetGrade
    headline: str
    summary_3lines: list[str]
    target_region: TargetRegion = TargetRegion.ALL
    is_online_live: bool = False
    silent: bool = False                    # 무음 푸시 (Quiet hours/정기 브리핑용)
    so_what: str = ""
    action_todos: list[str] = field(default_factory=list)
    booking_url: Optional[str] = None
    booking_button_text: str = "⚡ 선착순 예약창 바로가기"
    pdf_path: Optional[str] = None
    as_of: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d %H:%M"))

    def is_match_user(
        self,
        user_grade: TargetGrade,
        user_region: TargetRegion = TargetRegion.ALL,
    ) -> bool:
        """Determines whether this briefing matches subscribed parent's grade & region."""
        # 1. Grade matching
        grade_match = (self.target_grade == TargetGrade.ALL) or (self.target_grade == user_grade)
        if not grade_match:
            return False

        # 2. Region matching
        if user_region == TargetRegion.ALL:
            return True
        if user_region == TargetRegion.ONLINE:
            return self.is_online_live or (self.target_region == TargetRegion.ONLINE)
        if self.is_online_live or self.target_region == TargetRegion.ALL:
            return True
        return self.target_region == user_region


@dataclass
class ParentUserProfile:
    """Subscribed parent profile in Telegram."""
    chat_id: str
    target_grade: TargetGrade = TargetGrade.ALL
    target_region: TargetRegion = TargetRegion.ALL
    username: str = ""
    first_name: str = ""
    alert_enabled: bool = True
    registered_at: datetime = field(default_factory=datetime.now)
    last_active_at: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "target_grade": self.target_grade.value,
            "target_region": self.target_region.value,
            "username": self.username,
            "first_name": self.first_name,
            "alert_enabled": self.alert_enabled,
            "registered_at": self.registered_at.isoformat(),
            "last_active_at": self.last_active_at.isoformat(),
        }
