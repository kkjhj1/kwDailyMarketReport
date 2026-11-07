"""Kids Compass (키즈 나침반) unit + integration tests. All DBs are :memory: ([TG-06])."""
from __future__ import annotations
from datetime import date, datetime

import pytest

from kids_compass.channels.telegram import (
    HOSPITAL_PHONE_WARNING,
    MAX_HTML_CHARS,
    MAX_VISIBLE_CHARS,
    SafeTelegramDispatcher,
    TelegramAPIError,
    TelegramRateLimiter,
    format_cross_dosing_html,
    format_dday_alert_html,
    format_fever_result_html,
    format_hospital_list_html,
    format_weekend_briefing_html,
    measure_message,
)
from kids_compass.collector import (
    SEED_BOOKING_EVENTS,
    SEED_EXPERIENCES,
    SEED_HOSPITALS,
    build_booking_events,
    recommend_weekend_picks,
    seed_all,
)
from kids_compass.fever_engine import (
    DEFAULT_MEDICINES,
    calculate_all,
    calculate_dose,
    check_cross_dosing_safety,
)
from kids_compass.interactive_bot import (
    KidsCompassInteractiveBot,
    build_dday_messages,
    build_weekend_messages,
    cb,
)
from kids_compass.models import AgeGroup, ChildProfile, ExperienceCategory, IngredientType
from kids_compass.storage.db import KidsCompassDatabase

TYL, BRU, MAX = DEFAULT_MEDICINES["TYLENOL_32"], DEFAULT_MEDICINES["BRUFEN_20"], DEFAULT_MEDICINES["MAXIBUFEN_12"]
FIXED_NOW = datetime(2026, 11, 9, 9, 0)  # Monday 09:00


@pytest.fixture
def db():
    d = KidsCompassDatabase(":memory:")
    seed_all(d, FIXED_NOW.date())
    yield d
    d.close()


class RecordingChannel:
    def __init__(self, fail_with: Exception | None = None, fail_times: int = 0):
        self.calls: list[tuple] = []
        self.fail_with, self.fail_times = fail_with, fail_times

    def answer_callback_query(self, callback_query_id, text=""):
        self.calls.append(("answer", callback_query_id))
        return True

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.calls.append(("edit", chat_id, text, reply_markup))
        return True

    def send_message(self, chat_id, text, reply_markup=None, disable_notification=False):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise self.fail_with
        self.calls.append(("send", chat_id, text, reply_markup))
        return 1

    def rendered(self):
        return [c for c in self.calls if c[0] in ("edit", "send")]


def make_bot(db, channel=None):
    return KidsCompassInteractiveBot(channel or RecordingChannel(), db, clock=lambda: FIXED_NOW)


def tap(bot, data, chat_id=7, n=0):
    bot.handle_update({"callback_query": {"id": f"cb{n}", "data": data,
                                          "message": {"message_id": 55, "chat": {"id": chat_id}}}})


# ── fever engine ──────────────────────────────────────────────
def test_dose_11kg_24months_three_ingredients():
    assert calculate_dose(11, 24, TYL).recommended_ml == round(11 * 12.5 / 32, 1) == 4.3
    assert calculate_dose(11, 24, BRU).recommended_ml == round(11 * 7.5 / 20, 1) == 4.1
    assert calculate_dose(11, 24, MAX).recommended_ml == round(11 * 6.0 / 12, 1) == 5.5
    r = calculate_dose(11, 24, TYL)
    assert r.status == "OK" and r.interval_hours == 4 and r.max_times_per_day == 5
    assert r.max_daily_ml == round(min(11 * 75.0, 640.0 * 5) / 32, 1)


def test_single_dose_cap_for_heavy_child():
    r = calculate_dose(60, 144, TYL)
    assert r.recommended_mg == 640.0 and r.recommended_ml == 20.0
    for p in DEFAULT_MEDICINES.values():
        res = calculate_dose(60, 144, p)
        assert res.recommended_mg <= p.max_single_mg
        assert res.max_daily_ml <= round(p.max_single_mg * p.max_times_per_day / p.concentration_mg_ml, 1)


@pytest.mark.parametrize("months", [0, 1, 2, 3])
def test_infant_under_3m_blocks_every_product(months):
    for p in DEFAULT_MEDICINES.values():
        r = calculate_dose(5, months, p)
        assert r.status == "BLOCKED_INFANT_UNDER_3M"
        assert r.recommended_ml == 0.0 and r.recommended_mg == 0.0
        assert "즉시 응급실" in r.warning_message


def test_nsaid_blocked_under_6m_acetaminophen_allowed():
    res = calculate_all(7, 5)
    assert res[IngredientType.ACETAMINOPHEN].status == "OK"
    assert res[IngredientType.ACETAMINOPHEN].recommended_ml > 0
    for ing in (IngredientType.IBUPROFEN, IngredientType.DEXIBUPROFEN):
        assert res[ing].status == "BLOCKED_NSAID_UNDER_6M"
        assert res[ing].recommended_ml == 0.0
    assert calculate_dose(7, 6, BRU).status == "OK"


@pytest.mark.parametrize("weight", [0, -1, 100.1, 250])
def test_invalid_weight(weight):
    r = calculate_dose(weight, 24, TYL)
    assert r.status == "INVALID_WEIGHT" and r.recommended_ml == 0.0


def test_cross_dosing_rules():
    A, I, D = IngredientType.ACETAMINOPHEN, IngredientType.IBUPROFEN, IngredientType.DEXIBUPROFEN
    assert check_cross_dosing_safety(I, D) == "BLOCKED_SAME_NSAID_CLASS"
    assert check_cross_dosing_safety(D, I) == "BLOCKED_SAME_NSAID_CLASS"
    assert check_cross_dosing_safety(A, A) == "SAME_INGREDIENT"
    assert check_cross_dosing_safety(I, I) == "SAME_INGREDIENT"
    assert check_cross_dosing_safety(A, I) == "ALLOWED_WITH_CAUTION"
    assert check_cross_dosing_safety(D, A) == "ALLOWED_WITH_CAUTION"


def test_fever_card_blocked_infant_shows_no_dose():
    html_text = format_fever_result_html(calculate_all(4.5, 2))
    assert "mL" not in html_text and "즉시 응급실" in html_text and "진단·처방이 아닙니다" in html_text


# ── models ────────────────────────────────────────────────────
def test_child_age_months_and_group():
    c = ChildProfile(1, "7", "콩이", "2024-11-08", 9.0)
    assert c.age_months(date(2026, 11, 7)) == 23  # day-of-month not yet reached
    assert c.age_months(date(2026, 11, 8)) == 24
    assert c.age_group(date(2026, 11, 8)) == AgeGroup.INFANT
    assert ChildProfile(2, "7", "a", "2021-01-01", 18).age_group(date(2026, 1, 1)) == AgeGroup.PRESCHOOL
    assert ChildProfile(3, "7", "b", "2019-01-01", 25).age_group(date(2026, 1, 1)) == AgeGroup.ELEMENTARY


# ── seeds & recommendation ────────────────────────────────────
def test_seed_volumes():
    assert len(SEED_EXPERIENCES) >= 12 and len(SEED_BOOKING_EVENTS) >= 5 and len(SEED_HOSPITALS) >= 8
    for ag in (AgeGroup.INFANT, AgeGroup.PRESCHOOL, AgeGroup.ELEMENTARY):
        assert any(e.age_group == ag for e in SEED_EXPERIENCES)


def test_booking_events_never_in_the_past():
    today = date(2026, 12, 31)
    for ev in build_booking_events(today):
        assert ev.open_datetime[:10] >= today.isoformat()


@pytest.mark.parametrize("age", [AgeGroup.INFANT, AgeGroup.PRESCHOOL, AgeGroup.ELEMENTARY, AgeGroup.ALL])
def test_rain_or_dust_picks_are_indoor_only(db, age):
    for kwargs in ({"weather_rain": True}, {"fine_dust_bad": True}):
        picks = recommend_weekend_picks(db, age, "서울", **kwargs)
        assert picks and all(p.is_indoor for p in picks)


def test_sunny_picks_mix_outdoor_and_indoor(db):
    picks = recommend_weekend_picks(db, AgeGroup.PRESCHOOL, "서울")
    assert len(picks) == 3
    assert any(not p.is_indoor for p in picks) and any(p.is_indoor for p in picks)
    assert not picks[0].is_indoor and picks[0].category == ExperienceCategory.NATURE


def test_region_backfill_when_region_is_thin(db):
    picks = recommend_weekend_picks(db, AgeGroup.ELEMENTARY, "인천")
    assert len(picks) == 3


# ── hospital card ─────────────────────────────────────────────
def test_hospital_card_has_phone_warning_and_verified_date(db):
    hospitals = db.list_hospitals("서울")
    text = format_hospital_list_html(hospitals, "서울")
    assert "현재 진료 가능" not in text
    assert "유선" in text and "전화" in text and HOSPITAL_PHONE_WARNING in text
    assert "확인일" in text
    for h in hospitals[:8]:
        assert h.last_verified_at and h.last_verified_at in text


# ── storage ───────────────────────────────────────────────────
def test_schema_tables_indexes_and_fk(db):
    names = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master").fetchall()}
    for t in ("kids_user_profiles", "kids_children", "kids_medicine_products", "kids_dose_events",
              "kids_experiences", "kids_booking_events", "kids_notification_deliveries", "kids_moonlight_hospitals",
              "idx_kids_exp_filter", "idx_kids_hospital_region", "idx_kids_booking_open"):
        assert t in names
    assert db.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_child_ownership_and_dose_log(db):
    cid = db.add_child(7, "콩이", "2024-05-01", 11.0)
    assert db.get_child(cid, 7) is not None
    assert db.get_child(cid, 8) is None
    assert db.delete_child(cid, 8) is False
    db.log_dose_event(cid, "TYLENOL_32", 4.3, 137.5)
    assert db.delete_child(cid, 7) is True
    assert db.conn.execute("SELECT COUNT(*) FROM kids_dose_events").fetchone()[0] == 0  # cascade


# ── dispatch idempotency ──────────────────────────────────────
def _no_sleep_dispatcher():
    return SafeTelegramDispatcher(TelegramRateLimiter(sleep=lambda s: None), sleep=lambda s: None)


def test_weekend_dispatch_is_idempotent_per_week(db):
    db.ensure_user(101)
    db.ensure_user(102)
    db.add_child(101, "콩이", "2021-03-01", 18.0)
    db.set_alert(102, "weekend", False)
    key, messages = build_weekend_messages(db, date(2026, 11, 12))
    assert key == "kids_weekend:2026-W46" and [m[0] for m in messages] == ["101"]
    ch = RecordingChannel()
    s1 = _no_sleep_dispatcher().dispatch(ch, db, key, messages)
    s2 = _no_sleep_dispatcher().dispatch(ch, db, key, messages)
    assert s1["sent"] == 1 and s2["skipped"] == 1 and s2["sent"] == 0
    assert len(ch.rendered()) == 1
    row = db.conn.execute("SELECT COUNT(*) FROM kids_notification_deliveries WHERE dispatch_key = ?", (key,)).fetchone()
    assert row[0] == 1


def test_dday_dispatch_is_idempotent_and_skips_empty_window(db):
    db.ensure_user(201)
    key, messages = build_dday_messages(db, FIXED_NOW)
    assert key == "kids_dday:2026-W46" and len(messages) == 1
    ch = RecordingChannel()
    _no_sleep_dispatcher().dispatch(ch, db, key, messages)
    assert _no_sleep_dispatcher().dispatch(ch, db, key, messages)["skipped"] == 1
    assert len(ch.rendered()) == 1

    empty = KidsCompassDatabase(":memory:")
    empty.ensure_user(201)
    assert build_dday_messages(empty, FIXED_NOW)[1] == []
    empty.close()


def test_dispatch_timeout_is_not_resent_but_failure_is_retried(db):
    ch = RecordingChannel(fail_with=TimeoutError("timed out"), fail_times=1)
    assert _no_sleep_dispatcher().dispatch(ch, db, "k1", [("1", "hi")])["unknown"] == 1
    assert _no_sleep_dispatcher().dispatch(ch, db, "k1", [("1", "hi")])["skipped"] == 1

    ch = RecordingChannel(fail_with=TelegramAPIError("sendMessage", 500, "boom"), fail_times=1)
    assert _no_sleep_dispatcher().dispatch(ch, db, "k2", [("1", "hi")])["failed"] == 1
    assert _no_sleep_dispatcher().dispatch(ch, db, "k2", [("1", "hi")])["sent"] == 1


def test_dispatch_429_backs_off_and_retries(db):
    sleeps = []
    ch = RecordingChannel(fail_with=TelegramAPIError("sendMessage", 429, "Too Many", retry_after=3), fail_times=2)
    d = SafeTelegramDispatcher(TelegramRateLimiter(sleep=lambda s: None), sleep=sleeps.append)
    assert d.dispatch(ch, db, "k3", [("1", "hi")])["sent"] == 1
    assert sleeps == [3, 3]


# ── interactive bot ([TG-04]) ─────────────────────────────────
ALL_CALLBACKS = [
    "k:home", "k:fvr", "k:fvr:m3", "k:fvr:m5:7", "k:fvr:w:11", "k:fvr:x", "k:hsp", "k:hsp:r:SEL",
    "k:hsp:r:GGI", "k:hsp:r:ICN", "k:exp", "k:exp:a:INFANT:sunny", "k:exp:a:PRESCHOOL:rain",
    "k:exp:a:ELEMENTARY:sunny", *[f"k:exp:c:{c.value}" for c in ExperienceCategory], "k:dday", "k:set",
    "k:set:w", "k:set:d", "k:set:r:GGI",
]


def _collect_buttons(channel):
    for c in channel.rendered():
        for row in (c[3] or {}).get("inline_keyboard", []):
            yield from row


def test_every_callback_is_ascii_and_within_64_bytes(db):
    ch = RecordingChannel()
    bot = make_bot(db, ch)
    db.add_child(7, "가나다라마바사아자차", "2024-05-01", 11.0)
    bot.handle_update({"message": {"chat": {"id": 7}, "text": "/start"}})
    for n, data in enumerate(ALL_CALLBACKS):
        tap(bot, data, n=n)
    buttons = list(_collect_buttons(ch))
    assert buttons
    for b in buttons:
        data = b["callback_data"]
        assert data.isascii() and len(data.encode("utf-8")) <= 64, data
    with pytest.raises(ValueError):
        cb("x" * 70)
    with pytest.raises(ValueError):
        cb("hsp", "r", "서울")


@pytest.mark.parametrize("data", ALL_CALLBACKS + ["k:fvr:w:abc", "k:hsp:r:XXX", "garbage", "k:ch:x:999"])
def test_answer_callback_query_is_called_first(db, data):
    ch = RecordingChannel()
    tap(make_bot(db, ch), data)
    assert ch.calls and ch.calls[0] == ("answer", "cb0")


def test_child_callback_uses_child_age_and_owner_scope(db):
    ch = RecordingChannel()
    bot = make_bot(db, ch)
    cid = db.add_child(7, "콩이", "2026-09-01", 5.0)  # 2 months on FIXED_NOW
    tap(bot, f"k:fvr:c:{cid}")
    assert "즉시 응급실" in ch.rendered()[-1][2]
    tap(bot, f"k:fvr:c:{cid}", chat_id=8)  # other user → menu, no data leak
    assert "콩이" not in ch.rendered()[-1][2]


def test_child_delete_is_two_step(db):
    ch = RecordingChannel()
    bot = make_bot(db, ch)
    cid = db.add_child(7, "콩이", "2024-05-01", 11.0)
    tap(bot, f"k:ch:x:{cid}")
    assert db.get_child(cid, 7) is not None and "삭제할까요" in ch.rendered()[-1][2]
    tap(bot, f"k:ch:xy:{cid}")
    assert db.get_child(cid, 7) is None


def test_child_command_validation(db):
    ch = RecordingChannel()
    bot = make_bot(db, ch)
    bot.handle_update({"message": {"chat": {"id": 7}, "text": "/child 콩이 2030-01-01 11"}})
    bot.handle_update({"message": {"chat": {"id": 7}, "text": "/child <b>x</b> 2024-01-01 11"}})
    assert db.list_children(7) == []
    bot.handle_update({"message": {"chat": {"id": 7}, "text": "/child 콩이 2024-05-01 11.2"}})
    assert [c.weight_kg for c in db.list_children(7)] == [11.2]


# ── [TG-01] / [TG-02] ─────────────────────────────────────────
def test_all_rendered_messages_within_limits(db):
    ch = RecordingChannel()
    bot = make_bot(db, ch)
    for i in range(4):
        db.add_child(7, f"아이{i}", "2022-05-01", 12.0)
    bot.handle_update({"message": {"chat": {"id": 7}, "text": "/start"}})
    for n, data in enumerate(ALL_CALLBACKS):
        tap(bot, data, n=n)
    many = build_booking_events(FIXED_NOW.date()) * 3
    texts = [c[2] for c in ch.rendered()] + [
        format_dday_alert_html(many, FIXED_NOW),
        format_hospital_list_html(db.list_hospitals() * 3, "전체"),
        format_cross_dosing_html(),
        format_fever_result_html(calculate_all(100, 200)),
    ]
    for t in texts:
        html_len, visible_len = measure_message(t)
        assert html_len <= MAX_HTML_CHARS and visible_len <= MAX_VISIBLE_CHARS, (html_len, visible_len, t[:80])


def test_dynamic_fields_are_escaped(db):
    exp = SEED_EXPERIENCES[0]
    from dataclasses import replace
    evil = replace(exp, title="<script>x</script>", key_points="a & b", booking_url='https://x/"><b>')
    text = format_weekend_briefing_html([evil], AgeGroup.INFANT, "<i>rain</i>")
    assert "<script>" not in text and "&lt;script&gt;" in text and "a &amp; b" in text
    assert '"><b>' not in text and "&lt;i&gt;rain" in text


# ── portal hub ────────────────────────────────────────────────
def test_portal_hub_registers_kids_alongside_existing_bots():
    from portal_hub.db import PortalDatabase
    from portal_hub.models import BotCategory, BotServiceType

    assert BotServiceType.KIDS.value == "kids_bot"
    for name in ("BRIEF", "ADMISSION", "TRAVEL", "SILVER", "KIDS"):
        assert hasattr(BotServiceType, name)
    kids = BotServiceType.KIDS
    assert kids.title == "🍼 키즈 나침반 (체험 & 응급육아)" and kids.short_name == "키즈 나침반"
    assert kids.category == BotCategory.LIFE and kids.icon == "🍼" and "해열제" in kids.keywords
    assert kids.telegram_handle and not kids.telegram_handle.startswith("@")

    pdb = PortalDatabase(":memory:")
    rows = {r["bot_id"]: r for r in pdb._get_conn().execute("SELECT bot_id, sort_order FROM bots").fetchall()}
    assert rows["kids_bot"]["sort_order"] == 5 and rows["silver_bot"]["sort_order"] == 4
