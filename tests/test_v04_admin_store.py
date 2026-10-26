"""Unit and integration tests for v0.4 Admin Storage, Atomic Lease Locks, and Telegram Limiter."""
import io
import json
import socket
import tempfile
import unittest
import urllib.error
from datetime import date
from pathlib import Path

from market_brief.channels.telegram import TelegramChannel
from market_brief.channels.telegram_limiter import SafeTelegramDispatcher, TelegramRateLimiter
from market_brief.core import AssetSummary, Briefing
from market_brief.storage.db import MarketBriefDatabase
from market_brief.storage.models import BatchStatus, JobFinalStatus, Recipient


class FakeHTTPResponse(io.BytesIO):
    def __enter__(self):
        return self
    def __exit__(self, *_):
        self.close()


class AdminStoreAndLimiterTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_admin.sqlite3"
        self.db = MarketBriefDatabase(db_path=str(self.db_path))

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_atomic_lease_lock_and_concurrency_prevention(self):
        batch_key = "FINANCE:2026-10-26:morning"
        
        # 1. First runner acquires lease
        ok1, reason1, job1 = self.db.acquire_batch_lease(
            batch_key=batch_key,
            topic="FINANCE",
            kind="morning",
            scheduled_date="2026-10-26",
            runner_id="runner_github_actions_1",
            lease_minutes=15,
        )
        self.assertTrue(ok1)
        self.assertEqual(reason1, "LEASE_ACQUIRED")
        self.assertIsNotNone(job1)
        self.assertEqual(job1.attempt_no, 1)

        # 2. Second runner attempts at the exact same time -> BLOCKED!
        ok2, reason2, job2 = self.db.acquire_batch_lease(
            batch_key=batch_key,
            topic="FINANCE",
            kind="morning",
            scheduled_date="2026-10-26",
            runner_id="runner_github_actions_2",
            lease_minutes=15,
        )
        self.assertFalse(ok2)
        self.assertIn("ACTIVE_LEASE_HELD_BY_runner_github_actions_1", reason2)
        self.assertIsNone(job2)

        # 3. First runner completes job
        self.db.complete_job(job1.job_id, JobFinalStatus.SENT, total=5, success=5, fail=0, unknown=0)

        # 4. Third runner attempts after completion -> ALREADY_COMPLETED!
        ok3, reason3, job3 = self.db.acquire_batch_lease(
            batch_key=batch_key,
            topic="FINANCE",
            kind="morning",
            scheduled_date="2026-10-26",
            runner_id="runner_cron_3",
        )
        self.assertFalse(ok3)
        self.assertEqual(reason3, "ALREADY_COMPLETED")

        # 5. Admin executes FORCE re-run with force_reason
        ok_force, reason_force, job_force = self.db.acquire_batch_lease(
            batch_key=batch_key,
            topic="FINANCE",
            kind="morning",
            scheduled_date="2026-10-26",
            runner_id="admin_manual",
            is_forced=True,
            force_reason="시세 데이터 정정으로 인한 재발송",
        )
        self.assertTrue(ok_force)
        self.assertEqual(job_force.attempt_no, 2)
        self.assertTrue(job_force.is_forced)
        self.assertEqual(job_force.force_reason, "시세 데이터 정정으로 인한 재발송")

    def test_recipient_crud_and_snapshotting(self):
        self.db.add_recipient("111", "수신자A", group_tag="VIP", subscribed_topics="FINANCE,REAL_ESTATE")
        self.db.add_recipient("222", "수신자B", group_tag="GENERAL", subscribed_topics="TREND")
        
        finance_recipients = self.db.get_active_recipients(topic="FINANCE")
        self.assertEqual(len(finance_recipients), 1)
        self.assertEqual(finance_recipients[0].chat_id, "111")

        trend_recipients = self.db.get_active_recipients(topic="TREND")
        self.assertEqual(len(trend_recipients), 1)
        self.assertEqual(trend_recipients[0].chat_id, "222")

        # Deactivate
        self.db.deactivate_recipient("111", "TEST_STOP")
        self.assertEqual(len(self.db.get_active_recipients(topic="FINANCE")), 0)

    def test_safe_dispatcher_telegram_error_isolation(self):
        dispatcher = SafeTelegramDispatcher(rate_limiter=TelegramRateLimiter(max_global_rate=1000.0, min_chat_interval=0.0))
        briefing = Briefing(
            "morning", "헤드라인", "06:30 KST", (AssetSummary("국내", "KOSPI", "+0.5%", "상승"),), "https://example.com/r1"
        )

        # 1. 403 Bot Blocked by user -> deactivates recipient in DB
        self.db.add_recipient("999", "차단유저")
        def blocked_opener(req, timeout=10):
            err_json = json.dumps({"ok": False, "error_code": 403, "description": "Forbidden: bot was blocked by the user"}).encode("utf-8")
            fp = io.BytesIO(err_json)
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, fp)

        tg_blocked = TelegramChannel("123:TOKEN", opener=blocked_opener)
        rec = Recipient(chat_id="999", name="차단유저")
        status, msg_id, err = dispatcher.send_to_recipient(tg_blocked, rec, briefing, db=self.db)
        
        self.assertEqual(status, "FAILED")
        self.assertIn("403 Blocked", err)
        # Verify DB is_active is now False!
        rec_in_db = [r for r in self.db.get_all_recipients() if r.chat_id == "999"][0]
        self.assertFalse(rec_in_db.is_active)
        self.assertEqual(rec_in_db.inactivation_reason, "USER_BLOCKED_BOT")

        # 2. Network Timeout -> marks UNKNOWN instead of FAILED
        def timeout_opener(req, timeout=10):
            raise socket.timeout("timed out waiting for response")

        tg_timeout = TelegramChannel("123:TOKEN", opener=timeout_opener)
        rec_normal = Recipient(chat_id="888", name="일반유저")
        status_to, _, err_to = dispatcher.send_to_recipient(tg_timeout, rec_normal, briefing, db=self.db)
        self.assertEqual(status_to, "UNKNOWN")
        self.assertIn("Network Timeout", err_to)


if __name__ == "__main__":
    unittest.main()
