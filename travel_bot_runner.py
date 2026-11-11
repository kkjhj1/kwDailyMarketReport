"""Travel briefing telegram bot runner.
Supports manual dispatch, dry-run previews, scheduled automation, budget filtering, and on-demand polling.
Integrates SafeTelegramDispatcher (25 msg/s), UserCooldownManager, Single-Flight caching, and SQLite recipient storage.
Designed for Multi-Bot Monorepo Architecture with Zero-Breaking guarantees for Market Brief.
"""
from __future__ import annotations
import re
import argparse
import html
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Set console encoding on Windows to prevent UnicodeEncodeError
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

# Load .env if present
env_file = BASE_DIR / ".env"
if env_file.exists():
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'").strip('"'))

from travel_brief.models import (
    DestinationMeta,
    FlightDeal,
    TravelRecommendation,
    TravelSnapshot,
    reconcile_man_breakdown,
    round_man,
)
from travel_brief.flight_engine import FlightEngine
from travel_brief.budget_calculator import BudgetCalculator
from travel_brief.ai_prompts import AIPromptGenerator
from travel_brief.web_report import WebReportGenerator, build_travel_snapshot
from travel_brief.storage.db import TravelBriefDatabase
from travel_brief.channels.telegram import TelegramChannel
from travel_brief.channels.telegram_limiter import SafeTelegramDispatcher, TelegramRateLimiter
from travel_brief.fx_client import FXQuote

MIN_SAVING_DISPLAY_KRW = 30_000
SHORT_HAUL_HOURS = 2.5


def get_travel_bot_token() -> str:
    """Retrieves Travel bot token with strict namespace isolation."""
    return (
        os.environ.get("TRAVEL_TELEGRAM_BOT_TOKEN")
        or os.environ.get("TRAVEL_BOT_TOKEN")
        or os.environ.get("TELEGRAM_BOT_TOKEN")
        or ""
    ).strip()


def get_travel_chat_id() -> str:
    """Retrieves Travel chat id with strict namespace isolation."""
    return (
        os.environ.get("TRAVEL_TELEGRAM_CHAT_ID")
        or os.environ.get("TRAVEL_CHAT_ID")
        or os.environ.get("TELEGRAM_CHAT_ID")
        or ""
    ).strip()


def resolve_travel_report_url() -> str:
    """Resolves web report URL targeting GitHub Pages or local preview."""
    configured = os.environ.get("TRAVEL_REPORT_BASE_URL", "").strip()
    if configured:
        return configured
    base = os.environ.get("REPORT_BASE_URL", "").strip()
    if base:
        return f"{base.rstrip('/')}/reports/travel_latest.html"
    gh_repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if "/" in gh_repo:
        owner, repo = gh_repo.split("/", 1)
        return f"https://{owner.lower()}.github.io/{repo}/reports/travel_latest.html"
    return "http://161.33.196.211:8002/reports/travel_latest.html"


TG01_MAX_HTML = 3500
TG01_MAX_VISIBLE = 1800
_TAG_RE = re.compile(r"<[^>]+>")


def visible_text_len(rendered: str) -> int:
    """Conservative visible length (tags stripped, entities counted as written)."""
    return len(_TAG_RE.sub("", rendered))


def fits_tg01(rendered: str) -> bool:
    return len(rendered) <= TG01_MAX_HTML and visible_text_len(rendered) <= TG01_MAX_VISIBLE


def _clip(raw: str, limit: int) -> str:
    """Clips plain text *before* escaping so HTML entities are never cut in half."""
    raw = raw or ""
    return raw if len(raw) <= limit else raw[: max(1, limit - 1)] + "…"


def _fmt_fx_spot(spot: float) -> str:
    return f"{spot:,.1f}" if spot >= 100 else f"{spot:,.2f}"


def format_fx_single_line(quote: Optional[FXQuote], compact: bool = False) -> str:
    """Single-city FX header line (spot + 3M signal + VND/IDR 1만 단위 보조표기)."""
    if quote is None:
        return ""
    unit = f"{quote.quote_unit}{quote.unit_name}"
    if compact:
        sig = f" {quote.signal_label.split(' ')[0]}" if quote.signal else ""
        return f"💱 {html.escape(unit)} {_fmt_fx_spot(quote.spot)}원{sig}"
    line = f"💱 <b>환율:</b> {html.escape(unit)} = {_fmt_fx_spot(quote.spot)}원"
    if quote.per_10k_krw is not None:
        line += f" (1만{html.escape(quote.unit_name)} ≈ {quote.per_10k_krw:,.0f}원)"
    if quote.signal and quote.diff_vs_3m_pct is not None:
        line += f" · 3개월 평균 대비 {quote.diff_vs_3m_pct:+.1f}% {html.escape(quote.signal_label)}"
    return line


def format_fx_summary_line(
    recommendations: list[TravelRecommendation], fx_map: Optional[dict[str, FXQuote]], compact: bool = False
) -> str:
    """ALL TOP 3 compact multi-currency summary (one line, failed currencies omitted)."""
    if not fx_map:
        return ""
    parts: list[str] = []
    seen: set[str] = set()
    for rec in recommendations[:3]:
        cur = (rec.destination.currency or "").upper()
        q = fx_map.get(cur)
        if q is None or cur in seen:
            continue
        seen.add(cur)
        name = html.escape(q.unit_name)
        if q.signal and q.diff_vs_3m_pct is not None:
            icon = q.signal_label.split(" ")[0]
            parts.append(f"{name}{icon}" if compact else f"{name} {icon}{q.diff_vs_3m_pct:+.1f}%")
        elif not compact:
            parts.append(f"{name} {_fmt_fx_spot(q.spot)}원/{q.quote_unit}")
    if not parts:
        return ""
    return ("💱 " if compact else "💱 <b>환율(3개월 평균 대비):</b> ") + " · ".join(parts)


def build_motivation_parts(dest: DestinationMeta, deal: FlightDeal, compact: bool = False) -> list[str]:
    """Measured-data motivation only (DATA-01): live savings >= 30,000원 (CURATED baseline only),
    short-haul <= 2.5h, and CURATED experience hook (FALLBACK cities never get hype)."""
    parts: list[str] = []
    if deal.is_live and dest.is_curated and deal.baseline_krw > 0:
        saving = deal.baseline_krw - deal.price_min_krw
        if saving >= MIN_SAVING_DISPLAY_KRW:
            parts.append(f"💸 평소 시세보다 약 {round_man(saving)}만 원 저렴")
    if 0 < dest.flight_time_hours <= SHORT_HAUL_HOURS:
        parts.append(f"⏱️ 비행 약 {dest.flight_time_hours:g}시간 단거리")
    if not compact and dest.is_curated and dest.experience_hook:
        parts.append(f"🎯 {html.escape(_clip(dest.experience_hook, 40))}")
    return parts


def format_telegram_briefing(
    recommendations: list[TravelRecommendation],
    web_report_url: str = "http://161.33.196.211:8002/reports/travel_latest.html",
    engine: Optional[FlightEngine] = None,
    snapshot: Optional[TravelSnapshot] = None,
    target_city: Optional[str] = None,
    fx_map: Optional[dict[str, FXQuote]] = None,
) -> str:
    """Format mobile-optimized Telegram message under the strict TG-01 caps (HTML<=3500, visible<=1800).

    `target_city`:
      - None  : scheduled/legacy briefing (full 6-month monthly summary incl. SSE verification).
      - "ALL" : on-demand all-city query (monthly summary from the already-warm deal cache, no extra SSE).
      - code  : on-demand single-city query (single-city header, no monthly matrix / SSE).
    `fx_map`: pre-fetched {currency: FXQuote} injected by the worker (no network I/O here; Fail-Open).
    """
    now_str = snapshot.generated_at if snapshot else datetime.now().strftime("%Y-%m-%d %H:%M")
    is_live_mode = any(r.best_deal.is_live for r in recommendations[:3])
    target_norm = (target_city or "").strip().upper()
    single_city_meta: Optional[DestinationMeta] = None
    if target_norm and target_norm != "ALL":
        single_city_meta = next(
            (r.destination for r in recommendations if r.destination.code.upper() == target_norm),
            None,
        )
        if single_city_meta is None and engine is not None:
            single_city_meta = engine.destinations.get(target_norm)

    hotel_note = "💡 <i>숙소: 3성급 비즈니스 기준예산 (추천호텔 실시간 요금 상이)</i>"
    if single_city_meta is not None:
        city_name = html.escape(_clip(single_city_meta.name, 24))
        city_code = html.escape(single_city_meta.code)
        hero = recommendations[0].best_deal if recommendations else None
        if hero is not None and hero.data_source == "LIVE_SSE":
            source_tag = " (네이버 실시간 관측)"
            fare_note = "💡 <i>항공권: '가격 낮은 순' 1위 실시간 관측가 (상단 배너 제외)</i>"
        elif hero is not None and hero.data_source == "LIVE_PG":
            source_tag = " (네이버 가격그래프)"
            fare_note = "💡 <i>항공권: 네이버 가격그래프 최근값 (실시간 요금 링크 확인)</i>"
        else:
            source_tag = ""
            fare_note = "💡 <i>항공권: 시장 예상가 (실시간 잔여석 링크 확인)</i>"
        title_line = f"✈️ <b>[{city_name} 주말 특가 나침반{source_tag}]</b>"
        basis_line = f"📅 기준: {now_str} KST · 인천 직항 3박 4일 (유류·제세 포함)"
        notes = [fare_note, hotel_note]
        if not single_city_meta.is_curated:
            notes.append("ℹ️ <i>기본 메타 기준: 계절·입국 조건·숙소는 링크에서 확인 필요</i>")
        top_title = f"🔥 <b>{city_name} ({city_code}) 추천 최저가 일정 TOP {min(len(recommendations), 3)}</b>"
    elif is_live_mode:
        title_line = "✈️ <b>[주말 해외여행 6개월 특가 나침반 (네이버 실시간 관측)]</b>"
        basis_line = f"📅 기준: {now_str} KST · 인천 직항 3박 4일 (유류·제세 포함)"
        notes = ["💡 <i>항공권: '가격 낮은 순' 1위 실시간 관측가 (상단 배너 제외)</i>", hotel_note]
        top_title = "🔥 <b>이번 주 실시간 가성비 BEST TOP 3</b>"
    else:
        title_line = "✈️ <b>[주말 해외여행 6개월 특가 나침반]</b>"
        basis_line = f"📅 기준: {now_str} KST · 인천 직항 3박 4일 주말 예상 밴드"
        notes = ["💡 <i>항공권: 시장 예상가 (실시간 잔여석 링크 확인)</i>", hotel_note]
        top_title = "🔥 <b>이번 주 가성비 BEST TOP 3</b>"

    fx_map = fx_map or {}

    def _fx_line(compact: bool) -> str:
        if single_city_meta is not None:
            return format_fx_single_line(fx_map.get((single_city_meta.currency or "").upper()), compact=compact)
        return format_fx_summary_line(recommendations, fx_map, compact=compact)

    safe_web_url = html.escape(web_report_url, quote=True)

    def _build_body(
        include_banner_note: bool = True,
        include_monthly_airline: bool = True,
        monthly_limit: int = 6,
        fx_mode: str = "full",
        motivation_mode: str = "full",
        include_hotel_extras: bool = True,
        include_two_person: bool = True,
    ) -> str:
        lines = [title_line, basis_line]
        if fx_mode != "none":
            fx_line = _fx_line(compact=(fx_mode == "short"))
            if fx_line:
                lines.append(fx_line)
        lines.extend(notes)
        lines.extend(["──────────────────", top_title])
        for i, rec in enumerate(recommendations[:3], 1):
            dest = rec.destination
            deal = rec.best_deal
            budget = rec.budget

            season_icon = "✨" if rec.season_status == "BEST_SEASON" else ("⚠️" if rec.season_status == "CAUTION" else "🌤️")
            if deal.is_peak_holiday and deal.price_diff_pct >= 5.0:
                deal_tag = f" [🚨 {html.escape(_clip(deal.holiday_name or '연휴 성수기', 16))} {deal.price_diff_pct:+.0f}%]"
            elif deal.is_super_deal:
                deal_tag = f" [🔥 초특가 {deal.price_diff_pct:+.0f}%]"
            elif deal.discount_pct >= 7.0:
                deal_tag = f" [✨ 실속 {deal.price_diff_pct:+.0f}%]"
            else:
                deal_tag = ""

            hotel_raw = dest.recommended_hotel_name.split("(")[0].strip() if dest.recommended_hotel_name else f"{dest.name} 추천 호텔"
            hotel_name_str = html.escape(_clip(hotel_raw, 30))
            hotel_grade_str = f" ({html.escape(_clip(dest.recommended_hotel_grade, 20))})" if dest.recommended_hotel_grade else ""
            # FALLBACK hotels always disclose "실시간 요금·재고 확인 필요"; curated ones keep the legacy line.
            feat_raw = (
                dest.recommended_hotel_features.split(",")[0].strip()
                if (dest.recommended_hotel_features and not dest.is_curated)
                else ""
            )
            hotel_feat_str = f" · {html.escape(_clip(feat_raw, 24))}" if (feat_raw and include_hotel_extras) else ""

            naver_flight_link = rec.naver_flight_url or FlightEngine.build_naver_flight_url(
                deal.origin_code, deal.destination_code, deal.departure_date, deal.return_date
            )
            hotel_search_link = rec.hotel_search_url or FlightEngine.build_naver_hotel_url(
                hotel_raw, deal.departure_date, deal.return_date
            )
            safe_flight_url = html.escape(naver_flight_link, quote=True)
            safe_hotel_url = html.escape(hotel_search_link, quote=True)

            tip_clean = dest.tips.split(",")[0].strip() if dest.tips else ""
            tip_str = f" · {html.escape(_clip(tip_clean, 40))}" if tip_clean else ""
            safe_airline = html.escape(_clip(deal.airline, 20))

            if deal.data_source == "LIVE_SSE":
                fare_line = f"  • 항공: <b>{html.escape(deal.exact_price_str)}</b> ({safe_airline} 직항 · {html.escape(deal.live_payment_note)})"
            elif deal.data_source == "LIVE_PG":
                fare_line = f"  • 항공: <b>{html.escape(deal.exact_price_str)}</b> ({safe_airline} 직항 · 최근 가격그래프)"
            else:
                airline_disp_off = safe_airline if "직항" in safe_airline else f"{safe_airline} 직항"
                fare_line = f"  • 항공: <b>{html.escape(deal.price_band_str)}</b> ({airline_disp_off} · 유류세 포함)"

            share_1p = round_man(budget.estimated_for_two_krw / 2)
            f_man, h_man, m_man, total_1p_man = reconcile_man_breakdown(
                deal.price_min_krw, budget.hotel_total_krw, budget.meal_transit_total_krw
            )

            sched_lines = [
                f"\n<b>{i}. {html.escape(_clip(dest.name, 24))} ({html.escape(dest.code)})</b> {season_icon}{deal_tag}",
                f"  • 일정: <b>{deal.departure_date}({deal.departure_day_of_week}) ~ {deal.return_date}({deal.return_day_of_week})</b> (3박 4일)",
            ]
            if deal.data_source == "LIVE_SSE" and (deal.exact_ret_time or deal.next_day_arrival):
                sched_lines.append(f"  • 운항: 🛫 {html.escape(deal.departure_time_slot)} / 🛬 {html.escape(deal.return_time_slot)}")
            else:
                sched_lines.append(f"  • 운항: 🛫 {html.escape(deal.departure_time_slot)}")

            sched_lines.append(fare_line)
            if include_banner_note and deal.popular_banner_note:
                sched_lines.append("    └ 💡 <i>네이버 상단 배너 대신 '가격 낮은 순' 1위 실측가</i>")
            if motivation_mode != "none":
                motive = build_motivation_parts(dest, deal, compact=(motivation_mode == "short"))
                if motive:
                    sched_lines.append(f"  • 포인트: {' · '.join(motive)}")

            sched_lines.extend([
                f"  • 숙소 예산: <b>{html.escape(dest.hotel_budget_segment)}</b> 3박 약 {h_man}만 원",
                f"    └ 추천 호텔: {hotel_name_str}{hotel_grade_str}{hotel_feat_str} (실제 요금 링크 확인)",
                f"  • 예산: <b>1인 약 {total_1p_man}만 원</b> (항공 {f_man}만 + 숙소 {h_man}만 + 식비 {m_man}만)",
            ])
            if include_two_person:
                sched_lines.append(f"    └ 2인 동행 시: 약 {round_man(budget.estimated_for_two_krw)}만 원 (1인당 {share_1p}만)")
            if include_hotel_extras:
                visa_short = dest.visa_info.split("(")[0].strip() if dest.visa_info else "무비자"
                sched_lines.append(f"  • 참고: {html.escape(_clip(visa_short, 30))}{tip_str}")
                chatgpt_link = AIPromptGenerator.get_chatgpt_deep_link(
                    AIPromptGenerator.build_compact_prompt(dest, deal, budget)
                )
                safe_ai_url = html.escape(chatgpt_link, quote=True)
                sched_lines.append(
                    f'  👉 <a href="{safe_flight_url}">[✈️ 항공권 최저가]</a> · <a href="{safe_hotel_url}">[🏨 숙소 실시간 요금]</a> · <a href="{safe_ai_url}">[🤖 AI 맞춤일정]</a>'
                )
            else:
                sched_lines.append(
                    f'  👉 <a href="{safe_flight_url}">[✈️ 항공권 최저가]</a> · <a href="{safe_hotel_url}">[🏨 숙소 실시간 요금]</a>'
                )
            lines.extend(sched_lines)

        if monthly_limit > 0 and all_months:
            lines.append("\n──────────────────")
            lines.append("📅 <b>월별 주말 최저가 (한눈에 요약)</b>")
            for m, c_dest, c_deal in monthly_picks[:monthly_limit]:
                dep_short = c_deal.departure_date[5:].replace("-", ".")
                m_short = f"{m[:4]}.{m[5:]}월"
                p_disp = html.escape(c_deal.exact_price_str if c_deal.is_live else c_deal.price_band_str)
                airline_disp = f" · {html.escape(c_deal.airline)}" if (include_monthly_airline and c_deal.data_source == "LIVE_SSE") else ""
                lines.append(
                    f"  • <b>{m_short}</b>: {html.escape(c_dest.name)} <b>{p_disp}</b> ({dep_short} {c_deal.departure_day_of_week}~{c_deal.return_day_of_week}{airline_disp})"
                )

        lines.extend([
            "\n──────────────────",
            f'🗂️ <b>모바일·PC 상세 웹 리포트:</b>\n👉 <a href="{safe_web_url}">월별 큰글씨 카드 &amp; 6개월 전체 비교표 열기</a>',
        ])
        return "\n".join(lines)

    def _build_slim(name_limit: int, with_links: bool, with_report: bool) -> str:
        """TOP 3 preserving slim template: city · price · schedule · flight link only."""
        lines = [title_line, basis_line, "──────────────────", top_title]
        for i, rec in enumerate(recommendations[:3], 1):
            deal = rec.best_deal
            price = deal.exact_price_str if deal.is_live else deal.price_band_str
            lines.append(
                f"\n<b>{i}. {html.escape(_clip(rec.destination.name, name_limit))} ({html.escape(rec.destination.code)})</b>"
                f" · <b>{html.escape(price)}</b>"
            )
            lines.append(
                f"  📅 {deal.departure_date}({deal.departure_day_of_week}) ~ {deal.return_date}({deal.return_day_of_week})"
            )
            if with_links:
                link = rec.naver_flight_url or FlightEngine.build_naver_flight_url(
                    deal.origin_code, deal.destination_code, deal.departure_date, deal.return_date
                )
                lines.append(f'  👉 <a href="{html.escape(link, quote=True)}">[✈️ 항공권 최저가]</a>')
        if with_report:
            lines.append(f'\n🗂️ <a href="{safe_web_url}">상세 웹 리포트 열기</a>')
        return "\n".join(lines)

    # Build concise, readable 6-month monthly best value summary directly in Telegram
    monthly_picks: list[tuple[str, DestinationMeta, FlightDeal]] = []
    all_months: list[str] = []
    if snapshot and snapshot.monthly_picks:
        monthly_picks = [(m, d_meta, deal) for m, (d_meta, deal) in snapshot.monthly_picks.items()]
        all_months = [m for m, _, _ in monthly_picks]
    elif single_city_meta is not None:
        # On-demand single-city query: skip the 6-destination matrix and its SSE calls entirely.
        pass
    elif target_norm == "ALL":
        # On-demand all-city query: reuse the deal list the caller just warmed (same cache key,
        # no network I/O on a warm cache) and skip the synchronous monthly SSE loop for responsiveness.
        f_engine = engine or FlightEngine()
        matrix = _build_month_matrix(f_engine, f_engine.search_deals(months_ahead=6, fast_live=True))
        all_months, monthly_picks = _pick_monthly_best(f_engine, matrix)
    else:
        f_engine = engine or FlightEngine()
        matrix = f_engine.get_6month_matrix()
        all_months, monthly_picks = _pick_monthly_best(f_engine, matrix)
        if f_engine.live_naver and monthly_picks:
            verified_m_deals = f_engine.ensure_deals_sse_verified([p[2] for p in monthly_picks])
            monthly_picks = [
                (m, d_meta, v_deal)
                for (m, d_meta, _), v_deal in zip(monthly_picks, verified_m_deals)
            ]

    # TG-01 trimming ladder: ① 월별/배너 → ② FX·동기부여 → ③ 호텔특징·비자·AI → ④ 2인 설명
    # → ⑤ TOP 3 보존 슬림 → ⑥ 강제 상한. Never returns a message above the caps.
    base = dict(include_banner_note=False, include_monthly_airline=False)
    no_month = dict(base, monthly_limit=0)
    ladder = (
        dict(),
        base,
        dict(base, monthly_limit=3),
        no_month,
        dict(no_month, fx_mode="short", motivation_mode="short"),
        dict(no_month, fx_mode="none", motivation_mode="none"),
        dict(no_month, fx_mode="none", motivation_mode="none", include_hotel_extras=False),
        dict(no_month, fx_mode="none", motivation_mode="none", include_hotel_extras=False, include_two_person=False),
    )
    for opts in ladder:
        rendered = _build_body(**opts)
        if fits_tg01(rendered):
            return rendered
    for name_limit, with_links, with_report in ((24, True, True), (12, True, False), (12, False, False)):
        rendered = _build_slim(name_limit, with_links, with_report)
        if fits_tg01(rendered):
            return rendered
    return "✈️ <b>[주말 특가 나침반]</b>\n특가 요약이 너무 길어 표시를 줄였습니다. 아래 버튼으로 다시 조회해 주세요."


def _build_month_matrix(
    f_engine: FlightEngine, deals: list[FlightDeal]
) -> dict[str, dict[str, FlightDeal]]:
    """[destination_code][YYYY-MM] -> cheapest deal (same rule as FlightEngine.get_6month_matrix)."""
    matrix: dict[str, dict[str, FlightDeal]] = {code: {} for code in f_engine.destinations}
    for deal in deals:
        month_key = deal.departure_date[:7]
        d_map = matrix.setdefault(deal.destination_code, {})
        if month_key not in d_map or deal.price_krw < d_map[month_key].price_krw:
            d_map[month_key] = deal
    return matrix


def _pick_monthly_best(
    f_engine: FlightEngine, matrix: dict[str, dict[str, FlightDeal]]
) -> tuple[list[str], list[tuple[str, DestinationMeta, FlightDeal]]]:
    """Picks one best-value destination per month (destination de-duplicated where possible)."""
    all_months = sorted({m for d_map in matrix.values() for m in d_map.keys()})
    monthly_picks: list[tuple[str, DestinationMeta, FlightDeal]] = []
    used_dests: set[str] = set()
    for m in all_months[:6]:
        candidates = []
        for d_code, d_map in matrix.items():
            m_deal = d_map.get(m)
            d_meta = f_engine.destinations.get(d_code)
            if m_deal and d_meta:
                eval_rec = BudgetCalculator.evaluate_deal(d_meta, m_deal)
                candidates.append((eval_rec.recommendation_score, -m_deal.price_krw, d_meta, m_deal))
        if not candidates:
            continue
        candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
        chosen = next((c for c in candidates if c[2].code not in used_dests), candidates[0])
        _, _, c_dest, c_deal = chosen
        used_dests.add(c_dest.code)
        monthly_picks.append((m, c_dest, c_deal))
    return all_months, monthly_picks


def run_briefing(
    dest_code: Optional[str] = None,
    max_budget: Optional[int] = None,
    dry_run: bool = False,
    demo: bool = False,
    recipients_override: Optional[list[str]] = None,
    web_report_url: Optional[str] = None,
    db_path: str = "alerts.sqlite3",
    live_naver: bool = False,
    output_dir: Optional[Path | str] = None,
) -> bool:
    effective_live = bool(live_naver or os.environ.get("TRAVEL_LIVE_NAVER", "").strip() == "1")
    if demo:
        dry_run = True
        scratch_dir = BASE_DIR / "scratch"
        scratch_dir.mkdir(parents=True, exist_ok=True)
        db_path = str(scratch_dir / "demo_travel.sqlite3")
        effective_live = False
    print("=" * 60)
    print(f"✈️ Travel Briefing Runner Starting... (live_naver={effective_live})")
    print("=" * 60)

    db = TravelBriefDatabase(db_path=db_path)
    engine = FlightEngine(live_naver=effective_live)
    print(f"Loaded {len(engine.destinations)} destinations metadata.")

    # 1. Warm cache & build Single Snapshot (Single Source of Truth)
    snapshot = build_travel_snapshot(engine, dest_code=dest_code, max_budget=max_budget, top_n=5)
    recommendations = snapshot.recommendations

    # 2. Generate Web Reports (from the exact same snapshot!)
    if output_dir is not None:
        travel_reports_dir = Path(output_dir)
    elif demo or dry_run:
        travel_reports_dir = BASE_DIR / "scratch" / "reports_demo" / "travel"
    else:
        travel_reports_dir = BASE_DIR / "reports" / "travel"
    travel_reports_dir.mkdir(parents=True, exist_ok=True)
    report_gen = WebReportGenerator(engine=engine)
    dated_file, latest_file = report_gen.save_reports(output_dir=travel_reports_dir, snapshot=snapshot)

    # Also save a top-level travel_latest.html for easy GitHub Pages routing
    if output_dir is None and not demo and not dry_run:
        top_travel_latest = BASE_DIR / "reports" / "travel_latest.html"
        top_travel_latest.write_text(latest_file.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"Web Reports generated: {dated_file.name}, {latest_file.name}, {top_travel_latest.name}")
    else:
        print("Demo mode: Web Report generated in scratch without touching production reports.")

    if not recommendations:
        print("❌ No matching travel recommendations found.")
        return False

    print(f"Top {len(recommendations)} recommendations selected:")
    for r in recommendations[:3]:
        disp_p = r.best_deal.exact_price_str if r.best_deal.is_live else r.best_deal.price_band_str
        print(f"  - {r.destination.name} ({disp_p} [{r.best_deal.data_source}], 1인 약 {round_man(r.budget.total_per_person_krw)}만)")

    # 3. Format Telegram Message (with version query param to prevent browser cache mismatch)
    report_link = web_report_url or resolve_travel_report_url()
    versioned_report_link = f"{report_link}?v={snapshot.version_tag}"
    msg_text = format_telegram_briefing(
        recommendations, web_report_url=versioned_report_link, engine=engine, snapshot=snapshot
    )

    if dry_run:
        print("\n" + "=" * 30 + " [DRY RUN PREVIEW] " + "=" * 30)
        print(msg_text)
        print("=" * 79)
        print("Dry run completed. No telegram messages sent.")
        return True

    # 4. Dispatch Telegram
    token = get_travel_bot_token()
    if not token:
        print("⚠️ Travel Bot Token not configured in environment or .env. Falling back to dry-run mode.")
        print("\n" + "=" * 30 + " [PREVIEW] " + "=" * 30)
        print(msg_text)
        print("=" * 79)
        return True

    # Determine recipients
    if recipients_override:
        target_recipients = recipients_override
    else:
        active_objs = db.get_active_recipients(topic="TRAVEL")
        if active_objs:
            target_recipients = [r.chat_id for r in active_objs]
        else:
            default_chat_id = get_travel_chat_id()
            if default_chat_id:
                target_recipients = [default_chat_id]
                db.add_recipient(chat_id=default_chat_id, name="여행 구독자", topic="TRAVEL")
            else:
                target_recipients = []

    target_recipients = list(dict.fromkeys(str(r).strip() for r in target_recipients if str(r).strip()))
    if not target_recipients:
        print("⚠️ No recipients found to dispatch. Run with --dry-run or configure TRAVEL_CHAT_ID.")
        return False

    print(f"📡 Dispatching to {len(target_recipients)} recipients using SafeTelegramDispatcher...")
    tg = TelegramChannel(bot_token=token)
    dispatcher = SafeTelegramDispatcher()

    # Generate inline interactive keyboard for on-demand actions
    from travel_brief.interactive import TravelInteractiveHandler
    interactive_handler = TravelInteractiveHandler(bot_token=token, engine=engine, report_url=report_link)
    keyboard = interactive_handler.build_inline_keyboard()

    success_count = 0
    for chat_id in target_recipients:
        try:
            status, msg_id, err = dispatcher.send_text_to_recipient(
                tg=tg, chat_id=chat_id, text=msg_text, parse_mode="HTML", db=db, reply_markup=keyboard
            )
        except TypeError as type_err:
            if "reply_markup" in str(type_err):
                status, msg_id, err = dispatcher.send_text_to_recipient(
                    tg=tg, chat_id=chat_id, text=msg_text, parse_mode="HTML", db=db
                )
            else:
                raise
        if status == "SENT":
            success_count += 1
            print(f"  Sent to {chat_id} (msg_id: {msg_id})")
        else:
            print(f"  ❌ Failed to send to {chat_id}: {err}")

    print(f"Dispatch finished: {success_count}/{len(target_recipients)} succeeded.")
    return success_count > 0


def run_polling_mode(demo: bool = False, live_naver: bool = True) -> None:
    """Runs long-polling daemon for on-demand interactive travel queries.
    Equipped with 0.1s callback ACK, Single-Flight caching, and decoupled background workers.
    """
    token = get_travel_bot_token()
    if demo or not token:
        print("🎮 Running Travel Bot On-Demand Poller in DEMO mode...")
        from travel_brief.interactive import TravelInteractiveHandler
        engine = FlightEngine(live_naver=False)
        handler = TravelInteractiveHandler(bot_token=token or "dummy", engine=engine)
        kb = handler.build_inline_keyboard()
        print(f"  Inline keyboard generated with {len(kb.get('inline_keyboard', []))} rows.")
        allowed, toast = handler.cooldown.check_and_acquire("demo_user_1")
        print(f"  Demo cooldown check 1: allowed={allowed}, toast='{toast}'")
        allowed_2, toast_2 = handler.cooldown.check_and_acquire("demo_user_1")
        print(f"  Demo cooldown check 2 (blocked): allowed={allowed_2}, toast='{toast_2}'")
        handler.cooldown.release("demo_user_1")
        handler.shutdown()
        print("✅ Demo On-Demand Poller verification passed cleanly.")
        return

    print(f"🚀 Starting Travel Pathfinder Interactive Poller (v2.3, live_naver={live_naver})...")
    db_path = BASE_DIR / "alerts.sqlite3"
    db = TravelBriefDatabase(db_path=db_path)
    engine = FlightEngine(live_naver=live_naver)
    from travel_brief.interactive import TravelInteractiveHandler, dispatch_travel_update
    handler = TravelInteractiveHandler(bot_token=token, engine=engine)
    last_offset = db.get_telegram_offset()

    import json
    import urllib.request

    print(f"📡 Polling active on bot (offset: {last_offset}). Press Ctrl+C to stop.")
    try:
        while True:
            url = f"https://api.telegram.org/bot{token}/getUpdates?offset={last_offset + 1}&timeout=5"
            try:
                req = urllib.request.Request(url, method="GET")
                with urllib.request.urlopen(req, timeout=10) as resp:
                    data = json.load(resp)
            except Exception as exc:
                print(f"Poller getUpdates warning: {exc}")
                time.sleep(2.0)
                continue

            updates = data.get("result", [])
            for item in updates:
                try:
                    up_id = int(item.get("update_id", 0))
                    if up_id > last_offset:
                        last_offset = up_id

                    # Single routing entry shared with DynamicBotManager (handle_callback_query /
                    # handle_text_message); subscription hooks bind this runner's recipient DB.
                    dispatch_travel_update(
                        handler,
                        item,
                        on_subscribe=lambda cid, name: db.add_recipient(cid, name=name, topic="TRAVEL"),
                        on_unsubscribe=lambda cid: db.deactivate_recipient(cid, reason="USER_STOP_COMMAND"),
                    )
                except Exception as item_exc:
                    logger.warning("Error processing update item: %s", item_exc)

            db.update_telegram_offset(last_offset)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nStopping Travel Pathfinder poller...")
    finally:
        handler.shutdown()


def main():
    parser = argparse.ArgumentParser(description="Travel Briefing Bot Runner")
    parser.add_argument("--send-brief", action="store_true", help="Generate and send travel briefing")
    parser.add_argument("--dry-run", action="store_true", help="Generate report and preview message without sending")
    parser.add_argument("--demo", action="store_true", help="Run token-free demo mode without touching production DB or reports")
    parser.add_argument("--budget", type=int, help="Filter by maximum 1-person total budget (KRW)")
    parser.add_argument("--dest", type=str, help="Target specific destination code (e.g. FUK, DAD)")
    parser.add_argument("--recipients", type=str, help="Comma-separated chat IDs override")
    parser.add_argument("--auto-schedule", action="store_true", help="Run in periodic daemon schedule mode")
    parser.add_argument("--live-naver", action="store_true", help="Fetch real-time nonstop fares from Naver Flights")
    parser.add_argument("--poll", action="store_true", help="Run in on-demand interactive polling mode")

    args = parser.parse_args()

    recipients_list = [r.strip() for r in args.recipients.split(",") if r.strip()] if args.recipients else None

    if args.poll:
        run_polling_mode(demo=args.demo, live_naver=args.live_naver)
    elif args.auto_schedule:
        print("⏰ Starting Travel Briefing daemon scheduler...")
        run_briefing(
            dest_code=args.dest,
            max_budget=args.budget,
            dry_run=args.dry_run or args.demo,
            demo=args.demo,
            recipients_override=recipients_list,
            live_naver=args.live_naver,
        )
        print("Scheduler running. Press Ctrl+C to stop.")
        while True:
            time.sleep(3600)
    else:
        run_briefing(
            dest_code=args.dest,
            max_budget=args.budget,
            dry_run=args.dry_run or args.demo,
            demo=args.demo,
            recipients_override=recipients_list,
            live_naver=args.live_naver,
        )


if __name__ == "__main__":
    main()
