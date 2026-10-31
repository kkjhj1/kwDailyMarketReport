"""Unit tests for Admission Telegram Bot transport and interactive handlers."""
import io
import json
from unittest.mock import MagicMock
from admission_brief.ai_curator import AICurator
from admission_brief.channels.telegram import TelegramChannel, format_admission_html
from admission_brief.interactive_bot import (
    AdmissionInteractiveBot,
    build_grade_inline_keyboard,
    build_persistent_reply_keyboard,
)
from admission_brief.models import (
    AdmissionBriefing,
    BriefingType,
    ParentUserProfile,
    TargetGrade,
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
    assert "📢 설명회 3줄 요약" in all_btn_texts
    assert "🎓 학년 변경" in all_btn_texts
    assert "📍 지역/온라인 설정" in all_btn_texts


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

    # 2. Tap "📢 설명회 3줄 요약"
    bot.handle_update({
        "update_id": 202,
        "message": {"message_id": 11, "chat": {"id": 777}, "text": "📢 설명회 3줄 요약"},
    })
    assert mock_channel.send.call_count == 2
