"""Domain models for SilverCare (실버케어).
Benefits (전국/지자체 복지 혜택), parent profiles, and the A/B/C reliability grade.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional


# 행정표준 시도 코드. 강원(42→51)·전북(45→52) 특별자치도 전환 이전 코드도 받아준다.
SIDO_NAMES: dict[str, str] = {
    "11": "서울",
    "26": "부산",
    "27": "대구",
    "28": "인천",
    "29": "광주",
    "30": "대전",
    "31": "울산",
    "36": "세종",
    "41": "경기",
    "43": "충북",
    "44": "충남",
    "46": "전남",
    "47": "경북",
    "48": "경남",
    "50": "제주",
    "51": "강원",
    "52": "전북",
}
_LEGACY_SIDO = {"42": "51", "45": "52"}


def normalize_sido_code(code: str) -> str:
    """'42'(구 강원) → '51', '45'(구 전북) → '52'. 'ALL' 및 신규 코드는 그대로."""
    raw = str(code or "").strip().upper()
    return _LEGACY_SIDO.get(raw, raw)


def sido_name(code: str) -> str:
    norm = normalize_sido_code(code)
    if norm == "ALL":
        return "전국"
    return SIDO_NAMES.get(norm, norm)


# 1-클릭 연령대 기준선: band ➔ 가상 프로필 기준 나이 (2026년 기준 1958년생 / 1964년생)
AGE_BAND_PROBE_AGE: dict[int, int] = {65: 68, 60: 62}


def age_band_of(age_reached: int) -> int:
    """올해 도달 만 나이 ➔ 연령대 기준선 (65 / 60 / 0=60세 미만)."""
    if age_reached >= 65:
        return 65
    return 60 if age_reached >= 60 else 0


def age_band_label(band: int) -> str:
    return {65: "만 65세 이상", 60: "만 60~64세"}.get(band, "만 60세 미만")


def age_band_birth_year(band: int, year: int) -> int:
    return year - AGE_BAND_PROBE_AGE[band]


class JurisdictionType(str, Enum):
    NATIONAL = "NATIONAL"  # 전국 공통 (중앙정부·건강보험)
    LOCAL = "LOCAL"        # 지자체 조례·자체 사업


class BenefitCategory(str, Enum):
    MEDICAL = "MEDICAL"
    HOUSING = "HOUSING"
    FINANCE = "FINANCE"
    SOCIAL = "SOCIAL"

    @property
    def label(self) -> str:
        return {
            BenefitCategory.MEDICAL: "🏥 의료",
            BenefitCategory.HOUSING: "🏠 주거·안전",
            BenefitCategory.FINANCE: "💰 소득·감면",
            BenefitCategory.SOCIAL: "🤝 돌봄·생활",
        }[self]


class TargetIncome(str, Enum):
    ALL = "ALL"                # 소득 무관
    PENSION_70 = "PENSION_70"  # 기초연금 수급(소득하위 70%)
    MEDIAN_140 = "MEDIAN_140"  # 기준중위소득 140% 이하 또는 기초연금 수급
    LOW_INCOME = "LOW_INCOME"  # 기초생활수급·차상위


class TargetLiving(str, Enum):
    ALONE = "ALONE"
    ANY = "ANY"


class LivingType(str, Enum):
    ALONE = "ALONE"    # 독거
    COUPLE = "COUPLE"  # 노인 부부
    FAM = "FAM"        # 자녀 등과 동거

    @property
    def label(self) -> str:
        return {LivingType.ALONE: "혼자 사심", LivingType.COUPLE: "부부 함께", LivingType.FAM: "가족과 함께"}[self]


class ReliabilityGrade(str, Enum):
    """부모님 조건 대비 혜택 적용 신뢰도. 낮은 등급(C)이 우선한다."""
    GRADE_A = "조건 일치"
    GRADE_B = "신청 후보"
    GRADE_C = "지자체 확인"

    @property
    def code(self) -> str:
        return self.name[-1]

    @property
    def badge(self) -> str:
        return {"A": "🟢", "B": "🟡", "C": "🔵"}[self.code]

    @property
    def rank(self) -> int:
        return {"A": 0, "B": 1, "C": 2}[self.code]

    @classmethod
    def worst(cls, *grades: "ReliabilityGrade") -> "ReliabilityGrade":
        return max(grades, key=lambda g: g.rank)


@dataclass
class SilverBenefit:
    id: int
    title: str
    jurisdiction_type: str
    category: str
    min_age: int
    target_income: str
    target_living: str
    support_amount: str
    is_pre_approval_required: bool
    requires_prescription: bool
    region_code: str
    agency_name: str
    application_period: str
    detail_guide: str
    phone_script: str
    parent_msg_template: str
    # 고밀도 실전 필드 (기본값 → 기존 DB 행·호출부 하위호환). DB 스키마에는 저장하지 않고 시드/웹 리포트 전용.
    documents: list[dict[str, str]] = field(default_factory=list)  # [{"name", "issuer", "required"}]
    application_steps: list[str] = field(default_factory=list)
    rejection_response: str = ""   # 반려 시 법령·지침 근거 대응 대본
    warning_notes: list[str] = field(default_factory=list)
    source_url: str = ""           # 공식 공고/정보 누리집 (https 만)
    source_agency: str = ""        # 소관기관
    verified_at: str = "2026-11-01"  # 최종 팩트체크 기준일

    @property
    def has_field_guide(self) -> bool:
        return bool(self.documents and self.application_steps and self.rejection_response)

    def __post_init__(self) -> None:
        # Fail fast on typos in seed data / DB rows
        JurisdictionType(self.jurisdiction_type)
        BenefitCategory(self.category)
        TargetIncome(self.target_income)
        TargetLiving(self.target_living)
        self.region_code = normalize_sido_code(self.region_code)

    @property
    def is_local(self) -> bool:
        return self.jurisdiction_type == JurisdictionType.LOCAL.value

    @property
    def category_label(self) -> str:
        return BenefitCategory(self.category).label


@dataclass
class ParentProfile:
    id: int
    chat_id: int
    alias: str
    birth_year: int
    sido_code: str
    sigungu_name: str = ""
    living_type: str = LivingType.ALONE.value
    is_pension_receiver: int = -1  # 1: 수급, 0: 미수급, -1: 모름
    is_low_income: int = -1        # 1: 해당, 0: 아님, -1: 모름

    def __post_init__(self) -> None:
        LivingType(self.living_type)
        self.sido_code = normalize_sido_code(self.sido_code)

    def age_in(self, year: int) -> int:
        """해당 연도 중 도달하는 만 나이 (생일이 지나면 이 나이)."""
        return year - self.birth_year

    @property
    def is_age_only(self) -> bool:
        """연령대 1-클릭으로 만든 가상 프로필 (호칭 없음 · 전국 공통)."""
        return not self.alias and self.sido_code == "ALL"

    @property
    def age(self) -> int:
        """현재 연도 기준 만 나이 (올해 생일 이후 기준)."""
        return self.age_in(date.today().year)

    @property
    def region_label(self) -> str:
        base = sido_name(self.sido_code)
        return f"{base} {self.sigungu_name}".strip() if self.sigungu_name else base


@dataclass
class BenefitMatch:
    """Result of matching one benefit against one parent."""
    benefit: SilverBenefit
    grade: ReliabilityGrade
    checks: list[tuple[str, Optional[bool]]] = field(default_factory=list)  # (조건 설명, True/False/None=확인 필요)
    cautions: list[str] = field(default_factory=list)


class MissionStatus(str, Enum):
    """자녀 대리신청 미션 진행 상태."""
    NOT_STARTED = "NOT_STARTED"
    CONSENT_NEEDED = "CONSENT_NEEDED"
    DOCS_PREPARING = "DOCS_PREPARING"
    SUBMITTED = "SUBMITTED"
    RECEIVING = "RECEIVING"

    @property
    def label(self) -> str:
        return {
            MissionStatus.NOT_STARTED: "미착수",
            MissionStatus.CONSENT_NEEDED: "부모님 확인",
            MissionStatus.DOCS_PREPARING: "서류 준비",
            MissionStatus.SUBMITTED: "대리접수 완료",
            MissionStatus.RECEIVING: "수급 중",
        }[self]

    @property
    def badge(self) -> str:
        return {
            MissionStatus.NOT_STARTED: "⚪",
            MissionStatus.CONSENT_NEEDED: "🟡",
            MissionStatus.DOCS_PREPARING: "🔵",
            MissionStatus.SUBMITTED: "🟣",
            MissionStatus.RECEIVING: "🟢",
        }[self]


@dataclass
class SilverMission:
    """One parent × one benefit proxy-application mission."""
    id: int
    chat_id: int
    parent_id: int
    benefit_id: int
    status: str
    updated_at: str

    def __post_init__(self) -> None:
        MissionStatus(self.status)

    @property
    def status_label(self) -> str:
        return MissionStatus(self.status).label

    @property
    def status_badge(self) -> str:
        return MissionStatus(self.status).badge
