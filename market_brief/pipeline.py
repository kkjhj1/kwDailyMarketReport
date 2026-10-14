"""End-to-end orchestrator connecting Data Provider, SanityCheck, AlertStore, HTML Report & Gateway."""
from datetime import datetime
from decimal import Decimal
from pathlib import Path
import shutil
from zoneinfo import ZoneInfo

from market_brief.alert_store import AlertStore
from market_brief.calendar_manager import MarketCalendar
from market_brief.core import Briefing, alert_key
from market_brief.gateway import DeliveryGateway
from market_brief.market_data.models import Quote
from market_brief.market_data.naver_provider import NewsItem
from market_brief.market_data.storage import QuoteStore
from market_brief.market_data.validation import SanityChecker, validate_urgent_quote
from market_brief.report_engine import BriefingBuilder
from market_brief.web_report import HTMLReportRenderer, generate_signed_report_url

KST = ZoneInfo("Asia/Seoul")

DEFAULT_SYMBOLS = [
    "KOSPI", "KOSDAQ", "KR3Y", "KR10Y",
    "SP500", "NASDAQ_COMPOSITE", "US10Y", "US2Y",
    "USDKRW", "WTI", "GOLD",
]


class MarketBriefingPipeline:
    def __init__(
        self,
        provider,
        gateway: DeliveryGateway,
        *,
        quote_store: QuoteStore | None = None,
        alert_store: AlertStore | None = None,
        calendar: MarketCalendar | None = None,
        sanity_checker: SanityChecker | None = None,
        report_dir: str | Path = "reports",
        base_report_url: str = "http://localhost:8080",
        report_secret: str = "dev-secret-key",
    ):
        self.provider = provider
        self.gateway = gateway
        self.quote_store = quote_store or QuoteStore()
        self.alert_store = alert_store or AlertStore()
        self.calendar = calendar or MarketCalendar()
        self.sanity_checker = sanity_checker or SanityChecker()
        self.builder = BriefingBuilder()
        self.renderer = HTMLReportRenderer()
        self.report_dir = Path(report_dir)
        self.base_report_url = base_report_url
        self.report_secret = report_secret

    def _save_report_with_latest_aliases(self, briefing: Briefing, quotes: list[Quote],
                                         news: list[NewsItem] | None, report_id: str) -> Path:
        report_path = self.report_dir / f"{report_id}.html"
        self.renderer.render(briefing, quotes, news=news, output_path=report_path)
        # Also copy to latest.html and index.html for GitHub Pages instant access
        shutil.copyfile(report_path, self.report_dir / "latest.html")
        shutil.copyfile(report_path, self.report_dir / "index.html")
        return report_path

    def run_regular_briefing(
        self,
        kind: str,
        recipients: list[str],
        *,
        now: datetime | None = None,
        symbols: list[str] | None = None,
        news: list[NewsItem] | None = None,
    ) -> tuple[Briefing, Path]:
        ref_now = (now or datetime.now(KST)).astimezone(KST)
        target_symbols = symbols or DEFAULT_SYMBOLS
        quotes = self.provider.fetch(target_symbols)
        for q in quotes:
            self.sanity_checker.verify_quote(q)
        self.quote_store.upsert(quotes)

        report_id = f"briefing_{ref_now.strftime('%Y%m%d_%H%M')}_{kind}"
        report_url = generate_signed_report_url(self.base_report_url, report_id, self.report_secret)
        as_of_label = f"{ref_now.strftime('%Y-%m-%d %H:%M')} KST"

        briefing = self.builder.build(
            quotes,
            kind=kind,
            as_of_label=as_of_label,
            report_url=report_url,
            news=news,
        )
        report_path = self._save_report_with_latest_aliases(briefing, quotes, news, report_id)

        for r in recipients:
            self.gateway.dispatch(r, briefing, event_key=f"REGULAR:{report_id}")
        return briefing, report_path

    def check_and_send_urgent_alerts(
        self,
        quotes: list[Quote],
        recipients: list[str],
        *,
        now: datetime,
        required_confirmations: int = 1,
        news: list[NewsItem] | None = None,
    ) -> list[tuple[Quote, Briefing, Path]]:
        triggered: list[tuple[Quote, Briefing, Path]] = []
        for q in quotes:
            if q.market not in {"KR", "US"}:
                continue
            s_info = self.calendar.get_session_info(q.market, now)
            try:
                change_pct = validate_urgent_quote(q, now=now, market_open=s_info.is_open)
            except Exception:
                continue

            if not self.sanity_checker.confirm_urgent_breach(
                q, threshold_pct=Decimal("3.0"), required_confirmations=required_confirmations
            ):
                continue

            if self.alert_store.reserve_if_threshold(
                q.market, q.trade_date, float(change_pct), symbol=q.symbol
            ):
                ev_key = alert_key(q.market, q.trade_date)
                report_id = f"urgent_{q.market}_{q.symbol}_{q.trade_date.isoformat()}"
                report_url = generate_signed_report_url(self.base_report_url, report_id, self.report_secret)
                as_of_label = f"{now.astimezone(KST).strftime('%Y-%m-%d %H:%M')} KST ({s_info.session_label})"
                briefing = self.builder.build(
                    quotes,
                    kind="urgent",
                    as_of_label=as_of_label,
                    report_url=report_url,
                    news=news,
                )
                report_path = self._save_report_with_latest_aliases(briefing, quotes, news, report_id)
                for r in recipients:
                    self.gateway.dispatch(r, briefing, event_key=ev_key)
                triggered.append((q, briefing, report_path))
        return triggered
