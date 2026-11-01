"""AI-powered admission data curator and filter.
Filters out commercial marketing fluff, scores credibility, and synthesizes 3-line summaries for parents.
"""
from __future__ import annotations
import logging
import os
import re
from datetime import datetime
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

    def generate_weekly_todo_briefing(self, grade: TargetGrade, month: Optional[int] = None) -> AdmissionBriefing:
        """Generates the weekly Sunday night briefing for the given grade, dynamically adapting to academic calendar."""
        current_month = month if month is not None else datetime.now().month

        if grade == TargetGrade.HIGH_3_N:
            if current_month == 11:
                data = {
                    "headline": "11월 수능 카운트다운! 고3/수험생 학부모 필수 체크리스트",
                    "summary": [
                        "2027 수능 실전 바이오리듬(08:40 기상 및 집중 훈련) 체화",
                        "수능 당일 수험표 교부 및 고사장 반입 금지 물품(스마트워치 등) 체크",
                        "수능 직후 주말(11월 중순~하순) 대학별 수시 논술/면접 고사 이동 동선 점검",
                    ],
                    "so_what": "수능 당일 멘탈 관리와 수능 직후 쉴 틈 없이 이어지는 대학별 논술/면접 고사장 이동 전략이 수시 최종 합격의 열쇠입니다.",
                    "todos": [
                        "수능 고사장 반입 금지 물품(전자기기 등) 사전 배제 및 신분증/수험표 챙기기",
                        "수능 직후 주말 대학별 논술/면접 일정 및 이동 경로 사전 시뮬레이션",
                    ],
                }
            elif current_month == 12:
                data = {
                    "headline": "12월 수능 실채점 및 정시 지원 전략 체크리스트",
                    "summary": [
                        "수능 성적표 통지 및 실채점 기준 표준점수/백분위 심층 분석",
                        "정시 가/나/다군 안정·적정·상향 포트폴리오 조합 수립",
                        "대학별 환산점수 및 탐구 변환표준점수 반영 유불리 진단",
                    ],
                    "so_what": "원점수가 아닌 대학별 수능 환산점수와 과목별 반영 비율에서 실질 합불이 결정됩니다.",
                    "todos": [
                        "대형 입시기관 정시 모의지원 서비스 표본 변동 추이 모니터링",
                        "정시 가/나/다군 3장 지원 대학 및 학과 1차 후보군 확정",
                    ],
                }
            elif current_month == 1:
                data = {
                    "headline": "1월 정시 원서접수 마감 및 추가합격(추합) 충원율 분석",
                    "summary": [
                        "정시 가/나/다군 원서접수 마감 경쟁률 및 마감 직전 경쟁률 체크",
                        "전년도 대학별 추가합격(추합) 충원율 및 예비번호 회전율 분석",
                        "정시 최초합격자 발표 및 등록금 납부 일정 체크",
                    ],
                    "so_what": "원서접수 마지막 날 눈치작전 경쟁률과 추합 회전율을 끝까지 추적해야 합니다.",
                    "todos": [
                        "원서접수 마감 3시간 전 경쟁률 모니터링 및 최종 원서 접수",
                        "추가합격 전화 찬스 대비 비상 연락처 확인",
                    ],
                }
            elif current_month == 9:
                data = {
                    "headline": "9월 대입 수시 6장 원서접수 최종 체크리스트",
                    "summary": [
                        "수시 6장 최종 지원 대학 및 학과 포트폴리오 확정",
                        "대학별 원서접수 마감 시각 및 전형료 결제 최종 확인",
                        "9월 모의평가 가채점 기준 수능 최저 충족 가능성 재점검",
                    ],
                    "so_what": "수시 원서 접수는 1번 완료되면 취소가 불가능하므로 상향/적정 비율을 신중히 안배해야 합니다.",
                    "todos": [
                        "유웨이어플라이/진학어플라이 공통원서 사전 작성 및 저장",
                        "원서접수 마감일 경쟁률 추이 확인 및 전형료 결제 완료",
                    ],
                }
            else:
                data = {
                    "headline": f"{current_month}월 고3/수험생 학부모 필수 체크리스트",
                    "summary": [
                        "수능 모의평가 취약 과목 보완 및 주간 학습 완성도 점검",
                        "학교생활기록부 교과/세특 기재사항 최종 확인 및 보완",
                        "수능 최저학력기준 충족을 위한 주간 실전 모의고사 훈련",
                    ],
                    "so_what": "모의평가 성적과 학생부 성적의 균형을 바탕으로 수시/정시 비중을 판단해야 합니다.",
                    "todos": [
                        "취약 과목 일일 오답 노트 분석 및 집중 보완",
                        "목표 대학 수능 최저 기준 재확인",
                    ],
                }
        elif grade == TargetGrade.HIGH_1_2:
            data = {
                "headline": f"{current_month}월 고1~고2 학부모 필수 체크리스트",
                "summary": [
                    "2학기 기말고사 대비 D-4주 주간 계획표 및 지필평가 총력",
                    "수행평가 및 과목별 세특 탐구보고서 담임/교과 교사 피드백 준비",
                    "전국연합학력평가(모의고사) 오답 분석 및 취약 단원 보완",
                ],
                "so_what": "지필고사와 수행평가의 합산 점수에서 0.5점 차이로 1등급과 2등급이 갈립니다.",
                "todos": [
                    "이번 주 제출 수행평가 보고서 초안 검토 및 피드백",
                    "희망 학과 권장 이수 과목 선택 여부 최종 확인",
                ],
            }
        else:  # TargetGrade.MIDDLE or ALL
            data = {
                "headline": f"{current_month}월 중등/예비고1 입시 필수 체크리스트",
                "summary": [
                    "전기고(특목·자사고) 지원 여부 최종 결정 및 자기주도학습전형 대비",
                    "중등 수학 1학기/2학기 심화 학습 결손 여부 진단",
                    "예비 고1을 위한 통합사회·통합과학 겨울방학 선행 계획 수립",
                ],
                "so_what": "고등학교 진학 후 첫 1학기 중간고사 성적이 3년 내신의 80%를 좌우합니다.",
                "todos": [
                    "관내 고등학교 학교알리미 전년도 진학 실적 및 편제표 확인",
                    "대치동 예비고1 단과반/윈터스쿨 레벨테스트 신청 마감 확인",
                ],
            }

        return AdmissionBriefing(
            briefing_id=f"weekly_{grade.value}_{BriefingType.WEEKLY_TODO.value}_{current_month}m",
            briefing_type=BriefingType.WEEKLY_TODO,
            target_grade=grade,
            headline=data["headline"],
            summary_3lines=data["summary"],
            so_what=data["so_what"],
            action_todos=data["todos"],
        )
