"""Interactive state-machine & callback router for YOLO Bot (욜로봇).
Enforces:
- [TG-04] Immediate `answer_callback` BEFORE any DB or weather lookups.
- Strict callback_data allowlist validation (<= 64 bytes).
- State transitions: ONBOARDING -> ACTIVE -> UNSUBSCRIBED.
- Weekly regular & weather-aware briefing dispatch via `SafeTelegramDispatcher` ([TG-05]).
"""
from __future__ import annotations

from datetime import date, datetime
import logging
from typing import Any, Optional

from market_brief.channels.http_keepalive import answer_callback_once, callback_ack_scope

from yolo_brief.channels.telegram import YoloTelegramChannel
from yolo_brief.channels.telegram_card import (
    render_area_menu_card,
    render_curation_card,
    render_guide_card,
    render_onboarding_card,
    render_profile_card,
    render_saved_locker_card,
    render_unsub_confirm_card,
    render_unsubscribed_card,
    validate_callback_data,
)
from yolo_brief.channels.telegram_limiter import SafeTelegramDispatcher
from yolo_brief.matching import MatchResult, match_curations
from yolo_brief.models import (
    AREA_SHORT_CODES,
    KST,
    SoloArchetype,
    TimeSlot,
    VALID_MBTI_TYPES,
    WeatherClass,
    WeatherSnapshot,
    YoloUserProfile,
)
from yolo_brief.storage.db import YoloDatabase
from yolo_brief.weather_client import get_default_weather_client, unknown_snapshot
from yolo_brief.weather_engine import get_iso_week_key, get_kst_season

logger = logging.getLogger(__name__)

ALLOWED_STATIC_CALLBACKS = {
    "yolo:menu",
    "yolo:profile",
    "yolo:view:weekend",
    "yolo:view:rain",
    "yolo:view:next",
    "yolo:view:saved",
    "yolo:area:menu",
    "yolo:cfg:fb:0",
    "yolo:cfg:fb:1",
    "yolo:unsub:ask",
    "yolo:unsub:ok",
    "yolo:unsub:no",
}
ALLOWED_ARCHETYPE_CALLBACKS = {
    f"yolo:set:{arc.value}": arc for arc in SoloArchetype
}
ALLOWED_SLOT_CALLBACKS = {
    f"yolo:slot:{slot.callback_code}": slot for slot in TimeSlot
}
ALLOWED_AREA_CALLBACKS = {
    f"yolo:area:{short}": code for short, code in AREA_SHORT_CODES.items()
}
ALLOWED_MBTI_CALLBACKS = {
    f"yolo:mbti:{code}": code for code in VALID_MBTI_TYPES
}
ALLOWED_LEVEL_CALLBACKS = {
    f"yolo:lvl:{lvl}": lvl for lvl in range(1, 6)
}
_LOG_ACTION_MAP = {
    "sav": "SAVED",
    "done": "DONE",
    "skip": "SKIP",
}


class YoloInteractiveBot:
    """Handles Telegram `/start`, inline button callbacks, and weekly dispatches."""

    def __init__(
        self,
        db: YoloDatabase,
        channel: YoloTelegramChannel,
        dispatcher: Optional[SafeTelegramDispatcher] = None,
        default_weather: Optional[WeatherSnapshot] = None,
        weather_client: Optional[Any] = None,
    ) -> None:
        self.db = db
        self.channel = channel
        self.dispatcher = dispatcher or SafeTelegramDispatcher()
        # [INV-02] Fail-Safe: never default to an optimistic CLEAR snapshot.
        self.default_weather = default_weather
        self.weather_client = weather_client

    def _resolve_weather(
        self,
        area_code: str,
        weather_override: Optional[WeatherSnapshot] = None,
    ) -> WeatherSnapshot:
        if weather_override is not None:
            return weather_override
        if self.default_weather is not None:
            return self.default_weather
        client = self.weather_client
        if client is None:
            # [TG-06] In dry_run / unit tests without an injected client, never hit the live network.
            if getattr(self.channel, "dry_run", False):
                return unknown_snapshot(area_code)
            client = get_default_weather_client()
        try:
            return client.get_weather(area_code)
        except Exception as exc:
            logger.warning("[YOLO weather] client lookup failed for %s: %s", area_code, exc)
            return unknown_snapshot(area_code)

    def _match_for_user(
        self,
        user: YoloUserProfile,
        weather_override: Optional[WeatherSnapshot] = None,
        ref_date: Optional[date] = None,
    ) -> MatchResult:
        curations = self.db.list_active_curations()
        snap = self._resolve_weather(user.weather_area_code, weather_override=weather_override)
        user_stats = self.db.get_user_curation_stats(user.chat_id)
        return match_curations(
            user,
            curations,
            snapshot=snap,
            target_season=get_kst_season(ref_date),
            ref_date=ref_date,
            user_stats=user_stats,
        )

    def _record_seen_items(self, chat_id: str, match: MatchResult) -> None:
        if not match.items or match.blocked_by_weather_no_fallback:
            return
        batch_now = datetime.now(KST)
        for item in match.items[:3]:
            self.db.log_curation_action(chat_id, item.id, "SEEN", now=batch_now)

    def handle_update(
        self,
        update: dict[str, Any],
        weather_override: Optional[WeatherSnapshot] = None,
        ref_date: Optional[date] = None,
    ) -> Optional[dict[str, Any]]:
        """Routes incoming Telegram update (message or callback_query)."""
        if "callback_query" in update:
            return self._handle_callback(
                update["callback_query"],
                weather_override=weather_override,
                ref_date=ref_date,
            )
        if "message" in update:
            return self._handle_message(
                update["message"],
                weather_override=weather_override,
                ref_date=ref_date,
            )
        return None

    def _handle_message(
        self,
        message: dict[str, Any],
        weather_override: Optional[WeatherSnapshot] = None,
        ref_date: Optional[date] = None,
    ) -> Optional[dict[str, Any]]:
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id", "")).strip()
        if not chat_id:
            return None
        text = str(message.get("text", "")).strip()

        if text.startswith("/start") or text.startswith("/menu"):
            user = self.db.upsert_onboarding_user(chat_id)
            card_html, markup = render_onboarding_card(user)
            return self.channel.send_message(chat_id, card_html, reply_markup=markup)

        if text.startswith("/weekend") or text.startswith("/yolo"):
            user = self.db.get_user(chat_id) or self.db.upsert_onboarding_user(chat_id)
            if user.archetype is None:
                card_html, markup = render_onboarding_card(user)
                return self.channel.send_message(chat_id, card_html, reply_markup=markup)
            match = self._match_for_user(user, weather_override=weather_override, ref_date=ref_date)
            card_html, markup = render_curation_card(user, match)
            resp = self.channel.send_message(chat_id, card_html, reply_markup=markup)
            if resp is not None and resp.get("ok", True):
                self._record_seen_items(chat_id, match)
            return resp

        # Default response: guide user to onboarding/menu
        user = self.db.get_user(chat_id) or self.db.upsert_onboarding_user(chat_id)
        card_html, markup = render_onboarding_card(user)
        return self.channel.send_message(chat_id, card_html, reply_markup=markup)

    def _edit_curation_card(
        self,
        user: YoloUserProfile,
        message_id: Any,
        weather_override: Optional[WeatherSnapshot] = None,
        ref_date: Optional[date] = None,
    ) -> Optional[dict[str, Any]]:
        match = self._match_for_user(user, weather_override=weather_override, ref_date=ref_date)
        card_html, markup = render_curation_card(user, match)
        resp = self.channel.safe_edit_or_send(
            user.chat_id, message_id, card_html, reply_markup=markup
        )
        if resp is not None and resp.get("ok", True):
            self._record_seen_items(user.chat_id, match)
        return resp

    def _handle_callback(
        self,
        cb: dict[str, Any],
        weather_override: Optional[WeatherSnapshot] = None,
        ref_date: Optional[date] = None,
    ) -> Optional[dict[str, Any]]:
        # Single-ack scope: joins the manager's scope when dispatched by `DynamicBotManager`.
        with callback_ack_scope(str(cb.get("id", "")).strip()):
            return self._handle_callback_scoped(cb, weather_override=weather_override, ref_date=ref_date)

    def _handle_callback_scoped(
        self,
        cb: dict[str, Any],
        weather_override: Optional[WeatherSnapshot] = None,
        ref_date: Optional[date] = None,
    ) -> Optional[dict[str, Any]]:
        cb_id = str(cb.get("id", "")).strip()
        data = str(cb.get("data", "")).strip()
        msg = cb.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id", "")).strip()
        message_id = msg.get("message_id")

        # [TG-04] MANDATORY FIRST ACTION: answerCallbackQuery before any DB or weather work!
        if cb_id:
            answer_callback_once(cb_id, lambda: self.channel.answer_callback(cb_id))

        if not chat_id or not data:
            return None

        try:
            validate_callback_data(data)
        except ValueError:
            logger.warning("Rejected oversized callback_data: %r", data)
            return None

        # 1. Archetype selection (`yolo:set:<ARCHETYPE>`)
        if data in ALLOWED_ARCHETYPE_CALLBACKS:
            target_arc = ALLOWED_ARCHETYPE_CALLBACKS[data]
            user = self.db.set_user_archetype(chat_id, target_arc)
            return self._edit_curation_card(
                user, message_id, weather_override=weather_override, ref_date=ref_date
            )

        # 1b. TimeSlot selection (`yolo:slot:<all|q2|half|full>`)
        if data in ALLOWED_SLOT_CALLBACKS:
            target_slot = ALLOWED_SLOT_CALLBACKS[data]
            user = self.db.set_preferred_time_slot(chat_id, target_slot)
            if user.archetype is None:
                card_html, markup = render_onboarding_card(user)
                return self.channel.safe_edit_or_send(
                    chat_id, message_id, card_html, reply_markup=markup
                )
            return self._edit_curation_card(
                user, message_id, weather_override=weather_override, ref_date=ref_date
            )

        # 1c. Area selection (`yolo:area:<SEO..GWN>`)
        if data in ALLOWED_AREA_CALLBACKS:
            area_code = ALLOWED_AREA_CALLBACKS[data]
            user = self.db.set_weather_area_code(chat_id, area_code)
            if user.archetype is None:
                card_html, markup = render_onboarding_card(user)
                return self.channel.safe_edit_or_send(
                    chat_id, message_id, card_html, reply_markup=markup
                )
            return self._edit_curation_card(
                user, message_id, weather_override=weather_override, ref_date=ref_date
            )

        # 1d. Per-curation activity log (`yolo:log:<sav|done|skip>:<curation_id>`)
        if data.startswith("yolo:log:"):
            parts = data.split(":", 3)
            if len(parts) != 4 or parts[2] not in _LOG_ACTION_MAP or not parts[3].strip():
                logger.warning("Rejected malformed log callback_data: %r", data)
                return None
            action = _LOG_ACTION_MAP[parts[2]]
            curation_id = parts[3].strip()
            known_ids = {c.id for c in self.db.list_active_curations()}
            if curation_id not in known_ids:
                logger.warning("Rejected log callback for unknown curation_id: %r", curation_id)
                return None
            user = self.db.get_user(chat_id) or self.db.upsert_onboarding_user(chat_id)
            event_key = f"cb:{cb_id}" if cb_id else None
            self.db.log_curation_action(chat_id, curation_id, action, event_key=event_key)
            if user.archetype is None:
                card_html, markup = render_onboarding_card(user)
                return self.channel.safe_edit_or_send(
                    chat_id, message_id, card_html, reply_markup=markup
                )
            return self._edit_curation_card(
                user, message_id, weather_override=weather_override, ref_date=ref_date
            )

        # 1e. MBTI selection (`yolo:mbti:<ISTJ..ENTJ>`)
        if data in ALLOWED_MBTI_CALLBACKS:
            target_mbti = ALLOWED_MBTI_CALLBACKS[data]
            user = self.db.set_user_mbti(chat_id, target_mbti)
            card_html, markup = render_profile_card(user)
            return self.channel.safe_edit_or_send(
                chat_id, message_id, card_html, reply_markup=markup
            )

        # 1f. Solo level selection (`yolo:lvl:<1..5>`)
        if data in ALLOWED_LEVEL_CALLBACKS:
            target_lvl = ALLOWED_LEVEL_CALLBACKS[data]
            user = self.db.set_user_solo_level(chat_id, target_lvl)
            card_html, markup = render_profile_card(user)
            return self.channel.safe_edit_or_send(
                chat_id, message_id, card_html, reply_markup=markup
            )

        # 1g. 1-Person Execution Coach Guide (`yolo:guide:<curation_id>`)
        if data.startswith("yolo:guide:"):
            curation_id = data.split("yolo:guide:", 1)[1].strip()
            if not curation_id:
                return None
            curation = self.db.get_curation(curation_id)
            if curation is None:
                logger.warning("Rejected guide callback for unknown curation_id: %r", curation_id)
                return None
            user = self.db.get_user(chat_id) or self.db.upsert_onboarding_user(chat_id)
            snap = self._resolve_weather(user.weather_area_code, weather_override=weather_override)
            card_html, markup = render_guide_card(user, curation, snapshot=snap)
            return self.channel.safe_edit_or_send(
                chat_id, message_id, card_html, reply_markup=markup
            )

        if data not in ALLOWED_STATIC_CALLBACKS:
            logger.warning("Rejected unknown callback_data: %r", data)
            return None

        # 2. Menu / Archetype switcher (`yolo:menu`)
        if data == "yolo:menu":
            user = self.db.get_user(chat_id) or self.db.upsert_onboarding_user(chat_id)
            card_html, markup = render_onboarding_card(user)
            return self.channel.safe_edit_or_send(
                chat_id, message_id, card_html, reply_markup=markup
            )

        # 2b. Area menu (`yolo:area:menu`)
        if data == "yolo:area:menu":
            user = self.db.get_user(chat_id) or self.db.upsert_onboarding_user(chat_id)
            card_html, markup = render_area_menu_card(user)
            return self.channel.safe_edit_or_send(
                chat_id, message_id, card_html, reply_markup=markup
            )

        # 2c. Profile / MBTI / Solo Level menu (`yolo:profile`)
        if data == "yolo:profile":
            user = self.db.get_user(chat_id) or self.db.upsert_onboarding_user(chat_id)
            card_html, markup = render_profile_card(user)
            return self.channel.safe_edit_or_send(
                chat_id, message_id, card_html, reply_markup=markup
            )

        # 2d. Saved & Done Locker (`yolo:view:saved`)
        if data == "yolo:view:saved":
            user = self.db.get_user(chat_id) or self.db.upsert_onboarding_user(chat_id)
            locker = self.db.get_user_locker(chat_id)
            by_id = {c.id: c for c in self.db.list_active_curations()}
            saved_items = [by_id[cid] for cid in locker["saved"] if cid in by_id]
            done_items = [by_id[cid] for cid in locker["done"] if cid in by_id]
            snap = self._resolve_weather(user.weather_area_code, weather_override=weather_override)
            card_html, markup = render_saved_locker_card(
                user, saved_items, done_items, snapshot=snap
            )
            return self.channel.safe_edit_or_send(
                chat_id, message_id, card_html, reply_markup=markup
            )

        # 3. View Weekend Curations (`yolo:view:weekend`)
        if data == "yolo:view:weekend":
            user = self.db.get_user(chat_id) or self.db.upsert_onboarding_user(chat_id)
            if user.archetype is None:
                card_html, markup = render_onboarding_card(user)
                return self.channel.safe_edit_or_send(
                    chat_id, message_id, card_html, reply_markup=markup
                )
            return self._edit_curation_card(
                user, message_id, weather_override=weather_override, ref_date=ref_date
            )

        # 3b. Rotate to Next Curations (`yolo:view:next`)
        if data == "yolo:view:next":
            user = self.db.get_user(chat_id) or self.db.upsert_onboarding_user(chat_id)
            if user.archetype is None:
                card_html, markup = render_onboarding_card(user)
                return self.channel.safe_edit_or_send(
                    chat_id, message_id, card_html, reply_markup=markup
                )
            _, last_ids = self.db.get_last_seen_batch(chat_id)
            skip_now = datetime.now(KST)
            for idx, cur_id in enumerate(last_ids):
                ev = f"cb:{cb_id}:skip:{idx}:{cur_id}" if cb_id else None
                self.db.log_curation_action(chat_id, cur_id, "SKIP", event_key=ev, now=skip_now)
            return self._edit_curation_card(
                user, message_id, weather_override=weather_override, ref_date=ref_date
            )

        # 4. Preview Rainy/Severe Weather Fallback (`yolo:view:rain`)
        if data == "yolo:view:rain":
            user = self.db.get_user(chat_id) or self.db.upsert_onboarding_user(chat_id)
            if user.archetype is None:
                card_html, markup = render_onboarding_card(user)
                return self.channel.safe_edit_or_send(
                    chat_id, message_id, card_html, reply_markup=markup
                )
            rain_snap = WeatherSnapshot(
                area_code=user.weather_area_code,
                weather_class=WeatherClass.RAIN,
                pop_percent=80,
                precip_mm=12.0,
                feels_like_c=16.0,
            )
            return self._edit_curation_card(
                user, message_id, weather_override=rain_snap, ref_date=ref_date
            )

        # 5. Toggle Weather Fallback (`yolo:cfg:fb:0` or `yolo:cfg:fb:1`)
        if data in ("yolo:cfg:fb:0", "yolo:cfg:fb:1"):
            enable = data.endswith(":1")
            user = self.db.set_weather_fallback(chat_id, enable)
            if user.archetype is None:
                card_html, markup = render_onboarding_card(user)
                return self.channel.safe_edit_or_send(
                    chat_id, message_id, card_html, reply_markup=markup
                )
            return self._edit_curation_card(
                user, message_id, weather_override=weather_override, ref_date=ref_date
            )

        # 6. Unsubscribe 2-Step In-Place Confirmation (`yolo:unsub:ask`, `ok`, `no`)
        if data == "yolo:unsub:ask":
            card_html, markup = render_unsub_confirm_card()
            return self.channel.safe_edit_or_send(
                chat_id, message_id, card_html, reply_markup=markup
            )

        if data == "yolo:unsub:ok":
            self.db.unsubscribe_user(chat_id)
            card_html, markup = render_unsubscribed_card()
            return self.channel.safe_edit_or_send(
                chat_id, message_id, card_html, reply_markup=markup
            )

        if data == "yolo:unsub:no":
            user = self.db.get_user(chat_id) or self.db.upsert_onboarding_user(chat_id)
            if user.archetype is None:
                card_html, markup = render_onboarding_card(user)
                return self.channel.safe_edit_or_send(
                    chat_id, message_id, card_html, reply_markup=markup
                )
            return self._edit_curation_card(
                user, message_id, weather_override=weather_override, ref_date=ref_date
            )

        return None

    def dispatch_weekly_briefings(
        self,
        snapshot: Optional[WeatherSnapshot] = None,
        ref_date: Optional[date] = None,
        dispatch_type: str = "WEEKLY_REGULAR",
    ) -> dict[str, Any]:
        """Dispatches weekly briefings to all ACTIVE users with `UNIQUE(chat_id, week_key, dispatch_type)` safety.
        Looks up weather per user `weather_area_code` when `snapshot` is not overridden."""
        week_key = get_iso_week_key(ref_date)
        season = get_kst_season(ref_date)
        curations = self.db.list_active_curations()
        users = self.db.list_active_users()

        area_snapshots: dict[str, WeatherSnapshot] = {}
        user_matches: dict[str, MatchResult] = {}
        payloads: list[tuple[str, str, str, Optional[dict[str, Any]]]] = []
        for user in users:
            if user.archetype is None:
                continue
            area = user.weather_area_code
            if snapshot is not None:
                snap = snapshot
            else:
                if area not in area_snapshots:
                    area_snapshots[area] = self._resolve_weather(area)
                snap = area_snapshots[area]
            user_stats = self.db.get_user_curation_stats(user.chat_id)
            match = match_curations(
                user,
                curations,
                snapshot=snap,
                target_season=season,
                ref_date=ref_date,
                user_stats=user_stats,
            )
            user_matches[user.chat_id] = match
            card_html, markup = render_curation_card(user, match)
            payloads.append((user.chat_id, user.archetype.value, card_html, markup))

        before_sent = {
            uid for uid in user_matches
            if self.db.get_dispatch_status(uid, week_key, dispatch_type) == "SENT"
        }
        stats = self.dispatcher.dispatch_batch(
            channel=self.channel,
            db=self.db,
            week_key=week_key,
            dispatch_type=dispatch_type,
            payloads=payloads,
        )
        for uid, match in user_matches.items():
            if uid not in before_sent and self.db.get_dispatch_status(uid, week_key, dispatch_type) == "SENT":
                self._record_seen_items(uid, match)
        stats["week_key"] = week_key
        stats["season"] = season.value
        return stats
