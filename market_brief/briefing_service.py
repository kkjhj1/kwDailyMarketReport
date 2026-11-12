"""Bot Briefing Service.
Connects domain engines (Market Brief, Travel Brief, Admission Brief, SilverCare, Kids Compass, YOLO Bot, Menu Portal)
to provide real-time briefing previews, admin test dispatch, and batch subscriber dispatch.
"""
from __future__ import annotations

import html
import os
import sys
import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from market_brief.link_gate import GATE_REGISTRY, build_gate_button

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
        "silver_bot": ["SILVER_TELEGRAM_BOT_USERNAME", "SILVER_BOT_USERNAME"],
        "kids_bot": ["KIDS_TELEGRAM_BOT_USERNAME", "KIDS_BOT_USERNAME"],
        "yolo_bot": ["YOLO_TELEGRAM_BOT_USERNAME", "YOLO_BOT_USERNAME"],
        "menu_bot": ["PORTAL_TELEGRAM_BOT_USERNAME", "MENU_TELEGRAM_BOT_USERNAME", "MENU_BOT_USERNAME", "PORTAL_BOT_USERNAME"],
    }.get(bot_id, [f"{bot_id.upper()}_USERNAME"])

    for k in env_keys:
        val = _get_env_val(k).strip().lstrip("@")
        if val:
            return val

    # 3. Known fallback defaults for the verified platform bots
    defaults = {
        "brief_bot": "KwDailyMarketInfo_bot",
        "travel_bot": "KwTravelLowPrice_bot",
        "admission_bot": "KwEduInfo_bot",
        "edu_bot": "KwEduInfo_bot",
        "silver_bot": "KwSilver_bot",
        "kids_bot": "KwKids_bot",
        "yolo_bot": "KwYolo_bot",
        "menu_bot": "KwMenu_bot",
    }
    return defaults.get(bot_id, "")


def get_bot_token_and_chat(bot_id: str, db=None) -> Tuple[str, str]:
    """Retrieves bot token and admin chat ID by bot_id with fallback cascade."""
    token = ""
    admin_chat = ""

    # 1. Try DB first (BotRecord uses .token; keep .bot_token fallback for compatibility)
    if db is not None:
        try:
            bot = db.get_bot(bot_id)
            if bot:
                db_tok = getattr(bot, "token", None) or getattr(bot, "bot_token", None) or ""
                if str(db_tok).strip():
                    token = str(db_tok).strip()
        except Exception as e:
            logger.debug(f"DB token lookup error for {bot_id}: {e}")

    # 2. Domain-specific fallbacks (never fallback kids/silver/yolo to TELEGRAM_BOT_TOKEN)
    if bot_id == "brief_bot":
        token = token or _get_env_val("TELEGRAM_BOT_TOKEN")
        admin_chat = _get_env_val("TELEGRAM_CHAT_ID")
    elif bot_id == "travel_bot":
        token = token or _get_env_val("TRAVEL_TELEGRAM_BOT_TOKEN") or _get_env_val("TELEGRAM_BOT_TOKEN")
        admin_chat = _get_env_val("TRAVEL_TELEGRAM_CHAT_ID") or _get_env_val("TELEGRAM_CHAT_ID")
    elif bot_id == "admission_bot" or bot_id == "edu_bot":
        token = token or _get_env_val("EDU_TELEGRAM_BOT_TOKEN") or _get_env_val("TELEGRAM_BOT_TOKEN")
        admin_chat = _get_env_val("EDU_TELEGRAM_CHAT_ID") or _get_env_val("TELEGRAM_CHAT_ID")
    elif bot_id == "silver_bot":
        token = token or _get_env_val("SILVER_TELEGRAM_BOT_TOKEN") or _get_env_val("SILVER_BOT_TOKEN")
        admin_chat = _get_env_val("SILVER_TELEGRAM_CHAT_ID") or _get_env_val("TELEGRAM_CHAT_ID")
    elif bot_id == "kids_bot":
        token = token or _get_env_val("KIDS_TELEGRAM_BOT_TOKEN") or _get_env_val("KIDS_BOT_TOKEN")
        admin_chat = _get_env_val("KIDS_TELEGRAM_CHAT_ID") or _get_env_val("TELEGRAM_CHAT_ID")
    elif bot_id == "yolo_bot":
        token = token or _get_env_val("YOLO_TELEGRAM_BOT_TOKEN") or _get_env_val("YOLO_BOT_TOKEN")
        admin_chat = _get_env_val("YOLO_TELEGRAM_CHAT_ID") or _get_env_val("TELEGRAM_CHAT_ID")
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
    elif bot_id == "silver_bot":
        return [
            {"id": "weekly", "label": "💌 주간 효도 브리핑"},
            {"id": "benefits", "label": "👵 부모님 맞춤 복지 후보 안내"},
        ]
    elif bot_id == "kids_bot":
        return [
            {"id": "weekend", "label": "🎈 주말 맞춤 나들이 추천"},
            {"id": "dday", "label": "⏰ 예약 오픈 D-Day 알림"},
        ]
    elif bot_id == "yolo_bot":
        return [
            {"id": "weekly", "label": "🏄 성향별 솔로 라이프 큐레이션"},
            {"id": "weather_safe", "label": "☔ 우천/악천후 실내 플랜 B"},
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
        report_url = _get_env_val("REPORT_BASE_URL", "http://161.33.196.211:8002/")
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
            GATE_REGISTRY.update_preset_url("bf_rep", report_url)
            inline_keyboard = [
                [build_gate_button("bf_rep", "📊 웹 리포트 상세 분석 보기")],
                [{"text": "🔔 내 변동성 타겟 설정", "callback_data": "mbw:m"}],
            ]
        except Exception as e:
            logger.error(f"Error generating brief_bot briefing: {e}", exc_info=True)
            title = f"📊 [시황] {now_str}"
            content_html = f"<b>📊 [시황]</b>\n<i>{now_str} KST</i>\n\n데이터 수집 중 오류: {html.escape(str(e))}"
            GATE_REGISTRY.update_preset_url("bf_rep", report_url)
            inline_keyboard = [
                [build_gate_button("bf_rep", "📊 웹 리포트 보러가기")],
                [{"text": "🔔 내 변동성 타겟 설정", "callback_data": "mbw:m"}],
            ]

    # =========================================================================
    # 2. TRAVEL BOT (여행 특가 & AI 추천) -> FlightEngine / BudgetCalculator
    # =========================================================================
    elif bot_id == "travel_bot":
        travel_report_url = "http://161.33.196.211:8002/reports/travel_latest.html"
        try:
            import html as _html
            from travel_brief.models import round_man
            from travel_brief.flight_engine import FlightEngine
            from travel_brief.budget_calculator import BudgetCalculator
            from travel_brief.ai_prompts import AIPromptGenerator
            from travel_bot_runner import format_telegram_briefing, resolve_travel_report_url

            live_flag = os.environ.get("TRAVEL_LIVE_NAVER", "1").strip() != "0"
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
                GATE_REGISTRY.update_preset_url("trv_rep", travel_report_url)
                tok_gpt = GATE_REGISTRY.register_dynamic(chatgpt_link, "ChatGPT 3박4일 추천 일정", "AI가 맞춤 생성한 여행 동선 및 일정표로 이동합니다.", "travel:home")
                tok_nv = GATE_REGISTRY.register_dynamic(naver_flight, "네이버 항공권 실시간 조회", "선택하신 노선의 네이버 항공권 실시간 예약 페이지로 이동합니다.", "travel:home")
                inline_keyboard = [
                    [build_gate_button(tok_gpt, "🤖 ChatGPT 3박4일 일정 짜기")],
                    [build_gate_button(tok_nv, "🔍 네이버 항공권 실시간 확인")],
                    [build_gate_button("trv_rep", "📊 6개월 최저가 히트맵 전체보기")],
                ]
            else:
                title = f"✈️ [주말 해외여행 6개월 특가 TOP 3] {now_str}"
                content_html = format_telegram_briefing(recommendations, web_report_url=travel_report_url, engine=engine)
                GATE_REGISTRY.update_preset_url("trv_rep", travel_report_url)
                inline_keyboard = [
                    [build_gate_button("trv_rep", "📊 6개월 최저가 히트맵 전체보기")]
                ]
        except Exception as e:
            logger.error(f"Error generating travel_bot briefing: {e}", exc_info=True)
            title = f"✈️ [여행 특가 브리핑] {now_str}"
            content_html = f"<b>✈️ [여행 특가 브리핑]</b>\n<i>{now_str}</i>\n\n여행 데이터 생성 중 오류: {html.escape(str(e))}"
            GATE_REGISTRY.update_preset_url("trv_rep", travel_report_url)
            inline_keyboard = [[build_gate_button("trv_rep", "🏖️ 트래블 리포트 확인")]]

    # =========================================================================
    # 3. ADMISSION BOT (입시 & 교육 큐레이션) -> AdmissionDatabase / AICurator
    # =========================================================================
    elif bot_id == "admission_bot" or bot_id == "edu_bot":
        admission_url = _get_env_val("ADMISSION_REPORT_URL", "http://161.33.196.211:8002/reports/admission_latest.html")
        try:
            from admission_brief.ai_curator import AICurator
            from admission_brief.models import TargetGrade, BriefingType
            from admission_brief.channels.telegram import format_admission_html
            from admission_brief.storage.db import AdmissionDatabase

            curator = AICurator()
            adm_db_path = getattr(db, "db_path", None)
            if not adm_db_path or str(adm_db_path) == ":memory:":
                adm_db_path = ":memory:"
            elif not Path(adm_db_path).exists() and (ROOT_DIR / "data/admission_brief.db").exists():
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
            GATE_REGISTRY.update_preset_url("adm_rep", admission_url)
            tok_adm = GATE_REGISTRY.register_dynamic(btn_url, btn_text, "선착순 티켓팅 및 설명회 공식 접수 사이트로 이동합니다.", "admission:home")
            inline_keyboard = [[build_gate_button(tok_adm, btn_text)]]
        except Exception as e:
            logger.error(f"Error generating admission_bot briefing: {e}", exc_info=True)
            title = f"🎓 [입시 교육 브리핑] {now_str}"
            content_html = f"<b>🎓 [입시 교육 브리핑]</b>\n<i>{now_str}</i>\n\n입시 큐레이션 생성 중 오류: {html.escape(str(e))}"
            GATE_REGISTRY.update_preset_url("adm_rep", admission_url)
            inline_keyboard = [[build_gate_button("adm_rep", "📅 입시 캘린더 확인")]]

    # =========================================================================
    # 4. SILVER BOT (실버케어 부모님 복지 비서) -> SilverCareDatabase(:memory:)
    # =========================================================================
    elif bot_id == "silver_bot":
        silver_un = get_bot_username("silver_bot", db=db) or "KwSilver_bot"
        silver_link = f"https://t.me/{silver_un}"
        try:
            from silver_care.storage.db import SilverCareDatabase
            from silver_care.fixtures.seed_benefits import SEED_BENEFITS
            from silver_care.models import ParentProfile, LivingType
            from silver_care.channels.telegram_card import format_weekly_briefing, format_candidate_list

            mem_db = SilverCareDatabase(":memory:")
            mem_db.upsert_benefits(SEED_BENEFITS)
            sample_parent = ParentProfile(
                id=1,
                chat_id=1,
                alias="어머니",
                birth_year=1952,
                sido_code="11",
                sigungu_name="송파구",
                living_type=LivingType.ALONE.value,
                is_pension_receiver=1,
                is_low_income=-1,
            )
            matches = mem_db.match_for_parent(sample_parent, now_kst.year)
            if preset_id == "benefits":
                title = f"👵 [실버케어] 부모님 맞춤 복지 후보 ({now_str})"
                content_html = format_candidate_list(sample_parent, matches, page=0, ref_year=now_kst.year)
            else:
                week_label = f"{now_kst.month}월 {now_kst.day}일"
                title = f"💌 [실버케어] 주간 효도 브리핑 ({week_label})"
                content_html = format_weekly_briefing(
                    [(sample_parent, matches[:3])],
                    bookmarked_titles=[],
                    week_label=week_label,
                    ref_year=now_kst.year,
                )
            mem_db.close()
            GATE_REGISTRY.update_preset_url("bot_silver", silver_link)
            inline_keyboard = [[build_gate_button("bot_silver", f"👵 실버케어 봇에서 내 부모님 혜택 찾기 (@{silver_un})")]]
        except Exception as e:
            logger.error(f"Error generating silver_bot briefing: {e}", exc_info=True)
            title = f"👵 [실버케어 브리핑] {now_str}"
            content_html = f"<b>👵 [실버케어 브리핑]</b>\n<i>{now_str}</i>\n\n실버케어 데이터 생성 중 오류: {html.escape(str(e))}"
            GATE_REGISTRY.update_preset_url("bot_silver", silver_link)
            inline_keyboard = [[build_gate_button("bot_silver", f"👵 실버케어 바로가기 (@{silver_un})")]]

    # =========================================================================
    # 5. KIDS BOT (자녀·키즈 나침반) -> KidsCompassDatabase(:memory:)
    # =========================================================================
    elif bot_id == "kids_bot":
        kids_un = get_bot_username("kids_bot", db=db) or "KwKids_bot"
        kids_link = f"https://t.me/{kids_un}"
        try:
            from kids_compass.storage.db import KidsCompassDatabase
            from kids_compass.collector import seed_all, recommend_weekend_picks
            from kids_compass.models import AgeGroup
            from kids_compass.channels.telegram import format_weekend_briefing_html, format_dday_alert_html

            mem_db = KidsCompassDatabase(":memory:")
            seed_all(mem_db)
            if preset_id == "dday":
                events = mem_db.list_upcoming_bookings(
                    now_kst.strftime("%Y-%m-%d %H:%M"),
                    (now_kst + timedelta(days=14)).strftime("%Y-%m-%d %H:%M"),
                )
                title = f"⏰ [키즈 나침반] 선착순 예약 오픈 D-Day ({now_str})"
                content_html = format_dday_alert_html(events, now_kst.replace(tzinfo=None))
            else:
                picks = recommend_weekend_picks(mem_db, AgeGroup.PRESCHOOL, "서울", weather_rain=False)
                title = f"🎈 [키즈 나침반] 이번 주말 맞춤 나들이 추천 ({now_str})"
                content_html = format_weekend_briefing_html(
                    picks,
                    AgeGroup.PRESCHOOL,
                    "☀️ 맑음 기준 · 야외+실내 균형 추천",
                )
            mem_db.close()
            GATE_REGISTRY.update_preset_url("bot_kids", kids_link)
            inline_keyboard = [[build_gate_button("bot_kids", f"👶 키즈봇에서 해열제·달빛병원·나들이 보기 (@{kids_un})")]]
        except Exception as e:
            logger.error(f"Error generating kids_bot briefing: {e}", exc_info=True)
            title = f"👶 [키즈 나침반 브리핑] {now_str}"
            content_html = f"<b>👶 [키즈 나침반 브리핑]</b>\n<i>{now_str}</i>\n\n키즈 나침반 데이터 생성 중 오류: {html.escape(str(e))}"
            GATE_REGISTRY.update_preset_url("bot_kids", kids_link)
            inline_keyboard = [[build_gate_button("bot_kids", f"👶 키즈봇 바로가기 (@{kids_un})")]]

    # =========================================================================
    # 6. YOLO BOT (욜로 1인 라이프 큐레이션) -> YoloDatabase(:memory:)
    # =========================================================================
    elif bot_id == "yolo_bot":
        yolo_un = get_bot_username("yolo_bot", db=db) or "KwYolo_bot"
        yolo_link = f"https://t.me/{yolo_un}"
        try:
            from yolo_brief.storage.db import YoloDatabase
            from yolo_brief.fixtures.seed_curations import SEED_CURATIONS
            from yolo_brief.models import SoloArchetype, ProfileState, WeatherClass, WeatherSnapshot, YoloUserProfile
            from yolo_brief.matching import match_curations
            from yolo_brief.channels.telegram_card import render_curation_card

            mem_db = YoloDatabase(":memory:")
            mem_db.seed_curations(SEED_CURATIONS)
            curations = mem_db.list_active_curations()
            if preset_id == "weather_safe":
                sample_user = YoloUserProfile(
                    chat_id="preview_rain",
                    profile_state=ProfileState.ACTIVE,
                    archetype=SoloArchetype.ACTIVE_CHALLENGER,
                )
                snap = WeatherSnapshot(
                    weather_class=WeatherClass.RAIN,
                    pop_percent=80,
                    precip_mm=12.0,
                    feels_like_c=16.0,
                )
                title = f"☔ [욜로봇] 우천·악천후 실내 플랜 B 큐레이션 ({now_str})"
            else:
                sample_user = YoloUserProfile(
                    chat_id="preview_weekly",
                    profile_state=ProfileState.ACTIVE,
                    archetype=SoloArchetype.URBAN_HIPSTER,
                )
                snap = WeatherSnapshot(
                    weather_class=WeatherClass.CLEAR,
                    pop_percent=10,
                    feels_like_c=20.0,
                )
                title = f"🏄 [욜로봇] 이번 주 1인 라이프 큐레이션 ({now_str})"

            match = match_curations(sample_user, curations, snapshot=snap, ref_date=now_kst.date())
            content_html, _ = render_curation_card(sample_user, match)
            mem_db.close()
            GATE_REGISTRY.update_preset_url("bot_yolo", yolo_link)
            inline_keyboard = [[build_gate_button("bot_yolo", f"🏄 욜로봇에서 내 성향 맞춤 추천받기 (@{yolo_un})")]]
        except Exception as e:
            logger.error(f"Error generating yolo_bot briefing: {e}", exc_info=True)
            title = f"🏄 [욜로봇 브리핑] {now_str}"
            content_html = f"<b>🏄 [욜로봇 브리핑]</b>\n<i>{now_str}</i>\n\n욜로 큐레이션 데이터 생성 중 오류: {html.escape(str(e))}"
            GATE_REGISTRY.update_preset_url("bot_yolo", yolo_link)
            inline_keyboard = [[build_gate_button("bot_yolo", f"🏄 욜로봇 바로가기 (@{yolo_un})")]]

    # =========================================================================
    # 7. MENU BOT (통합 포털) -> .env usernames & telegram links (6대 정보봇)
    # =========================================================================
    elif bot_id == "menu_bot":
        brief_un = get_bot_username("brief_bot", db=db) or "KwDailyMarketInfo_bot"
        travel_un = get_bot_username("travel_bot", db=db) or "KwTravelLowPrice_bot"
        edu_un = get_bot_username("admission_bot", db=db) or "KwEduInfo_bot"
        silver_un = get_bot_username("silver_bot", db=db) or "KwSilver_bot"
        kids_un = get_bot_username("kids_bot", db=db) or "KwKids_bot"
        yolo_un = get_bot_username("yolo_bot", db=db) or "KwYolo_bot"

        title = f"🧭 [통합 메뉴봇 공식 카탈로그] {now_str}"
        content_html = (
            f"<b>🧭 [통합 안내 포털] 봇 구독 서비스에 오신 것을 환영합니다!</b>\n\n"
            f"원하시는 전문 정보봇을 선택하여 구독하시면 정기 브리핑을 실시간으로 수신하실 수 있습니다.\n\n"
            f"<b>[현재 운영 중인 6대 전문 정보봇]</b>\n"
            f"📈 <b>시황</b> (@{brief_un}): 06:30 모닝시황, 15:40 마감시황, 급변동 긴급알림\n"
            f"✈️ <b>여행</b> (@{travel_un}): 실시간 특가 항공권 및 AI 시즌별 추천지 가이드\n"
            f"🎓 <b>입시</b> (@{edu_un}): 학년별 주간 입시 큐레이션 및 설명회 티켓팅 알림\n"
            f"👵 <b>실버케어</b> (@{silver_un}): 부모님 맞춤 동사무소·보건소 복지 및 주간 효도 브리핑\n"
            f"👶 <b>자녀</b> (@{kids_un}): 주말 아이 맞춤 나들이 추천 및 해열제·달빛병원 3초 가이드\n"
            f"🏄 <b>욜로</b> (@{yolo_un}): 4대 성향별 1인 라이프·취미·혼여 큐레이션 및 실내 플랜 B\n\n"
            f"<i>※ 화이트 등급(무료)은 1개 봇, 그린 3개, 레드 5개, 블랙은 전체 봇을 이용하실 수 있습니다.</i>"
        )
        GATE_REGISTRY.update_preset_url("bot_brief", f"https://t.me/{brief_un}")
        GATE_REGISTRY.update_preset_url("bot_travel", f"https://t.me/{travel_un}")
        GATE_REGISTRY.update_preset_url("bot_admission", f"https://t.me/{edu_un}")
        GATE_REGISTRY.update_preset_url("bot_silver", f"https://t.me/{silver_un}")
        GATE_REGISTRY.update_preset_url("bot_kids", f"https://t.me/{kids_un}")
        GATE_REGISTRY.update_preset_url("bot_yolo", f"https://t.me/{yolo_un}")
        inline_keyboard = [
            [build_gate_button("bot_brief", f"📈 시황 바로가기 (@{brief_un})")],
            [build_gate_button("bot_travel", f"✈️ 여행 바로가기 (@{travel_un})")],
            [build_gate_button("bot_admission", f"🎓 입시 바로가기 (@{edu_un})")],
            [build_gate_button("bot_silver", f"👵 실버케어 바로가기 (@{silver_un})")],
            [build_gate_button("bot_kids", f"👶 자녀 바로가기 (@{kids_un})")],
            [build_gate_button("bot_yolo", f"🏄 욜로 바로가기 (@{yolo_un})")],
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


def _resolve_market_db(db=None):
    """Returns the underlying MarketBriefDatabase (delivery ledger) from a SubscriptionEngine or DB handle."""
    if db is None:
        return None
    if hasattr(db, "acquire_batch_lease"):
        return db
    inner = getattr(db, "db", None)
    if inner is not None and hasattr(inner, "acquire_batch_lease"):
        return inner
    return None


def open_manual_broadcast_job(db, bot_id: str, chat_ids: List[str], topic: Optional[str] = None) -> Optional[str]:
    """Creates a delivery_batch/delivery_job ledger entry for an admin manual broadcast.
    Best-effort: ledger failures never block the actual dispatch. Returns job_id or None."""
    market_db = _resolve_market_db(db)
    if market_db is None or not chat_ids:
        return None
    try:
        import uuid
        from market_brief.storage.models import Recipient

        now = datetime.now(KST)
        batch_key = f"MANUAL:{bot_id}:{now.strftime('%Y%m%d%H%M%S')}:{uuid.uuid4().hex[:6]}"
        acquired, _, job = market_db.acquire_batch_lease(
            batch_key=batch_key,
            topic=(topic or bot_id).strip().upper(),
            kind="manual",
            scheduled_date=now.date().isoformat(),
            runner_id="admin_manual_broadcast",
            force_reason=f"MANUAL_BROADCAST:{bot_id}",
        )
        if not acquired or job is None:
            return None
        market_db.snapshot_job_recipients(job.job_id, [Recipient(chat_id=str(c), name="") for c in chat_ids])
        return job.job_id
    except Exception as exc:
        logger.warning(f"Manual broadcast ledger open failed for {bot_id}: {exc}")
        return None


def record_manual_broadcast_result(
    db, job_id: Optional[str], chat_id: str, status: str,
    provider_msg_id: Optional[str] = None, error_detail: Optional[str] = None,
) -> None:
    market_db = _resolve_market_db(db)
    if market_db is None or not job_id:
        return
    try:
        market_db.record_recipient_result(
            job_id, str(chat_id), status,
            provider_msg_id=str(provider_msg_id) if provider_msg_id else None,
            error_detail=error_detail,
        )
    except Exception as exc:
        logger.warning(f"Manual broadcast recipient record failed ({job_id}/{chat_id}): {exc}")


def finish_manual_broadcast_job(db, job_id: Optional[str], total: int, success: int, fail: int, unknown: int = 0) -> None:
    market_db = _resolve_market_db(db)
    if market_db is None or not job_id:
        return
    try:
        from market_brief.storage.models import JobFinalStatus

        final_status = JobFinalStatus.SENT
        if fail > 0 or unknown > 0:
            final_status = JobFinalStatus.PARTIAL if success > 0 else JobFinalStatus.FAILED
        market_db.complete_job(job_id, status=final_status, total=total, success=success, fail=fail, unknown=unknown)
    except Exception as exc:
        logger.warning(f"Manual broadcast ledger completion failed ({job_id}): {exc}")


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
    if hasattr(db, "get_bot_active_recipients"):
        subs = db.get_bot_active_recipients(bot_id)
    elif hasattr(db, "get_subscriptions_by_bot"):
        subs = db.get_subscriptions_by_bot(bot_id, status="ACTIVE")
    else:
        subs = []
    if not subs:
        return {
            "success": True,
            "total_targets": 0,
            "sent_count": 0,
            "failed_count": 0,
            "message": f"봇 [{bot_id}]을 구독 중인 활성 유저가 없습니다.",
        }

    chat_ids = [str(getattr(sub, "chat_id", None) or getattr(sub, "telegram_user_id", None)) for sub in subs]
    topic = getattr(subs[0], "subscribed_topics", None) or bot_id
    job_id = open_manual_broadcast_job(db, bot_id, chat_ids, topic=topic)

    try:
        from travel_brief.channels.telegram import TelegramChannel
        from travel_brief.channels.telegram_limiter import SafeTelegramDispatcher
        tg = TelegramChannel(bot_token=token)
        dispatcher = SafeTelegramDispatcher()
        sent = 0
        failed = 0
        unknown = 0

        for chat_id in chat_ids:
            try:
                status, msg_id, err = dispatcher.send_text_to_recipient(tg=tg, chat_id=chat_id, text=content_html, parse_mode="HTML", reply_markup={"inline_keyboard": inline_keyboard} if inline_keyboard else None)
                if status == "SENT":
                    sent += 1
                elif status == "UNKNOWN":
                    unknown += 1
                else:
                    failed += 1
                record_manual_broadcast_result(db, job_id, chat_id, status, provider_msg_id=msg_id, error_detail=err)
            except Exception as exc:
                failed += 1
                record_manual_broadcast_result(db, job_id, chat_id, "FAILED", error_detail=str(exc))

        finish_manual_broadcast_job(db, job_id, total=len(subs), success=sent, fail=failed, unknown=unknown)
        failed_total = failed + unknown
        return {
            "success": True,
            "bot_id": bot_id,
            "job_id": job_id,
            "total_targets": len(subs),
            "sent_count": sent,
            "failed_count": failed_total,
            "message": f"🚀 총 {len(subs)}명 중 {sent}명에게 안전하게 발송을 완료했습니다. (실패: {failed_total}건)",
        }
    except Exception as e:
        logger.error(f"Broadcast failed for {bot_id}: {e}")
        market_db = _resolve_market_db(db)
        if market_db is not None and job_id:
            try:
                market_db.fail_job(job_id, str(e))
            except Exception:
                pass
        return {
            "success": False,
            "bot_id": bot_id,
            "error": f"브로드캐스트 발송 실패: {str(e)}",
        }
