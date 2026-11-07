"""Phase A parent conveniences: multi-child profile switching & Google Calendar deeplinks."""
import sqlite3
import urllib.parse
from datetime import datetime
from unittest.mock import MagicMock

from admission_brief.channels.telegram import GCAL_MAX_URL_BYTES, TelegramChannel, build_google_calendar_url
from admission_brief.interactive_bot import (
    AdmissionInteractiveBot,
    _dday_label,
    build_events_page_keyboard,
)
from admission_brief.models import (
    AdmissionEvent,
    BriefingType,
    ChildProfile,
    EventCategory,
    ParentUserProfile,
    TargetGrade,
    TargetRegion,
)
from admission_brief.storage.db import AdmissionDatabase

NOW = datetime(2026, 11, 7, 12, 0)


def _event(date_str="2026-11-20 19:30", so_what="정시 배치표 핵심 해석", **kw):
    base = dict(
        event_id="e1",
        title="2027 정시 지원전략 설명회",
        organizer="종로학원",
        target_grade=TargetGrade.HIGH_3_N,
        event_type=BriefingType.BRIEFING_RECAP,
        event_date_str=date_str,
        target_region=TargetRegion.SEOUL,
        booking_url="https://www.jongro.co.kr",
        so_what=so_what,
        category=EventCategory.BRIEFING,
    )
    base.update(kw)
    return AdmissionEvent(**base)


def _cb(data, chat_id=888, message_id=77):
    return {
        "update_id": 1,
        "callback_query": {"id": "cb1", "data": data, "message": {"message_id": message_id, "chat": {"id": chat_id}}},
    }


def _callbacks(markup):
    return [b["callback_data"] for row in markup["inline_keyboard"] for b in row if "callback_data" in b]


def _bot(db):
    ch = MagicMock(spec=TelegramChannel)
    ch.edit_message_text.return_value = True
    return AdmissionInteractiveBot(channel=ch, db=db), ch


# --- models ---

def test_child_profile_roundtrip_and_bad_values():
    c = ChildProfile(alias="둘째", grade=TargetGrade.MIDDLE, region=TargetRegion.SEOUL)
    assert ChildProfile.from_dict(c.to_dict()) == c
    bad = ChildProfile.from_dict({"alias": "", "grade": "NOPE", "region": "MARS"})
    assert bad.grade == TargetGrade.ALL and bad.region == TargetRegion.ALL and bad.alias == "자녀"


def test_parent_profile_legacy_fallback_and_multi_child():
    legacy = ParentUserProfile(chat_id="1", target_grade=TargetGrade.HIGH_3_N, target_region=TargetRegion.SEOUL)
    active = legacy.get_active_child()
    assert (active.alias, active.grade, active.region) == ("자녀", TargetGrade.HIGH_3_N, TargetRegion.SEOUL)
    assert legacy.get_all_grades() == {TargetGrade.HIGH_3_N}

    multi = ParentUserProfile(
        chat_id="2",
        children=[ChildProfile("첫째", TargetGrade.HIGH_3_N), ChildProfile("둘째", TargetGrade.MIDDLE)],
        active_child_index=1,
    )
    assert multi.get_active_child().alias == "둘째"
    assert multi.get_all_grades() == {TargetGrade.HIGH_3_N, TargetGrade.MIDDLE}
    multi.active_child_index = 9  # out of range → legacy fallback, never IndexError
    assert multi.get_active_child().alias == "자녀"
    d = multi.to_dict()
    assert d["children"][1] == {"alias": "둘째", "grade": "MIDDLE", "region": "ALL"}
    assert d["active_child_index"] == 9


# --- storage ---

def test_additive_migration_on_legacy_subscribers_table(tmp_path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE admission_subscribers (chat_id TEXT PRIMARY KEY, target_grade TEXT NOT NULL DEFAULT 'ALL',"
        " username TEXT, first_name TEXT, alert_enabled INTEGER NOT NULL DEFAULT 1,"
        " registered_at TEXT NOT NULL, last_active_at TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO admission_subscribers VALUES ('7', 'MIDDLE', 'u', 'f', 1, ?, ?)",
        (NOW.isoformat(), NOW.isoformat()),
    )
    conn.commit()
    conn.close()

    db = AdmissionDatabase(db_path=path)
    p = db.get_subscriber("7")
    assert p.target_grade == TargetGrade.MIDDLE and p.children == [] and p.active_child_index == 0
    assert [s.chat_id for s in db.list_subscribers(grade=TargetGrade.MIDDLE)] == ["7"]
    AdmissionDatabase(db_path=path)  # re-running migration is a no-op


def test_add_child_seeds_first_child_and_caps_at_three():
    db = AdmissionDatabase(db_path=":memory:")
    db.upsert_subscriber(ParentUserProfile(chat_id="1", target_grade=TargetGrade.HIGH_3_N, target_region=TargetRegion.SEOUL))
    assert db.add_child("1", ChildProfile("둘째", TargetGrade.MIDDLE, TargetRegion.SEOUL))
    p = db.get_subscriber("1")
    assert [(c.alias, c.grade) for c in p.children] == [("첫째", TargetGrade.HIGH_3_N), ("둘째", TargetGrade.MIDDLE)]
    assert p.active_child_index == 0 and p.target_grade == TargetGrade.HIGH_3_N
    assert db.add_child("1", ChildProfile("셋째", TargetGrade.HIGH_1_2))
    assert not db.add_child("1", ChildProfile("넷째", TargetGrade.ALL))
    assert len(db.get_subscriber("1").children) == 3
    assert not db.add_child("missing", ChildProfile())


def test_set_active_child_mirrors_legacy_columns_and_grade_update_syncs_child():
    db = AdmissionDatabase(db_path=":memory:")
    db.upsert_subscriber(ParentUserProfile(chat_id="1", target_grade=TargetGrade.HIGH_3_N, target_region=TargetRegion.SEOUL))
    db.add_child("1", ChildProfile("둘째", TargetGrade.MIDDLE, TargetRegion.ONLINE))

    assert db.set_active_child("1", 1)
    p = db.get_subscriber("1")
    assert p.active_child_index == 1
    assert (p.target_grade, p.target_region) == (TargetGrade.MIDDLE, TargetRegion.ONLINE)
    assert not db.set_active_child("1", 5)
    assert not db.set_active_child("1", -1)

    # Grade/region edits apply to the active child only
    db.update_subscriber_grade("1", TargetGrade.HIGH_1_2)
    db.update_subscriber_region("1", TargetRegion.GYEONGGI)
    p = db.get_subscriber("1")
    assert (p.children[1].grade, p.children[1].region) == (TargetGrade.HIGH_1_2, TargetRegion.GYEONGGI)
    assert (p.children[0].grade, p.children[0].region) == (TargetGrade.HIGH_3_N, TargetRegion.SEOUL)
    assert p.target_grade == TargetGrade.HIGH_1_2


def test_list_subscribers_multi_child_receives_every_childs_grade():
    db = AdmissionDatabase(db_path=":memory:")
    db.upsert_subscriber(ParentUserProfile(chat_id="multi", target_grade=TargetGrade.HIGH_3_N, target_region=TargetRegion.SEOUL))
    db.add_child("multi", ChildProfile("둘째", TargetGrade.MIDDLE, TargetRegion.ONLINE))
    db.upsert_subscriber(ParentUserProfile(chat_id="legacy", target_grade=TargetGrade.HIGH_1_2))

    def ids(**kw):
        return sorted(s.chat_id for s in db.list_subscribers(**kw))

    assert ids(grade=TargetGrade.HIGH_3_N) == ["multi"]
    assert ids(grade=TargetGrade.MIDDLE) == ["multi"]
    assert ids(grade=TargetGrade.HIGH_1_2) == ["legacy"]
    assert ids(grade=TargetGrade.ALL) == ["legacy", "multi"]
    # Region is matched per child: the 중등 child only wants online events
    assert ids(grade=TargetGrade.MIDDLE, region=TargetRegion.SEOUL) == []
    assert ids(grade=TargetGrade.MIDDLE, region=TargetRegion.SEOUL, is_online_live=True) == ["multi"]
    assert ids(grade=TargetGrade.HIGH_3_N, region=TargetRegion.SEOUL) == ["multi"]


def test_corrupt_children_json_falls_back_to_legacy():
    db = AdmissionDatabase(db_path=":memory:")
    db.upsert_subscriber(ParentUserProfile(chat_id="1", target_grade=TargetGrade.MIDDLE))
    with db._get_connection() as conn:
        conn.execute("UPDATE admission_subscribers SET children_json = '{broken', active_child_index = 4")
    p = db.get_subscriber("1")
    assert p.children == [] and p.get_active_child().grade == TargetGrade.MIDDLE


# --- Google Calendar deeplink ---

def test_calendar_url_converts_kst_to_utc_with_two_hour_slot():
    url = build_google_calendar_url(_event(), now=NOW)
    assert url.startswith("https://calendar.google.com/calendar/render?")
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["action"] == ["TEMPLATE"]
    assert q["dates"] == ["20261120T103000Z/20261120T123000Z"]
    assert q["text"] == ["[종로학원] 2027 정시 지원전략 설명회"]
    assert q["location"] == [TargetRegion.SEOUL.label]
    assert "원문/신청: https://www.jongro.co.kr" in q["details"][0]


def test_calendar_url_crosses_utc_midnight_and_all_day():
    q = urllib.parse.parse_qs(urllib.parse.urlparse(build_google_calendar_url(_event("2026-11-20 07:00"), now=NOW)).query)
    assert q["dates"] == ["20261119T220000Z/20261120T000000Z"]
    q = urllib.parse.parse_qs(urllib.parse.urlparse(build_google_calendar_url(_event("2026-11-20"), now=NOW)).query)
    assert q["dates"] == ["20261120/20261121"]
    assert build_google_calendar_url(_event("2026-11-07"), now=NOW) is not None  # today, all-day


def test_calendar_url_none_for_past_or_undated():
    assert build_google_calendar_url(_event("2026-11-07 09:00"), now=NOW) is None
    assert build_google_calendar_url(_event("2026-10-01"), now=NOW) is None
    assert build_google_calendar_url(_event("일정 추후 공지(TBA)"), now=NOW) is None
    assert build_google_calendar_url(_event(""), now=NOW) is None


def test_calendar_url_truncates_long_details_under_limit():
    url = build_google_calendar_url(_event(so_what="수시 6장 전략 " * 400), now=NOW)
    assert url is not None and len(url.encode("utf-8")) <= GCAL_MAX_URL_BYTES
    q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert q["details"][0].startswith("주최: 종로학원") and q["details"][0].endswith("…")


def test_events_page_keyboard_has_calendar_url_buttons():
    events = [_event("2099-01-02 14:00", event_id="a"), _event("일정 추후 공지", event_id="b")]
    kb = build_events_page_keyboard(EventCategory.BRIEFING, 0, 2, events)
    url_buttons = [b for row in kb["inline_keyboard"] for b in row if "url" in b]
    assert len(url_buttons) == 1 and url_buttons[0]["text"] == "📅 1번 구글 캘린더에 담기"
    assert _callbacks(kb) == ["adm:menu"]
    # Backward compatible 3-arg call
    assert build_events_page_keyboard(EventCategory.BRIEFING, 0, 2)["inline_keyboard"] == [
        [{"text": "🏛 카테고리 목록", "callback_data": "adm:menu"}]
    ]


def test_dday_label():
    assert _dday_label("2026-11-20 19:30", now=NOW) == " · D-13"
    assert _dday_label("2026-11-07", now=NOW) == " · <b>D-Day</b>"
    assert _dday_label("2026-11-01", now=NOW) == ""
    assert _dday_label("TBA", now=NOW) == ""


# --- interactive bot ---

def test_settings_legacy_shows_add_but_no_toggle():
    db = AdmissionDatabase(db_path=":memory:")
    db.upsert_subscriber(ParentUserProfile(chat_id="888", target_grade=TargetGrade.HIGH_3_N))
    bot, ch = _bot(db)
    bot.handle_update({"message": {"chat": {"id": 888}, "text": "/settings"}})
    text = ch.send_message.call_args[0][1]
    markup = ch.send_message.call_args[1]["reply_markup"]
    assert "맞춤 알림 설정 현황" in text and "등록 자녀" not in text
    assert _callbacks(markup) == ["menu:grade", "menu:region", "adm:child:add"]


def test_add_child_flow_then_toggle_in_place():
    db = AdmissionDatabase(db_path=":memory:")
    db.upsert_subscriber(ParentUserProfile(chat_id="888", target_grade=TargetGrade.HIGH_3_N, target_region=TargetRegion.SEOUL))
    bot, ch = _bot(db)

    bot.handle_update(_cb("adm:child:add"))
    picker = ch.edit_message_text.call_args[1]["reply_markup"]
    assert "둘째" in ch.edit_message_text.call_args[0][2]
    assert "adm:cadd:MIDDLE:1" in _callbacks(picker)

    bot.handle_update(_cb("adm:cadd:MIDDLE:1"))
    p = db.get_subscriber("888")
    assert [(c.alias, c.grade, c.region) for c in p.children] == [
        ("첫째", TargetGrade.HIGH_3_N, TargetRegion.SEOUL),
        ("둘째", TargetGrade.MIDDLE, TargetRegion.SEOUL),
    ]
    text = ch.edit_message_text.call_args[0][2]
    assert "추가되었습니다" in text
    assert "• <b>첫째</b>: 📕 고3/수험생 (서울) ✅" in text
    assert "• <b>둘째</b>: 📘 중등/예비고1 (서울)" in text
    markup = ch.edit_message_text.call_args[1]["reply_markup"]
    assert "adm:child:toggle:1:MIDDLE" in _callbacks(markup)

    # Double-tap of the stale picker must not add a duplicate child
    bot.handle_update(_cb("adm:cadd:MIDDLE:1"))
    assert len(db.get_subscriber("888").children) == 2

    ch.reset_mock()
    ch.edit_message_text.return_value = True
    bot.handle_update(_cb("adm:child:toggle:1:MIDDLE"))
    ch.answer_callback_query.assert_called_once_with("cb1", text="✅ 둘째 (중등 / 예비고1) 모드로 전환되었습니다!")
    p = db.get_subscriber("888")
    assert p.active_child_index == 1 and p.target_grade == TargetGrade.MIDDLE
    assert "모드로 전환되었습니다" in ch.edit_message_text.call_args[0][2]
    assert "adm:child:toggle:0:HIGH_3_N" in _callbacks(ch.edit_message_text.call_args[1]["reply_markup"])

    # Double-tap of the same toggle is idempotent (stays on 둘째)
    bot.handle_update(_cb("adm:child:toggle:1:MIDDLE"))
    assert db.get_subscriber("888").active_child_index == 1

    # Bare spec form cycles to the next child
    bot.handle_update(_cb("adm:child:toggle"))
    assert db.get_subscriber("888").active_child_index == 0


def test_add_child_hidden_at_max_and_rejected():
    db = AdmissionDatabase(db_path=":memory:")
    db.upsert_subscriber(ParentUserProfile(chat_id="888", target_grade=TargetGrade.HIGH_3_N))
    db.add_child("888", ChildProfile("둘째", TargetGrade.MIDDLE))
    db.add_child("888", ChildProfile("셋째", TargetGrade.HIGH_1_2))
    bot, ch = _bot(db)
    bot.handle_update(_cb("adm:settings"))
    assert "adm:child:add" not in _callbacks(ch.edit_message_text.call_args[1]["reply_markup"])
    bot.handle_update(_cb("adm:child:add"))
    assert "최대 3명" in ch.edit_message_text.call_args[0][2]
    bot.handle_update(_cb("adm:cadd:MIDDLE"))
    assert len(db.get_subscriber("888").children) == 3


def test_toggle_without_children_and_invalid_grade_are_safe():
    db = AdmissionDatabase(db_path=":memory:")
    db.upsert_subscriber(ParentUserProfile(chat_id="888"))
    bot, ch = _bot(db)
    bot.handle_update(_cb("adm:child:toggle"))
    assert "전환할 자녀가 없습니다" in ch.edit_message_text.call_args[0][2]
    bot.handle_update(_cb("adm:cadd:BOGUS:1"))
    assert db.get_subscriber("888").children == []


def test_child_callbacks_ack_first_and_fit_64_bytes():
    db = AdmissionDatabase(db_path=":memory:")
    db.upsert_subscriber(ParentUserProfile(chat_id="888", target_grade=TargetGrade.HIGH_3_N))
    db.add_child("888", ChildProfile("둘째", TargetGrade.HIGH_1_2))
    order = []
    real_get = db.get_subscriber
    db.get_subscriber = lambda cid: (order.append("db"), real_get(cid))[1]
    bot, ch = _bot(db)
    ch.answer_callback_query.side_effect = lambda *a, **k: order.append("ack")
    for data in ("adm:child:toggle:1:HIGH_1_2", "adm:child:toggle", "adm:child:add", "adm:cadd:MIDDLE:2", "adm:settings"):
        order.clear()
        bot.handle_update(_cb(data))
        assert order[0] == "ack" and order.count("ack") == 1, data

    db.get_subscriber = real_get
    bot._cmd_settings("888", 77)
    for cbd in _callbacks(ch.edit_message_text.call_args[1]["reply_markup"]):
        assert len(cbd.encode("utf-8")) <= 64


def test_todo_and_events_follow_active_child():
    db = AdmissionDatabase(db_path=":memory:")
    db.upsert_subscriber(ParentUserProfile(chat_id="888", target_grade=TargetGrade.HIGH_3_N))
    db.add_child("888", ChildProfile("둘째", TargetGrade.MIDDLE))
    db.set_active_child("888", 1)
    bot, ch = _bot(db)
    bot.handle_update({"message": {"chat": {"id": 888}, "text": "/todo"}})
    briefing = ch.send.call_args[0][1]
    assert briefing.target_grade == TargetGrade.MIDDLE
    assert briefing.headline.startswith("[둘째] ")

    bot.handle_update({"message": {"chat": {"id": 888}, "text": "/events"}})
    assert TargetGrade.MIDDLE.label in ch.send_message.call_args[0][1]
