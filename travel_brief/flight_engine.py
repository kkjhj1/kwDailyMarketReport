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

from travel_brief.city_catalog import CURATED_EXPERIENCE_HOOKS
from travel_brief.models import DestinationMeta, FlightDeal, format_departure_time_slot


AIRLINES_BY_REGION = {
    "동아시아": ["제주항공", "진에어", "티웨이항공", "에어서울"],
    "동남아": ["제주항공", "진에어", "티웨이항공", "비엣젯항공", "에어서울"],
    "태평양": ["진에어", "제주항공", "티웨이항공", "대한항공"],
    "Japan": ["제주항공", "진에어", "티웨이항공", "에어서울", "피치항공"],
    "Southeast Asia": ["비엣젯항공", "제주항공", "진에어", "타이에어아시아X", "티웨이항공"],
    "East Asia": ["제주항공", "진에어", "티웨이항공", "중화항공", "에바항공"],
    "US / Pacific": ["진에어", "제주항공", "티웨이항공", "대한항공"],
}

FSC_CARRIERS = {"대한항공", "아시아나항공", "가루다인도네시아", "중화항공", "에바항공"}


class FlightEngine:
    def __init__(self, data_path: Optional[str | Path] = None, live_naver: bool = False):
        if data_path is None:
            data_path = Path(__file__).resolve().parent.parent / "data" / "destinations_meta.json"
        self.data_path = Path(data_path)
        self.live_naver = bool(live_naver)
        self._live_client = None
        if self.live_naver:
            from travel_brief.naver_live import NaverFlightLiveClient
            self._live_client = NaverFlightLiveClient()
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
                recommended_hotel_name=item.get("recommended_hotel_name", ""),
                recommended_hotel_grade=item.get("recommended_hotel_grade", ""),
                recommended_hotel_features=item.get("recommended_hotel_features", ""),
                flight_min_floor_krw=item.get("flight_min_floor_krw", 0),
                airlines=item.get("airlines", []),
                hotel_budget_segment=item.get("hotel_budget_segment", "3성급 가성비 비즈니스 호텔"),
                metadata_quality=item.get("metadata_quality", "CURATED"),
                experience_hook=item.get("experience_hook") or CURATED_EXPERIENCE_HOOKS.get(item["code"], ""),
            )
            self.destinations[meta.code] = meta

    def get_destination(self, code: str) -> Optional[DestinationMeta]:
        return self.destinations.get(code.upper())

    def list_destinations(self) -> list[DestinationMeta]:
        return list(self.destinations.values())

    @staticmethod
    def build_naver_flight_url(origin: str, dest: str, dep_date: str, ret_date: str) -> str:
        """Builds exact Naver Flight real-time nonstop fare comparison URL.
        Example: https://m-flight.naver.com/flights/international/ICN-DAD-20261218/DAD-ICN-20261221?fareType=Y&adult=1&isDirect=true
        """
        dep_compact = dep_date.replace("-", "")
        ret_compact = ret_date.replace("-", "")
        return f"https://m-flight.naver.com/flights/international/{origin}-{dest}-{dep_compact}/{dest}-{origin}-{ret_compact}?fareType=Y&adult=1&isDirect=true"

    @staticmethod
    def build_naver_hotel_url(dest_name: str, dep_date: str, ret_date: str) -> str:
        """Builds exact Naver Hotel real-time comparison URL with checkin/checkout dates."""
        import urllib.parse
        encoded = urllib.parse.quote(dest_name)
        return f"https://m-hotel.naver.com/hotels/search?destination={encoded}&checkIn={dep_date}&checkOut={ret_date}&rooms=1&adults=2"

    @staticmethod
    def get_peak_holiday_info(dep_date: date, ret_date: Optional[date] = None) -> tuple[bool, str, float]:
        """Checks interval overlap [dep_date, ret_date] against Korean national holidays & peak travel seasons.
        Returns (is_peak_holiday, holiday_label, surge_multiplier).
        """
        end_d = ret_date or (dep_date + timedelta(days=3))
        windows: list[tuple[date, date, float, str]] = []

        for yr in sorted({dep_date.year - 1, dep_date.year, end_d.year}):
            windows.extend([
                (date(yr, 12, 23), date(yr + 1, 1, 3), 1.68, "크리스마스·연말 성수기"),
                (date(yr, 2, 26), date(yr, 3, 2), 1.35, "삼일절 연휴"),
                (date(yr, 4, 30), date(yr, 5, 6), 1.55, "5월 황금연휴"),
                (date(yr, 6, 4), date(yr, 6, 7), 1.25, "현충일 연휴"),
                (date(yr, 7, 23), date(yr, 8, 16), 1.50, "여름 휴가 극성수기"),
            ])

        # Lunar & substitute holiday windows (2026-2028)
        windows.extend([
            (date(2026, 2, 13), date(2026, 2, 19), 1.80, "설날 황금연휴"),
            (date(2026, 5, 22), date(2026, 5, 25), 1.38, "석가탄신일 연휴"),
            (date(2026, 9, 23), date(2026, 9, 28), 1.80, "추석 황금연휴"),
            (date(2026, 10, 2), date(2026, 10, 11), 1.40, "개천절·한글날 연휴"),
            (date(2027, 2, 4), date(2027, 2, 10), 1.80, "설날 황금연휴"),
            (date(2027, 5, 12), date(2027, 5, 16), 1.35, "석가탄신일 연휴"),
            (date(2027, 9, 13), date(2027, 9, 19), 1.80, "추석 황금연휴"),
            (date(2027, 10, 1), date(2027, 10, 10), 1.40, "개천절·한글날 연휴"),
            (date(2028, 1, 25), date(2028, 1, 31), 1.80, "설날 황금연휴"),
        ])

        best_mult = 1.0
        best_label = ""
        for w_start, w_end, mult, label in windows:
            if dep_date <= w_end and end_d >= w_start:
                if mult > best_mult:
                    best_mult = mult
                    best_label = label

        if best_mult > 1.0:
            return True, best_label, best_mult
        return False, "", 1.0

    def _pick_airline(self, region: str, seed: str) -> str:
        airlines = AIRLINES_BY_REGION.get(region, ["제주항공", "진에어", "티웨이항공"])
        idx = int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16) % len(airlines)
        return airlines[idx]

    def _pick_airline_for_dest(self, dest: DestinationMeta, seed: str, is_peak: bool = False) -> str:
        candidates = list(dest.airlines) if dest.airlines else list(
            AIRLINES_BY_REGION.get(dest.region, ["제주항공", "진에어", "티웨이항공"])
        )
        if not is_peak:
            lcc_only = [a for a in candidates if a not in FSC_CARRIERS]
            if lcc_only:
                candidates = lcc_only
        idx = int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16) % len(candidates)
        return candidates[idx]

    def _pick_time_category(self, dep_dow: str, seed: str, dest_code: str = "") -> str:
        """Determines realistic flight time category based on departure day of week & route.
        - Thursday (목): 'morning' (목·금 연차 2일 추천)
        - Friday (금): distributed realistically across:
          morning (오전 전일 연차), afternoon (오후 반차), night (야간 퇴근 후 출발)
        """
        if dep_dow == "목":
            return "morning"

        if dest_code == "DPS":
            # 7-hour Bali non-stop operates morning/afternoon out of ICN
            mod_dps = int(hashlib.md5(f"slot-{seed}".encode("utf-8")).hexdigest()[:4], 16) % 2
            return "morning" if mod_dps == 0 else "afternoon"

        mod = int(hashlib.md5(f"slot-{seed}".encode("utf-8")).hexdigest()[:4], 16) % 10
        if mod < 4:
            return "morning"
        elif mod < 7:
            return "afternoon"
        else:
            return "night"

    def _estimate_price(
        self,
        dest: DestinationMeta,
        dep_date: date,
        day_of_week: int,
        ret_date: Optional[date] = None,
        time_category: str = "morning",
        ref_date: Optional[date] = None,
    ) -> int:
        """Estimate realistic roundtrip weekend flight fare (Price-First model).
        Incorporates Korean holiday surge calendar, statutory tax+fuel floor,
        advance booking curve, and departure slot demand premium.
        day_of_week: 3=Thursday, 4=Friday
        """
        seed = f"{dest.code}-{dep_date.isoformat()}"
        hash_val = int(hashlib.md5(seed.encode("utf-8")).hexdigest()[:6], 16) / 0xFFFFFF  # 0.0 ~ 1.0

        is_peak, _, holiday_mult = self.get_peak_holiday_info(dep_date, ret_date)

        if is_peak:
            # Peak holidays block promo booking classes; apply surge directly
            multiplier = holiday_mult * (0.97 + (hash_val * 0.08))
            if day_of_week == 4 and time_category in ("afternoon", "night"):
                multiplier *= 1.05
        else:
            multiplier = 1.0

            # 1. Seasonality (realistic weekend total fare band + Korean winter vacation demand)
            if dest.is_best_month(dep_date.month):
                multiplier *= 0.96 + (hash_val * 0.12)  # 0.96 ~ 1.08
            elif dest.is_caution_month(dep_date.month):
                multiplier *= 0.80 + (hash_val * 0.10)  # 0.80 ~ 0.90 (off-season promo)
            else:
                multiplier *= 0.86 + (hash_val * 0.12)  # 0.86 ~ 0.98

            if dep_date.month in (1, 2) and dest.region in ("동남아", "Southeast Asia", "태평양", "US / Pacific"):
                multiplier *= 1.22  # Winter school vacation surge for warm-weather routes

            # 2. Advance booking window (deterministic against ref_date)
            base_ref = ref_date or date.today()
            weeks_ahead = max(0, (dep_date - base_ref).days // 7)
            if weeks_ahead < 2:
                multiplier *= 1.22  # Last-minute booking class jump
            elif weeks_ahead < 5:
                multiplier *= 1.03
            elif 5 <= weeks_ahead <= 12:
                multiplier *= 0.92  # LCC early-bird sweet spot (5~12 weeks ahead)
            elif weeks_ahead > 18:
                multiplier *= 1.03  # Far-out standard inventory

            # 3. Coupled day & departure time slot demand factor
            if day_of_week == 3:  # Thursday morning (requires 2 vacation days -> lower demand)
                multiplier *= 0.92
            else:
                if time_category == "morning":
                    multiplier *= 0.98
                elif time_category == "afternoon":
                    multiplier *= 1.04
                elif time_category == "night":
                    multiplier *= 1.08  # High-demand post-work Friday slot

        final_price = int(dest.flight_baseline_krw * multiplier)
        floor_krw = (
            dest.flight_min_floor_krw
            if dest.flight_min_floor_krw > 0
            else round(int(dest.flight_baseline_krw * 0.80), -3)
        )
        return max(floor_krw, round(final_price, -3))

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
        fast_live: bool = False,
        custom_destinations: Optional[dict[str, DestinationMeta]] = None,
    ) -> list[FlightDeal]:
        """Search flight deals across destinations and weekend dates.
        `fast_live=True` limits live SSE verification to the cheapest slot per destination (on-demand UX).
        `custom_destinations` is a request-scoped mapping (e.g. CityCatalog FALLBACK city) used only when
        `dest_code` targets it; it never mutates `self.destinations` and never joins the ALL search.
        """
        slots = self.generate_weekend_slots(ref_date=ref_date, months_ahead=months_ahead)
        if dest_code:
            code = dest_code.strip().upper()
            if code in self.destinations:
                targets = [self.destinations[code]]
            elif custom_destinations and code in custom_destinations:
                targets = [custom_destinations[code]]
            else:
                # Explicitly handle invalid destination code instead of silent fallback to all
                return []
        else:
            targets = list(self.destinations.values())

        if self.live_naver and self._live_client is not None:
            target_dest_map = {t.code: t for t in targets}
            live_all = self._live_client.fetch_all_weekend_deals(
                destinations=target_dest_map,
                slots=slots,
                holiday_fn=self.get_peak_holiday_info,
                ref_date=ref_date,
                months_ahead=months_ahead,
                fast_mode=fast_live,
            )
            if live_all:
                target_codes = {t.code for t in targets}
                filtered = [
                    d for d in live_all
                    if d.destination_code in target_codes
                    and (not max_budget_flight or d.price_krw <= max_budget_flight)
                    and (not super_deals_only or d.is_super_deal)
                ]
                filtered.sort(key=lambda d: d.price_krw)
                return filtered

        deals: list[FlightDeal] = []

        for dest in targets:
            for dep, ret, dep_dow, ret_dow, nights in slots:
                seed = f"{dest.code}-{dep.isoformat()}"
                time_cat = self._pick_time_category(dep_dow, seed, dest_code=dest.code)
                is_peak, holiday_name, _ = self.get_peak_holiday_info(dep, ret)

                price = self._estimate_price(
                    dest,
                    dep,
                    dep.weekday(),
                    ret_date=ret,
                    time_category=time_cat,
                    ref_date=ref_date,
                )
                if max_budget_flight and price > max_budget_flight:
                    continue

                is_super = (not is_peak) and (price <= dest.flight_deal_threshold_krw)
                if super_deals_only and not is_super:
                    continue

                airline = self._pick_airline_for_dest(dest, seed, is_peak=is_peak)
                time_slot = format_departure_time_slot(dep_dow, time_cat)
                spread = 1.20 if is_peak else 1.14
                price_max = max(price + 30000, round(int(price * spread), -3))

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
                    departure_time_slot=time_slot,
                    time_category=time_cat,
                    price_min_krw=price,
                    price_max_krw=price_max,
                    is_peak_holiday=is_peak,
                    holiday_name=holiday_name,
                )
                deals.append(deal)

        deals.sort(key=lambda d: d.price_krw)
        return deals

    def ensure_deals_sse_verified(
        self,
        deals: list[FlightDeal],
        custom_destinations: Optional[dict[str, DestinationMeta]] = None,
    ) -> list[FlightDeal]:
        """Ensures any display-selected deals are upgraded from LIVE_PG to LIVE_SSE when live mode is active."""
        if not self.live_naver or self._live_client is None or not deals:
            return deals
        dest_map = {**custom_destinations, **self.destinations} if custom_destinations else self.destinations
        return self._live_client.ensure_deals_sse_verified(deals, dest_map)

    def invalidate_scope(self, scope_codes: set[str] | list[str] | tuple[str, ...]) -> None:
        """Scope-isolated refresh: demotes only cache entries whose destination set intersects `scope_codes`.
        `trv:r:CTS` -> {"CTS"} leaves the 10-city ALL entries fresh; `trv:r:ALL` -> curated codes leaves
        custom-city entries fresh. Pre-refresh in-flight leaders can no longer repopulate fresh caches.
        """
        from travel_brief import naver_live as nl

        scope = {c.upper() for c in scope_codes if c}
        if not scope:
            return
        live_client = self._live_client if self._live_client is not None else nl.NaverFlightLiveClient()
        cache_path = Path(live_client.cache_path)

        def _hit(key_codes) -> bool:
            return bool(scope.intersection(key_codes))

        with nl._CACHE_LOCK:
            # Epoch bump detaches pre-refresh leaders (other scopes only lose one cache write, never data).
            nl._CACHE_EPOCH += 1
            for k in [k for k in nl._MEM_CACHE if _hit(k[2])]:
                nl._STALE_BACKUP[k] = nl._MEM_CACHE.pop(k)
            for k in [k for k in nl._FAIL_CACHE if _hit(k[2])]:
                nl._FAIL_CACHE.pop(k, None)
            for k in [k for k in nl._IN_FLIGHT if _hit(k[2])]:
                nl._IN_FLIGHT.pop(k, None)
            try:
                if cache_path.exists():
                    raw = json.loads(cache_path.read_text(encoding="utf-8"))
                    entries, backup = nl._normalize_disk_payload(raw)
                    moved = False
                    for dk in list(entries):
                        parsed = nl._parse_disk_key(dk)
                        if parsed is None or _hit(parsed[2]):
                            backup[dk] = entries.pop(dk)
                            moved = True
                    if moved:
                        nl._write_disk_payload(cache_path, entries, backup)
            except Exception:
                pass

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
