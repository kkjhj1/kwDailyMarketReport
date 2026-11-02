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
        hotel_mult = 1.30 if getattr(flight, "is_peak_holiday", False) else 1.0
        return BudgetBreakdown.calculate(
            dest=dest,
            flight_price=flight.price_krw,
            nights=nights,
            hotel_multiplier=hotel_mult,
        )

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

        # 2. Discount / Deal score (up to 40 pts; peak holiday dates receive 0 deal bonus)
        discount = flight.discount_pct
        if flight.is_peak_holiday:
            deal_score = 0
        elif discount >= 18:
            deal_score = 40
        elif discount >= 12:
            deal_score = 32
        elif discount >= 5:
            deal_score = 22
        else:
            deal_score = 10

        # 3. Budget friendliness score (up to 25 pts)
        if budget.total_per_person_krw <= 650000:
            budget_score = 25
        elif budget.total_per_person_krw <= 800000:
            budget_score = 20
        elif budget.total_per_person_krw <= 1050000:
            budget_score = 15
        else:
            budget_score = 10

        total_score = max(5, season_score + deal_score + budget_score - (20 if flight.is_peak_holiday else 0))

        # Summary reason (consistent negative sign for discounts, positive for peak surge)
        reasons = []
        if flight.is_peak_holiday:
            reasons.append(f"🚨 {flight.holiday_name or '연휴 성수기'} ({flight.price_diff_pct:+.0f}%)")
        elif flight.is_super_deal:
            reasons.append(f"항공 {flight.price_diff_pct:+.0f}% 실속특가")
        if season_status == "BEST_SEASON":
            reasons.append("여행 최적기")
        reasons.append(f"1인 총 {budget.total_per_person_krw // 10000:,}만원대")

        summary_reason = " · ".join(reasons)

        from travel_brief.flight_engine import FlightEngine
        naver_flight_url = FlightEngine.build_naver_flight_url(
            flight.origin_code,
            flight.destination_code,
            flight.departure_date,
            flight.return_date,
        )

        import urllib.parse
        clean_hotel = dest.recommended_hotel_name.split("(")[0].strip() if dest.recommended_hotel_name else dest.name
        hotel_query = clean_hotel if dest.name in clean_hotel else f"{dest.name} {clean_hotel}"
        encoded_hotel = urllib.parse.quote(hotel_query)
        hotel_search_url = f"https://m-hotel.naver.com/hotels/search?destination={encoded_hotel}&checkIn={flight.departure_date}&checkOut={flight.return_date}&rooms=1&adults=2"

        return TravelRecommendation(
            destination=dest,
            best_deal=flight,
            budget=budget,
            season_status=season_status,
            recommendation_score=total_score,
            summary_reason=summary_reason,
            naver_flight_url=naver_flight_url,
            hotel_search_url=hotel_search_url,
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

        # Sort by recommendation score descending, total price ascending, then discount pct descending
        recs.sort(
            key=lambda r: (
                -r.recommendation_score,
                r.budget.total_per_person_krw,
                -r.best_deal.discount_pct,
            )
        )
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
