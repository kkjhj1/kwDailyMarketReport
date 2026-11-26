"""WIP (서비스 준비 중) bot direct-chat subscription/access blocking.

Engine layer (`attempt_subscribe` / `atomic_swap_subscription`) and Telegram inbound layer
(`DynamicBotManager._enforce_info_access_gate` / `MultiBotInteractionRouter._handle_info_bot`).
No real Telegram I/O: every HTTP call goes to an in-memory fake opener.
"""
import io
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from market_brief.bot_visibility import (
    BOT_WIP_CODE,
    BOT_WIP_MESSAGE,
    PUBLIC_BOT_IDS,
    WIP_BOT_IDS,
    is_public_bot_id,
    is_wip_bot_id,
)
from market_brief.multi_bot_manager import DynamicBotManager, MultiBotInteractionRouter
from market_brief.storage.db import MarketBriefDatabase
from market_brief.subscription_engine import SubscriptionEngine
from portal_hub.models import PUBLIC_BOT_SERVICES, BotServiceType


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


DOMAIN_DISPATCHERS = (
    "_dispatch_travel_bot_update",
    "_dispatch_admission_bot_update",
    "_dispatch_silver_bot_update",
    "_dispatch_kids_bot_update",
    "_dispatch_yolo_bot_update",
    "_dispatch_chungyak_bot_update",
    "_dispatch_brief_watch_callback",
)
INFO_BOTS = ("brief_bot", "travel_bot", "admission_bot", "silver_bot", "kids_bot", "yolo_bot", "chungyak_bot")


class WipBotAccessTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "alerts.sqlite3"
        self.db = MarketBriefDatabase(db_path=str(self.db_path), env_path=Path(self.tmp_dir.name) / "empty.env")
        self.engine = SubscriptionEngine(self.db)
        self.router = MultiBotInteractionRouter(self.engine)
        with self.db._connect() as conn:
            for i, bid in enumerate(INFO_BOTS):
                conn.execute("UPDATE bots SET token = ?, last_offset = 1 WHERE bot_id = ?", (f"20{i}:FAKE_{bid}", bid))
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

    # ---- helpers -------------------------------------------------------------
    def _opener(self, req, timeout=10):
        method = req.full_url.rsplit("/", 1)[-1].split("?")[0]
        if method == "getUpdates":
            return _FakeResp(json.dumps({"ok": True, "result": self.updates}).encode("utf-8"))
        body = json.loads(req.data.decode("utf-8")) if req.data else {}
        self.calls.append((method, body))
        return _FakeResp(json.dumps({"ok": True, "result": {"message_id": 99}}).encode("utf-8"))

    @staticmethod
    def _cb(uid: str, data: str, update_id: int = 10) -> dict:
        return {
            "update_id": update_id,
            "callback_query": {
                "id": f"cq_{update_id}",
                "from": {"id": int(uid), "first_name": "준비중"},
                "message": {"message_id": 5, "chat": {"id": int(uid), "type": "private"}},
                "data": data,
            },
        }

    @staticmethod
    def _msg(uid: str, text: str, update_id: int = 10) -> dict:
        return {
            "update_id": update_id,
            "message": {
                "message_id": 5,
                "from": {"id": int(uid), "first_name": "준비중"},
                "chat": {"id": int(uid), "type": "private", "first_name": "준비중"},
                "text": text,
            },
        }

    def _manager_with_tripwires(self):
        mgr = DynamicBotManager(self.engine, self.router)
        for name in DOMAIN_DISPATCHERS:
            p = patch.object(mgr, name, side_effect=AssertionError(f"{name} reached"))
            p.start()
            self.addCleanup(p.stop)
        return mgr

    def _run(self, mgr, bot_id: str, update: dict) -> dict:
        self.calls.clear()
        self.updates = [update]
        res = mgr.sync_bot_updates_once(bot_id, opener=self._opener)
        self.assertEqual(res["processed"], 1)
        return res

    def _rows(self, sql: str, params: tuple = ()) -> list:
        conn = sqlite3.connect(self.db_path)
        try:
            return conn.execute(sql, params).fetchall()
        finally:
            conn.close()

    def _assert_wip_card(self):
        sends = [b for m, b in self.calls if m == "sendMessage"]
        self.assertEqual(len(sends), 1, [m for m, _ in self.calls])
        self.assertIn(BOT_WIP_MESSAGE, sends[0]["text"])
        kb = json.dumps(sends[0].get("reply_markup", {}), ensure_ascii=False)
        self.assertIn("https://t.me/KwMenu_bot", kb)
        self.assertNotIn("info:sub:", kb)  # no re-subscribe retry button
        return sends[0]

    # ---- SSOT ----------------------------------------------------------------
    def test_ssot_public_and_wip_sets(self):
        self.assertEqual(
            PUBLIC_BOT_IDS,
            frozenset({"brief_bot", "travel_bot", "kids_bot", "admission_bot", "chungyak_bot"}),
        )
        self.assertEqual(WIP_BOT_IDS, frozenset({"silver_bot", "yolo_bot"}))
        self.assertEqual({m.value for m in PUBLIC_BOT_SERVICES}, set(PUBLIC_BOT_IDS))
        self.assertTrue(BotServiceType.ADMISSION.is_public)
        self.assertTrue(BotServiceType.CHUNGYAK.is_public)
        for bid in ("silver_bot", "yolo_bot"):
            self.assertTrue(is_wip_bot_id(bid), bid)
            self.assertFalse(is_public_bot_id(bid), bid)
        for bid in ("admission_bot", "edu_bot", "chungyak_bot"):
            self.assertTrue(is_public_bot_id(bid), bid)
        for bid in ("brief_bot", "travel_bot", "kids_bot", "admission_bot", "edu_bot", "chungyak_bot",
                    "custom_bot", "", None):
            self.assertFalse(is_wip_bot_id(bid), bid)
        self.assertFalse(PUBLIC_BOT_IDS & WIP_BOT_IDS)

    # ---- 1) WIP USER /start -> BOT_WIP, no user / subscription row ------------
    def test_01_wip_start_blocked_without_db_rows(self):
        uid = "81001"
        res = self.engine.attempt_subscribe(uid, "yolo_bot", full_name="신규")
        self.assertFalse(res["success"])
        self.assertEqual(res["code"], BOT_WIP_CODE)
        self.assertEqual(res["message"], BOT_WIP_MESSAGE)

        mgr = self._manager_with_tripwires()
        self._run(mgr, "yolo_bot", self._msg(uid, "/start"))
        self._assert_wip_card()

        self.assertEqual(self._rows("SELECT 1 FROM users WHERE telegram_user_id = ?", (uid,)), [])
        self.assertEqual(self._rows("SELECT 1 FROM subscriptions WHERE telegram_user_id = ?", (uid,)), [])
        self.assertEqual(
            self._rows("SELECT 1 FROM subscription_audit_logs WHERE telegram_user_id = ?", (uid,)), []
        )

    # ---- 2) WIP callback -> answerCallbackQuery FIRST with show_alert ---------
    def test_02_wip_callback_answers_first_with_alert(self):
        uid = "81002"
        mgr = self._manager_with_tripwires()
        for i, data in enumerate(("info:sub:silver_bot", "SC:home", "info:swap:brief_bot:silver_bot")):
            self._run(mgr, "silver_bot", self._cb(uid, data, 100 + i))
            self.assertEqual(self.calls[0][0], "answerCallbackQuery", "[TG-04] ack must be first")
            self.assertEqual(self.calls[0][1].get("text"), BOT_WIP_MESSAGE)
            self.assertTrue(self.calls[0][1].get("show_alert"))
            self.assertEqual(sum(1 for m, _ in self.calls if m == "answerCallbackQuery"), 1)
            self._assert_wip_card()
        self.assertFalse(self.engine.is_user_subscribed(uid, "silver_bot"))

    # ---- 3) WIP general command -> domain dispatcher never reached ------------
    def test_03_wip_general_commands_never_reach_domain_dispatcher(self):
        uid = "81003"
        mgr = self._manager_with_tripwires()  # any dispatcher call raises AssertionError
        cases = {
            "yolo_bot": ["/weekend", "/curation", "/start", "안녕", "/구독"],
            "silver_bot": ["/parents", "/briefing", "안녕"],
        }
        n = 300
        for bot_id, texts in cases.items():
            for text in texts:
                n += 1
                res = self._run(mgr, bot_id, self._msg(uid, text, n))
                self.assertEqual(res["events"][0]["code"], BOT_WIP_CODE, (bot_id, text))
                self._assert_wip_card()

    # ---- 4) WIP /stop -> unsubscribe always possible; re-subscribe then blocked
    def test_04_wip_stop_always_allowed_and_grandfather(self):
        uid = "81004"
        self.assertEqual(self.engine.attempt_subscribe(uid, "yolo_bot", actor="ADMIN")["code"], "SUBSCRIBED")
        self.assertTrue(self.engine.is_user_subscribed(uid, "yolo_bot"))

        # grandfathered: existing subscriber may still use the bot (/start -> ALREADY_SUBSCRIBED)
        again = self.engine.attempt_subscribe(uid, "yolo_bot")
        self.assertTrue(again["success"])
        self.assertEqual(again["code"], "ALREADY_SUBSCRIBED")

        res = self.engine.unsubscribe_bot(uid, "yolo_bot")
        self.assertTrue(res["success"])
        self.assertFalse(self.engine.is_user_subscribed(uid, "yolo_bot"))

        # after /stop, USER re-subscribe is blocked
        self.assertEqual(self.engine.attempt_subscribe(uid, "yolo_bot")["code"], BOT_WIP_CODE)

        # /stop via Telegram on an un-subscribed WIP bot still routes to the unsubscribe handler
        mgr = self._manager_with_tripwires()
        res = self._run(mgr, "yolo_bot", self._msg(uid, "/stop", 400))
        self.assertNotEqual(res["events"][0]["code"], BOT_WIP_CODE)
        sends = [b for m, b in self.calls if m == "sendMessage"]
        self.assertEqual(len(sends), 1)
        self.assertNotIn("info:sub:", json.dumps(sends[0].get("reply_markup", {})))

        # router-level /stop & info:unsub on a subscribed WIP bot -> UNSUBSCRIBED, no re-subscribe button
        self.engine.attempt_subscribe(uid, "silver_bot", actor="ADMIN")
        out = self.router.handle_interaction("silver_bot", uid, callback_data="info:unsub:silver_bot")
        self.assertEqual(out["result_code"], "UNSUBSCRIBED")
        self.assertNotIn("info:sub:", json.dumps(out["inline_keyboard"]))

    # ---- 5) bypass_quota=True with actor=USER -> still blocked ----------------
    def test_05_bypass_quota_alone_does_not_bypass_wip(self):
        uid = "81005"
        res = self.engine.attempt_subscribe(uid, "silver_bot", actor="USER", bypass_quota=True)
        self.assertEqual(res["code"], BOT_WIP_CODE)
        res = self.engine.attempt_subscribe(uid, "silver_bot", actor="", bypass_quota=True)
        self.assertEqual(res["code"], BOT_WIP_CODE)
        self.assertEqual(self._rows("SELECT 1 FROM users WHERE telegram_user_id = ?", (uid,)), [])

    # ---- 6) actor=ADMIN -> allowed -------------------------------------------
    def test_06_admin_actor_allowed(self):
        uid = "81006"
        for actor in ("ADMIN", "admin", " Admin "):
            self.engine.unsubscribe_bot(uid, "silver_bot")
            res = self.engine.attempt_subscribe(uid, "silver_bot", actor=actor)
            self.assertTrue(res["success"], actor)
            self.assertEqual(res["code"], "SUBSCRIBED")
        self.assertTrue(self.engine.is_user_subscribed(uid, "silver_bot"))

        # Grandfathered subscriber passes the Telegram gate (reaches the domain dispatcher)
        mgr = DynamicBotManager(self.engine, self.router)
        with patch.object(mgr, "_dispatch_silver_bot_update", return_value=(True, "sc_handled")) as disp:
            self._run(mgr, "silver_bot", self._cb(uid, "SC:home", 600))
        disp.assert_called_once()

    # ---- 7) public -> WIP swap fails; old subscription kept --------------------
    def test_07_public_to_wip_swap_blocked_and_old_sub_kept(self):
        uid = "81007"
        self.assertEqual(self.engine.attempt_subscribe(uid, "travel_bot")["code"], "SUBSCRIBED")

        res = self.engine.atomic_swap_subscription(uid, "travel_bot", "yolo_bot", actor="USER")
        self.assertFalse(res["success"])
        self.assertEqual(res["code"], BOT_WIP_CODE)
        self.assertTrue(self.engine.is_user_subscribed(uid, "travel_bot"))
        self.assertFalse(self.engine.is_user_subscribed(uid, "yolo_bot"))

        out = self.router.handle_interaction(
            "yolo_bot", uid, callback_data="info:swap:travel_bot:yolo_bot"
        )
        self.assertEqual(out["result_code"], BOT_WIP_CODE)
        self.assertTrue(self.engine.is_user_subscribed(uid, "travel_bot"))
        active = [s["bot_id"] for s in self.engine.get_user_profile(uid)["active_subscriptions"]]
        self.assertEqual(active, ["travel_bot"])

        # ADMIN swap into WIP is allowed
        res = self.engine.atomic_swap_subscription(uid, "travel_bot", "yolo_bot", actor="ADMIN")
        self.assertTrue(res["success"])

    # ---- 8) WIP -> public swap succeeds ---------------------------------------
    def test_08_wip_to_public_swap_allowed(self):
        uid = "81008"
        self.engine.attempt_subscribe(uid, "silver_bot", actor="ADMIN")
        res = self.engine.atomic_swap_subscription(uid, "silver_bot", "kids_bot", actor="USER")
        self.assertTrue(res["success"])
        self.assertEqual(res["code"], "SWAPPED")
        self.assertFalse(self.engine.is_user_subscribed(uid, "silver_bot"))
        self.assertTrue(self.engine.is_user_subscribed(uid, "kids_bot"))

    # ---- 9) edu_bot alias -> canonical admission_bot (now PUBLIC, not blocked) ---
    def test_09_edu_bot_alias_resolves_to_public_admission(self):
        uid = "81009"
        # Not WIP-gated any more; the engine looks up the raw id (no `edu_bot` row) -> BOT_NOT_FOUND.
        res = self.engine.attempt_subscribe(uid, "edu_bot")
        self.assertFalse(res["success"], res)
        self.assertNotEqual(res["code"], BOT_WIP_CODE)
        self.assertEqual(res["code"], "BOT_NOT_FOUND")
        self.assertFalse(self.engine.is_user_subscribed(uid, "admission_bot"))
        # The canonical id is publicly subscribable.
        res = self.engine.attempt_subscribe(uid, "admission_bot")
        self.assertTrue(res["success"], res)
        self.assertEqual(res["code"], "SUBSCRIBED")

        # Router path for a bot record registered under the legacy alias id is not WIP-gated
        bot = self.engine.get_bot("admission_bot")
        bot.bot_id = "edu_bot"
        with patch.object(self.engine, "get_bot", return_value=bot):
            out = self.router.handle_interaction("edu_bot", "81010", text="/start")
        self.assertNotEqual(out["result_code"], BOT_WIP_CODE)

    # ---- 9b) newly public bots (admission / chungyak) -> USER self-service allowed --
    def test_09b_newly_public_bots_user_subscribe_and_swap(self):
        uid = "81012"
        for bid in ("admission_bot", "chungyak_bot"):
            bot = self.engine.get_bot(bid)
            self.assertIsNotNone(bot, bid)  # no BOT_NOT_FOUND
            self.assertTrue(bot.is_active, bid)  # no BOT_INACTIVE
        res = self.engine.attempt_subscribe(uid, "chungyak_bot", actor="USER")
        self.assertTrue(res["success"], res)
        self.assertEqual(res["code"], "SUBSCRIBED")
        res = self.engine.atomic_swap_subscription(uid, "chungyak_bot", "admission_bot", actor="USER")
        self.assertTrue(res["success"], res)
        self.assertEqual(res["code"], "SWAPPED")
        self.assertTrue(self.engine.is_user_subscribed(uid, "admission_bot"))
        self.assertFalse(self.engine.is_user_subscribed(uid, "chungyak_bot"))
        with self.db._connect() as conn:
            row = conn.execute(
                "SELECT is_active, telegram_username FROM bots WHERE bot_id = 'chungyak_bot'"
            ).fetchone()
        self.assertEqual(tuple(row), (1, "KwAptApply_bot"))

    # ---- 10) HTML 3,500 chars & callback_data 64 bytes -------------------------
    def test_10_wip_response_respects_telegram_limits(self):
        bot = self.engine.get_bot("yolo_bot")
        bot.name = "<b>&" + "욜" * 5000  # hostile/oversized name must be escaped and clipped
        out = self.router.render_wip_response(bot, "81011")
        self.assertEqual(out["result_code"], BOT_WIP_CODE)
        self.assertLessEqual(len(out["reply_html"]), 3500)
        self.assertNotIn("<b>&", out["reply_html"])
        self.assertTrue(out["show_alert"])
        self.assertEqual(out["callback_toast"], BOT_WIP_MESSAGE)
        self.assertLessEqual(len(out["callback_toast"]), 200)
        buttons = [btn for row in out["inline_keyboard"] for btn in row]
        self.assertEqual(len(buttons), 1)
        self.assertEqual(buttons[0]["url"], "https://t.me/KwMenu_bot")
        for btn in buttons:
            if "callback_data" in btn:
                self.assertLessEqual(len(btn["callback_data"].encode("utf-8")), 64)

        mgr = self._manager_with_tripwires()
        self._run(mgr, "yolo_bot", self._cb("81011", "yolo:" + "x" * 200, 700))
        sent = self._assert_wip_card()
        self.assertLessEqual(len(sent["text"]), 3500)
        for row in (sent.get("reply_markup") or {}).get("inline_keyboard", []):
            for btn in row:
                if "callback_data" in btn:
                    self.assertLessEqual(len(btn["callback_data"].encode("utf-8")), 64)


if __name__ == "__main__":
    unittest.main()
