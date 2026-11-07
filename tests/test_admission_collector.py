"""Unit tests for AdmissionCollector auto-scraping and curation."""
from unittest.mock import MagicMock
from admission_brief.ai_curator import AICurator
from admission_brief.collector import AdmissionCollector
from admission_brief.models import BriefingType, EventCategory, TargetGrade, TargetRegion
from admission_brief.storage.db import AdmissionDatabase


def test_collector_runs_and_saves_events():
    db = AdmissionDatabase(db_path=":memory:")
    curator = AICurator()
    collector = AdmissionCollector(db=db, curator=curator)

    results = collector.run_collection()
    assert results["total_saved"] >= 3
    assert results["public_count"] >= 2
    assert results["academy_count"] >= 1

    # Check database persistence
    all_events = db.list_all_events()
    assert len(all_events) == results["total_saved"]

    # Verify online live detection
    online_events = [e for e in all_events if e.is_online_live]
    assert len(online_events) > 0


def test_collector_drops_low_credibility_spam():
    db = AdmissionDatabase(db_path=":memory:")
    curator = AICurator()
    collector = AdmissionCollector(db=db, curator=curator)

    spam_post = {
        "title": "[마감임박] 대치동 1위 적중률 100% 비법 전수 사설 특강",
        "organizer": "정체불명 과외방",
        "target_grade": TargetGrade.MIDDLE,
        "target_region": TargetRegion.LOCAL,
        "event_type": BriefingType.TICKETING_ALERT,
        "event_date": "2026-11-20 18:00",
        "content": "마감임박 비법전수 파격할인",
    }
    event = collector._process_raw_post(spam_post)
    assert event is None  # Dropped because score < 60


def test_collector_megastudy_post_title_accurate():
    db = AdmissionDatabase(db_path=":memory:")
    curator = AICurator()
    collector = AdmissionCollector(db=db, curator=curator)

    academy_events = collector.collect_major_academy_seminars()
    mega_event = next(e for e in academy_events if e.organizer == "메가스터디")
    assert "수능 가채점 분석 및 2027 정시 합격 전략" in mega_event.title
    assert "수시 최종 지원" not in mega_event.title


def test_collector_loads_categorized_events():
    db = AdmissionDatabase(db_path=":memory:")
    results = AdmissionCollector(db=db, curator=AICurator()).run_collection()
    assert results["total_saved"] >= 11

    by_cat = {}
    for e in db.list_all_events():
        by_cat.setdefault(e.category, []).append(e)
        assert e.credibility_score >= 70
        assert e.source_name and e.source_url and e.source_url.startswith("https://")
        assert e.urgency in {"NORMAL", "HIGH", "BREAKING"}
    assert len(by_cat[EventCategory.EXPO]) >= 3
    assert len(by_cat[EventCategory.BRIEFING]) >= 5
    assert len(by_cat[EventCategory.SEMINAR]) >= 3

    briefing_orgs = {e.organizer for e in by_cat[EventCategory.BRIEFING]}
    assert {"메가스터디", "종로학원", "시대인재", "대성마이맥", "이투스"} <= briefing_orgs


def test_collector_events_respect_2027_admission_calendar():
    """No post-CSAT (가채점 / 수능 직후) event may be dated before the 2026-11-19 수능."""
    collector = AdmissionCollector(db=AdmissionDatabase(db_path=":memory:"), curator=AICurator())
    events = collector.collect_public_education_notices() + collector.collect_major_academy_seminars()
    for e in events:
        if "가채점" in e.title or "수능 직후" in e.title:
            assert e.event_date_str[:10] >= "2026-11-19", e.title


def test_collector_credibility_gate_is_70():
    curator = MagicMock(spec=AICurator)
    curator.clean_marketing_fluff.side_effect = lambda t: t
    collector = AdmissionCollector(db=AdmissionDatabase(db_path=":memory:"), curator=curator)
    post = {
        "title": "경계값 테스트 설명회",
        "organizer": "테스트",
        "target_grade": TargetGrade.ALL,
        "event_type": BriefingType.BRIEFING_RECAP,
        "event_date": "2026-12-01 10:00",
    }
    curator.score_credibility.return_value = 69
    assert collector._process_raw_post(post) is None
    curator.score_credibility.return_value = 70
    assert collector._process_raw_post(post) is not None
