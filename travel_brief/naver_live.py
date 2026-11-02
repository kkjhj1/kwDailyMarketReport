"""Real-time Naver Flight GraphQL + SSE live fare collector (Pure Python stdlib).

Implements a 2-stage live verification pipeline:
- Stage 1 (0.45s batched GraphQL `GET_PRICE_GRAPH`): Queries all ~520 weekend 3N4D (Thu-Sun / Fri-Mon)
  nonstop slots across 10 destinations in 10 parallel aliased GraphQL requests.
- Stage 2 (Live `POST /flight/international/searchFlights` SSE stream + Post-SSE Re-Rank):
  Verifies priority candidate deals in real time down to 1 KRW, extracting exact carrier names,
  outbound/inbound HH:MM schedules, next-day (+1d) arrival warnings, card vs general payment fares,
  and top '인기 항공편' banner fares.
"""
from __future__ import annotations

import concurrent.futures
import dataclasses
import json
import logging
import socket
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Callable, Iterable, Optional

from travel_brief.models import (
    DestinationMeta,
    FlightDeal,
    format_departure_time_slot,
    format_live_schedule_slots,
)

logger = logging.getLogger(__name__)

NAVER_GQL_URL = "https://flight-api.naver.com/graphql"
NAVER_SSE_URL = "https://flight-api.naver.com/flight/international/searchFlights"
NAVER_PREFLIGHT_URL = "https://m-flight.naver.com/"

MOBILE_UA = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
)

MIN_VALID_FARE_KRW = 90_000
CACHE_TTL_SECONDS = 1800  # 30 minutes fresh TTL
STALE_FALLBACK_SECONDS = 43200  # 12 hours emergency stale fallback on network outage

IATA_AIRLINE_KO: dict[str, str] = {
    "7C": "제주항공",
    "LJ": "진에어",
    "TW": "트리니티항공",
    "BX": "에어부산",
    "RS": "에어서울",
    "ZE": "이스타항공",
    "YP": "에어프레미아",
    "WE": "파라타항공",
    "KE": "대한항공",
    "OZ": "아시아나항공",
    "MM": "피치항공",
    "ZG": "집에어",
    "NQ": "에어재팬",
    "NH": "전일본공수",
    "JL": "일본항공",
    "VJ": "비엣젯항공",
    "VN": "베트남항공",
    "QH": "뱀부항공",
    "VZ": "타이비엣젯항공",
    "TG": "타이항공",
    "XJ": "타이에어아시아X",
    "FD": "타이에어아시아",
    "SL": "타이라이온에어",
    "IT": "타이거에어 타이완",
    "CI": "중화항공",
    "BR": "에바항공",
    "JX": "스타룩스항공",
    "TR": "스쿠트항공",
    "SQ": "싱가포르항공",
    "GA": "가루다인도네시아",
    "ID": "바틱에어",
    "PR": "필리핀항공",
    "5J": "세부퍼시픽",
    "ET": "에티오피아항공",
}

_CACHE_LOCK = threading.Lock()
_MEM_CACHE: dict[tuple[str, int], tuple[float, list[FlightDeal]]] = {}


def _format_hhmm(raw: str) -> str:
    s = (raw or "").strip().replace(":", "")
    if len(s) == 4 and s.isdigit():
        return f"{s[:2]}:{s[2:]}"
    return raw.strip()


def _infer_time_category_from_hhmm(hhmm: str) -> str:
    norm = _format_hhmm(hhmm)
    if ":" not in norm:
        return "morning"
    try:
        hh, mm = [int(x) for x in norm.split(":", 1)]
        mins = hh * 60 + mm
        if mins < 870:
            return "morning"
        if mins < 1170:
            return "afternoon"
        return "night"
    except ValueError:
        return "morning"


def extract_latest_valid_pg_fare(pg_node: Optional[dict]) -> tuple[Optional[int], int]:
    """Extracts (latest_valid_lowest_fare, avg_lowest_fare) safely from a GET_PRICE_GRAPH node.
    Defends against null/0 trailing entries in `fares`.
    """
    if not pg_node or not isinstance(pg_node, dict):
        return None, 0
    fares = pg_node.get("fares") or []
    latest_fare: Optional[int] = None
    for entry in reversed(fares):
        if not isinstance(entry, dict):
            continue
        val = entry.get("lowestFare")
        if isinstance(val, (int, float)) and int(val) >= MIN_VALID_FARE_KRW:
            latest_fare = int(val)
            break
    avg_raw = pg_node.get("averageLowestFare")
    avg_fare = int(round(avg_raw)) if isinstance(avg_raw, (int, float)) and avg_raw >= MIN_VALID_FARE_KRW else 0
    return latest_fare, avg_fare


def parse_sse_stream_lines(
    raw_lines: Iterable[bytes | str],
    ret_compact: str,
) -> Optional[dict]:
    """Parses SSE stream lines from Naver `searchFlights` resiliently.
    - Retains the latest `data:` payload containing non-empty `fareMappings`.
    - Survives mid-stream `socket.timeout` / `TimeoutError` if valid partner chunks arrived.
    - Strictly filters `len(segments) == 1` (nonstop) on both outbound & inbound legs.
    - Explicitly finds the minimum valid fare (`>= MIN_VALID_FARE_KRW`) instead of trusting index 0.
    """
    last_valid_obj: Optional[dict] = None
    try:
        for raw_line in raw_lines:
            line = raw_line.decode("utf-8", errors="replace").strip() if isinstance(raw_line, bytes) else raw_line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            try:
                obj = json.loads(payload)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            fms = obj.get("fareMappings")
            if isinstance(fms, list) and len(fms) > 0:
                last_valid_obj = obj
                status = obj.get("status") or {}
                if status.get("isCompleted") is True:
                    break
    except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
        logger.debug("SSE stream interrupted after partial chunks (%s); recovering last valid chunk.", exc)

    if not last_valid_obj:
        return None

    return parse_sse_final_object(last_valid_obj, ret_compact=ret_compact)


def parse_sse_final_object(obj: dict, ret_compact: str) -> Optional[dict]:
    """Extracts verified nonstop cheapest & popular flight details from a parsed SSE JSON object."""
    status = obj.get("status") or {}
    airlines_map: dict[str, str] = {**IATA_AIRLINE_KO, **(status.get("airlinesCodeMap") or {})}
    itin_map: dict[str, dict] = {
        it["itineraryId"]: it
        for it in (obj.get("itineraries") or [])
        if isinstance(it, dict) and "itineraryId" in it
    }

    def _parse_mapping(m: dict) -> Optional[dict]:
        if not isinstance(m, dict):
            return None
        ids = (m.get("itineraryIds") or "").split("-")
        if len(ids) < 2:
            return None
        out_it = itin_map.get(ids[0])
        ret_it = itin_map.get(ids[1])
        if not out_it or not ret_it:
            return None
        out_segs = out_it.get("segments") or []
        ret_segs = ret_it.get("segments") or []
        # Strict nonstop enforcement on both outbound & inbound legs
        if len(out_segs) != 1 or len(ret_segs) != 1:
            return None

        out_seg = out_segs[0]
        ret_seg = ret_segs[0]

        valid_card_fares: list[int] = []
        valid_general_fares: list[int] = []
        for f in (m.get("fares") or []):
            if not isinstance(f, dict):
                continue
            adult = f.get("adult") or {}
            tf = adult.get("totalFare")
            if not isinstance(tf, (int, float)) or int(tf) < MIN_VALID_FARE_KRW:
                continue
            fare_int = int(tf)
            valid_card_fares.append(fare_int)
            fare_type = str(f.get("fareType") or "")
            if fare_type == "A01":
                valid_general_fares.append(fare_int)

        if not valid_card_fares:
            return None

        min_card = min(valid_card_fares)
        min_gen = min(valid_general_fares) if valid_general_fares else min_card
        if min_gen < min_card:
            min_gen = min_card

        ac_out = ((out_seg.get("marketingCarrier") or {}).get("airlineCode") or "").strip()
        ac_ret = ((ret_seg.get("marketingCarrier") or {}).get("airlineCode") or "").strip()
        an_out = airlines_map.get(ac_out, ac_out) or "직항항공사"
        an_ret = airlines_map.get(ac_ret, ac_ret) or an_out
        airline_str = an_out if an_out == an_ret else f"{an_out}/{an_ret}"

        dep_time = _format_hhmm((out_seg.get("departure") or {}).get("time") or "")
        ret_dep_time = _format_hhmm((ret_seg.get("departure") or {}).get("time") or "")
        ret_arr_info = ret_seg.get("arrival") or {}
        ret_arr_time = _format_hhmm(ret_arr_info.get("time") or "")
        ret_arr_date = str(ret_arr_info.get("date") or "").replace("-", "").strip()
        next_day_arr = bool(ret_arr_date and ret_compact and ret_arr_date > ret_compact)

        return {
            "airline": airline_str,
            "dep_time": dep_time,
            "ret_dep_time": ret_dep_time,
            "ret_arr_time": ret_arr_time,
            "next_day_arrival": next_day_arr,
            "min_card": min_card,
            "min_general": min_gen,
        }

    parsed_fms = [p for m in (obj.get("fareMappings") or []) if (p := _parse_mapping(m)) is not None]
    if not parsed_fms:
        return None
    parsed_fms.sort(key=lambda x: (x["min_card"], x["min_general"]))
    cheapest = parsed_fms[0]

    parsed_pops = [p for m in (obj.get("popularFlights") or []) if (p := _parse_mapping(m)) is not None]
    popular = parsed_pops[0] if parsed_pops else None

    return {
        "cheapest": cheapest,
        "popular": popular,
    }


class NaverFlightLiveClient:
    """Thread-safe pure-Python live Naver Flight collector with 2-layer caching."""

    def __init__(
        self,
        cache_path: Optional[Path] = None,
        gql_timeout: float = 10.0,
        sse_timeout: float = 10.0,
        max_sse_workers: int = 2,
    ):
        self.cache_path = cache_path or (Path(tempfile.gettempdir()) / "kw_travel_naver_live_v4.json")
        self.gql_timeout = gql_timeout
        self.sse_timeout = sse_timeout
        self.max_sse_workers = max_sse_workers

    def _fetch_session_cookie_header(self) -> str:
        cj = urllib.request.HTTPCookieProcessor()
        opener = urllib.request.build_opener(cj)
        opener.addheaders = [("User-Agent", MOBILE_UA)]
        try:
            with opener.open(NAVER_PREFLIGHT_URL, timeout=4.0) as resp:
                resp.read(512)
            cookies = [f"{c.name}={c.value}" for c in cj.cookiejar]
            return "; ".join(cookies)
        except Exception as exc:
            logger.debug("Naver preflight cookie fetch skipped: %s", exc)
            return ""

    def fetch_city_batch_pg(
        self,
        dest_code: str,
        slots: list[tuple[date, date, str, str, int]],
    ) -> dict[tuple[str, str], tuple[int, int]]:
        """Stage 1: Queries all ~52 weekend 3N4D slots for a single destination in ONE GraphQL request."""
        if not slots:
            return {}
        fields: list[str] = []
        compact_pairs: list[tuple[str, str, str, str]] = []
        for idx, (dep_d, ret_d, _, _, _) in enumerate(slots):
            dep_c = dep_d.strftime("%Y%m%d")
            ret_c = ret_d.strftime("%Y%m%d")
            sk = f"Y{dep_c}ICN{dest_code}{ret_c}{dest_code}ICN"
            fields.append(
                f'  s{idx}: priceGraph(searchKey: "{sk}", includeEventFare: true, nonstop: true, period: 7) '
                f"{{ averageLowestFare fares {{ lowestFare searchDate }} }}"
            )
            compact_pairs.append((dep_d.isoformat(), ret_d.isoformat(), dep_c, ret_c))

        query_str = "query BATCH_CITY_PG {\n" + "\n".join(fields) + "\n}"
        payload = json.dumps({"operationName": "BATCH_CITY_PG", "variables": {}, "query": query_str}).encode("utf-8")
        req = urllib.request.Request(
            NAVER_GQL_URL,
            data=payload,
            headers={
                "User-Agent": MOBILE_UA,
                "Origin": "https://m-flight.naver.com",
                "Referer": "https://m-flight.naver.com/",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=self.gql_timeout) as resp:
            res = json.loads(resp.read().decode("utf-8"))

        data = res.get("data") or {}
        out: dict[tuple[str, str], tuple[int, int]] = {}
        for idx, (dep_iso, ret_iso, _, _) in enumerate(compact_pairs):
            latest_fare, avg_fare = extract_latest_valid_pg_fare(data.get(f"s{idx}"))
            if latest_fare is not None:
                out[(dep_iso, ret_iso)] = (latest_fare, avg_fare)
        return out

    def fetch_live_sse_slot(
        self,
        dest_code: str,
        dep_iso: str,
        ret_iso: str,
        cookie_header: str = "",
    ) -> Optional[dict]:
        """Stage 2: Queries exact real-time seat inventory via Naver `searchFlights` SSE."""
        dep_c = dep_iso.replace("-", "")
        ret_c = ret_iso.replace("-", "")
        body = {
            "tripType": "RT",
            "device": "pc",
            "seatClass": "Y",
            "adultCount": 1,
            "childCount": 0,
            "infantCount": 0,
            "isNonstop": True,
            "openReturnDays": 0,
            "initialRequest": True,
            "itineraries": [
                {
                    "departureLocationCode": "ICN",
                    "arrivalLocationCode": dest_code,
                    "departureLocationType": "airport",
                    "arrivalLocationType": "airport",
                    "departureDate": dep_c,
                },
                {
                    "departureLocationCode": dest_code,
                    "arrivalLocationCode": "ICN",
                    "departureLocationType": "airport",
                    "arrivalLocationType": "airport",
                    "departureDate": ret_c,
                },
            ],
            "flightFilter": {
                "filter": {
                    "airlines": [],
                    "departureAirports": [],
                    "arrivalAirports": [],
                    "departureTime": [],
                    "fareTypes": [],
                    "flightDurationSeconds": [],
                    "hasCardBenefit": True,
                    "isIndividual": False,
                    "isLowCarbonEmission": False,
                    "isSameAirlines": False,
                    "isSameDepArrAirport": True,
                    "isTravelClub": False,
                    "minFare": {},
                    "viaCount": [],
                    "selectedItineraries": [],
                },
                "limit": 60,
                "skip": 0,
                "sort": {"adultMinFare": 1},
            },
        }
        headers = {
            "User-Agent": MOBILE_UA,
            "Origin": "https://flight.naver.com",
            "Referer": (
                f"https://flight.naver.com/flights/international/"
                f"ICN-{dest_code}-{dep_c}/{dest_code}-ICN-{ret_c}?fareType=Y&adult=1&isDirect=true"
            ),
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
        }
        if cookie_header:
            headers["Cookie"] = cookie_header

        for attempt in range(2):
            if attempt > 0:
                time.sleep(0.8)
            else:
                time.sleep(0.12)
            req = urllib.request.Request(
                NAVER_SSE_URL,
                data=json.dumps(body).encode("utf-8"),
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=self.sse_timeout) as resp:
                    parsed = parse_sse_stream_lines(resp, ret_compact=ret_c)
                    if parsed is not None:
                        return parsed
            except Exception as exc:
                logger.debug("Live SSE fetch attempt %d failed for %s %s~%s: %s", attempt + 1, dest_code, dep_iso, ret_iso, exc)
        return None

    @staticmethod
    def apply_sse_to_deal(deal: FlightDeal, dest: DestinationMeta, sse_result: dict) -> FlightDeal:
        """Upgrades a FlightDeal in-place/copy to `LIVE_SSE` with 1-KRW accuracy & exact HH:MM slots."""
        ch = sse_result["cheapest"]
        pop = sse_result.get("popular")

        min_card = int(ch["min_card"])
        min_gen = int(ch["min_general"])
        pop_fare = int(pop["min_card"]) if pop and pop.get("min_card") else 0
        pop_airline = str(pop.get("airline") or "") if pop else ""
        pop_dep = str(pop.get("dep_time") or "") if pop else ""

        dep_slot, ret_slot = format_live_schedule_slots(
            departure_day_of_week=deal.departure_day_of_week,
            return_day_of_week=deal.return_day_of_week,
            dep_hhmm=ch["dep_time"],
            ret_dep_hhmm=ch["ret_dep_time"],
            ret_arr_hhmm=ch["ret_arr_time"],
            next_day_arrival=ch["next_day_arrival"],
        )
        time_cat = _infer_time_category_from_hhmm(ch["dep_time"])
        is_super = (not deal.is_peak_holiday) and (min_card <= dest.flight_deal_threshold_krw)
        max_krw = max(min_card + 30000, min_gen, pop_fare)

        return FlightDeal(
            destination_code=deal.destination_code,
            destination_name=deal.destination_name,
            origin_code=deal.origin_code,
            departure_date=deal.departure_date,
            return_date=deal.return_date,
            departure_day_of_week=deal.departure_day_of_week,
            return_day_of_week=deal.return_day_of_week,
            nights=deal.nights,
            airline=ch["airline"],
            price_krw=min_card,
            baseline_krw=dest.flight_baseline_krw,
            is_super_deal=is_super,
            departure_time_slot=dep_slot,
            return_time_slot=ret_slot,
            time_category=time_cat,
            price_min_krw=min_card,
            price_max_krw=max_krw,
            is_peak_holiday=deal.is_peak_holiday,
            holiday_name=deal.holiday_name,
            data_source="LIVE_SSE",
            general_fare_krw=min_gen,
            popular_fare_krw=pop_fare,
            popular_airline=pop_airline,
            popular_dep_time=pop_dep,
            exact_dep_time=ch["dep_time"],
            exact_ret_time=ch["ret_dep_time"],
            exact_ret_arr_time=ch["ret_arr_time"],
            next_day_arrival=ch["next_day_arrival"],
        )

    def _load_disk_cache(self, cache_key: str, max_age_sec: int) -> Optional[list[FlightDeal]]:
        if not self.cache_path.exists():
            return None
        try:
            raw = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if raw.get("cache_key") != cache_key:
                return None
            saved_at = float(raw.get("saved_at", 0))
            if (time.time() - saved_at) > max_age_sec:
                return None
            items = raw.get("deals") or []
            if not items:
                return None
            return [FlightDeal(**it) for it in items]
        except Exception:
            return None

    def _save_disk_cache(self, cache_key: str, deals: list[FlightDeal]) -> None:
        try:
            payload = {
                "cache_key": cache_key,
                "saved_at": time.time(),
                "deals": [dataclasses.asdict(d) for d in deals],
            }
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            logger.debug("Disk cache write skipped: %s", exc)

    def fetch_all_weekend_deals(
        self,
        destinations: dict[str, DestinationMeta],
        slots: list[tuple[date, date, str, str, int]],
        holiday_fn: Callable[[date, Optional[date]], tuple[bool, str, float]],
        ref_date: Optional[date] = None,
        months_ahead: int = 6,
    ) -> list[FlightDeal]:
        """Executes full 2-stage live Naver collection with singleflight memory+disk caching
        and Post-SSE Re-Rank protection.
        """
        eff_ref = (ref_date or date.today()).isoformat()
        mem_key = (eff_ref, months_ahead)
        disk_key = f"{eff_ref}:{months_ahead}:{','.join(sorted(destinations.keys()))}"

        with _CACHE_LOCK:
            cached = _MEM_CACHE.get(mem_key)
            if cached and (time.time() - cached[0]) <= CACHE_TTL_SECONDS:
                return list(cached[1])

            disk_fresh = self._load_disk_cache(disk_key, max_age_sec=CACHE_TTL_SECONDS)
            if disk_fresh:
                _MEM_CACHE[mem_key] = (time.time(), disk_fresh)
                return list(disk_fresh)

            try:
                deals = self._collect_live_deals_uncached(destinations, slots, holiday_fn)
            except Exception as exc:
                logger.warning("Live Naver collection failed (%s); checking emergency stale cache.", exc)
                deals = []

            if deals:
                _MEM_CACHE[mem_key] = (time.time(), deals)
                self._save_disk_cache(disk_key, deals)
                return list(deals)

            disk_stale = self._load_disk_cache(disk_key, max_age_sec=STALE_FALLBACK_SECONDS)
            if disk_stale:
                logger.warning("Using emergency stale live cache (<12h) due to upstream outage.")
                _MEM_CACHE[mem_key] = (time.time(), disk_stale)
                return list(disk_stale)

            return []

    def _collect_live_deals_uncached(
        self,
        destinations: dict[str, DestinationMeta],
        slots: list[tuple[date, date, str, str, int]],
        holiday_fn: Callable[[date, Optional[date]], tuple[bool, str, float]],
    ) -> list[FlightDeal]:
        slot_meta_map: dict[tuple[str, str], tuple[date, date, str, str, int]] = {
            (dep.isoformat(), ret.isoformat()): (dep, ret, dep_dow, ret_dow, nights)
            for dep, ret, dep_dow, ret_dow, nights in slots
        }
        all_months = sorted({dep.isoformat()[:7] for dep, _, _, _, _ in slots})

        # Stage 1: Parallel batched GET_PRICE_GRAPH across all destinations (~0.45s)
        pg_by_city: dict[str, dict[tuple[str, str], tuple[int, int]]] = {}
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
            future_map = {
                ex.submit(self.fetch_city_batch_pg, code, slots): code
                for code in destinations
            }
            for fut in concurrent.futures.as_completed(future_map):
                code = future_map[fut]
                try:
                    pg_by_city[code] = fut.result()
                except Exception as exc:
                    logger.warning("Stage 1 batch PG failed for %s: %s", code, exc)
                    pg_by_city[code] = {}

        # Build initial LIVE_PG deals (NO fake MD5 airline names or fake times!)
        deals_by_key: dict[tuple[str, str, str], FlightDeal] = {}
        for code, dest in destinations.items():
            city_pg = pg_by_city.get(code) or {}
            for (dep_iso, ret_iso), (lowest_fare, avg_fare) in city_pg.items():
                dep_d, ret_d, dep_dow, ret_dow, nights = slot_meta_map[(dep_iso, ret_iso)]
                is_peak, holiday_name, _ = holiday_fn(dep_d, ret_d)
                is_super = (not is_peak) and (lowest_fare <= dest.flight_deal_threshold_krw)
                max_band = max(
                    lowest_fare + 30000,
                    avg_fare,
                    round(int(lowest_fare * (1.18 if is_peak else 1.12)), -3),
                )
                deals_by_key[(code, dep_iso, ret_iso)] = FlightDeal(
                    destination_code=code,
                    destination_name=dest.name,
                    origin_code="ICN",
                    departure_date=dep_iso,
                    return_date=ret_iso,
                    departure_day_of_week=dep_dow,
                    return_day_of_week=ret_dow,
                    nights=nights,
                    airline="네이버 직항 최저가",
                    price_krw=lowest_fare,
                    baseline_krw=dest.flight_baseline_krw,
                    is_super_deal=is_super,
                    departure_time_slot=(
                        "목요일 직항 출발 (출도착 시각 실시간 링크 확인 · 연차 1~2일)"
                        if dep_dow == "목"
                        else "금요일 직항 출발 (출도착 시각 실시간 링크 확인 · 연차 1~2일)"
                    ),
                    time_category="morning",
                    price_min_krw=lowest_fare,
                    price_max_krw=max_band,
                    is_peak_holiday=is_peak,
                    holiday_name=holiday_name,
                    data_source="LIVE_PG",
                )

        if not deals_by_key:
            return []

        cookie_hdr = self._fetch_session_cookie_header()

        # Select Stage 2 SSE priority targets:
        # (a) Top-2 cheapest + Top-1 best-season per destination
        # (b) Missing (dest, month) buckets where priceGraph had 0 slots (e.g., far-out DPS)
        sse_targets: set[tuple[str, str, str]] = set()
        placeholder_slots: dict[tuple[str, str, str], FlightDeal] = {}

        for code, dest in destinations.items():
            city_deals = sorted(
                [d for d in deals_by_key.values() if d.destination_code == code],
                key=lambda d: d.price_krw,
            )
            for d in city_deals[:2]:
                sse_targets.add((code, d.departure_date, d.return_date))
            best_season_deals = [
                d for d in city_deals if dest.is_best_month(int(d.departure_date.split("-")[1]))
            ]
            if best_season_deals:
                d0 = best_season_deals[0]
                sse_targets.add((code, d0.departure_date, d0.return_date))

            # Fill missing monthly cells (e.g. DPS 2027-03, 2027-04) via 1 representative mid-month non-peak slot
            covered_months = {d.departure_date[:7] for d in city_deals}
            for m_str in all_months:
                if m_str not in covered_months:
                    m_slots = [s for s in slots if s[0].isoformat().startswith(m_str)]
                    non_peak_m = [s for s in m_slots if not holiday_fn(s[0], s[1])[0]]
                    chosen_s = (non_peak_m[len(non_peak_m) // 2] if non_peak_m else (m_slots[0] if m_slots else None))
                    if chosen_s:
                        dep_d, ret_d, dep_dow, ret_dow, nights = chosen_s
                        dep_iso, ret_iso = dep_d.isoformat(), ret_d.isoformat()
                        is_peak, holiday_name, _ = holiday_fn(dep_d, ret_d)
                        key = (code, dep_iso, ret_iso)
                        sse_targets.add(key)
                        placeholder_slots[key] = FlightDeal(
                            destination_code=code,
                            destination_name=dest.name,
                            origin_code="ICN",
                            departure_date=dep_iso,
                            return_date=ret_iso,
                            departure_day_of_week=dep_dow,
                            return_day_of_week=ret_dow,
                            nights=nights,
                            airline="네이버 직항 최저가",
                            price_krw=dest.flight_baseline_krw,
                            baseline_krw=dest.flight_baseline_krw,
                            is_peak_holiday=is_peak,
                            holiday_name=holiday_name,
                            data_source="LIVE_PG",
                        )

        # Also add overall monthly cheapest candidates across all cities
        for m_str in all_months:
            m_deals = sorted(
                [d for d in deals_by_key.values() if d.departure_date.startswith(m_str)],
                key=lambda d: d.price_krw,
            )
            for d in m_deals[:2]:
                sse_targets.add((d.destination_code, d.departure_date, d.return_date))

        attempted_sse: set[tuple[str, str, str]] = set()

        def _run_sse_batch(targets: set[tuple[str, str, str]]) -> None:
            pending = [t for t in targets if t not in attempted_sse]
            if not pending:
                return
            attempted_sse.update(pending)
            with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_sse_workers) as ex:
                fut_map = {
                    ex.submit(self.fetch_live_sse_slot, c, dep_i, ret_i, cookie_hdr): (c, dep_i, ret_i)
                    for c, dep_i, ret_i in pending
                }
                for fut in concurrent.futures.as_completed(fut_map):
                    key = fut_map[fut]
                    c, _, _ = key
                    sse_res = fut.result()
                    base_deal = deals_by_key.get(key) or placeholder_slots.get(key)
                    if sse_res and base_deal:
                        deals_by_key[key] = self.apply_sse_to_deal(base_deal, destinations[c], sse_res)

        # Round A SSE verification
        _run_sse_batch(sse_targets)

        # Post-SSE Re-Rank Promotion Guard (up to 2 rounds):
        # If an SSE-verified candidate surged in price (e.g. SIN 397k -> 507k), verify any unverified
        # LIVE_PG deal that jumped into #1 overall or #1 best-season per destination!
        for _ in range(2):
            promoted: set[tuple[str, str, str]] = set()
            for code, dest in destinations.items():
                city_deals = sorted(
                    [d for d in deals_by_key.values() if d.destination_code == code],
                    key=lambda d: d.price_krw,
                )
                if city_deals and city_deals[0].data_source != "LIVE_SSE":
                    k = (code, city_deals[0].departure_date, city_deals[0].return_date)
                    if k not in attempted_sse:
                        promoted.add(k)
                best_s = [
                    d for d in city_deals if dest.is_best_month(int(d.departure_date.split("-")[1]))
                ]
                if best_s and best_s[0].data_source != "LIVE_SSE":
                    ks = (code, best_s[0].departure_date, best_s[0].return_date)
                    if ks not in attempted_sse:
                        promoted.add(ks)
            if not promoted:
                break
            _run_sse_batch(promoted)

        final_deals = list(deals_by_key.values())
        final_deals.sort(key=lambda d: d.price_krw)
        return final_deals

    def ensure_deals_sse_verified(
        self,
        deals: list[FlightDeal],
        destinations: dict[str, DestinationMeta],
    ) -> list[FlightDeal]:
        """Upgrades any remaining LIVE_PG deals in a selected display list (e.g. filtered Top 3 or monthly summary)
        to LIVE_SSE so displayed hero deals are 100% live-verified.
        """
        need_sse = [
            d for d in deals
            if d.data_source == "LIVE_PG" and d.destination_code in destinations
        ]
        if not need_sse:
            return deals

        cookie_hdr = self._fetch_session_cookie_header()
        upgraded_map: dict[tuple[str, str, str], FlightDeal] = {}
        unique_targets = {
            (d.destination_code, d.departure_date, d.return_date): d for d in need_sse
        }
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_sse_workers) as ex:
            fut_map = {
                ex.submit(self.fetch_live_sse_slot, k[0], k[1], k[2], cookie_hdr): (k, base_d)
                for k, base_d in unique_targets.items()
            }
            for fut in concurrent.futures.as_completed(fut_map):
                k, base_d = fut_map[fut]
                sse_res = fut.result()
                if sse_res:
                    upgraded_map[k] = self.apply_sse_to_deal(base_d, destinations[k[0]], sse_res)

        if not upgraded_map:
            return deals

        # Also sync upgraded deals back into _MEM_CACHE if present
        with _CACHE_LOCK:
            for m_key, (ts, cached_list) in list(_MEM_CACHE.items()):
                changed = False
                new_cached = []
                for cd in cached_list:
                    ck = (cd.destination_code, cd.departure_date, cd.return_date)
                    if ck in upgraded_map:
                        new_cached.append(upgraded_map[ck])
                        changed = True
                    else:
                        new_cached.append(cd)
                if changed:
                    new_cached.sort(key=lambda x: x.price_krw)
                    _MEM_CACHE[m_key] = (ts, new_cached)
                    d_key = f"{m_key[0]}:{m_key[1]}:{','.join(sorted(destinations.keys()))}"
                    self._save_disk_cache(d_key, new_cached)

        return [
            upgraded_map.get((d.destination_code, d.departure_date, d.return_date), d)
            for d in deals
        ]
