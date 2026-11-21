"""Kids Compass Step 2: pull-only home play and development guidance."""
from __future__ import annotations

from datetime import date, datetime

import pytest

from kids_compass.channels.telegram import (
    fits_limits,
    format_dev_guide_html,
    format_home_play_html,
    measure_message,
)
from kids_compass.home_play import (
    DEV_DISCLAIMER,
    DEV_GUIDE_ITEMS,
    HOME_PLAY_ITEMS,
    DEV_STAGE_LABELS,
    PLAY_STAGE_LABELS,
    dev_stage_for_months,
    get_dev_guides,
    pick_home_plays,
    play_stage_for_months,
)
from kids_compass.interactive_bot import (
    KidsCompassInteractiveBot,
    build_dev_guide_keyboard,
    build_home_keyboard,
    build_home_play_keyboard,
    build_home_play_stage_keyboard,
    build_milestone_push_messages,
    build_weekend_push_messages,
    build_dday_push_messages,
)
from kids_compass.collector import seed_all
from kids_compass.storage.db import KidsCompassDatabase


NOW = datetime(2026, 11, 21, 21, 0, 0)


class FakeChannel:
    def __init__(self):
        self.sent = []
        self.edits = []
        self.acks = []

    def send_message(self, chat_id, text, reply_markup=None, disable_notification=False):
        self.sent.append((chat_id, text, reply_markup))
        return len(self.sent)

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append((chat_id, message_id, text, reply_markup))
        return True

    def answer_callback_query(self, callback_query_id, text=""):
        self.acks.append(callback_query_id)
        return True


def _callbacks(markup):
    return [button["callback_data"] for row in markup["inline_keyboard"] for button in row]


def _tap(bot, data, number=1):
    bot.handle_update({"callback_query": {
        "id": f"cq-{number}", "data": data,
        "message": {"chat": {"id": 7}, "message_id": 100},
    }})


@pytest.mark.parametrize("months, expected", [
    (0, "M0_11"), (3, "M0_11"), (4, "M0_11"), (6, "M0_11"),
    (7, "M0_11"), (11, "M0_11"), (12, "M12_23"), (23, "M12_23"),
    (24, "M24_47"), (36, "M24_47"), (47, "M24_47"), (48, "Y4_7"),
    (95, "Y4_7"), (96, "Y8_13"),
])
def test_play_stage_boundaries(months, expected):
    assert play_stage_for_months(months) == expected


@pytest.mark.parametrize("months, expected", [
    (0, "D0_3"), (3, "D0_3"), (4, "D4_6"), (6, "D4_6"),
    (7, "D7_12"), (11, "D7_12"), (12, "D7_12"), (23, "D13_24"),
    (24, "D13_24"), (36, "D25_36"), (47, "DY4_7"), (48, "DY4_7"),
    (95, "DY4_7"), (96, "DY8_13"),
])
def test_dev_stage_boundaries(months, expected):
    assert dev_stage_for_months(months) == expected


def test_catalogue_has_three_plays_per_bucket_and_guides_per_bucket():
    for stage in PLAY_STAGE_LABELS:
        assert len(pick_home_plays(stage, limit=3)) == 3
    for stage in DEV_STAGE_LABELS:
        assert get_dev_guides(stage)
    for item in HOME_PLAY_ITEMS:
        assert item.play_id and item.stage and item.title and item.materials
        assert item.duration_min > 0 and item.how_to and item.dev_benefit and item.safety_note
    assert len(HOME_PLAY_ITEMS) >= 18
    assert len(DEV_GUIDE_ITEMS) >= 8


def test_all_cards_fit_limits_and_escape_dynamic_html():
    malicious = "<img src=x> & 'quote'"
    for stage, label in PLAY_STAGE_LABELS.items():
        text = format_home_play_html(malicious, label, pick_home_plays(stage, limit=3))
        assert fits_limits(text)
        assert measure_message(text)[0] <= 3500
        assert malicious not in text and "&lt;img" in text and "&amp;" in text
    for stage, label in DEV_STAGE_LABELS.items():
        text = format_dev_guide_html(malicious, label, get_dev_guides(stage))
        assert fits_limits(text)
        assert DEV_DISCLAIMER in text
        assert malicious not in text and "&lt;img" in text


def test_home_keyboards_expose_hp_for_registered_and_unregistered():
    for markup in (build_home_keyboard(), build_home_keyboard([], True)):
        callbacks = _callbacks(markup)
        assert "k:hp" in callbacks
    for markup in (build_home_play_stage_keyboard(), build_home_play_keyboard("M0_11"),
                   build_dev_guide_keyboard("M0_11")):
        for data in _callbacks(markup):
            assert data.isascii() and len(data.encode("ascii")) <= 64


def test_registered_auto_matching_switch_and_pull_navigation():
    db = KidsCompassDatabase(":memory:")
    seed_all(db, NOW.date())
    first = db.add_child(7, "아기<", "2026-06-21")  # 5개월 -> M0_11 / D4_6
    second = db.add_child(7, "큰이", "2021-11-21")  # 60개월 -> Y4_7
    channel = FakeChannel()
    bot = KidsCompassInteractiveBot(channel, db, clock=lambda: NOW)

    _tap(bot, "k:hp", 1)
    assert channel.acks == ["cq-1"]
    text, markup = channel.edits[-1][2], channel.edits[-1][3]
    assert "아기&lt;" in text and "0~11개월" in text
    next_data = next(data for data in _callbacks(markup) if data.startswith("k:hp:n:"))
    _tap(bot, next_data, 2)
    assert channel.edits[-1][2] != text

    _tap(bot, "k:hp:d:M0_11", 3)
    assert "4~6개월" in channel.edits[-1][2]
    assert "소아과 상담 권장 신호" in channel.edits[-1][2]
    assert "k:hp:p:M0_11" in _callbacks(channel.edits[-1][3])

    db.set_primary_child(7, second)
    _tap(bot, "k:hp", 4)
    assert "큰이" in channel.edits[-1][2] and "4~7세" in channel.edits[-1][2]
    db.close()


def test_unregistered_age_selection_and_push_streams_remain_separate():
    db = KidsCompassDatabase(":memory:")
    seed_all(db, NOW.date())
    channel = FakeChannel()
    bot = KidsCompassInteractiveBot(channel, db, clock=lambda: NOW)
    _tap(bot, "k:hp", 10)
    assert "연령대를 골라" in channel.edits[-1][2]
    _tap(bot, "k:hp:p:Y8_13", 11)
    assert "8~13세" in channel.edits[-1][2]

    # Step 2 is Pull-only: no new stream, callback or recipient is introduced.
    db.add_child(7, "콩이", "2021-11-21")
    pushes = (
        build_weekend_push_messages(db, NOW.date()),
        build_milestone_push_messages(db, NOW.date()),
        build_dday_push_messages(db, NOW),
    )
    for messages in pushes:
        for message in messages:
            assert message.stream in {"weekend", "milestone", "dday", "milestone_d14"}
            assert not any(data.startswith("k:hp") for data in _callbacks(message.reply_markup or {"inline_keyboard": []}))
    db.close()

