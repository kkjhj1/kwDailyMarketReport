"""Domain models for YOLO Bot (욜로봇).
Defines 4 Solo Archetypes, multi-dimensional metadata (activity_mode, mobility_scope, setting),
ProfileState, Season, WeatherClass, WeatherSnapshot, YoloCuration, and YoloUserProfile.
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


AREA_NAMES: dict[str, str] = {
    "1100000000": "서울·수도권",
    "2600000000": "부산·경남",
    "2700000000": "대구·경북",
    "2900000000": "광주·호남",
    "3000000000": "대전·충청",
    "5000000000": "제주",
    "5100000000": "강원",
}


@dataclass(frozen=True)
class WeatherSnapshot:
    """특정 관측 지역(area_code)의 실시간 기상 스냅샷."""
    area_code: str = "1100000000"
    weather_class: WeatherClass = WeatherClass.CLEAR
    pop_percent: int = 0
    precip_mm: float = 0.0
    snow_mm: float = 0.0
    feels_like_c: float = 20.0
    pm_grade: str = "GOOD"  # GOOD | NORMAL | BAD | VERY_BAD
    has_advisory: bool = False


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
    is_fallback: bool = False
    fallback_for: Optional[str] = None
    priority: int = 100
    is_active: bool = True
    location_area_code: str = "1100000000"
    emoji: str = "✨"

    def __post_init__(self) -> None:
        if not self.id or not self.id.strip():
            raise ValueError("Curation id must not be empty")
        if not self.title or not self.title.strip():
            raise ValueError("Curation title must not be empty")
        if not self.archetypes:
            raise ValueError(f"Curation {self.id} must have at least one SoloArchetype")
        # [INV-01] 1인 완전성 보장: solo_tags가 없거나 공백이면 절대 생성 불가
        cleaned_tags = tuple(t.strip() for t in self.solo_tags if t and t.strip())
        if not cleaned_tags:
            raise ValueError(f"[INV-01 Violation] Curation {self.id} must have non-empty solo_tags")
        object.__setattr__(self, "solo_tags", cleaned_tags)
        if not str(self.action_url).startswith("https://"):
            raise ValueError(f"Curation {self.id} action_url must start with https://")


@dataclass
class YoloUserProfile:
    """욜로봇 구독 사용자 프로필."""
    chat_id: str
    profile_state: ProfileState = ProfileState.ONBOARDING
    archetype: Optional[SoloArchetype] = None
    weather_area_code: str = "1100000000"
    weather_fallback_enabled: bool = True
    created_at: str = field(default_factory=lambda: datetime.now(KST).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(KST).isoformat())

    def __post_init__(self) -> None:
        if self.profile_state == ProfileState.ONBOARDING and self.archetype is not None:
            raise ValueError("ONBOARDING profile cannot have an active archetype")
        if self.profile_state == ProfileState.ACTIVE and self.archetype is None:
            raise ValueError("ACTIVE profile must have a valid SoloArchetype")
