"""Comprehensive Test Suite for Tier Upgrade Requests & Admin Console Approval Workflow.
Verifies:
1. Menu Bot Telegram Callback records upgrade requests into alerts.sqlite3 database.
2. SubscriptionEngine processes pending requests, updates user tiers, and handles approval/rejection.
3. Admin Console REST APIs (/api/platform/upgrade-requests) provide 1-click approval.
4. User receives Telegram notification when admin approves/rejects on admin dashboard.
"""
from __future__ import annotations
import gc
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from admin_server import create_admin_app
from market_brief.storage.db import MarketBriefDatabase
from market_brief.subscription_engine import SubscriptionEngine
from portal_hub import (
    PortalDatabase,
    TelegramPortalClient,
    handle_callback_query,
)


class UpgradeRequestsWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_alerts.sqlite3"
        self.env_path = Path(self.tmp_dir.name) / "test.env"

        # Initialize databases
        self.mb_db = MarketBriefDatabase(db_path=str(self.db_path), env_path=self.env_path)
        self.sub_engine = SubscriptionEngine(self.mb_db)
        self.portal_db = PortalDatabase(self.db_path)

        # Admin FastAPI Test Client
        self.app = create_admin_app(db_path=str(self.db_path), env_path=self.env_path)
        self.client = TestClient(self.app)

        # Mock Telegram Client
        self.tg_client = MagicMock(spec=TelegramPortalClient)
        self.tg_client.send_message.return_value = {"ok": True, "result": {"message_id": 999}}
        self.tg_client.edit_message_text.return_value = True
        self.tg_client.answer_callback_query.return_value = True

        self.user_id = 7820975792
        self.user_profile = self.portal_db.get_or_create_user(
            user_id=self.user_id,
            chat_id=self.user_id,
            username="kw_tester",
            first_name="테스터조",
        )

    def tearDown(self):
        del self.sub_engine
        del self.portal_db
        del self.mb_db
        gc.collect()
        try:
            self.tmp_dir.cleanup()
        except PermissionError:
            pass

    def test_database_request_tier_upgrade(self):
        """PortalDatabase.request_tier_upgrade records new and updates pending requests."""
        # 1. First request: GREEN
        req_id, is_updated = self.portal_db.request_tier_upgrade(self.user_id, "GREEN", memo="첫 신청")
        self.assertFalse(is_updated)
        self.assertGreater(req_id, 0)

        pending = self.portal_db.get_pending_upgrade_requests()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["requested_tier"], "GREEN")
        self.assertEqual(pending[0]["current_tier"], "WHITE")
        self.assertEqual(pending[0]["telegram_user_id"], str(self.user_id))

        # 2. Second request before review: RED (should update existing pending request)
        req_id2, is_updated2 = self.portal_db.request_tier_upgrade(self.user_id, "RED", memo="레드로 변경")
        self.assertTrue(is_updated2)
        self.assertEqual(req_id2, req_id)

        pending_after = self.portal_db.get_pending_upgrade_requests()
        self.assertEqual(len(pending_after), 1)
        self.assertEqual(pending_after[0]["requested_tier"], "RED")

    def test_menu_bot_callback_records_upgrade_request_in_db(self):
        """When user clicks support:req_tier:GREEN in Menu Bot, it records to DB and updates UI in-place."""
        cq = {
            "id": "cq_tier_test",
            "from": {"id": self.user_id, "first_name": "테스터조", "username": "kw_tester"},
            "message": {"message_id": 123, "chat": {"id": self.user_id}},
            "data": "support:req_tier:GREEN",
        }
        handle_callback_query(cq, self.portal_db, self.tg_client)

        # 1. Verified DB record exists for admin console queue
        pending = self.portal_db.get_pending_upgrade_requests()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["requested_tier"], "GREEN")
        self.assertEqual(pending[0]["telegram_user_id"], str(self.user_id))

        # 2. Verified user screen was edited in-place with confirmation notice (no spam message)
        self.tg_client.edit_message_text.assert_called_once()
        user_text = self.tg_client.edit_message_text.call_args[0][2]
        self.assertIn("등급 업그레이드 신청 완료", user_text)
        self.assertIn("GREEN", user_text)

    def test_admin_api_list_and_approve_upgrade_request(self):
        """Admin API GET lists requests, and POST approve updates user tier and returns success."""
        # 1. Create request
        req_id, _ = self.portal_db.request_tier_upgrade(self.user_id, "GREEN")

        # 2. Admin GET /api/platform/upgrade-requests
        res_list = self.client.get("/api/platform/upgrade-requests")
        self.assertEqual(res_list.status_code, 200)
        requests = res_list.json()["requests"]
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["id"], req_id)
        self.assertEqual(requests[0]["requested_tier"], "GREEN")
        self.assertEqual(requests[0]["status"], "PENDING")

        # 3. Admin POST /api/platform/upgrade-requests/{id}/approve
        with patch.object(SubscriptionEngine, "_notify_user_upgrade_result") as mock_notify:
            res_approve = self.client.post(f"/api/platform/upgrade-requests/{req_id}/approve")
            self.assertEqual(res_approve.status_code, 200)
            data = res_approve.json()
            self.assertTrue(data["success"])
            self.assertEqual(data["new_tier"], "GREEN")
            mock_notify.assert_called_once_with(str(self.user_id), "GREEN", approved=True)

        # 4. Verify user in database is now GREEN tier with max 3 slots
        user_in_db = self.sub_engine.get_user_profile(str(self.user_id))
        self.assertEqual(user_in_db["tier"], "GREEN")
        self.assertEqual(user_in_db["max_slots"], 3)

        # 5. Verify request status is now APPROVED
        res_list2 = self.client.get("/api/platform/upgrade-requests?status=PENDING")
        self.assertEqual(len(res_list2.json()["requests"]), 0)

        res_list_all = self.client.get("/api/platform/upgrade-requests")
        self.assertEqual(res_list_all.json()["requests"][0]["status"], "APPROVED")

    def test_admin_api_reject_upgrade_request(self):
        """Admin POST /api/platform/upgrade-requests/{id}/reject records rejection."""
        req_id, _ = self.portal_db.request_tier_upgrade(self.user_id, "RED")

        with patch.object(SubscriptionEngine, "_notify_user_upgrade_result") as mock_notify:
            res_reject = self.client.post(
                f"/api/platform/upgrade-requests/{req_id}/reject",
                json={"reason": "추천인 확인 필요"},
            )
            self.assertEqual(res_reject.status_code, 200)
            data = res_reject.json()
            self.assertTrue(data["success"])
            self.assertEqual(data["status"], "REJECTED")
            mock_notify.assert_called_once_with(str(self.user_id), "RED", approved=False, reason="추천인 확인 필요")

        # User tier remains unchanged (WHITE)
        user_in_db = self.sub_engine.get_user_profile(str(self.user_id))
        self.assertEqual(user_in_db["tier"], "WHITE")

    def test_telegram_inline_admin_approval_callback(self):
        """Telegram inline admin approval callback (adm:appr:{id}:{tier}) approves request and notifies user."""
        req_id, _ = self.portal_db.request_tier_upgrade(self.user_id, "GREEN")

        admin_user_id = 99999999
        with patch.dict(
            "os.environ",
            {
                "PORTAL_ADMIN_USER_IDS": str(admin_user_id),
                "PORTAL_TELEGRAM_BOT_TOKEN": "mock_portal_bot_token",
            },
        ), patch("portal_hub.telegram.TelegramPortalClient.send_message") as mock_portal_send:
            cq = {
                "id": "cq_admin_appr",
                "from": {"id": admin_user_id, "first_name": "관리자", "username": "admin_kw"},
                "message": {"message_id": 456, "chat": {"id": admin_user_id}},
                "data": f"adm:appr:{req_id}:GREEN",
            }
            handle_callback_query(cq, self.portal_db, self.tg_client)

            # 1. User tier is upgraded to GREEN
            user_in_db = self.sub_engine.get_user_profile(str(self.user_id))
            self.assertEqual(user_in_db["tier"], "GREEN")

            # 2. Database request is marked APPROVED
            pending = self.portal_db.get_pending_upgrade_requests()
            self.assertEqual(len(pending), 0)

            # 3. Notification message sent to user via engine
            mock_portal_send.assert_called_with(
                str(self.user_id),
                unittest.mock.ANY,
                reply_markup=unittest.mock.ANY,
            )
            user_msg = mock_portal_send.call_args[0][1]
            self.assertIn("GREEN", user_msg)
            self.assertIn("승인", user_msg)

    def test_unauthorized_telegram_admin_attempt_blocked(self):
        """Regular user attempting to forge admin callback (adm:appr) is strictly rejected."""
        req_id, _ = self.portal_db.request_tier_upgrade(self.user_id, "BLACK")

        normal_user_id = self.user_id  # 7820975792
        with patch.dict(
            "os.environ",
            {
                "PORTAL_ADMIN_USER_IDS": "99999999",
                "PORTAL_ADMIN_CHAT_ID": "99999999",
                "PORTAL_TELEGRAM_CHAT_ID": "99999999",
            },
        ):
            cq = {
                "id": "cq_fake_admin",
                "from": {"id": normal_user_id, "first_name": "일반인", "username": "hacker"},
                "message": {"message_id": 999, "chat": {"id": normal_user_id}},
                "data": f"adm:appr:{req_id}:BLACK",
            }
            handle_callback_query(cq, self.portal_db, self.tg_client)

        # 1. Verification alert sent to client
        self.tg_client.answer_callback_query.assert_called_with(
            "cq_fake_admin", "⚠️ 관리자 권한이 없습니다.", show_alert=True
        )

        # 2. User tier is NOT upgraded (remains WHITE)
        user_in_db = self.sub_engine.get_user_profile(str(self.user_id))
        self.assertEqual(user_in_db["tier"], "WHITE")

        # 3. Request remains PENDING
        pending = self.portal_db.get_pending_upgrade_requests()
        self.assertEqual(len(pending), 1)

    def test_telegram_two_step_rejection_workflow(self):
        """Telegram rejection follows 2-step in-place confirmation (rejc -> rejx)."""
        req_id, _ = self.portal_db.request_tier_upgrade(self.user_id, "RED")

        admin_user_id = 99999999
        with patch.dict("os.environ", {"PORTAL_ADMIN_USER_IDS": str(admin_user_id)}):
            # Step 1: Click adm:rejc -> Prompts confirmation in-place
            cq1 = {
                "id": "cq_rejc_step1",
                "from": {"id": admin_user_id, "first_name": "관리자"},
                "message": {"message_id": 457, "chat": {"id": admin_user_id}},
                "data": f"adm:rejc:{req_id}:RED",
            }
            handle_callback_query(cq1, self.portal_db, self.tg_client)

            # Verification: still PENDING
            pending1 = self.portal_db.get_pending_upgrade_requests()
            self.assertEqual(len(pending1), 1)

            # Step 2: Confirm rejection via adm:rejx
            cq2 = {
                "id": "cq_rejc_step2",
                "from": {"id": admin_user_id, "first_name": "관리자"},
                "message": {"message_id": 457, "chat": {"id": admin_user_id}},
                "data": f"adm:rejx:{req_id}:RED",
            }
            handle_callback_query(cq2, self.portal_db, self.tg_client)

        # 1. User tier remains WHITE
        user_in_db = self.sub_engine.get_user_profile(str(self.user_id))
        self.assertEqual(user_in_db["tier"], "WHITE")

        # 2. Database request is marked REJECTED
        pending2 = self.portal_db.get_pending_upgrade_requests()
        self.assertEqual(len(pending2), 0)

    def test_double_approval_and_rejection_protection(self):
        """Prevent double approval or reviewing already processed requests."""
        req_id, _ = self.portal_db.request_tier_upgrade(self.user_id, "GREEN")

        # First approval succeeds
        res1 = self.sub_engine.approve_upgrade_request(req_id, actor="ADMIN")
        self.assertTrue(res1["success"])

        # Second approval raises ValueError
        with self.assertRaises(ValueError):
            self.sub_engine.approve_upgrade_request(req_id, actor="ADMIN2")

        # Attempt to reject approved request raises ValueError
        with self.assertRaises(ValueError):
            self.sub_engine.reject_upgrade_request(req_id, actor="ADMIN2")

    def test_invalid_tier_and_downgrade_request_rejection(self):
        """PortalDatabase rejects invalid tier names and requests for current or lower tiers."""
        # 1. Invalid tier name
        with self.assertRaises(ValueError):
            self.portal_db.request_tier_upgrade(self.user_id, "SUPER_ADMIN")

        # 2. Upgrade to GREEN succeeds
        self.portal_db.request_tier_upgrade(self.user_id, "GREEN")
        self.sub_engine.approve_upgrade_request(1, actor="ADMIN")

        # 3. User is now GREEN; requesting WHITE or GREEN fails
        with self.assertRaises(ValueError):
            self.portal_db.request_tier_upgrade(self.user_id, "GREEN")

        with self.assertRaises(ValueError):
            self.portal_db.request_tier_upgrade(self.user_id, "WHITE")

    def test_tier_mismatch_on_updated_request_blocked(self):
        """If user updates request from GREEN to BLACK, old GREEN approval button is blocked."""
        req_id, _ = self.portal_db.request_tier_upgrade(self.user_id, "GREEN")
        # User changes mind to BLACK
        self.portal_db.request_tier_upgrade(self.user_id, "BLACK")

        admin_user_id = 99999999
        with patch.dict("os.environ", {"PORTAL_ADMIN_USER_IDS": str(admin_user_id)}):
            # Admin clicks old button that expected GREEN
            cq = {
                "id": "cq_mismatch",
                "from": {"id": admin_user_id, "first_name": "관리자"},
                "message": {"message_id": 458, "chat": {"id": admin_user_id}},
                "data": f"adm:appr:{req_id}:GREEN",
            }
            handle_callback_query(cq, self.portal_db, self.tg_client)

        # User is NOT approved yet because expected_tier mismatch prevented race condition
        user_in_db = self.sub_engine.get_user_profile(str(self.user_id))
        self.assertEqual(user_in_db["tier"], "WHITE")


if __name__ == "__main__":
    unittest.main()
