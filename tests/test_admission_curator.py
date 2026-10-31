"""Unit tests for AICurator logic."""
from admission_brief.ai_curator import AICurator
from admission_brief.models import (
    AdmissionEvent,
    BriefingType,
    TargetGrade,
)


def test_credibility_scoring():
    curator = AICurator()

    # Public institution high credibility
    score_pub = curator.score_credibility("서울시교육청", "2028 대입 전형 요강 발표", has_official_source=True)
    assert score_pub >= 90

    # Academy with marketing buzzwords should have deductions
    spam_text = "[마감임박] 대치동 1위 적중률 100% 비법 전수 설명회!"
    score_spam = curator.score_credibility("일반학원", spam_text)
    assert score_spam < score_pub


def test_clean_marketing_fluff():
    curator = AICurator()
    raw = "[마감임박] 대치동 1위 예비고1 윈터스쿨 설명회"
    cleaned = curator.clean_marketing_fluff(raw)
    assert "마감임박" not in cleaned
    assert "대치동 1위" not in cleaned
    assert "예비고1 윈터스쿨 설명회" in cleaned


def test_weekly_todo_generation():
    curator = AICurator()

    briefing_mid = curator.generate_weekly_todo_briefing(TargetGrade.MIDDLE)
    assert briefing_mid.target_grade == TargetGrade.MIDDLE
    assert briefing_mid.briefing_type == BriefingType.WEEKLY_TODO
    assert len(briefing_mid.summary_3lines) == 3
    assert len(briefing_mid.action_todos) > 0
    assert "중등" in briefing_mid.headline or "예비고1" in briefing_mid.headline

    briefing_high3 = curator.generate_weekly_todo_briefing(TargetGrade.HIGH_3_N)
    assert briefing_high3.target_grade == TargetGrade.HIGH_3_N
    assert "고3" in briefing_high3.headline or "수험생" in briefing_high3.headline
