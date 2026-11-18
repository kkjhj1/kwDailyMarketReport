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
    DORMANT_BANNER,
    DORMANT_NOTICE,
    STATE_AWAITING_CITY,
    STATE_AWAITING_WATCH_CITY,
    STATE_AWAITING_WATCH_PRICE,
    STATE_IDLE,
    WATCH_LIMIT_NOTICE,
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
    assert "[맞춤발송 설정]" in handler.edits[-1][1]
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
    assert "(0/4)" in text and "trv:w:m:NRT" in all_callbacks(markup)
    # FALLBACK (custom) city works through the same catalog
    _cb(handler, "trv:w:p:CTS:D", mid=900)
    w = mem_db.get_travel_watch("c1", "CTS")
    assert w and w["target_price_krw"] == handler.catalog.get("CTS").meta.flight_deal_threshold_krw
    _cb(handler, "trv:w:m:삿포로")
    assert "삿포로" in handler.tg.send_text.call_args[0][1]
    for i, code in enumerate(["NRT", "KIX", "FUK"]):
        mem_db.upsert_travel_watch("c1", code, 200_000 + i)
    _cb(handler, "trv:w:p:HAN:D", mid=900)
    assert mem_db.get_travel_watch("c1", "HAN") is None
    assert "최대 4개" in handler.edits[-1][1] and WATCH_LIMIT_NOTICE in handler.edits[-1][1]
    assert "trv:w:g" not in all_callbacks(handler.edits[-1][2])  # no add entry at the cap
    assert len(handler.edits[-1][2]["inline_keyboard"]) <= 12
    # editing an existing watch at the cap is still allowed
    _cb(handler, "trv:w:p:NRT:F", mid=900)
    assert mem_db.get_travel_watch("c1", "NRT")["target_price_krw"] == handler.watch_reference_prices("NRT")["FLOOR"]


def test_watch_cap_grandfathers_existing_users_without_hiding_or_deleting(handler, mem_db):
    # a pre-cap user holding 6 active watches (inserted with the old, larger cap)
    legacy = ["NRT", "KIX", "FUK", "TPE", "DAD", "BKK"]
    for i, code in enumerate(legacy):
        mem_db.upsert_travel_watch("c1", code, 200_000 + i, max_active=10)
    handler._remember_watch_card("c1", 900)
    text, markup = handler.build_watch_list_card("c1")
    assert "(6/4)" in text and WATCH_LIMIT_NOTICE in text
    cbs = all_callbacks(markup)
    for code in legacy:  # every watch stays visible / editable / deletable (never sliced)
        assert f"trv:w:m:{code}" in cbs and f"trv:w:x:{code}" in cbs
    assert "trv:w:g" not in cbs
    # every new-addition entry is blocked with the grandfathering notice
    for data in ("trv:w:g", "trv:w:rg", "trv:w:rg:JP:0", "trv:w:ct:JP:0:CTS", "trv:w:ki"):
        _cb(handler, data, mid=900)
        assert WATCH_LIMIT_NOTICE in handler.edits[-1][1], data
    assert handler.get_chat_state("c1", "u1") == STATE_IDLE
    _cb(handler, "trv:w:p:CTS:D", mid=900)
    assert mem_db.get_travel_watch("c1", "CTS") is None
    # existing watches can still be edited and deleted
    _cb(handler, "trv:w:t:FUK:day", mid=900)
    assert mem_db.get_travel_watch("c1", "FUK")["day_pattern"] == "FRI_MON"
    _cb(handler, "trv:w:X:BKK", mid=900)
    assert len(mem_db.list_travel_watches("c1")) == 5
    assert [w["is_active"] for w in mem_db.list_travel_watches("c1", active_only=False)].count(1) == 5
    _assert_callbacks_fit(handler)


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


# ======================================================================
# V2: country scopes / horizons / weekday options / fixed dates / DAILY cadence
# ======================================================================
from datetime import date  # noqa: E402

from travel_brief import naver_live as nl  # noqa: E402
from travel_brief.city_catalog import (  # noqa: E402
    CityCatalog,
    get_watch_country_meta,
    is_country_watch_code,
    list_watch_countries,
    resolve_watch_target_codes,
)
from travel_brief.flight_engine import (  # noqa: E402
    FlightEngine,
    build_fixed_neighbor_slots,
    generate_watch_slots,
    kst_today,
    validate_fixed_dates,
)
from travel_brief.interactive import STATE_AWAITING_WATCH_DATES, parse_watch_date_range  # noqa: E402
from travel_brief.storage.db import make_watch_daily_key  # noqa: E402
from travel_brief.watch_engine import KST, WatchFetchSpec, make_live_quote_fetcher  # noqa: E402

LEGACY_WATCH_DDL = """CREATE TABLE travel_price_watches (
    watch_id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id TEXT NOT NULL, dest_code TEXT NOT NULL,
    target_price_krw INTEGER NOT NULL, preset_kind TEXT NOT NULL DEFAULT 'DEAL',
    day_pattern TEXT NOT NULL DEFAULT 'ANY', include_peak INTEGER NOT NULL DEFAULT 1,
    is_active INTEGER NOT NULL DEFAULT 1, last_seen_price_krw INTEGER, last_seen_slot TEXT, last_seen_at TEXT,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(chat_id, dest_code))"""
LEGACY_DELIVERY_DDL = """CREATE TABLE travel_watch_deliveries (
    delivery_key TEXT PRIMARY KEY, watch_id INTEGER NOT NULL, chat_id TEXT NOT NULL, dest_code TEXT NOT NULL,
    dep_date TEXT NOT NULL, ret_date TEXT NOT NULL, price_krw INTEGER NOT NULL, status TEXT NOT NULL,
    lease_token TEXT, lease_owner TEXT, lease_expires_at TEXT, attempt_no INTEGER NOT NULL DEFAULT 1,
    provider_msg_id INTEGER, error_detail TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"""


def kst_clock(y, m, d, hh, mm=0):
    return lambda: datetime(y, m, d, hh, mm, tzinfo=KST).astimezone(timezone.utc)


def mk_live(code="FUK", dep="2027-01-14", ret="2027-01-17", price=200_000, source="LIVE_SSE", freshness="FRESH",
            airline="제주항공", dep_time="", ret_time="", arr_time="", nights=None):
    d0, d1 = date.fromisoformat(dep), date.fromisoformat(ret)
    dow = ["월", "화", "수", "목", "금", "토", "일"]
    return FlightDeal(
        destination_code=code, destination_name=code, origin_code="ICN", departure_date=dep, return_date=ret,
        departure_day_of_week=dow[d0.weekday()], return_day_of_week=dow[d1.weekday()],
        nights=(d1 - d0).days if nights is None else nights, airline=airline, price_krw=price,
        baseline_krw=price + 50_000, data_source=source, freshness=freshness,
        exact_dep_time=dep_time, exact_ret_time=ret_time, exact_ret_arr_time=arr_time,
    )


def fixed_fetcher_from(deals_by_key):
    calls = []

    def fetch(code, custom, dep, ret, include_neighbors):
        calls.append((code, dep, ret, include_neighbors))
        return list(deals_by_key.get((code, dep, ret), []))

    fetch.calls = calls
    return fetch


def spec_fetcher_from(deals_by_code):
    calls = []

    def fetch(code, custom, max_target, spec=None):
        calls.append((code, spec))
        return list(deals_by_code.get(code, []))

    fetch.accepts_spec = True
    fetch.calls = calls
    return fetch


def ledger_rows(db, watch_id):
    with db._connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM travel_watch_deliveries WHERE watch_id = ? ORDER BY created_at, delivery_key",
            (watch_id,)).fetchall()]


# ----------------------------------------------------------------------
# V2-1. Idempotent additive migration, FIXED pre-fetch expiry, atomic limit
# ----------------------------------------------------------------------
def test_v2_migration_is_additive_idempotent_and_backfills_legacy_rows(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(LEGACY_WATCH_DDL)
    conn.execute(LEGACY_DELIVERY_DDL)
    conn.execute("""INSERT INTO travel_price_watches (chat_id, dest_code, target_price_krw, created_at, updated_at)
                    VALUES ('c1', 'FUK', 250000, '2026-11-01 00:00:00', '2026-11-01 00:00:00')""")
    conn.execute("""INSERT INTO travel_watch_deliveries (delivery_key, watch_id, chat_id, dest_code, dep_date,
                    ret_date, price_krw, status, created_at, updated_at) VALUES
                    ('watch:1:c1:FUK:2027-01-14:2027-01-17:200000', 1, 'c1', 'FUK', '2027-01-14', '2027-01-17',
                     200000, 'SENT', '2026-11-01 00:00:00', '2026-11-01 00:00:00')""")
    conn.commit()
    conn.close()

    db = TravelBriefDatabase(db_path=str(path))
    with db._connect() as c:
        wcols = {r[1] for r in c.execute("PRAGMA table_info(travel_price_watches)")}
        dcols = {r[1] for r in c.execute("PRAGMA table_info(travel_watch_deliveries)")}
    assert {"watch_mode", "horizon_months", "weekday_option", "fixed_dep_date", "fixed_ret_date",
            "notify_mode", "initial_price_krw", "last_daily_sent_date"} <= wcols
    assert {"delivery_kind", "kst_date"} <= dcols
    legacy = db.get_travel_watch("c1", "FUK")
    # pre-upgrade rows keep their exact legacy scope (6-month weekend), cadence DROP, FLEX
    assert (legacy["watch_mode"], legacy["horizon_months"], legacy["weekday_option"], legacy["notify_mode"],
            legacy["fixed_dep_date"], legacy["target_price_krw"]) == ("FLEX", 6, "WEEKEND", "DROP", None, 250_000)
    assert db.get_watch_delivery("watch:1:c1:FUK:2027-01-14:2027-01-17:200000")["delivery_kind"] == "DROP"

    # second init on the migrated file: no error, no re-backfill, data intact
    db2 = TravelBriefDatabase(db_path=str(path))
    probe = db2._open_file_conn()
    try:
        assert TravelBriefDatabase._migrate_travel_watch_schema(probe) == {
            "travel_price_watches": [], "travel_watch_deliveries": []}
    finally:
        probe.close()
    new = db2.upsert_travel_watch("c2", "NRT", 300_000)
    assert (new["horizon_months"], new["weekday_option"], new["watch_mode"]) == (3, "ANY", "FLEX")
    assert db2.get_travel_watch("c1", "FUK")["horizon_months"] == 6
    # legacy-key duplicate protection still works on the migrated ledger
    dup = db2.claim_watch_delivery(1, "c1", "FUK", "2027-01-14", "2027-01-17", 200_000, "T")
    assert (dup.claimed, dup.reason) == (False, "duplicate_sent")


def test_v2_upsert_validation_country_scope_and_atomic_limit(mem_db):
    w = mem_db.upsert_travel_watch("c", "@jp", 250_000, watch_mode="FLEX", horizon_months=12,
                                   weekday_option="WEEKEND", notify_mode="DAILY")
    assert (w["dest_code"], w["horizon_months"], w["weekday_option"], w["notify_mode"]) == ("@JP", 12, "WEEKEND", "DAILY")
    with pytest.raises(ValueError):
        mem_db.upsert_travel_watch("c", "FUK", 250_000, watch_mode="FIXED")  # FIXED needs dates
    with pytest.raises(ValueError):
        mem_db.upsert_travel_watch("c", "FUK", 250_000, horizon_months=2)
    with pytest.raises(ValueError):
        mem_db.upsert_travel_watch("c", "FUK", 250_000, weekday_option="SAT")
    with pytest.raises(ValueError):
        mem_db.upsert_travel_watch("c", "FUK", 250_000, watch_mode="FIXED",
                                   fixed_dep_date="2027-01-17", fixed_ret_date="2027-01-14")
    f = mem_db.upsert_travel_watch("c", "FUK", 250_000, watch_mode="FIXED",
                                   fixed_dep_date="2027-01-14", fixed_ret_date="2027-01-18", initial_price_krw=270_000)
    assert (f["watch_mode"], f["fixed_dep_date"], f["fixed_ret_date"], f["initial_price_krw"]) == \
        ("FIXED", "2027-01-14", "2027-01-18", 270_000)
    # legacy re-save keeps V2 options; switching to FLEX clears the dates and resets the anchor
    assert mem_db.upsert_travel_watch("c", "FUK", 240_000, preset_kind="CUSTOM")["watch_mode"] == "FIXED"
    flex = mem_db.upsert_travel_watch("c", "FUK", 240_000, watch_mode="FLEX")
    assert (flex["fixed_dep_date"], flex["fixed_ret_date"], flex["initial_price_krw"]) == (None, None, None)
    # the WATCH_MAX_ACTIVE_PER_CHAT (4) limit is shared by city / country / fixed watches
    # (@JP + FUK + 2 = 4)
    for i in range(2):
        mem_db.upsert_travel_watch("c", f"Z{i:02d}", 200_000, watch_mode="FIXED",
                                   fixed_dep_date="2027-02-01", fixed_ret_date="2027-02-04")
    with pytest.raises(WatchLimitError):
        mem_db.upsert_travel_watch("c", "@VN", 200_000)
    with pytest.raises(WatchLimitError):
        mem_db.upsert_travel_watch("c", "Z99", 200_000, watch_mode="FIXED",
                                   fixed_dep_date="2027-02-01", fixed_ret_date="2027-02-04")
    assert len(mem_db.list_travel_watches("c")) == 4


def test_fixed_watch_prefetch_expiry_and_dry_run_is_read_only(mem_db):
    seed_recipient(mem_db, "c")
    past = mem_db.upsert_travel_watch("c", "FUK", 300_000, watch_mode="FIXED",
                                      fixed_dep_date="2027-02-28", fixed_ret_date="2027-03-03")
    live = mem_db.upsert_travel_watch("c", "NRT", 300_000, watch_mode="FIXED",
                                      fixed_dep_date="2027-03-10", fixed_ret_date="2027-03-13")
    fetch = fixed_fetcher_from({
        ("NRT", "2027-03-10", "2027-03-13"): [mk_live("NRT", "2027-03-10", "2027-03-13", 250_000)],
        ("FUK", "2027-02-28", "2027-03-03"): [mk_live("FUK", "2027-02-28", "2027-03-03", 100_000)],
    })
    clock = kst_clock(2027, 3, 1, 12, 30)
    # dry run: the expired watch is skipped in memory but never written
    res = WatchCheckEngine(mem_db, fetcher_from({}), sender=None, dry_run=True, now_fn=clock,
                           fixed_quote_fetcher=fetch).run_once()
    assert res.expired_fixed_count == 0 and res.eligible_watch_count == 1
    assert mem_db.get_travel_watch("c", "FUK")["is_active"] == 1
    assert [c[0] for c in fetch.calls] == ["NRT"]
    fetch.calls.clear()
    sender = Recorder()
    res = WatchCheckEngine(mem_db, fetcher_from({}), sender=sender, now_fn=clock, fixed_quote_fetcher=fetch).run_once()
    assert res.expired_fixed_count == 1 and res.eligible_watch_count == 1
    assert mem_db.get_travel_watch("c", "FUK")["is_active"] == 0
    assert [c[0] for c in fetch.calls] == ["NRT"]  # the expired watch is never fetched
    assert [c for c, _, _ in sender.calls] == ["c"] and "ICN→NRT" in sender.calls[0][1]
    assert ledger_rows(mem_db, past["watch_id"]) == []
    assert ledger_rows(mem_db, live["watch_id"])[0]["delivery_key"].startswith("travel-watch:v2:")


# ----------------------------------------------------------------------
# V2-2. Country allowlist, slot generation, cache isolation, PG chunking
# ----------------------------------------------------------------------
def test_country_scope_allowlist_resolution():
    assert resolve_watch_target_codes("@JP") == ("NRT", "KIX", "FUK", "CTS", "OKA")
    assert resolve_watch_target_codes("@vn") == ("DAD", "CXR", "HAN", "SGN", "PQC")
    assert resolve_watch_target_codes("@XX") == () and resolve_watch_target_codes("FUK") == ("FUK",)
    assert is_country_watch_code("@TH") and not is_country_watch_code("BKK")
    assert get_watch_country_meta("@PH")["label"] == "🇵🇭 필리핀 전체"
    cat = CityCatalog(FlightEngine(live_naver=False).destinations)
    for meta in list_watch_countries():
        assert cat.watch_target_codes(meta["code"]) == meta["codes"]  # every member exists in the catalog
    assert [m["code"] for m in list_watch_countries()] == ["@JP", "@VN", "@TH", "@TW", "@PH"]
    assert cat.resolve_country("일본") == "@JP" and cat.resolve_country("베트남 전체") == "@VN"
    assert cat.resolve_country("대만") is None  # the city alias (TPE) wins
    assert cat.resolve_country("도쿄") is None and cat.resolve_country("@tw") == "@TW"


def test_watch_slot_generation_weekday_options_and_fixed_helpers():
    ref = date(2026, 11, 15)
    pats = {opt: {(d.weekday(), n) for d, _r, _a, _b, n in generate_watch_slots(ref, 6, opt)}
            for opt in ("WEEKEND", "WEEKDAY", "ANY")}
    assert pats["WEEKEND"] == {(3, 3), (4, 3)} and pats["WEEKDAY"] == {(0, 3), (1, 3)}
    assert pats["ANY"] == pats["WEEKEND"] | pats["WEEKDAY"]
    one = generate_watch_slots(ref, 1, "WEEKEND")
    assert min(s[0] for s in one) > ref and max(s[0] for s in one) <= date(2026, 12, 15)
    twelve = generate_watch_slots(ref, 12, "ANY")
    assert max(s[0] for s in twelve) <= date(2027, 11, 15) and 200 <= len(twelve) <= 212
    assert all(s[1] == s[0] + timedelta(days=3) for s in twelve)
    # the legacy weekend generator is untouched (Thu->Sun / Fri->Mon, 30-day months)
    legacy = FlightEngine(live_naver=False).generate_weekend_slots(ref_date=ref, months_ahead=6)
    assert {(s[2], s[3]) for s in legacy} == {("목", "일"), ("금", "월")}
    assert max(s[0] for s in legacy) <= ref + timedelta(days=180)
    # fixed-date rules
    assert validate_fixed_dates(date(2026, 11, 20), date(2026, 11, 23), ref) is None
    assert validate_fixed_dates(date(2026, 11, 14), date(2026, 11, 17), ref)
    assert validate_fixed_dates(date(2026, 11, 20), date(2026, 11, 20), ref)
    assert validate_fixed_dates(date(2026, 11, 20), date(2026, 12, 21), ref)  # 31 nights
    assert validate_fixed_dates(date(2026, 11, 20), date(2026, 12, 20), ref) is None  # 30 nights
    nb = build_fixed_neighbor_slots(ref, ref + timedelta(days=3), today=ref)
    assert [(s[0], s[1]) for s in nb] == [(ref + timedelta(days=1), ref + timedelta(days=4))]  # -1d is past
    assert FlightEngine(live_naver=False).fetch_fixed_quotes("FUK", "2027-01-14", "2027-01-17") == []  # never guessed


def _clear_live_caches():
    with nl._CACHE_LOCK:
        nl._MEM_CACHE.clear()
        nl._FAIL_CACHE.clear()
        nl._IN_FLIGHT.clear()
        nl._STALE_BACKUP.clear()


@pytest.fixture
def clean_live_client(tmp_path):
    _clear_live_caches()
    yield nl.NaverFlightLiveClient(cache_path=tmp_path / "cache.json")
    _clear_live_caches()


def test_live_cache_key_isolation_between_weekday_options(clean_live_client):
    client = clean_live_client
    engine = FlightEngine(live_naver=False)
    dests = {"FUK": engine.destinations["FUK"]}
    ref = date(2026, 11, 15)
    calls = []

    def fake_collect(destinations, slots, holiday_fn, fast_mode=False, deadline=None):
        calls.append({(s[0].weekday(), s[4]) for s in slots})
        d0 = slots[0]
        return [mk_live("FUK", d0[0].isoformat(), d0[1].isoformat(), 200_000 + len(calls))]

    def fetch(opt, signature=True):
        slots = generate_watch_slots(ref, 6, opt)
        sig = FlightEngine.watch_slot_signature(6, opt) if signature else None
        return client.fetch_all_weekend_deals(dests, slots, engine.get_peak_holiday_info, ref_date=ref,
                                              months_ahead=6, fast_mode=True, slot_signature=sig)

    with patch.object(client, "_collect_live_deals_uncached", side_effect=fake_collect):
        we, wd, an = fetch("WEEKEND"), fetch("WEEKDAY"), fetch("ANY")
        assert len(calls) == 3 and calls[0] == {(3, 3), (4, 3)} and calls[1] == {(0, 3), (1, 3)}
        assert {we[0].price_krw, wd[0].price_krw, an[0].price_krw} == {200_001, 200_002, 200_003}
        assert fetch("WEEKEND")[0].price_krw == 200_001 and len(calls) == 3  # same namespace -> cache hit
        # implicit namespace: weekday slots without a signature never reuse a weekend entry
        fetch("WEEKDAY", signature=False)
        assert len(calls) == 4
    refs = {k[0] for k in nl._MEM_CACHE}
    assert {"2026-11-15#wWEEKEND6#fast", "2026-11-15#wWEEKDAY6#fast", "2026-11-15#wANY6#fast"} <= refs
    for k in list(nl._MEM_CACHE):
        assert nl._parse_disk_key(nl._disk_key_for(k)) == k  # disk keys stay parseable
    # legacy weekend calls without a signature keep the legacy key shape
    assert nl.slot_signature_for(engine.generate_weekend_slots(ref_date=ref, months_ahead=6)) == ""


def test_pg_chunking_max_32_slots_for_12_month_horizon(clean_live_client):
    client = clean_live_client
    slots = generate_watch_slots(date(2026, 11, 15), 12, "ANY")
    sizes = []

    def fake_post(query, timeout):
        n = query.count("priceGraph(")
        sizes.append(n)
        return {"data": {f"s{i}": {"averageLowestFare": 250_000, "fares": [{"lowestFare": 200_000 + i}]}
                         for i in range(n)}}

    with patch.object(client, "_post_pg_query", side_effect=fake_post):
        out = client.fetch_city_batch_pg("FUK", slots)
    assert nl.MAX_PG_SLOTS_PER_QUERY == 32 and max(sizes) <= 32
    assert len(sizes) == -(-len(slots) // 32) and sum(sizes) == len(slots) == len(out)
    assert out[(slots[32][0].isoformat(), slots[32][1].isoformat())][0] == 200_000  # alias index per chunk

    # one failing chunk keeps the rest; all chunks failing re-raises; an exhausted deadline makes no call
    state = {"n": 0}

    def flaky(query, timeout):
        state["n"] += 1
        if state["n"] == 2:
            raise TimeoutError("chunk 2")
        return fake_post(query, timeout)

    with patch.object(client, "_post_pg_query", side_effect=flaky):
        assert len(client.fetch_city_batch_pg("FUK", slots)) == len(slots) - 32
    with patch.object(client, "_post_pg_query", side_effect=TimeoutError("down")):
        with pytest.raises(TimeoutError):
            client.fetch_city_batch_pg("FUK", slots[:10])
    with patch.object(client, "_post_pg_query", side_effect=AssertionError("no call")) as spy:
        assert client.fetch_city_batch_pg("FUK", slots, deadline=time.monotonic() - 1) == {}
        assert spy.call_count == 0


def test_fixed_date_sse_only_verified_and_live_fetcher_spec(clean_live_client):
    client = clean_live_client
    engine = FlightEngine(live_naver=False)
    dest = engine.destinations["FUK"]
    dep = kst_today() + timedelta(days=40)
    slots = [(dep, dep + timedelta(days=3), "x", "y", 3),
             (dep + timedelta(days=1), dep + timedelta(days=4), "x", "y", 3)]
    sse = {"cheapest": {"airline": "제주항공", "dep_time": "08:00", "ret_dep_time": "20:00", "ret_arr_time": "22:10",
                        "next_day_arrival": False, "min_card": 210_000, "min_general": 215_000}, "popular": None}
    with patch.object(client, "_fetch_session_cookie_header", return_value=""), \
         patch.object(client, "fetch_live_sse_slot", side_effect=[sse, None]):
        out = client.fetch_fixed_date_quotes(dest, slots, engine.get_peak_holiday_info)
    assert [(d.departure_date, d.data_source, d.freshness, d.price_krw) for d in out] == \
        [(dep.isoformat(), "LIVE_SSE", "FRESH", 210_000)]  # the unverified neighbour is simply absent
    with patch.object(client, "fetch_live_sse_slot", side_effect=AssertionError("no call")):
        assert client.fetch_fixed_date_quotes(dest, slots, engine.get_peak_holiday_info,
                                              deadline=time.monotonic() - 1) == []

    eng = MagicMock()
    eng.live_naver = False
    eng.search_watch_deals.return_value = [mk_live(price=1)]
    fetch = make_live_quote_fetcher(eng)
    assert fetch.accepts_spec and callable(fetch.fetch_fixed)
    fetch("FUK", None, 300_000, WatchFetchSpec(12, "WEEKDAY", 1))
    kw = eng.search_watch_deals.call_args.kwargs
    assert (kw["horizon_months"], kw["weekday_option"]) == (12, "WEEKDAY") and kw["live_deadline"] > time.monotonic()
    fetch("FUK", None, 300_000)  # the legacy call keeps the 6-month weekend search
    eng.search_deals.assert_called_once_with(dest_code="FUK", months_ahead=6, fast_live=True)


# ----------------------------------------------------------------------
# V2-3. Interactive wizard callbacks, date input state, legacy contracts
# ----------------------------------------------------------------------
def _all_markups(h):
    out = [m for _mid, _t, m in h.edits if m]
    out += [c.kwargs.get("reply_markup") for c in h.tg.send_text.call_args_list if c.kwargs.get("reply_markup")]
    return out


def _assert_callbacks_fit(h):
    for mk in _all_markups(h):
        for cb in all_callbacks(mk):
            assert cb.startswith("trv:") and len(cb.encode("utf-8")) <= 64, cb


def test_v2_country_flex_wizard_callbacks(handler, mem_db):
    _cb(handler, "trv:w:g")
    text, markup = handler.tg.send_text.call_args[0][1], handler.tg.send_text.call_args[1]["reply_markup"]
    assert "[맞춤발송 등록 · 1/2단계: 도시 선택]" in text
    cbs = set(all_callbacks(markup))
    assert {f"trv:w:rg:{r}:0" for r in ("JP", "TWHK", "CNMN", "SEASIA", "PAC", "OCE", "NA", "EU")} <= cbs
    assert {"trv:w:l", "trv:m:home"} <= cbs and "trv:w:ki" not in cbs
    assert handler.get_chat_state("c1", "u1") == STATE_AWAITING_WATCH_CITY
    assert handler.handle_text_message("c1", 10, "u1", "밴쿠버") == (True, "travel_watch_city_selected")
    assert not any("@" in cb for cb in cbs)  # legacy country scopes are never offered for new registration
    # a chat without an existing country watch can never start one (read-only compatibility)
    _cb(handler, "trv:w:s:@JP", mid=900)
    assert "신규 등록이 종료" in handler.edits[-1][1]
    _cb(handler, "trv:w:k:@JP:1", mid=900)
    assert mem_db.get_travel_watch("c1", "@JP") is None
    # a legacy @JP watch stays editable through the V2 hub
    mem_db.upsert_travel_watch("c1", "@JP", 300_000)
    _cb(handler, "trv:w:s:@JP", mid=900)
    _mid, text, markup = handler.edits[-1]
    assert "일본 전체" in text and "도쿄" in text and "5곳 중 최저가" in text and "참고 시세" in text
    assert {"trv:w:m2:@JP:F", "trv:w:m2:@JP:X", "trv:w:h:@JP:12", "trv:w:d:@JP:D", "trv:w:n:@JP:Y",
            "trv:w:k:@JP:1", "trv:w:i:@JP"} <= set(all_callbacks(markup))
    for h in (1, 3, 6, 12):
        _cb(handler, f"trv:w:h:@JP:{h}", mid=900)
        assert f"앞으로 {h}개월" in handler.edits[-1][1]
        assert f"✅ {h}개월" in [b["text"] for row in handler.edits[-1][2]["inline_keyboard"] for b in row]
    assert "미오픈" in handler.edits[-1][1]  # honest 12M disclosure
    for code, label in (("W", "주말(목~일·금~월)"), ("D", "평일(월~목·화~금)"), ("A", "주말+평일 전체")):
        _cb(handler, f"trv:w:d:@JP:{code}", mid=900)
        assert label in handler.edits[-1][1]
    _cb(handler, "trv:w:n:@JP:Y", mid=900)
    assert "매일 아침 7:30" in handler.edits[-1][1]
    stored = mem_db.get_travel_watch("c1", "@JP")  # nothing persisted before an explicit save
    assert (stored["target_price_krw"], stored["notify_mode"]) == (300_000, "DROP")
    # another user in the same chat has an independent draft
    _cb(handler, "trv:w:s:@JP", mid=901, user="u2")  # untracked message -> fresh card
    assert "앞으로 3개월" in handler.tg.send_text.call_args[0][1]

    _cb(handler, "trv:w:k:@JP:1", mid=900)
    w = mem_db.get_travel_watch("c1", "@JP")
    refs = handler.watch_reference_prices("@JP")
    assert (w["watch_mode"], w["horizon_months"], w["weekday_option"], w["notify_mode"], w["target_price_krw"]) == \
        ("FLEX", 12, "ANY", "DAILY", refs["DEAL"])
    assert refs["DEAL"] == min(handler.watch_reference_prices(c)["DEAL"] for c in ("NRT", "KIX", "FUK", "CTS", "OKA"))
    assert "특가 알림이 설정되었습니다" in handler.edits[-1][1] and "국가 전체" in handler.edits[-1][1]
    assert "trv:w:e:@JP" in all_callbacks(handler.edits[-1][2])

    # list card: country settings route to the hub; delete confirm works for countries
    _cb(handler, "trv:w:l", mid=900)
    cbs = all_callbacks(handler.edits[-1][2])
    assert {"trv:w:s:@JP", "trv:w:x:@JP", "trv:w:g"} <= set(cbs) and "일본 전체" in handler.edits[-1][1]
    _cb(handler, "trv:w:e:@JP", mid=900)
    assert "앞으로 12개월" in handler.edits[-1][1] and "✅ 현재 알림" in handler.edits[-1][1]
    _cb(handler, "trv:w:x:@JP", mid=900)
    assert all_callbacks(handler.edits[-1][2]) == ["trv:w:X:@JP", "trv:w:l"]
    _cb(handler, "trv:w:X:@JP", mid=900)
    assert mem_db.get_travel_watch("c1", "@JP")["is_active"] == 0

    # malformed V2 callbacks are rejected with one ack and no writes
    for bad in ("trv:w:h:@JP:2", "trv:w:d:@JP:Z", "trv:w:s:@XX", "trv:w:k:ALL:1", "trv:w:m2:FUK:Q", "trv:w:n:FUK"):
        _cb(handler, bad)
        assert "지원하지 않는" in handler.tg.answer_callback.call_args[1]["text"]
    assert mem_db.get_travel_watch("c1", "FUK") is None
    _assert_callbacks_fit(handler)


def test_v2_city_fixed_wizard_and_date_input_state(handler, mem_db):
    handler._launch_search = MagicMock()
    dep = kst_today() + timedelta(days=45)
    ret = dep + timedelta(days=4)
    dates_text = f"{dep.isoformat()} ~ {ret.isoformat()}"
    _cb(handler, "trv:w:s:FUK", chat="g1", user="u1")
    _cb(handler, "trv:w:m2:FUK:X", chat="g1", mid=900, user="u1")
    assert handler.get_chat_state("g1", "u1") == STATE_AWAITING_WATCH_DATES
    assert handler.get_chat_state("g1", "u2") == STATE_IDLE
    assert "여행 날짜 입력" in handler.edits[-1][1]
    # another member's date text is not consumed by u1's prompt
    assert handler.handle_text_message("g1", 1, "u2", dates_text) == (True, "travel_help_unknown_city")
    # invalid dates keep the prompt alive; HTML is escaped
    assert handler.handle_text_message("g1", 2, "u1", "2027-02-30~2027-03-02") == (True, "travel_watch_dates_invalid")
    assert handler.handle_text_message("g1", 3, "u1", "<b>x</b> 1") == (True, "travel_watch_dates_invalid")
    assert "&lt;b&gt;" in handler.tg.send_text.call_args[0][1]
    assert handler.handle_text_message("g1", 3, "u1", "아무거나") == (True, "travel_watch_dates_invalid")
    assert handler.get_chat_state("g1", "u1") == STATE_AWAITING_WATCH_DATES
    assert handler.handle_text_message("g1", 4, "u1", dates_text) == (True, "travel_watch_dates_set")
    assert handler.get_chat_state("g1", "u1") == STATE_IDLE
    hub = handler.tg.send_text.call_args[0][1]
    assert "📌 날짜 고정" in hub and "4박" in hub and "±1일" in hub
    assert mem_db.get_travel_watch("g1", "FUK") is None
    _cb(handler, "trv:w:n:FUK:D", chat="g1", mid=900, user="u1")
    _cb(handler, "trv:w:k:FUK:W", chat="g1", mid=900, user="u1")
    w = mem_db.get_travel_watch("g1", "FUK")
    assert (w["watch_mode"], w["fixed_dep_date"], w["fixed_ret_date"], w["notify_mode"], w["preset_kind"]) == \
        ("FIXED", dep.isoformat(), ret.isoformat(), "DROP", "BASELINE")
    saved = handler.edits[-1][1]
    assert "날짜 고정" in saved and "1만 원 이상" in saved
    assert "trv:w:t:FUK:day" not in all_callbacks(handler.edits[-1][2])  # FIXED has no weekday toggles

    # a custom target through the legacy price prompt applies this user's draft
    _cb(handler, "trv:w:s:NRT", chat="g1", mid=900, user="u1")
    _cb(handler, "trv:w:h:NRT:6", chat="g1", mid=900, user="u1")
    _cb(handler, "trv:w:d:NRT:D", chat="g1", mid=900, user="u1")
    _cb(handler, "trv:w:i:NRT", chat="g1", mid=900, user="u1")
    assert handler.handle_text_message("g1", 5, "u1", "22만") == (True, "travel_watch_saved")
    n = mem_db.get_travel_watch("g1", "NRT")
    assert (n["target_price_krw"], n["horizon_months"], n["weekday_option"], n["preset_kind"]) == \
        (220_000, 6, "WEEKDAY", "CUSTOM")

    # /cancel, any slash command, a city and a country all release the date prompt
    _cb(handler, "trv:w:f:FUK", chat="g1", user="u1")
    assert handler.get_chat_state("g1", "u1") == STATE_AWAITING_WATCH_DATES
    assert handler.handle_text_message("g1", 6, "u1", "/cancel") == (True, "travel_cancel")
    assert handler.get_chat_state("g1", "u1") == STATE_IDLE
    _cb(handler, "trv:w:f:FUK", chat="g1", user="u1")
    assert handler.handle_text_message("g1", 7, "u1", "/help") == (True, "travel_help")
    assert handler.get_chat_state("g1", "u1") == STATE_IDLE
    _cb(handler, "trv:w:f:FUK", chat="g1", user="u1")
    assert handler.handle_text_message("g1", 8, "u1", "삿포로") == (True, "travel_search_started")
    assert handler._launch_search.call_args[0][2] == "CTS" and handler.get_chat_state("g1", "u1") == STATE_IDLE
    _cb(handler, "trv:w:f:FUK", chat="g1", user="u1")
    assert handler.handle_text_message("g1", 9, "u1", "베트남") == (True, "travel_country_city_picker")
    assert "베트남 주요 도시 선택" in handler.tg.send_text.call_args[0][1]
    assert "trv:w:s:@VN" not in all_callbacks(handler.tg.send_text.call_args[1]["reply_markup"])
    assert handler.get_chat_state("g1", "u1") == STATE_IDLE
    assert mem_db.get_travel_watch("g1", "FUK")["fixed_dep_date"] == dep.isoformat()  # untouched
    # TTL expiry releases the date prompt as well
    handler.chat_state_ttl = 0.05
    _cb(handler, "trv:w:f:FUK", chat="g1", user="u1")
    time.sleep(0.1)
    assert handler.get_chat_state("g1", "u1") == STATE_IDLE
    _assert_callbacks_fit(handler)


@pytest.mark.parametrize("raw,expected", [
    ("2027-05-01~2027-05-04", ("2027-05-01", "2027-05-04")),
    ("2027.05.01-2027.05.04", ("2027-05-01", "2027-05-04")),
    ("2027/05/01 ~ 2027/05/04", ("2027-05-01", "2027-05-04")),
    ("270501 20270504", ("2027-05-01", "2027-05-04")),
    ("5.1~5.4", ("2027-05-01", "2027-05-04")),
    ("5월 1일 ~ 5월 4일", ("2027-05-01", "2027-05-04")),
    ("11/20~11/23", ("2026-11-20", "2026-11-23")),
    ("11.10~11.13", ("2027-11-10", "2027-11-13")),  # already passed this year -> next year
    ("12.30~1.3", ("2026-12-30", "2027-01-03")),
    ("２０２７－０５－０１~2027-05-04", ("2027-05-01", "2027-05-04")),
    ("2026-11-10~2026-11-13", None),  # an explicit past date is never auto-corrected
    ("2027-05-04~2027-05-01", None),
    ("2027-05-01~2027-05-01", None),
    ("2027-05-01~2027-06-15", None),  # > 30 nights
    ("2027-02-30~2027-03-02", None),
    ("5.1", None),
    ("2027-05-01~2027-05-04~2027-05-06", None),
    ("5.1 오후~5.4", None),
])
def test_parse_watch_date_range(raw, expected):
    dep, ret, reason = parse_watch_date_range(raw, today=date(2026, 11, 15))
    if expected is None:
        assert dep is None and ret is None and reason
    else:
        assert (dep.isoformat(), ret.isoformat(), reason) == (*expected, None)


def test_v2_preserves_legacy_keyboard_and_selection_contracts(handler, mem_db):
    for scope in ("ALL", "FUK", "CTS"):
        kb = handler.build_inline_keyboard(scope, chat_id="c1")["inline_keyboard"]
        assert kb[0][0]["callback_data"].startswith("trv:r:") and kb[-2][0]["callback_data"] == "trv:c:guide"
    assert handler.build_selected_target_keyboard("FUK")["inline_keyboard"][0][0]["callback_data"] == "trv:q:FUK"
    _cb(handler, "trv:w:m:FUK")
    assert "trv:w:s:FUK" in all_callbacks(handler.tg.send_text.call_args[1]["reply_markup"])
    # no active @TH: the legacy manage callback can never start a new country registration
    _cb(handler, "trv:w:m:@TH")
    assert "신규 등록이 종료" in handler.tg.send_text.call_args[0][1]
    assert mem_db.get_travel_watch("c1", "@TH") is None
    # grandfathered active @TH: the same callback still opens its edit/delete hub
    mem_db.upsert_travel_watch("c1", "@TH", 300_000)
    _cb(handler, "trv:w:m:@TH")
    assert "태국 전체" in handler.tg.send_text.call_args[0][1]
    _assert_callbacks_fit(handler)


# ----------------------------------------------------------------------
# V2-4. Engine cadence: DAILY morning window, FIXED re-drop, PG/STALE, UNKNOWN
# ----------------------------------------------------------------------
def test_daily_briefing_morning_window_no_compensation_and_unknown(mem_db):
    seed_recipient(mem_db, "c")
    w = mem_db.upsert_travel_watch("c", "FUK", 150_000, watch_mode="FLEX", horizon_months=6,
                                   weekday_option="WEEKEND", notify_mode="DAILY")
    wid = w["watch_id"]
    deals = {"FUK": [mk_live(price=200_000), mk_live(price=210_000, source="LIVE_PG"),
                     mk_live(dep="2027-01-15", ret="2027-01-18", price=190_000, freshness="STALE")]}
    sender = Recorder()

    def tick(clock, snd=sender, d=deals):
        return WatchCheckEngine(mem_db, fetcher_from(d), sender=snd, now_fn=clock).run_once()

    # 12:30 is the first tick of the day: no compensatory DAILY briefing
    res = tick(kst_clock(2026, 12, 1, 12, 30))
    assert (res.sent_count, res.daily_count, res.daily_skipped_window_count) == (0, 0, 1)
    assert (res.dropped_pg_count, res.dropped_stale_count) == (1, 1)
    # 07:30 -> the day's briefing (above target, still a briefing)
    res = tick(kst_clock(2026, 12, 2, 7, 30))
    assert (res.sent_count, res.daily_count) == (1, 1)
    text = sender.calls[-1][1]
    assert "[일일 브리핑]" in text and "실시간 최저가" in text and "200,000원" in text
    assert {"trv:w:l", "trv:w:x:FUK"} <= set(all_callbacks(sender.calls[-1][2]))
    row = mem_db.get_watch_delivery(make_watch_daily_key(wid, "2026-12-02"))
    assert (row["status"], row["delivery_kind"], row["kst_date"]) == ("SENT", "DAILY", "2026-12-02")
    assert mem_db.get_travel_watch("c", "FUK")["last_daily_sent_date"] == "2026-12-02"
    # same-morning re-run: no second briefing, no DROP (no further drop)
    assert tick(kst_clock(2026, 12, 2, 8, 0)).sent_count == 0 and len(sender.calls) == 1
    # the next day only a 12:30 tick ran -> nothing (a missed morning is never compensated)
    assert tick(kst_clock(2026, 12, 3, 12, 30)).sent_count == 0 and len(sender.calls) == 1
    # later tick: a >= 10,000 KRW further drop on another slot is a DROP ("특가 포착") even above target
    drop = {"FUK": [mk_live(price=200_000), mk_live(dep="2027-01-21", ret="2027-01-24", price=188_000)]}
    res = tick(kst_clock(2026, 12, 3, 18, 30), d=drop)
    assert res.sent_count == 1 and "[특가 포착]" in sender.calls[-1][1]
    assert mem_db.get_watch_delivery(f"travel-watch:v2:{wid}:DROP:2027-01-21:2027-01-24:188000")["status"] == "SENT"
    # an ambiguous DAILY send -> UNKNOWN quarantine, never retried that day
    boom = Recorder(status=TimeoutError("read timed out"))
    res = tick(kst_clock(2026, 12, 4, 7, 30), snd=boom, d=drop)
    assert res.unknown_count == 1 and len(boom.calls) == 1
    assert mem_db.get_watch_delivery(make_watch_daily_key(wid, "2026-12-04"))["status"] == "UNKNOWN"
    res = tick(kst_clock(2026, 12, 4, 8, 30), snd=boom, d=drop)
    assert len(boom.calls) == 1 and res.daily_count == 0


def test_fixed_same_slot_redrop_vs_flex_slot_cooldown(mem_db):
    for cid in ("fx", "fl"):
        seed_recipient(mem_db, cid)
    fx = mem_db.upsert_travel_watch("fx", "FUK", 300_000, watch_mode="FIXED",
                                    fixed_dep_date="2027-01-14", fixed_ret_date="2027-01-17")
    fl = mem_db.upsert_travel_watch("fl", "FUK", 300_000)
    sender = Recorder()

    def tick(price, minutes):
        main = mk_live(price=price)
        return WatchCheckEngine(
            mem_db, fetcher_from({"FUK": [main]}), sender=sender, now_fn=kst_clock(2026, 12, 1, 12, minutes),
            fixed_quote_fetcher=fixed_fetcher_from({("FUK", "2027-01-14", "2027-01-17"): [main]}),
        ).run_once()

    assert tick(250_000, 0).sent_count == 2
    res = tick(245_000, 10)  # 5,000 drop: FIXED insufficient_drop, FLEX slot_cooldown
    assert (res.sent_count, res.skipped_cooldown_count) == (0, 2)
    res = tick(238_000, 20)  # 12,000 drop on the SAME slot
    assert res.sent_count == 1 and sender.calls[-1][0] == "fx" and "[목표가 도달]" in sender.calls[-1][1]
    keys = [r["delivery_key"] for r in ledger_rows(mem_db, fx["watch_id"])]
    assert len(keys) == 2
    assert all(k.startswith(f"travel-watch:v2:{fx['watch_id']}:DROP:2027-01-14:2027-01-17:") for k in keys)
    assert [r["delivery_key"] for r in ledger_rows(mem_db, fl["watch_id"])] == \
        [make_watch_delivery_key(fl["watch_id"], "fl", "FUK", "2027-01-14", "2027-01-17", 250_000)]
    # FIXED above target with no prior alert never triggers; PG / STALE never trigger
    nrt = mem_db.upsert_travel_watch("fx", "NRT", 200_000, watch_mode="FIXED",
                                     fixed_dep_date="2027-02-04", fixed_ret_date="2027-02-07")
    seen = Recorder()
    res = WatchCheckEngine(
        mem_db, fetcher_from({}), sender=seen, now_fn=kst_clock(2026, 12, 1, 13),
        fixed_quote_fetcher=fixed_fetcher_from({("NRT", "2027-02-04", "2027-02-07"): [
            mk_live("NRT", "2027-02-04", "2027-02-07", 100_000, source="LIVE_PG"),
            mk_live("NRT", "2027-02-04", "2027-02-07", 90_000, freshness="STALE"),
            mk_live("NRT", "2027-02-04", "2027-02-07", 210_000)]}),
    ).run_once()
    assert ledger_rows(mem_db, nrt["watch_id"]) == []
    assert all("ICN→NRT" not in t for _c, t, _m in seen.calls)
    assert res.dropped_pg_count == 1 and res.dropped_stale_count == 1


def test_fixed_drop_unknown_quarantine_and_verified_neighbor_tip(mem_db):
    seed_recipient(mem_db, "c")
    mem_db.upsert_travel_watch("c", "FUK", 300_000, watch_mode="FIXED",
                               fixed_dep_date="2027-01-14", fixed_ret_date="2027-01-17")
    main = mk_live(price=260_000)
    nb_ok = mk_live(dep="2027-01-15", ret="2027-01-18", price=230_000)
    nb_pg = mk_live(dep="2027-01-13", ret="2027-01-16", price=150_000, source="LIVE_PG")
    ff = fixed_fetcher_from({("FUK", "2027-01-14", "2027-01-17"): [main, nb_ok, nb_pg]})
    boom = Recorder(status=TimeoutError("timed out"))
    res = WatchCheckEngine(mem_db, fetcher_from({}), sender=boom, now_fn=kst_clock(2026, 12, 1, 12),
                           fixed_quote_fetcher=ff).run_once()
    assert res.unknown_count == 1 and ff.calls == [("FUK", "2027-01-14", "2027-01-17", True)]
    card = boom.calls[0][1]
    assert "±1일 비교" in card and "01/15(금)~01/18(월) 230,000원 (30,000원 ↓)" in card
    assert "150,000" not in card  # an unverified LIVE_PG neighbour is never shown
    again = Recorder()
    res = WatchCheckEngine(mem_db, fetcher_from({}), sender=again, now_fn=kst_clock(2026, 12, 1, 18, 30),
                           fixed_quote_fetcher=ff).run_once()
    assert again.calls == [] and res.skipped_cooldown_count == 1  # UNKNOWN is never retried


def test_country_watch_grouping_union_spec_and_card(mem_db):
    for cid in ("a", "b"):
        seed_recipient(mem_db, cid)
    mem_db.upsert_travel_watch("a", "@JP", 200_000, watch_mode="FLEX", horizon_months=12, weekday_option="WEEKEND")
    mem_db.upsert_travel_watch("b", "FUK", 200_000, watch_mode="FLEX", horizon_months=3, weekday_option="WEEKDAY")
    deals = {
        "NRT": [mk_live("NRT", "2027-01-14", "2027-01-17", 180_000)],
        "KIX": [mk_live("KIX", "2027-01-15", "2027-01-18", 160_000, dep_time="09:00", ret_time="19:00")],
        "FUK": [mk_live("FUK", "2027-01-11", "2027-01-14", 150_000)],  # Mon->Thu (weekday)
    }
    fetch = spec_fetcher_from(deals)
    sender = Recorder()
    WatchCheckEngine(mem_db, fetch, sender=sender, now_fn=kst_clock(2026, 12, 1, 12, 30)).run_once()
    by_code = {c: s for c, s in fetch.calls}
    assert sorted(by_code) == ["CTS", "FUK", "KIX", "NRT", "OKA"] and len(fetch.calls) == 5  # one fetch per airport
    assert (by_code["FUK"].horizon_months, by_code["FUK"].weekday_option, by_code["FUK"].sse_cap) == (12, "ANY", 3)
    assert (by_code["NRT"].horizon_months, by_code["NRT"].weekday_option, by_code["NRT"].sse_cap) == (12, "WEEKEND", 1)
    got = {chat: (text, mk) for chat, text, mk in sender.calls}
    a_text, a_kb = got["a"]
    assert "일본 전체" in a_text and "ICN→KIX" in a_text and "160,000원" in a_text  # FUK weekday excluded
    assert "9~12개월" in a_text
    assert "ICN-KIX-20270115" in a_kb["inline_keyboard"][0][0]["url"]
    assert set(all_callbacks(a_kb)) == {"trv:w:s:@JP", "trv:w:x:@JP", "trv:w:l"}
    assert "ICN→FUK" in got["b"][0] and "150,000원" in got["b"][0]


def test_mobile_decision_card_contents_and_limits():
    from travel_bot_runner import fits_tg01, visible_text_len
    from travel_brief.models import BudgetBreakdown, round_man

    res = CityCatalog(FlightEngine(live_naver=False).destinations).get("FUK")
    watch = {"dest_code": "FUK", "target_price_krw": 300_000, "horizon_months": 12, "watch_mode": "FLEX"}
    deal = mk_live(dep="2027-01-14", ret="2027-01-18", price=250_000, dep_time="19:30", ret_time="08:10",
                   arr_time="10:20", nights=3)  # stale `nights` field: the card must use the real 4 nights
    nb = mk_live(dep="2027-01-15", ret="2027-01-19", price=235_000)
    text = build_watch_alert_card(watch, deal, res, prev_seen=275_000, initial_price=320_000, neighbor_deal=nb)
    for token in ("[목표가 도달]", "ICN→FUK", "01/14(목)~01/18(월) · 4박", "제주항공", "19:30", "08:10", "10:20",
                  "첫날 저녁 출발", "귀국편 오전 출발", "실시간 최저가", "50,000원(16.7%)", "등록 기준 320,000원",
                  "70,000원 하락", "지난 관측 대비 25,000원 하락", "1인 총예산", "4박 항공+숙박",
                  "±1일 비교", "위탁수하물", "9~12개월"):
        assert token in text, token
    assert f"{round_man(BudgetBreakdown.calculate(res.meta, 250_000, nights=4).total_per_person_krw)}만 원" in text
    assert fits_tg01(text) and visible_text_len(text) <= 1800

    # omissions: unverified / pricier neighbour, FIXED horizon note, FSC baggage, no short-stay flags
    fixed = {**watch, "watch_mode": "FIXED", "fixed_dep_date": "2027-01-14", "fixed_ret_date": "2027-01-18"}
    calm = mk_live(dep="2027-01-14", ret="2027-01-18", price=250_000, airline="대한항공", dep_time="09:00",
                   ret_time="15:00")
    t2 = build_watch_alert_card(fixed, calm, res, neighbor_deal=mk_live(price=200_000, source="LIVE_PG"))
    t3 = build_watch_alert_card(fixed, calm, res, neighbor_deal=mk_live(price=260_000))
    for t in (t2, t3):
        assert "±1일" not in t and "9~12개월" not in t and "위탁수하물" not in t
        assert "첫날 저녁" not in t and "귀국편 오전" not in t
    # escaping + rising anchor + above-target wording
    hostile = mk_live(price=330_000, airline="<script>x</script>")
    t4 = build_watch_alert_card(watch, hostile, res, initial_price=300_000)
    assert "&lt;script&gt;" in t4 and "<script>" not in t4 and "30,000원 상승" in t4
    assert "[특가 포착]" in t4 and "30,000원 남음" in t4
    # country scope label + DAILY headline
    t5 = build_watch_alert_card({"dest_code": "@JP", "target_price_krw": 300_000}, deal, res, kind="DAILY",
                                scope_label="🇯🇵 일본 전체")
    assert t5.startswith("📰 <b>[일일 브리핑] [목표가 도달] 🇯🇵 일본 전체 · ")
    assert all(fits_tg01(t) for t in (t2, t3, t4, t5))


# ======================================================================
# Instant toggle UX: silent ack, edit dispatched before ack, keep-alive edit session
# ======================================================================
def test_fast_toggles_silent_ack_and_dispatch_before_ack(handler, mem_db):
    _cb(handler, "trv:w:s:FUK", mid=900)
    order: list[str] = []
    real_run = handler._run_watch_action
    handler._submit_ui = lambda fn, *a: (order.append("submit:" + a[2]), fn(*a))
    handler.tg.answer_callback.side_effect = lambda *a, **kw: order.append("ack")
    with patch.object(handler, "_run_watch_action", wraps=real_run) as run:
        for data, sub in (("trv:w:h:FUK:6", "h"), ("trv:w:d:FUK:W", "d"), ("trv:w:n:FUK:Y", "n"),
                          ("trv:w:m2:FUK:F", "m2")):
            order.clear()
            _cb(handler, data, mid=900)
            assert order == [f"submit:{sub}", "ack"]  # editMessageText starts before answerCallbackQuery
            assert handler.tg.answer_callback.call_args[1]["text"] == ""  # no toast banner per tap
            assert run.call_args[0][2] == sub
    assert run.call_count == 4
    # read-only navigation (list card): edit submitted first, toast ack right after (parallel in production)
    order.clear()
    _cb(handler, "trv:w:l", mid=900)
    assert order[:2] == ["submit:l", "ack"] and handler.tg.answer_callback.call_args[1]["text"]
    # DB-mutating actions keep their toast and ack-before-work ordering
    order.clear()
    _cb(handler, "trv:w:wp", mid=900)
    assert order[:2] == ["ack", "submit:wp"] and handler.tg.answer_callback.call_args[1]["text"]


def test_fast_toggle_clears_pending_input_state(handler, mem_db):
    _cb(handler, "trv:w:i:FUK", mid=900)
    assert handler.get_chat_context("c1", "u1").state == STATE_AWAITING_WATCH_PRICE
    _cb(handler, "trv:w:h:FUK:1", mid=900)
    st = handler.get_chat_context("c1", "u1")
    assert st is None or st.state != STATE_AWAITING_WATCH_PRICE


def test_rapid_sequential_toggles_final_draft_state(handler, mem_db):
    _cb(handler, "trv:w:s:FUK", mid=900)
    taps = ["trv:w:h:FUK:1", "trv:w:d:FUK:D", "trv:w:h:FUK:12", "trv:w:n:FUK:Y", "trv:w:d:FUK:W",
            "trv:w:h:FUK:6", "trv:w:n:FUK:D", "trv:w:n:FUK:Y", "trv:w:d:FUK:A", "trv:w:h:FUK:3"]
    for data in taps:
        _cb(handler, data, mid=900)
    d = handler._peek_draft("c1", "u1", "FUK")
    assert (d.horizon_months, d.weekday_option, d.notify_mode) == (3, "ANY", "DAILY")
    assert "앞으로 3개월" in handler.edits[-1][1] and "매일 아침 7:30" in handler.edits[-1][1]
    assert mem_db.get_travel_watch("c1", "FUK") is None  # toggles never persist


def test_rapid_toggles_through_real_ui_pool(mem_db):
    h = TravelInteractiveHandler(bot_token="dummy", db=mem_db)
    h.tg = MagicMock()
    h.tg.send_text.return_value = "900"
    h._safe_edit_message = lambda *a, **kw: True
    try:
        h.handle_callback_query("cq", "c1", 900, "trv:w:s:FUK", {"id": "u1"})
        for data in ("trv:w:h:FUK:1", "trv:w:d:FUK:D", "trv:w:n:FUK:Y", "trv:w:h:FUK:6"):
            h.handle_callback_query("cq", "c1", 900, data, {"id": "u1"})
        assert h.ui_pool._max_workers == 4
        h.ui_pool.shutdown(wait=True)  # drain queued edits
        d = h._peek_draft("c1", "u1", "FUK")
        assert d is not None and d.weekday_option == "WEEKDAY" and d.notify_mode == "DAILY"
        assert d.horizon_months in (1, 6)  # pool may reorder same-key edits; both are valid choices
        assert h.tg.answer_callback.call_count == 5
    finally:
        h.shutdown()


class _FakeResp:
    def __init__(self, status, payload):
        self.status_code, self._payload, self.reason, self.headers = status, payload, "x", {}
        import json as _json
        self.content = _json.dumps(payload).encode()

    def json(self):
        return self._payload


def test_edit_uses_keepalive_session_and_preserves_error_semantics(mem_db):
    h = TravelInteractiveHandler(bot_token="dummy", db=mem_db)
    h.tg = MagicMock()
    h.tg.send_text.return_value = "900"
    sess = MagicMock()
    h._http_session = sess
    try:
        sess.post.return_value = _FakeResp(200, {"ok": True})
        assert h._safe_edit_message("c1", 44, "<b>x</b>") is True
        assert h._safe_edit_message("c1", 44, "<b>y</b>") is True
        assert sess.post.call_count == 2  # same session reused, no per-tap handshake
        assert sess.post.call_args[0][0].endswith("/editMessageText")
        # not modified -> success without a send
        sess.post.return_value = _FakeResp(400, {"description": "Bad Request: message is not modified"})
        assert h._safe_edit_message("c1", 44, "x") is True
        h.tg.send_text.assert_not_called()
        # 429 -> one retry after sleeping
        sess.post.reset_mock()
        sess.post.side_effect = [_FakeResp(429, {"description": "Too Many Requests: retry after 1"}),
                                 _FakeResp(200, {"ok": True})]
        with patch("travel_brief.interactive.time.sleep") as slp:
            assert h._safe_edit_message("c1", 44, "x") is True
        assert sess.post.call_count == 2 and slp.call_args[0][0] == 1.5
        # 400 -> HTML send fallback; loading-card edits never send
        sess.post.side_effect = None
        sess.post.return_value = _FakeResp(400, {"description": "Bad Request: can't parse entities"})
        assert h._safe_edit_message("c1", 44, "<b>x</b>") is True
        assert h.tg.send_text.call_args[1]["parse_mode"] == "HTML"
        h.tg.send_text.reset_mock()
        sess.post.side_effect = TimeoutError("t")
        assert h._edit_in_place_only("c1", 44, "loading") is False
        h.tg.send_text.assert_not_called()
    finally:
        h.shutdown()
    sess.close.assert_called_once()
    assert h._http_session is None


def test_shutdown_closes_real_session_cleanly(mem_db):
    pytest.importorskip("requests")
    h = TravelInteractiveHandler(bot_token="dummy", db=mem_db)
    s1 = h._get_http_session()
    assert s1 is not None and h._get_http_session() is s1  # lazily created once, then reused
    h.shutdown()
    assert h._http_session is None and h._get_http_session() is None  # no re-creation after shutdown
    h.shutdown()  # idempotent, no error
    h2 = TravelInteractiveHandler(bot_token="dummy", db=mem_db)
    h2.shutdown()  # never-created session: clean no-op


# ----------------------------------------------------------------------
# Country city-picker hub & typo suggestions
# ----------------------------------------------------------------------
@pytest.mark.parametrize("typo,code", [
    ("오시카", "KIX"), ("후쿠오까", "FUK"), ("방콕크", "BKK"), ("다낭ㅇ", "DAD"), ("세부부", "CEB"),
    ("후쿠오까 여행", "FUK"),
])
def test_suggest_cities_korean_typos(typo, code):
    cat = CityCatalog(FlightEngine(live_naver=False).destinations)
    assert cat.resolve(typo) is None  # typos never auto-resolve
    picks = cat.suggest_cities(typo, limit=3)
    assert picks and picks[0].code == code
    assert len({p.code for p in picks}) == len(picks) <= 3


@pytest.mark.parametrize("junk", ["zzzzzz", "우주여행", "몰디브", "XYZ", "25", "", "a", "파리바게트"])
def test_suggest_cities_rejects_unrelated(junk):
    cat = CityCatalog(FlightEngine(live_naver=False).destinations)
    assert cat.suggest_cities(junk) == []


def test_suggest_cities_exact_name_only_matches_itself():
    cat = CityCatalog(FlightEngine(live_naver=False).destinations)
    assert [r.code for r in cat.suggest_cities("런던")] == ["LHR"]
    assert cat.suggest_cities("오시카", limit=0) == []


def test_country_text_opens_city_picker_hub(handler):
    handler._launch_search = MagicMock()
    assert handler.handle_text_message("c1", 1, "u1", "일본") == (True, "travel_country_city_picker")
    text = handler.tg.send_text.call_args[0][1]
    markup = handler.tg.send_text.call_args[1]["reply_markup"]
    assert "일본 주요 도시 선택" in text and "어느 도시의 실시간 항공권" in text
    cbs = all_callbacks(markup)
    # legacy country watches are read-only: no new-registration button, a region drilldown instead
    assert cbs == ["trv:q:NRT", "trv:q:KIX", "trv:q:FUK", "trv:q:CTS", "trv:q:OKA", "trv:srg", "trv:q:ALL"]
    assert all(len(row) <= 3 for row in markup["inline_keyboard"])
    assert handler._launch_search.call_count == 0  # a country never starts a search by itself
    # tapping a fallback member city goes straight into a live search
    _cb(handler, "trv:q:CTS", mid=900)
    assert handler._launch_search.call_args[0][2] == "CTS"
    for word, scope in (("베트남", "@VN"), ("태국", "@TH"), ("필리핀", "@PH"), ("대만전체", "@TW")):
        assert handler.handle_text_message("c1", 2, "u1", word) == (True, "travel_country_city_picker")
        assert f"trv:w:s:{scope}" not in all_callbacks(handler.tg.send_text.call_args[1]["reply_markup"])
    _assert_callbacks_fit(handler)


def test_country_picker_without_db_hides_watch_button():
    h = TravelInteractiveHandler(bot_token="dummy", db=None)
    h.tg = MagicMock()
    h.tg.send_text.return_value = "900"
    try:
        assert h.handle_text_message("c1", 1, "u1", "태국") == (True, "travel_country_city_picker")
        cbs = all_callbacks(h.tg.send_text.call_args[1]["reply_markup"])
        assert cbs == ["trv:q:BKK", "trv:q:CNX", "trv:q:HKT", "trv:srg", "trv:q:ALL"]
    finally:
        h.shutdown()


def test_taiwan_bare_word_still_searches_tpe(handler):
    handler._launch_search = MagicMock()
    assert handler.handle_text_message("c1", 1, "u1", "대만") == (True, "travel_search_started")
    assert handler._launch_search.call_args[0][2] == "TPE"


def test_typo_shows_did_you_mean_button_above_default_keyboard(handler):
    handler._launch_search = MagicMock()
    assert handler.handle_text_message("c1", 1, "u1", "후쿠오까") == (True, "travel_help_unknown_city")
    text = handler.tg.send_text.call_args[0][1]
    rows = handler.tg.send_text.call_args[1]["reply_markup"]["inline_keyboard"]
    assert "혹시 아래 도시를 찾으셨나요?" in text and "후쿠오카" in text
    assert rows[0][0]["callback_data"] == "trv:q:FUK"
    assert rows[1:] == handler.build_inline_keyboard("ALL")["inline_keyboard"]
    assert handler._launch_search.call_count == 0  # suggestion only, never auto-search
    # unrelated / HTML input: no suggestion row, escaped echo
    assert handler.handle_text_message("c1", 2, "u1", "<b>zzzzzz</b>") == (True, "travel_help_unknown_city")
    text = handler.tg.send_text.call_args[0][1]
    rows = handler.tg.send_text.call_args[1]["reply_markup"]["inline_keyboard"]
    assert "찾으셨나요" not in text and "&lt;b&gt;" in text
    assert rows == handler.build_inline_keyboard("ALL")["inline_keyboard"]
    assert "찾으셨나요" not in handler.build_help_card("몰디브")  # legacy signature unchanged
    _assert_callbacks_fit(handler)


# ----------------------------------------------------------------------
# 2-Track delivery prefs: Track A weekly opt-out / Track B pause / 2-step reset
# ----------------------------------------------------------------------
def test_travel_user_prefs_defaults_and_deliverable_filter(mem_db):
    for cid in ("p1", "p2"):
        seed_recipient(mem_db, cid)
        mem_db.upsert_travel_watch(cid, "FUK", 250_000)
    assert mem_db.get_travel_user_prefs("p1") == {
        "chat_id": "p1", "weekly_brief_enabled": True, "watches_paused": False, "weekly_brief_freq": "THU_1X",
        "weekly_brief_region": "ALL", "weekly_brief_theme": "ALL", "updated_at": None,
    }
    assert mem_db.is_weekly_brief_enabled("nobody") is True  # no row -> legacy users keep Track A

    def deliverable():
        return sorted(w["chat_id"] for w in mem_db.list_deliverable_watches()[0])

    assert deliverable() == ["p1", "p2"]
    # Track A opt-out never touches Track B
    prefs = mem_db.set_weekly_brief_enabled("p1", False)
    assert prefs["weekly_brief_enabled"] is False and prefs["watches_paused"] is False and prefs["updated_at"]
    assert mem_db.is_weekly_brief_enabled("p1") is False
    assert deliverable() == ["p1", "p2"]
    # Track B pause blocks delivery but keeps the watch row active
    assert mem_db.set_watches_paused("p2", True)["watches_paused"] is True
    assert deliverable() == ["p1"]
    assert mem_db.get_travel_watch("p2", "FUK")["is_active"] == 1
    assert mem_db.is_weekly_brief_enabled("p2") is True
    # toggles (None) flip atomically
    assert mem_db.set_watches_paused("p2")["watches_paused"] is False
    assert deliverable() == ["p1", "p2"]
    assert mem_db.set_weekly_brief_enabled("p1")["weekly_brief_enabled"] is True


def test_reset_travel_watches_is_atomic_and_preserves_weekly_choice(mem_db):
    seed_recipient(mem_db, "r1")
    seed_recipient(mem_db, "r2")
    for code in ("FUK", "NRT", "@JP"):
        mem_db.upsert_travel_watch("r1", code, 250_000)
    mem_db.upsert_travel_watch("r2", "FUK", 250_000)
    mem_db.set_weekly_brief_enabled("r1", False)
    mem_db.set_watches_paused("r1", True)
    assert mem_db.reset_travel_watches("r1") == 3
    prefs = mem_db.get_travel_user_prefs("r1")
    assert prefs["weekly_brief_enabled"] is False and prefs["watches_paused"] is False
    assert mem_db.list_travel_watches("r1") == []
    assert len(mem_db.list_travel_watches("r2")) == 1  # other chats untouched
    assert mem_db.reset_travel_watches("r1") == 0  # idempotent
    # reset without a prefs row creates defaults (weekly stays ON)
    assert mem_db.reset_travel_watches("r2") == 1
    assert mem_db.get_travel_user_prefs("r2")["weekly_brief_enabled"] is True
    # /stop full unsubscribe path still intact alongside prefs
    seed_recipient(mem_db, "r3")
    mem_db.upsert_travel_watch("r3", "FUK", 250_000)
    mem_db.set_watches_paused("r3", True)
    res = mem_db.deactivate_recipient_and_watches("r3")
    assert res["recipient_deactivated"] is True and res["watches_deactivated"] == 1
    assert mem_db.list_travel_watches("r3") == []


def test_prefs_concurrent_toggles_and_reset_on_file_db(tmp_path):
    db = TravelBriefDatabase(db_path=str(tmp_path / "prefs.sqlite3"))
    seed_recipient(db, "cc")
    for code in ("FUK", "NRT", "KIX"):
        db.upsert_travel_watch("cc", code, 250_000)
    errors: list[BaseException] = []

    def run_threads(targets):
        threads = [threading.Thread(target=t) for t in targets]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def toggler(kind):
        def _run():
            try:
                for _ in range(5):
                    if kind == "wb":
                        db.set_weekly_brief_enabled("cc")
                    else:
                        db.set_watches_paused("cc")
            except BaseException as exc:  # pragma: no cover - asserted below
                errors.append(exc)
        return _run

    run_threads([toggler("wb" if i % 2 else "wp") for i in range(8)])
    assert errors == []
    prefs = db.get_travel_user_prefs("cc")
    # 4 threads x 5 toggles = 20 flips each; any lost update would leave an odd flip count
    assert prefs["weekly_brief_enabled"] is True and prefs["watches_paused"] is False

    db.set_watches_paused("cc", True)
    results: list[int] = []

    def resetter():
        try:
            results.append(db.reset_travel_watches("cc"))
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    def weekly_off():
        try:
            db.set_weekly_brief_enabled("cc", False)
        except BaseException as exc:  # pragma: no cover
            errors.append(exc)

    run_threads([resetter] * 4 + [weekly_off])
    assert errors == []
    assert sum(results) == 3  # each watch deactivated exactly once
    prefs = db.get_travel_user_prefs("cc")
    assert prefs["watches_paused"] is False and prefs["weekly_brief_enabled"] is False
    assert db.list_travel_watches("cc") == []


def test_watch_list_card_status_and_compact_two_column_rows(handler, mem_db):
    mem_db.upsert_travel_watch("c1", "FUK", 250_000)
    mem_db.upsert_travel_watch("c1", "@JP", 300_000)
    text, markup = handler.build_watch_list_card("c1")
    rows = markup["inline_keyboard"]
    assert "🔔 <b>[맞춤발송 설정]</b> (2/4)" in text and DORMANT_BANNER not in text
    assert "🟢 아래 도시의 목표가 도달 시 매일 3회(07:30·12:30·18:30) 체크해 알려드려요." in text
    assert "정기발송:" not in text  # 1:1 split: Track A status lives only on the 📰 정기발송 card
    assert rows[0] == [{"text": "⚙️ " + handler.label_for("FUK"), "callback_data": "trv:w:m:FUK"},
                       {"text": "🗑️", "callback_data": "trv:w:x:FUK"}]
    assert [b["callback_data"] for b in rows[1]] == ["trv:w:s:@JP", "trv:w:x:@JP"]
    assert {"text": "➕ 도시 추가하기", "callback_data": "trv:w:g"} in [b for r in rows for b in r]
    assert rows[-2] == [{"text": "⏸️ 일시중지", "callback_data": "trv:w:wp"},
                        {"text": "🧹 초기화", "callback_data": "trv:w:z"}]
    assert rows[-1] == [{"text": "📰 정기발송 설정", "callback_data": "trv:m:settings"},
                        {"text": "🏠 메뉴", "callback_data": "trv:m:home"}]
    assert not {"trv:w:wb", "trv:w:wb:s", "trv:w:wp:s", "trv:c:guide"} & set(all_callbacks(markup))
    mem_db.set_weekly_brief_enabled("c1", False)
    mem_db.set_watches_paused("c1", True)
    text, markup = handler.build_watch_list_card("c1")
    assert "⏸️ 맞춤발송이 일시중지 중이에요. [▶️ 다시 받기]를 누르면 재개돼요." in text
    assert text.startswith(DORMANT_BANNER)  # freq OFF + Track B paused -> search-only mode banner
    assert markup["inline_keyboard"][-2] == [{"text": "▶️ 다시 받기", "callback_data": "trv:w:wp"},
                                             {"text": "🧹 초기화", "callback_data": "trv:w:z"}]
    # setup card: save / input rows are 2-column
    _t, setup = handler.build_watch_setup_card("c1", "u1", "FUK")
    by_cb = {b["callback_data"]: i for i, r in enumerate(setup["inline_keyboard"]) for b in r}
    assert by_cb["trv:w:k:FUK:1"] == by_cb["trv:w:k:FUK:W"]
    assert by_cb["trv:w:i:FUK"] == by_cb["trv:w:x:FUK"]
    assert all(len(r) <= 4 for r in setup["inline_keyboard"])


def test_prefs_callbacks_ack_first_and_toggle(handler, mem_db):
    handler._remember_watch_card("c1", 900)
    order: list[str] = []
    handler.tg.answer_callback.side_effect = lambda *a, **kw: order.append("ack")
    for name in ("set_weekly_brief_enabled", "set_watches_paused"):
        real = getattr(mem_db, name)

        def wrapped(*a, _real=real, _name=name, **kw):
            order.append(_name)
            return _real(*a, **kw)

        setattr(mem_db, name, wrapped)

    _cb(handler, "trv:w:wb", mid=900)
    assert order[:2] == ["ack", "set_weekly_brief_enabled"]
    assert mem_db.is_weekly_brief_enabled("c1") is False
    assert "정기 여행정보 발송을 껐어요" in handler.edits[-1][1]
    # legacy Track A toggle lands on the card that owns Track A
    assert "[정기발송 설정]" in handler.edits[-1][1] and "정기발송: <b>꺼짐</b>" in handler.edits[-1][1]
    # weekly OFF + no active watch -> one-time dormant notice on this transition
    assert DORMANT_NOTICE in handler.edits[-1][1] and DORMANT_BANNER in handler.edits[-1][1]
    order.clear()
    _cb(handler, "trv:w:wp", mid=900)
    assert order[:2] == ["ack", "set_watches_paused"]
    assert mem_db.get_travel_user_prefs("c1")["watches_paused"] is True
    assert "일시중지했어요" in handler.edits[-1][1] and "[맞춤발송 설정]" in handler.edits[-1][1]
    _cb(handler, "trv:w:wb", mid=900)
    _cb(handler, "trv:w:wp", mid=900)
    prefs = mem_db.get_travel_user_prefs("c1")
    assert prefs["weekly_brief_enabled"] is True and prefs["watches_paused"] is False
    _assert_callbacks_fit(handler)


def test_two_step_reset_clears_only_target_users_draft_and_input(handler, mem_db):
    handler._remember_watch_card("g1", 900)
    for code in ("FUK", "NRT"):
        mem_db.upsert_travel_watch("g1", code, 250_000)
    mem_db.set_weekly_brief_enabled("g1", False)
    mem_db.set_watches_paused("g1", True)
    handler._get_draft("g1", "u1", "FUK")
    handler._get_draft("g1", "u1", "@JP")
    handler._get_draft("g1", "u2", "FUK")
    handler._set_chat_state("g1", STATE_AWAITING_WATCH_PRICE, user_id="u2", dest_code="FUK")
    handler._set_chat_state("g1", STATE_AWAITING_WATCH_PRICE, user_id="u1", dest_code="NRT")

    # step 1: in-place confirmation only, nothing destroyed
    _cb(handler, "trv:w:z", chat="g1", mid=900, user="u1")
    _mid, text, markup = handler.edits[-1]
    assert "정말 초기화할까요" in text and "2개" in text
    assert all_callbacks(markup) == ["trv:w:Z", "trv:w:l"]
    assert len(mem_db.list_travel_watches("g1")) == 2
    assert handler._peek_draft("g1", "u2", "FUK") is not None

    # step 2: confirmed reset (ack precedes the DB write)
    order: list[str] = []
    handler.tg.answer_callback.side_effect = lambda *a, **kw: order.append("ack")
    real_reset = mem_db.reset_travel_watches
    mem_db.reset_travel_watches = lambda cid: (order.append("reset"), real_reset(cid))[1]
    _cb(handler, "trv:w:Z", chat="g1", mid=900, user="u1")
    assert order == ["ack", "reset"]
    assert mem_db.list_travel_watches("g1") == []
    prefs = mem_db.get_travel_user_prefs("g1")
    assert prefs["watches_paused"] is False and prefs["weekly_brief_enabled"] is False
    assert handler._peek_draft("g1", "u1", "FUK") is None and handler._peek_draft("g1", "u1", "@JP") is None
    assert handler.get_chat_state("g1", "u1") == STATE_IDLE
    # another member of the same group chat keeps their draft + pending input
    assert handler._peek_draft("g1", "u2", "FUK") is not None
    assert handler.get_chat_state("g1", "u2") == STATE_AWAITING_WATCH_PRICE
    assert "2개를 초기화" in handler.edits[-1][1] and "정기발송 설정은 유지" in handler.edits[-1][1]
    _assert_callbacks_fit(handler)


def test_new_prefs_callbacks_fit_64_bytes_and_reject_garbage(handler, mem_db):
    for cb in ("trv:w:wb", "trv:w:wp", "trv:w:z", "trv:w:Z"):
        assert len(cb.encode("utf-8")) <= 64
    for bad in ("trv:w:wbx", "trv:w:zZ", "trv:w:ZZ"):
        _cb(handler, bad)
        assert "지원하지 않는" in handler.tg.answer_callback.call_args[1]["text"]
    assert mem_db.get_travel_user_prefs("c1")["updated_at"] is None  # nothing written
    no_db = TravelInteractiveHandler(bot_token="dummy")
    no_db.tg = MagicMock()
    no_db.handle_callback_query("cq", "c1", 1, "trv:w:Z", {"id": "u1"})
    assert "사용할 수 없습니다" in no_db.tg.answer_callback.call_args[1]["text"]
    no_db.shutdown()


def test_onboarding_card_introduces_both_tracks(handler):
    card = handler.build_onboarding_card()
    assert "목 12:00" in card and "정기발송" in card and "🔔 <b>맞춤발송" in card
    assert "/settings" in card
    assert "구독이 시작되었습니다" in card
    assert "내 발송 상태" not in card  # no chat_id -> no personal badge (backward compatible)
    personal = handler.build_onboarding_card(chat_id="c1")
    assert "📰 정기발송: <b>🟢 받는 중 (주1회(목) 12:00 · 🌏 전체 · ✨ 전체)</b>" in personal
    assert "🔔 맞춤발송: <b>⚪ 미등록 (도시 0/4)</b>" in personal
    # /start passes the chat id through to the badge
    assert handler.handle_text_message("c1", 1, "u1", "/start") == (True, "travel_start")
    assert "⚪ 미등록 (도시 0/4)" in handler.tg.send_text.call_args[0][1]
    kb = handler.build_inline_keyboard("ALL")["inline_keyboard"]
    assert kb[0][0]["callback_data"].startswith("trv:r:") and kb[-2][0]["callback_data"] == "trv:c:guide"


# ----------------------------------------------------------------------
# Main menu hub (`trv:m:*`), slim 4-row result keyboard, delivery settings card, slash commands
# ----------------------------------------------------------------------
def test_slim_result_keyboard_four_rows_preserves_contract(handler, mem_db):
    for scope, chat in (("ALL", None), ("ALL", "c1"), ("FUK", "c1"), ("CTS", "c1")):
        rows = handler.build_inline_keyboard(scope, chat_id=chat)["inline_keyboard"]
        assert len(rows) == 4
        assert rows[0] == [{"text": "🔄 최신 특가 새로고침", "callback_data": f"trv:r:{scope}"}]
        assert rows[-3][1] == {"text": "⚙️ 정기발송 설정", "callback_data": "trv:m:settings"}
        assert rows[-2] == [{"text": "🔍 권역·도시 찾기", "callback_data": "trv:c:guide"},
                            {"text": "🏠 메뉴", "callback_data": "trv:m:home"}]
        assert rows[-1] == [{"text": "📊 6개월 특가 웹 리포트 ↗", "url": handler.report_url}]
    assert handler.build_inline_keyboard("ALL")["inline_keyboard"][-3][0]["callback_data"] == "trv:w:l"
    assert handler.build_inline_keyboard("FUK", chat_id="c1")["inline_keyboard"][-3][0]["callback_data"] == "trv:w:m:FUK"
    # no direct city shortcuts on the result keyboard anymore (moved to the popular submenu)
    assert not any(cb.startswith("trv:q:") for cb in all_callbacks(handler.build_inline_keyboard("ALL")))


def test_menu_cards_layout_and_callbacks_fit(handler, mem_db):
    text, hub = handler.build_menu_hub_card("c1")
    assert "메인 메뉴" in text
    assert "📰 정기발송: <b>🟢 받는 중 (주1회(목) 12:00 · 🌏 전체 · ✨ 전체)</b>" in text
    assert "🔔 맞춤발송: <b>⚪ 미등록 (도시 0/4)</b>" in text
    rows = hub["inline_keyboard"]
    assert [b["callback_data"] for b in rows[0]] == ["trv:m:settings", "trv:m:watch"]
    assert [b["callback_data"] for b in rows[1]] == ["trv:m:search", "trv:c:guide"]
    assert rows[2][0]["callback_data"] == "trv:m:popular" and rows[2][1]["url"] == handler.report_url
    assert "정기발송:" not in handler.build_menu_hub_card()[0]  # no chat -> no personal status line

    ob_text = handler.build_onboarding_card()
    assert "정기발송" in ob_text and "맞춤발송" in ob_text
    ob_rows = handler.build_onboarding_keyboard()["inline_keyboard"]
    assert len(ob_rows) == 3
    assert [b["callback_data"] for b in ob_rows[0]] == ["trv:m:settings", "trv:m:watch"]
    assert [b["callback_data"] for b in ob_rows[1]] == ["trv:q:ALL", "trv:c:guide"]
    assert ob_rows[2][0]["url"] == handler.report_url

    text, pop = handler.build_popular_cities_card()
    assert "인기 도시" in text
    assert all_callbacks(pop) == ["trv:q:NRT", "trv:q:KIX", "trv:q:FUK", "trv:q:TPE", "trv:q:DAD", "trv:q:BKK",
                                  "trv:c:guide", "trv:m:home"]
    assert not any(cb.startswith("trv:s:") for cb in all_callbacks(pop))  # trv:s:* stays onboarding-only

    text, settings = handler.build_delivery_settings_card("c1")
    assert "📰 <b>[정기발송 설정]</b>" in text and "원하는 주기·권역·스타일을 누르면 바로 저장돼요." in text
    assert "정기발송: <b>주1회(목) 12:00</b> 🟢 받는 중" in text
    assert "관심권역: <b>🌏 전체</b>" in text and "여행스타일: <b>✨ 전체</b>" in text
    assert "맞춤발송:" not in text  # 1:1 split: Track B status lives only on the 🔔 맞춤발송 card
    assert DORMANT_BANNER not in text
    assert settings["inline_keyboard"] == [
        [{"text": "✅ 목 주1회", "callback_data": "trv:w:fq:T"},
         {"text": "화·목 주2회", "callback_data": "trv:w:fq:B"},
         {"text": "끄기", "callback_data": "trv:w:fq:O"}],
        [{"text": "✅ 🌏 전체", "callback_data": "trv:w:rgn:ALL"}, {"text": "🧭 권역 고르기", "callback_data": "trv:w:rgp"}],
        [{"text": "✅ ✨ 전체", "callback_data": "trv:w:th:A"}, {"text": "🏖️ 휴양", "callback_data": "trv:w:th:R"},
         {"text": "🏙️ 관광", "callback_data": "trv:w:th:C"}],
        [{"text": "🔔 맞춤발송 설정", "callback_data": "trv:m:watch"},
         {"text": "🏠 메뉴", "callback_data": "trv:m:home"}],
    ]
    for mk in (hub, pop, settings, handler.build_inline_keyboard("ALL", chat_id="c1")):
        for cb in all_callbacks(mk):
            assert cb.startswith("trv:") and len(cb.encode("utf-8")) <= 64, cb


def test_menu_callbacks_route_ack_once_and_edit_menu_cards_in_place(handler, mem_db):
    handler._launch_search = MagicMock()
    # entry from a search result (unknown message) opens a fresh hub card, remembered as a navigation card
    _cb(handler, "trv:m:home", mid=10)
    assert "메인 메뉴" in handler.tg.send_text.call_args[0][1]
    assert handler._is_watch_card("c1", 900)
    assert handler.edits == []

    _cb(handler, "trv:m:popular", mid=900)
    assert "인기 도시" in handler.edits[-1][1] and "trv:q:FUK" in all_callbacks(handler.edits[-1][2])
    _cb(handler, "trv:m:help", mid=900)
    assert "도움말" in handler.edits[-1][1]
    assert handler.edits[-1][2] == handler.build_inline_keyboard("ALL", chat_id="c1")
    _cb(handler, "trv:m:watch", mid=900)
    assert "[맞춤발송 설정]" in handler.edits[-1][1]
    _cb(handler, "trv:m:settings", mid=900)
    assert "[정기발송 설정]" in handler.edits[-1][1]
    _cb(handler, "trv:m:home", mid=900)
    assert "메인 메뉴" in handler.edits[-1][1]
    assert handler._launch_search.call_count == 0

    # search goes through the trv:q:ALL path: cooldown toast ack + in-place loading
    _cb(handler, "trv:m:search", mid=900)
    assert "조회 중" in handler.tg.answer_callback.call_args[1]["text"]
    handler._launch_search.assert_called_once_with("c1", 900, "ALL", "u1", is_refresh=False, show_loading=True)
    _assert_callbacks_fit(handler)


@pytest.mark.parametrize("bad", ["trv:m:unknown", "trv:m", "trv:m:", "trv:m:home:x", "trv:m:HOME", "trv:m:NRT"])
def test_unknown_menu_callback_rejected_never_searches(handler, mem_db, bad):
    handler._launch_search = MagicMock()
    handler._set_chat_state("c1", STATE_AWAITING_WATCH_PRICE, user_id="u1", dest_code="FUK")
    _cb(handler, bad)
    assert "지원하지 않는 메뉴" in handler.tg.answer_callback.call_args[1]["text"]
    assert handler._launch_search.call_count == 0
    assert handler.tg.send_text.call_count == 0 and handler.edits == []
    assert handler.get_chat_state("c1", "u1") == STATE_AWAITING_WATCH_PRICE  # rejected -> state untouched


def test_menu_watch_and_settings_unavailable_without_db():
    h = TravelInteractiveHandler(bot_token="dummy")
    h.tg = MagicMock()
    h._submit_ui = lambda fn, *a: fn(*a)
    try:
        for data in ("trv:m:watch", "trv:m:settings"):
            h.handle_callback_query("cq", "c1", 1, data, {"id": "u1"})
            assert "사용할 수 없습니다" in h.tg.answer_callback.call_args[1]["text"]
        assert h.tg.send_text.call_count == 0
        for cmd in ("/watch", "/settings"):
            assert h.handle_text_message("c1", 1, "u1", cmd) == (True, "travel_watch_unavailable")
        h.handle_callback_query("cq", "c1", 1, "trv:m:home", {"id": "u1"})  # hub still works
        assert "메인 메뉴" in h.tg.send_text.call_args[0][1]
    finally:
        h.shutdown()


def test_settings_card_toggles_return_to_settings_and_reject_bad_suffix(handler, mem_db):
    for bad in ("trv:w:wb:x", "trv:w:wp:S", "trv:w:wb:s:1", "trv:w:wbs", "trv:w:wp:l"):
        _cb(handler, bad)
        assert "지원하지 않는" in handler.tg.answer_callback.call_args[1]["text"]
    assert mem_db.get_travel_user_prefs("c1")["updated_at"] is None  # nothing written

    handler._remember_watch_card("c1", 900)
    # legacy `:s` toggles from older settings messages are still handled (backward compatibility)
    _cb(handler, "trv:w:wb:s", mid=900)
    _mid, text, markup = handler.edits[-1]
    assert "[정기발송 설정]" in text and "정기 여행정보 발송을 껐어요" in text
    assert "정기발송: <b>꺼짐</b>" in text
    assert markup["inline_keyboard"][0][2] == {"text": "✅ 끄기", "callback_data": "trv:w:fq:O"}
    assert not {"trv:w:wp:s", "trv:w:wb:s", "trv:w:wp", "trv:w:wb"} & set(all_callbacks(markup))
    _cb(handler, "trv:w:wp:s", mid=900)  # Track B toggle lands on the 🔔 맞춤발송 card that owns it
    _mid, text, markup = handler.edits[-1]
    assert "[맞춤발송 설정]" in text and "일시중지했어요" in text and "[정기발송 설정]" not in text
    # 0 watches: never "일시중지" status wording, and no pause/reset buttons
    assert "등록된 도시가 없어 맞춤발송이 나가지 않아요" in text
    assert "trv:w:wp" not in all_callbacks(markup) and "trv:w:z" not in all_callbacks(markup)
    prefs = mem_db.get_travel_user_prefs("c1")
    assert prefs["weekly_brief_enabled"] is False and prefs["watches_paused"] is True
    # bare legacy toggles keep working too
    _cb(handler, "trv:w:wb", mid=900)
    assert "[정기발송 설정]" in handler.edits[-1][1] and "[맞춤발송 설정]" not in handler.edits[-1][1]
    _cb(handler, "trv:w:wp", mid=900)
    assert "[맞춤발송 설정]" in handler.edits[-1][1] and "다시 시작했어요" in handler.edits[-1][1]
    prefs = mem_db.get_travel_user_prefs("c1")
    assert prefs["weekly_brief_enabled"] is True and prefs["watches_paused"] is False
    _assert_callbacks_fit(handler)


def test_menu_query_refresh_callbacks_clear_only_that_users_state(handler, mem_db):
    from travel_brief.interactive import UserCooldownManager

    handler._launch_search = MagicMock()
    handler._invalidate_scope = MagicMock()
    for data in ("trv:m:home", "trv:m:popular", "trv:m:help", "trv:m:watch", "trv:m:settings",
                 "trv:m:search", "trv:q:FUK", "trv:r:ALL"):
        handler.cooldown = UserCooldownManager(cooldown_sec=0)
        handler._set_chat_state("g1", STATE_AWAITING_WATCH_PRICE, user_id="u2", dest_code="FUK")
        handler._set_chat_state("g1", STATE_AWAITING_CITY, user_id="u1")
        handler._get_draft("g1", "u2", "FUK")
        _cb(handler, data, chat="g1", mid=900, user="u1")
        assert handler.get_chat_state("g1", "u1") == STATE_IDLE, data
        assert handler.get_chat_state("g1", "u2") == STATE_AWAITING_WATCH_PRICE, data
        assert handler._peek_draft("g1", "u2", "FUK") is not None, data
    assert handler._launch_search.call_count == 3  # m:search, q:FUK, r:ALL


@pytest.mark.parametrize("cmd,reason,needle", [
    ("/menu", "travel_menu", "메인 메뉴"),
    ("/메뉴@KwTravelBot", "travel_menu", "메인 메뉴"),
    ("/watch", "travel_watch_list", "[맞춤발송 설정]"),
    ("/알림", "travel_watch_list", "[맞춤발송 설정]"),
    ("/settings", "travel_settings", "[정기발송 설정]"),
    ("/설정", "travel_settings", "[정기발송 설정]"),
    ("/help", "travel_help", "도움말"),
    ("/guide", "travel_guide", "직접 검색"),
    ("/search", "travel_guide", "직접 검색"),
])
def test_menu_slash_commands_and_group_state_isolation(handler, mem_db, cmd, reason, needle):
    handler._set_chat_state("g1", STATE_AWAITING_WATCH_PRICE, user_id="u2", dest_code="FUK")
    handler._set_chat_state("g1", STATE_AWAITING_WATCH_PRICE, user_id="u1", dest_code="NRT")
    assert handler.handle_text_message("g1", 1, "u1", cmd) == (True, reason)
    assert needle in handler.tg.send_text.call_args[0][1]
    expected = STATE_AWAITING_CITY if reason == "travel_guide" else STATE_IDLE
    assert handler.get_chat_state("g1", "u1") == expected
    assert handler.get_chat_state("g1", "u2") == STATE_AWAITING_WATCH_PRICE  # other member untouched
    if reason in ("travel_menu", "travel_watch_list", "travel_settings"):
        assert handler._is_watch_card("g1", 900)  # later navigation edits this card in place
    _assert_callbacks_fit(handler)


def test_all_slash_command_starts_top3_search_without_touching_other_members(handler, mem_db):
    handler._launch_search = MagicMock()
    handler._set_chat_state("g1", STATE_AWAITING_WATCH_PRICE, user_id="u2", dest_code="FUK")
    handler._set_chat_state("g1", STATE_AWAITING_CITY, user_id="u1")
    assert handler.handle_text_message("g1", 1, "u1", "/all") == (True, "travel_search_started")
    assert handler._launch_search.call_args[0][:4] == ("g1", 900, "ALL", "u1")
    assert handler.get_chat_state("g1", "u1") == STATE_IDLE
    assert handler.get_chat_state("g1", "u2") == STATE_AWAITING_WATCH_PRICE


def test_search_result_is_no_longer_an_in_place_navigation_card(handler, mem_db):
    handler._remember_watch_card("c1", 900)
    handler.watchdog_seconds = 0
    with patch.object(handler.worker_pool, "submit") as submit:
        handler._launch_search("c1", 900, "ALL", "u9", is_refresh=False, show_loading=False)
    assert submit.call_count == 1
    assert not handler._is_watch_card("c1", 900)


# ----------------------------------------------------------------------
# Track A V2 prefs: atomic freq/enabled sync, region/theme, migration backfill
# ----------------------------------------------------------------------
def test_prefs_v2_freq_enabled_invariant_and_validation(mem_db):
    p = mem_db.set_weekly_brief_freq("v1", "TUE_THU_2X")
    assert (p["weekly_brief_freq"], p["weekly_brief_enabled"]) == ("TUE_THU_2X", True)
    p = mem_db.set_weekly_brief_freq("v1", "off")
    assert (p["weekly_brief_freq"], p["weekly_brief_enabled"]) == ("OFF", False)
    p = mem_db.set_weekly_brief_enabled("v1", True)  # legacy setter restores the default cadence
    assert (p["weekly_brief_freq"], p["weekly_brief_enabled"]) == ("THU_1X", True)
    mem_db.set_weekly_brief_freq("v1", "TUE_THU_2X")
    assert mem_db.set_weekly_brief_enabled("v1", True)["weekly_brief_freq"] == "TUE_THU_2X"  # already on: kept
    p = mem_db.set_weekly_brief_enabled("v1", False)
    assert (p["weekly_brief_freq"], p["weekly_brief_enabled"]) == ("OFF", False)
    p = mem_db.set_weekly_brief_enabled("v1")  # toggle
    assert (p["weekly_brief_freq"], p["weekly_brief_enabled"]) == ("THU_1X", True)
    mem_db.set_weekly_brief_region("v1", "seasia")
    p = mem_db.set_weekly_brief_theme("v1", "RESORT")
    assert (p["weekly_brief_region"], p["weekly_brief_theme"], p["weekly_brief_freq"]) == ("SEASIA", "RESORT", "THU_1X")
    for bad_call in (
        lambda: mem_db.set_weekly_brief_freq("v1", "DAILY"),
        lambda: mem_db.set_weekly_brief_region("v1", "SEA"),  # Seattle's IATA code is never a region
        lambda: mem_db.set_weekly_brief_region("v1", "ASIA"),
        lambda: mem_db.set_weekly_brief_theme("v1", "BEACH"),
    ):
        with pytest.raises(ValueError):
            bad_call()
    assert mem_db.get_travel_user_prefs("v1")["weekly_brief_region"] == "SEASIA"  # failed writes changed nothing
    p = mem_db.set_watches_paused("v1", True)  # Track B toggles never desync Track A
    assert (p["weekly_brief_freq"], p["weekly_brief_enabled"], p["watches_paused"]) == ("THU_1X", True, True)
    with mem_db._connect() as conn:
        rows = conn.execute("SELECT weekly_brief_enabled, weekly_brief_freq FROM travel_user_prefs").fetchall()
    assert rows and all((r[1] == "OFF") == (r[0] == 0) for r in rows)


def test_prefs_v2_migration_backfills_legacy_rows_idempotently(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE travel_user_prefs (
               chat_id TEXT PRIMARY KEY,
               weekly_brief_enabled INTEGER NOT NULL DEFAULT 1,
               watches_paused INTEGER NOT NULL DEFAULT 0,
               updated_at TEXT NOT NULL)"""
    )
    conn.execute("INSERT INTO travel_user_prefs VALUES ('on', 1, 0, 'x')")
    conn.execute("INSERT INTO travel_user_prefs VALUES ('off', 0, 1, 'x')")
    conn.commit()
    conn.close()

    db = TravelBriefDatabase(db_path=str(path))
    with db._connect() as c:
        cols = {r[1] for r in c.execute("PRAGMA table_info(travel_user_prefs)").fetchall()}
    assert {"weekly_brief_region", "weekly_brief_theme", "weekly_brief_freq"} <= cols
    on, off = db.get_travel_user_prefs("on"), db.get_travel_user_prefs("off")
    assert (on["weekly_brief_freq"], on["weekly_brief_enabled"], on["weekly_brief_region"],
            on["weekly_brief_theme"]) == ("THU_1X", True, "ALL", "ALL")
    assert (off["weekly_brief_freq"], off["weekly_brief_enabled"], off["watches_paused"]) == ("OFF", False, True)
    # a legacy writer's raw INSERT picks up the column default -> normalised on read, repaired on restart
    with db._connect() as c:
        c.execute(
            "INSERT INTO travel_user_prefs (chat_id, weekly_brief_enabled, watches_paused, updated_at) "
            "VALUES ('raw', 0, 0, 'x')"
        )
    assert db.get_travel_user_prefs("raw")["weekly_brief_freq"] == "OFF"
    db2 = TravelBriefDatabase(db_path=str(path))  # idempotent re-run: no duplicate column, rows repaired
    with db2._connect() as c:
        stored = {r[0]: (r[1], r[2]) for r in c.execute(
            "SELECT chat_id, weekly_brief_enabled, weekly_brief_freq FROM travel_user_prefs").fetchall()}
    assert stored == {"on": (1, "THU_1X"), "off": (0, "OFF"), "raw": (0, "OFF")}


# ----------------------------------------------------------------------
# 4-line /settings, dormant mode, shared region -> city drilldown
# ----------------------------------------------------------------------
def test_settings_four_line_controls_region_theme_freq_and_dormant(handler, mem_db):
    handler._remember_watch_card("c1", 900)
    _cb(handler, "trv:w:fq:B", mid=900)
    prefs = mem_db.get_travel_user_prefs("c1")
    assert (prefs["weekly_brief_freq"], prefs["weekly_brief_enabled"]) == ("TUE_THU_2X", True)
    assert "정기발송: <b>주2회(화·목) 12:00</b>" in handler.edits[-1][1]
    assert handler.edits[-1][2]["inline_keyboard"][0][1]["text"] == "✅ 화·목 주2회"
    _cb(handler, "trv:w:rgp", mid=900)
    _mid, text, markup = handler.edits[-1]
    assert "관심권역 선택" in text
    assert {f"trv:w:rgn:{r}" for r in ("JP", "TWHK", "CNMN", "SEASIA", "PAC", "OCE", "NA", "EU", "ALL")} <= \
        set(all_callbacks(markup))
    assert [len(r) for r in markup["inline_keyboard"][:4]] == [2, 2, 2, 2]  # 8 regions as a 4x2 grid
    _cb(handler, "trv:w:rgn:SEASIA", mid=900)
    assert mem_db.get_travel_user_prefs("c1")["weekly_brief_region"] == "SEASIA"
    assert "관심권역: <b>🌴 동남아</b>" in handler.edits[-1][1]
    _cb(handler, "trv:w:th:R", mid=900)
    assert mem_db.get_travel_user_prefs("c1")["weekly_brief_theme"] == "RESORT"
    assert "여행스타일: <b>🏖️ 휴양</b>" in handler.edits[-1][1]
    before = mem_db.get_travel_user_prefs("c1")
    for bad in ("trv:w:fq:X", "trv:w:fq", "trv:w:fq:T:1", "trv:w:rgn:SEA", "trv:w:rgn:ASIA", "trv:w:th:Z",
                "trv:w:rgp:1"):
        _cb(handler, bad)
        assert "지원하지 않는" in handler.tg.answer_callback.call_args[1]["text"], bad
    assert mem_db.get_travel_user_prefs("c1") == before  # rejected -> nothing written

    # turning off the last active track (no watches) -> banner + ONE friendly notice
    _cb(handler, "trv:w:fq:O", mid=900)
    text = handler.edits[-1][1]
    assert text.startswith(DORMANT_BANNER) and text.count(DORMANT_NOTICE) == 1
    _cb(handler, "trv:w:th:A", mid=900)  # already dormant: banner stays, notice is not repeated
    assert DORMANT_BANNER in handler.edits[-1][1] and DORMANT_NOTICE not in handler.edits[-1][1]
    assert DORMANT_BANNER in handler.build_watch_list_card("c1")[0]  # /watch shows the same banner
    mem_db.upsert_travel_watch("c1", "FUK", 250_000)  # an active, unpaused watch leaves dormant mode
    assert DORMANT_BANNER not in handler.build_delivery_settings_card("c1")[0]
    _cb(handler, "trv:w:X:FUK", mid=900)  # deleting the last watch while weekly is OFF -> notice once more
    assert DORMANT_NOTICE in handler.edits[-1][1]
    _assert_callbacks_fit(handler)


def test_region_city_drilldown_for_search_keeps_guide_contract(handler, mem_db):
    handler._launch_search = MagicMock()
    _cb(handler, "trv:c:guide", mid=10)
    assert handler.get_chat_state("c1", "u1") == STATE_AWAITING_CITY  # legacy guide contract kept
    assert "기타 지역 직접 검색" in handler.tg.send_text.call_args[0][1]
    guide_kb = handler.tg.send_text.call_args[1]["reply_markup"]["inline_keyboard"]
    assert [len(r) for r in guide_kb[:4]] == [2, 2, 2, 2]
    assert guide_kb[1][1] == {"text": "🌴 동남아", "callback_data": "trv:srg:SEASIA:0"}
    _cb(handler, "trv:srg:JP:0", mid=900)  # the guide card is edited in place
    _mid, text, markup = handler.edits[-1]
    assert "일본 도시 선택" in text and "(1/3)" in text
    rows = markup["inline_keyboard"]
    city_rows = [r for r in rows if all(b["callback_data"].startswith("trv:q:") for b in r)]
    assert [len(r) for r in city_rows] == [3, 3, 3]  # 3x3 = 9 per page
    first = [b["callback_data"][6:] for r in city_rows for b in r]
    assert first[:6] == ["NRT", "KIX", "FUK", "CTS", "OKA", "NGO"]  # popularity first
    assert rows[-2] == [{"text": "다음 ▶", "callback_data": "trv:srg:JP:1"}]
    assert rows[-1] == [{"text": "↩️ 돌아가기", "callback_data": "trv:srg"}]
    _cb(handler, "trv:srg:JP:2", mid=900)
    last = handler.edits[-1][2]["inline_keyboard"]
    assert last[-2] == [{"text": "◀ 이전", "callback_data": "trv:srg:JP:1"}]
    names = [handler.catalog.get(b["callback_data"][6:]).meta.name
             for r in last for b in r if b["callback_data"].startswith("trv:q:")]
    assert names and names == sorted(names)  # the non-popular tail is 가나다 ordered
    _cb(handler, "trv:srg", mid=900)
    assert "trv:srg:EU:0" in all_callbacks(handler.edits[-1][2])
    _cb(handler, "trv:q:CTS", mid=900)
    assert handler._launch_search.call_args[0][2] == "CTS"
    for bad in ("trv:srg:SEA:0", "trv:srg:JP", "trv:srg:JP:x", "trv:srg:JP:0:1", "trv:srg:JP:9999"):
        _cb(handler, bad)
        assert "지원하지 않는" in handler.tg.answer_callback.call_args[1]["text"], bad
    _assert_callbacks_fit(handler)


def test_watch_add_region_pages_seattle_city_and_direct_key_in(handler, mem_db):
    _cb(handler, "trv:w:g", mid=10)
    _cb(handler, "trv:w:rg:NA:0", mid=900)
    _mid, text, markup = handler.edits[-1]
    assert "북미 도시 선택" in text and "[맞춤발송 등록 · 1/2단계: 도시 선택]" in text
    cbs = all_callbacks(markup)
    assert "trv:w:ct:NA:0:LAX" in cbs and "trv:w:rg:NA:1" in cbs and cbs[-1] == "trv:w:rg"
    _cb(handler, "trv:w:ct:NA:0:SEA", mid=900)  # Seattle (IATA SEA) is a city, never the SEASIA region
    setup = handler.edits[-1][1]
    assert "[맞춤발송 등록 · 2/2단계: " in setup and "시애틀 목표가 입력]" in setup
    assert "원하는 목표가를 누르거나" in setup
    _cb(handler, "trv:w:k:SEA:1", mid=900)
    assert mem_db.get_travel_watch("c1", "SEA")["is_active"] == 1
    _cb(handler, "trv:w:rg:NA:0", mid=900)
    assert any(b["text"] == "🔔 시애틀" for r in handler.edits[-1][2]["inline_keyboard"] for b in r)
    assert handler.get_chat_state("c1", "u1") == STATE_AWAITING_WATCH_CITY
    for bad in ("trv:w:rg:SEA:0", "trv:w:ct:SEA:0:LAX", "trv:w:ct:NA:0:@JP", "trv:w:ct:NA:0:XYZ", "trv:w:ki:1"):
        _cb(handler, bad)
        assert "지원하지 않는" in handler.tg.answer_callback.call_args[1]["text"], bad

    # direct key-in: AWAITING_WATCH_CITY (per user) -> city -> AWAITING_WATCH_PRICE -> typed price saves
    _cb(handler, "trv:w:ki", mid=900)
    assert handler.get_chat_state("c1", "u1") == STATE_AWAITING_WATCH_CITY
    assert handler.get_chat_state("c1", "u2") == STATE_IDLE
    assert handler.handle_text_message("c1", 1, "u1", "일본") == (True, "travel_watch_city_invalid")
    assert "국가 전체 알림은 지원하지 않아요" in handler.tg.send_text.call_args[0][1]
    assert handler.handle_text_message("c1", 2, "u1", "후쿠오까") == (True, "travel_watch_city_invalid")
    assert "trv:w:s:FUK" in all_callbacks(handler.tg.send_text.call_args[1]["reply_markup"])
    assert handler.get_chat_state("c1", "u1") == STATE_AWAITING_WATCH_CITY  # prompt stays alive
    assert handler.handle_text_message("c1", 3, "u1", "프라하") == (True, "travel_watch_city_selected")
    assert handler.get_chat_state("c1", "u1") == STATE_AWAITING_WATCH_PRICE
    assert "프라하" in handler.tg.send_text.call_args[0][1]
    assert handler.handle_text_message("c1", 4, "u1", "120만") == (True, "travel_watch_saved")
    w = mem_db.get_travel_watch("c1", "PRG")
    assert (w["target_price_krw"], w["preset_kind"]) == (1_200_000, "CUSTOM")
    # /cancel releases the city prompt
    _cb(handler, "trv:w:ki", mid=900)
    assert handler.handle_text_message("c1", 5, "u1", "/cancel") == (True, "travel_cancel")
    assert handler.get_chat_state("c1", "u1") == STATE_IDLE
    _assert_callbacks_fit(handler)


# ----------------------------------------------------------------------
# UX clarity: live status badge, 1:1 Track A/B cards, breadcrumbs, in-place guide, parallel nav ack
# ----------------------------------------------------------------------
def test_zero_watches_with_paused_flag_reads_unregistered_never_paused(handler, mem_db):
    mem_db.set_watches_paused("c1", True)
    assert mem_db.get_travel_user_prefs("c1")["watches_paused"] is True
    assert handler._format_status_badge(None) == []  # no chat -> no badge
    badge = handler._format_status_badge("c1")
    assert badge == ["📰 정기발송: <b>🟢 받는 중 (주1회(목) 12:00 · 🌏 전체 · ✨ 전체)</b>",
                     "🔔 맞춤발송: <b>⚪ 미등록 (도시 0/4)</b>"]
    hub, _ = handler.build_menu_hub_card("c1")
    onboarding = handler.build_onboarding_card(chat_id="c1")
    text, markup = handler.build_watch_list_card("c1")
    for card in (hub, onboarding, text):
        assert "일시중지" not in card and "켜짐" not in card
    assert "미등록 (도시 0/4)" in hub and "미등록 (도시 0/4)" in onboarding
    assert "⚪ 등록된 도시가 없어 맞춤발송이 나가지 않아요. 아래 [➕ 도시 추가하기]로 등록해 보세요!" in text
    cbs = all_callbacks(markup)
    assert "trv:w:wp" not in cbs and "trv:w:z" not in cbs  # pause / reset hidden with 0 watches
    assert "trv:w:g" in cbs and markup["inline_keyboard"][-1] == [
        {"text": "📰 정기발송 설정", "callback_data": "trv:m:settings"},
        {"text": "🏠 메뉴", "callback_data": "trv:m:home"}]

    # one watch while the flag is still set -> paused wording + resume button
    mem_db.upsert_travel_watch("c1", "FUK", 250_000)
    assert handler._format_status_badge("c1")[1] == "🔔 맞춤발송: <b>⏸️ 일시중지 (1/4)</b>"
    _t, markup = handler.build_watch_list_card("c1")
    assert markup["inline_keyboard"][-2] == [{"text": "▶️ 다시 받기", "callback_data": "trv:w:wp"},
                                             {"text": "🧹 초기화", "callback_data": "trv:w:z"}]
    mem_db.set_watches_paused("c1", False)
    assert handler._format_status_badge("c1")[1] == "🔔 맞춤발송: <b>🟢 받는 중 (1/4)</b>"
    mem_db.set_weekly_brief_freq("c1", "OFF")
    assert handler._format_status_badge("c1")[0] == "📰 정기발송: <b>🔴 꺼짐</b>"
    mem_db.set_weekly_brief_freq("c1", "TUE_THU_2X")
    mem_db.set_weekly_brief_region("c1", "SEASIA")
    mem_db.set_weekly_brief_theme("c1", "RESORT")
    assert handler._format_status_badge("c1")[0] == \
        "📰 정기발송: <b>🟢 받는 중 (주2회(화·목) 12:00 · 🌴 동남아 · 🏖️ 휴양)</b>"


def test_watch_setup_breadcrumbs_new_vs_edit_and_step1_cards(handler, mem_db):
    text, _ = handler.build_watch_target_picker_card()
    assert text.startswith("🔔 <b>[맞춤발송 등록 · 1/2단계: 도시 선택]</b>")
    text, _ = handler.build_watch_city_prompt_card()
    assert text.startswith("🔔 <b>[맞춤발송 등록 · 1/2단계: 도시 선택]</b>")
    text, _ = handler.build_city_page_card("w", "JP", 0, chat_id="c1")
    assert text.startswith("🔔 <b>[맞춤발송 등록 · 1/2단계: 도시 선택]</b>") and "일본 도시 선택 (1/" in text
    search_page, _ = handler.build_city_page_card("s", "JP", 0)
    assert "1/2단계" not in search_page  # search drilldown keeps its own header
    label = handler.label_for("FUK")
    new, _ = handler.build_watch_setup_card("c1", "u1", "FUK")
    assert f"🔔 <b>[맞춤발송 등록 · 2/2단계: {label} 목표가 입력]</b>" in new
    assert "원하는 목표가를 누르거나" in new
    mem_db.upsert_travel_watch("c1", "FUK", 250_000)
    edit, _ = handler.build_watch_setup_card("c1", "u1", "FUK")
    assert f"🔔 <b>[맞춤발송 수정 · {label}]</b>" in edit and "2/2단계" not in edit


def test_guide_callback_edits_tracked_cards_in_place_and_sends_for_untracked(handler, mem_db):
    _cb(handler, "trv:m:home", mid=10)  # hub opened from a result -> fresh, tracked navigation card (900)
    assert handler._is_watch_card("c1", 900)
    sends = handler.tg.send_text.call_count
    _cb(handler, "trv:c:guide", mid=900)
    mid, text, markup = handler.edits[-1]
    assert mid == 900 and "기타 지역 직접 검색" in text and "trv:srg:JP:0" in all_callbacks(markup)
    assert handler.tg.send_text.call_count == sends  # edited in place, no new message
    assert handler.get_chat_state("c1", "u1") == STATE_AWAITING_CITY
    assert "가고 싶은 도시" in handler.tg.answer_callback.call_args[1]["text"]
    # an untracked message (search result / alert) keeps its content -> a fresh guide card is sent
    n_edits = len(handler.edits)
    _cb(handler, "trv:c:guide", mid=555)
    assert len(handler.edits) == n_edits
    assert handler.tg.send_text.call_count == sends + 1
    assert "기타 지역 직접 검색" in handler.tg.send_text.call_args[0][1]


def test_navigation_ack_not_blocked_by_slow_ui_edit(mem_db):
    h = TravelInteractiveHandler(bot_token="dummy", db=mem_db)
    h.tg = MagicMock()
    h.tg.send_text.return_value = "900"
    started, release = threading.Event(), threading.Event()
    done: list[str] = []

    def slow_edit(chat_id, message_id, text, reply_markup=None):
        started.set()
        release.wait(5)
        done.append(text)
        return True

    h._safe_edit_message = slow_edit
    h._remember_watch_card("c1", 900)
    try:
        navs = ("trv:m:home", "trv:m:settings", "trv:m:watch", "trv:w:l", "trv:w:g", "trv:c:guide",
                "trv:srg:JP:0", "trv:w:rg:JP:0", "trv:w:rgp", "trv:w:s:FUK")
        for i, data in enumerate(navs):
            started.clear()
            release.clear()
            n = h.tg.answer_callback.call_count
            t0 = time.monotonic()
            h.handle_callback_query("cq", "c1", 900, data, {"id": "u1"})
            assert h.tg.answer_callback.call_count == n + 1, data  # exactly one ack, sent while the edit blocks
            assert time.monotonic() - t0 < 1.0, data
            assert started.wait(2), data  # the edit was already running on the UI pool
            assert len(done) == i, data  # ...and is still blocked behind the barrier
            release.set()
            end = time.monotonic() + 3
            while len(done) < i + 1 and time.monotonic() < end:
                time.sleep(0.01)
            assert len(done) == i + 1, data
    finally:
        release.set()
        h.shutdown()


def test_nav_ack_sent_before_inline_fallback_when_ui_pool_is_closed(mem_db):
    h = TravelInteractiveHandler(bot_token="dummy", db=mem_db)
    h.tg = MagicMock()
    h.tg.send_text.return_value = "900"
    order: list[str] = []
    h.tg.answer_callback.side_effect = lambda *a, **kw: order.append("ack")

    def edit(chat_id, message_id, text, reply_markup=None):
        order.append("edit")
        return True

    h._safe_edit_message = edit
    h._remember_watch_card("c1", 900)
    h.ui_pool.shutdown(wait=True)  # submit now raises -> inline fallback on the caller thread
    try:
        h.handle_callback_query("cq", "c1", 900, "trv:m:home", {"id": "u1"})
        assert order == ["ack", "edit"]
        order.clear()
        h.handle_callback_query("cq", "c1", 900, "trv:w:wb", {"id": "u1"})  # DB write: ack-first as before
        assert order == ["ack", "edit"]
    finally:
        h.shutdown()
