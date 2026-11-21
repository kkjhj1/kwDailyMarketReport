"""Kids Compass Step 1-B: timer UI/dispatch, D-14 cards, and hospital status."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from kids_compass.channels.telegram import (
    format_d14_milestone_push_html,
    format_dose_timer_push_html,
    format_hospital_list_html,
)
from kids_compass.collector import seed_all
from kids_compass.interactive_bot import (
    KidsCompassInteractiveBot,
    dispatch_due_dose_timers,
)
from kids_compass.models import DoseTimer, HospitalHourSlot, MoonlightHospital
from kids_compass.storage.db import KidsCompassDatabase


NOW = datetime(2026, 11, 21, 21, 0, 0)


class FakeChannel:
    def __init__(self):
        self.edits = []
        self.sent = []
        self.acks = []

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edits.append((chat_id, message_id, text, reply_markup))
        return True

    def send_message(self, chat_id, text, reply_markup=None, disable_notification=False):
        self.sent.append((chat_id, text, reply_markup))
        return len(self.sent)

    def answer_callback_query(self, callback_query_id, text=""):
        self.acks.append(callback_query_id)
        return True


def _hospital(hid="h1", placeholder=False):
    return MoonlightHospital(
        hospital_id=hid, name="달빛 <병원>", region_sido="서울", region_sigungu="중구",
        address="서울 중구", tel="02-0000-0000", weekday_hours="09:00~23:00",
        weekend_hours="09:00~18:00", holiday_hours="확인 필요", paired_pharmacy="달빛약국",
        paired_pharmacy_tel="", map_url="https://example.test/map", last_verified_at="2026-11-21",
        is_placeholder=placeholder,
    )


def test_timer_card_contains_safety_notice_and_never_pairs_nsaids():
    timer = DoseTimer(1, 1, "7", 3, "IBUPROFEN", "ACETAMINOPHEN", "", "", "PENDING")
    text = format_dose_timer_push_html("콩이", timer, "2026-11-21 21:00:00")
    assert "지금 다른 성분을 복용해도 된다는 의미가 아닙니다" in text
    assert "이부프로펜" in text and "아세트아미노펜" in text
    assert "이부프로펜, 덱시부프로펜" not in text


def test_hospital_open_status_is_placeholder_safe():
    h = _hospital()
    slots = [HospitalHourSlot("h1", d, False, 0, 1440, False, "0000", "2400", "OK") for d in range(7)]
    text = format_hospital_list_html([h], "서울", hours_by_hospital={"h1": slots}, now_dt=NOW)
    assert "🟢 영업 중" in text
    assert "달빛 &lt;병원&gt;" in text
    assert "⚪ 전화 확인 필수" in format_hospital_list_html([_hospital(placeholder=True)], "서울")


def test_fever_log_button_only_for_registered_child_with_ok_result():
    db = KidsCompassDatabase(":memory:")
    seed_all(db, NOW.date())
    cid = db.add_child(7, "콩이", "2024-05-01", 11.0)
    channel = FakeChannel()
    bot = KidsCompassInteractiveBot(channel, db, clock=lambda: NOW)
    child = db.get_child(cid, 7)
    bot._show_fever_result(7, None, 11.0, 30, child)
    callbacks = [b["callback_data"] for row in channel.sent[-1][2]["inline_keyboard"] for b in row]
    assert any(data.startswith("k:fvr:log:") for data in callbacks)

    channel.sent.clear()
    bot._show_fever_result(7, None, 11.0, 5, None, age_label="4~5개월")
    assert not any("fvr:log" in b["callback_data"] for row in channel.sent[-1][2]["inline_keyboard"] for b in row)
    db.close()


def test_log_callback_records_dose_and_timer_then_dispatches_once():
    db = KidsCompassDatabase(":memory:")
    seed_all(db, NOW.date())
    cid = db.add_child(7, "콩이", "2024-05-01", 11.0)
    channel = FakeChannel()
    bot = KidsCompassInteractiveBot(channel, db, clock=lambda: NOW)
    bot._show_fever_result(7, None, 11.0, 30, db.get_child(cid, 7))
    data = next(
        b["callback_data"]
        for row in channel.sent[-1][2]["inline_keyboard"]
        for b in row
        if "fvr:log" in b["callback_data"]
    )
    bot.handle_update({"callback_query": {"id": "cq1", "data": data,
                                           "message": {"chat": {"id": 7, "message_id": 1}}}})
    assert channel.acks == ["cq1"]
    assert db.conn.execute("SELECT COUNT(*) FROM kids_dose_events").fetchone()[0] == 1
    timer = db.conn.execute("SELECT timer_id, due_at FROM kids_dose_timers WHERE status='PENDING'").fetchone()
    assert timer and timer[1] == "2026-11-21 23:30:00"

    due = NOW + timedelta(minutes=150)
    stats = dispatch_due_dose_timers(db, channel, due)
    assert stats["sent"] == 1
    assert db.conn.execute("SELECT status FROM kids_dose_timers").fetchone()[0] == "SENT"
    assert dispatch_due_dose_timers(db, channel, due)["total"] == 0
    db.close()


def test_d14_card_has_due_date_dday_and_confirmation_prompt():
    text = format_d14_milestone_push_html(
        [{"subject": "콩이", "kind": "CHECKUP", "title": "건강검진", "due_to": "2026-12-05"}],
        date(2026, 11, 21),
    )
    assert "콩이" in text and "2026-12-05" in text and "D-14" in text
    assert "확인 완료" in text
