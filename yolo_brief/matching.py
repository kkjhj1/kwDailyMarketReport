"""Curation matching & Rainy Day Fallback engine for YOLO Bot.
Implements GPT audit hard gates:
1. [INV-01] Every matched curation MUST have non-empty solo_tags.
2. [INV-02] When severe weather (or UNKNOWN) occurs:
   - Never expose OUTDOOR items (even if weather_class == ALL).
   - For outdoor-centric archetypes (ACTIVE_CHALLENGER, ROAD_NOMAD):
     - If weather_fallback_enabled is True: select INDOOR fallback items for that archetype.
     - If weather_fallback_enabled is False: block outdoor items and return empty with blocked_by_weather_no_fallback=True.

Phase A filter order (fixed):
    archetype / active / solo_tags -> season -> [INV-02] weather safety gate -> TimeSlot
TimeSlot relaxation (only when fewer than `MIN_SLOT_CANDIDATES` remain):
    (1) selected slot + `TimeSlot.ALL`, (2) other slots from the SAME weather-gated pool
    (`time_slot_relaxed=True`). The relaxation pool is already weather-gated, so OUTDOOR items can
    never be added under severe/UNKNOWN weather.
Rotation score (per user activity history):
    score = base - seen_penalty - done_penalty - skip_penalty + saved_bonus
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import math
from typing import Any, Mapping, Optional, Sequence

from yolo_brief.models import (
    ActivityMode,
    ProfileState,
    Season,
    SoloArchetype,
    TimeSlot,
    WeatherClass,
    WeatherSnapshot,
    YoloCuration,
    YoloUserProfile,
)
from yolo_brief.weather_engine import evaluate_weather_gate, get_kst_season

MIN_SLOT_CANDIDATES = 3
TIME_SLOT_RELAXED_NOTICE = "선택 시간대 후보가 부족해 다른 시간대에서 보강했어요"

UserStats = Mapping[str, Mapping[str, Any]]


@dataclass
class MatchResult:
    items: list[YoloCuration] = field(default_factory=list)
    is_severe_weather: bool = False
    effective_weather: WeatherClass = WeatherClass.CLEAR
    weather_reason: str = "맑음·활동 양호"
    fallback_applied: bool = False
    blocked_by_weather_no_fallback: bool = False
    season: Season = Season.AUTUMN
    time_slot: TimeSlot = TimeSlot.ALL
    time_slot_relaxed: bool = False


def _score_curation(
    curation: YoloCuration,
    effective_weather: WeatherClass,
    target_season: Season,
    is_severe: bool,
) -> float:
    # 1. Weather match (40 pts)
    if curation.weather_class == effective_weather:
        weather_score = 40.0
    elif is_severe and curation.is_fallback:
        weather_score = 32.0
    elif curation.weather_class == WeatherClass.ALL:
        weather_score = 20.0
    else:
        weather_score = 10.0

    # 2. Season match (25 pts)
    if curation.season == target_season:
        season_score = 25.0
    elif curation.season == Season.ALL:
        season_score = 15.0
    else:
        season_score = 0.0

    # 3. Operational priority (20 pts scaled around priority=100)
    priority_score = 20.0 * (curation.priority / 100.0)

    # 4. Data freshness / baseline (15 pts)
    freshness_score = 15.0

    return weather_score + season_score + priority_score + freshness_score


def personality_bonus(curation: YoloCuration, user: YoloUserProfile) -> float:
    """Bounded personality match bonus (max +24.0 pts):
    - primary archetype match: +6.0 (or secondary archetype match: +3.0)
    - exact MBTI tag match: +8.0
    - social_fit (I/E) match: +4.0
    - pace_fit (J/P) match: +4.0
    Mismatches or unset attributes receive 0.0 (never negative) to prevent pool fragmentation.
    """
    bonus = 0.0
    if user.archetype is not None and user.archetype in curation.archetypes:
        bonus += 6.0
    elif user.secondary_archetype is not None and user.secondary_archetype in curation.archetypes:
        bonus += 3.0

    if user.mbti and user.mbti in curation.mbti_tags:
        bonus += 8.0
    if user.social_energy and curation.social_fit == user.social_energy:
        bonus += 4.0
    if user.pace_style and curation.pace_fit == user.pace_style:
        bonus += 4.0
    return min(24.0, bonus)


def difficulty_penalty(curation: YoloCuration, user: YoloUserProfile) -> float:
    """Bounded difficulty penalty (max 24.0 pts) when curation difficulty exceeds user's solo_level."""
    over = max(0, int(curation.solo_difficulty) - int(user.solo_level))
    return min(24.0, 6.0 * over)


def rotation_adjustment(stat: Optional[Mapping[str, Any]]) -> float:
    """`- seen_penalty - done_penalty - skip_penalty + saved_bonus` for one curation."""
    if not stat:
        return 0.0
    adj = 0.0
    seen_count = int(stat.get("seen_30d_count") or 0)
    days_seen = stat.get("days_since_seen")
    if seen_count > 0 or days_seen is not None:
        recency = 24.0 * math.exp(-float(days_seen) / 14.0) if days_seen is not None else 0.0
        adj -= min(45.0, 12.0 * min(seen_count, 3) + recency)
    days_done = stat.get("days_since_done")
    if days_done is not None:
        adj -= 50.0 * math.exp(-float(days_done) / 45.0)
    days_skip = stat.get("days_since_skip")
    if days_skip is not None:
        adj -= 35.0 * math.exp(-float(days_skip) / 30.0)
    if stat.get("has_recent_saved"):
        adj += 8.0
    return adj


def _rank(
    items: Sequence[YoloCuration],
    effective_weather: WeatherClass,
    season: Season,
    is_severe: bool,
    user_stats: Optional[UserStats],
    user: Optional[YoloUserProfile] = None,
) -> list[YoloCuration]:
    stats = user_stats or {}

    def key(item: YoloCuration) -> tuple[float, str, int, str]:
        stat = stats.get(item.id)
        p_adj = (
            personality_bonus(item, user) - difficulty_penalty(item, user)
            if user is not None
            else 0.0
        )
        score = (
            _score_curation(item, effective_weather, season, is_severe)
            + p_adj
            + rotation_adjustment(stat)
        )
        last_seen = str((stat or {}).get("last_seen_iso") or "")
        # Highest score first; tie-breaker `(last_seen_iso or "", -priority, id)`
        return (-round(score, 9), last_seen, -item.priority, item.id)

    return sorted(items, key=key)


def _select_with_time_slot(
    pool: Sequence[YoloCuration],
    slot: TimeSlot,
    limit: int,
    effective_weather: WeatherClass,
    season: Season,
    is_severe: bool,
    user_stats: Optional[UserStats],
    user: Optional[YoloUserProfile] = None,
) -> tuple[list[YoloCuration], bool]:
    """Applies the TimeSlot filter on an already weather-gated pool. Returns `(items, relaxed)`."""
    take = max(1, limit)
    if is_severe:
        # Defense in depth for [INV-02]: the pool must never carry OUTDOOR items here.
        pool = [c for c in pool if c.activity_mode != ActivityMode.OUTDOOR]

    if slot == TimeSlot.ALL:
        return _rank(pool, effective_weather, season, is_severe, user_stats, user=user)[:take], False

    exact = [c for c in pool if c.time_slot == slot]
    if len(exact) >= MIN_SLOT_CANDIDATES:
        return _rank(exact, effective_weather, season, is_severe, user_stats, user=user)[:take], False

    # (1) selected slot + ALL
    merged = exact + [c for c in pool if c.time_slot == TimeSlot.ALL]
    ranked_primary = _rank(merged, effective_weather, season, is_severe, user_stats, user=user)
    if len(merged) >= MIN_SLOT_CANDIDATES:
        return ranked_primary[:take], False

    # (2) other slots from the same (season/weather/archetype) gated pool
    merged_ids = {c.id for c in merged}
    others = [c for c in pool if c.id not in merged_ids]
    if not others:
        return ranked_primary[:take], False
    ranked_others = _rank(others, effective_weather, season, is_severe, user_stats, user=user)
    need = max(0, take - len(ranked_primary))
    supplement = ranked_others[:need]
    return (ranked_primary + supplement)[:take], bool(supplement)


def match_curations(
    user: YoloUserProfile,
    curations: Sequence[YoloCuration],
    snapshot: Optional[WeatherSnapshot] = None,
    target_season: Optional[Season] = None,
    ref_date: Optional[date] = None,
    limit: int = 3,
    time_slot: Optional[TimeSlot] = None,
    user_stats: Optional[UserStats] = None,
) -> MatchResult:
    """Matches up to `limit` solo curations for the user's archetype, season, weather and time slot.

    `time_slot` defaults to `user.preferred_time_slot`; `user_stats` is the output of
    `YoloDatabase.get_user_curation_stats()` (rotation penalties/bonus)."""
    season = target_season or get_kst_season(ref_date)
    is_severe, eff_weather, reason = evaluate_weather_gate(snapshot)
    slot = TimeSlot(time_slot) if time_slot is not None else TimeSlot(user.preferred_time_slot)

    def result(
        items: list[YoloCuration],
        fallback_applied: bool = False,
        blocked: bool = False,
        relaxed: bool = False,
    ) -> MatchResult:
        return MatchResult(
            items=items,
            is_severe_weather=is_severe,
            effective_weather=eff_weather,
            weather_reason=reason,
            fallback_applied=fallback_applied,
            blocked_by_weather_no_fallback=blocked,
            season=season,
            time_slot=slot,
            time_slot_relaxed=relaxed,
        )

    if user.profile_state != ProfileState.ACTIVE or user.archetype is None:
        return result([])

    arc: SoloArchetype = user.archetype
    allowed_arcs = {arc}
    if user.secondary_archetype is not None:
        allowed_arcs.add(user.secondary_archetype)

    # 1-2. Base pool: active, non-empty solo_tags, matching primary/secondary archetype, matching season
    pool: list[YoloCuration] = []
    for c in curations:
        if not c.is_active or not c.solo_tags:
            continue
        if not any(a in allowed_arcs for a in c.archetypes):
            continue
        if c.season not in (season, Season.ALL):
            continue
        pool.append(c)

    # 3. [INV-02] Weather safety gate
    fallback_applied = False
    if is_severe:
        # Hard Gate: NEVER expose OUTDOOR items during severe/unknown weather!
        indoor_only = [c for c in pool if c.activity_mode == ActivityMode.INDOOR]
        if arc.is_outdoor_centric:
            if not user.weather_fallback_enabled:
                return result([], blocked=True)
            # Prefer explicit fallback items first, then any safe indoor item for this archetype
            fallback_candidates = [c for c in indoor_only if c.is_fallback]
            gated = fallback_candidates if fallback_candidates else indoor_only
            fallback_applied = True
        else:
            # URBAN_HIPSTER or WELLNESS_DEEPDIVER: filter out any outdoor popups/fleamarkets
            gated = indoor_only
    else:
        # Clear weather: prefer non-fallback regular curations
        regular = [c for c in pool if not c.is_fallback]
        gated = regular if regular else pool

    # 4. TimeSlot filter (+ safe relaxation inside the gated pool)
    items, relaxed = _select_with_time_slot(
        gated, slot, limit, eff_weather, season, is_severe, user_stats, user=user
    )
    return result(items, fallback_applied=fallback_applied, relaxed=relaxed)


# Phase C public planner exports are kept here as compatibility shims for
# callers that historically imported all recommendation functions from this
# module.  The implementation lives in planner.py to keep command parsing and
# legacy weather matching separate.
from yolo_brief.planner import (  # noqa: E402  (intentional late import)
    BudgetTier,
    MoodFilter,
    PlanQuery,
    PlanResult,
    SettingFilter,
    parse_plan_command,
    plan_curations,
)
