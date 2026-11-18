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
        self.assertIn("시황", m_home["reply_html"])
        self.assertIn("여행", m_home["reply_html"])

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

        # Option 1: GRACE_PERIOD keeps its 1 ACTIVE bot; SUSPENDED_OVER_QUOTA bots stay blocked
        kept = {s["bot_id"] for s in updated["active_subscriptions"]}
        suspended = {s["bot_id"] for s in updated["suspended_subscriptions"]}
        self.assertEqual(len(kept), 1)
        for b in kept:
            self.assertTrue(self.engine.is_user_subscribed(uid, b), b)
            self.assertIn(uid, [r.chat_id for r in self.engine.get_bot_active_recipients(b)])
        for b in suspended:
            self.assertFalse(self.engine.is_user_subscribed(uid, b), b)
            self.assertNotIn(uid, [r.chat_id for r in self.engine.get_bot_active_recipients(b)])

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


LOCKED_ALERT = "🔒 현재 구독 중인 봇이 아닙니다. 메뉴봇에서 구독 후 이용해 주세요."
DOMAIN_DISPATCHERS = (
    "_dispatch_travel_bot_update",
    "_dispatch_admission_bot_update",
    "_dispatch_silver_bot_update",
    "_dispatch_kids_bot_update",
    "_dispatch_yolo_bot_update",
    "_dispatch_brief_watch_callback",
)


class SubscriptionAccessGateTests(unittest.TestCase):
    """Fail-closed inbound gate: un-subscribed callers never reach `mbw:` or any domain dispatcher."""

    INFO_BOTS = ("brief_bot", "travel_bot", "admission_bot", "silver_bot", "kids_bot", "yolo_bot")

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "alerts.sqlite3"
        self.env_path = Path(self.tmp_dir.name) / "empty.env"
        self.db = MarketBriefDatabase(db_path=str(self.db_path), env_path=self.env_path)
        self.engine = SubscriptionEngine(self.db)
        self.router = MultiBotInteractionRouter(self.engine)
        with self.db._connect() as conn:
            for i, bid in enumerate(self.INFO_BOTS):
                conn.execute("UPDATE bots SET token = ?, last_offset = 1 WHERE bot_id = ?", (f"10{i}:FAKE_{bid}", bid))
            conn.execute("UPDATE bots SET telegram_username = 'KwMenu_bot' WHERE bot_id = 'menu_bot'")
        self.calls: list[tuple[str, dict]] = []
        self.updates: list[dict] = []

    def tearDown(self):
        import gc
        gc.collect()
        try:
            self.tmp_dir.cleanup()
        except PermissionError:
            pass

    # ---- fake Telegram ------------------------------------------------------
    def _opener(self, req, timeout=10):
        url = req.full_url
        method = url.rsplit("/", 1)[-1].split("?")[0]
        if method == "getUpdates":
            return FakeTelegramHTTPResponse(json.dumps({"ok": True, "result": self.updates}).encode("utf-8"))
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        self.calls.append((method, body))
        return FakeTelegramHTTPResponse(json.dumps({"ok": True, "result": {"message_id": 99}}).encode("utf-8"))

    def _cb(self, uid: str, data: str, update_id: int = 10) -> dict:
        return {
            "update_id": update_id,
            "callback_query": {
                "id": f"cq_{update_id}",
                "from": {"id": int(uid), "first_name": "잠금"},
                "message": {"message_id": 5, "chat": {"id": int(uid), "type": "private"}},
                "data": data,
            },
        }

    def _msg(self, uid: str, text: str, update_id: int = 10) -> dict:
        return {
            "update_id": update_id,
            "message": {
                "message_id": 5,
                "from": {"id": int(uid), "first_name": "잠금"},
                "chat": {"id": int(uid), "type": "private", "first_name": "잠금"},
                "text": text,
            },
        }

    def _manager_with_tripwires(self):
        from unittest.mock import patch

        mgr = DynamicBotManager(self.engine, self.router)
        patches = [patch.object(mgr, name, side_effect=AssertionError(f"{name} reached")) for name in DOMAIN_DISPATCHERS]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        return mgr

    def _run(self, mgr, bot_id: str, update: dict) -> dict:
        self.calls.clear()
        self.updates = [update]
        res = mgr.sync_bot_updates_once(bot_id, opener=self._opener)
        self.assertEqual(res["processed"], 1)
        return res

    def _assert_locked(self, bot_id: str, is_callback: bool):
        methods = [m for m, _ in self.calls]
        if is_callback:
            self.assertEqual(methods[0], "answerCallbackQuery", "[TG-04] answerCallbackQuery must be first")
            self.assertEqual(self.calls[0][1].get("text"), LOCKED_ALERT)
            self.assertTrue(self.calls[0][1].get("show_alert"))
        sends = [b for m, b in self.calls if m == "sendMessage"]
        self.assertEqual(len(sends), 1, methods)
        self.assertIn("구독 중인 봇이 아닙니다", sends[0]["text"])
        kb = json.dumps(sends[0].get("reply_markup", {}), ensure_ascii=False)
        self.assertIn("https://t.me/KwMenu_bot", kb)
        self.assertIn(f"info:sub:{bot_id}", kb)

    # ---- tests --------------------------------------------------------------
    def test_swap_away_blocks_old_bot_callbacks_commands_and_text(self):
        uid = "60001"
        self.assertEqual(self.engine.attempt_subscribe(uid, "travel_bot")["code"], "SUBSCRIBED")
        self.assertTrue(self.engine.is_user_subscribed(uid, "travel_bot"))
        self.assertTrue(self.engine.atomic_swap_subscription(uid, "travel_bot", "admission_bot")["success"])
        self.assertFalse(self.engine.is_user_subscribed(uid, "travel_bot"))

        mgr = self._manager_with_tripwires()
        for i, data in enumerate(("trv:menu", "info:preview:travel_bot", "info:target:travel_bot:NRT", "info:retarget:travel_bot")):
            self._run(mgr, "travel_bot", self._cb(uid, data, 100 + i))
            self._assert_locked("travel_bot", is_callback=True)
        for i, text in enumerate(("/watch", "/menu", "도쿄 항공권")):
            self._run(mgr, "travel_bot", self._msg(uid, text, 200 + i))
            self._assert_locked("travel_bot", is_callback=False)
        # Plain text never auto-subscribes
        self.assertFalse(self.engine.is_user_subscribed(uid, "travel_bot"))

    def test_unsubscribed_user_blocked_on_every_domain_bot(self):
        uid = "60002"
        self.engine.attempt_subscribe(uid, "brief_bot")
        self.engine.unsubscribe_bot(uid, "brief_bot")
        mgr = self._manager_with_tripwires()
        cases = {
            "brief_bot": (["mbw:m", "mbw:q3", "info:preview:brief_bot"], ["/briefing", "/market", "/watch", "안녕"]),
            "travel_bot": (["trv:home"], ["/search"]),
            "admission_bot": (["adm:home", "grade:HIGH_3_N", "region:SEOUL", "menu:todo"], ["/todo", "/briefing"]),
            "silver_bot": (["SC:home"], ["/parents", "/bookmarks"]),
            "kids_bot": (["k:home"], ["/fever", "/weekend"]),
            "yolo_bot": (["yolo:home"], ["/weekend", "/curation"]),
        }
        n = 300
        for bot_id, (cbs, texts) in cases.items():
            for data in cbs:
                n += 1
                self._run(mgr, bot_id, self._cb(uid, data, n))
                self._assert_locked(bot_id, is_callback=True)
            for text in texts:
                n += 1
                self._run(mgr, bot_id, self._msg(uid, text, n))
                self._assert_locked(bot_id, is_callback=False)

    def test_start_on_unsubscribed_bot_shows_swap_prompt_not_domain_onboarding(self):
        uid = "60003"
        self.engine.attempt_subscribe(uid, "brief_bot")  # WHITE: 1/1 used
        mgr = self._manager_with_tripwires()
        res = self._run(mgr, "travel_bot", self._msg(uid, "/start"))
        self.assertEqual(res["events"][0]["code"], "QUOTA_EXCEEDED")
        kb = json.dumps([b for m, b in self.calls if m == "sendMessage"][0]["reply_markup"])
        self.assertIn("info:swap:brief_bot:travel_bot", kb)

        # The swap callback itself is allowed and re-opens access to travel_bot
        res = self._run(mgr, "travel_bot", self._cb(uid, "info:swap:brief_bot:travel_bot", 11))
        self.assertEqual(res["events"][0]["code"], "SWAPPED")
        self.assertTrue(self.engine.is_user_subscribed(uid, "travel_bot"))
        self.assertFalse(self.engine.is_user_subscribed(uid, "brief_bot"))
        # ...and brief_bot is now locked (mbw: never reaches the watch dispatcher)
        self._run(mgr, "brief_bot", self._cb(uid, "mbw:m", 12))
        self._assert_locked("brief_bot", is_callback=True)

    def test_stop_and_info_sub_allowed_for_unsubscribed_user(self):
        uid = "60004"
        mgr = self._manager_with_tripwires()
        res = self._run(mgr, "kids_bot", self._msg(uid, "/stop"))
        self.assertEqual(res["events"][0]["code"], "NOT_SUBSCRIBED")
        res = self._run(mgr, "kids_bot", self._cb(uid, "info:sub:kids_bot", 11))
        self.assertEqual(res["events"][0]["code"], "SUBSCRIBED")
        self.assertTrue(self.engine.is_user_subscribed(uid, "kids_bot"))

    def test_subscribed_user_still_reaches_domain_dispatcher(self):
        from unittest.mock import patch

        uid = "60005"
        self.engine.attempt_subscribe(uid, "kids_bot")
        mgr = DynamicBotManager(self.engine, self.router)
        with patch.object(mgr, "_dispatch_kids_bot_update", return_value=(True, "kids_callback_handled")) as disp:
            res = self._run(mgr, "kids_bot", self._cb(uid, "k:home"))
        disp.assert_called_once()
        self.assertEqual(res["events"][0]["code"], "kids_callback_handled")

    def test_gate_cancel_for_unsubscribed_user_shows_locked_card(self):
        uid = "60006"
        out = self.router.handle_interaction("brief_bot", uid, callback_data="gate:c:bf_rep")
        self.assertEqual(out["result_code"], "GATE_CANCEL")
        self.assertEqual(out.get("access"), "LOCKED")
        self.assertIn("구독 중인 봇이 아닙니다", out["reply_html"])

        mgr = self._manager_with_tripwires()
        self._run(mgr, "brief_bot", self._cb(uid, "gate:c:bf_rep"))
        edits = [b for m, b in self.calls if m == "editMessageText"]
        self.assertEqual(len(edits), 1)
        self.assertIn("구독 중인 봇이 아닙니다", edits[0]["text"])

    def test_router_plain_text_and_preview_never_auto_subscribe(self):
        uid = "60007"
        for kwargs in ({"text": "안녕하세요"}, {"callback_data": "info:preview:travel_bot"},
                       {"callback_data": "info:target:travel_bot:NRT"}):
            out = self.router.handle_interaction("travel_bot", uid, **kwargs)
            self.assertEqual(out["result_code"], "NOT_SUBSCRIBED_LOCKED", kwargs)
        self.assertFalse(self.engine.is_user_subscribed(uid, "travel_bot"))
        out = self.router.handle_interaction("travel_bot", uid, text="/start")
        self.assertEqual(out["result_code"], "SUBSCRIBED")
        out = self.router.handle_interaction("travel_bot", uid, callback_data="info:preview:travel_bot")
        self.assertEqual(out["result_code"], "BRIEFING_PREVIEW")

    def test_is_user_subscribed_fails_closed(self):
        from unittest.mock import patch

        uid = "60008"
        self.assertFalse(self.engine.is_user_subscribed(uid, "brief_bot"))  # unknown user
        self.engine.attempt_subscribe(uid, "admission_bot")
        self.assertTrue(self.engine.is_user_subscribed(uid, "admission_bot"))
        self.assertTrue(self.engine.is_user_subscribed(uid, "edu_bot"))  # legacy alias

        for status in ("BANNED", "BLOCKED"):
            with self.db._connect() as conn:
                conn.execute("UPDATE users SET status = ? WHERE telegram_user_id = ?", (status, uid))
            self.assertFalse(self.engine.is_user_subscribed(uid, "admission_bot"), status)
        # GRACE_PERIOD keeps access to its kept ACTIVE subscription ...
        with self.db._connect() as conn:
            conn.execute("UPDATE users SET status = 'GRACE_PERIOD' WHERE telegram_user_id = ?", (uid,))
        self.assertTrue(self.engine.is_user_subscribed(uid, "admission_bot"))
        # ... but never to a SUSPENDED_OVER_QUOTA one
        with self.db._connect() as conn:
            conn.execute(
                "UPDATE subscriptions SET status = 'SUSPENDED_OVER_QUOTA' WHERE telegram_user_id = ? AND bot_id = 'admission_bot'",
                (uid,),
            )
        self.assertFalse(self.engine.is_user_subscribed(uid, "admission_bot"))
        with self.db._connect() as conn:
            conn.execute("UPDATE subscriptions SET status = 'ACTIVE' WHERE telegram_user_id = ?", (uid,))
            conn.execute("UPDATE users SET status = 'ACTIVE' WHERE telegram_user_id = ?", (uid,))
            conn.execute(
                "UPDATE subscriptions SET status = 'SUSPENDED_OVER_QUOTA' WHERE telegram_user_id = ? AND bot_id = 'admission_bot'",
                (uid,),
            )
        self.assertFalse(self.engine.is_user_subscribed(uid, "admission_bot"))
        with self.db._connect() as conn:
            conn.execute("UPDATE subscriptions SET status = 'ACTIVE' WHERE telegram_user_id = ?", (uid,))
            conn.execute("UPDATE bots SET is_active = 0 WHERE bot_id = 'admission_bot'")
        self.assertFalse(self.engine.is_user_subscribed(uid, "admission_bot"))
        with self.db._connect() as conn:
            conn.execute("UPDATE bots SET is_active = 1 WHERE bot_id = 'admission_bot'")
        self.assertTrue(self.engine.is_user_subscribed(uid, "admission_bot"))
        self.assertFalse(self.engine.is_user_subscribed(uid, "menu_bot"))  # MENU is not an INFO bot

        with patch.object(self.engine, "_read_conn", side_effect=RuntimeError("db down")):
            self.assertFalse(self.engine.is_user_subscribed(uid, "admission_bot"))
        mgr = DynamicBotManager(self.engine, self.router)
        with patch.object(self.engine, "is_user_subscribed", side_effect=RuntimeError("boom")):
            self.assertFalse(mgr._brief_subscription_active(uid))

    def test_active_recipients_exclude_non_active_users(self):
        uid = "60009"
        self.engine.attempt_subscribe(uid, "brief_bot")
        self.assertIn(uid, [r.chat_id for r in self.engine.get_bot_active_recipients("brief_bot")])
        with self.db._connect() as conn:
            conn.execute("UPDATE users SET status = 'GRACE_PERIOD' WHERE telegram_user_id = ?", (uid,))
        # GRACE_PERIOD keeps receiving its kept ACTIVE bot
        self.assertIn(uid, [r.chat_id for r in self.engine.get_bot_active_recipients("brief_bot")])
        with self.db._connect() as conn:
            conn.execute(
                "UPDATE subscriptions SET status = 'SUSPENDED_OVER_QUOTA' WHERE telegram_user_id = ? AND bot_id = 'brief_bot'",
                (uid,),
            )
        self.assertNotIn(uid, [r.chat_id for r in self.engine.get_bot_active_recipients("brief_bot")])
        with self.db._connect() as conn:
            conn.execute("UPDATE subscriptions SET status = 'ACTIVE' WHERE telegram_user_id = ?", (uid,))
        for status in ("BANNED", "BLOCKED"):
            with self.db._connect() as conn:
                conn.execute("UPDATE users SET status = ? WHERE telegram_user_id = ?", (status, uid))
            self.assertNotIn(uid, [r.chat_id for r in self.engine.get_bot_active_recipients("brief_bot")], status)

    def test_unsubscribe_and_swap_deactivate_brief_watches(self):
        uid = "60010"
        self.router.handle_interaction("brief_bot", uid, text="/start")
        self.router.handle_interaction("brief_bot", uid, callback_data="mbw:q3")
        self.assertTrue(self.db.list_market_watches(uid))
        self.assertTrue(self.db.list_evaluable_market_watches())
        with self.db._connect() as conn:
            conn.execute("UPDATE users SET status = 'BANNED' WHERE telegram_user_id = ?", (uid,))
        self.assertEqual(self.db.list_evaluable_market_watches(), [])  # fail-closed outbound
        with self.db._connect() as conn:
            conn.execute("UPDATE users SET status = 'ACTIVE' WHERE telegram_user_id = ?", (uid,))

        self.assertTrue(self.engine.atomic_swap_subscription(uid, "brief_bot", "travel_bot")["success"])
        self.assertEqual(self.db.list_market_watches(uid), [])

    def test_domain_outbound_filters_respect_platform_subscriptions(self):
        from admission_brief.models import ParentUserProfile
        from admission_brief.storage.db import AdmissionDatabase
        from kids_compass.storage.db import KidsCompassDatabase
        from yolo_brief.models import SoloArchetype
        from yolo_brief.storage.db import YoloDatabase

        sub_uid, other_uid = "60011", "60012"
        self.engine.attempt_subscribe(other_uid, "brief_bot")  # platform user, not a domain subscriber

        # admission lives in the platform DB itself
        self.engine.attempt_subscribe(sub_uid, "admission_bot")
        adm = AdmissionDatabase(db_path=str(self.db_path))
        for uid in (sub_uid, other_uid):
            adm.upsert_subscriber(ParentUserProfile(chat_id=uid))
        self.assertEqual([p.chat_id for p in adm.list_subscribers()], [sub_uid])
        self.engine.unsubscribe_bot(sub_uid, "admission_bot")
        self.assertEqual(adm.list_subscribers(), [])
        self.assertFalse(adm.get_subscriber(sub_uid).alert_enabled)  # deactivate_domain_alerts
        self.engine.attempt_subscribe(sub_uid, "admission_bot")
        self.assertTrue(adm.get_subscriber(sub_uid).alert_enabled)  # restored on re-subscribe
        self.assertEqual([p.chat_id for p in adm.list_subscribers()], [sub_uid])

        # kids / yolo use sibling files next to alerts.sqlite3
        self.engine.unsubscribe_bot(sub_uid, "admission_bot")
        self.engine.attempt_subscribe(sub_uid, "kids_bot")
        kids = KidsCompassDatabase(Path(self.tmp_dir.name) / "kids_compass.sqlite3")
        try:
            for uid in (sub_uid, other_uid):
                kids.ensure_user(uid)
            self.assertEqual([r["telegram_user_id"] for r in kids.list_alert_subscribers("weekend")], [sub_uid])
        finally:
            kids.close()

        self.engine.unsubscribe_bot(sub_uid, "kids_bot")
        self.engine.attempt_subscribe(sub_uid, "yolo_bot")
        yolo = YoloDatabase(Path(self.tmp_dir.name) / "yolo_brief.sqlite3")
        try:
            for uid in (sub_uid, other_uid):
                yolo.set_user_archetype(uid, SoloArchetype.URBAN_HIPSTER)
            self.assertEqual([u.chat_id for u in yolo.list_active_users()], [sub_uid])
            with self.db._connect() as conn:
                conn.execute("UPDATE users SET status = 'BANNED' WHERE telegram_user_id = ?", (sub_uid,))
            self.assertEqual(yolo.list_active_users(), [])
        finally:
            yolo.close()

        # Standalone `:memory:` stores (no platform tables) are left unfiltered
        mem_kids = KidsCompassDatabase(":memory:")
        mem_kids.ensure_user("1")
        self.assertEqual(len(mem_kids.list_alert_subscribers("weekend")), 1)
        mem_kids.close()
        mem_yolo = YoloDatabase(":memory:")
        mem_yolo.set_user_archetype("1", SoloArchetype.URBAN_HIPSTER)
        self.assertEqual(len(mem_yolo.list_active_users()), 1)
        mem_yolo.close()


if __name__ == "__main__":
    unittest.main()

