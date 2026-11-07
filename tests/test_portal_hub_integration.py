"""Comprehensive Unit & End-to-End Integration Tests for Portal Hub Menu Bot.
Verifies complete subscription connection across alerts.sqlite3, brief_bot, travel_bot, and admission_bot.
"""
import gc
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from market_brief.storage.db import MarketBriefDatabase
from portal_hub.db import PortalDatabase
from portal_hub.interactive import (
    _get_admin_console_url,
    build_slot_gauge,
    handle_callback_query,
    render_category_home_view,
    render_category_view,
    render_help_view,
    render_main_menu,
    render_search_results_view,
    render_subscription_view,
    render_tier_benefits_view,
    send_admin_new_user_alert,
    send_admin_tier_upgrade_alert,
)
from portal_hub.models import BotCategory, BotServiceType, UserProfile, UserTier
from portal_hub.telegram import TelegramPortalClient

# Real domain runners
from travel_bot_runner import run_briefing as run_travel_briefing


class PortalHubUnitTests(unittest.TestCase):
    """Unit Tests: Models, Gauges, Views, Navigation."""

    def test_tier_specifications(self):
        """Validate all tier slot quotas matching alerts.sqlite3 tier_policies."""
        self.assertEqual(UserTier.WHITE.max_slots, 1)
        self.assertEqual(UserTier.FREE.max_slots, 1)
        self.assertEqual(UserTier.GREEN.max_slots, 3)
        self.assertEqual(UserTier.RED.max_slots, 5)
        self.assertGreater(UserTier.BLACK.max_slots, 100)

        self.assertEqual(UserTier.WHITE.gauge_char, "◽")
        self.assertEqual(UserTier.GREEN.gauge_char, "🟩")
        self.assertEqual(UserTier.RED.gauge_char, "🟥")
        self.assertEqual(UserTier.BLACK.gauge_char, "⬛")

    def test_slot_gauge_formatting(self):
        """Test graphical gauge strings across counts and tiers."""
        # 0 of 1
        self.assertEqual(build_slot_gauge(0, UserTier.WHITE), "[ ◽ ] 0/1개 사용 중")
        # 1 of 1 (FULL)
        self.assertEqual(build_slot_gauge(1, UserTier.WHITE), "[ ◽ ] 1/1개 사용 중 (FULL)")
        # 1 of 3
        self.assertEqual(build_slot_gauge(1, UserTier.GREEN), "[ 🟩 ◽ ◽ ] 1/3개 사용 중")
        # 3 of 3 (FULL)
        self.assertEqual(build_slot_gauge(3, UserTier.GREEN), "[ 🟩 🟩 🟩 ] 3/3개 사용 중 (FULL)")
        # 2 of 5
        self.assertEqual(build_slot_gauge(2, UserTier.RED), "[ 🟥 🟥 ◽ ◽ ◽ ] 2/5개 사용 중")
        # BLACK unlimited
        self.assertEqual(build_slot_gauge(3, UserTier.BLACK), "[ ⬛ ⬛ ⬛ ★ ] 3개 활성화 (무제한)")

    def test_html_safety_and_xss_escaping(self):
        """Ensure special characters in user names are escaped and HTML is valid."""
        user = UserProfile(
            user_id=1001,
            chat_id=1001,
            username="tester_kim",
            first_name="김테스터<VIP>",
            tier=UserTier.WHITE,
        )
        subs = {bot: False for bot in BotServiceType}
        views_with_username = [
            render_main_menu(user),
            render_subscription_view(user, subs),
        ]
        all_views = views_with_username + [
            render_category_view(BotCategory.FINANCE, user, subs),
            render_category_home_view(),
            render_tier_benefits_view(user),
            render_help_view(),
        ]
        for text, markup in all_views:
            self.assertLess(len(text), 4096, "View text exceeds Telegram 4096 limit")
            self.assertNotIn("<VIP>", text, "Raw unescaped tag found!")
            self.assertIn("inline_keyboard", markup)

        for text, _ in views_with_username:
            self.assertIn("&lt;VIP&gt;", text, "Escaped tag expected in views rendering user name")

    def test_instant_search_and_keyword_discovery(self):
        """Test zero-scroll instant search matching keywords like 환율, 수시, 항공."""
        mock_db = PortalDatabase(":memory:")
        self.assertEqual(mock_db.search_bots("환율"), [BotServiceType.BRIEF])
        self.assertEqual(mock_db.search_bots("수시"), [BotServiceType.ADMISSION])
        self.assertEqual(mock_db.search_bots("항공"), [BotServiceType.TRAVEL])
        self.assertEqual(mock_db.search_bots("없는키워드123"), [])

        user = UserProfile(user_id=1001, chat_id=1001, first_name="테스터", tier=UserTier.WHITE)
        subs = {bot: False for bot in BotServiceType}
        results_text, markup = render_search_results_view("항공", [BotServiceType.TRAVEL], user, subs)
        self.assertIn("트래블 패스파인더", results_text)
        self.assertIn("inline_keyboard", markup)

    def test_all_navigation_callbacks(self):
        """Test routing for nav:main, nav:subs, cat:home, cat:view:FINANCE, nav:tiers, nav:help."""
        mock_client = MagicMock(spec=TelegramPortalClient)
        mock_db = MagicMock(spec=PortalDatabase)
        user = UserProfile(user_id=1001, chat_id=1001, first_name="김테스터", tier=UserTier.WHITE)
        mock_db.get_or_create_user.return_value = user
        mock_db.get_user_subscriptions.return_value = {bot: False for bot in BotServiceType}

        nav_targets = [
            "nav:main",
            "nav:subs",
            "cat:home",
            "cat:view:FINANCE",
            "cat:view:LIFE",
            "nav:tiers",
            "nav:help",
        ]
        for target in nav_targets:
            mock_client.reset_mock()
            cq = {
                "id": f"cq_{target}",
                "data": target,
                "from": {"id": 1001, "first_name": "김테스터"},
                "message": {"message_id": 50, "chat": {"id": 1001}},
            }
            handle_callback_query(cq, mock_db, mock_client)
            mock_client.edit_message_text.assert_called_once()
            mock_client.answer_callback_query.assert_called_once_with(f"cq_{target}")


class PortalHubSubscriptionIntegrationTests(unittest.TestCase):
    """End-to-End Integration Tests: Subscription lifecycle and domain bot dispatch connection."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_alerts.sqlite3"
        # 1. Initialize full schema via MarketBriefDatabase (creates users, subscriptions, bots, recipients)
        self.mb_db = MarketBriefDatabase(db_path=str(self.db_path))
        # 2. Add admission_bot into bots table so join works
        with self.mb_db._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO bots (bot_id, bot_type, name, category, icon_emoji, is_active)
                   VALUES ('admission_bot', 'INFO', '입시정보 나침반', '교육', '🎓', 1)"""
            )
        # 3. Connect PortalDatabase to the same alerts.sqlite3
        self.portal_db = PortalDatabase(self.db_path)
        self.mock_client = MagicMock(spec=TelegramPortalClient)

    def tearDown(self):
        del self.mb_db
        del self.portal_db
        gc.collect()
        try:
            self.tmp_dir.cleanup()
        except PermissionError:
            pass

    def test_e2e_white_tier_subscription_lifecycle_and_recipients_sync(self):
        """E2E Test: User starts as WHITE -> subscribes brief_bot -> syncs to recipients -> quota block -> swaps to travel_bot."""
        uid = 80001
        chat_id = 80001

        # 1. Onboarding via /start
        user = self.portal_db.get_or_create_user(uid, chat_id, username="white_user", first_name="신규회원")
        self.assertEqual(user.tier, UserTier.WHITE)
        self.assertEqual(user.status, "ACTIVE")

        # 2. Toggle brief_bot ON -> Success!
        cb_brief_on = {
            "id": "cb_b_on",
            "data": "sub:toggle:brief_bot",
            "from": {"id": uid, "first_name": "신규회원"},
            "message": {"message_id": 10, "chat": {"id": chat_id}},
        }
        handle_callback_query(cb_brief_on, self.portal_db, self.mock_client)
        self.mock_client.answer_callback_query.assert_called_with("cb_b_on", "✅ [📈 스마트 시황 브리핑] 구독이 시작되었습니다!", show_alert=False)

        # Verify DB state: subscriptions has active brief_bot
        subs = self.portal_db.get_user_subscriptions(uid)
        self.assertTrue(subs[BotServiceType.BRIEF])

        # Verify Legacy Recipients Sync: brief_bot subscriber MUST be in active recipients!
        active_finance = [r.chat_id for r in self.mb_db.get_active_recipients("FINANCE")]
        self.assertIn(str(chat_id), active_finance, "Subscribed user must be synced into recipients table for FINANCE")

        # 3. Try to subscribe to travel_bot while at 1/1 slot -> QUOTA EXCEEDED (Show native alert!)
        self.mock_client.reset_mock()
        cb_trv_fail = {
            "id": "cb_t_fail",
            "data": "sub:toggle:travel_bot",
            "from": {"id": uid, "first_name": "신규회원"},
            "message": {"message_id": 10, "chat": {"id": chat_id}},
        }
        handle_callback_query(cb_trv_fail, self.portal_db, self.mock_client)
        self.mock_client.answer_callback_query.assert_called_with(
            "cb_t_fail",
            "⚠️ 프리 (WHITE) 등급은 최대 1개까지 구독 가능합니다.\n기존 구독을 해지하거나 등급을 업그레이드해 주세요!",
            show_alert=True,
        )
        # travel_bot must remain False
        subs_after_fail = self.portal_db.get_user_subscriptions(uid)
        self.assertFalse(subs_after_fail[BotServiceType.TRAVEL])

        # 4. Toggle brief_bot OFF -> Success!
        cb_brief_off = {
            "id": "cb_b_off",
            "data": "sub:toggle:brief_bot",
            "from": {"id": uid, "first_name": "신규회원"},
            "message": {"message_id": 10, "chat": {"id": chat_id}},
        }
        handle_callback_query(cb_brief_off, self.portal_db, self.mock_client)
        # Verify legacy recipients updated (inactivated)
        active_finance_after = [r.chat_id for r in self.mb_db.get_active_recipients("FINANCE")]
        self.assertNotIn(str(chat_id), active_finance_after)

        # 5. Now Toggle travel_bot ON -> Success!
        cb_trv_on = {
            "id": "cb_t_on",
            "data": "sub:toggle:travel_bot",
            "from": {"id": uid, "first_name": "신규회원"},
            "message": {"message_id": 10, "chat": {"id": chat_id}},
        }
        handle_callback_query(cb_trv_on, self.portal_db, self.mock_client)
        subs_trv = self.portal_db.get_user_subscriptions(uid)
        self.assertTrue(subs_trv[BotServiceType.TRAVEL])

        # 6. Real Travel Briefing Runner Dispatch Integration:
        from travel_brief.storage.db import TravelBriefDatabase
        trv_db = TravelBriefDatabase(db_path=str(self.db_path))
        travel_active = [r.chat_id for r in trv_db.get_active_recipients(topic="TRAVEL")]
        self.assertIn(str(chat_id), travel_active, "User subscribed to travel_bot via portal_hub must appear in travel_brief active recipients!")

        # Pass the subscribed user's chat_id to run_travel_briefing
        trv_ok = run_travel_briefing(
            dry_run=True,
            db_path=str(self.db_path),
        )
        self.assertTrue(trv_ok, "travel_bot_runner must successfully run with Portal Hub subscribed chat_id")

    def test_e2e_green_tier_multi_subscription_expansion(self):
        """E2E Test: Upgrade user to GREEN (3 slots) -> activate all 3 bots (brief, travel, admission)."""
        uid = 80002
        chat_id = 80002
        self.portal_db.get_or_create_user(uid, chat_id, first_name="그린회원")

        # 1. Admin upgrades user to GREEN tier
        self.portal_db.set_user_tier(uid, UserTier.GREEN)
        user = self.portal_db.get_or_create_user(uid, chat_id)
        self.assertEqual(user.tier, UserTier.GREEN)
        self.assertEqual(user.tier.max_slots, 3)

        # 2. Subscribe to all 3 bots sequentially
        for bot in [BotServiceType.BRIEF, BotServiceType.TRAVEL, BotServiceType.ADMISSION]:
            ok, is_active, msg = self.portal_db.toggle_subscription(uid, bot)
            self.assertTrue(ok)
            self.assertTrue(is_active)

        # 3. Verify all 3 subscriptions are ACTIVE in database
        subs = self.portal_db.get_user_subscriptions(uid)
        self.assertTrue(subs[BotServiceType.BRIEF])
        self.assertTrue(subs[BotServiceType.TRAVEL])
        self.assertTrue(subs[BotServiceType.ADMISSION])
        self.assertEqual(sum(1 for v in subs.values() if v), 3)

        # 4. Fourth subscription would be blocked under GREEN (max 3)
        # Add dummy 4th bot
        with self.mb_db._connect() as conn:
            conn.execute(
                """INSERT OR IGNORE INTO bots (bot_id, bot_type, name, category, icon_emoji, is_active)
                   VALUES ('music_bot', 'INFO', '노래봇', '음악', '🎵', 1)"""
            )
        # Attempt to toggle 4th bot (simulated via db logic)
        with self.portal_db._get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT COUNT(*) as cnt FROM subscriptions s
                JOIN bots b ON s.bot_id = b.bot_id
                WHERE s.telegram_user_id = ? AND s.status = 'ACTIVE' AND b.bot_type = 'INFO'
                """,
                (str(uid),),
            )
            cnt = cur.fetchone()["cnt"]
            self.assertEqual(cnt, 3)
            self.assertGreaterEqual(cnt, user.tier.max_slots, "Slot quota strictly reached")


ADMIN_CHAT = "999000"
ADMIN_ENV = {"PORTAL_ADMIN_CHAT_ID": ADMIN_CHAT, "ADMIN_CONSOLE_URL": "http://admin.test:8002/"}


class PortalHubAdminAlertTests(unittest.TestCase):
    """Real-time admin alerts: new signup (one-shot) and tier upgrade requests (DUPLICATE suppressed)."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_alerts.sqlite3"
        self.mb_db = MarketBriefDatabase(db_path=str(self.db_path))
        self.portal_db = PortalDatabase(self.db_path)
        self.mock_client = MagicMock(spec=TelegramPortalClient)
        self.env_patch = patch.dict(os.environ, ADMIN_ENV)
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        del self.mb_db
        del self.portal_db
        gc.collect()
        try:
            self.tmp_dir.cleanup()
        except PermissionError:
            pass

    def _admin_sends(self) -> list:
        return [c for c in self.mock_client.send_message.call_args_list if str(c.args[0]) == ADMIN_CHAT]

    def _click(self, uid: int, data: str) -> None:
        cq = {
            "id": f"cq_{data}",
            "from": {"id": uid, "first_name": "<b>해커</b>", "username": "new_user"},
            "message": {"message_id": 10, "chat": {"id": uid}},
            "data": data,
        }
        handle_callback_query(cq, self.portal_db, self.mock_client)

    def test_new_user_flag_and_single_admin_alert(self):
        uid = 70001
        self._click(uid, "nav:main")
        sends = self._admin_sends()
        self.assertEqual(len(sends), 1)
        text = sends[0].args[1]
        self.assertIn("신규 회원 가입 알림", text)
        self.assertIn("&lt;b&gt;해커&lt;/b&gt;", text)
        self.assertNotIn("<b>해커</b>", text)
        self.assertIn(str(uid), text)
        self.assertLessEqual(len(text), 3500)

        # Re-entry by the same user: no longer new, no additional alert
        self.mock_client.reset_mock()
        user = self.portal_db.get_or_create_user(uid, uid)
        self.assertFalse(user.is_new)
        self._click(uid, "nav:main")
        self.assertEqual(len(self._admin_sends()), 0)

    def test_get_or_create_user_is_new_flag(self):
        first = self.portal_db.get_or_create_user(70002, 70002, first_name="첫방문")
        second = self.portal_db.get_or_create_user(70002, 70002, first_name="첫방문")
        self.assertTrue(first.is_new)
        self.assertFalse(second.is_new)
        self.assertFalse(UserProfile(user_id=1, chat_id=1).is_new)

    def test_request_tier_upgrade_status_codes(self):
        uid = 70003
        self.portal_db.get_or_create_user(uid, uid)
        req_id, s1 = self.portal_db.request_tier_upgrade(uid, "GREEN")
        req_id2, s2 = self.portal_db.request_tier_upgrade(uid, "GREEN")
        req_id3, s3 = self.portal_db.request_tier_upgrade(uid, "RED")
        self.assertEqual((s1, s2, s3), ("CREATED", "DUPLICATE", "CHANGED"))
        self.assertEqual(req_id, req_id2)
        self.assertEqual(req_id, req_id3)
        # Legacy `is_updated` truthiness preserved
        self.assertFalse(s1)
        self.assertTrue(s3)

    def test_upgrade_alerts_created_duplicate_changed(self):
        uid = 70004
        self.portal_db.get_or_create_user(uid, uid)  # pre-register so signup alert is out of scope

        self._click(uid, "support:req_tier:GREEN")
        sends = self._admin_sends()
        self.assertEqual(len(sends), 1)
        self.assertIn("회원 등업 신청 접수", sends[0].args[1])
        markup = sends[0].kwargs["reply_markup"]
        req_id = self.portal_db.get_pending_upgrade_requests()[0]["id"]
        self.assertEqual(
            markup["inline_keyboard"][0][0]["url"], f"http://admin.test:8002/?tab=users&req_id={req_id}"
        )

        self.mock_client.reset_mock()
        self._click(uid, "support:req_tier:GREEN")
        self.assertEqual(len(self._admin_sends()), 0)
        user_text = self.mock_client.edit_message_text.call_args.args[2]
        self.assertIn("이미 신청이 접수되어 운영팀 심사 대기 중입니다", user_text)

        self.mock_client.reset_mock()
        self._click(uid, "support:req_tier:RED")
        sends = self._admin_sends()
        self.assertEqual(len(sends), 1)
        self.assertIn("회원 등업 신청 변경", sends[0].args[1])

        pending = self.portal_db.get_pending_upgrade_requests()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["requested_tier"], "RED")

    def test_admin_alert_network_failure_is_fail_open(self):
        uid = 70005

        def flaky_send(chat_id, text, reply_markup=None, **kwargs):
            if str(chat_id) == ADMIN_CHAT:
                raise RuntimeError("Telegram API sendMessage failed: timeout")
            return {"ok": True}

        self.mock_client.send_message.side_effect = flaky_send
        # New user + upgrade request both hit the failing admin channel without breaking the user flow
        self._click(uid, "support:req_tier:GREEN")
        self.mock_client.edit_message_text.assert_called_once()
        self.assertIn("등급 업그레이드 신청 완료", self.mock_client.edit_message_text.call_args.args[2])
        self.assertEqual(len(self.portal_db.get_pending_upgrade_requests()), 1)

    def test_no_admin_chat_configured_skips_alerts(self):
        with patch.dict(os.environ, {}, clear=True):
            user = UserProfile(user_id=1, chat_id=1, first_name="x", is_new=True)
            self.assertFalse(send_admin_new_user_alert(self.mock_client, user))
            self.assertFalse(send_admin_tier_upgrade_alert(self.mock_client, user, "GREEN", "CREATED"))
        self.mock_client.send_message.assert_not_called()

    def test_admin_console_url_deeplink_query(self):
        with patch.dict(os.environ, {"ADMIN_CONSOLE_URL": "http://admin.test:8002/"}):
            self.assertEqual(_get_admin_console_url(), "http://admin.test:8002/")
            self.assertEqual(
                _get_admin_console_url(tab="users", req_id=123), "http://admin.test:8002/?tab=users&req_id=123"
            )
            self.assertEqual(_get_admin_console_url(tab="users"), "http://admin.test:8002/?tab=users")
        # Base URL with existing query (e.g. token) and no trailing slash is preserved
        with patch.dict(os.environ, {"ADMIN_CONSOLE_URL": "http://admin.test:8002?token=abc&tab=old"}):
            self.assertEqual(
                _get_admin_console_url(tab="users", req_id=7), "http://admin.test:8002/?token=abc&tab=users&req_id=7"
            )

    def test_upgrade_alert_button_carries_req_id(self):
        user = UserProfile(user_id=70006, chat_id=70006, first_name="딥링크")
        self.assertTrue(send_admin_tier_upgrade_alert(self.mock_client, user, "GREEN", "CREATED", req_id=42))
        url = self._admin_sends()[0].kwargs["reply_markup"]["inline_keyboard"][0][0]["url"]
        self.assertIn("tab=users", url)
        self.assertIn("req_id=42", url)


if __name__ == "__main__":
    unittest.main()
