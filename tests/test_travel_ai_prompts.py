from travel_brief.ai_prompts import AIPromptGenerator
from travel_brief.flight_engine import FlightEngine
from travel_brief.budget_calculator import BudgetCalculator


def test_ai_prompt_generation_and_deep_link():
    engine = FlightEngine()
    dest = engine.get_destination("FUK")
    deals = engine.search_deals(dest_code="FUK", months_ahead=3)
    deal = deals[0]
    budget = BudgetCalculator.calculate_breakdown(dest, deal)

    prompt = AIPromptGenerator.build_prompt(dest, deal, budget)
    assert "후쿠오카" in prompt
    assert "3박 4일" in prompt
    assert "무비자 90일" in prompt
    assert "⑤ 위 일정 동선과 예산에 맞는 숙소 추천 지역(역세권/해변 등)과 숙소 유형도 2곳 추천해줘." in prompt

    chatgpt_url = AIPromptGenerator.get_chatgpt_deep_link(prompt)
    assert chatgpt_url.startswith("https://chatgpt.com/?q=")
    assert "3%EB%B0%95" in chatgpt_url  # URL-encoded Korean
    assert AIPromptGenerator.build_compact_prompt(dest, deal, budget) == "후쿠오카 3박4일 일정·맛집 추천"


def test_ai_prompt_long_haul_uses_five_plus_nights():
    from dataclasses import replace

    from travel_brief.city_catalog import CityCatalog

    engine = FlightEngine()
    dest = CityCatalog(engine.destinations).get("CDG").meta
    assert dest.is_long_haul
    # Even a legacy 3-night deal/budget is floored to the 5+ night long-haul stay.
    base = engine.search_deals(dest_code="FUK", months_ahead=3)[0]
    deal = replace(base, destination_code="CDG", destination_name=dest.name, nights=3)
    budget = BudgetCalculator.calculate_breakdown(dest, deal, nights=3)

    prompt = AIPromptGenerator.build_prompt(dest, deal, budget)
    assert f"[여행 계획 요청: {dest.name} 5박 6일 최적 일정]" in prompt
    assert "(5박 6일 장거리 일정 (5박 이상 권장))" in prompt
    assert "① 위 5박 6일 일정에" in prompt
    assert "3박" not in prompt and "주말 일정" not in prompt
    assert AIPromptGenerator.build_compact_prompt(dest, deal, budget) == f"{dest.name} 5박6일 일정·맛집 추천"

    # Longer long-haul stays are preserved, not clamped down.
    budget7 = BudgetCalculator.calculate_breakdown(dest, replace(deal, nights=7), nights=7)
    assert "7박 8일" in AIPromptGenerator.build_prompt(dest, deal, budget7)
    assert AIPromptGenerator.build_compact_prompt(dest, deal, budget7).endswith("7박8일 일정·맛집 추천")
