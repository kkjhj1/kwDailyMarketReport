"""Unit tests for AdmissionCollector auto-scraping and curation."""
from unittest.mock import MagicMock
from admission_brief.ai_curator import AICurator
from admission_brief.collector import AdmissionCollector
from admission_brief.models import BriefingType, TargetGrade, TargetRegion
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
