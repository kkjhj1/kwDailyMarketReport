"""Budget calculator and recommendation scorer for weekend travel.
Computes 1-person and 2-person itemized expense estimates and scores trips.
"""
from __future__ import annotations
from typing import Optional, Literal
from travel_brief.models import DestinationMeta, FlightDeal, BudgetBreakdown, TravelRecommendation


class BudgetCalculator:
    """Computes realistic travel budgets based on flight deals and destination cost baselines."""

    @staticmethod
    def calculate_breakdown(dest: DestinationMeta, flight: FlightDeal, nights: int = 3) -> BudgetBreakdown:
        return BudgetBreakdown.calculate(dest=dest, flight_price=flight.price_krw, nights=nights)

    @classmethod
    def evaluate_deal(cls, dest: DestinationMeta, flight: FlightDeal) -> TravelRecommendation:
        budget = cls.calculate_breakdown(dest, flight, nights=flight.nights)
        dep_month = int(flight.departure_date.split("-")[1])

        # 1. Season status
        if dest.is_best_month(dep_month):
            season_status: Literal["BEST_SEASON", "NORMAL", "CAUTION"] = "BEST_SEASON"
            season_score = 35
            season_text = "✨ 최적의 여행 시즌 (날씨 쾌적)"
        elif dest.is_caution_month(dep_month):
            season_status = "CAUTION"
            season_score = 10
            season_text = f"⚠️ 주의 기간 ({dest.caution_note})"
        else:
            season_status = "NORMAL"
            season_score = 25
            season_text = "🌤️ 무난한 여행 시즌"

        # 2. Discount / Deal score (up to 40 pts)
        discount = flight.discount_pct
        if discount >= 25:
            deal_score = 40
        elif discount >= 15:
            deal_score = 30
        elif discount >= 5:
            deal_score = 20
        else:
            deal_score = 10

        # 3. Budget friendliness score (up to 25 pts)
        # 1-person budget <= 500k: 25pts, <= 700k: 20pts, <= 1M: 15pts, > 1M: 10pts
        if budget.total_per_person_krw <= 500000:
            budget_score = 25
        elif budget.total_per_person_krw <= 750000:
            budget_score = 20
        elif budget.total_per_person_krw <= 1000000:
            budget_score = 15
        else:
            budget_score = 10

        total_score = season_score + deal_score + budget_score

        # Summary reason
        reasons = []
        if flight.is_super_deal:
            reasons.append(f"항공 {discount:+.0f}% 초특가")
        if season_status == "BEST_SEASON":
            reasons.append("여행 최적기")
        reasons.append(f"1인 총 {budget.total_per_person_krw // 10000:,}만원대")

        summary_reason = " · ".join(reasons)

        return TravelRecommendation(
            destination=dest,
            best_deal=flight,
            budget=budget,
            season_status=season_status,
            recommendation_score=total_score,
            summary_reason=summary_reason,
        )

    @classmethod
    def rank_recommendations(
        cls,
        destinations: dict[str, DestinationMeta],
        deals: list[FlightDeal],
        max_budget_per_person: Optional[int] = None,
        top_n: int = 10,
    ) -> list[TravelRecommendation]:
        """Rank and return top travel recommendations."""
        recs: list[TravelRecommendation] = []
        for deal in deals:
            dest = destinations.get(deal.destination_code)
            if not dest:
                continue
            rec = cls.evaluate_deal(dest, deal)
            if max_budget_per_person and rec.budget.total_per_person_krw > max_budget_per_person:
                continue
            recs.append(rec)

        # Sort by recommendation score descending, then by total price ascending
        recs.sort(key=lambda r: (-r.recommendation_score, r.budget.total_per_person_krw))
        # Deduplicate by destination: keep single best recommendation per destination
        deduped: list[TravelRecommendation] = []
        seen_dest_codes: set[str] = set()
        for r in recs:
            code = r.destination.code.upper()
            if code not in seen_dest_codes:
                seen_dest_codes.add(code)
                deduped.append(r)
                if len(deduped) >= top_n:
                    break

        return deduped
