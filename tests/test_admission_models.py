"""Unit tests for Admission Briefing domain models."""
from datetime import datetime
from admission_brief.models import (
    AdmissionBriefing,
    AdmissionEvent,
    BriefingType,
    ParentUserProfile,
    TargetGrade,
    TargetRegion,
)


def test_target_region_properties():
    assert TargetRegion.ONLINE.label == "🌐 온라인/유튜브 라이브만"
    assert TargetRegion.SEOUL.label == "📍 서울 (대치·목동·중계)"
    assert TargetRegion.ALL.label == "전국 (온·오프라인 전체)"


def test_briefing_region_matching():
    # 1. Offline Seoul event
    briefing_seoul = AdmissionBriefing(
        briefing_id="b_seoul",
        briefing_type=BriefingType.TICKETING_ALERT,
        target_grade=TargetGrade.MIDDLE,
        target_region=TargetRegion.SEOUL,
        is_online_live=False,
        headline="대치동 현장 설명회",
        summary_3lines=["A", "B", "C"],
    )
    # Seoul parent matches
    assert briefing_seoul.is_match_user(TargetGrade.MIDDLE, TargetRegion.SEOUL) is True
    # Online-only parent does NOT match
    assert briefing_seoul.is_match_user(TargetGrade.MIDDLE, TargetRegion.ONLINE) is False
    # Gyeonggi parent does NOT match
    assert briefing_seoul.is_match_user(TargetGrade.MIDDLE, TargetRegion.GYEONGGI) is False
    # ALL region parent matches
    assert briefing_seoul.is_match_user(TargetGrade.MIDDLE, TargetRegion.ALL) is True

    # 2. Event with YouTube Online Live broadcast
    briefing_youtube = AdmissionBriefing(
        briefing_id="b_yt",
        briefing_type=BriefingType.TICKETING_ALERT,
        target_grade=TargetGrade.MIDDLE,
        target_region=TargetRegion.SEOUL,
        is_online_live=True,
        headline="대치동 설명회 (유튜브 동시송출)",
        summary_3lines=["A", "B", "C"],
    )
    # Online-only parent MATCHES because is_online_live is True
    assert briefing_youtube.is_match_user(TargetGrade.MIDDLE, TargetRegion.ONLINE) is True
    # Local parent MATCHES because is_online_live is True
    assert briefing_youtube.is_match_user(TargetGrade.MIDDLE, TargetRegion.LOCAL) is True


def test_target_grade_properties():
    assert TargetGrade.MIDDLE.label == "중등 / 예비고1"
    assert "중등" in TargetGrade.MIDDLE.badge
    assert TargetGrade.HIGH_1_2.label == "고1 ~ 고2"
    assert TargetGrade.HIGH_3_N.label == "고3 / N수생"
    assert TargetGrade.ALL.label == "전 학년 공통"


def test_briefing_type_properties():
    assert BriefingType.WEEKLY_TODO.icon == "📋"
    assert BriefingType.TICKETING_ALERT.icon == "⚡"
    assert BriefingType.BRIEFING_RECAP.icon == "📢"
    assert BriefingType.POLICY_TRANSLATE.icon == "💡"


def test_briefing_user_matching():
    briefing_all = AdmissionBriefing(
        briefing_id="b1",
        briefing_type=BriefingType.POLICY_TRANSLATE,
        target_grade=TargetGrade.ALL,
        headline="교육부 긴급 개편안",
        summary_3lines=["A", "B", "C"],
    )
    assert briefing_all.is_match_user(TargetGrade.MIDDLE) is True
    assert briefing_all.is_match_user(TargetGrade.HIGH_3_N) is True

    briefing_middle = AdmissionBriefing(
        briefing_id="b2",
        briefing_type=BriefingType.TICKETING_ALERT,
        target_grade=TargetGrade.MIDDLE,
        headline="예비고1 설명회",
        summary_3lines=["A", "B", "C"],
    )
    assert briefing_middle.is_match_user(TargetGrade.MIDDLE) is True
    assert briefing_middle.is_match_user(TargetGrade.HIGH_1_2) is False


def test_parent_user_profile_serialization():
    profile = ParentUserProfile(
        chat_id="12345",
        target_grade=TargetGrade.MIDDLE,
        username="test_mom",
        first_name="엄마",
    )
    data = profile.to_dict()
    assert data["chat_id"] == "12345"
    assert data["target_grade"] == "MIDDLE"
    assert data["username"] == "test_mom"
    assert data["alert_enabled"] is True
