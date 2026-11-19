"""Phase 2-A: DB / model / seed / weather foundations for the 1:1 Kids Compass assistant. Network-free."""
from __future__ import annotations
import json
import sqlite3
from datetime import date, datetime, timedelta

import pytest

from kids_compass.collector import (
    MILESTONE_SCHEDULE_VERSION,
    SEED_EXPERIENCES,
    SEED_MILESTONES,
    milestone_window,
    naver_search,
    recommend_weekend_picks,
    seed_all,
    sync_user_milestones,
)
from kids_compass.models import AgeGroup, ChildProfile, PregnancyProfile, PushMessage, WeatherSnapshot
from kids_compass.storage.db import KidsCompassDatabase
from kids_compass import weather

TODAY = date(2026, 11, 19)  # Thursday
NOW = datetime(2026, 11, 19, 9, 0, 0)


@pytest.fixture
def db():
    d = KidsCompassDatabase(":memory:")
    seed_all(d, TODAY)
    yield d
    d.close()


# ── models ────────────────────────────────────────────────────
def test_gestational_week_counts_back_from_due_date():
    p = PregnancyProfile("7", "복덩이", "2027-03-07")
    assert p.gestational_days(date(2027, 3, 7)) == 280 and p.gestational_week(date(2027, 3, 7)) == 40
    lmp = date(2027, 3, 7) - timedelta(days=280)
    assert p.gestational_days(lmp + timedelta(days=12 * 7 + 3)) == 87
    assert p.gestational_week(lmp + timedelta(days=12 * 7 + 3)) == 12
    assert p.gestational_days(lmp - timedelta(days=30)) == 0       # clamp low
    assert p.gestational_days(date(2027, 6, 1)) == 300             # clamp high


def test_child_weight_optional_and_push_message_defaults():
    assert ChildProfile(1, "7", "콩이", "2024-05-01", None).weight_kg is None
    assert ChildProfile(1, "7", "콩이", "2024-05-01", 11.2).weight_kg == 11.2
    a, b = PushMessage("7", "hi"), PushMessage("7", "hi")
    a.entity_ids.append("x")
    assert b.entity_ids == [] and a.reply_markup is None


# ── schema & migration ───────────────────────────────────────
NEW_TABLES = ("kids_onboarding_state", "kids_milestone_definitions", "kids_milestone_states",
              "kids_recommendation_batches", "kids_recommendation_items", "kids_user_place_feedbacks",
              "kids_sent_history", "kids_weather_snapshots")


def test_new_tables_and_profile_defaults(db):
    names = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert set(NEW_TABLES) <= names
    db.ensure_user(7)
    prof = db.get_user_profile(7)
    assert prof["alert_health_dday"] == 1 and prof["alert_weekend_mode"] == "TOGETHER"
    assert prof["alert_weekend_brief"] == 1 and prof["alert_dday_reserve"] == 1
    db.set_weekend_mode(7, "PRIMARY")
    db.set_alert(7, "health", False)
    prof = db.get_user_profile(7)
    assert prof["alert_weekend_mode"] == "PRIMARY" and prof["alert_health_dday"] == 0
    with pytest.raises(ValueError):
        db.set_weekend_mode(7, "ALL")


def test_legacy_children_not_null_weight_is_migrated(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE kids_user_profiles (telegram_user_id TEXT PRIMARY KEY, default_region TEXT NOT NULL DEFAULT '서울',
            alert_weekend_brief INTEGER NOT NULL DEFAULT 1, alert_dday_reserve INTEGER NOT NULL DEFAULT 1,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE kids_children (child_id INTEGER PRIMARY KEY AUTOINCREMENT,
            telegram_user_id TEXT NOT NULL REFERENCES kids_user_profiles(telegram_user_id) ON DELETE CASCADE,
            nickname TEXT NOT NULL, birth_date TEXT NOT NULL, weight_kg REAL NOT NULL,
            is_primary INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE kids_medicine_products (product_id TEXT PRIMARY KEY, product_name TEXT NOT NULL,
            ingredient TEXT NOT NULL, concentration_mg_ml REAL NOT NULL, min_age_months INTEGER NOT NULL,
            recommended_mg_kg REAL NOT NULL, max_single_mg REAL NOT NULL, interval_hours INTEGER NOT NULL,
            max_times_per_day INTEGER NOT NULL, max_daily_mg_kg REAL NOT NULL, disclaimer TEXT NOT NULL DEFAULT '');
        CREATE TABLE kids_dose_events (event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            child_id INTEGER NOT NULL REFERENCES kids_children(child_id) ON DELETE CASCADE,
            product_id TEXT NOT NULL REFERENCES kids_medicine_products(product_id),
            dose_ml REAL NOT NULL, dose_mg REAL NOT NULL, given_at TEXT NOT NULL, notes TEXT NOT NULL DEFAULT '');
        INSERT INTO kids_user_profiles (telegram_user_id) VALUES ('7');
        INSERT INTO kids_children (child_id, telegram_user_id, nickname, birth_date, weight_kg) VALUES (5, '7', '콩이', '2024-05-01', 11.0);
        INSERT INTO kids_medicine_products VALUES ('p', 'x', 'ACETAMINOPHEN', 32, 4, 10, 480, 4, 5, 75, '');
        INSERT INTO kids_dose_events (child_id, product_id, dose_ml, dose_mg, given_at) VALUES (5, 'p', 3.4, 110, '2026-11-01');
    """)
    c.commit()
    c.close()

    d = KidsCompassDatabase(path)
    weight = {r[1]: r for r in d.conn.execute("PRAGMA table_info(kids_children)").fetchall()}["weight_kg"]
    assert weight[3] == 0  # NOT NULL dropped
    assert d.conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert d.conn.execute("PRAGMA foreign_key_check").fetchall() == []
    assert [(ch.child_id, ch.weight_kg) for ch in d.list_children(7)] == [(5, 11.0)]
    prof = d.get_user_profile(7)
    assert prof["alert_health_dday"] == 1 and prof["alert_weekend_mode"] == "TOGETHER"
    new_id = d.add_child(7, "둘째", "2026-01-01", None)
    assert new_id > 5 and d.get_child(new_id, 7).weight_kg is None
    assert d.delete_child(5, 7)  # FK cascade still wired to the rebuilt table
    assert d.conn.execute("SELECT COUNT(*) FROM kids_dose_events").fetchone()[0] == 0
    d.close()
    KidsCompassDatabase(path).close()  # idempotent re-open


def test_set_primary_child_is_owner_scoped(db):
    a = db.add_child(7, "첫째", "2021-03-01", 18.0)
    b = db.add_child(7, "둘째", "2024-03-01")
    other = db.add_child(8, "남의집", "2022-03-01", 13.0)
    assert db.set_primary_child(7, b)
    assert [(c.child_id, c.is_primary) for c in db.list_children(7)] == [(b, True), (a, False)]
    assert not db.set_primary_child(7, other)
    assert db.get_child(other, 8).is_primary


def test_has_active_profile(db):
    assert not db.has_active_profile(7)
    db.ensure_user(7)
    assert not db.has_active_profile(7)
    db.set_pregnancy(7, "복덩이", "2027-03-07")
    assert db.has_active_profile(7)
    db.delete_pregnancy(7)
    db.add_child(7, "콩이", "2024-05-01")
    assert db.has_active_profile(7)


def test_onboarding_state_roundtrip_and_expiry(db):
    db.set_onboarding_state(7, "f1", "CHILD", "ASK_BIRTH", {"nickname": "콩이"}, NOW + timedelta(minutes=30))
    st = db.get_onboarding_state(7, NOW)
    assert st["step"] == "ASK_BIRTH" and st["payload"] == {"nickname": "콩이"}
    assert db.get_onboarding_state(7, NOW + timedelta(hours=1)) is None
    assert db.get_onboarding_state(7, NOW) is None  # expired row was cleaned up


# ── milestones ───────────────────────────────────────────────
def test_seed_milestones_cover_checkups_vaccines_and_pregnancy(db):
    codes = {m.code for m in SEED_MILESTONES}
    assert {f"CHECKUP_{i}" for i in range(1, 9)} <= codes
    assert {"VAC_BCG", "VAC_2M", "VAC_4M", "VAC_6M", "VAC_12M", "VAC_HEPA_1", "VAC_4_6Y"} <= codes
    assert {f"PREG_W{w}" for w in (8, 12, 16, 24, 28, 32, 36)} <= codes
    assert len({m.definition_id for m in SEED_MILESTONES}) == len(SEED_MILESTONES)
    assert len(db.list_milestone_definitions()) == len(SEED_MILESTONES)
    for m in SEED_MILESTONES:
        assert m.schedule_version == MILESTONE_SCHEDULE_VERSION
        assert m.source_url.startswith("https://search.naver.com/")
        if m.kind == "PREGNANCY":
            assert m.pregnancy_week and m.age_min_months is None
        else:
            assert 0 <= m.age_min_months <= m.age_max_months


def test_milestone_windows():
    by = {m.code: m for m in SEED_MILESTONES}
    assert milestone_window(by["CHECKUP_2"], birth_date="2026-01-31") == ("2026-05-31", "2026-08-30")
    assert milestone_window(by["CHECKUP_8"], birth_date="2020-03-01") == ("2025-09-01", "2026-02-28")
    lmp = date(2027, 3, 7) - timedelta(days=280)
    frm, to = milestone_window(by["PREG_W24"], due_date="2027-03-07")
    assert frm == (lmp + timedelta(weeks=24)).isoformat() and to == (lmp + timedelta(weeks=24, days=13)).isoformat()
    with pytest.raises(ValueError):
        milestone_window(by["PREG_W24"])


def test_milestone_sync_status_transitions(db):
    cid = db.add_child(7, "콩이", "2026-07-01", 7.0)  # 4 months on TODAY
    db.set_pregnancy(7, "둘째", "2027-03-07")
    n = sync_user_milestones(db, 7)
    child_defs = sum(1 for m in SEED_MILESTONES if m.kind != "PREGNANCY")
    preg_defs = sum(1 for m in SEED_MILESTONES if m.kind == "PREGNANCY")
    assert n == child_defs + preg_defs
    assert sync_user_milestones(db, 7) == n  # idempotent
    assert len(db.list_subject_milestones(7)) == n

    due = db.list_due_milestones(7, TODAY)
    checkup2 = next(m for m in due if m.definition_id.startswith("CHECKUP_2@"))
    assert checkup2.subject_kind == "CHILD" and checkup2.subject_id == str(cid) and checkup2.completed_at is None

    assert db.set_milestone_status(7, checkup2.milestone_id, "UNKNOWN", "USER", NOW)
    assert db.get_subject_milestone(7, checkup2.milestone_id).completed_at is None
    assert all(m.milestone_id != checkup2.milestone_id for m in db.list_due_milestones(7, TODAY))  # no repeat push

    assert db.set_milestone_status(7, checkup2.milestone_id, "USER_CONFIRMED", "USER", NOW)
    done = db.get_subject_milestone(7, checkup2.milestone_id)
    assert done.status == "USER_CONFIRMED" and done.completed_at == "2026-11-19 09:00:00"
    sync_user_milestones(db, 7)  # re-sync never resets a user's answer
    assert db.get_subject_milestone(7, checkup2.milestone_id).status == "USER_CONFIRMED"

    assert not db.set_milestone_status(8, checkup2.milestone_id, "DUE")  # other user cannot touch it
    with pytest.raises(ValueError):
        db.set_milestone_status(7, checkup2.milestone_id, "DONE")
    assert db.list_subject_milestones(7, statuses=["USER_CONFIRMED"])[0].milestone_id == checkup2.milestone_id


# ── recommendation tokens ────────────────────────────────────
def test_rec_tokens_are_short_ascii_owner_scoped_and_expire(db):
    picks = recommend_weekend_picks(db, AgeGroup.PRESCHOOL, "서울")
    pairs = db.create_rec_items(7, "weekend", picks, NOW + timedelta(days=3), now=NOW)
    assert [p for p, _ in pairs] == picks
    tokens = [t for _, t in pairs]
    assert len(set(tokens)) == 3
    for t in tokens:
        assert t.isascii() and t.isalnum() and len(t) <= 12
    assert db.resolve_rec_token(7, tokens[0], NOW).exp_id == picks[0].exp_id
    assert db.resolve_rec_token(8, tokens[0], NOW) is None                         # not the owner
    assert db.resolve_rec_token(7, tokens[0], NOW + timedelta(days=4)) is None     # expired
    assert db.resolve_rec_token(7, "zzzz", NOW) is None
    assert db.resolve_rec_token(7, "'; DROP TABLE x;--", NOW) is None
    cov = json.loads(db.conn.execute("SELECT coverage_json FROM kids_recommendation_items WHERE callback_token = ?",
                                     (tokens[0],)).fetchone()[0])
    assert cov["region_sido"] == picks[0].region_sido
    assert db.purge_expired_rec_items(NOW + timedelta(days=4)) == 3


# ── feedback, sent history, exclusion ────────────────────────
def test_place_feedback_suppression_windows(db):
    db.record_place_feedback(7, "seoulforest_insect", "VISITED", NOW)
    db.record_place_feedback(7, "grandpark_zoo", "NOT_INTERESTED", NOW)
    assert db.get_excluded_exp_ids(7, TODAY) == {"seoulforest_insect", "grandpark_zoo"}
    assert db.get_excluded_exp_ids(7, TODAY + timedelta(days=29)) == {"grandpark_zoo"}
    assert db.get_excluded_exp_ids(7, TODAY + timedelta(days=91)) == set()
    assert db.get_excluded_exp_ids(8, TODAY) == set()
    with pytest.raises(ValueError):
        db.record_place_feedback(7, "x", "LIKE", NOW)


def test_claim_and_finalize_sent_history(db):
    key = (7, "weekend", "EXPERIENCE", "nmk_main", "2026-W47")
    assert db.claim_sent_history(*key, content_hash="h1", now=NOW)
    assert not db.claim_sent_history(*key, content_hash="h1", now=NOW)  # IN_FLIGHT blocks
    for final in ("SENT", "UNKNOWN", "BLOCKED"):
        db.conn.execute("UPDATE kids_sent_history SET status = 'IN_FLIGHT'")
        assert db.finalize_sent_history(*key, final)
        assert not db.claim_sent_history(*key, now=NOW), final
    db.conn.execute("UPDATE kids_sent_history SET status = 'IN_FLIGHT'")
    assert db.finalize_sent_history(*key, "FAILED")
    assert not db.finalize_sent_history(*key, "SENT")  # only IN_FLIGHT rows finalize
    assert db.claim_sent_history(*key, content_hash="h2", now=NOW)  # FAILED is retryable
    assert db.get_sent_status(*key) == "IN_FLIGHT"
    with pytest.raises(ValueError):
        db.claim_sent_history(7, "weekend", "PLACE", "x", "w")
    with pytest.raises(ValueError):
        db.finalize_sent_history(*key, "IN_FLIGHT")


def test_sent_history_cooldown_feeds_exclusion(db):
    db.claim_sent_history(7, "weekend", "EXPERIENCE", "nmk_main", "2026-W47", now=NOW)
    db.finalize_sent_history(7, "weekend", "EXPERIENCE", "nmk_main", "2026-W47", "SENT")
    db.claim_sent_history(7, "weekend", "EXPERIENCE", "bok_money", "2026-W47", now=NOW)
    db.finalize_sent_history(7, "weekend", "EXPERIENCE", "bok_money", "2026-W47", "FAILED")
    db.claim_sent_history(7, "dday", "BOOKING", "1", "2026-W47", now=NOW)
    assert db.get_excluded_exp_ids(7, TODAY) == {"nmk_main"}
    assert db.get_excluded_exp_ids(7, TODAY + timedelta(days=29)) == set()
    assert db.get_excluded_exp_ids(7, TODAY + timedelta(days=10), cooldown_days=7) == set()


def test_recommend_respects_exclusions_and_backfill_flag(db):
    base = recommend_weekend_picks(db, AgeGroup.PRESCHOOL, "서울")
    excluded = {base[0].exp_id}
    again = recommend_weekend_picks(db, AgeGroup.PRESCHOOL, "서울", excluded_exp_ids=excluded)
    assert again and base[0].exp_id not in {p.exp_id for p in again}
    # Push mode: never back-fill from other regions
    for rain in (False, True):
        picks = recommend_weekend_picks(db, AgeGroup.INFANT, "인천", weather_rain=rain, is_dispatch=True,
                                        allow_backfill=False)
        assert all(p.region_sido == "인천" for p in picks)
    everything = {e.exp_id for e in SEED_EXPERIENCES}
    assert recommend_weekend_picks(db, AgeGroup.PRESCHOOL, "서울", excluded_exp_ids=everything) == []


# ── seeds ────────────────────────────────────────────────────
def test_seed_expansion_and_fact_safety():
    assert len(SEED_EXPERIENCES) >= 40
    by_region = {r: [e for e in SEED_EXPERIENCES if e.region_sido == r] for r in ("서울", "경기", "인천")}
    assert len(by_region["경기"]) >= 10 and len(by_region["인천"]) >= 8
    assert {"수원시", "성남시", "용인시", "고양시", "부천시", "과천시"} <= {e.region_sigungu for e in by_region["경기"]}
    assert {"연수구", "부평구", "남동구", "중구"} <= {e.region_sigungu for e in by_region["인천"]}
    assert len({e.exp_id for e in SEED_EXPERIENCES}) == len(SEED_EXPERIENCES)
    for e in SEED_EXPERIENCES:
        assert e.operating_hours.strip(), e.exp_id
        assert e.booking_url == naver_search(f"{e.venue_name} 예약"), e.exp_id
    for region in ("경기", "인천"):
        walk_in = {e.venue_name for e in by_region[region]
                   if not e.booking_required and e.age_group in (AgeGroup.PRESCHOOL, AgeGroup.ALL)}
        assert len(walk_in) >= 3


def test_regional_push_picks_without_backfill(db):
    for region in ("서울", "경기", "인천"):
        for age in (AgeGroup.INFANT, AgeGroup.PRESCHOOL, AgeGroup.ELEMENTARY):
            picks = recommend_weekend_picks(db, age, region, allow_backfill=False)
            assert len(picks) == 3 and all(p.region_sido == region for p in picks), (region, age)


# ── weather ──────────────────────────────────────────────────
def _payload(days, probs, sums, codes):
    return json.dumps({"daily": {"time": days, "precipitation_probability_max": probs,
                                 "precipitation_sum": sums, "weather_code": codes}}).encode()


def test_weekend_dates():
    assert weather.weekend_dates(date(2026, 11, 19)) == [date(2026, 11, 21), date(2026, 11, 22)]
    assert weather.weekend_dates(date(2026, 11, 21)) == [date(2026, 11, 21), date(2026, 11, 22)]
    assert weather.weekend_dates(date(2026, 11, 22)) == [date(2026, 11, 22)]


def test_weather_rain_and_dry_parsing_with_injected_opener(db):
    calls = []

    def opener(url, timeout):
        calls.append((url, timeout))
        return _payload(["2026-11-21", "2026-11-22"], [80, 10], [5.2, 0.0], [61, 1])

    snaps = weather.fetch_weekend_weather("서울", TODAY, db=db, opener=opener, now=NOW)
    assert len(calls) == 1 and calls[0][1] == 5.0
    assert "api.open-meteo.com/v1/forecast" in calls[0][0] and "start_date=2026-11-21" in calls[0][0]
    sat, sun = snaps
    assert sat.is_rain_or_snow and not sun.is_rain_or_snow
    assert not sat.is_failed and not sun.is_failed
    for s in snaps:
        assert s.is_bad_dust is False and "좋음" not in s.summary_text and "미세먼지 정보 미제공" in s.summary_text
    assert weather.weekend_outlook(snaps) == (True, False)
    # cached: second call does not hit the network
    again = weather.fetch_weekend_weather("서울", TODAY, db=db, opener=lambda u, t: pytest.fail("network"), now=NOW)
    assert [s.is_rain_or_snow for s in again] == [True, False]
    # after TTL the cache is stale -> refetch
    weather.fetch_weekend_weather("서울", TODAY, db=db, opener=opener, now=NOW + timedelta(hours=4))
    assert len(calls) == 2


@pytest.mark.parametrize("opener", [
    lambda u, t: (_ for _ in ()).throw(OSError("timeout")),
    lambda u, t: b"not json",
    lambda u, t: json.dumps({"error": True}).encode(),
    lambda u, t: _payload(["2026-11-21"], [None], [None], [None]),
])
def test_weather_failure_is_never_fake_sunny(db, opener):
    snaps = weather.fetch_weekend_weather("경기", TODAY, db=db, opener=opener, now=NOW)
    assert snaps and all(s.is_failed and not s.is_rain_or_snow and not s.is_bad_dust for s in snaps)
    assert all("맑" not in s.summary_text and "좋음" not in s.summary_text for s in snaps)
    assert weather.weekend_outlook(snaps)[1] is True
    assert db.get_weather_snapshot("경기", "2026-11-21", NOW) is None  # failures are not cached


def test_weather_unsupported_region_fails_closed(db):
    snaps = weather.fetch_weekend_weather("부산", TODAY, db=db, opener=lambda u, t: pytest.fail("network"), now=NOW)
    assert all(s.is_failed for s in snaps)
    assert weather.weekend_outlook([]) == (False, True)


def test_weather_snapshot_roundtrip(db):
    snap = WeatherSnapshot("인천", "2026-11-21", True, False, "x", "open-meteo",
                           "2026-11-19 09:00:00", "2026-11-19 12:00:00")
    db.upsert_weather_snapshot(snap)
    assert db.get_weather_snapshot("인천", "2026-11-21", NOW) == snap
    assert db.get_weather_snapshot("인천", "2026-11-21", NOW + timedelta(hours=3)) is None
