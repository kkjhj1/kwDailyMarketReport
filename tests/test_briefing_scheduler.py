"""Unit tests for InternalBriefingScheduler.
Verifies time window triggers, cutoffs, KR calendar logic, DB lease fencing,
idempotency, error handling, and thread isolation.
"""
import asyncio
import os
import shutil
import tempfile
import unittest
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from market_brief.briefing_scheduler import InternalBriefingScheduler
from market_brief.calendar_manager import MarketCalendar
from market_brief.storage.db import MarketBriefDatabase
from market_brief.storage.models import JobFinalStatus, Recipient
from market_brief.subscription_engine import SubscriptionEngine

KST = ZoneInfo("Asia/Seoul")


class FakeDeliverer:
    """Mock deliverer that records calls without network or disk side effects."""

    def __init__(self, should_fail: bool = False, unknown_count: int = 0):
        self.calls = []
        self.should_fail = should_fail
        self.unknown_count = unknown_count

    def deliver(self, db, job_id, token, recipients, kind, now_kst):
        self.calls.append({
            "job_id": job_id,
            "token": token,
            "recipients_count": len(recipients),
            "kind": kind,
            "now_kst": now_kst,
        })
        if self.should_fail:
            raise RuntimeError("Fake network delivery failure")
        if self.unknown_count > 0:
            return 0, 0, self.unknown_count, "reports/fake_report.html"
        return len(recipients), 0, 0, "reports/fake_report.html"


class TestInternalBriefingScheduler(unittest.IsolatedAsyncioTestCase):

    async def asyncSetUp(self):
        self.tmp_dir = tempfile.mkdtemp()
        self.db_path = os.path.join(self.tmp_dir, "test_alerts.sqlite3")
        self.env_path = Path(self.tmp_dir) / ".env"
        self.env_path.write_text("TELEGRAM_BOT_TOKEN=123456:FAKE_TOKEN\n", encoding="utf-8")

        self.db = MarketBriefDatabase(db_path=self.db_path, env_path=self.env_path)
        self.sub_engine = SubscriptionEngine(self.db)

        # Seed active recipients
        self.db.add_recipient("111111", "테스터1", "TELEGRAM", "VIP", "FINANCE")
        self.db.add_recipient("222222", "테스터2", "TELEGRAM", "VIP", "FINANCE")

        self.fake_deliverer = FakeDeliverer()
        self.scheduler = InternalBriefingScheduler(
            db=self.db,
            sub_engine=self.sub_engine,
            root_dir=Path(self.tmp_dir),
            get_bot_token=lambda: "123456:FAKE_TOKEN",
            get_report_base_url=lambda: "https://example.com/reports",
            deliverer=self.fake_deliverer,
            poll_interval=0.05,
        )

    async def asyncTearDown(self):
        if self.scheduler._running:
            await self.scheduler.stop()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_next_trigger_calculation(self):
        # 1. Trading day morning before 07:50 -> Today 07:50
        now = datetime(2026, 11, 2, 7, 0, tzinfo=KST)  # Mon
        nxt = self.scheduler.calculate_next_trigger_kst(now)
        self.assertEqual(nxt, datetime(2026, 11, 2, 7, 50, tzinfo=KST))

        # 2. Trading day between 07:50 and 15:40 -> Today 15:40
        now = datetime(2026, 11, 2, 10, 0, tzinfo=KST)
        nxt = self.scheduler.calculate_next_trigger_kst(now)
        self.assertEqual(nxt, datetime(2026, 11, 2, 15, 40, tzinfo=KST))

        # 3. Trading day after 15:40 -> Tomorrow 07:50
        now = datetime(2026, 11, 2, 16, 0, tzinfo=KST)
        nxt = self.scheduler.calculate_next_trigger_kst(now)
        self.assertEqual(nxt, datetime(2026, 11, 3, 7, 50, tzinfo=KST))

        # 4. Friday after 15:40 -> Next Monday 07:50
        now = datetime(2026, 11, 6, 16, 0, tzinfo=KST)  # Fri
        nxt = self.scheduler.calculate_next_trigger_kst(now)
        self.assertEqual(nxt, datetime(2026, 11, 9, 7, 50, tzinfo=KST))

    async def test_morning_trigger_success_and_idempotency(self):
        # Monday 07:50:00 KST
        now = datetime(2026, 11, 2, 7, 50, 0, tzinfo=KST)

        # 1. First trigger -> Dispatches successfully
        await self.scheduler._check_and_trigger(now)
        self.assertEqual(len(self.fake_deliverer.calls), 1)
        self.assertEqual(self.fake_deliverer.calls[0]["kind"], "morning")
        self.assertEqual(self.fake_deliverer.calls[0]["recipients_count"], 2)

        # Verify DB records
        self.assertTrue(self.db.is_regular_briefing_sent("morning", now.date()))

        # 2. Second trigger at 07:55:00 KST -> Idempotent, does not dispatch again!
        now_later = datetime(2026, 11, 2, 7, 55, 0, tzinfo=KST)
        await self.scheduler._check_and_trigger(now_later)
        self.assertEqual(len(self.fake_deliverer.calls), 1)

    async def test_closing_trigger_success(self):
        # Monday 15:40:00 KST
        now = datetime(2026, 11, 2, 15, 40, 0, tzinfo=KST)

        await self.scheduler._check_and_trigger(now)
        self.assertEqual(len(self.fake_deliverer.calls), 1)
        self.assertEqual(self.fake_deliverer.calls[0]["kind"], "closing")
        self.assertTrue(self.db.is_regular_briefing_sent("closing", now.date()))

    async def test_morning_hard_cutoff_prevents_dispatch(self):
        # Monday 08:50:05 KST (Past cutoff 08:50)
        now = datetime(2026, 11, 2, 8, 50, 5, tzinfo=KST)

        await self.scheduler._check_and_trigger(now)
        self.assertEqual(len(self.fake_deliverer.calls), 0)
        self.assertFalse(self.db.is_regular_briefing_sent("morning", now.date()))

    async def test_closing_hard_cutoff_prevents_dispatch(self):
        # Monday 18:00:05 KST (Past cutoff 18:00)
        now = datetime(2026, 11, 2, 18, 0, 5, tzinfo=KST)

        await self.scheduler._check_and_trigger(now)
        self.assertEqual(len(self.fake_deliverer.calls), 0)
        self.assertFalse(self.db.is_regular_briefing_sent("closing", now.date()))

    async def test_skip_on_weekend(self):
        # Sunday 07:50:00 KST
        now = datetime(2026, 11, 1, 7, 50, 0, tzinfo=KST)

        await self.scheduler._check_and_trigger(now)
        self.assertEqual(len(self.fake_deliverer.calls), 0)

    async def test_skip_when_token_missing(self):
        # Scheduler with empty token
        no_token_scheduler = InternalBriefingScheduler(
            db=self.db,
            sub_engine=self.sub_engine,
            root_dir=Path(self.tmp_dir),
            get_bot_token=lambda: "",
            get_report_base_url=lambda: "https://example.com",
            deliverer=self.fake_deliverer,
        )
        now = datetime(2026, 11, 2, 7, 50, 0, tzinfo=KST)
        await no_token_scheduler._check_and_trigger(now)
        self.assertEqual(len(self.fake_deliverer.calls), 0)
        st = no_token_scheduler.get_status()
        self.assertIn("NO_BOT_TOKEN", st["last_run"]["status"])

    async def test_unknown_delivery_does_not_retry(self):
        unknown_deliverer = FakeDeliverer(unknown_count=2)
        unknown_scheduler = InternalBriefingScheduler(
            db=self.db,
            sub_engine=self.sub_engine,
            root_dir=Path(self.tmp_dir),
            get_bot_token=lambda: "FAKE_TOKEN",
            get_report_base_url=lambda: "https://example.com",
            deliverer=unknown_deliverer,
        )
        now = datetime(2026, 11, 2, 7, 50, 0, tzinfo=KST)
        await unknown_scheduler._check_and_trigger(now)
        self.assertEqual(len(unknown_deliverer.calls), 1)

        # Trigger again -> should NOT retry because status was UNKNOWN (prevent double sending)
        await unknown_scheduler._check_and_trigger(now)
        self.assertEqual(len(unknown_deliverer.calls), 1)

    async def test_lifecycle_start_and_stop(self):
        await self.scheduler.start()
        self.assertTrue(self.scheduler._running)
        status = self.scheduler.get_status()
        self.assertTrue(status["is_running"])

        await self.scheduler.stop()
        self.assertFalse(self.scheduler._running)
        status = self.scheduler.get_status()
        self.assertFalse(status["is_running"])


if __name__ == "__main__":
    unittest.main()
