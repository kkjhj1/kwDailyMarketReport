from __future__ import annotations

from dataclasses import replace

from yolo_brief.channels.telegram import YoloTelegramChannel
from yolo_brief.channels.telegram_card import MAX_CALLBACK_BYTES, measure_message, render_plan_card
from yolo_brief.fixtures.seed_curations import SEED_CURATIONS
from yolo_brief.interactive import YoloInteractiveBot
from yolo_brief.models import (
    ActivityMode,
    BudgetLine,
    ProfileState,
    SettingType,
    SoloArchetype,
    TimeSlot,
    WeatherClass,
    WeatherSnapshot,
    YoloCuration,
    YoloUserProfile,
)
from yolo_brief.planner import (
    BudgetTier,
    MoodFilter,
    PlanQuery,
    SettingFilter,
    parse_plan_command,
    plan_curations,
)
from yolo_brief.storage.db import YoloDatabase


def _active_user(arc: SoloArchetype = SoloArchetype.ACTIVE_CHALLENGER) -> YoloUserProfile:
    return YoloUserProfile(
        chat_id="phase-c-user",
        profile_state=ProfileState.ACTIVE,
        archetype=arc,
    )


def test_phase_c_seed_count_completeness_and_explicit_home_scope() -> None:
    assert len(SEED_CURATIONS) == 56
    home = [c for c in SEED_CURATIONS if c.setting == SettingType.HOME]
    assert len(home) >= 14
    new_home = [c for c in home if "-HOME-" in c.id]
    assert len(new_home) == 14
    assert all(c.activity_mode == ActivityMode.INDOOR for c in new_home)
    assert all(len(c.how_to_steps) == 3 and 2 <= len(c.checklist) <= 4 for c in new_home)
    assert all(c.budget_known and c.timeline and c.solo_secrets for c in new_home)


def test_phase_c_budget_known_distinguishes_unknown_from_free_and_round_trips_setting() -> None:
    unknown = replace(SEED_CURATIONS[0], id="PHASE-C-UNKNOWN", budget_breakdown=())
    free = replace(
        unknown,
        id="PHASE-C-FREE",
        setting=SettingType.HOME,
        budget_breakdown=(BudgetLine("명시적 무료", 0),),
    )
    assert unknown.budget_known is False and unknown.known_budget_krw is None
    assert free.budget_known is True and free.known_budget_krw == 0

    db = YoloDatabase(":memory:")
    db.upsert_curation(free)
    loaded = db.get_curation("PHASE-C-FREE")
    assert loaded is not None
    assert loaded.setting == SettingType.HOME
    assert loaded.budget_known and loaded.known_budget_krw == 0


def test_phase_c_parser_is_strict_and_normalises_conditions() -> None:
    query, error = parse_plan_command("/plan@YoloBot 집콕 무료 2시간 차분")
    assert error is None
    assert query is not None
    assert query.setting == SettingFilter.HOME_ONLY
    assert query.budget_tier == BudgetTier.FREE
    assert query.time_slot == TimeSlot.QUICK_2H
    assert query.mood == MoodFilter.CALM
    assert parse_plan_command("/planx 무료")[0] is None
    assert parse_plan_command("/plan 집콕 외출")[1]
    assert parse_plan_command("/plan " + ("가" * 201))[1]


def test_phase_c_weather_gate_forces_safe_indoor_for_out_only() -> None:
    result = plan_curations(
        _active_user(),
        SEED_CURATIONS,
        PlanQuery(setting=SettingFilter.OUT_ONLY),
        snapshot=WeatherSnapshot(weather_class=WeatherClass.UNKNOWN),
    )
    assert result.weather_forced_indoor is True
    assert result.items
    assert all(c.activity_mode == ActivityMode.INDOOR for c in result.items)


def test_phase_c_budget_cap_is_never_relaxed() -> None:
    result = plan_curations(
        _active_user(SoloArchetype.WELLNESS_DEEPDIVER),
        SEED_CURATIONS,
        PlanQuery(budget_tier=BudgetTier.FREE, budget_cap_krw=0, mood=MoodFilter.ACTIVE),
        snapshot=WeatherSnapshot(weather_class=WeatherClass.CLEAR),
    )
    assert all(c.budget_known and c.known_budget_krw == 0 for c in result.items)


def test_phase_c_plan_card_and_interactive_routes_are_safe() -> None:
    db = YoloDatabase(":memory:")
    db.seed_curations(SEED_CURATIONS)
    user = db.set_user_archetype("phase-c-chat", SoloArchetype.ACTIVE_CHALLENGER)
    result = plan_curations(user, db.list_active_curations(), PlanQuery())
    text, markup = render_plan_card(user, result)
    visible, raw = measure_message(text)
    assert visible <= 1800 and raw <= 3500
    assert "예산 미상" in text or "₩" in text
    callbacks = [
        button.get("callback_data")
        for row in markup["inline_keyboard"]
        for button in row
        if button.get("callback_data")
    ]
    assert {"yolo:plan:all", "yolo:plan:home", "yolo:plan:out", "yolo:plan:0", "yolo:plan:10k", "yolo:plan:30k"} <= set(callbacks)
    assert all(len(c.encode("utf-8")) <= MAX_CALLBACK_BYTES for c in callbacks)

    channel = YoloTelegramChannel(token="", dry_run=True)
    bot = YoloInteractiveBot(db=db, channel=channel, default_weather=WeatherSnapshot(weather_class=WeatherClass.CLEAR))
    bot.handle_update({"message": {"chat": {"id": "phase-c-chat"}, "text": "/plan 무료"}})
    assert "맞춤 플랜" in channel.call_log[-1][1]["text"]
    bot.handle_update({"callback_query": {"id": "phase-c-cb", "data": "yolo:plan:home", "message": {"chat": {"id": "phase-c-chat"}, "message_id": 1}}})
    assert channel.call_log[-2][0] == "answerCallbackQuery"
