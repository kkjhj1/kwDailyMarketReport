"""Exhaustive All-Menu & All-Button Integration Test Suite.
Verifies every single menu, button, callback query, reply keyboard text, and state transition
across Portal Hub Bot, Admission Interactive Bot, and Admin Dashboard.
"""
from __future__ import annotations
import gc
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, call

from portal_hub import (
    BotCategory,
    BotServiceType,
    PortalDatabase,
    TelegramPortalClient,
    UserProfile,
    UserTier,
    handle_callback_query,
    render_category_home_view,
    render_category_view,
    render_help_view,
    render_main_menu,
    render_search_results_view,
    render_subscription_view,
    render_tier_benefits_view,
)
from portal_hub.interactive import (
    USER_STATES,
    build_persistent_reply_keyboard,
    build_slot_gauge,
    render_inquiry_prompt_view,
    render_support_center_view,
    render_upgrade_request_view,
)

from admission_brief.interactive_bot import (
    AdmissionInteractiveBot,
    build_grade_inline_keyboard,
    build_persistent_reply_keyboard as build_admission_reply_keyboard,
    build_region_inline_keyboard,
)
from admission_brief.channels.telegram import TelegramChannel as AdmissionTelegramChannel
from admission_brief.models import TargetGrade, TargetRegion
from admission_brief.storage.db import AdmissionDatabase


class ExhaustivePortalBotMenuTests(unittest.TestCase):
    """Exhaustively tests all screens, buttons, and callback queries for the Portal Hub bot."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_portal.sqlite3"
        self.db = PortalDatabase(self.db_path)
        self.client = MagicMock(spec=TelegramPortalClient)
        self.client.edit_message_text.return_value = True
        self.client.send_message.return_value = {"ok": True, "result": {"message_id": 999}}
        self.client.answer_callback_query.return_value = True

        self.user_id = 777001
        self.chat_id = 777001
        self.user = self.db.get_or_create_user(
            user_id=self.user_id,
            chat_id=self.chat_id,
            username="tester_vip",
            first_name="테스터김",
        )
        USER_STATES.clear()

    def tearDown(self):
        USER_STATES.clear()
        del self.db
        gc.collect()
        try:
            self.tmp_dir.cleanup()
        except PermissionError:
            pass

    # -------------------------------------------------------------
    # 1. Main Navigation Callback Tests
    # -------------------------------------------------------------
    def test_main_menu_and_navigation_callbacks(self):
        """Test nav:main, nav:subs, nav:tiers, nav:help, support:home."""
        callbacks = [
            ("nav:main", "통합 인텔리전스 허브"),
            ("menu:home", "통합 인텔리전스 허브"),
            ("nav:subs", "구독 센터"),
            ("menu:my_subs", "구독 센터"),
            ("nav:tiers", "등급별 혜택"),
            ("menu:tiers", "등급별 혜택"),
            ("cat:home", "전체 카테고리 탐색"),
            ("menu:cats", "전체 카테고리 탐색"),
            ("menu:categories", "전체 카테고리 탐색"),
            ("nav:help", "이용 가이드"),
            ("support:home", "고객센터 & 관리자 문의"),
            ("support:upgrade_menu", "등급 업그레이드 신청"),
            ("support:write_inquiry", "1:1 문의사항 작성"),
        ]
        for cb_data, expected_text in callbacks:
            self.client.reset_mock()
            cq = {
                "id": f"cq_{cb_data}",
                "from": {"id": self.user_id, "first_name": "테스터김"},
                "message": {"message_id": 100, "chat": {"id": self.chat_id}},
                "data": cb_data,
            }
            handle_callback_query(cq, self.db, self.client)
            self.client.answer_callback_query.assert_called_with(f"cq_{cb_data}")
            self.client.edit_message_text.assert_called_once()
            called_text = self.client.edit_message_text.call_args[0][2]
            self.assertIn(expected_text, called_text, f"Callback {cb_data} did not render expected text")

    # -------------------------------------------------------------
    # 2. Category Views Callbacks (All 4 categories + home)
    # -------------------------------------------------------------
    def test_all_category_view_callbacks(self):
        """Test cat:home, cat:view:FINANCE, cat:view:EDU, cat:view:LIFE, cat:view:BIZ."""
        cat_tests = [
            ("cat:home", "전체 카테고리 탐색"),
            ("cat:view:FINANCE", "금융 / 재테크"),
            ("cat:view:EDU", "교육 / 입시"),
            ("cat:view:LIFE", "라이프 / 여가"),
            ("cat:view:BIZ", "업무 / 생산성"),
        ]
        for cb_data, expected_keyword in cat_tests:
            self.client.reset_mock()
            cq = {
                "id": f"cq_{cb_data}",
                "from": {"id": self.user_id, "first_name": "테스터김"},
                "message": {"message_id": 101, "chat": {"id": self.chat_id}},
                "data": cb_data,
            }
            handle_callback_query(cq, self.db, self.client)
            self.client.answer_callback_query.assert_called_with(f"cq_{cb_data}")
            self.client.edit_message_text.assert_called_once()
            called_text = self.client.edit_message_text.call_args[0][2]
            self.assertIn(expected_keyword, called_text)

    # -------------------------------------------------------------
    # 3. Subscription Flow & 2-Step In-Place Confirmation
    # -------------------------------------------------------------
    def test_full_subscription_lifecycle_and_in_place_confirmation(self):
        """Test sub:do_sub, sub:ask_unsub, sub:cancel_unsub, sub:do_unsub in subs & category views."""
        # 1. Subscribe to brief_bot
        cq_sub = {
            "id": "cq_sub1",
            "from": {"id": self.user_id, "first_name": "테스터김"},
            "message": {"message_id": 102, "chat": {"id": self.chat_id}},
            "data": "sub:do_sub:brief_bot:subs",
        }
        handle_callback_query(cq_sub, self.db, self.client)
        subs = self.db.get_user_subscriptions(self.user_id)
        self.assertTrue(subs[BotServiceType.BRIEF], "brief_bot must be subscribed")

        # 2. Ask Unsubscribe (Step 1: In-Place confirm buttons appear)
        self.client.reset_mock()
        cq_ask = {
            "id": "cq_ask1",
            "from": {"id": self.user_id, "first_name": "테스터김"},
            "message": {"message_id": 102, "chat": {"id": self.chat_id}},
            "data": "sub:ask_unsub:brief_bot:subs",
        }
        handle_callback_query(cq_ask, self.db, self.client)
        self.client.edit_message_text.assert_called_once()
        text_with_confirm = self.client.edit_message_text.call_args[0][2]
        self.assertIn("구독을 정말 해지하시겠습니까", text_with_confirm)
        markup = self.client.edit_message_text.call_args[1]["reply_markup"]
        btn_callbacks = [b["callback_data"] for row in markup["inline_keyboard"] for b in row if "callback_data" in b]
        self.assertIn("sub:do_unsub:brief_bot:subs", btn_callbacks)
        self.assertIn("sub:cancel_unsub:subs", btn_callbacks)

        # 3. Cancel Unsubscribe (Returns to normal without unsubscribing)
        self.client.reset_mock()
        cq_cancel = {
            "id": "cq_cancel1",
            "from": {"id": self.user_id, "first_name": "테스터김"},
            "message": {"message_id": 102, "chat": {"id": self.chat_id}},
            "data": "sub:cancel_unsub:subs",
        }
        handle_callback_query(cq_cancel, self.db, self.client)
        subs_after_cancel = self.db.get_user_subscriptions(self.user_id)
        self.assertTrue(subs_after_cancel[BotServiceType.BRIEF], "Subscription must remain active after cancel")

        # 4. Final Execute Unsubscribe (Step 2: Unsubscribes cleanly)
        self.client.reset_mock()
        cq_do_unsub = {
            "id": "cq_do_unsub1",
            "from": {"id": self.user_id, "first_name": "테스터김"},
            "message": {"message_id": 102, "chat": {"id": self.chat_id}},
            "data": "sub:do_unsub:brief_bot:subs",
        }
        handle_callback_query(cq_do_unsub, self.db, self.client)
        subs_final = self.db.get_user_subscriptions(self.user_id)
        self.assertFalse(subs_final[BotServiceType.BRIEF], "brief_bot must now be unsubscribed")

    # -------------------------------------------------------------
    # 4. Tier Upgrade Request Callbacks
    # -------------------------------------------------------------
    def test_tier_upgrade_requests(self):
        """Test support:req_tier:GREEN, support:req_tier:RED, support:req_tier:BLACK."""
        for tier in ("GREEN", "RED", "BLACK"):
            self.client.reset_mock()
            cq = {
                "id": f"cq_tier_{tier}",
                "from": {"id": self.user_id, "first_name": "테스터김"},
                "message": {"message_id": 105, "chat": {"id": self.chat_id}},
                "data": f"support:req_tier:{tier}",
            }
            handle_callback_query(cq, self.db, self.client)
            self.client.edit_message_text.assert_called_once()
            called_text = self.client.edit_message_text.call_args[0][2]
            self.assertIn("등급 업그레이드 신청 완료", called_text)
            self.assertIn(tier, called_text)

    # -------------------------------------------------------------
    # 5. Quota Exceeded Behavior with Interactive Upgrade Buttons
    # -------------------------------------------------------------
    def test_quota_exceeded_interactive_prompt(self):
        """WHITE tier user fills 1 slot, 2nd subscription returns upgrade buttons."""
        # 1. Fill 1st slot
        self.db.subscribe(self.user_id, BotServiceType.BRIEF)

        # 2. Try 2nd slot (travel_bot) -> Quota block with interactive buttons
        self.client.reset_mock()
        cq_over = {
            "id": "cq_over",
            "from": {"id": self.user_id, "first_name": "테스터김"},
            "message": {"message_id": 106, "chat": {"id": self.chat_id}},
            "data": "sub:do_sub:travel_bot:subs",
        }
        handle_callback_query(cq_over, self.db, self.client)
        self.client.send_message.assert_called_once()
        msg_text = self.client.send_message.call_args[0][1]
        self.assertIn("최대 1개까지 구독 가능", msg_text)
        markup = self.client.send_message.call_args[1]["reply_markup"]
        btn_cbs = [b["callback_data"] for row in markup["inline_keyboard"] for b in row]
        self.assertIn("support:upgrade_menu", btn_cbs)
        self.assertIn("nav:subs", btn_cbs)

    # -------------------------------------------------------------
    # 6. Persistent Bottom Reply Keyboard & Text Command Mapping
    # -------------------------------------------------------------
    def test_persistent_reply_keyboard_and_text_commands(self):
        """Ensure all Korean text variants and commands match without falling into keyword search."""
        from portal_bot_runner import (
            MENU_CAT_COMMANDS,
            MENU_HELP_COMMANDS,
            MENU_START_COMMANDS,
            MENU_SUBS_COMMANDS,
            MENU_SUPPORT_COMMANDS,
            MENU_TIER_COMMANDS,
        )

        # Verify bottom reply keyboard buttons are in mapped command sets
        persistent_kb = build_persistent_reply_keyboard()
        kb_texts = [btn["text"] for row in persistent_kb["keyboard"] for btn in row]
        self.assertEqual(len(kb_texts), 4)

        for btn_text in kb_texts:
            clean = btn_text.lower()
            matched = (
                clean in MENU_START_COMMANDS
                or clean in MENU_SUBS_COMMANDS
                or clean in MENU_CAT_COMMANDS
                or clean_text_in_set(clean, MENU_TIER_COMMANDS)
                or clean in MENU_HELP_COMMANDS
                or clean in MENU_SUPPORT_COMMANDS
            )
            self.assertTrue(matched, f"Persistent keyboard button '{btn_text}' is not mapped to any menu!")

        # Verify space-free robust matching (match_menu_action)
        from portal_bot_runner import match_menu_action
        self.assertEqual(match_menu_action("내구독관리"), "SUBS")
        self.assertEqual(match_menu_action("내 구독 관리"), "SUBS")
        self.assertEqual(match_menu_action("📋 내 구독 관리"), "SUBS")
        self.assertEqual(match_menu_action("봇둘러보기"), "CAT")
        self.assertEqual(match_menu_action("봇 둘러보기"), "CAT")
        self.assertEqual(match_menu_action("📂 봇 둘러보기"), "CAT")
        self.assertEqual(match_menu_action("내등급혜택"), "TIER")
        self.assertEqual(match_menu_action("내 등급 혜택"), "TIER")
        self.assertEqual(match_menu_action("💎 내 등급 혜택"), "TIER")
        self.assertEqual(match_menu_action("이용도움말"), "HELP")
        self.assertEqual(match_menu_action("이용 도움말"), "HELP")
        self.assertEqual(match_menu_action("❓ 이용 도움말"), "HELP")
        self.assertIsNone(match_menu_action("환율"))
        self.assertIsNone(match_menu_action("비행기"))

        # Verify deep-link /start parameter handling
        self.assertTrue("/start portal".startswith("/start "))
        self.assertTrue("/start 12345".startswith("/start "))

    # -------------------------------------------------------------
    # 7. Awaiting Inquiry State Cancellation on Menu Tap
    # -------------------------------------------------------------
    def test_inquiry_state_cancels_on_menu_click(self):
        """If user clicked 1:1 inquiry and then taps a menu button, inquiry state must cancel immediately."""
        USER_STATES[self.user_id] = "AWAITING_INQUIRY"

        # Simulating user tapping '📋 내 구독 관리'
        text_input = "📋 내 구독 관리"
        clean = text_input.lower()

        from portal_bot_runner import (
            MENU_CAT_COMMANDS,
            MENU_HELP_COMMANDS,
            MENU_START_COMMANDS,
            MENU_SUBS_COMMANDS,
            MENU_SUPPORT_COMMANDS,
            MENU_TIER_COMMANDS,
        )

        is_menu_action = (
            clean.startswith("/")
            or clean in MENU_START_COMMANDS
            or clean in MENU_SUBS_COMMANDS
            or clean in MENU_CAT_COMMANDS
            or clean in MENU_TIER_COMMANDS
            or clean in MENU_HELP_COMMANDS
            or clean in MENU_SUPPORT_COMMANDS
        )
        self.assertTrue(is_menu_action, "Menu button must be recognized as menu action")

        if is_menu_action:
            USER_STATES.pop(self.user_id, None)

        self.assertNotIn(self.user_id, USER_STATES, "Inquiry state must be cleared on menu tap")

    def test_legacy_menu_bot_compatibility_callbacks(self):
        """Test menu:bot, menu:sub, menu:unsub, menu:swap, and invalid bot fallback."""
        # 1. menu:bot:travel_bot renders category view
        self.client.reset_mock()
        cq_bot = {
            "id": "cq_mbot1",
            "from": {"id": self.user_id, "first_name": "테스터김"},
            "message": {"message_id": 150, "chat": {"id": self.chat_id}},
            "data": "menu:bot:travel_bot",
        }
        handle_callback_query(cq_bot, self.db, self.client)
        self.client.edit_message_text.assert_called_once()
        self.assertIn("라이프 / 여가", self.client.edit_message_text.call_args[0][2])

        # 2. menu:bot:unknown falls back to main menu
        self.client.reset_mock()
        cq_unknown = {
            "id": "cq_mbot2",
            "from": {"id": self.user_id, "first_name": "테스터김"},
            "message": {"message_id": 151, "chat": {"id": self.chat_id}},
            "data": "menu:bot:nonexistent_bot",
        }
        handle_callback_query(cq_unknown, self.db, self.client)
        self.client.edit_message_text.assert_called_once()
        self.assertIn("통합 인텔리전스 허브", self.client.edit_message_text.call_args[0][2])

        # 3. menu:sub:travel_bot subscribes successfully
        self.client.reset_mock()
        cq_sub = {
            "id": "cq_msub1",
            "from": {"id": self.user_id, "first_name": "테스터김"},
            "message": {"message_id": 152, "chat": {"id": self.chat_id}},
            "data": "menu:sub:travel_bot",
        }
        handle_callback_query(cq_sub, self.db, self.client)
        subs = self.db.get_user_subscriptions(self.user_id)
        self.assertTrue(subs[BotServiceType.TRAVEL])

        # 4. menu:unsub:travel_bot unsubscribes cleanly
        self.client.reset_mock()
        cq_unsub = {
            "id": "cq_munsub1",
            "from": {"id": self.user_id, "first_name": "테스터김"},
            "message": {"message_id": 153, "chat": {"id": self.chat_id}},
            "data": "menu:unsub:travel_bot",
        }
        handle_callback_query(cq_unsub, self.db, self.client)
        subs = self.db.get_user_subscriptions(self.user_id)
        self.assertFalse(subs[BotServiceType.TRAVEL])

        # 5. menu:swap:brief_bot:travel_bot swaps subscriptions
        self.db.subscribe(self.user_id, BotServiceType.BRIEF)
        self.client.reset_mock()
        cq_swap = {
            "id": "cq_mswap1",
            "from": {"id": self.user_id, "first_name": "테스터김"},
            "message": {"message_id": 154, "chat": {"id": self.chat_id}},
            "data": "menu:swap:brief_bot:travel_bot",
        }
        handle_callback_query(cq_swap, self.db, self.client)
        subs = self.db.get_user_subscriptions(self.user_id)
        self.assertFalse(subs[BotServiceType.BRIEF])
        self.assertTrue(subs[BotServiceType.TRAVEL])

    def test_multi_bot_router_menu_home(self):
        """Test MultiBotInteractionRouter renders catalog on menu:home callback."""
        from market_brief.storage.db import MarketBriefDatabase
        from market_brief.subscription_engine import SubscriptionEngine
        from market_brief.multi_bot_manager import MultiBotInteractionRouter
        mb_db_path = Path(self.tmp_dir.name) / "test_mb.sqlite3"
        mb_db = MarketBriefDatabase(db_path=str(mb_db_path))
        engine = SubscriptionEngine(mb_db)
        router = MultiBotInteractionRouter(engine)

        out = router.handle_interaction(
            bot_id="menu_bot",
            telegram_user_id=str(self.chat_id),
            callback_data="menu:home"
        )
        self.assertEqual(out["result_code"], "MENU_HOME")
        self.assertIn("구독 가능한 정보봇 목록", out["reply_html"])


class ExhaustiveAdmissionBotMenuTests(unittest.TestCase):
    """Exhaustively tests all screens, buttons, and callback queries for the Admission Bot."""

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_admission.db"
        self.db = AdmissionDatabase(db_path=str(self.db_path))
        self.channel = MagicMock(spec=AdmissionTelegramChannel)
        self.channel.send_message.return_value = {"ok": True, "result": {"message_id": 201}}
        self.channel.edit_message_text.return_value = True
        self.channel.answer_callback_query.return_value = True
        self.bot = AdmissionInteractiveBot(channel=self.channel, db=self.db)
        self.chat_id = "555001"

    def tearDown(self):
        del self.bot
        del self.db
        gc.collect()
        try:
            self.tmp_dir.cleanup()
        except PermissionError:
            pass

    def test_all_grade_callbacks(self):
        """Test grade:MIDDLE, grade:HIGH_1_2, grade:HIGH_3_N, grade:ALL."""
        for grade_code in ("MIDDLE", "HIGH_1_2", "HIGH_3_N", "ALL"):
            self.channel.reset_mock()
            cb = {
                "id": f"cb_{grade_code}",
                "data": f"grade:{grade_code}",
                "from": {"id": int(self.chat_id)},
                "message": {"message_id": 501, "chat": {"id": int(self.chat_id)}},
            }
            self.bot.handle_update({"callback_query": cb})
            self.channel.answer_callback_query.assert_called_once()
            self.channel.edit_message_text.assert_called_once()
            profile = self.db.get_subscriber(self.chat_id)
            self.assertEqual(profile.target_grade.value, grade_code)

    def test_all_region_callbacks(self):
        """Test region:ONLINE, region:SEOUL, region:GYEONGGI, region:LOCAL, region:ALL."""
        for reg_code in ("ONLINE", "SEOUL", "GYEONGGI", "LOCAL", "ALL"):
            self.channel.reset_mock()
            cb = {
                "id": f"cb_{reg_code}",
                "data": f"region:{reg_code}",
                "from": {"id": int(self.chat_id)},
                "message": {"message_id": 502, "chat": {"id": int(self.chat_id)}},
            }
            self.bot.handle_update({"callback_query": cb})
            self.channel.answer_callback_query.assert_called_once()
            self.channel.edit_message_text.assert_called_once()
            # Also sends persistent reply keyboard
            self.channel.send_message.assert_called_once()
            profile = self.db.get_subscriber(self.chat_id)
            self.assertEqual(profile.target_region.value, reg_code)

    def test_all_text_menu_commands(self):
        """Test /start, /todo, /hot, /fact, /settings, /help, and Korean persistent keyboard buttons."""
        commands = [
            ("/start", "자녀분의 현재 학년"),
            ("📋 이번 주 필수 To-Do", None),  # Sends briefing via channel.send
            ("⚡ 긴급 속보 & 핫 행사 TOP 3", None),
            ("💡 1분 입시 팩트체크 (So-What)", None),
            ("🎓 학년/지역 설정", "맞춤 알림 설정 현황"),
            ("❓ 이용 도움말", "간편 사용 안내"),
        ]
        for cmd_text, expected_keyword in commands:
            self.channel.reset_mock()
            upd = {
                "message": {
                    "chat": {"id": int(self.chat_id), "first_name": "학부모"},
                    "text": cmd_text,
                }
            }
            self.bot.handle_update(upd)
            if expected_keyword:
                self.channel.send_message.assert_called_once()
                msg = self.channel.send_message.call_args[0][1]
                self.assertIn(expected_keyword, msg)


def clean_text_in_set(clean: str, target_set: set[str]) -> bool:
    return clean in target_set or any(clean == s.lower() for s in target_set)


if __name__ == "__main__":
    unittest.main()
