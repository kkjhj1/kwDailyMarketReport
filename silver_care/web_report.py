"""SilverCare (실버케어) Mobile Web Report Generator.

Generates reports/silver_latest.html for publishing to GitHub Pages.
[SAFE-01 / GPT Audit Rule]:
Strict PII-free invariant — NO personal names, chat IDs, individual birthdates,
or private tokens. Completely anonymized public handbook for 6070+ parents.
Mobile 360px responsive, zero horizontal overflow, touch target >= 44px.
"""
from __future__ import annotations
import html
from datetime import datetime
from pathlib import Path
from typing import Iterable, Sequence

from silver_care.models import BenefitCategory, SilverBenefit


def _esc(val: object) -> str:
    return html.escape(str(val or ""), quote=True)


def _safe_tel(agency: str) -> str:
    """Extract digits for official tel link (e.g. '1577-1000' -> '15771000')."""
    import re
    m = re.search(r"(\d{2,4}-\d{3,4}-\d{4}|\d{4}|\d{3})", agency)
    return m.group(1).replace("-", "") if m else ""


def generate_silver_web_report(
    benefits: Sequence[SilverBenefit],
    output_path: Path | str,
    base_url: str = "",
) -> Path:
    out_file = Path(output_path)
    out_file.parent.mkdir(parents=True, exist_ok=True)

    now_str = datetime.now().strftime("%Y년 %m월 %d일")
    core_guides = [b for b in benefits if b.has_field_guide]
    all_benefits = sorted(benefits, key=lambda b: (b.category, b.id))

    by_category: dict[str, list[SilverBenefit]] = {}
    for b in all_benefits:
        by_category.setdefault(b.category, []).append(b)

    html_content = f"""<!DOCTYPE html>
<html lang="ko">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
  <title>실버케어 2026 대한민국 부모님 복지 대백과</title>
  <meta name="description" content="자녀가 대신 챙기는 2026년 시니어·부모님 생활밀착형 복지 혜택 및 실전 신청 가이드">
  <meta name="theme-color" content="#1a365d">
  <style>
    :root {{
      --primary: #1e3a8a;
      --primary-light: #3b82f6;
      --primary-soft: #eff6ff;
      --success: #15803d;
      --success-bg: #f0fdf4;
      --warning: #b45309;
      --warning-bg: #fffbeb;
      --danger: #b91c1c;
      --danger-bg: #fef2f2;
      --text-main: #1f2937;
      --text-muted: #6b7280;
      --bg: #f8fafc;
      --card-bg: #ffffff;
      --border: #e2e8f0;
      --radius: 14px;
    }}
    * {{
      box-sizing: border-box;
      margin: 0;
      padding: 0;
      -webkit-tap-highlight-color: transparent;
    }}
    body {{
      font-family: -apple-system, BlinkMacSystemFont, "Pretendard", "Apple SD Gothic Neo", "Noto Sans KR", sans-serif;
      background: var(--bg);
      color: var(--text-main);
      line-height: 1.6;
      font-size: 15px;
      overflow-x: hidden;
      padding-bottom: 60px;
    }}
    .container {{
      max-width: 640px;
      margin: 0 auto;
      padding: 16px;
    }}
    /* Header */
    .header {{
      background: linear-gradient(135deg, #1e3a8a 0%, #1e40af 100%);
      color: #ffffff;
      padding: 24px 20px;
      border-radius: var(--radius);
      margin-bottom: 20px;
      box-shadow: 0 4px 12px rgba(30, 58, 138, 0.15);
    }}
    .header-badge {{
      display: inline-block;
      background: rgba(255, 255, 255, 0.2);
      padding: 4px 10px;
      border-radius: 20px;
      font-size: 12px;
      font-weight: 600;
      margin-bottom: 8px;
      letter-spacing: 0.5px;
    }}
    .header h1 {{
      font-size: 22px;
      font-weight: 800;
      line-height: 1.3;
      margin-bottom: 8px;
    }}
    .header p {{
      font-size: 14px;
      opacity: 0.9;
    }}
    .header-meta {{
      margin-top: 12px;
      font-size: 12px;
      opacity: 0.75;
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-top: 1px solid rgba(255, 255, 255, 0.15);
      padding-top: 10px;
    }}

    /* Stat Cards */
    .stats-grid {{
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 10px;
      margin-bottom: 24px;
    }}
    .stat-card {{
      background: var(--card-bg);
      padding: 12px;
      border-radius: 12px;
      text-align: center;
      border: 1px solid var(--border);
    }}
    .stat-num {{
      font-size: 18px;
      font-weight: 800;
      color: var(--primary);
    }}
    .stat-label {{
      font-size: 12px;
      color: var(--text-muted);
      margin-top: 2px;
    }}

    /* Section Title */
    .section-title {{
      font-size: 18px;
      font-weight: 800;
      margin: 24px 0 14px;
      display: flex;
      align-items: center;
      gap: 8px;
      color: #0f172a;
    }}
    .section-title span.badge {{
      background: var(--primary-soft);
      color: var(--primary);
      font-size: 12px;
      padding: 2px 8px;
      border-radius: 10px;
      font-weight: 700;
    }}

    /* Accordion Guide Cards */
    .guide-card {{
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: var(--radius);
      margin-bottom: 14px;
      overflow: hidden;
      box-shadow: 0 2px 6px rgba(0, 0, 0, 0.03);
      transition: border-color 0.2s;
    }}
    .guide-card:focus-within, .guide-card:hover {{
      border-color: var(--primary-light);
    }}
    .guide-header {{
      padding: 16px;
      cursor: pointer;
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      user-select: none;
    }}
    .guide-title-wrap {{
      flex: 1;
      padding-right: 12px;
    }}
    .guide-category {{
      font-size: 12px;
      font-weight: 600;
      color: var(--primary);
      margin-bottom: 4px;
      display: inline-block;
    }}
    .guide-title {{
      font-size: 16px;
      font-weight: 700;
      color: #111827;
      line-height: 1.35;
    }}
    .guide-amount {{
      font-size: 14px;
      font-weight: 700;
      color: var(--danger);
      margin-top: 4px;
    }}
    .guide-arrow {{
      font-size: 16px;
      color: var(--text-muted);
      transition: transform 0.2s ease;
      min-width: 24px;
      height: 24px;
      display: flex;
      align-items: center;
      justify-content: center;
    }}
    .guide-card.open .guide-arrow {{
      transform: rotate(180deg);
    }}

    /* Accordion Body */
    .guide-body {{
      display: none;
      padding: 0 16px 16px;
      border-top: 1px solid var(--border);
      background: #fafafa;
    }}
    .guide-card.open .guide-body {{
      display: block;
    }}

    /* Key Meta Badges */
    .meta-tags {{
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin: 14px 0;
    }}
    .meta-tag {{
      background: #ffffff;
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 4px 8px;
      font-size: 12px;
      color: #374151;
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }}

    /* Steps */
    .step-list {{
      list-style: none;
      margin: 14px 0;
      counter-reset: step-counter;
    }}
    .step-item {{
      position: relative;
      padding-left: 32px;
      margin-bottom: 10px;
      font-size: 14px;
      line-height: 1.45;
    }}
    .step-item::before {{
      counter-increment: step-counter;
      content: counter(step-counter);
      position: absolute;
      left: 0;
      top: 1px;
      width: 22px;
      height: 22px;
      background: var(--primary);
      color: #ffffff;
      border-radius: 50%;
      display: flex;
      align-items: center;
      justify-content: center;
      font-size: 12px;
      font-weight: 700;
    }}

    /* Document Table */
    .doc-table {{
      width: 100%;
      border-collapse: collapse;
      margin: 12px 0;
      background: #ffffff;
      border-radius: 8px;
      overflow: hidden;
      border: 1px solid var(--border);
      font-size: 13px;
    }}
    .doc-table th {{
      background: #f1f5f9;
      color: #475569;
      text-align: left;
      padding: 8px 10px;
      font-weight: 600;
    }}
    .doc-table td {{
      padding: 8px 10px;
      border-top: 1px solid #f1f5f9;
      color: #334155;
    }}
    .req-badge {{
      display: inline-block;
      padding: 2px 6px;
      border-radius: 4px;
      font-size: 11px;
      font-weight: 700;
    }}
    .req-badge.must {{
      background: #fee2e2;
      color: #991b1b;
    }}
    .req-badge.opt {{
      background: #f1f5f9;
      color: #475569;
    }}

    /* Rejection Callout */
    .rejection-box {{
      background: var(--warning-bg);
      border-left: 4px solid var(--warning);
      border-radius: 0 8px 8px 0;
      padding: 12px 14px;
      margin: 12px 0;
      font-size: 13.5px;
    }}
    .rejection-title {{
      font-weight: 800;
      color: var(--warning);
      margin-bottom: 4px;
      display: flex;
      align-items: center;
      gap: 6px;
      font-size: 13px;
    }}

    /* Warning Callout */
    .warning-box {{
      background: var(--danger-bg);
      border-left: 4px solid var(--danger);
      border-radius: 0 8px 8px 0;
      padding: 12px 14px;
      margin: 12px 0;
      font-size: 13.5px;
      color: #7f1d1d;
    }}

    /* Action Buttons */
    .action-group {{
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 8px;
      margin-top: 14px;
    }}
    .btn {{
      min-height: 44px;
      padding: 10px 14px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 700;
      border: none;
      cursor: pointer;
      display: inline-flex;
      align-items: center;
      justify-content: center;
      gap: 6px;
      text-decoration: none;
      transition: background-color 0.2s;
    }}
    .btn-tel {{
      background: var(--primary-soft);
      color: var(--primary);
      border: 1px solid #bfdbfe;
    }}
    .btn-tel:hover {{
      background: #dbeafe;
    }}
    .btn-copy {{
      background: #fef08a;
      color: #854d0e;
      border: 1px solid #fde047;
    }}
    .btn-copy:hover {{
      background: #fde047;
    }}
    .btn-source {{
      grid-column: span 2;
      background: #ffffff;
      color: var(--text-muted);
      border: 1px solid var(--border);
      font-weight: 500;
      font-size: 12px;
    }}

    /* Catalog Table */
    .catalog-list {{
      background: var(--card-bg);
      border-radius: var(--radius);
      border: 1px solid var(--border);
      overflow: hidden;
      margin-bottom: 24px;
    }}
    .catalog-item {{
      padding: 12px 16px;
      border-bottom: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      align-items: center;
      gap: 10px;
    }}
    .catalog-item:last-child {{
      border-bottom: none;
    }}
    .catalog-title {{
      font-size: 14px;
      font-weight: 700;
      color: #111827;
    }}
    .catalog-sub {{
      font-size: 12px;
      color: var(--text-muted);
      margin-top: 2px;
    }}
    .catalog-amount {{
      font-size: 13px;
      font-weight: 700;
      color: var(--primary);
      text-align: right;
      white-space: nowrap;
    }}

    /* Toast */
    .toast {{
      position: fixed;
      bottom: 24px;
      left: 50%;
      transform: translateX(-50%) translateY(100px);
      background: #111827;
      color: #ffffff;
      padding: 10px 20px;
      border-radius: 30px;
      font-size: 13px;
      font-weight: 600;
      box-shadow: 0 4px 12px rgba(0, 0, 0, 0.2);
      transition: transform 0.25s ease-out;
      z-index: 9999;
      pointer-events: none;
    }}
    .toast.show {{
      transform: translateX(-50%) translateY(0);
    }}

    /* Footer */
    .footer {{
      margin-top: 32px;
      padding-top: 20px;
      border-top: 1px solid var(--border);
      font-size: 12px;
      color: var(--text-muted);
      line-height: 1.6;
      text-align: center;
    }}
  </style>
</head>
<body>
  <div class="container">
    <header class="header">
      <div class="header-badge">👵 실버케어 v2.0 공식 가이드</div>
      <h1>2026 대한민국<br>부모님 복지 대백과</h1>
      <p>동사무소·보건소 혜택, 자녀가 대신 신청해서 챙겨드려요</p>
      <div class="header-meta">
        <span>기준: 2026년 정부 지침 팩트체크</span>
        <span>갱신: {now_str}</span>
      </div>
    </header>

    <div class="stats-grid">
      <div class="stat-card">
        <div class="stat-num">{len(core_guides)}개</div>
        <div class="stat-label">실전 집중 가이드</div>
      </div>
      <div class="stat-card">
        <div class="stat-num">{len(all_benefits)}개</div>
        <div class="stat-label">전국/지자체 카탈로그</div>
      </div>
      <div class="stat-card">
        <div class="stat-num">300만+</div>
        <div class="stat-label">평균 수혜 금액(연)</div>
      </div>
    </div>

    <!-- 10대 핵심 실전 가이드 -->
    <div class="section-title">
      🎯 10대 핵심 복지 실전 가이드
      <span class="badge">필수 확인</span>
    </div>
"""

    for b in core_guides:
        tel_digits = _safe_tel(b.agency_name)
        tel_link = f'<a href="tel:{tel_digits}" class="btn btn-tel">📞 {_esc(b.agency_name)} 통화</a>' if tel_digits else f'<span class="btn btn-tel" style="opacity:0.7">🏛 {_esc(b.agency_name)}</span>'

        # Docs table
        doc_rows = ""
        for d in b.documents:
            is_req = "필수" in d.get("required", "")
            req_cls = "must" if is_req else "opt"
            doc_rows += f"""
            <tr>
              <td><strong>{_esc(d.get('name'))}</strong></td>
              <td>{_esc(d.get('issuer'))}</td>
              <td><span class="req-badge {req_cls}">{_esc(d.get('required'))}</span></td>
            </tr>"""

        # Steps
        steps_html = "".join(f'<li class="step-item">{_esc(s)}</li>' for s in b.application_steps)

        # Rejection
        rejection_html = f"""
        <div class="rejection-box">
          <div class="rejection-title">⚠️ 담당 공무원이 거절하거나 안 된다고 할 때</div>
          <p>{_esc(b.rejection_response)}</p>
        </div>""" if b.rejection_response else ""

        # Warnings
        warns_html = ""
        if b.warning_notes:
            warn_items = "".join(f"<li>{_esc(w)}</li>" for w in b.warning_notes)
            warns_html = f"""
            <div class="warning-box">
              <strong style="display:block; margin-bottom:4px;">🚨 필수 주의사항</strong>
              <ul style="padding-left:18px; margin:0;">{warn_items}</ul>
            </div>"""

        # Kakao share body
        parent_msg = b.parent_msg_template.replace("{alias}", "엄마")
        kakao_text = f"{parent_msg}\\n\\n✔ {b.title}\\n✔ {b.support_amount}\\n✔ 문의: {b.agency_name}\\n\\n신청은 제가 도와드릴게요 💕"
        kakao_esc_data = _esc(kakao_text)

        # Source link
        source_btn = ""
        if b.source_url and b.source_url.startswith("https://"):
            source_btn = f'<a href="{_esc(b.source_url)}" target="_blank" rel="noopener noreferrer" class="btn btn-source">🌐 공식 공고·누리집 원문 확인 ↗</a>'

        html_content += f"""
    <div class="guide-card" id="benefit-{b.id}">
      <div class="guide-header" onclick="toggleGuide({b.id})">
        <div class="guide-title-wrap">
          <span class="guide-category">{_esc(b.category_label)} · 만 {b.min_age}세 이상</span>
          <h2 class="guide-title">{_esc(b.title)}</h2>
          <div class="guide-amount">{_esc(b.support_amount)}</div>
        </div>
        <div class="guide-arrow">▼</div>
      </div>
      <div class="guide-body">
        <div class="meta-tags">
          <span class="meta-tag">🏛 {_esc(b.agency_name)}</span>
          <span class="meta-tag">🗓 {_esc(b.application_period)}</span>
          {'<span class="meta-tag" style="color:var(--danger);font-weight:700;">🚨 사전승인 필수</span>' if b.is_pre_approval_required else ''}
          {'<span class="meta-tag">📄 진단서·처방전 필요</span>' if b.requires_prescription else ''}
        </div>

        <p style="font-size:14px; color:#374151; margin-bottom:12px;">{_esc(b.detail_guide)}</p>

        <h4 style="font-size:14px; font-weight:700; margin-top:14px; color:#1e293b;">📋 신청 절차 4단계</h4>
        <ol class="step-list">{steps_html}</ol>

        <h4 style="font-size:14px; font-weight:700; margin-top:14px; color:#1e293b;">🎒 자녀 대리신청 준비 서류</h4>
        <table class="doc-table">
          <thead>
            <tr><th>서류명</th><th>발급처</th><th>구분</th></tr>
          </thead>
          <tbody>{doc_rows}</tbody>
        </table>

        {rejection_html}
        {warns_html}

        <div class="action-group">
          {tel_link}
          <button class="btn btn-copy" onclick="copyKakao(this, `{kakao_esc_data}`)">💌 카톡 문구 복사</button>
          {source_btn}
        </div>
      </div>
    </div>
"""

    # 31개 전체 복지 카탈로그
    html_content += """
    <div class="section-title">
      📚 전체 31개 복지 카탈로그
      <span class="badge">전국 공통 및 지자체</span>
    </div>
    <div class="catalog-list">
"""

    for cat in (BenefitCategory.MEDICAL, BenefitCategory.FINANCE, BenefitCategory.HOUSING, BenefitCategory.SOCIAL):
        cat_items = by_category.get(cat.value, [])
        if not cat_items:
            continue
        html_content += f"""
      <div style="background:#f1f5f9; padding:8px 16px; font-size:13px; font-weight:700; color:#475569;">
        {_esc(cat.label)} ({len(cat_items)}개)
      </div>
"""
        for b in cat_items:
            target_badge = "전국" if not b.is_local else "지자체"
            html_content += f"""
      <div class="catalog-item">
        <div>
          <div class="catalog-title">{_esc(b.title)}</div>
          <div class="catalog-sub">만 {b.min_age}세 이상 · {_esc(b.agency_name)} · [{target_badge}]</div>
        </div>
        <div class="catalog-amount">{_esc(b.support_amount)}</div>
      </div>
"""

    html_content += f"""
    </div>

    <footer class="footer">
      <p>👵 <strong>실버케어 (SilverCare) 부모님 복지 비서</strong></p>
      <p style="margin-top:6px;">본 리포트는 보건복지부, 국민건강보험공단, 국민연금공단 등 공식 지침을 기반으로 제작된 대국민 정보입니다.<br>
      개인 식별 정보(PII)는 일체 수집·저장하거나 리포트에 포함하지 않습니다.</p>
      <p style="margin-top:8px; opacity:0.6;">© 2026 Multi-Bot Intelligence Platform. All rights reserved.</p>
    </footer>
  </div>

  <div id="toast" class="toast">카톡 문구가 복사되었습니다!</div>

  <script>
    function toggleGuide(id) {{
      const card = document.getElementById('benefit-' + id);
      if (card) {{
        card.classList.toggle('open');
      }}
    }}

    function copyKakao(btn, text) {{
      const decoded = text.replace(/\\\\n/g, '\\n');
      if (navigator.clipboard && navigator.clipboard.writeText) {{
        navigator.clipboard.writeText(decoded).then(() => {{
          showToast();
        }}).catch(() => {{
          fallbackCopy(decoded);
        }});
      }} else {{
        fallbackCopy(decoded);
      }}
    }}

    function fallbackCopy(text) {{
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.style.position = 'fixed';
      ta.style.opacity = '0';
      document.body.appendChild(ta);
      ta.select();
      try {{
        document.execCommand('copy');
        showToast();
      }} catch (err) {{
        alert('복사에 실패했습니다. 직접 복사해주세요.');
      }}
      document.body.removeChild(ta);
    }}

    function showToast() {{
      const toast = document.getElementById('toast');
      toast.classList.add('show');
      setTimeout(() => {{
        toast.classList.remove('show');
      }}, 2000);
    }}

    // Auto-open first guide for preview
    document.addEventListener('DOMContentLoaded', () => {{
      const first = document.querySelector('.guide-card');
      if (first) {{
        first.classList.add('open');
      }}
    }});
  </script>
</body>
</html>
"""

    out_file.write_text(html_content, encoding="utf-8")
    return out_file
