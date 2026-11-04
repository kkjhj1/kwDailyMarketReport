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

from travel_brief.models import DestinationMeta, FlightDeal, TravelRecommendation, reconcile_man_breakdown, round_man
from travel_brief.flight_engine import FlightEngine
from travel_brief.budget_calculator import BudgetCalculator
from travel_brief.ai_prompts import AIPromptGenerator
from travel_brief.web_report import WebReportGenerator
from travel_brief.storage.db import TravelBriefDatabase
from travel_brief.channels.telegram import TelegramChannel
from travel_brief.channels.telegram_limiter import SafeTelegramDispatcher, TelegramRateLimiter


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
    return "https://kkjhj1.github.io/kwDailyMarketReport/reports/travel_latest.html"


def format_telegram_briefing(
    recommendations: list[TravelRecommendation],
    web_report_url: str = "https://kkjhj1.github.io/kwDailyMarketReport/reports/travel_latest.html",
    engine: Optional[FlightEngine] = None,
) -> str:
    """Format mobile-optimized Telegram message with concise hierarchy and <=4096 single-message guarantee."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    is_live_mode = any(r.best_deal.is_live for r in recommendations[:3])
    if is_live_mode:
        header_lines = [
            "✈️ <b>[주말 해외여행 6개월 실시간 특가 브리핑]</b>",
            f"📅 기준: {now_str} KST · 인천 직항 3박 4일 (유류·제세 포함)",
            "💡 <i>항공권: '가격 낮은 순' 1위 실측가 (상단 배너 제외)</i>",
            "💡 <i>숙소: 3~4성급 기준예산 (객실 타입별 실시간 요금 상이)</i>",
            "──────────────────",
            "🔥 <b>이번 주 실시간 가성비 BEST TOP 3</b>",
        ]
    else:
        header_lines = [
            "✈️ <b>[주말 해외여행 6개월 특가 브리핑]</b>",
            f"📅 기준: {now_str} KST · 인천 직항 3박 4일 주말 예상 밴드",
            "💡 <i>항공권: 시장 예상가 (실시간 잔여석 링크 확인)</i>",
            "💡 <i>숙소: 3~4성급 기준예산 (객실 타입별 실시간 요금 상이)</i>",
            "──────────────────",
            "🔥 <b>이번 주 가성비 BEST TOP 3</b>",
        ]

    def _build_body(include_banner_note: bool = True, include_monthly_airline: bool = True, monthly_limit: int = 6, top_limit: int = 3) -> str:
        lines = list(header_lines)
        for i, rec in enumerate(recommendations[:top_limit], 1):
            dest = rec.destination
            deal = rec.best_deal
            budget = rec.budget

            season_icon = "✨" if rec.season_status == "BEST_SEASON" else ("⚠️" if rec.season_status == "CAUTION" else "🌤️")
            if deal.is_peak_holiday and deal.price_diff_pct >= 5.0:
                deal_tag = f" [🚨 {html.escape(deal.holiday_name or '연휴 성수기')} {deal.price_diff_pct:+.0f}%]"
            elif deal.is_super_deal:
                deal_tag = f" [🔥 초특가 {deal.price_diff_pct:+.0f}%]"
            elif deal.discount_pct >= 7.0:
                deal_tag = f" [✨ 실속 {deal.price_diff_pct:+.0f}%]"
            else:
                deal_tag = ""

            chatgpt_link = AIPromptGenerator.get_chatgpt_deep_link(
                AIPromptGenerator.build_compact_prompt(dest, deal, budget)
            )

            hotel_raw = dest.recommended_hotel_name.split("(")[0].strip() if dest.recommended_hotel_name else f"{dest.name} 추천 호텔"
            hotel_name_str = html.escape(hotel_raw)
            hotel_grade_str = f" ({html.escape(dest.recommended_hotel_grade)})" if dest.recommended_hotel_grade else ""
            feat_raw = dest.recommended_hotel_features.split(",")[0].strip() if dest.recommended_hotel_features else ""
            hotel_feat_str = f" · {html.escape(feat_raw)}" if feat_raw else ""

            naver_flight_link = rec.naver_flight_url or FlightEngine.build_naver_flight_url(
                deal.origin_code, deal.destination_code, deal.departure_date, deal.return_date
            )
            hotel_search_link = rec.hotel_search_url or FlightEngine.build_naver_hotel_url(
                hotel_raw, deal.departure_date, deal.return_date
            )

            safe_flight_url = html.escape(naver_flight_link, quote=True)
            safe_hotel_url = html.escape(hotel_search_link, quote=True)
            safe_ai_url = html.escape(chatgpt_link, quote=True)

            tip_clean = dest.tips.split(",")[0].strip() if dest.tips else ""
            tip_str = f" · {html.escape(tip_clean)}" if tip_clean else ""
            safe_airline = html.escape(deal.airline)

            if deal.data_source == "LIVE_SSE":
                fare_line = f"  • 항공: <b>{html.escape(deal.exact_price_str)}</b> ({safe_airline} 직항 · {html.escape(deal.live_payment_note)})"
            elif deal.data_source == "LIVE_PG":
                fare_line = f"  • 항공: <b>{html.escape(deal.exact_price_str)}</b> ({html.escape(deal.price_band_str)} · 직항 캐시)"
            else:
                airline_disp_off = safe_airline if "직항" in safe_airline else f"{safe_airline} 직항"
                fare_line = f"  • 항공: <b>{html.escape(deal.price_band_str)}</b> ({airline_disp_off} · 유류세 포함)"

            share_1p = round_man(budget.estimated_for_two_krw / 2)
            f_man, h_man, m_man, total_1p_man = reconcile_man_breakdown(
                deal.price_min_krw, budget.hotel_total_krw, budget.meal_transit_total_krw
            )

            sched_lines = [
                f"\n<b>{i}. {html.escape(dest.name)} ({html.escape(dest.code)})</b> {season_icon}{deal_tag}",
                f"  • 일정: <b>{deal.departure_date}({deal.departure_day_of_week}) ~ {deal.return_date}({deal.return_day_of_week})</b> (3박 4일)",
            ]
            if deal.data_source == "LIVE_SSE" and (deal.exact_ret_time or deal.next_day_arrival):
                sched_lines.append(f"  • 운항: 🛫 {html.escape(deal.departure_time_slot)} / 🛬 {html.escape(deal.return_time_slot)}")
            else:
                sched_lines.append(f"  • 운항: 🛫 {html.escape(deal.departure_time_slot)}")

            sched_lines.append(fare_line)
            if include_banner_note and deal.popular_banner_note:
                sched_lines.append(f"    └ 💡 <i>네이버 상단 배너 대신 '가격 낮은 순' 1위 실측가</i>")

            visa_short = dest.visa_info.split("(")[0].strip() if dest.visa_info else "무비자"
            sched_lines.extend([
                f"  • 숙소: <b>{hotel_name_str}</b>{hotel_grade_str} (3박 기준예산 약 {h_man}만{hotel_feat_str})",
                f"  • 예산: <b>1인 약 {total_1p_man}만 원</b> (항공 {f_man}만 + 숙소 {h_man}만 + 식비 {m_man}만)",
                f"    └ 2인 동행 시: 약 {round_man(budget.estimated_for_two_krw)}만 원 (1인당 {share_1p}만)",
                f"  • 참고: {html.escape(visa_short)}{tip_str}",
                f'  👉 <a href="{safe_flight_url}">[✈️ 항공권 최저가]</a> · <a href="{safe_hotel_url}">[🏨 숙소 실시간 요금]</a> · <a href="{safe_ai_url}">[🤖 AI 맞춤일정]</a>',
            ])
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

        safe_web_url = html.escape(web_report_url, quote=True)
        lines.extend([
            "\n──────────────────",
            f'🗂️ <b>모바일·PC 상세 웹 리포트:</b>\n👉 <a href="{safe_web_url}">월별 큰글씨 카드 &amp; 6개월 전체 비교표 열기</a>',
        ])
        return "\n".join(lines)

    # Build concise, readable 6-month monthly best value summary directly in Telegram
    f_engine = engine or FlightEngine()
    matrix = f_engine.get_6month_matrix()
    all_months = sorted({m for d_map in matrix.values() for m in d_map.keys()})
    monthly_picks: list[tuple[str, DestinationMeta, FlightDeal]] = []
    if all_months:
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
            candidates.sort(reverse=True)
            chosen = next((c for c in candidates if c[2].code not in used_dests), candidates[0])
            _, _, c_dest, c_deal = chosen
            used_dests.add(c_dest.code)
            monthly_picks.append((m, c_dest, c_deal))

        if f_engine.live_naver and monthly_picks:
            verified_m_deals = f_engine.ensure_deals_sse_verified([p[2] for p in monthly_picks])
            monthly_picks = [
                (m, d_meta, v_deal)
                for (m, d_meta, _), v_deal in zip(monthly_picks, verified_m_deals)
            ]

    for top_lim, banner_opt, m_air_opt, m_lim in (
        (3, True, True, 6),
        (3, True, False, 6),
        (3, False, False, 6),
        (3, False, False, 3),
        (3, False, False, 0),
        (2, False, False, 0),
        (1, False, False, 0),
    ):
        rendered = _build_body(
            include_banner_note=banner_opt,
            include_monthly_airline=m_air_opt,
            monthly_limit=m_lim,
            top_limit=top_lim,
        )
        visible = re.sub(r'<[^>]+>', '', rendered)
        # TG-01 strict goal: HTML <= 3500 & visible <= 1800; hard limit <= 4096
        if len(rendered) <= 3500 and len(visible) <= 1800:
            return rendered
        if len(rendered) <= 4096:
            last_valid = rendered
    if 'last_valid' in locals():
        return last_valid
    raise ValueError(f"Formatted Telegram message ({len(rendered)} chars) exceeds Telegram 4096-char limit")


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

    # 1. Warm cache & get Top Recommendations first
    all_deals = engine.search_deals(dest_code=dest_code, months_ahead=6)
    recommendations = BudgetCalculator.rank_recommendations(
        destinations=engine.destinations,
        deals=all_deals,
        max_budget_per_person=max_budget,
        top_n=5,
    )
    if effective_live and recommendations:
        upgraded = engine.ensure_deals_sse_verified([r.best_deal for r in recommendations])
        all_deals = engine.search_deals(dest_code=dest_code, months_ahead=6)
        recommendations = BudgetCalculator.rank_recommendations(
            destinations=engine.destinations,
            deals=all_deals,
            max_budget_per_person=max_budget,
            top_n=5,
        )

    # 2. Generate Web Reports (reuses singleflight memory cache in 0.00s!)
    if output_dir is not None:
        travel_reports_dir = Path(output_dir)
    elif demo or dry_run:
        travel_reports_dir = BASE_DIR / "scratch" / "reports_demo" / "travel"
    else:
        travel_reports_dir = BASE_DIR / "reports" / "travel"
    travel_reports_dir.mkdir(parents=True, exist_ok=True)
    report_gen = WebReportGenerator(engine=engine)
    dated_file, latest_file = report_gen.save_reports(output_dir=travel_reports_dir)

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

    # 3. Format Telegram Message
    report_link = web_report_url or resolve_travel_report_url()
    msg_text = format_telegram_briefing(recommendations, web_report_url=report_link, engine=engine)

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

    print(f"🚀 Starting Travel Pathfinder Interactive Poller (v2.2, live_naver={live_naver})...")
    db_path = BASE_DIR / "alerts.sqlite3"
    db = TravelBriefDatabase(db_path=db_path)
    engine = FlightEngine(live_naver=live_naver)
    from travel_brief.interactive import TravelInteractiveHandler
    handler = TravelInteractiveHandler(bot_token=token, engine=engine)
    dispatcher = SafeTelegramDispatcher()
    last_offset = db.get_telegram_offset()

    import json
    import urllib.request

    def _async_send_welcome(chat_id: str, sender_name: str) -> None:
        try:
            all_deals = engine.search_deals(dest_code=None, months_ahead=6)
            recs = BudgetCalculator.rank_recommendations(
                destinations=engine.destinations,
                deals=all_deals,
                top_n=3,
            )
            if not recs:
                return
            welcome_text = format_telegram_briefing(
                recommendations=recs[:3],
                web_report_url=handler.report_url,
                engine=engine,
            )
            kb = handler.build_inline_keyboard()
            dispatcher.send_text_to_recipient(
                tg=handler.tg,
                chat_id=chat_id,
                text=welcome_text,
                parse_mode="HTML",
                reply_markup=kb,
            )
        except Exception as exc:
            logger.error("Async welcome dispatch failed for %s: %s", chat_id, exc)

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

                    if "callback_query" in item:
                        cq = item["callback_query"]
                        cq_id = str(cq.get("id", ""))
                        sender = cq.get("from", {}) or {}
                        msg = cq.get("message", {}) or {}
                        chat = msg.get("chat", {}) or sender
                        chat_id = str(chat.get("id") or sender.get("id") or "")
                        msg_id = int(msg.get("message_id") or 0)
                        cb_data = str(cq.get("data") or "")

                        if cb_data.startswith("trv:"):
                            handler.handle_callback_query(
                                cq_id=cq_id,
                                chat_id=chat_id,
                                message_id=msg_id,
                                callback_data=cb_data,
                                from_user=sender,
                            )

                    elif "message" in item:
                        msg = item["message"]
                        chat = msg.get("chat", {}) or {}
                        chat_id = str(chat.get("id") or "")
                        text = (msg.get("text") or "").strip().lower()
                        sender = msg.get("from", {}) or chat
                        sender_name = chat.get("title") or chat.get("first_name") or f"사용자_{chat_id[-4:]}"

                        if not chat_id or not text:
                            continue

                        if text in {"/stop", "/구독취소", "/unsubscribe"}:
                            db.deactivate_recipient(chat_id, reason="USER_STOP_COMMAND")
                            dispatcher.send_text_to_recipient(
                                tg=handler.tg,
                                chat_id=chat_id,
                                text="👋 여행 특가 구독이 해지되었습니다. 언제든 <code>/start</code>를 누르면 다시 받아보실 수 있습니다.",
                                parse_mode="HTML",
                            )
                        elif text in {"/start", "/구독", "안녕", "/여행", "/today", "/특가"}:
                            db.add_recipient(chat_id, name=sender_name, topic="TRAVEL")
                            # Offload welcome brief to worker pool so polling loop NEVER blocks (C-B)
                            handler.worker_pool.submit(_async_send_welcome, chat_id, sender_name)
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
