"""Kids Compass (키즈 나침반) Telegram Bot Runner.
Supports:
1. `--demo`: Token-free console walkthrough on an in-memory DB (no file writes, [TG-06]).
2. `--seed-sample`: Seeds medicines, experiences, booking D-Days and moonlight hospitals into SQLite.
3. `--poll`: Long-polling interactive bot.
4. `--dispatch-weekend`: Thursday 17:00 1:1 weekend push — registered users only, own region only (no back-fill),
   1~2 places with [가봄]/[관심없음] buttons, weather-aware (unknown forecast → indoor only, no weather wording).
5. `--dispatch-dday`: Monday 09:00 booking-open D-Day push — only bookings matching my children's age + region.
6. `--dispatch-milestone`: 검진·예방접종·임신 체크포인트 1통 통합 push with [✅ 확인 완료] buttons.
   All pushes: Silence Rule (nothing to say → nothing sent), idempotent per (user, stream, ISO week) via
   kids_sent_history; UNKNOWN (ambiguous timeout) is never resent.
   Add `--dry-run` to preview messages + buttons without calling Telegram or writing the sent history.
"""
from __future__ import annotations
import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

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

from kids_compass.channels.telegram import KidsTelegramChannel, SafeTelegramDispatcher, measure_message
from kids_compass.collector import SEED_EXPERIENCES, seed_all
from kids_compass.channels.telegram import PUSH_MESSAGE_SENTINEL
from kids_compass.interactive_bot import (
    KidsCompassInteractiveBot,
    build_d14_milestone_push_messages,
    build_dday_push_messages,
    build_milestone_push_messages,
    build_weekend_push_messages,
    cb,
    dispatch_due_dose_timers,
    iter_push_preview,
    offline_weather,
    push_window,
)
from kids_compass.storage.db import KidsCompassDatabase

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("kids_bot_runner")

DEFAULT_DB_PATH = str(BASE_DIR / "kids_compass.sqlite3")


def get_kids_bot_token() -> str:
    """Kids-only token. Deliberately no fallback to TELEGRAM_BOT_TOKEN (would hijack another bot's getUpdates)."""
    return (os.environ.get("KIDS_TELEGRAM_BOT_TOKEN") or os.environ.get("KIDS_BOT_TOKEN") or "").strip()


class _ConsoleDemoChannel:
    """Token-free stand-in for KidsTelegramChannel that prints outgoing messages."""

    def __init__(self) -> None:
        self._next_id = 1000

    def _print(self, label: str, text: str, reply_markup: dict | None = None) -> None:
        html_len, visible_len = measure_message(text)
        print(f"\n----- [{label}] html={html_len} visible={visible_len} -----")
        print(text)
        for row in (reply_markup or {}).get("inline_keyboard", []):
            print("  " + " | ".join(f"[{b['text']}]({b.get('callback_data')})" for b in row))

    def send_message(self, chat_id, text, reply_markup=None, disable_notification=False) -> int:
        self._next_id += 1
        self._print(f"sendMessage #{self._next_id}", text, reply_markup)
        return self._next_id

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None) -> bool:
        self._print(f"editMessageText #{message_id}", text, reply_markup)
        return True

    def answer_callback_query(self, callback_query_id, text="") -> bool:
        print(f"\n(answerCallbackQuery {callback_query_id})")
        return True


def _print_push_preview(db: KidsCompassDatabase, title: str, messages: list, window: str) -> None:
    print(f"\n===== [{title}] window={window} → {len(messages)} chat(s) =====")
    for m, button_lines in iter_push_preview(messages):
        html_len, visible_len = measure_message(m.text)
        status = db.get_sent_status(m.chat_id, m.stream, m.entity_type, PUSH_MESSAGE_SENTINEL, window)
        already = f" (already {status})" if status else ""
        print(f"\n----- chat {m.chat_id}{already} stream={m.stream} entities={m.entity_ids} "
              f"html={html_len} visible={visible_len} -----\n{m.text}")
        for line in button_lines:
            print("  " + line)


def run_demo() -> None:
    """미등록 홈 ➔ 대화형 아이 등록(/cancel 포함) ➔ 임신 등록 ➔ 다자녀 스위처·Pull 허브 ➔ 체중 미입력 해열제 ➔
    기존 연령별 나들이/D-Day/달빛병원 ➔ Push 3종 미리보기 (in-memory, network-free, nothing sent)."""
    db = KidsCompassDatabase(":memory:")
    counts = seed_all(db)
    print(f"✅ [DEMO] Seeded into :memory: DB → {counts}")
    bot = KidsCompassInteractiveBot(channel=_ConsoleDemoChannel(), db=db)
    chat = {"id": 1, "first_name": "데모"}

    def tap(data: str, n: int) -> None:
        bot.handle_update({"callback_query": {"id": f"demo_cb_{n}", "data": data,
                                              "message": {"message_id": 1001, "chat": chat}}})

    def say(text: str) -> None:
        bot.handle_update({"message": {"chat": chat, "text": text}})

    today = date.today()
    say("/start")                                   # 미등록 홈: 등록 버튼 최상단 + 응급 버튼
    tap(cb("ob", "c"), 0)
    say("/cancel")                                  # 어느 단계든 즉시 취소
    tap(cb("ob", "c"), 1)
    say("콩이")
    say("2099-01-01")                               # 잘못된 날짜 → 현재 단계 유지
    say((today - timedelta(days=365 * 5)).strftime("%Y%m%d"))
    tap(cb("ob", "r", "SEL"), 2)
    tap(cb("ob", "c"), 3)
    say("별이")
    say((today - timedelta(days=200)).isoformat())
    tap(cb("ob", "r", "SEL"), 4)
    tap(cb("ob", "p"), 5)
    say("복덩이")
    say((today + timedelta(days=120)).strftime("%Y%m%d"))
    tap(cb("ob", "r", "SEL"), 6)
    tap(cb("hp"), 7)                              # Step 2: 월령 자동 매칭 집콕놀이 Pull preview

    star = db.list_children(1)[1]
    steps = [
        cb("ch", "p", star.child_id), cb("hub", "pl"), cb("hub", "out", "t"), cb("hub", "med"), cb("fd"),
        cb("fd", "c", "FORBIDDEN"), cb("fd", "i", "honey"), cb("fd", "q"),
        cb("ms", "l"),
        cb("fvr"), cb("fvr", "c", star.child_id), cb("fvr", "cw", star.child_id, 7),
        cb("set"), cb("set", "m"), cb("exp", "a", "PRESCHOOL", "sunny", "walk"), cb("dday"), cb("hsp", "r", "SEL"),
    ]
    for n, data in enumerate(steps, start=10):
        tap(data, n)
    say("꿀")  # FOOD_SEARCH remains Pull-only and accepts a second query as plain text.

    # Push previews (in-memory, offline weather → indoor-only + no weather wording, nothing sent)
    window = push_window(today)
    _print_push_preview(db, "MILESTONE PUSH PREVIEW", build_milestone_push_messages(db, today), window)
    _print_push_preview(db, "WEEKEND PUSH PREVIEW",
                        build_weekend_push_messages(db, today, weather_fetcher=offline_weather), window)
    _print_push_preview(db, "D-DAY PUSH PREVIEW", build_dday_push_messages(db, datetime.now()), window)
    db.ensure_user(2)  # 미등록자 → Silence (0건)
    print(f"\n🔕 [SILENCE] 미등록 사용자 weekend push: "
          f"{[m.chat_id for m in build_weekend_push_messages(db, today, weather_fetcher=offline_weather) if m.chat_id == '2']}")
    db.close()


def run_poll(db: KidsCompassDatabase, channel: KidsTelegramChannel) -> None:
    try:
        channel.set_my_commands()
    except Exception as exc:  # noqa: BLE001
        logger.warning("setMyCommands failed: %s", exc)
    bot = KidsCompassInteractiveBot(channel=channel, db=db)
    logger.info("Starting Kids Compass bot (long polling)... Ctrl+C to stop.")
    offset = 0
    last_timer_dispatch = time.monotonic()
    while True:
        try:
            offset = bot.poll_once(offset)
            if time.monotonic() - last_timer_dispatch >= 30.0:
                try:
                    dispatch_due_dose_timers(db, channel)
                except Exception:  # noqa: BLE001 - timer delivery must not kill polling
                    logger.exception("Due dose timer dispatch failed inside poll loop")
                last_timer_dispatch = time.monotonic()
        except KeyboardInterrupt:
            logger.info("Polling stopped by user.")
            break
        except Exception as exc:  # noqa: BLE001
            logger.error("Polling error: %s", exc)
            time.sleep(2.0)


def build_push_messages(db: KidsCompassDatabase, kind: str, dry_run: bool = False) -> list:
    now = datetime.now()
    if kind == "weekend":
        # dry-run renders placeholder feedback tokens (no recommendation rows written)
        return build_weekend_push_messages(db, now.date(), now=now, issue_tokens=not dry_run)
    if kind == "milestone":
        return build_milestone_push_messages(db, now.date())
    return build_dday_push_messages(db, now)


def run_dispatch(db: KidsCompassDatabase, token: str, dry_run: bool, kind: str) -> int:
    window = push_window(date.today())
    if not dry_run and not token:
        print("❌ KIDS_TELEGRAM_BOT_TOKEN is not configured.")
        return 1
    messages = build_push_messages(db, kind, dry_run=dry_run)
    d14_messages = build_d14_milestone_push_messages(db, date.today()) if kind == "milestone" else []
    if dry_run:
        _print_push_preview(db, f"DRY-RUN {kind}", messages, window)
        if d14_messages:
            _print_push_preview(db, "DRY-RUN milestone_d14", d14_messages, "D14")
        return 0
    dispatcher = SafeTelegramDispatcher()
    channel = KidsTelegramChannel(token)
    stats = dispatcher.dispatch_push_messages(channel, db, messages, window)
    if d14_messages:
        d14_stats = dispatcher.dispatch_push_messages(channel, db, d14_messages, "D14")
        for key in ("total", "sent", "skipped", "unknown", "blocked", "failed", "invalid"):
            stats[key] = stats.get(key, 0) + d14_stats.get(key, 0)
        stats["errors"].update(d14_stats.get("errors", {}))
    print(f"📊 kids_{kind}:{window}: total={stats['total']} sent={stats['sent']} skipped={stats['skipped']} "
          f"unknown={stats['unknown']} blocked={stats['blocked']} failed={stats['failed']} invalid={stats['invalid']}")
    if stats["errors"]:
        print(f"  - errors: {stats['errors']}")
    return 0


def _print_timer_preview(db: KidsCompassDatabase, now: Optional[datetime] = None) -> None:
    """Read-only timer preview used by --dispatch-timers --dry-run."""
    now = now or datetime.now()
    rows = db.conn.execute(
        "SELECT timer_id, telegram_user_id, child_id, given_ingredient, suggested_ingredient, due_at, expires_at "
        "FROM kids_dose_timers WHERE status = 'PENDING' ORDER BY due_at, timer_id"
    ).fetchall()
    print(f"🔎 TIMER DRY-RUN: pending={len(rows)} now={now:%Y-%m-%d %H:%M:%S}")
    for row in rows:
        print(f"  timer={row['timer_id']} chat={row['telegram_user_id']} child={row['child_id']} "
              f"{row['given_ingredient']}→{row['suggested_ingredient']} due={row['due_at']} expires={row['expires_at']}")


def run_timer_dispatch(db: KidsCompassDatabase, token: str, dry_run: bool) -> int:
    if dry_run:
        _print_timer_preview(db)
        return 0
    if not token:
        print("❌ KIDS_TELEGRAM_BOT_TOKEN is not configured.")
        return 1
    stats = dispatch_due_dose_timers(db, KidsTelegramChannel(token))
    print(f"📊 kids_timers: total={stats['total']} sent={stats['sent']} unknown={stats['unknown']} "
          f"blocked={stats['blocked']} failed={stats['failed']}")
    if stats["errors"]:
        print(f"  - errors: {stats['errors']}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Kids Compass (키즈 나침반) Telegram Bot Runner")
    parser.add_argument("--demo", action="store_true", help="Token-free console demo (in-memory DB, no file writes)")
    parser.add_argument("--seed-sample", action="store_true", help="Seed medicines/experiences/D-Days/hospitals")
    parser.add_argument("--poll", action="store_true", help="Start interactive long-polling bot")
    parser.add_argument("--dispatch-weekend", action="store_true", help="Send the weekend outing briefing (Thu 17:00)")
    parser.add_argument("--dispatch-dday", action="store_true", help="Send the booking-open D-Day alert (Mon 09:00)")
    parser.add_argument("--dispatch-milestone", action="store_true",
                        help="Send the 검진·예방접종·임신 checkpoint push (one message per user)")
    parser.add_argument("--dispatch-timers", action="store_true",
                        help="Send due fever cross-dosing timers")
    parser.add_argument("--import-hospitals", metavar="PATH",
                        help="Import E-gen hospital XML/JSON/CSV and opening hours")
    parser.add_argument("--dry-run", action="store_true", help="With --dispatch-*: preview without sending")
    parser.add_argument("--db-path", default=os.environ.get("KIDS_DB_PATH", DEFAULT_DB_PATH), help="SQLite path")
    args = parser.parse_args()

    if args.demo:
        run_demo()
        return
    dispatching = args.dispatch_weekend or args.dispatch_dday or args.dispatch_milestone or args.dispatch_timers
    if not (args.seed_sample or args.poll or dispatching or args.import_hospitals):
        parser.print_help()
        return

    db = KidsCompassDatabase(args.db_path)
    if args.import_hospitals:
        from kids_compass.hospitals_importer import import_hospitals_file
        result = import_hospitals_file(db, args.import_hospitals)
        print(f"✅ Imported hospitals from {args.import_hospitals} → {result}")
        if not (args.seed_sample or args.poll or dispatching):
            db.close()
            return
    if args.seed_sample:
        print(f"✅ Seeded into {args.db_path} → {seed_all(db)}")
        if not (args.poll or dispatching):
            return
    elif db.count_experiences() == 0:
        logger.info("Catalogue was empty — auto-seeded %s", seed_all(db))
    else:
        # Booking open dates are rule-derived; refresh so D-Days never go stale. Catalogue upsert is idempotent and
        # brings new seed spots (e.g. 현장직행 / 임신기) into an existing DB.
        from kids_compass.collector import SEED_MILESTONES, build_booking_events
        db.upsert_experiences(SEED_EXPERIENCES)
        db.upsert_booking_events(build_booking_events())
        db.upsert_milestone_definitions(SEED_MILESTONES)

    token = get_kids_bot_token()
    if args.dispatch_weekend:
        sys.exit(run_dispatch(db, token, args.dry_run, "weekend"))
    if args.dispatch_dday:
        sys.exit(run_dispatch(db, token, args.dry_run, "dday"))
    if args.dispatch_milestone:
        sys.exit(run_dispatch(db, token, args.dry_run, "milestone"))
    if args.dispatch_timers:
        sys.exit(run_timer_dispatch(db, token, args.dry_run))
    if args.poll:
        if not token:
            print("❌ KIDS_TELEGRAM_BOT_TOKEN is not configured.")
            sys.exit(1)
        run_poll(db, KidsTelegramChannel(token))


if __name__ == "__main__":
    main()
