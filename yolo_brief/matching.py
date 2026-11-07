"""Curation matching & Rainy Day Fallback engine for YOLO Bot.
Implements GPT audit hard gates:
1. [INV-01] Every matched curation MUST have non-empty solo_tags.
2. [INV-02] When severe weather (or UNKNOWN) occurs:
   - Never expose OUTDOOR items (even if weather_class == ALL).
   - For outdoor-centric archetypes (ACTIVE_CHALLENGER, ROAD_NOMAD):
     - If weather_fallback_enabled is True: select INDOOR fallback items for that archetype.
     - If weather_fallback_enabled is False: block outdoor items and return empty with blocked_by_weather_no_fallback=True.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Sequence

from yolo_brief.models import (
    ActivityMode,
    ProfileState,
    Season,
    SoloArchetype,
    WeatherClass,
    WeatherSnapshot,
    YoloCuration,
    YoloUserProfile,
)
from yolo_brief.weather_engine import evaluate_weather_gate, get_kst_season


@dataclass
class MatchResult:
    items: list[YoloCuration] = field(default_factory=list)
    is_severe_weather: bool = False
    effective_weather: WeatherClass = WeatherClass.CLEAR
    weather_reason: str = "맑음·활동 양호"
    fallback_applied: bool = False
    blocked_by_weather_no_fallback: bool = False
    season: Season = Season.AUTUMN


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


def match_curations(
    user: YoloUserProfile,
    curations: Sequence[YoloCuration],
    snapshot: Optional[WeatherSnapshot] = None,
    target_season: Optional[Season] = None,
    ref_date: Optional[date] = None,
    limit: int = 3,
) -> MatchResult:
    """Matches up to `limit` solo curations for the user's archetype, season, and weather."""
    season = target_season or get_kst_season(ref_date)
    is_severe, eff_weather, reason = evaluate_weather_gate(snapshot)

    if user.profile_state != ProfileState.ACTIVE or user.archetype is None:
        return MatchResult(
            items=[],
            is_severe_weather=is_severe,
            effective_weather=eff_weather,
            weather_reason=reason,
            fallback_applied=False,
            blocked_by_weather_no_fallback=False,
            season=season,
        )

    arc = user.archetype

    # Base pool: active, non-empty solo_tags, matching archetype, matching season
    pool: list[YoloCuration] = []
    for c in curations:
        if not c.is_active or not c.solo_tags:
            continue
        if arc not in c.archetypes:
            continue
        if c.season not in (season, Season.ALL):
            continue
        pool.append(c)

    if is_severe:
        # Hard Gate: NEVER expose OUTDOOR items during severe/unknown weather!
        indoor_only = [c for c in pool if c.activity_mode == ActivityMode.INDOOR]

        if arc.is_outdoor_centric:
            if not user.weather_fallback_enabled:
                return MatchResult(
                    items=[],
                    is_severe_weather=True,
                    effective_weather=eff_weather,
                    weather_reason=reason,
                    fallback_applied=False,
                    blocked_by_weather_no_fallback=True,
                    season=season,
                )
            # Prefer explicit fallback items first, then any safe indoor item for this archetype
            fallback_candidates = [c for c in indoor_only if c.is_fallback]
            candidates = fallback_candidates if fallback_candidates else indoor_only
            ranked = sorted(
                candidates,
                key=lambda item: _score_curation(item, eff_weather, season, True),
                reverse=True,
            )
            return MatchResult(
                items=ranked[: max(1, limit)],
                is_severe_weather=True,
                effective_weather=eff_weather,
                weather_reason=reason,
                fallback_applied=True,
                blocked_by_weather_no_fallback=False,
                season=season,
            )
        else:
            # URBAN_HIPSTER or WELLNESS_DEEPDIVER: filter out any outdoor popups/fleamarkets
            ranked = sorted(
                indoor_only,
                key=lambda item: _score_curation(item, eff_weather, season, True),
                reverse=True,
            )
            return MatchResult(
                items=ranked[: max(1, limit)],
                is_severe_weather=True,
                effective_weather=eff_weather,
                weather_reason=reason,
                fallback_applied=False,
                blocked_by_weather_no_fallback=False,
                season=season,
            )

    # Clear weather: prefer non-fallback regular curations
    regular = [c for c in pool if not c.is_fallback]
    candidates = regular if regular else pool
    ranked = sorted(
        candidates,
        key=lambda item: _score_curation(item, eff_weather, season, False),
        reverse=True,
    )
    return MatchResult(
        items=ranked[: max(1, limit)],
        is_severe_weather=False,
        effective_weather=eff_weather,
        weather_reason=reason,
        fallback_applied=False,
        blocked_by_weather_no_fallback=False,
        season=season,
    )
