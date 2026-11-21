"""Conditional `/plan` parser and safe curation planner for YOLO Bot.

The planner intentionally keeps the command surface small.  User-provided
natural language is parsed into enums first; renderers only receive those
normalised values and never echo the original command.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from enum import Enum
import re
from typing import Any, Mapping, Optional, Sequence

from yolo_brief.models import (
    ActivityMode,
    Season,
    SettingType,
    SoloArchetype,
    TimeSlot,
    WeatherClass,
    WeatherSnapshot,
    YoloCuration,
    YoloUserProfile,
)
from yolo_brief.weather_engine import evaluate_weather_gate, get_kst_season


class BudgetTier(str, Enum):
    ALL = "ALL"
    FREE = "FREE"
    FREE_OR_10K = "FREE_OR_10K"
    UNDER_30K = "UNDER_30K"
    FLEX = "FLEX"

    @property
    def cap_krw(self) -> Optional[int]:
        return {
            BudgetTier.ALL: None,
            BudgetTier.FREE: 0,
            BudgetTier.FREE_OR_10K: 10_000,
            BudgetTier.UNDER_30K: 30_000,
            BudgetTier.FLEX: None,
        }[self]

    @property
    def label(self) -> str:
        return {
            BudgetTier.ALL: "예산 전체",
            BudgetTier.FREE: "무료",
            BudgetTier.FREE_OR_10K: "1만원 이하",
            BudgetTier.UNDER_30K: "3만원 이하",
            BudgetTier.FLEX: "예산 자유",
        }[self]


class SettingFilter(str, Enum):
    ALL = "ALL"
    HOME_ONLY = "HOME_ONLY"
    OUT_ONLY = "OUT_ONLY"

    @property
    def label(self) -> str:
        return {
            SettingFilter.ALL: "공간 전체",
            SettingFilter.HOME_ONLY: "집콕",
            SettingFilter.OUT_ONLY: "외출",
        }[self]


class MoodFilter(str, Enum):
    ALL = "ALL"
    ACTIVE = "ACTIVE"
    CALM = "CALM"
    LEARN = "LEARN"

    @property
    def label(self) -> str:
        return {
            MoodFilter.ALL: "기분 전체",
            MoodFilter.ACTIVE: "활동",
            MoodFilter.CALM: "차분",
            MoodFilter.LEARN: "배움",
        }[self]


@dataclass(frozen=True)
class PlanQuery:
    time_slot: TimeSlot = TimeSlot.ALL
    budget_tier: BudgetTier = BudgetTier.ALL
    setting: SettingFilter = SettingFilter.ALL
    mood: MoodFilter = MoodFilter.ALL
    budget_cap_krw: Optional[int] = None
    # Constructor aliases make the public object tolerant of callers that use
    # the enum names (`setting_filter` / `mood_filter`) from the audit wording.
    setting_filter: Optional[SettingFilter] = None
    mood_filter: Optional[MoodFilter] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "time_slot", TimeSlot(self.time_slot))
        object.__setattr__(self, "budget_tier", BudgetTier(self.budget_tier))
        setting = SettingFilter(self.setting_filter) if self.setting_filter is not None else SettingFilter(self.setting)
        mood = MoodFilter(self.mood_filter) if self.mood_filter is not None else MoodFilter(self.mood)
        object.__setattr__(self, "setting", setting)
        object.__setattr__(self, "mood", mood)
        object.__setattr__(self, "setting_filter", setting)
        object.__setattr__(self, "mood_filter", mood)
        cap = self.budget_cap_krw
        if cap is None:
            cap = self.budget_tier.cap_krw
        if cap is not None and int(cap) < 0:
            raise ValueError("budget_cap_krw must be >= 0")
        object.__setattr__(self, "budget_cap_krw", int(cap) if cap is not None else None)

@dataclass
class PlanResult:
    items: list[YoloCuration]
    query: PlanQuery
    weather_forced_indoor: bool = False
    effective_weather: WeatherClass = WeatherClass.UNKNOWN
    weather_reason: str = "기상 정보 미확인 (안전 실내 모드)"
    relaxed_mood: bool = False
    relaxed_time_slot: bool = False
    effective_setting: SettingFilter = SettingFilter.ALL

    @property
    def mood_relaxed(self) -> bool:
        return self.relaxed_mood

    @property
    def time_slot_relaxed(self) -> bool:
        return self.relaxed_time_slot

    @property
    def relaxation_labels(self) -> tuple[str, ...]:
        labels: list[str] = []
        if self.relaxed_mood:
            labels.append("기분 조건 완화")
        if self.relaxed_time_slot:
            labels.append("시간대 조건 완화")
        return tuple(labels)


_COMMAND_RE = re.compile(r"^/plan(?:@[A-Za-z0-9_]+)?(?:\s+(?P<args>.*))?$", re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")


def _contains_any(text: str, words: Sequence[str]) -> bool:
    return any(word in text for word in words)


def parse_plan_command(text: str) -> tuple[Optional[PlanQuery], Optional[str]]:
    """Parse exactly `/plan` or `/plan@BotName` plus a small option vocabulary."""
    raw = str(text or "").strip()
    if len(raw) > 200:
        return None, "맞춤 플랜 명령은 200자 이내로 입력해 주세요."
    if _CONTROL_RE.search(raw):
        return None, "맞춤 플랜 명령에 사용할 수 없는 제어 문자가 있어요."
    match = _COMMAND_RE.fullmatch(raw)
    if not match:
        if re.match(r"^/plan", raw, re.IGNORECASE):
            return None, "맞춤 플랜은 /plan 또는 /plan@BotName 형식으로 입력해 주세요."
        return None, None
    args = (match.group("args") or "").strip()

    wants_home = _contains_any(args, ("집콕", "집에서", "홈", "집 안"))
    wants_out = _contains_any(args, ("외출", "밖", "야외"))
    if wants_home and wants_out:
        return None, "공간 조건은 집콕과 외출 중 하나만 선택해 주세요."

    budget = BudgetTier.ALL
    cap: Optional[int] = None
    if _contains_any(args, ("무료", "0원")):
        budget, cap = BudgetTier.FREE, 0
    elif re.search(r"(?:1만원|10,?000원)", args):
        budget, cap = BudgetTier.FREE_OR_10K, 10_000
    elif re.search(r"(?:3만원|30,?000원)", args):
        budget, cap = BudgetTier.UNDER_30K, 30_000
    elif "자유" in args:
        budget = BudgetTier.FLEX

    slot = TimeSlot.ALL
    if _contains_any(args, ("2시간", "두시간", "2h")):
        slot = TimeSlot.QUICK_2H
    elif "반나절" in args:
        slot = TimeSlot.HALF_DAY
    elif _contains_any(args, ("하루", "1박", "종일")):
        slot = TimeSlot.FULL_OR_TRIP

    mood = MoodFilter.ALL
    if _contains_any(args, ("활동", "운동", "액티브")):
        mood = MoodFilter.ACTIVE
    elif _contains_any(args, ("차분", "휴식", "힐링")):
        mood = MoodFilter.CALM
    elif _contains_any(args, ("배움", "학습", "자기계발", "공부")):
        mood = MoodFilter.LEARN

    setting = SettingFilter.HOME_ONLY if wants_home else (SettingFilter.OUT_ONLY if wants_out else SettingFilter.ALL)
    return PlanQuery(
        time_slot=slot,
        budget_tier=budget,
        setting=setting,
        mood=mood,
        budget_cap_krw=cap,
    ), None


def _mood_matches(curation: YoloCuration, mood: MoodFilter) -> bool:
    if mood == MoodFilter.ALL:
        return True
    arcs = set(curation.archetypes)
    if mood == MoodFilter.ACTIVE:
        return SoloArchetype.ACTIVE_CHALLENGER in arcs
    if mood == MoodFilter.CALM:
        return SoloArchetype.WELLNESS_DEEPDIVER in arcs
    return SoloArchetype.URBAN_HIPSTER in arcs


def _setting_matches(curation: YoloCuration, setting: SettingFilter) -> bool:
    if setting == SettingFilter.ALL:
        return True
    if setting == SettingFilter.HOME_ONLY:
        return curation.setting == SettingType.HOME
    return curation.setting in (SettingType.URBAN, SettingType.OUTDOOR)


def _budget_matches(curation: YoloCuration, query: PlanQuery) -> bool:
    cap = query.budget_cap_krw
    if cap is None:
        return True
    # [DATA-01] Unknown budgets never pass a bounded tier, including FREE.
    return curation.budget_known and curation.known_budget_krw is not None and curation.known_budget_krw <= cap


def _rank_plan(
    items: Sequence[YoloCuration],
    user: YoloUserProfile,
    user_stats: Optional[Mapping[str, Mapping[str, Any]]],
) -> list[YoloCuration]:
    stats = user_stats or {}

    def key(item: YoloCuration) -> tuple[float, int, str]:
        profile_bonus = 1 if user.archetype and user.archetype in item.archetypes else 0
        seen = int((stats.get(item.id) or {}).get("seen_30d_count") or 0)
        return (-(float(item.priority) + profile_bonus * 20 - min(seen, 3) * 8), -item.priority, item.id)

    return sorted(items, key=key)


def plan_curations(
    user: YoloUserProfile,
    curations: Sequence[YoloCuration],
    query: PlanQuery,
    snapshot: Optional[WeatherSnapshot] = None,
    ref_date: Optional[date] = None,
    user_stats: Optional[Mapping[str, Mapping[str, Any]]] = None,
    limit: int = 3,
) -> PlanResult:
    """Return a safe, conditionally filtered plan without relaxing its budget."""
    query = query if isinstance(query, PlanQuery) else PlanQuery(**dict(query))
    take = max(1, int(limit))
    severe, weather, reason = evaluate_weather_gate(snapshot)
    forced = severe and query.setting == SettingFilter.OUT_ONLY
    effective_setting = SettingFilter.ALL if forced else query.setting
    season = get_kst_season(ref_date)

    base: list[YoloCuration] = []
    for curation in curations:
        if not curation.is_active or not curation.solo_tags:
            continue
        if curation.season not in (season, Season.ALL):
            continue
        if user.archetype is not None and not any(
            arc in curation.archetypes for arc in (user.archetype, user.secondary_archetype)
            if arc is not None
        ):
            continue
        # Safety is applied before any later relaxation and is never undone.
        if severe and curation.activity_mode == ActivityMode.OUTDOOR:
            continue
        if not _setting_matches(curation, effective_setting):
            continue
        if not _budget_matches(curation, query):
            continue
        base.append(curation)

    def select(*, use_mood: bool, use_slot: bool) -> list[YoloCuration]:
        pool = base
        if use_mood and query.mood != MoodFilter.ALL:
            pool = [c for c in pool if _mood_matches(c, query.mood)]
        if use_slot and query.time_slot != TimeSlot.ALL:
            pool = [c for c in pool if c.time_slot in (query.time_slot, TimeSlot.ALL)]
        return _rank_plan(pool, user, user_stats)[:take]

    target = min(3, take)
    items = select(use_mood=True, use_slot=True)
    relaxed_mood = False
    relaxed_slot = False
    if len(items) < target and query.mood != MoodFilter.ALL:
        relaxed_mood = True
        items = select(use_mood=False, use_slot=True)
    if len(items) < target and query.time_slot != TimeSlot.ALL:
        relaxed_slot = True
        items = select(use_mood=False, use_slot=False)

    return PlanResult(
        items=items,
        query=query,
        weather_forced_indoor=forced,
        effective_weather=weather,
        weather_reason=reason,
        relaxed_mood=relaxed_mood,
        relaxed_time_slot=relaxed_slot,
        effective_setting=effective_setting,
    )


__all__ = [
    "BudgetTier",
    "SettingFilter",
    "MoodFilter",
    "PlanQuery",
    "PlanResult",
    "parse_plan_command",
    "plan_curations",
]
