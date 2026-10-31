"""Flight deal discovery and projection engine for weekend travel.
Finds Thursday-Sunday and Friday-Monday (3N4D) flights across a 6-month horizon.
"""
from __future__ import annotations
import calendar
import hashlib
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from travel_brief.models import DestinationMeta, FlightDeal


AIRLINES_BY_REGION = {
    "Japan": ["제주항공", "진에어", "티웨이항공", "에아서울", "대한항공", "피치항공"],
    "Southeast Asia": ["비엣젯항공", "제주항공", "진에어", "에어아시아", "티웨이항공", "대한항공"],
    "East Asia": ["중화항공", "에바항공", "제주항공", "티웨이항공", "아시아나항공"],
    "US / Pacific": ["진에어", "제주항공", "티웨이항공", "대한항공"],
}


class FlightEngine:
    def __init__(self, data_path: Optional[str | Path] = None):
        if data_path is None:
            data_path = Path(__file__).resolve().parent.parent / "data" / "destinations_meta.json"
        self.data_path = Path(data_path)
        self.destinations: dict[str, DestinationMeta] = {}
        self._load_destinations()

    def _load_destinations(self) -> None:
        if not self.data_path.exists():
            return
        with open(self.data_path, "r", encoding="utf-8") as f:
            raw_list = json.load(f)
        for item in raw_list:
            meta = DestinationMeta(
                code=item["code"],
                name=item["name"],
                country=item["country"],
                region=item["region"],
                flight_time_hours=item["flight_time_hours"],
                visa_info=item["visa_info"],
                best_months=item["best_months"],
                caution_months=item["caution_months"],
                caution_note=item["caution_note"],
                flight_baseline_krw=item["flight_baseline_krw"],
                flight_deal_threshold_krw=item["flight_deal_threshold_krw"],
                hotel_night_avg_krw=item["hotel_night_avg_krw"],
                daily_meal_transit_krw=item["daily_meal_transit_krw"],
                currency=item["currency"],
                tips=item["tips"],
            )
            self.destinations[meta.code] = meta

    def get_destination(self, code: str) -> Optional[DestinationMeta]:
        return self.destinations.get(code.upper())

    def list_destinations(self) -> list[DestinationMeta]:
        return list(self.destinations.values())

    def _pick_airline(self, region: str, seed: str) -> str:
        airlines = AIRLINES_BY_REGION.get(region, ["국적항공사", "LCC특가"])
        idx = int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16) % len(airlines)
        return airlines[idx]

    def _estimate_price(self, dest: DestinationMeta, dep_date: date, day_of_week: int) -> int:
        """Estimate realistic roundtrip weekend flight fare using seasonality and booking curves.
        day_of_week: 3=Thursday, 4=Friday
        """
        seed = f"{dest.code}-{dep_date.isoformat()}"
        hash_val = int(hashlib.md5(seed.encode("utf-8")).hexdigest()[:6], 16) / 0xFFFFFF  # 0.0 ~ 1.0

        multiplier = 1.0

        # 1. Seasonality
        if dest.is_best_month(dep_date.month):
            multiplier *= 1.05 + (hash_val * 0.15)  # 1.05 ~ 1.20
        elif dest.is_caution_month(dep_date.month):
            multiplier *= 0.72 + (hash_val * 0.15)  # 0.72 ~ 0.87 (big discount in rainy/off season)
        else:
            multiplier *= 0.88 + (hash_val * 0.18)  # 0.88 ~ 1.06

        # 2. Advance booking window sweet spot (4 to 12 weeks ahead is cheapest)
        today = date.today()
        weeks_ahead = max(0, (dep_date - today).days // 7)
        if weeks_ahead < 2:
            multiplier *= 1.25  # Last minute surge
        elif 4 <= weeks_ahead <= 10:
            multiplier *= 0.90  # Early bird sweet spot
        elif weeks_ahead > 16:
            multiplier *= 1.02  # Too far ahead (LCC promos not yet released)

        # 3. Thursday departure bonus (usually 8-12% cheaper than Friday evening)
        if day_of_week == 3:  # Thursday
            multiplier *= 0.92

        # 4. Super deal flash promo (1 out of ~6 flights has extra discount)
        if (int(seed[-2:], 16) if seed[-2:].isalnum() else 0) % 6 == 0:
            multiplier *= 0.85

        final_price = int(dest.flight_baseline_krw * multiplier)
        # Round to nearest 1,000 KRW
        return max(79000, round(final_price, -3))

    def generate_weekend_slots(self, ref_date: Optional[date] = None, months_ahead: int = 6) -> list[tuple[date, date, str, str, int]]:
        """Generate weekend departure-return date pairs:
        - Thursday to Sunday (3 nights)
        - Friday to Monday (3 nights)
        """
        if ref_date is None:
            ref_date = date.today()

        slots = []
        # Start looking from next Thursday
        curr = ref_date + timedelta(days=1)
        end_date = ref_date + timedelta(days=months_ahead * 30)

        dow_names = ["월", "화", "수", "목", "금", "토", "일"]

        while curr <= end_date:
            # 3 = Thursday
            if curr.weekday() == 3:
                ret = curr + timedelta(days=3)  # Sunday
                slots.append((curr, ret, "목", "일", 3))
            # 4 = Friday
            elif curr.weekday() == 4:
                ret = curr + timedelta(days=3)  # Monday
                slots.append((curr, ret, "금", "월", 3))
            curr += timedelta(days=1)

        return slots

    def search_deals(
        self,
        dest_code: Optional[str] = None,
        max_budget_flight: Optional[int] = None,
        months_ahead: int = 6,
        super_deals_only: bool = False,
        ref_date: Optional[date] = None,
    ) -> list[FlightDeal]:
        """Search flight deals across destinations and weekend dates."""
        slots = self.generate_weekend_slots(ref_date=ref_date, months_ahead=months_ahead)
        if dest_code:
            code = dest_code.strip().upper()
            if code in self.destinations:
                targets = [self.destinations[code]]
            else:
                # Explicitly handle invalid destination code instead of silent fallback to all
                return []
        else:
            targets = list(self.destinations.values())

        deals: list[FlightDeal] = []

        for dest in targets:
            for dep, ret, dep_dow, ret_dow, nights in slots:
                price = self._estimate_price(dest, dep, dep.weekday())
                if max_budget_flight and price > max_budget_flight:
                    continue

                is_super = price <= dest.flight_deal_threshold_krw
                if super_deals_only and not is_super:
                    continue

                airline = self._pick_airline(dest.region, f"{dest.code}-{dep.isoformat()}")
                deal = FlightDeal(
                    destination_code=dest.code,
                    destination_name=dest.name,
                    origin_code="ICN",
                    departure_date=dep.isoformat(),
                    return_date=ret.isoformat(),
                    departure_day_of_week=dep_dow,
                    return_day_of_week=ret_dow,
                    nights=nights,
                    airline=airline,
                    price_krw=price,
                    baseline_krw=dest.flight_baseline_krw,
                    is_super_deal=is_super,
                )
                deals.append(deal)

        deals.sort(key=lambda d: d.price_krw)
        return deals

    def get_cheapest_deal_per_destination(
        self, months_ahead: int = 6, ref_date: Optional[date] = None
    ) -> dict[str, FlightDeal]:
        """Return the single cheapest weekend flight deal for each destination."""
        cheapest: dict[str, FlightDeal] = {}
        all_deals = self.search_deals(months_ahead=months_ahead, ref_date=ref_date)
        for deal in all_deals:
            code = deal.destination_code
            if code not in cheapest or deal.price_krw < cheapest[code].price_krw:
                cheapest[code] = deal
        return cheapest

    def get_optimal_deal_per_destination(
        self, months_ahead: int = 6, ref_date: Optional[date] = None
    ) -> dict[str, FlightDeal]:
        """Return the cheapest weekend deal that falls within the destination's BEST season."""
        optimal: dict[str, FlightDeal] = {}
        all_deals = self.search_deals(months_ahead=months_ahead, ref_date=ref_date)
        for deal in all_deals:
            dest = self.destinations[deal.destination_code]
            dep_month = int(deal.departure_date.split("-")[1])
            if dest.is_best_month(dep_month):
                code = deal.destination_code
                if code not in optimal or deal.price_krw < optimal[code].price_krw:
                    optimal[code] = deal

        # Fallback to cheapest overall if no deals fell in best month within window
        cheapest = self.get_cheapest_deal_per_destination(months_ahead=months_ahead, ref_date=ref_date)
        for code, deal in cheapest.items():
            if code not in optimal:
                optimal[code] = deal

        return optimal

    def get_6month_matrix(
        self, ref_date: Optional[date] = None
    ) -> dict[str, dict[str, FlightDeal]]:
        """Return a matrix of [destination_code][YYYY-MM] -> cheapest FlightDeal."""
        if ref_date is None:
            ref_date = date.today()

        all_deals = self.search_deals(months_ahead=6, ref_date=ref_date)
        matrix: dict[str, dict[str, FlightDeal]] = {code: {} for code in self.destinations}

        for deal in all_deals:
            month_key = deal.departure_date[:7]  # YYYY-MM
            dest_code = deal.destination_code
            if month_key not in matrix[dest_code] or deal.price_krw < matrix[dest_code][month_key].price_krw:
                matrix[dest_code][month_key] = deal

        return matrix
