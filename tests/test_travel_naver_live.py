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
                )
        self.assertTrue(ok)
        self.assertEqual(sent_ids, ["7820975792"])


if __name__ == "__main__":
    unittest.main()
