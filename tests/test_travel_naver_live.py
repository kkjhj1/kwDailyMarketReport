"""Unit tests for real-time Naver Flight GraphQL + SSE collector and schedule realism.
Compatible with both `unittest discover` (GitHub Actions CI) and `pytest`.
"""
from __future__ import annotations

import json
import socket
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from travel_brief.budget_calculator import BudgetCalculator
from travel_brief.flight_engine import FlightEngine
from travel_brief.models import FlightDeal, format_departure_time_slot, format_live_schedule_slots, reconcile_man_breakdown, round_man
from travel_brief.naver_live import (
    NaverFlightLiveClient,
    _MEM_CACHE,
    extract_latest_valid_pg_fare,
    parse_sse_final_object,
    parse_sse_stream_lines,
)


class TravelNaverLiveTests(unittest.TestCase):
    def setUp(self):
        _MEM_CACHE.clear()
        self.engine = FlightEngine(live_naver=False)

    def test_pg_trailing_null_and_zero_defense(self):
        pg_node = {
            "averageLowestFare": 351200.4,
            "fares": [
                {"lowestFare": 340000, "searchDate": "2026-10-30"},
                {"lowestFare": 298900, "searchDate": "2026-10-31"},
                {"lowestFare": 0, "searchDate": "2026-11-01"},
                {"lowestFare": None, "searchDate": "2026-11-02"},
            ],
        }
        latest, avg = extract_latest_valid_pg_fare(pg_node)
        self.assertEqual(latest, 298900)
        self.assertEqual(avg, 351200)

    def test_sse_nonstop_filter_and_exact_fare_extraction(self):
        sample_sse = {
            "status": {
                "isCompleted": True,
                "airlinesCodeMap": {"VJ": "비엣젯항공", "WE": "파라타항공", "CX": "캐세이퍼시픽"},
            },
            "itineraries": [
                {
                    "itineraryId": "OUT_LAYOVER",
                    "segments": [{"departure": {"time": "0800"}}, {"departure": {"time": "1300"}}],
                },
                {
                    "itineraryId": "OUT_WE",
                    "segments": [
                        {
                            "departure": {"airportCode": "ICN", "date": "20270114", "time": "1815"},
                            "arrival": {"airportCode": "DAD", "date": "20270114", "time": "2115"},
                            "marketingCarrier": {"airlineCode": "WE"},
                        }
                    ],
                },
                {
                    "itineraryId": "RET_WE",
                    "segments": [
                        {
                            "departure": {"airportCode": "DAD", "date": "20270117", "time": "2300"},
                            "arrival": {"airportCode": "ICN", "date": "20270118", "time": "0520"},
                            "marketingCarrier": {"airlineCode": "WE"},
                        }
                    ],
                },
                {
                    "itineraryId": "OUT_VJ",
                    "segments": [
                        {
                            "departure": {"airportCode": "ICN", "date": "20270114", "time": "2350"},
                            "arrival": {"airportCode": "DAD", "date": "20270115", "time": "0250"},
                            "marketingCarrier": {"airlineCode": "VJ"},
                        }
                    ],
                },
                {
                    "itineraryId": "RET_VJ",
                    "segments": [
                        {
                            "departure": {"airportCode": "DAD", "date": "20270117", "time": "2340"},
                            "arrival": {"airportCode": "ICN", "date": "20270118", "time": "0600"},
                            "marketingCarrier": {"airlineCode": "VJ"},
                        }
                    ],
                },
            ],
            "fareMappings": [
                # Cheaper layover must be ignored
                {
                    "itineraryIds": "OUT_LAYOVER-RET_VJ",
                    "fares": [{"fareType": "A01", "adult": {"totalFare": 390000}}],
                },
                # Higher fare placed first to ensure explicit min sorting
                {
                    "itineraryIds": "OUT_WE-RET_WE",
                    "fares": [{"fareType": "A01", "adult": {"totalFare": 679000}}],
                },
                # True cheapest nonstop with card vs general A01 distinction
                {
                    "itineraryIds": "OUT_VJ-RET_VJ",
                    "fares": [
                        {"fareType": "A01/B04", "adult": {"totalFare": 452000}},
                        {"fareType": "A01", "adult": {"totalFare": 466000}},
                        {"fareType": "ERR", "adult": {"totalFare": 0}},
                    ],
                },
            ],
            "popularFlights": [
                {
                    "itineraryIds": "OUT_WE-RET_WE",
                    "fares": [{"fareType": "A01", "adult": {"totalFare": 679000}}],
                }
            ],
        }
        parsed = parse_sse_final_object(sample_sse, ret_compact="20270117")
        self.assertIsNotNone(parsed)
        ch = parsed["cheapest"]
        pop = parsed["popular"]
        self.assertEqual(ch["airline"], "비엣젯항공")
        self.assertEqual(ch["min_card"], 452000)
        self.assertEqual(ch["min_general"], 466000)
        self.assertEqual(ch["dep_time"], "23:50")
        self.assertEqual(ch["ret_dep_time"], "23:40")
        self.assertEqual(ch["ret_arr_time"], "06:00")
        self.assertTrue(ch["next_day_arrival"])
        self.assertEqual(pop["min_card"], 679000)
        self.assertEqual(pop["airline"], "파라타항공")

    def test_sse_stream_timeout_recovery(self):
        valid_chunk = {
            "status": {"isCompleted": False, "airlinesCodeMap": {"7C": "제주항공"}},
            "itineraries": [
                {
                    "itineraryId": "O1",
                    "segments": [
                        {
                            "departure": {"time": "1620"},
                            "arrival": {"date": "20270114", "time": "1755"},
                            "marketingCarrier": {"airlineCode": "7C"},
                        }
                    ],
                },
                {
                    "itineraryId": "R1",
                    "segments": [
                        {
                            "departure": {"time": "0855"},
                            "arrival": {"date": "20270117", "time": "1030"},
                            "marketingCarrier": {"airlineCode": "7C"},
                        }
                    ],
                },
            ],
            "fareMappings": [
                {"itineraryIds": "O1-R1", "fares": [{"fareType": "A01", "adult": {"totalFare": 347900}}]}
            ],
        }

        def interrupted_stream():
            yield b"event: message\n"
            yield f"data: {json.dumps(valid_chunk)}\n".encode("utf-8")
            # Trailing empty heartbeat chunk followed by socket timeout
            yield b'data: {"status": {"isCompleted": false}, "fareMappings": []}\n'
            raise socket.timeout("partner timeout")

        recovered = parse_sse_stream_lines(interrupted_stream(), ret_compact="20270117")
        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["cheapest"]["min_card"], 347900)
        self.assertEqual(recovered["cheapest"]["airline"], "제주항공")
        self.assertFalse(recovered["cheapest"]["next_day_arrival"])

    def test_live_clock_time_vacation_cutoffs_and_backward_compatibility(self):
        # Backward compatibility for existing offline slot helper
        self.assertEqual(format_departure_time_slot("목"), "오전 08:30~10:30 (목·금 연차 2일 추천)")
        self.assertEqual(format_departure_time_slot("금", "afternoon"), "오후 14:00~16:00 (금요일 오후 반차 추천)")

        # 1. TPE Friday 00:30 red-eye -> must require Thursday night airport + 2 leave days, NEVER post-work!
        dep_tpe, _ = format_live_schedule_slots("금", "월", "00:30", "20:00", "23:30", False)
        self.assertIn("목 퇴근 후 공항행", dep_tpe)
        self.assertIn("금·월 연차 2일", dep_tpe)

        # 2. NRT Friday 12:55 -> midday requires full Friday leave, NOT half-day!
        dep_nrt, _ = format_live_schedule_slots("금", "월", "12:55", "08:55", "11:30", False)
        self.assertIn("금·월 연차 2일", dep_nrt)
        self.assertNotIn("반차", dep_nrt)

        # 3. FUK Friday 18:15 -> half-day / early leave, NOT post-18:00 work!
        dep_fuk, _ = format_live_schedule_slots("금", "월", "18:15", "08:55", "10:30", False)
        self.assertIn("금 오후반차", dep_fuk)

        # 4. GUM Friday 23:15 + next-day return arrival
        dep_gum, ret_gum = format_live_schedule_slots("금", "월", "23:15", "23:45", "06:05", True)
        self.assertIn("금 퇴근 후 출발", dep_gum)
        self.assertIn("익일 +1일 귀국", ret_gum)

    def test_live_promo_fare_never_clamped_by_min_floor(self):
        gum = self.engine.get_destination("GUM")
        self.assertEqual(gum.flight_min_floor_krw, 430000)

        base_deal = FlightDeal(
            destination_code="GUM",
            destination_name=gum.name,
            origin_code="ICN",
            departure_date="2027-03-19",
            return_date="2027-03-22",
            departure_day_of_week="금",
            return_day_of_week="월",
            nights=3,
            airline="네이버 직항 최저가",
            price_krw=257800,
            baseline_krw=gum.flight_baseline_krw,
            data_source="LIVE_PG",
        )
        sse_data = {
            "cheapest": {
                "airline": "진에어",
                "dep_time": "23:15",
                "ret_dep_time": "03:00",
                "ret_arr_time": "06:45",
                "next_day_arrival": False,
                "min_card": 257800,
                "min_general": 272400,
            },
            "popular": {
                "airline": "대한항공",
                "dep_time": "09:50",
                "ret_dep_time": "16:50",
                "ret_arr_time": "20:35",
                "next_day_arrival": False,
                "min_card": 549800,
                "min_general": 565000,
            },
        }
        upgraded = NaverFlightLiveClient.apply_sse_to_deal(base_deal, gum, sse_data)
        # Exact 1-KRW preservation without 430,000 floor clamp or 1,000 rounding
        self.assertEqual(upgraded.price_krw, 257800)
        self.assertEqual(upgraded.exact_price_str, "257,800원~")
        self.assertIn("일반결제 272,400원", upgraded.live_payment_note)
        self.assertIn("549,800원~", upgraded.popular_banner_note)
        self.assertIn("만원대", upgraded.price_band_str)

    def test_post_sse_rerank_prevents_stale_pg_inversion_and_direct_url(self):
        sin_dest = {"SIN": self.engine.get_destination("SIN")}
        slots = [
            (date(2026, 11, 13), date(2026, 11, 16), "금", "월", 3),
            (date(2026, 11, 20), date(2026, 11, 23), "금", "월", 3),
            (date(2026, 11, 27), date(2026, 11, 30), "금", "월", 3),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            client = NaverFlightLiveClient(cache_path=Path(tmp) / "cache.json")

            # Stage 1: Slot 1 (397k) and Slot 2 (410k) look cheaper than Slot 3 (445k)
            fake_pg = {
                ("2026-11-13", "2026-11-16"): (397300, 420000),
                ("2026-11-20", "2026-11-23"): (410000, 430000),
                ("2026-11-27", "2026-11-30"): (445000, 460000),
            }

            # Stage 2: Slot 1 & Slot 2 surged to 506k / 520k in real time!
            # Promotion guard must catch Slot 3 jumping to #1 and SSE-verify Slot 3 -> 448,200!
            sse_responses = {
                "2026-11-13": {
                    "cheapest": {
                        "airline": "제주항공", "dep_time": "18:10", "ret_dep_time": "01:10",
                        "ret_arr_time": "08:40", "next_day_arrival": False,
                        "min_card": 506700, "min_general": 543300,
                    },
                    "popular": None,
                },
                "2026-11-20": {
                    "cheapest": {
                        "airline": "티웨이항공", "dep_time": "19:00", "ret_dep_time": "02:00",
                        "ret_arr_time": "09:30", "next_day_arrival": False,
                        "min_card": 520000, "min_general": 520000,
                    },
                    "popular": None,
                },
                "2026-11-27": {
                    "cheapest": {
                        "airline": "스쿠트항공", "dep_time": "22:30", "ret_dep_time": "01:45",
                        "ret_arr_time": "09:10", "next_day_arrival": False,
                        "min_card": 448200, "min_general": 448200,
                    },
                    "popular": None,
                },
            }

            with patch.object(client, "fetch_city_batch_pg", return_value=fake_pg), patch.object(
                client, "_fetch_session_cookie_header", return_value=""
            ), patch.object(
                client,
                "fetch_live_sse_slot",
                side_effect=lambda code, dep_i, ret_i, cookie_header="": sse_responses.get(dep_i),
            ):
                deals = client.fetch_all_weekend_deals(
                    destinations=sin_dest,
                    slots=slots,
                    holiday_fn=FlightEngine.get_peak_holiday_info,
                    ref_date=date(2026, 11, 2),
                    months_ahead=1,
                )

            self.assertEqual(len(deals), 3)
            best = deals[0]
            self.assertEqual(best.departure_date, "2026-11-27")
            self.assertEqual(best.data_source, "LIVE_SSE")
            self.assertEqual(best.price_krw, 448200)
            self.assertEqual(best.airline, "스쿠트항공")

            rec = BudgetCalculator.evaluate_deal(sin_dest["SIN"], best)
            self.assertIn("m-flight.naver.com", rec.naver_flight_url)
            self.assertIn("isDirect=true", rec.naver_flight_url)

    def test_round_man_half_up_consistency(self):
        # 285,000 KRW must round half-up to 29만 (NOT Banker's even-round 28만), so 20 + 29 + 18 == 67만
        self.assertEqual(round_man(203204), 20)
        self.assertEqual(round_man(285000), 29)
        self.assertEqual(round_man(180000), 18)
        self.assertEqual(round_man(668204), 67)
        self.assertEqual(round_man(203204) + round_man(285000) + round_man(180000), round_man(668204))
        # Taipei live 215,754 + 285,000 + 180,000 = 680,754 -> 68만 must reconcile cleanly (22 + 28 + 18 == 68)
        f_m, h_m, m_m, tot_m = reconcile_man_breakdown(215754, 285000, 180000)
        self.assertEqual(tot_m, 68)
        self.assertEqual(f_m + h_m + m_m, tot_m)

    def test_telegram_html_escaping_length_guard_and_recipient_dedupe(self):
        from travel_bot_runner import format_telegram_briefing, run_briefing

        deals = self.engine.search_deals(months_ahead=6, ref_date=date(2026, 11, 2))
        recs = BudgetCalculator.rank_recommendations(self.engine.destinations, deals, top_n=3)
        msg = format_telegram_briefing(recs, engine=self.engine)

        self.assertLessEqual(len(msg), 4096)
        self.assertIn("&amp;adult=1&amp;isDirect=true", msg)
        self.assertNotIn("&adult=1", msg)
        self.assertIn("1인당", msg)

        sent_ids: list[str] = []
        with tempfile.TemporaryDirectory() as tmp:
            db_file = str(Path(tmp) / "test_alerts.sqlite3")
            with patch("travel_bot_runner.get_travel_bot_token", return_value="DUMMY_TOKEN"), patch(
                "travel_bot_runner.SafeTelegramDispatcher.send_text_to_recipient",
                side_effect=lambda tg, chat_id, text, parse_mode="HTML", db=None: (sent_ids.append(chat_id) or ("SENT", "101", None)),
            ):
                ok = run_briefing(
                    dry_run=False,
                    recipients_override=["7820975792", "7820975792", " 7820975792 "],
                    db_path=db_file,
                    live_naver=False,
                    output_dir=Path(tmp),
                )
        self.assertTrue(ok)
        self.assertEqual(sent_ids, ["7820975792"])

    def test_worker_caps_are_never_raised(self):
        from travel_brief import naver_live as nl

        self.assertEqual(nl.PG_MAX_WORKERS, 5)
        self.assertEqual(NaverFlightLiveClient(max_sse_workers=9).max_sse_workers, 2)
        self.assertEqual(NaverFlightLiveClient(max_sse_workers=0).max_sse_workers, 1)
        self.assertEqual(NaverFlightLiveClient().max_sse_workers, 2)

    def test_pg_only_collection_skips_sse_and_uses_its_own_namespace(self):
        import time as _time

        ref = date(2031, 1, 6)
        dests = {"FUK": self.engine.destinations["FUK"]}
        slots = self.engine.generate_weekend_slots(ref_date=ref, months_ahead=1)
        holiday_fn = self.engine.get_peak_holiday_info
        dep_iso, ret_iso = slots[0][0].isoformat(), slots[0][1].isoformat()
        with tempfile.TemporaryDirectory() as tmp:
            client = NaverFlightLiveClient(cache_path=Path(tmp) / "c.json")
            # 1. stage-1 only: no cookie preflight, no SSE call, LIVE_PG deals
            with patch.object(client, "fetch_city_batch_pg", return_value={(dep_iso, ret_iso): (150_000, 160_000)}), \
                    patch.object(client, "fetch_live_sse_slot", side_effect=AssertionError("no SSE in pg_only")), \
                    patch.object(client, "_fetch_session_cookie_header", side_effect=AssertionError("no preflight")):
                deals = client._collect_live_deals_uncached(dests, slots, holiday_fn, pg_only=True)
            self.assertEqual([d.data_source for d in deals], ["LIVE_PG"])

            # 2. pg_only lives under `#pg` and never overwrites the full entry
            seen: list[dict] = []

            def fake_collect(destinations, slots_, holiday_fn_, **kw):
                seen.append(kw)
                return list(deals)

            with patch.object(client, "_collect_live_deals_uncached", side_effect=fake_collect):
                out = client.fetch_all_weekend_deals(dests, slots, holiday_fn, ref_date=ref, pg_only=True)
            self.assertTrue(out)
            self.assertEqual(seen, [{"pg_only": True}])
            self.assertIn(("2031-01-06#pg", 6, ("FUK",)), _MEM_CACHE)
            self.assertNotIn(("2031-01-06", 6, ("FUK",)), _MEM_CACHE)

            # 3. a fresh full (SSE) entry is a strict upgrade: pg_only lookups reuse it without network I/O
            full = FlightDeal(
                destination_code="FUK", destination_name="후쿠오카", origin_code="ICN",
                departure_date=dep_iso, return_date=ret_iso, departure_day_of_week="목", return_day_of_week="일",
                nights=3, airline="진에어", price_krw=140_000, baseline_krw=220_000, data_source="LIVE_SSE",
            )
            _MEM_CACHE[("2031-01-06", 6, ("FUK",))] = (_time.time(), [full])
            with patch.object(client, "_collect_live_deals_uncached", side_effect=AssertionError("must hit cache")):
                reused = client.fetch_all_weekend_deals(dests, slots, holiday_fn, ref_date=ref, pg_only=True)
            self.assertEqual([d.data_source for d in reused], ["LIVE_SSE"])

    def test_trip_profile_namespaces_long_haul_slots_in_cache_keys(self):
        from travel_brief.city_catalog import CityCatalog

        class _FakeLive:
            def __init__(self):
                self.calls = []

            def fetch_all_weekend_deals(self, destinations, slots, holiday_fn, ref_date=None, months_ahead=6,
                                        fast_mode=False, slot_signature=None, deadline=None, pg_only=False):
                self.calls.append((sorted(destinations), slot_signature, {s[4] for s in slots}, pg_only))
                return []

        engine = FlightEngine(live_naver=False)
        engine.live_naver = True
        engine._live_client = _FakeLive()
        extra = CityCatalog(engine.destinations).destinations_for(["CDG", "CTS"])
        engine.search_deals(extra_destinations=extra, ref_date=date(2026, 11, 16), pg_only=True)
        calls = engine._live_client.calls
        self.assertIn((["CTS"], None, {3}, True), calls)       # short-haul: legacy key namespace
        self.assertIn((["CDG"], "#L5", {5}, True), calls)      # long-haul: own `#L5` namespace, 5N slots
        self.assertEqual(len(calls), 2)
        # the legacy curated ALL search stays a single short-haul call
        engine._live_client.calls.clear()
        engine.search_deals(ref_date=date(2026, 11, 16))
        self.assertEqual(engine._live_client.calls, [(sorted(engine.destinations), None, {3}, False)])

    # ------------------------------------------------------------------
    # 15s live budget: deadline-bounded, non-blocking SSE
    # ------------------------------------------------------------------
    @staticmethod
    def _pg_deal(code: str = "FUK", dep: str = "2031-01-09", ret: str = "2031-01-12", price: int = 150_000) -> FlightDeal:
        return FlightDeal(
            destination_code=code, destination_name=code, origin_code="ICN", departure_date=dep, return_date=ret,
            departure_day_of_week="금", return_day_of_week="월", nights=3, airline="네이버 직항 최저가",
            price_krw=price, baseline_krw=220_000, data_source="LIVE_PG",
        )

    def test_ensure_sse_deadline_abandons_slow_future_without_blocking(self):
        import threading
        import time as _time

        release = threading.Event()
        seen: list = []

        def slow_sse(code, dep_i, ret_i, cookie_header="", deadline=None):
            seen.append(deadline)
            release.wait(5)  # a hung SSE socket
            return None

        target = self._pg_deal()
        with tempfile.TemporaryDirectory() as tmp:
            client = NaverFlightLiveClient(cache_path=Path(tmp) / "c.json")
            deadline = _time.monotonic() + 0.3
            try:
                with patch.object(client, "fetch_live_sse_slot", side_effect=slow_sse), \
                     patch.object(client, "_fetch_session_cookie_header", return_value="") as cookie:
                    t0 = _time.monotonic()
                    out = client.ensure_deals_sse_verified([target], self.engine.destinations, deadline=deadline)
                    elapsed = _time.monotonic() - t0
            finally:
                release.set()
        self.assertLess(elapsed, 1.5)  # never blocked by the executor's __exit__ on the slow future
        self.assertEqual(out, [target])  # PG deal returned as-is (never an error)
        self.assertEqual(seen, [deadline])  # deadline propagated into the SSE slot call
        self.assertEqual(cookie.call_args.kwargs, {"deadline": deadline})
        # an already-spent deadline makes no SSE call at all
        with patch.object(client, "fetch_live_sse_slot", side_effect=AssertionError("no SSE")):
            self.assertEqual(client.ensure_deals_sse_verified([target], self.engine.destinations,
                                                              deadline=_time.monotonic() - 1), [target])

    def test_fast_collection_returns_pg_deals_when_sse_hits_deadline(self):
        import threading
        import time as _time

        release = threading.Event()
        ref = date(2031, 1, 6)
        dests = {"FUK": self.engine.destinations["FUK"], "KIX": self.engine.destinations["KIX"]}
        slots = self.engine.generate_weekend_slots(ref_date=ref, months_ahead=1)

        def fake_pg(code, slots_, deadline=None):
            base = 150_000 if code == "FUK" else 170_000
            return {(s[0].isoformat(), s[1].isoformat()): (base + i * 1000, base + 20_000) for i, s in enumerate(slots_)}

        def slow_sse(code, dep_i, ret_i, cookie_header="", deadline=None):
            release.wait(5)
            return None

        with tempfile.TemporaryDirectory() as tmp:
            client = NaverFlightLiveClient(cache_path=Path(tmp) / "c.json")
            try:
                with patch.object(client, "fetch_city_batch_pg", side_effect=fake_pg), \
                     patch.object(client, "fetch_live_sse_slot", side_effect=slow_sse), \
                     patch.object(client, "_fetch_session_cookie_header", return_value=""):
                    t0 = _time.monotonic()
                    deals = client._collect_live_deals_uncached(
                        dests, slots, self.engine.get_peak_holiday_info, fast_mode=True, deadline=_time.monotonic() + 0.4
                    )
                    elapsed = _time.monotonic() - t0
            finally:
                release.set()
        self.assertLess(elapsed, 1.5)
        self.assertEqual(len(deals), len(dests) * len(slots))  # full PG matrix kept
        self.assertTrue(all(d.data_source == "LIVE_PG" for d in deals))
        self.assertEqual(deals[0].destination_code, "FUK")

    def test_sse_slot_timeout_clamped_and_retry_skipped_near_deadline(self):
        import time as _time

        client = NaverFlightLiveClient(sse_timeout=10.0)
        timeouts: list = []

        def failing_urlopen(req, timeout=None):
            timeouts.append(timeout)
            raise OSError("offline")

        with patch("urllib.request.urlopen", side_effect=failing_urlopen):
            # 1.5s left: one attempt, socket timeout clamped below the remaining budget, no retry (<= 2.0s left)
            self.assertIsNone(client.fetch_live_sse_slot("FUK", "2031-01-09", "2031-01-12", deadline=_time.monotonic() + 1.5))
            self.assertEqual(len(timeouts), 1)
            self.assertLessEqual(timeouts[0], 1.5)
            # spent deadline: no request at all
            timeouts.clear()
            self.assertIsNone(client.fetch_live_sse_slot("FUK", "2031-01-09", "2031-01-12", deadline=_time.monotonic() - 1))
            self.assertEqual(timeouts, [])
            # no deadline: legacy 2 attempts with the full socket timeout
            self.assertIsNone(client.fetch_live_sse_slot("FUK", "2031-01-09", "2031-01-12"))
            self.assertEqual(timeouts, [10.0, 10.0])

    def test_engine_forwards_live_deadline_to_sse_verification(self):
        class _FakeLive:
            def __init__(self):
                self.calls: list = []

            def ensure_deals_sse_verified(self, deals, destinations, deadline=None):
                self.calls.append(deadline)
                return deals

        engine = FlightEngine(live_naver=False)
        engine.live_naver = True
        engine._live_client = _FakeLive()
        deal = self._pg_deal()
        engine.ensure_deals_sse_verified([deal], live_deadline=123.0)
        engine.ensure_deals_sse_verified([deal])
        self.assertEqual(engine._live_client.calls, [123.0, None])

    def test_parse_sse_extracts_office_candidate_and_rejects_bad_arrivals(self):
        from datetime import date
        from travel_brief.naver_live import NaverFlightLiveClient, parse_sse_stream_lines
        from travel_brief.flight_engine import FlightEngine

        # Fri 2026-12-04 -> Sun 2026-12-06: morning cheapest (1.0 leave), evening good-return office (0.0 leave),
        # plus next_day_arrival and missing ret_arr_time decoys that are cheaper than valid office!
        payload = {
            "status": {
                "isCompleted": True,
                "airlinesCodeMap": {"7C": "제주항공", "KE": "대한항공"},
            },
            "itineraries": [
                {
                    "itineraryId": "OUT_1",
                    "segments": [{"marketingCarrier": {"airlineCode": "7C"}, "departure": {"airportCode": "ICN", "date": "20261204", "time": "0830"}}],
                },
                {
                    "itineraryId": "RET_1",
                    "segments": [{"marketingCarrier": {"airlineCode": "7C"}, "departure": {"airportCode": "FUK", "date": "20261206", "time": "1030"}, "arrival": {"airportCode": "ICN", "date": "20261206", "time": "1200"}}],
                },
                {
                    "itineraryId": "OUT_2",
                    "segments": [{"marketingCarrier": {"airlineCode": "7C"}, "departure": {"airportCode": "ICN", "date": "20261204", "time": "1900"}}],
                },
                {
                    "itineraryId": "RET_2",
                    "segments": [{"marketingCarrier": {"airlineCode": "7C"}, "departure": {"airportCode": "FUK", "date": "20261206", "time": "2300"}, "arrival": {"airportCode": "ICN", "date": "20261207", "time": "0030"}}],
                },
                {
                    "itineraryId": "OUT_3",
                    "segments": [{"marketingCarrier": {"airlineCode": "7C"}, "departure": {"airportCode": "ICN", "date": "20261204", "time": "1910"}}],
                },
                {
                    "itineraryId": "RET_3",
                    "segments": [{"marketingCarrier": {"airlineCode": "7C"}, "departure": {"airportCode": "FUK", "date": "20261206", "time": "1400"}, "arrival": {"airportCode": "ICN", "date": "20261206", "time": ""}}],
                },
                {
                    "itineraryId": "OUT_4",
                    "segments": [{"marketingCarrier": {"airlineCode": "KE"}, "departure": {"airportCode": "ICN", "date": "20261204", "time": "1920"}}],
                },
                {
                    "itineraryId": "RET_4",
                    "segments": [{"marketingCarrier": {"airlineCode": "KE"}, "departure": {"airportCode": "FUK", "date": "20261206", "time": "1640"}, "arrival": {"airportCode": "ICN", "date": "20261206", "time": "1810"}}],
                },
            ],
            "fareMappings": [
                {"itineraryIds": "OUT_1-RET_1", "fares": [{"fareType": "A01", "adult": {"totalFare": 210000}}]},
                {"itineraryIds": "OUT_2-RET_2", "fares": [{"fareType": "A01", "adult": {"totalFare": 230000}}]},  # next_day_arrival -> rejected
                {"itineraryIds": "OUT_3-RET_3", "fares": [{"fareType": "A01", "adult": {"totalFare": 240000}}]},  # missing ret_arr_time -> rejected
                {"itineraryIds": "OUT_4-RET_4", "fares": [{"fareType": "A01", "adult": {"totalFare": 265000}}]},
            ],
        }
        line = "data: " + json.dumps(payload) + "\n"
        parsed = parse_sse_stream_lines([line], ret_compact="20261206", dep_date=date(2026, 12, 4), ret_date=date(2026, 12, 6))
        self.assertEqual(parsed["cheapest"]["min_card"], 210000)
        self.assertIsNotNone(parsed["office"])
        self.assertEqual(parsed["office"]["min_card"], 265000)
        self.assertEqual(parsed["office"]["dep_time"], "19:20")
        self.assertEqual(parsed["office"]["leave_days"], 0.0)

        engine = FlightEngine(live_naver=False)
        dest = engine.get_destination("FUK")
        base_deal = engine.search_deals(dest_code="FUK", months_ahead=1, ref_date=date(2026, 11, 20))[0]
        from dataclasses import replace
        base_deal = replace(base_deal, departure_date="2026-12-04", return_date="2026-12-06", nights=2)
        upgraded = NaverFlightLiveClient.apply_sse_to_deal(base_deal, dest, parsed)
        self.assertEqual(upgraded.office_fare_krw, 265000)
        self.assertEqual(upgraded.office_leave_days, 0.0)
        self.assertEqual(upgraded.office_ret_arr_time, "18:10")

    # ------------------------------------------------------------------
    # Time-window watch: SSE candidates, unknown next-day arrival, cache roundtrip
    # ------------------------------------------------------------------
    @staticmethod
    def _tw_payload():
        def leg(iid, code, dep_date, dep_time, arr_date=None, arr_time=None):
            seg = {"marketingCarrier": {"airlineCode": code}, "departure": {"date": dep_date, "time": dep_time}}
            if arr_date is not None or arr_time is not None:
                seg["arrival"] = {"date": arr_date or "", "time": arr_time or ""}
            return {"itineraryId": iid, "segments": [seg]}

        return {
            "status": {"airlinesCodeMap": {"7C": "제주항공", "KE": "대한항공"}},
            "itineraries": [
                leg("O1", "7C", "20261204", "0700"), leg("R1", "7C", "20261206", "1030", "20261206", "1200"),
                leg("O2", "KE", "20261204", "0930"), leg("R2", "KE", "20261206", "1640", "20261206", "1810"),
                leg("O3", "7C", "20261204", "1000"), leg("R3", "7C", "20261206", "1500", "", "1630"),  # no arr date
            ],
            "fareMappings": [
                {"itineraryIds": "O1-R1", "fares": [{"fareType": "A01", "adult": {"totalFare": 180000}}]},
                {"itineraryIds": "O2-R2", "fares": [{"fareType": "A01", "adult": {"totalFare": 260000}}]},
                {"itineraryIds": "O3-R3", "fares": [{"fareType": "A01", "adult": {"totalFare": 220000}}]},
            ],
        }

    def test_sse_candidates_ids_and_unknown_next_day_arrival(self):
        parsed = parse_sse_final_object(
            self._tw_payload(), ret_compact="20261206", dep_date=date(2026, 12, 4), ret_date=date(2026, 12, 6)
        )
        cands = parsed["candidates"]
        self.assertEqual([c["min_card"] for c in cands], [180000, 220000, 260000])
        self.assertEqual((cands[0]["outbound_id"], cands[0]["return_id"]), ("O1", "R1"))
        self.assertIs(cands[0]["next_day_arrival"], False)
        self.assertIsNone(cands[1]["next_day_arrival"])  # missing arrival date -> unknown, never "same day"
        self.assertEqual(cands[1]["ret_arr_date"], "")
        self.assertEqual(parsed["cheapest"]["min_card"], 180000)
        # OFFICE needs a VERIFIED same-day arrival: the unknown-date 220k decoy is never the office pick
        self.assertEqual(parsed["office"]["min_card"], 260000)
        # requested return date missing -> every candidate unknown
        unknown = parse_sse_final_object(self._tw_payload(), ret_compact="")
        self.assertTrue(all(c["next_day_arrival"] is None for c in unknown["candidates"]))

    def test_apply_sse_populates_candidates_and_cache_roundtrip(self):
        from dataclasses import replace

        from travel_brief.models import SSEFlightCandidate
        from travel_brief.naver_live import NaverFlightLiveClient

        parsed = parse_sse_final_object(
            self._tw_payload(), ret_compact="20261206", dep_date=date(2026, 12, 4), ret_date=date(2026, 12, 6)
        )
        engine = FlightEngine(live_naver=False)
        dest = engine.get_destination("FUK")
        base = engine.search_deals(dest_code="FUK", months_ahead=1, ref_date=date(2026, 11, 20))[0]
        base = replace(base, departure_date="2026-12-04", return_date="2026-12-06", nights=2)
        deal = NaverFlightLiveClient.apply_sse_to_deal(base, dest, parsed)
        self.assertEqual(deal.data_source, "LIVE_SSE")
        self.assertEqual(len(deal.sse_candidates), 3)
        self.assertIsInstance(deal.sse_candidates[0], SSEFlightCandidate)
        self.assertEqual(deal.sse_candidates[0].fingerprint, "O1:R1")
        self.assertEqual(deal.sse_candidates[2].airline, "대한항공")
        self.assertEqual(deal.office_fare_krw, 260000)

        # JSON roundtrip through the disk-cache dict form keeps candidates (incl. the None arrival flag)
        blob = json.loads(json.dumps(deal.to_cache_dict(), ensure_ascii=False))
        self.assertIsInstance(blob["sse_candidates"], list)
        back = FlightDeal.from_cache_dict(blob)
        self.assertEqual(back.sse_candidates, deal.sse_candidates)
        self.assertIsNone(back.sse_candidates[1].next_day_arrival)
        self.assertEqual((back.price_krw, back.exact_dep_time), (deal.price_krw, deal.exact_dep_time))
        # legacy cache rows (no `sse_candidates` key) still load
        legacy = deal.to_cache_dict()
        legacy.pop("sse_candidates")
        legacy.pop("candidate_fingerprint")
        self.assertEqual(FlightDeal.from_cache_dict(legacy).sse_candidates, ())

        with tempfile.TemporaryDirectory() as tmp:
            client = NaverFlightLiveClient(cache_path=Path(tmp) / "c.json")
            client._save_disk_cache("k1", [deal])
            loaded = client._load_disk_cache("k1", max_age_sec=600)
            self.assertEqual(loaded[0].sse_candidates, deal.sse_candidates)


if __name__ == "__main__":
    unittest.main()
