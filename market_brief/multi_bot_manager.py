"""Unified Multi-Bot Interaction Router & Dynamic Asyncio Lifecycle Manager (v2.0).

Supports:
1. Menu Hub Bot (`bot_type='MENU'`):
   - Displays all active Info Bots with titles, descriptions, categories, and subscription badges.
   - Provides deep links (`https://t.me/<bot_username>?start=menu`) and inline self-service controls.
   - Displays user's current Tier (WHITE=1, GREEN=3, RED=5, BLACK=Unlimited) and quota usage.
2. Info Content Bots (`bot_type='INFO'`, e.g., 시황, 트렌드, 노래, 여행):
   - Automatically verifies user tier quota when a user initiates a chat (`/start`, etc.).
   - Subscribes immediately if within quota; blocks politely if over quota and offers
     atomic one-click swap buttons (`[🔄 기존봇 탈퇴 후 즉시 교체]`).
   - Supports `/stop`, `/탈퇴`, `/해지` and inline unsubscribe buttons to free up quota slots.
3. DynamicBotManager:
   - Manages background polling tasks for N bots inside a single FastAPI process without server restarts (Hot-Reload).
   - Fault-isolated per bot: an invalid token or network glitch on one bot never affects others.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
import html
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any

from market_brief.storage.models import BotRecord, BotType
from market_brief.subscription_engine import SubscriptionEngine

logger = logging.getLogger(__name__)

TIER_ICONS = {
    "WHITE": "⚪",
    "GREEN": "🟢",
    "RED": "🔴",
    "BLACK": "⚫",
}


def format_slot_usage(used: int, max_slots: int | None, is_unlimited: bool = False) -> str:
    if is_unlimited or max_slots is None:
        return f"{used}개 사용 중 (무제한 · 모든 봇 이용 가능)"
    return f"{used} / {max_slots}개 사용 중"


class TelegramBotTransport:
    """Lightweight Telegram Bot API helper supporting inline keyboards and callback queries."""

    def __init__(self, token: str, opener=None):
        self.token = token.strip()
        self._opener = opener or urllib.request.urlopen

    def api_call(self, method: str, payload: dict[str, Any] | None = None, timeout: int = 10) -> dict[str, Any]:
        url = f"https://api.telegram.org/bot{self.token}/{method}"
        if payload is None:
            req = urllib.request.Request(url, method="GET", headers={"User-Agent": "MultiBotPlatform/2.0"})
        else:
            body = json.dumps(payload).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=body,
                headers={"Content-Type": "application/json", "User-Agent": "MultiBotPlatform/2.0"},
                method="POST",
            )
        with self._opener(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error ({method}): {data.get('description', 'unknown')}")
        return data

    def get_me(self) -> dict[str, Any]:
        return self.api_call("getMe", timeout=6).get("result", {})

    def delete_webhook(self, drop_pending_updates: bool = False) -> bool:
        """Deletes any active webhook so long-polling getUpdates can function without 409 conflict."""
        res = self.api_call("deleteWebhook", {"drop_pending_updates": drop_pending_updates}, timeout=8)
        return bool(res.get("ok"))

    def get_updates(self, offset: int = 0, timeout: int = 25) -> list[dict[str, Any]]:
        try:
            res = self.api_call(
                "getUpdates",
                {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
                timeout=timeout + 6,
            )
            return res.get("result", [])
        except RuntimeError as err:
            err_msg = str(err).lower()
            if "webhook is active" in err_msg:
                logger.info(f"Active webhook detected on bot. Auto-deleting webhook to enable polling...")
                self.delete_webhook(drop_pending_updates=False)
                res = self.api_call(
                    "getUpdates",
                    {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
                    timeout=timeout + 6,
                )
                return res.get("result", [])
            elif "conflict" in err_msg or "409" in err_msg:
                # 409 Conflict: another poller instance is active on this bot token.
                # Do NOT auto-delete webhook; log warning and back off gracefully to avoid race condition.
                logger.warning("Telegram getUpdates conflict (409): another poller is active on this bot token. Backing off.")
                return []
            raise

    def send_html(
        self,
        chat_id: str,
        html_text: str,
        inline_keyboard: list[list[dict[str, Any]]] | None = None,
        reply_markup: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": str(chat_id).strip(),
            "text": html_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        elif inline_keyboard:
            payload["reply_markup"] = {"inline_keyboard": inline_keyboard}
        return self.api_call("sendMessage", payload, timeout=10)

    def answer_callback(self, callback_query_id: str, text: str = "", show_alert: bool = False) -> None:
        try:
            self.api_call(
                "answerCallbackQuery",
                {"callback_query_id": callback_query_id, "text": text[:190], "show_alert": show_alert},
                timeout=5,
            )
        except Exception as exc:
            logger.debug(f"answerCallbackQuery ignored error: {exc}")


COMMON_BRIEFING_BOTS: set[str] = {"brief_bot"}

TARGET_SELECTION_CONFIG: dict[str, dict[str, Any]] = {
    "admission_bot": {
        "prompt": "🎯 <b>[자녀 학년 선택]</b> 아래에서 대상을 먼저 선택하시면 <b>[📋 직전 발송 정보 보기]</b> 버튼이 활성화됩니다.",
        "options": [
            [
                {"code": "MIDDLE", "label": "🌱 초·중등 (특목/자사고)", "short": "초·중등"},
                {"code": "HIGH_1_2", "label": "🌿 고1·고2 (내신/학종)", "short": "고1·고2"},
            ],
            [
                {"code": "HIGH_3_N", "label": "🔥 고3·N수 (대입 실전)", "short": "고3·N수"},
                {"code": "ALL", "label": "🎓 전체 학년 공통", "short": "전체 학년"},
            ],
        ],
    },
    "edu_bot": {
        "prompt": "🎯 <b>[자녀 학년 선택]</b> 아래에서 대상을 먼저 선택하시면 <b>[📋 직전 발송 정보 보기]</b> 버튼이 활성화됩니다.",
        "options": [
            [
                {"code": "MIDDLE", "label": "🌱 초·중등 (특목/자사고)", "short": "초·중등"},
                {"code": "HIGH_1_2", "label": "🌿 고1·고2 (내신/학종)", "short": "고1·고2"},
            ],
            [
                {"code": "HIGH_3_N", "label": "🔥 고3·N수 (대입 실전)", "short": "고3·N수"},
                {"code": "ALL", "label": "🎓 전체 학년 공통", "short": "전체 학년"},
            ],
        ],
    },
    "travel_bot": {
        "prompt": "🎯 <b>[관심 여행지 선택]</b> 아래에서 여행지를 먼저 선택하시면 <b>[📋 직전 발송 정보 보기]</b> 버튼이 활성화됩니다.",
        "options": [
            [
                {"code": "NRT", "label": "🇯🇵 도쿄", "short": "도쿄"},
                {"code": "KIX", "label": "🇯🇵 오사카", "short": "오사카"},
                {"code": "FUK", "label": "🇯🇵 후쿠오카", "short": "후쿠오카"},
            ],
            [
                {"code": "TPE", "label": "🇹🇼 타이베이", "short": "타이베이"},
                {"code": "DAD", "label": "🇻🇳 다낭", "short": "다낭"},
                {"code": "BKK", "label": "🇹🇭 방콕", "short": "방콕"},
            ],
            [
                {"code": "ALL", "label": "✈️ 전체 주말 특가 TOP 3", "short": "전체 특가 TOP 3"},
            ],
        ],
    },
    "silver_bot": {
        "prompt": "🎯 <b>[부모님 세대 유형 선택]</b> 아래에서 대상을 먼저 선택하시면 <b>[📋 직전 발송 정보 보기]</b> 버튼이 활성화됩니다.",
        "options": [
            [
                {"code": "ALONE", "label": "👵 독거 부모님 맞춤 복지", "short": "독거 부모님"},
                {"code": "COUPLE", "label": "👴👵 노부부 세대 복지", "short": "노부부 세대"},
            ],
            [
                {"code": "FAM", "label": "🏠 자녀 동거 부모님 복지", "short": "자녀 동거"},
                {"code": "ALL", "label": "💌 주간 효도 브리핑 전체", "short": "주간 효도 브리핑"},
            ],
        ],
    },
    "kids_bot": {
        "prompt": "🎯 <b>[자녀 연령대 선택]</b> 아래에서 아이 연령대를 먼저 선택하시면 <b>[📋 직전 발송 정보 보기]</b> 버튼이 활성화됩니다.",
        "options": [
            [
                {"code": "INFANT", "label": "🍼 영아 (0~3세)", "short": "0~3세 영아"},
                {"code": "PRESCHOOL", "label": "🎈 유아 (4~7세)", "short": "4~7세 유아"},
            ],
            [
                {"code": "ELEMENTARY_LOW", "label": "🎒 초등 저학년 (8~10세)", "short": "초등 저학년"},
                {"code": "ALL", "label": "⏰ 예약 오픈 D-Day 전체", "short": "예약 D-Day"},
            ],
        ],
    },
    "yolo_bot": {
        "prompt": "🎯 <b>[1인 라이프 성향 선택]</b> 아래에서 성향을 먼저 선택하시면 <b>[📋 직전 발송 정보 보기]</b> 버튼이 활성화됩니다.",
        "options": [
            [
                {"code": "ACTIVE_CHALLENGER", "label": "🧗 액티브 챌린저 (도전·운동)", "short": "액티브 챌린저"},
                {"code": "URBAN_HIPSTER", "label": "🎨 어반 힙스터 (전시·공간)", "short": "어반 힙스터"},
            ],
            [
                {"code": "GOURMET_NOMAD", "label": "🍷 고메 노마드 (미식·혼술)", "short": "고메 노마드"},
                {"code": "COZY_HEALER", "label": "🍵 코지 힐러 (휴식·자연)", "short": "코지 힐러"},
            ],
        ],
    },
}


class MultiBotInteractionRouter:
    """Pure, deterministic interaction router shared by live Telegram bots and the Admin Simulator."""

    def __init__(self, engine: SubscriptionEngine):
        self.engine = engine
        self._delivered_initial_briefing: set[tuple[str, str]] = set()
        self._user_targets: dict[tuple[str, str], str] = {}
        self._briefing_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}

    @staticmethod
    def is_common_briefing_bot(bot_id: str) -> bool:
        """Returns True if `bot_id` is a common broadcast bot (no target selection required)."""
        return bot_id in COMMON_BRIEFING_BOTS

    @staticmethod
    def get_quick_callback_toast(callback_data: str | None) -> str:
        """Computes immediate <0.1s callback acknowledgement text ([TG-04]) before DB/Briefing work."""
        cb = (callback_data or "").strip()
        if cb.startswith("info:target:"):
            return "🎯 맞춤 대상이 선택되었습니다. [정보보기] 버튼을 눌러주세요!"
        if cb.startswith("info:preview:"):
            return "📋 직전 발송 최신 브리핑을 불러오는 중입니다..."
        if cb.startswith("info:retarget:"):
            return "🔄 대상 선택 메뉴를 불러옵니다."
        if cb.startswith("info:sub:"):
            return "🎉 구독 상태를 확인합니다..."
        if cb.startswith("info:unsub:"):
            return "👋 구독 해지 처리를 진행합니다..."
        if cb.startswith("info:swap:"):
            return "🔄 봇 교체 구독을 진행합니다..."
        return ""

    def handle_interaction(
        self,
        bot_id: str,
        telegram_user_id: str,
        full_name: str = "",
        username: str = "",
        text: str | None = None,
        callback_data: str | None = None,
    ) -> dict[str, Any]:
        """Processes either a text message or an inline button callback on `bot_id`.
        Returns: {
            "bot_id": str,
            "reply_html": str,
            "inline_keyboard": list[list[dict]],
            "callback_toast": str | None,
            "result_code": str,
            "user_profile": dict | None,
        }
        """
        bot = self.engine.get_bot(bot_id)
        if not bot:
            return {
                "bot_id": bot_id,
                "reply_html": "⚠️ 등록되지 않은 봇입니다.",
                "inline_keyboard": [],
                "callback_toast": None,
                "result_code": "BOT_NOT_FOUND",
                "user_profile": None,
            }

        uid = str(telegram_user_id).strip()
        if bot.bot_type == BotType.MENU.value:
            return self._handle_menu_bot(bot, uid, full_name, username, text, callback_data)
        return self._handle_info_bot(bot, uid, full_name, username, text, callback_data)

    # -------------------------------------------------------------------------
    # Menu Hub Bot Handler (Requirement #3)
    # -------------------------------------------------------------------------

    def _handle_menu_bot(
        self,
        menu_bot: BotRecord,
        uid: str,
        full_name: str,
        username: str,
        text: str | None,
        callback_data: str | None,
    ) -> dict[str, Any]:
        user = self.engine.ensure_user(uid, full_name=full_name, username=username)
        cb = (callback_data or "").strip()
        clean_text = (text or "").strip().lower()

        toast: str | None = None
        banner_notice = ""

        # Execute action callbacks first if requested inside Menu Bot
        if cb.startswith("menu:sub:"):
            target_bot_id = cb.split(":", 2)[2]
            res = self.engine.attempt_subscribe(uid, target_bot_id, full_name=full_name, username=username)
            if res["success"]:
                toast = f"🎉 {res['bot']['name']} 구독 완료!"
                return self._render_menu_bot_detail(menu_bot, uid, target_bot_id, notice=res["message"], toast=toast)
            if res["code"] == "QUOTA_EXCEEDED":
                return self._render_quota_exceeded_prompt(
                    uid=uid,
                    current_bot=menu_bot,
                    target_bot_info=res["bot"],
                    res=res,
                    prefix="menu",
                    toast="⚠️ 등급 구독 한도를 초과했습니다.",
                )
            banner_notice = f"⚠️ {res['message']}\n\n"

        elif cb.startswith("menu:unsub:"):
            target_bot_id = cb.split(":", 2)[2]
            res = self.engine.unsubscribe_bot(uid, target_bot_id, actor="USER", reason="MENU_BOT_UNSUB")
            toast = res["message"]
            banner_notice = f"{res['message']}\n\n"

        elif cb.startswith("menu:swap:"):
            parts = cb.split(":")
            if len(parts) == 4:
                old_id, new_id = parts[2], parts[3]
                try:
                    res = self.engine.atomic_swap_subscription(uid, old_id, new_id, actor="USER")
                    toast = "✅ 봇 교체 완료!" if res["success"] else res["message"]
                    banner_notice = f"{res['message']}\n\n"
                except ValueError as err:
                    toast = str(err)
                    banner_notice = f"⚠️ {html.escape(str(err))}\n\n"

        elif cb.startswith("menu:bot:"):
            target_bot_id = cb.split(":", 2)[2]
            return self._render_menu_bot_detail(menu_bot, uid, target_bot_id)

        elif cb == "menu:my_subs" or clean_text in {"/mysubs", "/내구독", "내구독"}:
            return self._render_menu_my_subscriptions(menu_bot, uid, banner_notice=banner_notice, toast=toast)

        elif cb == "menu:tiers" or clean_text in {"/tiers", "/등급", "등급"}:
            return self._render_menu_tier_guide(menu_bot, uid)

        # Default: Render Menu Catalog Home (`menu:home` or `/start`)
        user = self.engine.get_user_profile(uid) or user
        info_bots = [b for b in self.engine.list_bots() if b["bot_type"] == BotType.INFO.value and b["is_active"]]
        active_ids = {s["bot_id"] for s in user.get("active_subscriptions", [])}
        tier_icon = TIER_ICONS.get(user["tier"], "⚪")
        slot_str = format_slot_usage(user["used_slots"], user["max_slots"], user["is_unlimited"])

        lines = []
        if banner_notice:
            lines.append(banner_notice.strip())
            lines.append("━━━━━━━━━━━━━━━━━━")
        lines.extend(
            [
                f"{menu_bot.icon_emoji} <b>[{html.escape(menu_bot.name)}]</b>",
                f"{html.escape(menu_bot.description)}" if menu_bot.description else "원하시는 정보봇을 선택하여 구독하실 수 있습니다.",
                "",
                f"👤 <b>회원님 등급:</b> {tier_icon} <b>{html.escape(user['tier_name'])}</b>",
                f"📊 <b>구독 슬롯:</b> <code>{html.escape(slot_str)}</code>",
            ]
        )
        if user.get("tier_expires_at"):
            lines.append(f"⏳ <b>등급 유효기한:</b> <code>{html.escape(user['tier_expires_at'][:10])}</code>")
        if user.get("status") == "GRACE_PERIOD":
            lines.append("⚠️ <i>등급 변경으로 인해 초과 구독 정리 대기 중입니다. [내 구독 관리]에서 유지할 봇을 정리해 주세요.</i>")

        lines.append("━━━━━━━━━━━━━━━━━━")
        lines.append("📡 <b>[구독 가능한 정보봇 목록]</b>")

        keyboard: list[list[dict[str, Any]]] = []
        for b in info_bots:
            is_sub = b["bot_id"] in active_ids
            status_tag = "✅ 구독중" if is_sub else "➕ 선택가능"
            lines.append(f"• {b['icon_emoji']} <b>{html.escape(b['name'])}</b> ({html.escape(b['category'])}) — <i>{status_tag}</i>")
            btn_label = f"{'✅ ' if is_sub else ''}{b['icon_emoji']} {b['name']} {'(구독중 · 관리)' if is_sub else '선택하기'}"
            keyboard.append([{"text": btn_label, "callback_data": f"menu:bot:{b['bot_id']}"}])

        lines.append("")
        lines.append("💡 <i>아래 버튼을 눌러 각 정보봇 상세 안내 확인, 대화방 이동 및 구독/탈퇴를 진행하세요.</i>")

        keyboard.append(
            [
                {"text": f"📦 내 구독 관리·탈퇴 ({user['used_slots']}개)", "callback_data": "menu:my_subs"},
                {"text": "👑 등급별 혜택 안내", "callback_data": "menu:tiers"},
            ]
        )

        return {
            "bot_id": menu_bot.bot_id,
            "reply_html": "\n".join(lines),
            "inline_keyboard": keyboard,
            "callback_toast": toast,
            "result_code": "MENU_HOME",
            "user_profile": user,
        }

    def _render_menu_bot_detail(
        self,
        menu_bot: BotRecord,
        uid: str,
        target_bot_id: str,
        notice: str = "",
        toast: str | None = None,
    ) -> dict[str, Any]:
        user = self.engine.get_user_profile(uid)
        target = self.engine.get_bot(target_bot_id)
        if not target or not user:
            return self._handle_menu_bot(menu_bot, uid, "", "", "/start", "menu:home")

        active_ids = {s["bot_id"] for s in user.get("active_subscriptions", [])}
        is_sub = target.bot_id in active_ids
        uname = (target.telegram_username or "").lstrip("@").strip()
        slot_str = format_slot_usage(user["used_slots"], user["max_slots"], user["is_unlimited"])

        lines = []
        if notice:
            lines.append(f"<b>{html.escape(notice)}</b>")
            lines.append("━━━━━━━━━━━━━━━━━━")
        lines.extend(
            [
                f"{target.icon_emoji} <b>[{html.escape(target.name)}]</b>",
                f"🏷️ 카테고리: <code>{html.escape(target.category)}</code>",
                f"📌 상태: <b>{'✅ 현재 구독 중' if is_sub else '⚪ 미구독'}</b>",
                "",
                f"{html.escape(target.description or '유익한 최신 정보를 발송해 드리는 정보봇입니다.')}",
                "",
                f"👤 내 슬롯 현황: <code>{html.escape(slot_str)}</code>",
            ]
        )

        keyboard: list[list[dict[str, Any]]] = []
        if uname:
            keyboard.append(
                [{"text": f"🚀 {target.icon_emoji} {target.name} 대화방으로 바로 이동", "url": f"https://t.me/{uname}?start=menu"}]
            )
            lines.append(f"\n🔗 직접 접속: @{html.escape(uname)} 에 들어가서 대화를 걸면 등급 검증 후 즉시 구독됩니다.")

        if is_sub:
            keyboard.append([{"text": f"❌ {target.name} 구독 탈퇴(해지)하기", "callback_data": f"menu:unsub:{target.bot_id}"}])
        else:
            keyboard.append([{"text": f"✅ {target.name} 즉시 구독 신청", "callback_data": f"menu:sub:{target.bot_id}"}])

        keyboard.append([{"text": "⬅️ 전체 정보봇 목록으로 돌아가기", "callback_data": "menu:home"}])

        return {
            "bot_id": menu_bot.bot_id,
            "reply_html": "\n".join(lines),
            "inline_keyboard": keyboard,
            "callback_toast": toast,
            "result_code": "MENU_BOT_DETAIL",
            "user_profile": user,
        }

    def _render_menu_my_subscriptions(
        self,
        menu_bot: BotRecord,
        uid: str,
        banner_notice: str = "",
        toast: str | None = None,
    ) -> dict[str, Any]:
        user = self.engine.get_user_profile(uid) or {}
        active_subs = user.get("active_subscriptions", [])
        tier_icon = TIER_ICONS.get(user.get("tier", "WHITE"), "⚪")
        slot_str = format_slot_usage(user.get("used_slots", 0), user.get("max_slots", 1), user.get("is_unlimited", False))

        lines = []
        if banner_notice:
            lines.append(banner_notice.strip())
            lines.append("━━━━━━━━━━━━━━━━━━")
        lines.extend(
            [
                "📦 <b>[내 구독 현황 및 탈퇴 관리]</b>",
                f"👤 등급: {tier_icon} <b>{html.escape(user.get('tier_name', '화이트'))}</b> ({html.escape(slot_str)})",
                "━━━━━━━━━━━━━━━━━━",
            ]
        )

        keyboard: list[list[dict[str, Any]]] = []
        if not active_subs:
            lines.append("현재 구독 중인 정보봇이 없습니다. 전체 목록에서 원하는 봇을 선택해 보세요!")
        else:
            lines.append("다른 봇으로 변경하려면 아래에서 기존 봇의 <b>[탈퇴]</b> 버튼을 눌러 슬롯을 비워주세요:")
            for s in active_subs:
                lines.append(f"• {s['icon_emoji']} <b>{html.escape(s['bot_name'])}</b> (구독일: <code>{(s['subscribed_at'] or '')[:10]}</code>)")
                keyboard.append(
                    [{"text": f"❌ {s['icon_emoji']} {s['bot_name']} 탈퇴(해지)", "callback_data": f"menu:unsub:{s['bot_id']}"}]
                )

        keyboard.append([{"text": "⬅️ 전체 정보봇 목록으로 돌아가기", "callback_data": "menu:home"}])
        return {
            "bot_id": menu_bot.bot_id,
            "reply_html": "\n".join(lines),
            "inline_keyboard": keyboard,
            "callback_toast": toast,
            "result_code": "MENU_MY_SUBS",
            "user_profile": user,
        }

    def _render_menu_tier_guide(self, menu_bot: BotRecord, uid: str) -> dict[str, Any]:
        user = self.engine.get_user_profile(uid) or {}
        tiers = self.engine.get_tiers()
        lines = [
            "👑 <b>[회원 등급별 봇 구독 가능 개수 안내]</b>",
            f"현재 회원님 등급: <b>{TIER_ICONS.get(user.get('tier', 'WHITE'), '⚪')} {html.escape(user.get('tier_name', '화이트'))}</b>",
            "━━━━━━━━━━━━━━━━━━",
        ]
        for t in tiers:
            icon = TIER_ICONS.get(t["tier_code"], "⚪")
            limit_txt = "모든 봇 무제한 사용 가능" if t["is_unlimited"] else f"최대 {t['max_bots']}개 봇 동시 사용 가능"
            lines.append(f"{icon} <b>{html.escape(t['name'])}</b> : {limit_txt}")
        lines.extend(
            [
                "━━━━━━━━━━━━━━━━━━",
                "💡 <i>예시: 화이트(무료) 등급에서 [시황봇]을 선택 중일 때 [트렌드봇]을 이용하시려면, 기존 [시황봇]을 탈퇴하거나 원클릭 교체 버튼을 통해 전환하실 수 있습니다.</i>",
            ]
        )
        keyboard = [[{"text": "⬅️ 전체 정보봇 목록으로 돌아가기", "callback_data": "menu:home"}]]
        return {
            "bot_id": menu_bot.bot_id,
            "reply_html": "\n".join(lines),
            "inline_keyboard": keyboard,
            "callback_toast": None,
            "result_code": "MENU_TIERS",
            "user_profile": user,
        }

    # -------------------------------------------------------------------------
    # Info Content Bot Handler (Requirements #2 & #4)
    # -------------------------------------------------------------------------

    def _get_target_selection_config(self, bot: BotRecord) -> dict[str, Any]:
        """Returns target selection configuration for Target-Selection Bots (Type B)."""
        if bot.bot_id in TARGET_SELECTION_CONFIG:
            return TARGET_SELECTION_CONFIG[bot.bot_id]
        return {
            "prompt": f"🎯 <b>[{html.escape(bot.name)} 맞춤 대상 선택]</b> 대상을 먼저 선택하시면 <b>[📋 직전 발송 정보 보기]</b> 버튼이 활성화됩니다.",
            "options": [
                [
                    {"code": "ALL", "label": f"🎯 {bot.icon_emoji} {bot.name} 전체 대상 선택", "short": "전체 대상"},
                ]
            ],
        }

    def _build_target_selection_keyboard(
        self, bot: BotRecord, uid: str, menu_uname: str
    ) -> list[list[dict[str, Any]]]:
        """Constructs Step 1 target-selection keyboard for Target-Selection Bots (Type B)."""
        cfg = self._get_target_selection_config(bot)
        kb: list[list[dict[str, Any]]] = []
        selected_code = self._user_targets.get((uid, bot.bot_id))

        for row in cfg["options"]:
            btn_row: list[dict[str, Any]] = []
            for opt in row:
                code = opt["code"]
                label = opt["label"]
                prefix_mark = "✅ " if selected_code == code else ""
                btn_row.append(
                    {
                        "text": f"{prefix_mark}{label}",
                        "callback_data": f"info:target:{bot.bot_id}:{code}"[:64],
                    }
                )
            kb.append(btn_row)

        if selected_code:
            short_lbl = self._resolve_target_short_label(bot, selected_code)
            kb.append(
                [
                    {
                        "text": f"📋 [{short_lbl}] 직전 발송 정보 보기",
                        "callback_data": f"info:preview:{bot.bot_id}:{selected_code}"[:64],
                    }
                ]
            )

        kb.append([{"text": f"❌ {bot.name} 구독 탈퇴(해지)", "callback_data": f"info:unsub:{bot.bot_id}"}])
        if menu_uname:
            kb.append([{"text": "🧭 메뉴봇(다른 정보봇 보기)", "url": f"https://t.me/{menu_uname}"}])
        return kb

    def _resolve_target_short_label(self, bot: BotRecord, target_code: str) -> str:
        cfg = self._get_target_selection_config(bot)
        for row in cfg["options"]:
            for opt in row:
                if opt["code"] == target_code:
                    return str(opt.get("short") or opt["label"])
        return target_code or "맞춤"

    def _resolve_target_full_label(self, bot: BotRecord, target_code: str) -> str:
        cfg = self._get_target_selection_config(bot)
        for row in cfg["options"]:
            for opt in row:
                if opt["code"] == target_code:
                    return str(opt["label"])
        return target_code or "전체 대상"

    @staticmethod
    def _enforce_tg01_limits(html_text: str) -> str:
        """Strictly enforces [TG-01] limits: visible <= 1800 chars, HTML <= 3500 chars."""
        import re

        text_out = (html_text or "").strip()
        if len(text_out) > 3450:
            text_out = text_out[:3400].rsplit("\n", 1)[0] + "\n\n<i>... (상세 내용은 웹 리포트 참조)</i>"
        visible = re.sub(r"<[^>]+>", "", text_out)
        if len(visible) > 1780:
            lines = text_out.splitlines()
            kept: list[str] = []
            vis_count = 0
            for ln in lines:
                ln_vis = len(re.sub(r"<[^>]+>", "", ln)) + 1
                if vis_count + ln_vis > 1720:
                    kept.append("<i>... (상세 내용은 하단 리포트 링크 참조)</i>")
                    break
                kept.append(ln)
                vis_count += ln_vis
            text_out = "\n".join(kept)
        return text_out

    def _build_target_briefing(
        self, bot: BotRecord, target_code: str = "ALL", uid: str = "", menu_uname: str = ""
    ) -> dict[str, Any]:
        """Generates the most recently dispatched / current domain briefing for `bot` and `target_code`."""
        import time

        norm_target = (target_code or "ALL").strip().upper()
        cache_key = (bot.bot_id, norm_target)
        now_mono = time.monotonic()
        cached = self._briefing_cache.get(cache_key)
        if cached and (now_mono - cached[0]) < 30.0:
            return cached[1]

        content_html = ""
        kb: list[list[dict[str, Any]]] = []

        # Case 1: Common Broadcast Bot (brief_bot / 시황 브리핑)
        if bot.bot_id == "brief_bot":
            report_url = os.environ.get("REPORT_BASE_URL", "https://kkjhj1.github.io/kwDailyMarketReport/")
            try:
                from pathlib import Path
                from zoneinfo import ZoneInfo
                from market_brief.market_data import CompositeMarketDataProvider, FixtureProvider, NaverFinanceProvider
                from market_brief.market_data.naver_provider import NewsItem
                from market_brief.pipeline import DEFAULT_SYMBOLS
                from market_brief.report_engine import BriefingBuilder
                from market_brief.channels.telegram import format_telegram_html

                root_dir = Path(__file__).resolve().parent.parent
                fixture_prov = FixtureProvider(str(root_dir / "fixtures/market_snapshot.json"))
                use_live = (
                    os.environ.get("PYTEST_CURRENT_TEST") is None
                    and os.environ.get("BRIEF_LIVE_ON_START", "0") == "1"
                )
                prov = CompositeMarketDataProvider(NaverFinanceProvider(), fixture_prov) if use_live else fixture_prov
                quotes = prov.fetch(DEFAULT_SYMBOLS)

                news: list[NewsItem] = []
                try:
                    raw_news = json.loads((root_dir / "fixtures/market_news.json").read_text(encoding="utf-8"))
                    news = [NewsItem(**item) for item in raw_news[:5]]
                except Exception:
                    news = []

                now_kst = datetime.now(ZoneInfo("Asia/Seoul"))
                kind = "closing" if now_kst.hour >= 15 else "morning"
                as_of_label = f"{now_kst.strftime('%Y-%m-%d %H:%M')} KST"
                briefing = BriefingBuilder().build(
                    quotes,
                    kind=kind,
                    as_of_label=as_of_label,
                    report_url=report_url,
                    news=news,
                )
                body_html = format_telegram_html(briefing)
                header_banner = "📢 <b>[직전 발송 최신 시황 브리핑]</b>\n━━━━━━━━━━━━━━━━━━\n"
                content_html = self._enforce_tg01_limits(header_banner + body_html)
                kb = [
                    [{"text": "📊 웹 리포트 상세 분석 보기", "url": report_url}],
                    [{"text": "🔄 최신 시황 브리핑 다시 보기", "callback_data": "info:preview:brief_bot"}],
                ]
            except Exception as exc:
                logger.warning("brief_bot onboarding preview fallback: %s", exc)
                content_html = (
                    "📢 <b>[직전 발송 최신 시황 브리핑]</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "📈 <b>주요 지수 및 환율 요약</b>\n"
                    "• <b>KOSPI</b> / <b>S&amp;P 500</b> / <b>USD/KRW</b> 최신 장전·마감 시황 브리핑이 제공됩니다.\n"
                    "• 매일 <b>06:30 모닝 시황</b>, <b>15:40 마감 시황</b> 및 장중 급등락 알림이 발송됩니다."
                )
                kb = [[{"text": "📊 웹 리포트 상세 분석 보기", "url": report_url}]]

        # Case 2: Target-Selection Bot — Admission Bot (입시 나침반)
        elif bot.bot_id in ("admission_bot", "edu_bot"):
            admission_url = os.environ.get(
                "ADMISSION_REPORT_URL",
                "https://kkjhj1.github.io/kwDailyMarketReport/reports/admission_latest.html",
            )
            try:
                from admission_brief.ai_curator import AICurator
                from admission_brief.models import TargetGrade
                from admission_brief.channels.telegram import format_admission_html

                try:
                    grade = TargetGrade(norm_target)
                except ValueError:
                    grade = TargetGrade.ALL

                curator = AICurator()
                briefing = curator.generate_weekly_todo_briefing(grade)
                body_html = format_admission_html(briefing)
                header_banner = (
                    f"📋 <b>[직전 발송 맞춤 입시 브리핑 — {html.escape(grade.label)}]</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                )
                content_html = self._enforce_tg01_limits(header_banner + body_html)
                btn_url = briefing.booking_url or admission_url
                kb = [
                    [{"text": briefing.booking_button_text or "🌐 상세 입시 리포트 보기", "url": btn_url}],
                    [
                        {"text": "⚡ 핫 행사 TOP 3 보기", "callback_data": "adm:hot"},
                        {"text": "🎪 설명회·박람회 전체보기", "callback_data": "adm:menu"},
                    ],
                    [{"text": "🔄 자녀 학년 다시 선택하기", "callback_data": f"info:retarget:{bot.bot_id}"}],
                ]
            except Exception as exc:
                logger.warning("admission_bot onboarding preview fallback: %s", exc)
                content_html = (
                    f"📋 <b>[직전 발송 맞춤 입시 브리핑 ({html.escape(norm_target)})]</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "🎓 선택한 학년에 맞춘 이번 주 필수 입시 체크리스트(To-Do) 및 주요 설명회 일정입니다."
                )
                kb = [[{"text": "🔄 자녀 학년 다시 선택하기", "callback_data": f"info:retarget:{bot.bot_id}"}]]

        # Case 3: Target-Selection Bot — Travel Pathfinder (트래블 패스파인더)
        elif bot.bot_id == "travel_bot":
            travel_report_url = "https://kkjhj1.github.io/kwDailyMarketReport/reports/travel_latest.html"
            try:
                from travel_brief.flight_engine import FlightEngine
                from travel_brief.budget_calculator import BudgetCalculator
                from travel_bot_runner import format_telegram_briefing, resolve_travel_report_url

                travel_report_url = resolve_travel_report_url()
                engine = FlightEngine(live_naver=False)
                dest_filter = None if norm_target == "ALL" else norm_target
                deals = engine.search_deals(dest_code=dest_filter, months_ahead=6)
                if not deals and dest_filter is not None:
                    deals = engine.search_deals(dest_code=None, months_ahead=6)
                recs = BudgetCalculator.rank_recommendations(
                    destinations=engine.destinations,
                    deals=deals,
                    top_n=3,
                )
                body_html = format_telegram_briefing(recs[:3], web_report_url=travel_report_url, engine=engine)
                short_lbl = self._resolve_target_short_label(bot, norm_target)
                header_banner = (
                    f"✈️ <b>[직전 발송 맞춤 여행 특가 — {html.escape(short_lbl)}]</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                )
                content_html = self._enforce_tg01_limits(header_banner + body_html)
                kb = [
                    [{"text": "📊 6개월 특가 웹 리포트 ↗", "url": travel_report_url}],
                    [{"text": "🔄 관심 여행지 다시 선택하기", "callback_data": "info:retarget:travel_bot"}],
                ]
            except Exception as exc:
                logger.warning("travel_bot onboarding preview fallback: %s", exc)
                content_html = (
                    f"✈️ <b>[직전 발송 맞춤 여행 특가 ({html.escape(norm_target)})]</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "선택하신 여행지의 주말 3박 4일 왕복 항공권 및 숙박 가성비 특가 브리핑입니다."
                )
                kb = [[{"text": "🔄 관심 여행지 다시 선택하기", "callback_data": "info:retarget:travel_bot"}]]

        # Case 4: Target-Selection Bot — SilverCare Bot (실버케어)
        elif bot.bot_id == "silver_bot":
            try:
                from silver_care.storage.db import SilverCareDatabase
                from silver_care.fixtures.seed_benefits import SEED_BENEFITS
                from silver_care.models import ParentProfile, LivingType
                from silver_care.channels.telegram_card import format_weekly_briefing, format_candidate_list

                mem_db = SilverCareDatabase(":memory:")
                mem_db.upsert_benefits(SEED_BENEFITS)
                living_val = (
                    norm_target
                    if norm_target in {LivingType.ALONE.value, LivingType.COUPLE.value, LivingType.FAM.value}
                    else LivingType.ALONE.value
                )
                sample_parent = ParentProfile(
                    id=1,
                    chat_id=1,
                    alias="부모님",
                    birth_year=1952,
                    sido_code="11",
                    sigungu_name="송파구",
                    living_type=living_val,
                    is_pension_receiver=1,
                    is_low_income=-1,
                )
                ref_year = datetime.now().year
                matches = mem_db.match_for_parent(sample_parent, ref_year)
                short_lbl = self._resolve_target_short_label(bot, norm_target)
                if norm_target == "ALL":
                    body_html = format_weekly_briefing(
                        [(sample_parent, matches[:3])],
                        bookmarked_titles=[],
                        week_label="이번 주",
                        ref_year=ref_year,
                    )
                else:
                    body_html = format_candidate_list(sample_parent, matches, page=0, ref_year=ref_year)
                mem_db.close()
                header_banner = (
                    f"👵 <b>[직전 발송 실버케어 브리핑 — {html.escape(short_lbl)}]</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                )
                content_html = self._enforce_tg01_limits(header_banner + body_html)
                kb = [[{"text": "🔄 부모님 세대 유형 다시 선택하기", "callback_data": "info:retarget:silver_bot"}]]
            except Exception as exc:
                logger.warning("silver_bot onboarding preview fallback: %s", exc)
                content_html = (
                    f"👵 <b>[직전 발송 실버케어 브리핑 ({html.escape(norm_target)})]</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "선택하신 부모님 세대 유형에 맞춘 동사무소·보건소 복지 혜택 및 효도 안부 가이드입니다."
                )
                kb = [[{"text": "🔄 부모님 세대 유형 다시 선택하기", "callback_data": "info:retarget:silver_bot"}]]

        # Case 5: Target-Selection Bot — Kids Compass Bot (자녀·키즈 나침반)
        elif bot.bot_id == "kids_bot":
            try:
                from datetime import timedelta
                from kids_compass.storage.db import KidsCompassDatabase
                from kids_compass.collector import seed_all, recommend_weekend_picks
                from kids_compass.models import AgeGroup
                from kids_compass.channels.telegram import format_weekend_briefing_html, format_dday_alert_html

                mem_db = KidsCompassDatabase(":memory:")
                seed_all(mem_db)
                short_lbl = self._resolve_target_short_label(bot, norm_target)
                now_dt = datetime.now()
                if norm_target == "ALL":
                    events = mem_db.list_upcoming_bookings(
                        now_dt.strftime("%Y-%m-%d %H:%M"),
                        (now_dt + timedelta(days=14)).strftime("%Y-%m-%d %H:%M"),
                    )
                    body_html = format_dday_alert_html(events, now_dt)
                else:
                    try:
                        age_grp = AgeGroup(norm_target)
                    except ValueError:
                        age_grp = AgeGroup.PRESCHOOL
                    picks = recommend_weekend_picks(mem_db, age_grp, "서울", weather_rain=False)
                    body_html = format_weekend_briefing_html(
                        picks, age_grp, "☀️ 맑음 기준 · 야외+실내 균형 추천"
                    )
                mem_db.close()
                header_banner = (
                    f"👶 <b>[직전 발송 키즈 나침반 브리핑 — {html.escape(short_lbl)}]</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                )
                content_html = self._enforce_tg01_limits(header_banner + body_html)
                kb = [[{"text": "🔄 자녀 연령대 다시 선택하기", "callback_data": "info:retarget:kids_bot"}]]
            except Exception as exc:
                logger.warning("kids_bot onboarding preview fallback: %s", exc)
                content_html = (
                    f"👶 <b>[직전 발송 키즈 나침반 브리핑 ({html.escape(norm_target)})]</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "선택하신 자녀 연령대에 맞춘 이번 주말 나들이 추천 및 예약 오픈 D-Day 안내입니다."
                )
                kb = [[{"text": "🔄 자녀 연령대 다시 선택하기", "callback_data": "info:retarget:kids_bot"}]]

        # Case 6: Target-Selection Bot — YOLO Bot (욜로 1인 라이프)
        elif bot.bot_id == "yolo_bot":
            try:
                from yolo_brief.storage.db import YoloDatabase
                from yolo_brief.fixtures.seed_curations import SEED_CURATIONS
                from yolo_brief.models import SoloArchetype, ProfileState, WeatherClass, WeatherSnapshot, YoloUserProfile
                from yolo_brief.matching import match_curations
                from yolo_brief.channels.telegram_card import render_curation_card

                mem_db = YoloDatabase(":memory:")
                mem_db.seed_curations(SEED_CURATIONS)
                curations = mem_db.list_active_curations()
                try:
                    arc = SoloArchetype(norm_target)
                except ValueError:
                    arc = SoloArchetype.URBAN_HIPSTER
                sample_user = YoloUserProfile(
                    chat_id=uid or "preview_user",
                    profile_state=ProfileState.ACTIVE,
                    archetype=arc,
                )
                snap = WeatherSnapshot(weather_class=WeatherClass.CLEAR, pop_percent=10, feels_like_c=20.0)
                match = match_curations(sample_user, curations, snapshot=snap, ref_date=datetime.now().date())
                body_html, _ = render_curation_card(sample_user, match)
                mem_db.close()
                short_lbl = self._resolve_target_short_label(bot, norm_target)
                header_banner = (
                    f"🏄 <b>[직전 발송 욜로봇 큐레이션 — {html.escape(short_lbl)}]</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                )
                content_html = self._enforce_tg01_limits(header_banner + body_html)
                kb = [[{"text": "🔄 1인 라이프 성향 다시 선택하기", "callback_data": "info:retarget:yolo_bot"}]]
            except Exception as exc:
                logger.warning("yolo_bot onboarding preview fallback: %s", exc)
                content_html = (
                    f"🏄 <b>[직전 발송 욜로봇 큐레이션 ({html.escape(norm_target)})]</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    "선택하신 1인 라이프 성향에 맞춘 이번 주 솔로 인증 액티비티·전시·미식 큐레이션입니다."
                )
                kb = [[{"text": "🔄 1인 라이프 성향 다시 선택하기", "callback_data": "info:retarget:yolo_bot"}]]

        # Case 7: Other Info Bots
        else:
            try:
                from market_brief.briefing_service import generate_bot_briefing

                gen = generate_bot_briefing(bot.bot_id, db=self.engine)
                content_html = self._enforce_tg01_limits(gen.get("content_html") or "")
                kb = list(gen.get("inline_keyboard") or [])
            except Exception:
                content_html = (
                    f"📋 <b>[{html.escape(bot.icon_emoji + ' ' + bot.name)}] 직전 발송 브리핑</b>\n"
                    "━━━━━━━━━━━━━━━━━━\n"
                    f"{html.escape(bot.description or '최신 맞춤 브리핑 정보입니다.')}"
                )
                kb = []
            kb.append([{"text": "🔄 대상 다시 선택하기", "callback_data": f"info:retarget:{bot.bot_id}"[:64]}])

        if menu_uname:
            kb.append([{"text": "🧭 메뉴봇(다른 정보봇 보기)", "url": f"https://t.me/{menu_uname}"}])

        result = {"html": content_html, "inline_keyboard": kb}
        self._briefing_cache[cache_key] = (now_mono, result)
        return result

    def _handle_info_bot(
        self,
        bot: BotRecord,
        uid: str,
        full_name: str,
        username: str,
        text: str | None,
        callback_data: str | None,
    ) -> dict[str, Any]:
        cb = (callback_data or "").strip()
        clean_text = (text or "").strip().lower()
        menu_bot = self.engine.get_menu_bot()
        menu_uname = (menu_bot.telegram_username or "").lstrip("@").strip() if menu_bot else ""

        # 1. Explicit Unsubscribe via command or inline button
        if clean_text in {"/stop", "/탈퇴", "/해지", "/구독취소", "/unsubscribe", "탈퇴", "구독취소"} or cb == f"info:unsub:{bot.bot_id}":
            res = self.engine.unsubscribe_bot(uid, bot.bot_id, actor="USER", reason="INFO_BOT_STOP")
            self._delivered_initial_briefing.discard((uid, bot.bot_id))
            user = self.engine.get_user_profile(uid)
            slot_str = format_slot_usage(
                res.get("used_slots", 0),
                res.get("max_slots"),
                res.get("tier", {}).get("is_unlimited", False),
            )
            lines = [
                f"👋 <b>[{html.escape(bot.icon_emoji + ' ' + bot.name)}] 구독 탈퇴 완료</b>"
                if res["success"]
                else f"ℹ️ {html.escape(res['message'])}",
                "",
                html.escape(res["message"]),
                f"📊 현재 구독 슬롯: <code>{html.escape(slot_str)}</code>",
                "이제 다른 정보봇에 접속하거나 메뉴봇에서 새로운 봇을 구독하실 수 있습니다.",
            ]
            kb: list[list[dict[str, Any]]] = [
                [{"text": f"🔄 {bot.icon_emoji} {bot.name} 다시 구독하기", "callback_data": f"info:sub:{bot.bot_id}"}]
            ]
            if menu_uname:
                kb.append([{"text": "🧭 메뉴봇(전체 봇 목록)으로 이동", "url": f"https://t.me/{menu_uname}"}])
            return {
                "bot_id": bot.bot_id,
                "reply_html": "\n".join(lines),
                "inline_keyboard": kb,
                "callback_toast": res["message"] if cb else None,
                "result_code": res["code"],
                "user_profile": user,
            }

        # 2. Target Selection Callback (`info:target:<bot_id>:<target_code>`) for Target-Selection Bots (Type B)
        if cb.startswith("info:target:"):
            parts = cb.split(":")
            target_code = parts[3] if len(parts) >= 4 else "ALL"
            self._user_targets[(uid, bot.bot_id)] = target_code

            full_label = self._resolve_target_full_label(bot, target_code)
            short_label = self._resolve_target_short_label(bot, target_code)
            user = self.engine.get_user_profile(uid)
            lines = [
                f"✅ <b>[{html.escape(bot.icon_emoji + ' ' + bot.name)}] 맞춤 대상 선택 완료!</b>",
                "━━━━━━━━━━━━━━━━━━",
                f"🎯 <b>선택하신 대상:</b> <b>{html.escape(full_label)}</b>",
                "",
                "👇 아래 <b>[📋 직전 발송 정보 보기]</b> 버튼을 누르시면 구독 직전에 발송되었던 최신 맞춤 브리핑을 즉시 확인하실 수 있습니다!",
            ]
            kb = [
                [
                    {
                        "text": f"📋 [{short_label}] 직전 발송 정보 보기",
                        "callback_data": f"info:preview:{bot.bot_id}:{target_code}"[:64],
                    }
                ],
                [
                    {
                        "text": "🔄 대상 다시 선택하기",
                        "callback_data": f"info:retarget:{bot.bot_id}"[:64],
                    }
                ],
                [{"text": f"❌ {bot.name} 구독 탈퇴(해지)", "callback_data": f"info:unsub:{bot.bot_id}"}],
            ]
            if menu_uname:
                kb.append([{"text": "🧭 메뉴봇(다른 정보봇 보기)", "url": f"https://t.me/{menu_uname}"}])
            return {
                "bot_id": bot.bot_id,
                "reply_html": "\n".join(lines),
                "inline_keyboard": kb,
                "callback_toast": f"🎯 [{short_label}] 선택 완료! 정보보기를 눌러주세요.",
                "result_code": "TARGET_SELECTED",
                "selected_target": target_code,
                "user_profile": user,
            }

        # 3. Briefing Preview Callback (`info:preview:<bot_id>[:<target_code>]`)
        if cb.startswith("info:preview:"):
            parts = cb.split(":")
            target_code = parts[3] if len(parts) >= 4 else self._user_targets.get((uid, bot.bot_id), "ALL")
            user = self.engine.get_user_profile(uid)
            brief_payload = self._build_target_briefing(
                bot, target_code=target_code, uid=uid, menu_uname=menu_uname
            )
            return {
                "bot_id": bot.bot_id,
                "reply_html": brief_payload["html"],
                "inline_keyboard": brief_payload["inline_keyboard"],
                "callback_toast": "📋 직전 발송 최신 브리핑입니다!",
                "result_code": "BRIEFING_PREVIEW",
                "selected_target": target_code,
                "user_profile": user,
            }

        # 4. Atomic One-Click Swap callback inside Info Bot (`info:swap:<old_bot>:<this_bot>`)
        if cb.startswith("info:swap:"):
            parts = cb.split(":")
            if len(parts) == 4:
                old_id, new_id = parts[2], parts[3]
                try:
                    res = self.engine.atomic_swap_subscription(uid, old_id, new_id, actor="USER")
                except ValueError as err:
                    res = {"success": False, "code": "STILL_OVER_QUOTA", "message": str(err)}
                user = self.engine.get_user_profile(uid)
                if res["success"]:
                    self._delivered_initial_briefing.discard((uid, old_id))
                    slot_str = format_slot_usage(
                        res["used_slots"], res["max_slots"], res["tier"]["is_unlimited"]
                    )
                    lines = [
                        f"🎉 <b>[{html.escape(bot.icon_emoji + ' ' + bot.name)}] 교체 구독 완료!</b>",
                        "",
                        html.escape(res["message"]),
                        f"👤 회원 등급: <b>{html.escape(res['tier']['name'])}</b> (<code>{html.escape(slot_str)}</code>)",
                        "",
                        f"앞으로 <b>{html.escape(bot.name)}</b>의 핵심 정보를 빠르게 받아보실 수 있습니다.",
                    ]
                    extra_messages: list[dict[str, Any]] = []
                    if self.is_common_briefing_bot(bot.bot_id):
                        lines.append("📢 <b>공통 브리핑 안내:</b> 직전에 발송되었던 최신 브리핑을 함께 보내드립니다.")
                        brief_payload = self._build_target_briefing(bot, target_code="ALL", uid=uid, menu_uname=menu_uname)
                        extra_messages.append(brief_payload)
                        self._delivered_initial_briefing.add((uid, bot.bot_id))
                        kb = [
                            [{"text": "📊 직전 발송 최신 시황 브리핑 보기", "callback_data": f"info:preview:{bot.bot_id}"}],
                            [{"text": f"❌ {bot.name} 구독 탈퇴(해지)", "callback_data": f"info:unsub:{bot.bot_id}"}],
                        ]
                        if menu_uname:
                            kb.append([{"text": "🧭 메뉴봇(전체 봇 목록) 열기", "url": f"https://t.me/{menu_uname}"}])
                    else:
                        cfg = self._get_target_selection_config(bot)
                        lines.append("")
                        lines.append(cfg["prompt"])
                        kb = self._build_target_selection_keyboard(bot, uid, menu_uname)

                    return {
                        "bot_id": bot.bot_id,
                        "reply_html": "\n".join(lines),
                        "inline_keyboard": kb,
                        "extra_messages": extra_messages,
                        "callback_toast": "✅ 기존 봇 탈퇴 및 신규 구독 전환 완료!",
                        "result_code": "SWAPPED",
                        "user_profile": user,
                    }
                return {
                    "bot_id": bot.bot_id,
                    "reply_html": f"⚠️ <b>교체 실패:</b> {html.escape(res['message'])}",
                    "inline_keyboard": [[{"text": "🧭 메뉴봇에서 정리하기", "url": f"https://t.me/{menu_uname}"}]] if menu_uname else [],
                    "callback_toast": res["message"],
                    "result_code": res["code"],
                    "user_profile": user,
                }

        # 5. Default on Info Bot (`/start`, `/구독`, `info:sub:...`, `info:retarget:...`, or any direct greeting/message):
        #    Verify tier quota & auto-subscribe if eligible! (Requirement #4)
        res = self.engine.attempt_subscribe(uid, bot.bot_id, full_name=full_name, username=username, actor="USER")
        user = self.engine.get_user_profile(uid)

        if res["code"] == "QUOTA_EXCEEDED":
            return self._render_quota_exceeded_prompt(
                uid=uid,
                current_bot=bot,
                target_bot_info=res["bot"],
                res=res,
                prefix="info",
                toast="⚠️ 회원 등급별 최대 구독 개수를 초과했습니다.",
            )

        if not res["success"]:
            return {
                "bot_id": bot.bot_id,
                "reply_html": f"⚠️ <b>안내:</b> {html.escape(res['message'])}",
                "inline_keyboard": [],
                "callback_toast": res["message"] if cb else None,
                "result_code": res["code"],
                "user_profile": user,
            }

        tier_info = res["tier"]
        tier_icon = TIER_ICONS.get(tier_info["tier_code"], "⚪")
        slot_str = format_slot_usage(res["used_slots"], res["max_slots"], tier_info["is_unlimited"])

        if res["code"] == "SUBSCRIBED":
            header = f"🎉 <b>[{html.escape(bot.icon_emoji + ' ' + bot.name)}] 구독이 시작되었습니다!</b>"
            sub_note = f"{html.escape(bot.description)}" if bot.description else "유익한 콘텐츠를 전해드리겠습니다."
        else:
            header = f"✅ <b>[{html.escape(bot.icon_emoji + ' ' + bot.name)}] 구독 유지 중</b>"
            sub_note = "이미 정상적으로 구독 중인 정보봇입니다."

        lines = [
            header,
            sub_note,
            "━━━━━━━━━━━━━━━━━━",
            f"👤 <b>회원 등급:</b> {tier_icon} <b>{html.escape(tier_info['name'])}</b>",
            f"📊 <b>구독 현황:</b> <code>{html.escape(slot_str)}</code>",
        ]

        extra_messages: list[dict[str, Any]] = []
        if self.is_common_briefing_bot(bot.bot_id):
            # Type A (Common Bot, e.g. brief_bot):
            # Immediately deliver the most recent briefing upon entering (idempotent per session/subscription)
            should_auto_send = (
                res["code"] == "SUBSCRIBED"
                or (uid, bot.bot_id) not in self._delivered_initial_briefing
            )
            lines.append("")
            lines.append("📢 <b>공통 브리핑 안내:</b> 구독 직전에 발송되었던 최신 시황 브리핑을 즉시 확인하실 수 있습니다.")
            lines.append("💡 <i>구독을 해지(탈퇴)하시려면 언제든 <code>/stop</code>을 입력하거나 아래 버튼을 눌러주세요.</i>")
            if should_auto_send:
                brief_payload = self._build_target_briefing(bot, target_code="ALL", uid=uid, menu_uname=menu_uname)
                extra_messages.append(brief_payload)
                self._delivered_initial_briefing.add((uid, bot.bot_id))

            kb = [
                [{"text": "📊 직전 발송 최신 시황 브리핑 보기", "callback_data": f"info:preview:{bot.bot_id}"}],
                [{"text": f"❌ {bot.name} 구독 탈퇴(해지)", "callback_data": f"info:unsub:{bot.bot_id}"}],
            ]
            if menu_uname:
                kb.append([{"text": "🧭 메뉴봇(다른 정보봇 보기)", "url": f"https://t.me/{menu_uname}"}])
        else:
            # Type B (Target-Selection Bot, e.g. admission_bot, travel_bot):
            # Prompt target selection first -> once selected, user taps [직전 발송 정보 보기] button!
            cfg = self._get_target_selection_config(bot)
            lines.append("")
            lines.append(cfg["prompt"])
            lines.append("💡 <i>구독을 해지(탈퇴)하시려면 언제든 <code>/stop</code>을 입력하거나 아래 버튼을 눌러주세요.</i>")
            kb = self._build_target_selection_keyboard(bot, uid, menu_uname)

        return {
            "bot_id": bot.bot_id,
            "reply_html": "\n".join(lines),
            "inline_keyboard": kb,
            "extra_messages": extra_messages,
            "callback_toast": "🎉 구독이 완료되었습니다!" if (cb and res["code"] == "SUBSCRIBED") else None,
            "result_code": res["code"],
            "user_profile": user,
        }

    def _render_quota_exceeded_prompt(
        self,
        uid: str,
        current_bot: BotRecord,
        target_bot_info: dict[str, Any],
        res: dict[str, Any],
        prefix: str,
        toast: str | None = None,
    ) -> dict[str, Any]:
        tier_info = res["tier"]
        tier_icon = TIER_ICONS.get(tier_info["tier_code"], "⚪")
        active_subs = res.get("active_subscriptions", [])
        active_list_txt = ", ".join([f"<b>{html.escape(s['icon_emoji'] + ' ' + s['bot_name'])}</b>" for s in active_subs])
        target_label = f"{target_bot_info['icon_emoji']} {target_bot_info['name']}"

        lines = [
            "🚫 <b>[구독 가능 봇 개수 초과 안내]</b>",
            "",
            f"회원님은 현재 {tier_icon} <b>{html.escape(tier_info['name'])}</b> 등급으로 동시에 최대 <b>{res['max_slots']}개</b>의 봇만 사용할 수 있습니다.",
            f"📌 <b>현재 구독 중인 봇 ({res['used_slots']}/{res['max_slots']}):</b> {active_list_txt}",
            "",
            f"<b>[{html.escape(target_label)}]</b>을 사용하시려면 기존에 구독 중인 봇을 먼저 탈퇴하셔야 합니다.",
            "👇 아래 버튼을 누르시면 <b>기존 봇 탈퇴와 동시에 이 봇으로 즉시 전환</b>됩니다!",
        ]

        keyboard: list[list[dict[str, Any]]] = []
        for s in active_subs:
            keyboard.append(
                [
                    {
                        "text": f"🔄 '{s['icon_emoji']} {s['bot_name']}' 탈퇴하고 '{target_label}' 구독하기",
                        "callback_data": f"{prefix}:swap:{s['bot_id']}:{target_bot_info['bot_id']}",
                    }
                ]
            )

        if prefix == "menu":
            keyboard.append([{"text": "⬅️ 전체 정보봇 목록으로 돌아가기", "callback_data": "menu:home"}])
        else:
            menu_meta = res.get("menu_bot")
            if menu_meta and menu_meta.get("telegram_username"):
                keyboard.append(
                    [
                        {
                            "text": "🧭 메뉴봇에서 전체 봇·등급 확인하기",
                            "url": f"https://t.me/{menu_meta['telegram_username']}",
                        }
                    ]
                )

        user = self.engine.get_user_profile(uid)
        return {
            "bot_id": current_bot.bot_id,
            "reply_html": "\n".join(lines),
            "inline_keyboard": keyboard,
            "callback_toast": toast,
            "result_code": "QUOTA_EXCEEDED",
            "user_profile": user,
        }


class DynamicBotManager:
    """Centralized Hot-Reloadable Asyncio Polling Manager for Menu & Info Bots."""

    def __init__(self, engine: SubscriptionEngine, router: MultiBotInteractionRouter | None = None):
        self.engine = engine
        self.router = router or MultiBotInteractionRouter(engine)
        self._tasks: dict[str, tuple[str, asyncio.Task]] = {}  # bot_id -> (token, task)
        self._running = False

    def get_worker_statuses(self) -> dict[str, dict[str, Any]]:
        statuses: dict[str, dict[str, Any]] = {}
        for bid, (_, task) in self._tasks.items():
            statuses[bid] = {
                "worker_running": not task.done(),
            }
        return statuses

    def _dispatch_portal_menu_update(self, bot_token: str, item: dict[str, Any]) -> tuple[bool, str]:
        """Dispatches update to portal_hub's full-featured interactive engine."""
        try:
            from portal_hub import PortalDatabase, TelegramPortalClient, handle_callback_query
            from portal_hub.interactive import (
                USER_STATES,
                render_main_menu,
                render_subscription_view,
                render_category_home_view,
                render_tier_benefits_view,
            )
            from portal_bot_runner import (
                MENU_START_COMMANDS,
                MENU_SUBS_COMMANDS,
                MENU_CAT_COMMANDS,
                MENU_TIER_COMMANDS,
                MENU_HELP_COMMANDS,
                MENU_SUPPORT_COMMANDS,
                match_menu_action,
            )
        except ImportError:
            return False, "portal_hub_not_available"

        portal_client = TelegramPortalClient(bot_token)
        portal_db = PortalDatabase()

        if "callback_query" in item:
            cq = item["callback_query"]
            handle_callback_query(cq, portal_db, portal_client)
            return True, "portal_callback_handled"

        if "message" in item:
            msg = item["message"]
            chat = msg.get("chat", {}) or {}
            chat_id = chat.get("id")
            text = (msg.get("text") or "").strip()
            user_info = msg.get("from", {}) or chat
            user_id = user_info.get("id", chat_id)
            if not chat_id or not text:
                return True, "empty_message"

            user = portal_db.get_or_create_user(
                user_id=user_id,
                chat_id=chat_id,
                username=user_info.get("username"),
                first_name=user_info.get("first_name"),
            )

            action = match_menu_action(text)
            is_menu_action = action is not None

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
                            portal_client.send_message(admin_chat, admin_forward)
                        except Exception:
                            pass

                    ack_text = (
                        f"✅ <b>문의사항이 운영팀에 정상 전달되었습니다!</b>\n"
                        f"━━━━━━━━━━━━━━━━━━━━\n"
                        f"작성하신 내용:\n"
                        f"<i>\"{html.escape(text)}\"</i>\n\n"
                        f"운영팀에서 확인 후 빠르게 조치 및 안내해 드리겠습니다. 감사합니다. 🙏"
                    )
                    portal_client.send_message(
                        chat_id, ack_text,
                        reply_markup={"inline_keyboard": [[{"text": "🏠 메인 메뉴로", "callback_data": "nav:main"}]]}
                    )
                    return True, "inquiry_forwarded"
                else:
                    USER_STATES.pop(user_id, None)

            # Auto-register official bot commands menu (blue [/] button)
            if not getattr(self, "_menu_commands_registered", False):
                try:
                    portal_client.set_my_commands([
                        {"command": "start", "description": "🏠 메인 포털 홈"},
                        {"command": "subs", "description": "📋 내 구독 관리"},
                        {"command": "category", "description": "📂 봇 둘러보기"},
                        {"command": "tiers", "description": "💎 내 등급 혜택"},
                        {"command": "support", "description": "💬 고객센터 / 문의"},
                        {"command": "help", "description": "❓ 이용 도움말"},
                    ])
                    self._menu_commands_registered = True
                except Exception as exc:
                    logger.debug("set_my_commands fallback: %s", exc)

            if action == "START":
                from portal_hub.interactive import build_persistent_reply_keyboard
                # Send persistent 2x2 reply keyboard pinned to mobile screen
                portal_client.send_message(chat_id, "👇 아래 메뉴를 선택하세요.", reply_markup=build_persistent_reply_keyboard())
                t_out, m_out = render_main_menu(user)
            elif action == "SUBS":
                subs = portal_db.get_user_subscriptions(user_id)
                t_out, m_out = render_subscription_view(user, subs)
            elif action == "CAT":
                t_out, m_out = render_category_home_view()
            elif action == "TIER":
                t_out, m_out = render_tier_benefits_view(user)
            elif action == "SUPPORT":
                from portal_hub.interactive import render_support_center_view
                t_out, m_out = render_support_center_view(user)
            elif action == "HELP":
                from portal_hub.interactive import render_help_view
                t_out, m_out = render_help_view()
            else:
                from portal_hub.interactive import render_search_results_view
                clean_text = text.lower()
                matched_bots = portal_db.search_bots(clean_text)
                subs = portal_db.get_user_subscriptions(user_id)
                t_out, m_out = render_search_results_view(text, matched_bots, user, subs)

            portal_client.send_message(chat_id, t_out, reply_markup=m_out)
            return True, "portal_message_handled"

        return False, "unsupported_update_type"

    def _dispatch_travel_bot_update(self, bot_token: str, item: dict[str, Any]) -> tuple[bool, str]:
        """Dispatches update to travel_brief's interactive on-demand handler."""
        try:
            from travel_brief.interactive import TravelInteractiveHandler
        except ImportError:
            return False, "travel_interactive_not_available"

        handler = getattr(self, "_travel_handler", None)
        if handler is None or getattr(handler, "bot_token", "") != bot_token:
            handler = TravelInteractiveHandler(bot_token=bot_token)
            self._travel_handler = handler

        if "callback_query" in item:
            cq = item["callback_query"]
            cq_id = str(cq.get("id", ""))
            sender = cq.get("from", {}) or {}
            msg = cq.get("message", {}) or {}
            chat = msg.get("chat", {}) or sender
            chat_id = str(chat.get("id") or sender.get("id") or "")
            msg_id = int(msg.get("message_id") or 0)
            cb_data = str(cq.get("data") or "")

            if cb_data.startswith("trv:"):
                handler.handle_callback_query(
                    cq_id=cq_id,
                    chat_id=chat_id,
                    message_id=msg_id,
                    callback_data=cb_data,
                    from_user=sender,
                )
                return True, "travel_callback_handled"

        return False, "not_travel_interactive_event"

    def _dispatch_admission_bot_update(self, bot_token: str, item: dict[str, Any], opener=None) -> tuple[bool, str]:
        """Dispatches domain-specific admission callbacks (`adm:`, `grade:`, `region:`, `menu:`) to AdmissionInteractiveBot."""
        if "callback_query" not in item:
            return False, "not_admission_callback"
        cq = item["callback_query"]
        cb_data = str(cq.get("data") or "")
        if not cb_data.startswith(("adm:", "grade:", "region:", "menu:")):
            return False, "not_admission_domain_callback"

        try:
            from admission_brief.channels.telegram import TelegramChannel as AdmTelegramChannel
            from admission_brief.interactive_bot import AdmissionInteractiveBot
            from admission_brief.storage.db import AdmissionDatabase
        except ImportError:
            return False, "admission_interactive_not_available"

        adm_db_path = getattr(self.engine.db, "db_path", None) or ":memory:"
        adm_db = AdmissionDatabase(db_path=str(adm_db_path))
        adm_channel = AdmTelegramChannel(bot_token=bot_token, opener=opener)
        adm_bot = AdmissionInteractiveBot(channel=adm_channel, db=adm_db)
        adm_bot.handle_update(item)
        return True, "admission_callback_handled"

    def sync_bot_updates_once(self, bot_id: str, opener=None, timeout: int = 1) -> dict[str, Any]:
        """Synchronously fetches pending Telegram updates for `bot_id`, routes them, and replies."""
        bot = self.engine.get_bot(bot_id)
        if not bot:
            raise KeyError(f"봇을 찾을 수 없습니다: {bot_id}")
        if not bot.token or not bot.token.strip():
            return {"bot_id": bot_id, "processed": 0, "status": "NO_TOKEN"}

        transport = TelegramBotTransport(bot.token, opener=opener)
        last_offset = bot.last_offset or 0
        processed = 0
        events: list[dict[str, Any]] = []

        if last_offset <= 0:
            try:
                stale = transport.get_updates(offset=-1, timeout=1)
                if stale:
                    last_offset = int(stale[-1]["update_id"])
                    self.engine.update_bot_offset(bot.bot_id, last_offset)
                    logger.info("⏩ [초기화] 과거 대기 업데이트 %d건 소거 (offset: %d)", len(stale), last_offset)
            except Exception as exc:
                logger.warning("초기 offset 소거 실패 on %s: %s", bot_id, exc)

        updates = transport.get_updates(offset=last_offset + 1, timeout=timeout)
        for item in updates:
            up_id = int(item.get("update_id", 0))
            if up_id > last_offset:
                last_offset = up_id

            if bot.bot_id == "menu_bot":
                handled, reason = self._dispatch_portal_menu_update(bot.token, item)
                if handled:
                    processed += 1
                    events.append({"user_id": "portal", "type": "portal_integrated", "code": reason})
                    continue

            if bot.bot_id == "travel_bot":
                handled, reason = self._dispatch_travel_bot_update(bot.token, item)
                if handled:
                    processed += 1
                    events.append({"user_id": "travel", "type": "travel_integrated", "code": reason})
                    continue

            if bot.bot_id in ("admission_bot", "edu_bot"):
                handled, reason = self._dispatch_admission_bot_update(bot.token, item, opener=opener)
                if handled:
                    processed += 1
                    events.append({"user_id": "admission", "type": "admission_integrated", "code": reason})
                    continue

            if "callback_query" in item:
                cq = item["callback_query"]
                cq_id = str(cq.get("id", ""))
                sender = cq.get("from", {}) or {}
                msg = cq.get("message", {}) or {}
                chat = msg.get("chat", {}) or sender
                chat_id = str(chat.get("id") or sender.get("id") or "")
                if not chat_id:
                    continue
                full_name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])).strip()
                uname = sender.get("username") or ""
                cb_data = cq.get("data") or ""

                # [TG-04] Pre-emptive callback acknowledgement (<0.1s) before DB/Briefing work
                if cq_id:
                    quick_toast = self.router.get_quick_callback_toast(cb_data)
                    transport.answer_callback(cq_id, text=quick_toast)

                out = self.router.handle_interaction(
                    bot_id=bot.bot_id,
                    telegram_user_id=chat_id,
                    full_name=full_name,
                    username=uname,
                    callback_data=cb_data,
                )
                transport.send_html(chat_id, out["reply_html"], out.get("inline_keyboard"))
                for extra in out.get("extra_messages") or []:
                    if extra.get("html"):
                        transport.send_html(chat_id, extra["html"], extra.get("inline_keyboard"))
                processed += 1
                events.append({"user_id": chat_id, "type": "callback", "code": out["result_code"]})

            elif "message" in item:
                msg = item["message"]
                chat = msg.get("chat", {}) or {}
                if chat.get("type", "private") != "private":
                    # Only enforce personal tier subscriptions on 1:1 private chats
                    continue
                sender = msg.get("from", {}) or chat
                chat_id = str(chat.get("id") or "")
                text = (msg.get("text") or "").strip()
                if not chat_id or not text:
                    continue
                full_name = " ".join(filter(None, [sender.get("first_name"), sender.get("last_name")])).strip()
                if not full_name:
                    full_name = chat.get("first_name") or ""
                uname = sender.get("username") or chat.get("username") or ""

                out = self.router.handle_interaction(
                    bot_id=bot.bot_id,
                    telegram_user_id=chat_id,
                    full_name=full_name,
                    username=uname,
                    text=text,
                )
                transport.send_html(chat_id, out["reply_html"], out.get("inline_keyboard"))
                for extra in out.get("extra_messages") or []:
                    if extra.get("html"):
                        transport.send_html(chat_id, extra["html"], extra.get("inline_keyboard"))
                processed += 1
                events.append({"user_id": chat_id, "type": "message", "code": out["result_code"]})

        if last_offset != (bot.last_offset or 0):
            self.engine.update_bot_offset(bot.bot_id, last_offset)

        return {"bot_id": bot.bot_id, "processed": processed, "last_offset": last_offset, "events": events}

    async def _poll_loop_for_bot(self, bot_id: str) -> None:
        while self._running:
            try:
                await asyncio.to_thread(self.sync_bot_updates_once, bot_id, timeout=25)
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"[DynamicBotManager] Poll warning on {bot_id}: {exc}")
                await asyncio.sleep(3.0)
                continue

    async def start(self) -> None:
        self._running = True
        await self.reload_bots()

    async def stop(self) -> None:
        self._running = False
        for _, (_, task) in list(self._tasks.items()):
            task.cancel()
        if self._tasks:
            await asyncio.gather(*(t for _, t in self._tasks.values()), return_exceptions=True)
        self._tasks.clear()

    async def reload_bots(self) -> dict[str, Any]:
        """Hot-reloads active bot polling tasks without restarting FastAPI."""
        if not self._running:
            return {"active_workers": []}

        bots = self.engine.list_bots(include_raw_token=True)
        desired: dict[str, str] = {
            b["bot_id"]: b["token"].strip()
            for b in bots
            if b["is_active"] and b.get("token") and b["token"].strip()
        }

        # Stop removed, deactivated, or token-changed workers
        for bid in list(self._tasks.keys()):
            old_token, old_task = self._tasks[bid]
            if bid not in desired or desired[bid] != old_token or old_task.done():
                old_task.cancel()
                self._tasks.pop(bid, None)

        # Start newly added or updated workers
        for bid, tok in desired.items():
            if bid not in self._tasks:
                task = asyncio.create_task(self._poll_loop_for_bot(bid), name=f"bot_worker_{bid}")
                self._tasks[bid] = (tok, task)

        return {"active_workers": list(self._tasks.keys())}
