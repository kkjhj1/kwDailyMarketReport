"""Pure builders for the three Admission personalized push streams.

The builders only select content and prepare a Telegram payload.  They do not
claim delivery slots or make network calls; that boundary lives in
``SafeTelegramDispatcher.dispatch_personalized``.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from admission_brief.models import (
    AdmissionBriefing,
    AdmissionEvent,
    BriefingType,
    ChildProfile,
    EducationStage,
    EventCategory,
    GradeLevel,
    ParentUserProfile,
    TargetGrade,
    TargetRegion,
)
from admission_brief.storage.db import AdmissionDatabase

KST = ZoneInfo("Asia/Seoul")
EXAM_SELECTION_MONTHS = frozenset({4, 5, 6, 7, 10, 11, 12})


@dataclass(frozen=True)
class PersonalizedPush:
    """One consolidated, user-specific message ready for safe dispatch."""

    chat_id: str
    briefing: AdmissionBriefing
    item_ids: tuple[str, ...]
    item_kind: str
    stream: str
    reply_markup: Optional[dict[str, Any]] = None


def _kst(value: datetime) -> datetime:
    return value.replace(tzinfo=KST) if value.tzinfo is None else value.astimezone(KST)


def _children(profile: ParentUserProfile) -> list[ChildProfile]:
    return list(profile.children) if profile.children else [profile.get_active_child()]


def _catalog_filter(catalog: Any, child: ChildProfile, now: datetime, card_type: str) -> tuple[Any, ...]:
    """Call the validated catalog while remaining friendly to small test doubles."""
    try:
        return tuple(catalog.filter(
            child.education_stage,
            child.grade_level,
            month=now.month,
            today=now.date(),
            card_type=card_type,
        ))
    except TypeError:
        try:
            return tuple(catalog.filter(child.education_stage, child.grade_level, card_type=card_type))
        except (AttributeError, TypeError, ValueError):
            return ()
    except (AttributeError, TypeError, ValueError):
        return ()


def _urgent(card: Any) -> bool:
    value = getattr(card, "urgent", getattr(card, "is_urgent", False))
    if isinstance(value, str):
        return value.upper() in {"URGENT", "HIGH", "BREAKING", "TRUE", "1"}
    return bool(value) or str(getattr(card, "urgency", "")).upper() in {"URGENT", "HIGH", "BREAKING"}


def _fresh_card(db: AdmissionDatabase, chat_id: str, card: Any, stream: str) -> bool:
    return not db.is_card_acked(chat_id, card.content_id) and not db.has_content_been_sent(
        chat_id, card.content_id, stream
    )


def _fresh_event(db: AdmissionDatabase, chat_id: str, event: AdmissionEvent, stream: str) -> bool:
    return not db.is_card_acked(chat_id, event.event_id) and not db.has_content_been_sent(
        chat_id, event.event_id, stream
    )


def _target_grade_for_children(children: Iterable[ChildProfile]) -> TargetGrade:
    grades = {child.grade for child in children}
    return next(iter(grades)) if len(grades) == 1 else TargetGrade.ALL


def _ack_markup(item_ids: Iterable[str], *, include_menu: bool = False) -> Optional[dict[str, Any]]:
    rows: list[list[dict[str, str]]] = []
    for item_id in item_ids:
        # Catalog ids are already validated as <=24 ASCII.  Event ids can be
        # legacy/untrusted, so long IDs deliberately get the safe menu action.
        if item_id.isascii() and len(item_id) <= 24 and all(ch.isalnum() or ch == "-" for ch in item_id):
            rows.append([{"text": f"✅ 확인: {item_id[:18]}", "callback_data": f"adm:ack:{item_id}"}])
    if include_menu or not rows:
        rows.append([{"text": "📚 자세히 보기", "callback_data": "adm:menu"}])
    return {"inline_keyboard": rows} if rows else None


def _briefing_from_cards(
    briefing_id: str,
    cards: list[Any],
    now: datetime,
    *,
    target_grade: TargetGrade,
    briefing_type: BriefingType,
    headline_prefix: str,
    silent: bool = True,
) -> AdmissionBriefing:
    summaries: list[str] = []
    todos: list[str] = []
    for card in cards:
        summaries.extend(str(item) for item in getattr(card, "summary_3lines", ())[:3])
        todos.extend(str(item) for item in getattr(card, "action_todos", ())[:2])
    summaries = summaries[:3]
    todos = todos[:6]
    headlines = " · ".join(str(card.headline) for card in cards[:3])
    first = cards[0]
    return AdmissionBriefing(
        briefing_id=briefing_id,
        briefing_type=briefing_type,
        target_grade=target_grade,
        target_region=TargetRegion.ALL,
        headline=f"{headline_prefix}: {headlines}"[:240],
        summary_3lines=summaries,
        so_what=str(getattr(first, "so_what", "")),
        action_todos=todos,
        source_name=str(getattr(first, "source_name", "공식 오피셜")),
        source_url=getattr(first, "source_url", None),
        silent=silent,
        as_of=now.strftime("%Y-%m-%d %H:%M KST"),
    )


def _briefing_from_events(
    briefing_id: str, events: list[AdmissionEvent], now: datetime, *, stream: str
) -> AdmissionBriefing:
    first = events[0]
    summaries: list[str] = []
    todos: list[str] = []
    for event in events:
        summaries.extend(event.summary_3lines[:3])
        todos.extend(event.action_todos[:2])
    return AdmissionBriefing(
        briefing_id=briefing_id,
        briefing_type=BriefingType.BREAKING_NEWS if stream == "policy" else BriefingType.TICKETING_ALERT,
        target_grade=first.target_grade if all(e.target_grade == first.target_grade for e in events) else TargetGrade.ALL,
        target_region=TargetRegion.ALL,
        headline=("🚨 정책 긴급 알림" if stream == "policy" else "⏰ 자녀 맞춤 행사 D-Day") + " · " + " · ".join(e.title for e in events[:3]),
        summary_3lines=summaries[:3],
        so_what=first.so_what,
        action_todos=todos[:6],
        booking_url=first.booking_url,
        source_name=first.source_name,
        source_url=first.source_url,
        urgency="BREAKING" if stream == "policy" else "HIGH",
        category=first.category if stream == "dday" else None,
        silent=False,
        as_of=now.strftime("%Y-%m-%d %H:%M KST"),
    )


def _cadence_allows(stage: EducationStage, now: datetime, urgent: bool) -> bool:
    if stage in {EducationStage.HIGH, EducationStage.POST_HIGH, EducationStage.ALL}:
        return True
    if stage in {EducationStage.MIDDLE, EducationStage.ELEMENTARY}:
        iso_week = now.date().isocalendar().week
        return iso_week % 2 == 0 or now.month in EXAM_SELECTION_MONTHS or urgent
    return False


def build_weekly_personalized_pushes(
    db: AdmissionDatabase, catalog: Any, now_kst: datetime
) -> list[PersonalizedPush]:
    """Build at most one fresh consolidated weekly push per user."""
    now = _kst(now_kst)
    iso_year, iso_week = now.date().isocalendar()[:2]
    pushes: list[PersonalizedPush] = []
    for profile in db.list_subscribers(active_only=True):
        if not profile.weekly_guide_enabled:
            continue
        briefing_id = f"push:weekly:{iso_year}W{iso_week:02d}:{profile.chat_id}"
        if db.has_dispatched(briefing_id, profile.chat_id):
            continue
        cards_by_id: dict[str, Any] = {}
        for child in _children(profile):
            candidates = list(_catalog_filter(catalog, child, now, "STUDY_GUIDE"))
            candidates.extend(_catalog_filter(catalog, child, now, "WEEKLY_TODO"))
            urgent = any(_urgent(card) for card in candidates)
            if not _cadence_allows(child.education_stage, now, urgent):
                continue
            for card in candidates:
                if _fresh_card(db, profile.chat_id, card, "weekly"):
                    cards_by_id[card.content_id] = card
        cards = list(cards_by_id.values())
        if not cards:
            continue
        children = _children(profile)
        briefing = _briefing_from_cards(
            briefing_id, cards, now, target_grade=_target_grade_for_children(children),
            briefing_type=BriefingType.WEEKLY_TODO, headline_prefix="이번 주 자녀 맞춤 가이드",
        )
        pushes.append(PersonalizedPush(
            chat_id=str(profile.chat_id), briefing=briefing,
            item_ids=tuple(card.content_id for card in cards), item_kind="CARD", stream="weekly",
            reply_markup=_ack_markup((card.content_id for card in cards)),
        ))
    return pushes


def _event_matches_child(event: AdmissionEvent, child: ChildProfile) -> bool:
    if child.grade != TargetGrade.ALL and event.target_grade not in {TargetGrade.ALL, child.grade}:
        return False
    if child.region == TargetRegion.ALL:
        return True
    if child.region == TargetRegion.ONLINE:
        return event.is_online_live or event.target_region == TargetRegion.ONLINE
    return event.is_online_live or event.target_region in {TargetRegion.ALL, child.region}


def _policy_event_matches_child(event: AdmissionEvent, child: ChildProfile) -> bool:
    """Policy breaking news is stage-scoped; event D-Day remains grade-scoped."""
    event_stage = {
        TargetGrade.MIDDLE: EducationStage.MIDDLE,
        TargetGrade.HIGH_1_2: EducationStage.HIGH,
        TargetGrade.HIGH_3_N: EducationStage.HIGH,
        TargetGrade.ALL: EducationStage.ALL,
    }.get(event.target_grade, EducationStage.ALL)
    stage_match = (
        child.education_stage == EducationStage.ALL
        or event_stage == EducationStage.ALL
        or child.education_stage == event_stage
        or (child.education_stage == EducationStage.POST_HIGH and event.target_grade == TargetGrade.HIGH_3_N)
    )
    if not stage_match:
        return False
    if child.region == TargetRegion.ALL:
        return True
    if child.region == TargetRegion.ONLINE:
        return event.is_online_live or event.target_region == TargetRegion.ONLINE
    return event.is_online_live or event.target_region in {TargetRegion.ALL, child.region}


def _event_date(event: AdmissionEvent) -> Optional[date]:
    try:
        return date.fromisoformat((event.event_date_str or "")[:10])
    except ValueError:
        return None


def build_event_dday_pushes(db: AdmissionDatabase, now_kst: datetime) -> list[PersonalizedPush]:
    """Build only D-3/D-1 event alerts using the KST calendar date."""
    now = _kst(now_kst)
    events: list[tuple[AdmissionEvent, int]] = []
    for event in db.list_all_events():
        event_date = _event_date(event)
        if event_date is None:
            continue
        days_left = (event_date - now.date()).days
        if days_left in (1, 3):
            events.append((event, days_left))
    pushes: list[PersonalizedPush] = []
    for profile in db.list_subscribers(active_only=True):
        if not profile.event_dday_enabled:
            continue
        briefing_id = f"push:dday:{now.strftime('%Y%m%d')}:{profile.chat_id}"
        if db.has_dispatched(briefing_id, profile.chat_id):
            continue
        matched = [event for event, _ in events if any(_event_matches_child(event, child) for child in _children(profile))]
        matched = [event for event in matched if _fresh_event(db, profile.chat_id, event, "dday")]
        if not matched:
            continue
        briefing = _briefing_from_events(briefing_id, matched, now, stream="dday")
        pushes.append(PersonalizedPush(
            chat_id=str(profile.chat_id), briefing=briefing,
            item_ids=tuple(event.event_id for event in matched), item_kind="EVENT", stream="dday",
            reply_markup=_ack_markup((event.event_id for event in matched), include_menu=True),
        ))
    return pushes


def build_policy_pushes(
    db: AdmissionDatabase, catalog: Any, now_kst: datetime
) -> list[PersonalizedPush]:
    """Build fresh POLICY cards and BREAKING_NEWS event pushes per user."""
    now = _kst(now_kst)
    iso_year, iso_week = now.date().isocalendar()[:2]
    pushes: list[PersonalizedPush] = []
    breaking_events = [event for event in db.list_all_events() if event.event_type == BriefingType.BREAKING_NEWS]
    for profile in db.list_subscribers(active_only=True):
        if not profile.policy_breaking_enabled:
            continue
        briefing_id = f"push:policy:{iso_year}W{iso_week:02d}:{profile.chat_id}"
        if db.has_dispatched(briefing_id, profile.chat_id):
            continue
        cards_by_id: dict[str, Any] = {}
        for child in _children(profile):
            for card in _catalog_filter(catalog, child, now, "POLICY"):
                if _fresh_card(db, profile.chat_id, card, "policy"):
                    cards_by_id[card.content_id] = card
        events = [
            event for event in breaking_events
            if any(_policy_event_matches_child(event, child) for child in _children(profile))
            and _fresh_event(db, profile.chat_id, event, "policy")
        ]
        if not cards_by_id and not events:
            continue
        if cards_by_id and events:
            # Keep a single message per stream while retaining a card acknowledgement
            # button for the catalog content.  Breaking-news items use the safe menu.
            briefing = _briefing_from_cards(
                briefing_id, list(cards_by_id.values()), now,
                target_grade=_target_grade_for_children(_children(profile)),
                briefing_type=BriefingType.POLICY_TRANSLATE,
                headline_prefix="정책·긴급 변화 알림",
                silent=False,
            )
            briefing.headline += " · " + " · ".join(event.title for event in events[:2])
            item_ids = tuple(cards_by_id) + tuple(event.event_id for event in events)
            markup = _ack_markup(tuple(cards_by_id), include_menu=True)
        elif cards_by_id:
            briefing = _briefing_from_cards(
                briefing_id, list(cards_by_id.values()), now,
                target_grade=_target_grade_for_children(_children(profile)),
                briefing_type=BriefingType.POLICY_TRANSLATE,
                headline_prefix="정책 변화 알림",
                silent=False,
            )
            item_ids = tuple(cards_by_id)
            markup = _ack_markup(item_ids)
        else:
            briefing = _briefing_from_events(briefing_id, events, now, stream="policy")
            item_ids = tuple(event.event_id for event in events)
            markup = _ack_markup(item_ids, include_menu=True)
        pushes.append(PersonalizedPush(
            chat_id=str(profile.chat_id), briefing=briefing,
            item_ids=item_ids, item_kind="MIXED" if cards_by_id and events else ("CARD" if cards_by_id else "EVENT"),
            stream="policy", reply_markup=markup,
        ))
    return pushes

