"""Kids Compass (키즈 나침반) Telegram Bot Runner.
Supports:
1. `--demo`: Token-free console walkthrough on an in-memory DB (no file writes, [TG-06]).
2. `--seed-sample`: Seeds medicines, experiences, booking D-Days and moonlight hospitals into SQLite.
3. `--poll`: Long-polling interactive bot.
4. `--dispatch-weekend`: Thursday 17:00 weekend outing briefing (idempotent per ISO week).
   4~7세 → 현장직행 3-Pick (booking_required == False only) · 임신기(아이 미등록) → 태교·바우처 대시보드.
5. `--dispatch-dday`: Monday 09:00 booking-open D-Day alert (idempotent per ISO week).
   Add `--dry-run` to preview the messages without calling Telegram or writing the delivery log.
"""
from __future__ import annotations
import argparse
import logging
import os
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path

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
from kids_compass.interactive_bot import (
    KidsCompassInteractiveBot,
    build_dday_messages,
    build_weekend_messages,
    cb,
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


def run_demo() -> None:
    """홈 ➔ 임신기 대시보드 ➔ 4~7세 현장직행/전체(맑음/우천) ➔ D-Day ➔ 해열제 ➔ 달빛병원 ➔ 목요일 발송 미리보기 (in-memory)."""
    db = KidsCompassDatabase(":memory:")
    counts = seed_all(db)
    print(f"✅ [DEMO] Seeded into :memory: DB → {counts}")
    bot = KidsCompassInteractiveBot(channel=_ConsoleDemoChannel(), db=db)
    chat = {"id": 1, "first_name": "데모"}

    def tap(data: str, n: int) -> None:
        bot.handle_update({"callback_query": {"id": f"demo_cb_{n}", "data": data,
                                              "message": {"message_id": 1001, "chat": chat}}})

    due = (date.today() + timedelta(days=120)).isoformat()
    bot.handle_update({"message": {"chat": chat, "text": f"/preg 복덩이 {due}"}})
    bot.handle_update({"message": {"chat": chat, "text": "/start"}})
    steps = [
        cb("preg"), cb("exp", "a", "PRESCHOOL", "sunny", "walk"), cb("exp", "a", "PRESCHOOL", "sunny", "all"),
        cb("exp", "a", "PRESCHOOL", "rain"), cb("dday"),
        cb("fvr", "w", 11), cb("fvr", "m3"), cb("fvr", "m5", 7), cb("fvr", "x"), cb("hsp", "r", "SEL"),
    ]
    for n, data in enumerate(steps):
        tap(data, n)

    # Thursday dispatch preview: pregnancy-only subscriber + 4~7세 subscriber (in-memory, nothing sent)
    db.add_child(2, "콩이", (date.today() - timedelta(days=365 * 5)).isoformat(), 18.0)
    key, messages = build_weekend_messages(db, date.today())
    print(f"\n===== [DISPATCH PREVIEW] {key} → {len(messages)} chat(s) =====")
    for chat_id, text in messages:
        html_len, visible_len = measure_message(text)
        print(f"\n----- chat {chat_id} html={html_len} visible={visible_len} -----\n{text}")
    db.close()


def run_poll(db: KidsCompassDatabase, channel: KidsTelegramChannel) -> None:
    try:
        channel.set_my_commands()
    except Exception as exc:  # noqa: BLE001
        logger.warning("setMyCommands failed: %s", exc)
    bot = KidsCompassInteractiveBot(channel=channel, db=db)
    logger.info("Starting Kids Compass bot (long polling)... Ctrl+C to stop.")
    offset = 0
    while True:
        try:
            offset = bot.poll_once(offset)
        except KeyboardInterrupt:
            logger.info("Polling stopped by user.")
            break
        except Exception as exc:  # noqa: BLE001
            logger.error("Polling error: %s", exc)
            time.sleep(2.0)


def run_dispatch(db: KidsCompassDatabase, token: str, dry_run: bool, kind: str) -> int:
    if kind == "weekend":
        key, messages = build_weekend_messages(db, date.today())
    else:
        key, messages = build_dday_messages(db, datetime.now())
    if dry_run:
        print(f"🔍 [DRY-RUN] {key} → {len(messages)} chat(s)")
        for chat_id, text in messages:
            html_len, visible_len = measure_message(text)
            already = " (already sent)" if db.has_dispatched(key, chat_id) else ""
            print(f"\n----- chat {chat_id}{already} html={html_len} visible={visible_len} -----\n{text}")
        return 0
    if not token:
        print("❌ KIDS_TELEGRAM_BOT_TOKEN is not configured.")
        return 1
    stats = SafeTelegramDispatcher().dispatch(KidsTelegramChannel(token), db, key, messages)
    print(f"📊 {key}: total={stats['total']} sent={stats['sent']} skipped={stats['skipped']} "
          f"unknown={stats['unknown']} blocked={stats['blocked']} failed={stats['failed']}")
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
    parser.add_argument("--dry-run", action="store_true", help="With --dispatch-*: preview without sending")
    parser.add_argument("--db-path", default=os.environ.get("KIDS_DB_PATH", DEFAULT_DB_PATH), help="SQLite path")
    args = parser.parse_args()

    if args.demo:
        run_demo()
        return
    if not (args.seed_sample or args.poll or args.dispatch_weekend or args.dispatch_dday):
        parser.print_help()
        return

    db = KidsCompassDatabase(args.db_path)
    if args.seed_sample:
        print(f"✅ Seeded into {args.db_path} → {seed_all(db)}")
        if not (args.poll or args.dispatch_weekend or args.dispatch_dday):
            return
    elif db.count_experiences() == 0:
        logger.info("Catalogue was empty — auto-seeded %s", seed_all(db))
    else:
        # Booking open dates are rule-derived; refresh so D-Days never go stale. Catalogue upsert is idempotent and
        # brings new seed spots (e.g. 현장직행 / 임신기) into an existing DB.
        from kids_compass.collector import build_booking_events
        db.upsert_experiences(SEED_EXPERIENCES)
        db.upsert_booking_events(build_booking_events())

    token = get_kids_bot_token()
    if args.dispatch_weekend:
        sys.exit(run_dispatch(db, token, args.dry_run, "weekend"))
    if args.dispatch_dday:
        sys.exit(run_dispatch(db, token, args.dry_run, "dday"))
    if args.poll:
        if not token:
            print("❌ KIDS_TELEGRAM_BOT_TOKEN is not configured.")
            sys.exit(1)
        run_poll(db, KidsTelegramChannel(token))


if __name__ == "__main__":
    main()
