"""Domain models for Admission Briefing (입시정보 브리핑).
Handles target grades, regions, briefing types, event metadata, and parent subscriptions.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
from typing import Any, Optional
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")


class EducationStage(str, Enum):
    """Broad education stage used by the additive grade model."""
    ELEMENTARY = "ELEMENTARY"
    MIDDLE = "MIDDLE"
    HIGH = "HIGH"
    POST_HIGH = "POST_HIGH"
    ALL = "ALL"

    @property
    def label(self) -> str:
        return {
            "ELEMENTARY": "초등",
            "MIDDLE": "중등",
            "HIGH": "고등",
            "POST_HIGH": "고등 이후",
            "ALL": "전 학년",
        }[self.value]


class GradeLevel(str, Enum):
    """Specific school grade, or an explicit unknown/any-grade marker."""
    E1 = "E1"
    E2 = "E2"
    E3 = "E3"
    E4 = "E4"
    E5 = "E5"
    E6 = "E6"
    M1 = "M1"
    M2 = "M2"
    M3 = "M3"
    H1 = "H1"
    H2 = "H2"
    H3 = "H3"
    N = "N"
    ALL = "ALL"
    LEGACY = "LEGACY"

    @property
    def label(self) -> str:
        labels = {
            **{f"E{i}": f"초{i}학년" for i in range(1, 7)},
            **{f"M{i}": f"중{i}학년" for i in range(1, 4)},
            **{f"H{i}": f"고{i}학년" for i in range(1, 4)},
            "N": "N수생",
            "ALL": "전 학년",
            "LEGACY": "기존 학년 분류",
        }
        return labels[self.value]


class GradeMode(str, Enum):
    MANUAL = "MANUAL"
    AUTO = "AUTO"


def legacy_target_grade_to_stage_level(grade: TargetGrade) -> tuple[EducationStage, GradeLevel]:
    """Maps the four legacy buckets without guessing a specific school grade."""
    mapping = {
        TargetGrade.MIDDLE: (EducationStage.MIDDLE, GradeLevel.LEGACY),
        TargetGrade.HIGH_1_2: (EducationStage.HIGH, GradeLevel.LEGACY),
        TargetGrade.HIGH_3_N: (EducationStage.HIGH, GradeLevel.LEGACY),
        TargetGrade.ALL: (EducationStage.ALL, GradeLevel.ALL),
    }
    return mapping[grade]


def stage_level_to_legacy_target_grade(stage: EducationStage, level: GradeLevel) -> TargetGrade:
    """Returns the closest legacy delivery bucket for additive profiles."""
    if stage == EducationStage.MIDDLE or level in (GradeLevel.M1, GradeLevel.M2, GradeLevel.M3):
        return TargetGrade.MIDDLE
    if stage == EducationStage.HIGH:
        if level in (GradeLevel.H3, GradeLevel.N):
            return TargetGrade.HIGH_3_N
        if level in (GradeLevel.H1, GradeLevel.H2):
            return TargetGrade.HIGH_1_2
        return TargetGrade.HIGH_1_2
    if stage == EducationStage.POST_HIGH or level == GradeLevel.N:
        return TargetGrade.HIGH_3_N
    return TargetGrade.ALL


def academic_year_kst(value: Optional[datetime | date] = None) -> int:
    """Returns the Korean academic year; it rolls over on March 1 in KST."""
    current = value or datetime.now(KST)
    if isinstance(current, datetime):
        if current.tzinfo is None:
            current = current.replace(tzinfo=KST)
        current = current.astimezone(KST)
        return current.year if (current.month, current.day) >= (3, 1) else current.year - 1
    return current.year if (current.month, current.day) >= (3, 1) else current.year - 1


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
    POLICY_TRANSLATE = "POLICY_TRANSLATE" # 교육 정책 팩트 번역 (득실 분석)
    BREAKING_NEWS = "BREAKING_NEWS"       # 실시간 대입 정책/등급컷 긴급 속보

    @property
    def icon(self) -> str:
        icons = {
            "WEEKLY_TODO": "📋",
            "BRIEFING_RECAP": "📢",
            "TICKETING_ALERT": "⚡",
            "POLICY_TRANSLATE": "💡",
            "BREAKING_NEWS": "🚨",
        }
        return icons.get(self.value, "📌")

    @property
    def title(self) -> str:
        titles = {
            "WEEKLY_TODO": "위클리 입시 To-Do 체크리스트",
            "BRIEFING_RECAP": "설명회 대리참석 3줄 요약",
            "TICKETING_ALERT": "선착순 예약 마감 임박 알람",
            "POLICY_TRANSLATE": "입시 팩트체크 (So-What)",
            "BREAKING_NEWS": "실시간 대입 긴급 속보",
        }
        return titles.get(self.value, self.value)


class EventCategory(str, Enum):
    """Event menu category (orthogonal to BriefingType, which is the dispatch content type)."""
    EXPO = "EXPO"           # 공공·대학 대입 박람회
    BRIEFING = "BRIEFING"   # 5대 대형 입시기관 설명회
    SEMINAR = "SEMINAR"     # 테마별 전략 세미나

    @property
    def icon(self) -> str:
        icons = {
            "EXPO": "🎪",
            "BRIEFING": "📢",
            "SEMINAR": "💡",
        }
        return icons.get(self.value, "📌")

    @property
    def badge(self) -> str:
        badges = {
            "EXPO": "🎪 [대입박람회]",
            "BRIEFING": "📢 [대형설명회]",
            "SEMINAR": "💡 [테마세미나]",
        }
        return badges.get(self.value, "📌")

    @property
    def title(self) -> str:
        titles = {
            "EXPO": "공공·대학 대입 박람회",
            "BRIEFING": "5대 대형 입시기관 설명회",
            "SEMINAR": "테마별 전략 세미나",
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
    source_name: str = "공식 오피셜"         # 정보 출처 (예: 교육부, 서울시교육청, EBSi, 대교협)
    source_url: Optional[str] = None        # 원문 공문/기사/모집요강 URL
    urgency: str = "NORMAL"                 # 긴급도 ("NORMAL", "BREAKING", "HIGH")
    category: EventCategory = EventCategory.BRIEFING  # 행사 메뉴 분류 (박람회/설명회/세미나)
    expires_at: Optional[datetime] = None   # 노출 만료 시각 (None이면 event_date_str 기준)
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
            "source_name": self.source_name,
            "source_url": self.source_url,
            "urgency": self.urgency,
            "category": self.category.value,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
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
    source_name: str = "공식 오피셜"
    source_url: Optional[str] = None
    urgency: str = "NORMAL"                 # "NORMAL", "BREAKING", "HIGH"
    category: Optional[EventCategory] = None  # 행사 기반 브리핑만 설정 (To-Do/팩트체크는 None)
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


MAX_CHILDREN = 3
CHILD_ALIASES = ("첫째", "둘째", "셋째")


@dataclass
class ChildProfile:
    """One child of a parent subscriber (multi-child households switch between these)."""
    alias: str = "첫째"
    grade: TargetGrade = TargetGrade.ALL
    region: TargetRegion = TargetRegion.ALL
    education_stage: EducationStage | None = None
    grade_level: GradeLevel | None = None
    school_entry_year: Optional[int] = None
    grade_mode: GradeMode = GradeMode.MANUAL
    last_promoted_academic_year: Optional[int] = None
    _extended: bool = field(default=False, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._extended = (
            self.education_stage is not None or self.grade_level is not None
            or self.school_entry_year is not None or self.grade_mode != GradeMode.MANUAL
            or self.last_promoted_academic_year is not None
        )
        try:
            self.grade = self.grade if isinstance(self.grade, TargetGrade) else TargetGrade(self.grade)
        except (TypeError, ValueError):
            self.grade = TargetGrade.ALL
        try:
            self.region = self.region if isinstance(self.region, TargetRegion) else TargetRegion(self.region)
        except (TypeError, ValueError):
            self.region = TargetRegion.ALL
        if self.education_stage is None or self.grade_level is None:
            self.education_stage, self.grade_level = legacy_target_grade_to_stage_level(self.grade)
        else:
            try:
                self.education_stage = EducationStage(self.education_stage)
            except (TypeError, ValueError):
                self.education_stage = EducationStage.ALL
            try:
                self.grade_level = GradeLevel(self.grade_level)
            except (TypeError, ValueError):
                self.grade_level = GradeLevel.ALL
            # Explicit additive values are authoritative, while retaining the legacy
            # bucket for old delivery and filtering code.
            if self.grade == TargetGrade.ALL:
                self.grade = stage_level_to_legacy_target_grade(self.education_stage, self.grade_level)
        try:
            self.grade_mode = self.grade_mode if isinstance(self.grade_mode, GradeMode) else GradeMode(self.grade_mode)
        except (TypeError, ValueError):
            self.grade_mode = GradeMode.MANUAL

    def to_dict(self) -> dict[str, Any]:
        data = {
            "alias": self.alias,
            "grade": self.grade.value,
            "region": self.region.value,
        }
        if self._extended:
            data.update({
                "education_stage": self.education_stage.value,
                "grade_level": self.grade_level.value,
                "school_entry_year": self.school_entry_year,
                "grade_mode": self.grade_mode.value,
                "last_promoted_academic_year": self.last_promoted_academic_year,
            })
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ChildProfile:
        try:
            grade = TargetGrade(data.get("grade", "ALL"))
        except (TypeError, ValueError):
            grade = TargetGrade.ALL
        try:
            region = TargetRegion(data.get("region", "ALL"))
        except (TypeError, ValueError):
            region = TargetRegion.ALL
        stage_raw = data.get("education_stage")
        level_raw = data.get("grade_level")
        try:
            stage = EducationStage(stage_raw) if stage_raw is not None else None
        except (TypeError, ValueError):
            stage = None
        try:
            level = GradeLevel(level_raw) if level_raw is not None else None
        except (TypeError, ValueError):
            level = None
        try:
            mode = GradeMode(data.get("grade_mode", GradeMode.MANUAL.value))
        except (TypeError, ValueError):
            mode = GradeMode.MANUAL
        entry_year = data.get("school_entry_year")
        try:
            entry_year = int(entry_year) if entry_year is not None else None
        except (TypeError, ValueError):
            entry_year = None
        promoted = data.get("last_promoted_academic_year")
        try:
            promoted = int(promoted) if promoted is not None else None
        except (TypeError, ValueError):
            promoted = None
        result = cls(
            alias=str(data.get("alias") or "자녀"), grade=grade, region=region,
            education_stage=stage, grade_level=level, school_entry_year=entry_year,
            grade_mode=mode, last_promoted_academic_year=promoted,
        )
        result._extended = any(key in data for key in (
            "education_stage", "grade_level", "school_entry_year", "grade_mode", "last_promoted_academic_year"
        ))
        return result

    def promote_if_due(self, now: Optional[datetime | date] = None) -> bool:
        """Promotes an AUTO child once per KST academic year.

        ``school_entry_year`` is the academic year in which the child entered E1.
        The method is intentionally idempotent so repeated reads or scheduler runs
        cannot promote the child twice in one academic year.
        """
        if self.grade_mode != GradeMode.AUTO or self.school_entry_year is None:
            return False
        academic_year = academic_year_kst(now)
        if self.last_promoted_academic_year is not None and academic_year <= self.last_promoted_academic_year:
            return False
        offset = academic_year - int(self.school_entry_year)
        if offset < 0:
            return False
        if offset <= 5:
            level = GradeLevel(f"E{offset + 1}")
        elif offset <= 8:
            level = GradeLevel(f"M{offset - 5}")
        elif offset <= 11:
            level = GradeLevel(f"H{offset - 8}")
        else:
            level = GradeLevel.N
        stage = (
            EducationStage.ELEMENTARY if level.value.startswith("E")
            else EducationStage.MIDDLE if level.value.startswith("M")
            else EducationStage.HIGH if level.value.startswith("H")
            else EducationStage.POST_HIGH
        )
        changed = (self.education_stage != stage or self.grade_level != level)
        self.education_stage = stage
        self.grade_level = level
        self.grade = stage_level_to_legacy_target_grade(stage, level)
        self.last_promoted_academic_year = academic_year
        self._extended = True
        return changed

    # Friendly aliases for callers that describe the same operation differently.
    auto_promote = promote_if_due
    apply_auto_promotion = promote_if_due
    promote_if_needed = promote_if_due

    @property
    def legacy_target_grade(self) -> TargetGrade:
        return self.grade

    def set_legacy_target_grade(self, grade: TargetGrade) -> None:
        self.grade = grade if isinstance(grade, TargetGrade) else TargetGrade(grade)
        self.education_stage, self.grade_level = legacy_target_grade_to_stage_level(self.grade)
        self._extended = True


@dataclass
class ParentUserProfile:
    """Subscribed parent profile in Telegram.

    target_grade/target_region always mirror the active child so legacy readers keep working;
    an empty children list means a legacy single-child subscriber.
    """
    chat_id: str
    target_grade: TargetGrade = TargetGrade.ALL
    target_region: TargetRegion = TargetRegion.ALL
    username: str = ""
    first_name: str = ""
    alert_enabled: bool = True
    registered_at: datetime = field(default_factory=datetime.now)
    last_active_at: datetime = field(default_factory=datetime.now)
    children: list[ChildProfile] = field(default_factory=list)
    active_child_index: int = 0
    education_stage: EducationStage | None = None
    grade_level: GradeLevel | None = None
    weekly_guide_enabled: bool = True
    event_dday_enabled: bool = True
    policy_breaking_enabled: bool = True

    def __post_init__(self) -> None:
        if self.children and 0 <= self.active_child_index < len(self.children):
            active = self.children[self.active_child_index]
            self.education_stage = active.education_stage
            self.grade_level = active.grade_level
            self.target_grade = active.grade
            self.target_region = active.region
        else:
            try:
                self.target_grade = self.target_grade if isinstance(self.target_grade, TargetGrade) else TargetGrade(self.target_grade)
                if self.education_stage is None or self.grade_level is None:
                    self.education_stage, self.grade_level = legacy_target_grade_to_stage_level(self.target_grade)
                else:
                    self.education_stage = EducationStage(self.education_stage)
                    self.grade_level = GradeLevel(self.grade_level)
                    if self.grade_level not in (GradeLevel.LEGACY, GradeLevel.ALL):
                        self.target_grade = stage_level_to_legacy_target_grade(self.education_stage, self.grade_level)
            except (TypeError, ValueError, KeyError):
                self.target_grade = TargetGrade.ALL
                self.education_stage, self.grade_level = legacy_target_grade_to_stage_level(TargetGrade.ALL)

    def get_active_child(self) -> ChildProfile:
        if self.children and 0 <= self.active_child_index < len(self.children):
            return self.children[self.active_child_index]
        return ChildProfile(
            alias="자녀", grade=self.target_grade, region=self.target_region,
            education_stage=self.education_stage, grade_level=self.grade_level,
        )
    def get_all_grades(self) -> set[TargetGrade]:
        if not self.children:
            return {self.target_grade}
        return {c.grade for c in self.children}

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
            "children": [c.to_dict() for c in self.children],
            "active_child_index": self.active_child_index,
            "education_stage": self.education_stage.value,
            "grade_level": self.grade_level.value,
            "weekly_guide_enabled": self.weekly_guide_enabled,
            "event_dday_enabled": self.event_dday_enabled,
            "policy_breaking_enabled": self.policy_breaking_enabled,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ParentUserProfile":
        """Tolerant deserializer for API/session callers using profile dictionaries."""
        try:
            target_grade = TargetGrade(data.get("target_grade", "ALL"))
        except (TypeError, ValueError):
            target_grade = TargetGrade.ALL
        try:
            target_region = TargetRegion(data.get("target_region", "ALL"))
        except (TypeError, ValueError):
            target_region = TargetRegion.ALL
        children_raw = data.get("children", [])
        children = [ChildProfile.from_dict(item) for item in children_raw if isinstance(item, dict)] if isinstance(children_raw, list) else []
        try:
            stage = EducationStage(data["education_stage"]) if data.get("education_stage") is not None else None
        except ValueError:
            stage = None
        try:
            level = GradeLevel(data["grade_level"]) if data.get("grade_level") is not None else None
        except ValueError:
            level = None
        return cls(
            chat_id=str(data.get("chat_id", "")), target_grade=target_grade, target_region=target_region,
            username=str(data.get("username") or ""), first_name=str(data.get("first_name") or ""),
            alert_enabled=bool(data.get("alert_enabled", True)), children=children,
            active_child_index=int(data.get("active_child_index", 0) or 0),
            education_stage=stage, grade_level=level,
            weekly_guide_enabled=bool(data.get("weekly_guide_enabled", True)),
            event_dday_enabled=bool(data.get("event_dday_enabled", True)),
            policy_breaking_enabled=bool(data.get("policy_breaking_enabled", True)),
        )
