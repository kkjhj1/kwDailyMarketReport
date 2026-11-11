"""Travel price watchlist & drop alert (L2) — GPT audit blockers 1~8."""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from travel_brief.interactive import (
    STATE_AWAITING_CITY,
    STATE_AWAITING_WATCH_PRICE,
    STATE_IDLE,
    TravelInteractiveHandler,
    dispatch_travel_update,
    parse_watch_price_krw,
)
from travel_brief.models import FlightDeal
from travel_brief.storage.db import (
    TravelBriefDatabase,
    WatchLimitError,
    make_watch_delivery_key,
)
from travel_brief.watch_engine import (
    TICK_LEASE_NAME,
    WatchCheckEngine,
    build_watch_alert_card,
    build_watch_alert_keyboard,
    classify_quote,
)

ROOT = Path(__file__).resolve().parent.parent


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------
def mk_deal(code="FUK", dep="2027-01-14", ret="2027-01-17", price=200_000, source="LIVE_SSE",
            freshness="FRESH", peak=False):
    return FlightDeal(
        destination_code=code, destination_name=code, origin_code="ICN",
        departure_date=dep, return_date=ret, departure_day_of_week="목", return_day_of_week="일",
        nights=3, airline="테스트항공", price_krw=price, baseline_krw=price + 50_000,
        data_source=source, freshness=freshness, is_peak_holiday=peak,
    )


def seed_recipient(db, chat_id, topics="TRAVEL"):
    db.add_recipient(chat_id, name=f"u{chat_id}", topic=topics)


def add_subscriptions_table(db, rows=()):
    with db._connect() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT, telegram_user_id TEXT NOT NULL, bot_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'ACTIVE', subscribed_at TEXT, cancelled_at TEXT,
                UNIQUE(telegram_user_id, bot_id))"""
        )
        for uid, bot, status in rows:
            conn.execute(
                "INSERT INTO subscriptions (telegram_user_id, bot_id, status) VALUES (?, ?, ?)", (uid, bot, status)
            )


def sub_status(db, uid, bot="travel_bot"):
    with db._connect() as conn:
        row = conn.execute(
            "SELECT status FROM subscriptions WHERE telegram_user_id = ? AND bot_id = ?", (uid, bot)
        ).fetchone()
        return row[0] if row else None


class Recorder:
    def __init__(self, status="SENT"):
        self.status = status
        self.calls: list[tuple[str, str, dict]] = []

    def __call__(self, chat_id, text, markup):
        self.calls.append((chat_id, text, markup))
        if isinstance(self.status, Exception):
            raise self.status
        return self.status, 500 + len(self.calls), None if self.status == "SENT" else "err"


def fetcher_from(deals_by_code):
    calls = []

    def fetch(code, custom, max_target):
        calls.append(code)
        return list(deals_by_code.get(code, []))

    fetch.calls = calls
    return fetch


@pytest.fixture
def mem_db():
    db = TravelBriefDatabase(db_path=":memory:")
    yield db
    db.close()


def all_callbacks(markup):
    return [b["callback_data"] for row in markup["inline_keyboard"] for b in row if "callback_data" in b]


# ----------------------------------------------------------------------
# 1. DB concurrency: WAL, atomic max_active, shared :memory:
# ----------------------------------------------------------------------
def test_file_db_wal_and_atomic_limit_under_concurrency(tmp_path):
    db = TravelBriefDatabase(db_path=str(tmp_path / "w.sqlite3"))
    assert db.journal_mode() == "wal"
    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(i):
        try:
            TravelBriefDatabase(db_path=db.db_path).upsert_travel_watch("c1", f"X{i:02d}", 200_000, max_active=10)
            r = "ok"
        except WatchLimitError:
            r = "limit"
        with lock:
            outcomes.append(r)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert outcomes.count("ok") == 10 and outcomes.count("limit") == 10
    assert len(db.list_travel_watches("c1")) == 10


def test_memory_db_shared_connection_across_threads(mem_db):
    errors: list[Exception] = []

    def worker(i):
        try:
            mem_db.upsert_travel_watch(f"chat{i % 3}", f"Y{i:02d}", 100_000 + i, max_active=4)
        except WatchLimitError:
            pass
        except Exception as exc:  # pragma: no cover - would be a real failure
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(18)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    for c in ("chat0", "chat1", "chat2"):
        assert len(mem_db.list_travel_watches(c)) == 4


def test_upsert_rules_reactivation_limit_and_toggles(mem_db):
    with pytest.raises(ValueError):
        mem_db.upsert_travel_watch("c", "ALL", 200_000)
    with pytest.raises(ValueError):
        mem_db.upsert_travel_watch("c", "FUK", 0)
    w = mem_db.upsert_travel_watch("c", "fuk", 200_000)
    assert (w["dest_code"], w["day_pattern"], w["include_peak"], w["preset_kind"]) == ("FUK", "ANY", 1, "DEAL")
    assert mem_db.toggle_travel_watch_option("c", "FUK", "day")["day_pattern"] == "FRI_MON"
    assert mem_db.toggle_travel_watch_option("c", "FUK", "day")["day_pattern"] == "THU_SUN"
    assert mem_db.toggle_travel_watch_option("c", "FUK", "day")["day_pattern"] == "ANY"
    assert mem_db.toggle_travel_watch_option("c", "FUK", "pk")["include_peak"] == 0
    # re-saving a preset keeps the user's toggles
    w = mem_db.upsert_travel_watch("c", "FUK", 180_000, preset_kind="FLOOR")
    assert (w["target_price_krw"], w["include_peak"], w["preset_kind"]) == (180_000, 0, "FLOOR")
    # inactive watch re-activation is limit-checked atomically
    assert mem_db.deactivate_travel_watch("c", "FUK")
    mem_db.upsert_travel_watch("c", "NRT", 200_000, max_active=1)
    with pytest.raises(WatchLimitError):
        mem_db.upsert_travel_watch("c", "FUK", 200_000, max_active=1)
    assert mem_db.toggle_travel_watch_option("c", "FUK", "day") is None  # inactive -> no toggle


# ----------------------------------------------------------------------
# 2. Ledger: watch_id key, lease quarantine, SENT/UNKNOWN block, cooldown
# ----------------------------------------------------------------------
def test_delivery_key_contains_watch_id_and_lease_quarantine(mem_db):
    w1 = mem_db.upsert_travel_watch("c", "FUK", 250_000)
    w2 = mem_db.upsert_travel_watch("c2", "FUK", 250_000)
    k1 = make_watch_delivery_key(w1["watch_id"], "c", "FUK", "2027-01-14", "2027-01-17", 200_000)
    k2 = make_watch_delivery_key(w2["watch_id"], "c", "FUK", "2027-01-14", "2027-01-17", 200_000)
    assert k1 != k2 and f"watch:{w1['watch_id']}:" in k1

    now = datetime.now(timezone.utc)
    c = mem_db.claim_watch_delivery(w1["watch_id"], "c", "FUK", "2027-01-14", "2027-01-17", 200_000, "A",
                                    lease_ttl_seconds=60, now=now)
    assert c.claimed and c.lease_token
    dup = mem_db.claim_watch_delivery(w1["watch_id"], "c", "FUK", "2027-01-14", "2027-01-17", 200_000, "B", now=now)
    assert (dup.claimed, dup.reason) == (False, "duplicate_in_flight")
    # Lease expiry -> UNKNOWN quarantine, never re-claimable
    later = (now + timedelta(seconds=120)).strftime("%Y-%m-%d %H:%M:%S")
    assert mem_db.expire_stale_watch_in_flight(later) == 1
    assert mem_db.get_watch_delivery(c.delivery_key)["status"] == "UNKNOWN"
    again = mem_db.claim_watch_delivery(w1["watch_id"], "c", "FUK", "2027-01-14", "2027-01-17", 200_000, "B",
                                        now=now + timedelta(seconds=130))
    assert (again.claimed, again.reason) == (False, "duplicate_unknown")
    assert not mem_db.finalize_watch_delivery(c.delivery_key, "wrong-token", "SENT")
    assert not mem_db.finalize_watch_delivery(c.delivery_key, c.lease_token, "FAILED")  # no downgrade
    assert mem_db.finalize_watch_delivery(c.delivery_key, c.lease_token, "SENT", provider_msg_id="77")
    row = mem_db.get_watch_delivery(c.delivery_key)
    assert (row["status"], row["provider_msg_id"]) == ("SENT", 77)


def test_cooldown_rules_and_failed_recovery(mem_db):
    w = mem_db.upsert_travel_watch("c", "FUK", 300_000)
    wid = w["watch_id"]
    now = datetime.now(timezone.utc)

    def claim(dep, ret, price, at=now):
        return mem_db.claim_watch_delivery(wid, "c", "FUK", dep, ret, price, "T", now=at)

    first = claim("2027-01-14", "2027-01-17", 250_000)
    assert mem_db.finalize_watch_delivery(first.delivery_key, first.lease_token, "SENT")
    assert claim("2027-01-14", "2027-01-17", 200_000).reason == "slot_cooldown"  # same slot, even cheaper
    assert claim("2027-01-15", "2027-01-18", 241_000).reason == "insufficient_drop"  # < 10,000 drop
    ok = claim("2027-01-15", "2027-01-18", 240_000)  # >= 10,000 drop on an unsent slot
    assert ok.claimed
    mem_db.finalize_watch_delivery(ok.delivery_key, ok.lease_token, "UNKNOWN", error_detail="timed out")
    assert claim("2027-01-21", "2027-01-24", 235_000).reason == "insufficient_drop"  # UNKNOWN counts as sent
    # after 7 days the same slot may alert again
    assert claim("2027-01-14", "2027-01-17", 230_000, at=now + timedelta(days=8)).claimed

    # FAILED (non-ambiguous) may be re-claimed with attempt_no + 1
    w2 = mem_db.upsert_travel_watch("c", "NRT", 300_000)
    f = mem_db.claim_watch_delivery(w2["watch_id"], "c", "NRT", "2027-02-04", "2027-02-07", 250_000, "T")
    mem_db.finalize_watch_delivery(f.delivery_key, f.lease_token, "FAILED", error_detail="429")
    r = mem_db.claim_watch_delivery(w2["watch_id"], "c", "NRT", "2027-02-04", "2027-02-07", 250_000, "T")
    assert r.claimed and r.attempt_no == 2


# ----------------------------------------------------------------------
# 3. Strict LIVE_SSE + FRESH filter, counters, 1/watch/tick
# ----------------------------------------------------------------------
def test_strict_live_sse_fresh_filter_and_counters(mem_db):
    seed_recipient(mem_db, "c")
    mem_db.upsert_travel_watch("c", "FUK", 300_000)
    blocked = [
        mk_deal(price=150_000, source="LIVE_PG"),
        mk_deal(price=160_000, source="LIVE_SSE", freshness="STALE"),
        mk_deal(price=170_000, source="OFFLINE_ESTIMATE"),
    ]
    assert blocked[0].is_live  # LIVE_PG is "live" for display but never pushable
    assert [classify_quote(d) for d in blocked] == ["PG", "STALE", "ESTIMATE"]
    sender = Recorder()
    res = WatchCheckEngine(mem_db, fetcher_from({"FUK": blocked}), sender=sender).run_once()
    assert sender.calls == []
    assert (res.candidate_count, res.sse_fresh_count, res.dropped_pg_count, res.dropped_stale_count,
            res.dropped_estimate_count, res.eligible_watch_count, res.triggered_count, res.sent_count) == \
        (3, 0, 1, 1, 1, 1, 0, 0)

    fresh = blocked + [
        mk_deal(dep="2027-01-15", ret="2027-01-18", price=280_000),
        mk_deal(dep="2027-01-21", ret="2027-01-24", price=260_000),
    ]
    res = WatchCheckEngine(mem_db, fetcher_from({"FUK": fresh}), sender=sender).run_once()
    assert (res.sse_fresh_count, res.triggered_count, res.sent_count) == (2, 1, 1)
    assert len(sender.calls) == 1 and "260,000원" in sender.calls[0][1]  # cheapest, one per watch per tick
    w = mem_db.get_travel_watch("c", "FUK")
    assert (w["last_seen_price_krw"], w["last_seen_slot"]) == (260_000, "2027-01-21~2027-01-24")


def test_day_pattern_peak_filter_and_one_fetch_per_city(mem_db):
    for cid in ("a", "b", "c"):
        seed_recipient(mem_db, cid)
    mem_db.upsert_travel_watch("a", "FUK", 300_000, day_pattern="FRI_MON")
    mem_db.upsert_travel_watch("b", "FUK", 300_000, include_peak=0)
    mem_db.upsert_travel_watch("c", "FUK", 300_000, day_pattern="THU_SUN")
    deals = [
        mk_deal(dep="2026-12-24", ret="2026-12-27", price=150_000),  # Thu-Sun, Christmas peak
        mk_deal(dep="2027-01-15", ret="2027-01-18", price=220_000),  # Fri-Mon
        mk_deal(dep="2027-01-14", ret="2027-01-17", price=210_000),  # Thu-Sun
    ]
    fetch = fetcher_from({"FUK": deals})
    sender = Recorder()
    WatchCheckEngine(mem_db, fetch, sender=sender).run_once()
    assert fetch.calls == ["FUK"]
    got = {chat: text for chat, text, _ in sender.calls}
    assert "220,000원" in got["a"]
    assert "210,000원" in got["b"]  # peak 150,000 excluded
    assert "150,000원" in got["c"] and "연휴" in got["c"]


def test_unknown_and_failed_outcomes_and_dry_run(mem_db):
    seed_recipient(mem_db, "c")
    mem_db.upsert_travel_watch("c", "FUK", 300_000)
    fetch = fetcher_from({"FUK": [mk_deal(price=200_000)]})
    # dry run: read-only, no sender, no ledger/observation writes
    res = WatchCheckEngine(mem_db, fetch, sender=None, dry_run=True).run_once()
    assert [e["status"] for e in res.events] == ["DRY_RUN"]
    assert mem_db.get_travel_watch("c", "FUK")["last_seen_price_krw"] is None
    with mem_db._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM travel_watch_deliveries").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM travel_watch_tick_leases").fetchone()[0] == 0
    # sender exception -> UNKNOWN, never retried
    boom = Recorder(status=TimeoutError("timed out"))
    res = WatchCheckEngine(mem_db, fetch, sender=boom).run_once()
    assert res.unknown_count == 1
    res = WatchCheckEngine(mem_db, fetch, sender=Recorder()).run_once()
    assert (res.sent_count, res.skipped_cooldown_count) == (0, 1)


def test_tick_lease_skips_when_locked_and_quarantines_stale(tmp_path):
    db = TravelBriefDatabase(db_path=str(tmp_path / "lease.sqlite3"))
    seed_recipient(db, "c")
    w = db.upsert_travel_watch("c", "FUK", 300_000)
    stale = db.claim_watch_delivery(w["watch_id"], "c", "FUK", "2027-03-04", "2027-03-07", 280_000, "dead",
                                    lease_ttl_seconds=1, now=datetime.now(timezone.utc) - timedelta(minutes=5))
    assert stale.claimed

    started = threading.Event()
    release = threading.Event()

    def slow_fetch(code, custom, max_target):
        started.set()
        release.wait(5)
        return [mk_deal(price=200_000)]

    sender = Recorder()
    out: dict = {}
    t = threading.Thread(target=lambda: out.setdefault("a", WatchCheckEngine(
        TravelBriefDatabase(db_path=db.db_path), slow_fetch, sender=sender, lease_owner="A").run_once()))
    t.start()
    assert started.wait(5)
    other = WatchCheckEngine(db, fetcher_from({}), sender=sender, lease_owner="B").run_once()
    release.set()
    t.join(10)
    assert other.skipped_locked and other.eligible_watch_count == 0
    assert out["a"].expired_in_flight == 1 and out["a"].sent_count == 1
    assert db.get_watch_delivery(stale.delivery_key)["status"] == "UNKNOWN"
    # lease released after the tick; an expired foreign lease is taken over
    assert db.acquire_watch_tick_lease(TICK_LEASE_NAME, "C") is not None
    assert db.acquire_watch_tick_lease(
        TICK_LEASE_NAME, "D", now=datetime.now(timezone.utc) + timedelta(hours=1)) is not None


def test_subscription_ssot_filter(mem_db):
    for cid, topics in (("ok", "TRAVEL"), ("fin", "FINANCE"), ("off", "TRAVEL"), ("nosub", "TRAVEL"),
                        ("multi", "FINANCE,TRAVEL")):
        seed_recipient(mem_db, cid, topics)
        mem_db.upsert_travel_watch(cid, "FUK", 300_000)
    mem_db.deactivate_recipient("off")
    rows, checked = mem_db.list_deliverable_watches()
    assert not checked and {r["chat_id"] for r in rows} == {"ok", "off", "multi", "nosub"} - {"off"}
    add_subscriptions_table(mem_db, [("ok", "travel_bot", "ACTIVE"), ("multi", "travel_bot", "ACTIVE"),
                                     ("nosub", "travel_bot", "CANCELLED"), ("fin", "travel_bot", "ACTIVE")])
    rows, checked = mem_db.list_deliverable_watches()
    assert checked and {r["chat_id"] for r in rows} == {"ok", "multi"}
    sender = Recorder()
    WatchCheckEngine(mem_db, fetcher_from({"FUK": [mk_deal(price=200_000)]}), sender=sender).run_once()
    assert sorted(c for c, _, _ in sender.calls) == ["multi", "ok"]


def test_alert_card_escaped_short_and_callbacks():
    from travel_brief.city_catalog import CityCatalog
    from travel_brief.flight_engine import FlightEngine
    from travel_bot_runner import fits_tg01

    res = CityCatalog(FlightEngine(live_naver=False).destinations).get("CTS")
    watch = {"dest_code": "CTS", "target_price_krw": 400_000}
    deal = mk_deal(code="CTS", dep="<b>", ret="2027-01-17", price=300_000)
    text = build_watch_alert_card(watch, deal, res, prev_seen=330_000)
    assert "&lt;b&gt;" in text and "30,000원 하락" in text and "100,000원(25.0%)" in text
    assert fits_tg01(text)
    kb = build_watch_alert_keyboard("CTS", mk_deal(code="CTS", price=300_000))
    assert kb["inline_keyboard"][0][0]["url"].startswith("https://m-flight.naver.com/")
    assert all(len(cb.encode()) <= 64 for cb in all_callbacks(kb))
    assert set(all_callbacks(kb)) == {"trv:w:m:CTS", "trv:w:x:CTS"}


# ----------------------------------------------------------------------
# 4. Dispatcher UNKNOWN classification
# ----------------------------------------------------------------------
@pytest.mark.parametrize("exc,expected", [
    (TimeoutError("boom"), "UNKNOWN"),
    (RuntimeError("Read timed out"), "UNKNOWN"),
    (RuntimeError("[Errno 104] Connection reset by peer"), "UNKNOWN"),
    (ConnectionResetError(10054, "An existing connection was forcibly closed"), "UNKNOWN"),
    (RuntimeError("RemoteDisconnected('Remote end closed connection')"), "UNKNOWN"),
    (RuntimeError("Connection aborted."), "UNKNOWN"),
    (RuntimeError("Telegram HTTP 400: Bad Request: can't parse entities"), "FAILED"),
])
def test_send_text_ambiguous_errors_are_unknown(exc, expected):
    from travel_brief.channels.telegram_limiter import SafeTelegramDispatcher, TelegramRateLimiter

    tg = MagicMock()
    tg.send_text.side_effect = exc
    d = SafeTelegramDispatcher(rate_limiter=TelegramRateLimiter(max_global_rate=10_000, min_chat_interval=0))
    status, msg_id, err = d.send_text_to_recipient(tg=tg, chat_id="1", text="x")
    assert (status, msg_id) == (expected, None) and err
    assert tg.send_text.call_count == 1  # never retried


# ----------------------------------------------------------------------
# 5. Interactive callbacks
# ----------------------------------------------------------------------
@pytest.fixture
def handler(mem_db):
    h = TravelInteractiveHandler(bot_token="dummy", db=mem_db)
    h.tg = MagicMock()
    h.tg.send_text.return_value = "900"
    h._submit_ui = lambda fn, *a: fn(*a)
    h.edits = []

    def fake_edit(chat_id, message_id, text, reply_markup=None):
        h.edits.append((message_id, text, reply_markup))
        return True

    h._safe_edit_message = fake_edit
    yield h
    h.shutdown()


def _cb(h, data, chat="c1", mid=10, user="u1"):
    n = h.tg.answer_callback.call_count
    h.handle_callback_query("cq", chat, mid, data, {"id": user})
    assert h.tg.answer_callback.call_count == n + 1  # exactly one immediate ack


def test_keyboard_contract_and_entry_buttons(handler, mem_db):
    kb_all = handler.build_inline_keyboard("ALL")["inline_keyboard"]
    assert kb_all[0][0]["callback_data"] == "trv:r:ALL"
    assert kb_all[-2][0]["callback_data"] == "trv:c:guide"
    assert kb_all[-3][0]["callback_data"] == "trv:w:l"
    kb_fuk = handler.build_inline_keyboard("FUK", chat_id="c1")["inline_keyboard"]
    assert kb_fuk[-3][0] == {"text": "🔔 이 도시 특가 알림 설정", "callback_data": "trv:w:m:FUK"}
    mem_db.upsert_travel_watch("c1", "FUK", 250_000)
    assert handler.build_inline_keyboard("FUK", chat_id="c1")["inline_keyboard"][-3][0]["text"] == "🔔 알림 설정중 (25만 원)"
    for code in sorted(handler.catalog.curated_codes | handler.catalog.fallback_codes):
        for cb in all_callbacks(handler.build_inline_keyboard(code, chat_id="c1")):
            assert len(cb.encode("utf-8")) <= 64 and cb.startswith("trv:")


def test_watch_callback_flow_preset_toggle_delete(handler, mem_db):
    _cb(handler, "trv:w:m:FUK")
    text, markup = handler.tg.send_text.call_args[0][1], handler.tg.send_text.call_args[1]["reply_markup"]
    assert "후쿠오카 특가 알림 설정" in text and "바닥가" in text and "실속 특가" in text and "평균가" in text
    cbs = all_callbacks(markup)
    assert {"trv:w:p:FUK:D", "trv:w:p:FUK:F", "trv:w:i:FUK", "trv:w:l"} <= set(cbs)
    assert not mem_db.list_travel_watches("c1")  # opening the card never creates a watch

    _cb(handler, "trv:w:p:FUK:D", mid=900)
    w = mem_db.get_travel_watch("c1", "FUK")
    refs = handler.watch_reference_prices("FUK")
    assert w["is_active"] == 1 and w["target_price_krw"] == refs["DEAL"] and w["preset_kind"] == "DEAL"
    mid, text, markup = handler.edits[-1]
    assert mid == 900 and "특가 알림이 설정되었습니다" in text
    assert {"trv:w:t:FUK:day", "trv:w:t:FUK:pk", "trv:w:l"} <= set(all_callbacks(markup))

    _cb(handler, "trv:w:t:FUK:day", mid=900)
    _cb(handler, "trv:w:t:FUK:pk", mid=900)
    w = mem_db.get_travel_watch("c1", "FUK")
    assert (w["day_pattern"], w["include_peak"]) == ("FRI_MON", 0)
    assert "금~월" in handler.edits[-1][1] and "제외" in handler.edits[-1][1]

    _cb(handler, "trv:w:p:FUK:F", mid=900)
    assert mem_db.get_travel_watch("c1", "FUK")["target_price_krw"] == refs["FLOOR"]

    _cb(handler, "trv:w:x:FUK", mid=900)
    assert "정말" in handler.edits[-1][1]
    assert all_callbacks(handler.edits[-1][2]) == ["trv:w:X:FUK", "trv:w:l"]
    assert mem_db.get_travel_watch("c1", "FUK")["is_active"] == 1  # step 1 never deletes
    _cb(handler, "trv:w:X:FUK", mid=900)
    assert mem_db.get_travel_watch("c1", "FUK")["is_active"] == 0
    assert "내 특가 알림" in handler.edits[-1][1]
    for _mid, _t, mk in handler.edits:
        assert all(len(cb.encode("utf-8")) <= 64 and cb.startswith("trv:") for cb in all_callbacks(mk))


def test_watch_callbacks_reject_all_and_unknown_and_missing_db(handler, mem_db):
    _cb(handler, "trv:w:p:ALL:D")
    assert "지원하지 않는" in handler.tg.answer_callback.call_args[1]["text"]
    _cb(handler, "trv:w:m:XYZ")
    _cb(handler, "trv:w:p:FUK:Z")
    _cb(handler, "trv:w:zz")
    assert mem_db.list_travel_watches(active_only=False) == []
    no_db = TravelInteractiveHandler(bot_token="dummy")
    no_db.tg = MagicMock()
    no_db.handle_callback_query("cq", "c1", 1, "trv:w:l", {"id": "u1"})
    assert "사용할 수 없습니다" in no_db.tg.answer_callback.call_args[1]["text"]
    no_db.shutdown()


def test_watch_list_quick_add_custom_city_and_limit(handler, mem_db):
    _cb(handler, "trv:w:l")
    text, markup = handler.tg.send_text.call_args[0][1], handler.tg.send_text.call_args[1]["reply_markup"]
    assert "(0/10)" in text and "trv:w:m:NRT" in all_callbacks(markup)
    # FALLBACK (custom) city works through the same catalog
    _cb(handler, "trv:w:p:CTS:D", mid=900)
    w = mem_db.get_travel_watch("c1", "CTS")
    assert w and w["target_price_krw"] == handler.catalog.get("CTS").meta.flight_deal_threshold_krw
    _cb(handler, "trv:w:m:삿포로")
    assert "삿포로" in handler.tg.send_text.call_args[0][1]
    for i, code in enumerate(["NRT", "KIX", "FUK", "TPE", "DAD", "BKK", "OKA", "NGO", "CEB"]):
        mem_db.upsert_travel_watch("c1", code, 200_000 + i)
    _cb(handler, "trv:w:p:HAN:D", mid=900)
    assert mem_db.get_travel_watch("c1", "HAN") is None
    assert "최대 10개" in handler.edits[-1][1]
    assert len(handler.edits[-1][2]["inline_keyboard"]) <= 12


# ----------------------------------------------------------------------
# 6. Input state, TTL, command priority, parser
# ----------------------------------------------------------------------
@pytest.mark.parametrize("raw,expected", [
    ("25", 250_000), ("25만", 250_000), ("25만원", 250_000), ("25.5만", 255_000), ("25.5만원", 255_000),
    (" 25 만 원 ", 250_000), ("250000", 250_000), ("250,000", 250_000), ("250,000원", 250_000),
    ("25만원 이하", 250_000), ("５", 50_000), ("500만", 5_000_000),
    ("2,500", None), ("1,000", None), ("1000", None), ("49,999", None), ("5,000,001", None), ("501만", None),
    ("0", None), ("-25", None), ("4", None), ("25.5", 255_000), ("abc", None), ("25만만", None), ("", None),
])
def test_parse_watch_price_krw(raw, expected):
    assert parse_watch_price_krw(raw) == expected


def test_input_state_isolation_ttl_and_commands(handler, mem_db):
    handler._launch_search = MagicMock()
    _cb(handler, "trv:w:i:FUK", chat="g1", user="u1")
    assert handler.get_chat_state("g1", "u1") == STATE_AWAITING_WATCH_PRICE
    assert handler.get_chat_state("g1", "u2") == STATE_IDLE
    # another member's number is not consumed by u1's prompt
    assert handler.handle_text_message("g1", 1, "u2", "25") == (True, "travel_help_unknown_city")
    assert mem_db.get_travel_watch("g1", "FUK") is None
    # invalid / ambiguous price keeps the prompt alive
    assert handler.handle_text_message("g1", 2, "u1", "2,500") == (True, "travel_watch_price_invalid")
    assert handler.handle_text_message("g1", 2, "u1", "음 글쎄") == (True, "travel_watch_price_invalid")
    assert handler.get_chat_state("g1", "u1") == STATE_AWAITING_WATCH_PRICE
    assert handler.handle_text_message("g1", 3, "u1", "25만") == (True, "travel_watch_saved")
    w = mem_db.get_travel_watch("g1", "FUK")
    assert (w["target_price_krw"], w["preset_kind"]) == (250_000, "CUSTOM")
    assert handler.get_chat_state("g1", "u1") == STATE_IDLE

    # /cancel and any other command clear the prompt first
    _cb(handler, "trv:w:i:NRT", chat="g1", user="u1")
    assert handler.handle_text_message("g1", 4, "u1", "/cancel") == (True, "travel_cancel")
    assert handler.get_chat_state("g1", "u1") == STATE_IDLE
    _cb(handler, "trv:w:i:NRT", chat="g1", user="u1")
    assert handler.handle_text_message("g1", 5, "u1", "/help@KwTravelBot") == (True, "travel_help")
    assert handler.get_chat_state("g1", "u1") == STATE_IDLE
    assert mem_db.get_travel_watch("g1", "NRT") is None

    # a city name escapes the prompt into the normal city search
    _cb(handler, "trv:w:i:NRT", chat="g1", user="u1")
    assert handler.handle_text_message("g1", 6, "u1", "삿포로") == (True, "travel_search_started")
    assert handler.get_chat_state("g1", "u1") == STATE_IDLE
    assert handler._launch_search.call_args[0][2] == "CTS"

    # TTL expiry
    handler.chat_state_ttl = 0.05
    _cb(handler, "trv:w:i:NRT", chat="g1", user="u1")
    time.sleep(0.1)
    assert handler.get_chat_state("g1", "u1") == STATE_IDLE
    assert handler.handle_text_message("g1", 7, "u1", "30") == (True, "travel_help_unknown_city")
    assert mem_db.get_travel_watch("g1", "NRT") is None

    # AWAITING_CITY keeps its legacy chat-level lookup
    handler.chat_state_ttl = 300
    handler.handle_callback_query("cq", "g2", 1, "trv:c:guide", {"id": "u9"})
    assert handler.get_chat_state("g2") == STATE_AWAITING_CITY


# ----------------------------------------------------------------------
# 7. /stop SSOT (standalone + manager), /start never revives watches
# ----------------------------------------------------------------------
def test_standalone_stop_is_transactional_and_start_does_not_revive(tmp_path):
    from travel_bot_runner import make_travel_unsubscribe_hook

    db = TravelBriefDatabase(db_path=str(tmp_path / "s.sqlite3"))
    add_subscriptions_table(db, [("777", "travel_bot", "ACTIVE"), ("888", "travel_bot", "ACTIVE")])
    seed_recipient(db, "777")
    seed_recipient(db, "888", "FINANCE,TRAVEL")
    for cid in ("777", "888"):
        db.upsert_travel_watch(cid, "FUK", 250_000)
        db.upsert_travel_watch(cid, "NRT", 250_000)

    h = TravelInteractiveHandler(bot_token="dummy", db=db)
    h.tg = MagicMock()
    hook = make_travel_unsubscribe_hook(db)
    sub = lambda cid, name: db.add_recipient(cid, name=name, topic="TRAVEL")  # noqa: E731

    def msg(cid, text):
        return {"update_id": 1, "message": {"message_id": 3, "chat": {"id": int(cid), "type": "private"},
                                            "from": {"id": int(cid)}, "text": text}}

    for cid in ("777", "888"):
        assert dispatch_travel_update(h, msg(cid, "/stop"), on_subscribe=sub, on_unsubscribe=hook) == \
            (True, "travel_message_handled")
    recips = {r.chat_id: r for r in db.get_all_recipients()}
    assert recips["777"].is_active is False
    assert recips["888"].is_active is True and recips["888"].subscribed_topics == "FINANCE"
    assert db.list_travel_watches("777") == [] and db.list_travel_watches("888") == []
    assert sub_status(db, "777") == "CANCELLED" and sub_status(db, "888") == "CANCELLED"

    dispatch_travel_update(h, msg("777", "/start"), on_subscribe=sub, on_unsubscribe=hook)
    assert {r.chat_id: r for r in db.get_all_recipients()}["777"].is_active is True
    assert db.list_travel_watches("777") == []  # not revived
    assert all(w["is_active"] == 0 for w in db.list_travel_watches("777", active_only=False))
    h.shutdown()


def test_manager_stop_deactivates_watches_and_router_cancels_subscription(tmp_path):
    from market_brief.multi_bot_manager import DynamicBotManager, MultiBotInteractionRouter, SubscriptionEngine
    from market_brief.storage.db import MarketBriefDatabase

    platform = MarketBriefDatabase(db_path=str(tmp_path / "platform.sqlite3"))
    engine = SubscriptionEngine(platform)
    manager = DynamicBotManager(engine, MultiBotInteractionRouter(engine))
    uid = "4242"
    engine.ensure_user(uid, full_name="여행자")
    assert engine.attempt_subscribe(uid, "travel_bot", full_name="여행자")["success"]
    travel_db = manager._get_travel_db()
    assert travel_db.db_path == platform.db_path
    travel_db.upsert_travel_watch(uid, "FUK", 250_000)
    assert [w["chat_id"] for w in travel_db.list_deliverable_watches()[0]] == [uid]

    def msg(text):
        return {"update_id": 9, "message": {"message_id": 5, "chat": {"id": int(uid), "type": "private"},
                                            "from": {"id": int(uid)}, "text": text}}

    with patch("travel_brief.interactive.TelegramChannel"):
        assert manager._dispatch_travel_bot_update("tok", msg("/stop")) == (False, "travel_stop_delegated")
        assert manager._travel_handler.db is travel_db
        assert travel_db.list_travel_watches(uid) == []
        out = manager.router.handle_interaction("travel_bot", uid, text="/stop")
        assert sub_status(travel_db, uid) == "CANCELLED", out["result_code"]
        assert travel_db.list_deliverable_watches()[0] == []
        manager._dispatch_travel_bot_update("tok", msg("/start"))
        assert travel_db.list_travel_watches(uid) == []  # /start never revives
        # direct /start on travel_bot re-subscribes (subscriptions ACTIVE + TRAVEL recipient active)
        assert sub_status(travel_db, uid) == "ACTIVE"
        old = {w["dest_code"]: w for w in travel_db.list_travel_watches(uid, active_only=False)}
        assert old["FUK"]["is_active"] == 0  # old watch stays deactivated
        # a NEW watch after re-subscribe is deliverable again (only NRT, never the old FUK)
        travel_db.upsert_travel_watch(uid, "NRT", 250_000)
        deliverable = travel_db.list_deliverable_watches()[0]
        assert [(w["chat_id"], w["dest_code"]) for w in deliverable] == [(uid, "NRT")]
        # inline info:unsub:travel_bot callback path also deactivates watches
        cq = {"update_id": 10, "callback_query": {"id": "q", "from": {"id": int(uid)}, "data": "info:unsub:travel_bot",
                                                   "message": {"message_id": 6, "chat": {"id": int(uid)}}}}
        assert manager._dispatch_travel_bot_update("tok", cq) == (False, "not_travel_callback")
        assert travel_db.list_travel_watches(uid) == []
    manager._travel_handler.shutdown()


# ----------------------------------------------------------------------
# 8. Runner --check-watches --demo isolation
# ----------------------------------------------------------------------
def _digest(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def test_runner_check_watches_demo_is_isolated(capsys):
    import travel_bot_runner

    prod = ROOT / "alerts.sqlite3"
    before = (_digest(prod), prod.exists() and prod.stat().st_mtime)
    opened: list[str] = []
    real_connect = sqlite3.connect

    def spy_connect(path, *a, **kw):
        opened.append(str(path))
        return real_connect(path, *a, **kw)

    with patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden in demo")), \
         patch("travel_brief.storage.db.sqlite3.connect", side_effect=spy_connect):
        out = travel_bot_runner.run_watch_check(demo=True)
    assert out["outbox"] == 2
    assert out["first"]["sent_count"] == 2 and out["second"]["sent_count"] == 0
    assert out["first"]["dropped_pg_count"] > 0 and out["first"]["dropped_stale_count"] > 0
    assert opened and set(opened) == {":memory:"}
    assert (_digest(prod), prod.exists() and prod.stat().st_mtime) == before
    assert "DEMO ALERT" in capsys.readouterr().out


def test_run_cron_has_travel_watches_task():
    text = (ROOT / "run_cron.sh").read_text(encoding="utf-8")
    assert "travel_watches)" in text and "travel_bot_runner.py --check-watches --live-naver" in text
