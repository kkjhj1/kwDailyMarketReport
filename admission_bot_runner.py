"""Admission Briefing Telegram Bot Runner (에듀브리프 텔레그램 봇 실행기).
Supports:
1. `--poll`: Long-polling background bot process handling /start, /todo, /grade inline buttons.
2. `--dispatch`: Dispatches grade-specific weekly briefings or ticketing alerts to subscribers.
3. `--dry-run`: Console preview of HTML cards and target recipients without sending.
4. `--seed-sample`: Seeds realistic high-end admission events into SQLite database.
"""
from __future__ import annotations
import argparse
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

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

from admission_brief.ai_curator import AICurator
from admission_brief.collector import AdmissionCollector
from admission_brief.channels.telegram import TelegramChannel, format_admission_html
from admission_brief.channels.telegram_limiter import SafeTelegramDispatcher
from admission_brief.interactive_bot import AdmissionInteractiveBot
from admission_brief.models import (
    AdmissionBriefing,
    AdmissionEvent,
    BriefingType,
    ParentUserProfile,
    TargetGrade,
)
from admission_brief.storage.db import AdmissionDatabase

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("admission_bot_runner")


def get_admission_bot_token() -> str:
    """Retrieves bot token with strict namespace isolation."""
    return (
        os.environ.get("ADMISSION_TELEGRAM_BOT_TOKEN")
        or os.environ.get("ADMISSION_BOT_TOKEN")
        or os.environ.get("EDU_TELEGRAM_BOT_TOKEN")
        or os.environ.get("TELEGRAM_BOT_TOKEN")
        or ""
    ).strip()


def get_admission_chat_id() -> str:
    """Retrieves target chat ID or default recipient."""
    return (
        os.environ.get("ADMISSION_TELEGRAM_CHAT_ID")
        or os.environ.get("ADMISSION_CHAT_ID")
        or os.environ.get("EDU_TELEGRAM_CHAT_ID")
        or os.environ.get("TELEGRAM_CHAT_ID")
        or ""
    ).strip()


def seed_sample_events(db: AdmissionDatabase) -> None:
    """Populates realistic admission events for testing."""
    sample_events = [
        AdmissionEvent(
            event_id="evt_daechi_winter_2026",
            title="[대치동 실전] 시대인재 예비고1 윈터스쿨 설명회",
            organizer="시대인재",
            target_grade=TargetGrade.MIDDLE,
            event_type=BriefingType.TICKETING_ALERT,
            event_date_str="2026-11-15 14:00",
            booking_opens_at=datetime.now() + timedelta(minutes=5),
            is_first_come=True,
            booking_url="https://sdij.com/admission/winter_notice",
            summary_3lines=[
                "2028 수능 개편 후 통합사회·통합과학 1등급 컷 예상 심층 분석",
                "주요 자사고(외대부고·하나고·상산고) 합격생들의 중3 겨울방학 주간 시간표 공개",
                "수학(상/하) 심화 및 수1 선행 권장 진도 가이드라인 제시",
            ],
            so_what="고교 진학 후 첫 중간고사 성적이 대입 수시의 70%를 결정합니다. 겨울방학 결손을 반드시 막아야 합니다.",
            action_todos=[
                "오늘 14:00 선착순 접수 오픈 시 1분 내 예약 완료",
                "자녀의 현재 수학 심화(블랙라벨/일품) 성취도 자가진단",
            ],
            credibility_score=95,
        ),
        AdmissionEvent(
            event_id="evt_seoul_edu_policy",
            title="교육부 2028 대입 전형 개편 및 의대/자율전공 확정안",
            organizer="교육부 / 서울시교육청",
            target_grade=TargetGrade.ALL,
            event_type=BriefingType.POLICY_TRANSLATE,
            event_date_str="2026-11-01 10:00",
            booking_url="https://sen.go.kr",
            summary_3lines=[
                "내신 9등급제에서 5등급제로 완화되면서 상위권 대학 '정성평가(세특)' 비중 40% 이상 확대",
                "수능 선택과목 전면 폐지 ➔ 문·이과 공통 시험으로 수학 표준점수 변별력 극대화",
                "무전공(자율전공) 선발 인원 30% 확대로 이과생의 상위권 대학 인문계열 교차지원 심화",
            ],
            so_what="내신 1등급 인원이 늘어남에 따라, 학교생활기록부 교과 세특과 탐구 역량이 실질 당락을 결정합니다.",
            action_todos=[
                "담임교사 면담 전 희망 진로 연계 세특 키워드 3가지 준비",
                "공통 수학 및 공통 과학 개념 완성도 사전 점검",
            ],
            credibility_score=100,
        ),
    ]

    for evt in sample_events:
        db.save_event(evt)
    logger.info("Seeded %d sample admission events into database.", len(sample_events))


def run_poll(channel: TelegramChannel, db: AdmissionDatabase, curator: AICurator) -> None:
    """Runs interactive long-polling bot loop."""
    try:
        channel.set_my_commands()
        logger.info("Successfully registered Telegram Official Menu Commands (setMyCommands).")
    except Exception as e:
        logger.warning("Could not register setMyCommands: %s", e)

    bot = AdmissionInteractiveBot(channel=channel, db=db, curator=curator)
    logger.info("Starting Admission Interactive Telegram Bot (polling mode)... Press Ctrl+C to stop.")
    offset = 0
    while True:
        try:
            offset = bot.poll_once(offset)
            time.sleep(0.5)
        except KeyboardInterrupt:
            logger.info("Bot polling stopped by user.")
            break
        except Exception as e:
            logger.error("Polling unexpected exception: %s", e)
            time.sleep(2.0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Admission Briefing Telegram Bot Runner")
    parser.add_argument("--poll", action="store_true", help="Start interactive long-polling bot")
    parser.add_argument("--dispatch", action="store_true", help="Dispatch briefing to registered subscribers")
    parser.add_argument("--dry-run", action="store_true", help="Preview output without making Telegram API calls")
    parser.add_argument("--seed-sample", action="store_true", help="Seed sample admission events into database")
    parser.add_argument("--collect", action="store_true", help="Run automated crawler for public & academy notices")
    parser.add_argument("--grade", type=str, default="MIDDLE", choices=["MIDDLE", "HIGH_1_2", "HIGH_3_N", "ALL"],
                        help="Target student grade for weekly briefing")
    parser.add_argument("--db-path", type=str, default="data/admission_brief.db", help="SQLite database path")
    args = parser.parse_args()

    token = get_admission_bot_token()
    db = AdmissionDatabase(db_path=args.db_path)
    curator = AICurator()
    channel = TelegramChannel(bot_token=token)

    if args.seed_sample:
        seed_sample_events(db)
        print("✅ Sample events successfully seeded.")
        return

    if args.collect:
        collector = AdmissionCollector(db=db, curator=curator)
        results = collector.run_collection()
        print(f"✅ Automated collection completed: {results['total_saved']} event(s) saved (Public: {results['public_count']}, Academy: {results['academy_count']}).")
        return

    target_grade = TargetGrade(args.grade)

    # 1. Long-polling bot mode
    if args.poll:
        if not token and not args.dry_run:
            print("❌ Error: ADMISSION_TELEGRAM_BOT_TOKEN is not configured.")
            sys.exit(1)
        run_poll(channel=channel, db=db, curator=curator)
        return

    # 2. Dispatch / Dry-run briefing mode
    briefing = curator.generate_weekly_todo_briefing(target_grade)

    if args.dry_run:
        print("\n" + "=" * 50)
        print("🔍 [DRY-RUN PREVIEW] Admission Briefing HTML Message")
        print("=" * 50)
        print(format_admission_html(briefing))
        print("=" * 50)

        subscribers = db.list_subscribers(grade=target_grade, active_only=True)
        print(f"👥 Matched subscribers for [{target_grade.label}]: {len(subscribers)} parent(s)")
        for sub in subscribers:
            print(f"  ▪ Chat ID: {sub.chat_id} | Grade: {sub.target_grade.value} | User: {sub.username}")
        print("=" * 50 + "\n")
        return

    if args.dispatch:
        if not token:
            print("❌ Error: ADMISSION_TELEGRAM_BOT_TOKEN is not configured.")
            sys.exit(1)

        env_chat_ids_str = get_admission_chat_id()
        env_recipients = [x.strip() for x in env_chat_ids_str.split(",") if x.strip()]

        subscribers = db.list_subscribers(grade=target_grade, active_only=True)
        db_recipients = [s.chat_id for s in subscribers]

        # If explicit GitHub Actions variable (EDU_TELEGRAM_CHAT_ID) is provided, prioritize it; else use DB
        if env_recipients:
            recipients = env_recipients
            print(f"📡 Using {len(recipients)} recipient(s) from GitHub Actions environment variable.")
        else:
            recipients = db_recipients

        if not recipients:
            print(f"⚠️ No active subscribers found for {target_grade.label}.")
            return

        print(f"🚀 Dispatching [{briefing.headline}] to {len(recipients)} recipient(s)...")
        dispatcher = SafeTelegramDispatcher()
        stats = dispatcher.dispatch(channel=channel, briefing=briefing, db=db, recipients=recipients)

        print("\n📊 Dispatch Results:")
        print(f"  - Total: {stats['total']}")
        print(f"  - Success: {stats['success']}")
        print(f"  - Skipped (Duplicated): {stats['skipped']}")
        print(f"  - Failed: {stats['failed']}")
        if stats["errors"]:
            print(f"  - Error details: {stats['errors']}")
        return

    parser.print_help()


if __name__ == "__main__":
    main()
