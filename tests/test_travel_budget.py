from travel_brief.budget_calculator import BudgetCalculator
from travel_brief.flight_engine import FlightEngine


def test_budget_recommendations_ranking():
    engine = FlightEngine()
    deals = engine.search_deals(months_ahead=6)
    recs = BudgetCalculator.rank_recommendations(
        destinations=engine.destinations, deals=deals, top_n=5
    )
    assert len(recs) == 5
    for r in recs:
        assert r.recommendation_score > 0
        assert r.budget.total_per_person_krw > 0
        assert len(r.summary_reason) > 0


def test_budget_filter():
    engine = FlightEngine()
    deals = engine.search_deals(months_ahead=6)
    # Filter 1-person budget <= 500k
    recs = BudgetCalculator.rank_recommendations(
        destinations=engine.destinations,
        deals=deals,
        max_budget_per_person=500000,
        top_n=10,
    )
    for r in recs:
        assert r.budget.total_per_person_krw <= 500000


def test_rank_recommendations_deduplicate_destinations():
    engine = FlightEngine()
    deals = engine.search_deals(months_ahead=6)
    recs = BudgetCalculator.rank_recommendations(
        destinations=engine.destinations,
        deals=deals,
        top_n=5,
    )
    dest_codes = [r.destination.code for r in recs]
    # Verify that each recommendation in top N is a unique destination
    assert len(dest_codes) == len(set(dest_codes))
