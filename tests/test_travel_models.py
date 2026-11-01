from datetime import date
from travel_brief.models import DestinationMeta, FlightDeal, BudgetBreakdown, TravelRecommendation, format_departure_time_slot


def test_destination_meta_methods():
    dest = DestinationMeta(
        code="FUK",
        name="후쿠오카",
        country="일본",
        region="Japan",
        flight_time_hours=1.5,
        visa_info="무비자 90일",
        best_months=[10, 11, 3, 4],
        caution_months=[7, 8],
        caution_note="폭염",
        flight_baseline_krw=220000,
        flight_deal_threshold_krw=170000,
        hotel_night_avg_krw=90000,
        daily_meal_transit_krw=50000,
        currency="JPY",
        tips="가까움",
    )
    assert dest.is_best_month(11) is True
    assert dest.is_best_month(6) is False
    assert dest.is_caution_month(7) is True
    assert dest.is_caution_month(11) is False


def test_flight_deal_discount_pct():
    deal = FlightDeal(
        destination_code="FUK",
        destination_name="후쿠오카",
        origin_code="ICN",
        departure_date="2026-11-12",
        return_date="2026-11-15",
        departure_day_of_week="목",
        return_day_of_week="일",
        nights=3,
        airline="제주항공",
        price_krw=165000,
        baseline_krw=220000,
        is_super_deal=True,
    )
    assert deal.discount_pct == 25.0
    assert deal.is_super_deal is True


def test_budget_breakdown_calculation():
    dest = DestinationMeta(
        code="DAD",
        name="다낭",
        country="베트남",
        region="Southeast Asia",
        flight_time_hours=4.5,
        visa_info="무비자 45일",
        best_months=[1, 2, 3],
        caution_months=[9, 10],
        caution_note="우기",
        flight_baseline_krw=350000,
        flight_deal_threshold_krw=240000,
        hotel_night_avg_krw=60000,
        daily_meal_transit_krw=35000,
        currency="VND",
        tips="그랩",
    )
    budget = BudgetBreakdown.calculate(dest=dest, flight_price=200000, nights=3)
    assert budget.nights == 3
    assert budget.days == 4
    assert budget.flight_krw == 200000
    assert budget.hotel_total_krw == 180000  # 60k * 3
    assert budget.meal_transit_total_krw == 140000  # 35k * 4
    assert budget.total_per_person_krw == 520000  # 200k + 180k + 140k
    assert budget.estimated_for_two_krw == 860000  # (200k*2) + 180k + (140k*2)


def test_departure_time_slot_branching():
    # 1. Thursday
    assert format_departure_time_slot("목") == "오전 08:30~10:30 (목·금 연차 2일 추천)"
    assert format_departure_time_slot("목", "afternoon") == "오전 08:30~10:30 (목·금 연차 2일 추천)"

    # 2. Friday Morning
    assert format_departure_time_slot("금", "morning") == "오전 08:30~10:30 (금요일 전일 연차 · 알찬 3박4일)"

    # 3. Friday Afternoon
    assert format_departure_time_slot("금", "afternoon") == "오후 14:00~16:00 (금요일 오후 반차 추천)"
    assert format_departure_time_slot("금", "오후") == "오후 14:00~16:00 (금요일 오후 반차 추천)"

    # 4. Friday Night
    assert format_departure_time_slot("금", "night") == "야간 19:30~21:00 (퇴근 후 출발 · 연차 절약)"
    assert format_departure_time_slot("금", "야간") == "야간 19:30~21:00 (퇴근 후 출발 · 연차 절약)"

    # 5. FlightDeal auto post-init
    deal_thu = FlightDeal(
        destination_code="FUK", destination_name="후쿠오카", origin_code="ICN",
        departure_date="2026-12-24", return_date="2026-12-27",
        departure_day_of_week="목", return_day_of_week="일",
        nights=3, airline="진에어", price_krw=167000, baseline_krw=220000,
    )
    assert deal_thu.departure_time_slot == "오전 08:30~10:30 (목·금 연차 2일 추천)"

    deal_fri_aft = FlightDeal(
        destination_code="DAD", destination_name="다낭", origin_code="ICN",
        departure_date="2026-12-18", return_date="2026-12-21",
        departure_day_of_week="금", return_day_of_week="월",
        nights=3, airline="비엣젯", price_krw=223000, baseline_krw=320000,
        time_category="afternoon",
    )
    assert deal_fri_aft.departure_time_slot == "오후 14:00~16:00 (금요일 오후 반차 추천)"

    deal_fri_night = FlightDeal(
        destination_code="DAD", destination_name="다낭", origin_code="ICN",
        departure_date="2026-12-18", return_date="2026-12-21",
        departure_day_of_week="금", return_day_of_week="월",
        nights=3, airline="비엣젯", price_krw=223000, baseline_krw=320000,
        time_category="night",
    )
    assert deal_fri_night.departure_time_slot == "야간 19:30~21:00 (퇴근 후 출발 · 연차 절약)"
