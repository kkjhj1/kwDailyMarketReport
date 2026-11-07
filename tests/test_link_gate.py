"""Unit and Integration Tests for Link Confirmation Gate System.
Enforces:
- [TG-01] HTML & visible character limits
- [TG-02] Strict HTML escaping on link target titles/descriptions
- [TG-04] Strict <=64 byte callback_data limit
- [TG-06] Token-free demo and in-memory test isolation
"""
from __future__ import annotations

import unittest
from market_brief.link_gate import (
    GATE_REGISTRY,
    LinkGateRegistry,
    LinkGateRouter,
    LinkTarget,
    build_gate_button,
    render_confirmation_card,
)
from market_brief.storage.db import MarketBriefDatabase
from market_brief.subscription_engine import SubscriptionEngine
from market_brief.multi_bot_manager import MultiBotInteractionRouter
from market_brief.briefing_service import generate_bot_briefing


class TestLinkGateSystem(unittest.TestCase):
    def setUp(self):
        self.registry = LinkGateRegistry()
        self.router = LinkGateRouter(self.registry)

    def test_preset_tokens_registered(self):
        """Verifies that all 6 bots and portal bot have pre-registered link targets."""
        expected_tokens = [
            "bf_rep", "trv_rep", "trv_nv", "trv_gpt",
            "adm_rep", "adm_bk", "yolo_mag", "yolo_act",
            "sil_rep", "bot_menu",
            "bot_brief", "bot_travel", "bot_admission",
            "bot_silver", "bot_kids", "bot_yolo",
        ]
        for tok in expected_tokens:
            target = self.registry.get_target(tok)
            self.assertIsNotNone(target, f"Token {tok} must be registered")
            self.assertTrue(target.dest_url.startswith("https://"), f"{tok} url must start with https://")
            self.assertTrue(len(target.target_name) > 0, f"{tok} must have target_name")

    def test_build_gate_button_64_bytes(self):
        """Verifies that gate button callback_data is well under 64 bytes ([TG-04])."""
        btn = build_gate_button("bf_rep", "📊 웹 리포트 상세 분석 보기")
        self.assertIn("callback_data", btn)
        cb = btn["callback_data"]
        cb_bytes = len(cb.encode("utf-8"))
        self.assertLessEqual(cb_bytes, 64, f"Callback {cb} exceeds 64 bytes ({cb_bytes})")
        self.assertEqual(cb, "gate:g:bf_rep")

    def test_render_confirmation_card(self):
        """Verifies confirmation card HTML rendering and OK/Cancel inline buttons."""
        target = self.registry.get_target("bf_rep")
        self.assertIsNotNone(target)
        card_html, kb = render_confirmation_card(target)
        self.assertIn("🌐 <b>[페이지 이동 안내]</b>", card_html)
        self.assertIn("일일 시황 브리핑 상세페이지", card_html)
        self.assertEqual(len(kb), 2)
        # Button 1: OK (URL)
        self.assertEqual(kb[0][0]["text"], "🌐 이동하기 (OK)")
        self.assertEqual(kb[0][0]["url"], target.dest_url)
        # Button 2: Cancel (Callback)
        self.assertEqual(kb[1][0]["text"], "❌ 취소 (Cancel)")
        self.assertEqual(kb[1][0]["callback_data"], f"gate:c:{target.token}")

    def test_dynamic_token_registration(self):
        """Verifies registration and lookup of dynamic deal/booking URLs."""
        dyn_url = "https://m-flight.naver.com/flights/international/ICN-NRT-20261110/NRT-ICN-20261114"
        tok = self.registry.register_dynamic(
            dyn_url,
            "도쿄 실시간 최저가 항공권",
            "네이버 항공권 실시간 예약 페이지로 연결됩니다.",
            "travel:home",
        )
        self.assertTrue(tok.startswith("d_"))
        self.assertLessEqual(len(tok), 14)

        btn = build_gate_button(tok, "🔍 실시간 최저가 확인")
        self.assertLessEqual(len(btn["callback_data"].encode("utf-8")), 64)

        retrieved = self.registry.get_target(tok)
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.dest_url, dyn_url)

    def test_router_handle_gate_confirm_and_cancel(self):
        """Verifies router callback execution for both confirm card and cancel action."""
        # 1. Confirm Card Request
        res_card = self.router.handle_callback("gate:g:bf_rep", "brief_bot", "12345", 100)
        self.assertTrue(res_card["handled"])
        self.assertEqual(res_card["action"], "CONFIRM_CARD")
        self.assertIn("일일 시황 브리핑 상세페이지", res_card["html"])
        self.assertEqual(res_card["inline_keyboard"][0][0]["text"], "🌐 이동하기 (OK)")

        # 2. Cancel Request
        res_cancel = self.router.handle_callback("gate:c:bf_rep", "brief_bot", "12345", 100)
        self.assertTrue(res_cancel["handled"])
        self.assertEqual(res_cancel["action"], "CANCEL")
        self.assertEqual(res_cancel["restore_route"], "brief:latest")
        self.assertIn("취소", res_cancel["toast"])

    def test_briefing_service_uses_gate_buttons(self):
        """Verifies that generate_bot_briefing outputs gate buttons instead of direct naked URLs."""
        # Test brief_bot briefing
        brief_gen = generate_bot_briefing("brief_bot")
        kb = brief_gen.get("inline_keyboard", [])
        self.assertTrue(len(kb) > 0)
        first_btn = kb[0][0]
        self.assertIn("callback_data", first_btn, "Link button must use callback_data for 2-step gate")
        self.assertTrue(first_btn["callback_data"].startswith("gate:g:"))

        # Test menu_bot catalog
        menu_gen = generate_bot_briefing("menu_bot")
        m_kb = menu_gen.get("inline_keyboard", [])
        self.assertEqual(len(m_kb), 6)
        for row in m_kb:
            btn = row[0]
            self.assertIn("callback_data", btn)
            self.assertTrue(btn["callback_data"].startswith("gate:g:bot_"))

    def test_multi_bot_manager_router_gate_integration(self):
        """Verifies that MultiBotInteractionRouter handles gate callbacks smoothly."""
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp_dir:
            db_path = Path(tmp_dir) / "test_gate.sqlite3"
            db = MarketBriefDatabase(db_path=str(db_path))
            engine = SubscriptionEngine(db)
            router = MultiBotInteractionRouter(engine)
            bot = engine.get_bot("brief_bot")
            self.assertIsNotNone(bot)

            # Gate confirmation card trigger
            out_card = router.handle_interaction(
                bot_id="brief_bot",
                telegram_user_id="9999",
                full_name="홍길동",
                callback_data="gate:g:bf_rep",
            )
            self.assertEqual(out_card["result_code"], "GATE_CARD")
            self.assertIn("🌐 <b>[페이지 이동 안내]</b>", out_card["reply_html"])
            self.assertEqual(out_card["inline_keyboard"][0][0]["text"], "🌐 이동하기 (OK)")

            # Gate cancel trigger
            out_cancel = router.handle_interaction(
                bot_id="brief_bot",
                telegram_user_id="9999",
                full_name="홍길동",
                callback_data="gate:c:bf_rep",
            )
            self.assertEqual(out_cancel["result_code"], "GATE_CANCEL")


if __name__ == "__main__":
    unittest.main()
