"""Unit tests for Travel Brief Web Report Generator.
Verifies HTML structure, data transparency notice, interactive heatmap cells,
and Naver Flight/Hotel live verification link bindings.
"""
from pathlib import Path
import tempfile
import unittest

from travel_brief.flight_engine import FlightEngine
from travel_brief.web_report import WebReportGenerator, DEST_FLAGS, DEST_REGION_TAGS


class TravelWebReportTests(unittest.TestCase):
    def setUp(self):
        self.engine = FlightEngine()
        self.generator = WebReportGenerator(self.engine)

    def test_generate_html_report_structure(self):
        html = self.generator.generate_html_report()
        self.assertIsInstance(html, str)
        self.assertGreater(len(html), 10000)

        # 1. Header & Title
        self.assertIn("✈️ 주말 해외여행 6개월 특가 & 예산 브리핑", html)
        self.assertIn("Weekend Travel Intelligence Engine", html)

        # 2. Data Source & Transparency Verification Card
        self.assertIn("source-transparency-card", html)
        self.assertIn("항공권 가격 산정 기준 & 실시간 검증 원칙", html)
        self.assertIn("국토교통부 항공정보포털", html)
        self.assertIn("100% 원클릭 실시간 최저가 검증", html)

        # 3. Top 3 Cards
        self.assertIn("top-cards-grid", html)
        self.assertIn("BEST #1", html)
        self.assertIn("BEST #2", html)
        self.assertIn("BEST #3", html)
        self.assertIn("1인 총 예상 경비", html)
        self.assertIn("2인 1실 쉐어 총합", html)
        self.assertIn("m-flight.naver.com", html)
        self.assertIn("m-hotel.naver.com", html)

        # 4. Region Filter Bar & Legend
        self.assertIn("filter-bar", html)
        self.assertIn("setRegionFilter", html)
        self.assertIn("heatmap-legend", html)
        self.assertIn("🔥 초특가", html)

        # 5. Sticky Heatmap Table & Interactive Cells
        self.assertIn("table-container", html)
        self.assertIn("dest-col-th", html)
        self.assertIn("dest-col", html)
        self.assertIn("matrix-cell", html)
        self.assertIn("🔍 실시간 확인 ↗", html)

        # All 10 destinations in metadata should be present in table
        for code, dest in self.engine.destinations.items():
            self.assertIn(dest.name, html)
            self.assertIn(code, html)

    def test_save_reports(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            tmp_path = Path(tmp_dir) / "reports"
            dated, latest = self.generator.save_reports(output_dir=tmp_path)
            self.assertTrue(dated.exists())
            self.assertTrue(latest.exists())
            self.assertGreater(dated.stat().st_size, 10000)
            self.assertGreater(latest.stat().st_size, 10000)


if __name__ == "__main__":
    unittest.main()
