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
