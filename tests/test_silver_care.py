"""Unit tests for SilverCare (실버케어): age/region matching, A/B/C grades, TG-01/02/04 rules,
storage, wizard flow, weekly idempotent dispatch. All DBs are ':memory:' ([TG-06])."""
from __future__ import annotations
import io
import json
import urllib.error
from datetime import date

import pytest

from silver_care.channels.telegram import (
    SafeTelegramDispatcher,
    SilverTelegramChannel,
    TelegramAPIError,
    TelegramRateLimiter,
)
from silver_care.channels.telegram_card import (
    TG_MAX_HTML_LEN,
    TG_MAX_VISIBLE_LEN,
    format_benefit_card,
    format_candidate_list,
    format_kakao_share_html,
    format_kakao_share_text,
    format_main_menu,
    format_phone_script,
    format_weekly_briefing,
    measure_message,
)
from silver_care.fixtures.seed_benefits import SEED_BENEFITS
from silver_care.interactive import (
    MAX_CALLBACK_BYTES,
    SilverCareInteractiveBot,
    build_detail_keyboard,
    build_list_keyboard,
    build_main_menu_keyboard,
    build_wizard_keyboard,
    cb,
)
from silver_care.matching import evaluate_benefit, match_benefits
from silver_care.models import (
    ParentProfile,
    ReliabilityGrade,
    SilverBenefit,
    normalize_sido_code,
)
from silver_care.storage.db import SilverCareDatabase
from silver_care.weekly import build_weekly_messages, rotate_matches, weekly_dispatch_key

YEAR = 2026
BY_ID = {b.id: b for b in SEED_BENEFITS}


def _parent(**kw) -> ParentProfile:
    base = dict(id=1, chat_id=100, alias="엄마", birth_year=1954, sido_code="51", sigungu_name="원주시",
                living_type="ALONE", is_pension_receiver=-1, is_low_income=-1)
    base.update(kw)
    return ParentProfile(**base)


def _benefit(**kw) -> SilverBenefit:
    base = dict(id=999, title="테스트", jurisdiction_type="NATIONAL", category="MEDICAL", min_age=65,
                target_income="ALL", target_living="ANY", support_amount="1만원",
                is_pre_approval_required=False, requires_prescription=False, region_code="ALL",
                agency_name="주민센터", application_period="상시", detail_guide="안내",
                phone_script="문의", parent_msg_template="{alias}, 안내")
    base.update(kw)
    return SilverBenefit(**base)


def _all_callbacks(markup: dict) -> list[str]:
    return [b["callback_data"] for row in markup["inline_keyboard"] for b in row if "callback_data" in b]


@pytest.fixture
def db():
    d = SilverCareDatabase(":memory:")
    d.upsert_benefits(SEED_BENEFITS)
    yield d
    d.close()


# ── Seed data facts ──────────────────────────────────────────
class TestSeedFacts:
    def test_seed_count_and_unique_ids(self):
        assert len(SEED_BENEFITS) >= 30
        assert len(BY_ID) == len(SEED_BENEFITS)

    def test_gpt_audited_figures(self):
        assert BY_ID[1].min_age == 65 and "2개" in BY_ID[1].support_amount and "30%" in BY_ID[1].support_amount
        assert BY_ID[2].min_age == 65 and "7년" in BY_ID[2].support_amount  # 틀니 65세 (70세 아님)
        knee = BY_ID[5]
        assert knee.min_age == 60 and knee.is_pre_approval_required and knee.target_income == "LOW_INCOME"
        assert "120만원" in knee.support_amount
        assert "349,700" in BY_ID[13].support_amount and "559,520" in BY_ID[13].support_amount
        assert "12,100" in BY_ID[14].support_amount
        massage = BY_ID[10]
        assert massage.requires_prescription and massage.is_local and massage.min_age == 60

    def test_templates_have_alias_placeholder(self):
        assert all("{alias}" in b.parent_msg_template for b in SEED_BENEFITS)


# ── Age / region ─────────────────────────────────────────────
class TestAgeAndRegion:
    def test_age_in_year(self):
        assert _parent(birth_year=1954).age_in(YEAR) == 72

    def test_too_young_excluded(self):
        assert evaluate_benefit(_benefit(min_age=65), _parent(birth_year=1962), YEAR) is None  # 64 at most

    def test_exact_threshold_year_is_grade_b(self):
        # 1961년생 → 2026년 생일 이후 만 65세: 생일 미도래 가능성 → B
        m = evaluate_benefit(_benefit(min_age=65), _parent(birth_year=1961), YEAR)
        assert m is not None and m.grade == ReliabilityGrade.GRADE_B
        assert any(ok is None for _, ok in m.checks)

    def test_one_year_past_threshold_is_grade_a(self):
        m = evaluate_benefit(_benefit(min_age=65), _parent(birth_year=1960), YEAR)
        assert m.grade == ReliabilityGrade.GRADE_A

    @pytest.mark.parametrize("raw,expected", [("42", "51"), ("45", "52"), ("51", "51"), ("all", "ALL"), ("11", "11")])
    def test_sido_normalization(self, raw, expected):
        assert normalize_sido_code(raw) == expected

    def test_legacy_code_matches_new_code(self):
        b = _benefit(jurisdiction_type="LOCAL", region_code="42")
        assert evaluate_benefit(b, _parent(sido_code="51"), YEAR) is not None

    def test_other_region_excluded(self):
        assert evaluate_benefit(BY_ID[11], _parent(sido_code="51"), YEAR) is None  # 서울 전용
        assert evaluate_benefit(BY_ID[11], _parent(sido_code="11"), YEAR) is not None


# ── Grade engine ─────────────────────────────────────────────
class TestGrades:
    def test_national_all_conditions_is_a(self):
        assert evaluate_benefit(BY_ID[1], _parent(), YEAR).grade == ReliabilityGrade.GRADE_A

    def test_unknown_income_is_b(self):
        assert evaluate_benefit(BY_ID[13], _parent(is_pension_receiver=-1), YEAR).grade == ReliabilityGrade.GRADE_B

    def test_pension_receiver_is_a(self):
        assert evaluate_benefit(BY_ID[13], _parent(is_pension_receiver=1), YEAR).grade == ReliabilityGrade.GRADE_A

    def test_low_income_no_excludes(self):
        assert evaluate_benefit(BY_ID[5], _parent(is_low_income=0), YEAR) is None

    def test_prescription_forces_b_and_caution(self):
        m = evaluate_benefit(BY_ID[5], _parent(is_low_income=1, is_pension_receiver=1), YEAR)
        assert m.grade == ReliabilityGrade.GRADE_B
        assert any("사전신청" in c for c in m.cautions) and any("진단서" in c for c in m.cautions)

    def test_nationwide_local_is_c(self):
        assert evaluate_benefit(BY_ID[9], _parent(), YEAR).grade == ReliabilityGrade.GRADE_C

    def test_region_specific_local_is_at_most_b(self):
        m = evaluate_benefit(BY_ID[11], _parent(sido_code="11"), YEAR)
        assert m.grade == ReliabilityGrade.GRADE_B

    def test_alone_only_excludes_couple(self):
        assert evaluate_benefit(BY_ID[21], _parent(living_type="COUPLE"), YEAR) is None
        assert evaluate_benefit(BY_ID[21], _parent(living_type="ALONE"), YEAR) is not None

    def test_sorted_by_grade(self):
        ranks = [m.grade.rank for m in match_benefits(SEED_BENEFITS, _parent(), YEAR)]
        assert ranks == sorted(ranks)


# ── TG-04 callbacks ──────────────────────────────────────────
class TestCallbacks:
    def test_cb_rejects_oversize(self):
        with pytest.raises(ValueError):
            cb("x" * 70)

    def test_all_keyboards_within_64_bytes(self):
        big = 2_147_483_647
        parent = _parent(id=big)
        matches = match_benefits(SEED_BENEFITS, parent, YEAR)
        markups = [
            build_main_menu_keyboard([parent], True),
            build_list_keyboard(parent, matches, 0),
            build_list_keyboard(parent, matches, 3),
            build_detail_keyboard(big, 999, 99, True),
        ]
        markups += [build_wizard_keyboard(s, {"decade": 1950}) for s in
                    ("alias", "decade", "year", "region", "sigungu", "living", "income")]
        for mk in markups:
            for data in _all_callbacks(mk):
                assert len(data.encode("utf-8")) <= MAX_CALLBACK_BYTES
                assert data.startswith("SC:") and data.isascii()


# ── TG-01 / TG-02 formatting ─────────────────────────────────
class TestFormatting:
    def _assert_limits(self, text: str):
        html_len, visible_len = measure_message(text)
        assert html_len <= TG_MAX_HTML_LEN and visible_len <= TG_MAX_VISIBLE_LEN

    def test_all_cards_within_limits(self):
        for parent in (_parent(), _parent(sido_code="11", is_low_income=1, is_pension_receiver=1)):
            matches = match_benefits(SEED_BENEFITS, parent, YEAR)
            self._assert_limits(format_candidate_list(parent, matches, 0, YEAR))
            for m in matches:
                self._assert_limits(format_benefit_card(parent, m, True, YEAR))
                self._assert_limits(format_phone_script(m.benefit, parent))
                self._assert_limits(format_kakao_share_html(m.benefit, parent.alias))

    def test_html_escape(self):
        evil = _benefit(title="<script>x</script>", support_amount="A & B", detail_guide="<b>bad</b>")
        parent = _parent(alias="<엄마>", sigungu_name="<i>구</i>")
        m = evaluate_benefit(evil, parent, YEAR)
        card = format_benefit_card(parent, m, False, YEAR)
        assert "<script>" not in card and "&lt;script&gt;" in card and "A &amp; B" in card
        assert "<b>bad</b>" not in card
        assert "&lt;엄마&gt;" in card
        script = format_phone_script(evil, parent)
        assert "<i>구</i>" not in script
        assert "<script>" not in format_kakao_share_html(evil, "<엄마>")
        assert "<엄마>" not in format_main_menu([parent])

    def test_only_allowed_tags(self):
        import re
        parent = _parent()
        m = match_benefits(SEED_BENEFITS, parent, YEAR)[0]
        for text in (format_benefit_card(parent, m), format_phone_script(m.benefit, parent),
                     format_kakao_share_html(m.benefit, "엄마")):
            tags = {t.lower() for t in re.findall(r"</?([a-zA-Z]+)", text)}
            assert tags <= {"b", "i", "code", "pre", "a"}

    def test_kakao_text_substitutes_alias(self):
        text = format_kakao_share_text(BY_ID[5], "아빠")
        assert text.startswith("아빠,") and "{alias}" not in text
        assert "먼저 신청" in text  # pre-approval reminder

    def test_phone_script_pre_approval_question(self):
        assert "승인" in format_phone_script(BY_ID[5])
        assert "승인까지" not in format_phone_script(BY_ID[1])

    def test_card_includes_proxy_docs_and_prescription(self):
        parent = _parent(is_low_income=1, is_pension_receiver=1)
        card = format_benefit_card(parent, evaluate_benefit(BY_ID[5], parent, YEAR))
        for doc in ("부모님 신분증", "가족관계증명서", "위임장", "진단서"):
            assert doc in card

    def test_weekly_briefing_within_limits(self):
        parents = [_parent(id=i, alias=f"부모{i}") for i in range(5)]
        sections = [(p, match_benefits(SEED_BENEFITS, p, YEAR)) for p in parents]
        self._assert_limits(format_weekly_briefing(sections, [b.title for b in SEED_BENEFITS], "11월 8일", YEAR))


# ── Storage ──────────────────────────────────────────────────
class TestStorage:
    def test_seed_roundtrip(self, db):
        assert db.count_benefits() == len(SEED_BENEFITS)
        assert db.get_benefit(5) == BY_ID[5]
        db.upsert_benefits(SEED_BENEFITS)  # idempotent
        assert db.count_benefits() == len(SEED_BENEFITS)

    def test_parent_crud_and_ownership(self, db):
        pid = db.save_parent(_parent(id=0, sido_code="42"))
        assert db.get_parent(pid).sido_code == "51"
        assert db.get_parent(pid, chat_id=100) is not None
        assert db.get_parent(pid, chat_id=999) is None
        assert db.delete_parent(pid, 999) is False
        db.toggle_bookmark(100, pid, 1)
        assert db.delete_parent(pid, 100) is True
        assert db.list_bookmarks(100) == []

    def test_bookmark_toggle(self, db):
        assert db.toggle_bookmark(1, 1, 5) is True
        assert db.is_bookmarked(1, 1, 5)
        assert db.toggle_bookmark(1, 1, 5) is False
        assert not db.is_bookmarked(1, 1, 5)

    def test_weekly_opt_out(self, db):
        db.save_parent(_parent(id=0, chat_id=1))
        db.save_parent(_parent(id=0, chat_id=2))
        db.set_weekly_opt_in(2, False)
        assert db.list_weekly_chat_ids() == [1]

    def test_dispatch_log(self, db):
        assert not db.has_dispatched("k", 1)
        db.log_dispatch("k", 1, "FAILED_429")
        assert not db.has_dispatched("k", 1)  # failures may be retried
        db.log_dispatch("k", 1, "SUCCESS")
        assert db.has_dispatched("k", 1)


# ── Interactive flow ─────────────────────────────────────────
class FakeChannel:
    def __init__(self):
        self.calls: list[tuple] = []

    def send_message(self, chat_id, text, reply_markup=None, disable_notification=False):
        self.calls.append(("send", chat_id, text, reply_markup))
        return len(self.calls)

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.calls.append(("edit", chat_id, text, reply_markup))
        return True

    def answer_callback_query(self, cq_id, text=""):
        self.calls.append(("ack", cq_id, text))
        return True


def _tap(bot, data, chat_id=7, n=0):
    bot.handle_update({"callback_query": {"id": f"cb{n}", "data": data,
                                          "message": {"message_id": 55, "chat": {"id": chat_id}}}})


class TestInteractive:
    def _register(self, bot, chat_id=7):
        _tap(bot, cb("new"), chat_id)
        _tap(bot, cb("a", "M"), chat_id)
        _tap(bot, cb("dc", 1950), chat_id)
        _tap(bot, cb("y", 1954), chat_id)
        _tap(bot, cb("r", "42"), chat_id)
        bot.handle_update({"message": {"chat": {"id": chat_id}, "text": "원주<b>시"}})
        _tap(bot, cb("l", "ALONE"), chat_id)
        _tap(bot, cb("i", "UNK"), chat_id)

    def test_full_wizard_creates_parent(self, db):
        ch = FakeChannel()
        bot = SilverCareInteractiveBot(ch, db, ref_year=YEAR)
        self._register(bot)
        parents = db.list_parents(7)
        assert len(parents) == 1
        p = parents[0]
        assert (p.alias, p.birth_year, p.sido_code, p.sigungu_name, p.living_type) == ("엄마", 1954, "51", "원주시", "ALONE")
        assert "받을 수 있는 혜택" in ch.calls[-1][2]
        assert db.get_draft(7) == {}

    def test_ack_is_first_call(self, db):
        ch = FakeChannel()
        bot = SilverCareInteractiveBot(ch, db, ref_year=YEAR)
        _tap(bot, cb("m"))
        assert ch.calls[0][0] == "ack"

    def test_detail_bookmark_and_script(self, db):
        ch = FakeChannel()
        bot = SilverCareInteractiveBot(ch, db, ref_year=YEAR)
        self._register(bot)
        pid = db.list_parents(7)[0].id
        _tap(bot, cb("b", pid, 1, 0))
        assert db.is_bookmarked(7, pid, 1)
        assert "⭐ 찜 해제" in json.dumps(ch.calls[-1][3], ensure_ascii=False)
        _tap(bot, cb("t", pid, 1, 0))
        assert "통화 대본" in ch.calls[-1][2]
        _tap(bot, cb("k", pid, 1, 0))
        assert "<pre>엄마," in ch.calls[-1][2]

    def test_other_chat_cannot_access_parent(self, db):
        ch = FakeChannel()
        bot = SilverCareInteractiveBot(ch, db, ref_year=YEAR)
        self._register(bot, chat_id=7)
        pid = db.list_parents(7)[0].id
        _tap(bot, cb("d", pid, 1, 0), chat_id=8)
        assert "부모님 복지 비서" in ch.calls[-1][2]  # bounced to menu
        _tap(bot, cb("b", pid, 1, 0), chat_id=8)
        assert db.list_bookmarks(8) == [] and db.list_bookmarks(7) == []
        _tap(bot, cb("xy", pid), chat_id=8)
        assert db.get_parent(pid) is not None

    def test_delete_requires_confirm(self, db):
        ch = FakeChannel()
        bot = SilverCareInteractiveBot(ch, db, ref_year=YEAR)
        self._register(bot)
        pid = db.list_parents(7)[0].id
        _tap(bot, cb("x", pid))
        assert db.get_parent(pid) is not None and "삭제할까요" in ch.calls[-1][2]
        _tap(bot, cb("xy", pid))
        assert db.get_parent(pid) is None

    def test_malformed_and_stale_callbacks(self, db):
        ch = FakeChannel()
        bot = SilverCareInteractiveBot(ch, db, ref_year=YEAR)
        for data in ("SC:d:abc", "SC:y:1954", "SC:zz", "XX:m", "SC:L:1:x"):
            _tap(bot, data)
        acks = [c for c in ch.calls if c[0] == "ack"]
        assert len(acks) == 5
        assert db.list_parents(7) == []

    def test_stale_pager_is_clamped(self, db):
        ch = FakeChannel()
        bot = SilverCareInteractiveBot(ch, db, ref_year=YEAR)
        self._register(bot)
        pid = db.list_parents(7)[0].id
        _tap(bot, cb("L", pid, 99))
        assert "받을 수 있는 혜택" in ch.calls[-1][2]


# ── Weekly dispatch ──────────────────────────────────────────
class _Opener:
    """Fake urlopen returning queued responses (dict → 200 OK, int → HTTPError)."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, req, timeout=0):
        self.requests.append(json.loads(req.data.decode("utf-8")))
        r = self.responses.pop(0)
        if isinstance(r, int):
            body = json.dumps({"ok": False, "description": "x", "parameters": {"retry_after": 1}}).encode()
            raise urllib.error.HTTPError(req.full_url, r, "err", {}, io.BytesIO(body))
        return io.BytesIO(json.dumps(r).encode())


class TestWeekly:
    def test_dispatch_key(self):
        assert weekly_dispatch_key(date(2026, 11, 8)) == "weekly:2026-W45"

    def test_rotation_changes_weekly(self):
        matches = match_benefits(SEED_BENEFITS, _parent(), YEAR)
        a, b = rotate_matches(matches, 1), rotate_matches(matches, 2)
        assert len(a) == 3 and [m.benefit.id for m in a] != [m.benefit.id for m in b]

    def test_build_and_idempotent_dispatch(self, db):
        db.save_parent(_parent(id=0, chat_id=1))
        db.save_parent(_parent(id=0, chat_id=2, alias="아빠"))
        key, messages = build_weekly_messages(db, date(2026, 11, 8))
        assert [c for c, _ in messages] == [1, 2]
        ok = {"ok": True, "result": {"message_id": 1}}
        channel = SilverTelegramChannel("T", opener=_Opener([ok, 429, ok]))
        sleeps: list[float] = []
        dispatcher = SafeTelegramDispatcher(TelegramRateLimiter(sleep=lambda s: None), sleep=sleeps.append)
        stats = dispatcher.dispatch(channel, db, key, messages)
        assert stats["success"] == 2 and stats["failed"] == 0 and sleeps  # 429 backed off
        again = dispatcher.dispatch(channel, db, key, messages)
        assert again["skipped"] == 2 and again["success"] == 0

    def test_blocked_user_not_counted_as_failure(self, db):
        channel = SilverTelegramChannel("T", opener=_Opener([403]))
        dispatcher = SafeTelegramDispatcher(TelegramRateLimiter(sleep=lambda s: None), sleep=lambda s: None)
        stats = dispatcher.dispatch(channel, db, "k", [(1, "hi")])
        assert stats["blocked"] == 1 and stats["failed"] == 0

    def test_api_error_carries_retry_after(self):
        channel = SilverTelegramChannel("T", opener=_Opener([429]))
        with pytest.raises(TelegramAPIError) as ei:
            channel.send_message(1, "x")
        assert ei.value.code == 429 and ei.value.retry_after == 1.0

    def test_edit_not_modified_is_success(self):
        class _NotModified(_Opener):
            def __call__(self, req, timeout=0):
                body = json.dumps({"ok": False, "description": "Bad Request: message is not modified"}).encode()
                raise urllib.error.HTTPError(req.full_url, 400, "err", {}, io.BytesIO(body))
        assert SilverTelegramChannel("T", opener=_NotModified([])).edit_message_text(1, 2, "x") is True


def test_rate_limiter_per_chat_interval():
    t = [0.0]
    waits: list[float] = []

    def sleep(s):
        waits.append(s)
        t[0] += s

    lim = TelegramRateLimiter(sleep=sleep, clock=lambda: t[0])
    lim.wait_before_send(1)
    lim.wait_before_send(1)
    assert waits and waits[-1] == pytest.approx(1.0)
    lim.wait_before_send(2)
    assert waits[-1] == pytest.approx(0.04)


def test_demo_runs_without_file_writes(tmp_path, monkeypatch, capsys):
    import silver_bot_runner
    monkeypatch.chdir(tmp_path)
    silver_bot_runner.run_demo()
    out = capsys.readouterr().out
    assert "통화 대본" in out and "강원 원주시" in out
    assert list(tmp_path.iterdir()) == []
