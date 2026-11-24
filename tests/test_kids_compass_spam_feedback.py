from datetime import date, datetime, timedelta

from kids_compass.collector import seed_all
from kids_compass.interactive_bot import (
    KidsCompassInteractiveBot,
    build_d14_milestone_push_messages,
    build_dday_push_messages,
    build_milestone_push_messages,
    push_window,
)
from kids_bot_runner import _merge_milestone_push_messages
from kids_compass.models import PushMessage
from kids_compass.storage.db import KidsCompassDatabase
from kids_compass.channels.telegram import SafeTelegramDispatcher


TODAY = date(2026, 11, 19)
NOW = datetime(2026, 11, 19, 9, 0)


class Channel:
    def __init__(self):
        self.sent = []
        self.acks = []

    def answer_callback_query(self, callback_query_id, text=""):
        self.acks.append(callback_query_id)
        return True

    def send_message(self, chat_id, text, reply_markup=None, disable_notification=False):
        self.sent.append((str(chat_id), text, reply_markup))
        return len(self.sent)

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.sent.append((str(chat_id), text, reply_markup))
        return True


def _seed(db):
    seed_all(db, TODAY)
    db.add_child(7, "콩이", "2021-03-01", 18.0)
    return db


def test_feedback_and_default_cooldown_policy():
    db = KidsCompassDatabase(":memory:")
    try:
        db.record_place_feedback(7, "visited", "VISITED", NOW)
        db.record_place_feedback(7, "nope", "NOT_INTERESTED", NOW)
        assert db.get_excluded_exp_ids(7, TODAY + timedelta(days=179)) >= {"visited", "nope"}
        assert "visited" not in db.get_excluded_exp_ids(7, TODAY + timedelta(days=181))
        assert "nope" in db.get_excluded_exp_ids(7, TODAY + timedelta(days=3649))
        assert "nope" not in db.get_excluded_exp_ids(7, TODAY + timedelta(days=3651))
        db.claim_sent_history(7, "weekend", "EXPERIENCE", "cooldown", "w", now=NOW)
        db.finalize_sent_history(7, "weekend", "EXPERIENCE", "cooldown", "w", "SENT")
        assert "cooldown" in db.get_excluded_exp_ids(7, TODAY + timedelta(days=55))
        assert "cooldown" not in db.get_excluded_exp_ids(7, TODAY + timedelta(days=57))
    finally:
        db.close()


def test_weekly_booking_id_cooldown_survives_open_time_change():
    db = _seed(KidsCompassDatabase(":memory:"))
    try:
        first = {m.chat_id: m for m in build_dday_push_messages(db, NOW)}["7"]
        weekly = next(e for e in db.list_upcoming_bookings("2000", "2100") if e.booking_id == 2)
        assert any(eid.startswith("2@") for eid in first.entity_ids)
        dispatcher = SafeTelegramDispatcher(sleep=lambda _: None)
        dispatcher.dispatch_push_messages(Channel(), db, [first], push_window(TODAY))
        changed = type(weekly)(weekly.booking_id, weekly.exp_id, weekly.title, "2026-11-30 10:00",
                               weekly.target_date_range, weekly.booking_url, weekly.memo)
        db.upsert_booking_events([changed])
        later = {m.chat_id: m for m in build_dday_push_messages(db, datetime(2026, 11, 24, 9, 0))}["7"]
        assert all(not eid.startswith("2@") for eid in later.entity_ids)
        assert any(eid.startswith("1@") for eid in later.entity_ids)
    finally:
        db.close()


def test_booking_mute_callback_permanently_excludes_push():
    db = _seed(KidsCompassDatabase(":memory:"))
    channel = Channel()
    try:
        msg = next(m for m in build_dday_push_messages(db, NOW) if any("@" in eid for eid in m.entity_ids))
        mute = next(b["callback_data"] for row in msg.reply_markup["inline_keyboard"] for b in row
                    if b["callback_data"] == "k:dd:m:2")
        bot = KidsCompassInteractiveBot(channel, db, clock=lambda: NOW)
        bot.handle_update({"callback_query": {"id": "mute", "data": mute,
                                               "message": {"chat": {"id": 7, "message_id": 1}}}})
        assert channel.acks == ["mute"]
        assert db.list_muted_booking_ids(7) == {2}
        assert all(2 not in [int(eid.split("@", 1)[0]) for eid in m.entity_ids]
                   for m in build_dday_push_messages(db, NOW))
    finally:
        db.close()


def test_d14_window_is_week_scoped_and_combined_message_is_one_send():
    db = _seed(KidsCompassDatabase(":memory:"))
    try:
        definition = next(d for d in db.list_milestone_definitions() if d.kind == "CHECKUP")
        first_child = db.list_children(7)[0]
        db.ensure_subject_milestone(7, "CHILD", first_child.child_id, definition.definition_id,
                                    definition.schedule_version, "2026-11-19", "2026-12-03")
        d14 = build_d14_milestone_push_messages(db, TODAY)
        assert d14 and d14[0].dispatch_window == "D14:2026-W47"
        dispatcher = SafeTelegramDispatcher(sleep=lambda _: None)
        dispatcher.dispatch_push_messages(Channel(), db, d14, d14[0].dispatch_window)

        second = db.add_child(7, "둘째", "2021-03-01", 18.0)
        db.ensure_subject_milestone(7, "CHILD", second, definition.definition_id,
                                    definition.schedule_version, "2026-12-20", "2027-01-03")
        next_month = build_d14_milestone_push_messages(db, date(2026, 12, 20))
        assert next_month and str(second) in {str(i.get("subject_id")) for i in db.list_d14_milestones(date(2026, 12, 20))}

        weekly = PushMessage("7", "weekly", {"inline_keyboard": [[{"text": "✅ 주간", "callback_data": "k:ms:ok:2"}]]},
                             "milestone", "MILESTONE", ["2"], "w")
        merged = _merge_milestone_push_messages([weekly], next_month, push_window(date(2026, 12, 20)))
        assert len(merged) == 1
        assert len([b for row in merged[0].reply_markup["inline_keyboard"] for b in row]) <= 7  # six + home
        sent_channel = Channel()
        stats = dispatcher.dispatch_combined_push_messages(sent_channel, db, merged)
        assert stats["sent"] == 1 and len(sent_channel.sent) == 1
    finally:
        db.close()


def test_free_feedback_input_and_cancel():
    db = KidsCompassDatabase(":memory:")
    channel = Channel()
    try:
        bot = KidsCompassInteractiveBot(channel, db, clock=lambda: NOW)
        bot.handle_update({"callback_query": {"id": "inq", "data": "k:inq",
                                               "message": {"chat": {"id": 7, "message_id": 1}}}})
        bot.handle_update({"message": {"chat": {"id": 7}, "text": "   "}})
        assert db.list_feedback_notes() == []
        bot.handle_update({"message": {"chat": {"id": 7}, "text": "예약 시간이 잘못 보여요"}})
        notes = db.list_feedback_notes()
        assert len(notes) == 1 and notes[0]["message"] == "예약 시간이 잘못 보여요"
        bot.handle_update({"callback_query": {"id": "inq2", "data": "k:inq",
                                               "message": {"chat": {"id": 7, "message_id": 2}}}})
        bot.handle_update({"message": {"chat": {"id": 7}, "text": "/cancel"}})
        assert db.get_onboarding_state(7) is None
        assert len(db.list_feedback_notes()) == 1
    finally:
        db.close()
