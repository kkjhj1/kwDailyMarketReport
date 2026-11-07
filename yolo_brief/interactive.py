"""Interactive state-machine & callback router for YOLO Bot (욜로봇).
Enforces:
- [TG-04] Immediate `answer_callback` BEFORE any DB or weather lookups.
- Strict callback_data allowlist validation (<= 64 bytes).
- State transitions: ONBOARDING -> ACTIVE -> UNSUBSCRIBED.
- Weekly regular & weather-aware briefing dispatch via `SafeTelegramDispatcher` ([TG-05]).
"""
from __future__ import annotations

from datetime import date
import logging
from typing import Any, Optional

from yolo_brief.channels.telegram import YoloTelegramChannel
from yolo_brief.channels.telegram_card import (
    render_curation_card,
    render_onboarding_card,
    render_unsub_confirm_card,
    render_unsubscribed_card,
    validate_callback_data,
)
from yolo_brief.channels.telegram_limiter import SafeTelegramDispatcher
from yolo_brief.matching import match_curations
from yolo_brief.models import (
    SoloArchetype,
    WeatherClass,
    WeatherSnapshot,
)
from yolo_brief.storage.db import YoloDatabase
from yolo_brief.weather_engine import get_iso_week_key, get_kst_season

logger = logging.getLogger(__name__)

ALLOWED_STATIC_CALLBACKS = {
    "yolo:menu",
    "yolo:view:weekend",
    "yolo:view:rain",
    "yolo:cfg:fb:0",
    "yolo:cfg:fb:1",
    "yolo:unsub:ask",
    "yolo:unsub:ok",
    "yolo:unsub:no",
}
ALLOWED_ARCHETYPE_CALLBACKS = {
    f"yolo:set:{arc.value}": arc for arc in SoloArchetype
}


class YoloInteractiveBot:
    """Handles Telegram `/start`, inline button callbacks, and weekly dispatches."""

    def __init__(
        self,
        db: YoloDatabase,
        channel: YoloTelegramChannel,
        dispatcher: Optional[SafeTelegramDispatcher] = None,
        default_weather: Optional[WeatherSnapshot] = None,
    ) -> None:
        self.db = db
        self.channel = channel
        self.dispatcher = dispatcher or SafeTelegramDispatcher()
        self.default_weather = default_weather or WeatherSnapshot(
            weather_class=WeatherClass.CLEAR,
            pop_percent=10,
            feels_like_c=21.0,
            pm_grade="GOOD",
        )

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
            curations = self.db.list_active_curations()
            snap = weather_override or self.default_weather
            match = match_curations(
                user,
                curations,
                snapshot=snap,
                target_season=get_kst_season(ref_date),
                ref_date=ref_date,
            )
            card_html, markup = render_curation_card(user, match)
            return self.channel.send_message(chat_id, card_html, reply_markup=markup)

        # Default response: guide user to onboarding/menu
        user = self.db.get_user(chat_id) or self.db.upsert_onboarding_user(chat_id)
        card_html, markup = render_onboarding_card(user)
        return self.channel.send_message(chat_id, card_html, reply_markup=markup)

    def _handle_callback(
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
            self.channel.answer_callback(cb_id)

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
            curations = self.db.list_active_curations()
            snap = weather_override or self.default_weather
            match = match_curations(
                user,
                curations,
                snapshot=snap,
                target_season=get_kst_season(ref_date),
                ref_date=ref_date,
            )
            card_html, markup = render_curation_card(user, match)
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

        # 3. View Weekend Curations (`yolo:view:weekend`)
        if data == "yolo:view:weekend":
            user = self.db.get_user(chat_id) or self.db.upsert_onboarding_user(chat_id)
            if user.archetype is None:
                card_html, markup = render_onboarding_card(user)
                return self.channel.safe_edit_or_send(
                    chat_id, message_id, card_html, reply_markup=markup
                )
            curations = self.db.list_active_curations()
            snap = weather_override or self.default_weather
            match = match_curations(
                user,
                curations,
                snapshot=snap,
                target_season=get_kst_season(ref_date),
                ref_date=ref_date,
            )
            card_html, markup = render_curation_card(user, match)
            return self.channel.safe_edit_or_send(
                chat_id, message_id, card_html, reply_markup=markup
            )

        # 4. Preview Rainy/Severe Weather Fallback (`yolo:view:rain`)
        if data == "yolo:view:rain":
            user = self.db.get_user(chat_id) or self.db.upsert_onboarding_user(chat_id)
            if user.archetype is None:
                card_html, markup = render_onboarding_card(user)
                return self.channel.safe_edit_or_send(
                    chat_id, message_id, card_html, reply_markup=markup
                )
            curations = self.db.list_active_curations()
            rain_snap = WeatherSnapshot(
                area_code=user.weather_area_code,
                weather_class=WeatherClass.RAIN,
                pop_percent=80,
                precip_mm=12.0,
                feels_like_c=16.0,
            )
            match = match_curations(
                user,
                curations,
                snapshot=rain_snap,
                target_season=get_kst_season(ref_date),
                ref_date=ref_date,
            )
            card_html, markup = render_curation_card(user, match)
            return self.channel.safe_edit_or_send(
                chat_id, message_id, card_html, reply_markup=markup
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
            curations = self.db.list_active_curations()
            snap = weather_override or self.default_weather
            match = match_curations(
                user,
                curations,
                snapshot=snap,
                target_season=get_kst_season(ref_date),
                ref_date=ref_date,
            )
            card_html, markup = render_curation_card(user, match)
            return self.channel.safe_edit_or_send(
                chat_id, message_id, card_html, reply_markup=markup
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
            else:
                curations = self.db.list_active_curations()
                snap = weather_override or self.default_weather
                match = match_curations(
                    user,
                    curations,
                    snapshot=snap,
                    target_season=get_kst_season(ref_date),
                    ref_date=ref_date,
                )
                card_html, markup = render_curation_card(user, match)
            return self.channel.safe_edit_or_send(
                chat_id, message_id, card_html, reply_markup=markup
            )

        return None

    def dispatch_weekly_briefings(
        self,
        snapshot: Optional[WeatherSnapshot] = None,
        ref_date: Optional[date] = None,
        dispatch_type: str = "WEEKLY_REGULAR",
    ) -> dict[str, Any]:
        """Dispatches weekly briefings to all ACTIVE users with `UNIQUE(chat_id, week_key, dispatch_type)` safety."""
        week_key = get_iso_week_key(ref_date)
        season = get_kst_season(ref_date)
        snap = snapshot or self.default_weather
        curations = self.db.list_active_curations()
        users = self.db.list_active_users()

        payloads: list[tuple[str, str, str, Optional[dict[str, Any]]]] = []
        for user in users:
            if user.archetype is None:
                continue
            match = match_curations(
                user,
                curations,
                snapshot=snap,
                target_season=season,
                ref_date=ref_date,
            )
            card_html, markup = render_curation_card(user, match)
            payloads.append((user.chat_id, user.archetype.value, card_html, markup))

        stats = self.dispatcher.dispatch_batch(
            channel=self.channel,
            db=self.db,
            week_key=week_key,
            dispatch_type=dispatch_type,
            payloads=payloads,
        )
        stats["week_key"] = week_key
        stats["season"] = season.value
        return stats
