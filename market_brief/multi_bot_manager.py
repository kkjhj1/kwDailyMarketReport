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
import html
import json
import logging
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

    def get_updates(self, offset: int = 0, timeout: int = 3) -> list[dict[str, Any]]:
        try:
            res = self.api_call(
                "getUpdates",
                {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
                timeout=timeout + 6,
            )
            return res.get("result", [])
        except RuntimeError as err:
            err_msg = str(err).lower()
            if "webhook is active" in err_msg or "conflict" in err_msg:
                logger.info(f"Active webhook detected on bot. Auto-deleting webhook to enable polling...")
                self.delete_webhook(drop_pending_updates=False)
                res = self.api_call(
                    "getUpdates",
                    {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
                    timeout=timeout + 6,
                )
                return res.get("result", [])
            raise

    def send_html(
        self,
        chat_id: str,
        html_text: str,
        inline_keyboard: list[list[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": str(chat_id).strip(),
            "text": html_text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if inline_keyboard:
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


class MultiBotInteractionRouter:
    """Pure, deterministic interaction router shared by live Telegram bots and the Admin Simulator."""

    def __init__(self, engine: SubscriptionEngine):
        self.engine = engine

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

        # 2. Atomic One-Click Swap callback inside Info Bot (`info:swap:<old_bot>:<this_bot>`)
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
                    kb = [[{"text": f"❌ {bot.name} 구독 탈퇴(해지)", "callback_data": f"info:unsub:{bot.bot_id}"}]]
                    if menu_uname:
                        kb.append([{"text": "🧭 메뉴봇(전체 봇 목록) 열기", "url": f"https://t.me/{menu_uname}"}])
                    return {
                        "bot_id": bot.bot_id,
                        "reply_html": "\n".join(lines),
                        "inline_keyboard": kb,
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

        # 3. Default on Info Bot (`/start`, `/구독`, `info:sub:...`, or any direct greeting/message):
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
            "",
            "💡 <i>구독을 해지(탈퇴)하시려면 언제든 <code>/stop</code>을 입력하거나 아래 버튼을 눌러주세요.</i>",
        ]
        kb = [[{"text": f"❌ {bot.name} 구독 탈퇴(해지)", "callback_data": f"info:unsub:{bot.bot_id}"}]]
        if menu_uname:
            kb.append([{"text": "🧭 메뉴봇(다른 정보봇 보기)", "url": f"https://t.me/{menu_uname}"}])

        return {
            "bot_id": bot.bot_id,
            "reply_html": "\n".join(lines),
            "inline_keyboard": kb,
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

    def sync_bot_updates_once(self, bot_id: str, opener=None) -> dict[str, Any]:
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

        updates = transport.get_updates(offset=last_offset + 1, timeout=1)
        for item in updates:
            up_id = int(item.get("update_id", 0))
            if up_id > last_offset:
                last_offset = up_id

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

                out = self.router.handle_interaction(
                    bot_id=bot.bot_id,
                    telegram_user_id=chat_id,
                    full_name=full_name,
                    username=uname,
                    callback_data=cb_data,
                )
                if cq_id:
                    transport.answer_callback(cq_id, text=out.get("callback_toast") or "")
                transport.send_html(chat_id, out["reply_html"], out.get("inline_keyboard"))
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
                processed += 1
                events.append({"user_id": chat_id, "type": "message", "code": out["result_code"]})

        if last_offset != (bot.last_offset or 0):
            self.engine.update_bot_offset(bot.bot_id, last_offset)

        return {"bot_id": bot.bot_id, "processed": processed, "last_offset": last_offset, "events": events}

    async def _poll_loop_for_bot(self, bot_id: str) -> None:
        while self._running:
            try:
                await asyncio.to_thread(self.sync_bot_updates_once, bot_id)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"[DynamicBotManager] Poll warning on {bot_id}: {exc}")
                await asyncio.sleep(5.0)
                continue
            await asyncio.sleep(2.0)

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
