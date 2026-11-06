"""Internal Scheduled Dispatcher for Daily Market Brief v2.0.

Runs as a 24/7 background asyncio task inside FastAPI (Render Web Service).
Fires morning briefing at 06:30 KST and closing briefing at 15:40 KST on KR trading days.
All blocking synchronous I/O (DB, HTTP quotes, HTML render, Telegram dispatcher)
is isolated to worker threads via asyncio.to_thread.
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

from market_brief.calendar_manager import MarketCalendar
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

    MORNING_START = time(6, 30)
    MORNING_CUTOFF = time(8, 50)
    CLOSING_START = time(15, 40)
    CLOSING_CUTOFF = time(18, 0)

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
    ):
        self.db = db
        self.sub_engine = sub_engine
        self.root_dir = root_dir
        self.get_bot_token = get_bot_token
        self.get_report_base_url = get_report_base_url
        self.calendar = calendar or MarketCalendar()
        self.deliverer = deliverer or MarketBriefingDeliverer(root_dir, get_report_base_url)
        self.poll_interval = poll_interval

        self.runner_id = f"render_scheduler_{os.getpid()}_{uuid.uuid4().hex[:8]}"
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

    def is_enabled(self) -> bool:
        env_val = os.environ.get("ENABLE_BRIEFING_SCHEDULER", "true").strip().lower()
        return env_val in ("1", "true", "yes", "on")

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._has_crashed = False
        self._stop_event.clear()
        self._task = asyncio.create_task(self._main_loop(), name="render_briefing_scheduler")
        logger.info(f"⏰ [BriefingScheduler] Started internal daemon (runner_id={self.runner_id})")

    async def stop(self, timeout: float = 10.0) -> None:
        if not self._running:
            return
        logger.info("🛑 [BriefingScheduler] Stopping internal daemon...")
        self._running = False
        self._stop_event.set()
        if self._task:
            try:
                await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
                try:
                    await self._task
                except (asyncio.CancelledError, Exception):
                    pass
            self._task = None
        logger.info("✅ [BriefingScheduler] Stopped successfully.")

    def get_status(self) -> Dict[str, Any]:
        with self._lock:
            now_kst = datetime.now(KST)
            next_trigger = self.calculate_next_trigger_kst(now_kst)
            return {
                "is_running": self._running and (self._task is not None and not self._task.done()),
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
                "next_scheduled_kst": next_trigger.strftime("%Y-%m-%d %H:%M:%S KST") if next_trigger else None,
            }

    def calculate_next_trigger_kst(self, now_kst: datetime) -> Optional[datetime]:
        """Calculates exact timestamp for next scheduled briefing (06:30 or 15:40 on KR trading day)."""
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

    async def _main_loop(self) -> None:
        logger.info(f"🚀 [BriefingScheduler] Main loop running. Next check every {self.poll_interval}s.")
        while self._running:
            try:
                now_kst = datetime.now(KST)
                with self._lock:
                    self._last_tick_kst = now_kst.strftime("%Y-%m-%d %H:%M:%S")

                if self.is_enabled():
                    await self._check_and_trigger(now_kst)

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

        # Morning Briefing Window (06:30 ~ 08:50 KST)
        if self.MORNING_START <= current_time <= self.MORNING_CUTOFF:
            if not await asyncio.to_thread(self.db.is_regular_briefing_sent, "morning", today):
                await self._run_briefing_job("morning", now_kst)

        # Closing Briefing Window (15:40 ~ 18:00 KST)
        elif self.CLOSING_START <= current_time <= self.CLOSING_CUTOFF:
            if not await asyncio.to_thread(self.db.is_regular_briefing_sent, "closing", today):
                await self._run_briefing_job("closing", now_kst)

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
                db=self.db,
                job_id=job.job_id,
                token=token,
                recipients=recipients,
                kind=kind,
                now_kst=now_kst,
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

            # Sync legacy alert_reservations for backward compatibility
            if success > 0:
                await asyncio.to_thread(self.db.reserve_regular_briefing, kind, now_kst.date())

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
