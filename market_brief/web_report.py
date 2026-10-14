"""Generates intuitive, mobile-responsive standalone HTML reports and HMAC signed URLs."""
from datetime import datetime
import hashlib
import hmac
import html
from pathlib import Path
from market_brief.core import Briefing, format_briefing
from market_brief.market_data.models import Quote
from market_brief.market_data.naver_provider import NewsItem
from market_brief.report_engine import ASSET_LABELS, DISPLAY_NAMES, format_movement, deterministic_keyword


def generate_signed_report_url(base_url: str, report_id: str, secret_key: str) -> str:
    sig = hmac.new(secret_key.encode("utf-8"), report_id.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    return f"{base_url.rstrip('/')}/reports/{report_id}.html?sig={sig}"


def verify_signed_report_token(report_id: str, sig: str, secret_key: str) -> bool:
    expected = hmac.new(secret_key.encode("utf-8"), report_id.encode("utf-8"), hashlib.sha256).hexdigest()[:16]
    return hmac.compare_digest(expected, sig or "")


class HTMLReportRenderer:
    """Renders a self-contained, intuitive HTML dashboard file for mobile & PC browsers."""

    def render(
        self,
        briefing: Briefing,
        quotes: list[Quote],
        news: list[NewsItem] | None = None,
        *,
        output_path: str | Path | None = None,
    ) -> str:
        news = news or []
        kind_badge = {
            "morning": ("07:30 장전 브리핑", "#2563eb", "#dbeafe"),
            "closing": ("15:40 장마감 브리핑", "#059669", "#d1fae5"),
            "urgent": ("±3% 급변동 경보", "#dc2626", "#fee2e2"),
        }.get(briefing.kind, ("시황 리포트", "#475569", "#f1f5f9"))

        quotes_map = {q.symbol: q for q in quotes}

        # 5 distinct asset zones
        zones = [
            ("🇰🇷 국내 증시", ["KOSPI", "KOSDAQ"]),
            ("🇺🇸 글로벌 증시", ["SP500", "NASDAQ_COMPOSITE"]),
            ("🏦 국내 채권", ["KR3Y", "KR10Y"]),
            ("🌐 해외 채권", ["US10Y", "US2Y"]),
            ("💵 환율 & 원자재", ["USDKRW", "WTI", "GOLD"]),
        ]

        def _fmt_row(sym: str) -> str:
            if sym not in quotes_map:
                return ""
            q = quotes_map[sym]
            name = html.escape(DISPLAY_NAMES.get(q.symbol, q.symbol))
            mov = format_movement(q)
            is_up = mov.startswith("+") and mov not in {"+0.00%", "+0.0bp"}
            is_down = mov.startswith("-") and mov not in {"-0.00%", "-0.0bp"}
            color = "#dc2626" if is_up else ("#2563eb" if is_down else "#475569")
            bg_pill = "#fef2f2" if is_up else ("#eff6ff" if is_down else "#f1f5f9")

            if q.unit == "PERCENT_YIELD":
                val_str = f"{q.value:.3f}%"
            elif q.unit == "FX":
                val_str = f"{q.value:,.1f}원"
            elif q.unit == "PRICE":
                val_str = f"${q.value:,.2f}"
            else:
                val_str = f"{q.value:,.2f}"

            diff_str = ""
            if q.previous_close is not None:
                d = q.value - q.previous_close
                if q.unit == "INDEX":
                    diff_str = f"({d:+,.2f})"
                elif q.unit == "FX":
                    diff_str = f"({d:+,.1f})"

            diff_display = f"{mov} {diff_str}".strip()

            return f"""
            <div class="asset-row">
              <div class="row-left">
                <span class="symbol-name">{name}</span>
              </div>
              <div class="row-right">
                <span class="price-val">{val_str}</span>
                <span class="pill-chg" style="color:{color};background:{bg_pill};">{html.escape(diff_display)}</span>
              </div>
            </div>
            """

        zones_html = []
        for zone_title, syms in zones:
            rows = [_fmt_row(s) for s in syms if s in quotes_map]
            if rows:
                zones_html.append(f"""
                <div class="zone-card">
                  <div class="zone-header">{html.escape(zone_title)}</div>
                  <div class="zone-body">
                    {''.join(rows)}
                  </div>
                </div>
                """)

        news_html = []
        for n in news[:3]:
            news_html.append(
                f"""
                <div class="news-item">
                  <a href="{html.escape(n.url)}" target="_blank" rel="noopener" class="news-title">{html.escape(n.title)}</a>
                  <div class="news-meta">
                    <span>{html.escape(n.source)}</span>
                    <span>{html.escape(n.published_at[:16].replace('T', ' '))}</span>
                  </div>
                </div>
                """
            )

        summary_html = ""
        if briefing.one_line_summary:
            summary_html = f"""
            <div class="summary-box">
              <div class="summary-label">📌 한 줄 요약</div>
              <div class="summary-text">{html.escape(briefing.one_line_summary)}</div>
            </div>
            """

        doc = f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
  <title>{html.escape(briefing.headline)} - 상세 시황 리포트</title>
  <style>
    :root {{
      --bg: #f8fafc; --card: #ffffff; --text: #0f172a; --muted: #64748b; --border: #e2e8f0;
    }}
    html, body {{
      width: 100%;
      max-width: 100vw;
      margin: 0;
      padding: 0;
      overflow-x: hidden;
      box-sizing: border-box;
      touch-action: pan-y;
      -webkit-text-size-adjust: 100%;
    }}
    *, *::before, *::after {{
      box-sizing: inherit;
    }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Pretendard", "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding: 12px 10px 36px;
      word-break: keep-all;
      overflow-wrap: break-word;
    }}
    .container {{
      width: 100%;
      max-width: 680px;
      margin: 0 auto;
    }}
    .header-card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 14px;
      padding: 16px;
      margin-bottom: 12px;
      box-shadow: 0 1px 2px rgba(0,0,0,0.03);
    }}
    .badge {{
      display: inline-block;
      padding: 3px 10px;
      border-radius: 999px;
      font-size: 12px;
      font-weight: 700;
      color: {kind_badge[1]};
      background: {kind_badge[2]};
      margin-bottom: 8px;
    }}
    h1 {{
      font-size: 18px;
      font-weight: 800;
      margin-bottom: 4px;
      color: #0f172a;
      line-height: 1.35;
    }}
    .as-of {{
      font-size: 12px;
      color: var(--muted);
    }}
    .summary-box {{
      margin-top: 10px;
      padding: 10px 12px;
      background: #f1f5f9;
      border-left: 3px solid #2563eb;
      border-radius: 6px;
    }}
    .summary-label {{
      font-size: 11px;
      font-weight: 700;
      color: #1e40af;
      margin-bottom: 2px;
    }}
    .summary-text {{
      font-size: 13px;
      font-weight: 600;
      color: #1e293b;
      line-height: 1.45;
    }}
    .section-title {{
      font-size: 15px;
      font-weight: 700;
      margin: 16px 0 8px;
      color: #1e293b;
    }}
    .zones-grid {{
      display: flex;
      flex-direction: column;
      gap: 10px;
    }}
    .zone-card {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 12px 14px;
    }}
    .zone-header {{
      font-size: 13px;
      font-weight: 700;
      color: #334155;
      padding-bottom: 8px;
      border-bottom: 1px solid #f1f5f9;
      margin-bottom: 8px;
    }}
    .zone-body {{
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}
    .asset-row {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 13px;
    }}
    .row-left {{
      display: flex;
      align-items: center;
      gap: 6px;
    }}
    .symbol-name {{
      font-weight: 700;
      color: #1e293b;
      font-size: 14px;
    }}
    .row-right {{
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .price-val {{
      font-weight: 700;
      font-size: 14px;
      color: #0f172a;
    }}
    .pill-chg {{
      padding: 2px 7px;
      border-radius: 6px;
      font-weight: 700;
      font-size: 12px;
      white-space: nowrap;
    }}
    .news-list {{
      display: flex;
      flex-direction: column;
      gap: 8px;
    }}
    .news-item {{
      background: var(--card);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 12px;
    }}
    .news-title {{
      font-size: 14px;
      font-weight: 700;
      color: #0f172a;
      text-decoration: none;
      display: block;
      margin-bottom: 4px;
      line-height: 1.4;
    }}
    .news-title:hover {{
      text-decoration: underline;
      color: #2563eb;
    }}
    .news-meta {{
      display: flex;
      justify-content: space-between;
      font-size: 11px;
      color: var(--muted);
    }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header-card">
      <span class="badge">{html.escape(kind_badge[0])}</span>
      <h1>{html.escape(briefing.headline)}</h1>
      <div class="as-of">기준: {html.escape(briefing.as_of)} (전 지표 종가 기준)</div>
      {summary_html}
    </div>

    <div class="section-title">5대 자산군 지표</div>
    <div class="zones-grid">
      {''.join(zones_html)}
    </div>

    <div class="section-title">주요 시황 뉴스</div>
    <div class="news-list">
      {''.join(news_html) if news_html else '<div class="news-item"><p style="font-size:13px;color:#64748b;">등록된 주요 시황 뉴스가 없습니다.</p></div>'}
    </div>
  </div>
</body>
</html>"""
        if output_path is not None:
            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(doc, encoding="utf-8")
        return doc
