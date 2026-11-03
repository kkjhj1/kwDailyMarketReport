"""Unit tests for Naver Live Future-based Single-Flight, narrowed cache lock, and circuit breaker."""
from __future__ import annotations

import concurrent.futures
import sys
import time
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

import travel_brief.naver_live as nl
from travel_brief.flight_engine import FlightEngine
from travel_brief.models import FlightDeal


class TestNaverLiveSingleFlight(unittest.TestCase):
    def setUp(self):
        # Reset cache structures for isolated test
        with nl._CACHE_LOCK:
            nl._MEM_CACHE.clear()
            nl._IN_FLIGHT.clear()
            nl._FAIL_CACHE.clear()

        self.collector = nl.NaverFlightLiveClient()
        engine = FlightEngine()
        self.dest = engine.destinations["NRT"]
        self.dests = {"NRT": self.dest}
        today = date(2026, 11, 20)
        self.slots = [(today, today + timedelta(days=3), "목", "일", 3)]
        self.holiday_fn = lambda d1, d2: (False, "", 1.0)

    def test_single_flight_consolidates_concurrent_requests(self):
        call_count = 0

        def mock_collect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            time.sleep(0.1)  # Simulate network latency
            return [
                FlightDeal(
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
            ]

        with patch.object(self.collector, "_load_disk_cache", return_value=None), \
             patch.object(self.collector, "_save_disk_cache"), \
             patch.object(self.collector, "_collect_live_deals_uncached", side_effect=mock_collect):
            # Launch 5 parallel threads calling fetch_all_weekend_deals simultaneously
            with concurrent.futures.ThreadPoolExecutor(max_workers=5) as ex:
                futures = [
                    ex.submit(
                        self.collector.fetch_all_weekend_deals,
                        self.dests,
                        self.slots,
                        self.holiday_fn,
                        ref_date=date(2026, 11, 20),
                        months_ahead=6,
                    )
                    for _ in range(5)
                ]
                results = [f.result(timeout=5.0) for f in futures]

            # All 5 requests must get the exact same deal
            for res in results:
                self.assertEqual(len(res), 1)
                self.assertEqual(res[0].price_krw, 300000)

            # Single-Flight guarantee: actual scraper was called EXACTLY ONCE!
            self.assertEqual(call_count, 1)

    def test_circuit_breaker_fail_cache_on_upstream_outage(self):
        call_count = 0

        def mock_failing_collect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            raise RuntimeError("Upstream Naver 502 Bad Gateway")

        with patch.object(self.collector, "_load_disk_cache", return_value=None), \
             patch.object(self.collector, "_save_disk_cache"), \
             patch.object(self.collector, "_collect_live_deals_uncached", side_effect=mock_failing_collect):
            # First call fails and records into _FAIL_CACHE
            res1 = self.collector.fetch_all_weekend_deals(
                self.dests, self.slots, self.holiday_fn, ref_date=date(2026, 11, 20), months_ahead=6
            )
            self.assertEqual(res1, [])
            self.assertEqual(call_count, 1)

            # Immediate second call should be caught by 60s Circuit Breaker without calling scraper again!
            res2 = self.collector.fetch_all_weekend_deals(
                self.dests, self.slots, self.holiday_fn, ref_date=date(2026, 11, 20), months_ahead=6
            )
            self.assertEqual(res2, [])
            self.assertEqual(call_count, 1)  # Stays 1, no duplicate upstream storm!


if __name__ == "__main__":
    unittest.main()
