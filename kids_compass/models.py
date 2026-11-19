"""Domain models for Kids Compass (키즈 나침반)."""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from enum import Enum
from typing import Optional


class AgeGroup(str, Enum):
    PREGNANCY = "PREGNANCY"
    INFANT = "INFANT"
    PRESCHOOL = "PRESCHOOL"
    ELEMENTARY = "ELEMENTARY"
    ALL = "ALL"

    @property
    def label(self) -> str:
        return {
            AgeGroup.PREGNANCY: "🤰 임신기 (예비부모 / 태교·바우처)",
            AgeGroup.INFANT: "👶 0~3세 (영유아 / 유아차·수유실·해열제)",
            AgeGroup.PRESCHOOL: "👦 4~7세 (유치부 / 현장직행 오감놀이터)",
            AgeGroup.ELEMENTARY: "🧑 8~13세 (초등부 / 교과체험·경제교실)",
            AgeGroup.ALL: "👨‍👩‍👧 전 연령",
        }[self]


class ExperienceCategory(str, Enum):
    SAFETY = "SAFETY"
    NATURE = "NATURE"
    SCIENCE = "SCIENCE"
    ECONOMY = "ECONOMY"
    PLAY = "PLAY"

    @property
    def label(self) -> str:
        return {
            ExperienceCategory.SAFETY: "🚒 소방/안전체험",
            ExperienceCategory.NATURE: "🌲 숲/곤충/생태",
            ExperienceCategory.SCIENCE: "🔭 과학관/천문대",
            ExperienceCategory.ECONOMY: "💰 화폐/경제교실",
            ExperienceCategory.PLAY: "🏛️ 박물관/공공키즈카페",
        }[self]


class IngredientType(str, Enum):
    ACETAMINOPHEN = "ACETAMINOPHEN"  # 아세트아미노펜 - 타이레놀 현탁액, 챔프 빨강
    IBUPROFEN = "IBUPROFEN"          # 이부프로펜 - 부루펜 시럽, 챔프 파랑
    DEXIBUPROFEN = "DEXIBUPROFEN"    # 덱시부프로펜 - 맥시부펜 시럽

    @property
    def label(self) -> str:
        return {
            IngredientType.ACETAMINOPHEN: "아세트아미노펜",
            IngredientType.IBUPROFEN: "이부프로펜",
            IngredientType.DEXIBUPROFEN: "덱시부프로펜",
        }[self]

    @property
    def is_nsaid(self) -> bool:
        return self in (IngredientType.IBUPROFEN, IngredientType.DEXIBUPROFEN)


def months_between(birth: date, today: date) -> int:
    """Completed months of age (a month counts only once its day-of-month is reached)."""
    months = (today.year - birth.year) * 12 + (today.month - birth.month)
    if today.day < birth.day:
        months -= 1
    return max(months, 0)


def age_group_for_months(months: int) -> AgeGroup:
    """0~47개월 = 만 0~3세, 48~95개월 = 만 4~7세, 96개월~ = 만 8세 이상. A born child is never PREGNANCY."""
    if months < 48:
        return AgeGroup.INFANT
    if months < 96:
        return AgeGroup.PRESCHOOL
    return AgeGroup.ELEMENTARY


@dataclass
class ChildProfile:
    child_id: int
    telegram_user_id: str
    nickname: str
    birth_date: str  # YYYY-MM-DD
    weight_kg: Optional[float]  # None/0.0 = 미입력 (fever calculator must ask before dosing)
    is_primary: bool = True

    def age_months(self, today: Optional[date] = None) -> int:
        return months_between(date.fromisoformat(self.birth_date), today or date.today())

    def age_group(self, today: Optional[date] = None) -> AgeGroup:
        return age_group_for_months(self.age_months(today))


@dataclass
class PregnancyProfile:
    """Expecting parent — kept apart from ChildProfile so it never reaches the fever calculator."""
    telegram_user_id: str
    nickname: str  # 태명
    due_date: str  # 출산예정일 YYYY-MM-DD
    created_at: str = ""

    def days_left(self, today: Optional[date] = None) -> int:
        return (date.fromisoformat(self.due_date) - (today or date.today())).days

    def dday(self, today: Optional[date] = None) -> str:
        days = self.days_left(today)
        if days > 0:
            return f"D-{days}"
        return "D-DAY" if days == 0 else f"D+{-days}"

    def gestational_days(self, today: Optional[date] = None) -> int:
        """Days of pregnancy counted back from the due date (40주 0일 = 280일), clamped to 0~300."""
        days = 280 - (date.fromisoformat(self.due_date) - (today or date.today())).days
        return min(max(days, 0), 300)

    def gestational_week(self, today: Optional[date] = None) -> int:
        return self.gestational_days(today) // 7


@dataclass
class PregnancyBenefit:
    benefit_id: str
    title: str
    amount: str
    detail: str
    url: str


@dataclass
class MedicineProduct:
    product_id: str
    product_name: str
    ingredient: IngredientType
    concentration_mg_ml: float
    min_age_months: int
    recommended_mg_kg: float
    max_single_mg: float
    interval_hours: int
    max_times_per_day: int
    max_daily_mg_kg: float
    disclaimer: str


@dataclass
class FeverCalculationResult:
    status: str  # OK | BLOCKED_INFANT_UNDER_3M | BLOCKED_NSAID_UNDER_6M | INVALID_WEIGHT
    age_months: int
    weight_kg: float
    product: Optional[MedicineProduct]
    recommended_ml: float = 0.0
    recommended_mg: float = 0.0
    max_daily_ml: float = 0.0
    interval_hours: int = 0
    max_times_per_day: int = 0
    warning_message: str = ""
    disclaimer: str = ""

    @property
    def is_ok(self) -> bool:
        return self.status == "OK"


@dataclass
class ExperienceItem:
    exp_id: str
    title: str
    age_group: AgeGroup
    category: ExperienceCategory
    region_sido: str
    region_sigungu: str
    venue_name: str
    fee_type: str
    fee_detail: str
    is_indoor: bool
    stroller_accessible: bool
    nursing_room: bool
    booking_required: bool
    booking_url: str
    booking_open_rule: str
    key_points: str
    operating_hours: str = ""  # 예: "10:00~18:00 (입장마감 17:00 · 매주 월 휴관)"


@dataclass
class BookingEvent:
    booking_id: int
    exp_id: str
    title: str
    open_datetime: str  # YYYY-MM-DD HH:MM (KST)
    target_date_range: str
    booking_url: str
    memo: str


@dataclass
class MoonlightHospital:
    hospital_id: str
    name: str
    region_sido: str
    region_sigungu: str
    address: str
    tel: str
    weekday_hours: str
    weekend_hours: str
    holiday_hours: str
    paired_pharmacy: str
    paired_pharmacy_tel: str
    map_url: str
    last_verified_at: str


MILESTONE_KINDS = ("CHECKUP", "VACCINE", "PREGNANCY")
MILESTONE_STATUSES = ("DUE", "USER_CONFIRMED", "DEFERRED", "NOT_APPLICABLE", "UNKNOWN")


@dataclass
class MilestoneDefinition:
    """Catalogue row: 영유아 건강검진 / 국가필수예방접종 / 임신 주차 체크포인트.
    Child items use age_min/max_months (inclusive months); PREGNANCY items use pregnancy_week."""
    definition_id: str
    kind: str  # CHECKUP | VACCINE | PREGNANCY
    code: str
    schedule_version: str
    age_min_months: Optional[int]
    age_max_months: Optional[int]
    pregnancy_week: Optional[int]
    title: str
    description: str
    source_url: str


@dataclass
class SubjectMilestone:
    """Per-subject (child / pregnancy) milestone state. Only USER_CONFIRMED counts as done."""
    milestone_id: int
    subject_id: str
    subject_kind: str  # CHILD | PREGNANCY
    definition_id: str
    due_from: str  # YYYY-MM-DD
    due_to: str    # YYYY-MM-DD
    status: str    # DUE | USER_CONFIRMED | DEFERRED | NOT_APPLICABLE | UNKNOWN
    status_source: str
    completed_at: Optional[str]
    schedule_version: str
    telegram_user_id: str = ""


@dataclass
class WeatherSnapshot:
    """Weekend weather for one region/day. is_failed=True means 'unknown' — never treat it as sunny.
    is_bad_dust is always False from Open-Meteo forecast (no air-quality data) — never present it as '좋음'."""
    region_sido: str
    target_date: str  # YYYY-MM-DD
    is_rain_or_snow: bool
    is_bad_dust: bool
    summary_text: str
    provider: str
    fetched_at: str
    expires_at: str
    is_failed: bool = False


@dataclass
class PushMessage:
    chat_id: str
    text: str
    reply_markup: Optional[dict] = None
    stream: str = ""
    entity_type: str = ""
    entity_ids: list[str] = field(default_factory=list)
    content_hash: str = ""
