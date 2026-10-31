"""Comprehensive Unit & Integration Tests for Multi-Bot Subscription Platform v2.0."""
import concurrent.futures
import io
import json
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient

from admin_server import create_admin_app
from market_brief.multi_bot_manager import DynamicBotManager, MultiBotInteractionRouter
from market_brief.storage.db import MarketBriefDatabase
from market_brief.subscription_engine import SubscriptionEngine


class FakeTelegramHTTPResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class MultiBotSubscriptionPlatformTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_platform_v20.sqlite3"
        self.env_path = Path(self.tmp_dir.name) / "empty.env"
        self.db = MarketBriefDatabase(db_path=str(self.db_path), env_path=self.env_path)
        self.engine = SubscriptionEngine(self.db)
        self.router = MultiBotInteractionRouter(self.engine)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_default_seed_menu_and_four_info_bots(self):
        bots = self.engine.list_bots()
        bot_ids = [b["bot_id"] for b in bots]
        self.assertIn("menu_bot", bot_ids)
        self.assertIn("brief_bot", bot_ids)
        self.assertIn("travel_bot", bot_ids)
        self.assertIn("admission_bot", bot_ids)

        menu_bot = self.engine.get_menu_bot()
        self.assertIsNotNone(menu_bot)
        self.assertEqual(menu_bot.bot_type, "MENU")

    def test_white_tier_max_1_bot_and_unsubscribe_then_switch(self):
        """Requirement #2 & #4: WHITE user can only use 1 bot; must leave brief_bot before travel_bot."""
        uid = "10001"
        # 1. Enter brief_bot (/start) -> auto-registered as WHITE and subscribed (1/1)
        r1 = self.router.handle_interaction("brief_bot", uid, full_name="김화이트", text="/start")
        self.assertEqual(r1["result_code"], "SUBSCRIBED")
        self.assertEqual(r1["user_profile"]["tier"], "WHITE")
        self.assertEqual(r1["user_profile"]["used_slots"], 1)

        # Legacy sync check: brief_bot subscriber appears in active FINANCE recipients
        legacy_active = [r.chat_id for r in self.db.get_active_recipients("FINANCE")]
        self.assertIn(uid, legacy_active)

        # 2. Try entering travel_bot (/start) while subscribed to brief_bot -> BLOCKED!
        r2 = self.router.handle_interaction("travel_bot", uid, full_name="김화이트", text="/start")
        self.assertEqual(r2["result_code"], "QUOTA_EXCEEDED")
        self.assertIn("구독 가능 봇 개수 초과", r2["reply_html"])
        # Offers one-click swap inline button
        swap_callbacks = [
            btn["callback_data"]
            for row in r2["inline_keyboard"]
            for btn in row
            if "callback_data" in btn
        ]
        self.assertIn("info:swap:brief_bot:travel_bot", swap_callbacks)

        # 3. Leave brief_bot via /stop -> slot freed (0/1), then enter travel_bot -> SUBSCRIBED!
        r_stop = self.router.handle_interaction("brief_bot", uid, full_name="김화이트", text="/stop")
        self.assertEqual(r_stop["result_code"], "UNSUBSCRIBED")
        self.assertEqual(r_stop["user_profile"]["used_slots"], 0)
        self.assertNotIn(uid, [r.chat_id for r in self.db.get_active_recipients("FINANCE")])

        r3 = self.router.handle_interaction("travel_bot", uid, full_name="김화이트", text="/start")
        self.assertEqual(r3["result_code"], "SUBSCRIBED")
        active_ids = [s["bot_id"] for s in r3["user_profile"]["active_subscriptions"]]
        self.assertEqual(active_ids, ["travel_bot"])

        # 4. Test Atomic One-Click Swap from travel_bot -> admission_bot directly!
        r_swap = self.router.handle_interaction(
            "admission_bot", uid, full_name="김화이트", callback_data="info:swap:travel_bot:admission_bot"
        )
        self.assertEqual(r_swap["result_code"], "SWAPPED")
        active_ids_after = [s["bot_id"] for s in r_swap["user_profile"]["active_subscriptions"]]
        self.assertEqual(active_ids_after, ["admission_bot"])

    def test_green_red_black_tier_quotas(self):
        """Requirement #2: GREEN=3, RED=5, BLACK=Unlimited."""
        # Add 3 more info bots so total INFO bots = 6
        self.engine.upsert_bot("realestate_bot", "부동산 청약봇", bot_type="INFO", category="부동산", icon_emoji="🏢")
        self.engine.upsert_bot("english_bot", "매일 영어봇", bot_type="INFO", category="교육", icon_emoji="📘")
        self.engine.upsert_bot("trend_bot", "트렌드 봇", bot_type="INFO", category="트렌드", icon_emoji="🔥")
        all_6_bots = ["brief_bot", "travel_bot", "admission_bot", "realestate_bot", "english_bot", "trend_bot"]

        # GREEN user: up to 3 bots allowed, 4th rejected
        u_green = "20002"
        self.engine.upsert_user(u_green, "이그린", tier="GREEN")
        for b in all_6_bots[:3]:
            res = self.engine.attempt_subscribe(u_green, b)
            self.assertTrue(res["success"], f"GREEN failed on {b}")
        res_4th = self.engine.attempt_subscribe(u_green, all_6_bots[3])
        self.assertFalse(res_4th["success"])
        self.assertEqual(res_4th["code"], "QUOTA_EXCEEDED")

        # RED user: up to 5 bots allowed, 6th rejected
        u_red = "30003"
        self.engine.upsert_user(u_red, "박레드", tier="RED")
        for b in all_6_bots[:5]:
            res = self.engine.attempt_subscribe(u_red, b)
            self.assertTrue(res["success"], f"RED failed on {b}")
        res_6th = self.engine.attempt_subscribe(u_red, all_6_bots[5])
        self.assertFalse(res_6th["success"])
        self.assertEqual(res_6th["code"], "QUOTA_EXCEEDED")

        # BLACK user: all 6 bots (and more) allowed without limit
        u_black = "40004"
        self.engine.upsert_user(u_black, "최블랙", tier="BLACK")
        for b in all_6_bots:
            res = self.engine.attempt_subscribe(u_black, b)
            self.assertTrue(res["success"], f"BLACK failed on {b}")
        prof = self.engine.get_user_profile(u_black)
        self.assertEqual(prof["used_slots"], 6)
        self.assertTrue(prof["is_unlimited"])

    def test_menu_bot_portal_does_not_consume_quota(self):
        """Requirement #3: Menu bot shows catalog & allows selecting info bots without consuming quota."""
        uid = "50005"
        m_home = self.router.handle_interaction("menu_bot", uid, full_name="정메뉴", text="/start")
        self.assertEqual(m_home["result_code"], "MENU_HOME")
        self.assertEqual(m_home["user_profile"]["used_slots"], 0)
        self.assertIn("시황 브리핑봇", m_home["reply_html"])
        self.assertIn("여행 특가봇", m_home["reply_html"])

        # Select travel_bot detail inside menu_bot
        m_detail = self.router.handle_interaction("menu_bot", uid, callback_data="menu:bot:travel_bot")
        self.assertEqual(m_detail["result_code"], "MENU_BOT_DETAIL")

        # Subscribe via menu_bot
        m_sub = self.router.handle_interaction("menu_bot", uid, callback_data="menu:sub:travel_bot")
        self.assertEqual(m_sub["user_profile"]["used_slots"], 1)

    def test_concurrency_race_condition_protection_for_white_user(self):
        """Claude Review #1: Concurrent `/start` across 4 bots for a WHITE user must allow strictly 1 bot."""
        uid = "60006"
        self.engine.upsert_user(uid, "동시성테스터", tier="WHITE")
        self.engine.upsert_bot("custom_bot", "커스텀 봇", bot_type="INFO", category="기타", icon_emoji="🤖")
        bots = ["brief_bot", "travel_bot", "admission_bot", "custom_bot"]

        def worker(bot_id: str):
            eng = SubscriptionEngine(self.db)
            return eng.attempt_subscribe(uid, bot_id)

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(worker, bots))

        successes = [r for r in results if r["success"] and r["code"] == "SUBSCRIBED"]
        rejections = [r for r in results if not r["success"] and r["code"] == "QUOTA_EXCEEDED"]
        self.assertEqual(len(successes), 1)
        self.assertEqual(len(rejections), 3)

    def test_tier_downgrade_grace_and_expiry_auto_rollback(self):
        """Claude Review #3 & #4: Tier downgrade reconciliation & expired tier rollback."""
        uid = "70007"
        self.engine.upsert_user(uid, "강등만료테스터", tier="GREEN", tier_expires_at="2099-12-31")
        for b in ["brief_bot", "travel_bot", "admission_bot"]:
            self.engine.attempt_subscribe(uid, b)

        # Admin downgrades GREEN (3 used) -> WHITE (max 1) with GRACE mode
        updated = self.engine.update_user_tier(uid, "WHITE", downgrade_mode="GRACE")
        self.assertEqual(updated["tier"], "WHITE")
        self.assertEqual(updated["status"], "GRACE_PERIOD")
        self.assertEqual(updated["used_slots"], 1)
        self.assertEqual(len(updated["suspended_subscriptions"]), 2)

        # Upgrade back to RED -> suspended bots automatically restored!
        upgraded = self.engine.update_user_tier(uid, "RED", tier_expires_at="2000-01-01")
        # Since 2000-01-01 is in the past, next profile fetch sweeps expiry -> rolls back to WHITE!
        swept = self.engine.get_user_profile(uid)
        self.assertEqual(swept["tier"], "WHITE")

    def test_dynamic_bot_polling_sync_once_with_mock_telegram(self):
        """Verifies live Telegram getUpdates consumption & automatic reply routing."""
        self.engine.upsert_bot("trend_bot", "트렌드 인사이트봇", token="999999:FAKE_TREND_TOKEN", is_active=True)
        sent_messages = []

        def fake_opener(req, timeout=10):
            url = req.full_url
            if "/getUpdates" in url:
                payload = {
                    "ok": True,
                    "result": [
                        {
                            "update_id": 501,
                            "message": {
                                "message_id": 1,
                                "from": {"id": 88801, "first_name": "텔레유저", "username": "tele_u"},
                                "chat": {"id": 88801, "type": "private", "first_name": "텔레유저"},
                                "text": "/start",
                            },
                        }
                    ],
                }
                return FakeTelegramHTTPResponse(json.dumps(payload).encode("utf-8"))
            if "/sendMessage" in url:
                body = json.loads(req.data.decode("utf-8"))
                sent_messages.append(body)
                return FakeTelegramHTTPResponse(
                    json.dumps({"ok": True, "result": {"message_id": 10}}).encode("utf-8")
                )
            return FakeTelegramHTTPResponse(json.dumps({"ok": True, "result": {}}).encode("utf-8"))

        mgr = DynamicBotManager(self.engine, self.router)
        sync_res = mgr.sync_bot_updates_once("trend_bot", opener=fake_opener)
        self.assertEqual(sync_res["processed"], 1)
        self.assertEqual(sync_res["last_offset"], 501)
        self.assertEqual(len(sent_messages), 1)
        self.assertIn("구독이 시작되었습니다", sent_messages[0]["text"])

    def test_admin_platform_rest_api_endpoints(self):
        """Requirement #5: Admin API for bots, users, tier updates, matrix, simulator & token masking."""
        app = create_admin_app(db_path=str(self.db_path), env_path=self.env_path, enable_bot_workers=False)
        client = TestClient(app)

        # 1. Overview
        r_ov = client.get("/api/platform/overview")
        self.assertEqual(r_ov.status_code, 200)
        self.assertEqual(len(r_ov.json()["tier_distribution"]), 4)

        # 2. Create Bot with secret token -> response must mask raw token!
        r_bot = client.post(
            "/api/platform/bots",
            json={
                "bot_id": "food_bot",
                "name": "맛집 추천봇",
                "bot_type": "INFO",
                "token": "1234567890:AAHSecretTokenValue9876",
                "category": "라이프",
                "icon_emoji": "🍽️",
                "description": "전국 숨은 맛집 큐레이션",
            },
        )
        self.assertEqual(r_bot.status_code, 200)
        bot_data = r_bot.json()["bot"]
        self.assertTrue(bot_data["has_token"])
        self.assertNotIn("AAHSecretTokenValue", json.dumps(bot_data))
        self.assertEqual(bot_data["token_masked"], "123456...9876")

        # 3. Create User & Change Tier (WHITE -> GREEN)
        r_u = client.post(
            "/api/platform/users",
            json={"telegram_user_id": "77701", "full_name": "관리대상유저", "tier": "WHITE"},
        )
        self.assertEqual(r_u.status_code, 200)

        r_tier = client.put(
            "/api/platform/users/77701/tier",
            json={"tier": "GREEN", "tier_expires_at": "2027-12-31"},
        )
        self.assertEqual(r_tier.status_code, 200)
        self.assertEqual(r_tier.json()["user"]["tier"], "GREEN")
        self.assertEqual(r_tier.json()["user"]["max_slots"], 3)

        # 4. Simulator API & Matrix API
        r_sim = client.post(
            "/api/platform/simulate",
            json={"bot_id": "food_bot", "telegram_user_id": "77701", "text": "/start"},
        )
        self.assertEqual(r_sim.status_code, 200)
        self.assertEqual(r_sim.json()["result_code"], "SUBSCRIBED")

        r_mat = client.get("/api/platform/matrix")
        self.assertEqual(r_mat.status_code, 200)
        row_77701 = [r for r in r_mat.json()["rows"] if r["telegram_user_id"] == "77701"][0]
        self.assertEqual(row_77701["cells"]["food_bot"], "ACTIVE")

        # 5. Briefing Center Preview & Presets API
        r_pre = client.get("/api/platform/bots/brief_bot/presets")
        self.assertEqual(r_pre.status_code, 200)
        self.assertTrue(len(r_pre.json()["presets"]) >= 2)

        r_gen = client.post("/api/platform/bots/brief_bot/preview", json={"preset": "morning"})
        self.assertEqual(r_gen.status_code, 200)
        self.assertIn("모닝", r_gen.json()["title"])


if __name__ == "__main__":
    unittest.main()

