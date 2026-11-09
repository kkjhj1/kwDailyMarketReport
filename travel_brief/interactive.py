"""Interactive On-Demand Handler & Background Dispatcher for Travel Pathfinder Bot (v2.2).
Equipped with:
1. 0.1s Pre-emptive Callback Acknowledgement (TG-04) to prevent spinner timeout.
2. Per-User 3s Debounce Cooldown + In-Progress Flight Lock.
3. Decoupled ThreadPoolExecutor worker pool to prevent poller loop blocking (C3).
4. Single-Flight caching with 5-minute Global Admin Refresh Cooldown & real cache invalidation (M-F).
5. Safe In-place edit with reply_markup retention & non-destructive empty fallbacks (M-D, M-E).
6. Instant in-place loading card + per-message request sequence guard so a slower, older query
   (e.g. Tokyo) never overwrites a newer one (e.g. Osaka), and failures always end in an error card.
7. Single-city hero deal SSE verification with re-rank (up to 2 rounds).
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
from collections import OrderedDict
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
MAX_TRACKED_MESSAGES = 5000  # LRU bound for per-message sequence/lock registries
SSE_RERANK_ROUNDS = 2

CITY_CODE_ALIASES: dict[str, str] = {
    "TYO": "NRT",
    "OSA": "KIX",
}

CITY_LABELS: dict[str, str] = {
    "NRT": "🇯🇵 도쿄",
    "KIX": "🇯🇵 오사카",
    "FUK": "🇯🇵 후쿠오카",
    "TPE": "🇹🇼 타이베이",
    "DAD": "🇻🇳 다낭",
    "BKK": "🇹🇭 방콕",
    "ALL": "✈️ 전체 주말 특가 TOP 3",
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
        report_url: str = "http://161.33.196.211:8002/reports/travel_latest.html",
    ):
        self.bot_token = bot_token
        self.engine = engine or FlightEngine()
        self.report_url = report_url
        self.cooldown = UserCooldownManager()
        self.worker_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=max_workers, thread_name_prefix="TravelWorker"
        )
        # Separate tiny pool for lightweight UI edits (loading cards) so they never queue behind scrapes.
        self.ui_pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="TravelUI"
        )
        self.tg = TelegramChannel(bot_token=bot_token)
        self.dispatcher = SafeTelegramDispatcher()
        self._admin_ids = self._load_admin_ids()
        # Per-message request sequencing: (chat_id, message_id) -> latest seq / final-rendered seq / edit lock.
        self._seq_lock = threading.Lock()
        self._msg_seq: OrderedDict[tuple[str, int], int] = OrderedDict()
        self._msg_final_seq: dict[tuple[str, int], int] = {}
        self._msg_locks: dict[tuple[str, int], threading.Lock] = {}

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

    def build_onboarding_keyboard(self) -> dict[str, Any]:
        """Step 1 onboarding target-selection keyboard for new subscribers (64-byte safe `trv:s:<CITY>`)."""
        return {
            "inline_keyboard": [
                [
                    {"text": "🇯🇵 도쿄", "callback_data": "trv:s:NRT"},
                    {"text": "🇯🇵 오사카", "callback_data": "trv:s:KIX"},
                    {"text": "🇯🇵 후쿠오카", "callback_data": "trv:s:FUK"},
                ],
                [
                    {"text": "🇹🇼 타이베이", "callback_data": "trv:s:TPE"},
                    {"text": "🇻🇳 다낭", "callback_data": "trv:s:DAD"},
                    {"text": "🇹🇭 방콕", "callback_data": "trv:s:BKK"},
                ],
                [
                    {"text": "✈️ 전체 주말 특가 TOP 3 선택", "callback_data": "trv:s:ALL"},
                ],
                [
                    {"text": "📊 6개월 특가 웹 리포트 ↗", "url": self.report_url},
                ],
                [
                    {"text": "🏠 통합 메뉴봇 바로가기", "url": "https://t.me/KwMenu_bot?start=from_sub"},
                ],
            ]
        }

    def build_selected_target_keyboard(self, target_city: str = "ALL") -> dict[str, Any]:
        """Step 2 keyboard shown right after target selection, exposing [📋 직전 발송 특가 정보 보기]."""
        city_norm = CITY_CODE_ALIASES.get(target_city, target_city)
        label = CITY_LABELS.get(city_norm, city_norm)
        return {
            "inline_keyboard": [
                [
                    {
                        "text": f"📋 [{label}] 직전 발송 특가 정보 보기",
                        "callback_data": f"trv:q:{city_norm}"[:64],
                    }
                ],
                [
                    {"text": "🇯🇵 도쿄", "callback_data": "trv:s:NRT"},
                    {"text": "🇯🇵 오사카", "callback_data": "trv:s:KIX"},
                    {"text": "🇯🇵 후쿠오카", "callback_data": "trv:s:FUK"},
                ],
                [
                    {"text": "🇹🇼 타이베이", "callback_data": "trv:s:TPE"},
                    {"text": "🇻🇳 다낭", "callback_data": "trv:s:DAD"},
                    {"text": "🇹🇭 방콕", "callback_data": "trv:s:BKK"},
                ],
                [
                    {"text": "📊 6개월 특가 웹 리포트 ↗", "url": self.report_url},
                ],
                [
                    {"text": "🏠 통합 메뉴봇 바로가기", "url": "https://t.me/KwMenu_bot?start=from_sub"},
                ],
            ]
        }

    # ------------------------------------------------------------------
    # Per-message sequence guard
    # ------------------------------------------------------------------
    def _next_request_seq(self, chat_id: str, message_id: int) -> int:
        """Registers a new accepted request for this message and returns its sequence (O(1), polling-thread safe)."""
        key = (str(chat_id), int(message_id or 0))
        with self._seq_lock:
            seq = self._msg_seq.get(key, 0) + 1
            self._msg_seq[key] = seq
            self._msg_seq.move_to_end(key)
            self._msg_locks.setdefault(key, threading.Lock())
            while len(self._msg_seq) > MAX_TRACKED_MESSAGES:
                old_key, _ = self._msg_seq.popitem(last=False)
                self._msg_final_seq.pop(old_key, None)
                self._msg_locks.pop(old_key, None)
            return seq

    def _is_latest_seq(self, chat_id: str, message_id: int, seq: Optional[int]) -> bool:
        if seq is None:
            return True
        with self._seq_lock:
            return self._msg_seq.get((str(chat_id), int(message_id or 0))) == seq

    def _get_msg_lock(self, chat_id: str, message_id: int) -> threading.Lock:
        key = (str(chat_id), int(message_id or 0))
        with self._seq_lock:
            return self._msg_locks.setdefault(key, threading.Lock())

    def _edit_if_latest(
        self,
        chat_id: str,
        message_id: int,
        request_seq: Optional[int],
        text: str,
        reply_markup: Optional[dict[str, Any]] = None,
        is_loading: bool = False,
    ) -> bool:
        """Edits the message only if `request_seq` is still the newest request for it.
        "Check sequence then edit" runs under a per-message lock taken only by worker threads, so an
        older result can never land after a newer one. A loading card additionally never overwrites
        the final result of the same request. Returns True when an edit was issued.
        """
        if request_seq is None:
            if is_loading:
                self._edit_in_place_only(chat_id, message_id, text, reply_markup)
            else:
                self._safe_edit_message(
                    chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup
                )
            return True

        key = (str(chat_id), int(message_id or 0))
        with self._get_msg_lock(chat_id, message_id):
            if not self._is_latest_seq(chat_id, message_id, request_seq):
                logger.info("Skipping superseded edit for %s (seq=%s, loading=%s).", key, request_seq, is_loading)
                return False
            if is_loading:
                with self._seq_lock:
                    if self._msg_final_seq.get(key, 0) >= request_seq:
                        return False
                self._edit_in_place_only(chat_id, message_id, text, reply_markup)
            else:
                self._safe_edit_message(
                    chat_id=chat_id, message_id=message_id, text=text, reply_markup=reply_markup
                )
                with self._seq_lock:
                    self._msg_final_seq[key] = max(self._msg_final_seq.get(key, 0), request_seq)
            return True

    # ------------------------------------------------------------------
    # Transient cards (TG-01/TG-02: short, fully escaped)
    # ------------------------------------------------------------------
    @staticmethod
    def build_loading_card(target_city: str, is_refresh: bool = False) -> str:
        label = "🔄 전체 최신 특가" if is_refresh else CITY_LABELS.get(target_city, target_city)
        return (
            f"✈️ <b>[{html.escape(label)} 실시간 특가 검색 중...]</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"⏳ 네이버 항공권 실시간 최저가를 조회하고 있습니다.\n"
            f"💡 조회가 끝나면 최신 요금과 예약 링크로 자동 갱신됩니다."
        )

    @staticmethod
    def build_error_card(target_city: str, reason: str) -> str:
        label = CITY_LABELS.get(target_city, target_city)
        return (
            f"⚠️ <b>[{html.escape(label)} 특가 조회 실패]</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"{html.escape(reason)}\n"
            f"🔄 잠시 후 아래 버튼으로 다시 시도해 주세요."
        )

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

        # Parse command: e.g. "trv:s:NRT", "trv:q:NRT", "trv:r:ALL"
        parts = callback_data.split(":")
        action = parts[1] if len(parts) > 1 else "q"
        target = parts[2] if len(parts) > 2 else "ALL"
        target_norm = CITY_CODE_ALIASES.get(target, target)

        # Target-selection step (`trv:s:<CITY>`): acknowledge immediately & show [📋 직전 발송 특가 정보 보기] button
        if action == "s":
            city_label = CITY_LABELS.get(target_norm, target_norm)
            try:
                self.tg.answer_callback(
                    cq_id,
                    text=f"🎯 {city_label} 선택 완료! [직전 발송 특가 정보 보기] 버튼을 눌러주세요.",
                    show_alert=False,
                )
            except Exception as exc:
                logger.warning("answerCallbackQuery failed for %s: %s", cq_id, exc)

            confirm_html = (
                f"✅ <b>[트래블 패스파인더 맞춤 여행지 선택 완료]</b>\n"
                f"━━━━━━━━━━━━━━━━━━\n"
                f"🎯 <b>선택하신 여행지:</b> <b>{html.escape(city_label)}</b>\n\n"
                f"👇 아래 <b>[📋 직전 발송 특가 정보 보기]</b> 버튼을 누르시면 "
                f"구독 직전에 발송되었던 최신 주말 항공·숙박 특가 브리핑을 즉시 확인하실 수 있습니다!"
            )
            markup = self.build_selected_target_keyboard(target_norm)
            s_seq = self._next_request_seq(chat_id, message_id)
            self.worker_pool.submit(
                self._edit_if_latest,
                chat_id=chat_id,
                message_id=message_id,
                request_seq=s_seq,
                text=confirm_html,
                reply_markup=markup,
            )
            return

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

        # 3. Register request sequence & show instant in-place loading card (edit-only, no send fallback)
        request_seq = self._next_request_seq(chat_id, message_id)
        loading_future: Optional[concurrent.futures.Future] = None
        try:
            loading_future = self.ui_pool.submit(
                self._edit_if_latest,
                chat_id=chat_id,
                message_id=message_id,
                request_seq=request_seq,
                text=self.build_loading_card(target_norm, is_refresh=is_refresh),
                reply_markup=self.build_inline_keyboard(current_filter=target_norm),
                is_loading=True,
            )
        except Exception as exc:
            logger.warning("Loading card submission skipped: %s", exc)

        # 4. Offload execution to decoupled background worker (C3)
        try:
            self.worker_pool.submit(
                self._async_execute_and_update,
                chat_id=chat_id,
                message_id=message_id,
                target_city=target_norm,
                user_id=user_id,
                request_seq=request_seq,
                loading_future=loading_future,
            )
        except Exception as exc:
            logger.error("Worker submission failed for %s: %s", user_id, exc)
            self.cooldown.release(user_id)

    def _async_execute_and_update(
        self,
        chat_id: str,
        message_id: int,
        target_city: str,
        user_id: str,
        request_seq: Optional[int] = None,
        loading_future: Optional[concurrent.futures.Future] = None,
    ) -> None:
        """Runs in background worker thread to collect deals and edit Telegram message in-place.
        Always ends with either the briefing or an error card (never a stuck loading card).
        """
        markup = self.build_inline_keyboard(current_filter=target_city)
        try:
            # C-A fix: use real existing methods search_deals and rank_recommendations
            target_dest = None if target_city == "ALL" else target_city
            all_deals = self.engine.search_deals(dest_code=target_dest, months_ahead=6)
            recs = BudgetCalculator.rank_recommendations(
                destinations=self.engine.destinations,
                deals=all_deals,
                top_n=3,
            )

            # Single-city: SSE-verify the hero deal and re-rank (a price surge may promote another date)
            if recs and target_dest and getattr(self.engine, "live_naver", False):
                recs = self._verify_hero_with_rerank(all_deals, recs)

            # M-D fix: never overwrite with blank text, but never leave the loading card stuck either
            if not recs:
                logger.warning("On-demand query yielded empty recommendations for target %s.", target_city)
                self._finish_with_card(
                    chat_id, message_id, request_seq, loading_future,
                    self.build_error_card(target_city, "현재 조회 가능한 직항 특가가 없습니다."),
                    markup,
                )
                return

            # Format updated message
            from travel_bot_runner import format_telegram_briefing
            new_text = format_telegram_briefing(
                recommendations=recs[:3],
                web_report_url=self.report_url,
                engine=self.engine,
                target_city=target_city,
            )

            # Safe in-place edit (M-E), sequence-guarded
            self._finish_with_card(chat_id, message_id, request_seq, loading_future, new_text, markup)
        except Exception as exc:
            logger.error("Background on-demand update failed for user %s: %s", user_id, exc, exc_info=True)
            try:
                self._finish_with_card(
                    chat_id, message_id, request_seq, loading_future,
                    self.build_error_card(target_city, "네이버 항공권 조회 중 일시적인 오류가 발생했습니다."),
                    markup,
                )
            except Exception as card_exc:
                logger.error("Error card edit failed for user %s: %s", user_id, card_exc)
        finally:
            self.cooldown.release(user_id)

    def _finish_with_card(
        self,
        chat_id: str,
        message_id: int,
        request_seq: Optional[int],
        loading_future: Optional[concurrent.futures.Future],
        text: str,
        markup: dict[str, Any],
    ) -> None:
        # Cancel a not-yet-started loading edit; if it is already running, the per-message lock and
        # final-seq marker in `_edit_if_latest` guarantee it can never overwrite this result.
        if loading_future is not None:
            loading_future.cancel()
        self._edit_if_latest(
            chat_id=chat_id,
            message_id=message_id,
            request_seq=request_seq,
            text=text,
            reply_markup=markup,
        )

    def _verify_hero_with_rerank(
        self, deals: list[Any], recs: list[TravelRecommendation]
    ) -> list[TravelRecommendation]:
        """SSE-verifies the #1 deal, re-ranks, and repeats if a different unverified deal becomes #1.
        On SSE timeout/empty the deal stays LIVE_PG and is labelled as price-graph data by the formatter.
        """
        current = list(deals)
        attempted: set[tuple[str, str, str]] = set()
        for _ in range(SSE_RERANK_ROUNDS):
            if not recs:
                break
            hero = recs[0].best_deal
            k = (hero.destination_code, hero.departure_date, hero.return_date)
            if hero.data_source != "LIVE_PG" or k in attempted:
                break
            attempted.add(k)
            try:
                verified = self.engine.ensure_deals_sse_verified([hero])
            except Exception as exc:
                logger.warning("Hero SSE verification failed for %s: %s", k, exc)
                break
            if not verified or verified[0].data_source != "LIVE_SSE":
                break
            upgraded = verified[0]
            current = [
                upgraded if (d.destination_code, d.departure_date, d.return_date) == k else d
                for d in current
            ]
            recs = BudgetCalculator.rank_recommendations(
                destinations=self.engine.destinations,
                deals=current,
                top_n=3,
            )
        return recs

    def _safe_edit_message(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> None:
        """Safely edits message with retry-after handling, not-modified idempotency, and send fallback (M-E)."""
        self._edit_message_core(chat_id, message_id, text, reply_markup, allow_send_fallback=True)

    def _edit_in_place_only(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: Optional[dict[str, Any]] = None,
    ) -> None:
        """Edit-only variant for transient loading cards: never sends a new message on failure."""
        self._edit_message_core(chat_id, message_id, text, reply_markup, allow_send_fallback=False)

    def _edit_message_core(
        self,
        chat_id: str,
        message_id: int,
        text: str,
        reply_markup: Optional[dict[str, Any]] = None,
        allow_send_fallback: bool = True,
    ) -> None:
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

        if not allow_send_fallback:
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
        self.ui_pool.shutdown(wait=False)
