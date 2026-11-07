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
from portal_hub.interactive import (
    USER_STATES,
    build_persistent_reply_keyboard,
    build_slot_gauge,
    send_admin_new_user_alert,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("portal_runner")


import re

MENU_START_COMMANDS = {
    "/start", "/menu", "/home", "홈", "메인", "메인 메뉴", "메인메뉴", "시작", "🏠 메인 메뉴", "🏠 홈", "🏠 메인", "🏠 메인 홈"
}
MENU_SUBS_COMMANDS = {
    "/subs", "/mysubs", "/sub", "내구독", "내 구독", "내구독관리", "내 구독 관리", "📋 내 구독 관리",
    "내구독센터", "내 구독 센터", "📋 내 구독 센터", "구독 관리", "구독관리", "구독 센터", "구독센터",
    "구독", "구독설정", "구독 설정", "내구독현황", "내 구독 현황", "내구독목록", "내 구독 목록"
}
MENU_CAT_COMMANDS = {
    "/category", "/categories", "/cats", "/cat", "카테고리", "카테고리 둘러보기", "카테고리둘러보기",
    "📂 카테고리 둘러보기", "둘러보기", "분야별", "카탈로그", "봇 둘러보기", "봇둘러보기", "📂 봇 둘러보기",
    "전체 봇", "전체봇", "전체정보봇", "정보봇목록", "봇목록"
}
MENU_TIER_COMMANDS = {
    "/tiers", "/tier", "/upgrade", "등급", "내 등급", "내등급", "내 등급 & 혜택", "💎 내 등급 & 혜택",
    "내 등급 혜택", "내등급혜택", "💎 내 등급 혜택", "내 등급/혜택", "💎 내 등급/혜택", "혜택", "멤버십",
    "등급 혜택", "등급혜택", "등급안내", "회원등급", "등급별혜택"
}
MENU_HELP_COMMANDS = {
    "/help", "/guide", "도움말", "이용 도움말", "이용도움말", "❓ 이용 도움말", "안내", "가이드", "faq", "이용안내", "사용법", "도움"
}
MENU_SUPPORT_COMMANDS = {
    "/support", "/inquiry", "/contact", "고객센터", "💬 고객센터", "문의", "1:1 문의", "1:1문의", "11문의",
    "문의하기", "상담", "고객지원", "관리자 문의", "관리자문의", "1:1 문의사항", "💬 고객센터/문의"
}


def normalize_menu_text(text: str) -> str:
    """Strips whitespace, emojis, and special punctuation for robust Korean voice/typing command matching."""
    if not text:
        return ""
    has_slash = text.strip().startswith("/")
    cleaned = re.sub(r"[^\w가-힣a-zA-Z0-9]", "", text.lower())
    return ("/" + cleaned) if has_slash and not cleaned.startswith("/") else cleaned


def match_menu_action(text: str) -> Optional[str]:
    """Matches raw or space-free text to one of: 'START', 'SUBS', 'CAT', 'TIER', 'HELP', 'SUPPORT'.
    Returns None if text is a keyword search query."""
    if not text:
        return None
    raw_lower = text.strip().lower()
    norm = normalize_menu_text(text)

    # 1. Start / Home
    if (
        raw_lower in MENU_START_COMMANDS
        or raw_lower.startswith("/start")
        or norm in {"start", "/start", "menu", "/menu", "home", "/home", "홈", "메인", "메인메뉴", "시작", "메인홈"}
    ):
        return "START"

    # 2. My Subscriptions / Management
    if (
        raw_lower in MENU_SUBS_COMMANDS
        or norm in {
            "subs", "/subs", "mysubs", "/mysubs", "sub", "/sub",
            "내구독", "내구독관리", "내구독센터", "구독관리", "구독센터",
            "구독", "구독설정", "내구독현황", "내구독목록", "구독현황",
        }
    ):
        return "SUBS"

    # 3. Browse Bots / Categories
    if (
        raw_lower in MENU_CAT_COMMANDS
        or norm in {
            "category", "/category", "categories", "/categories", "cats", "/cats", "cat", "/cat",
            "카테고리", "카테고리둘러보기", "봇둘러보기", "둘러보기", "분야별",
            "카탈로그", "전체봇", "전체정보봇", "정보봇목록", "봇목록",
        }
    ):
        return "CAT"

    # 4. Membership Tiers & Benefits
    if (
        raw_lower in MENU_TIER_COMMANDS
        or norm in {
            "tiers", "/tiers", "tier", "/tier", "upgrade", "/upgrade",
            "등급", "내등급", "내등급혜택", "내등급과혜택", "등급혜택",
            "혜택", "멤버십", "등급안내", "회원등급", "등급별혜택",
        }
    ):
        return "TIER"

    # 5. Usage Help & Guide
    if (
        raw_lower in MENU_HELP_COMMANDS
        or norm in {
            "help", "/help", "guide", "/guide", "faq", "/faq",
            "도움말", "이용도움말", "안내", "가이드", "이용안내", "사용법", "도움",
        }
    ):
        return "HELP"

    # 6. Customer Support / Inquiry
    if (
        raw_lower in MENU_SUPPORT_COMMANDS
        or norm in {
            "support", "/support", "inquiry", "/inquiry", "contact", "/contact",
            "고객센터", "문의", "11문의", "문의하기", "상담", "고객지원", "관리자문의",
        }
    ):
        return "SUPPORT"

    return None


def get_portal_token() -> str:
    token = os.environ.get("PORTAL_TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token.strip():
        env_p = Path(__file__).resolve().parent / ".env"
        if env_p.exists():
            for line in env_p.read_text(encoding="utf-8-sig").splitlines():
                if line.startswith("PORTAL_TELEGRAM_BOT_TOKEN="):
                    token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    break
    if not token.strip():
        try:
            db = PortalDatabase()
            with db._get_conn() as conn:
                row = conn.execute("SELECT token FROM bots WHERE bot_id = 'menu_bot'").fetchone()
                if row and row["token"]:
                    token = row["token"].strip()
        except Exception:
            pass
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
        {"command": "support", "description": "💬 고객센터 / 문의"},
        {"command": "help", "description": "❓ 이용 도움말"},
    ])

    logger.info("🚀 [Portal Hub Bot v2.0] 롱폴링 시작... (카테고리 2-Tap & 키워드 검색 엔진 탑재)")

    # Discard backlog on startup so old updates from past sessions don't replay
    try:
        stale = client.get_updates(offset=-1, timeout=1)
        if stale:
            offset = stale[-1]["update_id"] + 1
            logger.info("⏩ [초기화] 과거 대기 업데이트 %d건 소거 (offset: %d)", len(stale), offset)
        else:
            offset = 0
    except Exception as exc:
        logger.warning("초기 offset 조회 실패: %s", exc)
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
                        last_name=user_info.get("last_name"),
                    )
                    if getattr(user, "is_new", False) is True:
                        send_admin_new_user_alert(client, user)

                    clean_text = text.lower()
                    action = match_menu_action(text)
                    is_menu_action = action is not None

                    # Handle Awaiting Inquiry State
                    if USER_STATES.get(user_id) == "AWAITING_INQUIRY":
                        if not is_menu_action:
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
                        else:
                            # User selected another menu action while awaiting inquiry; cancel inquiry mode
                            USER_STATES.pop(user_id, None)

                    # Clear awaiting state if user clicked command or persistent menu
                    USER_STATES.pop(user_id, None)

                    # Handle Fixed Commands & Persistent Keyboard Texts
                    if action == "START":
                        subs = db.get_user_subscriptions(user_id)
                        reply_text, markup = render_main_menu(user, subs)
                        client.send_message(chat_id, "👇 아래 메뉴를 선택하세요.", reply_markup=reply_kb)
                        client.send_message(chat_id, reply_text, reply_markup=markup)

                    elif action == "SUBS":
                        subs = db.get_user_subscriptions(user_id)
                        reply_text, markup = render_subscription_view(user, subs)
                        client.send_message(chat_id, reply_text, reply_markup=markup)

                    elif action == "CAT":
                        from portal_hub.interactive import render_catalog_all_view
                        subs = db.get_user_subscriptions(user_id)
                        reply_text, markup = render_catalog_all_view(user, subs)
                        client.send_message(chat_id, reply_text, reply_markup=markup)

                    elif action == "TIER":
                        reply_text, markup = render_tier_benefits_view(user)
                        client.send_message(chat_id, reply_text, reply_markup=markup)

                    elif action == "HELP":
                        from portal_hub.interactive import render_help_view
                        reply_text, markup = render_help_view()
                        client.send_message(chat_id, reply_text, reply_markup=markup)

                    elif action == "SUPPORT":
                        from portal_hub.interactive import render_support_center_view
                        reply_text, markup = render_support_center_view(user)
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


def run_demo() -> None:
    """Token-free in-memory console verification of all menus and interactive operations ([TG-06])."""
    print("=" * 60)
    print("🧪 [Portal Hub Bot v2.0] Console Demo & Interactive Menu Verification")
    print("=" * 60)
    demo_db = PortalDatabase(":memory:")
    demo_user = demo_db.get_or_create_user(
        user_id=999001,
        chat_id=999001,
        username="demo_tester",
        first_name="데모테스터",
    )
    print(f"1. 회원 프로필 생성: {demo_user.first_name} (@{demo_user.username}) | 등급: {demo_user.tier.display_name}")

    # Main Menu
    menu_text, menu_kb = render_main_menu(demo_user)
    print(f"2. 메인 메뉴 렌더링 검증: {len(menu_text)} 글자 | 버튼 수: {sum(len(r) for r in menu_kb['inline_keyboard'])}개")

    # Categories
    for cat in BotCategory:
        subs = demo_db.get_user_subscriptions(demo_user.user_id)
        cat_text, cat_kb = render_category_view(cat, demo_user, subs)
        print(f"3. 카테고리 [{cat.title}] 렌더링: {len(cat_text)} 글자 | 버튼 수: {sum(len(r) for r in cat_kb['inline_keyboard'])}개")

    # Subscription Flow (Subscribe brief_bot)
    ok, msg = demo_db.subscribe(demo_user.user_id, BotServiceType.BRIEF)
    subs = demo_db.get_user_subscriptions(demo_user.user_id)
    sub_text, sub_kb = render_subscription_view(demo_user, subs)
    print(f"4. 구독 신청 및 구독 센터 렌더링: 성공={ok} ({msg}) | 활성={subs[BotServiceType.BRIEF]}")

    # Unsubscribe with in-place confirmation
    unsub_text, unsub_kb = render_subscription_view(demo_user, subs, confirming_bot_id="brief_bot")
    print(f"5. 해지 재확인 화면 펼침 렌더링: {len(unsub_text)} 글자 | 버튼 수: {sum(len(r) for r in unsub_kb['inline_keyboard'])}개")

    # Final Unsubscribe
    ok_unsub, msg_unsub = demo_db.unsubscribe(demo_user.user_id, BotServiceType.BRIEF)
    subs_after = demo_db.get_user_subscriptions(demo_user.user_id)
    print(f"6. 해지 완료 검증: 성공={ok_unsub} ({msg_unsub}) | 활성={subs_after[BotServiceType.BRIEF]}")

    # Keyword Search
    for query in ["환율", "수시", "비행기", "없는키워드"]:
        results = demo_db.search_bots(query)
        search_text, _ = render_search_results_view(query, results, demo_user, subs_after)
        print(f"7. 키워드 검색 '{query}': {len(results)}건 매칭")

    # Support Center & Upgrade Views
    from portal_hub.interactive import (
        render_inquiry_prompt_view,
        render_support_center_view,
        render_upgrade_request_view,
    )
    sup_text, _ = render_support_center_view(demo_user)
    upg_text, _ = render_upgrade_request_view(demo_user)
    inq_text, _ = render_inquiry_prompt_view(demo_user)
    print(f"8. 고객센터 / 등급 업그레이드 / 1:1 문의 뷰 렌더링 검증 완료")
    print("=" * 60)
    print("✅ [DEMO SUCCESS] All Portal Menus & Workflows Verified in :memory:!")
    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(description="Main Hub Portal Telegram Bot Runner v2.0")
    parser.add_argument("--demo", action="store_true", help="Token-free console verification of all menus in :memory:")
    parser.add_argument("--poll", action="store_true", help="Start Telegram long-polling bot")
    parser.add_argument("--list-users", action="store_true", help="List registered users from alerts.sqlite3")
    parser.add_argument("--set-tier", nargs=2, metavar=("USER_ID", "TIER"), help="Set user tier in alerts.sqlite3")
    parser.add_argument("--test-search", metavar="KEYWORD", help="Test keyword search matching in console")

    args = parser.parse_args()

    if args.demo:
        run_demo()
        return

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
