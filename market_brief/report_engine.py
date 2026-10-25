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
    "KOSPI200": "KOSPI 200",
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

    PRIMARY_REPRESENTATIVES = ("KOSPI", "KOSDAQ", "KOSPI200", "SP500", "NASDAQ_COMPOSITE", "KR3Y", "KR10Y", "US10Y", "US2Y", "USDKRW", "WTI", "GOLD")

    def build(
        self,
        quotes: list[Quote],
        *,
        kind: str = "closing",
        as_of_label: str = "2026-10-14 15:40 KST",
        report_url: str = "http://localhost:8080/reports/latest",
        news: list[NewsItem] | None = None,
        ai_headline: str | None = None,
    ) -> Briefing:
        by_symbol = {q.symbol: q for q in quotes}
        summaries: list[AssetSummary] = []

        # Maintain consistent ordering based on PRIMARY_REPRESENTATIVES, followed by any others
        ordered_quotes = [by_symbol[s] for s in self.PRIMARY_REPRESENTATIVES if s in by_symbol]
        for q in quotes:
            if q.symbol not in by_symbol:
                ordered_quotes.append(q)

        for q in ordered_quotes:
            cat = "KR_EQUITY"
            if q.symbol in {"SP500", "NASDAQ_COMPOSITE"}:
                cat = "US_EQUITY"
            elif q.symbol in {"KR3Y", "KR10Y", "US10Y", "US2Y"}:
                cat = "BOND"
            elif q.symbol in {"USDKRW", "WTI", "GOLD"}:
                cat = "ALTERNATIVE"

            diff = (q.value - q.previous_close) if q.previous_close is not None else None
            t_date = q.trade_date.strftime("%m/%d") if q.trade_date else ""

            summaries.append(
                AssetSummary(
                    label=ASSET_LABELS.get(q.asset_class, q.asset_class),
                    indicator=DISPLAY_NAMES.get(q.symbol, q.symbol),
                    change=format_movement(q),
                    keyword=deterministic_keyword(q),
                    value=q.value,
                    diff=diff,
                    unit=q.unit,
                    trade_date=t_date,
                    category=cat,
                )
            )

        # Build catchy and informative summaries
        one_line = self._build_one_line_summary(quotes, kind)
        three_lines = self._build_three_line_summary(quotes, kind)

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
            one_line_summary=one_line,
            three_line_summary=three_lines,
            news=tuple(news or []),
        )

    @staticmethod
    def _build_one_line_summary(quotes: list[Quote], kind: str) -> str:
        by_sym = {q.symbol: q for q in quotes}
        kospi = by_sym.get("KOSPI")
        kosdaq = by_sym.get("KOSDAQ")
        nasdaq = by_sym.get("NASDAQ_COMPOSITE")
        usdkrw = by_sym.get("USDKRW")

        if kind == "urgent":
            urgent_q = max(
                (q for q in quotes if q.change_pct is not None),
                key=lambda x: abs(x.change_pct or Decimal("0")),
                default=None,
            )
            if urgent_q and urgent_q.change_pct is not None:
                d_str = "급등" if urgent_q.change_pct > 0 else "급락"
                name = DISPLAY_NAMES.get(urgent_q.symbol, urgent_q.symbol)
                return f"{name} {urgent_q.change_pct:+.2f}% {d_str} 돌파 발생! 실시간 변동성 주의"
            return "장중 주요 지표 ±3% 급변동 경보 발생"

        if kind == "morning":
            if nasdaq and nasdaq.change_pct is not None and usdkrw:
                n_dir = "상승 훈풍" if nasdaq.change_pct >= 0 else "조정 약세"
                fx_str = f"원/달러 {usdkrw.value:,.1f}원"
                return f"미 기술주 {n_dir}(나스닥 {nasdaq.change_pct:+.2f}%) 속 국내 증시 출발 관망 · {fx_str}"
            return "글로벌 증시 마감 흐름과 환율 추이를 주시하는 장전 모닝 시황"

        if kospi and kospi.change_pct is not None and kosdaq and kosdaq.change_pct is not None:
            k_dir = "상승" if kospi.change_pct >= 0 else "하락"
            kq_dir = "상승" if kosdaq.change_pct >= 0 else "약세"
            fx_info = f" · 환율 {usdkrw.value:,.1f}원" if usdkrw else ""
            return f"코스피 {k_dir} 마감({kospi.change_pct:+.2f}%), 코스닥 {kq_dir}({kosdaq.change_pct:+.2f}%){fx_info}"

        return "국내외 증시 및 주요 금융지표 일일 마감 시황"

    @staticmethod
    def _build_three_line_summary(quotes: list[Quote], kind: str) -> tuple[str, str, str]:
        by_sym = {q.symbol: q for q in quotes}
        kospi = by_sym.get("KOSPI")
        kosdaq = by_sym.get("KOSDAQ")
        nasdaq = by_sym.get("NASDAQ_COMPOSITE")
        kr10y = by_sym.get("KR10Y")
        kr3y = by_sym.get("KR3Y")
        us10y = by_sym.get("US10Y")
        usdkrw = by_sym.get("USDKRW")
        wti = by_sym.get("WTI")
        gold = by_sym.get("GOLD")

        # 1. 주가 (국내 및 해외 주식)
        stock_parts = []
        if kospi and kospi.change_pct is not None:
            k_dir = "상승" if kospi.change_pct >= 0 else "하락"
            stock_parts.append(f"코스피 {k_dir}({kospi.change_pct:+.2f}%)")
        if kosdaq and kosdaq.change_pct is not None:
            kq_dir = "상승" if kosdaq.change_pct >= 0 else "약세"
            stock_parts.append(f"코스닥 {kq_dir}({kosdaq.change_pct:+.2f}%)")
        if nasdaq and nasdaq.change_pct is not None:
            n_dir = "상승" if nasdaq.change_pct >= 0 else "숨고르기"
            stock_parts.append(f"미 나스닥 {n_dir}({nasdaq.change_pct:+.2f}%)")
        stock_line = ", ".join(stock_parts) if stock_parts else "국내외 증시 등락 혼조세"

        # 2. 채권 (국내 및 해외 금리)
        bond_parts = []
        if kr10y and kr10y.value is not None:
            chg = f"({kr10y.change_bp:+.1f}bp)" if kr10y.change_bp is not None else ""
            bond_parts.append(f"국고10년 {kr10y.value:.3f}%{chg}")
        if us10y and us10y.value is not None:
            chg = f"({us10y.change_bp:+.1f}bp)" if us10y.change_bp is not None else ""
            bond_parts.append(f"미10년 {us10y.value:.3f}%{chg}")
        if us10y and kr10y and us10y.value is not None and kr10y.value is not None:
            diff_bp = (us10y.value - kr10y.value) * 100
            diff_str = "역전 지속" if diff_bp > 0 else "정상 스프레드"
            bond_parts.append(f"한·미 10년물 금리차 {diff_bp:+.1f}bp {diff_str}")
        bond_line = " · ".join(bond_parts) if bond_parts else "주요국 국채 금리 보합권"

        # 3. 기타 (환율 및 원자재)
        other_parts = []
        if usdkrw and usdkrw.value is not None:
            chg = f"({usdkrw.change_pct:+.2f}%)" if usdkrw.change_pct is not None else ""
            other_parts.append(f"원/달러 {usdkrw.value:,.1f}원{chg}")
        if wti and wti.value is not None:
            chg = f"({wti.change_pct:+.2f}%)" if wti.change_pct is not None else ""
            other_parts.append(f"WTI유가 ${wti.value:,.2f}{chg}")
        if gold and gold.value is not None:
            chg = f"({gold.change_pct:+.2f}%)" if gold.change_pct is not None else ""
            other_parts.append(f"국제금 ${gold.value:,.2f}{chg}")
        other_line = " · ".join(other_parts) if other_parts else "환율 및 주요 원자재 등락"

        return (stock_line, bond_line, other_line)

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
