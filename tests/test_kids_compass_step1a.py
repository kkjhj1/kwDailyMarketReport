"""Kids Compass Step 1-A: 응급 타이머 엔진·DB, 구강검진/D-14/지원금, E-gen 임포터 (offline, no Telegram)."""
from __future__ import annotations

import re
import sqlite3
import threading
from datetime import date, datetime, timedelta

import pytest

from kids_compass import hospitals_importer as hi
from kids_compass.collector import SEED_HOSPITALS, SEED_MILESTONES, milestone_window, seed_all, sync_user_milestones
from kids_compass.fever_engine import (
    CROSS_TIMER_NOTICE,
    DEFAULT_MEDICINES,
    STATUS_BLOCKED_INFANT,
    STATUS_BLOCKED_MIN_AGE,
    STATUS_BLOCKED_NSAID,
    STATUS_INVALID_WEIGHT,
    calculate_dose,
    check_24h_dose_limit,
    cross_dosing_candidates,
)
from kids_compass.models import HospitalHourSlot, IngredientType, MedicineProduct, MilestoneDefinition, MoonlightHospital
from kids_compass.storage.db import KidsCompassDatabase
from kids_compass.storage.db import milestone_window as db_milestone_window

A, I, D = IngredientType.ACETAMINOPHEN, IngredientType.IBUPROFEN, IngredientType.DEXIBUPROFEN
TYL, BRU, MAX = DEFAULT_MEDICINES["TYLENOL_32"], DEFAULT_MEDICINES["BRUFEN_20"], DEFAULT_MEDICINES["MAXIBUFEN_12"]
NOW = datetime(2026, 11, 21, 21, 0, 0)


@pytest.fixture
def db():
    d = KidsCompassDatabase(":memory:")
    seed_all(d, date(2026, 11, 21))
    yield d
    d.close()


# ── fever engine: weight / age boundaries ─────────────────────
@pytest.mark.parametrize("weight", [None, float("nan"), float("inf"), float("-inf"), 0, -3, 100.0001, "abc"])
def test_invalid_weight_never_raises(weight):
    for p in DEFAULT_MEDICINES.values():
        r = calculate_dose(weight, 24, p)
        assert r.status == STATUS_INVALID_WEIGHT and r.recommended_ml == 0.0 and r.recommended_mg == 0.0


def test_weight_upper_bound_inclusive():
    assert calculate_dose(100, 144, TYL).status == "OK"


@pytest.mark.parametrize("months,tyl,bru,maxi", [
    (3, STATUS_BLOCKED_INFANT, STATUS_BLOCKED_INFANT, STATUS_BLOCKED_INFANT),
    (4, "OK", STATUS_BLOCKED_NSAID, STATUS_BLOCKED_NSAID),
    (5, "OK", STATUS_BLOCKED_NSAID, STATUS_BLOCKED_NSAID),
    (6, "OK", "OK", "OK"),
])
def test_age_boundaries_3_4_6_months(months, tyl, bru, maxi):
    assert calculate_dose(7, months, TYL).status == tyl
    assert calculate_dose(7, months, BRU).status == bru
    assert calculate_dose(7, months, MAX).status == maxi


def test_product_min_age_enforced():
    late = MedicineProduct("X", "x", A, 32.0, 24, 12.5, 640, 4, 5, 75, "")
    r = calculate_dose(12, 18, late)
    assert r.status == STATUS_BLOCKED_MIN_AGE and r.recommended_ml == 0.0
    assert calculate_dose(12, 24, late).status == "OK"


def test_unknown_age_fails_closed():
    assert calculate_dose(10, None, TYL).status == STATUS_BLOCKED_INFANT


# ── cross dosing candidates ───────────────────────────────────
def test_cross_candidates_never_pair_ibuprofen_dexibuprofen():
    assert cross_dosing_candidates(A, 12) == [I, D]
    assert cross_dosing_candidates(I, 12) == [A]
    assert cross_dosing_candidates(D, 12) == [A]
    assert D not in cross_dosing_candidates(I, 60) and I not in cross_dosing_candidates(D, 60)


@pytest.mark.parametrize("months", [0, 3, 4, 5])
def test_cross_candidates_empty_under_6m(months):
    for ing in (A, I, D):
        assert cross_dosing_candidates(ing, months) == []


def test_cross_timer_notice_text():
    assert "다른 성분을 복용해도 된다는 의미가 아닙니다" in CROSS_TIMER_NOTICE
    assert "수두" in CROSS_TIMER_NOTICE and "이부프로펜 계열" in CROSS_TIMER_NOTICE


# ── 24h limit ─────────────────────────────────────────────────
def _ev(hours_ago: float, ing=A, mg=125.0):
    return {"ingredient": ing.value, "dose_mg": mg, "given_at": (NOW - timedelta(hours=hours_ago)).strftime("%Y-%m-%d %H:%M:%S")}


def test_24h_count_limit_same_ingredient_only():
    events = [_ev(h) for h in (2, 6, 10, 14, 18)]  # 5 acetaminophen doses
    ok, msg = check_24h_dose_limit(events, A, 10, TYL, NOW)
    assert not ok and "5회" in msg
    ok, _ = check_24h_dose_limit(events, I, 10, BRU, NOW)  # different ingredient unaffected
    assert ok


def test_24h_window_excludes_older_doses():
    events = [_ev(h) for h in (24, 25, 30, 40, 2)]  # only the 2h-ago dose is inside (now-24h, now]
    assert check_24h_dose_limit(events, A, 10, TYL, NOW)[0]


def test_24h_cumulative_mg_limit():
    # 10kg -> 750mg/day limit, single dose 125mg. 4 x 160mg = 640 + 125 = 765 > 750
    events = [_ev(h, mg=160.0) for h in (1, 5, 9, 13)]
    ok, msg = check_24h_dose_limit(events, A, 10, TYL, NOW)
    assert not ok and "누적" in msg
    events = [_ev(h, mg=125.0) for h in (1, 5, 9, 13)]  # 500 + 125 = 625 <= 750, 4 < 5 times
    assert check_24h_dose_limit(events, A, 10, TYL, NOW)[0]


def test_24h_fail_closed_on_bad_input():
    assert not check_24h_dose_limit([], A, None, TYL, NOW)[0]
    assert not check_24h_dose_limit([], A, float("nan"), TYL, NOW)[0]
    bad = [{"ingredient": "ACETAMINOPHEN", "dose_mg": 150, "given_at": "garbage"}] * 5
    assert not check_24h_dose_limit(bad, A, 10, TYL, NOW)[0]  # unparseable time counts as recent


# ── fever tokens ──────────────────────────────────────────────
def test_fever_token_owner_expiry_single_use(db):
    cid = db.add_child(7, "콩이", "2024-05-01", 11.0)
    tok = db.create_fever_token(7, cid, "TYLENOL_32", "ACETAMINOPHEN", 4.3, 137.5, 11.0, 30, 32.0,
                                ttl_minutes=10, now=NOW)
    assert tok.isascii() and tok.isalnum() and len(tok) <= 32
    assert db.consume_fever_token(8, tok, NOW) is None  # other user
    assert db.consume_fever_token(7, tok, NOW + timedelta(minutes=11)) is None  # expired
    row = db.consume_fever_token(7, tok, NOW + timedelta(minutes=1))
    assert row["child_id"] == cid and row["dose_ml"] == 4.3 and row["ingredient"] == "ACETAMINOPHEN"
    assert db.consume_fever_token(7, tok, NOW + timedelta(minutes=2)) is None  # single use
    assert db.consume_fever_token(7, "bad token!", NOW) is None


# ── dose events + timers ──────────────────────────────────────
def _record(d, uid, cid, key, now=NOW, ing="ACETAMINOPHEN", sug="IBUPROFEN"):
    return d.record_dose_and_start_timer(uid, cid, "TYLENOL_32", ing, 4.3, 137.5, 32.0, 11.0, 30, key,
                                         suggested_ingredient=sug, now=now)


def test_record_dose_idempotent_and_timer_window(db):
    cid = db.add_child(7, "콩이", "2024-05-01", 11.0)
    r1 = _record(db, 7, cid, "k1")
    assert not r1["duplicate"] and r1["timer_id"]
    r2 = _record(db, 7, cid, "k1")
    assert r2 == {"event_id": r1["event_id"], "timer_id": r1["timer_id"], "duplicate": True}
    assert db.conn.execute("SELECT COUNT(*) FROM kids_dose_events").fetchone()[0] == 1
    assert db.conn.execute("SELECT COUNT(*) FROM kids_dose_timers").fetchone()[0] == 1
    t = db.get_dose_timer(r1["timer_id"])
    assert t.status == "PENDING" and t.due_at == "2026-11-21 23:30:00" and t.expires_at == "2026-11-22 00:00:00"
    ev = db.list_recent_dose_events(cid, NOW - timedelta(hours=24))
    assert len(ev) == 1 and ev[0]["ingredient"] == "ACETAMINOPHEN" and ev[0]["idempotency_key"] == "k1"


def test_record_dose_owner_scoped(db):
    cid = db.add_child(7, "콩이", "2024-05-01", 11.0)
    with pytest.raises(PermissionError):
        _record(db, 8, cid, "evil")
    assert db.conn.execute("SELECT COUNT(*) FROM kids_dose_events").fetchone()[0] == 0
    _record(db, 7, cid, "shared")
    with pytest.raises(PermissionError):
        _record(db, 8, cid, "shared")


def test_new_dose_supersedes_pending_timer(db):
    cid = db.add_child(7, "콩이", "2024-05-01", 11.0)
    r1 = _record(db, 7, cid, "k1")
    r2 = _record(db, 7, cid, "k2", now=NOW + timedelta(minutes=30), ing="IBUPROFEN", sug="ACETAMINOPHEN")
    assert db.get_dose_timer(r1["timer_id"]).status == "SUPERSEDED"
    assert db.get_dose_timer(r2["timer_id"]).status == "PENDING"
    r3 = _record(db, 7, cid, "k3", now=NOW + timedelta(minutes=40), sug="")  # no candidate -> no timer
    assert r3["timer_id"] is None and db.get_dose_timer(r2["timer_id"]).status == "SUPERSEDED"


def test_legacy_dose_event_ingredient_fallback(db):
    cid = db.add_child(7, "콩이", "2024-05-01", 11.0)
    db.log_dose_event(cid, "BRUFEN_20", 4.1, 82.5, given_at="2026-11-21 20:00:00")
    ev = db.list_recent_dose_events(cid, "2026-11-20T21:00:00")
    assert ev[0]["ingredient"] == "IBUPROFEN"


def test_claim_due_and_finalize(db):
    cid = db.add_child(7, "콩이", "2024-05-01", 11.0)
    r = _record(db, 7, cid, "k1")
    assert db.claim_due_dose_timers(NOW + timedelta(minutes=149), "c0") == []  # not yet due
    won = db.claim_due_dose_timers(NOW + timedelta(minutes=150), "c1")
    assert [t.timer_id for t in won] == [r["timer_id"]]
    t = won[0]
    assert t.status == "IN_FLIGHT" and t.claim_token == "c1" and t.attempt_count == 1
    assert db.claim_due_dose_timers(NOW + timedelta(minutes=150), "c2") == []  # already claimed
    assert not db.finalize_dose_timer(t.timer_id, "c2", "SENT")  # wrong token
    assert db.finalize_dose_timer(t.timer_id, "c1", "SENT", now=NOW + timedelta(minutes=151))
    done = db.get_dose_timer(t.timer_id)
    assert done.status == "SENT" and done.sent_at == "2026-11-21 23:31:00"
    assert not db.finalize_dose_timer(t.timer_id, "c1", "FAILED")  # terminal stays terminal
    with pytest.raises(ValueError):
        db.finalize_dose_timer(t.timer_id, "c1", "PENDING")


def test_ttl_30min_expired_never_sent(db):
    cid = db.add_child(7, "콩이", "2024-05-01", 11.0)
    r = _record(db, 7, cid, "k1")
    late = NOW + timedelta(minutes=150 + 30, seconds=1)  # restart after the window
    assert db.claim_due_dose_timers(late, "c1") == []
    t = db.get_dose_timer(r["timer_id"])
    assert t.status == "EXPIRED" and t.terminal_reason == "TTL_EXPIRED" and t.attempt_count == 0
    # exactly at expires_at is still claimable
    r2 = _record(db, 7, cid, "k2")
    edge = datetime.strptime(db.get_dose_timer(r2["timer_id"]).expires_at, "%Y-%m-%d %H:%M:%S")
    assert [x.timer_id for x in db.claim_due_dose_timers(edge, "c2")] == [r2["timer_id"]]


def test_stale_lease_becomes_unknown_and_is_never_resent(db):
    cid = db.add_child(7, "콩이", "2024-05-01", 11.0)
    r = _record(db, 7, cid, "k1")
    due = NOW + timedelta(minutes=150)
    assert db.claim_due_dose_timers(due, "c1", lease_seconds=60)
    # worker crashed; next sweep after the lease
    assert db.claim_due_dose_timers(due + timedelta(seconds=61), "c2") == []
    t = db.get_dose_timer(r["timer_id"])
    assert t.status == "UNKNOWN" and t.terminal_reason == "STALE_LEASE" and t.attempt_count == 1
    for k in range(1, 5):
        assert db.claim_due_dose_timers(due + timedelta(minutes=k), f"c{k + 2}") == []
    assert db.get_dose_timer(r["timer_id"]).status == "UNKNOWN"
    assert not db.finalize_dose_timer(r["timer_id"], "c1", "SENT")  # late finalize from dead worker ignored


def test_concurrent_claim_single_winner(tmp_path):
    path = str(tmp_path / "kids.sqlite3")
    base = KidsCompassDatabase(path)
    seed_all(base, date(2026, 11, 21))
    cids = [base.add_child(7, f"아이{i}", "2024-05-01", 11.0) for i in range(5)]
    for i, cid in enumerate(cids):
        _record(base, 7, cid, f"k{i}")
    base.close()

    due = NOW + timedelta(minutes=151)
    results: dict[str, list[int]] = {}
    errors: list[BaseException] = []
    barrier = threading.Barrier(4)

    def worker(name: str) -> None:
        d = KidsCompassDatabase(path)
        try:
            barrier.wait()
            results[name] = [t.timer_id for t in d.claim_due_dose_timers(due, name)]
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)
        finally:
            d.close()

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    claimed = [tid for ids in results.values() for tid in ids]
    assert len(claimed) == 5 and len(set(claimed)) == 5  # every timer exactly once


def test_cancel_child_timers_keeps_history(db):
    cid = db.add_child(7, "콩이", "2024-05-01", 11.0)
    r = _record(db, 7, cid, "k1")
    assert db.cancel_child_dose_timers(8, cid) == 0  # other user
    assert db.cancel_child_dose_timers(7, cid) == 1
    t = db.get_dose_timer(r["timer_id"])
    assert t.status == "CANCELLED"
    assert db.claim_due_dose_timers(NOW + timedelta(minutes=155), "c1") == []


def test_single_pending_timer_per_child_enforced(db):
    cid = db.add_child(7, "콩이", "2024-05-01", 11.0)
    r = _record(db, 7, cid, "k1")
    with pytest.raises(sqlite3.IntegrityError):
        db.conn.execute(
            "INSERT INTO kids_dose_timers (dose_event_id, telegram_user_id, child_id, given_ingredient, "
            "suggested_ingredient, due_at, expires_at, status, created_at, updated_at) "
            "VALUES (?, '7', ?, 'A', 'I', 'x', 'y', 'PENDING', 'n', 'n')",
            (r["event_id"], cid),
        )
    db.conn.rollback()


# ── migration of a legacy DB ──────────────────────────────────
def test_legacy_db_migrates_new_columns(tmp_path):
    path = str(tmp_path / "legacy.sqlite3")
    c = sqlite3.connect(path)
    c.executescript("""
        CREATE TABLE kids_moonlight_hospitals (hospital_id TEXT PRIMARY KEY, name TEXT NOT NULL,
            region_sido TEXT NOT NULL, region_sigungu TEXT NOT NULL DEFAULT '', address TEXT NOT NULL DEFAULT '',
            tel TEXT NOT NULL DEFAULT '', weekday_hours TEXT NOT NULL DEFAULT '', weekend_hours TEXT NOT NULL DEFAULT '',
            holiday_hours TEXT NOT NULL DEFAULT '', paired_pharmacy TEXT NOT NULL DEFAULT '',
            paired_pharmacy_tel TEXT NOT NULL DEFAULT '', map_url TEXT NOT NULL DEFAULT '',
            last_verified_at TEXT NOT NULL DEFAULT '', is_active INTEGER NOT NULL DEFAULT 1);
        INSERT INTO kids_moonlight_hospitals (hospital_id, name, region_sido, last_verified_at)
            VALUES ('old_seed', '옛 시드', '서울', '미검증(운영 전 E-gen 확인 필요)');
        CREATE TABLE kids_milestone_definitions (definition_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
            code TEXT NOT NULL, schedule_version TEXT NOT NULL, age_min_months INTEGER, age_max_months INTEGER,
            pregnancy_week INTEGER, title TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '');
    """)
    c.commit()
    c.close()
    d = KidsCompassDatabase(path)
    assert {"is_placeholder", "source"} <= set(d._columns("kids_moonlight_hospitals"))
    assert {"window_type", "age_min_days", "age_max_days"} <= set(d._columns("kids_milestone_definitions"))
    assert {"idempotency_key", "ingredient", "weight_kg_snapshot"} <= set(d._columns("kids_dose_events"))
    assert d.get_hospital("old_seed").is_placeholder  # legacy unverified seed flagged as placeholder
    d.close()
    KidsCompassDatabase(path).close()  # idempotent re-open


# ── milestones: windows ───────────────────────────────────────
BY = {m.code: m for m in SEED_MILESTONES}


def test_seed_milestones_dental_and_benefits():
    for code, rng in (("DENTAL_1", (18, 29)), ("DENTAL_2", (30, 41)), ("DENTAL_3", (42, 53)), ("DENTAL_4", (54, 65))):
        m = BY[code]
        assert m.kind == "DENTAL" and (m.age_min_months, m.age_max_months) == rng
    assert BY["CHECKUP_1"].window_type == "DAYS" and (BY["CHECKUP_1"].age_min_days, BY["CHECKUP_1"].age_max_days) == (14, 35)
    assert BY["BENEFIT_POSTBIRTH"].kind == "BENEFIT"
    assert BY["BENEFIT_AGE3_PRESCHOOL"].window_type == "SCHOOL_YEAR"
    for code in ("BENEFIT_POSTBIRTH", "BENEFIT_AGE3_PRESCHOOL"):
        text = BY[code].title + BY[code].description
        assert re.search(r"\d[\d,]*\s*만?\s*원", text) is None  # no hard-coded amounts
        assert "https://www." in BY[code].description and ".go.kr" in BY[code].description


def test_definitions_roundtrip_window_fields(db):
    defs = {d.code: d for d in db.list_milestone_definitions()}
    assert defs["CHECKUP_1"].window_type == "DAYS" and defs["CHECKUP_1"].age_max_days == 35
    assert defs["BENEFIT_AGE3_PRESCHOOL"].window_type == "SCHOOL_YEAR"
    assert defs["CHECKUP_2"].window_type == "MONTHS"


def test_checkup1_day_window():
    assert milestone_window(BY["CHECKUP_1"], birth_date="2026-01-31") == ("2026-02-14", "2026-03-07")
    assert db_milestone_window("2024-02-29", BY["CHECKUP_1"]) == ("2024-03-14", "2024-04-04")


def test_month_end_safe_windows():
    # 1/31 birth: 18 months -> 2027-07-31; end = (30 months) 2028-07-31 - 1
    assert db_milestone_window("2026-01-31", BY["DENTAL_1"]) == ("2027-07-31", "2028-07-30")
    # 1/31 + 1 month clamps to Feb end
    m = MilestoneDefinition("T@v", "VACCINE", "T", "v", 1, 1, None, "t", "", "")
    assert db_milestone_window("2026-01-31", m) == ("2026-02-28", "2026-03-30")
    # 2/29 leap birth
    assert db_milestone_window("2024-02-29", BY["DENTAL_1"]) == ("2025-08-29", "2026-08-28")
    y12 = MilestoneDefinition("Y@v", "VACCINE", "Y", "v", 12, 12, None, "t", "", "")
    assert db_milestone_window("2024-02-29", y12) == ("2025-02-28", "2025-03-28")


def test_school_year_window():
    assert db_milestone_window("2023-01-15", BY["BENEFIT_AGE3_PRESCHOOL"]) == ("2026-01-01", "2026-03-01")
    assert db_milestone_window("2023-12-31", BY["BENEFIT_AGE3_PRESCHOOL"]) == ("2026-01-01", "2026-03-01")
    assert db_milestone_window(date(2024, 2, 29), BY["BENEFIT_AGE3_PRESCHOOL"]) == ("2027-01-01", "2027-03-01")


# ── D-14 ──────────────────────────────────────────────────────
def _due_to_state(db, cid, code):
    return next(m for m in db.list_subject_milestones(7, subject_id=cid) if m.definition_id.startswith(code + "@"))


def test_d14_boundaries(db):
    cid = db.add_child(7, "콩이", "2026-10-01", 4.0)
    sync_user_milestones(db, 7)
    m = _due_to_state(db, cid, "CHECKUP_1")  # 2026-10-15 ~ 2026-11-05
    due_to = date.fromisoformat(m.due_to)
    assert due_to == date(2026, 11, 5)

    def ids(today):
        return {r["milestone_id"] for r in db.list_d14_milestones(today)}

    assert m.milestone_id not in ids(due_to - timedelta(days=15))  # D-15
    assert m.milestone_id in ids(due_to - timedelta(days=14))      # D-14
    assert m.milestone_id in ids(due_to)                            # D-0
    assert m.milestone_id not in ids(due_to + timedelta(days=1))    # D+1
    row = next(r for r in db.list_d14_milestones(due_to - timedelta(days=3)) if r["milestone_id"] == m.milestone_id)
    assert row["days_left"] == 3 and row["code"] == "CHECKUP_1" and row["title"]


def test_d14_status_filter(db):
    cid = db.add_child(7, "콩이", "2026-10-01", 4.0)
    sync_user_milestones(db, 7)
    m = _due_to_state(db, cid, "CHECKUP_1")
    today = date(2026, 11, 1)
    for status, expected in (("DEFERRED", True), ("UNKNOWN", True), ("USER_CONFIRMED", False),
                             ("NOT_APPLICABLE", False), ("DUE", True)):
        db.set_milestone_status(7, m.milestone_id, status, "USER", NOW)
        assert (m.milestone_id in {r["milestone_id"] for r in db.list_d14_milestones(today)}) is expected, status


# ── hospitals: placeholder protection ─────────────────────────
def _official(hid="sel_seongbuk_woori", name="공식 달빛병원"):
    return MoonlightHospital(hid, name, "서울", "성북구", "서울 성북구 1", "02-000-0000", "평일", "주말", "공휴일",
                             "", "", "", "2026-11-21", is_placeholder=False, source="EGEN")


def test_seed_hospitals_are_placeholders():
    assert SEED_HOSPITALS and all(h.is_placeholder and h.source == "SEED" for h in SEED_HOSPITALS)


def test_seed_never_overwrites_official(db):
    assert db.upsert_hospitals([_official()]) == 1
    assert db.upsert_hospitals(SEED_HOSPITALS) == len(SEED_HOSPITALS) - 1  # the official id is skipped
    h = db.get_hospital("sel_seongbuk_woori")
    assert h.name == "공식 달빛병원" and not h.is_placeholder and h.source == "EGEN" and h.tel == "02-000-0000"
    seed_all(db, date(2026, 11, 22))  # bot restart re-seed
    assert db.get_hospital("sel_seongbuk_woori").name == "공식 달빛병원"
    # official may replace a placeholder, and official may refresh official
    assert db.upsert_hospitals([_official("sel_seocho_gomdori", "공식2")]) == 1
    assert not db.get_hospital("sel_seocho_gomdori").is_placeholder
    assert db.upsert_hospitals([_official(name="공식 갱신")]) == 1
    assert db.get_hospital("sel_seongbuk_woori").name == "공식 갱신"


# ── importer: parsing ─────────────────────────────────────────
@pytest.mark.parametrize("o,c,status,om,cm,cross", [
    ("0900", "1800", "OK", 540, 1080, False),
    ("0000", "2400", "OK", 0, 1440, False),
    ("1800", "0000", "OK", 1080, 1440, False),
    ("1800", "0200", "OK", 1080, 120, True),
    ("2000", "2400", "OK", 1200, 1440, False),
    ("", "", "MISSING", 0, 0, False),
    ("-", "-", "MISSING", 0, 0, False),
    ("0900", "", "INVALID", 540, 0, False),
    ("2400", "0900", "INVALID", 0, 540, False),
    ("0960", "1800", "INVALID", 0, 1080, False),
    ("2500", "0100", "INVALID", 0, 60, False),
    ("abcd", "1800", "INVALID", 0, 1080, False),
    ("0000", "0000", "AMBIGUOUS", 0, 0, False),
    ("0900", "0900", "AMBIGUOUS", 540, 540, False),
])
def test_parse_slot(o, c, status, om, cm, cross):
    s = hi.parse_slot("H", 0, o, c)
    assert (s.parse_status, s.open_minute, s.close_minute, s.crosses_midnight) == (status, om, cm, cross)


def _slots(**by_day):
    rows = []
    for dow, (o, c) in by_day.items():
        idx = int(dow[1:])
        rows.append(hi.parse_slot("H", idx, o, c))
    return rows


def test_is_open_now_basic_and_unknown():
    mon = datetime(2026, 11, 23, 10, 0)  # Monday
    assert mon.weekday() == 0
    rows = _slots(d0=("0900", "1800"))
    assert hi.is_open_now(rows, mon) == "OPEN"
    assert hi.is_open_now(rows, mon.replace(hour=18)) == "CLOSED"  # close is exclusive
    assert hi.is_open_now(rows, mon.replace(hour=8, minute=59)) == "CLOSED"
    assert hi.is_open_now([], mon) == "UNKNOWN"
    assert hi.is_open_now(_slots(d0=("", "")), mon) == "UNKNOWN"
    assert hi.is_open_now(_slots(d0=("0000", "0000")), mon) == "UNKNOWN"
    assert hi.is_open_now(_slots(d1=("0900", "1800")), mon) == "UNKNOWN"  # no Monday slot
    assert hi.is_open_now(_slots(d0=("0000", "2400")), mon.replace(hour=23, minute=59)) == "OPEN"


def test_is_open_now_overnight():
    rows = _slots(d6=("1800", "0200"), d0=("1800", "0200"))  # Sun & Mon 18:00 ~ next day 02:00
    mon_1am = datetime(2026, 11, 23, 1, 0)
    assert hi.is_open_now(rows, mon_1am) == "OPEN"            # Sunday's slot still running
    assert hi.is_open_now(rows, mon_1am.replace(hour=2)) == "CLOSED"
    assert hi.is_open_now(rows, mon_1am.replace(hour=23)) == "OPEN"
    # previous day unparseable -> early morning is UNKNOWN, not CLOSED
    rows2 = _slots(d6=("abcd", "0200"), d0=("1800", "0200"))
    assert hi.is_open_now(rows2, mon_1am) == "UNKNOWN"


def test_is_open_now_holiday():
    rows = _slots(d0=("0900", "1800"), d7=("1000", "1400"))
    mon = datetime(2026, 11, 23, 15, 0)
    assert hi.is_open_now(rows, mon) == "OPEN"
    assert hi.is_open_now(rows, mon, is_holiday=True) == "CLOSED"
    assert hi.is_open_now(_slots(d0=("0900", "1800")), mon, is_holiday=True) == "UNKNOWN"


EGEN_XML = """<?xml version="1.0" encoding="UTF-8"?>
<response><header><resultCode>00</resultCode></header><body><items>
<item><hpid>A1100001</hpid><dutyName>서울달빛소아과</dutyName><dutyAddr>서울특별시 성북구 보문로 1</dutyAddr>
<dutyTel1>02-111-2222</dutyTel1><dutyTime1s>0900</dutyTime1s><dutyTime1c>2300</dutyTime1c>
<dutyTime6s>0900</dutyTime6s><dutyTime6c>1800</dutyTime6c><dutyTime7s>1800</dutyTime7s><dutyTime7c>0200</dutyTime7c>
<dutyTime8s>0000</dutyTime8s><dutyTime8c>2400</dutyTime8c></item>
<item><hpid>A1100002</hpid><dutyName>성북24소아과</dutyName><dutyAddr>서울특별시 성북구 2</dutyAddr>
<dutyTime1s>-</dutyTime1s><dutyTime1c>-</dutyTime1c></item>
<item><hpid></hpid><dutyName>아이디없음</dutyName><dutyAddr>서울특별시 중구</dutyAddr></item>
</items><totalCount>3</totalCount></body></response>"""


def test_import_xml(db):
    res = hi.import_hospitals_payload(db, EGEN_XML, now=NOW)
    assert res["parsed"] == 3 and res["imported"] == 2 and res["skipped"] == 1 and res["regions"] == ["서울"]
    h = db.get_hospital("A1100001")
    assert h.region_sido == "서울" and h.region_sigungu == "성북구" and not h.is_placeholder and h.source == "EGEN"
    assert h.tel == "02-111-2222" and h.last_verified_at == "2026-11-21"
    hours = db.list_hospital_hours("A1100001")
    assert len(hours) == 8
    sun = next(s for s in hours if s.day_of_week == 6)
    assert sun.crosses_midnight and sun.close_minute == 120
    hol = next(s for s in hours if s.is_holiday)
    assert (hol.open_minute, hol.close_minute, hol.parse_status) == (0, 1440, "OK")
    assert hi.is_open_now(hours, datetime(2026, 11, 23, 1, 30)) == "OPEN"  # Mon 01:30 via Sunday night
    assert hi.is_open_now(hours, datetime(2026, 11, 24, 10, 0)) == "UNKNOWN"  # Tue missing
    assert hi.is_open_now(db.list_hospital_hours("A1100002"), datetime(2026, 11, 23, 10, 0)) == "UNKNOWN"


def test_import_json_variants(db):
    payload = ('{"response":{"body":{"items":{"item":{"hpid":"B1","dutyName":"경기달빛","dutyAddr":"경기도 수원시 1",'
               '"dutyTime1s":"1900","dutyTime1c":"0000"}}}}}')
    res = hi.import_hospitals_payload(db, payload)
    assert res["imported"] == 1 and db.get_hospital("B1").region_sido == "경기"
    mon = next(s for s in db.list_hospital_hours("B1") if s.day_of_week == 0)
    assert (mon.open_minute, mon.close_minute, mon.crosses_midnight) == (1140, 1440, False)
    res = hi.import_hospitals_payload(db, '[{"hpid":"B2","dutyName":"인천","dutyAddr":"인천광역시 남동구 2"}]', fmt="json")
    assert res["imported"] == 1 and db.get_hospital("B2").region_sido == "인천"


def _csv_text():
    return ("hpid,dutyName,dutyAddr,dutyTel1,dutyTime1s,dutyTime1c\r\n"
            "C1,부천달빛,경기도 부천시 3,032-1,2000,0100\r\n"
            "C2,고양달빛,경기도 고양시 4,,0960,1800\r\n")


@pytest.mark.parametrize("encoding", ["utf-8-sig", "cp949"])
def test_import_csv_encodings(db, tmp_path, encoding):
    p = tmp_path / "egen.csv"
    p.write_bytes(_csv_text().encode(encoding))
    res = hi.import_hospitals_file(db, p)
    assert res["imported"] == 2 and db.get_hospital("C1").name == "부천달빛"
    c1 = next(s for s in db.list_hospital_hours("C1") if s.day_of_week == 0)
    assert c1.crosses_midnight and c1.close_minute == 60
    c2 = next(s for s in db.list_hospital_hours("C2") if s.day_of_week == 0)
    assert c2.parse_status == "INVALID"


def test_import_csv_korean_headers(db):
    text ="기관ID,기관명,주소,대표전화1,진료시간(월)S,진료시간(월)C\nK1,한글헤더병원,서울특별시 노원구 5,02-9,0900,2100\n"
    res = hi.import_hospitals_payload(db, text.encode("cp949"), fmt="csv")
    assert res["imported"] == 1
    mon = next(s for s in db.list_hospital_hours("K1") if s.day_of_week == 0)
    assert (mon.open_minute, mon.close_minute) == (540, 1260)


def test_imported_official_survives_reseed(db):
    hi.import_hospitals_payload(db, EGEN_XML, now=NOW)
    seed_all(db, date(2026, 11, 22))
    assert not db.get_hospital("A1100001").is_placeholder


def test_replace_snapshot_scoped_to_imported_regions(db):
    hi.import_hospitals_payload(db, EGEN_XML, now=NOW)  # 서울 A1100001, A1100002
    hi.import_hospitals_payload(db, '[{"hpid":"G1","dutyName":"경기","dutyAddr":"경기도 성남시 1"}]')
    # partial 서울 import without A1100002, no replace -> nothing deactivated
    one = '[{"hpid":"A1100001","dutyName":"서울달빛소아과","dutyAddr":"서울특별시 성북구 보문로 1"}]'
    assert hi.import_hospitals_payload(db, one)["deactivated"] == 0
    assert "A1100002" in {h.hospital_id for h in db.list_hospitals("서울")}
    res = hi.import_hospitals_payload(db, one, replace_official_snapshot=True)
    assert res["deactivated"] == 1
    seoul = {h.hospital_id for h in db.list_hospitals("서울")}
    assert "A1100002" not in seoul and "A1100001" in seoul
    assert "sel_seongbuk_woori" in seoul  # placeholders untouched
    assert "G1" in {h.hospital_id for h in db.list_hospitals("경기")}  # other region untouched
    assert db.get_hospital("A1100002") is not None  # deactivated, not deleted
    # empty payload never wipes anything
    assert hi.import_hospitals_payload(db, "[]", replace_official_snapshot=True)["deactivated"] == 0
    assert "A1100001" in {h.hospital_id for h in db.list_hospitals("서울")}
