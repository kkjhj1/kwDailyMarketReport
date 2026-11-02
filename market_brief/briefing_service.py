"""Bot Briefing Service.
Connects domain engines (Market Brief, Travel Brief, Admission Brief, Menu Portal)
to provide real-time briefing previews, admin test dispatch, and batch subscriber dispatch.
"""
from __future__ import annotations

import html
import os
import sys
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

logger = logging.getLogger("briefing_service")

ROOT_DIR = Path(__file__).resolve().parent.parent
KST = ZoneInfo("Asia/Seoul")


def _get_env_val(key: str, default: str = "") -> str:
    val = os.environ.get(key, "").strip()
    if val:
        return val
    env_file = ROOT_DIR / ".env"
    if env_file.exists():
        try:
            for line in env_file.read_text(encoding="utf-8-sig").splitlines():
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            pass
    return default


def get_bot_username(bot_id: str, db=None) -> str:
    """Resolves Telegram @username for bot_id using DB, .env, or known live defaults."""
    # 1. Try DB first
    if db is not None:
        try:
            bot = db.get_bot(bot_id)
            if bot and getattr(bot, "telegram_username", None):
                un = str(bot.telegram_username).strip().lstrip("@")
                if un:
                    return un
        except Exception:
            pass

    # 2. Try .env specific variables
    env_keys = {
        "brief_bot": ["BRIEF_TELEGRAM_BOT_USERNAME", "TELEGRAM_BOT_USERNAME", "BRIEF_BOT_USERNAME"],
        "travel_bot": ["TRAVEL_TELEGRAM_BOT_USERNAME", "TRAVEL_BOT_USERNAME"],
        "admission_bot": ["EDU_TELEGRAM_BOT_USERNAME", "ADMISSION_TELEGRAM_BOT_USERNAME", "EDU_BOT_USERNAME"],
        "edu_bot": ["EDU_TELEGRAM_BOT_USERNAME", "ADMISSION_TELEGRAM_BOT_USERNAME", "EDU_BOT_USERNAME"],
        "menu_bot": ["PORTAL_TELEGRAM_BOT_USERNAME", "MENU_TELEGRAM_BOT_USERNAME", "MENU_BOT_USERNAME", "PORTAL_BOT_USERNAME"],
    }.get(bot_id, [f"{bot_id.upper()}_USERNAME"])

    for k in env_keys:
        val = _get_env_val(k).strip().lstrip("@")
        if val:
            return val

    # 3. Known fallback defaults for the 4 verified platform bots
    defaults = {
        "brief_bot": "KwDailyMarketInfo_bot",
        "travel_bot": "KwTravelLowPrice_bot",
        "admission_bot": "KwEduInfo_bot",
        "edu_bot": "KwEduInfo_bot",
        "menu_bot": "KwMenu_bot",
    }
    return defaults.get(bot_id, "")


def get_bot_token_and_chat(bot_id: str, db=None) -> Tuple[str, str]:
    """Retrieves bot token and admin chat ID by bot_id with fallback cascade."""
    token = ""
    admin_chat = ""

    # 1. Try DB first
    if db is not None:
        try:
            bot = db.get_bot(bot_id)
            if bot and bot.bot_token:
                token = bot.bot_token
        except Exception as e:
            logger.debug(f"DB token lookup error for {bot_id}: {e}")

    # 2. Domain-specific fallbacks
    if bot_id == "brief_bot":
        token = token or _get_env_val("TELEGRAM_BOT_TOKEN")
        admin_chat = _get_env_val("TELEGRAM_CHAT_ID")
    elif bot_id == "travel_bot":
        token = token or _get_env_val("TRAVEL_TELEGRAM_BOT_TOKEN") or _get_env_val("TELEGRAM_BOT_TOKEN")
        admin_chat = _get_env_val("TRAVEL_TELEGRAM_CHAT_ID") or _get_env_val("TELEGRAM_CHAT_ID")
    elif bot_id == "admission_bot" or bot_id == "edu_bot":
        token = token or _get_env_val("EDU_TELEGRAM_BOT_TOKEN") or _get_env_val("TELEGRAM_BOT_TOKEN")
        admin_chat = _get_env_val("EDU_TELEGRAM_CHAT_ID") or _get_env_val("TELEGRAM_CHAT_ID")
    elif bot_id == "menu_bot":
        token = token or _get_env_val("PORTAL_TELEGRAM_BOT_TOKEN") or _get_env_val("MENU_TELEGRAM_BOT_TOKEN") or _get_env_val("MENU_BOT_TOKEN") or _get_env_val("TELEGRAM_BOT_TOKEN")
        admin_chat = _get_env_val("PORTAL_TELEGRAM_CHAT_ID") or _get_env_val("MENU_TELEGRAM_CHAT_ID") or _get_env_val("TELEGRAM_CHAT_ID")
    else:
        token = token or _get_env_val("TELEGRAM_BOT_TOKEN")
        admin_chat = _get_env_val("TELEGRAM_CHAT_ID")

    # If admin_chat has comma-separated values, pick first
    if "," in admin_chat:
        admin_chat = admin_chat.split(",")[0].strip()

    return token, admin_chat


def get_bot_presets(bot_id: str) -> List[Dict[str, str]]:
    """Returns supported briefing preset options for each bot."""
    if bot_id == "brief_bot":
        return [
            {"id": "morning", "label": "🌅 06:30 모닝 시황"},
            {"id": "closing", "label": "📊 15:40 마감 시황"},
            {"id": "urgent", "label": "⚡ 변동성 긴급 알림"},
        ]
    elif bot_id == "travel_bot":
        return [
            {"id": "deals", "label": "✈️ 실시간 특가 항공권 TOP 3"},
            {"id": "ai_recommendation", "label": "🌴 AI 시즌 추천지 가이드"},
        ]
    elif bot_id == "admission_bot" or bot_id == "edu_bot":
        return [
            {"id": "weekly", "label": "🎓 학년별 주간 입시 큐레이션"},
            {"id": "ticketing", "label": "🎫 대입/고입 설명회 티켓팅 D-day"},
        ]
    elif bot_id == "menu_bot":
        return [
            {"id": "portal_welcome", "label": "🧭 메인 메뉴 & 카탈로그 안내"},
        ]
    return [
        {"id": "custom", "label": "📢 일반 공지 브리핑"},
    ]


def generate_bot_briefing(bot_id: str, preset: Optional[str] = None, db=None) -> Dict[str, Any]:
    """Generates real-time domain briefing content and rich HTML directly from domain engines."""
    now_kst = datetime.now(KST)
    now_str = now_kst.strftime("%Y-%m-%d %H:%M")
    presets = get_bot_presets(bot_id)
    preset_id = preset or (presets[0]["id"] if presets else "default")

    title = ""
    content_html = ""
    inline_keyboard: List[List[Dict[str, str]]] = []

    # =========================================================================
    # 1. BRIEF BOT (금융 / 시장 시황) -> NaverFinanceProvider / BriefingBuilder
    # =========================================================================
    if bot_id == "brief_bot":
        report_url = _get_env_val("REPORT_BASE_URL", "https://kkjhj1.github.io/kwDailyMarketReport/")
        try:
            from market_brief.market_data import CompositeMarketDataProvider, FixtureProvider, NaverFinanceProvider
            from market_brief.market_data.naver_provider import NewsItem, fetch_naver_market_news
            from market_brief.pipeline import DEFAULT_SYMBOLS
            from market_brief.report_engine import BriefingBuilder
            from market_brief.channels.telegram import format_telegram_html

            fixture_prov = FixtureProvider(str(ROOT_DIR / "fixtures/market_snapshot.json"))
            prov = CompositeMarketDataProvider(NaverFinanceProvider(), fixture_prov)
            quotes = prov.fetch(DEFAULT_SYMBOLS)

            news: List[NewsItem] = []
            try:
                news = fetch_naver_market_news(5)
            except Exception as e:
                logger.debug(f"Live naver news fetch skipped: {e}")
            if not news:
                try:
                    raw_news = json.loads((ROOT_DIR / "fixtures/market_news.json").read_text(encoding="utf-8"))
                    news = [NewsItem(**item) for item in raw_news]
                except Exception:
                    news = []

            as_of_label = f"{now_kst.strftime('%Y-%m-%d %H:%M')} KST"
            builder = BriefingBuilder()
            briefing = builder.build(
                quotes,
                kind=preset_id if preset_id in ("morning", "closing", "urgent") else "morning",
                as_of_label=as_of_label,
                report_url=report_url,
                news=news,
            )
            content_html = format_telegram_html(briefing)
            kind_kr = {"morning": "모닝 시황", "closing": "마감 시황", "urgent": "긴급 변동성"}.get(briefing.kind, briefing.kind.upper())
            title = f"[{kind_kr}] {briefing.headline} ({as_of_label})"
            inline_keyboard = [[{"text": "📊 웹 리포트 상세 분석 보기", "url": report_url}]]
        except Exception as e:
            logger.error(f"Error generating brief_bot briefing: {e}", exc_info=True)
            title = f"📊 [시황 브리핑] {now_str}"
            content_html = f"<b>📊 [시황 브리핑]</b>\n<i>{now_str} KST</i>\n\n데이터 수집 중 오류: {html.escape(str(e))}"
            inline_keyboard = [[{"text": "📊 웹 리포트 보러가기", "url": report_url}]]

    # =========================================================================
    # 2. TRAVEL BOT (여행 특가 & AI 추천) -> FlightEngine / BudgetCalculator
    # =========================================================================
    elif bot_id == "travel_bot":
        travel_report_url = "https://kkjhj1.github.io/kwDailyMarketReport/reports/travel_latest.html"
        try:
            import html as _html
            from travel_brief.models import round_man
            from travel_brief.flight_engine import FlightEngine
            from travel_brief.budget_calculator import BudgetCalculator
            from travel_brief.ai_prompts import AIPromptGenerator
            from travel_bot_runner import format_telegram_briefing, resolve_travel_report_url

            live_flag = os.environ.get("TRAVEL_LIVE_NAVER", "").strip() == "1"
            engine = FlightEngine(live_naver=live_flag)
            all_deals = engine.search_deals(months_ahead=6)
            recommendations = BudgetCalculator.rank_recommendations(
                destinations=engine.destinations,
                deals=all_deals,
                top_n=3,
            )
            travel_report_url = resolve_travel_report_url()
            if live_flag and recommendations:
                engine.ensure_deals_sse_verified([r.best_deal for r in recommendations])
                all_deals = engine.search_deals(months_ahead=6)
                recommendations = BudgetCalculator.rank_recommendations(destinations=engine.destinations, deals=all_deals, top_n=3)

            if preset_id == "ai_recommendation" and recommendations:
                top_rec = recommendations[0]
                dest = top_rec.destination
                deal = top_rec.best_deal
                budget = top_rec.budget
                chatgpt_link = AIPromptGenerator.get_chatgpt_deep_link(
                    AIPromptGenerator.build_compact_prompt(dest, deal, budget)
                )
                naver_flight = top_rec.naver_flight_url or FlightEngine.build_naver_flight_url(
                    deal.origin_code, deal.destination_code, deal.departure_date, deal.return_date
                )
                season_label = "✨ 최고시즌" if top_rec.season_status == "BEST_SEASON" else "🌤️ 여행적기"
                hotel_name_str = dest.recommended_hotel_name.split("(")[0].strip() if dest.recommended_hotel_name else f"{dest.name} 추천 호텔"

                title = f"🌴 [AI 시즌 추천 여행지] {dest.name} ({dest.code})"
                _air_raw = deal.airline if "직항" in deal.airline else f"{deal.airline} 직항"
                content_html = (
                    f"🌴 <b>[AI 여행 큐레이션] 이번 달 가성비 1등 추천 여행지</b>\n"
                    f"📅 <i>{now_str} KST 실시간 분석</i>\n\n"
                    f"<b>추천지: {_html.escape(dest.name, quote=False)} ({_html.escape(dest.code, quote=False)})</b> {season_label}\n"
                    f"  • 일정: <b>{deal.departure_date}({deal.departure_day_of_week}) ~ {deal.return_date}({deal.return_day_of_week})</b> (3박 4일)\n"
                    f"  • 시간대: <b>{_html.escape(deal.departure_time_slot, quote=False)}</b>\n"
                    f"  • {'실시간 직항 최저가' if deal.is_live else '예상 항공'}: <b>{_html.escape(deal.exact_price_str if deal.is_live else deal.price_band_str, quote=False)}</b> ({_html.escape(_air_raw, quote=False)} · 유류세 포함)\n"
                    f"  • 추천 숙소: <b>{_html.escape(hotel_name_str, quote=False)}</b> (3박 약 {round_man(budget.hotel_total_krw)}만 원)\n"
                    f"  • 1인 총예산: <b>약 {round_man(budget.total_per_person_krw)}만 원</b> (항공 + 숙박 + 식비·교통)\n"
                    f"  • 2인 총예산: 약 {round_man(budget.estimated_for_two_krw)}만 원 (1실 쉐어 · 1인당 약 {round_man(budget.estimated_for_two_krw / 2)}만)\n"
                    f"  • 비자/팁: {_html.escape(dest.visa_info, quote=False)} ({_html.escape(dest.tips, quote=False)})\n\n"
                    f"💡 <i>ChatGPT 맞춤 코스 생성을 통해 최적 3박 4일 동선을 3초 만에 확인하세요.</i>"
                )
                inline_keyboard = [
                    [{"text": "🤖 ChatGPT 3박4일 일정 짜기", "url": chatgpt_link}],
                    [{"text": "🔍 네이버 항공권 실시간 확인", "url": naver_flight}],
                    [{"text": "📊 6개월 최저가 히트맵 전체보기", "url": travel_report_url}],
                ]
            else:
                title = f"✈️ [주말 해외여행 6개월 특가 TOP 3] {now_str}"
                content_html = format_telegram_briefing(recommendations, web_report_url=travel_report_url, engine=engine)
                inline_keyboard = [
                    [{"text": "📊 6개월 최저가 히트맵 전체보기", "url": travel_report_url}]
                ]
        except Exception as e:
            logger.error(f"Error generating travel_bot briefing: {e}", exc_info=True)
            title = f"✈️ [여행 특가 브리핑] {now_str}"
            content_html = f"<b>✈️ [여행 특가 브리핑]</b>\n<i>{now_str}</i>\n\n여행 데이터 생성 중 오류: {html.escape(str(e))}"
            inline_keyboard = [[{"text": "🏖️ 트래블 리포트 확인", "url": travel_report_url}]]

    # =========================================================================
    # 3. ADMISSION BOT (입시 & 교육 큐레이션) -> AdmissionDatabase / AICurator
    # =========================================================================
    elif bot_id == "admission_bot" or bot_id == "edu_bot":
        admission_url = _get_env_val("ADMISSION_REPORT_URL", "https://kkjhj1.github.io/kwDailyMarketReport/reports/admission_latest.html")
        try:
            from admission_brief.ai_curator import AICurator
            from admission_brief.models import TargetGrade, BriefingType
            from admission_brief.channels.telegram import format_admission_html
            from admission_brief.storage.db import AdmissionDatabase

            curator = AICurator()
            adm_db_path = getattr(db, "db_path", None) or str(ROOT_DIR / "alerts.sqlite3")
            if not Path(adm_db_path).exists() and (ROOT_DIR / "data/admission_brief.db").exists():
                adm_db_path = str(ROOT_DIR / "data/admission_brief.db")
            adm_db = AdmissionDatabase(db_path=adm_db_path)

            if preset_id == "ticketing":
                events = adm_db.list_all_events()
                ticketing_events = [e for e in events if e.event_type == BriefingType.TICKETING_ALERT]
                if ticketing_events:
                    briefing = curator.curate_event_briefing(ticketing_events[0])
                else:
                    briefing = curator.generate_weekly_todo_briefing(TargetGrade.HIGH_3_N)
            else:
                briefing = curator.generate_weekly_todo_briefing(TargetGrade.ALL)

            title = f"[{briefing.briefing_type.title}] {briefing.headline}"
            content_html = format_admission_html(briefing)
            btn_text = briefing.booking_button_text or "🌐 상세 안내 확인하기"
            btn_url = briefing.booking_url or admission_url
            inline_keyboard = [[{"text": btn_text, "url": btn_url}]]
        except Exception as e:
            logger.error(f"Error generating admission_bot briefing: {e}", exc_info=True)
            title = f"🎓 [입시 교육 브리핑] {now_str}"
            content_html = f"<b>🎓 [입시 교육 브리핑]</b>\n<i>{now_str}</i>\n\n입시 큐레이션 생성 중 오류: {html.escape(str(e))}"
            inline_keyboard = [[{"text": "📅 입시 캘린더 확인", "url": admission_url}]]

    # =========================================================================
    # 4. MENU BOT (통합 포털) -> .env usernames & telegram links
    # =========================================================================
    elif bot_id == "menu_bot":
        brief_un = get_bot_username("brief_bot", db=db) or "KwDailyMarketInfo_bot"
        travel_un = get_bot_username("travel_bot", db=db) or "KwTravelLowPrice_bot"
        edu_un = get_bot_username("admission_bot", db=db) or "KwEduInfo_bot"

        brief_link = f"https://t.me/{brief_un}"
        travel_link = f"https://t.me/{travel_un}"
        edu_link = f"https://t.me/{edu_un}"

        title = f"🧭 [통합 메뉴봇 공식 카탈로그] {now_str}"
        content_html = (
            f"<b>🧭 [통합 안내 포털] 봇 구독 서비스에 오신 것을 환영합니다!</b>\n\n"
            f"원하시는 전문 정보봇을 선택하여 구독하시면 정기 브리핑을 실시간으로 수신하실 수 있습니다.\n\n"
            f"<b>[현재 운영 중인 전문 정보봇]</b>\n"
            f"📈 <b>시황 브리핑봇</b> (@{brief_un}): 06:30 모닝시황, 15:40 마감시황, 급변동 긴급알림\n"
            f"✈️ <b>여행 특가봇</b> (@{travel_un}): 실시간 특가 항공권 및 AI 시즌별 추천지 가이드\n"
            f"🎓 <b>입시 교육봇</b> (@{edu_un}): 학년별 주간 입시 큐레이션 및 설명회 티켓팅 알림\n\n"
            f"<i>※ 화이트 등급(무료)은 1개 봇, 그린 3개, 레드 5개, 블랙은 전체 봇을 이용하실 수 있습니다.</i>"
        )
        inline_keyboard = [
            [{"text": f"📈 시황 브리핑봇 바로가기 (@{brief_un})", "url": brief_link}],
            [{"text": f"✈️ 여행 특가봇 바로가기 (@{travel_un})", "url": travel_link}],
            [{"text": f"🎓 입시 교육봇 바로가기 (@{edu_un})", "url": edu_link}],
        ]

    else:
        title = f"📢 [일반 공지 브리핑] {now_str}"
        content_html = f"<b>📢 [{bot_id}] 실시간 공지 브리핑</b>\n\n구독자 여러분께 알림을 전해드립니다."
        inline_keyboard = []

    return {
        "bot_id": bot_id,
        "preset": preset_id,
        "title": title,
        "content_html": content_html,
        "inline_keyboard": inline_keyboard,
        "generated_at": now_str,
        "available_presets": presets,
    }


def send_bot_test_message(
    bot_id: str,
    content_html: str,
    inline_keyboard: Optional[List[List[Dict[str, str]]]] = None,
    target_chat_id: Optional[str] = None,
    db=None,
) -> Dict[str, Any]:
    """Sends a single test message to admin's Telegram chat."""
    token, default_chat = get_bot_token_and_chat(bot_id, db=db)
    chat_id = (target_chat_id or default_chat).strip()

    if not token:
        return {
            "success": False,
            "error": f"봇 [{bot_id}]의 토큰이 등록되어 있지 않습니다. .env 또는 설정 모달에서 등록해 주세요.",
            "bot_id": bot_id,
        }
    if not chat_id:
        return {
            "success": False,
            "error": "관리자 텔레그램 Chat ID(TELEGRAM_CHAT_ID)가 설정되어 있지 않습니다.",
            "bot_id": bot_id,
        }

    try:
        from travel_brief.channels.telegram import TelegramChannel
        from travel_brief.channels.telegram_limiter import SafeTelegramDispatcher
        tg = TelegramChannel(bot_token=token)
        dispatcher = SafeTelegramDispatcher()
        rm = {"inline_keyboard": inline_keyboard} if (inline_keyboard and len(inline_keyboard) > 0) else None
        status, msg_id, err = dispatcher.send_text_to_recipient(
            tg=tg, chat_id=chat_id, text=content_html, parse_mode="HTML", reply_markup=rm
        )
        if status != "SENT":
            raise RuntimeError(err or f"Dispatch status: {status}")
        return {
            "success": True,
            "bot_id": bot_id,
            "target_chat_id": chat_id,
            "message_id": msg_id,
            "message": f"✅ 관리자 텔레그램({chat_id})으로 테스트 브리핑 1건이 즉시 발송되었습니다.",
        }
    except Exception as e:
        logger.error(f"Test dispatch failed for {bot_id}: {e}")
        return {
            "success": False,
            "bot_id": bot_id,
            "target_chat_id": chat_id,
            "error": f"텔레그램 발송 실패: {str(e)}",
        }


def send_bot_broadcast_message(
    bot_id: str,
    content_html: str,
    inline_keyboard: Optional[List[List[Dict[str, str]]]] = None,
    db=None,
) -> Dict[str, Any]:
    """Dispatches briefing message to all active subscribers using SafeTelegramDispatcher."""
    token, _ = get_bot_token_and_chat(bot_id, db=db)
    if not token:
        return {
            "success": False,
            "error": f"봇 [{bot_id}]의 토큰이 등록되어 있지 않습니다.",
        }

    if db is None:
        return {"success": False, "error": "데이터베이스 연결이 필요합니다."}

    # Fetch active subscriptions
    subs = db.get_subscriptions_by_bot(bot_id, status="ACTIVE")
    if not subs:
        return {
            "success": True,
            "total_targets": 0,
            "sent_count": 0,
            "failed_count": 0,
            "message": f"봇 [{bot_id}]을 구독 중인 활성 유저가 없습니다.",
        }

    try:
        from travel_brief.channels.telegram import TelegramChannel
        from travel_brief.channels.telegram_limiter import SafeTelegramDispatcher
        tg = TelegramChannel(bot_token=token)
        dispatcher = SafeTelegramDispatcher()
        sent = 0
        failed = 0

        for sub in subs:
            chat_id = str(sub.telegram_user_id)
            payload: Dict[str, Any] = {
                "chat_id": chat_id,
                "text": content_html,
                "parse_mode": "HTML",
            }
            if inline_keyboard and len(inline_keyboard) > 0:
                payload["reply_markup"] = {"inline_keyboard": inline_keyboard}

            try:
                status, _, _ = dispatcher.send_text_to_recipient(tg=tg, chat_id=chat_id, text=content_html, parse_mode="HTML", reply_markup={"inline_keyboard": inline_keyboard} if inline_keyboard else None)
                ok = (status == "SENT")
                if ok:
                    sent += 1
                else:
                    failed += 1
            except Exception:
                failed += 1

        return {
            "success": True,
            "bot_id": bot_id,
            "total_targets": len(subs),
            "sent_count": sent,
            "failed_count": failed,
            "message": f"🚀 총 {len(subs)}명 중 {sent}명에게 안전하게 발송을 완료했습니다. (실패: {failed}건)",
        }
    except Exception as e:
        logger.error(f"Broadcast failed for {bot_id}: {e}")
        return {
            "success": False,
            "bot_id": bot_id,
            "error": f"브로드캐스트 발송 실패: {str(e)}",
        }

