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
        
        try:
            handled, reason = manager._dispatch_travel_bot_update("fake_travel_token", item)
            self.assertTrue(handled)
            self.assertEqual(reason, "travel_message_handled")
            # P0: the real TelegramChannel API is send_text (send_message never existed)
            mock_tg.send_message.assert_not_called()
            mock_tg.send_text.assert_called_once()
            args, kwargs = mock_tg.send_text.call_args
            self.assertEqual(args[0], "12345")
            self.assertIn("트래블 패스파인더", args[1])
            self.assertEqual(kwargs["parse_mode"], "HTML")
            self.assertIn("inline_keyboard", kwargs["reply_markup"])
        finally:
            manager._travel_handler.shutdown()


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
        handler.fx_client = MagicMock()
        handler.fx_client.get_many.return_value = {}
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
            self.assertEqual(handler._msg_states[("c9", 11)].generation, 1)
            self.assertEqual(handler._msg_states[("c9", 11)].phase, "DONE")
            self.assertEqual(handler._msg_states[("c9", 11)].active_workers, 0)
            # Blocked click (cooldown / in-progress) must not bump the sequence
            handler.handle_callback_query("cq2", "c9", 11, "trv:q:NRT", {"id": 901})
            self.assertEqual(handler._msg_states[("c9", 11)].generation, 1)
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


# ----------------------------------------------------------------------
# v2.3 integration: CityCatalog / scope cache / FX / TG-01 / Zero-Stuck / routing
# ----------------------------------------------------------------------
class _FakeResp:
    def __init__(self, payload):
        self._b = payload if isinstance(payload, bytes) else __import__("json").dumps(payload).encode("utf-8")

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fx_opener(routes: dict, calls: list | None = None):
    """routes: {(CUR, "spot"|"chart"): payload | Exception}"""

    def opener(req, timeout=None):
        url = req.full_url
        kind = "spot" if "productDetail" in url else "chart"
        cur = url.split("FX_", 1)[1][:3]
        if calls is not None:
            calls.append((cur, kind, timeout))
        val = routes.get((cur, kind))
        if val is None:
            raise AssertionError(f"unexpected FX url {url}")
        if isinstance(val, BaseException):
            raise val
        return _FakeResp(val)

    return opener


def _spot(close, full_name, ratio="-0.49"):
    return {"result": {"closePrice": close, "fluctuationsRatio": ratio, "fullName": full_name,
                       "localTradedAt": "2026-11-11T10:00:00+09:00"}}


def _chart(*closes):
    return {"result": {"priceInfos": [{"localDate": f"2026-0{8 + i % 2}-01", "closePrice": c} for i, c in enumerate(closes)]}}


FX_ROUTES_OK = {
    ("JPY", "spot"): _spot("912.34", "일본 JPY 100"),
    ("JPY", "chart"): _chart(940.0, 930.0, 950.0),  # avg 940 -> -2.94% GREEN
    ("VND", "spot"): _spot("5.42", "베트남 VND 100"),
    ("VND", "chart"): _chart(5.40, 5.44),  # avg 5.42 -> 0% YELLOW
    ("IDR", "spot"): _spot("8.70", "인도네시아 IDR 100"),
    ("IDR", "chart"): _chart(8.50),  # +2.35% ORANGE
    ("USD", "spot"): _spot("1,339.40", None),
    ("USD", "chart"): _chart(1339.40),
}


def _make_manager():
    import tempfile
    from market_brief.multi_bot_manager import DynamicBotManager, SubscriptionEngine, MultiBotInteractionRouter
    from market_brief.storage.db import MarketBriefDatabase

    tmp = tempfile.NamedTemporaryFile(suffix=".sqlite3", delete=False)
    tmp.close()
    db = MarketBriefDatabase(db_path=tmp.name)
    sub_engine = SubscriptionEngine(db)
    return DynamicBotManager(sub_engine, MultiBotInteractionRouter(sub_engine))


class TestTravelV23Integration(unittest.TestCase):
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
        self.client = nl.NaverFlightLiveClient(cache_path=Path(self._tmp.name) / "live_cache.json")
        self.engine = FlightEngine(live_naver=False)
        self.handlers: list = []

    def tearDown(self):
        for h in self.handlers:
            h.shutdown()
        with self.nl._CACHE_LOCK:
            self.nl._MEM_CACHE.clear()
            self.nl._IN_FLIGHT.clear()
            self.nl._FAIL_CACHE.clear()
            self.nl._STALE_BACKUP.clear()
        self._tmp.cleanup()

    def _handler(self, engine=None, capture=True):
        h = TravelInteractiveHandler(bot_token="dummy_token", engine=engine or self.engine)
        h.tg = MagicMock()
        h.tg.send_text.return_value = "501"
        self.handlers.append(h)
        self.edits: list = []
        self.loading_edits: list = []
        if capture:
            h._safe_edit_message = lambda chat_id, message_id, text, reply_markup=None: self.edits.append((message_id, text))
            h._edit_in_place_only = lambda chat_id, message_id, text, reply_markup=None: self.loading_edits.append((message_id, text))
        return h

    def _catalog(self):
        from travel_brief.city_catalog import CityCatalog

        return CityCatalog(self.engine.destinations)

    # 1. NFKC / whitespace / punctuation / case normalization
    def test_01_catalog_normalization(self):
        cat = self._catalog()
        for q in ("ＣＴＳ", "cts", " 삿  포로 ", "삿​포로", "SAPPORO", "Sap-poro", "(삿포로)", "삿포로 여행"):
            self.assertEqual(cat.resolve(q).code, "CTS", q)
        for q in ("호찌민", "호치민", "Ho Chi Minh", "ho.chi.minh", "사이공"):
            self.assertEqual(cat.resolve(q).code, "SGN", q)
        self.assertEqual(cat.resolve("나뜨랑").code, "CXR")
        self.assertEqual(cat.resolve("TYO").code, "NRT")
        self.assertEqual(cat.resolve("오사카").metadata_quality, "CURATED")

    # 2. 삿포로 / 신치토세 / CTS -> CTS
    def test_02_sapporo_aliases_resolve_to_cts(self):
        cat = self._catalog()
        for q in ("삿포로", "신치토세", "CTS"):
            res = cat.resolve(q)
            self.assertEqual(res.code, "CTS")
            self.assertEqual(res.metadata_quality, "FALLBACK")
            self.assertIn("삿포로", res.display_name)
        self.assertGreaterEqual(len(cat.fallback_codes), 25)
        for code in ("OKA", "NGO", "MYJ", "CEB", "KLO", "HAN", "SGN", "BKI", "PQC", "CNX", "HNL", "SPN",
                     "HKG", "MFM", "KHH", "TAO", "PVG", "PEK", "CDG", "LHR", "FCO", "SYD"):
            self.assertTrue(cat.is_known_code(code), code)

    # 3. Unregistered 3-letter codes are rejected
    def test_03_unknown_iata_rejected(self):
        cat = self._catalog()
        for q in ("XYZ", "abc", "QQQ", "LAX"):
            self.assertIsNone(cat.resolve(q), q)
        h = self._handler()
        self.assertIsNone(h.normalize_filter("XYZ"))
        h.worker_pool.submit = MagicMock()
        h.handle_callback_query("cq", "c1", 3, "trv:q:XYZ", {"id": 7})
        h.worker_pool.submit.assert_not_called()
        self.assertIn("지원하지 않는", h.tg.answer_callback.call_args[1]["text"])
        self.assertEqual(h.build_inline_keyboard("XYZ")["inline_keyboard"][0][0]["callback_data"], "trv:r:ALL")

    # 4. Custom city never leaks into ALL results / engine / cache keys
    def test_04_custom_city_isolated_from_all(self):
        from datetime import date

        cat = self._catalog()
        custom = cat.custom_destinations_for("CTS")
        offline_cts = self.engine.search_deals(dest_code="CTS", custom_destinations=custom, ref_date=date(2026, 11, 20))
        self.assertTrue(offline_cts)
        self.assertEqual({d.destination_code for d in offline_cts}, {"CTS"})
        self.assertNotIn("CTS", self.engine.destinations)
        self.assertEqual(self.engine.search_deals(dest_code="CTS", ref_date=date(2026, 11, 20)), [])
        offline_all = self.engine.search_deals(custom_destinations=custom, ref_date=date(2026, 11, 20))
        self.assertNotIn("CTS", {d.destination_code for d in offline_all})

        live = FlightEngine(live_naver=True)
        live._live_client = self.client
        calls: list = []

        def fake_collect(destinations, slots, holiday_fn, fast_mode=False):
            calls.append(tuple(sorted(destinations)))
            return [_mk_deal(c, "2026-11-26", "2026-11-29", 300000) for c in sorted(destinations)]

        with patch.object(self.client, "_collect_live_deals_uncached", side_effect=fake_collect):
            cts = live.search_deals(dest_code="CTS", custom_destinations=custom, ref_date=date(2026, 11, 20), fast_live=True)
            all_deals = live.search_deals(ref_date=date(2026, 11, 20), fast_live=True)
        self.assertEqual({d.destination_code for d in cts}, {"CTS"})
        self.assertNotIn("CTS", {d.destination_code for d in all_deals})
        self.assertEqual(calls[0], ("CTS",))
        self.assertNotIn("CTS", calls[1])
        with self.nl._CACHE_LOCK:
            for key in self.nl._MEM_CACHE:
                self.assertFalse("CTS" in key[2] and len(key[2]) > 1, key)
        self.assertNotIn("CTS", live.destinations)

    # 5. trv:r:CTS cooldown + scope-only cache invalidation
    def test_05_refresh_scope_cooldown_and_invalidation(self):
        engine = FlightEngine(live_naver=False)
        engine._live_client = self.client
        curated = tuple(sorted(engine.destinations))
        all_key = ("2026-11-20", 6, curated)
        cts_key = ("2026-11-20#fast", 6, ("CTS",))
        all_deals = [_mk_deal(c, "2026-11-26", "2026-11-29", 300000) for c in curated]
        cts_deals = [_mk_deal("CTS", "2026-11-26", "2026-11-29", 410000)]
        with self.nl._CACHE_LOCK:
            self.nl._MEM_CACHE[all_key] = (time.time(), all_deals)
            self.nl._MEM_CACHE[cts_key] = (time.time(), cts_deals)
        self.client._save_disk_cache(self.nl._disk_key_for(all_key), all_deals)
        self.client._save_disk_cache(self.nl._disk_key_for(cts_key), cts_deals)

        h = self._handler(engine=engine)
        h._async_execute_and_update = MagicMock()
        h.handle_callback_query("cq1", "c1", 5, "trv:r:CTS", {"id": 1001})
        with self.nl._CACHE_LOCK:
            self.assertIn(all_key, self.nl._MEM_CACHE)  # ALL untouched
            self.assertNotIn(cts_key, self.nl._MEM_CACHE)
            self.assertIn(cts_key, self.nl._STALE_BACKUP)
        ttl = self.nl.CACHE_TTL_SECONDS
        self.assertIsNotNone(self.client._load_disk_cache(self.nl._disk_key_for(all_key), max_age_sec=ttl))
        self.assertIsNone(self.client._load_disk_cache(self.nl._disk_key_for(cts_key), max_age_sec=ttl))
        self.assertIsNotNone(self.client._load_disk_cache(
            self.nl._disk_key_for(cts_key), max_age_sec=self.nl.STALE_FALLBACK_SECONDS, allow_backup=True))
        # Refresh button keeps its scope
        self.assertEqual(h.build_inline_keyboard("CTS")["inline_keyboard"][0][0]["callback_data"], "trv:r:CTS")

        # Per-scope cooldown: another user's CTS refresh is throttled, ALL refresh is not
        h.handle_callback_query("cq2", "c2", 6, "trv:r:CTS", {"id": 1002})
        self.assertIn("CTS 새로고침은", h.tg.answer_callback.call_args[1]["text"])
        with self.nl._CACHE_LOCK:
            self.nl._MEM_CACHE[cts_key] = (time.time(), cts_deals)
        h.handle_callback_query("cq3", "c3", 7, "trv:r:ALL", {"id": 1003})
        self.assertIn("조회 중", h.tg.answer_callback.call_args[1]["text"])
        with self.nl._CACHE_LOCK:
            self.assertNotIn(all_key, self.nl._MEM_CACHE)
            self.assertIn(cts_key, self.nl._MEM_CACHE)  # ALL refresh never touches custom scope

    # 6. Every callback_data <= 64 UTF-8 bytes
    def test_06_all_callbacks_within_64_bytes(self):
        h = self._handler()
        cat = h.catalog
        boards = [h.build_inline_keyboard("ALL"), h.build_onboarding_keyboard()]
        for code in sorted(cat.curated_codes | cat.fallback_codes):
            boards.append(h.build_inline_keyboard(code))
            boards.append(h.build_selected_target_keyboard(code))
        seen_guide = False
        for kb in boards:
            for row in kb["inline_keyboard"]:
                for btn in row:
                    cb = btn.get("callback_data")
                    if cb:
                        self.assertLessEqual(len(cb.encode("utf-8")), 64, cb)
                        self.assertTrue(cb.startswith("trv:"))
                        seen_guide = seen_guide or cb == "trv:c:guide"
        self.assertTrue(seen_guide)
        self.assertEqual(h.build_inline_keyboard("ALL")["inline_keyboard"][-2][0]["callback_data"], "trv:c:guide")

    # 7. JPY/VND/IDR are 100-unit quotes, others 1-unit
    def test_07_fx_hundred_unit_parsing(self):
        from travel_brief.fx_client import FXClient, parse_spot_payload, quote_unit_for

        self.assertEqual(parse_spot_payload("USD", _spot("1,339.40", None))["spot"], 1339.40)
        client = FXClient(opener=_fx_opener(FX_ROUTES_OK))
        out = client.get_many(["JPY", "VND", "IDR"])
        self.assertEqual({c: q.quote_unit for c, q in out.items()}, {"JPY": 100, "VND": 100, "IDR": 100})
        self.assertAlmostEqual(out["JPY"].spot, 912.34)
        self.assertAlmostEqual(out["VND"].per_10k_krw, 542.0)
        self.assertEqual(out["JPY"].full_name, "일본 JPY 100")
        usd = FXClient(opener=_fx_opener(FX_ROUTES_OK)).get("USD")
        self.assertEqual(usd.quote_unit, 1)
        self.assertEqual(quote_unit_for("THB"), 1)
        self.assertIsNone(usd.per_10k_krw)

    # 8. 3M average + signal boundaries (same quote unit)
    def test_08_fx_average_and_signal_boundaries(self):
        from travel_brief.fx_client import FXClient, build_quote, classify_signal, parse_chart_average

        self.assertEqual(classify_signal(-1.5), "GREEN")
        self.assertEqual(classify_signal(-1.4999), "GREEN")  # rounds to -1.50
        self.assertEqual(classify_signal(-1.49), "YELLOW")
        self.assertEqual(classify_signal(1.49), "YELLOW")
        self.assertEqual(classify_signal(1.5), "ORANGE")
        self.assertEqual(build_quote("JPY", {"spot": 98.5}, 100.0).signal, "GREEN")
        self.assertEqual(build_quote("JPY", {"spot": 101.5}, 100.0).signal, "ORANGE")
        self.assertEqual(build_quote("JPY", {"spot": 100.0}, 100.0).signal, "YELLOW")
        self.assertAlmostEqual(parse_chart_average(_chart(940.0, 930.0, 950.0)), 940.0)
        out = FXClient(opener=_fx_opener(FX_ROUTES_OK)).get_many(["JPY", "VND", "IDR"])
        self.assertAlmostEqual(out["JPY"].avg_3m, 940.0)
        self.assertEqual(out["JPY"].diff_vs_3m_pct, round((912.34 / 940.0 - 1) * 100, 2))
        self.assertEqual([out[c].signal for c in ("JPY", "VND", "IDR")], ["GREEN", "YELLOW", "ORANGE"])

    # 9. Timeout / malformed / partial failure + TTL cache + 3-currency parallel cap
    def test_09_fx_failures_and_cache(self):
        from travel_brief.fx_client import FXClient

        routes = dict(FX_ROUTES_OK)
        routes[("VND", "spot")] = TimeoutError("timed out")
        routes[("THB", "spot")] = b"<html>not json"
        routes[("IDR", "chart")] = {"result": {"priceInfos": "broken"}}
        calls: list = []
        client = FXClient(opener=_fx_opener(routes, calls))
        out = client.get_many(["JPY", "VND", "THB"])
        self.assertEqual(set(out), {"JPY"})
        self.assertTrue(all(t == 2.0 for _, _, t in calls))
        partial = client.get_many(["IDR"])["IDR"]  # chart malformed -> spot only, no signal
        self.assertIsNone(partial.signal)
        self.assertAlmostEqual(partial.spot, 8.70)
        n = len(calls)
        client.get_many(["JPY", "IDR"])
        self.assertEqual(len(calls), n)  # TTL hit
        self.assertEqual(len(client.get_many(["JPY", "KRW", "IDR", "USD", "VND"])), 3)  # <= 3 currencies
        self.assertEqual(FXClient(opener=_fx_opener({})).get_many(["JPY"]), {})  # total failure -> empty

    # 10. FX unavailable -> briefing renders normally
    def test_10_briefing_without_fx(self):
        from travel_bot_runner import fits_tg01, format_telegram_briefing
        from travel_brief.fx_client import FXClient

        h = self._handler()
        h.fx_enabled = True
        h.fx_client = FXClient(opener=_fx_opener({}))
        seq = h._next_request_seq("c1", 30)
        h._async_execute_and_update("c1", 30, "FUK", "u1", request_seq=seq)
        self.assertEqual(len(self.edits), 1)
        self.assertIn("후쿠오카 주말 특가 나침반", self.edits[0][1])
        self.assertNotIn("환율", self.edits[0][1])

        h.fx_client = MagicMock()
        h.fx_client.get_many.side_effect = RuntimeError("fx down")
        seq = h._next_request_seq("c1", 30)
        h._async_execute_and_update("c1", 30, "FUK", "u1", request_seq=seq)
        self.assertIn("후쿠오카 주말 특가 나침반", self.edits[-1][1])

        recs = self._recs("FUK")
        for fx in (None, {}):
            msg = format_telegram_briefing(recs, engine=self.engine, target_city="FUK", fx_map=fx)
            self.assertTrue(fits_tg01(msg))
            self.assertNotIn("💱", msg)

        h.fx_client = FXClient(opener=_fx_opener(FX_ROUTES_OK))
        seq = h._next_request_seq("c1", 30)
        h._async_execute_and_update("c1", 30, "FUK", "u1", request_seq=seq)
        self.assertIn("100엔 = 912.3원", self.edits[-1][1])
        self.assertIn("🟢 환율 꿀구간", self.edits[-1][1])

    def _recs(self, code, **deal_kwargs):
        from datetime import date
        from travel_brief.budget_calculator import BudgetCalculator

        deals = self.engine.search_deals(dest_code=code, months_ahead=6, ref_date=date(2026, 11, 20))
        return BudgetCalculator.rank_recommendations(self.engine.destinations, deals, top_n=3)

    def _live_rec(self, code, price, baseline):
        from travel_brief.budget_calculator import BudgetCalculator

        deal = _mk_deal(code, "2026-12-03", "2026-12-06", price, source="LIVE_SSE")
        deal.baseline_krw = baseline
        meta = self.engine.destinations.get(code) or self._catalog().get(code).meta
        return BudgetCalculator.evaluate_deal(meta, deal)

    # 11. Savings < 30,000원 are not displayed
    def test_11_savings_threshold(self):
        from travel_bot_runner import build_motivation_parts, format_telegram_briefing

        below = self._live_rec("NRT", 351000, 380000)  # 29,000
        exact = self._live_rec("NRT", 350000, 380000)  # 30,000
        self.assertFalse(any("💸" in p for p in build_motivation_parts(below.destination, below.best_deal)))
        self.assertTrue(any("💸" in p for p in build_motivation_parts(exact.destination, exact.best_deal)))
        self.assertNotIn("💸", format_telegram_briefing([below], engine=self.engine, target_city="NRT"))
        self.assertIn("💸 평소 시세보다 약 3만 원 저렴", format_telegram_briefing([exact], engine=self.engine, target_city="NRT"))
        # Offline estimates never claim savings (DATA-01)
        off = self._recs("NRT")[0]
        self.assertFalse(any("💸" in p for p in build_motivation_parts(off.destination, off.best_deal)))

    # 12. Short-haul (<= 2.5h) motivation
    def test_12_short_haul_motivation(self):
        from travel_bot_runner import build_motivation_parts

        fuk = self._live_rec("FUK", 250000, 260000)
        dad = self._live_rec("DAD", 380000, 400000)
        tpe = self._live_rec("TPE", 330000, 340000)  # exactly 2.5h
        self.assertTrue(any("단거리" in p for p in build_motivation_parts(fuk.destination, fuk.best_deal)))
        self.assertTrue(any("단거리" in p for p in build_motivation_parts(tpe.destination, tpe.best_deal)))
        self.assertFalse(any("단거리" in p for p in build_motivation_parts(dad.destination, dad.best_deal)))

    # 13. CURATED vs FALLBACK hook / honesty
    def test_13_curated_vs_fallback_hooks(self):
        from travel_bot_runner import build_motivation_parts, format_telegram_briefing

        fuk = self._live_rec("FUK", 200000, 260000)
        self.assertTrue(fuk.destination.experience_hook)
        self.assertTrue(any("🎯" in p for p in build_motivation_parts(fuk.destination, fuk.best_deal)))
        cts = self._live_rec("CTS", 200000, 380000)
        self.assertEqual(cts.destination.metadata_quality, "FALLBACK")
        parts = build_motivation_parts(cts.destination, cts.best_deal)
        self.assertFalse(any("🎯" in p or "💸" in p for p in parts))
        msg = format_telegram_briefing([cts], engine=self.engine, target_city="CTS")
        for needle in ("삿포로 주말 특가 나침반", "입국 조건 확인 필요", "실시간 요금·재고 확인 필요", "시내 3성급 호텔 기준", "기본 메타 기준"):
            self.assertIn(needle, msg)
        meta = cts.destination
        self.assertEqual((meta.best_months, meta.caution_months, meta.caution_note), ([], [], "계절 정보 확인 필요"))
        self.assertTrue(meta.flight_baseline_krw > 0 and meta.hotel_night_avg_krw > 0 and meta.daily_meal_transit_krw > 0)

    def _worst_case_recs(self, url_len=600):
        from dataclasses import replace
        from travel_brief.budget_calculator import BudgetCalculator

        recs = []
        for i, code in enumerate(("FUK", "NRT", "KIX")):
            meta = replace(
                self.engine.destinations[code],
                name=f"<b>도시&'\"{i}" * 30,
                recommended_hotel_name="그랜드<호텔>&리조트" * 40,
                recommended_hotel_grade="5성<급>&" * 20,
                tips="팁<>&설명" * 60,
                visa_info="비자<>&규정" * 40,
                experience_hook="체험<>&" * 50,
                hotel_budget_segment="세그먼트" * 10,
            )
            deal = _mk_deal(code, "2026-12-03", "2026-12-06", 250000 + i, source="LIVE_SSE")
            deal.airline = "항공<사>&" * 20
            deal.popular_fare_krw = 400000
            deal.popular_airline = "인기<항공>" * 10
            deal.is_peak_holiday = True
            deal.holiday_name = "연휴<>&" * 30
            rec = BudgetCalculator.evaluate_deal(meta, deal)
            rec.naver_flight_url = "https://m-flight.naver.com/x?" + "a=1&" * (url_len // 4)
            rec.hotel_search_url = "https://m-hotel.naver.com/x?" + "b=2&" * (url_len // 4)
            recs.append(rec)
        return recs

    # 14. Worst-case HTML <= 3500 & visible <= 1800 with escaping
    def test_14_tg01_worst_case_limits(self):
        import re
        from travel_bot_runner import fits_tg01, format_telegram_briefing, visible_text_len
        from travel_brief.fx_client import FXClient

        fx = FXClient(opener=_fx_opener(FX_ROUTES_OK)).get_many(["JPY", "VND", "IDR"])
        for url_len in (100, 600, 6000):
            recs = self._worst_case_recs(url_len)
            for target in (None, "ALL", "FUK"):
                msg = format_telegram_briefing(recs, engine=self.engine, target_city=target, fx_map=fx)
                self.assertLessEqual(len(msg), 3500, (url_len, target))
                self.assertLessEqual(visible_text_len(msg), 1800, (url_len, target))
                self.assertTrue(fits_tg01(msg))
                tags = set(re.findall(r"</?([a-zA-Z]+)", msg))
                self.assertTrue(tags <= {"b", "i", "a", "code"}, tags)
                self.assertNotIn("<호텔>", msg)
                self.assertNotIn("<b>도시", msg)

    # 15. Compact formatter preserves TOP 3 (city · price · schedule · flight link)
    def test_15_compact_preserves_top3(self):
        import re
        from travel_bot_runner import format_telegram_briefing

        # Realistic Naver deep-link length (~200 chars); absurd URLs are covered by test_14's hard cap.
        msg = format_telegram_briefing(self._worst_case_recs(200), engine=self.engine, target_city="ALL")
        visible = re.sub(r"<[^>]+>", "", msg)
        for i in (1, 2, 3):
            self.assertIn(f"\n{i}. ", visible)
        for code in ("FUK", "NRT", "KIX"):
            self.assertIn(f"({code})", visible)
        self.assertEqual(msg.count("[✈️ 항공권 최저가]"), 3)
        self.assertEqual(msg.count("2026-12-03(목)"), 3)
        self.assertGreaterEqual(msg.count("250,00"), 3)

    # 16. BudgetCalculator & hotel total invariants (also for FALLBACK meta)
    def test_16_budget_invariants(self):
        from travel_brief.models import BudgetBreakdown

        cat = self._catalog()
        for meta in (self.engine.destinations["FUK"], cat.get("CTS").meta, cat.get("CDG").meta):
            for mult in (1.0, 1.30):
                b = BudgetBreakdown.calculate(meta, 412345, nights=3, hotel_multiplier=mult)
                self.assertEqual(b.days, 4)
                self.assertEqual(b.hotel_night_krw * b.nights, b.hotel_total_krw)
                self.assertEqual(b.meal_transit_total_krw, meta.daily_meal_transit_krw * 4)
                self.assertEqual(b.total_per_person_krw, 412345 + b.hotel_total_krw + b.meal_transit_total_krw)
                self.assertEqual(b.estimated_for_two_krw, 412345 * 2 + b.hotel_total_krw + b.meal_transit_total_krw * 2)
                if mult == 1.0:
                    self.assertEqual(b.hotel_total_krw, meta.hotel_night_avg_krw * 3)

    def _http_error(self, code, desc):
        import io
        import urllib.error

        return urllib.error.HTTPError(
            "https://api.telegram.org/x", code, desc, {}, io.BytesIO(('{"description": "%s"}' % desc).encode())
        )

    # 17. editMessageText 400 / timeout -> HTML send fallback
    def test_17_edit_failure_falls_back_to_html_send(self):
        h = self._handler(capture=False)
        for err in (self._http_error(400, "Bad Request: can't parse entities"), TimeoutError("timed out")):
            h.tg.reset_mock()
            h.tg.send_text.return_value = "900"
            with patch("travel_brief.interactive.urllib.request.urlopen", side_effect=err):
                ok = h._safe_edit_message("c1", 44, "<b>결과</b>", {"inline_keyboard": []})
            self.assertTrue(ok)
            h.tg.send_text.assert_called_once()
            self.assertEqual(h.tg.send_text.call_args[1]["parse_mode"], "HTML")
        # "not modified" is success without any send
        h.tg.reset_mock()
        with patch("travel_brief.interactive.urllib.request.urlopen",
                   side_effect=self._http_error(400, "Bad Request: message is not modified")):
            self.assertTrue(h._safe_edit_message("c1", 44, "x"))
        h.tg.send_text.assert_not_called()
        # Loading cards never send new messages
        with patch("travel_brief.interactive.urllib.request.urlopen", side_effect=TimeoutError("t")):
            self.assertFalse(h._edit_in_place_only("c1", 44, "loading"))
        h.tg.send_text.assert_not_called()

    # 18. HTML send fallback failure -> plain-text fallback
    def test_18_html_send_failure_falls_back_to_plain(self):
        h = self._handler(capture=False)
        h.tg.send_text.side_effect = [RuntimeError("Telegram HTTP 400 (sendMessage): can't parse"), "901"]
        with patch("travel_brief.interactive.urllib.request.urlopen", side_effect=self._http_error(400, "Bad Request")):
            ok = h._safe_edit_message("c1", 45, "<b>요금 &amp; 일정</b>")
        self.assertTrue(ok)
        second = h.tg.send_text.call_args_list[1]
        self.assertEqual(second[1]["parse_mode"], "")
        self.assertEqual(second[0][1], "요금 & 일정")
        h.tg.send_text.side_effect = RuntimeError("down")
        with patch("travel_brief.interactive.urllib.request.urlopen", side_effect=TimeoutError("t")):
            self.assertFalse(h._safe_edit_message("c1", 45, "<b>x</b>"))

    def _wait_for(self, cond, timeout=3.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if cond():
                return True
            time.sleep(0.02)
        return cond()

    # 19. Worker submit failure -> immediate error card; watchdog -> timeout card (latest generation only)
    def test_19_submit_failure_and_watchdog(self):
        import concurrent.futures

        h = self._handler()
        h.worker_pool.submit = MagicMock(side_effect=RuntimeError("pool down"))
        h.handle_callback_query("cq1", "c1", 50, "trv:q:FUK", {"id": 2001})
        self.assertTrue(any("특가 조회 실패" in t and "시작하지 못했습니다" in t for _, t in self.edits))
        st = h._msg_states[("c1", 50)]
        self.assertEqual((st.active_workers, st.phase, st.watchdog), (0, "DONE", None))
        self.assertNotIn("2001", h.cooldown._in_progress_users)

        h2 = self._handler()
        h2.watchdog_seconds = 0.15
        h2.worker_pool.submit = MagicMock(return_value=concurrent.futures.Future())  # never completes
        h2.handle_callback_query("cq2", "c2", 60, "trv:q:FUK", {"id": 2002})
        h2.handle_callback_query("cq3", "c2", 60, "trv:q:NRT", {"id": 2003})  # newer generation
        self.assertTrue(self._wait_for(lambda: any("조회 시간 초과" in t for _, t in self.edits)))
        time.sleep(0.3)
        timeouts = [t for _, t in self.edits if "조회 시간 초과" in t]
        self.assertEqual(len(timeouts), 1)
        self.assertIn("도쿄", timeouts[0])
        self.assertEqual(h2._msg_states[("c2", 60)].phase, "TIMEOUT")
        self.assertNotIn("2003", h2.cooldown._in_progress_users)

    # 20. Messages with active workers are never LRU-evicted
    def test_20_active_worker_not_evicted(self):
        h = self._handler()
        h.max_tracked_messages = 2
        h._next_request_seq("c", 1)
        lock_a = h._get_msg_lock("c", 1)
        h._worker_enter(("c", 1))
        for mid in (2, 3, 4):
            h._next_request_seq("c", mid)
        self.assertIn(("c", 1), h._msg_states)
        self.assertNotIn(("c", 2), h._msg_states)
        self.assertIs(h._get_msg_lock("c", 1), lock_a)
        h._worker_exit(("c", 1))
        h._next_request_seq("c", 5)
        self.assertNotIn(("c", 1), h._msg_states)
        self.assertLessEqual(len(h._msg_states), 2)

    # 21. Older generation never overwrites a newer result
    def test_21_older_generation_cannot_overwrite(self):
        from travel_brief.interactive import MessageState

        self.assertEqual(
            set(MessageState.__dataclass_fields__) >= {"generation", "phase", "lock", "active_workers", "deadline"}, True
        )
        h = self._handler()
        old = h._next_request_seq("c1", 70)
        new = h._next_request_seq("c1", 70)
        self.assertTrue(h._edit_if_latest("c1", 70, new, "NEW"))
        self.assertFalse(h._edit_if_latest("c1", 70, old, "OLD"))
        self.assertFalse(h._edit_if_latest("c1", 70, new, "LOADING", is_loading=True))
        self.assertEqual([t for _, t in self.edits], ["NEW"])
        self.assertGreater(h._msg_states[("c1", 70)].deadline, time.monotonic())

    # 22. Standalone runner & manager route text through the identical entry
    def test_22_standalone_and_manager_routing_identical(self):
        import inspect
        import travel_bot_runner
        from market_brief.multi_bot_manager import DynamicBotManager
        from travel_brief.interactive import dispatch_travel_update

        item = {"update_id": 5, "message": {"message_id": 9, "chat": {"id": 777, "type": "private", "first_name": "여행자"},
                                            "from": {"id": 777, "first_name": "여행자"}, "text": "삿포로"}}
        calls: list = []

        def fake(self_, *args, **kwargs):
            calls.append((args, kwargs.get("sender_name")))
            return True, "travel_search_started"

        manager = _make_manager()
        with patch.object(TravelInteractiveHandler, "handle_text_message", autospec=True, side_effect=fake), \
             patch("travel_brief.interactive.TelegramChannel"):
            self.assertEqual(manager._dispatch_travel_bot_update("tok", item), (True, "travel_message_handled"))
            standalone = self._handler()
            dispatch_travel_update(standalone, item, on_subscribe=lambda *_: None, on_unsubscribe=lambda *_: None)
        manager._travel_handler.shutdown()
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], calls[1])
        self.assertEqual(calls[0][0][:4], ("777", 9, "777", "삿포로"))
        self.assertIn("dispatch_travel_update(", inspect.getsource(travel_bot_runner.run_polling_mode))
        self.assertNotIn("send_message", inspect.getsource(DynamicBotManager._dispatch_travel_bot_update))
        # Manager without an unsubscribe hook delegates /stop back to its router
        with patch("travel_brief.interactive.TelegramChannel"):
            stop_item = dict(item, message=dict(item["message"], text="/stop"))
            self.assertEqual(manager._dispatch_travel_bot_update("tok", stop_item), (False, "travel_stop_delegated"))
        manager._travel_handler.shutdown()

    # 23. /start@BotName, trv:c:guide → AWAITING_CITY, direct city input → loading send → edit
    def test_23_commands_guide_and_direct_city(self):
        from travel_brief.interactive import STATE_AWAITING_CITY, STATE_IDLE

        h = self._handler()
        subs: list = []
        self.assertEqual(
            h.handle_text_message("c1", 1, "u1", "/start@KwTravelBot", {}, on_subscribe=lambda c, n: subs.append(c)),
            (True, "travel_start"),
        )
        self.assertEqual(subs, ["c1"])
        args, kwargs = h.tg.send_text.call_args
        self.assertIn("트래블 패스파인더", args[1])
        self.assertIn("trv:c:guide", str(kwargs["reply_markup"]))
        self.assertEqual(h.handle_text_message("c1", 2, "u1", "/HELP@KwTravelBot", {}), (True, "travel_help"))

        h.handle_callback_query("cq", "c1", 3, "trv:c:guide", {"id": "u1"})
        self.assertEqual(h.get_chat_state("c1"), STATE_AWAITING_CITY)
        h.ui_pool.shutdown(wait=True)
        self.assertIn("기타 지역 직접 검색", h.tg.send_text.call_args[0][1])

        h.tg.send_text.reset_mock()
        self.assertEqual(h.handle_text_message("c1", 4, "u1", "몰디브 여행", {}), (True, "travel_help_unknown_city"))
        self.assertIn("아직 지원하지 않는", h.tg.send_text.call_args[0][1])
        self.assertEqual(h.handle_text_message("c1", 4, "u1", "XYZ", {}), (True, "travel_help_unknown_city"))
        self.assertEqual(h.get_chat_state("c1"), STATE_AWAITING_CITY)

        h.tg.send_text.reset_mock()
        h.tg.send_text.return_value = "501"
        self.assertEqual(h.handle_text_message("c1", 5, "u1", "@KwTravelBot  신치토세", {}), (True, "travel_search_started"))
        self.assertEqual(h.get_chat_state("c1"), STATE_IDLE)
        loading = h.tg.send_text.call_args_list[0]
        self.assertIn("삿포로 실시간 특가 검색 중", loading[0][1])
        self.assertEqual(loading[1]["reply_markup"]["inline_keyboard"][0][0]["callback_data"], "trv:r:CTS")
        h.worker_pool.shutdown(wait=True)
        final = [(mid, t) for mid, t in self.edits if mid == 501]
        self.assertEqual(len(final), 1)
        self.assertIn("삿포로 주말 특가 나침반", final[0][1])
        self.assertIn("(CTS)", final[0][1])
        self.assertEqual(h._msg_states[("c1", 501)].phase, "DONE")
        self.assertNotIn("CTS", h.engine.destinations)


if __name__ == "__main__":
    unittest.main()
