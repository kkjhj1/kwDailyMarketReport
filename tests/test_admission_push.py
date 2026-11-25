"""v2 personalized Admission push contracts."""
from datetime import datetime
from unittest.mock import MagicMock
from zoneinfo import ZoneInfo

from admission_brief.catalog import KnowledgeCatalog
from admission_brief.channels.telegram import TelegramChannel
from admission_brief.channels.telegram_limiter import SafeTelegramDispatcher, TelegramRateLimiter
from admission_brief.interactive_bot import AdmissionInteractiveBot
from admission_brief.models import (
    AdmissionEvent,
    BriefingType,
    ChildProfile,
    EducationStage,
    GradeLevel,
    ParentUserProfile,
    TargetGrade,
    TargetRegion,
)
from admission_brief.push_engine import (
    build_event_dday_pushes,
    build_policy_pushes,
    build_weekly_personalized_pushes,
)
from admission_brief.storage.db import AdmissionDatabase

KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 11, 22, 20, 0, tzinfo=KST)


def _exact_high_profile(chat_id: str) -> ParentUserProfile:
    return ParentUserProfile(
        chat_id=chat_id,
        children=[ChildProfile(
            alias="첫째", education_stage=EducationStage.HIGH, grade_level=GradeLevel.H1,
        )],
    )


def _callback(data: str, chat_id: int = 10) -> dict:
    return {"id": "push-cb", "data": data, "from": {"id": chat_id},
            "message": {"message_id": 7, "chat": {"id": chat_id}}}


def test_preferences_are_additive_and_upsert_does_not_reset_opt_out():
    db = AdmissionDatabase(":memory:")
    db.upsert_subscriber(ParentUserProfile(chat_id="p", target_grade=TargetGrade.HIGH_1_2))
    assert db.update_alert_preferences("p", weekly=False, dday=False)
    db.upsert_subscriber(ParentUserProfile(chat_id="p", target_grade=TargetGrade.MIDDLE))
    profile = db.get_subscriber("p")
    assert profile.weekly_guide_enabled is False
    assert profile.event_dday_enabled is False
    assert profile.policy_breaking_enabled is True
    assert profile.to_dict()["policy_breaking_enabled"] is True


def test_weekly_consolidates_children_and_is_silent_when_fresh_items_are_gone():
    db = AdmissionDatabase(":memory:")
    profile = _exact_high_profile("p")
    profile.children.append(ChildProfile(alias="둘째", education_stage=EducationStage.ELEMENTARY, grade_level=GradeLevel.E1))
    db.upsert_subscriber(profile)
    db.add_child("p", ChildProfile(alias="둘째", education_stage=EducationStage.ELEMENTARY, grade_level=GradeLevel.E1))
    pushes = build_weekly_personalized_pushes(db, KnowledgeCatalog(), NOW)
    assert len(pushes) == 1 and pushes[0].chat_id == "p"
    assert pushes[0].briefing.briefing_id == "push:weekly:2026W47:p"
    assert len(pushes[0].item_ids) >= 1
    for row in pushes[0].reply_markup["inline_keyboard"]:
        for button in row:
            if button["callback_data"].startswith("adm:ack:"):
                assert len(button["callback_data"].encode()) <= 64

    channel = MagicMock(spec=TelegramChannel)
    channel.send.return_value = "message"
    dispatcher = SafeTelegramDispatcher(TelegramRateLimiter(1000, 0))
    assert dispatcher.dispatch_personalized(pushes, db, channel)["sent"] == 1
    assert build_weekly_personalized_pushes(db, KnowledgeCatalog(), NOW) == []


def test_dday_is_strictly_d3_or_d1_and_has_no_past_compensation():
    db = AdmissionDatabase(":memory:")
    db.upsert_subscriber(ParentUserProfile(chat_id="p", target_grade=TargetGrade.HIGH_1_2))
    db.save_event(AdmissionEvent(
        event_id="event-dday", title="행사", organizer="주최", target_grade=TargetGrade.HIGH_1_2,
        event_type=BriefingType.BRIEFING_RECAP, event_date_str="2026-11-25 10:00",
    ))
    assert len(build_event_dday_pushes(db, NOW)) == 1
    assert build_event_dday_pushes(db, datetime(2026, 11, 23, 20, 0, tzinfo=KST)) == []
    assert build_event_dday_pushes(db, datetime(2026, 11, 26, 8, 0, tzinfo=KST)) == []


def test_policy_stream_obeys_opt_out_and_ack_callback_checks_ownership():
    db = AdmissionDatabase(":memory:")
    db.upsert_subscriber(_exact_high_profile("10"))
    pushes = build_policy_pushes(db, KnowledgeCatalog(), NOW)
    assert pushes and pushes[0].stream == "policy"

    channel = MagicMock(spec=TelegramChannel)
    channel.edit_message_text.return_value = True
    bot = AdmissionInteractiveBot(channel, db)
    # No successful sent-history ownership: the callback is acknowledged but not recorded.
    bot.handle_update({"callback_query": _callback("adm:ack:policy-2028-reform")})
    assert not db.is_card_acked("10", "policy-2028-reform")
    assert channel.answer_callback_query.call_count == 1


def test_personalized_dry_run_and_unknown_never_write_content_history():
    db = AdmissionDatabase(":memory:")
    db.upsert_subscriber(_exact_high_profile("p"))
    push = build_weekly_personalized_pushes(db, KnowledgeCatalog(), NOW)[0]
    channel = MagicMock(spec=TelegramChannel)
    channel.send.side_effect = TimeoutError("ambiguous")
    dispatcher = SafeTelegramDispatcher(TelegramRateLimiter(1000, 0))
    dry = dispatcher.dispatch_personalized([push], db, channel, dry_run=True)
    assert dry["sent"] == 0
    assert not db.has_content_been_sent("p", push.item_ids[0], "weekly")
    live = dispatcher.dispatch_personalized([push], db, channel)
    assert live["unknown"] == 1
    assert not db.has_content_been_sent("p", push.item_ids[0], "weekly")
    channel.send.assert_called_once()
