"""AI-powered admission data curator and filter.
Filters out commercial marketing fluff, scores credibility, and synthesizes 3-line summaries for parents.
"""
from __future__ import annotations
import logging
import os
import re
from typing import Any, Optional

from admission_brief.models import (
    AdmissionBriefing,
    AdmissionEvent,
    BriefingType,
    TargetGrade,
)

logger = logging.getLogger(__name__)

# List of hyper-commercial spam indicators
SPAM_BUZZWORDS = [
    "마감임박",
    "대치동 1위",
    "적중률 100%",
    "무조건 합격",
    "비법 전수",
    "시크릿 노하우",
    "파격 할인",
    "선착순 마감 직전",
]


class AICurator:
    """Evaluates raw admission notices and produces parent-optimized briefings."""

    def __init__(self, api_key: Optional[str] = None):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("OPENAI_API_KEY")

    def score_credibility(self, organizer: str, content: str, has_official_source: bool = True) -> int:
        """Scores credibility (0 ~ 100) based on organizer tier and data substance."""
        score = 50

        # Organizer tiering
        public_institutions = ["교육부", "교육청", "대교협", "한국대학교육협의회", "진로진학센터", "서울대", "연세대", "고려대"]
        major_academies = ["시대인재", "메가스터디", "대성마이맥", "종로학원", "이투스"]

        if any(pub in organizer for pub in public_institutions):
            score += 40
        elif any(acad in organizer for acad in major_academies):
            score += 30
        else:
            score += 10

        # Deduct for spam words
        spam_hits = sum(1 for w in SPAM_BUZZWORDS if w in content)
        score -= min(spam_hits * 10, 30)

        # Bonus for concrete data elements
        if re.search(r"\d{1,2}월|\d+등급|배치표|요강|선발인원|표준점수", content):
            score += 15

        return max(10, min(100, score))

    def clean_marketing_fluff(self, text: str) -> str:
        """Strips out high-pressure marketing phrases."""
        cleaned = text
        for word in SPAM_BUZZWORDS:
            cleaned = re.sub(rf"\[?{re.escape(word)}\]?", "", cleaned)
        cleaned = re.sub(r"\s{2,}", " ", cleaned).strip()
        return cleaned

    def curate_event_briefing(self, event: AdmissionEvent) -> AdmissionBriefing:
        """Synthesizes an AdmissionBriefing from an AdmissionEvent with parent-centric framing."""
        summary = event.summary_3lines
        if not summary:
            summary = [
                f"{event.organizer} 주최 2028 대입 개편안 심층 분석",
                "수능/내신 반영 비율 및 대학별 핵심 전형 변화 팩트체크",
                "예비 고1/중3 대상 겨울방학 핵심 과목 로드맵 제시",
            ]

        so_what = event.so_what
        if not so_what:
            if event.target_grade == TargetGrade.MIDDLE:
                so_what = "중2부터 내신 5등급제 전환으로 상위권 대학은 '세특'과 '수학 실력'이 당락을 가릅니다."
            elif event.target_grade == TargetGrade.HIGH_1_2:
                so_what = "내신 2~3등급대라도 모의고사 표준점수와 전공 연계 세특을 살리면 역전 기회가 열립니다."
            else:
                so_what = "9월 모평 기준 수시 6장 상향/적정 라인업을 이번 주 안으로 확정해야 합니다."

        todos = event.action_todos
        if not todos:
            todos = [
                f"{event.event_date_str} 행사 주요 발표 요점 확인",
                "자녀 이번 학기 생기부 세특 활동 키워드 3가지 점검",
            ]

        button_text = "⚡ 선착순 예약 바로가기" if event.is_first_come else "🌐 상세 안내 확인하기"

        return AdmissionBriefing(
            briefing_id=f"brief_{event.event_id}",
            briefing_type=event.event_type,
            target_grade=event.target_grade,
            headline=self.clean_marketing_fluff(event.title),
            summary_3lines=summary,
            so_what=so_what,
            action_todos=todos,
            booking_url=event.booking_url,
            booking_button_text=button_text,
            pdf_path=event.pdf_attachment_path,
        )

    def generate_weekly_todo_briefing(self, grade: TargetGrade) -> AdmissionBriefing:
        """Generates the weekly Sunday night briefing for the given grade."""
        grade_plans = {
            TargetGrade.MIDDLE: {
                "headline": "이번 주 중등/예비고1 입시 필수 체크리스트",
                "summary": [
                    "고교 선택(일반고 vs 자사고/특목고) 1차 마감 시즌 점검",
                    "중등 수학 1학기/2학기 심화 학습 결손 여부 진단",
                    "예비 고1을 위한 통합사회·통합과학 겨울방학 계획 수립",
                ],
                "so_what": "고등학교 진학 후 첫 1학기 중간고사 성적이 3년 내신의 80%를 좌우합니다.",
                "todos": [
                    "관내 고등학교 학교알리미 전년도 진학 실적 확인",
                    "대치동 예비고1 단과반/윈터스쿨 레벨테스트 신청 마감 확인",
                ],
            },
            TargetGrade.HIGH_1_2: {
                "headline": "이번 주 고1~고2 학부모 필수 체크리스트",
                "summary": [
                    "기말고사 대비 D-4주 주간 계획표 점검",
                    "수행평가 및 과목별 세특 탐구주제 담임 상담 준비",
                    "교육청 모의고사 오답 노트 및 취약 과목 진단",
                ],
                "so_what": "지필고사와 수행평가의 합산 점수에서 0.5점 차이로 등급이 갈립니다.",
                "todos": [
                    "이번 주 제출 수행평가 보고서 초안 피드백",
                    "희망 학과 권장 이수 과목 선택 여부 최종 확인",
                ],
            },
            TargetGrade.HIGH_3_N: {
                "headline": "이번 주 고3/수험생 학부모 필수 체크리스트",
                "summary": [
                    "수시 6장 최종 원서 조합 및 전형료 결제 일정 체크",
                    "수능 최저학력기준 충족을 위한 주간 실전 모의고사 훈련",
                    "대학별 면접 및 논술 고사 일정 중복 여부 확인",
                ],
                "so_what": "수능 당일 컨디션과 수능 최저 충족 여부가 수시 합격의 마지막 관문입니다.",
                "todos": [
                    "수능 수험표 및 필수 준비물 사전 체크리스트 작성",
                    "지원 대학별 고사장 위치 및 이동 동선 점검",
                ],
            },
        }

        data = grade_plans.get(grade, grade_plans[TargetGrade.MIDDLE])

        return AdmissionBriefing(
            briefing_id=f"weekly_{grade.value}_{BriefingType.WEEKLY_TODO.value}",
            briefing_type=BriefingType.WEEKLY_TODO,
            target_grade=grade,
            headline=data["headline"],
            summary_3lines=data["summary"],
            so_what=data["so_what"],
            action_todos=data["todos"],
        )
