"""Tests for Admission Intelligence Bot Phase B: 1초 입시 은어·용어 팩트사전 & 카카오톡 원터치 공유."""
import html
from unittest.mock import MagicMock

from admission_brief.channels.telegram import (
    TG_MAX_HTML_LEN,
    TG_MAX_VISIBLE_LEN,
    TelegramChannel,
    build_briefing_share_text,
    build_event_share_text,
    build_glossary_share_text,
    format_kakao_share_html,
    measure_message,
)
from admission_brief.fixtures.glossary import (
    GLOSSARY_ENTRIES,
    GlossaryEntry,
    get_glossary_entry,
    list_glossary_entries,
)
from admission_brief.interactive_bot import (
    CALLBACK_MAX_BYTES,
    GLOSSARY_MENU_TEXT,
    SHARE_TOAST,
    AdmissionInteractiveBot,
    build_glossary_detail_keyboard,
    build_glossary_menu_keyboard,
    format_glossary_entry_html,
    format_glossary_menu_html,
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


def _cb(data: str, chat_id: str = "777", message_id: int = 100) -> dict:
    return {
        "update_id": 9999,
        "callback_query": {
            "id": "cb_test_id",
            "from": {"id": int(chat_id), "first_name": "학부모", "username": "parent_user"},
            "message": {"message_id": message_id, "chat": {"id": int(chat_id)}},
            "data": data,
        },
    }


def _msg(text: str, chat_id: str = "777") -> dict:
    return {
        "update_id": 9998,
        "message": {
            "message_id": 50,
            "chat": {"id": int(chat_id)},
            "from": {"id": int(chat_id), "first_name": "학부모", "username": "parent_user"},
            "text": text,
        },
    }


# ============================================================
# 1. Glossary Seed Data Integrity Tests
# ============================================================


def test_glossary_entries_count_and_uniqueness():
    """15 core admission terms must exist with unique ASCII codes."""
    entries = list_glossary_entries()
    assert len(entries) == 15
    assert len(GLOSSARY_ENTRIES) == 15

    codes = [e.code for e in entries]
    assert len(codes) == len(set(codes)), "Codes must be unique"
    for code in codes:
        assert code.isupper() and code.isascii(), f"Code {code} must be uppercase ASCII"
        assert len(code) <= 12, f"Code {code} must be short for 64-byte callbacks"


def test_glossary_entries_required_fields():
    """All 15 entries must have non-empty definition, impact, and caution fields."""
    expected_codes = {
        "NAPCHI", "BYEONPYO", "PYOJEOM", "BAEK", "CHOECHO",
        "CHUNGWON", "SETEUK", "RECOMMEND", "GRADE5", "GUN",
        "CONVERT", "BOOM", "HAKJONG", "GYOGWA", "NONSUL",
    }
    entries = list_glossary_entries()
    found_codes = {e.code for e in entries}
    assert expected_codes == found_codes

    for e in entries:
        assert e.term.strip(), f"{e.code}: term is empty"
        assert e.short_name.strip() and len(e.short_name) <= 10, f"{e.code}: short_name too long"
        assert e.definition.strip(), f"{e.code}: definition is empty"
        assert e.impact.strip(), f"{e.code}: impact is empty"
        assert e.caution.strip(), f"{e.code}: caution is empty"


def test_get_glossary_entry_case_insensitive():
    """get_glossary_entry resolves codes case-insensitively and returns None for unknown."""
    entry = get_glossary_entry("napchi")
    assert entry is not None
    assert entry.term == "수시납치"

    assert get_glossary_entry("CHOECHO") is not None
    assert get_glossary_entry("unknown_code") is None
    assert get_glossary_entry("") is None


# ============================================================
# 2. Glossary UI & Telegram 7 Rules Compliance
# ============================================================


def test_glossary_menu_keyboard_layout_and_bytes():
    """Glossary menu keyboard has 3 columns and all callbacks <= 64 bytes."""
    kb = build_glossary_menu_keyboard()
    rows = kb["inline_keyboard"]
    # 15 entries in 3 columns = 5 rows, plus 1 nav row
    assert len(rows) == 6
    for row in rows[:5]:
        assert len(row) == 3
        for btn in row:
            cb = btn["callback_data"]
            assert len(cb.encode("utf-8")) <= CALLBACK_MAX_BYTES
            assert cb.startswith("adm:gl:")

    last_row = rows[-1]
    assert len(last_row) == 1
    assert last_row[0]["callback_data"] == "adm:menu"


def test_glossary_detail_keyboard():
    """Detail keyboard provides Kakao share, back to glossary, and main menu."""
    entry = get_glossary_entry("NAPCHI")
    kb = build_glossary_detail_keyboard(entry)
    rows = kb["inline_keyboard"]
    assert len(rows) == 2

    share_btn = rows[0][0]
    assert share_btn["text"] == "📲 카톡 공유용 요약"
    assert share_btn["callback_data"] == "adm:sh:gl:NAPCHI"
    assert len(share_btn["callback_data"].encode("utf-8")) <= CALLBACK_MAX_BYTES

    nav_row = rows[1]
    assert nav_row[0]["callback_data"] == "adm:gl:menu"
    assert nav_row[1]["callback_data"] == "adm:menu"


def test_glossary_html_rendering_and_limits():
    """All glossary cards must satisfy TG_MAX_HTML_LEN and TG_MAX_VISIBLE_LEN."""
    menu_html = format_glossary_menu_html()
    h_len, v_len = measure_message(menu_html)
    assert h_len <= TG_MAX_HTML_LEN and v_len <= TG_MAX_VISIBLE_LEN

    for entry in list_glossary_entries():
        detail_html = format_glossary_entry_html(entry)
        h_len, v_len = measure_message(detail_html)
        assert h_len <= TG_MAX_HTML_LEN, f"{entry.code} HTML too long: {h_len}"
        assert v_len <= TG_MAX_VISIBLE_LEN, f"{entry.code} visible too long: {v_len}"
        assert html.escape(entry.term) in detail_html
        assert "핵심 개념" in detail_html
        assert "학부모 실전 득실" in detail_html
        assert "주의사항 & 꿀팁" in detail_html


# ============================================================
# 3. KakaoTalk One-Touch Share Formatting Tests
# ============================================================


def test_format_kakao_share_html():
    """format_kakao_share_html wraps text in <code> and includes bot handle."""
    plain = "테스트 공유 내용입니다."
    out = format_kakao_share_html(plain)
    assert "<code>" in out and "</code>" in out
    assert "@KwEduInfo_bot" in out
    assert "카카오톡 원터치 복사 카드" in out
    h_len, v_len = measure_message(out)
    assert h_len <= TG_MAX_HTML_LEN
    assert v_len <= TG_MAX_VISIBLE_LEN


def test_build_glossary_share_text():
    """Glossary share text has 3-line breakdown and term header."""
    entry = get_glossary_entry("BYEONPYO")
    share_txt = build_glossary_share_text(entry)
    assert "[에듀브리프 입시사전: 변환표준점수(변표)]" in share_txt
    assert "💡 개념:" in share_txt
    assert "🎯 학부모 득실:" in share_txt
    assert "⚠️ 주의사항:" in share_txt


def test_build_briefing_share_text():
    """Briefing share text serializes weekly To-Do cleanly."""
    briefing = AdmissionBriefing(
        briefing_id="todo_test_1",
        briefing_type=BriefingType.WEEKLY_TODO,
        target_grade=TargetGrade.HIGH_3_N,
        target_region=TargetRegion.SEOUL,
        headline="수능 D-7 파이널 체크",
        summary_3lines=["가채점 기준선 점검", "대학별고사 응시 판단", "정시 환산점수 시뮬레이션"],
        so_what="가채점 성적으로 수시 최저 충족 여부를 확인해야 합니다.",
        action_todos=["가채점 성적표 취합", "논술고사 일정 확인"],
    )
    share_txt = build_briefing_share_text(briefing)
    assert "[에듀브리프" in share_txt
    assert "수능 D-7 파이널 체크" in share_txt
    assert "1. 가채점 기준선 점검" in share_txt
    assert "💡" in share_txt
    assert "✅ 가채점 성적표 취합" in share_txt


def test_build_event_share_text():
    """Event share text formats title, date, organizer, and booking link."""
    event = AdmissionEvent(
        event_id="ev_jongro_1",
        title="종로학원 정시 설명회",
        organizer="종로학원",
        category=EventCategory.BRIEFING,
        event_type="설명회",
        target_grade=TargetGrade.HIGH_3_N,
        target_region=TargetRegion.ONLINE,
        event_date_str="2026-11-22 14:00",
        booking_url="https://www.jongro.co.kr",
        is_online_live=True,
        is_first_come=True,
        so_what="가채점 결과 분석 및 정시 합격선 제공",
    )
    share_txt = build_event_share_text(event)
    assert "[에듀브리프 행사] 종로학원 정시 설명회" in share_txt
    assert "2026-11-22 14:00" in share_txt
    assert "종로학원" in share_txt
    assert "온라인 생중계" in share_txt
    assert "⚡ 선착순 마감" in share_txt
    assert "https://www.jongro.co.kr" in share_txt


# ============================================================
# 4. Interactive Bot Routing & Callback ACK Tests
# ============================================================


def test_bot_glossary_menu_command_and_text():
    """User can open glossary menu via persistent reply text or /glossary."""
    mock_channel = MagicMock(spec=TelegramChannel)
    db = AdmissionDatabase(db_path=":memory:")
    bot = AdmissionInteractiveBot(channel=mock_channel, db=db)

    # 1. Via bottom touch button
    bot.handle_update(_msg(GLOSSARY_MENU_TEXT))
    mock_channel.send_message.assert_called_once()
    args, kwargs = mock_channel.send_message.call_args
    assert "입시 은어·용어 사전" in args[1]
    assert len(kwargs["reply_markup"]["inline_keyboard"]) == 6

    # 2. Via /glossary command
    mock_channel.reset_mock()
    bot.handle_update(_msg("/glossary"))
    mock_channel.send_message.assert_called_once()


def test_bot_glossary_entry_detail_and_back_nav():
    """Clicking a term card edits message in place; navigating back returns to grid."""
    mock_channel = MagicMock(spec=TelegramChannel)
    db = AdmissionDatabase(db_path=":memory:")
    bot = AdmissionInteractiveBot(channel=mock_channel, db=db)

    # 1. Tap term button adm:gl:SETEUK
    bot.handle_update(_cb("adm:gl:SETEUK", message_id=101))
    mock_channel.answer_callback_query.assert_called_once_with("cb_test_id")
    mock_channel.edit_message_text.assert_called_once()
    args, kwargs = mock_channel.edit_message_text.call_args
    assert args[1] == 101
    assert "세부능력 및 특기사항" in args[2]
    assert kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "adm:sh:gl:SETEUK"

    # 2. Tap back to menu adm:gl:menu
    mock_channel.reset_mock()
    bot.handle_update(_cb("adm:gl:menu", message_id=101))
    mock_channel.answer_callback_query.assert_called_once_with("cb_test_id")
    mock_channel.edit_message_text.assert_called_once()
    args, kwargs = mock_channel.edit_message_text.call_args
    assert "입시 은어·용어 사전" in args[2]


def test_bot_kakao_share_callbacks():
    """Kakao share callback answers with toast immediately and sends <code> card."""
    mock_channel = MagicMock(spec=TelegramChannel)
    db = AdmissionDatabase(db_path=":memory:")
    bot = AdmissionInteractiveBot(channel=mock_channel, db=db)

    # 1. Glossary share: adm:sh:gl:NAPCHI
    bot.handle_update(_cb("adm:sh:gl:NAPCHI"))
    mock_channel.answer_callback_query.assert_called_once_with("cb_test_id", text=SHARE_TOAST)
    mock_channel.send_message.assert_called_once()
    args, kwargs = mock_channel.send_message.call_args
    assert args[0] == "777"
    assert "<code>" in args[1] and "수시납치" in args[1]
    assert kwargs.get("disable_notification") is True

    # 2. To-Do share: adm:sh:todo
    mock_channel.reset_mock()
    db.upsert_subscriber(ParentUserProfile(chat_id="777", target_grade=TargetGrade.HIGH_3_N))
    bot.handle_update(_cb("adm:sh:todo"))
    mock_channel.answer_callback_query.assert_called_once_with("cb_test_id", text=SHARE_TOAST)
    mock_channel.send_message.assert_called_once()
    args, kwargs = mock_channel.send_message.call_args
    assert "<code>" in args[1]
    assert "에듀브리프" in args[1]
