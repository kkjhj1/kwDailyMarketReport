"""Travel briefing telegram bot runner.
Supports manual dispatch, dry-run previews, scheduled automation, and budget filtering.
Integrates SafeTelegramDispatcher (25 msg/s) and SQLite recipient storage.
Designed for Multi-Bot Monorepo Architecture with Zero-Breaking guarantees for Market Brief.
"""
from __future__ import annotations
import argparse
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

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

from travel_brief.models import DestinationMeta, FlightDeal, TravelRecommendation
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
) -> str:
    """Format rich Telegram message using clean HTML formatting."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    lines = [
        "✈️ <b>[주말 해외여행 6개월 특가 & 예산 브리핑]</b>",
        f"📅 기준 일시: {now_str}",
        "📌 대상: 목~일 & 금~월 (3박 4일) 직장인 실속 주말 일정",
        "──────────────────────",
        "🔥 <b>이번 주 BEST 추천 TOP 3</b>",
    ]

    for i, rec in enumerate(recommendations[:3], 1):
        dest = rec.destination
        deal = rec.best_deal
        budget = rec.budget

        season_icon = "✨" if rec.season_status == "BEST_SEASON" else ("⚠️" if rec.season_status == "CAUTION" else "🌤️")
        deal_tag = f" [초특가 {deal.discount_pct:+.0f}%]" if deal.is_super_deal else ""

        chatgpt_link = AIPromptGenerator.get_chatgpt_deep_link(
            AIPromptGenerator.build_compact_prompt(dest, deal, budget)
        )

        lines.extend([
            f"\n<b>{i}. {dest.name} ({dest.code})</b> {season_icon}{deal_tag}",
            f"  • 일정: <b>{deal.departure_date}({deal.departure_day_of_week}) ~ {deal.return_date}({deal.return_day_of_week})</b> (3박 4일)",
            f"  • 항공: <b>{deal.price_krw:,}원</b> ({deal.airline} 왕복 직항)",
            f"  • 1인 총예산: <b>약 {budget.total_per_person_krw // 10000}만 원</b> (숙소 {budget.hotel_total_krw // 10000}만 + 식비/교통 {budget.meal_transit_total_krw // 10000}만)",
            f"  • 2인 총예산: 약 {budget.estimated_for_two_krw // 10000}만 원 (1실 쉐어)",
            f"  • 비자/팁: {dest.visa_info} ({dest.tips})",
            f'  👉 <a href="{chatgpt_link}">🤖 ChatGPT로 3박4일 일정 짜기</a>',
        ])

    lines.extend([
        "\n──────────────────────",
        f'📊 <b>6개월 최저가 히트맵 전체보기:</b>\n<a href="{web_report_url}">{web_report_url}</a>',
        "\n💡 <i>본 브리핑은 SafeTelegramDispatcher를 통해 안전하게 발송되었습니다.</i>",
    ])

    return "\n".join(lines)


def run_briefing(
    dest_code: Optional[str] = None,
    max_budget: Optional[int] = None,
    dry_run: bool = False,
    recipients_override: Optional[list[str]] = None,
    web_report_url: Optional[str] = None,
    db_path: str = "alerts.sqlite3",
) -> bool:
    print("=" * 60)
    print("✈️ Travel Briefing Runner Starting...")
    print("=" * 60)

    db = TravelBriefDatabase(db_path=db_path)
    engine = FlightEngine()
    print(f"Loaded {len(engine.destinations)} destinations metadata.")

    # 1. Generate Web Reports (Isolated in reports/travel/ and reports/travel_latest.html)
    travel_reports_dir = BASE_DIR / "reports" / "travel"
    travel_reports_dir.mkdir(parents=True, exist_ok=True)
    report_gen = WebReportGenerator(engine=engine)
    dated_file, latest_file = report_gen.save_reports(output_dir=travel_reports_dir)
    
    # Also save a top-level travel_latest.html for easy GitHub Pages routing
    top_travel_latest = BASE_DIR / "reports" / "travel_latest.html"
    top_travel_latest.write_text(latest_file.read_text(encoding="utf-8"), encoding="utf-8")
    print(f"Web Reports generated: {dated_file.name}, {latest_file.name}, {top_travel_latest.name}")

    # 2. Get Top Recommendations
    all_deals = engine.search_deals(dest_code=dest_code, months_ahead=6)
    recommendations = BudgetCalculator.rank_recommendations(
        destinations=engine.destinations,
        deals=all_deals,
        max_budget_per_person=max_budget,
        top_n=5,
    )

    if not recommendations:
        print("❌ No matching travel recommendations found.")
        return False

    print(f"Top {len(recommendations)} recommendations selected:")
    for r in recommendations[:3]:
        print(f"  - {r.destination.name} ({r.best_deal.price_krw:,} KRW, 1인 약 {r.budget.total_per_person_krw // 10000}만)")

    # 3. Format Telegram Message
    report_link = web_report_url or resolve_travel_report_url()
    msg_text = format_telegram_briefing(recommendations, web_report_url=report_link)

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

    if not target_recipients:
        print("⚠️ No recipients found to dispatch. Run with --dry-run or configure TRAVEL_CHAT_ID.")
        return False

    print(f"📡 Dispatching to {len(target_recipients)} recipients using SafeTelegramDispatcher...")
    tg = TelegramChannel(bot_token=token)
    dispatcher = SafeTelegramDispatcher()

    success_count = 0
    for chat_id in target_recipients:
        status, msg_id, err = dispatcher.send_text_to_recipient(
            tg=tg, chat_id=chat_id, text=msg_text, parse_mode="HTML", db=db
        )
        if status == "SENT":
            success_count += 1
            print(f"  Sent to {chat_id} (msg_id: {msg_id})")
        else:
            print(f"  ❌ Failed to send to {chat_id}: {err}")

    print(f"Dispatch finished: {success_count}/{len(target_recipients)} succeeded.")
    return success_count > 0


def main():
    parser = argparse.ArgumentParser(description="Travel Briefing Bot Runner")
    parser.add_argument("--send-brief", action="store_true", help="Generate and send travel briefing")
    parser.add_argument("--dry-run", action="store_true", help="Generate report and preview message without sending")
    parser.add_argument("--budget", type=int, help="Filter by maximum 1-person total budget (KRW)")
    parser.add_argument("--dest", type=str, help="Target specific destination code (e.g. FUK, DAD)")
    parser.add_argument("--recipients", type=str, help="Comma-separated chat IDs override")
    parser.add_argument("--auto-schedule", action="store_true", help="Run in periodic daemon schedule mode")

    args = parser.parse_args()

    recipients_list = [r.strip() for r in args.recipients.split(",") if r.strip()] if args.recipients else None

    if args.auto_schedule:
        print("⏰ Starting Travel Briefing daemon scheduler...")
        run_briefing(dest_code=args.dest, max_budget=args.budget, dry_run=args.dry_run, recipients_override=recipients_list)
        print("Scheduler running. Press Ctrl+C to stop.")
        while True:
            time.sleep(3600)
    else:
        run_briefing(
            dest_code=args.dest,
            max_budget=args.budget,
            dry_run=args.dry_run,
            recipients_override=recipients_list,
        )


if __name__ == "__main__":
    main()
