"""Content parsers, special-session calendar, and Telegram size guard tests."""

import io
import json
import unittest
from datetime import date, datetime, time
from decimal import Decimal
from urllib.error import HTTPError
from zoneinfo import ZoneInfo

from market_brief.calendar_manager import MarketCalendar
from market_brief.channels.telegram import format_telegram_html, telegram_visible_len
from market_brief.core import AssetSummary, Briefing
from market_brief.market_data.naver_provider import (
    NewsItem,
    fetch_naver_investor_trend,
    fetch_naver_leading_sectors,
)

KST = ZoneInfo("Asia/Seoul")


class _JsonResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _opener_for(payload):
    def opener(_request, timeout=5):
        del timeout
        return _JsonResponse(json.dumps(payload).encode("utf-8"))

    return opener


class ContentAndCalendarTests(unittest.TestCase):
    def setUp(self):
        self.calendar = MarketCalendar()

    def test_kr_holidays_and_special_session_windows(self):
        self.assertFalse(self.calendar.is_trading_day("KR", date(2026, 12, 31)))
        self.assertEqual(
            self.calendar.get_kr_session_window(date(2026, 11, 19)),
            (time(10, 0), time(16, 30)),
        )
        self.assertEqual(
            self.calendar.get_kr_session_window(date(2026, 1, 2)),
            (time(10, 0), time(15, 30)),
        )

    def test_csat_session_status_and_closing_briefing_window(self):
        csat = date(2026, 11, 19)
        for clock in (time(10, 5), time(16, 25)):
            info = self.calendar.get_session_info("KR", datetime.combine(csat, clock, tzinfo=KST))
            self.assertTrue(info.is_open)
        for clock in (time(9, 30), time(16, 35)):
            info = self.calendar.get_session_info("KR", datetime.combine(csat, clock, tzinfo=KST))
            self.assertFalse(info.is_open)

        self.assertEqual(
            self.calendar.get_regular_briefing_window("closing", csat),
            (time(16, 40), time(16, 55), time(19, 0)),
        )
        self.assertFalse(
            self.calendar.should_run_regular_briefing(
                "closing", datetime(2026, 11, 19, 15, 42, tzinfo=KST)
            )
        )
        self.assertTrue(
            self.calendar.should_run_regular_briefing(
                "closing", datetime(2026, 11, 19, 16, 42, tzinfo=KST)
            )
        )

    def test_investor_trend_parser_is_fail_safe(self):
        payload = {
            "bizdate": "20261119",
            "personalValue": "-12,345",
            "foreignValue": "8,765억",
            "institutionalValue": "3,580",
        }
        trend = fetch_naver_investor_trend(opener=_opener_for(payload), trade_date=date(2026, 11, 19))
        self.assertIsNotNone(trend)
        self.assertEqual(trend.personal, "-12,345억")
        self.assertEqual(trend.foreign, "+8,765억")
        self.assertEqual(trend.institutional, "+3,580억")
        self.assertIsNone(
            fetch_naver_investor_trend(
                opener=_opener_for(payload), trade_date=date(2026, 11, 20)
            )
        )

        for malformed in ({}, [], "", {"bizdate": "bad"}):
            self.assertIsNone(fetch_naver_investor_trend(opener=_opener_for(malformed)))
        self.assertIsNone(fetch_naver_investor_trend(opener=lambda *_args, **_kwargs: _JsonResponse(b"")))

        def non_200(*_args, **_kwargs):
            raise HTTPError("https://m.stock.naver.com", 503, "unavailable", {}, None)

        self.assertIsNone(fetch_naver_investor_trend(opener=non_200))

    def test_leading_sector_parser_returns_top_positive_three(self):
        payload = {
            "groups": [
                {"name": "반도체", "changeRate": "4.20"},
                {"name": "바이오", "changeRate": "1.10%"},
                {"name": "자동차", "changeRate": "3.30"},
                {"name": "은행", "changeRate": "-2.00"},
                {"name": "조선", "changeRate": "0.50"},
            ]
        }
        sectors = fetch_naver_leading_sectors(opener=_opener_for(payload))
        self.assertEqual([sector.name for sector in sectors], ["반도체", "자동차", "바이오"])
        self.assertEqual([sector.change_rate for sector in sectors], ["+4.20%", "+3.30%", "+1.10%"])

        self.assertEqual(fetch_naver_leading_sectors(opener=_opener_for({})), [])
        self.assertEqual(
            fetch_naver_leading_sectors(
                opener=_opener_for({"groups": [{"name": "하락", "changeRate": "-1.0"}]})
            ),
            [],
        )

        def broken(*_args, **_kwargs):
            raise OSError("offline")

        self.assertEqual(fetch_naver_leading_sectors(opener=broken), [])

    def test_economic_event_labels(self):
        fomc = self.calendar.get_economic_events(date(2026, 1, 29))
        csat = self.calendar.get_economic_events(date(2026, 11, 19))
        self.assertTrue(any("FOMC" in label for label in fomc))
        self.assertTrue(any("대학수학능력시험" in label for label in csat))
        self.assertEqual(self.calendar.get_economic_events(date(2026, 1, 30)), [])

    def test_telegram_html_guard_with_long_content(self):
        assets = tuple(
            AssetSummary(
                "국내주식",
                "매우 긴 지표 이름 " + "지표" * 20,
                "+1.23%",
                "상승",
                value=Decimal("1234.56"),
                diff=Decimal("12.34"),
                unit="INDEX",
            )
            for _ in range(12)
        )
        long_news = tuple(
            NewsItem(
                title="긴 뉴스 제목 " + "제목" * 500,
                summary="긴 뉴스 요약 " + "요약" * 500,
                source="출처" * 100,
                url="https://example.com/news/1",
                published_at="2026-11-19T16:40:00+09:00",
            )
            for _ in range(5)
        )
        briefing = Briefing(
            kind="closing",
            headline="긴 헤드라인 " + "헤드라인" * 500,
            as_of="2026-11-19 16:42 KST",
            assets=assets,
            report_url="https://example.com/reports/closing",
            one_line_summary="긴 요약 " + "요약" * 1000,
            news=long_news,
        )
        rendered = format_telegram_html(briefing)
        self.assertLessEqual(telegram_visible_len(rendered), 1800)
        self.assertLessEqual(len(rendered), 3500)


if __name__ == "__main__":
    unittest.main()
