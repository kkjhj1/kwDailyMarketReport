"""Unit & Integration Tests for Onboarding Latest Briefing Delivery:
- Type A (Common Broadcast Bot: brief_bot): Auto-delivers latest briefing on initial subscription (/start or swap), idempotent on repeat.
- Type B (Target-Selection Bots: admission_bot, travel_bot): Prompts target selection first -> shows [직전 발송 정보 보기] button -> delivers target-specific latest briefing on click.
- Enforces [TG-01] length limits, [TG-02] HTML safety, and [TG-04] <0.1s callback ack & <=64-byte callback_data.
"""
from __future__ import annotations

import io
import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from admission_brief.channels.telegram import TelegramChannel as AdmTelegramChannel
from admission_brief.interactive_bot import AdmissionInteractiveBot
from admission_brief.models import ParentUserProfile, TargetGrade
from admission_brief.storage.db import AdmissionDatabase
from market_brief.multi_bot_manager import (
    COMMON_BRIEFING_BOTS,
    DynamicBotManager,
    MultiBotInteractionRouter,
)
from market_brief.storage.db import MarketBriefDatabase
from market_brief.subscription_engine import SubscriptionEngine
from travel_brief.interactive import TravelInteractiveHandler


class _FakeHTTPResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _visible_len(html_text: str) -> int:
    return len(re.sub(r"<[^>]+>", "", html_text or ""))


class OnboardingBriefingDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_onboarding.sqlite3"
        self.env_path = Path(self.tmp_dir.name) / "empty.env"
        self.db = MarketBriefDatabase(db_path=str(self.db_path), env_path=self.env_path)
        self.engine = SubscriptionEngine(self.db)
        self.router = MultiBotInteractionRouter(self.engine)
        # Upgrade test users to BLACK so they can test multiple bots without slot quota blocks
        self.engine.upsert_user("u_test_1", "테스터1", tier="BLACK")

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_common_bot_brief_bot_auto_delivers_latest_briefing_on_start_and_is_idempotent(self):
        """Type A (brief_bot): Immediately delivers latest market briefing upon entering, without spamming on repeat /start."""
        self.assertIn("brief_bot", COMMON_BRIEFING_BOTS)
        self.assertTrue(self.router.is_common_briefing_bot("brief_bot"))

        # 1. First /start -> SUBSCRIBED + extra_messages has 1 market briefing
        r1 = self.router.handle_interaction("brief_bot", "u_test_1", full_name="테스터1", text="/start")
        self.assertEqual(r1["result_code"], "SUBSCRIBED")
        extras = r1.get("extra_messages") or []
        self.assertEqual(len(extras), 1)
        brief_html = extras[0]["html"]
        self.assertIn("직전 발송 최신 시황 브리핑", brief_html)
        self.assertLessEqual(len(brief_html), 3500)
        self.assertLessEqual(_visible_len(brief_html), 1800)

        # Inline keyboard on welcome card also has on-demand preview button
        cbs = [btn.get("callback_data") for row in r1["inline_keyboard"] for btn in row if "callback_data" in btn]
        self.assertIn("info:preview:brief_bot", cbs)

        # 2. Second /start while already active -> idempotent (no duplicate auto-push)
        r2 = self.router.handle_interaction("brief_bot", "u_test_1", full_name="테스터1", text="/start")
        self.assertEqual(r2["result_code"], "ALREADY_SUBSCRIBED")
        self.assertEqual(len(r2.get("extra_messages") or []), 0)

        # 3. Clicking [📊 직전 발송 최신 시황 브리핑 보기] button returns latest briefing on demand
        r_prev = self.router.handle_interaction(
            "brief_bot", "u_test_1", full_name="테스터1", callback_data="info:preview:brief_bot"
        )
        self.assertEqual(r_prev["result_code"], "BRIEFING_PREVIEW")
        self.assertIn("직전 발송 최신 시황 브리핑", r_prev["reply_html"])

        # 4. Unsubscribe (/stop) then re-subscribe (/start) -> auto-delivers latest briefing again
        self.router.handle_interaction("brief_bot", "u_test_1", text="/stop")
        r3 = self.router.handle_interaction("brief_bot", "u_test_1", text="/start")
        self.assertEqual(r3["result_code"], "SUBSCRIBED")
        self.assertEqual(len(r3.get("extra_messages") or []), 1)

    def test_target_selection_bot_admission_bot_requires_target_then_preview_button(self):
        """Type B (admission_bot): Step 1 target selection -> Step 2 [정보보기] button -> Step 3 grade-specific briefing."""
        self.assertFalse(self.router.is_common_briefing_bot("admission_bot"))

        # Step 1: Enter admission_bot (/start) -> NO extra_messages; shows target selection buttons
        r1 = self.router.handle_interaction("admission_bot", "u_test_1", full_name="학부모", text="/start")
        self.assertEqual(r1["result_code"], "SUBSCRIBED")
        self.assertEqual(len(r1.get("extra_messages") or []), 0)
        self.assertIn("자녀 학년 선택", r1["reply_html"])

        cbs_step1 = [
            btn["callback_data"]
            for row in r1["inline_keyboard"]
            for btn in row
            if "callback_data" in btn
        ]
        self.assertIn("info:target:admission_bot:MIDDLE", cbs_step1)
        self.assertIn("info:target:admission_bot:HIGH_1_2", cbs_step1)
        self.assertIn("info:target:admission_bot:HIGH_3_N", cbs_step1)
        for cb in cbs_step1:
            self.assertLessEqual(len(cb.encode("utf-8")), 64)

        # Step 2: Select target grade (HIGH_3_N) -> shows [📋 [고3·N수] 직전 발송 정보 보기] button
        r2 = self.router.handle_interaction(
            "admission_bot", "u_test_1", callback_data="info:target:admission_bot:HIGH_3_N"
        )
        self.assertEqual(r2["result_code"], "TARGET_SELECTED")
        self.assertEqual(r2["selected_target"], "HIGH_3_N")
        self.assertIn("직전 발송 정보 보기", r2["reply_html"])

        cbs_step2 = [
            btn["callback_data"]
            for row in r2["inline_keyboard"]
            for btn in row
            if "callback_data" in btn
        ]
        self.assertIn("info:preview:admission_bot:HIGH_3_N", cbs_step2)

        # Step 3: Click [📋 [고3·N수] 직전 발송 정보 보기] -> delivers HIGH_3_N admission briefing
        r3 = self.router.handle_interaction(
            "admission_bot", "u_test_1", callback_data="info:preview:admission_bot:HIGH_3_N"
        )
        self.assertEqual(r3["result_code"], "BRIEFING_PREVIEW")
        self.assertIn("직전 발송 맞춤 입시 브리핑", r3["reply_html"])
        self.assertIn("고3", r3["reply_html"])
        self.assertLessEqual(len(r3["reply_html"]), 3500)
        self.assertLessEqual(_visible_len(r3["reply_html"]), 1800)

    def test_target_selection_bot_travel_bot_requires_target_then_preview_button(self):
        """Type B (travel_bot): Step 1 destination selection -> Step 2 [정보보기] button -> Step 3 destination briefing."""
        self.assertFalse(self.router.is_common_briefing_bot("travel_bot"))

        # Step 1: Enter travel_bot (/start) -> NO extra_messages; shows destination buttons
        r1 = self.router.handle_interaction("travel_bot", "u_test_1", full_name="여행자", text="/start")
        self.assertEqual(r1["result_code"], "SUBSCRIBED")
        self.assertEqual(len(r1.get("extra_messages") or []), 0)
        self.assertIn("관심 여행지 선택", r1["reply_html"])

        cbs_step1 = [
            btn["callback_data"]
            for row in r1["inline_keyboard"]
            for btn in row
            if "callback_data" in btn
        ]
        self.assertIn("info:target:travel_bot:NRT", cbs_step1)
        self.assertIn("info:target:travel_bot:KIX", cbs_step1)
        self.assertIn("info:target:travel_bot:FUK", cbs_step1)
        for cb in cbs_step1:
            self.assertLessEqual(len(cb.encode("utf-8")), 64)

        # Step 2: Select destination (NRT / 도쿄) -> shows [📋 [도쿄] 직전 발송 정보 보기] button
        r2 = self.router.handle_interaction(
            "travel_bot", "u_test_1", callback_data="info:target:travel_bot:NRT"
        )
        self.assertEqual(r2["result_code"], "TARGET_SELECTED")
        self.assertEqual(r2["selected_target"], "NRT")

        cbs_step2 = [
            btn["callback_data"]
            for row in r2["inline_keyboard"]
            for btn in row
            if "callback_data" in btn
        ]
        self.assertIn("info:preview:travel_bot:NRT", cbs_step2)

        # Step 3: Click [📋 [도쿄] 직전 발송 정보 보기] -> delivers Tokyo travel briefing
        r3 = self.router.handle_interaction(
            "travel_bot", "u_test_1", callback_data="info:preview:travel_bot:NRT"
        )
        self.assertEqual(r3["result_code"], "BRIEFING_PREVIEW")
        self.assertIn("직전 발송 맞춤 여행 특가", r3["reply_html"])
        self.assertIn("도쿄", r3["reply_html"])
        self.assertLessEqual(len(r3["reply_html"]), 3500)
        self.assertLessEqual(_visible_len(r3["reply_html"]), 1800)

    def test_dynamic_bot_manager_sends_two_messages_for_brief_bot_and_preemptive_ack_tg04(self):
        """Verifies DynamicBotManager sends welcome + briefing for brief_bot, and calls answerCallbackQuery first ([TG-04])."""
        self.engine.upsert_bot("brief_bot", "시황 브리핑", token="111111:FAKE_BRIEF_TOKEN", is_active=True)
        self.engine.update_bot_offset("brief_bot", 700)
        api_call_order: list[tuple[str, dict]] = []

        updates_queue = [
            # Poll 1: /start message on brief_bot
            [
                {
                    "update_id": 701,
                    "message": {
                        "message_id": 10,
                        "from": {"id": 99001, "first_name": "시황신규", "username": "new_brief"},
                        "chat": {"id": 99001, "type": "private", "first_name": "시황신규"},
                        "text": "/start",
                    },
                }
            ],
            # Poll 2: callback_query clicking info:preview:brief_bot
            [
                {
                    "update_id": 702,
                    "callback_query": {
                        "id": "cq_tg04_check",
                        "from": {"id": 99001, "first_name": "시황신규", "username": "new_brief"},
                        "message": {"message_id": 11, "chat": {"id": 99001, "type": "private"}},
                        "data": "info:preview:brief_bot",
                    },
                }
            ],
        ]

        def fake_opener(req, timeout=10):
            url = req.full_url
            if "/getUpdates" in url:
                batch = updates_queue.pop(0) if updates_queue else []
                return _FakeHTTPResp(json.dumps({"ok": True, "result": batch}).encode("utf-8"))
            if "/answerCallbackQuery" in url:
                body = json.loads(req.data.decode("utf-8"))
                api_call_order.append(("answerCallbackQuery", body))
                return _FakeHTTPResp(json.dumps({"ok": True, "result": True}).encode("utf-8"))
            if "/sendMessage" in url:
                body = json.loads(req.data.decode("utf-8"))
                api_call_order.append(("sendMessage", body))
                return _FakeHTTPResp(json.dumps({"ok": True, "result": {"message_id": 99}}).encode("utf-8"))
            return _FakeHTTPResp(json.dumps({"ok": True, "result": {}}).encode("utf-8"))

        mgr = DynamicBotManager(self.engine, self.router)
        # First poll: initial /start on brief_bot -> must send 2 messages (welcome + latest briefing)
        mgr.sync_bot_updates_once("brief_bot", opener=fake_opener)
        send_calls = [b for name, b in api_call_order if name == "sendMessage"]
        self.assertEqual(len(send_calls), 2)
        self.assertIn("구독이 시작되었습니다", send_calls[0]["text"])
        self.assertIn("직전 발송 최신 시황 브리핑", send_calls[1]["text"])

        # Second poll: callback_query -> answerCallbackQuery MUST precede sendMessage ([TG-04])
        api_call_order.clear()
        mgr.sync_bot_updates_once("brief_bot", opener=fake_opener)
        self.assertGreaterEqual(len(api_call_order), 2)
        self.assertEqual(api_call_order[0][0], "answerCallbackQuery")
        self.assertEqual(api_call_order[1][0], "sendMessage")

    def test_standalone_admission_and_travel_handlers_expose_preview_buttons(self):
        """Verifies standalone AdmissionInteractiveBot and TravelInteractiveHandler support target -> preview button flow."""
        # 1. AdmissionInteractiveBot region completion includes adm:todo preview button
        adm_db = AdmissionDatabase(db_path=":memory:")
        adm_db.upsert_subscriber(ParentUserProfile(chat_id="501", target_grade=TargetGrade.HIGH_1_2))
        mock_adm_ch = MagicMock(spec=AdmTelegramChannel)
        adm_bot = AdmissionInteractiveBot(channel=mock_adm_ch, db=adm_db)
        adm_bot.handle_update(
            {
                "update_id": 1,
                "callback_query": {
                    "id": "cb_reg",
                    "data": "region:SEOUL",
                    "message": {"message_id": 7, "chat": {"id": 501}},
                },
            }
        )
        edit_kwargs = mock_adm_ch.edit_message_text.call_args[1]
        reg_markup = edit_kwargs.get("reply_markup", {})
        reg_cbs = [btn["callback_data"] for row in reg_markup.get("inline_keyboard", []) for btn in row]
        self.assertIn("adm:todo", reg_cbs)

        # 2. TravelInteractiveHandler trv:s:NRT acknowledges callback and shows trv:q:NRT preview button
        trv_handler = TravelInteractiveHandler(bot_token="dummy_tok")
        trv_handler.tg = MagicMock()
        edited = []
        trv_handler._safe_edit_message = lambda chat_id, message_id, text, reply_markup=None: edited.append(
            (chat_id, message_id, text, reply_markup)
        )
        try:
            trv_handler.handle_callback_query(
                cq_id="cq_trv_s",
                chat_id="701",
                message_id=12,
                callback_data="trv:s:NRT",
                from_user={"id": 701},
            )
            trv_handler.tg.answer_callback.assert_called_once()
            trv_handler.worker_pool.shutdown(wait=True)
            self.assertEqual(len(edited), 1)
            _, _, confirm_text, markup = edited[0]
            self.assertIn("직전 발송 특가 정보 보기", confirm_text)
            trv_cbs = [
                btn["callback_data"]
                for row in markup["inline_keyboard"]
                for btn in row
                if "callback_data" in btn
            ]
            self.assertIn("trv:q:NRT", trv_cbs)
        finally:
            trv_handler.shutdown()

    def test_silver_kids_yolo_target_selection_and_preview_flow(self):
        """Verifies silver_bot, kids_bot, and yolo_bot follow the 3-step target selection -> [정보보기] -> briefing flow."""
        cases = [
            ("silver_bot", "ALONE", "직전 발송 실버케어 브리핑"),
            ("kids_bot", "PRESCHOOL", "직전 발송 키즈 나침반 브리핑"),
            ("yolo_bot", "URBAN_HIPSTER", "직전 발송 욜로봇 큐레이션"),
        ]
        for bot_id, target_code, expected_banner in cases:
            # 1. /start -> SUBSCRIBED, no auto-push, shows target options
            r1 = self.router.handle_interaction(bot_id, "u_test_1", full_name="통합테스터", text="/start")
            self.assertEqual(r1["result_code"], "SUBSCRIBED")
            self.assertEqual(len(r1.get("extra_messages") or []), 0)
            cbs1 = [btn["callback_data"] for row in r1["inline_keyboard"] for btn in row if "callback_data" in btn]
            self.assertIn(f"info:target:{bot_id}:{target_code}", cbs1)

            # 2. Select target -> TARGET_SELECTED, exposes [직전 발송 정보 보기] button
            r2 = self.router.handle_interaction(
                bot_id, "u_test_1", callback_data=f"info:target:{bot_id}:{target_code}"
            )
            self.assertEqual(r2["result_code"], "TARGET_SELECTED")
            cbs2 = [btn["callback_data"] for row in r2["inline_keyboard"] for btn in row if "callback_data" in btn]
            self.assertIn(f"info:preview:{bot_id}:{target_code}", cbs2)

            # 3. Click [직전 발송 정보 보기] -> BRIEFING_PREVIEW within [TG-01] limits
            r3 = self.router.handle_interaction(
                bot_id, "u_test_1", callback_data=f"info:preview:{bot_id}:{target_code}"
            )
            self.assertEqual(r3["result_code"], "BRIEFING_PREVIEW")
            self.assertIn(expected_banner, r3["reply_html"])
            self.assertLessEqual(len(r3["reply_html"]), 3500)
            self.assertLessEqual(_visible_len(r3["reply_html"]), 1800)


if __name__ == "__main__":
    unittest.main()
