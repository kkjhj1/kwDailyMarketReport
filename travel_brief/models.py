from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

@dataclass
class DestinationMeta:
    code: str
    name: str
    country: str
    region: str
    flight_time_hours: float
    visa_info: str
    best_months: list[int]
    caution_months: list[int]
    caution_note: str
    flight_baseline_krw: int
    flight_deal_threshold_krw: int
    hotel_night_avg_krw: int
    daily_meal_transit_krw: int
    currency: str
    tips: str
    recommended_hotel_name: str = ""
    recommended_hotel_grade: str = ""
    recommended_hotel_features: str = ""

    def is_best_month(self, month: int) -> bool:
        return month in self.best_months

    def is_caution_month(self, month: int) -> bool:
        return month in self.caution_months


def format_departure_time_slot(departure_day_of_week: str, time_category: str = "morning") -> str:
    """Formats realistic departure time slot with worker vacation recommendation.
    - 목요일: 오전 08:30~10:30 (목·금 연차 2일 추천)
    - 금요일 아침: 오전 08:30~10:30 (금요일 전일 연차 · 알찬 3박4일)
    - 금요일 오후: 오후 14:00~16:00 (금요일 오후 반차 추천)
    - 금요일 야간: 야간 19:30~21:00 (퇴근 후 출발 · 연차 절약)
    """
    dow = departure_day_of_week.strip()
    tc = time_category.strip().lower()

    if dow in ("목", "Thu", "Thursday"):
        return "오전 08:30~10:30 (목·금 연차 2일 추천)"

    if dow in ("금", "Fri", "Friday"):
        if tc in ("afternoon", "오후", "afternoon_half"):
            return "오후 14:00~16:00 (금요일 오후 반차 추천)"
        elif tc in ("night", "evening", "야간", "night_saver"):
            return "야간 19:30~21:00 (퇴근 후 출발 · 연차 절약)"
        else:
            return "오전 08:30~10:30 (금요일 전일 연차 · 알찬 3박4일)"

    return "오전 08:30~10:30 (주말 출발)"


@dataclass
class FlightDeal:
    destination_code: str
    destination_name: str
    origin_code: str  # ICN, GMP
    departure_date: str  # YYYY-MM-DD
    return_date: str     # YYYY-MM-DD
    departure_day_of_week: str  # 목, 금, 토
    return_day_of_week: str     # 일, 월
    nights: int  # 3 or 4
    airline: str
    price_krw: int
    baseline_krw: int
    is_super_deal: bool = False
    departure_time_slot: str = ""
    return_time_slot: str = "오후 18:00~21:30 (귀국 후 휴식 가능)"
    time_category: str = "morning"  # morning, afternoon, night

    def __post_init__(self):
        if not self.departure_time_slot or self.departure_time_slot == "오전 08:30~10:30 (직장인 반차 추천)":
            self.departure_time_slot = format_departure_time_slot(
                self.departure_day_of_week, self.time_category
            )

    @property
    def discount_pct(self) -> float:
        if self.baseline_krw <= 0:
            return 0.0
        return round((1 - (self.price_krw / self.baseline_krw)) * 100, 1)


@dataclass
class BudgetBreakdown:
    destination_name: str
    nights: int  # 3박
    days: int    # 4일
    flight_krw: int
    hotel_total_krw: int      # hotel_night_avg * nights
    meal_transit_total_krw: int  # daily_meal_transit * days
    total_per_person_krw: int
    estimated_for_two_krw: int  # 2인 기준 (항공*2 + 호텔1실 + 식비*2)

    @classmethod
    def calculate(cls, dest: DestinationMeta, flight_price: int, nights: int = 3) -> BudgetBreakdown:
        days = nights + 1
        hotel_total = dest.hotel_night_avg_krw * nights
        meal_total = dest.daily_meal_transit_krw * days
        total_one = flight_price + hotel_total + meal_total
        # 2인 기준: 호텔은 2인 1실로 공유하므로 1실 요금만 적용
        total_two = (flight_price * 2) + hotel_total + (meal_total * 2)
        return cls(
            destination_name=dest.name,
            nights=nights,
            days=days,
            flight_krw=flight_price,
            hotel_total_krw=hotel_total,
            meal_transit_total_krw=meal_total,
            total_per_person_krw=total_one,
            estimated_for_two_krw=total_two,
        )


@dataclass
class TravelRecommendation:
    destination: DestinationMeta
    best_deal: FlightDeal
    budget: BudgetBreakdown
    season_status: Literal["BEST_SEASON", "NORMAL", "CAUTION"]
    recommendation_score: int  # 1~100 가성비 점수
    summary_reason: str
    naver_flight_url: str = ""
    hotel_search_url: str = ""
