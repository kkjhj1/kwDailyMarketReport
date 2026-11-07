"""YOLO Bot (욜로봇) Telegram Bot Runner.
Supports:
1. `--demo`: Token-free console walkthrough on an in-memory DB (no file writes, [TG-06]).
2. `--seed-sample`: Seeds the verified solo curations into SQLite.
3. `--poll`: Long-polling interactive bot.
4. `--dispatch-weekly`: Weekly solo curation briefing (idempotent per ISO week, [TG-05]).
   Add `--dry-run` to preview messages without calling Telegram.
5. `--generate-report [PATH]`: v2.0 visual web magazine (default `reports/yolo_latest.html`),
   rendered from the seed catalog via a `:memory:` DB round-trip (no SQLite file writes).
"""
from __future__ import annotations

import argparse
from datetime import date
import logging
import os
from pathlib import Path
import sys
import time

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

env_file = BASE_DIR / ".env"
if env_file.exists():
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'").strip('"'))

from yolo_brief.channels.telegram import YoloTelegramChannel
from yolo_brief.channels.telegram_card import (
    MAX_HTML_CHARS,
    MAX_VISIBLE_CHARS,
    measure_message,
    render_curation_card,
    render_onboarding_card,
)
from yolo_brief.channels.telegram_limiter import (
    SafeTelegramDispatcher,
    TelegramRateLimiter,
)
from yolo_brief.fixtures.seed_curations import SEED_CURATIONS
from yolo_brief.interactive import YoloInteractiveBot
from yolo_brief.matching import match_curations
from yolo_brief.models import (
    Season,
    SoloArchetype,
    WeatherClass,
    WeatherSnapshot,
)
from yolo_brief.storage.db import YoloDatabase
from yolo_brief.web_report import DEFAULT_REPORT_PATH, YoloWebReportGenerator

DEFAULT_REPORT_OUTPUT = BASE_DIR / DEFAULT_REPORT_PATH


def generate_report(
    output_path: str | Path = DEFAULT_REPORT_OUTPUT,
    season: Season | None = None,
    weather_snapshot: WeatherSnapshot | None = None,
) -> Path:
    """Renders the v2.0 web magazine from SEED_CURATIONS through a `:memory:` DB round-trip."""
    db = YoloDatabase(":memory:")
    try:
        db.seed_curations(SEED_CURATIONS)
        curations = db.list_active_curations()
    finally:
        db.close()
    generator = YoloWebReportGenerator()
    html_text = generator.render_html(curations, season=season, weather_snapshot=weather_snapshot)
    return generator.save_report(html_text, output_path)


def run_demo(ref_date: date = date(2026, 10, 17)) -> int:
    """[TG-06] Token-free console walkthrough using strictly `:memory:` SQLite."""
    db = YoloDatabase(":memory:")
    seeded = db.seed_curations(SEED_CURATIONS)
    channel = YoloTelegramChannel(token="", dry_run=True)
    limiter = TelegramRateLimiter(
        max_global_rate=1000.0, min_chat_interval=0.0, sleep=lambda _: None
    )
    dispatcher = SafeTelegramDispatcher(rate_limiter=limiter, sleep=lambda _: None)
    bot = YoloInteractiveBot(db=db, channel=channel, dispatcher=dispatcher)

    print("=" * 72)
    print(f"🎯 [YOLO Bot Demo] Seeded {seeded} solo-certified curations in :memory:")
    print("=" * 72)

    # 1. Onboarding walkthrough
    bot.handle_update(
        {"message": {"chat": {"id": "demo_user_1"}, "text": "/start"}},
        ref_date=ref_date,
    )
    onboard_text = channel.call_log[-1][1]["text"]
    vis_len, html_len = measure_message(onboard_text)
    print(f"\n[Step 1] /start Onboarding Card (visible={vis_len}, html={html_len}):")
    print(onboard_text)

    # 2. Walk through all 4 Solo Archetypes under CLEAR weather
    clear_snap = WeatherSnapshot(
        weather_class=WeatherClass.CLEAR, pop_percent=10, feels_like_c=19.0
    )
    for idx, arc in enumerate(SoloArchetype, start=1):
        chat_id = f"demo_arc_{idx}"
        bot.handle_update(
            {
                "callback_query": {
                    "id": f"cb_clear_{idx}",
                    "data": f"yolo:set:{arc.value}",
                    "message": {"chat": {"id": chat_id}, "message_id": 100 + idx},
                }
            },
            weather_override=clear_snap,
            ref_date=ref_date,
        )
        card_text = channel.call_log[-1][1]["text"]
        v_len, h_len = measure_message(card_text)
        assert v_len <= MAX_VISIBLE_CHARS and h_len <= MAX_HTML_CHARS
        print("-" * 72)
        print(f"[Step 2.{idx}] {arc.emoji} {arc.label} (CLEAR, visible={v_len}, html={h_len}):")
        print(card_text)

    # 3. Rainy Day Fallback walkthrough for ACTIVE_CHALLENGER
    bot.handle_update(
        {
            "callback_query": {
                "id": "cb_rain_preview",
                "data": "yolo:view:rain",
                "message": {"chat": {"id": "demo_arc_1"}, "message_id": 201},
            }
        },
        ref_date=ref_date,
    )
    rain_text = channel.call_log[-1][1]["text"]
    rv_len, rh_len = measure_message(rain_text)
    print("-" * 72)
    print(f"[Step 3] ☔ Rainy Day Fallback Card (visible={rv_len}, html={rh_len}):")
    print(rain_text)

    # 4. Weekly Dispatch Idempotency verification
    stats_1 = bot.dispatch_weekly_briefings(snapshot=clear_snap, ref_date=ref_date)
    stats_2 = bot.dispatch_weekly_briefings(snapshot=clear_snap, ref_date=ref_date)
    print("-" * 72)
    print(f"[Step 4] Weekly Dispatch #1: {stats_1}")
    print(f"[Step 4] Weekly Dispatch #2 (Idempotent Dedupe): {stats_2}")
    print("=" * 72)

    db.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="YOLO Bot (욜로봇) Runner")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="Run token-free console demo on :memory: DB ([TG-06])",
    )
    parser.add_argument(
        "--seed-sample",
        action="store_true",
        help="Seed verified solo curations into SQLite DB",
    )
    parser.add_argument(
        "--poll",
        action="store_true",
        help="Run interactive Telegram long-polling loop",
    )
    parser.add_argument(
        "--dispatch-weekly",
        action="store_true",
        help="Dispatch weekly solo curation briefings",
    )
    parser.add_argument(
        "--test-send",
        nargs="?",
        const="",
        help="Send a test onboarding/curation card to the specified chat_id (or YOLO_TELEGRAM_CHAT_ID)",
    )
    parser.add_argument(
        "--generate-report",
        nargs="?",
        const=str(DEFAULT_REPORT_OUTPUT),
        default=None,
        metavar="PATH",
        help="Generate v2.0 visual web magazine HTML (default: reports/yolo_latest.html)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not make network calls to Telegram API",
    )
    parser.add_argument(
        "--db-path",
        default=os.environ.get("YOLO_DB_PATH", str(BASE_DIR / "yolo_bot.sqlite3")),
        help="Path to SQLite database file (ignored in --demo)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.demo:
        return run_demo()

    if args.generate_report is not None:
        out_path = generate_report(args.generate_report)
        print(f"[YOLO Bot] Web magazine generated: {out_path}")
        return 0

    db = YoloDatabase(args.db_path)
    if args.seed_sample:
        count = db.seed_curations(SEED_CURATIONS)
        print(f"[YOLO Bot] Seeded {count} curations into {args.db_path}")
        if not (args.poll or args.dispatch_weekly):
            return 0

    if not db.list_active_curations():
        db.seed_curations(SEED_CURATIONS)

    channel = YoloTelegramChannel(dry_run=args.dry_run)
    bot = YoloInteractiveBot(db=db, channel=channel)

    if args.test_send is not None:
        target_chat = (
            args.test_send
            or os.environ.get("YOLO_TELEGRAM_CHAT_ID")
            or os.environ.get("TELEGRAM_CHAT_ID")
            or ""
        ).strip()
        if not target_chat:
            print("[YOLO Bot] Error: No chat_id provided and YOLO_TELEGRAM_CHAT_ID is not set.")
            return 1
        user = db.get_user(target_chat) or db.upsert_onboarding_user(target_chat)
        if user.archetype is None:
            card_html, markup = render_onboarding_card(user)
        else:
            curations = db.list_active_curations()
            snap = WeatherSnapshot(
                weather_class=WeatherClass.CLEAR,
                pop_percent=10,
                feels_like_c=18.0,
                pm_grade="GOOD",
            )
            match = match_curations(user, curations, snapshot=snap)
            card_html, markup = render_curation_card(user, match)
        res = channel.send_message(target_chat, card_html, reply_markup=markup)
        print(f"[YOLO Bot] Test card dispatched to {target_chat}: {res.get('ok')}")
        return 0

    if args.dispatch_weekly:
        stats = bot.dispatch_weekly_briefings()
        print(f"[YOLO Bot] Weekly dispatch result: {stats}")
        return 0

    if args.poll:
        logging.info("Starting YOLO Bot long-polling...")
        offset: int | None = None
        while True:
            try:
                updates = channel.get_updates(offset=offset, timeout=20)
                for upd in updates:
                    offset = int(upd["update_id"]) + 1
                    bot.handle_update(upd)
            except KeyboardInterrupt:
                logging.info("Stopped YOLO Bot polling.")
                break
            except Exception as exc:
                logging.error("Polling error: %s", exc)
                time.sleep(3.0)
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
