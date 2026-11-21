"""Domain models for YOLO Bot (욜로봇).
Defines 4 Solo Archetypes, multi-dimensional metadata (activity_mode, mobility_scope, setting),
ProfileState, Season, WeatherClass, WeatherSnapshot, TimeSlot, YoloCuration, and YoloUserProfile.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone, timedelta
from enum import Enum
from typing import Optional

KST = timezone(timedelta(hours=9))


class ActivityMode(str, Enum):
    INDOOR = "INDOOR"
    OUTDOOR = "OUTDOOR"


class MobilityScope(str, Enum):
    LOCAL = "LOCAL"
    REGIONAL = "REGIONAL"
    HOME = "HOME"


class SettingType(str, Enum):
    OUTDOOR = "OUTDOOR"
    URBAN = "URBAN"
    HOME = "HOME"


class SoloArchetype(str, Enum):
    """4대 욜로 성향 분류 모델 (Solo Archetypes)."""
    ACTIVE_CHALLENGER = "ACTIVE_CHALLENGER"
    URBAN_HIPSTER = "URBAN_HIPSTER"
    ROAD_NOMAD = "ROAD_NOMAD"
    WELLNESS_DEEPDIVER = "WELLNESS_DEEPDIVER"

    @property
    def label(self) -> str:
        return {
            SoloArchetype.ACTIVE_CHALLENGER: "액티브 챌린저",
            SoloArchetype.URBAN_HIPSTER: "어반 힙스터",
            SoloArchetype.ROAD_NOMAD: "로드 노마드",
            SoloArchetype.WELLNESS_DEEPDIVER: "웰니스 딥다이버",
        }[self]

    @property
    def emoji(self) -> str:
        return {
            SoloArchetype.ACTIVE_CHALLENGER: "🏃",
            SoloArchetype.URBAN_HIPSTER: "🎨",
            SoloArchetype.ROAD_NOMAD: "✈️",
            SoloArchetype.WELLNESS_DEEPDIVER: "🛋️",
        }[self]

    @property
    def tagline(self) -> str:
        return {
            SoloArchetype.ACTIVE_CHALLENGER: "땀·성취·스포츠 (마라톤/등산/클라이밍)",
            SoloArchetype.URBAN_HIPSTER: "문화·전시·미식 (팝업/LP바/독립서점)",
            SoloArchetype.ROAD_NOMAD: "혼여·방랑·풍경 (KTX당일/섬/드라이브)",
            SoloArchetype.WELLNESS_DEEPDIVER: "홈바·휴식·취향 (칵테일/북캉스/OTT)",
        }[self]

    @property
    def default_activity_mode(self) -> ActivityMode:
        return {
            SoloArchetype.ACTIVE_CHALLENGER: ActivityMode.OUTDOOR,
            SoloArchetype.URBAN_HIPSTER: ActivityMode.INDOOR,
            SoloArchetype.ROAD_NOMAD: ActivityMode.OUTDOOR,
            SoloArchetype.WELLNESS_DEEPDIVER: ActivityMode.INDOOR,
        }[self]

    @property
    def mobility_scope(self) -> MobilityScope:
        return {
            SoloArchetype.ACTIVE_CHALLENGER: MobilityScope.LOCAL,
            SoloArchetype.URBAN_HIPSTER: MobilityScope.LOCAL,
            SoloArchetype.ROAD_NOMAD: MobilityScope.REGIONAL,
            SoloArchetype.WELLNESS_DEEPDIVER: MobilityScope.HOME,
        }[self]

    @property
    def setting(self) -> SettingType:
        return {
            SoloArchetype.ACTIVE_CHALLENGER: SettingType.OUTDOOR,
            SoloArchetype.URBAN_HIPSTER: SettingType.URBAN,
            SoloArchetype.ROAD_NOMAD: SettingType.OUTDOOR,
            SoloArchetype.WELLNESS_DEEPDIVER: SettingType.HOME,
        }[self]

    @property
    def is_outdoor_centric(self) -> bool:
        return self in (SoloArchetype.ACTIVE_CHALLENGER, SoloArchetype.ROAD_NOMAD)


class ProfileState(str, Enum):
    ONBOARDING = "ONBOARDING"
    ACTIVE = "ACTIVE"
    UNSUBSCRIBED = "UNSUBSCRIBED"


class Season(str, Enum):
    ALL = "ALL"
    SPRING = "SPRING"
    SUMMER = "SUMMER"
    AUTUMN = "AUTUMN"
    WINTER = "WINTER"

    @property
    def label(self) -> str:
        return {
            Season.ALL: "사계절",
            Season.SPRING: "🌸 봄",
            Season.SUMMER: "☀️ 여름",
            Season.AUTUMN: "🍁 가을",
            Season.WINTER: "❄️ 겨울",
        }[self]

    @classmethod
    def from_month(cls, month: int) -> Season:
        if month in (3, 4, 5):
            return cls.SPRING
        if month in (6, 7, 8):
            return cls.SUMMER
        if month in (9, 10, 11):
            return cls.AUTUMN
        if month in (12, 1, 2):
            return cls.WINTER
        raise ValueError(f"Invalid month: {month}")


class WeatherClass(str, Enum):
    ALL = "ALL"
    CLEAR = "CLEAR"
    RAIN = "RAIN"
    SNOW = "SNOW"
    HEAT = "HEAT"
    COLD = "COLD"
    DUST = "DUST"
    FOG = "FOG"
    UNKNOWN = "UNKNOWN"

    @property
    def label(self) -> str:
        return {
            WeatherClass.ALL: "전천후",
            WeatherClass.CLEAR: "맑음",
            WeatherClass.RAIN: "우천",
            WeatherClass.SNOW: "강설",
            WeatherClass.HEAT: "폭염",
            WeatherClass.COLD: "한파",
            WeatherClass.DUST: "미세먼지",
            WeatherClass.FOG: "안개",
            WeatherClass.UNKNOWN: "기상미확인",
        }[self]


class TimeSlot(str, Enum):
    """가용 시간대 필터 (2시간 컷 / 반나절 / 하루·1박)."""
    ALL = "ALL"
    QUICK_2H = "QUICK_2H"
    HALF_DAY = "HALF_DAY"
    FULL_OR_TRIP = "FULL_OR_TRIP"

    @property
    def label(self) -> str:
        return {
            TimeSlot.ALL: "전체",
            TimeSlot.QUICK_2H: "⚡ 2시간 컷",
            TimeSlot.HALF_DAY: "🌤 반나절",
            TimeSlot.FULL_OR_TRIP: "🧳 하루·1박",
        }[self]

    @property
    def callback_code(self) -> str:
        return {
            TimeSlot.ALL: "all",
            TimeSlot.QUICK_2H: "q2",
            TimeSlot.HALF_DAY: "half",
            TimeSlot.FULL_OR_TRIP: "full",
        }[self]


AREA_NAMES: dict[str, str] = {
    "1100000000": "서울·수도권",
    "2600000000": "부산·경남",
    "2700000000": "대구·경북",
    "2900000000": "광주·호남",
    "3000000000": "대전·충청",
    "5000000000": "제주",
    "5100000000": "강원",
}

# [TG-04] 64-byte callback short codes (`yolo:area:<CODE>`) <-> weather area codes
AREA_SHORT_CODES: dict[str, str] = {
    "SEO": "1100000000",
    "BUS": "2600000000",
    "DGU": "2700000000",
    "GWJ": "2900000000",
    "DJN": "3000000000",
    "JEJ": "5000000000",
    "GWN": "5100000000",
}

VALID_MBTI_TYPES: tuple[str, ...] = (
    "ISTJ", "ISFJ", "INFJ", "INTJ",
    "ISTP", "ISFP", "INFP", "INTP",
    "ESTP", "ESFP", "ENFP", "ENTP",
    "ESTJ", "ESFJ", "ENFJ", "ENTJ",
)

SOLO_LEVEL_LABELS: dict[int, str] = {
    1: "Lv.1 입문",
    2: "Lv.2 초급",
    3: "Lv.3 중급",
    4: "Lv.4 숙련",
    5: "Lv.5 마스터",
}

# MBTI별 기본 성향 친화도 (1순위, 2순위) - 보조 성향 도출용
MBTI_ARCHETYPE_AFFINITY: dict[str, tuple[SoloArchetype, SoloArchetype]] = {
    "ESTP": (SoloArchetype.ACTIVE_CHALLENGER, SoloArchetype.ROAD_NOMAD),
    "ESFP": (SoloArchetype.ACTIVE_CHALLENGER, SoloArchetype.URBAN_HIPSTER),
    "ENTJ": (SoloArchetype.ACTIVE_CHALLENGER, SoloArchetype.URBAN_HIPSTER),
    "ISTP": (SoloArchetype.ACTIVE_CHALLENGER, SoloArchetype.ROAD_NOMAD),
    "ENTP": (SoloArchetype.URBAN_HIPSTER, SoloArchetype.ROAD_NOMAD),
    "INTJ": (SoloArchetype.URBAN_HIPSTER, SoloArchetype.WELLNESS_DEEPDIVER),
    "ENFJ": (SoloArchetype.URBAN_HIPSTER, SoloArchetype.ACTIVE_CHALLENGER),
    "ESTJ": (SoloArchetype.ACTIVE_CHALLENGER, SoloArchetype.WELLNESS_DEEPDIVER),
    "INFP": (SoloArchetype.ROAD_NOMAD, SoloArchetype.WELLNESS_DEEPDIVER),
    "ENFP": (SoloArchetype.ROAD_NOMAD, SoloArchetype.URBAN_HIPSTER),
    "INFJ": (SoloArchetype.ROAD_NOMAD, SoloArchetype.URBAN_HIPSTER),
    "ISFP": (SoloArchetype.ROAD_NOMAD, SoloArchetype.WELLNESS_DEEPDIVER),
    "ISTJ": (SoloArchetype.WELLNESS_DEEPDIVER, SoloArchetype.ACTIVE_CHALLENGER),
    "ISFJ": (SoloArchetype.WELLNESS_DEEPDIVER, SoloArchetype.URBAN_HIPSTER),
    "INTP": (SoloArchetype.WELLNESS_DEEPDIVER, SoloArchetype.URBAN_HIPSTER),
    "ESFJ": (SoloArchetype.URBAN_HIPSTER, SoloArchetype.WELLNESS_DEEPDIVER),
}


def derive_mbti_traits(
    mbti: Optional[str], primary_archetype: Optional[SoloArchetype] = None
) -> tuple[Optional[str], Optional[str], Optional[SoloArchetype]]:
    """Derives `(social_energy, pace_style, suggested_secondary_archetype)` from a 4-letter MBTI."""
    if not mbti:
        return None, None, None
    code = str(mbti).strip().upper()
    if code not in VALID_MBTI_TYPES:
        raise ValueError(f"Invalid MBTI type: {mbti!r}")
    first_arc, second_arc = MBTI_ARCHETYPE_AFFINITY[code]
    secondary = second_arc if primary_archetype == first_arc else first_arc
    return code[0], code[3], secondary


@dataclass(frozen=True)
class WeatherSnapshot:
    """특정 관측 지역(area_code)의 실시간 기상 스냅샷.
    [INV-02] Fail-Safe: 기본값은 `UNKNOWN` (명시 없는 스냅샷을 맑음으로 간주하지 않는다)."""
    area_code: str = "1100000000"
    weather_class: WeatherClass = WeatherClass.UNKNOWN
    pop_percent: int = 0
    precip_mm: float = 0.0
    snow_mm: float = 0.0
    feels_like_c: float = 20.0
    pm_grade: str = "GOOD"  # GOOD | NORMAL | BAD | VERY_BAD
    has_advisory: bool = False


@dataclass(frozen=True)
class TimelineEntry:
    """나홀로 풀코스 타임라인 1개 구간."""
    time: str          # "10:30", "14:00" 등 KST 시각
    spot: str          # 방문 장소명
    desc: str          # 나홀로 즐기는 구체적 행동 묘사


@dataclass(frozen=True)
class BudgetLine:
    """1인 예산서 1개 항목 (1원 단위 정수)."""
    item: str          # "KTX 왕복 (창가1인석)", "초계국수 1인분"
    cost_krw: int      # 1원 단위 정수 (합산 검증용)
    category: str = "etc"  # "transport", "food", "ticket", "stay", "etc"

    def __post_init__(self) -> None:
        if int(self.cost_krw) < 0:
            raise ValueError(f"BudgetLine {self.item!r} cost_krw must be >= 0")


@dataclass(frozen=True)
class GalleryImage:
    """매거진 갤러리 이미지."""
    url: str           # HTTPS 고화질 이미지 URL
    alt: str           # 접근성 및 이미지 설명

    def __post_init__(self) -> None:
        if not str(self.url).startswith("https://"):
            raise ValueError(f"GalleryImage url must start with https://: {self.url!r}")
        if not self.alt or not self.alt.strip():
            raise ValueError(f"GalleryImage {self.url!r} must have non-empty alt")


@dataclass(frozen=True)
class YoloCuration:
    """[INV-01] 1인 전용 검증 완료 라이프스타일 큐레이션 도메인 모델."""
    id: str
    title: str
    summary: str
    season: Season
    weather_class: WeatherClass
    activity_mode: ActivityMode
    location_name: str
    cost_or_duration: str
    action_url: str
    archetypes: tuple[SoloArchetype, ...]
    solo_tags: tuple[str, ...]
    setting: SettingType = SettingType.HOME
    is_fallback: bool = False
    fallback_for: Optional[str] = None
    priority: int = 100
    is_active: bool = True
    location_area_code: str = "1100000000"
    emoji: str = "✨"
    # === v2.0 비주얼 & 디테일 확장 필드 ===
    hero_image_url: str = ""
    hero_image_alt: str = ""
    gallery_images: tuple[GalleryImage, ...] = ()
    timeline: tuple[TimelineEntry, ...] = ()
    budget_breakdown: tuple[BudgetLine, ...] = ()
    solo_secrets: str = ""
    map_naver_url: str = ""
    map_kakao_url: str = ""
    time_slot: TimeSlot = TimeSlot.ALL
    # === Phase B: 입체 성향 & 1인 실행 코치 필드 ===
    solo_difficulty: int = 2
    social_fit: str = "ALL"  # "ALL" | "I" | "E"
    pace_fit: str = "ALL"    # "ALL" | "J" | "P"
    mbti_tags: tuple[str, ...] = ()
    how_to_steps: tuple[str, ...] = ()
    checklist: tuple[str, ...] = ()

    @property
    def total_budget_krw(self) -> int:
        return sum(b.cost_krw for b in self.budget_breakdown)

    @property
    def budget_known(self) -> bool:
        """Whether this curation carries an explicit budget breakdown.

        An explicit zero-cost line is known to be free; an empty breakdown is
        deliberately kept distinct as an unknown budget ([DATA-01]).
        """
        return bool(self.budget_breakdown)

    @property
    def known_budget_krw(self) -> Optional[int]:
        return self.total_budget_krw if self.budget_breakdown else None

    @property
    def difficulty_label(self) -> str:
        return SOLO_LEVEL_LABELS.get(self.solo_difficulty, f"Lv.{self.solo_difficulty}")

    def __post_init__(self) -> None:
        if not self.id or not self.id.strip():
            raise ValueError("Curation id must not be empty")
        if len(str(self.id).encode("utf-8")) > 53:
            raise ValueError(f"Curation id exceeds 53 bytes for 64-byte callback safety: {self.id!r}")
        if not self.title or not self.title.strip():
            raise ValueError("Curation title must not be empty")
        if not self.archetypes:
            raise ValueError(f"Curation {self.id} must have at least one SoloArchetype")
        object.__setattr__(self, "setting", SettingType(self.setting))
        # [INV-01] 1인 완전성 보장: solo_tags가 없거나 공백이면 절대 생성 불가
        cleaned_tags = tuple(t.strip() for t in self.solo_tags if t and t.strip())
        if not cleaned_tags:
            raise ValueError(f"[INV-01 Violation] Curation {self.id} must have non-empty solo_tags")
        object.__setattr__(self, "solo_tags", cleaned_tags)
        if not str(self.action_url).startswith("https://"):
            raise ValueError(f"Curation {self.id} action_url must start with https://")
        # v2.0: 선택 URL 필드는 비어 있거나 https:// 만 허용 (웹 매거진 href/src 주입 방어)
        for field_name in ("hero_image_url", "map_naver_url", "map_kakao_url"):
            value = getattr(self, field_name)
            if value and not str(value).startswith("https://"):
                raise ValueError(f"Curation {self.id} {field_name} must start with https://")
        object.__setattr__(self, "gallery_images", tuple(self.gallery_images))
        object.__setattr__(self, "timeline", tuple(self.timeline))
        object.__setattr__(self, "budget_breakdown", tuple(self.budget_breakdown))
        object.__setattr__(self, "time_slot", TimeSlot(self.time_slot))

        # Phase B validations
        diff = int(self.solo_difficulty)
        if not 1 <= diff <= 5:
            raise ValueError(f"Curation {self.id} solo_difficulty must be 1..5, got {diff}")
        object.__setattr__(self, "solo_difficulty", diff)

        s_fit = str(self.social_fit or "ALL").strip().upper()
        if s_fit not in ("ALL", "I", "E"):
            raise ValueError(f"Curation {self.id} social_fit must be ALL/I/E, got {self.social_fit!r}")
        object.__setattr__(self, "social_fit", s_fit)

        p_fit = str(self.pace_fit or "ALL").strip().upper()
        if p_fit not in ("ALL", "J", "P"):
            raise ValueError(f"Curation {self.id} pace_fit must be ALL/J/P, got {self.pace_fit!r}")
        object.__setattr__(self, "pace_fit", p_fit)

        cleaned_mbti: list[str] = []
        for m in self.mbti_tags:
            mc = str(m).strip().upper().lstrip("#")
            if not mc:
                continue
            if mc not in VALID_MBTI_TYPES:
                raise ValueError(f"Curation {self.id} has invalid mbti_tag: {m!r}")
            if mc not in cleaned_mbti:
                cleaned_mbti.append(mc)
        object.__setattr__(self, "mbti_tags", tuple(cleaned_mbti))

        cleaned_howto = tuple(str(s).strip() for s in self.how_to_steps if s and str(s).strip())
        if len(cleaned_howto) > 3:
            raise ValueError(f"Curation {self.id} how_to_steps must have at most 3 items")
        object.__setattr__(self, "how_to_steps", cleaned_howto)

        cleaned_check = tuple(str(c).strip() for c in self.checklist if c and str(c).strip())
        if cleaned_check and not (2 <= len(cleaned_check) <= 4):
            raise ValueError(f"Curation {self.id} checklist must have 2..4 items when provided")
        object.__setattr__(self, "checklist", cleaned_check)


@dataclass
class YoloUserProfile:
    """욜로봇 구독 사용자 프로필."""
    chat_id: str
    profile_state: ProfileState = ProfileState.ONBOARDING
    archetype: Optional[SoloArchetype] = None
    weather_area_code: str = "1100000000"
    weather_fallback_enabled: bool = True
    preferred_time_slot: TimeSlot = TimeSlot.ALL
    mbti: Optional[str] = None
    solo_level: int = 3
    secondary_archetype: Optional[SoloArchetype] = None
    social_energy: Optional[str] = None
    pace_style: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now(KST).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(KST).isoformat())

    @property
    def solo_level_label(self) -> str:
        return SOLO_LEVEL_LABELS.get(self.solo_level, f"Lv.{self.solo_level}")

    def __post_init__(self) -> None:
        self.preferred_time_slot = TimeSlot(self.preferred_time_slot)
        if self.secondary_archetype is not None:
            self.secondary_archetype = SoloArchetype(self.secondary_archetype)
        lvl = int(self.solo_level)
        if not 1 <= lvl <= 5:
            raise ValueError(f"solo_level must be 1..5, got {lvl}")
        self.solo_level = lvl

        if self.mbti is not None and str(self.mbti).strip():
            code = str(self.mbti).strip().upper()
            if code not in VALID_MBTI_TYPES:
                raise ValueError(f"Invalid user mbti: {self.mbti!r}")
            self.mbti = code
            # Anti-drift: when MBTI is set, E/I and J/P are always synchronized with it.
            self.social_energy = code[0]
            self.pace_style = code[3]
        else:
            self.mbti = None
            if self.social_energy is not None:
                se = str(self.social_energy).strip().upper()
                if se not in ("I", "E"):
                    raise ValueError(f"Invalid social_energy: {self.social_energy!r}")
                self.social_energy = se
            if self.pace_style is not None:
                ps = str(self.pace_style).strip().upper()
                if ps not in ("J", "P"):
                    raise ValueError(f"Invalid pace_style: {self.pace_style!r}")
                self.pace_style = ps

        if self.profile_state == ProfileState.ONBOARDING and self.archetype is not None:
            raise ValueError("ONBOARDING profile cannot have an active archetype")
        if self.profile_state == ProfileState.ACTIVE and self.archetype is None:
            raise ValueError("ACTIVE profile must have a valid SoloArchetype")
