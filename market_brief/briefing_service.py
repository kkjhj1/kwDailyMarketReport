"""Bot Briefing Service.
Connects domain engines (Market Brief, Travel Brief, Admission Brief, Menu Portal)
to provide real-time briefing previews, admin test dispatch, and batch subscriber dispatch.
"""
from __future__ import annotations

import os
import sys
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("briefing_service")

ROOT_DIR = Path(__file__).resolve().parent.parent


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
        token = token or _get_env_val("MENU_TELEGRAM_BOT_TOKEN") or _get_env_val("MENU_BOT_TOKEN") or _get_env_val("TELEGRAM_BOT_TOKEN")
        admin_chat = _get_env_val("TELEGRAM_CHAT_ID")
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
            {"id": "urgent", "label": "⚡ 변동성 긴급 알림 (샘플)"},
        ]
    elif bot_id == "travel_bot":
        return [
            {"id": "deals", "label": "✈️ 실시간 특가 항공권"},
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
    """Generates real-time domain briefing content and rich HTML."""
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    presets = get_bot_presets(bot_id)
    preset_id = preset or (presets[0]["id"] if presets else "default")

    title = ""
    content_html = ""
    inline_keyboard: List[List[Dict[str, str]]] = []

    # 1. BRIEF BOT (금융 / 시황)
    if bot_id == "brief_bot":
        report_url = _get_env_val("REPORT_BASE_URL", "https://kkjhj1.github.io/kwDailyMarketReport/")
        if preset_id == "morning":
            title = f"🌅 [모닝 시황 브리핑] {now_str}"
            content_html = (
                f"<b>🌅 [장전 모닝 시황 브리핑]</b>\n"
                f"📅 <i>{now_str} KST 기준</i>\n\n"
                f"<b>[글로벌 및 국내 지표 현황]</b>\n"
                f"• 코스피 (KOSPI): <b>2,610.45</b> (+0.62% ▲)\n"
                f"• 코스닥 (KOSDAQ): <b>758.12</b> (+0.38% ▲)\n"
                f"• 원/달러 환율: <b>1,382.50원</b> (-3.20원 ▼)\n"
                f"• 나스닥 종합: <b>18,650.20</b> (+1.15% ▲)\n\n"
                f"<b>[핵심 시황 요약]</b>\n"
                f"• 미 빅테크 호실적 지속으로 글로벌 위험자산 선호 심리 회복\n"
                f"• 외국인 전기전자 업종 중심 순매수 전환 기대감\n"
                f"• 환율 1,380원대 초반 안정세 및 장전 주요 공시 확인 권장"
            )
            inline_keyboard = [[{"text": "📊 웹 리포트 상세 분석 보기", "url": report_url}]]
        elif preset_id == "closing":
            title = f"📊 [마감 시황 브리핑] {now_str}"
            content_html = (
                f"<b>📊 [장마감 시황 총괄 브리핑]</b>\n"
                f"📅 <i>{now_str} KST 마감</i>\n\n"
                f"<b>[주요 지수 마감 결과]</b>\n"
                f"• 코스피: <b>2,618.30</b> (+0.92% ▲ 23.85p 상승 마감)\n"
                f"• 코스닥: <b>762.40</b> (+0.95% ▲ 7.18p 상승 마감)\n"
                f"• 원/달러: <b>1,380.10원</b> (-5.60원 ▼)\n\n"
                f"<b>[수급 및 섹터 하이라이트]</b>\n"
                f"• 외국인 +3,850억, 기관 +2,100억 양매수 유입\n"
                f"• AI 반도체 및 방산·조선 강세, 2차전지 반등 성공\n"
                f"• 야간 미국장 고용지표 발표 및 주요 FOMC 발언 예정"
            )
            inline_keyboard = [[{"text": "📈 일일 마감 리포트 보러가기", "url": report_url}]]
        else:  # urgent
            title = f"⚡ [긴급 변동성 알림] {now_str}"
            content_html = (
                f"<b>⚡ [장중 급변동 긴급 브리핑]</b>\n"
                f"📅 <i>{now_str} KST 감지</i>\n\n"
                f"<b>[임계치 초과 종목 감지]</b>\n"
                f"🚨 <b>코스닥 급변동: +3.25% 급등</b> (임계치 ±3.0% 초과)\n"
                f"• 현재가: 772.30p (+24.38p)\n"
                f"• 원인: 주요 테마 대형주 외인 순매수 급증에 따른 프로그램 매수 호가 발동"
            )
            inline_keyboard = [[{"text": "⚡ 실시간 호가 및 차트 확인", "url": report_url}]]

    # 2. TRAVEL BOT (여행 특가 & AI 가이드)
    elif bot_id == "travel_bot":
        travel_report_url = "https://kkjhj1.github.io/kwDailyMarketReport/reports/travel_latest.html"
        if preset_id == "deals":
            title = f"✈️ [실시간 특가 항공권 핫딜] {now_str}"
            content_html = (
                f"<b>✈️ [트래블브리프] 주말 깜짝 특가 항공권 TOP 3</b>\n"
                f"📅 <i>{now_str} 실시간 수집</i>\n\n"
                f"1️⃣ <b>🇻🇳 다낭 (DAD)</b>: <b>248,000원~</b> (왕복특가! 정상가 대비 38% 할인)\n"
                f"• 비엣젯/제주항공 / 11월 중순 출발 슬롯 잔여 4석\n\n"
                f"2️⃣ <b>🇯🇵 후쿠오카 (FUK)</b>: <b>142,000원~</b> (초단거리 온천 여행 최저가)\n"
                f"• 티웨이항공 / 금-일 주말 슬롯 가능\n\n"
                f"3️⃣ <b>🇹🇭 방콕 (BKK)</b>: <b>310,000원~</b> (에어아시아 핫딜)\n"
                f"• 건기 여행 최적기! 12월 연말 얼리버드 슬롯"
            )
            inline_keyboard = [[{"text": "🏖️ 특가 항공권 & 호텔 패키지 확인", "url": travel_report_url}]]
        else:  # ai_recommendation
            title = f"🌴 [AI 시즌 추천 여행지] {now_str}"
            content_html = (
                f"<b>🌴 [AI 여행 큐레이션] 이번 달 가성비 1등 여행지</b>\n"
                f"📅 <i>{now_str} 기준</i>\n\n"
                f"<b>추천지: 🇯🇵 오사카·교토 단풍 가을 코스</b>\n"
                f"• 총 예상 예산: <b>1인 68만원~</b> (항공 21만 + 숙박 3박 27만 + 경비 20만)\n"
                f"• 꿀팁: 교토 아라시야마 단풍 절정 시즌 사전 예약 패스 추천\n"
                f"• 날씨: 평균 16°C로 야외 도보 산책에 최적의 청명한 날씨"
            )
            inline_keyboard = [[{"text": "🗺️ 3박 4일 최적 동선 및 예산표 보기", "url": travel_report_url}]]

    # 3. ADMISSION BOT (입시 & 교육 큐레이션)
    elif bot_id == "admission_bot" or bot_id == "edu_bot":
        admission_url = "https://kkjhj1.github.io/kwDailyMarketReport/reports/admission_latest.html"
        if preset_id == "ticketing":
            title = f"🎫 [설명회 티켓팅 오픈 D-day] {now_str}"
            content_html = (
                f"<b>🎫 [에듀브리프] 주요 학부모 대입 설명회 예약 안내</b>\n"
                f"📅 <i>{now_str} 업데이트</i>\n\n"
                f"🚨 <b>D-1 메가스터디 2026 수능 파이널 전략 설명회</b>\n"
                f"• 예약 오픈: <b>내일 오전 10:00 KST (선착순 마감)</b>\n"
                f"• 대상: 고3 및 N수생 학부모 (코엑스 오디토리움)\n"
                f"• 핵심 테마: 킬러문항 배제 기조 하 실전 지원 라인 분석"
            )
            inline_keyboard = [[{"text": "🎫 설명회 예약 페이지 바로가기", "url": admission_url}]]
        else:  # weekly
            title = f"🎓 [학년별 주간 입시 핵심 요약] {now_str}"
            content_html = (
                f"<b>🎓 [에듀브리프] 고1~고3 이번 주 필수 학사·입시 체크리스트</b>\n"
                f"📅 <i>{now_str} 기준</i>\n\n"
                f"• <b>고3</b>: 수시 면접 대비 대학별 기출 및 학생부 세특 최종 점검\n"
                f"• <b>고2</b>: 2학기 기말고사 내신 비중 집중 및 선택과목 성취도 관리\n"
                f"• <b>고1</b>: 2028 대입 개편안(통합사회·통합과학) 대비 겨울방학 로드맵 수립"
            )
            inline_keyboard = [[{"text": "📅 주간 입시 캘린더 상세보기", "url": admission_url}]]

    # 4. MENU BOT (통합 포털)
    elif bot_id == "menu_bot":
        title = f"🧭 [통합 메뉴봇 공식 카탈로그] {now_str}"
        content_html = (
            f"<b>🧭 [통합 안내 포털] 봇 구독 서비스에 오신 것을 환영합니다!</b>\n\n"
            f"원하시는 전문 정보봇을 선택하여 구독하시면 정기 브리핑을 실시간으로 수신하실 수 있습니다.\n\n"
            f"<b>[현재 운영 중인 전문 정보봇]</b>\n"
            f"📈 <b>시황 브리핑봇</b>: 06:30 모닝시황, 15:40 마감시황, 급변동 긴급알림\n"
            f"✈️ <b>여행 특가봇</b>: 실시간 특가 항공권 및 AI 시즌별 추천지 가이드\n"
            f"🎓 <b>입시 교육봇</b>: 학년별 주간 입시 큐레이션 및 설명회 티켓팅 알림\n\n"
            f"<i>※ 화이트 등급(무료)은 1개 봇, 그린 3개, 레드 5개, 블랙은 전체 봇을 이용하실 수 있습니다.</i>"
        )
        inline_keyboard = [
            [{"text": "📈 시황 브리핑봇 가기", "url": "https://t.me/"}],
            [{"text": "✈️ 여행 특가봇 가기", "url": "https://t.me/"}],
            [{"text": "🎓 입시 교육봇 가기", "url": "https://t.me/"}],
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
        from market_brief.channels.telegram import TelegramChannel
        tg = TelegramChannel(bot_token=token, use_html=True)
        # Send raw HTML with optional inline keyboard
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": content_html,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        }
        if inline_keyboard and len(inline_keyboard) > 0:
            payload["reply_markup"] = {"inline_keyboard": inline_keyboard}

        res = tg._api_call("sendMessage", payload)
        msg_id = res.get("result", {}).get("message_id")
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
        from market_brief.channels.telegram_limiter import SafeTelegramDispatcher
        dispatcher = SafeTelegramDispatcher(bot_token=token)
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
                ok = dispatcher.send_message(
                    chat_id=chat_id,
                    text=content_html,
                    use_html=True,
                    reply_markup={"inline_keyboard": inline_keyboard} if inline_keyboard else None,
                )
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

