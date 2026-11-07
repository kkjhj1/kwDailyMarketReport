"""Interactive On-Demand Handler & Background Dispatcher for Travel Pathfinder Bot (v2.2).
Equipped with:
1. 0.1s Pre-emptive Callback Acknowledgement (TG-04) to prevent spinner timeout.
2. Per-User 3s Debounce Cooldown + In-Progress Flight Lock.
3. Decoupled ThreadPoolExecutor worker pool to prevent poller loop blocking (C3).
4. Single-Flight caching with 5-minute Global Admin Refresh Cooldown & real cache invalidation (M-F).
5. Safe In-place edit with reply_markup retention & non-destructive empty fallbacks (M-D, M-E).
"""
from __future__ import annotations

import concurrent.futures
import html
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Optional

from travel_brief.channels.telegram import TelegramChannel
from travel_brief.channels.telegram_limiter import SafeTelegramDispatcher
from travel_brief.flight_engine import FlightEngine
from travel_brief.budget_calculator import BudgetCalculator
from travel_brief.models import TravelRecommendation
from travel_brief.naver_live import NaverFlightLiveClient

logger = logging.getLogger(__name__)

USER_COOLDOWN_SECONDS = 3.0
GLOBAL_REFRESH_COOLDOWN_SECONDS = 300.0  # 5 minutes minimum between full live scrapes

CITY_CODE_ALIASES: dict[str, str] = {
    "TYO": "NRT",
    "OSA": "KIX",
}


class UserCooldownManager:
    """Thread-safe cooldown and in-progress tracker using monotonic clocks."""

    def __init__(self, cooldown_sec: float = USER_COOLDOWN_SECONDS):
        self.cooldown_sec = cooldown_sec
        self._lock = threading.Lock()
        self._last_click: dict[str, float] = {}
        self._in_progress_users: set[str] = set()
        self._last_global_refresh: float = 0.0

    def check_and_acquire(
        self, user_id: str, is_force_refresh: bool = False, is_admin: bool = False
    ) -> tuple[bool, str]:
        """Checks if request is allowed. Returns (allowed, toast_message)."""
        now = time.monotonic()
        with self._lock:
            # 1. Check if user already has an active query in progress
            if user_id in self._in_progress_users:
                return False, "이미 최신 정보를 수집 중입니다. 잠시만 기다려주세요."

            # 2. Check per-user 3s cooldown
            last_ts = self._last_click.get(user_id, 0.0)
            if (now - last_ts) < self.cooldown_sec:
                remain = int(self.cooldown_sec - (now - last_ts)) + 1
                return False, f"방금 조회되었습니다. {remain}초 후 다시 시도해주세요."

            # 3. Check global force-refresh cooldown (M7)
            if is_force_refresh and not is_admin:
                elapsed_global = now - self._last_global_refresh
                if elapsed_global < GLOBAL_REFRESH_COOLDOWN_SECONDS:
                    wait_min = int((GLOBAL_REFRESH_COOLDOWN_SECONDS - elapsed_global) // 60) + 1
                    return False, f"전체 새로고침은 {wait_min}분 후에 가능합니다 (기존 캐시 제공)."

            # Acquire state
            self._last_click[user_id] = now
            self._in_progress_users.add(user_id)
            if is_force_refresh:
                self._last_global_refresh = now

            return True, "✈️ 최신 실시간 항공권을 조회 중입니다..."

    def release(self, user_id: str) -> None:
        """Releases the in-progress lock for the user."""
        with self._lock:
            self._in_progress_users.discard(user_id)


class TravelInteractiveHandler:
    """Manages Telegram callback queries for the Travel Bot without blocking the polling loop."""

    def __init__(
        self,
        bot_token: str,
        engine: Optional[FlightEngine] = None,
        max_workers: int = 2,
        report_url: str = "https://kkjhj1.github.io/kwDailyMarketReport/reports/travel_latest.html",
    ):
        self.bot_token = bot_token
        self.engine = engine or FlightEngine()
        self.report_url = report_url
        self.cooldown = UserCooldownManager()
        self.worker_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="TravelWorker"
        )
        self.tg = TelegramChannel(bot_token=bot_token)
        self.dispatcher = SafeTelegramDispatcher()
        self._admin_ids = self._load_admin_ids()

    def _load_admin_ids(self) -> set[str]:
        raw = os.environ.get("ADMIN_TELEGRAM_ID", "") or os.environ.get("TELEGRAM_CHAT_ID", "")
        return {r.strip() for r in raw.split(",") if r.strip()}

    def build_inline_keyboard(self, current_filter: str = "ALL") -> dict[str, Any]:
        """Constructs mobile-optimized slim keyboard within 64-byte callback_data limits."""
        return {
            "inline_keyboard": [
                [
                    {"text": "🔄 최신 특가 새로고침", "callback_data": "trv:r:ALL"},
                ],
                [
                    {"text": "🇯🇵 도쿄", "callback_data": "trv:q:NRT"},
                    {"text": "🇯🇵 오사카", "callback_data": "trv:q:KIX"},
                    {"text": "🇯🇵 후쿠오카", "callback_data": "trv:q:FUK"},
                ],
                [
                    {"text": "🇹🇼 타이베이", "callback_data": "trv:q:TPE"},
                    {"text": "🇻🇳 다낭", "callback_data": "trv:q:DAD"},
                    {"text": "🇹🇭 방콕", "callback_data": "trv:q:BKK"},
                ],
                [
                    {"text": "📊 6개월 특가 웹 리포트 ↗", "url": self.report_url},
                ],
            ]
        }

    def handle_callback_query(
        self,
        cq_id: str,
        chat_id: str,
        message_id: int,
        callback_data: str,
        from_user: dict[str, Any],
    ) -> None:
        """Entry point for incoming callback queries.
        CRITICAL ARCHITECTURE GUARANTEES:
        1. Evaluates cooldown in <0.001s.
        2. Dispatches answerCallbackQuery IMMEDIATELY (TG-04) - 0.1s target.
        3. Hands heavy scraping to background worker pool so the polling loop is NEVER blocked (C3).
        """
        user_id = str(from_user.get("id") or chat_id).strip()
        is_admin = user_id in self._admin_ids

        # Parse command: e.g. "trv:q:NRT", "trv:r:ALL"
        parts = callback_data.split(":")
        action = parts[1] if len(parts) > 1 else "q"
        target = parts[2] if len(parts) > 2 else "ALL"
        target_norm = CITY_CODE_ALIASES.get(target, target)
        is_refresh = (action == "r")

        # 1. Evaluate cooldown & in-progress state
        allowed, toast = self.cooldown.check_and_acquire(
            user_id=user_id, is_force_refresh=is_refresh, is_admin=is_admin
        )

        # 2. Immediate single callback acknowledgement (TG-04 / M4)
        try:
            self.tg.answer_callback(cq_id, text=toast, show_alert=False)
        except Exception as exc:
            logger.warning("answerCallbackQuery failed for %s: %s", cq_id, exc)

        if not allowed:
            # Blocked by cooldown or already in-progress; no background task submitted
            return

        # If force refresh, invalidate memory and disk cache so single-flight fetches fresh data (M-F)
        if is_refresh:
            if hasattr(self.engine, "_live_client") and self.engine._live_client:
                self.engine._live_client.invalidate_cache()
            else:
                NaverFlightLiveClient.invalidate_global_cache()

        # 3. Offload execution to decoupled background worker (C3)
        self.worker_pool.submit(
            self._async_execute_and_update,
            chat_id=chat_id,
            message_id=message_id,
            target_city=target_norm,
            user_id=user_id,
        )

    def _async_execute_and_update(
        self, chat_id: str, message_id: int, target_city: str, user_id: str
    ) -> None:
        """Runs in background worker thread to collect deals and edit Telegram message in-place."""
        try:
            # C-A fix: use real existing methods search_deals and rank_recommendations
            target_dest = None if target_city == "ALL" else target_city
            all_deals = self.engine.search_deals(dest_code=target_dest, months_ahead=6)
            recs = BudgetCalculator.rank_recommendations(
                destinations=self.engine.destinations,
                deals=all_deals,
                top_n=3,
            )

            # M-D fix: if no recommendations found, do NOT overwrite with blank text
            if not recs:
                logger.warning("On-demand query yielded empty recommendations for target %s; skipping edit.", target_city)
                return

            # Format updated message
            from travel_bot_runner import format_telegram_briefing
            new_text = format_telegram_briefing(
                recommendations=recs[:3],
                web_report_url=self.report_url,
                engine=self.engine,
            )
            markup = self.build_inline_keyboard(current_filter=target_city)

            # Safe in-place edit (M-E)
            self._safe_edit_message(
                chat_id=chat_id,
                message_id=message_id,
                text=new_text,
                reply_markup=markup,
            )
        except Exception as exc:
            logger.error("Background on-demand update failed for user %s: %s", user_id, exc, exc_info=True)
        finally:
            self.cooldown.release(user_id)

    def _safe_edit_message(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> None:
        """Safely edits message with retry-after handling, not-modified idempotency, and send fallback (M-E)."""
        if not chat_id:
            return

        body: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            body["reply_markup"] = reply_markup

        if message_id and message_id > 0:
            body["message_id"] = message_id
            edit_url = f"https://api.telegram.org/bot{self.bot_token}/editMessageText"
            for attempt in range(2):
                try:
                    req = urllib.request.Request(
                        edit_url,
                        data=json.dumps(body).encode("utf-8"),
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = json.load(resp)
                    if data.get("ok"):
                        return
                except urllib.error.HTTPError as http_err:
                    err_desc = ""
                    try:
                        err_json = json.loads(http_err.read().decode("utf-8", errors="ignore"))
                        err_desc = err_json.get("description", "")
                    except Exception:
                        err_desc = str(http_err)

                    if "message is not modified" in err_desc.lower():
                        logger.debug("Message text and keyboard are already up-to-date.")
                        return

                    if http_err.code == 429 and attempt == 0:
                        import re
                        m = re.search(r"retry after (\d+)", err_desc.lower())
                        sec = int(m.group(1)) if m else 2
                        time.sleep(sec + 0.5)
                        continue

                    # If message was deleted or cannot be edited, fallback to send_text below
                    if "message to edit not found" in err_desc.lower() or "message can't be edited" in err_desc.lower():
                        break
                    logger.warning("editMessageText failed HTTP %s: %s", http_err.code, err_desc)
                    return
                except Exception as exc:
                    logger.warning("editMessageText network exception: %s", exc)
                    return

        # Fallback: send fresh message if edit not applicable
        try:
            self.dispatcher.send_text_to_recipient(
                tg=self.tg,
                chat_id=chat_id,
                text=text,
                parse_mode="HTML",
                reply_markup=reply_markup,
            )
        except Exception as exc:
            logger.warning("Fallback send_text_to_recipient failed: %s", exc)

    def shutdown(self) -> None:
        """Shuts down background workers cleanly."""
        self.worker_pool.shutdown(wait=False)
