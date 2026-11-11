"""Travel price watch checker (cron tick): per-city target price alerts.

Invariants:
1. Singleton tick lease (`travel_watch_tick_leases`) — a concurrent manager/cron tick exits as `skipped_locked`.
2. Expired IN_FLIGHT claims are quarantined to UNKNOWN at tick start and never retried ([PROD-LIVE]).
3. Subscription SSOT: active watch AND active TRAVEL recipient AND ACTIVE `travel_bot` subscription.
4. Only `LIVE_SSE` + `FRESH` quotes may trigger a push (LIVE_PG / STALE / estimates are dropped and counted).
5. Ledger (`travel_watch_deliveries`) is the only delivery state; max 1 alert per watch per tick, 7-day cooldown.
6. `dry_run` is read-only: no lease, no ledger writes, no observation updates, no Telegram calls.
"""
from __future__ import annotations

import html
import logging
import os
import socket
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Any, Callable, Optional

from travel_brief.city_catalog import CityCatalog
from travel_brief.flight_engine import FlightEngine
from travel_brief.models import BudgetBreakdown, FlightDeal, round_man
from travel_brief.storage.db import (
    TravelBriefDatabase,
    WATCH_MIN_REDROP_KRW,
    make_watch_delivery_key,
    watch_cooldown_block_reason,
)

logger = logging.getLogger(__name__)

TICK_LEASE_NAME = "travel_watch_check"
TICK_LEASE_TTL_SECONDS = 900
DELIVERY_LEASE_TTL_SECONDS = 120
SSE_VERIFY_PER_CITY = 3
DAY_PATTERN_LABELS = {"ANY": "전체 요일", "FRI_MON": "금~월", "THU_SUN": "목~일"}
_DOW = ["월", "화", "수", "목", "금", "토", "일"]

# (dest_code, custom_destinations | None, max_target_krw) -> deals
QuoteFetcher = Callable[[str, Optional[dict], int], list[FlightDeal]]
# (chat_id, html_text, reply_markup) -> (status, provider_msg_id, error_detail)
WatchSender = Callable[[str, str, dict], tuple[str, Any, Optional[str]]]


@dataclass
class WatchTickResult:
    skipped_locked: bool = False
    dry_run: bool = False
    subscriptions_checked: bool = False
    expired_in_flight: int = 0
    eligible_watch_count: int = 0
    city_count: int = 0
    fetch_error_count: int = 0
    candidate_count: int = 0
    sse_fresh_count: int = 0
    dropped_pg_count: int = 0
    dropped_stale_count: int = 0
    dropped_estimate_count: int = 0
    triggered_count: int = 0
    skipped_cooldown_count: int = 0
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


def is_peak_deal(deal: FlightDeal) -> bool:
    if getattr(deal, "is_peak_holiday", False):
        return True
    dep, ret = _parse_day(deal.departure_date), _parse_day(deal.return_date)
    if dep is None:
        return False
    return bool(FlightEngine.get_peak_holiday_info(dep, ret)[0])


def _date_label(value: str) -> str:
    d = _parse_day(value)
    return f"{value}({_DOW[d.weekday()]})" if d else value


def make_live_quote_fetcher(engine: FlightEngine, sse_verify_top: int = SSE_VERIFY_PER_CITY) -> QuoteFetcher:
    """One live search per city, then SSE-verifies the cheapest FRESH price-graph slots under the max target."""

    def fetch(code: str, custom: Optional[dict], max_target: int) -> list[FlightDeal]:
        kwargs: dict[str, Any] = {"dest_code": code, "months_ahead": 6, "fast_live": True}
        if custom:
            kwargs["custom_destinations"] = custom
        deals = list(engine.search_deals(**kwargs) or [])
        if not getattr(engine, "live_naver", False) or sse_verify_top <= 0:
            return deals
        pg = sorted(
            (d for d in deals if d.data_source == "LIVE_PG" and d.freshness == "FRESH" and d.price_krw <= max_target),
            key=lambda d: d.price_krw,
        )[:sse_verify_top]
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
    ):
        if sender is None and not dry_run:
            raise ValueError("a sender is required unless dry_run=True")
        self.db = db
        self.quote_fetcher = quote_fetcher
        self.sender = sender
        self.catalog = catalog or CityCatalog(FlightEngine(live_naver=False).destinations)
        self.dry_run = bool(dry_run)
        self.lease_owner = lease_owner or default_lease_owner()
        self.lease_ttl_seconds = lease_ttl_seconds
        self.now_fn = now_fn

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
            if not self.dry_run:
                result.expired_in_flight = self.db.expire_stale_watch_in_flight()
            self._run_tick(result)
        finally:
            if token is not None:
                self.db.release_watch_tick_lease(TICK_LEASE_NAME, token)
        logger.info("[WATCH] tick summary: %s", result.summary())
        return result

    def _run_tick(self, result: WatchTickResult) -> None:
        watches, result.subscriptions_checked = self.db.list_deliverable_watches()
        if not result.subscriptions_checked:
            logger.info("[WATCH] `subscriptions` table absent; recipient/watch filters only (standalone DB).")
        result.eligible_watch_count = len(watches)
        by_city: dict[str, list[dict[str, Any]]] = {}
        for w in watches:
            by_city.setdefault(w["dest_code"], []).append(w)
        result.city_count = len(by_city)

        for code, city_watches in by_city.items():
            res = self.catalog.get(code)
            if res is None:
                logger.warning("[WATCH] unknown destination %s skipped (%d watches).", code, len(city_watches))
                result.fetch_error_count += 1
                continue
            custom = self.catalog.custom_destinations_for(code)
            max_target = max(int(w["target_price_krw"]) for w in city_watches)
            try:
                deals = list(self.quote_fetcher(code, custom, max_target) or [])
            except Exception as exc:
                logger.warning("[WATCH] quote fetch failed for %s: %s", code, exc)
                result.fetch_error_count += 1
                continue

            fresh: list[FlightDeal] = []
            for d in deals:
                if d.destination_code.upper() != code:
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

            for w in city_watches:
                try:
                    self._process_watch(w, res, fresh, result)
                except Exception as exc:
                    logger.error("[WATCH] watch %s failed: %s", w.get("watch_id"), exc, exc_info=True)

    def _process_watch(self, watch: dict[str, Any], res: Any, fresh: list[FlightDeal], result: WatchTickResult) -> None:
        include_peak = bool(watch["include_peak"])
        matching = sorted(
            (
                d for d in fresh
                if matches_day_pattern(d, watch["day_pattern"]) and (include_peak or not is_peak_deal(d))
            ),
            key=lambda d: (d.price_krw, d.departure_date),
        )
        if not matching:
            return
        best = matching[0]
        prev_seen = watch.get("last_seen_price_krw")
        if not self.dry_run:
            self.db.update_watch_observation(
                watch["watch_id"], best.price_krw, f"{best.departure_date}~{best.return_date}",
            )
        target = int(watch["target_price_krw"])
        candidates = [d for d in matching if d.price_krw <= target]
        if not candidates:
            return
        result.triggered_count += 1

        for deal in candidates:  # cheapest first; at most ONE alert per watch per tick
            if self.dry_run:
                blocked = self._preview_block_reason(watch, deal)
                if blocked:
                    continue
                result.events.append(self._event(watch, deal, "DRY_RUN"))
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
            )
            if not claim.claimed:
                continue
            text = build_watch_alert_card(watch, deal, res, prev_seen)
            markup = build_watch_alert_keyboard(watch["dest_code"], deal)
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
            result.events.append(self._event(watch, deal, status, err))
            return
        result.skipped_cooldown_count += 1

    def _preview_block_reason(self, watch: dict[str, Any], deal: FlightDeal) -> Optional[str]:
        key = make_watch_delivery_key(
            watch["watch_id"], watch["chat_id"], watch["dest_code"], deal.departure_date, deal.return_date,
            deal.price_krw,
        )
        dup = self.db.is_watch_delivery_blocked(key)
        if dup:
            return dup
        recent = self.db.recent_blocking_watch_deliveries(watch["watch_id"], now=self.now_fn())
        return watch_cooldown_block_reason(recent, deal.departure_date, deal.return_date, deal.price_krw,
                                           WATCH_MIN_REDROP_KRW)

    @staticmethod
    def _event(watch: dict[str, Any], deal: FlightDeal, status: str, err: Optional[str] = None) -> dict[str, Any]:
        return {
            "watch_id": watch["watch_id"],
            "chat_id": str(watch["chat_id"]),
            "dest_code": watch["dest_code"],
            "dep_date": deal.departure_date,
            "ret_date": deal.return_date,
            "price_krw": deal.price_krw,
            "target_price_krw": int(watch["target_price_krw"]),
            "status": status,
            "error": err,
        }


# ----------------------------------------------------------------------
# Alert card (TG-01/TG-02: short, fully escaped; callbacks <= 64 bytes)
# ----------------------------------------------------------------------
def build_watch_alert_card(
    watch: dict[str, Any], deal: FlightDeal, res: Any, prev_seen: Optional[int] = None
) -> str:
    target = int(watch["target_price_krw"])
    price = int(deal.price_krw)
    saving = max(0, target - price)
    saving_pct = (saving / target * 100) if target > 0 else 0.0
    label = getattr(res, "display_name", None) or watch["dest_code"]
    lines = [
        f"🔔 <b>[목표가 도달] {html.escape(label)} 특가 알림</b>",
        "━━━━━━━━━━━━━━━━━━",
        f"✈️ 일정: {html.escape(_date_label(deal.departure_date))} → {html.escape(_date_label(deal.return_date))}"
        f" · {int(deal.nights)}박",
        f"💰 실시간 최저가: <b>{price:,}원</b> (네이버 실시간 관측)",
        f"🎯 내 목표가 {target:,}원 대비 <b>{saving:,}원({saving_pct:.1f}%)</b> 저렴",
    ]
    if prev_seen and int(prev_seen) > price:
        lines.append(f"📉 지난 관측 대비 {int(prev_seen) - price:,}원 하락")
    meta = getattr(res, "meta", None)
    if meta is not None:
        try:
            budget = BudgetBreakdown.calculate(meta, price, nights=int(deal.nights) or 3)
            lines.append(f"🧳 1인 총예산 약 {round_man(budget.total_per_person_krw)}만 원 (항공+숙박+식비·교통 추정)")
        except Exception:
            pass
    if is_peak_deal(deal):
        lines.append("🗓️ 연휴·성수기 일정이 포함된 요금입니다.")
    lines.append("⚠️ 결제 단계에서 요금·재고가 변동될 수 있습니다.")
    return "\n".join(lines)


def build_watch_alert_keyboard(dest_code: str, deal: FlightDeal) -> dict[str, Any]:
    code = dest_code.upper()
    rows: list[list[dict[str, Any]]] = []
    url = FlightEngine.build_naver_flight_url(
        deal.origin_code or "ICN", code, deal.departure_date, deal.return_date
    )
    if url.startswith("https://"):
        rows.append([{"text": "✈️ 네이버 항공권 예약 ↗", "url": url}])
    rows.append([
        {"text": "⚙️ 알림 설정", "callback_data": f"trv:w:m:{code}"},
        {"text": "🔕 알림 끄기", "callback_data": f"trv:w:x:{code}"},
    ])
    return {"inline_keyboard": rows}
