"""Web report generator for Travel Briefing.
Generates responsive, glassmorphism HTML reports for GitHub Pages and admin console preview.
"""
from __future__ import annotations
import html
import json
from datetime import datetime
from pathlib import Path
from typing import Optional

from travel_brief.models import DestinationMeta, FlightDeal, TravelRecommendation
from travel_brief.flight_engine import FlightEngine
from travel_brief.budget_calculator import BudgetCalculator
from travel_brief.ai_prompts import AIPromptGenerator


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>✈️ 주말 해외여행 6개월 특가 & 예산 브리핑</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Pretendard:wght@400;500;600;700;800&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg-gradient: linear-gradient(135deg, #0b0f19 0%, #111827 50%, #0d1527 100%);
      --card-bg: rgba(30, 41, 59, 0.7);
      --card-border: rgba(255, 255, 255, 0.08);
      --card-hover: rgba(51, 65, 85, 0.85);
      --text-main: #f8fafc;
      --text-sub: #94a3b8;
      --accent-cyan: #38bdf8;
      --accent-emerald: #34d399;
      --accent-amber: #fbbf24;
      --accent-rose: #f43f5e;
      --accent-violet: #a855f7;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Pretendard', -apple-system, BlinkMacSystemFont, system-ui, Roboto, sans-serif;
      background: var(--bg-gradient);
      color: var(--text-main);
      min-height: 100vh;
      padding: 2rem 1rem;
      line-height: 1.6;
    }
    .container {
      max-width: 1200px;
      margin: 0 auto;
    }
    header {
      text-align: center;
      margin-bottom: 2.5rem;
      padding: 2rem;
      background: rgba(15, 23, 42, 0.6);
      border-radius: 20px;
      border: 1px solid var(--card-border);
      backdrop-filter: blur(12px);
    }
    .badge {
      display: inline-block;
      padding: 0.25rem 0.75rem;
      border-radius: 9999px;
      font-size: 0.75rem;
      font-weight: 700;
      letter-spacing: 0.05em;
      text-transform: uppercase;
      margin-bottom: 0.75rem;
    }
    .badge-deal { background: rgba(52, 211, 153, 0.15); color: var(--accent-emerald); border: 1px solid rgba(52, 211, 153, 0.3); }
    .badge-best { background: rgba(251, 191, 36, 0.15); color: var(--accent-amber); border: 1px solid rgba(251, 191, 36, 0.3); }
    .badge-caution { background: rgba(244, 63, 94, 0.15); color: var(--accent-rose); border: 1px solid rgba(244, 63, 94, 0.3); }
    .badge-normal { background: rgba(56, 189, 248, 0.15); color: var(--accent-cyan); border: 1px solid rgba(56, 189, 248, 0.3); }

    h1 {
      font-size: 2.2rem;
      font-weight: 800;
      margin-bottom: 0.5rem;
      background: linear-gradient(135deg, #ffffff 0%, #38bdf8 100%);
      -webkit-background-clip: text;
      -webkit-text-fill-color: transparent;
    }
    .subtitle {
      color: var(--text-sub);
      font-size: 1rem;
    }
    .meta-bar {
      margin-top: 1rem;
      font-size: 0.85rem;
      color: #64748b;
    }

    h2 {
      font-size: 1.5rem;
      font-weight: 700;
      margin: 2rem 0 1rem;
      display: flex;
      align-items: center;
      gap: 0.5rem;
    }

    /* TOP 3 Cards Grid */
    .top-cards-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(350px, 1fr));
      gap: 1.5rem;
      margin-bottom: 3rem;
    }
    .top-card {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      padding: 1.5rem;
      backdrop-filter: blur(10px);
      transition: all 0.25s ease;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      position: relative;
      overflow: hidden;
    }
    .top-card::before {
      content: '';
      position: absolute;
      top: 0; left: 0; right: 0;
      height: 4px;
      background: linear-gradient(90deg, var(--accent-cyan), var(--accent-emerald));
    }
    .top-card:hover {
      transform: translateY(-4px);
      background: var(--card-hover);
      border-color: rgba(56, 189, 248, 0.4);
      box-shadow: 0 12px 28px rgba(0, 0, 0, 0.4);
    }
    .card-header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      margin-bottom: 1rem;
    }
    .dest-title {
      font-size: 1.4rem;
      font-weight: 800;
      color: #fff;
    }
    .dest-country {
      font-size: 0.85rem;
      color: var(--text-sub);
    }
    .card-metric {
      text-align: right;
    }
    .flight-price {
      font-size: 1.3rem;
      font-weight: 800;
      color: var(--accent-emerald);
    }
    .flight-discount {
      font-size: 0.8rem;
      color: var(--accent-emerald);
    }
    .card-dates {
      background: rgba(15, 23, 42, 0.5);
      border-radius: 10px;
      padding: 0.75rem;
      margin-bottom: 1rem;
      font-size: 0.9rem;
    }
    .card-dates .date-row {
      display: flex;
      justify-content: space-between;
      margin-bottom: 0.25rem;
    }
    .budget-box {
      border-top: 1px solid var(--card-border);
      padding-top: 0.75rem;
      margin-bottom: 1rem;
    }
    .budget-row {
      display: flex;
      justify-content: space-between;
      font-size: 0.85rem;
      margin-bottom: 0.25rem;
      color: var(--text-sub);
    }
    .budget-row.total {
      font-size: 1rem;
      font-weight: 700;
      color: var(--accent-cyan);
      margin-top: 0.5rem;
      padding-top: 0.5rem;
      border-top: 1px dashed rgba(255, 255, 255, 0.1);
    }
    .tip-box {
      font-size: 0.8rem;
      color: #cbd5e1;
      background: rgba(56, 189, 248, 0.08);
      border-left: 3px solid var(--accent-cyan);
      padding: 0.5rem 0.75rem;
      border-radius: 4px;
      margin-bottom: 1rem;
    }
    .card-actions {
      display: flex;
      gap: 0.5rem;
    }
    .btn {
      flex: 1;
      text-align: center;
      padding: 0.6rem 0.8rem;
      border-radius: 8px;
      font-size: 0.85rem;
      font-weight: 600;
      text-decoration: none;
      cursor: pointer;
      transition: background 0.2s;
      border: none;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 0.35rem;
    }
    .btn-primary {
      background: #0284c7;
      color: #fff;
    }
    .btn-primary:hover {
      background: #0369a1;
    }
    .btn-secondary {
      background: rgba(255, 255, 255, 0.1);
      color: #fff;
    }
    .btn-secondary:hover {
      background: rgba(255, 255, 255, 0.18);
    }

    /* 6-Month Heatmap Table */
    .table-container {
      overflow-x: auto;
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 16px;
      padding: 1rem;
      margin-bottom: 3rem;
      backdrop-filter: blur(10px);
    }
    table {
      width: 100%;
      border-collapse: collapse;
      text-align: center;
      font-size: 0.9rem;
    }
    th, td {
      padding: 0.85rem 0.5rem;
      border-bottom: 1px solid var(--card-border);
    }
    th {
      color: var(--text-sub);
      font-weight: 600;
      background: rgba(15, 23, 42, 0.4);
    }
    td.dest-col {
      text-align: left;
      font-weight: 700;
      color: #fff;
      padding-left: 1rem;
      min-width: 140px;
    }
    .matrix-deal {
      padding: 0.4rem 0.6rem;
      border-radius: 8px;
      display: inline-block;
      font-size: 0.8rem;
      font-weight: 600;
    }
    .deal-super {
      background: rgba(52, 211, 153, 0.15);
      color: var(--accent-emerald);
      border: 1px solid rgba(52, 211, 153, 0.3);
    }
    .deal-best-season {
      background: rgba(251, 191, 36, 0.15);
      color: var(--accent-amber);
      border: 1px solid rgba(251, 191, 36, 0.3);
    }
    .deal-caution {
      background: rgba(244, 63, 94, 0.15);
      color: var(--accent-rose);
      border: 1px solid rgba(244, 63, 94, 0.3);
    }
    .deal-normal {
      background: rgba(255, 255, 255, 0.05);
      color: #cbd5e1;
    }

    /* All 10 Destinations Grid */
    .all-dest-grid {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
      gap: 1rem;
    }
    .dest-card-compact {
      background: var(--card-bg);
      border: 1px solid var(--card-border);
      border-radius: 12px;
      padding: 1.25rem;
    }
    .dest-card-compact h3 {
      font-size: 1.1rem;
      margin-bottom: 0.5rem;
      display: flex;
      justify-content: space-between;
    }
    .dest-detail-line {
      font-size: 0.8rem;
      color: var(--text-sub);
      margin-bottom: 0.35rem;
    }

    footer {
      text-align: center;
      margin-top: 3rem;
      color: #64748b;
      font-size: 0.85rem;
    }

    /* Modal */
    .modal {
      display: none;
      position: fixed;
      top: 0; left: 0; right: 0; bottom: 0;
      background: rgba(0,0,0,0.7);
      backdrop-filter: blur(5px);
      z-index: 1000;
      align-items: center;
      justify-content: center;
      padding: 1rem;
    }
    .modal.active { display: flex; }
    .modal-content {
      background: #1e293b;
      border: 1px solid rgba(255,255,255,0.15);
      border-radius: 16px;
      max-width: 650px;
      width: 100%;
      padding: 1.75rem;
      box-shadow: 0 20px 40px rgba(0,0,0,0.5);
    }
    .modal-header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      margin-bottom: 1rem;
    }
    .modal-header h3 { font-size: 1.25rem; color: #fff; }
    .modal-close {
      background: none;
      border: none;
      color: var(--text-sub);
      font-size: 1.5rem;
      cursor: pointer;
    }
    .modal-body textarea {
      width: 100%;
      height: 220px;
      background: #0f172a;
      border: 1px solid rgba(255,255,255,0.1);
      border-radius: 8px;
      color: #e2e8f0;
      padding: 0.75rem;
      font-family: monospace;
      font-size: 0.85rem;
      resize: vertical;
      margin-bottom: 1rem;
    }
    .modal-footer {
      display: flex;
      justify-content: flex-end;
      gap: 0.75rem;
    }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="badge badge-normal">Weekend Escape Engine</div>
      <h1>✈️ 주말 해외여행 6개월 특가 & 예산 브리핑</h1>
      <p class="subtitle">인천(ICN) 출발 | 목~일 & 금~월 (3박 4일) 직장인 맞춤 실속 주말 일정</p>
      <div class="meta-bar">
        <span>기준 일시: <!-- GENERATED_AT --></span> &nbsp;|&nbsp;
        <span>분석 대상: 인기 10대 근거리 해외 여행지</span>
      </div>
    </header>

    <h2>🔥 이번 주 베스트 TOP 3 추천 (초특가 & 여행 최적기)</h2>
    <div class="top-cards-grid">
      <!-- TOP_CARDS_HTML -->
    </div>

    <h2>📊 6개월 주말 왕복 항공권 최저가 히트맵</h2>
    <div class="table-container">
      <table>
        <thead>
          <tr>
            <th style="text-align: left; padding-left: 1rem;">도시명</th>
            <!-- MONTHS_HEADERS_HTML -->
          </tr>
        </thead>
        <tbody>
          <!-- MATRIX_ROWS_HTML -->
        </tbody>
      </table>
    </div>

    <h2>🌍 10대 추천 여행지별 비자 규정 및 핵심 여행 팁</h2>
    <div class="all-dest-grid">
      <!-- DEST_CARDS_HTML -->
    </div>

    <footer>
      <p>본 리포트는 자동화된 주말 항공권 트렌드 분석 엔진에 의해 생성되었습니다.</p>
      <p>© 2026 kwTravelBrief · SafeTelegramDispatcher Enabled · Zero Phantom Fares</p>
    </footer>
  </div>

  <!-- Prompt Modal -->
  <div class="modal" id="promptModal">
    <div class="modal-content">
      <div class="modal-header">
        <h3 id="modalTitle">🤖 AI 여행 계획 프롬프트</h3>
        <button class="modal-close" onclick="closeModal()">&times;</button>
      </div>
      <div class="modal-body">
        <p style="font-size: 0.85rem; color: var(--text-sub); margin-bottom: 0.5rem;">
          복사하여 ChatGPT나 Gemini에 붙여넣으면 3박 4일 맞춤 일정과 로컬 맛집을 바로 짜줍니다:
        </p>
        <textarea id="promptText" readonly></textarea>
      </div>
      <div class="modal-footer">
        <button class="btn btn-secondary" onclick="closeModal()">닫기</button>
        <button class="btn btn-primary" onclick="copyPrompt()">📋 프롬프트 복사</button>
        <a class="btn btn-primary" id="modalChatGptBtn" target="_blank" style="background: #10a37f;">ChatGPT 바로 열기</a>
      </div>
    </div>
  </div>

  <script>
    const promptsData = /* PROMPTS_JSON */ {};

    function openPromptModal(code) {
      const data = promptsData[code];
      if (!data) return;
      document.getElementById('modalTitle').innerText = '🤖 ' + data.dest_name + ' AI 여행 계획 프롬프트';
      document.getElementById('promptText').value = data.prompt;
      document.getElementById('modalChatGptBtn').href = data.chatgpt_link;
      document.getElementById('promptModal').classList.add('active');
    }

    function closeModal() {
      document.getElementById('promptModal').classList.remove('active');
    }

    function copyPrompt() {
      const textarea = document.getElementById('promptText');
      textarea.select();
      navigator.clipboard.writeText(textarea.value).then(() => {
        alert('프롬프트가 클립보드에 복사되었습니다! ChatGPT나 Gemini에 붙여넣으세요.');
      });
    }

    window.onclick = function(event) {
      const modal = document.getElementById('promptModal');
      if (event.target === modal) {
        closeModal();
      }
    };
  </script>
</body>
</html>
"""


class WebReportGenerator:
    def __init__(self, engine: Optional[FlightEngine] = None):
        self.engine = engine or FlightEngine()

    def generate_html_report(self) -> str:
        all_destinations = self.engine.destinations
        all_deals = self.engine.search_deals(months_ahead=6)

        # Top 3 recommendations
        top_recs = BudgetCalculator.rank_recommendations(
            destinations=all_destinations, deals=all_deals, top_n=3
        )

        # 6-Month Matrix
        matrix = self.engine.get_6month_matrix()
        # Extract unique sorted months
        all_months = sorted(list({m for d in matrix.values() for m in d.keys()}))

        # Prompts data dictionary for interactive modal
        prompts_dict = {}

        # 1. TOP CARDS HTML
        top_cards_html = []
        for rec in top_recs:
            dest = rec.destination
            deal = rec.best_deal
            budget = rec.budget

            prompt = AIPromptGenerator.build_prompt(dest, deal, budget)
            chatgpt_link = AIPromptGenerator.get_chatgpt_deep_link(prompt)
            prompts_dict[dest.code] = {
                "dest_name": dest.name,
                "prompt": prompt,
                "chatgpt_link": chatgpt_link,
            }

            season_badge = {
                "BEST_SEASON": '<span class="badge badge-best">✨ 여행 최적기</span>',
                "CAUTION": f'<span class="badge badge-caution">⚠️ 주의 ({dest.caution_note})</span>',
                "NORMAL": '<span class="badge badge-normal">🌤️ 무난한 시즌</span>',
            }.get(rec.season_status, "")

            deal_badge = (
                f'<span class="badge badge-deal">🔥 초특가 {deal.discount_pct:+.0f}%</span>'
                if deal.is_super_deal
                else ""
            )

            card = f"""
      <div class="top-card">
        <div>
          <div class="card-header">
            <div>
              <div class="dest-country">{html.escape(dest.country)} · 직항 {dest.flight_time_hours}시간</div>
              <div class="dest-title">{html.escape(dest.name)} ({dest.code})</div>
            </div>
            <div class="card-metric">
              <div class="flight-price">{deal.price_krw:,}원</div>
              <div class="flight-discount">{deal.airline} 왕복</div>
            </div>
          </div>
          <div style="margin-bottom: 0.75rem;">
            {deal_badge} {season_badge}
          </div>
          <div class="card-dates">
            <div class="date-row">
              <span style="color: var(--text-sub);">출발</span>
              <span><strong>{deal.departure_date} ({deal.departure_day_of_week})</strong></span>
            </div>
            <div class="date-row">
              <span style="color: var(--text-sub);">도착</span>
              <span><strong>{deal.return_date} ({deal.return_day_of_week})</strong> [3박 4일]</span>
            </div>
          </div>
          <div class="budget-box">
            <div class="budget-row">
              <span>숙박 (3박 평균)</span>
              <span>{budget.hotel_total_krw:,}원</span>
            </div>
            <div class="budget-row">
              <span>식비/교통 (4일)</span>
              <span>{budget.meal_transit_total_krw:,}원</span>
            </div>
            <div class="budget-row total">
              <span>1인 총 예상 경비</span>
              <span>약 {budget.total_per_person_krw // 10000}만 원</span>
            </div>
            <div class="budget-row" style="font-size: 0.75rem; color: #64748b;">
              <span>(2인 기준 총합: 약 {budget.estimated_for_two_krw // 10000}만 원)</span>
            </div>
          </div>
          <div class="tip-box">
            <strong>비자/팁:</strong> {html.escape(dest.visa_info)} · {html.escape(dest.tips)}
          </div>
        </div>
        <div class="card-actions">
          <button class="btn btn-secondary" onclick="openPromptModal('{dest.code}')">📋 프롬프트 보기</button>
          <a class="btn btn-primary" href="{chatgpt_link}" target="_blank">🤖 ChatGPT 일정 짜기</a>
        </div>
      </div>
"""
            top_cards_html.append(card)

        # 2. MATRIX TABLE HTML
        months_headers = "".join(f"<th>{m}</th>" for m in all_months)
        matrix_rows = []

        for dest_code, dest in all_destinations.items():
            cells = []
            for m in all_months:
                deal = matrix.get(dest_code, {}).get(m)
                if deal:
                    month_int = int(m.split("-")[1])
                    if deal.is_super_deal:
                        cls_name = "deal-super"
                    elif dest.is_best_month(month_int):
                        cls_name = "deal-best-season"
                    elif dest.is_caution_month(month_int):
                        cls_name = "deal-caution"
                    else:
                        cls_name = "deal-normal"

                    cells.append(
                        f'<td><span class="matrix-deal {cls_name}">{deal.price_krw // 10000}만<br>'
                        f'<small style="font-size: 0.7rem; opacity: 0.8;">{deal.departure_date[5:]}</small></span></td>'
                    )
                else:
                    cells.append("<td>-</td>")

            row = f"""
          <tr>
            <td class="dest-col">{html.escape(dest.name)} <span style="font-size: 0.75rem; color: var(--text-sub);">({dest.code})</span></td>
            {"".join(cells)}
          </tr>
"""
            matrix_rows.append(row)

        # 3. DESTINATIONS CARDS HTML
        dest_cards = []
        for dest in all_destinations.values():
            best_m_str = ", ".join(f"{m}월" for m in dest.best_months)
            caution_m_str = (
                ", ".join(f"{m}월" for m in dest.caution_months) if dest.caution_months else "없음"
            )
            card = f"""
        <div class="dest-card-compact">
          <h3>
            <span>{html.escape(dest.name)} ({dest.code})</span>
            <span style="font-size: 0.85rem; color: var(--accent-cyan);">{html.escape(dest.country)}</span>
          </h3>
          <div class="dest-detail-line"><strong>비자:</strong> {html.escape(dest.visa_info)}</div>
          <div class="dest-detail-line"><strong>최적 시즌:</strong> {best_m_str}</div>
          <div class="dest-detail-line"><strong>주의 시기:</strong> {caution_m_str} ({html.escape(dest.caution_note)})</div>
          <div class="dest-detail-line"><strong>1박 숙소 평균:</strong> 약 {dest.hotel_night_avg_krw // 10000}만 원</div>
          <div class="dest-detail-line"><strong>하루 식비/교통:</strong> 약 {dest.daily_meal_transit_krw // 10000}만 원</div>
          <div class="dest-detail-line" style="margin-top: 0.5rem; color: #cbd5e1;">💡 {html.escape(dest.tips)}</div>
        </div>
"""
            dest_cards.append(card)

        # Fill template with clean replace
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        rendered = (
            HTML_TEMPLATE.replace("<!-- GENERATED_AT -->", now_str)
            .replace("<!-- TOP_CARDS_HTML -->", "\n".join(top_cards_html))
            .replace("<!-- MONTHS_HEADERS_HTML -->", months_headers)
            .replace("<!-- MATRIX_ROWS_HTML -->", "\n".join(matrix_rows))
            .replace("<!-- DEST_CARDS_HTML -->", "\n".join(dest_cards))
            .replace("/* PROMPTS_JSON */ {}", json.dumps(prompts_dict, ensure_ascii=False))
        )
        return rendered

    def save_reports(self, output_dir: Optional[str | Path] = None) -> tuple[Path, Path]:
        if output_dir is None:
            output_dir = Path(__file__).resolve().parent.parent / "reports"
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        now_tag = datetime.now().strftime("%Y%m%d_%H%M")
        dated_file = out_path / f"travel_briefing_{now_tag}.html"
        latest_file = out_path / "latest.html"

        content = self.generate_html_report()
        dated_file.write_text(content, encoding="utf-8")
        latest_file.write_text(content, encoding="utf-8")

        return dated_file, latest_file
