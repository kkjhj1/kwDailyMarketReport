"""AI Prompt generator and deep linking utilities for ChatGPT, Gemini, and Claude.
Allows users to click once and immediately get an AI-tailored 3N4D itinerary based on the deal.
"""
from __future__ import annotations
import urllib.parse
from typing import Optional

from travel_brief.models import DestinationMeta, FlightDeal, BudgetBreakdown


class AIPromptGenerator:
    """Generates structured prompts and URL deep links for external AI assistants."""

    @staticmethod
    def build_prompt(
        dest: DestinationMeta,
        deal: FlightDeal,
        budget: BudgetBreakdown,
        travel_style: str = "가성비 & 로컬 힐링/맛집 탐방",
    ) -> str:
        prompt = f"""[여행 계획 요청: {dest.name} 3박 4일 최적 일정]

1. 여행 기본 정보:
- 목적지: {dest.country} {dest.name} ({dest.code})
- 비행 시간: 약 {dest.flight_time_hours}시간
- 여행 일정: {deal.departure_date}({deal.departure_day_of_week}) ~ {deal.return_date}({deal.return_day_of_week}) (3박 4일 주말 일정)
- 항공 정보: {deal.origin_code} ↔ {deal.destination_code} ({deal.airline}, 왕복 약 {deal.price_krw:,}원)
- 예상 예산: 1인 총 {budget.total_per_person_krw:,}원 (항공 {budget.flight_krw:,}원 / 숙소 {budget.hotel_total_krw:,}원 / 식비·교통 {budget.meal_transit_total_krw:,}원)
- 2인 기준 예상: 약 {budget.estimated_for_two_krw:,}원 (숙소 1실 쉐어)
- 비자/입국 정보: {dest.visa_info}
- 여행 팁/주의사항: {dest.tips} (주의: {dest.caution_note})
- 선호 스타일: {travel_style}

2. 요청 사항:
① 위 3박 4일 일정에 맞춘 '무리 없는 시간대별 동선(오전/오후/저녁)'을 상세히 짜줘.
② 현지인 추천 가성비 로컬 맛집과 카페를 각 일자별로 2곳씩 추천해줘.
③ 예상 예산({budget.total_per_person_krw // 10000}만 원대) 안에서 알뜰하게 즐길 수 있는 패스권(교통권)이나 결제 꿀팁을 알려줘.
④ {deal.departure_date[:7]}월 현지 날씨에 맞는 옷차림과 꼭 챙겨야 할 준비물 목록을 정리해줘."""
        return prompt

    @staticmethod
    def build_compact_prompt(
        dest: DestinationMeta,
        deal: FlightDeal,
        budget: BudgetBreakdown,
    ) -> str:
        """Generates an ultra-compact single-line prompt to stay strictly within Telegram URL length limits."""
        return f"{dest.name} 3박4일 여행 일정과 맛집 추천해줘 ({deal.departure_date} 출발, 항공 {deal.price_krw//10000}만, 1인예산 {budget.total_per_person_krw//10000}만)"

    @classmethod
    def get_chatgpt_deep_link(cls, prompt: str) -> str:
        """Generate direct ChatGPT search/prompt link."""
        encoded_prompt = urllib.parse.quote(prompt)
        return f"https://chatgpt.com/?q={encoded_prompt}"

    @classmethod
    def get_gemini_link(cls) -> str:
        return "https://gemini.google.com/app"

    @classmethod
    def get_claude_link(cls) -> str:
        return "https://claude.ai/new"
