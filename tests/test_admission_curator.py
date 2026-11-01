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


def test_high3_monthly_dynamic_plans():
    curator = AICurator()

    # 1. November (11월): 수능 직전 카운트다운 & 논술/면접
    b_nov = curator.generate_weekly_todo_briefing(TargetGrade.HIGH_3_N, month=11)
    assert "11월 수능 카운트다운" in b_nov.headline
    all_nov_text = " ".join(b_nov.summary_3lines + b_nov.action_todos + [b_nov.so_what])
    assert "바이오리듬" in all_nov_text
    assert "수험표" in all_nov_text
    assert "논술/면접" in all_nov_text
    # 수시 원서 접수는 9월에 마감되었으므로 11월에 나오면 안 됨!
    assert "수시 6장 최종 원서 조합" not in all_nov_text

    # 2. December (12월): 실채점 성적표 및 정시 가나다군
    b_dec = curator.generate_weekly_todo_briefing(TargetGrade.HIGH_3_N, month=12)
    assert "12월 수능 실채점" in b_dec.headline
    all_dec_text = " ".join(b_dec.summary_3lines + b_dec.action_todos + [b_dec.so_what])
    assert "실채점" in all_dec_text
    assert "가/나/다군" in all_dec_text

    # 3. January (1월): 정시 원서접수 마감 & 추가합격 충원율
    b_jan = curator.generate_weekly_todo_briefing(TargetGrade.HIGH_3_N, month=1)
    assert "1월 정시 원서접수" in b_jan.headline
    all_jan_text = " ".join(b_jan.summary_3lines + b_jan.action_todos + [b_jan.so_what])
    assert "추가합격" in all_jan_text or "추합" in all_jan_text
    assert "충원율" in all_jan_text

    # 4. September (9월): 수시 6장 원서접수
    b_sep = curator.generate_weekly_todo_briefing(TargetGrade.HIGH_3_N, month=9)
    assert "9월 대입 수시 6장" in b_sep.headline
    all_sep_text = " ".join(b_sep.summary_3lines + b_sep.action_todos + [b_sep.so_what])
    assert "수시 6장" in all_sep_text
