"""Internal Scheduled Dispatcher for Daily Market Brief v2.0 & Urgent Volatility Daemon.

Runs as a 24/7 background asyncio task inside FastAPI (Oracle Cloud VM / multibot.service).
Fires morning briefing at 07:50 KST and closing briefing at 15:40 KST on KR trading days.
Monitors ±3.0% sudden breaches during regular KR trading hours (09:00~15:30 KST).

Enforces:
- [PROD-LIVE] Zero Accidental Redelivery: Strict 15-minute start window (07:50~08:05, 15:40~15:55).
- Dual-DB Consistency: Cross-checks both portal.sqlite3 and alerts.sqlite3.
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import uuid
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from market_brief.alert_store import AlertStore
from market_brief.calendar_manager import MarketCalendar
from market_brief.market_watch import WATCH_TICK_INTERVAL_SEC, MarketWatchEvaluator, legacy_urgent_broadcast_enabled
from market_brief.pipeline import DEFAULT_SYMBOLS, MarketBriefingPipeline
from market_brief.storage.db import MarketBriefDatabase
from market_brief.storage.models import JobFinalStatus, Recipient
from market_brief.subscription_engine import SubscriptionEngine

logger = logging.getLogger("briefing_scheduler")
KST = ZoneInfo("Asia/Seoul")


class MarketBriefingDeliverer:
    """Production deliverer: fetches live quotes, renders reports, and dispatches via Telegram."""

    def __init__(self, root_dir: Path, get_report_base_url: Callable[[], str]):
        self.root_dir = root_dir
        self.get_report_base_url = get_report_base_url

    def deliver(
        self,
        db: MarketBriefDatabase,
        job_id: str,
        token: str,
        recipients: List[Recipient],
        kind: str,
        now_kst: datetime,
    ) -> Tuple[int, int, int, str]:
        """Synchronous delivery function intended to run in a worker thread.

        Returns (success_count, fail_count, unknown_count, report_path).
        """
        from market_brief.market_data import CompositeMarketDataProvider, FixtureProvider, NaverFinanceProvider
        from market_brief.market_data.naver_provider import fetch_naver_market_news
        from market_brief.pipeline import DEFAULT_SYMBOLS
        from market_brief.report_engine import BriefingBuilder
        from market_brief.channels.telegram import TelegramChannel
        from market_brief.channels.telegram_limiter import SafeTelegramDispatcher, TelegramRateLimiter
        from market_brief.web_report import HTMLReportRenderer, generate_signed_report_url

        # 1. Quotes
        fixture_p = FixtureProvider(str(self.root_dir / "fixtures/market_snapshot.json"))
        prov = CompositeMarketDataProvider(NaverFinanceProvider(), fixture_p)
        quotes = prov.fetch(DEFAULT_SYMBOLS)

        # 2. News
        news = []
        try:
            news = fetch_naver_market_news(5)
        except Exception:
            pass

        # 3. Report URLs and builder
        report_id = f"briefing_{now_kst.strftime('%Y%m%d_%H%M')}_{kind}"
        report_base_url = self.get_report_base_url()
        report_url = generate_signed_report_url(report_base_url, report_id, "market-brief-secret-2026")
        as_of_label = f"{now_kst.strftime('%Y-%m-%d %H:%M')} KST"

        builder = BriefingBuilder()
        briefing = builder.build(
            quotes,
            kind=kind,
            as_of_label=as_of_label,
            report_url=report_url,
            news=news,
        )

        # 4. Render HTML Report
        report_dir = self.root_dir / "reports"
        report_dir.mkdir(parents=True, exist_ok=True)
        report_path = report_dir / f"{report_id}.html"
        renderer = HTMLReportRenderer()
        renderer.render(briefing, quotes, news, output_path=report_path)

        # Atomic replace for latest.html
        latest_tmp = report_dir / "latest.html.tmp"
        latest_target = report_dir / "latest.html"
        try:
            renderer.render(briefing, quotes, news, output_path=latest_tmp)
            latest_tmp.replace(latest_target)
        except Exception as e:
            logger.warning(f"[Scheduler] Failed to atomic-update latest.html: {e}")

        # 5. Dispatch via SafeTelegramDispatcher
        tg = TelegramChannel(token, include_inline_button=True, use_html=True)
        dispatcher = SafeTelegramDispatcher(rate_limiter=TelegramRateLimiter(max_global_rate=25.0, min_chat_interval=1.0))

        success_count = 0
        fail_count = 0
        unknown_count = 0

        for r in recipients:
            status, msg_id, err_detail = dispatcher.send_to_recipient(tg, r, briefing, db=db)
            db.record_recipient_result(job_id, r.chat_id, status, provider_msg_id=msg_id, error_detail=err_detail)
            if status == "SENT":
                success_count += 1
            elif status == "UNKNOWN":
                unknown_count += 1
            else:
                fail_count += 1

        return success_count, fail_count, unknown_count, f"reports/{report_id}.html"


class InternalBriefingScheduler:
    """Asyncio background daemon scheduler running inside FastAPI lifespan."""

    MORNING_START = time(7, 50)
    MORNING_WINDOW_END = time(8, 5)   # Strict 15-minute start window
    MORNING_CUTOFF = time(8, 50)      # Hard expiration

    CLOSING_START = time(15, 40)
    CLOSING_WINDOW_END = time(15, 55)  # Strict 15-minute start window
    CLOSING_CUTOFF = time(18, 0)       # Hard expiration

    URGENT_MARKET_OPEN = time(9, 0)
    URGENT_MARKET_CLOSE = time(15, 30)
    URGENT_CHECK_INTERVAL_SEC = 900.0  # 15 minutes

    def __init__(
        self,
        db: MarketBriefDatabase,
        sub_engine: SubscriptionEngine,
        root_dir: Path,
        get_bot_token: Callable[[], str],
        get_report_base_url: Callable[[], str],
        calendar: Optional[MarketCalendar] = None,
        deliverer: Optional[Any] = None,
        poll_interval: float = 20.0,
        watch_evaluator: Optional[Any] = None,
        watch_interval: float = WATCH_TICK_INTERVAL_SEC,
    ):
        self.db = db
        self.sub_engine = sub_engine
        self.root_dir = root_dir
        self.get_bot_token = get_bot_token
        self.get_report_base_url = get_report_base_url
        self.calendar = calendar or MarketCalendar()
        self.deliverer = deliverer or MarketBriefingDeliverer(root_dir, get_report_base_url)
        self.poll_interval = poll_interval
        self.alert_store = AlertStore(root_dir / "alerts.sqlite3")

        self.runner_id = f"oracle_scheduler_{os.getpid()}_{uuid.uuid4().hex[:8]}"
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self._lock = threading.Lock()

        # Operational status tracking
        self._has_crashed = False
        self._last_tick_kst: Optional[str] = None
        self._last_run_kind: Optional[str] = None
        self._last_run_time_kst: Optional[str] = None
        self._last_run_status: Optional[str] = None
        self._last_error: Optional[str] = None
        self._retry_counts: Dict[str, int] = {}
        self._last_urgent_check: Optional[datetime] = None

        # User-configured intraday volatility target watches (single dedicated loop, all sessions)
        self.watch_evaluator = watch_evaluator
        self.watch_interval = watch_interval
        self._watch_task: Optional[asyncio.Task] = None
        self._last_watch_tick_kst: Optional[str] = None
        self._last_watch_result: Optional[str] = None

    def is_enabled(self) -> bool:
        env_val = os.environ.get("ENABLE_BRIEFING_SCHEDULER", "true").strip().lower()
        return env_val in ("1", "true", "yes", "on")

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._has_crashed = False
        self._stop_event.clear()
        self._task = asyncio.create_task(self._main_loop(), name="oracle_briefing_scheduler")
        self._watch_task = asyncio.create_task(self._watch_loop(), name="oracle_market_watch_loop")
        logger.info(f"⏰ [BriefingScheduler] Started internal daemon (runner_id={self.runner_id})")

    async def stop(self, timeout: float = 10.0) -> None:
        if not self._running:
            return
        logger.info("🛑 [BriefingScheduler] Stopping internal daemon...")
        self._running = False
        self._stop_event.set()
        for task in (self._task, self._watch_task):
            if task:
                try:
                    await asyncio.wait_for(task, timeout=timeout)
                except (asyncio.TimeoutError, asyncio.CancelledError):
                    task.cancel()
        logger.info("✅ [BriefingScheduler] Daemon stopped.")

    # ------------------------------------------------------------------
    # User-configured intraday volatility target watches
    # ------------------------------------------------------------------

    async def _watch_loop(self) -> None:
        """Single dedicated loop (every `watch_interval`s) evaluating KR/US/GLOBAL watches on weekdays.
        One loop for all sessions so a reading is never counted twice by parallel per-market loops."""
        while self._running:
            try:
                now_kst = datetime.now(KST)
                if self.is_enabled():
                    await self._check_market_watches(now_kst)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"⚠️ [MarketWatch] Error in watch tick: {exc}", exc_info=True)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.watch_interval)
                break
            except asyncio.TimeoutError:
                continue

    async def _check_market_watches(self, now_kst: datetime) -> None:
        sessions_open = any(self.calendar.get_session_info(m, now_kst).is_open for m in ("KR", "US", "GLOBAL"))
        if not sessions_open:
            return
        await asyncio.to_thread(self._run_watch_tick_sync, now_kst)

    def _run_watch_tick_sync(self, now_kst: datetime) -> Optional[Any]:
        if self.db.count_active_market_watches() == 0:
            return None
        evaluator = self.watch_evaluator
        if evaluator is None:
            token = self.get_bot_token()
            if not token:
                logger.warning("[MarketWatch] brief_bot token missing; watch tick skipped")
                return None
            from market_brief.market_watch import TelegramWatchSender

            evaluator = MarketWatchEvaluator(
                self.db,
                calendar=self.calendar,
                sender=TelegramWatchSender(token),
                runner_id=f"{self.runner_id}_mw",
            )
        result = evaluator.run_once(now_kst)
        with self._lock:
            self._last_watch_tick_kst = now_kst.strftime("%Y-%m-%d %H:%M:%S")
            self._last_watch_result = (
                f"evaluated={result.evaluated} fired={result.fired} sent={result.sent} "
                f"failed={result.failed} unknown={result.unknown} {result.skipped_reason}".strip()
            )
        if result.fired:
            logger.info(f"🔔 [MarketWatch] {self._last_watch_result}")
        return result

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            next_dt = self.calculate_next_trigger_kst()
            return {
                "is_running": self._running,
                "is_enabled": self.is_enabled(),
                "has_crashed": self._has_crashed,
                "runner_id": self.runner_id,
                "poll_interval_sec": self.poll_interval,
                "last_tick_kst": self._last_tick_kst,
                "last_run": {
                    "kind": self._last_run_kind,
                    "time_kst": self._last_run_time_kst,
                    "status": self._last_run_status,
                } if self._last_run_kind else None,
                "last_error": self._last_error,
                "next_scheduled_kst": next_dt.strftime("%Y-%m-%d %H:%M:%S KST") if next_dt else None,
                "legacy_urgent_broadcast_enabled": legacy_urgent_broadcast_enabled(),
                "market_watch": {
                    "interval_sec": self.watch_interval,
                    "last_tick_kst": self._last_watch_tick_kst,
                    "last_result": self._last_watch_result,
                },
            }

    def calculate_next_trigger_kst(self, from_kst: Optional[datetime] = None) -> Optional[datetime]:
        """Calculates next regular briefing timestamp in KST."""
        now_kst = from_kst or datetime.now(KST)
        target_date = now_kst.date()

        for _ in range(14):  # Check up to 14 days ahead
            if self.calendar.is_trading_day("KR", target_date):
                m_time = datetime.combine(target_date, self.MORNING_START, tzinfo=KST)
                if now_kst < m_time:
                    return m_time
                c_time = datetime.combine(target_date, self.CLOSING_START, tzinfo=KST)
                if now_kst < c_time:
                    return c_time
            target_date += timedelta(days=1)
        return None

    get_next_scheduled_kst = calculate_next_trigger_kst

    def is_briefing_already_sent(self, kind: str, target_date: date) -> bool:
        """Dual-DB Cross Check: Returns True if marked sent in EITHER portal.sqlite3 OR alerts.sqlite3."""
        try:
            if self.db.is_regular_briefing_sent(kind, target_date):
                return True
        except Exception as e:
            logger.warning(f"Error checking portal.sqlite3 for {kind}: {e}")

        try:
            if self.alert_store.is_regular_briefing_sent(kind, target_date):
                return True
        except Exception as e:
            logger.warning(f"Error checking alerts.sqlite3 for {kind}: {e}")

        return False

    async def _main_loop(self) -> None:
        logger.info(f"🚀 [BriefingScheduler] Main loop running. Next check every {self.poll_interval}s.")
        while self._running:
            try:
                now_kst = datetime.now(KST)
                with self._lock:
                    self._last_tick_kst = now_kst.strftime("%Y-%m-%d %H:%M:%S")

                if self.is_enabled():
                    await self._check_and_trigger(now_kst)
                    await self._check_urgent_volatility(now_kst)

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"⚠️ [BriefingScheduler] Error in tick: {exc}", exc_info=True)
                with self._lock:
                    self._last_error = str(exc)

            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.poll_interval)
                break
            except asyncio.TimeoutError:
                continue

    async def _check_and_trigger(self, now_kst: datetime) -> None:
        today = now_kst.date()
        if not self.calendar.is_trading_day("KR", today):
            return

        current_time = now_kst.time()

        # Morning Briefing: Only trigger within strict 15-minute start window (07:50 ~ 08:05 KST)
        if self.MORNING_START <= current_time <= self.MORNING_WINDOW_END:
            if not await asyncio.to_thread(self.is_briefing_already_sent, "morning", today):
                await self._run_briefing_job("morning", now_kst)
        elif self.MORNING_WINDOW_END < current_time <= self.MORNING_CUTOFF:
            # Server restart after 08:05: strictly block compensatory auto-redelivery
            pass

        # Closing Briefing: Only trigger within strict 15-minute start window (15:40 ~ 15:55 KST)
        elif self.CLOSING_START <= current_time <= self.CLOSING_WINDOW_END:
            if not await asyncio.to_thread(self.is_briefing_already_sent, "closing", today):
                await self._run_briefing_job("closing", now_kst)
        elif self.CLOSING_WINDOW_END < current_time <= self.CLOSING_CUTOFF:
            # Server restart after 15:55: strictly block compensatory auto-redelivery
            pass

    async def _check_urgent_volatility(self, now_kst: datetime) -> None:
        """Legacy global ±3.0% FINANCE broadcast (KR regular session). OFF unless LEGACY_URGENT_BROADCAST_ENABLED."""
        if not legacy_urgent_broadcast_enabled():
            return
        today = now_kst.date()
        if not self.calendar.is_trading_day("KR", today):
            return

        current_time = now_kst.time()
        if not (self.URGENT_MARKET_OPEN <= current_time <= self.URGENT_MARKET_CLOSE):
            return

        # Throttle to every URGENT_CHECK_INTERVAL_SEC (15 mins)
        if self._last_urgent_check is not None:
            elapsed = (now_kst - self._last_urgent_check).total_seconds()
            if elapsed < (self.URGENT_CHECK_INTERVAL_SEC - 30.0):
                return

        self._last_urgent_check = now_kst
        logger.info(f"🔍 [BriefingScheduler] Running in-process ±3% urgent volatility check at {now_kst.strftime('%H:%M:%S')} KST")

        try:
            await asyncio.to_thread(self._run_urgent_check_sync, now_kst)
        except Exception as e:
            logger.error(f"Error during in-process urgent volatility check: {e}", exc_info=True)

    def _build_legacy_urgent_pipeline(self, token: str) -> MarketBriefingPipeline:
        from market_brief.channels.telegram import TelegramChannel
        from market_brief.gateway import DeliveryGateway
        from market_brief.market_data import CompositeMarketDataProvider, FixtureProvider, NaverFinanceProvider, QuoteStore

        provider = CompositeMarketDataProvider(
            NaverFinanceProvider(), FixtureProvider(str(self.root_dir / "fixtures/market_snapshot.json"))
        )
        tg = TelegramChannel(token, include_inline_button=True, use_html=True)
        gateway = DeliveryGateway(
            tg,
            primary_name="telegram",
            fallback_channel=None,
            alert_store=self.alert_store,
            max_retries=2,
            backoff_seconds=0.5,
        )
        return MarketBriefingPipeline(
            provider=provider,
            gateway=gateway,
            quote_store=QuoteStore(str(self.root_dir / "quotes.sqlite3")),
            alert_store=self.alert_store,
            calendar=self.calendar,
            report_dir=self.root_dir / "reports",
            base_report_url=self.get_report_base_url(),
        )

    def _run_urgent_check_sync(self, now_kst: datetime) -> None:
        if not legacy_urgent_broadcast_enabled():
            return
        token = self.get_bot_token()
        if not token:
            return

        pipeline = self._build_legacy_urgent_pipeline(token)

        quotes = pipeline.provider.fetch(DEFAULT_SYMBOLS)
        recipients = [r.chat_id for r in self.db.get_active_recipients(topic="FINANCE")]
        if not recipients:
            admin_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
            if admin_chat:
                recipients = [c.strip() for c in admin_chat.split(",") if c.strip()]

        if not recipients:
            return

        triggered = pipeline.check_and_send_urgent_alerts(quotes, recipients, now=now_kst)
        if triggered:
            for q, _, r_path in triggered:
                logger.info(f"🚨 [BriefingScheduler] ±3% sudden breach alert dispatched: {q.symbol} -> {r_path}")

    async def _run_briefing_job(self, kind: str, now_kst: datetime) -> None:
        today_str = now_kst.date().isoformat()
        batch_key = f"REGULAR:{today_str}:{kind}"

        # 1. Check retry limit
        current_retries = self._retry_counts.get(batch_key, 0)
        if current_retries >= 3:
            return

        # 2. Acquire atomic batch lease
        acquired, reason, job = await asyncio.to_thread(
            self.db.acquire_batch_lease,
            batch_key=batch_key,
            topic="FINANCE",
            kind=kind,
            scheduled_date=today_str,
            runner_id=self.runner_id,
            is_forced=False,
        )

        if not acquired:
            if reason != "ALREADY_COMPLETED":
                logger.debug(f"[Scheduler] Lease acquisition skipped for {batch_key}: {reason}")
            return

        logger.info(f"🎯 [Scheduler] Acquired batch lease for {batch_key} (Job ID: {job.job_id})")

        # 3. Check bot token
        token = self.get_bot_token()
        if not token:
            logger.error(f"[Scheduler] No bot token available for {batch_key}")
            await asyncio.to_thread(self.db.fail_job, job.job_id, "NO_BOT_TOKEN")
            self._record_run_result(kind, now_kst, "FAILED: NO_BOT_TOKEN", "Bot token missing")
            return

        # 4. Fetch active recipients
        recipients = await asyncio.to_thread(self.db.get_active_recipients, "FINANCE")
        if not recipients:
            admin_chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
            if admin_chat:
                recipients = [Recipient(chat_id=c.strip(), name="Admin", subscribed_topics="FINANCE") for c in admin_chat.split(",") if c.strip()]

        if not recipients:
            logger.warning(f"[Scheduler] No active FINANCE recipients for {batch_key}")
            await asyncio.to_thread(self.db.fail_job, job.job_id, "NO_ACTIVE_RECIPIENTS")
            self._record_run_result(kind, now_kst, "FAILED: NO_RECIPIENTS", "Zero active recipients")
            return

        await asyncio.to_thread(self.db.snapshot_job_recipients, job.job_id, recipients)

        # 5. Check cutoff again right before delivery
        cutoff = self.MORNING_CUTOFF if kind == "morning" else self.CLOSING_CUTOFF
        if now_kst.time() > cutoff:
            logger.warning(f"[Scheduler] Hard cutoff exceeded before delivery for {batch_key}")
            await asyncio.to_thread(self.db.fail_job, job.job_id, "CUTOFF_EXPIRED")
            self._record_run_result(kind, now_kst, "EXPIRED", "Hard cutoff exceeded")
            return

        # 6. Execute delivery via worker thread
        try:
            success, fail, unknown, report_rel_path = await asyncio.to_thread(
                self.deliverer.deliver,
                self.db,
                job.job_id,
                token,
                recipients,
                kind,
                now_kst,
            )

            # Determine final status
            if success > 0 and fail == 0 and unknown == 0:
                final_status = JobFinalStatus.SENT
            elif success > 0:
                final_status = JobFinalStatus.PARTIAL
            elif unknown > 0:
                final_status = JobFinalStatus.UNKNOWN
            else:
                final_status = JobFinalStatus.FAILED

            await asyncio.to_thread(
                self.db.complete_job,
                job_id=job.job_id,
                status=final_status,
                total=len(recipients),
                success=success,
                fail=fail,
                unknown=unknown,
                report_path=report_rel_path,
            )

            # Sync legacy alert_reservations for backward compatibility & Dual-DB consistency
            if success > 0:
                await asyncio.to_thread(self.db.reserve_regular_briefing, kind, now_kst.date())
                try:
                    self.alert_store.reserve_regular_briefing(kind, now_kst.date())
                except Exception as exc:
                    logger.warning(f"Failed to record briefing to AlertStore: {exc}")

            status_str = f"{final_status.value} (Sent: {success}/{len(recipients)}, Fail: {fail}, Unknown: {unknown})"
            logger.info(f"🚀 [Scheduler] Completed dispatch for {batch_key}: {status_str}")
            self._record_run_result(kind, now_kst, status_str, None)

            # Retry handling
            if final_status == JobFinalStatus.FAILED and unknown == 0:
                self._retry_counts[batch_key] = current_retries + 1
            else:
                self._retry_counts.pop(batch_key, None)

        except Exception as exc:
            logger.error(f"❌ [Scheduler] Delivery error for {batch_key}: {exc}", exc_info=True)
            await asyncio.to_thread(self.db.fail_job, job.job_id, f"DELIVERY_EXCEPTION: {exc}")
            self._record_run_result(kind, now_kst, "FAILED: EXCEPTION", str(exc))
            self._retry_counts[batch_key] = current_retries + 1

    def _record_run_result(self, kind: str, now_kst: datetime, status: str, error: Optional[str]) -> None:
        with self._lock:
            self._last_run_kind = kind
            self._last_run_time_kst = now_kst.strftime("%Y-%m-%d %H:%M:%S")
            self._last_run_status = status
            if error:
                self._last_error = error
