"""Portal Hub Telegram Bot Runner (v2.0).
Equipped with:
1. Category-based 2-Tap Discovery.
2. Instant Natural Language Keyword Search Routing.
3. Persistent Mobile 2x2 Reply Keyboard.
4. Direct alerts.sqlite3 Real-time Sync.
"""
from __future__ import annotations
import argparse
from datetime import datetime
import html
import logging
import os
import sys
import time
from pathlib import Path

# Fix Windows console encoding
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Load .env
env_file = ROOT_DIR / ".env"
if env_file.exists():
    with open(env_file, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'").strip('"'))

from portal_hub import (
    BotCategory,
    BotServiceType,
    PortalDatabase,
    TelegramPortalClient,
    UserProfile,
    UserTier,
    handle_callback_query,
    render_category_home_view,
    render_category_view,
    render_main_menu,
    render_search_results_view,
    render_subscription_view,
    render_tier_benefits_view,
)
from portal_hub.interactive import USER_STATES, build_persistent_reply_keyboard, build_slot_gauge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("portal_runner")


def get_portal_token() -> str:
    token = os.environ.get("PORTAL_TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    return token.strip()


def run_polling(token: str) -> None:
    """Runs long-polling loop handling persistent reply keyboards, slash commands, search queries, and inline callbacks."""
    if not token:
        logger.error("❌ PORTAL_TELEGRAM_BOT_TOKEN 이 설정되지 않았습니다.")
        return

    client = TelegramPortalClient(token)
    db = PortalDatabase()

    # Register blue menu button commands
    client.set_my_commands([
        {"command": "start", "description": "🏠 메인 포털 홈"},
        {"command": "subs", "description": "📋 내 구독 센터"},
        {"command": "category", "description": "📂 카테고리 둘러보기"},
        {"command": "tiers", "description": "💎 등급 혜택 안내"},
        {"command": "help", "description": "❓ 이용 도움말"},
    ])

    logger.info("🚀 [Portal Hub Bot v2.0] 롱폴링 시작... (카테고리 2-Tap & 키워드 검색 엔진 탑재)")

    offset = 0
    reply_kb = build_persistent_reply_keyboard()

    while True:
        try:
            updates = client.get_updates(offset=offset, timeout=3)
            for update in updates:
                offset = update["update_id"] + 1

                # 1. Inline Button Clicks (Callback Queries)
                if "callback_query" in update:
                    cq = update["callback_query"]
                    logger.info("🔘 [버튼 터치] %s (유저: %s)", cq.get("data"), cq.get("from", {}).get("first_name"))
                    try:
                        handle_callback_query(cq, db, client)
                    except Exception as err:
                        logger.exception("Callback error on %s: %s", cq.get("data"), err)
                    continue

                # 2. Text Messages & Persistent Bottom Keyboard Touch & Natural Language Keyword Search
                if "message" in update:
                    msg = update["message"]
                    chat_id = msg["chat"]["id"]
                    text = msg.get("text", "").strip()
                    user_info = msg.get("from", {})
                    user_id = user_info.get("id", chat_id)
                    logger.info("📩 [메시지 수신] '%s' (유저: %s, ID: %s)", text, user_info.get("first_name"), user_id)

                    user = db.get_or_create_user(
                        user_id=user_id,
                        chat_id=chat_id,
                        username=user_info.get("username"),
                        first_name=user_info.get("first_name"),
                    )

                    clean_text = text.lower()

                    # Handle Awaiting Inquiry State
                    if USER_STATES.get(user_id) == "AWAITING_INQUIRY" and not clean_text.startswith("/"):
                        USER_STATES.pop(user_id, None)
                        admin_chat = os.environ.get("PORTAL_TELEGRAM_CHAT_ID") or os.environ.get("TELEGRAM_CHAT_ID")
                        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        u_name = html.escape(user.first_name or user.username or "회원")
                        u_handle = f"@{user.username}" if user.username else "없음"

                        if admin_chat:
                            admin_forward = (
                                f"📩 <b>[1:1 고객 문의 접수]</b>\n"
                                f"━━━━━━━━━━━━━━━━━━━━\n"
                                f"• 문의자: <b>{u_name}</b> (ID: <code>{user_id}</code>)\n"
                                f"• 핸들: {u_handle}\n"
                                f"• 회원 등급: {user.tier.display_name}\n"
                                f"• 접수 시각: <code>{now_str}</code>\n"
                                f"━━━━━━━━━━━━━━━━━━━━\n"
                                f"<b>【문의 내용】</b>\n"
                                f"<i>{html.escape(text)}</i>\n"
                                f"━━━━━━━━━━━━━━━━━━━━"
                            )
                            try:
                                client.send_message(admin_chat, admin_forward)
                            except Exception as e:
                                logger.warning("Failed forwarding inquiry to admin: %s", e)

                        ack_text = (
                            f"✅ <b>문의사항이 운영팀에 정상 전달되었습니다!</b>\n"
                            f"━━━━━━━━━━━━━━━━━━━━\n"
                            f"작성하신 내용:\n"
                            f"<i>\"{html.escape(text)}\"</i>\n\n"
                            f"운영팀에서 확인 후 빠르게 조치 및 안내해 드리겠습니다. 감사합니다. 🙏\n"
                            f"━━━━━━━━━━━━━━━━━━━━"
                        )
                        ack_markup = {
                            "inline_keyboard": [
                                [{"text": "🏠 메인 홈으로", "callback_data": "nav:main"}]
                            ]
                        }
                        client.send_message(chat_id, ack_text, reply_markup=ack_markup)
                        continue

                    # Clear awaiting state if user clicked command or persistent menu
                    USER_STATES.pop(user_id, None)

                    # Handle Fixed Commands & Persistent Keyboard Texts
                    if clean_text in ("/start", "/menu", "홈", "메인"):
                        reply_text, markup = render_main_menu(user)
                        client.send_message(chat_id, "👇 아래 메뉴를 선택하세요.", reply_markup=reply_kb)
                        client.send_message(chat_id, reply_text, reply_markup=markup)

                    elif clean_text in ("/subs", "/mysubs", "📋 내 구독 관리", "내 구독 관리", "내구독"):
                        subs = db.get_user_subscriptions(user_id)
                        reply_text, markup = render_subscription_view(user, subs)
                        client.send_message(chat_id, reply_text, reply_markup=markup)

                    elif clean_text in ("/category", "/cats", "📂 카테고리 둘러보기", "카테고리", "둘러보기"):
                        reply_text, markup = render_category_home_view()
                        client.send_message(chat_id, reply_text, reply_markup=markup)

                    elif clean_text in ("/tiers", "/upgrade", "💎 내 등급 & 혜택", "내 등급 & 혜택", "등급"):
                        reply_text, markup = render_tier_benefits_view(user)
                        client.send_message(chat_id, reply_text, reply_markup=markup)

                    elif clean_text in ("/help", "❓ 이용 도움말", "도움말", "안내"):
                        from portal_hub.interactive import render_help_view
                        reply_text, markup = render_help_view()
                        client.send_message(chat_id, reply_text, reply_markup=markup)

                    else:
                        # 3. Natural Language Keyword Search Routing!
                        matched_bots = db.search_bots(clean_text)
                        subs = db.get_user_subscriptions(user_id)
                        reply_text, markup = render_search_results_view(text, matched_bots, user, subs)
                        client.send_message(chat_id, reply_text, reply_markup=markup)

        except KeyboardInterrupt:
            logger.info("🛑 봇이 종료되었습니다.")
            break
        except Exception as e:
            logger.error("Polling loop error: %s", e)
            time.sleep(2)


def main() -> None:
    parser = argparse.ArgumentParser(description="Main Hub Portal Telegram Bot Runner v2.0")
    parser.add_argument("--poll", action="store_true", help="Start Telegram long-polling bot")
    parser.add_argument("--list-users", action="store_true", help="List registered users from alerts.sqlite3")
    parser.add_argument("--set-tier", nargs=2, metavar=("USER_ID", "TIER"), help="Set user tier in alerts.sqlite3")
    parser.add_argument("--test-search", metavar="KEYWORD", help="Test keyword search matching in console")

    args = parser.parse_args()
    db = PortalDatabase()

    if args.test_search:
        kw = args.test_search
        results = db.search_bots(kw)
        print(f"\n🔍 키워드 '{kw}' 검색 결과 (총 {len(results)}건):")
        for b in results:
            print(f"- {b.icon} {b.title} ({b.category.title}): {b.one_line_desc}")
        return

    if args.list_users:
        with db._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT telegram_user_id, username, full_name, tier, status FROM users")
            rows = cur.fetchall()
            print(f"\n총 {len(rows)}명의 회원이 등록되어 있습니다 (alerts.sqlite3):")
            for r in rows:
                subs = db.get_user_subscriptions(int(r["telegram_user_id"]))
                active = [b.short_name for b, v in subs.items() if v]
                print(f"- [ID: {r['telegram_user_id']}] {r['full_name']} (@{r['username']}) | 등급: {r['tier']} | 구독중: {active}")
        return

    if args.set_tier:
        uid_str, tier_str = args.set_tier
        try:
            uid = int(uid_str)
            tier_enum = UserTier(tier_str.upper())
            if db.set_user_tier(uid, tier_enum):
                print(f"✅ 유저 {uid}의 등급이 {tier_enum.display_name} 으로 변경되었습니다.")
            else:
                print(f"❌ 유저 {uid}를 찾을 수 없습니다.")
        except Exception as e:
            print(f"❌ 오류 발생: {e}")
        return

    # Default: run polling
    run_polling(get_portal_token())


if __name__ == "__main__":
    main()
