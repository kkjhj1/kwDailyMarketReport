"""Travel price watch checker (cron tick): per-city / per-country target price alerts.

Invariants:
1. Singleton tick lease (`travel_watch_tick_leases`) — a concurrent manager/cron tick exits as `skipped_locked`.
2. Expired IN_FLIGHT claims are quarantined to UNKNOWN at tick start and never retried ([PROD-LIVE]).
3. Subscription SSOT: active watch AND active TRAVEL recipient AND ACTIVE `travel_bot` subscription.
4. Only `LIVE_SSE` + `FRESH` quotes may trigger a push (LIVE_PG / STALE / estimates are dropped and counted).
5. Ledger (`travel_watch_deliveries`) is the only delivery state; max 1 alert per watch per tick, 7-day cooldown.
6. `dry_run` is read-only: no lease, no ledger writes, no observation updates, no Telegram calls.
7. FIXED watches whose departure date already passed (KST) are deactivated BEFORE any fetch.
8. DAILY briefings are claimed only inside the KST morning window (06:00~10:59, the 07:30 tick). A missed
   morning is never compensated at 12:30 / 18:30 / startup ([PROD-LIVE]); later ticks only evaluate DROP.
   A watch never gets a DAILY briefing and a DROP alert in the same tick.
9. FLEX watches keep the legacy ledger key + 7-day same-slot cooldown; FIXED watches use the V2 key and may
   re-alert on the exact same slot only after a further >= 10,000 KRW drop (`allow_same_slot_drop`).
"""
from __future__ import annotations

import html
import logging
import os
import re
import socket
import time
import uuid
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from travel_brief.city_catalog import CityCatalog, get_watch_country_meta, is_country_watch_code
from travel_brief.flight_engine import (
    FSC_CARRIERS,
    OFFICE_MAX_LEAVE_DAYS,
    OFFICE_MAX_NIGHTS,
    OFFICE_MIN_NIGHTS,
    OFFICE_RET_MIN_ARR_MINUTES,
    WEEKDAY_OPTION_PATTERNS,
    FlightEngine,
    _hhmm_to_minutes,
    add_months,
    calc_required_leave_days,
    normalize_weekday_option,
)
from travel_brief.models import BudgetBreakdown, FlightDeal, SSEFlightCandidate, round_man
from travel_brief.storage.db import (
    TravelBriefDatabase,
    WATCH_MIN_REDROP_KRW,
    validate_time_pair,
    make_watch_daily_key,
    make_watch_delivery_key,
    make_watch_drop_key_v2,
    watch_cooldown_block_reason,
)

logger = logging.getLogger(__name__)

KST = timezone(timedelta(hours=9))
TICK_LEASE_NAME = "travel_watch_check"
TICK_LEASE_TTL_SECONDS = 900
DELIVERY_LEASE_TTL_SECONDS = 120
SSE_VERIFY_PER_CITY = 3
COUNTRY_SSE_VERIFY_PER_AIRPORT = 1  # country fan-out: only the PG-cheapest slot per member airport
WATCH_TICK_BUDGET_SECONDS = 600.0  # whole-tick fetch budget (lease TTL is 900s)
DAILY_WINDOW_START_HOUR = 6
DAILY_WINDOW_END_HOUR = 10
DAILY_PRICE_CEILING_KRW = 5_000_000  # DAILY briefs need the verified best fare even above the target
CARD_MAX_VISIBLE = 1800
DAY_PATTERN_LABELS = {"ANY": "전체 요일", "FRI_MON": "금~월", "THU_SUN": "목~일"}
_DOW = ["월", "화", "수", "목", "금", "토", "일"]
_TAG_RE = re.compile(r"<[^>]+>")

# (dest_code, custom_destinations | None, max_target_krw[, WatchFetchSpec]) -> deals
QuoteFetcher = Callable[..., list[FlightDeal]]
# (dest_code, custom_destinations | None, dep_iso, ret_iso, include_neighbors) -> verified deals
FixedQuoteFetcher = Callable[[str, Optional[dict], str, str, bool], list[FlightDeal]]
# (chat_id, html_text, reply_markup) -> (status, provider_msg_id, error_detail)
WatchSender = Callable[[str, str, dict], tuple[str, Any, Optional[str]]]


@dataclass(frozen=True)
class WatchFetchSpec:
    """Union search spec for every FLEX watch sharing one airport in a tick."""
    horizon_months: int = 6
    weekday_option: str = "WEEKEND"
    sse_cap: int = SSE_VERIFY_PER_CITY
    # Time-window watches: SSE-verify EVERY fresh price-graph slot under the ceiling (no `sse_cap` truncation).
    needs_time_coverage: bool = False
    price_ceiling_krw: Optional[int] = None


class WatchDeals(list):
    """Fetch result list carrying coverage metadata. `partial_coverage=True` means a time-coverage fetch could
    not SSE-verify every eligible slot (deadline / failure) -> time-filtered alerts are suppressed ([DATA-01])."""
    partial_coverage: bool = False


def matches_time_window(hhmm: str, from_min: Optional[int], to_min: Optional[int]) -> bool:
    """No filter (either bound None) -> True. Strict `HH:MM`; missing/malformed -> False.
    `from <= to`: inclusive range; `from > to`: overnight wrap (`m >= from or m <= to`)."""
    if from_min is None or to_min is None:
        return True
    m = _hhmm_to_minutes(hhmm or "")
    if m is None:
        return False
    f, t = int(from_min), int(to_min)
    if f <= t:
        return f <= m <= t
    return m >= f or m <= t


def watch_time_filters(watch: dict[str, Any]) -> tuple[Optional[int], Optional[int], Optional[int], Optional[int]]:
    """(dep_from, dep_to, arr_from, arr_to); an invalid / half-set stored pair is ignored (treated as no filter)."""
    def _pair(f_key: str, t_key: str) -> tuple[Optional[int], Optional[int]]:
        try:
            return validate_time_pair(watch.get(f_key), watch.get(t_key))
        except (TypeError, ValueError):
            return None, None

    dep_f, dep_t = _pair("dep_time_from_min", "dep_time_to_min")
    arr_f, arr_t = _pair("ret_arr_from_min", "ret_arr_to_min")
    return dep_f, dep_t, arr_f, arr_t


def has_time_filter(watch: dict[str, Any]) -> bool:
    dep_f, _dt, arr_f, _at = watch_time_filters(watch)
    return dep_f is not None or arr_f is not None


def format_minutes(m: int) -> str:
    return f"{int(m) // 60:02d}:{int(m) % 60:02d}"


def time_window_label(watch: dict[str, Any]) -> str:
    """`출국 09:00~12:00 · 귀국 인천도착 18:00~23:59` (empty when no filter)."""
    dep_f, dep_t, arr_f, arr_t = watch_time_filters(watch)
    parts = []
    if dep_f is not None:
        parts.append(f"출국 {format_minutes(dep_f)}~{format_minutes(dep_t)}")
    if arr_f is not None:
        parts.append(f"귀국 인천도착 {format_minutes(arr_f)}~{format_minutes(arr_t)}")
    return " · ".join(parts)


_MATCH, _NO, _UNKNOWN = "MATCH", "NO", "UNKNOWN"


def _candidate_status(
    cand: SSEFlightCandidate,
    filters: tuple[Optional[int], Optional[int], Optional[int], Optional[int]],
    office: bool,
    dep_day: Optional[date],
    ret_day: Optional[date],
    leave_days: Optional[float] = None,
) -> str:
    """MATCH / NO (verified mismatch) / UNKNOWN (a needed time or arrival date is missing)."""
    dep_f, dep_t, arr_f, arr_t = filters
    statuses: list[str] = []
    dep_m = _hhmm_to_minutes(cand.dep_time or "")
    arr_m = _hhmm_to_minutes(cand.ret_arr_time or "")
    if dep_f is not None:
        statuses.append(_UNKNOWN if dep_m is None else (_MATCH if matches_time_window(cand.dep_time, dep_f, dep_t) else _NO))
    if arr_f is not None:
        if arr_m is None or cand.next_day_arrival is None:
            statuses.append(_UNKNOWN)
        else:
            statuses.append(_MATCH if matches_time_window(cand.ret_arr_time, arr_f, arr_t) else _NO)
    if office:
        if dep_m is None or arr_m is None or cand.next_day_arrival is None or dep_day is None or ret_day is None:
            statuses.append(_UNKNOWN)
        elif cand.next_day_arrival or arr_m < OFFICE_RET_MIN_ARR_MINUTES:
            statuses.append(_NO)
        else:
            leave = leave_days if leave_days is not None else calc_required_leave_days(dep_day, ret_day, cand.dep_time)
            statuses.append(_MATCH if float(leave) <= OFFICE_MAX_LEAVE_DAYS else _NO)
    if _NO in statuses:
        return _NO
    if _UNKNOWN in statuses:
        return _UNKNOWN
    return _MATCH


def resolve_time_window_deal(
    deal: FlightDeal,
    filters: tuple[Optional[int], Optional[int], Optional[int], Optional[int]],
    office: bool = False,
) -> tuple[Optional[FlightDeal], Optional[int]]:
    """Primary (cheapest matching itinerary) of one verified slot + the cheapest price whose match is UNKNOWN.
    Returns (effective_deal | None, unknown_floor_krw | None). Only `LIVE_SSE` + `FRESH` deals are evaluated.
    Without `sse_candidates` the deal's own exact times are the only (cheapest) itinerary known: if it does not
    verifiably match, pricier itineraries of the slot are unknown -> the slot price becomes the unknown floor."""
    if getattr(deal, "data_source", "") != "LIVE_SSE" or getattr(deal, "freshness", "FRESH") != "FRESH":
        return None, None
    dep_day, ret_day = _parse_day(deal.departure_date), _parse_day(deal.return_date)
    cands: list[tuple[SSEFlightCandidate, Optional[float]]] = [(c, None) for c in (deal.sse_candidates or ())]
    fallback = not cands
    if fallback:
        cands.append((SSEFlightCandidate(
            airline=deal.airline or "", dep_time=deal.exact_dep_time or "", ret_dep_time=deal.exact_ret_time or "",
            ret_arr_time=deal.exact_ret_arr_time or "", next_day_arrival=deal.next_day_arrival,
            min_card=int(deal.price_krw or 0), min_general=int(deal.general_fare_krw or 0),
        ), None))
        if office and int(getattr(deal, "office_fare_krw", 0) or 0) > 0:
            cands.append((SSEFlightCandidate(
                airline=deal.office_airline or deal.airline or "", dep_time=deal.office_dep_time or "",
                ret_dep_time="", ret_arr_time=deal.office_ret_arr_time or "", next_day_arrival=False,
                min_card=int(deal.office_fare_krw), min_general=int(deal.office_fare_krw),
            ), deal.office_leave_days))
    best: Optional[SSEFlightCandidate] = None
    unknown_floor: Optional[int] = None
    for cand, leave in sorted(cands, key=lambda x: (x[0].min_card, x[0].min_general)):
        if cand.min_card <= 0:
            continue
        status = _candidate_status(cand, filters, office, dep_day, ret_day, leave)
        if status == _MATCH:
            if best is None or cand.min_card < best.min_card:
                best = cand
        elif status == _UNKNOWN or fallback:
            unknown_floor = cand.min_card if unknown_floor is None else min(unknown_floor, cand.min_card)
    if fallback and best is None:
        # Only the cheapest itinerary is known and it does not match: the slot's in-window fare is unknown.
        unknown_floor = int(deal.price_krw or 0) or unknown_floor
    if best is None:
        return None, unknown_floor
    eff = replace(
        deal,
        price_krw=best.min_card,
        price_min_krw=best.min_card,
        general_fare_krw=max(best.min_general, best.min_card),
        airline=best.airline or deal.airline,
        exact_dep_time=best.dep_time,
        exact_ret_time=best.ret_dep_time,
        exact_ret_arr_time=best.ret_arr_time,
        next_day_arrival=best.next_day_arrival,
        candidate_fingerprint=best.fingerprint,
    )
    return eff, unknown_floor


@dataclass
class WatchTickResult:
    skipped_locked: bool = False
    dry_run: bool = False
    subscriptions_checked: bool = False
    expired_in_flight: int = 0
    expired_fixed_count: int = 0
    eligible_watch_count: int = 0
    city_count: int = 0
    fetch_error_count: int = 0
    deadline_skipped_count: int = 0
    fixed_unavailable_count: int = 0
    candidate_count: int = 0
    sse_fresh_count: int = 0
    dropped_pg_count: int = 0
    dropped_stale_count: int = 0
    dropped_estimate_count: int = 0
    triggered_count: int = 0
    skipped_cooldown_count: int = 0
    daily_count: int = 0
    daily_skipped_window_count: int = 0
    daily_no_data_count: int = 0
    time_partial_suppressed_count: int = 0
    sent_count: int = 0
    unknown_count: int = 0
    failed_count: int = 0
    events: list[dict[str, Any]] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        out = {k: v for k, v in self.__dict__.items() if k != "events"}
        out["event_count"] = len(self.events)
        return out


def classify_quote(deal: FlightDeal) -> str:
    """`SSE_FRESH` is the only pushable class; everything else is a drop reason."""
    if getattr(deal, "freshness", "FRESH") != "FRESH":
        return "STALE"
    source = getattr(deal, "data_source", "")
    if source == "LIVE_SSE":
        return "SSE_FRESH" if int(getattr(deal, "price_krw", 0) or 0) > 0 else "INVALID"
    if source == "LIVE_PG":
        return "PG"
    return "ESTIMATE"


def _parse_day(value: str) -> Optional[date]:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def matches_day_pattern(deal: FlightDeal, pattern: str) -> bool:
    if pattern in (None, "", "ANY"):
        return True
    dep, ret = _parse_day(deal.departure_date), _parse_day(deal.return_date)
    if dep is None or ret is None:
        return False
    if pattern == "FRI_MON":
        return dep.weekday() == 4 and ret.weekday() == 0
    if pattern == "THU_SUN":
        return dep.weekday() == 3 and ret.weekday() == 6
    return False


def matches_weekday_option(deal: FlightDeal, option: Optional[str]) -> bool:
    """WEEKEND: Thu->Sun / Fri->Mon, WEEKDAY: Mon->Thu / Tue->Fri, ANY: all four (3N), OFFICE: 2~4N <=1 leave."""
    dep, ret = _parse_day(deal.departure_date), _parse_day(deal.return_date)
    if dep is None or ret is None:
        return False
    norm = normalize_weekday_option(option or "ANY")
    nights = (ret - dep).days
    if norm == "OFFICE":
        return OFFICE_MIN_NIGHTS <= nights <= OFFICE_MAX_NIGHTS and calc_required_leave_days(dep, ret, "18:30") <= OFFICE_MAX_LEAVE_DAYS
    if norm == "ANY_OFFICE":
        if (dep.weekday(), nights) in set(WEEKDAY_OPTION_PATTERNS["ANY"]):
            return True
        return OFFICE_MIN_NIGHTS <= nights <= OFFICE_MAX_NIGHTS and calc_required_leave_days(dep, ret, "18:30") <= OFFICE_MAX_LEAVE_DAYS
    patterns = WEEKDAY_OPTION_PATTERNS[norm]
    return (dep.weekday(), nights) in set(patterns)


def _resolve_office_deal(deal: FlightDeal) -> Optional[FlightDeal]:
    """Strict [DATA-01] filter for OFFICE watches: requires LIVE_SSE + FRESH + valid exact dep/ret_arr times
    (ret_arr >= 13:00, not next-day arrival) and required leave <= 1.0 day. Falls back to `office_*`
    candidate on the same verified deal when `cheapest` fails time/leave constraints."""
    if getattr(deal, "data_source", "") != "LIVE_SSE" or getattr(deal, "freshness", "FRESH") != "FRESH":
        return None
    dep, ret = _parse_day(deal.departure_date), _parse_day(deal.return_date)
    if dep is None or ret is None:
        return None
    dep_m = _hhmm_to_minutes(getattr(deal, "exact_dep_time", "") or "")
    arr_m = _hhmm_to_minutes(getattr(deal, "exact_ret_arr_time", "") or "")
    if (
        dep_m is not None
        and arr_m is not None
        and arr_m >= OFFICE_RET_MIN_ARR_MINUTES
        and getattr(deal, "next_day_arrival", False) is False  # unknown (None) arrival date == rejected
        and calc_required_leave_days(dep, ret, deal.exact_dep_time) <= OFFICE_MAX_LEAVE_DAYS
    ):
        return deal
    off_fare = int(getattr(deal, "office_fare_krw", 0) or 0)
    off_dep = getattr(deal, "office_dep_time", "") or ""
    off_arr = getattr(deal, "office_ret_arr_time", "") or ""
    off_leave = getattr(deal, "office_leave_days", None)
    off_dep_m = _hhmm_to_minutes(off_dep)
    off_arr_m = _hhmm_to_minutes(off_arr)
    if (
        off_fare > 0
        and off_dep_m is not None
        and off_arr_m is not None
        and off_arr_m >= OFFICE_RET_MIN_ARR_MINUTES
        and not getattr(deal, "next_day_arrival", False)
        and off_leave is not None
        and float(off_leave) <= OFFICE_MAX_LEAVE_DAYS
    ):
        # The `office_*` itinerary itself was selected with a verified same-day arrival (parse_sse_final_object).
        return replace(
            deal,
            price_krw=off_fare,
            price_min_krw=off_fare,
            airline=getattr(deal, "office_airline", "") or deal.airline,
            exact_dep_time=off_dep,
            exact_ret_arr_time=off_arr,
            next_day_arrival=False,
        )
    return None


def within_horizon(deal: FlightDeal, today: date, horizon_months: int) -> bool:
    dep = _parse_day(deal.departure_date)
    return dep is not None and dep <= add_months(today, int(horizon_months or 6))


def union_weekday_option(options: list[str]) -> str:
    opts = {normalize_weekday_option(o or "ANY") for o in options}
    if len(opts) == 1:
        return opts.pop()
    if "OFFICE" in opts or "ANY_OFFICE" in opts:
        return "ANY_OFFICE"
    return "ANY"


def is_peak_deal(deal: FlightDeal) -> bool:
    if getattr(deal, "is_peak_holiday", False):
        return True
    dep, ret = _parse_day(deal.departure_date), _parse_day(deal.return_date)
    if dep is None:
        return False
    return bool(FlightEngine.get_peak_holiday_info(dep, ret)[0])


def is_daily_window(kst_now: datetime) -> bool:
    return DAILY_WINDOW_START_HOUR <= kst_now.hour <= DAILY_WINDOW_END_HOUR


def _date_label(value: str) -> str:
    d = _parse_day(value)
    return f"{value}({_DOW[d.weekday()]})" if d else value


def _short_date(value: str) -> str:
    d = _parse_day(value)
    return f"{d.month:02d}/{d.day:02d}({_DOW[d.weekday()]})" if d else (value or "")


def _watch_mode(watch: dict[str, Any]) -> str:
    mode = str(watch.get("watch_mode") or "FLEX").upper()
    if mode == "FIXED" and watch.get("fixed_dep_date") and watch.get("fixed_ret_date"):
        return "FIXED"
    return "FLEX"


def _notify_mode(watch: dict[str, Any]) -> str:
    return "DAILY" if str(watch.get("notify_mode") or "DROP").upper() == "DAILY" else "DROP"


def _is_legacy_cadence(watch: dict[str, Any]) -> bool:
    """FLEX + DROP watches keep the legacy ledger key (bit-identical duplicate semantics)."""
    return _watch_mode(watch) == "FLEX" and _notify_mode(watch) == "DROP"


def is_expired_fixed(watch: dict[str, Any], today: date) -> bool:
    dep = _parse_day(watch.get("fixed_dep_date") or "")
    return _watch_mode(watch) == "FIXED" and dep is not None and dep < today


def make_live_quote_fetcher(engine: FlightEngine, sse_verify_top: int = SSE_VERIFY_PER_CITY) -> QuoteFetcher:
    """One live search per airport, then SSE-verifies the cheapest FRESH price-graph slots under the max target.
    Called without a spec it is the legacy 6-month weekend fetch; with a `WatchFetchSpec` it searches the
    watch horizon / weekday slot set in its own cache namespace under a 45s collection deadline."""
    from travel_brief.naver_live import WATCH_COLLECTION_DEADLINE_SECONDS

    def _verify(code: str, custom: Optional[dict], deals: list[FlightDeal], max_target: int, cap: int) -> list[FlightDeal]:
        if not getattr(engine, "live_naver", False) or cap <= 0:
            return deals
        pg = sorted(
            (d for d in deals if d.data_source == "LIVE_PG" and d.freshness == "FRESH" and d.price_krw <= max_target),
            key=lambda d: d.price_krw,
        )[:cap]
        if not pg:
            return deals
        try:
            if custom:
                verified = engine.ensure_deals_sse_verified(pg, custom_destinations=custom)
            else:
                verified = engine.ensure_deals_sse_verified(pg)
        except Exception as exc:
            logger.warning("Watch SSE verification failed for %s: %s", code, exc)
            return deals
        upgraded = {(d.departure_date, d.return_date): d for d in verified or [] if d.data_source == "LIVE_SSE"}
        return [upgraded.get((d.departure_date, d.return_date), d) for d in deals]

    def _verify_all(code: str, custom: Optional[dict], deals: list[FlightDeal], ceiling: int,
                    deadline: float) -> WatchDeals:
        """Time-coverage mode: SSE-verify EVERY fresh PG slot priced <= ceiling (no cap). Any eligible slot left
        unverified (deadline / failure / live off) marks the result `partial_coverage`."""
        out = WatchDeals(deals)
        eligible = [d for d in deals if d.data_source == "LIVE_PG" and d.freshness == "FRESH" and d.price_krw <= ceiling]
        if not eligible:
            return out
        if not getattr(engine, "live_naver", False) or time.monotonic() >= deadline:
            out.partial_coverage = True
            return out
        kwargs: dict[str, Any] = {"live_deadline": deadline}
        if custom:
            kwargs["custom_destinations"] = custom
        try:
            verified = engine.ensure_deals_sse_verified(eligible, **kwargs)
        except Exception as exc:
            logger.warning("Watch time-coverage SSE verification failed for %s: %s", code, exc)
            out.partial_coverage = True
            return out
        upgraded = {(d.departure_date, d.return_date): d for d in verified or [] if d.data_source == "LIVE_SSE"}
        merged = WatchDeals(upgraded.get((d.departure_date, d.return_date), d) for d in deals)
        merged.partial_coverage = any((d.departure_date, d.return_date) not in upgraded for d in eligible)
        if merged.partial_coverage:
            logger.info("[WATCH] %s time coverage partial: %d/%d slots verified.", code,
                        sum(1 for d in eligible if (d.departure_date, d.return_date) in upgraded), len(eligible))
        return merged

    def fetch(code: str, custom: Optional[dict], max_target: int, spec: Optional[WatchFetchSpec] = None) -> list[FlightDeal]:
        if spec is None:
            kwargs: dict[str, Any] = {"dest_code": code, "months_ahead": 6, "fast_live": True}
            if custom:
                kwargs["custom_destinations"] = custom
            deals = list(engine.search_deals(**kwargs) or [])
            return _verify(code, custom, deals, max_target, sse_verify_top)
        deadline = time.monotonic() + WATCH_COLLECTION_DEADLINE_SECONDS
        deals = list(engine.search_watch_deals(
            code,
            horizon_months=spec.horizon_months,
            weekday_option=spec.weekday_option,
            custom_destinations=custom,
            fast_live=True,
            live_deadline=deadline,
        ) or [])
        coverage = bool(getattr(spec, "needs_time_coverage", False))
        if time.monotonic() >= deadline:
            if coverage:
                out = WatchDeals(deals)
                out.partial_coverage = True
                return out
            return deals
        verified = _verify(code, custom, deals, max_target, min(sse_verify_top, spec.sse_cap))
        if not coverage:
            return verified
        ceiling = int(spec.price_ceiling_krw or max_target)
        return _verify_all(code, custom, verified, ceiling, deadline)

    def fetch_fixed(code: str, custom: Optional[dict], dep_iso: str, ret_iso: str,
                    include_neighbors: bool = True) -> list[FlightDeal]:
        return list(engine.fetch_fixed_quotes(
            code, dep_iso, ret_iso, include_neighbors=include_neighbors, custom_destinations=custom,
        ) or [])

    fetch.accepts_spec = True  # type: ignore[attr-defined]
    fetch.fetch_fixed = fetch_fixed  # type: ignore[attr-defined]
    return fetch


def default_lease_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


class WatchCheckEngine:
    def __init__(
        self,
        db: TravelBriefDatabase,
        quote_fetcher: QuoteFetcher,
        sender: Optional[WatchSender] = None,
        catalog: Optional[CityCatalog] = None,
        dry_run: bool = False,
        lease_owner: Optional[str] = None,
        lease_ttl_seconds: int = TICK_LEASE_TTL_SECONDS,
        now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        fixed_quote_fetcher: Optional[FixedQuoteFetcher] = None,
        tick_budget_seconds: float = WATCH_TICK_BUDGET_SECONDS,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ):
        if sender is None and not dry_run:
            raise ValueError("a sender is required unless dry_run=True")
        self.db = db
        self.quote_fetcher = quote_fetcher
        self.fixed_quote_fetcher = fixed_quote_fetcher or getattr(quote_fetcher, "fetch_fixed", None)
        self.sender = sender
        self.catalog = catalog or CityCatalog(FlightEngine(live_naver=False).destinations)
        self.dry_run = bool(dry_run)
        self.lease_owner = lease_owner or default_lease_owner()
        self.lease_ttl_seconds = lease_ttl_seconds
        self.now_fn = now_fn
        self.tick_budget_seconds = float(tick_budget_seconds)
        self.monotonic_fn = monotonic_fn

    # ------------------------------------------------------------------
    def run_once(self) -> WatchTickResult:
        result = WatchTickResult(dry_run=self.dry_run)
        token: Optional[str] = None
        if not self.dry_run:
            token = self.db.acquire_watch_tick_lease(
                TICK_LEASE_NAME, self.lease_owner, ttl_seconds=self.lease_ttl_seconds, now=self.now_fn()
            )
            if token is None:
                result.skipped_locked = True
                logger.info("[WATCH] tick skipped: lease '%s' is held by another runner.", TICK_LEASE_NAME)
                return result
        try:
            kst_now = self.now_fn().astimezone(KST)
            if not self.dry_run:
                result.expired_in_flight = self.db.expire_stale_watch_in_flight()
                # Pre-fetch expiry: a past FIXED trip is never fetched nor alerted.
                result.expired_fixed_count = self.db.expire_fixed_watches(kst_now.date().isoformat())
            self._run_tick(result, kst_now)
        finally:
            if token is not None:
                self.db.release_watch_tick_lease(TICK_LEASE_NAME, token)
        logger.info("[WATCH] tick summary: %s", result.summary())
        return result

    def _call_quote_fetcher(self, code: str, custom: Optional[dict], max_target: int, spec: WatchFetchSpec):
        if getattr(self.quote_fetcher, "accepts_spec", False):
            return self.quote_fetcher(code, custom, max_target, spec)
        return self.quote_fetcher(code, custom, max_target)

    def _classify_into(self, deals: list[FlightDeal], codes: set[str], result: WatchTickResult) -> list[FlightDeal]:
        fresh: list[FlightDeal] = []
        for d in deals:
            if d.destination_code.upper() not in codes:
                continue
            result.candidate_count += 1
            kind = classify_quote(d)
            if kind == "SSE_FRESH":
                fresh.append(d)
            elif kind == "PG":
                result.dropped_pg_count += 1
            elif kind == "STALE":
                result.dropped_stale_count += 1
            else:
                result.dropped_estimate_count += 1
        result.sse_fresh_count += len(fresh)
        return fresh

    def _run_tick(self, result: WatchTickResult, kst_now: datetime) -> None:
        today = kst_now.date()
        watches, result.subscriptions_checked = self.db.list_deliverable_watches()
        if not result.subscriptions_checked:
            logger.info("[WATCH] `subscriptions` table absent; recipient/watch filters only (standalone DB).")
        if self.dry_run:  # read-only: expiry is applied in memory only
            watches = [w for w in watches if not is_expired_fixed(w, today)]
        result.eligible_watch_count = len(watches)
        tick_deadline = self.monotonic_fn() + self.tick_budget_seconds

        flex_by_code: dict[str, list[dict[str, Any]]] = {}
        fixed_by_key: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        targets: dict[int, tuple[str, ...]] = {}
        unknown_scopes: set[str] = set()
        for w in watches:
            scope = str(w["dest_code"]).upper()
            codes = self.catalog.watch_target_codes(scope)
            if not codes:
                if scope not in unknown_scopes:
                    unknown_scopes.add(scope)
                    logger.warning("[WATCH] unknown destination %s skipped.", scope)
                    result.fetch_error_count += 1
                continue
            if _watch_mode(w) != "FIXED" and str(w.get("weekday_option") or "").upper() == "OFFICE":
                codes = tuple(
                    c for c in codes
                    if not getattr(getattr(self.catalog.get(c), "meta", None), "is_long_haul", False)
                )
                if not codes:
                    continue
            targets[int(w["watch_id"])] = codes
            if _watch_mode(w) == "FIXED":
                for c in codes:
                    fixed_by_key.setdefault((c, w["fixed_dep_date"], w["fixed_ret_date"]), []).append(w)
            else:
                for c in codes:
                    flex_by_code.setdefault(c, []).append(w)
        result.city_count = len(set(flex_by_code) | {k[0] for k in fixed_by_key})

        # 1) FLEX: one fetch per airport (union horizon / weekday option across its watches)
        flex_fresh: dict[str, list[FlightDeal]] = {}
        flex_partial: set[str] = set()
        for code, city_watches in flex_by_code.items():
            if self.monotonic_fn() >= tick_deadline:
                result.deadline_skipped_count += 1
                continue
            custom = self.catalog.custom_destinations_for(code)
            if any(_notify_mode(w) == "DAILY" for w in city_watches):
                max_target = DAILY_PRICE_CEILING_KRW
            else:
                max_target = max(int(w["target_price_krw"]) for w in city_watches)
            city_scoped = any(str(w["dest_code"]).upper() == code for w in city_watches)
            timed = [w for w in city_watches if has_time_filter(w)]
            ceiling: Optional[int] = None
            if timed:
                if any(_notify_mode(w) == "DAILY" for w in timed):
                    ceiling = DAILY_PRICE_CEILING_KRW
                else:
                    ceiling = max(int(w["target_price_krw"]) for w in timed)
            spec = WatchFetchSpec(
                horizon_months=max(int(w.get("horizon_months") or 6) for w in city_watches),
                weekday_option=union_weekday_option([w.get("weekday_option") or "ANY" for w in city_watches]),
                sse_cap=SSE_VERIFY_PER_CITY if city_scoped else COUNTRY_SSE_VERIFY_PER_AIRPORT,
                needs_time_coverage=bool(timed),
                price_ceiling_krw=ceiling,
            )
            try:
                raw = self._call_quote_fetcher(code, custom, max_target, spec)
                deals = list(raw or [])
            except Exception as exc:
                logger.warning("[WATCH] quote fetch failed for %s: %s", code, exc)
                result.fetch_error_count += 1
                continue
            if getattr(raw, "partial_coverage", False):
                flex_partial.add(code)
            flex_fresh[code] = self._classify_into(deals, {code}, result)

        # 2) FIXED: exact dates (+ ±1d neighbours for city scopes) per (airport, dep, ret)
        fixed_fresh: dict[tuple[str, str, str], list[FlightDeal]] = {}
        for key, key_watches in fixed_by_key.items():
            code, dep_iso, ret_iso = key
            if self.fixed_quote_fetcher is None:
                result.fixed_unavailable_count += len(key_watches)
                continue
            if self.monotonic_fn() >= tick_deadline:
                result.deadline_skipped_count += 1
                continue
            custom = self.catalog.custom_destinations_for(code)
            include_neighbors = any(str(w["dest_code"]).upper() == code for w in key_watches)
            try:
                deals = list(self.fixed_quote_fetcher(code, custom, dep_iso, ret_iso, include_neighbors) or [])
            except Exception as exc:
                logger.warning("[WATCH] fixed quote fetch failed for %s %s~%s: %s", code, dep_iso, ret_iso, exc)
                result.fetch_error_count += 1
                continue
            fixed_fresh[key] = self._classify_into(deals, {code}, result)

        # 3) Per watch decisions (at most one message per watch per tick)
        for w in watches:
            codes = targets.get(int(w["watch_id"]))
            if not codes:
                continue
            try:
                timed = has_time_filter(w)
                if _watch_mode(w) == "FIXED":
                    dep_iso, ret_iso = w["fixed_dep_date"], w["fixed_ret_date"]
                    pool = [d for c in codes for d in fixed_fresh.get((c, dep_iso, ret_iso), [])]
                    main = [d for d in pool if d.departure_date == dep_iso and d.return_date == ret_iso]
                    neighbors = [d for d in pool if not (d.departure_date == dep_iso and d.return_date == ret_iso)]
                    if not timed:
                        self._process_watch(w, main, result, kst_now, neighbors=neighbors)
                        continue
                    sel_main, reference, complete = self._select_time_window(w, main)
                    sel_nb, _ref_nb, _complete_nb = self._select_time_window(w, neighbors)
                    if not complete:
                        result.time_partial_suppressed_count += 1
                        continue
                    self._process_watch(w, sel_main, result, kst_now, neighbors=sel_nb, reference=reference)
                else:
                    pool = [d for c in codes for d in flex_fresh.get(c, [])]
                    if not timed:
                        self._process_watch(w, self._filter_flex(w, pool, today), result, kst_now)
                        continue
                    if any(c in flex_partial or c not in flex_fresh for c in codes):
                        # [DATA-01] fail-closed: an incomplete slot sweep can neither prove nor rule out a fare.
                        result.time_partial_suppressed_count += 1
                        continue
                    scoped = self._filter_flex_dates(w, pool, today)
                    selected, reference, complete = self._select_time_window(w, scoped)
                    if not complete:
                        result.time_partial_suppressed_count += 1
                        continue
                    self._process_watch(w, selected, result, kst_now, reference=reference)
            except Exception as exc:
                logger.error("[WATCH] watch %s failed: %s", w.get("watch_id"), exc, exc_info=True)

    @staticmethod
    def _filter_flex_dates(watch: dict[str, Any], deals: list[FlightDeal], today: date) -> list[FlightDeal]:
        """Date scope only (weekday option / horizon / day pattern / peak); no OFFICE time resolution."""
        include_peak = bool(watch["include_peak"])
        horizon = int(watch.get("horizon_months") or 6)
        option = watch.get("weekday_option") or "ANY"
        return [
            d for d in deals
            if matches_weekday_option(d, option)
            and within_horizon(d, today, horizon)
            and matches_day_pattern(d, watch["day_pattern"])
            and (include_peak or not is_peak_deal(d))
        ]

    @staticmethod
    def _select_time_window(
        watch: dict[str, Any], scoped: list[FlightDeal]
    ) -> tuple[list[FlightDeal], Optional[FlightDeal], bool]:
        """Time-window watch selection over date-scoped verified deals.
        Returns (primary deals — one cheapest matching itinerary per slot, unfiltered_best reference, complete).
        `complete=False` when an itinerary whose match is UNKNOWN is cheaper than the chosen primary (or when
        there is no primary at all but unknown itineraries exist): the watch is then suppressed ([DATA-01])."""
        filters = watch_time_filters(watch)
        office = _watch_mode(watch) == "FLEX" and normalize_weekday_option(watch.get("weekday_option") or "ANY") == "OFFICE"
        primaries: list[FlightDeal] = []
        unknown_floors: list[int] = []
        unfiltered: list[FlightDeal] = []
        for d in scoped:
            if classify_quote(d) != "SSE_FRESH":
                continue
            plain = _resolve_office_deal(d) if office else d
            if plain is not None:
                unfiltered.append(plain)
            eff, unknown = resolve_time_window_deal(d, filters, office=office)
            if eff is not None:
                primaries.append(eff)
            if unknown is not None:
                unknown_floors.append(int(unknown))
        best_price = min((p.price_krw for p in primaries), default=None)
        complete = not any(best_price is None or u < best_price for u in unknown_floors)
        reference = min(unfiltered, key=lambda x: (x.price_krw, x.departure_date, x.destination_code), default=None)
        return primaries, reference, complete

    @classmethod
    def _filter_flex(cls, watch: dict[str, Any], deals: list[FlightDeal], today: date) -> list[FlightDeal]:
        option = watch.get("weekday_option") or "ANY"
        norm_opt = normalize_weekday_option(option)
        out: list[FlightDeal] = []
        for d in cls._filter_flex_dates(watch, deals, today):
            if norm_opt == "OFFICE":
                eff = _resolve_office_deal(d)
                if eff is not None:
                    out.append(eff)
            else:
                out.append(d)
        return out

    def _scope_label(self, watch: dict[str, Any]) -> str:
        meta = get_watch_country_meta(watch["dest_code"])
        return meta["label"] if meta else ""

    def _process_watch(
        self,
        watch: dict[str, Any],
        deals: list[FlightDeal],
        result: WatchTickResult,
        kst_now: datetime,
        neighbors: Optional[list[FlightDeal]] = None,
        reference: Optional[FlightDeal] = None,
    ) -> None:
        matching = sorted(deals, key=lambda d: (d.price_krw, d.departure_date, d.destination_code))
        notify = _notify_mode(watch)
        in_window = is_daily_window(kst_now)
        if not matching:
            if notify == "DAILY" and in_window:
                result.daily_no_data_count += 1  # [DATA-01]: no verified fare -> no briefing (never guessed)
            return
        best = matching[0]
        prev_seen = watch.get("last_seen_price_krw")
        initial = watch.get("initial_price_krw")
        if not self.dry_run:
            self.db.update_watch_observation(
                watch["watch_id"], best.price_krw, f"{best.departure_date}~{best.return_date}",
            )
            if initial is None and self.db.set_watch_initial_price_if_null(watch["watch_id"], best.price_krw):
                initial = best.price_krw
        neighbor_best = self._cheaper_neighbor(neighbors or [], best)

        if notify == "DAILY":
            if in_window:
                if self._try_daily(watch, best, result, kst_now, prev_seen, initial, neighbor_best, reference):
                    return  # never DAILY + DROP for the same watch in one tick
            else:
                result.daily_skipped_window_count += 1  # no compensatory briefing outside the morning

        target = int(watch["target_price_krw"])
        mode = _watch_mode(watch)
        extended = mode == "FIXED" or notify == "DAILY"
        floor: Optional[int] = None
        if extended:
            recent = self.db.recent_blocking_watch_deliveries(watch["watch_id"], now=self.now_fn())
            if recent:
                floor = min(int(r["price_krw"]) for r in recent)
        candidates = [
            d for d in matching
            if d.price_krw <= target or (floor is not None and d.price_krw <= floor - WATCH_MIN_REDROP_KRW)
        ]
        if not candidates:
            return
        result.triggered_count += 1
        allow_same_slot = mode == "FIXED"
        key_version = 1 if _is_legacy_cadence(watch) else 2

        for deal in candidates:  # cheapest first; at most ONE alert per watch per tick
            tip = neighbor_best if deal is best else self._cheaper_neighbor(neighbors or [], deal)
            fingerprint = getattr(deal, "candidate_fingerprint", "") or ""
            if self.dry_run:
                if self._preview_block_reason(watch, deal, allow_same_slot, key_version, fingerprint):
                    continue
                result.events.append(self._event(watch, deal, "DRY_RUN", kind="DROP"))
                return
            claim = self.db.claim_watch_delivery(
                watch_id=watch["watch_id"],
                chat_id=watch["chat_id"],
                dest_code=watch["dest_code"],
                dep_date=deal.departure_date,
                ret_date=deal.return_date,
                price_krw=deal.price_krw,
                lease_owner=self.lease_owner,
                lease_ttl_seconds=DELIVERY_LEASE_TTL_SECONDS,
                now=self.now_fn(),
                allow_same_slot_drop=allow_same_slot,
                key_version=key_version,
                fingerprint=fingerprint,
            )
            if not claim.claimed:
                continue
            text = build_watch_alert_card(
                watch, deal, self.catalog.get(deal.destination_code), prev_seen,
                kind="DROP", neighbor_deal=tip, scope_label=self._scope_label(watch), initial_price=initial,
                reference_deal=reference,
            )
            markup = build_watch_alert_keyboard(watch["dest_code"], deal, include_list=True)
            self._send_and_finalize(watch, deal, claim, text, markup, result, kind="DROP")
            return
        result.skipped_cooldown_count += 1

    def _try_daily(
        self,
        watch: dict[str, Any],
        best: FlightDeal,
        result: WatchTickResult,
        kst_now: datetime,
        prev_seen: Optional[int],
        initial: Optional[int],
        neighbor_best: Optional[FlightDeal],
        reference: Optional[FlightDeal] = None,
    ) -> bool:
        """Returns True when this tick produced (or attempted) the day's briefing for the watch."""
        kst_date = kst_now.date().isoformat()
        if self.dry_run:
            if self.db.is_watch_delivery_blocked(make_watch_daily_key(watch["watch_id"], kst_date)):
                return False
            result.events.append(self._event(watch, best, "DRY_RUN", kind="DAILY"))
            return True
        claim = self.db.claim_watch_daily_delivery(
            watch_id=watch["watch_id"],
            chat_id=watch["chat_id"],
            dest_code=watch["dest_code"],
            kst_date=kst_date,
            dep_date=best.departure_date,
            ret_date=best.return_date,
            price_krw=best.price_krw,
            lease_owner=self.lease_owner,
            lease_ttl_seconds=DELIVERY_LEASE_TTL_SECONDS,
            now=self.now_fn(),
        )
        if not claim.claimed:
            return False
        result.daily_count += 1
        text = build_watch_alert_card(
            watch, best, self.catalog.get(best.destination_code), prev_seen,
            kind="DAILY", neighbor_deal=neighbor_best, scope_label=self._scope_label(watch), initial_price=initial,
            reference_deal=reference,
        )
        markup = build_watch_alert_keyboard(watch["dest_code"], best, include_list=True)
        self._send_and_finalize(watch, best, claim, text, markup, result, kind="DAILY")
        return True

    def _send_and_finalize(self, watch, deal, claim, text, markup, result: WatchTickResult, kind: str) -> None:
        try:
            status, msg_id, err = self.sender(str(watch["chat_id"]), text, markup)
        except Exception as exc:  # sender contract violated -> ambiguous, quarantine
            status, msg_id, err = "UNKNOWN", None, f"sender exception: {exc}"
        status = status if status in ("SENT", "UNKNOWN", "FAILED") else "UNKNOWN"
        self.db.finalize_watch_delivery(claim.delivery_key, claim.lease_token, status, msg_id, err)
        if status == "SENT":
            result.sent_count += 1
        elif status == "UNKNOWN":
            result.unknown_count += 1
        else:
            result.failed_count += 1
        result.events.append(self._event(watch, deal, status, err, kind=kind))

    @staticmethod
    def _cheaper_neighbor(neighbors: list[FlightDeal], deal: FlightDeal) -> Optional[FlightDeal]:
        verified = [
            n for n in neighbors
            if classify_quote(n) == "SSE_FRESH" and n.price_krw < deal.price_krw
            and n.destination_code == deal.destination_code
        ]
        return min(verified, key=lambda n: (n.price_krw, n.departure_date)) if verified else None

    def _preview_block_reason(
        self, watch: dict[str, Any], deal: FlightDeal, allow_same_slot: bool = False, key_version: int = 1,
        fingerprint: str = "",
    ) -> Optional[str]:
        if key_version == 2:
            key = make_watch_drop_key_v2(
                watch["watch_id"], deal.departure_date, deal.return_date, deal.price_krw, fingerprint
            )
        else:
            key = make_watch_delivery_key(
                watch["watch_id"], watch["chat_id"], watch["dest_code"], deal.departure_date, deal.return_date,
                deal.price_krw, fingerprint,
            )
        dup = self.db.is_watch_delivery_blocked(key)
        if dup:
            return dup
        recent = self.db.recent_blocking_watch_deliveries(watch["watch_id"], now=self.now_fn())
        return watch_cooldown_block_reason(recent, deal.departure_date, deal.return_date, deal.price_krw,
                                           WATCH_MIN_REDROP_KRW, allow_same_slot_drop=allow_same_slot)

    @staticmethod
    def _event(watch: dict[str, Any], deal: FlightDeal, status: str, err: Optional[str] = None,
               kind: str = "DROP") -> dict[str, Any]:
        return {
            "watch_id": watch["watch_id"],
            "chat_id": str(watch["chat_id"]),
            "dest_code": watch["dest_code"],
            "airport": deal.destination_code,
            "kind": kind,
            "dep_date": deal.departure_date,
            "ret_date": deal.return_date,
            "price_krw": deal.price_krw,
            "target_price_krw": int(watch["target_price_krw"]),
            "status": status,
            "error": err,
        }


# ----------------------------------------------------------------------
# Mobile decision card (TG-01/TG-02: one screen, fully escaped; callbacks <= 64 bytes)
# ----------------------------------------------------------------------
def _visible_len(text: str) -> int:
    return len(_TAG_RE.sub("", text))


def _trip_nights(deal: FlightDeal) -> int:
    dep, ret = _parse_day(deal.departure_date), _parse_day(deal.return_date)
    if dep is not None and ret is not None and ret > dep:
        return (ret - dep).days
    return int(deal.nights or 0) or 3


def _hhmm(value: str) -> str:
    v = (value or "").strip()
    return v if re.fullmatch(r"\d{2}:\d{2}", v) else ""


def build_watch_alert_card(
    watch: dict[str, Any],
    deal: FlightDeal,
    res: Any,
    prev_seen: Optional[int] = None,
    *,
    kind: str = "DROP",
    neighbor_deal: Optional[FlightDeal] = None,
    scope_label: str = "",
    initial_price: Optional[int] = None,
    reference_deal: Optional[FlightDeal] = None,
) -> str:
    """Single-screen decision card: headline, exact itinerary/times, short-stay traps, target / anchor /
    last-seen comparisons, N-night budget, verified ±1d tip, LCC baggage & far-horizon disclosures.
    Time-window watches: `deal` is the in-window Primary; a strictly cheaper verified `reference_deal`
    (time-agnostic best) adds ONE optional line, the first to be trimmed for [TG-01]."""
    target = int(watch["target_price_krw"])
    price = int(deal.price_krw)
    saving = target - price
    saving_pct = (max(0, saving) / target * 100) if target > 0 else 0.0
    code = (deal.destination_code or watch["dest_code"]).upper()
    label = getattr(res, "display_name", None) or code
    if kind == "DAILY":
        headline = "📰 <b>[일일 브리핑]" + (" [목표가 도달]" if price <= target else "")
    elif price <= target:
        headline = "🔔 <b>[목표가 도달]"
    else:
        headline = "📉 <b>[특가 포착]"
    scope = f"{scope_label} · " if scope_label else ""
    nights = _trip_nights(deal)

    core = [
        f"{headline} {html.escape(scope)}{html.escape(label)} · ICN→{html.escape(code)}</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"✈️ {html.escape(_short_date(deal.departure_date))}~{html.escape(_short_date(deal.return_date))}"
        f" · {nights}박",
    ]
    dep_t, ret_t, arr_t = _hhmm(deal.exact_dep_time), _hhmm(deal.exact_ret_time), _hhmm(deal.exact_ret_arr_time)
    airline = (deal.airline or "").strip()
    if dep_t or ret_t:
        times = f"가는편 {dep_t or '--:--'} 출발 / 오는편 {ret_t or '--:--'} 출발"
        if arr_t:
            times += f" ({arr_t} 도착{' +1일' if deal.next_day_arrival else ''})"
        core.append(f"🛫 {html.escape(airline)} · {times}" if airline else f"🛫 {times}")
    elif airline and airline != "네이버 직항 최저가":
        core.append(f"🛫 {html.escape(airline)}")
    if dep_t and dep_t >= "17:00":
        core.append("⚠️ 첫날 저녁 출발 — 현지 첫날 일정이 짧아요")
    if ret_t and ret_t < "10:00":
        core.append("⚠️ 귀국편 오전 출발(10시 전) — 마지막 날 일정이 짧아요")
    if deal.next_day_arrival:
        core.append("⚠️ 귀국 다음날 도착 편입니다")
    window = time_window_label(watch)
    if window:
        core.append(f"⏱ 시간 조건: {html.escape(window)}")
        core.append(f"💰 조건 내 실시간 최저가: <b>{price:,}원</b> (네이버 실시간 왕복·1인)")
    else:
        core.append(f"💰 실시간 최저가: <b>{price:,}원</b> (네이버 실시간 왕복·1인)")
    if saving >= 0:
        core.append(f"🎯 내 목표가 {target:,}원 대비 <b>{saving:,}원({saving_pct:.1f}%)</b> 저렴")
    else:
        core.append(f"🎯 내 목표가 {target:,}원까지 {-saving:,}원 남음")

    optional: list[str] = []
    if initial_price and int(initial_price) != price:
        diff = int(initial_price) - price
        trend = f"{diff:,}원 하락 ⬇️" if diff > 0 else f"{-diff:,}원 상승 ⬆️"
        optional.append(f"📌 등록 기준 {int(initial_price):,}원 → 지금 {trend}")
    if prev_seen and int(prev_seen) > price:
        drop = int(prev_seen) - price
        optional.append(f"📉 지난 관측 대비 {drop:,}원 하락" + (" 🔥" if drop >= WATCH_MIN_REDROP_KRW else ""))
    meta = getattr(res, "meta", None)
    if meta is not None:
        try:
            budget = BudgetBreakdown.calculate(meta, price, nights=nights)
            optional.append(
                f"🧳 1인 총예산 약 {round_man(budget.total_per_person_krw)}만 원 ({nights}박 항공+숙박+식비·교통 추정)"
            )
        except Exception:
            pass
    if is_peak_deal(deal):
        name = (getattr(deal, "holiday_name", "") or "").strip()
        optional.append("🗓️ 연휴·성수기 일정이 포함된 요금입니다." + (f" ({html.escape(name)})" if name else ""))
    if neighbor_deal is not None and classify_quote(neighbor_deal) == "SSE_FRESH" and neighbor_deal.price_krw < price:
        optional.append(
            f"💡 ±1일 비교: {html.escape(_short_date(neighbor_deal.departure_date))}~"
            f"{html.escape(_short_date(neighbor_deal.return_date))} {int(neighbor_deal.price_krw):,}원 "
            f"({price - int(neighbor_deal.price_krw):,}원 ↓)"
        )
    if airline not in FSC_CARRIERS:
        optional.append("🎒 특가 운임은 위탁수하물 불포함 경우가 많습니다.")
    if int(watch.get("horizon_months") or 0) > 6 and _watch_mode(watch) == "FLEX":
        optional.append("ℹ️ 9~12개월 먼 일정은 LCC 특가가 아직 미오픈일 수 있어요 (실시간 검증 요금만 안내).")
    if (
        window
        and reference_deal is not None
        and classify_quote(reference_deal) == "SSE_FRESH"
        and int(reference_deal.price_krw) < price
    ):
        ref_dep = _hhmm(reference_deal.exact_dep_time) or "--:--"
        ref_arr = _hhmm(reference_deal.exact_ret_arr_time) or "--:--"
        ref_dates = f"{_short_date(reference_deal.departure_date)}~{_short_date(reference_deal.return_date)}"
        # Appended LAST so the [TG-01] trim loop (pops from the end) drops it first.
        optional.append(
            f"💡 참고(시간 무관 최저 특가): {int(reference_deal.price_krw):,}원~ "
            f"({html.escape(ref_dep)} 출발 · {html.escape(ref_arr)} 인천도착 · {html.escape(ref_dates)})"
        )
    footer ="⚠️ 결제 단계에서 요금·재고가 변동될 수 있습니다."

    lines = core + optional + [footer]
    while optional and _visible_len("\n".join(lines)) > CARD_MAX_VISIBLE:
        optional.pop()
        lines = core + optional + [footer]
    return "\n".join(lines)


def build_watch_alert_keyboard(dest_code: str, deal: FlightDeal, include_list: bool = False) -> dict[str, Any]:
    code = dest_code.upper()
    country = is_country_watch_code(code)
    airport = (deal.destination_code or "").upper() if country else code
    rows: list[list[dict[str, Any]]] = []
    url = FlightEngine.build_naver_flight_url(
        deal.origin_code or "ICN", airport, deal.departure_date, deal.return_date
    )
    if url.startswith("https://") and airport:
        rows.append([{"text": "✈️ 네이버 항공권 예약 ↗", "url": url}])
    settings_cb = f"trv:w:s:{code}" if country else f"trv:w:m:{code}"
    if include_list:
        rows.append([
            {"text": "⚙️ 조건 변경", "callback_data": settings_cb},
            {"text": "🔕 이 알림 끄기", "callback_data": f"trv:w:x:{code}"},
        ])
        rows.append([{"text": "📋 내 알림", "callback_data": "trv:w:l"}])
    else:
        rows.append([
            {"text": "⚙️ 알림 설정", "callback_data": settings_cb},
            {"text": "🔕 알림 끄기", "callback_data": f"trv:w:x:{code}"},
        ])
    return {"inline_keyboard": rows}
