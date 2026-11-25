"""Chungyak Bot (아파트 청약 알림봇) runner.

Flags:
  --demo           Token-free console walkthrough on `:memory:` (no file / network writes, [TG-06]).
  --seed-sample    Seed sample notices (is_sample=1; never delivered by live dispatch).
  --collect        Fetch 청약홈 OpenAPI notices into the sidecar DB (needs CHUNGYAK_API_KEY).
  --poll           Interactive long-polling loop.
  --dispatch       One alert tick (custom match / 08:00 bundle / regular digest).
                   Default is DRY-RUN (plan only). Real sends need `--confirm-live` ([PROD-LIVE]).
  --confirm-live   Explicit approval for real Telegram delivery (requires token + alerts.sqlite3 gate).
  --dry-run        Force dry-run even with --confirm-live.
  --trigger        CRON_TICK (default) | STARTUP_INIT (never sends) | MANUAL.
Token: APTAPP_TELEGRAM_BOT_TOKEN -> CHUNGYAK_TELEGRAM_BOT_TOKEN -> TELEGRAM_BOT_TOKEN (never printed).
"""
from __future__ import annotations

import argparse
from datetime import datetime
import logging
import os
from pathlib import Path
import sys
import time
from typing import Optional
from zoneinfo import ZoneInfo

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

BASE_DIR = Path(__file__).resolve().parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))


def _load_env_file() -> None:
    env_file = BASE_DIR / ".env"
    if not env_file.exists():
        return
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'").strip('"'))


from chungyak_brief import DB_FILENAME  # noqa: E402
from chungyak_brief.channels.telegram import (  # noqa: E402
    ChungyakTelegramChannel,
    measure_message,
    resolve_bot_token,
    resolve_default_chat_id,
)
from chungyak_brief.collector import ChungyakCollector, sample_notices  # noqa: E402
from chungyak_brief.interactive_bot import (  # noqa: E402
    ChungyakAlertService,
    ChungyakInteractiveBot,
    build_demo_dispatcher,
)
from chungyak_brief.models import TriggerContext  # noqa: E402
from chungyak_brief.storage.db import (  # noqa: E402
    ChungyakDatabase,
    SubscriptionGateBlocked,
    load_active_chat_ids,
)

KST = ZoneInfo("Asia/Seoul")
DEFAULT_ALERTS_DB = BASE_DIR / "alerts.sqlite3"


def default_db_path(alerts_db: Path) -> Path:
    return Path(alerts_db).resolve().with_name(DB_FILENAME)


def run_demo(now_kst: Optional[datetime] = None) -> int:
    """[TG-06] Token-free walkthrough on `:memory:`: no file writes, no network."""
    db = ChungyakDatabase(":memory:")
    channel = ChungyakTelegramChannel(token="", dry_run=True)
    bot = ChungyakInteractiveBot(db=db, channel=channel, include_samples=True,
                                 today_provider=lambda: datetime(2026, 11, 24).date())
    stored = ChungyakCollector.store(db, sample_notices())
    print("=" * 72)
    print(f"🏠 [Chungyak Demo] :memory: 샘플 공고 NEW={len(stored['NEW'])}")
    print("=" * 72)

    chat = "demo_chat_1"
    bot.handle_update({"message": {"chat": {"id": chat}, "text": "/start"}})
    home = channel.call_log[-1][1]["text"]
    print(f"[1] /start (visible={measure_message(home)[0]}, html={measure_message(home)[1]})\n{home}")
    for i, data in enumerate(["cy:sido:SEL", "cy:hl:Y", "cy:hh:Y", "cy:acct:24", "cy:dep:600",
                              "cy:ry:2", "cy:price:10", "cy:sp:NW"], start=1):
        bot.handle_update({"callback_query": {"id": f"cb{i}", "data": data,
                                              "message": {"chat": {"id": chat}, "message_id": 10}}})
    bot.handle_update({"callback_query": {"id": "cb_list", "data": "cy:list",
                                          "message": {"chat": {"id": chat}, "message_id": 10}}})
    listing = channel.call_log[-1][1]["text"]
    print("-" * 72)
    print(f"[2] 진행중 공고 (visible={measure_message(listing)[0]}, html={measure_message(listing)[1]})\n{listing}")

    db.set_regular_sub(chat, True)
    db.ensure_profile("demo_chat_2")  # empty profile -> PARTIAL_UNKNOWN only (no custom alert)
    active = {chat, "demo_chat_2"}
    service = ChungyakAlertService(db=db, channel=channel, dispatcher=build_demo_dispatcher(),
                                   active_chat_ids_provider=lambda: active, include_samples=True)
    base = now_kst or datetime(2026, 11, 24, 23, 10, tzinfo=KST)
    night = service.run_tick(TriggerContext.CRON_TICK, now=base)
    print("-" * 72)
    print(f"[3] 23:10 CRON_TICK (quiet hours) custom={night['custom']}")
    restart = service.run_tick(TriggerContext.STARTUP_INIT, now=base.replace(hour=23, minute=40))
    print(f"[4] STARTUP_INIT → sends=0, custom={restart['custom']}")
    morning = service.run_tick(TriggerContext.CRON_TICK, now=datetime(2026, 11, 25, 8, 1, tzinfo=KST))
    print(f"[5] 08:01 CRON_TICK morning_bundle={morning['morning_bundle']} regular={morning['regular_daily']}")
    again = service.run_tick(TriggerContext.CRON_TICK, now=datetime(2026, 11, 25, 8, 5, tzinfo=KST))
    print(f"[6] 08:05 재실행 (멱등) morning={again['morning_bundle']} regular={again['regular_daily']} custom={again['custom']}")
    statuses: dict[str, int] = {}
    for row in db.list_deliveries():
        statuses[row["status"]] = statuses.get(row["status"], 0) + 1
    print(f"[7] 원장 상태 집계: {statuses}")
    sends = [p for m, p in channel.call_log if m == "sendMessage"]
    if sends:
        print("-" * 72)
        print(f"[8] 마지막 발송 미리보기 (visible={measure_message(sends[-1]['text'])[0]})\n{sends[-1]['text']}")
    print("=" * 72)
    db.close()
    return 0


def _gate_provider(alerts_db: Path):
    return lambda: load_active_chat_ids(alerts_db)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Chungyak Bot (아파트 청약 알림봇) Runner")
    parser.add_argument("--demo", action="store_true", help="Token-free :memory: console demo ([TG-06])")
    parser.add_argument("--seed-sample", action="store_true", help="Seed sample notices (is_sample=1)")
    parser.add_argument("--collect", action="store_true", help="Collect 청약홈 OpenAPI notices")
    parser.add_argument("--poll", action="store_true", help="Interactive Telegram long-polling")
    parser.add_argument("--dispatch", action="store_true", help="Run one alert tick (default dry-run)")
    parser.add_argument("--confirm-live", action="store_true", help="Explicit approval for real sends")
    parser.add_argument("--dry-run", action="store_true", help="Force dry-run (plan only)")
    parser.add_argument("--trigger", default="CRON_TICK", choices=[t.value for t in TriggerContext])
    parser.add_argument("--alerts-db", default=os.environ.get("ALERTS_DB_PATH", str(DEFAULT_ALERTS_DB)))
    parser.add_argument("--db-path", default=os.environ.get("CHUNGYAK_DB_PATH", ""))
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    if args.demo:
        return run_demo()

    _load_env_file()
    alerts_db = Path(args.alerts_db)
    db_path = Path(args.db_path) if args.db_path else default_db_path(alerts_db)

    if not any((args.seed_sample, args.collect, args.poll, args.dispatch)):
        parser.print_help()
        return 0

    db = ChungyakDatabase(db_path)
    try:
        if args.seed_sample:
            res = ChungyakCollector.store(db, sample_notices())
            print(f"[Chungyak] 샘플 공고 저장: NEW={len(res['NEW'])} AMENDED={len(res['AMENDED'])} "
                  f"UNCHANGED={len(res['UNCHANGED'])} (is_sample=1, 실발송 제외)")

        if args.collect:
            include_ofc = os.environ.get("CHUNGYAK_INCLUDE_OFFICETEL", "").strip() == "1"
            res = ChungyakCollector.store(db, ChungyakCollector().fetch(include_officetel=include_ofc))
            print(f"[Chungyak] 수집: NEW={len(res['NEW'])} AMENDED={len(res['AMENDED'])} UNCHANGED={len(res['UNCHANGED'])}")

        if args.dispatch:
            live = bool(args.confirm_live) and not args.dry_run
            token = resolve_bot_token()
            if live and not token:
                print("[Chungyak] BLOCKED: --confirm-live 이지만 봇 토큰이 설정되지 않았습니다.")
                return 2
            channel = ChungyakTelegramChannel(token=token if live else "", dry_run=not live)
            service = ChungyakAlertService(
                db=db, channel=channel, active_chat_ids_provider=_gate_provider(alerts_db),
                include_samples=False, dry_run=not live,
            )
            stats = service.run_tick(TriggerContext(args.trigger))
            mode = "LIVE" if live else "DRY-RUN"
            ref_chat = resolve_default_chat_id()
            print(f"[Chungyak] dispatch {mode} trigger={stats['trigger']} blocked={stats['blocked']} "
                  f"custom={stats['custom']} morning={stats['morning_bundle']} regular={stats['regular_daily']} "
                  f"planned={len(stats['planned'])} ref_chat_set={bool(ref_chat)}")
            if stats["blocked"]:
                return 3

        if args.poll:
            token = resolve_bot_token()
            channel = ChungyakTelegramChannel(token=token, dry_run=args.dry_run)

            def access(chat_id: str) -> bool:
                try:
                    return chat_id in load_active_chat_ids(alerts_db)
                except SubscriptionGateBlocked:
                    return False  # Fail-Closed

            bot = ChungyakInteractiveBot(db=db, channel=channel, access_checker=access)
            logging.info("Starting Chungyak Bot long-polling (dry_run=%s)", channel.dry_run)
            offset: Optional[int] = None
            while True:
                try:
                    for upd in channel.get_updates(offset=offset, timeout=20):
                        offset = int(upd["update_id"]) + 1
                        bot.handle_update(upd)
                    if channel.dry_run:
                        logging.info("dry-run poll: no token -> exit")
                        break
                except KeyboardInterrupt:
                    break
                except Exception as exc:
                    logging.error("Polling error: %s", type(exc).__name__)
                    time.sleep(3.0)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
