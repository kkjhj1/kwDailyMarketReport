import unittest
from datetime import datetime
from travel_brief.models import (
    DestinationMeta,
    FlightDeal,
    BudgetBreakdown,
    TravelRecommendation,
    TravelSnapshot,
    reconcile_man_breakdown,
    round_man,
)
from travel_brief.flight_engine import FlightEngine
from travel_brief.web_report import WebReportGenerator, build_travel_snapshot
from travel_bot_runner import format_telegram_briefing


class TravelPriceRedesignTests(unittest.TestCase):
    def setUp(self):
        self.engine = FlightEngine(live_naver=False)

    def test_hotel_budget_segment_and_invariant(self):
        """Invariant: hotel_night_krw * nights == hotel_total_krw and segment loaded."""
        dest = self.engine.get_destination("KIX")
        self.assertIsNotNone(dest)
        self.assertEqual(dest.hotel_budget_segment, "3성급 가성비 비즈니스 호텔")
        self.assertEqual(dest.recommended_hotel_name, "크로스 호텔 오사카 (Cross Hotel Osaka)")

        budget = BudgetBreakdown.calculate(dest=dest, flight_price=270000, nights=3)
        self.assertEqual(budget.hotel_night_krw, dest.hotel_night_avg_krw)
        self.assertEqual(budget.hotel_night_krw * budget.nights, budget.hotel_total_krw)

    def test_flight_deal_honest_source_labels(self):
        """Honest source labels: LIVE_SSE vs LIVE_PG vs OFFLINE_ESTIMATE."""
        # 1. LIVE_SSE
        deal_sse = FlightDeal(
            origin_code="ICN",
            destination_code="KIX",
            destination_name="오사카",
            departure_date="2026-12-18",
            return_date="2026-12-21",
            departure_day_of_week="금",
            return_day_of_week="월",
            nights=3,
            airline="티웨이항공",
            price_krw=215900,
            baseline_krw=330000,
            data_source="LIVE_SSE",
            freshness="FRESH",
        )
        self.assertIn("네이버 실시간 관측 최저가", deal_sse.live_payment_note)
        self.assertIn("실시간 관측", deal_sse.source_badge)
        self.assertNotIn("확정가", deal_sse.live_payment_note)

        # 2. LIVE_PG (Price Graph Cache)
        deal_pg = FlightDeal(
            origin_code="ICN",
            destination_code="KIX",
            destination_name="오사카",
            departure_date="2026-12-18",
            return_date="2026-12-21",
            departure_day_of_week="금",
            return_day_of_week="월",
            nights=3,
            airline="티웨이항공",
            price_krw=270000,
            baseline_krw=330000,
            data_source="LIVE_PG",
            freshness="FRESH",
        )
        self.assertIn("가격그래프 최근값", deal_pg.live_payment_note)
        self.assertIn("최근 시세", deal_pg.exact_price_str)
        self.assertNotIn("확정가", deal_pg.live_payment_note)

        # 3. STALE tag
        deal_stale = FlightDeal(
            origin_code="ICN",
            destination_code="KIX",
            destination_name="오사카",
            departure_date="2026-12-18",
            return_date="2026-12-21",
            departure_day_of_week="금",
            return_day_of_week="월",
            nights=3,
            airline="티웨이항공",
            price_krw=270000,
            baseline_krw=330000,
            data_source="LIVE_PG",
            freshness="STALE",
        )
        self.assertTrue(deal_stale.is_stale)
        self.assertIn("캐시", deal_stale.source_badge)

    def test_travel_snapshot_synchronization(self):
        """Single snapshot ensures Telegram and Web report share identical data."""
        snapshot = build_travel_snapshot(self.engine, top_n=3)
        self.assertIsInstance(snapshot, TravelSnapshot)
        self.assertEqual(len(snapshot.recommendations), 3)
        self.assertTrue(snapshot.version_tag.isdigit())

        # Generate HTML from snapshot
        gen = WebReportGenerator(self.engine)
        html = gen.generate_html_report(snapshot=snapshot)
        self.assertIn(snapshot.generated_at, html)
        self.assertIn("숙박 예산: 3성급 가성비 비즈니스 호텔", html)
        self.assertIn("추천 참고 호텔:", html)
        self.assertIn("스폰서 항공사 배너 아래에 있는", html)

        # Generate Telegram message from snapshot
        msg = format_telegram_briefing(
            snapshot.recommendations,
            web_report_url=f"https://example.com/travel_latest.html?v={snapshot.version_tag}",
            engine=self.engine,
            snapshot=snapshot,
        )
        self.assertIn(snapshot.generated_at, msg)
        self.assertIn("숙소 예산:", msg)
        self.assertIn("추천 호텔:", msg)
        self.assertIn(f"?v={snapshot.version_tag}", msg)

    def test_telegram_message_length_guard_tg01(self):
        """Strict compliance with [TG-01] length limits."""
        snapshot = build_travel_snapshot(self.engine, top_n=3)
        msg = format_telegram_briefing(
            snapshot.recommendations,
            web_report_url=f"https://example.com/travel_latest.html?v={snapshot.version_tag}",
            engine=self.engine,
            snapshot=snapshot,
        )
        import re
        visible_text = re.sub(r"<[^>]+>", "", msg)
        self.assertLessEqual(len(visible_text), 1800, f"Visible text too long: {len(visible_text)}")
        self.assertLessEqual(len(msg), 3500, f"HTML text too long: {len(msg)}")


if __name__ == "__main__":
    unittest.main()
