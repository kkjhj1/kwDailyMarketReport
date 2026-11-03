"""Unit tests for Travel Pathfinder on-demand interactive poller, cooldown manager, and worker decoupling."""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from travel_brief.flight_engine import FlightEngine
from travel_brief.interactive import (
    GLOBAL_REFRESH_COOLDOWN_SECONDS,
    USER_COOLDOWN_SECONDS,
    TravelInteractiveHandler,
    UserCooldownManager,
)


class TestTravelInteractive(unittest.TestCase):
    def test_user_cooldown_manager_basic_and_debounce(self):
        cd = UserCooldownManager(cooldown_sec=1.0)
        # 1. First click succeeds
        allowed1, toast1 = cd.check_and_acquire("user_100")
        self.assertTrue(allowed1)
        self.assertIn("조회 중", toast1)

        # 2. Immediate second click is blocked by in-progress lock
        allowed2, toast2 = cd.check_and_acquire("user_100")
        self.assertFalse(allowed2)
        self.assertIn("이미 최신 정보를 수집 중입니다", toast2)

        # 3. Another user is independent and allowed
        allowed_other, _ = cd.check_and_acquire("user_200")
        self.assertTrue(allowed_other)
        cd.release("user_200")

        # 4. Release in-progress lock for user_100
        cd.release("user_100")

        # 5. Immediate re-click is blocked by 1.0s cooldown
        allowed3, toast3 = cd.check_and_acquire("user_100")
        self.assertFalse(allowed3)
        self.assertIn("방금 조회되었습니다", toast3)

        # 6. Wait for cooldown to expire
        time.sleep(1.05)
        allowed4, toast4 = cd.check_and_acquire("user_100")
        self.assertTrue(allowed4)
        cd.release("user_100")

    def test_global_force_refresh_cooldown_for_admin_vs_normal_user(self):
        cd = UserCooldownManager(cooldown_sec=0.1)

        # Normal user first refresh allowed
        allowed1, _ = cd.check_and_acquire("user_normal", is_force_refresh=True, is_admin=False)
        self.assertTrue(allowed1)
        cd.release("user_normal")

        # Normal user second refresh within 5 min is blocked
        time.sleep(0.12)
        allowed2, toast2 = cd.check_and_acquire("user_normal", is_force_refresh=True, is_admin=False)
        self.assertFalse(allowed2)
        self.assertIn("전체 새로고침은", toast2)

        # Admin user bypasses global refresh cooldown
        allowed_admin, _ = cd.check_and_acquire("user_admin", is_force_refresh=True, is_admin=True)
        self.assertTrue(allowed_admin)
        cd.release("user_admin")

    def test_inline_keyboard_structure_and_callback_data_length(self):
        handler = TravelInteractiveHandler(bot_token="dummy_token")
        try:
            kb = handler.build_inline_keyboard()
            self.assertIn("inline_keyboard", kb)
            rows = kb["inline_keyboard"]
            self.assertGreaterEqual(len(rows), 4)

            # Check that all callback_data strings are <= 64 bytes (TG-04 rule)
            for row in rows:
                for btn in row:
                    cb = btn.get("callback_data")
                    if cb:
                        self.assertLessEqual(len(cb.encode("utf-8")), 64)
                        self.assertTrue(cb.startswith("trv:"))
        finally:
            handler.shutdown()

    @patch("travel_brief.interactive.TelegramChannel")
    def test_handle_callback_query_immediate_ack_tg04(self, mock_tg_cls):
        mock_tg = MagicMock()
        mock_tg_cls.return_value = mock_tg

        handler = TravelInteractiveHandler(bot_token="dummy_token")
        handler.tg = mock_tg

        try:
            # 1. Normal callback trigger
            handler.handle_callback_query(
                cq_id="cq_12345",
                chat_id="chat_999",
                message_id=42,
                callback_data="trv:q:NRT",
                from_user={"id": 999, "first_name": "Tester"},
            )

            # Assert answer_callback called immediately (TG-04)
            mock_tg.answer_callback.assert_called_once_with(
                "cq_12345", text="✈️ 최신 실시간 항공권을 조회 중입니다...", show_alert=False
            )

            # 2. Immediate second click gets rejected toast immediately
            mock_tg.reset_mock()
            handler.handle_callback_query(
                cq_id="cq_67890",
                chat_id="chat_999",
                message_id=42,
                callback_data="trv:q:NRT",
                from_user={"id": 999, "first_name": "Tester"},
            )
            mock_tg.answer_callback.assert_called_once()
            args, kwargs = mock_tg.answer_callback.call_args
            self.assertEqual(args[0], "cq_67890")
            self.assertIn("수집 중", kwargs.get("text", ""))
        finally:
            handler.shutdown()

    def test_async_execute_and_update_end_to_end_real_engine(self):
        """Validates that _async_execute_and_update actually runs with real FlightEngine methods (C-A verification)."""
        engine = FlightEngine(live_naver=False)
        handler = TravelInteractiveHandler(bot_token="dummy_token", engine=engine)

        edited_payloads = []

        def mock_edit(chat_id, message_id, text, reply_markup=None):
            edited_payloads.append((chat_id, message_id, text, reply_markup))

        handler._safe_edit_message = mock_edit

        try:
            # Pre-acquire lock as handle_callback_query would do
            allowed, _ = handler.cooldown.check_and_acquire("user_test")
            self.assertTrue(allowed)

            # Run worker method synchronously
            handler._async_execute_and_update(
                chat_id="chat_777",
                message_id=123,
                target_city="NRT",
                user_id="user_test",
            )

            # Lock must be released cleanly
            self.assertNotIn("user_test", handler.cooldown._in_progress_users)

            # Message must have been edited with formatted travel briefing
            self.assertEqual(len(edited_payloads), 1)
            chat_id, msg_id, text, markup = edited_payloads[0]
            self.assertEqual(chat_id, "chat_777")
            self.assertEqual(msg_id, 123)
            self.assertIn("도쿄", text)
            self.assertIsNotNone(markup)
        finally:
            handler.shutdown()

    @patch("travel_brief.interactive.NaverFlightLiveClient.invalidate_cache")
    def test_force_refresh_invalidates_cache(self, mock_inv):
        handler = TravelInteractiveHandler(bot_token="dummy_token")
        try:
            handler.handle_callback_query(
                cq_id="cq_ref",
                chat_id="chat_adm",
                message_id=55,
                callback_data="trv:r:ALL",
                from_user={"id": 111},
            )
            mock_inv.assert_called_once()
        finally:
            handler.shutdown()


if __name__ == "__main__":
    unittest.main()
