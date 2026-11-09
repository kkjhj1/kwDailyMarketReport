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

    def test_force_refresh_invalidates_disk_cache_real_trigger(self):
        """Validates that trv:r:ALL invalidates the fresh disk cache (keeping only an emergency backup)
        and triggers uncached collection without mocks (M-F)."""
        import tempfile
        from datetime import date, timedelta
        from travel_brief.models import FlightDeal
        from travel_brief.naver_live import (
            CACHE_TTL_SECONDS,
            STALE_FALLBACK_SECONDS,
            NaverFlightLiveClient,
            _CACHE_LOCK,
            _MEM_CACHE,
        )

        with tempfile.TemporaryDirectory() as tmp_dir:
            custom_cache = Path(tmp_dir) / "test_live_cache.json"
            live_client = NaverFlightLiveClient(cache_path=custom_cache)
            engine = FlightEngine(live_naver=True)
            engine._live_client = live_client

            # 1. Pre-seed disk cache
            dest = engine.destinations["NRT"]
            today = date(2026, 11, 20)
            slots = [(today, today + timedelta(days=3), "목", "일", 3)]
            mock_deal = FlightDeal(
                destination_code="NRT",
                destination_name="도쿄",
                origin_code="ICN",
                departure_date="2026-11-20",
                return_date="2026-11-23",
                departure_day_of_week="목",
                return_day_of_week="일",
                nights=3,
                airline="티웨이항공",
                price_krw=300000,
                baseline_krw=350000,
                is_peak_holiday=False,
                holiday_name="",
                data_source="LIVE_PG",
            )
            disk_key = "2026-11-20:6:NRT"
            live_client._save_disk_cache(disk_key, [mock_deal])
            self.assertTrue(custom_cache.exists())

            # Clear memory cache
            with _CACHE_LOCK:
                _MEM_CACHE.clear()

            # First fetch should read from disk cache (uncached collection not called)
            collect_count = 0

            def mock_collect(*args, **kwargs):
                nonlocal collect_count
                collect_count += 1
                return [mock_deal]

            with patch.object(live_client, "_collect_live_deals_uncached", side_effect=mock_collect):
                deals1 = live_client.fetch_all_weekend_deals(
                    {"NRT": dest}, slots, lambda d1, d2: (False, "", 1.0), ref_date=today
                )
                self.assertEqual(len(deals1), 1)
                self.assertEqual(collect_count, 0)  # Hit disk cache!

            # 2. Trigger on-demand force refresh callback
            handler = TravelInteractiveHandler(bot_token="dummy_token", engine=engine)
            handler._async_execute_and_update = MagicMock()
            try:
                handler.handle_callback_query(
                    cq_id="cq_ref_real",
                    chat_id="chat_adm",
                    message_id=55,
                    callback_data="trv:r:ALL",
                    from_user={"id": 111},
                )
                # Fresh disk lookup must miss after refresh ...
                self.assertIsNone(live_client._load_disk_cache(disk_key, max_age_sec=CACHE_TTL_SECONDS))
                # ... while the emergency backup survives for upstream-outage STALE fallback.
                backup = live_client._load_disk_cache(
                    disk_key, max_age_sec=STALE_FALLBACK_SECONDS, allow_backup=True
                )
                self.assertIsNotNone(backup)
                self.assertEqual(backup[0].price_krw, 300000)

                # 3. Next fetch must actually trigger uncached live scraper!
                with patch.object(live_client, "_collect_live_deals_uncached", side_effect=mock_collect):
                    deals2 = live_client.fetch_all_weekend_deals(
                        {"NRT": dest}, slots, lambda d1, d2: (False, "", 1.0), ref_date=today
                    )
                    self.assertEqual(len(deals2), 1)
                    self.assertEqual(collect_count, 1)  # Scraper executed!
            finally:
                handler.shutdown()


    @patch("travel_brief.interactive.TelegramChannel")
    def test_dispatch_travel_bot_message_update_sends_welcome_card(self, mock_tg_cls):
        from market_brief.multi_bot_manager import DynamicBotManager, SubscriptionEngine, MultiBotInteractionRouter
        from market_brief.storage.db import MarketBriefDatabase
        
        mock_tg = MagicMock()
        mock_tg_cls.return_value = mock_tg
        
        import tempfile
        tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
        tmp.close()
        db = MarketBriefDatabase(db_path=tmp.name)
        engine = SubscriptionEngine(db)
        router = MultiBotInteractionRouter(engine)
        manager = DynamicBotManager(engine, router)
        
        item = {
            "update_id": 999,
            "message": {
                "message_id": 1,
                "chat": {"id": 12345},
                "text": "/start",
                "from": {"id": 12345, "first_name": "여행자"}
            }
        }
        
        handled, reason = manager._dispatch_travel_bot_update("fake_travel_token", item)
        self.assertTrue(handled)
        self.assertEqual(reason, "travel_message_handled")
        mock_tg.send_message.assert_called_once()
        call_args = mock_tg.send_message.call_args[1]
        self.assertEqual(call_args["chat_id"], "12345")
        self.assertIn("트래블 패스파인더", call_args["text"])
        self.assertIn("inline_keyboard", call_args["reply_markup"])


def _mk_deal(code: str, dep: str, ret: str, price: int, source: str = "LIVE_PG", name: str = ""):
    from travel_brief.models import FlightDeal

    return FlightDeal(
        destination_code=code,
        destination_name=name or code,
        origin_code="ICN",
        departure_date=dep,
        return_date=ret,
        departure_day_of_week="목",
        return_day_of_week="일",
        nights=3,
        airline="네이버 직항 최저가",
        price_krw=price,
        baseline_krw=price + 50000,
        is_peak_holiday=False,
        holiday_name="",
        data_source=source,
    )


class TestTravelCacheAndSequenceGuards(unittest.TestCase):
    """Regression tests for GPT audit items: city cache isolation, superset slicing, refresh epoch,
    per-message sequence guard, loading-card ordering, SSE re-rank, and on-demand formatter modes."""

    def setUp(self):
        import tempfile
        import travel_brief.naver_live as nl

        self.nl = nl
        with nl._CACHE_LOCK:
            nl._MEM_CACHE.clear()
            nl._IN_FLIGHT.clear()
            nl._FAIL_CACHE.clear()
            nl._STALE_BACKUP.clear()
        self._tmp = tempfile.TemporaryDirectory()
        self.cache_path = Path(self._tmp.name) / "live_cache.json"
        self.client = nl.NaverFlightLiveClient(cache_path=self.cache_path)
        self.engine = FlightEngine()
        self.holiday_fn = lambda d1, d2: (False, "", 1.0)
        from datetime import date, timedelta

        self.ref = date(2026, 11, 20)
        self.slots = [(self.ref, self.ref + timedelta(days=3), "목", "일", 3)]

    def tearDown(self):
        with self.nl._CACHE_LOCK:
            self.nl._MEM_CACHE.clear()
            self.nl._IN_FLIGHT.clear()
            self.nl._FAIL_CACHE.clear()
            self.nl._STALE_BACKUP.clear()
        self._tmp.cleanup()

    def _dests(self, *codes):
        return {c: self.engine.destinations[c] for c in codes}

    def _fake_collect(self, calls):
        def _collect(destinations, slots, holiday_fn):
            calls.append(tuple(sorted(destinations)))
            return [_mk_deal(c, "2026-11-20", "2026-11-23", 300000 + i * 1000) for i, c in enumerate(sorted(destinations))]
        return _collect

    def test_fast_mode_skips_heavy_sse_batch(self):
        from datetime import date, timedelta

        dests = dict(self.engine.destinations)
        start = date(2026, 11, 20)
        slots = [
            (start + timedelta(days=7 * w), start + timedelta(days=7 * w + 3), "금", "월", 3)
            for w in range(24)  # ~6 months of weekends -> full mode would queue dozens of SSE targets
        ]

        def fake_pg(code, slots_arg):
            base = 200000 + sorted(dests).index(code) * 10000
            return {
                (dep.isoformat(), ret.isoformat()): (base + i * 1000, base + i * 1000 + 20000)
                for i, (dep, ret, _, _, _) in enumerate(slots_arg)
            }

        sse_calls: list = []

        def fake_sse(code, dep_i, ret_i, cookie_header=""):
            sse_calls.append((code, dep_i))
            return None  # SSE miss keeps LIVE_PG; we only count targets here

        def run(fast: bool):
            sse_calls.clear()
            with patch.object(self.client, "fetch_city_batch_pg", side_effect=fake_pg),                  patch.object(self.client, "_fetch_session_cookie_header", return_value=""),                  patch.object(self.client, "fetch_live_sse_slot", side_effect=fake_sse):
                t0 = time.monotonic()
                deals = self.client.fetch_all_weekend_deals(
                    dests, slots, self.holiday_fn, ref_date=start, months_ahead=6, fast_mode=fast
                )
                return deals, list(sse_calls), time.monotonic() - t0

        fast_deals, fast_sse, fast_elapsed = run(True)
        self.assertEqual(len(fast_deals), len(dests) * len(slots))  # full PG matrix still returned
        self.assertLessEqual(len(fast_sse), 6)
        self.assertEqual(len({c for c, _ in fast_sse}), len(fast_sse))  # at most 1 SSE per destination
        self.assertTrue(all(dep == start.isoformat() for _, dep in fast_sse))  # cheapest slot per city
        self.assertLess(fast_elapsed, 2.0)

        # Fast entries live in their own namespace: a full (06:30 batch) request must re-collect
        full_deals, full_sse, _ = run(False)
        self.assertGreater(len(full_sse), len(fast_sse))
        self.assertEqual(len(full_deals), len(fast_deals))

        # ...while a later fast request reuses the fresh full-batch entry without any network I/O
        with patch.object(self.client, "_collect_live_deals_uncached", side_effect=AssertionError("must hit cache")):
            reused = self.client.fetch_all_weekend_deals(
                dests, slots, self.holiday_fn, ref_date=start, months_ahead=6, fast_mode=True
            )
        self.assertEqual(len(reused), len(full_deals))

    def test_on_demand_all_refresh_uses_fast_live_and_verifies_hero(self):
        engine = MagicMock()
        engine.destinations = self.engine.destinations
        engine.live_naver = True
        engine.search_deals = MagicMock(return_value=[_mk_deal("NRT", "2026-12-03", "2026-12-06", 250000, name="도쿄")])
        engine.ensure_deals_sse_verified = MagicMock(side_effect=lambda deals: list(deals))
        handler = TravelInteractiveHandler(bot_token="test", engine=engine)
        handler._finish_with_card = MagicMock()
        try:
            with patch("travel_bot_runner.format_telegram_briefing", return_value="ok"):
                handler._async_execute_and_update("c1", 1, "ALL", "u1", request_seq=1)
            engine.search_deals.assert_called_once_with(dest_code=None, months_ahead=6, fast_live=True)
            engine.ensure_deals_sse_verified.assert_called()  # ALL hero is SSE-verified too
            handler._finish_with_card.assert_called_once()
        finally:
            handler.shutdown()

    def test_city_cache_isolation_nrt_then_kix_fuk(self):
        calls: list = []
        with patch.object(self.client, "_collect_live_deals_uncached", side_effect=self._fake_collect(calls)):
            nrt = self.client.fetch_all_weekend_deals(self._dests("NRT"), self.slots, self.holiday_fn, ref_date=self.ref)
            kix = self.client.fetch_all_weekend_deals(self._dests("KIX"), self.slots, self.holiday_fn, ref_date=self.ref)
            fuk = self.client.fetch_all_weekend_deals(self._dests("FUK"), self.slots, self.holiday_fn, ref_date=self.ref)
        self.assertEqual({d.destination_code for d in nrt}, {"NRT"})
        self.assertEqual({d.destination_code for d in kix}, {"KIX"})
        self.assertEqual({d.destination_code for d in fuk}, {"FUK"})
        self.assertEqual(calls, [("NRT",), ("KIX",), ("FUK",)])

    def test_superset_cache_slicing_memory_and_disk(self):
        calls: list = []
        with patch.object(self.client, "_collect_live_deals_uncached", side_effect=self._fake_collect(calls)):
            full = self.client.fetch_all_weekend_deals(
                self._dests("NRT", "KIX", "FUK"), self.slots, self.holiday_fn, ref_date=self.ref
            )
            self.assertEqual(len(full), 3)
            kix = self.client.fetch_all_weekend_deals(self._dests("KIX"), self.slots, self.holiday_fn, ref_date=self.ref)
            self.assertEqual([d.destination_code for d in kix], ["KIX"])
            self.assertEqual(len(calls), 1)  # served from memory superset, no network

            # Disk superset (e.g. separate cron process warmed the file): clear memory only
            with self.nl._CACHE_LOCK:
                self.nl._MEM_CACHE.clear()
            fuk = self.client.fetch_all_weekend_deals(self._dests("FUK"), self.slots, self.holiday_fn, ref_date=self.ref)
            self.assertEqual([d.destination_code for d in fuk], ["FUK"])
            self.assertEqual(len(calls), 1)

            # A destination absent from the superset data must NOT be sliced to an empty list
            tpe = self.client.fetch_all_weekend_deals(self._dests("TPE"), self.slots, self.holiday_fn, ref_date=self.ref)
            self.assertEqual([d.destination_code for d in tpe], ["TPE"])
            self.assertEqual(calls[-1], ("TPE",))

    def test_sse_upgrade_syncs_all_cache_keys(self):
        calls: list = []
        dests = self._dests("NRT", "KIX")
        with patch.object(self.client, "_collect_live_deals_uncached", side_effect=self._fake_collect(calls)):
            self.client.fetch_all_weekend_deals(dests, self.slots, self.holiday_fn, ref_date=self.ref)
            self.client.fetch_all_weekend_deals(self._dests("NRT", "KIX", "FUK"), self.slots, self.holiday_fn, ref_date=self.ref)
        sse = {
            "cheapest": {
                "airline": "제주항공", "dep_time": "08:00", "ret_dep_time": "20:00",
                "ret_arr_time": "22:10", "next_day_arrival": False,
                "min_card": 287000, "min_general": 290000,
            },
            "popular": None,
        }
        target = _mk_deal("NRT", "2026-11-20", "2026-11-23", 300000)
        with patch.object(self.client, "fetch_live_sse_slot", return_value=sse), \
             patch.object(self.client, "_fetch_session_cookie_header", return_value=""):
            out = self.client.ensure_deals_sse_verified([target], self.engine.destinations)
        self.assertEqual(out[0].data_source, "LIVE_SSE")
        with self.nl._CACHE_LOCK:
            for key in [("2026-11-20", 6, ("KIX", "NRT")), ("2026-11-20", 6, ("FUK", "KIX", "NRT"))]:
                nrt = [d for d in self.nl._MEM_CACHE[key][1] if d.destination_code == "NRT"][0]
                self.assertEqual(nrt.data_source, "LIVE_SSE", key)
        for disk_key in ["2026-11-20:6:KIX,NRT", "2026-11-20:6:FUK,KIX,NRT"]:
            disk = self.client._load_disk_cache(disk_key, max_age_sec=self.nl.CACHE_TTL_SECONDS)
            nrt = [d for d in disk if d.destination_code == "NRT"][0]
            self.assertEqual(nrt.price_krw, 287000, disk_key)

    def test_refresh_during_inflight_does_not_reuse_or_cache_old_result(self):
        import threading

        dests = self._dests("NRT")
        gate = threading.Event()
        started = threading.Event()
        calls: list = []

        def slow_old(destinations, slots, holiday_fn):
            calls.append("old")
            started.set()
            gate.wait(5)
            return [_mk_deal("NRT", "2026-11-20", "2026-11-23", 111111)]

        def fresh_new(destinations, slots, holiday_fn):
            calls.append("new")
            return [_mk_deal("NRT", "2026-11-20", "2026-11-23", 222222)]

        result_old: list = []
        with patch.object(self.client, "_collect_live_deals_uncached", side_effect=slow_old):
            t = threading.Thread(
                target=lambda: result_old.extend(
                    self.client.fetch_all_weekend_deals(dests, self.slots, self.holiday_fn, ref_date=self.ref)
                )
            )
            t.start()
            self.assertTrue(started.wait(5))
            self.client.invalidate_cache()
            with patch.object(self.client, "_collect_live_deals_uncached", side_effect=fresh_new):
                new = self.client.fetch_all_weekend_deals(dests, self.slots, self.holiday_fn, ref_date=self.ref)
            gate.set()
            t.join(5)

        self.assertEqual(new[0].price_krw, 222222)  # did not join the pre-refresh flight
        self.assertEqual(result_old[0].price_krw, 111111)
        self.assertEqual(calls, ["old", "new"])
        # Old leader finished after invalidate -> must not overwrite the fresh post-refresh cache
        cached = self.client.fetch_all_weekend_deals(dests, self.slots, self.holiday_fn, ref_date=self.ref)
        self.assertEqual(cached[0].price_krw, 222222)

    def _make_handler(self):
        handler = TravelInteractiveHandler(bot_token="dummy_token", engine=FlightEngine(live_naver=False))
        handler.tg = MagicMock()
        self.edits: list = []
        self.loading_edits: list = []
        handler._safe_edit_message = lambda chat_id, message_id, text, reply_markup=None: self.edits.append(text)
        handler._edit_in_place_only = lambda chat_id, message_id, text, reply_markup=None: self.loading_edits.append(text)
        return handler

    def test_sequence_guard_latest_click_wins(self):
        handler = self._make_handler()
        try:
            seq_tokyo = handler._next_request_seq("c1", 7)
            seq_osaka = handler._next_request_seq("c1", 7)
            # Osaka (newer) finishes first, Tokyo (older, slower) finishes later
            handler._async_execute_and_update("c1", 7, "KIX", "u2", request_seq=seq_osaka)
            handler._async_execute_and_update("c1", 7, "NRT", "u1", request_seq=seq_tokyo)
            self.assertEqual(len(self.edits), 1)
            self.assertIn("오사카", self.edits[0])
            # Other messages are independent
            seq_other = handler._next_request_seq("c1", 8)
            handler._async_execute_and_update("c1", 8, "NRT", "u3", request_seq=seq_other)
            self.assertEqual(len(self.edits), 2)
        finally:
            handler.shutdown()

    def test_loading_card_never_overwrites_final_result(self):
        handler = self._make_handler()
        try:
            seq = handler._next_request_seq("c1", 9)
            handler._async_execute_and_update("c1", 9, "FUK", "u1", request_seq=seq)
            # Delayed loading edit of the same request arrives after the final result
            issued = handler._edit_if_latest("c1", 9, seq, "loading", None, is_loading=True)
            self.assertFalse(issued)
            self.assertEqual(self.loading_edits, [])
            self.assertEqual(len(self.edits), 1)
        finally:
            handler.shutdown()

    def test_callback_shows_loading_card_then_result_and_s_increments_seq(self):
        handler = self._make_handler()
        try:
            handler.handle_callback_query("cq1", "c9", 11, "trv:q:FUK", {"id": 901})
            handler.tg.answer_callback.assert_called_once()
            handler.worker_pool.shutdown(wait=True)
            handler.ui_pool.shutdown(wait=True)
            self.assertEqual(len(self.edits), 1)
            self.assertIn("후쿠오카", self.edits[0])
            self.assertLessEqual(len(self.loading_edits), 1)
            if self.loading_edits:
                self.assertIn("실시간 특가 검색 중", self.loading_edits[0])
            self.assertEqual(handler._msg_seq[("c9", 11)], 1)
            # Blocked click (cooldown / in-progress) must not bump the sequence
            handler.handle_callback_query("cq2", "c9", 11, "trv:q:NRT", {"id": 901})
            self.assertEqual(handler._msg_seq[("c9", 11)], 1)
        finally:
            handler.shutdown()

    def test_empty_results_and_exceptions_end_in_error_card(self):
        handler = self._make_handler()
        try:
            handler.engine.search_deals = MagicMock(return_value=[])
            seq = handler._next_request_seq("c1", 21)
            handler._async_execute_and_update("c1", 21, "DAD", "u1", request_seq=seq)
            self.assertIn("특가 조회 실패", self.edits[-1])
            self.assertNotIn("u1", handler.cooldown._in_progress_users)

            handler.engine.search_deals = MagicMock(side_effect=RuntimeError("boom <x>"))
            seq = handler._next_request_seq("c1", 21)
            handler._async_execute_and_update("c1", 21, "DAD", "u1", request_seq=seq)
            self.assertIn("일시적인 오류", self.edits[-1])
            self.assertNotIn("<x>", self.edits[-1])
        finally:
            handler.shutdown()

    def test_hero_sse_rerank_promotes_and_verifies_next_deal(self):
        from dataclasses import replace

        handler = self._make_handler()
        try:
            a = _mk_deal("NRT", "2026-12-03", "2026-12-06", 250000, name="도쿄")
            b = _mk_deal("NRT", "2026-12-10", "2026-12-13", 260000, name="도쿄")
            calls: list = []

            def fake_verify(deals):
                d = deals[0]
                calls.append(d.departure_date)
                surged = 420000 if d.departure_date == "2026-12-03" else 262000
                return [replace(d, data_source="LIVE_SSE", price_krw=surged, price_min_krw=surged, airline="제주항공")]

            handler.engine.live_naver = True
            handler.engine.ensure_deals_sse_verified = fake_verify
            from travel_brief.budget_calculator import BudgetCalculator

            recs = BudgetCalculator.rank_recommendations(handler.engine.destinations, [a, b], top_n=3)
            self.assertEqual(recs[0].best_deal.departure_date, "2026-12-03")
            out = handler._verify_hero_with_rerank([a, b], recs)
            self.assertEqual(calls, ["2026-12-03", "2026-12-10"])
            self.assertEqual(out[0].best_deal.departure_date, "2026-12-10")
            self.assertEqual(out[0].best_deal.data_source, "LIVE_SSE")
            self.assertIn("20261210", out[0].naver_flight_url)

            # SSE timeout/empty -> stays LIVE_PG and formatter discloses price-graph source
            calls.clear()
            handler.engine.ensure_deals_sse_verified = lambda deals: list(deals)
            out2 = handler._verify_hero_with_rerank([a, b], recs)
            self.assertEqual(out2[0].best_deal.data_source, "LIVE_PG")
            from travel_bot_runner import format_telegram_briefing

            msg = format_telegram_briefing(out2, engine=handler.engine, target_city="NRT")
            self.assertIn("가격그래프", msg)
            self.assertNotIn("네이버 실시간 관측", msg)
        finally:
            handler.shutdown()

    def test_formatter_modes_skip_heavy_paths(self):
        from datetime import date
        from travel_bot_runner import format_telegram_briefing
        from travel_brief.budget_calculator import BudgetCalculator

        engine = FlightEngine(live_naver=False)
        deals = engine.search_deals(dest_code="KIX", months_ahead=6, ref_date=date(2026, 11, 20))
        recs = BudgetCalculator.rank_recommendations(engine.destinations, deals, top_n=3)
        with patch.object(engine, "get_6month_matrix", side_effect=AssertionError("matrix must be skipped")), \
             patch.object(engine, "ensure_deals_sse_verified", side_effect=AssertionError("SSE must be skipped")):
            single = format_telegram_briefing(recs, engine=engine, target_city="KIX")
            self.assertIn("오사카 주말 특가 나침반", single)
            self.assertIn("추천 최저가 일정 TOP 1", single)
            self.assertNotIn("월별 주말 최저가", single)

            engine.live_naver = True  # ALL on-demand must not run the monthly SSE loop even in live mode
            all_recs = BudgetCalculator.rank_recommendations(
                engine.destinations, FlightEngine(live_naver=False).search_deals(months_ahead=6), top_n=3
            )
            with patch.object(engine, "search_deals", wraps=engine.search_deals) as spy_search:
                all_msg = format_telegram_briefing(all_recs, engine=engine, target_city="ALL")
            # Monthly picks come from the caller-warmed cache key (ref_date=None), not get_6month_matrix
            spy_search.assert_called_once_with(months_ahead=6, fast_live=True)
            self.assertIn("주말 해외여행 6개월 특가 나침반", all_msg.split("\n")[0])  # not single-city header
        for msg in (single, all_msg):
            import re

            self.assertLessEqual(len(msg), 3500)
            self.assertLessEqual(len(re.sub(r"<[^>]+>", "", msg)), 1800)


if __name__ == "__main__":
    unittest.main()
