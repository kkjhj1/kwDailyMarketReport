import unittest
from travel_brief.flight_engine import FlightEngine
from travel_brief.web_report import WebReportGenerator


class TravelWebReportTests(unittest.TestCase):
    def setUp(self):
        self.engine = FlightEngine()
        self.generator = WebReportGenerator(self.engine)

    def test_generate_html_report_structure(self):
        html = self.generator.generate_html_report()
        self.assertIsInstance(html, str)
        self.assertGreater(len(html), 10000)

        # 1. Header & Title
        self.assertIn("✈️ 주말 해외여행 6개월 특가 & 예산 나침반", html)
        self.assertIn("Weekend Travel Intelligence Engine", html)

        # 2. Strict Deletion: Source Transparency Card should NOT be in HTML
        self.assertNotIn("source-transparency-card", html)
        self.assertNotIn("항공권 가격 산정 기준 & 실시간 검증 원칙", html)
        self.assertNotIn("VERIFIED BENCHMARK", html)
        # Ensure no isolated </div> immediately after </header>
        self.assertNotIn("</header>\n\n    </div>", html)

        # 3. Action Buttons Verification (4 exact labels with links & modal)
        self.assertIn("✈️ 네이버항공권 ↗", html)
        self.assertIn("🏨 호텔요금 ↗", html)
        self.assertIn("🤖 GPT일정짜기 ↗", html)
        self.assertIn("📋 AI프롬프트실행", html)
        self.assertIn("openPromptModal", html)
        self.assertIn("m-flight.naver.com", html)
        self.assertIn("m-hotel.naver.com", html)
        self.assertIn("chatgpt.com/?q=", html)

        # 4. Top 3 Cards
        self.assertIn("top-cards-grid", html)
        self.assertIn("BEST #1", html)
        self.assertIn("BEST #2", html)
        self.assertIn("BEST #3", html)
        self.assertIn("1인 총 예상 경비", html)
        self.assertIn("2인 1실 쉐어 총합", html)

        # 5. Region Filter Bar & Legend
        self.assertIn("filter-bar", html)
        self.assertIn("setRegionFilter", html)
        self.assertIn("heatmap-legend", html)
        self.assertIn("🔥 초특가", html)

        # 6. Smart Compact Heatmap Matrix Table
        self.assertIn("table-container", html)
        self.assertIn("dest-col-th", html)
        self.assertIn("dest-col", html)
        self.assertIn("matrix-cell", html)
        self.assertIn("cell-price", html)
        self.assertIn("min-width: 760px", html)
        self.assertIn("min-height: 58px", html)

        # 7. High-Readability Monthly Big-Card View & Dual-View Switcher
        self.assertIn("monthlyCardsSection", html)
        self.assertIn("monthly-big-card", html)
        self.assertIn("mbc-price-val", html)
        self.assertIn("setViewMode", html)
        self.assertIn("setMonthTab", html)

        # All 10 destinations in metadata should be present in table
        for code, dest in self.engine.destinations.items():
            self.assertIn(dest.name, html)
            self.assertIn(dest.code, html)

    def test_dynamic_region_filter_and_region_query_param(self):
        from travel_brief.web_report import build_travel_snapshot

        snap = build_travel_snapshot(self.engine, top_n=3, segments={("ALL", "ALL"), ("EU", "ALL"), ("NA", "CITY")})
        html = self.generator.generate_html_report(snapshot=snap)
        # no 10-city hardcoded filter: buttons come from catalog region metadata of this run's cities
        self.assertNotIn('data-filter="japan"', html)
        self.assertNotIn("전체 (10개 도시)", html)
        for region in ("JP", "TWHK", "SEASIA", "PAC", "EU", "NA"):
            self.assertIn(f'data-filter="{region}"', html)
        self.assertIn('data-dest-region="EU"', html)
        self.assertIn("파리", html)  # FALLBACK EU city rendered from catalog metadata
        self.assertIn("5박 이상 권장", html)
        # `?region=` deep link support + 360px / 44px rules
        self.assertIn("URLSearchParams", html)
        self.assertIn("get('region')", html)
        self.assertIn('"SEASIA": "🌴 동남아"', html)
        self.assertIn("min-height: 44px", html)
        self.assertIn("repeat(3, minmax(0, 1fr))", html)
        # segments are filled in memory from one extra pass, with the sparse fallback notice
        self.assertIn("EU:ALL", snap.segment_recs)
        self.assertTrue(snap.segment_recs["EU:ALL"])
        self.assertTrue(all(r.destination.region_code == "EU" for r in snap.segment_recs["EU:ALL"]))
        self.assertTrue(all(r.destination.region_code == "NA" for r in snap.segment_recs["NA:CITY"]))
        self.assertTrue(all(r.best_deal.nights == 5 for r in snap.segment_recs["NA:CITY"]))

    def test_sparse_region_theme_falls_back_with_notice(self):
        from travel_brief.web_report import SPARSE_SEGMENT_NOTICE, build_travel_snapshot

        snap = build_travel_snapshot(self.engine, top_n=3, segments={("EU", "RESORT"), ("ALL", "RESORT")})
        self.assertEqual(snap.segment_notices.get("EU:RESORT"), SPARSE_SEGMENT_NOTICE)
        self.assertTrue(snap.segment_recs["EU:RESORT"])
        self.assertTrue(all(r.destination.region_code == "EU" for r in snap.segment_recs["EU:RESORT"]))
        # ALL region + theme filters the curated universe in memory (no notice needed)
        self.assertNotIn("ALL:RESORT", snap.segment_notices)
        self.assertTrue(all(r.destination.theme in ("resort", "both") for r in snap.segment_recs["ALL:RESORT"]))
        self.assertTrue(all(r.destination.code in self.engine.destinations for r in snap.segment_recs["ALL:RESORT"]))

    def test_save_reports(self):
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmpdir:
            dated_path, latest_path = self.generator.save_reports(Path(tmpdir))
            self.assertTrue(dated_path.exists())
            self.assertTrue(latest_path.exists())
            self.assertEqual(dated_path.read_text(encoding="utf-8"), latest_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
