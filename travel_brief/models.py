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
    flight_min_floor_krw: int = 0
    airlines: list[str] = field(default_factory=list)

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


def round_man(krw: int | float) -> int:
    """Standard half-up rounding to 10,000 KRW (만 원) units (avoids Banker's even-rounding mismatch)."""
    return max(0, (int(krw) + 5000) // 10000)


def reconcile_man_breakdown(flight_krw: int | float, hotel_krw: int | float, meal_krw: int | float) -> tuple[int, int, int, int]:
    """Rounds (flight, hotel, meal) to 10,000 KRW units using Largest-Remainder reconciliation
    so `flight_man + hotel_man + meal_man == total_man == round_man(flight_krw + hotel_krw + meal_krw)` ALWAYS holds.
    """
    exact_vals = [float(flight_krw) / 10000.0, float(hotel_krw) / 10000.0, float(meal_krw) / 10000.0]
    rounded = [round_man(flight_krw), round_man(hotel_krw), round_man(meal_krw)]
    target_total = round_man(float(flight_krw) + float(hotel_krw) + float(meal_krw))
    diff = sum(rounded) - target_total
    while diff > 0:
        # Reduce the item with the largest upward rounding error (rounded[i] - exact_vals[i])
        idx = max(range(3), key=lambda i: (rounded[i] - exact_vals[i], i == 1))
        rounded[idx] = max(0, rounded[idx] - 1)
        diff -= 1
    while diff < 0:
        # Increase the item with the largest downward rounding error (exact_vals[i] - rounded[i])
        idx = max(range(3), key=lambda i: (exact_vals[i] - rounded[i], i == 1))
        rounded[idx] += 1
        diff += 1
    return rounded[0], rounded[1], rounded[2], target_total


def _normalize_hhmm(raw: str) -> str:
    s = (raw or "").strip().replace(":", "")
    if len(s) == 4 and s.isdigit():
        return f"{s[:2]}:{s[2:]}"
    return (raw or "").strip()


def format_live_schedule_slots(
    departure_day_of_week: str,
    return_day_of_week: str,
    dep_hhmm: str,
    ret_dep_hhmm: str = "",
    ret_arr_hhmm: str = "",
    next_day_arrival: bool = False,
) -> tuple[str, str]:
    """Formats real-time exact HH:MM flight departure & return schedule with realistic office-worker leave guidance."""
    dow = departure_day_of_week.strip()
    ret_dow = return_day_of_week.strip()
    norm_dep = _normalize_hhmm(dep_hhmm)
    norm_ret_dep = _normalize_hhmm(ret_dep_hhmm)
    norm_ret_arr = _normalize_hhmm(ret_arr_hhmm)

    dep_slot = format_departure_time_slot(dow, "morning")
    if ":" in norm_dep:
        try:
            hh, mm = [int(x) for x in norm_dep.split(":", 1)]
            mins = hh * 60 + mm
            is_thu = dow in ("목", "Thu", "Thursday")
            if mins < 360:  # 00:00 ~ 05:59 (전날 밤 공항 이동 필수)
                leave_note = "수 퇴근 후 공항행 · 목·금 연차 2일" if is_thu else "목 퇴근 후 공항행 · 금·월 연차 2일"
                dep_slot = f"심야 {norm_dep} 출발 ({leave_note})"
            elif mins < 870:  # 06:00 ~ 14:29 (국제선 수속 마감 고려 시 전일 연차 필수)
                prefix = "오전" if hh < 12 else "낮"
                leave_note = "목·금 연차 2일 추천" if is_thu else "금·월 연차 2일 · 알찬 3박4일"
                dep_slot = f"{prefix} {norm_dep} 출발 ({leave_note})"
            elif mins < 1170:  # 14:30 ~ 19:29 (오후 반차 탑승 가능 구간)
                prefix = "오후" if hh < 18 else "저녁"
                leave_note = "목 오후반차 + 금 연차 1.5일" if is_thu else "금 오후반차 + 월 연차 1.5일"
                dep_slot = f"{prefix} {norm_dep} 출발 ({leave_note})"
            else:  # 19:30 ~ 23:59 (퇴근 후 공항 이동 가능)
                leave_note = "목 퇴근 후 출발 · 금 연차 1일" if is_thu else "금 퇴근 후 출발 · 월 연차 1일"
                dep_slot = f"야간 {norm_dep} 출발 ({leave_note})"
        except ValueError:
            pass

    if norm_ret_dep and norm_ret_arr:
        if next_day_arrival:
            ret_slot = f"현지 {norm_ret_dep} 출발 → 인천 {norm_ret_arr} 도착 (⚠️ 익일 +1일 귀국)"
        elif ret_dow in ("월", "Mon", "Monday"):
            ret_slot = f"현지 {norm_ret_dep} 출발 → 인천 {norm_ret_arr} 도착 (월요일 귀국)"
        else:
            ret_slot = f"현지 {norm_ret_dep} 출발 → 인천 {norm_ret_arr} 도착 (귀국 후 휴식 가능)"
    elif norm_ret_dep:
        suffix = " (⚠️ 익일 +1일 도착 가능)" if next_day_arrival else ""
        ret_slot = f"현지 {norm_ret_dep} 출발{suffix}"
    else:
        ret_slot = "오후 18:00~21:30 (귀국 후 휴식 가능)"

    return dep_slot, ret_slot


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
    price_min_krw: int = 0
    price_max_krw: int = 0
    is_peak_holiday: bool = False
    holiday_name: str = ""
    data_source: Literal["LIVE_SSE", "LIVE_PG", "OFFLINE_ESTIMATE"] = "OFFLINE_ESTIMATE"
    general_fare_krw: int = 0
    popular_fare_krw: int = 0
    popular_airline: str = ""
    popular_dep_time: str = ""
    exact_dep_time: str = ""
    exact_ret_time: str = ""
    exact_ret_arr_time: str = ""
    next_day_arrival: bool = False

    def __post_init__(self):
        if self.exact_dep_time and (
            not self.departure_time_slot
            or self.departure_time_slot == "오전 08:30~10:30 (직장인 반차 추천)"
        ):
            dep_s, ret_s = format_live_schedule_slots(
                self.departure_day_of_week,
                self.return_day_of_week,
                self.exact_dep_time,
                self.exact_ret_time,
                self.exact_ret_arr_time,
                self.next_day_arrival,
            )
            self.departure_time_slot = dep_s
            if self.return_time_slot == "오후 18:00~21:30 (귀국 후 휴식 가능)" and (
                self.exact_ret_time or self.exact_ret_arr_time
            ):
                self.return_time_slot = ret_s
        elif not self.departure_time_slot or self.departure_time_slot == "오전 08:30~10:30 (직장인 반차 추천)":
            if self.data_source == "LIVE_PG":
                self.departure_time_slot = (
                    "목요일 직항 출발 (출도착 시각 실시간 링크 확인 · 연차 1~2일)"
                    if self.departure_day_of_week.strip() in ("목", "Thu", "Thursday")
                    else "금요일 직항 출발 (출도착 시각 실시간 링크 확인 · 연차 1~2일)"
                )
            else:
                self.departure_time_slot = format_departure_time_slot(
                    self.departure_day_of_week, self.time_category
                )
        if self.is_peak_holiday:
            self.is_super_deal = False
        if self.price_min_krw <= 0:
            self.price_min_krw = self.price_krw
        if self.price_max_krw <= 0:
            if self.data_source == "LIVE_SSE":
                self.price_max_krw = max(
                    self.price_min_krw + 30000,
                    self.general_fare_krw,
                    self.popular_fare_krw,
                )
            else:
                spread = 1.20 if self.is_peak_holiday else 1.14
                self.price_max_krw = max(
                    self.price_min_krw + 30000,
                    round(int(self.price_min_krw * spread), -3),
                )

    @property
    def is_live(self) -> bool:
        return self.data_source in ("LIVE_SSE", "LIVE_PG")

    @property
    def exact_price_str(self) -> str:
        if self.data_source in ("LIVE_SSE", "LIVE_PG"):
            return f"{self.price_krw:,}원~"
        return self.price_band_str

    @property
    def live_payment_note(self) -> str:
        if self.data_source == "LIVE_SSE":
            if self.general_fare_krw > self.price_krw:
                return f"카드혜택가 · 일반결제 {self.general_fare_krw:,}원"
            return "모든 결제수단 동일가"
        if self.data_source == "LIVE_PG":
            return "최근 검색 직항 캐시"
        return "유류세 포함 예상 밴드"

    @property
    def popular_banner_note(self) -> str:
        if self.data_source == "LIVE_SSE" and self.popular_fare_krw >= self.price_krw + 15000:
            meta_parts = [
                p
                for p in (
                    self.popular_airline,
                    f"{self.popular_dep_time} 출발" if self.popular_dep_time else "",
                )
                if p
            ]
            meta_str = f" ({' · '.join(meta_parts)})" if meta_parts else ""
            return f"상단 '인기항공편' 배너 {self.popular_fare_krw:,}원~{meta_str} · 아래 '가격 낮은 순' 1위 최저가"
        return ""

    @property
    def discount_pct(self) -> float:
        if self.baseline_krw <= 0:
            return 0.0
        return round((1 - (self.price_krw / self.baseline_krw)) * 100, 1)

    @property
    def price_diff_pct(self) -> float:
        """Signed % difference vs baseline: negative = cheaper (-14.0), positive = peak surge (+65.0)."""
        if self.baseline_krw <= 0:
            return 0.0
        return round(((self.price_krw / self.baseline_krw) - 1) * 100, 1)

    @property
    def price_band_str(self) -> str:
        """Human-readable market fare band in 만원 units (e.g. '34만~39만원대')."""
        low_man = max(1, round(self.price_min_krw / 10000))
        high_man = max(low_man + 3, round(self.price_max_krw / 10000))
        return f"{low_man}만~{high_man}만원대"

    @property
    def price_band_compact_str(self) -> str:
        """Compact fare band for matrix cells (e.g. '34~39만원')."""
        low_man = max(1, round(self.price_min_krw / 10000))
        high_man = max(low_man + 3, round(self.price_max_krw / 10000))
        return f"{low_man}~{high_man}만원"


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
    def calculate(
        cls,
        dest: DestinationMeta,
        flight_price: int,
        nights: int = 3,
        hotel_multiplier: float = 1.0,
    ) -> BudgetBreakdown:
        days = nights + 1
        effective_hotel_night = (
            dest.hotel_night_avg_krw
            if hotel_multiplier == 1.0
            else round(int(dest.hotel_night_avg_krw * hotel_multiplier), -3)
        )
        hotel_total = effective_hotel_night * nights
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
