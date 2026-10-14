"""Deterministic metric builder + MCP/AI narrative engine with numeric hallucination guard."""
from dataclasses import dataclass
from decimal import Decimal
import re
from market_brief.core import AssetSummary, Briefing
from market_brief.market_data.models import Quote
from market_brief.market_data.naver_provider import NewsItem

ASSET_LABELS = {
    "KR_EQUITY": "국내주식",
    "KR_BOND": "국내채권",
    "US_EQUITY": "해외주식",
    "US_BOND": "해외채권",
    "ALTERNATIVE": "대체자산",
}

DISPLAY_NAMES = {
    "KOSPI": "KOSPI",
    "KOSDAQ": "KOSDAQ",
    "KR3Y": "국고3년",
    "KR10Y": "국고10년",
    "SP500": "S&P500",
    "NASDAQ_COMPOSITE": "NASDAQ",
    "US10Y": "미국채10년",
    "US2Y": "미국채2년",
    "USDKRW": "USD/KRW",
    "WTI": "WTI유가",
    "GOLD": "국제금",
}


def format_movement(q: Quote) -> str:
    if q.unit == "PERCENT_YIELD":
        bp = q.change_bp or Decimal("0")
        return f"{bp:+.1f}bp"
    pct = q.change_pct or Decimal("0")
    return f"{pct:+.2f}%"


def deterministic_keyword(q: Quote) -> str:
    if q.unit == "PERCENT_YIELD":
        bp = q.change_bp or Decimal("0")
        if bp <= Decimal("-3.0"):
            return "금리 하락(채권 강세)"
        if bp >= Decimal("3.0"):
            return "금리 상승(채권 약세)"
        return "금리 보합권"
    pct = q.change_pct or Decimal("0")
    if pct <= Decimal("-3.0"):
        return "급락 경보(±3% 돌파)"
    if pct >= Decimal("3.0"):
        return "급등 경보(±3% 돌파)"
    if pct <= Decimal("-0.5"):
        return "외국인·기관 매물 출회" if q.market == "KR" else "차익실현 약세"
    if pct >= Decimal("0.5"):
        return "위험자산 매수 유입" if q.unit == "INDEX" else "달러/원자재 강세"
    return "보합권 등락"


def build_mcp_context(quotes: list[Quote], news: list[NewsItem], kind: str) -> dict:
    """Constructs structured MCP context combining verified quotes and market news."""
    return {
        "schema_version": "mcp.market_brief.v1",
        "briefing_kind": kind,
        "observations": [
            {
                "symbol": q.symbol,
                "display_name": DISPLAY_NAMES.get(q.symbol, q.symbol),
                "asset_class": q.asset_class,
                "label": ASSET_LABELS.get(q.asset_class, q.asset_class),
                "value": str(q.value),
                "movement": format_movement(q),
                "source": q.source,
                "as_of": q.as_of.isoformat(),
            }
            for q in quotes
        ],
        "news_catalysts": [
            {
                "title": n.title,
                "summary": n.summary,
                "source": n.source,
                "url": n.url,
                "asset_class": n.asset_class,
            }
            for n in news
        ],
    }


def verify_numeric_fidelity(narrative: str, quotes: list[Quote]) -> bool:
    """Checks that any percentage or bp numbers mentioned in AI narrative exist in verified quotes."""
    allowed_numbers = set()
    for q in quotes:
        allowed_numbers.add(f"{q.value:.2f}")
        allowed_numbers.add(str(q.value))
        if q.change_pct is not None:
            allowed_numbers.add(f"{abs(q.change_pct):.2f}")
            allowed_numbers.add(f"{abs(q.change_pct):.1f}")
        if q.change_bp is not None:
            allowed_numbers.add(f"{abs(q.change_bp):.1f}")
            allowed_numbers.add(f"{abs(q.change_bp):.0f}")

    # Find all percentages like 1.25% or 3.0bp in narrative
    matches = re.findall(r"(\d+\.\d+)\s*(?:%|bp)", narrative)
    for m in matches:
        if m not in allowed_numbers:
            return False
    return True


class BriefingBuilder:
    """Builds channel-neutral Briefing and detailed commentary with deterministic fallback."""

    PRIMARY_REPRESENTATIVES = ("KOSPI", "KR3Y", "SP500", "US10Y", "USDKRW")

    def build(
        self,
        quotes: list[Quote],
        *,
        kind: str = "closing",
        as_of_label: str = "2026-10-13 15:40 KST",
        report_url: str = "http://localhost:8080/reports/latest",
        news: list[NewsItem] | None = None,
        ai_headline: str | None = None,
    ) -> Briefing:
        by_symbol = {q.symbol: q for q in quotes}
        summaries: list[AssetSummary] = []

        selected = [by_symbol[s] for s in self.PRIMARY_REPRESENTATIVES if s in by_symbol]
        if not selected:
            selected = quotes[:5]

        for q in selected:
            summaries.append(
                AssetSummary(
                    label=ASSET_LABELS.get(q.asset_class, q.asset_class),
                    indicator=DISPLAY_NAMES.get(q.symbol, q.symbol),
                    change=format_movement(q),
                    keyword=deterministic_keyword(q),
                )
            )

        # Determine headline deterministically unless verified AI headline is provided
        if ai_headline and verify_numeric_fidelity(ai_headline, quotes):
            headline = ai_headline
        else:
            headline = self._deterministic_headline(quotes, kind)

        return Briefing(
            kind=kind,
            headline=headline,
            as_of=as_of_label,
            assets=tuple(summaries),
            report_url=report_url,
        )

    @staticmethod
    def _deterministic_headline(quotes: list[Quote], kind: str) -> str:
        by_sym = {q.symbol: q for q in quotes}
        if kind == "urgent":
            urgent_q = max(
                (q for q in quotes if q.change_pct is not None),
                key=lambda x: abs(x.change_pct or Decimal("0")),
                default=None,
            )
            if urgent_q and urgent_q.change_pct is not None:
                direction = "급등" if urgent_q.change_pct > 0 else "급락"
                return f"[긴급 ±3%] {urgent_q.symbol} {urgent_q.change_pct:+.2f}% {direction} 경보"
        kospi = by_sym.get("KOSPI")
        usdkrw = by_sym.get("USDKRW")
        if kospi and kospi.change_pct is not None and usdkrw and usdkrw.change_pct is not None:
            k_dir = "상승" if kospi.change_pct >= 0 else "약세"
            fx_dir = "상승" if usdkrw.change_pct >= 0 else "하락"
            return f"KOSPI {kospi.change_pct:+.2f}% {k_dir} · 원/달러 {usdkrw.value:,.1f}원({fx_dir})"
        return "글로벌 5대 자산군 정기 시황 브리핑"
