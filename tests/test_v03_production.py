import io
import json
import tempfile
import unittest
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

from market_brief.alert_store import AlertStore
from market_brief.calendar_manager import MarketCalendar
from market_brief.channels.console import ConsoleChannel
from market_brief.channels.kakao import KakaoAlimtalkChannel
from market_brief.channels.telegram import TelegramChannel
from market_brief.gateway import DeliveryGateway
from market_brief.market_data import (
    CompositeMarketDataProvider,
    DataValidationError,
    FixtureProvider,
    NaverFinanceProvider,
    QuoteStore,
    SanityChecker,
)
from market_brief.market_data.models import Quote
from market_brief.market_data.naver_provider import NewsItem
from market_brief.pipeline import MarketBriefingPipeline
from market_brief.report_engine import verify_numeric_fidelity
from market_brief.web_report import verify_signed_report_token, generate_signed_report_url

ROOT = Path(__file__).resolve().parents[1]
KST = ZoneInfo("Asia/Seoul")


class FakeHTTPResponse(io.BytesIO):
    def __enter__(self):
        return self
    def __exit__(self, *_):
        self.close()


class FailingChannel:
    def __init__(self):
        self.calls = 0
    def send(self, recipient, briefing):
        self.calls += 1
        raise RuntimeError("simulated network timeout")


class ProductionV03Tests(unittest.TestCase):
    def test_naver_finance_provider_mocked_parsing(self):
        payloads = {
            "KOSPI": {
                "closePrice": "3,025.50",
                "compareToPreviousClosePrice": "24.50",
                "compareToPreviousPrice": {"code": "5"},
                "localTradedAt": "2026-10-13T15:30:00+09:00",
            },
            "KOSPI200": {
                "closePrice": "405.20",
                "compareToPreviousClosePrice": "2.80",
                "compareToPreviousPrice": {"code": "5"},
                "localTradedAt": "2026-10-13T15:30:00+09:00",
            },
            "USDKRW": {
                "result": {
                    "calcPrice": "1,350.00",
                    "previousClose": "1,342.00",
                    "localTradedAt": "2026-10-13T15:30:00+09:00",
                }
            },
        }
        def fake_opener(req, timeout=8):
            if "KPI200" in req.full_url:
                return FakeHTTPResponse(json.dumps(payloads["KOSPI200"]).encode("utf-8"))
            if "KOSPI" in req.full_url:
                return FakeHTTPResponse(json.dumps(payloads["KOSPI"]).encode("utf-8"))
            return FakeHTTPResponse(json.dumps(payloads["USDKRW"]).encode("utf-8"))

        provider = NaverFinanceProvider(opener=fake_opener)
        quotes = provider.fetch(["KOSPI", "KOSPI200", "USDKRW"], now=datetime(2026, 10, 13, 15, 35, tzinfo=KST))
        self.assertEqual(len(quotes), 3)
        self.assertEqual(quotes[0].symbol, "KOSPI")
        self.assertEqual(quotes[0].value, Decimal("3025.50"))
        self.assertEqual(quotes[0].previous_close, Decimal("3050.00"))
        self.assertTrue(quotes[0].change_pct < 0)
        self.assertEqual(quotes[1].symbol, "KOSPI200")
        self.assertEqual(quotes[1].value, Decimal("405.20"))
        self.assertEqual(quotes[1].previous_close, Decimal("408.00"))
        self.assertTrue(quotes[1].change_pct < 0)
        self.assertEqual(quotes[2].symbol, "USDKRW")
        self.assertEqual(quotes[2].value, Decimal("1350.00"))

    def test_composite_provider_fallback_on_error(self):
        def broken_opener(req, timeout=8):
            raise OSError("Naver blocked or offline")
        primary = NaverFinanceProvider(opener=broken_opener)
        fallback = FixtureProvider(str(ROOT / "fixtures/market_snapshot.json"))
        comp = CompositeMarketDataProvider(primary, fallback)
        quotes = comp.fetch(["KOSPI", "USDKRW"])
        self.assertEqual(comp.last_used_source, "FALLBACK")
        self.assertEqual(len(quotes), 2)

    def test_sanity_checker_blocks_single_tick_spike_and_confirms_breach(self):
        checker = SanityChecker(max_daily_move_pct=Decimal("20.0"), max_tick_jump_pct=Decimal("8.0"))
        now = datetime(2026, 10, 13, 10, 0, tzinfo=KST)
        q1 = Quote(
            symbol="KOSPI", asset_class="KR_EQUITY", market="KR", session="REGULAR",
            trade_date=date(2026, 10, 13), as_of=now,
            value=Decimal("2950.00"), previous_close=Decimal("3050.00"),
            unit="INDEX", source="TEST",
        )
        # First observation (-3.28%): streak = 1 -> not yet confirmed if required_confirmations=2
        self.assertFalse(checker.confirm_urgent_breach(q1, required_confirmations=2))
        # Second consecutive observation (-3.31%): streak = 2 -> confirmed!
        q2 = Quote(
            symbol="KOSPI", asset_class="KR_EQUITY", market="KR", session="REGULAR",
            trade_date=date(2026, 10, 13), as_of=now,
            value=Decimal("2949.00"), previous_close=Decimal("3050.00"),
            unit="INDEX", source="TEST",
        )
        self.assertTrue(checker.confirm_urgent_breach(q2, required_confirmations=2))

        # Corrupt single-tick spike (+15% jump from 2949) -> rejected
        q_spike = Quote(
            symbol="KOSPI", asset_class="KR_EQUITY", market="KR", session="REGULAR",
            trade_date=date(2026, 10, 13), as_of=now,
            value=Decimal("3450.00"), previous_close=Decimal("3050.00"),
            unit="INDEX", source="TEST",
        )
        with self.assertRaises(DataValidationError):
            checker.verify_quote(q_spike)

    def test_market_calendar_us_dst_and_krx_sessions(self):
        cal = MarketCalendar()
        # Summer (EDT, UTC-4): 2026-10-13 23:00 KST is 10:00 EDT -> US market OPEN
        summer_kst = datetime(2026, 10, 13, 23, 0, tzinfo=KST)
        us_summer = cal.get_session_info("US", summer_kst)
        self.assertTrue(us_summer.is_dst)
        self.assertTrue(us_summer.is_open)

        # Winter (EST, UTC-5): 2026-11-10 23:00 KST is 09:00 EST -> US market NOT YET OPEN (opens 23:30 KST)
        winter_kst = datetime(2026, 11, 10, 23, 0, tzinfo=KST)
        us_winter = cal.get_session_info("US", winter_kst)
        self.assertFalse(us_winter.is_dst)
        self.assertFalse(us_winter.is_open)

    def test_gateway_retry_and_fallback_with_audit_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = AlertStore(str(Path(tmp) / "alerts.db"))
            failing = FailingChannel()
            fallback = ConsoleChannel()
            gw = DeliveryGateway(
                failing, primary_name="telegram",
                fallback_channel=fallback, fallback_name="console",
                alert_store=store, max_retries=3, backoff_seconds=0.0,
            )
            provider = FixtureProvider(str(ROOT / "fixtures/market_snapshot.json"))
            pipeline = MarketBriefingPipeline(
                provider=provider, gateway=gw,
                quote_store=QuoteStore(str(Path(tmp) / "quotes.db")),
                alert_store=store, report_dir=Path(tmp) / "reports",
            )
            briefing, report_path = pipeline.run_regular_briefing(
                "closing", ["emp-01"], now=datetime(2026, 10, 13, 15, 40, tzinfo=KST)
            )
            self.assertEqual(failing.calls, 3)
            self.assertTrue(report_path.exists())
            logs = store.list_delivery_logs()
            self.assertEqual(len(logs), 2)
            self.assertEqual(logs[0]["status"], "FAILED")
            self.assertEqual(logs[0]["attempts"], 3)
            self.assertEqual(logs[1]["status"], "SENT_FALLBACK")

    def test_numeric_fidelity_and_signed_report_url(self):
        quotes = FixtureProvider(str(ROOT / "fixtures/market_snapshot.json")).fetch(["KOSPI", "KR3Y"])
        # KOSPI change is -0.80%, KR3Y is -3.0bp
        self.assertTrue(verify_numeric_fidelity("KOSPI -0.80% 하락 및 국고3년 3.0bp 하락", quotes))
        self.assertFalse(verify_numeric_fidelity("KOSPI +9.99% 폭등 (환각 수치)", quotes))

        url = generate_signed_report_url("https://brief.company.internal", "rep_01", "secret123")
        sig = url.split("sig=")[1]
        self.assertTrue(verify_signed_report_token("rep_01", sig, "secret123"))
        self.assertFalse(verify_signed_report_token("rep_01", "wrongsig", "secret123"))



    def test_telegram_discover_chat_ids_and_inline_button(self):
        sent_payloads = []
        def fake_opener(req, timeout=10):
            url = req.full_url
            if "getMe" in url:
                return FakeHTTPResponse(b'{"ok":true,"result":{"id":123,"username":"market_brief_bot","first_name":"MarketBot"}}')
            if "getUpdates" in url:
                return FakeHTTPResponse(json.dumps({
                    "ok": True,
                    "result": [
                        {"update_id": 1, "message": {"chat": {"id": 987654321, "type": "private", "first_name": "홍길동", "username": "hong"}, "text": "/start"}}
                    ]
                }).encode("utf-8"))
            if "sendMessage" in url:
                sent_payloads.append(json.loads(req.data.decode("utf-8")))
                return FakeHTTPResponse(b'{"ok":true,"result":{"message_id":555}}')
            raise AssertionError("Unexpected URL: " + url)

        tg = TelegramChannel("123456:ABC-DEF", include_inline_button=True, opener=fake_opener)
        me = tg.get_me()
        self.assertEqual(me["username"], "market_brief_bot")
        chats = tg.discover_chat_ids()
        self.assertEqual(len(chats), 1)
        self.assertEqual(chats[0]["chat_id"], "987654321")

        from market_brief.core import AssetSummary, Briefing
        b = Briefing("closing", "테스트 헤드라인", "15:40 KST", (AssetSummary("국내주식", "KOSPI", "-0.8%", "약세"),), "https://example.com/r1")
        msg_id = tg.send("987654321", b)
        self.assertEqual(msg_id, "555")
        self.assertIn("reply_markup", sent_payloads[0])



    def test_telegram_localhost_url_omits_inline_button_to_prevent_http_400(self):
        sent_payloads = []
        def fake_opener(req, timeout=10):
            sent_payloads.append(json.loads(req.data.decode("utf-8")))
            return FakeHTTPResponse(b'{"ok":true,"result":{"message_id":777}}')

        tg = TelegramChannel("123456:ABC-DEF", include_inline_button=True, opener=fake_opener)
        from market_brief.core import AssetSummary, Briefing
        b_local = Briefing("closing", "테스트", "15:40 KST", (AssetSummary("국내주식", "KOSPI", "-0.8%", "약세"),), "http://localhost:8080/reports/r1.html")
        tg.send("987654321", b_local)
        # localhost must NOT include reply_markup because Telegram rejects localhost in inline_keyboard!
        self.assertNotIn("reply_markup", sent_payloads[0])



    def test_telegram_pretty_html_format_and_clickable_hyperlink(self):
        from market_brief.channels.telegram import format_telegram_html
        from market_brief.core import AssetSummary, Briefing
        b = Briefing(
            "closing",
            "KOSPI -0.80% 약세 · 원/달러 1,350.0원(상승)",
            "2026-10-13 15:40 KST",
            (AssetSummary("국내주식", "KOSPI", "-0.80%", "외국인 매도"), AssetSummary("대체자산", "USD/KRW", "+0.60%", "달러 강세")),
            "https://myuser.github.io/market-daily-briefing/reports/briefing_20261013_1540_closing.html?sig=abc",
        )
        rendered = format_telegram_html(b)
        self.assertIn("<b>[15:40 장마감 시황 브리핑]</b>", rendered)
        self.assertIn("🔵 KOSPI", rendered)
        self.assertIn("🔴 USD/KRW", rendered)
        self.assertIn('<a href="https://myuser.github.io/market-daily-briefing/reports/briefing_20261013_1540_closing.html?sig=abc">', rendered)

    def test_three_line_summary_and_macro_radar_rendering(self):
        from market_brief.report_engine import BriefingBuilder
        from market_brief.web_report import HTMLReportRenderer
        provider = FixtureProvider(str(ROOT / "fixtures/market_snapshot.json"))
        from market_brief.pipeline import DEFAULT_SYMBOLS
        quotes = provider.fetch(DEFAULT_SYMBOLS)

        builder = BriefingBuilder()
        briefing = builder.build(quotes, kind="closing", as_of_label="2026-10-13 15:40 KST")

        # 1. Check three_line_summary generation
        self.assertIsNotNone(briefing.three_line_summary)
        stock_line, bond_line, other_line = briefing.three_line_summary
        self.assertIn("코스피", stock_line)
        self.assertIn("국고10년", bond_line)
        self.assertIn("원/달러", other_line)

        # 2. Check HTML rendering with all 12 assets, radar, and 3-point summary
        renderer = HTMLReportRenderer()
        html_out = renderer.render(briefing, quotes)
        self.assertIn("📌 오늘의 3줄 핵심 요약", html_out)
        self.assertIn("<b>주가:</b>", html_out)
        self.assertIn("<b>채권:</b>", html_out)
        self.assertIn("<b>기타:</b>", html_out)
        self.assertIn("⚡ 매크로 금융 레이더", html_out)
        self.assertIn("한·미 10년물 금리차", html_out)
        self.assertIn("KOSPI 200", html_out)
        self.assertIn("WTI유가", html_out)
        self.assertIn("국제금", html_out)

    def test_resilient_macro_radar_with_partial_quotes(self):
        from market_brief.report_engine import BriefingBuilder
        from market_brief.web_report import HTMLReportRenderer
        provider = FixtureProvider(str(ROOT / "fixtures/market_snapshot.json"))
        # Partial quotes: only KOSPI and US10Y (no KR10Y, no US2Y)
        quotes = provider.fetch(["KOSPI", "US10Y"])
        builder = BriefingBuilder()
        briefing = builder.build(quotes, kind="closing")
        renderer = HTMLReportRenderer()
        html_out = renderer.render(briefing, quotes)
        # Should render cleanly without throwing exception
        self.assertIn("KOSPI", html_out)
        self.assertNotIn("⚡ 매크로 금융 레이더", html_out)  # Gracefully omitted when missing pair


if __name__ == "__main__":
    unittest.main()
