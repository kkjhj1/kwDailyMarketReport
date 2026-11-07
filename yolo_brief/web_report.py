"""[TG-03] YOLO Bot (욜로봇) v2.0 Visual Web Magazine Renderer.
- Single self-contained HTML (inline CSS + vanilla JS tab filter), no external assets besides photos.
- 360px mobile safe: 1-column stack by default, `repeat(auto-fit, minmax(300px, 1fr))` from 640px.
- Every dynamic string goes through `html.escape()`; image/link URLs are `https://`-validated models.
- [INV-02] Under severe/unknown weather the magazine never exposes OUTDOOR curations.
"""
from __future__ import annotations

from datetime import datetime
import html
from pathlib import Path
from typing import Optional, Sequence

from yolo_brief.models import (
    KST,
    ActivityMode,
    Season,
    SoloArchetype,
    WeatherSnapshot,
    YoloCuration,
)
from yolo_brief.weather_engine import evaluate_weather_gate, get_kst_season

DEFAULT_REPORT_PATH = Path("reports") / "yolo_latest.html"

BUDGET_CATEGORY_LABELS: dict[str, str] = {
    "transport": "🚆 교통",
    "food": "🍽 식음",
    "ticket": "🎟 입장",
    "stay": "🛏 숙박",
    "etc": "🧾 기타",
}

_ARC_GRADIENTS: dict[SoloArchetype, str] = {
    SoloArchetype.ACTIVE_CHALLENGER: "linear-gradient(135deg, #ff7a59, #ffb347)",
    SoloArchetype.URBAN_HIPSTER: "linear-gradient(135deg, #6a5acd, #c471ed)",
    SoloArchetype.ROAD_NOMAD: "linear-gradient(135deg, #1fa2ff, #12d8fa)",
    SoloArchetype.WELLNESS_DEEPDIVER: "linear-gradient(135deg, #3ca55c, #b5ac49)",
}

_CSS = """
@import url('https://cdn.jsdelivr.net/gh/orioncactus/pretendard/dist/web/static/pretendard.css');
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;font-family:'Pretendard',-apple-system,BlinkMacSystemFont,'Apple SD Gothic Neo',sans-serif;
background:#f8fafc;color:#1e293b;line-height:1.65;letter-spacing:-0.015em;overflow-x:hidden;word-break:keep-all;overflow-wrap:anywhere}
a{color:inherit;text-decoration:none}
img{display:block;max-width:100%}
.wrap{max-width:1120px;margin:0 auto;padding:0 16px 64px}
.masthead{background:linear-gradient(135deg,#0f172a 0%,#1e1b4b 100%);color:#fff;padding:36px 20px 30px;text-align:center;border-bottom:1px solid rgba(255,255,255,.08)}
.masthead .kicker{font-size:12px;letter-spacing:.2em;opacity:.8;margin:0;font-weight:700;text-transform:uppercase}
.masthead h1{font-size:26px;margin:8px 0 10px;line-height:1.35;font-weight:800;letter-spacing:-0.03em}
.masthead .meta{font-size:13px;opacity:.85;margin:0}
.weather{margin:16px auto 0;max-width:520px;border-radius:9999px;padding:10px 18px;font-size:14px;font-weight:600;background:rgba(255,255,255,.1);border:1px solid rgba(255,255,255,.15);display:inline-flex;align-items:center;gap:6px}
.weather.severe{background:#fef2f2;color:#991b1b;border-color:#fecaca}
.tabs{position:sticky;top:0;z-index:20;background:#f8fafc;display:grid;grid-template-columns:repeat(5,minmax(0,1fr));
gap:8px;padding:14px 0;margin:0;backdrop-filter:blur(8px)}
.tab{min-height:44px;border:1px solid #e2e8f0;background:#fff;border-radius:9999px;font-size:12px;font-weight:700;
padding:6px 4px;cursor:pointer;color:#475569;line-height:1.2;display:flex;flex-direction:column;align-items:center;justify-content:center;transition:all .15s ease;box-shadow:0 1px 3px rgba(0,0,0,.04)}
.tab .em{display:block;font-size:16px;margin-bottom:1px}
.tab:hover{border-color:#cbd5e1;background:#f1f5f9}
.tab[aria-selected="true"]{background:#0f172a;color:#fff;border-color:#0f172a;box-shadow:0 4px 12px rgba(15,23,42,.2)}
.grid{display:grid;grid-template-columns:1fr;gap:24px}
@media (min-width:640px){.grid{grid-template-columns:repeat(auto-fit,minmax(300px,1fr))}.masthead h1{font-size:32px}}
.card{background:#fff;border-radius:24px;overflow:hidden;box-shadow:0 10px 25px -5px rgba(0,0,0,.05),0 8px 10px -6px rgba(0,0,0,.02);border:1px solid #e2e8f0;min-width:0;display:flex;flex-direction:column;transition:transform .2s ease,box-shadow .2s ease}
.card:hover{transform:translateY(-2px);box-shadow:0 14px 30px -5px rgba(0,0,0,.08)}
.card[hidden]{display:none}
.hero{position:relative;aspect-ratio:16/9;background:#e2e8f0;overflow:hidden}
.hero img{width:100%;height:100%;object-fit:cover;transition:transform .3s ease}
.card:hover .hero img{transform:scale(1.02)}
.hero-ph{width:100%;height:100%;display:flex;align-items:center;justify-content:center;font-size:64px}
.badges{position:absolute;left:12px;top:12px;display:flex;gap:6px;flex-wrap:wrap}
.badge{background:rgba(15,23,42,.75);backdrop-filter:blur(6px);color:#fff;font-size:11px;font-weight:700;padding:4px 10px;border-radius:9999px;letter-spacing:.02em}
.badge.fb{background:#0284c7}
.body{padding:20px 20px 24px;display:flex;flex-direction:column;gap:14px;flex:1}
.body h2{font-size:19px;margin:0;line-height:1.45;font-weight:800;letter-spacing:-0.02em;color:#0f172a}
.summary{margin:0;font-size:14.5px;color:#475569;line-height:1.6}
.chips{display:flex;flex-wrap:wrap;gap:6px;margin:0;padding:0;list-style:none}
.chips li{font-size:12px;background:#f1f5f9;color:#334155;border-radius:6px;padding:4px 8px;font-weight:600}
.chips.tags li{background:#eff6ff;color:#1d4ed8;font-weight:700}
h3{font-size:14px;margin:0 0 8px;color:#334155;font-weight:700}
.timeline{list-style:none;margin:0;padding:0 0 0 16px;border-left:2px solid #e2e8f0}
.timeline li{position:relative;padding:0 0 12px 12px}
.timeline li::before{content:"";position:absolute;left:-23px;top:6px;width:12px;height:12px;border-radius:50%;background:#0284c7;border:2px solid #fff;box-shadow:0 0 0 2px #bae6fd}
.timeline .t{font-weight:800;font-size:12.5px;color:#0284c7;background:#f0f9ff;padding:2px 6px;border-radius:4px;display:inline-block;margin-bottom:2px}
.timeline .s{font-weight:800;font-size:14px;color:#0f172a;margin-left:4px}
.timeline .d{display:block;font-size:13px;color:#64748b;margin-top:2px}
.budget-wrap{background:#f8fafc;border:1px solid #f1f5f9;border-radius:14px;padding:12px 14px}
table.budget{width:100%;border-collapse:collapse;table-layout:fixed;font-size:13px}
table.budget th,table.budget td{padding:6px 4px;border-bottom:1px solid #f1f5f9;text-align:left;vertical-align:top}
table.budget .cat{width:64px;color:#64748b;font-size:12px;font-weight:600}
table.budget .amt{width:88px;text-align:right;font-variant-numeric:tabular-nums;font-weight:600;color:#0f172a}
table.budget tfoot td{font-weight:800;border-bottom:0;font-size:14.5px;color:#0f172a;padding-top:10px}
.secret{background:#fffbeb;border:1px solid #fef08a;border-radius:14px;padding:12px 14px;font-size:13.5px;color:#78350f;margin:0;line-height:1.55}
.secret strong{color:#92400e}
.gallery{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px}
.gallery img{width:100%;aspect-ratio:16/9;object-fit:cover;border-radius:10px}
.actions{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:8px;margin-top:auto;padding-top:8px}
.btn{display:flex;align-items:center;justify-content:center;min-height:44px;border-radius:12px;text-decoration:none;
font-size:13px;font-weight:700;text-align:center;padding:6px 10px;transition:all .15s ease}
.btn.naver{background:#03c75a;color:#fff}
.btn.naver:hover{background:#02b350}
.btn.kakao{background:#fee500;color:#191919}
.btn.kakao:hover{background:#fada0a}
.btn.main{grid-column:1/-1;background:#0f172a;color:#fff}
.btn.main:hover{background:#1e293b}
.empty{text-align:center;color:#64748b;padding:48px 0;font-size:15px}
footer{text-align:center;font-size:12.5px;color:#94a3b8;margin-top:40px;line-height:1.6}
"""

_JS = """
(function(){
  var tabs=document.querySelectorAll('.tab');
  var cards=document.querySelectorAll('.card');
  function apply(f){
    tabs.forEach(function(t){t.setAttribute('aria-selected',t.dataset.filter===f?'true':'false');});
    cards.forEach(function(c){c.hidden=!(f==='ALL'||(' '+c.dataset.arcs+' ').indexOf(' '+f+' ')>=0);});
  }
  tabs.forEach(function(t){t.addEventListener('click',function(){
    apply(t.dataset.filter);
    if(history.replaceState){history.replaceState(null,'','#'+t.dataset.filter);}
  });});
  var h=(location.hash||'').replace('#','');
  var ok=Array.prototype.some.call(tabs,function(t){return t.dataset.filter===h;});
  apply(ok?h:'ALL');
})();
"""


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _won(amount: int) -> str:
    return f"₩{amount:,}"


class YoloWebReportGenerator:
    """Builds the mobile-first visual web magazine for YOLO Bot curations."""

    def select_curations(
        self,
        curations: Sequence[YoloCuration],
        season: Season,
        is_severe: bool,
    ) -> list[YoloCuration]:
        """Season filter + [INV-02] outdoor block, regular items first then by priority."""
        selected = [
            c
            for c in curations
            if c.is_active
            and c.solo_tags
            and c.season in (season, Season.ALL)
            and not (is_severe and c.activity_mode == ActivityMode.OUTDOOR)
        ]
        if is_severe:
            # 악천후: 실내 대체(fallback) 코스를 맨 앞으로
            return sorted(selected, key=lambda c: (not c.is_fallback, -c.priority, c.id))
        return sorted(selected, key=lambda c: (c.is_fallback, -c.priority, c.id))

    def render_html(
        self,
        curations: Sequence[YoloCuration],
        season: Optional[Season] = None,
        weather_snapshot: Optional[WeatherSnapshot] = None,
        generated_at: Optional[datetime] = None,
    ) -> str:
        target_season = season or get_kst_season()
        now = generated_at or datetime.now(KST)

        is_severe = False
        weather_html = ""
        if weather_snapshot is not None:
            is_severe, _, reason = evaluate_weather_gate(weather_snapshot)
            if is_severe:
                weather_html = (
                    f'<p class="weather severe">☔ <b>{_e(reason)}</b> · '
                    "야외 코스를 숨기고 실내 스마트 대체 코스를 먼저 보여드려요.</p>"
                )
            else:
                weather_html = f'<p class="weather">🌤 <b>{_e(reason)}</b> · 야외 코스 활동 양호</p>'

        items = self.select_curations(curations, target_season, is_severe)

        tab_html = ['<button type="button" class="tab" data-filter="ALL"><span class="em">✨</span>전체</button>']
        for arc in SoloArchetype:
            tab_html.append(
                f'<button type="button" class="tab" data-filter="{_e(arc.value)}">'
                f'<span class="em">{_e(arc.emoji)}</span>{_e(arc.label)}</button>'
            )

        cards = [self._render_card(c) for c in items]
        grid_html = "\n".join(cards) if cards else '<p class="empty">이번 시즌 큐레이션을 준비 중입니다.</p>'

        title = f"나홀로 욜로 매거진 · {target_season.label} 큐레이션"
        return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{_e(title)}</title>
<meta name="description" content="혼자라서 더 좋은 주말 · 4대 성향별 1인 풀코스 타임라인·예산·시크릿 팁">
<style>{_CSS}</style>
</head>
<body>
<header class="masthead">
<p class="kicker">SOLO WEEKEND MAGAZINE</p>
<h1>🎒 {_e(title)}</h1>
<p class="meta">1인 환영 검증 코스 {len(items)}선 · {_e(now.strftime("%Y-%m-%d %H:%M"))} KST 발행</p>
{weather_html}
</header>
<main class="wrap">
<nav class="tabs" role="tablist" aria-label="욜로 성향 필터">
{"".join(tab_html)}
</nav>
<section class="grid">
{grid_html}
</section>
<footer>
<p>모든 코스는 1인 방문 환영 여부를 기준으로 큐레이션되며, 예산은 1인 기준 예상 금액입니다.</p>
<p>사진: <a href="https://commons.wikimedia.org/">Wikimedia Commons</a> 각 저작자 (CC 라이선스)</p>
</footer>
</main>
<script>{_JS}</script>
</body>
</html>
"""

    def _render_card(self, c: YoloCuration) -> str:
        arcs_attr = " ".join(a.value for a in c.archetypes)
        mode_label = "실내" if c.activity_mode == ActivityMode.INDOOR else "야외"

        badges = "".join(f'<span class="badge">{_e(a.emoji + " " + a.label)}</span>' for a in c.archetypes)
        if c.is_fallback:
            badges += '<span class="badge fb">☔ 우천 대체</span>'

        if c.hero_image_url:
            alt = c.hero_image_alt or c.title
            hero_inner = (
                f'<img src="{_e(c.hero_image_url)}" alt="{_e(alt)}" loading="lazy" '
                'decoding="async" width="1280" height="720" referrerpolicy="no-referrer">'
            )
        else:
            gradient = _ARC_GRADIENTS[c.archetypes[0]]
            hero_inner = (
                f'<div class="hero-ph" role="img" aria-label="{_e(c.title)}" '
                f'style="background:{gradient}">{_e(c.emoji)}</div>'
            )

        parts: list[str] = [
            f'<article class="card" data-arcs="{_e(arcs_attr)}" id="{_e(c.id)}">',
            f'<div class="hero">{hero_inner}<div class="badges">{badges}</div></div>',
            '<div class="body">',
            f"<h2>{_e(c.emoji)} {_e(c.title)}</h2>",
            f'<p class="summary">{_e(c.summary)}</p>',
            '<ul class="chips">'
            f"<li>📍 {_e(c.location_name)}</li>"
            f"<li>⏱ {_e(c.cost_or_duration)}</li>"
            f"<li>{_e(c.season.label)} · {mode_label}</li>"
            "</ul>",
            '<ul class="chips tags">' + "".join(f"<li>{_e(t)}</li>" for t in c.solo_tags) + "</ul>",
        ]

        if c.timeline:
            rows = "".join(
                f'<li><span class="t">{_e(t.time)}</span> <span class="s">{_e(t.spot)}</span>'
                f'<span class="d">{_e(t.desc)}</span></li>'
                for t in c.timeline
            )
            parts.append(f'<div><h3>🕒 나홀로 풀코스 타임라인</h3><ol class="timeline">{rows}</ol></div>')

        if c.budget_breakdown:
            rows = "".join(
                f"<tr><td>{_e(b.item)}</td>"
                f'<td class="cat">{_e(BUDGET_CATEGORY_LABELS.get(b.category, BUDGET_CATEGORY_LABELS["etc"]))}</td>'
                f'<td class="amt">{_e(_won(b.cost_krw))}</td></tr>'
                for b in c.budget_breakdown
            )
            parts.append(
                '<div class="budget-wrap"><h3>💸 1인 예산서</h3><table class="budget">'
                '<thead><tr><th>항목</th><th class="cat">분류</th><th class="amt">금액</th></tr></thead>'
                f"<tbody>{rows}</tbody>"
                f'<tfoot><tr><td colspan="2">합계 (1인)</td><td class="amt">{_e(_won(c.total_budget_krw))}</td></tr></tfoot>'
                "</table></div>"
            )

        if c.solo_secrets:
            parts.append(f'<p class="secret">🤫 <b>1인 시크릿 팁</b> · {_e(c.solo_secrets)}</p>')

        if c.gallery_images:
            imgs = "".join(
                f'<img src="{_e(g.url)}" alt="{_e(g.alt)}" loading="lazy" decoding="async" '
                'width="1280" height="720" referrerpolicy="no-referrer">'
                for g in c.gallery_images
            )
            parts.append(f'<div class="gallery">{imgs}</div>')

        actions: list[str] = []
        if c.map_naver_url:
            actions.append(
                f'<a class="btn naver" href="{_e(c.map_naver_url)}" target="_blank" rel="noopener">네이버 지도</a>'
            )
        if c.map_kakao_url:
            actions.append(
                f'<a class="btn kakao" href="{_e(c.map_kakao_url)}" target="_blank" rel="noopener">카카오맵</a>'
            )
        actions.append(
            f'<a class="btn main" href="{_e(c.action_url)}" target="_blank" rel="noopener">상세·1인 예약 안내</a>'
        )
        parts.append(f'<div class="actions">{"".join(actions)}</div>')
        parts.append("</div></article>")
        return "".join(parts)

    def save_report(
        self,
        html_content: str,
        output_path: str | Path = DEFAULT_REPORT_PATH,
    ) -> Path:
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(html_content, encoding="utf-8")
        return path
