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

    chatgpt_url = AIPromptGenerator.get_chatgpt_deep_link(prompt)
    assert chatgpt_url.startswith("https://chatgpt.com/?q=")
    assert "3%EB%B0%95" in chatgpt_url  # URL-encoded Korean
