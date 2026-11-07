"""SilverCare (실버케어) Telegram Bot Runner.
Supports:
1. `--demo`: Token-free console walkthrough on an in-memory DB (no file writes, [TG-06]).
2. `--seed-sample`: Seeds the fact-checked benefit catalogue into SQLite.
3. `--poll`: Long-polling interactive bot.
4. `--dispatch-weekly`: Sunday 20:00 weekly 효도 briefing (idempotent per ISO week).
   Add `--dry-run` to preview the messages without calling Telegram.
"""
from __future__ import annotations
import argparse
import logging
import os
import sys
import time
from datetime import date
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

from silver_care.channels.telegram import SafeTelegramDispatcher, SilverTelegramChannel
from silver_care.channels.telegram_card import measure_message
from silver_care.fixtures.seed_benefits import SEED_BENEFITS
from silver_care.interactive import SilverCareInteractiveBot, cb
from silver_care.storage.db import SilverCareDatabase
from silver_care.weekly import build_weekly_messages

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("silver_bot_runner")

DEFAULT_DB_PATH = str(BASE_DIR / "silver_care.sqlite3")


def get_silver_bot_token() -> str:
    """SilverCare-only token. Deliberately no fallback to TELEGRAM_BOT_TOKEN (would hijack another bot's getUpdates)."""
    return (os.environ.get("SILVER_TELEGRAM_BOT_TOKEN") or os.environ.get("SILVER_BOT_TOKEN") or "").strip()


class _ConsoleDemoChannel:
    """Token-free stand-in for SilverTelegramChannel that prints outgoing messages."""

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
        print(f"\n(answerCallbackQuery {callback_query_id} {text!r})")
        return True


def run_demo() -> None:
    """1954년생 강원 원주 독거 어머니 ➔ 후보 리스트 ➔ 혜택 카드 ➔ 통화 대본 (in-memory)."""
    db = SilverCareDatabase(":memory:")
    db.upsert_benefits(SEED_BENEFITS)
    print(f"✅ [DEMO] Seeded {db.count_benefits()} benefit(s) into :memory: DB")
    bot = SilverCareInteractiveBot(channel=_ConsoleDemoChannel(), db=db)
    chat = {"id": 1, "first_name": "데모"}

    def tap(data: str, n: int) -> None:
        bot.handle_update({"callback_query": {"id": f"demo_cb_{n}", "data": data,
                                              "message": {"message_id": 1001, "chat": chat}}})

    bot.handle_update({"message": {"chat": chat, "text": "/start"}})
    steps = [cb("new"), cb("a", "M"), cb("dc", 1950), cb("y", 1954), cb("r", "42")]
    for n, data in enumerate(steps):
        tap(data, n)
    bot.handle_update({"message": {"chat": chat, "text": "원주시"}})
    tap(cb("l", "ALONE"), 10)
    tap(cb("i", "UNK"), 11)

    parent = db.list_parents(1)[0]
    first = db.match_for_parent(parent)[0].benefit.id
    tap(cb("d", parent.id, first, 0), 12)
    tap(cb("t", parent.id, first, 0), 13)
    tap(cb("k", parent.id, first, 0), 14)
    db.close()


def run_poll(db: SilverCareDatabase, channel: SilverTelegramChannel) -> None:
    try:
        channel.set_my_commands()
    except Exception as exc:  # noqa: BLE001
        logger.warning("setMyCommands failed: %s", exc)
    bot = SilverCareInteractiveBot(channel=channel, db=db)
    logger.info("Starting SilverCare bot (long polling)... Ctrl+C to stop.")
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


def run_dispatch_weekly(db: SilverCareDatabase, token: str, dry_run: bool) -> int:
    key, messages = build_weekly_messages(db, date.today())
    if dry_run:
        print(f"🔍 [DRY-RUN] {key} → {len(messages)} chat(s)")
        for chat_id, text in messages:
            html_len, visible_len = measure_message(text)
            print(f"\n----- chat {chat_id} html={html_len} visible={visible_len} -----\n{text}")
        return 0
    if not token:
        print("❌ SILVER_TELEGRAM_BOT_TOKEN is not configured.")
        return 1
    stats = SafeTelegramDispatcher().dispatch(SilverTelegramChannel(token), db, key, messages)
    print(f"📊 {key}: total={stats['total']} success={stats['success']} skipped={stats['skipped']} "
          f"blocked={stats['blocked']} failed={stats['failed']}")
    if stats["errors"]:
        print(f"  - errors: {stats['errors']}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="SilverCare (실버케어) Telegram Bot Runner")
    parser.add_argument("--demo", action="store_true", help="Token-free console demo (in-memory DB, no file writes)")
    parser.add_argument("--web-report", action="store_true", help="Generate reports/silver_latest.html web report")
    parser.add_argument("--seed-sample", action="store_true", help="Seed fact-checked benefits into SQLite")
    parser.add_argument("--poll", action="store_true", help="Start interactive long-polling bot")
    parser.add_argument("--dispatch-weekly", action="store_true", help="Send the weekly 효도 briefing (Sun 20:00)")
    parser.add_argument("--dry-run", action="store_true", help="With --dispatch-weekly: preview without sending")
    parser.add_argument("--db-path", default=os.environ.get("SILVER_DB_PATH", DEFAULT_DB_PATH), help="SQLite path")
    args = parser.parse_args()

    if args.demo:
        run_demo()
        return
    if args.web_report:
        from silver_care.web_report import generate_silver_web_report
        out = generate_silver_web_report(SEED_BENEFITS, BASE_DIR / "reports" / "silver_latest.html")
        print(f"✅ Generated SilverCare web report: {out}")
        return
    if not (args.seed_sample or args.poll or args.dispatch_weekly):
        parser.print_help()
        return

    db = SilverCareDatabase(args.db_path)
    if args.seed_sample:
        n = db.upsert_benefits(SEED_BENEFITS)
        print(f"✅ Seeded {n} benefit(s) into {args.db_path}")
        if not (args.poll or args.dispatch_weekly):
            return
    if db.count_benefits() == 0:
        db.upsert_benefits(SEED_BENEFITS)
        logger.info("Benefit table was empty — auto-seeded %d benefit(s).", len(SEED_BENEFITS))

    token = get_silver_bot_token()
    if args.dispatch_weekly:
        sys.exit(run_dispatch_weekly(db, token, args.dry_run))
    if args.poll:
        if not token:
            print("❌ SILVER_TELEGRAM_BOT_TOKEN is not configured.")
            sys.exit(1)
        run_poll(db, SilverTelegramChannel(token))


if __name__ == "__main__":
    main()
