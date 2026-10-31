"""Unit tests for AdmissionDatabase with isolated in-memory SQLite."""
from datetime import datetime
from admission_brief.models import (
    AdmissionEvent,
    BriefingType,
    ParentUserProfile,
    TargetGrade,
    TargetRegion,
)
from admission_brief.storage.db import AdmissionDatabase


def test_subscriber_upsert_and_grade_update():
    db = AdmissionDatabase(db_path=":memory:")

    # 1. Initial upsert
    user = ParentUserProfile(
        chat_id="999",
        target_grade=TargetGrade.ALL,
        username="smart_mom",
        first_name="민지맘",
    )
    db.upsert_subscriber(user)

    fetched = db.get_subscriber("999")
    assert fetched is not None
    assert fetched.username == "smart_mom"
    assert fetched.target_grade == TargetGrade.ALL

    # 2. Update grade
    success = db.update_subscriber_grade("999", TargetGrade.HIGH_1_2)
    assert success is True

    updated = db.get_subscriber("999")
    assert updated.target_grade == TargetGrade.HIGH_1_2


def test_list_subscribers_by_grade():
    db = AdmissionDatabase(db_path=":memory:")

    # Add 3 subscribers
    db.upsert_subscriber(ParentUserProfile(chat_id="1", target_grade=TargetGrade.MIDDLE))
    db.upsert_subscriber(ParentUserProfile(chat_id="2", target_grade=TargetGrade.HIGH_1_2))
    db.upsert_subscriber(ParentUserProfile(chat_id="3", target_grade=TargetGrade.ALL))

    # Query MIDDLE: should return '1' (MIDDLE) and '3' (ALL)
    middle_subs = db.list_subscribers(grade=TargetGrade.MIDDLE)
    cids = {s.chat_id for s in middle_subs}
    assert "1" in cids
    assert "3" in cids
    assert "2" not in cids


def test_list_subscribers_by_region_and_online():
    db = AdmissionDatabase(db_path=":memory:")

    # User A: Seoul only
    db.upsert_subscriber(ParentUserProfile(chat_id="u_seoul", target_region=TargetRegion.SEOUL))
    # User B: Online only
    db.upsert_subscriber(ParentUserProfile(chat_id="u_online", target_region=TargetRegion.ONLINE))
    # User C: ALL regions
    db.upsert_subscriber(ParentUserProfile(chat_id="u_all", target_region=TargetRegion.ALL))

    # Case 1: Offline Seoul event (is_online_live=False)
    subs1 = db.list_subscribers(region=TargetRegion.SEOUL, is_online_live=False)
    cids1 = {s.chat_id for s in subs1}
    assert "u_seoul" in cids1
    assert "u_all" in cids1
    assert "u_online" not in cids1  # Online-only excluded

    # Case 2: Seoul event with Online YouTube live stream
    subs2 = db.list_subscribers(region=TargetRegion.SEOUL, is_online_live=True)
    cids2 = {s.chat_id for s in subs2}
    assert "u_seoul" in cids2
    assert "u_all" in cids2
    assert "u_online" in cids2  # Online-only INCLUDED


def test_event_save_and_ticketing_query():
    db = AdmissionDatabase(db_path=":memory:")

    event = AdmissionEvent(
        event_id="evt_101",
        title="시대인재 예비고1 설명회",
        organizer="시대인재",
        target_grade=TargetGrade.MIDDLE,
        event_type=BriefingType.TICKETING_ALERT,
        event_date_str="2026-11-20 19:00",
        is_first_come=True,
        booking_url="https://sdij.com/test",
        summary_3lines=["A", "B", "C"],
        so_what="중요합니다",
    )
    db.save_event(event)

    fetched = db.get_event("evt_101")
    assert fetched is not None
    assert fetched.title == "시대인재 예비고1 설명회"
    assert fetched.is_first_come is True
    assert fetched.summary_3lines == ["A", "B", "C"]

    # Upcoming ticketing
    ticketing_list = db.list_upcoming_ticketing()
    assert len(ticketing_list) == 1
    assert ticketing_list[0].event_id == "evt_101"


def test_dispatch_log_duplicate_prevention():
    db = AdmissionDatabase(db_path=":memory:")

    assert db.has_dispatched("brief_1", "chat_user_1") is False

    db.log_dispatch("brief_1", "chat_user_1", "SUCCESS")
    assert db.has_dispatched("brief_1", "chat_user_1") is True

    # Another user
    assert db.has_dispatched("brief_1", "chat_user_2") is False
