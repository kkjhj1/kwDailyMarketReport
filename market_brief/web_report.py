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

        # 4 distinct asset zones matching Telegram message (All 12 indicators)
        zones = [
            ("■ 🇰🇷 국내 증시 (종가)", ["KOSPI", "KOSDAQ", "KOSPI200"]),
            ("■ 🇺🇸 글로벌 증시 (종가)", ["SP500", "NASDAQ_COMPOSITE"]),
            ("■ 🏦 금리 & 채권 (종가)", ["KR3Y", "KR10Y", "US10Y", "US2Y"]),
            ("■ 💵 환율 & 원자재 (종가/고시)", ["USDKRW", "WTI", "GOLD"]),
        ]

        def _fmt_row(sym: str) -> str:
            if sym not in quotes_map:
                return ""
            q = quotes_map[sym]
            name = html.escape(DISPLAY_NAMES.get(q.symbol, q.symbol))
            mov = format_movement(q)
            is_up = mov.startswith("+") and mov not in {"+0.00%", "+0.0bp"}
            is_down = mov.startswith("-") and mov not in {"-0.00%", "-0.0bp"}
            pill_cls = "up" if is_up else ("down" if is_down else "steady")

            if q.unit == "PERCENT_YIELD":
                val_str = f"{q.value:.3f}%"
                diff_display = mov
            elif q.unit == "FX":
                val_str = f"{q.value:,.1f}원"
                d = (q.value - q.previous_close) if q.previous_close is not None else None
                diff_display = f"{mov} ({d:+,.1f}원)" if d is not None else mov
            elif q.unit == "PRICE":
                val_str = f"${q.value:,.2f}"
                diff_display = mov
            else:  # INDEX
                val_str = f"{q.value:,.2f} pt"
                d = (q.value - q.previous_close) if q.previous_close is not None else None
                diff_display = f"{mov} ({d:+,.2f}pt)" if d is not None else mov

            icon = "🔴" if is_up else ("🔵" if is_down else "⚪")

            return f"""
            <div class="asset-item">
              <div class="asset-row">
                <span class="symbol-name">{icon} {name}</span>
                <div class="row-right">
                  <span class="price-val">{val_str}</span>
                  <span class="pill-chg {pill_cls}">{html.escape(diff_display)}</span>
                </div>
              </div>
            </div>
            """

        zones_html = []
        for zone_title, syms in zones:
            rows = [_fmt_row(s) for s in syms if s in quotes_map]
            if rows:
                zones_html.append(f"""
                <div class="zone-card">
                  <div class="zone-header">
                    <span>{html.escape(zone_title)}</span>
                  </div>
                  <div class="zone-body">
                    {''.join(rows)}
                  </div>
                </div>
                """)

        # 1. Macro Spread Radar Cards (Resilient to partial data)
        us10y = quotes_map.get("US10Y")
        kr10y = quotes_map.get("KR10Y")
        us2y = quotes_map.get("US2Y")
        macro_cards = []

        if us10y and kr10y and us10y.value is not None and kr10y.value is not None:
            spread_10y = (us10y.value - kr10y.value) * 100
            s10_cls = "alert" if spread_10y > 0 else "ok"
            s10_desc = "⚠️ 한·미 금리 역전 지속 (외환 변동성 주의)" if spread_10y > 0 else "🟢 정상 스프레드 (외환 안정권)"
            macro_cards.append(f"""
            <div class="macro-card">
              <div class="macro-title">
                <span>한·미 10년물 금리차</span>
                <span style="font-size:10px;">외환/외인수급</span>
              </div>
              <div class="macro-val">{spread_10y:+.1f} bp</div>
              <span class="macro-desc {s10_cls}">{s10_desc}</span>
            </div>
            """)

        if us10y and us2y and us10y.value is not None and us2y.value is not None:
            spread_2_10 = (us10y.value - us2y.value) * 100
            s2_cls = "ok" if spread_2_10 > 0 else "alert"
            s2_desc = "🟢 정상 수익률 곡선 (연착륙 시그널)" if spread_2_10 > 0 else "🔴 장단기 금리 역전 (경기 침체 주의)"
            macro_cards.append(f"""
            <div class="macro-card">
              <div class="macro-title">
                <span>미국 장단기 금리차 (10Y - 2Y)</span>
                <span style="font-size:10px;">경기 선행 지표</span>
              </div>
              <div class="macro-val">{spread_2_10:+.1f} bp</div>
              <span class="macro-desc {s2_cls}">{s2_desc}</span>
            </div>
            """)

        macro_html = ""
        if macro_cards:
            macro_html = f"""
            <div class="macro-section">
              <div class="section-title">⚡ 매크로 금융 레이더 (스프레드 분석)</div>
              <div class="macro-grid">
                {''.join(macro_cards)}
              </div>
            </div>
            """

        # 2. Rich News Items with 3-line Summary
        news_html = []
        for n in news[:3]:
            title = html.escape(n.title)
            src = html.escape(n.source)
            url = html.escape(n.url)
            pub = html.escape(n.published_at[:16].replace('T', ' '))
            summary_txt = html.escape(getattr(n, "summary", "") or "")
            summary_block = f'<div class="news-summary-txt">{summary_txt}</div>' if summary_txt else ""

            news_html.append(
                f"""
                <div class="news-card">
                  <a href="{url}" target="_blank" rel="noopener" class="news-card-title">{title}</a>
                  {summary_block}
                  <div class="news-meta-row">
                    <div class="news-tags">
                      <span class="tag-badge">#{html.escape(getattr(n, "asset_class", "시황"))}</span>
                    </div>
                    <span>{src} · {pub}</span>
                  </div>
                </div>
                """
            )

        # 3. 3-Point Summary Box (주가, 채권, 기타)
        if briefing.three_line_summary:
            stock_txt = html.escape(briefing.three_line_summary[0])
            bond_txt = html.escape(briefing.three_line_summary[1])
            other_txt = html.escape(briefing.three_line_summary[2])
            summary_html = f"""
            <div class="summary-box">
              <div class="summary-label">📌 오늘의 3줄 핵심 요약</div>
              <ul class="summary-list" id="summaryList">
                <li class="summary-item"><b>주가:</b> {stock_txt}</li>
                <li class="summary-item"><b>채권:</b> {bond_txt}</li>
                <li class="summary-item"><b>기타:</b> {other_txt}</li>
              </ul>
            </div>
            """
        elif briefing.one_line_summary:
            summary_html = f"""
            <div class="summary-box">
              <div class="summary-label">📌 오늘의 핵심 요약</div>
              <div class="summary-item">{html.escape(briefing.one_line_summary)}</div>
            </div>
            """
        else:
            summary_html = ""

        doc = f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
  <title>{html.escape(briefing.headline)} - 상세 시황 리포트</title>
  <style>
    :root {{
      --bg: #f8fafc;
      --card-bg: #ffffff;
      --card-border: #e2e8f0;
      --text-main: #0f172a;
      --text-muted: #64748b;
      --text-sub: #334155;
      --primary: #2563eb;
      --primary-bg: #eff6ff;
      --up-color: #dc2626;
      --up-bg: #fef2f2;
      --down-color: #2563eb;
      --down-bg: #eff6ff;
      --steady-color: #64748b;
      --steady-bg: #f1f5f9;
      --summary-border: #2563eb;
      --summary-bg: #f1f5f9;
      --shadow-sm: 0 1px 2px rgba(0,0,0,0.03);
    }}

    [data-theme="dark"] {{
      --bg: #0b0f19;
      --card-bg: #151d2f;
      --card-border: #223049;
      --text-main: #f1f5f9;
      --text-muted: #94a3b8;
      --text-sub: #cbd5e1;
      --primary: #3b82f6;
      --primary-bg: #1e293b;
      --up-color: #f87171;
      --up-bg: rgba(239, 68, 68, 0.15);
      --down-color: #60a5fa;
      --down-bg: rgba(59, 130, 246, 0.15);
      --steady-color: #94a3b8;
      --steady-bg: #1e293b;
      --summary-border: #3b82f6;
      --summary-bg: #152238;
      --shadow-sm: 0 1px 3px rgba(0,0,0,0.4);
    }}

    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Pretendard", "Segoe UI", Roboto, sans-serif;
      background: var(--bg);
      color: var(--text-main);
      line-height: 1.5;
      padding: 16px 12px 56px;
      transition: background 0.2s ease, color 0.2s ease;
      word-break: keep-all;
    }}
    .container {{
      width: 100%;
      max-width: 680px;
      margin: 0 auto;
    }}

    .top-nav {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 12px;
      padding: 2px 2px;
    }}
    .hist-nav {{
      display: flex;
      align-items: center;
      gap: 6px;
    }}
    .nav-btn {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      color: var(--text-sub);
      padding: 6px 12px;
      border-radius: 8px;
      font-size: 12px;
      font-weight: 600;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      transition: all 0.15s ease;
      text-decoration: none;
    }}
    .nav-btn:hover {{
      border-color: var(--primary);
      color: var(--primary);
    }}
    .curr-date {{
      font-size: 13px;
      font-weight: 700;
      color: var(--text-main);
      padding: 0 4px;
    }}
    .util-actions {{
      display: flex;
      align-items: center;
      gap: 6px;
    }}
    .theme-toggle-btn {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      color: var(--text-sub);
      width: 34px;
      height: 34px;
      border-radius: 8px;
      display: flex;
      align-items: center;
      justify-content: center;
      cursor: pointer;
      font-size: 15px;
      transition: all 0.15s ease;
    }}
    .theme-toggle-btn:hover {{
      border-color: var(--primary);
    }}

    .header-card {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      padding: 18px 18px 16px;
      margin-bottom: 14px;
      box-shadow: var(--shadow-sm);
    }}
    .badge {{
      display: inline-block;
      padding: 3px 10px;
      border-radius: 999px;
      font-size: 11px;
      font-weight: 700;
      color: {kind_badge[1]};
      background: {kind_badge[2]};
      margin-bottom: 8px;
    }}
    h1 {{
      font-size: 18px;
      font-weight: 800;
      margin-bottom: 4px;
      color: var(--text-main);
      line-height: 1.35;
    }}
    .as-of {{
      font-size: 12px;
      color: var(--text-muted);
      margin-bottom: 12px;
    }}
    .summary-box {{
      padding: 12px 14px;
      background: var(--summary-bg);
      border-left: 3px solid var(--summary-border);
      border-radius: 8px;
    }}
    .summary-label {{
      font-size: 11px;
      font-weight: 700;
      color: var(--primary);
      margin-bottom: 6px;
      display: flex;
      align-items: center;
      gap: 4px;
    }}
    .summary-list {{
      list-style: none;
      display: flex;
      flex-direction: column;
      gap: 5px;
    }}
    .summary-item {{
      font-size: 13px;
      font-weight: 500;
      color: var(--text-sub);
      line-height: 1.5;
      display: flex;
      align-items: baseline;
      gap: 6px;
    }}
    .summary-item::before {{
      content: "•";
      color: var(--primary);
      font-size: 15px;
      font-weight: 800;
      line-height: 1;
    }}
    .summary-item b {{
      font-weight: 700;
      color: var(--text-main);
      flex-shrink: 0;
    }}

    .macro-section {{
      margin-bottom: 16px;
    }}
    .macro-grid {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
    }}
    @media (max-width: 480px) {{
      .macro-grid {{ grid-template-columns: 1fr; }}
    }}
    .macro-card {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 12px 14px;
      box-shadow: var(--shadow-sm);
    }}
    .macro-title {{
      font-size: 12px;
      font-weight: 600;
      color: var(--text-muted);
      margin-bottom: 4px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .macro-val {{
      font-size: 18px;
      font-weight: 800;
      color: var(--text-main);
      margin-bottom: 4px;
    }}
    .macro-desc {{
      font-size: 11px;
      font-weight: 600;
      padding: 2px 7px;
      border-radius: 5px;
      display: inline-block;
    }}
    .macro-desc.alert {{
      color: #b91c1c;
      background: #fee2e2;
    }}
    [data-theme="dark"] .macro-desc.alert {{
      color: #f87171;
      background: rgba(239, 68, 68, 0.2);
    }}
    .macro-desc.ok {{
      color: #047857;
      background: #d1fae5;
    }}
    [data-theme="dark"] .macro-desc.ok {{
      color: #34d399;
      background: rgba(16, 185, 129, 0.2);
    }}

    .section-title {{
      font-size: 14px;
      font-weight: 700;
      margin: 18px 0 10px;
      color: var(--text-sub);
      display: flex;
      align-items: center;
      gap: 6px;
    }}
    .zones-grid {{
      display: flex;
      flex-direction: column;
      gap: 12px;
    }}
    .zone-card {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 14px;
      padding: 14px 16px;
      box-shadow: var(--shadow-sm);
    }}
    .zone-header {{
      font-size: 13px;
      font-weight: 700;
      color: var(--text-sub);
      padding-bottom: 8px;
      border-bottom: 1px solid var(--card-border);
      margin-bottom: 12px;
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .zone-body {{
      display: flex;
      flex-direction: column;
      gap: 14px;
    }}
    .asset-item {{
      display: flex;
      flex-direction: column;
      gap: 6px;
    }}
    .asset-row {{
      display: flex;
      justify-content: space-between;
      align-items: center;
    }}
    .symbol-name {{
      font-weight: 700;
      color: var(--text-main);
      font-size: 14px;
      display: flex;
      align-items: center;
      gap: 6px;
    }}
    .row-right {{
      display: flex;
      align-items: center;
      gap: 8px;
    }}
    .price-val {{
      font-weight: 700;
      font-size: 14px;
      color: var(--text-main);
    }}
    .pill-chg {{
      padding: 2px 7px;
      border-radius: 6px;
      font-weight: 700;
      font-size: 12px;
      white-space: nowrap;
    }}
    .pill-chg.up {{ color: var(--up-color); background: var(--up-bg); }}
    .pill-chg.down {{ color: var(--down-color); background: var(--down-bg); }}
    .pill-chg.steady {{ color: var(--steady-color); background: var(--steady-bg); }}

    .news-list {{
      display: flex;
      flex-direction: column;
      gap: 10px;
    }}
    .news-card {{
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 14px;
      box-shadow: var(--shadow-sm);
    }}
    .news-card-title {{
      font-size: 14px;
      font-weight: 700;
      color: var(--text-main);
      text-decoration: none;
      display: block;
      margin-bottom: 6px;
      line-height: 1.4;
    }}
    .news-card-title:hover {{
      color: var(--primary);
    }}
    .news-summary-txt {{
      font-size: 12px;
      color: var(--text-sub);
      line-height: 1.5;
      margin-bottom: 8px;
      background: var(--summary-bg);
      padding: 8px 10px;
      border-radius: 6px;
    }}
    .news-meta-row {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      font-size: 11px;
      color: var(--text-muted);
    }}
    .news-tags {{
      display: flex;
      gap: 4px;
    }}
    .tag-badge {{
      background: var(--primary-bg);
      color: var(--primary);
      padding: 1px 6px;
      border-radius: 4px;
      font-weight: 600;
      font-size: 10px;
    }}

    .footer {{
      text-align: center;
      margin-top: 36px;
      font-size: 11px;
      color: var(--text-muted);
    }}
  </style>
</head>
<body>
  <div class="container">
    <div class="top-nav">
      <div class="hist-nav">
        <span class="curr-date">📅 {html.escape(briefing.as_of[:10])}</span>
      </div>
      <div class="util-actions">
        <button class="nav-btn" onclick="copySummary()" title="요약 복사">📋 요약 복사</button>
        <button class="theme-toggle-btn" onclick="toggleTheme()" id="themeBtn" title="다크/라이트 모드">🌙</button>
      </div>
    </div>

    <div class="header-card">
      <span class="badge">{html.escape(kind_badge[0])}</span>
      <h1>{html.escape(briefing.headline)}</h1>
      <div class="as-of">기준: {html.escape(briefing.as_of)} (전 지표 종가 기준)</div>
      {summary_html}
    </div>

    {macro_html}

    <div class="section-title">📊 5대 자산군 전 지표 종합 (종가 기준)</div>
    <div class="zones-grid">
      {''.join(zones_html)}
    </div>

    <div class="section-title">📰 주요 시황 뉴스 & 변동 요인</div>
    <div class="news-list">
      {''.join(news_html) if news_html else '<div class="news-card"><p style="font-size:13px;color:var(--text-muted);">등록된 주요 시황 뉴스가 없습니다.</p></div>'}
    </div>

    <div class="footer">
      © 2026 Daily Market Brief Bot · Automated Intelligence Pipeline
    </div>
  </div>

  <script>
    function toggleTheme() {{
      const current = document.documentElement.getAttribute('data-theme');
      const target = current === 'dark' ? 'light' : 'dark';
      document.documentElement.setAttribute('data-theme', target);
      document.getElementById('themeBtn').textContent = target === 'dark' ? '☀️' : '🌙';
    }}

    function copySummary() {{
      const listEl = document.getElementById('summaryList');
      if (listEl) {{
        const items = Array.from(listEl.querySelectorAll('.summary-item')).map(el => el.textContent.trim());
        const txt = "📌 [오늘의 3줄 핵심 요약]\\n" + items.map(i => "• " + i).join("\\n");
        navigator.clipboard.writeText(txt).then(() => {{
          alert('3줄 핵심 요약이 클립보드에 복사되었습니다! 팀 채널이나 카톡에 바로 붙여넣기 하세요.');
        }}).catch(() => {{
          alert('복사 실패');
        }});
      }}
    }}
  </script>
</body>
</html>"""
        if output_path is not None:
            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(doc, encoding="utf-8")
        return doc
