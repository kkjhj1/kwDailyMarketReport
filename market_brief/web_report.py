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
            "closing": ("15:40 마감 브리핑", "#059669", "#d1fae5"),
            "urgent": ("±3% 급변동 긴급경보", "#dc2626", "#fee2e2"),
        }.get(briefing.kind, ("시황 리포트", "#475569", "#f1f5f9"))

        cards_html = []
        for q in quotes:
            mov = format_movement(q)
            is_up = mov.startswith("+") and mov not in {"+0.00%", "+0.0bp"}
            is_down = mov.startswith("-") and mov not in {"-0.00%", "-0.0bp"}
            color = "#dc2626" if is_up else ("#2563eb" if is_down else "#475569")
            bg_pill = "#fef2f2" if is_up else ("#eff6ff" if is_down else "#f8fafc")
            label = html.escape(ASSET_LABELS.get(q.asset_class, q.asset_class))
            name = html.escape(DISPLAY_NAMES.get(q.symbol, q.symbol))
            val_str = f"{q.value:,.2f}" if q.unit != "PERCENT_YIELD" else f"{q.value:.3f}%"
            prev_str = (
                f"{q.previous_close:,.2f}"
                if (q.previous_close is not None and q.unit != "PERCENT_YIELD")
                else (f"{q.previous_close:.3f}%" if q.previous_close is not None else "-")
            )
            kw = html.escape(deterministic_keyword(q))
            cards_html.append(
                f"""
                <div class="quote-card">
                  <div class="quote-top">
                    <span class="asset-tag">{label}</span>
                    <span class="source-tag">{html.escape(q.source)}</span>
                  </div>
                  <div class="quote-main">
                    <div>
                      <div class="symbol-title">{name} <small>({html.escape(q.symbol)})</small></div>
                      <div class="price-val">{val_str}</div>
                    </div>
                    <div class="change-pill" style="color:{color};background:{bg_pill};">{html.escape(mov)}</div>
                  </div>
                  <div class="quote-footer">
                    <span>전일종가: {prev_str}</span>
                    <span class="kw-text">{kw}</span>
                  </div>
                </div>
                """
            )

        news_html = []
        for n in news:
            news_html.append(
                f"""
                <div class="news-item">
                  <div class="news-meta">
                    <span class="asset-tag">{html.escape(ASSET_LABELS.get(n.asset_class, n.asset_class))}</span>
                    <span>{html.escape(n.source)} · {html.escape(n.published_at[:16].replace('T', ' '))}</span>
                  </div>
                  <a href="{html.escape(n.url)}" target="_blank" rel="noopener" class="news-title">{html.escape(n.title)}</a>
                  <p class="news-summary">{html.escape(n.summary)}</p>
                </div>
                """
            )

        msg_preview = html.escape(format_briefing(briefing))
        doc = f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{html.escape(briefing.headline)} - 상세 시황 리포트</title>
  <style>
    :root {{
      --bg: #f8fafc; --card: #ffffff; --text: #0f172a; --muted: #64748b; --border: #e2e8f0;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Pretendard", "Noto Sans KR", sans-serif; background: var(--bg); color: var(--text); line-height: 1.5; padding: 20px 14px 48px; }}
    .container {{ max-width: 920px; margin: 0 auto; }}
    .header-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 16px; padding: 24px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.04); }}
    .badge {{ display: inline-block; padding: 4px 12px; border-radius: 999px; font-size: 13px; font-weight: 700; color: {kind_badge[1]}; background: {kind_badge[2]}; margin-bottom: 10px; }}
    h1 {{ font-size: 22px; font-weight: 800; margin-bottom: 6px; color: #0f172a; }}
    .as-of {{ font-size: 13px; color: var(--muted); }}
    .section-title {{ font-size: 16px; font-weight: 700; margin: 24px 0 12px; color: #1e293b; display: flex; align-items: center; justify-content: space-between; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(275px, 1fr)); gap: 14px; }}
    .quote-card {{ background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 16px; display: flex; flex-direction: column; gap: 10px; }}
    .quote-top {{ display: flex; justify-content: space-between; align-items: center; font-size: 12px; }}
    .asset-tag {{ background: #f1f5f9; color: #334155; padding: 2px 8px; border-radius: 6px; font-weight: 600; font-size: 11px; }}
    .source-tag {{ color: var(--muted); font-size: 11px; }}
    .quote-main {{ display: flex; justify-content: space-between; align-items: baseline; }}
    .symbol-title {{ font-size: 14px; font-weight: 700; color: #334155; }}
    .symbol-title small {{ font-weight: 400; color: var(--muted); }}
    .price-val {{ font-size: 22px; font-weight: 800; letter-spacing: -0.02em; }}
    .change-pill {{ padding: 4px 10px; border-radius: 8px; font-weight: 700; font-size: 14px; }}
    .quote-footer {{ display: flex; justify-content: space-between; font-size: 12px; color: var(--muted); border-top: 1px solid #f1f5f9; padding-top: 8px; }}
    .kw-text {{ font-weight: 600; color: #475569; }}
    .news-list {{ display: flex; flex-direction: column; gap: 12px; }}
    .news-item {{ background: var(--card); border: 1px solid var(--border); border-radius: 14px; padding: 16px; }}
    .news-meta {{ display: flex; justify-content: space-between; font-size: 12px; color: var(--muted); margin-bottom: 6px; }}
    .news-title {{ font-size: 15px; font-weight: 700; color: #0f172a; text-decoration: none; display: block; margin-bottom: 4px; }}
    .news-title:hover {{ text-decoration: underline; color: #2563eb; }}
    .news-summary {{ font-size: 13px; color: #475569; }}
    .msg-box {{ background: #0f172a; color: #f8fafc; border-radius: 14px; padding: 18px; font-family: monospace; font-size: 13px; white-space: pre-wrap; line-height: 1.6; }}
  </style>
</head>
<body>
  <div class="container">
    <div class="header-card">
      <span class="badge">{html.escape(kind_badge[0])}</span>
      <h1>{html.escape(briefing.headline)}</h1>
      <div class="as-of">기준 시각: {html.escape(briefing.as_of)} · 채널 독립형 리포트 엔진</div>
    </div>

    <div class="section-title">
      <span>5대 자산군 핵심 지표 스냅샷</span>
      <span style="font-size:12px;color:#64748b;">한국식 표기: 상승(적색) / 하락(청색)</span>
    </div>
    <div class="grid">
      {''.join(cards_html)}
    </div>

    <div class="section-title"><span>주요 시황 뉴스 및 변동 요인 (MCP 컨텍스트)</span></div>
    <div class="news-list">
      {''.join(news_html) if news_html else '<div class="news-item"><p class="news-summary">등록된 주요 시황 뉴스가 없습니다.</p></div>'}
    </div>

    <div class="section-title"><span>발송 게이트웨이 요약 메시지 원문 (텔레그램·카카오 공통)</span></div>
    <div class="msg-box">{msg_preview}</div>
  </div>
</body>
</html>"""
        if output_path is not None:
            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(doc, encoding="utf-8")
        return doc
