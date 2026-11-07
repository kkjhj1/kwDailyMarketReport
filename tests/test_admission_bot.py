"""Unit tests for Admission Telegram Bot transport and interactive handlers."""
import io
import json
from unittest.mock import MagicMock
from admission_brief.ai_curator import AICurator
from admission_brief.channels.telegram import (
    TG_MAX_HTML_LEN,
    TG_MAX_VISIBLE_LEN,
    TelegramChannel,
    format_admission_html,
    measure_message,
)
from admission_brief.collector import AdmissionCollector
from admission_brief.interactive_bot import (
    AdmissionInteractiveBot,
    build_events_category_keyboard,
    build_events_page_keyboard,
    build_grade_inline_keyboard,
    build_persistent_reply_keyboard,
    format_events_page_html,
)
from admission_brief.models import (
    AdmissionBriefing,
    AdmissionEvent,
    BriefingType,
    EventCategory,
    ParentUserProfile,
    TargetGrade,
    TargetRegion,
)
from admission_brief.storage.db import AdmissionDatabase


def test_format_admission_html():
    briefing = AdmissionBriefing(
        briefing_id="b_test",
        briefing_type=BriefingType.TICKETING_ALERT,
        target_grade=TargetGrade.MIDDLE,
        headline="시대인재 윈터스쿨 접수 오픈",
        summary_3lines=[
            "2028 수능 개편 후 통합사회·통합과학 1등급 컷 분석",
            "주요 자사고 합격생 시간표 공개",
            "수학(상/하) 심화 가이드",
        ],
        so_what="첫 중간고사 성적이 대입의 70%를 결정합니다.",
        action_todos=["14:00 선착순 접수 1분 내 완료", "수학 결손 진단"],
        booking_url="https://sdij.com/test",
    )

    html_out = format_admission_html(briefing)
    assert "⚡ <b>[선착순 예약 마감 임박 알람]</b>" in html_out
    assert "📘 [중등/예비고1]" in html_out
    assert "시대인재 윈터스쿨 접수 오픈" in html_out
    assert "<b>1.</b> 2028 수능 개편 후" in html_out
    assert "학부모 득실 분석" in html_out
    assert "이번 주 필수 행동 지침" in html_out


def test_build_grade_inline_keyboard():
    kb = build_grade_inline_keyboard()
    buttons = [row[0] for row in kb["inline_keyboard"]]
    callbacks = [b["callback_data"] for b in buttons]
    assert "grade:MIDDLE" in callbacks
    assert "grade:HIGH_1_2" in callbacks
    assert "grade:HIGH_3_N" in callbacks
    assert "grade:ALL" in callbacks


def test_interactive_bot_start_command():
    db = AdmissionDatabase(db_path=":memory:")
    mock_channel = MagicMock(spec=TelegramChannel)
    bot = AdmissionInteractiveBot(channel=mock_channel, db=db)

    update = {
        "update_id": 100,
        "message": {
            "message_id": 1,
            "chat": {"id": 12345, "first_name": "영희맘", "username": "yh_mom"},
            "text": "/start",
        },
    }

    bot.handle_update(update)

    # 1. Subscriber registered in DB
    sub = db.get_subscriber("12345")
    assert sub is not None
    assert sub.first_name == "영희맘"

    # 2. Welcome message with inline keyboard sent
    mock_channel.send_message.assert_called_once()
    args, kwargs = mock_channel.send_message.call_args
    assert args[0] == "12345"
    assert "영희맘" in args[1]
    assert "inline_keyboard" in kwargs.get("reply_markup", {})


def test_interactive_bot_callback_query_grade_and_region_selection():
    db = AdmissionDatabase(db_path=":memory:")
    # Pre-register user with ALL
    db.upsert_subscriber(ParentUserProfile(chat_id="12345", target_grade=TargetGrade.ALL))

    mock_channel = MagicMock(spec=TelegramChannel)
    bot = AdmissionInteractiveBot(channel=mock_channel, db=db)

    # 1. Grade selection callback
    callback_update_grade = {
        "update_id": 101,
        "callback_query": {
            "id": "cb_query_999",
            "data": "grade:HIGH_1_2",
            "message": {
                "message_id": 42,
                "chat": {"id": 12345},
            },
        },
    }

    bot.handle_update(callback_update_grade)

    # DB should now have HIGH_1_2
    sub = db.get_subscriber("12345")
    assert sub.target_grade == TargetGrade.HIGH_1_2

    # Acknowledge callback and edit message with region keyboard
    mock_channel.answer_callback_query.assert_called_with("cb_query_999", text="✅ 고1 ~ 고2 설정 완료!")
    args, kwargs = mock_channel.edit_message_text.call_args
    assert args[0] == "12345"
    assert args[1] == 42
    assert "고1~고2" in args[2]
    assert "inline_keyboard" in kwargs.get("reply_markup", {})

    # 2. Region selection callback
    callback_update_region = {
        "update_id": 102,
        "callback_query": {
            "id": "cb_query_1000",
            "data": "region:ONLINE",
            "message": {
                "message_id": 42,
                "chat": {"id": 12345},
            },
        },
    }

    bot.handle_update(callback_update_region)
    sub = db.get_subscriber("12345")
    assert sub.target_region.value == "ONLINE"
    mock_channel.answer_callback_query.assert_called_with("cb_query_1000", text="✅ 🌐 온라인/유튜브 라이브만 설정 완료!")


def test_interactive_bot_todo_command():
    db = AdmissionDatabase(db_path=":memory:")
    db.upsert_subscriber(ParentUserProfile(chat_id="555", target_grade=TargetGrade.MIDDLE))

    mock_channel = MagicMock(spec=TelegramChannel)
    bot = AdmissionInteractiveBot(channel=mock_channel, db=db)

    update = {
        "update_id": 102,
        "message": {
            "message_id": 2,
            "chat": {"id": 555},
            "text": "/todo",
        },
    }

    bot.handle_update(update)

    # channel.send should be invoked with MIDDLE grade briefing
    mock_channel.send.assert_called_once()
    chat_id, briefing = mock_channel.send.call_args[0]
    assert chat_id == "555"
    assert briefing.target_grade == TargetGrade.MIDDLE
    assert briefing.briefing_type == BriefingType.WEEKLY_TODO


def test_build_persistent_reply_keyboard():
    kb = build_persistent_reply_keyboard()
    assert kb["resize_keyboard"] is True
    assert kb["is_persistent"] is True
    all_btn_texts = [btn["text"] for row in kb["keyboard"] for btn in row]
    assert "📋 이번 주 필수 To-Do" in all_btn_texts
    assert "⚡ 긴급 속보 & 핫 행사 TOP 3" in all_btn_texts
    assert "💡 1분 입시 팩트체크 (So-What)" in all_btn_texts
    assert "🎪 설명회·박람회 전체보기" in all_btn_texts
    assert "🎓 학년/지역 설정" in all_btn_texts
    assert "❓ 이용 도움말" in all_btn_texts
    assert "📖 1초 입시용어 사전" in all_btn_texts
    assert [len(row) for row in kb["keyboard"]] in ([2, 2, 2], [2, 2, 2, 2])


def test_interactive_bot_korean_text_touch_buttons():
    db = AdmissionDatabase(db_path=":memory:")
    db.upsert_subscriber(ParentUserProfile(chat_id="777", target_grade=TargetGrade.HIGH_1_2))

    mock_channel = MagicMock(spec=TelegramChannel)
    bot = AdmissionInteractiveBot(channel=mock_channel, db=db)

    # 1. Tap "📋 이번 주 필수 To-Do"
    bot.handle_update({
        "update_id": 201,
        "message": {"message_id": 10, "chat": {"id": 777}, "text": "📋 이번 주 필수 To-Do"},
    })
    mock_channel.send.assert_called_once()
    chat_id, briefing = mock_channel.send.call_args[0]
    assert chat_id == "777"
    assert briefing.target_grade == TargetGrade.HIGH_1_2

    # 2. Tap "⚡ 긴급 속보 & 핫 행사 TOP 3" (fetches top 3 events)
    bot.handle_update({
        "update_id": 202,
        "message": {"message_id": 11, "chat": {"id": 777}, "text": "⚡ 긴급 속보 & 핫 행사 TOP 3"},
    })
    # 1 (todo) + 3 (top3 events) = 4 sends
    assert mock_channel.send.call_count == 4

    # 3. Tap "💡 1분 입시 팩트체크 (So-What)"
    bot.handle_update({
        "update_id": 203,
        "message": {"message_id": 12, "chat": {"id": 777}, "text": "💡 1분 입시 팩트체크 (So-What)"},
    })
    assert mock_channel.send.call_count == 5
    last_chat_id, last_briefing = mock_channel.send.call_args[0]
    assert last_chat_id == "777"
    assert last_briefing.briefing_type == BriefingType.POLICY_TRANSLATE


# --- Events menu (박람회 / 대형설명회 / 테마세미나) ---

def _future_event(event_id, category, day, grade=TargetGrade.HIGH_3_N, title=None):
    return AdmissionEvent(
        event_id=event_id,
        title=title or f"행사 {event_id}",
        organizer="종로학원",
        target_grade=grade,
        target_region=TargetRegion.SEOUL,
        is_online_live=True,
        event_type=BriefingType.BRIEFING_RECAP,
        event_date_str=f"2099-01-{day:02d} 14:00",
        booking_url="https://www.jongro.co.kr",
        category=category,
    )


def _cb(data, cb_id="cb_evt", chat_id=888, message_id=77):
    return {
        "update_id": 900,
        "callback_query": {
            "id": cb_id,
            "data": data,
            "message": {"message_id": message_id, "chat": {"id": chat_id}},
        },
    }


def _all_callback_data(markup):
    return [b["callback_data"] for row in markup["inline_keyboard"] for b in row if "callback_data" in b]


def test_events_menu_keyboard_and_callback_byte_limit():
    kb = build_events_category_keyboard()
    assert _all_callback_data(kb) == ["adm:cat:EXPO:0", "adm:cat:BRIEF:0", "adm:cat:SEMI:0"]
    for cat in EventCategory:
        for page in (0, 5, 999):
            for data in _all_callback_data(build_events_page_keyboard(cat, page, 10_000)):
                assert data.isascii()
                assert len(data.encode("utf-8")) <= 64


def test_events_menu_text_routing():
    db = AdmissionDatabase(db_path=":memory:")
    mock_channel = MagicMock(spec=TelegramChannel)
    bot = AdmissionInteractiveBot(channel=mock_channel, db=db)

    for text in ("🎪 설명회·박람회 전체보기", "박람회", "설명회 일정", "세미나", "행사 전체", "/events"):
        mock_channel.reset_mock()
        bot.handle_update({"update_id": 1, "message": {"chat": {"id": 888}, "text": text}})
        mock_channel.send_message.assert_called_once()
        _, kwargs = mock_channel.send_message.call_args
        assert _all_callback_data(kwargs["reply_markup"]) == ["adm:cat:EXPO:0", "adm:cat:BRIEF:0", "adm:cat:SEMI:0"]

    # Existing hot-events button still routes to TOP 3 (channel.send), not the events menu
    mock_channel.reset_mock()
    bot.handle_update({"update_id": 2, "message": {"chat": {"id": 888}, "text": "⚡ 긴급 속보 & 핫 행사 TOP 3"}})
    mock_channel.send_message.assert_not_called()
    assert mock_channel.send.call_count >= 1


def test_callback_answer_precedes_db_access():
    db = AdmissionDatabase(db_path=":memory:")
    order = []
    real_get = db.get_subscriber
    db.get_subscriber = lambda cid: (order.append("db"), real_get(cid))[1]
    mock_channel = MagicMock(spec=TelegramChannel)
    mock_channel.answer_callback_query.side_effect = lambda *a, **k: order.append("ack")
    bot = AdmissionInteractiveBot(channel=mock_channel, db=db)

    for data in ("adm:cat:BRIEF:0", "grade:MIDDLE", "region:SEOUL", "menu:grade", "adm:menu"):
        order.clear()
        bot.handle_update(_cb(data))
        assert order[0] == "ack", data
        assert order.count("ack") == 1, data


def test_category_page_navigation_in_place():
    db = AdmissionDatabase(db_path=":memory:")
    for i in range(1, 8):
        db.save_event(_future_event(f"b{i}", EventCategory.BRIEFING, i))
    db.save_event(_future_event("x1", EventCategory.EXPO, 1))
    mock_channel = MagicMock(spec=TelegramChannel)
    mock_channel.edit_message_text.return_value = True
    bot = AdmissionInteractiveBot(channel=mock_channel, db=db)

    # Page 0: 3 cards, only "다음" + category list
    bot.handle_update(_cb("adm:cat:BRIEF:0"))
    args, kwargs = mock_channel.edit_message_text.call_args
    assert args[0] == "888" and args[1] == 77
    text = args[2]
    assert "5대 대형 입시기관 설명회" in text and "1/3쪽" in text
    assert "행사 b1" in text and "행사 b3" in text and "행사 b4" not in text
    assert "행사 x1" not in text
    assert _all_callback_data(kwargs["reply_markup"]) == ["adm:cat:BRIEF:1", "adm:menu"]
    mock_channel.send_message.assert_not_called()

    # Middle page: both directions
    bot.handle_update(_cb("adm:cat:BRIEF:1"))
    args, kwargs = mock_channel.edit_message_text.call_args
    assert "<b>4. 행사 b4</b>" in args[2]
    assert _all_callback_data(kwargs["reply_markup"]) == ["adm:cat:BRIEF:0", "adm:cat:BRIEF:2", "adm:menu"]

    # Last page: only "이전"
    bot.handle_update(_cb("adm:cat:BRIEF:2"))
    args, kwargs = mock_channel.edit_message_text.call_args
    assert "행사 b7" in args[2] and "3/3쪽" in args[2]
    assert _all_callback_data(kwargs["reply_markup"]) == ["adm:cat:BRIEF:1", "adm:menu"]

    # Stale page beyond range clamps to last page
    bot.handle_update(_cb("adm:cat:BRIEF:9"))
    args, _ = mock_channel.edit_message_text.call_args
    assert "3/3쪽" in args[2]

    # Back to category list
    bot.handle_update(_cb("adm:menu"))
    _, kwargs = mock_channel.edit_message_text.call_args
    assert _all_callback_data(kwargs["reply_markup"]) == ["adm:cat:EXPO:0", "adm:cat:BRIEF:0", "adm:cat:SEMI:0"]


def test_category_page_empty_and_edit_fallback():
    db = AdmissionDatabase(db_path=":memory:")
    db.upsert_subscriber(ParentUserProfile(chat_id="888", target_grade=TargetGrade.MIDDLE))
    db.save_event(_future_event("s1", EventCategory.SEMINAR, 1, grade=TargetGrade.HIGH_3_N))
    mock_channel = MagicMock(spec=TelegramChannel)
    mock_channel.edit_message_text.return_value = False  # e.g. message too old to edit
    bot = AdmissionInteractiveBot(channel=mock_channel, db=db)

    bot.handle_update(_cb("adm:cat:SEMI:0"))
    mock_channel.send_message.assert_called_once()
    args, kwargs = mock_channel.send_message.call_args
    assert "예정 행사가 없습니다" in args[1]
    assert _all_callback_data(kwargs["reply_markup"]) == ["adm:menu"]

    # Unknown category code falls back to the category menu
    mock_channel.reset_mock()
    mock_channel.edit_message_text.return_value = True
    bot.handle_update(_cb("adm:cat:NOPE:0"))
    _, kwargs = mock_channel.edit_message_text.call_args
    assert _all_callback_data(kwargs["reply_markup"])[0] == "adm:cat:EXPO:0"


def test_events_page_html_escape_and_limits():
    malicious = _future_event("m1", EventCategory.BRIEFING, 1, title="<script>alert(1)</script> & 설명회")
    out = format_events_page_html(EventCategory.BRIEFING, [malicious], 0, 1, TargetGrade.ALL, TargetRegion.ALL)
    assert "<script>" not in out
    assert "&lt;script&gt;" in out

    # Every collector page (3 cards) stays within [TG-01] limits
    db = AdmissionDatabase(db_path=":memory:")
    events = AdmissionCollector(db=db, curator=AICurator()).collect_public_education_notices()
    events += AdmissionCollector(db=db, curator=AICurator()).collect_major_academy_seminars()
    for cat in EventCategory:
        cat_events = [e for e in events if e.category == cat]
        for page in range(0, len(cat_events), 3):
            out = format_events_page_html(cat, cat_events[page:page + 3], page // 3, len(cat_events),
                                          TargetGrade.ALL, TargetRegion.ALL)
            html_len, visible_len = measure_message(out)
            assert html_len <= TG_MAX_HTML_LEN
            assert visible_len <= TG_MAX_VISIBLE_LEN


def test_format_admission_html_category_badge_and_limits():
    curator = AICurator()
    events = AdmissionCollector(db=AdmissionDatabase(db_path=":memory:"), curator=curator).collect_public_education_notices()
    expo = next(e for e in events if e.category == EventCategory.EXPO)
    briefing = curator.curate_event_briefing(expo)
    assert briefing.category == EventCategory.EXPO
    out = format_admission_html(briefing)
    assert "🎪 [대입박람회]" in out.splitlines()[0]
    html_len, visible_len = measure_message(out)
    assert html_len <= TG_MAX_HTML_LEN and visible_len <= TG_MAX_VISIBLE_LEN

    # Non-event briefings (weekly To-Do) carry no category badge
    todo = format_admission_html(curator.generate_weekly_todo_briefing(TargetGrade.MIDDLE))
    assert "[대형설명회]" not in todo and "[대입박람회]" not in todo


def test_curator_get_events_by_category():
    db = AdmissionDatabase(db_path=":memory:")
    db.save_event(_future_event("e1", EventCategory.EXPO, 2))
    db.save_event(_future_event("b1", EventCategory.BRIEFING, 1))
    briefings = AICurator().get_events_by_category(EventCategory.EXPO, TargetGrade.HIGH_3_N, TargetRegion.SEOUL, db=db)
    assert [b.briefing_id for b in briefings] == ["brief_e1"]
    assert briefings[0].category == EventCategory.EXPO
    assert AICurator().get_events_by_category(EventCategory.EXPO, db=None) == []


def test_onboarding_region_completion_sends_welcome_todo():
    db = AdmissionDatabase(db_path=":memory:")
    db.upsert_subscriber(ParentUserProfile(chat_id="888", target_grade=TargetGrade.HIGH_3_N))
    mock_channel = MagicMock(spec=TelegramChannel)
    bot = AdmissionInteractiveBot(channel=mock_channel, db=db)

    bot.handle_update(_cb("region:SEOUL"))

    # Summary edit in place + persistent reply keyboard registration message
    summary = mock_channel.edit_message_text.call_args[0][2]
    assert "설정이 완료" in summary
    mock_channel.send_message.assert_called_once()
    assert "reply_markup" in mock_channel.send_message.call_args[1]

    mock_channel.send.assert_called_once()
    args, kwargs = mock_channel.send.call_args
    chat_id, briefing = args
    assert chat_id == "888"
    assert briefing.briefing_type == BriefingType.WEEKLY_TODO
    assert briefing.target_grade == TargetGrade.HIGH_3_N
    assert briefing.silent is True
    markup = kwargs["reply_markup"]
    assert _all_callback_data(markup) == ["adm:hot", "adm:menu"]
    assert db.has_dispatched(briefing.briefing_id, "888") is True

    # Re-selecting region in the same ISO week does not resend the same briefing
    bot.handle_update(_cb("region:ONLINE"))
    mock_channel.send.assert_called_once()


def test_onboarding_welcome_send_failure_is_not_logged():
    db = AdmissionDatabase(db_path=":memory:")
    mock_channel = MagicMock(spec=TelegramChannel)
    mock_channel.send.side_effect = RuntimeError("network down")
    bot = AdmissionInteractiveBot(channel=mock_channel, db=db)

    bot.handle_update(_cb("region:ALL"))

    briefing = AICurator().generate_weekly_todo_briefing(db.get_subscriber("888").target_grade)
    assert db.has_dispatched(briefing.briefing_id, "888") is False


def test_grade_change_offers_todo_preview_button():
    db = AdmissionDatabase(db_path=":memory:")
    mock_channel = MagicMock(spec=TelegramChannel)
    bot = AdmissionInteractiveBot(channel=mock_channel, db=db)

    bot.handle_update(_cb("grade:HIGH_1_2"))

    markup = mock_channel.edit_message_text.call_args[1]["reply_markup"]
    data = _all_callback_data(markup)
    assert "adm:todo" in data
    assert "region:SEOUL" in data


def test_adm_hot_and_todo_callbacks_route():
    db = AdmissionDatabase(db_path=":memory:")
    db.upsert_subscriber(ParentUserProfile(chat_id="888", target_grade=TargetGrade.MIDDLE))
    mock_channel = MagicMock(spec=TelegramChannel)
    bot = AdmissionInteractiveBot(channel=mock_channel, db=db)
    bot._cmd_hot_events = MagicMock()

    bot.handle_update(_cb("adm:hot"))
    bot._cmd_hot_events.assert_called_once_with("888")

    mock_channel.reset_mock()
    bot.handle_update(_cb("adm:todo"))
    mock_channel.answer_callback_query.assert_called_once()
    mock_channel.send.assert_called_once()
    chat_id, briefing = mock_channel.send.call_args[0]
    assert chat_id == "888"
    assert briefing.briefing_type == BriefingType.WEEKLY_TODO
    # Manual preview is not recorded as a scheduled dispatch
    assert db.has_dispatched(briefing.briefing_id, "888") is False


def test_send_reply_markup_overrides_booking_button():
    sent = {}

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def opener(req, timeout=15):
        sent.update(json.loads(req.data.decode("utf-8")))
        return _Resp(b'{"ok": true, "result": {"message_id": 1}}')

    channel = TelegramChannel("tok", opener=opener)
    briefing = AdmissionBriefing(
        briefing_id="b_markup",
        briefing_type=BriefingType.TICKETING_ALERT,
        target_grade=TargetGrade.MIDDLE,
        headline="h",
        summary_3lines=["a", "b", "c"],
        booking_url="https://sdij.com/test",
    )
    custom = {"inline_keyboard": [[{"text": "x", "callback_data": "adm:hot"}]]}
    channel.send("1", briefing, reply_markup=custom)
    assert sent["reply_markup"] == custom

    channel.send("1", briefing)
    assert sent["reply_markup"]["inline_keyboard"][0][0]["url"] == "https://sdij.com/test"
