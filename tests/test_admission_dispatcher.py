"""Unit tests for SafeTelegramDispatcher in Admission Briefing."""
import io
import urllib.error
from unittest.mock import MagicMock
from admission_brief.channels.telegram import TelegramChannel
from admission_brief.channels.telegram_limiter import SafeTelegramDispatcher, TelegramRateLimiter
from admission_brief.models import (
    AdmissionBriefing,
    BriefingType,
    ParentUserProfile,
    TargetGrade,
)
from admission_brief.storage.db import AdmissionDatabase


def test_dispatcher_success_and_duplicate_skip():
    db = AdmissionDatabase(db_path=":memory:")
    db.upsert_subscriber(ParentUserProfile(chat_id="user_1", target_grade=TargetGrade.MIDDLE))
    db.upsert_subscriber(ParentUserProfile(chat_id="user_2", target_grade=TargetGrade.MIDDLE))

    mock_channel = MagicMock(spec=TelegramChannel)
    mock_channel.send.return_value = "msg_123"

    dispatcher = SafeTelegramDispatcher(rate_limiter=TelegramRateLimiter(max_global_rate=1000.0, min_chat_interval=0.0))
    briefing = AdmissionBriefing(
        briefing_id="brief_dispatch_01",
        briefing_type=BriefingType.TICKETING_ALERT,
        target_grade=TargetGrade.MIDDLE,
        headline="시대인재 윈터스쿨 오픈",
        summary_3lines=["요약1", "요약2", "요약3"],
    )

    # First dispatch
    stats1 = dispatcher.dispatch(channel=mock_channel, briefing=briefing, db=db)
    assert stats1["total"] == 2
    assert stats1["success"] == 2
    assert stats1["skipped"] == 0
    assert mock_channel.send.call_count == 2

    # Second dispatch with identical briefing_id should be skipped
    stats2 = dispatcher.dispatch(channel=mock_channel, briefing=briefing, db=db)
    assert stats2["total"] == 2
    assert stats2["success"] == 0
    assert stats2["skipped"] == 2
    assert mock_channel.send.call_count == 2


def test_dispatcher_handles_403_blocked_user():
    db = AdmissionDatabase(db_path=":memory:")
    db.upsert_subscriber(ParentUserProfile(chat_id="blocked_user", target_grade=TargetGrade.MIDDLE))

    mock_channel = MagicMock(spec=TelegramChannel)
    # Simulate HTTP 403 Forbidden (user blocked bot)
    err_fp = io.BytesIO(b'{"ok":false,"description":"Forbidden: bot was blocked by the user"}')
    mock_channel.send.side_effect = urllib.error.HTTPError("url", 403, "Forbidden", {}, err_fp)

    dispatcher = SafeTelegramDispatcher(rate_limiter=TelegramRateLimiter(max_global_rate=1000.0, min_chat_interval=0.0))
    briefing = AdmissionBriefing(
        briefing_id="brief_403",
        briefing_type=BriefingType.WEEKLY_TODO,
        target_grade=TargetGrade.MIDDLE,
        headline="체크리스트",
        summary_3lines=["A", "B", "C"],
    )

    stats = dispatcher.dispatch(channel=mock_channel, briefing=briefing, db=db)
    assert stats["total"] == 1
    assert stats["success"] == 0
    assert stats["deactivated"] == 1
    assert db.has_dispatched("brief_403", "blocked_user") is False


def test_weekday_onboarding_blocks_same_week_sunday_batch_only():
    from datetime import datetime
    from admission_brief.ai_curator import AICurator

    db = AdmissionDatabase(db_path=":memory:")
    db.upsert_subscriber(ParentUserProfile(chat_id="new_user", target_grade=TargetGrade.HIGH_3_N))
    curator = AICurator()

    # Onboarded on Wednesday 2026-11-04 (ISO 2026-W45)
    welcome = curator.generate_weekly_todo_briefing(TargetGrade.HIGH_3_N, dt=datetime(2026, 11, 4, 14, 0))
    assert welcome.briefing_id.endswith("_2026W45")
    db.log_dispatch(welcome.briefing_id, "new_user", "SUCCESS")

    # Sunday 2026-11-08 batch is still W45 -> duplicate blocked
    sunday = curator.generate_weekly_todo_briefing(TargetGrade.HIGH_3_N, dt=datetime(2026, 11, 8, 21, 0))
    assert sunday.briefing_id == welcome.briefing_id
    assert db.has_dispatched(sunday.briefing_id, "new_user") is True

    mock_channel = MagicMock(spec=TelegramChannel)
    mock_channel.send.return_value = "msg_1"
    dispatcher = SafeTelegramDispatcher(rate_limiter=TelegramRateLimiter(max_global_rate=1000.0, min_chat_interval=0.0))
    stats = dispatcher.dispatch(channel=mock_channel, briefing=sunday, db=db)
    assert stats["skipped"] == 1
    mock_channel.send.assert_not_called()

    # Next Sunday 2026-11-15 is W46 -> sent normally
    next_sunday = curator.generate_weekly_todo_briefing(TargetGrade.HIGH_3_N, dt=datetime(2026, 11, 15, 21, 0))
    assert next_sunday.briefing_id.endswith("_2026W46")
    assert db.has_dispatched(next_sunday.briefing_id, "new_user") is False
    stats = dispatcher.dispatch(channel=mock_channel, briefing=next_sunday, db=db)
    assert stats["success"] == 1
