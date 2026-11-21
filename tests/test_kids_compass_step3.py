from __future__ import annotations

from datetime import datetime

from kids_compass.channels.telegram import (
    esc,
    fits_limits,
    format_food_detail_html,
    format_food_hub_html,
    format_food_list_html,
)
from kids_compass.collector import seed_all
from kids_compass.food_safety import FOOD_ITEMS, evaluate_food_for_child, get_food, search_foods
from kids_compass.interactive_bot import KidsCompassInteractiveBot, build_home_keyboard
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


def _buttons(markup):
    return [button for row in markup["inline_keyboard"] for button in row]


def _tap(bot, data, n=1):
    bot.handle_update({"callback_query": {"id": f"step3-{n}", "data": data,
                                           "message": {"chat": {"id": 7}, "message_id": 100}}})


def test_food_catalogue_and_month_boundaries():
    assert len(FOOD_ITEMS) >= 26
    honey, milk = get_food("honey"), get_food("cow_milk")
    assert evaluate_food_for_child(honey, 9)[0] == "BLOCKED"
    assert "절대 금지" in evaluate_food_for_child(honey, 9)[1]
    assert evaluate_food_for_child(milk, 9)[0] == "BLOCKED"

    tomato = get_food("cherry_tomato_grape")
    assert evaluate_food_for_child(tomato, 20)[0] == "OK"
    assert "4등분" in tomato.safety_warning
    assert evaluate_food_for_child(get_food("whole_nuts"), 20)[0] == "BLOCKED"
    assert search_foods(" 방울-토마토 ")[0].food_id == "cherry_tomato_grape"


def test_food_formatters_escape_and_fit_limits():
    item = get_food("cherry_tomato_grape")
    texts = [
        format_food_hub_html("콩이<bad>", 20),
        format_food_list_html("검색 <bad>", [item], 20),
        format_food_detail_html(item, "콩이<bad>", 20),
    ]
    for text in texts:
        assert fits_limits(text)
        assert "<bad>" not in text and "&lt;bad&gt;" in text
    assert "세로 4등분" in texts[-1]


def test_food_callbacks_search_state_and_multi_child_switch():
    db = KidsCompassDatabase(":memory:")
    seed_all(db, NOW.date())
    first = db.add_child(7, "콩이", "2026-02-21")  # 9 months
    second = db.add_child(7, "큰이", "2025-03-21")  # 20 months
    channel = FakeChannel()
    bot = KidsCompassInteractiveBot(channel, db, clock=lambda: NOW)

    for markup in (build_home_keyboard(), build_home_keyboard([], True)):
        assert "k:fd" in [b["callback_data"] for b in _buttons(markup)]

    _tap(bot, "k:fd:q")
    assert db.get_onboarding_state(7, NOW)["step"] == "FOOD_SEARCH"
    bot.handle_update({"message": {"chat": {"id": 7}, "text": "꿀"}})
    text, markup = channel.sent[-1][1], channel.sent[-1][2]
    assert "돌(12개월) 미만 절대 금지" in text
    assert any(b["callback_data"] == "k:fd:i:honey" for b in _buttons(markup))

    bot.handle_update({"message": {"chat": {"id": 7}, "text": "/food 복숭아"}})
    assert "복숭아" in channel.sent[-1][1]

    _tap(bot, "k:fd:i:cherry_tomato_grape", 2)
    assert "생후 9개월" in channel.edits[-1][2]
    db.set_primary_child(7, second)
    _tap(bot, "k:fd:i:cherry_tomato_grape", 3)
    assert "생후 20개월" in channel.edits[-1][2] and "섭취 가능" in channel.edits[-1][2]

    for b in _buttons(channel.edits[-1][3]):
        assert b["callback_data"].isascii() and len(b["callback_data"].encode()) <= 64
    db.close()


def test_food_is_pull_only_and_does_not_add_push_streams():
    db = KidsCompassDatabase(":memory:")
    seed_all(db, NOW.date())
    db.add_child(7, "콩이", "2025-03-21")
    from kids_compass.interactive_bot import (
        build_dday_push_messages,
        build_milestone_push_messages,
        build_weekend_push_messages,
    )

    messages = (build_weekend_push_messages(db, NOW.date()),
                build_milestone_push_messages(db, NOW.date()),
                build_dday_push_messages(db, NOW))
    assert {m.stream for batch in messages for m in batch} <= {"weekend", "milestone", "milestone_d14", "dday"}
    db.close()

