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
    format_parent_peace_card,
    format_parent_peace_text,
    format_proxy_action_kit,
    format_single_mission_card,
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
    build_mission_keyboard,
    build_mission_status_keyboard,
    build_wizard_keyboard,
    cb,
)
from silver_care.matching import evaluate_benefit, match_benefits, rank_missions, select_primary_mission
from silver_care.models import (
    MissionStatus,
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
        assert "자녀가 대신 신청할 1순위 복지" in ch.calls[-1][2]  # mission card, not the 28-item list
        assert "기초연금" in ch.calls[-1][2]
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
        assert "<pre>이번에 " in ch.calls[-1][2] and "엄마" not in ch.calls[-1][2]

    def test_other_chat_cannot_access_parent(self, db):
        ch = FakeChannel()
        bot = SilverCareInteractiveBot(ch, db, ref_year=YEAR)
        self._register(bot, chat_id=7)
        pid = db.list_parents(7)[0].id
        _tap(bot, cb("d", pid, 1, 0), chat_id=8)
        assert "부모님 복지 대리신청 비서" in ch.calls[-1][2]  # bounced to menu
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
        _tap(bot, cb("all", pid, 99))
        assert "페이지" in ch.calls[-1][2] and "받을 수 있는 혜택" in ch.calls[-1][2]


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


# ── Step 1: High-Density Field Guides & Web Report Tests ───────
class TestStep1FieldGuides:
    def test_top10_field_guides_present(self):
        guides = [b for b in SEED_BENEFITS if b.has_field_guide]
        assert len(guides) == 10
        expected_ids = {1, 2, 3, 5, 7, 8, 10, 13, 14, 25}
        assert {b.id for b in guides} == expected_ids

    def test_field_guide_structure(self):
        for b in SEED_BENEFITS:
            if not b.has_field_guide:
                continue
            assert len(b.documents) >= 1
            for doc in b.documents:
                assert "name" in doc and "issuer" in doc and "required" in doc
            assert len(b.application_steps) >= 3
            assert len(b.rejection_response) > 10
            if b.source_url:
                assert b.source_url.startswith("https://")

    def test_knee_surgery_strict_pre_approval_warning(self):
        knee = BY_ID[5]
        assert knee.is_pre_approval_required is True
        assert any("수술 전" in w for w in knee.warning_notes)
        assert "소급" in knee.rejection_response or "승인" in knee.rejection_response

    def test_database_preserves_field_guides(self, db):
        knee = db.get_benefit(5)
        assert knee is not None
        assert knee.has_field_guide is True
        assert len(knee.documents) >= 1
        assert len(knee.application_steps) >= 3
        assert len(knee.warning_notes) >= 1


class TestWebReport:
    def test_generate_web_report(self, tmp_path):
        from silver_care.web_report import generate_silver_web_report
        out_path = tmp_path / "reports" / "silver_latest.html"
        res = generate_silver_web_report(SEED_BENEFITS, out_path)
        assert res.exists()
        content = res.read_text(encoding="utf-8")

        # HTML validity & responsiveness
        assert "<!DOCTYPE html>" in content
        assert '<meta name="viewport" content="width=device-width, initial-scale=1.0' in content
        assert "2026 대한민국" in content
        assert "부모님 복지 대백과" in content

        # 10 core guides rendered
        for gid in [1, 2, 3, 5, 7, 8, 10, 13, 14, 25]:
            assert f'id="benefit-{gid}"' in content

        # PII-Zero Invariant: No user personal data, chat IDs, or private tokens
        for forbidden in ["chat_id", "7820975792", "SILVER_TELEGRAM_BOT_TOKEN", "bot_token"]:
            assert forbidden not in content

        # All URLs are HTTPS
        import re
        urls = re.findall(r'href="(http[^"]+)"', content)
        for u in urls:
            assert u.startswith("https://"), f"Non-HTTPS URL found: {u}"

    def test_detail_keyboard_has_web_report_button(self):
        kb = build_detail_keyboard(pid=1, bid=5, page=0, bookmarked=False)
        buttons = [b for row in kb["inline_keyboard"] for b in row]
        url_btns = [b for b in buttons if "url" in b]
        assert len(url_btns) == 1
        assert "silver_latest.html" in url_btns[0]["url"]
        assert "상세 리포트" in url_btns[0]["text"]


# ── 실버케어 2.0: 자녀 대리신청 1순위 미션 ─────────────────────
class TestMissionSelection:
    def test_basic_pension_is_primary_for_non_receiver(self):
        parent = _parent(is_pension_receiver=-1)
        primary = select_primary_mission(match_benefits(SEED_BENEFITS, parent, YEAR), parent)
        assert primary.benefit.id == 13

    def test_pension_receiver_skips_basic_pension(self):
        parent = _parent(is_pension_receiver=1, is_low_income=1)
        ids = [m.benefit.id for m in rank_missions(match_benefits(SEED_BENEFITS, parent, YEAR), parent)]
        assert 13 not in ids
        assert ids[:4] == [8, 5, 7, 14]  # 장기요양 → 무릎 → 치매 → 통신비(수급자)

    def test_telecom_requires_known_pension_receiver(self):
        parent = _parent(is_pension_receiver=-1)
        ids = [m.benefit.id for m in rank_missions(match_benefits(SEED_BENEFITS, parent, YEAR), parent, 10)]
        assert 14 not in ids and ids[:4] == [13, 8, 5, 7]

    def test_under_65_falls_back_to_60s_benefits(self):
        parent = _parent(birth_year=1964)  # 만 61~62세
        ids = [m.benefit.id for m in rank_missions(match_benefits(SEED_BENEFITS, parent, YEAR), parent)]
        assert 13 not in ids and 8 not in ids
        assert ids[:2] == [5, 7]

    def test_rest_ordered_grade_a_first_and_capped_at_five(self):
        parent = _parent()
        matches = match_benefits(SEED_BENEFITS, parent, YEAR)
        ranked = rank_missions(matches, parent)
        assert len(ranked) == 5 and len({m.benefit.id for m in ranked}) == 5
        tail = [m.grade.rank for m in rank_missions(matches, parent, 10)[4:]]
        assert tail == sorted(tail)

    def test_no_matches_returns_none(self):
        assert select_primary_mission([], _parent()) is None
        assert rank_missions([], _parent()) == []


class TestMissionStorage:
    def test_default_none_then_upsert(self, db):
        pid = db.save_parent(_parent(id=0))
        assert db.get_mission(100, pid, 13) is None
        m = db.set_mission_status(100, pid, 13, "CONSENT_NEEDED")
        assert m.status == "CONSENT_NEEDED" and m.status_label == "부모님 확인" and m.status_badge == "🟡"
        m2 = db.set_mission_status(100, pid, 13, MissionStatus.RECEIVING.value)
        assert m2.id == m.id and m2.status_label == "수급 중" and m2.status_badge == "🟢"
        assert [x.benefit_id for x in db.list_missions(100, pid)] == [13]

    def test_full_status_transition(self, db):
        for s in MissionStatus:
            assert db.set_mission_status(1, 1, 5, s.value).status == s.value
        assert len(db.list_missions(1, 1)) == 1

    def test_invalid_status_rejected(self, db):
        with pytest.raises(ValueError):
            db.set_mission_status(1, 1, 5, "HACKED")
        assert db.get_mission(1, 1, 5) is None

    def test_missions_isolated_by_chat(self, db):
        db.set_mission_status(1, 1, 5, "SUBMITTED")
        assert db.get_mission(2, 1, 5) is None
        assert db.list_missions(2, 1) == []

    def test_delete_parent_cascades_missions(self, db):
        pid = db.save_parent(_parent(id=0))
        db.set_mission_status(100, pid, 13, "SUBMITTED")
        db.set_mission_status(100, pid, 8, "DOCS_PREPARING")
        assert db.delete_parent(pid, 100) is True
        assert db.list_missions(100, pid) == []


class TestMissionFormatting:
    def _assert_limits(self, text: str):
        html_len, visible_len = measure_message(text)
        assert html_len <= TG_MAX_HTML_LEN and visible_len <= TG_MAX_VISIBLE_LEN

    def test_single_mission_card_content(self):
        parent = _parent()
        matches = match_benefits(SEED_BENEFITS, parent, YEAR)
        card = format_single_mission_card(parent, matches[0], None, 1, 5, len(matches), YEAR)
        assert "자녀가 대신 신청할 1순위 복지" in card and "(1/5)" in card
        assert "미착수" in card and "⚪" in card
        assert f"<b>{len(matches)}개</b>" in card
        assert "대신 신청할 수 있어요" in card

    def test_mission_card_shows_status_and_pre_approval(self, db):
        parent = _parent(is_low_income=1, is_pension_receiver=1)
        knee = evaluate_benefit(BY_ID[5], parent, YEAR)
        mission = db.set_mission_status(100, 1, 5, "SUBMITTED")
        card = format_single_mission_card(parent, knee, mission, 2, 5, 28, YEAR)
        assert "대리접수 완료" in card and "🟣" in card and "사전승인" in card
        assert "사전승인" not in format_single_mission_card(parent, evaluate_benefit(BY_ID[7], parent, YEAR))

    def test_proxy_kit_docs_and_consent_warning(self):
        parent = _parent(is_low_income=1, is_pension_receiver=1)
        kit = format_proxy_action_kit(parent, BY_ID[13])
        for doc in ("부모님 신분증 원본", "자녀(신청인) 신분증", "가족관계증명서", "위임장"):
            assert doc in kit
        assert "금융정보등 제공동의서" in kit and "자필" in kit
        assert "읍면동 주민센터 복지팀" in kit
        # 소득 무관 혜택에는 금융정보 동의서 경고를 띄우지 않는다
        assert "금융정보등 제공동의서" not in format_proxy_action_kit(parent, BY_ID[7])
        knee_kit = format_proxy_action_kit(parent, BY_ID[5])
        assert "진단서" in knee_kit and "사전승인" in knee_kit

    def test_peace_card_finance_vs_other(self):
        fin = format_parent_peace_text(BY_ID[13])
        assert fin.startswith("이번에 기초연금 알아보고") and "매달 통장으로" in fin
        other = format_parent_peace_text(BY_ID[7])
        assert other.startswith(f"이번에 {BY_ID[7].title} 알아봤는데 제가 신청해둘게요.") and "통장" not in other
        assert len(fin.splitlines()) == 2 and len(other.splitlines()) == 2
        assert "병원 가시기 전에 제가 미리 신청" in format_parent_peace_text(BY_ID[5])  # pre-approval

    def test_peace_text_has_no_honorific(self):
        for b in SEED_BENEFITS:
            text = format_parent_peace_text(b)
            assert text.startswith(f"이번에 {b.title} ")
            for alias in ("엄마", "아빠", "어머님", "아버님", "{alias}"):
                assert alias not in text

    def test_peace_card_pii_zero(self):
        parent = _parent(id=424242, chat_id=7820975792, birth_year=1954, sigungu_name="원주시")
        for b in SEED_BENEFITS:
            card = format_parent_peace_card(b)
            for forbidden in ("1954", "원주", "강원", "7820975792", "424242", "chat_id", "위임장", "금융정보"):
                assert forbidden not in card

    def test_new_cards_escape_and_limits(self):
        import re
        evil = _benefit(title="<script>x</script>", support_amount="A & B", agency_name="<b>기관</b>",
                        target_income="PENSION_70", category="FINANCE")
        parent = _parent(alias="<엄마>", sigungu_name="<i>구</i>", is_pension_receiver=1)
        m = evaluate_benefit(evil, parent, YEAR)
        for text in (format_single_mission_card(parent, m), format_proxy_action_kit(parent, evil),
                     format_parent_peace_card(evil)):
            assert "<script>" not in text and "<엄마>" not in text and "<b>기관</b>" not in text
            assert {t.lower() for t in re.findall(r"</?([a-zA-Z]+)", text)} <= {"b", "i", "code", "pre", "a"}
        for p in (_parent(), _parent(sido_code="11", is_low_income=1, is_pension_receiver=1)):
            for m in match_benefits(SEED_BENEFITS, p, YEAR):
                self._assert_limits(format_single_mission_card(p, m, None, 5, 5, 31, YEAR))
                self._assert_limits(format_proxy_action_kit(p, m.benefit))
                self._assert_limits(format_parent_peace_card(m.benefit))

    def test_mission_keyboards_within_64_bytes(self):
        big = 2_147_483_647
        for mk in (build_mission_keyboard(big, 999, 4, 5), build_mission_status_keyboard(big, 999, "CONSENT_NEEDED")):
            for data in _all_callbacks(mk):
                assert len(data.encode("utf-8")) <= MAX_CALLBACK_BYTES
                assert data.startswith("SC:") and data.isascii()


class TestMissionInteractive:
    def _bot(self, db):
        ch = FakeChannel()
        bot = SilverCareInteractiveBot(ch, db, ref_year=YEAR)
        TestInteractive()._register(bot)
        return ch, bot, db.list_parents(7)[0].id

    def test_L_shows_primary_mission_not_list(self, db):
        ch, bot, pid = self._bot(db)
        _tap(bot, cb("L", pid, 0))
        text = ch.calls[-1][2]
        assert "1순위 복지" in text and "기초연금" in text and "페이지" not in text
        datas = _all_callbacks(ch.calls[-1][3])
        assert cb("prx", pid, 13) in datas and cb("all", pid, 0) in datas and cb("nxt", pid, 13, 1) in datas

    def test_next_mission_cycles_up_to_five(self, db):
        ch, bot, pid = self._bot(db)
        seen = []
        for idx in range(6):
            _tap(bot, cb("nxt", pid, 0, idx))
            seen.append(ch.calls[-1][2])
        assert "(1/5)" in seen[0] and "(5/5)" in seen[4]
        assert "(5/5)" in seen[5]  # out-of-range index is clamped
        nxt_btns = [d for d in _all_callbacks(ch.calls[-1][3]) if d.startswith("SC:nxt:")]
        assert nxt_btns and nxt_btns[0].endswith(":0")  # wraps back to the first mission

    def test_status_picker_and_save(self, db):
        ch, bot, pid = self._bot(db)
        _tap(bot, cb("mst", pid, 13))
        assert "진행 상태를 골라주세요" in ch.calls[-1][2]
        assert len([d for d in _all_callbacks(ch.calls[-1][3]) if d.startswith("SC:mss:")]) == 5
        _tap(bot, cb("mss", pid, 13, "DOCS_PREPARING"))
        assert db.get_mission(7, pid, 13).status == "DOCS_PREPARING"
        assert "서류 준비" in ch.calls[-1][2] and "기초연금" in ch.calls[-1][2]  # back on the same mission
        assert ch.calls[-2] == ("ack", "cb0", "📌 진행 상태를 저장했어요")

    def test_invalid_status_and_foreign_chat_rejected(self, db):
        ch, bot, pid = self._bot(db)
        _tap(bot, cb("mss", pid, 13, "BOGUS"))
        assert db.get_mission(7, pid, 13) is None
        _tap(bot, cb("mss", pid, 13, "SUBMITTED"), chat_id=8)
        assert db.get_mission(8, pid, 13) is None and db.get_mission(7, pid, 13) is None

    def test_mission_tools_return_to_mission_card(self, db):
        ch, bot, pid = self._bot(db)
        _tap(bot, cb("prx", pid, 13))
        assert "자녀 대리신청 키트" in ch.calls[-1][2] and "금융정보등 제공동의서" in ch.calls[-1][2]
        assert _all_callbacks(ch.calls[-1][3])[0] == cb("mc", pid, 13)
        _tap(bot, cb("t", pid, 13))
        assert "통화 대본" in ch.calls[-1][2] and _all_callbacks(ch.calls[-1][3])[0] == cb("mc", pid, 13)
        _tap(bot, cb("k", pid, 13))
        assert "<pre>이번에 기초연금" in ch.calls[-1][2] and "매달 통장으로" in ch.calls[-1][2]
        _tap(bot, cb("mc", pid, 13))
        assert "1순위 복지" in ch.calls[-1][2]

    def test_legacy_three_arg_callbacks_return_to_detail(self, db):
        ch, bot, pid = self._bot(db)
        _tap(bot, cb("t", pid, 1, 2))
        assert _all_callbacks(ch.calls[-1][3])[0] == cb("d", pid, 1, 2)
        _tap(bot, cb("k", pid, 1, 2))
        assert _all_callbacks(ch.calls[-1][3])[0] == cb("d", pid, 1, 2)

    def test_all_list_and_detail_back_use_all(self, db):
        ch, bot, pid = self._bot(db)
        _tap(bot, cb("all", pid, 0))
        datas = _all_callbacks(ch.calls[-1][3])
        assert "페이지" in ch.calls[-1][2]
        assert cb("all", pid, 1) in datas and cb("L", pid, 0) in datas
        _tap(bot, cb("d", pid, 1, 1))
        assert cb("all", pid, 1) in _all_callbacks(ch.calls[-1][3])

    def test_delete_parent_cleans_missions_via_bot(self, db):
        ch, bot, pid = self._bot(db)
        _tap(bot, cb("mss", pid, 13, "SUBMITTED"))
        _tap(bot, cb("x", pid))
        assert "진행 상태" in ch.calls[-1][2]
        _tap(bot, cb("xy", pid))
        assert db.list_missions(7, pid) == []



# ── 실버케어 3.0: 연령대 1-클릭 (호칭·설문 제로) ─────────────
class TestAgeQuickStart:
    def test_main_menu_offers_only_two_age_buttons(self):
        mk = build_main_menu_keyboard([_parent()], True, YEAR)
        assert _all_callbacks(mk) == [cb("age", 65), cb("age", 60)]
        labels = json.dumps(mk, ensure_ascii=False)
        assert "1961년 이전 출생" in labels and "1962~1966년생" in labels
        menu = format_main_menu([_parent()], YEAR)
        assert "부모님 복지 대리신청 비서" in menu and "엄마" not in menu

    def test_start_shows_age_menu_without_wizard(self, db):
        ch = FakeChannel()
        bot = SilverCareInteractiveBot(ch, db, ref_year=YEAR)
        bot.handle_update({"message": {"chat": {"id": 7}, "text": "/start"}})
        assert "연령대를 선택" in ch.calls[-1][2] and "1/6" not in ch.calls[-1][2]
        assert _all_callbacks(ch.calls[-1][3]) == [cb("age", 65), cb("age", 60)]

    def test_age_65_jumps_to_basic_pension(self, db):
        ch = FakeChannel()
        bot = SilverCareInteractiveBot(ch, db, ref_year=YEAR)
        _tap(bot, cb("age", 65))
        text = ch.calls[-1][2]
        assert "만 65세 이상 자녀가 대신 신청할 1순위 복지" in text and "기초연금" in text
        assert "엄마" not in text and "년생" not in text
        p = db.list_parents(7)[0]
        assert (p.alias, p.birth_year, p.sido_code) == ("", 1958, "ALL") and p.is_age_only
        datas = _all_callbacks(ch.calls[-1][3])
        assert cb("prx", p.id, 13) in datas and cb("m") in datas
        assert "🔄 연령대 다시 선택" in json.dumps(ch.calls[-1][3], ensure_ascii=False)
        _tap(bot, cb("age", 65))  # second tap reuses the same profile
        assert len(db.list_parents(7)) == 1

    def test_age_60_jumps_to_knee_surgery(self, db):
        ch = FakeChannel()
        bot = SilverCareInteractiveBot(ch, db, ref_year=YEAR)
        _tap(bot, cb("age", 60))
        text = ch.calls[-1][2]
        assert "만 60~64세 자녀가 대신 신청할 1순위 복지" in text and BY_ID[5].title in text
        assert "기초연금" not in text
        p = db.list_parents(7)[0]
        assert (p.alias, p.birth_year, p.sido_code) == ("", 1964, "ALL")
        _tap(bot, cb("age", 65))
        assert len(db.list_parents(7)) == 2

    def test_age_tools_have_no_honorific(self, db):
        ch = FakeChannel()
        bot = SilverCareInteractiveBot(ch, db, ref_year=YEAR)
        _tap(bot, cb("age", 65))
        pid = db.list_parents(7)[0].id
        _tap(bot, cb("k", pid, 13))
        assert "<pre>이번에 기초연금" in ch.calls[-1][2]
        _tap(bot, cb("t", pid, 13))
        script = ch.calls[-1][2]
        assert "만 65세 이상 어르신 자녀" in script and "ALL" not in script and "전국 사시는" not in script
        _tap(bot, cb("prx", pid, 13))
        assert "</b> · " not in ch.calls[-1][2].splitlines()[1]
        _tap(bot, cb("all", pid, 0))
        assert "만 65세 이상" in ch.calls[-1][2] and "전국 공통" in ch.calls[-1][2]

    def test_invalid_age_band_falls_back_to_menu(self, db):
        ch = FakeChannel()
        bot = SilverCareInteractiveBot(ch, db, ref_year=YEAR)
        _tap(bot, "SC:age:70")
        assert db.list_parents(7) == [] and "연령대를 선택" in ch.calls[-1][2]
