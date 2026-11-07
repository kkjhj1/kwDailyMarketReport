"""Weekly 효도 briefing builder (일요일 20:00 KST).
dispatch_key = 'weekly:<ISO year>-W<week>' → silver_dispatch_log keeps re-runs idempotent per chat.
"""
from __future__ import annotations
from datetime import date
from typing import Optional

from silver_care.channels.telegram_card import WEEKLY_ITEMS_PER_PARENT, format_weekly_briefing
from silver_care.models import BenefitMatch
from silver_care.storage.db import SilverCareDatabase


def weekly_dispatch_key(today: date) -> str:
    iso = today.isocalendar()
    return f"weekly:{iso[0]}-W{iso[1]:02d}"


def rotate_matches(matches: list[BenefitMatch], week: int, size: int = WEEKLY_ITEMS_PER_PARENT) -> list[BenefitMatch]:
    """Picks a different window of the graded list each week so the briefing does not repeat."""
    if len(matches) <= size:
        return list(matches)
    start = (week * size) % len(matches)
    return [matches[(start + i) % len(matches)] for i in range(size)]


def build_weekly_messages(db: SilverCareDatabase, today: Optional[date] = None) -> tuple[str, list[tuple[int, str]]]:
    today = today or date.today()
    week = today.isocalendar()[1]
    key = weekly_dispatch_key(today)
    messages: list[tuple[int, str]] = []
    for chat_id in db.list_weekly_chat_ids():
        sections = [
            (p, rotate_matches(db.match_for_parent(p, today.year), week))
            for p in db.list_parents(chat_id)
        ]
        titles = [b.title for _, bid in db.list_bookmarks(chat_id) if (b := db.get_benefit(bid))]
        label = f"{today.month}월 {today.day}일"
        messages.append((chat_id, format_weekly_briefing(sections, titles, label, today.year)))
    return key, messages
