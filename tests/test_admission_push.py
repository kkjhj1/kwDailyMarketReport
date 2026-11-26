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


def test_record_content_sent_upserts_sent_at_and_weekly_cooldown_is_28_days():
    db = AdmissionDatabase(":memory:")
    first = datetime(2026, 9, 1, 20, 0, tzinfo=KST)
    db.record_content_sent("p", "todo-high12-year", "CARD", "weekly", sent_at=first)
    # Strict (default) check never expires — used for ACK ownership.
    assert db.has_content_been_sent("p", "todo-high12-year", "weekly")
    assert db.has_content_been_sent("p", "todo-high12-year", "weekly", cooldown_days=28,
                                    now=datetime(2026, 9, 20, tzinfo=KST))
    assert not db.has_content_been_sent("p", "todo-high12-year", "weekly", cooldown_days=28,
                                        now=datetime(2026, 9, 30, tzinfo=KST))
    # A re-send refreshes sent_at, so the cooldown restarts instead of firing every week.
    resend = datetime(2026, 9, 30, 20, 0, tzinfo=KST)
    db.record_content_sent("p", "todo-high12-year", "CARD", "weekly", sent_at=resend)
    assert db.get_content_sent_at("p", "todo-high12-year", "weekly") == resend
    assert db.has_content_been_sent("p", "todo-high12-year", "weekly", cooldown_days=28,
                                    now=datetime(2026, 10, 7, 20, 0, tzinfo=KST))


def test_weekly_push_resurfaces_card_only_after_cooldown():
    db = AdmissionDatabase(":memory:")
    db.upsert_subscriber(_exact_high_profile("p"))
    pushes = build_weekly_personalized_pushes(db, KnowledgeCatalog(), NOW)
    ids = pushes[0].item_ids
    for item_id in ids:
        db.record_content_sent("p", item_id, "CARD", "weekly", sent_at=NOW)
    one_week = datetime(2026, 11, 29, 20, 0, tzinfo=KST)
    assert build_weekly_personalized_pushes(db, KnowledgeCatalog(), one_week) == []
    after_cooldown = datetime(2026, 12, 27, 20, 0, tzinfo=KST)
    again = build_weekly_personalized_pushes(db, KnowledgeCatalog(), after_cooldown)
    assert again and set(again[0].item_ids) & set(ids)


def test_card_ack_is_version_aware_and_alias_kept():
    db = AdmissionDatabase(":memory:")
    assert db.record_card_ack("p", "guide-high-h123") is True
    assert db.record_card_ack("p", "guide-high-h123") is False  # idempotent
    assert db.is_card_acked("p", "guide-high-h123")
    assert db.is_card_acked("p", "guide-high-h123", current_version=1)
    # Content bumped to v2 -> the old ACK no longer suppresses it.
    assert not db.is_card_acked("p", "guide-high-h123", current_version=2)
    assert db.ack_card("p", "guide-high-h123", version=2) is True
    assert db.is_card_acked("p", "guide-high-h123", current_version=2)
    assert db.record_card_ack("p", "guide-high-h123", version=1) is False  # no downgrade
    assert db.is_card_acked("p", "guide-high-h123", current_version=2)


def test_card_ack_migration_adds_acked_version_to_legacy_table(tmp_path):
    import sqlite3

    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE admission_card_acks (chat_id TEXT NOT NULL, content_id TEXT NOT NULL, "
                 "acked_at TEXT NOT NULL, PRIMARY KEY(chat_id, content_id))")
    conn.execute("INSERT INTO admission_card_acks VALUES ('p', 'old-card', '2026-01-01T00:00:00+09:00')")
    conn.commit()
    conn.close()
    db = AdmissionDatabase(str(path))
    assert db.is_card_acked("p", "old-card")
    assert not db.is_card_acked("p", "old-card", current_version=2)


def test_ack_callback_accepts_dday_offset_history_and_uses_parent_label():
    db = AdmissionDatabase(":memory:")
    db.upsert_subscriber(_exact_high_profile("10"))
    db.record_content_sent("10", "event-dday", "EVENT", "dday:D3")
    channel = MagicMock(spec=TelegramChannel)
    channel.edit_message_text.return_value = True
    bot = AdmissionInteractiveBot(channel, db)
    bot.handle_update({"callback_query": _callback("adm:ack:event-dday")})
    assert db.is_card_acked("10", "event-dday")

    push = build_weekly_personalized_pushes(db, KnowledgeCatalog(), NOW)[0]
    labels = [b["text"] for row in push.reply_markup["inline_keyboard"] for b in row
              if b["callback_data"].startswith("adm:ack:")]
    assert labels and all(label.startswith("✅ 확인 완료") for label in labels)


def _dday_db(*events):
    db = AdmissionDatabase(":memory:")
    db.upsert_subscriber(ParentUserProfile(chat_id="p", target_grade=TargetGrade.HIGH_1_2))
    for event_id, date_str in events:
        db.save_event(AdmissionEvent(
            event_id=event_id, title=f"행사 {event_id}", organizer="주최", target_grade=TargetGrade.HIGH_1_2,
            event_type=BriefingType.BRIEFING_RECAP, event_date_str=date_str,
        ))
    return db


def test_dday_same_event_alerts_at_d3_and_again_at_d1_once_each():
    db = _dday_db(("ev-a", "2026-11-25 10:00"))
    channel = MagicMock(spec=TelegramChannel)
    channel.send.return_value = "message"
    dispatcher = SafeTelegramDispatcher(TelegramRateLimiter(1000, 0))

    d3 = build_event_dday_pushes(db, NOW)  # 11-22 -> D-3
    assert len(d3) == 1 and d3[0].stream == "dday:D3"
    assert d3[0].briefing.briefing_id == "push:dday:20261122:p"
    assert dispatcher.dispatch_personalized(d3, db, channel)["sent"] == 1
    assert build_event_dday_pushes(db, NOW) == []  # never twice on the same day/offset

    d1_now = datetime(2026, 11, 24, 20, 0, tzinfo=KST)
    d1 = build_event_dday_pushes(db, d1_now)
    assert len(d1) == 1 and d1[0].stream == "dday:D1" and d1[0].item_ids == ("ev-a",)
    assert dispatcher.dispatch_personalized(d1, db, channel)["sent"] == 1
    assert build_event_dday_pushes(db, d1_now) == []
    assert channel.send.call_count == 2


def test_dday_mixed_offsets_consolidate_into_one_daily_message():
    db = _dday_db(("ev-d3", "2026-11-25 10:00"), ("ev-d1", "2026-11-23 10:00"))
    pushes = build_event_dday_pushes(db, NOW)
    assert len(pushes) == 1
    assert pushes[0].item_ids == ("ev-d1", "ev-d3")  # most urgent first
    assert pushes[0].stream == "dday"
    # Simulate the confirmed send (history stamped at send time, like the dispatcher does).
    for item_id in pushes[0].item_ids:
        db.record_content_sent("p", item_id, "EVENT", pushes[0].stream, sent_at=NOW)
    # Same day: the shared tag blocks a repeat even without the daily dispatch claim.
    assert build_event_dday_pushes(db, NOW) == []
    # The shared tag blocks only today; ev-d3 still alerts again at its D-1.
    later = build_event_dday_pushes(db, datetime(2026, 11, 24, 20, 0, tzinfo=KST))
    assert len(later) == 1 and later[0].item_ids == ("ev-d3",) and later[0].stream == "dday:D1"


def test_every_stage_has_weekly_todo_and_study_guide_cards():
    from datetime import date

    catalog = KnowledgeCatalog()
    probes = {
        EducationStage.ELEMENTARY: (GradeLevel.E1, GradeLevel.E4, GradeLevel.E6),
        EducationStage.MIDDLE: (GradeLevel.M1, GradeLevel.M3, GradeLevel.LEGACY),
        EducationStage.HIGH: (GradeLevel.H1, GradeLevel.H3, GradeLevel.N),
        EducationStage.POST_HIGH: (GradeLevel.N,),
    }
    for stage, levels in probes.items():
        for level in levels:
            for card_type in ("WEEKLY_TODO", "STUDY_GUIDE"):
                found = catalog.filter(stage, level, month=11, today=date(2026, 11, 26), card_type=card_type)
                assert found, (stage, level, card_type)
    assert EducationStage.POST_HIGH in catalog.get("todo-high3-year").target_stages
    for cid in ("todo-elementary-year", "guide-middle-m123", "guide-high-h123", "guide-post-high-n"):
        card = catalog.get(cid)
        assert card.reviewed_at == date(2026, 11, 26)
        assert card.valid_until >= date(2027, 12, 31)


def test_catalog_version_optional_and_unknown_keys_rejected():
    import pytest
    from admission_brief.catalog import CatalogValidationError, KnowledgeCard

    raw = KnowledgeCatalog().get("todo-middle-year").to_dict()
    legacy = {k: v for k, v in raw.items() if k != "version"}
    assert KnowledgeCard.from_dict(legacy).version == 1
    assert KnowledgeCard.from_dict({**legacy, "version": 3}).version == 3
    with pytest.raises(CatalogValidationError):
        KnowledgeCard.from_dict({**legacy, "bogus": 1})
    with pytest.raises(CatalogValidationError):
        KnowledgeCard.from_dict({**legacy, "version": 0})


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
