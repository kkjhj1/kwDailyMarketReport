"""Safe end-to-end demo (v0.3): generates 5-asset briefing, standalone HTML report, and console dispatch."""
import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from market_brief.alert_store import AlertStore
from market_brief.channels.console import ConsoleChannel
from market_brief.gateway import DeliveryGateway
from market_brief.market_data import FixtureProvider, QuoteStore
from market_brief.market_data.naver_provider import NewsItem
from market_brief.pipeline import MarketBriefingPipeline

ROOT = Path(__file__).resolve().parent
KST = ZoneInfo("Asia/Seoul")

def main():
    provider = FixtureProvider(str(ROOT / "fixtures/market_snapshot.json"))
    raw_news = json.loads((ROOT / "fixtures/market_news.json").read_text(encoding="utf-8"))
    news = [NewsItem(**item) for item in raw_news]

    alert_store = AlertStore(str(ROOT / "alerts_dev.sqlite3"))
    quote_store = QuoteStore(str(ROOT / "market_quotes_dev.sqlite3"))
    gateway = DeliveryGateway(ConsoleChannel(), primary_name="console", alert_store=alert_store)

    pipeline = MarketBriefingPipeline(
        provider=provider,
        gateway=gateway,
        quote_store=quote_store,
        alert_store=alert_store,
        report_dir=ROOT / "reports",
        base_report_url="http://localhost:8080",
    )

    briefing, report_path = pipeline.run_regular_briefing(
        kind="closing",
        recipients=["test-employee"],
        now=datetime(2026, 10, 13, 15, 40, tzinfo=KST),
        news=news,
    )
    print(f"\n[OK] 상세 HTML 리포트 생성 완료: {report_path}")

if __name__ == "__main__":
    main()
