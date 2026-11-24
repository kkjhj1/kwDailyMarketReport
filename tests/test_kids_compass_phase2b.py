"""Phase 2-B: 1:1 assistant — onboarding state machine, Pull hub, feedback/milestone callbacks, Silence-Rule push
builders and the button-aware idempotent push dispatcher. All DBs are :memory:, network-free ([TG-06])."""
from __future__ import annotations
from datetime import date, datetime, timedelta

import pytest

from kids_compass.channels.telegram import (
    MAX_HTML_CHARS,
    MAX_VISIBLE_CHARS,
    MILESTONE_DISCLAIMER,
    PUSH_MESSAGE_SENTINEL,
    SafeTelegramDispatcher,
    TelegramAPIError,
    TelegramRateLimiter,
    format_fever_result_html,
    format_home_html,
    format_milestone_push_html,
    format_weekend_push_html,
    measure_message,
)
from kids_compass.collector import SEED_EXPERIENCES, recommend_weekend_picks, seed_all
from kids_compass.fever_engine import calculate_all
from kids_compass.interactive_bot import (
    QUICK_WEIGHTS_4_5M,
    QUICK_WEIGHTS_6M,
    KidsCompassInteractiveBot,
    build_dday_push_messages,
    build_milestone_push_messages,
    build_weekend_push_messages,
    cb,
    offline_weather,
    parse_user_date,
    push_window,
)
from kids_compass.models import AgeGroup, ChildProfile, PushMessage, WeatherSnapshot
from kids_compass.storage.db import KidsCompassDatabase

TODAY = date(2026, 11, 19)              # Thursday
NOW = datetime(2026, 11, 19, 17, 0)
WINDOW = push_window(TODAY)


@pytest.fixture
def db():
    d = KidsCompassDatabase(":memory:")
    seed_all(d, TODAY)
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

    def rendered(self, chat_id=None):
        return [c for c in self.calls if c[0] in ("edit", "send") and (chat_id is None or str(c[1]) == str(chat_id))]

    def last_text(self):
        return self.rendered()[-1][2]

    def last_kb(self):
        return self.rendered()[-1][3]


def make_bot(db, channel=None, now=NOW):
    return KidsCompassInteractiveBot(channel or RecordingChannel(), db, clock=lambda: now)


def tap(bot, data, chat_id=7, n=0):
    bot.handle_update({"callback_query": {"id": f"cb{n}", "data": data,
                                          "message": {"message_id": 55, "chat": {"id": chat_id}}}})


def say(bot, text, chat_id=7):
    bot.handle_update({"message": {"chat": {"id": chat_id}, "text": text}})


def callbacks(kb):
    return [b["callback_data"] for row in (kb or {}).get("inline_keyboard", []) for b in row]


def step(db, chat_id=7):
    st = db.get_onboarding_state(chat_id, NOW)
    return st["step"] if st else None


def _no_sleep_dispatcher():
    return SafeTelegramDispatcher(TelegramRateLimiter(sleep=lambda s: None), sleep=lambda s: None)


def snaps(rain: bool = False, failed: bool = False):
    def fetch(region, day):
        text = "11/21(토) 강수확률 80% · 비/눈 예보 → 실내 위주" if rain else "11/21(토) 강수확률 10% · 비 소식 적음"
        return [WeatherSnapshot(region, "2026-11-21", rain and not failed, False, text, "open-meteo",
                                "2026-11-19 17:00:00", "2026-11-19 20:00:00", is_failed=failed)]
    return fetch


# ── onboarding state machine ─────────────────────────────────
def test_parse_user_date_formats():
    assert parse_user_date("20230501") == date(2023, 5, 1)
    assert parse_user_date("2023-05-01") == date(2023, 5, 1)
    for bad in ("2023/05/01", "230501", "20230230", "2023-5-1", "abc", ""):
        assert parse_user_date(bad) is None


def test_child_onboarding_full_flow_with_retries(db):
    ch = RecordingChannel()
    bot = make_bot(db, ch)
    tap(bot, cb("ob", "c"))
    assert step(db) == "CHILD_NICKNAME" and "별명" in ch.last_text() and "k:ob:x" in callbacks(ch.last_kb())
    say(bot, "<b>x</b>")                                   # invalid → stay
    assert step(db) == "CHILD_NICKNAME" and "⚠️" in ch.last_text()
    say(bot, "콩이")
    assert step(db) == "CHILD_BIRTH_DATE"
    for bad in ("2099-01-01", "20230230", "2010-01-01", "어제"):
        say(bot, bad)
        assert step(db) == "CHILD_BIRTH_DATE" and "⚠️" in ch.last_text(), bad
    say(bot, "20230501")
    assert step(db) == "CHILD_REGION"
    assert {"k:ob:r:SEL", "k:ob:r:GGI", "k:ob:r:ICN", "k:ob:x"} <= set(callbacks(ch.last_kb()))
    say(bot, "부산")                                        # not a supported region → stay
    assert step(db) == "CHILD_REGION"
    assert db.list_children(7) == []
    tap(bot, cb("ob", "r", "GGI"))
    kids = db.list_children(7)
    assert [(c.nickname, c.birth_date, c.weight_kg) for c in kids] == [("콩이", "2023-05-01", None)]
    assert db.get_user_profile(7)["default_region"] == "경기"
    assert step(db) is None
    assert any("등록 완료" in c[2] for c in ch.rendered())
    assert "k:hub:pl" in callbacks(ch.last_kb())             # registered home = Pull hub


def test_pregnancy_onboarding_with_iso_date_and_text_region(db):
    ch = RecordingChannel()
    bot = make_bot(db, ch)
    tap(bot, cb("ob", "p"))
    assert step(db) == "PREGNANCY_NICKNAME"
    say(bot, "복덩이")
    say(bot, "2028-12-01")                                  # too far ahead → stay
    assert step(db) == "PREGNANCY_DUE_DATE"
    say(bot, "2027-03-15")
    assert step(db) == "PREGNANCY_REGION"
    say(bot, "인천")
    p = db.get_pregnancy(7)
    assert p is not None and (p.nickname, p.due_date) == ("복덩이", "2027-03-15")
    assert db.get_user_profile(7)["default_region"] == "인천" and step(db) is None
    assert db.list_children(7) == []                         # pregnancy is never a child


@pytest.mark.parametrize("inputs", [[], ["콩이"], ["콩이", "20230501"]])
def test_cancel_command_at_every_step(db, inputs):
    bot = make_bot(db)
    tap(bot, cb("ob", "c"))
    for text in inputs:
        say(bot, text)
    say(bot, "/cancel")
    assert step(db) is None and db.list_children(7) == []
    say(bot, "콩이")                                        # plain text after cancel → home, not onboarding
    assert step(db) is None and db.list_children(7) == []


@pytest.mark.parametrize("inputs", [[], ["복덩이"], ["복덩이", "20270315"]])
def test_cancel_button_at_every_step(db, inputs):
    ch = RecordingChannel()
    bot = make_bot(db, ch)
    tap(bot, cb("ob", "p"))
    for text in inputs:
        say(bot, text)
    tap(bot, cb("ob", "x"))
    assert step(db) is None and db.get_pregnancy(7) is None
    assert "k:ob:c" in callbacks(ch.last_kb())               # back to the unregistered home


def test_stale_region_button_and_other_commands_are_harmless(db):
    bot = make_bot(db)
    tap(bot, cb("ob", "r", "SEL"))                           # no open flow → no-op
    assert db.list_children(7) == [] and step(db) is None
    tap(bot, cb("ob", "c"))
    say(bot, "콩이")
    tap(bot, cb("ob", "r", "SEL"))                           # wrong step → no-op
    assert db.list_children(7) == []
    say(bot, "/start")                                      # any other command abandons the flow
    assert step(db) is None


def test_onboarding_expires(db):
    bot = make_bot(db)
    tap(bot, cb("ob", "c"))
    later = make_bot(db, now=NOW + timedelta(hours=1))
    say(later, "콩이")
    assert db.get_onboarding_state(7, NOW + timedelta(hours=1)) is None and db.list_children(7) == []


def test_legacy_slash_commands_still_work(db):
    bot = make_bot(db)
    say(bot, "/child 콩이 2024-05-01 11.2")
    say(bot, "/preg 복덩이 2027-03-07")
    assert [c.weight_kg for c in db.list_children(7)] == [11.2] and db.get_pregnancy(7) is not None


# ── home / switcher / pull hub ───────────────────────────────
def test_home_unregistered_shows_register_first_and_emergency(db):
    ch = RecordingChannel()
    say(make_bot(db, ch), "/start")
    rows = ch.last_kb()["inline_keyboard"]
    assert [b["callback_data"] for b in rows[0]] == ["k:ob:c", "k:ob:p"]
    assert {"k:fvr", "k:hsp"} <= set(callbacks(ch.last_kb()))
    assert "5초 간편 등록" in ch.last_text()


def test_home_registered_switcher_and_hub(db):
    ch = RecordingChannel()
    bot = make_bot(db, ch)
    a = db.add_child(7, "첫째", "2021-03-01", 18.0)
    b = db.add_child(7, "둘째", "2024-03-01")
    say(bot, "/start")
    cbs = callbacks(ch.last_kb())
    for expected in ("k:hub:pl", "k:hub:ln", "k:hub:out", "k:hub:med", "k:preg", "k:set",
                     f"k:ch:p:{a}", f"k:ch:p:{b}", "k:hub:out:t"):
        assert expected in cbs
    assert "k:ob:c" not in cbs and "체중 미입력" in ch.last_text()
    tap(bot, f"k:ch:p:{b}")
    assert db.list_children(7)[0].child_id == b
    other = db.add_child(8, "남의집", "2022-03-01", 13.0)
    tap(bot, f"k:ch:p:{other}")                              # another user's child → no change
    assert db.list_children(7)[0].child_id == b and db.get_child(other, 8).is_primary


def test_hub_applies_selected_child_and_region(db):
    ch = RecordingChannel()
    bot = make_bot(db, ch)
    db.add_child(7, "첫째", "2017-03-01", 30.0)              # 초등
    db.add_child(7, "둘째", "2024-03-01", 12.0)              # 영유아
    db.set_default_region(7, "경기")
    tap(bot, "k:hub:ln")
    text = ch.last_text()
    assert "첫째" in text and "📍 경기" in text and "📍 서울" not in text
    tap(bot, "k:hub:out:t")
    text = ch.last_text()
    assert "첫째·둘째 함께" in text and "🏠실내" not in text
    tap(bot, "k:hub:med")
    assert "k:ms:l" in callbacks(ch.last_kb())


# ── fever with missing weight ────────────────────────────────
def test_fever_child_without_weight_asks_weight_by_age(db):
    ch = RecordingChannel()
    bot = make_bot(db, ch)
    older = db.add_child(7, "큰애", "2026-03-01")             # 8 months
    mid = db.add_child(7, "중간", "2026-06-10")               # 5 months
    baby = db.add_child(7, "아기", "2026-09-01")              # 2 months
    tap(bot, "k:fvr")
    assert "체중 선택" in str(ch.last_kb())
    tap(bot, f"k:fvr:c:{older}")
    assert callbacks(ch.last_kb())[:len(QUICK_WEIGHTS_6M)] == [f"k:fvr:cw:{older}:{kg}" for kg in QUICK_WEIGHTS_6M]
    tap(bot, f"k:fvr:c:{mid}")
    assert callbacks(ch.last_kb())[:len(QUICK_WEIGHTS_4_5M)] == [f"k:fvr:cw:{mid}:{kg}" for kg in QUICK_WEIGHTS_4_5M]
    assert "4~5개월" in ch.last_text()
    tap(bot, f"k:fvr:c:{baby}")
    assert "즉시 응급실" in ch.last_text() and "mL" not in ch.last_text()
    tap(bot, f"k:fvr:cw:{older}:9")
    assert "큰애" in ch.last_text() and "9kg" in ch.last_text() and "mL" in ch.last_text()
    tap(bot, f"k:fvr:cw:{mid}:7")
    assert "이부프로펜" in ch.last_text() and "계산 차단" in ch.last_text()   # NSAID blocked under 6m
    tap(bot, f"k:fvr:cw:{older}:9", chat_id=8)               # other user → menu, no leak
    assert "큰애" not in ch.last_text()
    tap(bot, "k:set")
    assert "체중 미입력" in ch.last_text()


def test_formatters_never_crash_on_missing_weight():
    child = ChildProfile(1, "7", "콩이", "2024-05-01", None)
    assert "체중 미입력" in format_home_html([child], {"default_region": "서울"}, None, TODAY)
    assert "체중 미입력" in format_fever_result_html({}, child)
    assert "11kg" in format_fever_result_html(calculate_all(11, 24), child)


# ── settings ─────────────────────────────────────────────────
def test_settings_toggles(db):
    ch = RecordingChannel()
    bot = make_bot(db, ch)
    db.ensure_user(7)
    tap(bot, "k:set:h")
    tap(bot, "k:set:m")
    prof = db.get_user_profile(7)
    assert prof["alert_health_dday"] == 0 and prof["alert_weekend_mode"] == "PRIMARY"
    tap(bot, "k:set:m")
    tap(bot, "k:set:w")
    tap(bot, "k:set:d")
    tap(bot, "k:set:r:ICN")
    prof = db.get_user_profile(7)
    assert prof["alert_weekend_mode"] == "TOGETHER" and prof["alert_weekend_brief"] == 0
    assert prof["alert_dday_reserve"] == 0 and prof["default_region"] == "인천"
    assert {"k:set:h", "k:set:w", "k:set:d", "k:set:m", "k:ob:c"} <= set(callbacks(ch.last_kb()))


# ── weekend push (Silence Rule) ──────────────────────────────
def test_weekend_push_silence_for_unregistered_and_alert_off(db):
    db.ensure_user(1)                                        # 미등록
    db.add_child(2, "콩이", "2021-03-01", 18.0)
    db.set_alert(2, "weekend", False)                        # 알림 OFF
    db.add_child(3, "별이", "2021-03-01", 18.0)
    msgs = build_weekend_push_messages(db, TODAY, weather_fetcher=snaps(), now=NOW)
    assert [m.chat_id for m in msgs] == ["3"]


def test_weekend_push_silence_when_no_candidates(db):
    db.add_child(3, "별이", "2021-03-01", 18.0)
    for e in SEED_EXPERIENCES:
        db.record_place_feedback(3, e.exp_id, "NOT_INTERESTED", NOW)
    assert build_weekend_push_messages(db, TODAY, weather_fetcher=snaps(), now=NOW) == []


@pytest.mark.parametrize("region", ["서울", "경기", "인천"])
def test_weekend_push_never_backfills_other_regions(db, region):
    for uid, birth in ((1, "2025-03-01"), (2, "2021-03-01"), (3, "2017-03-01")):
        db.add_child(uid, "아이", birth, 12.0)
        db.set_default_region(uid, region)
    db.set_pregnancy(4, "복덩이", "2027-03-07")
    db.set_default_region(4, region)
    for fetcher in (snaps(), snaps(rain=True), snaps(failed=True)):
        for m in build_weekend_push_messages(db, TODAY, weather_fetcher=fetcher, now=NOW):
            assert 1 <= len(m.entity_ids) <= 2
            assert all(db.get_experience(e).region_sido == region for e in m.entity_ids), (region, m.entity_ids)


def test_weekend_push_weather_rules(db):
    db.add_child(3, "별이", "2017-03-01", 30.0)              # 초등 · 서울
    calls = []

    def counting(region, day):
        calls.append(region)
        return snaps(rain=True)(region, day)

    db.add_child(4, "달이", "2017-03-01", 30.0)
    rain = build_weekend_push_messages(db, TODAY, weather_fetcher=counting, now=NOW)
    assert calls == ["서울"]                                  # one fetch per region
    for m in rain:
        assert "날씨 반영" in m.text and "실내 위주" in m.text
        assert all(db.get_experience(e).is_indoor for e in m.entity_ids)

    d2 = KidsCompassDatabase(":memory:")
    seed_all(d2, TODAY)
    d2.add_child(3, "별이", "2017-03-01", 30.0)
    failed = build_weekend_push_messages(d2, TODAY, weather_fetcher=snaps(failed=True), now=NOW)
    offline = build_weekend_push_messages(d2, TODAY + timedelta(days=7), weather_fetcher=offline_weather, now=NOW)

    def boom(region, day):
        raise OSError("down")

    d3 = KidsCompassDatabase(":memory:")
    seed_all(d3, TODAY)
    d3.add_child(3, "별이", "2017-03-01", 30.0)
    crashed = build_weekend_push_messages(d3, TODAY, weather_fetcher=boom, now=NOW)
    for m in failed + offline + crashed:
        assert "날씨" not in m.text and "강수확률" not in m.text and "맑" not in m.text
        assert all(d2.get_experience(e).is_indoor for e in m.entity_ids)
    assert failed and crashed

    sunny = build_weekend_push_messages(d3, TODAY + timedelta(days=7), weather_fetcher=snaps(), now=NOW)
    assert sunny and "날씨 반영" in sunny[0].text
    d2.close()
    d3.close()


def test_weekend_push_multi_child_is_one_message(db):
    big = db.add_child(5, "형", "2017-03-01", 30.0)           # 초등
    db.add_child(5, "동생", "2021-03-01", 18.0)               # 4~7세
    msgs = build_weekend_push_messages(db, TODAY, weather_fetcher=snaps(), now=NOW)
    assert len(msgs) == 1 and len(msgs[0].entity_ids) == 2 and "형·동생 함께" in msgs[0].text
    ages = {db.get_experience(e).age_group for e in msgs[0].entity_ids}
    assert AgeGroup.ELEMENTARY in ages or AgeGroup.ALL in ages
    assert ages & {AgeGroup.PRESCHOOL, AgeGroup.ALL}

    d2 = KidsCompassDatabase(":memory:")
    seed_all(d2, TODAY)
    d2.add_child(5, "형", "2017-03-01", 30.0)
    d2.add_child(5, "동생", "2021-03-01", 18.0)
    d2.set_weekend_mode(5, "PRIMARY")
    (m,) = build_weekend_push_messages(d2, TODAY, weather_fetcher=snaps(), now=NOW)
    assert "형 ·" in m.text and "함께" not in m.text
    assert all(d2.get_experience(e).age_group in (AgeGroup.ELEMENTARY, AgeGroup.ALL) for e in m.entity_ids)
    d2.close()
    assert big


def test_weekend_push_buttons_and_feedback_callbacks(db):
    db.add_child(7, "별이", "2017-03-01", 30.0)
    (m,) = build_weekend_push_messages(db, TODAY, weather_fetcher=snaps(), now=NOW)
    rows = m.reply_markup["inline_keyboard"]
    assert [b["text"] for b in rows[0]] == ["1 가봄", "1 관심없음"]
    assert [b["text"] for b in rows[1]] == ["2 가봄", "2 관심없음"]
    visit, nope = rows[0][0]["callback_data"], rows[1][1]["callback_data"]
    assert visit.startswith("k:fb:v:") and nope.startswith("k:fb:x:")
    for data in callbacks(m.reply_markup):
        assert data.isascii() and len(data.encode()) <= 64
    first, second = m.entity_ids

    ch = RecordingChannel()
    other = make_bot(db, ch)
    tap(other, visit, chat_id=8)                             # another user's token → harmless no-op
    assert ch.calls == [("answer", "cb0")]
    assert db.get_excluded_exp_ids(8, TODAY) == set()
    expired = make_bot(db, ch, now=NOW + timedelta(days=15))
    tap(expired, visit, n=1)                                 # expired → no-op
    tap(expired, "k:fb:v:zzzz", n=2)                          # forged → no-op
    tap(expired, "k:fb:q:abc", n=3)                           # unknown action → no-op
    assert ch.rendered() == [] and db.get_excluded_exp_ids(7, TODAY) == set()

    bot = make_bot(db, ch)
    tap(bot, visit, n=4)
    tap(bot, nope, n=5)
    assert db.get_excluded_exp_ids(7, TODAY) == {first, second}
    assert db.get_excluded_exp_ids(7, TODAY + timedelta(days=30)) == {first, second}   # VISITED 180d, NOT_INTERESTED 3650d
    assert "180일간" in ch.rendered()[0][2] and "다시 추천하지" in ch.rendered()[1][2]
    # next week never re-suggests them
    nxt = build_weekend_push_messages(db, TODAY + timedelta(days=7), weather_fetcher=snaps(), now=NOW)
    assert all(not ({first, second} & set(x.entity_ids)) for x in nxt)


# ── milestone push ───────────────────────────────────────────
def test_milestone_push_one_message_per_user_with_confirm(db):
    cid = db.add_child(7, "콩이", "2026-07-10", 7.0)         # 4 months on TODAY
    db.set_pregnancy(7, "둘째", "2027-03-07")
    db.add_child(8, "남의집", "2026-07-10", 7.0)
    db.set_alert(8, "health", False)                         # alert OFF → silence
    db.ensure_user(9)                                        # 미등록 → silence
    msgs = build_milestone_push_messages(db, TODAY)
    assert [m.chat_id for m in msgs] == ["7"]
    m = msgs[0]
    assert "영유아 건강검진 2차" in m.text and "4개월 접종" in m.text and "임신" in m.text
    assert MILESTONE_DISCLAIMER in m.text and m.stream == "milestone" and m.entity_type == "MILESTONE"
    ok_buttons = [d for d in callbacks(m.reply_markup) if d.startswith("k:ms:ok:")]
    assert len(ok_buttons) == len(m.entity_ids) >= 3
    html_len, visible_len = measure_message(m.text)
    assert html_len <= MAX_HTML_CHARS and visible_len <= MAX_VISIBLE_CHARS

    mid = int(ok_buttons[0].rsplit(":", 1)[1])
    ch = RecordingChannel()
    tap(make_bot(db, ch), ok_buttons[0], chat_id=8)          # other user → no-op
    assert db.get_subject_milestone(7, mid).status == "DUE" and ch.rendered() == []
    tap(make_bot(db, ch), ok_buttons[0])
    assert db.get_subject_milestone(7, mid).status == "USER_CONFIRMED"
    assert "확인 완료" in ch.last_text()
    tap(make_bot(db, ch), "k:ms:ok:999999")                  # unknown id → no-op
    assert len(ch.rendered()) == 1

    # deleted child's milestones are never pushed (the pregnancy item still is)
    db.delete_child(cid, 7)
    assert all("콩이" not in x.text for x in build_milestone_push_messages(db, TODAY))
    gone = db.add_child(10, "삭제될아이", "2026-07-10", 7.0)
    db.set_pregnancy(10, "셋째", "2027-03-07")
    assert "삭제될아이" in build_milestone_push_messages(db, TODAY)[-1].text
    db.delete_child(gone, 10)
    (left,) = [x for x in build_milestone_push_messages(db, TODAY) if x.chat_id == "10"]
    assert "삭제될아이" not in left.text and "셋째" in left.text


def test_milestone_push_dispatch_is_idempotent_and_never_repeats(db):
    db.add_child(7, "콩이", "2026-07-10", 7.0)
    msgs = build_milestone_push_messages(db, TODAY)
    ch = RecordingChannel()
    stats = _no_sleep_dispatcher().dispatch_push_messages(ch, db, msgs, WINDOW)
    assert stats["sent"] == 1 and ch.rendered()[0][3] == msgs[0].reply_markup
    assert build_milestone_push_messages(db, TODAY) == []                       # same week: claimed
    assert build_milestone_push_messages(db, TODAY + timedelta(days=7)) == []   # already-sent items never repeat


def test_milestone_pull_list(db):
    db.add_child(7, "콩이", "2026-07-10", 7.0)
    ch = RecordingChannel()
    tap(make_bot(db, ch), "k:ms:l")
    assert "영유아 건강검진 2차" in ch.last_text() and any(d.startswith("k:ms:ok:") for d in callbacks(ch.last_kb()))
    db2 = KidsCompassDatabase(":memory:")
    seed_all(db2, TODAY)
    tap(make_bot(db2, ch), "k:ms:l")
    assert "없어요" in ch.last_text()
    db2.close()


# ── D-Day push ───────────────────────────────────────────────
def test_dday_push_matches_age_and_region_only(db):
    now = datetime(2026, 11, 19, 9, 0)
    db.add_child(1, "유치원생", "2021-03-01", 18.0)           # 4~7세 · 서울
    db.add_child(2, "초등", "2017-03-01", 30.0)               # 초등 · 경기
    db.set_default_region(2, "경기")
    db.add_child(3, "아기", "2025-03-01", 10.0)               # 영유아 · 인천 → nothing
    db.set_default_region(3, "인천")
    db.ensure_user(4)                                        # 미등록
    msgs = {m.chat_id: m for m in build_dday_push_messages(db, now)}
    assert set(msgs) == {"1", "2"}
    assert len(msgs["1"].entity_ids) == 2
    for eid in msgs["1"].entity_ids:
        exp = db.get_experience(next(e.exp_id for e in db.list_upcoming_bookings("2000", "2100")
                                     if f"{e.booking_id}@{e.open_datetime}" == eid))
        assert exp.region_sido == "서울" and exp.age_group in (AgeGroup.PRESCHOOL, AgeGroup.ALL)
    assert "국립과천과학관 천체투영관" in msgs["2"].text and len(msgs["2"].entity_ids) == 1

    stats = _no_sleep_dispatcher().dispatch_push_messages(RecordingChannel(), db, list(msgs.values()), WINDOW)
    assert stats["sent"] == 2
    later = datetime(2026, 11, 26, 9, 0)
    nxt = {m.chat_id: m for m in build_dday_push_messages(db, later)}
    sent_before = set(msgs["1"].entity_ids)
    assert "1" in nxt and not (set(nxt["1"].entity_ids) & sent_before)        # already-sent occurrence excluded
    assert "2" not in nxt                                                      # its only booking was sent


# ── dispatcher ───────────────────────────────────────────────
KB = {"inline_keyboard": [[{"text": "1 가봄", "callback_data": "k:fb:v:abc"}]]}


def _push(chat="7", text="hi", ids=("a", "b"), stream="weekend", etype="EXPERIENCE"):
    return PushMessage(chat, text, reply_markup=KB, stream=stream, entity_type=etype, entity_ids=list(ids))


def test_dispatcher_sends_buttons_and_is_idempotent(db):
    ch = RecordingChannel()
    s1 = _no_sleep_dispatcher().dispatch_push_messages(ch, db, [_push()], WINDOW)
    assert s1["sent"] == 1 and ch.rendered() == [("send", "7", "hi", KB)]
    for eid in (PUSH_MESSAGE_SENTINEL, "a", "b"):
        assert db.get_sent_status("7", "weekend", "EXPERIENCE", eid, WINDOW) == "SENT"
    s2 = _no_sleep_dispatcher().dispatch_push_messages(ch, db, [_push(ids=("c",))], WINDOW)
    assert s2["skipped"] == 1 and len(ch.rendered()) == 1   # one push per (user, stream, window)
    s3 = _no_sleep_dispatcher().dispatch_push_messages(ch, db, [_push()], "2026-W48")
    assert s3["sent"] == 1


def test_dispatcher_unknown_is_never_resent(db):
    ch = RecordingChannel(fail_with=TimeoutError("timed out"), fail_times=1)
    assert _no_sleep_dispatcher().dispatch_push_messages(ch, db, [_push()], WINDOW)["unknown"] == 1
    assert db.get_sent_status("7", "weekend", "EXPERIENCE", PUSH_MESSAGE_SENTINEL, WINDOW) == "UNKNOWN"
    s = _no_sleep_dispatcher().dispatch_push_messages(ch, db, [_push()], WINDOW)
    assert s["skipped"] == 1 and ch.rendered() == []
    assert "a" in db.get_excluded_exp_ids("7", TODAY)        # possibly delivered → still counts for cooldown


def test_dispatcher_failed_is_retried_and_blocked_is_not(db):
    ch = RecordingChannel(fail_with=TelegramAPIError("sendMessage", 500, "boom"), fail_times=1)
    assert _no_sleep_dispatcher().dispatch_push_messages(ch, db, [_push()], WINDOW)["failed"] == 1
    assert _no_sleep_dispatcher().dispatch_push_messages(ch, db, [_push()], WINDOW)["sent"] == 1

    ch = RecordingChannel(fail_with=TelegramAPIError("sendMessage", 403, "blocked"), fail_times=1)
    assert _no_sleep_dispatcher().dispatch_push_messages(ch, db, [_push(chat="8")], WINDOW)["blocked"] == 1
    assert _no_sleep_dispatcher().dispatch_push_messages(ch, db, [_push(chat="8")], WINDOW)["skipped"] == 1


def test_dispatcher_in_flight_and_oversize_are_skipped(db):
    db.claim_sent_history("7", "weekend", "EXPERIENCE", PUSH_MESSAGE_SENTINEL, WINDOW)   # crashed mid-send
    ch = RecordingChannel()
    assert _no_sleep_dispatcher().dispatch_push_messages(ch, db, [_push()], WINDOW)["skipped"] == 1
    s = _no_sleep_dispatcher().dispatch_push_messages(ch, db, [_push(chat="9", text="가" * 2000)], WINDOW)
    assert s["invalid"] == 1 and ch.rendered() == []
    assert db.get_sent_status("9", "weekend", "EXPERIENCE", PUSH_MESSAGE_SENTINEL, WINDOW) is None
    s = _no_sleep_dispatcher().dispatch_push_messages(ch, db, [_push(chat="9", etype="PLACE")], WINDOW)
    assert s["invalid"] == 1


def test_dispatcher_429_backoff_with_buttons(db):
    sleeps = []
    ch = RecordingChannel(fail_with=TelegramAPIError("sendMessage", 429, "Too Many", retry_after=2), fail_times=1)
    d = SafeTelegramDispatcher(TelegramRateLimiter(sleep=lambda s: None), sleep=sleeps.append)
    assert d.dispatch_push_messages(ch, db, [_push()], WINDOW)["sent"] == 1
    assert sleeps == [2] and ch.rendered()[0][3] == KB


# ── formatters / [TG-01] [TG-02] [TG-04] ─────────────────────
def test_weekend_push_card_weather_wording_and_escape(db):
    picks = recommend_weekend_picks(db, AgeGroup.PRESCHOOL, "서울", limit=2)
    for w in (None, WeatherSnapshot("서울", "2026-11-21", False, False, "x", "p", "", "", is_failed=True)):
        text = format_weekend_push_html(picks, "콩이", w, "서울")
        assert "날씨" not in text and "맑" not in text
    evil = WeatherSnapshot("서울", "2026-11-21", True, False, "<b>비</b> & 바람", "p", "", "")
    text = format_weekend_push_html(picks, "<i>콩</i>", evil, "서울")
    assert "&lt;b&gt;비&lt;/b&gt; &amp; 바람" in text and "&lt;i&gt;콩" in text and "<i>콩" not in text
    assert format_weekend_push_html(picks * 3, "콩이", None, "서울").count("가봄") == 0
    assert format_weekend_push_html(picks * 3, "콩이", None, "서울").count("<b>3.") == 0   # max 2 places


def test_milestone_card_limits_and_escape():
    items = [{"milestone_id": i, "subject": "<b>콩</b>" + "가" * 8, "kind": "VACCINE", "title": "x" * 60,
              "description": "y" * 80, "due_from": "2026-11-10", "due_to": "2026-12-09"} for i in range(40)]
    text = format_milestone_push_html(items, TODAY)
    html_len, visible_len = measure_message(text)
    assert html_len <= MAX_HTML_CHARS and visible_len <= MAX_VISIBLE_CHARS
    assert "<b>콩</b>" not in text and "&lt;b&gt;콩" in text and "그 외 34건" in text and "D-20" in text
    long_window = [{"milestone_id": 1, "subject": "콩이", "kind": "VACCINE", "title": "만 4~6세 추가접종",
                    "description": "", "due_from": "2025-11-20", "due_to": "2028-11-19"}]
    text = format_milestone_push_html(long_window, TODAY)
    assert "2025.11/20" in text and "2028.11/19" in text and "D-7" not in text


NEW_CALLBACKS = [
    "k:ob:c", "k:ob:p", "k:ob:x", "k:ob:r:SEL", "k:ob:r:XXX", "k:hub:pl", "k:hub:ln", "k:hub:out", "k:hub:out:t",
    "k:hub:med", "k:hub:zzz", "k:ms:l", "k:ms:ok:1", "k:ms:ok:abc", "k:fb:v:abc", "k:fb:x:abc", "k:fb",
    "k:set:h", "k:set:m", "k:ch:p:1", "k:ch:p:999", "k:fvr:cw:1:9", "k:fvr:cw:1:500", "k:fvr:c:2",
]


@pytest.mark.parametrize("data", NEW_CALLBACKS)
def test_new_callbacks_answer_first(db, data):
    db.add_child(7, "콩이", "2024-05-01")
    db.add_child(7, "별이", "2021-05-01", 18.0)
    ch = RecordingChannel()
    tap(make_bot(db, ch), data)
    assert ch.calls and ch.calls[0] == ("answer", "cb0")


def test_all_new_screens_within_limits_and_64_bytes(db):
    ch = RecordingChannel()
    bot = make_bot(db, ch)
    for i in range(4):
        db.add_child(7, "가나다라마바사아자" + str(i), "2022-05-01" if i % 2 else "2026-07-10")
    db.set_pregnancy(7, "가나다라마바사아자차", "2027-03-07")
    say(bot, "/start")
    for n, data in enumerate(NEW_CALLBACKS + ["k:home", "k:set", "k:fvr"]):
        tap(bot, data, n=n)
    say(bot, "/start", chat_id=99)
    texts = [c[2] for c in ch.rendered()]
    pushes = (build_milestone_push_messages(db, TODAY)
              + build_weekend_push_messages(db, TODAY, weather_fetcher=snaps(rain=True), now=NOW)
              + build_dday_push_messages(db, NOW))
    assert pushes
    texts += [m.text for m in pushes]
    for t in texts:
        html_len, visible_len = measure_message(t)
        assert html_len <= MAX_HTML_CHARS and visible_len <= MAX_VISIBLE_CHARS, (html_len, visible_len, t[:60])
    kbs = [c[3] for c in ch.rendered()] + [m.reply_markup for m in pushes]
    for data in (d for kb in kbs for d in callbacks(kb)):
        assert data.isascii() and len(data.encode("utf-8")) <= 64, data
    with pytest.raises(ValueError):
        cb("fb", "v", "x" * 60)


# ── MultiBotManager routing (conversational onboarding) ───────
def _kids_mgr(tmp_path):
    from unittest.mock import MagicMock

    from market_brief.multi_bot_manager import DynamicBotManager

    engine = MagicMock()
    engine.db.db_path = str(tmp_path / "alerts.sqlite3")
    return DynamicBotManager(engine, router=MagicMock()), tmp_path / "kids_compass.sqlite3"


def _kmsg(text, chat_id=7):
    return {"update_id": 1, "message": {"message_id": 1, "chat": {"id": chat_id, "type": "private"}, "text": text}}


def _route(mgr, text, chat_id=7):
    from unittest.mock import patch

    with patch("kids_compass.collector.seed_all"), \
         patch("kids_compass.interactive_bot.KidsCompassInteractiveBot.handle_update") as handle:
        result = mgr._dispatch_kids_bot_update("1:A", _kmsg(text, chat_id))
    return result, handle.call_count


@pytest.mark.parametrize("cmd", ["/preg", "/cancel", "/settings", "/fever@KidsBot", "/help"])
def test_manager_routes_new_kids_commands(tmp_path, cmd):
    mgr, _ = _kids_mgr(tmp_path)
    assert _route(mgr, cmd) == ((True, "kids_message_handled"), 1)


def test_manager_does_not_intercept_start(tmp_path):
    mgr, _ = _kids_mgr(tmp_path)
    assert _route(mgr, "/start") == ((False, "not_kids_interactive_event"), 0)


def test_manager_routes_onboarding_text_only_while_flow_open(tmp_path):
    mgr, kc_path = _kids_mgr(tmp_path)
    # no open flow -> plain text is not claimed
    assert _route(mgr, "콩이") == ((False, "not_kids_interactive_event"), 0)

    kc = KidsCompassDatabase(str(kc_path))
    try:
        kc.set_onboarding_state(7, "f1", "child", "nickname", {}, datetime.now() + timedelta(minutes=30))
    finally:
        kc.close()
    assert _route(mgr, "콩이") == ((True, "kids_onboarding_message_handled"), 1)
    # other users / other slash commands are unaffected
    assert _route(mgr, "콩이", chat_id=8) == ((False, "not_kids_interactive_event"), 0)
    assert _route(mgr, "/start") == ((False, "not_kids_interactive_event"), 0)
    assert _route(mgr, "/cancel") == ((True, "kids_message_handled"), 1)


def test_manager_onboarding_routing_memory_db_is_safe():
    from unittest.mock import MagicMock

    from market_brief.multi_bot_manager import DynamicBotManager

    engine = MagicMock()
    engine.db.db_path = ":memory:"
    mgr = DynamicBotManager(engine, router=MagicMock())
    assert _route(mgr, "콩이") == ((False, "not_kids_interactive_event"), 0)
    assert _route(mgr, "/preg") == ((True, "kids_message_handled"), 1)
