"""Unit tests for AdmissionDatabase with isolated in-memory SQLite."""
import sqlite3
from datetime import datetime
from admission_brief.models import (
    AdmissionEvent,
    BriefingType,
    EventCategory,
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


# --- Event category listing (EXPO / BRIEFING / SEMINAR) ---

_NOW = datetime(2026, 11, 7, 12, 0)


def _ev(event_id, category, date_str, grade=TargetGrade.HIGH_3_N, region=TargetRegion.SEOUL,
        online=False, expires_at=None, event_type=BriefingType.BRIEFING_RECAP):
    return AdmissionEvent(
        event_id=event_id,
        title=f"행사 {event_id}",
        organizer="종로학원",
        target_grade=grade,
        target_region=region,
        is_online_live=online,
        event_type=event_type,
        event_date_str=date_str,
        category=category,
        expires_at=expires_at,
    )


def test_event_category_and_expires_roundtrip():
    db = AdmissionDatabase(db_path=":memory:")
    exp = datetime(2027, 1, 7, 18, 0)
    db.save_event(_ev("e1", EventCategory.EXPO, "2026년 12월 중 (공고 예정)", expires_at=exp))
    fetched = db.get_event("e1")
    assert fetched.category == EventCategory.EXPO
    assert fetched.expires_at == exp

    # Upsert must refresh date/category (stale rows from old collector titles get corrected)
    db.save_event(_ev("e1", EventCategory.SEMINAR, "2026-12-01 10:00"))
    fetched = db.get_event("e1")
    assert fetched.category == EventCategory.SEMINAR
    assert fetched.event_date_str == "2026-12-01 10:00"
    assert fetched.expires_at is None


def test_get_events_by_category_filters_category_and_past_events():
    db = AdmissionDatabase(db_path=":memory:")
    db.save_event(_ev("past", EventCategory.EXPO, "2026-11-06 10:00"))
    db.save_event(_ev("today", EventCategory.EXPO, "2026-11-07 09:00"))  # same day stays visible
    db.save_event(_ev("future", EventCategory.EXPO, "2026-12-13 10:00"))
    db.save_event(_ev("tba", EventCategory.EXPO, "2026년 12월 중", expires_at=datetime(2027, 1, 7)))
    db.save_event(_ev("tba_expired", EventCategory.EXPO, "2099-01-01 10:00", expires_at=datetime(2026, 11, 1)))
    db.save_event(_ev("other_cat", EventCategory.SEMINAR, "2026-12-13 10:00"))
    db.save_event(_ev("notice", EventCategory.EXPO, "2026-12-13 10:00", event_type=BriefingType.POLICY_TRANSLATE))

    events, total = db.get_events_by_category(EventCategory.EXPO, now=_NOW)
    assert total == 3
    # Chronological, undated (TBA) entries after dated ones
    assert [e.event_id for e in events] == ["today", "future", "tba"]


def test_get_events_by_category_grade_and_region_matching():
    db = AdmissionDatabase(db_path=":memory:")
    db.save_event(_ev("seoul_off", EventCategory.BRIEFING, "2026-11-22 14:00", region=TargetRegion.SEOUL))
    db.save_event(_ev("seoul_live", EventCategory.BRIEFING, "2026-11-22 15:00", region=TargetRegion.SEOUL, online=True))
    db.save_event(_ev("middle", EventCategory.BRIEFING, "2026-11-22 16:00", grade=TargetGrade.MIDDLE))
    db.save_event(_ev("all_grade", EventCategory.BRIEFING, "2026-11-22 17:00", grade=TargetGrade.ALL))

    events, _ = db.get_events_by_category(EventCategory.BRIEFING, TargetGrade.HIGH_3_N, TargetRegion.ONLINE, now=_NOW)
    assert [e.event_id for e in events] == ["seoul_live"]

    events, total = db.get_events_by_category(EventCategory.BRIEFING, TargetGrade.HIGH_3_N, TargetRegion.GYEONGGI, now=_NOW)
    assert [e.event_id for e in events] == ["seoul_live"]
    assert total == 1

    events, total = db.get_events_by_category(EventCategory.BRIEFING, TargetGrade.HIGH_3_N, TargetRegion.SEOUL, now=_NOW)
    assert {e.event_id for e in events} == {"seoul_off", "seoul_live", "all_grade"}
    assert total == 3


def test_get_events_by_category_paging():
    db = AdmissionDatabase(db_path=":memory:")
    for i in range(7):
        db.save_event(_ev(f"b{i}", EventCategory.BRIEFING, f"2026-11-{20 + i} 10:00"))

    p0, total = db.get_events_by_category(EventCategory.BRIEFING, page=0, page_size=3, now=_NOW)
    p1, _ = db.get_events_by_category(EventCategory.BRIEFING, page=1, page_size=3, now=_NOW)
    p2, _ = db.get_events_by_category(EventCategory.BRIEFING, page=2, page_size=3, now=_NOW)
    p3, _ = db.get_events_by_category(EventCategory.BRIEFING, page=3, page_size=3, now=_NOW)
    assert total == 7
    assert [e.event_id for e in p0] == ["b0", "b1", "b2"]
    assert [e.event_id for e in p1] == ["b3", "b4", "b5"]
    assert [e.event_id for e in p2] == ["b6"]
    assert p3 == []


def test_additive_migration_on_legacy_events_table(tmp_path):
    legacy = tmp_path / "legacy_admission.db"
    conn = sqlite3.connect(str(legacy))
    conn.execute(
        """
        CREATE TABLE admission_events (
            event_id TEXT PRIMARY KEY, title TEXT NOT NULL, organizer TEXT NOT NULL,
            target_grade TEXT NOT NULL, event_type TEXT NOT NULL, event_date_str TEXT NOT NULL,
            booking_opens_at TEXT, is_first_come INTEGER NOT NULL DEFAULT 0, booking_url TEXT,
            summary_3lines_json TEXT, so_what TEXT, action_todos_json TEXT, pdf_attachment_path TEXT,
            credibility_score INTEGER NOT NULL DEFAULT 80, status TEXT NOT NULL DEFAULT 'SCHEDULED',
            created_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "INSERT INTO admission_events (event_id, title, organizer, target_grade, event_type, event_date_str, created_at) "
        "VALUES ('old1', '기존 행사', '종로학원', 'HIGH_3_N', 'BRIEFING_RECAP', '2026-12-01 10:00', ?)",
        (datetime(2026, 11, 1).isoformat(),),
    )
    conn.commit()
    conn.close()

    db = AdmissionDatabase(db_path=str(legacy))
    old = db.get_event("old1")
    assert old.category == EventCategory.BRIEFING
    assert old.expires_at is None
    events, total = db.get_events_by_category(EventCategory.BRIEFING, now=_NOW)
    assert total == 1 and events[0].event_id == "old1"
