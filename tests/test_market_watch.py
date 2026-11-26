"""User-configured intraday volatility target watches (brief_bot) — L2 regression suite."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest import mock
from zoneinfo import ZoneInfo

from market_brief.calendar_manager import MarketCalendar
from market_brief.market_data.models import Quote
from market_brief.market_watch import (
    ASSET_CATALOG,
    CONDITION_CHANGE_BP,
    CONDITION_CHANGE_PCT,
    CONDITION_LEVEL,
    MAX_HTML_CHARS,
    MAX_VISIBLE_CHARS,
    ASSET_CONTEXT_MAP,
    DAYTIME_ACTIVE_SYMBOLS,
    QUICK_DEFAULT_SYMBOLS,
    MarketWatchEvaluator,
    MarketWatchService,
    WatchState,
    check_live_quote,
    dec_str,
    evaluate_reading,
    iter_callback_samples,
    legacy_urgent_broadcast_enabled,
    parse_level_input,
    render_watch_alert_card,
    render_watch_alert_html,
    resolve_watch_report_url,
    select_context_symbols,
    visible_len,
    _HtmlSendAdapter,
)
from market_brief.market_data.naver_provider import NewsItem
from market_brief.multi_bot_manager import (
    OFFICIAL_BOT_COMMANDS,
    DynamicBotManager,
    MultiBotInteractionRouter,
)
from market_brief.storage.db import MarketBriefDatabase
from market_brief.subscription_engine import SubscriptionEngine

KST = ZoneInfo("Asia/Seoul")
T0 = datetime(2026, 11, 12, 10, 0, tzinfo=KST)  # Thursday, KR + GLOBAL sessions open
UID = "700000001"
UID2 = "700000002"


def make_quote(symbol: str, value, prev, at: datetime, source: str | None = None, delayed: int = 0) -> Quote:
    meta = {
        "KOSPI": ("KR_EQUITY", "KR", "REGULAR", "INDEX"),
        "KOSDAQ": ("KR_EQUITY", "KR", "REGULAR", "INDEX"),
        "KOSPI200": ("KR_EQUITY", "KR", "REGULAR", "INDEX"),
        "SP500": ("US_EQUITY", "US", "REGULAR", "INDEX"),
        "NASDAQ_COMPOSITE": ("US_EQUITY", "US", "REGULAR", "INDEX"),
        "WTI": ("ALTERNATIVE", "GLOBAL", "REFERENCE", "PRICE"),
        "KR3Y": ("KR_BOND", "KR", "REFERENCE", "PERCENT_YIELD"),
        "US2Y": ("US_BOND", "US", "REFERENCE", "PERCENT_YIELD"),
        "USDKRW": ("ALTERNATIVE", "GLOBAL", "REFERENCE", "FX"),
        "GOLD": ("ALTERNATIVE", "GLOBAL", "REFERENCE", "PRICE"),
        "KR10Y": ("KR_BOND", "KR", "REFERENCE", "PERCENT_YIELD"),
        "US10Y": ("US_BOND", "US", "REFERENCE", "PERCENT_YIELD"),
    }[symbol]
    return Quote(
        symbol, meta[0], meta[1], meta[2], at.date(), at, Decimal(str(value)), Decimal(str(prev)), meta[3],
        source or f"NAVER:{symbol}", delayed_minutes=delayed,
    )


class FakeProvider:
    def __init__(self):
        self.quotes: dict[str, Quote] = {}
        self.calls: list[list[str]] = []

    def fetch(self, symbols):
        self.calls.append(list(symbols))
        return [self.quotes[s] for s in symbols if s in self.quotes]


class RecordingSender:
    def __init__(self, status="SENT"):
        self.status = status
        self.sent: list[tuple[str, str]] = []
        self.payloads: list = []

    def __call__(self, chat_id, payload):
        self.payloads.append(payload)
        html_text = payload["html"] if isinstance(payload, dict) else payload
        self.sent.append((chat_id, html_text))
        return self.status, (f"m{len(self.sent)}" if self.status == "SENT" else None), (None if self.status == "SENT" else self.status)


class DbTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mw_test_")
        self.db_path = str(Path(self.tmp) / "platform.sqlite3")
        self.db = MarketBriefDatabase(db_path=self.db_path)
        for uid in (UID, UID2):
            self._add_user(uid, "WHITE", subscribed=True)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _add_user(self, uid, tier, subscribed=True):
        with self.db._connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO users (telegram_user_id, full_name, tier, status) VALUES (?, 'T', ?, 'ACTIVE')",
                (uid, tier),
            )
            if subscribed:
                conn.execute(
                    """INSERT INTO subscriptions (telegram_user_id, bot_id, status) VALUES (?, 'brief_bot', 'ACTIVE')
                       ON CONFLICT(telegram_user_id, bot_id) DO UPDATE SET status='ACTIVE'""",
                    (uid,),
                )

    def _create(self, uid=UID, symbol="KOSPI", cond=CONDITION_CHANGE_PCT, direction="BOTH", threshold="3", unit=None):
        spec = ASSET_CATALOG[symbol]
        return self.db.create_market_watch(
            uid, uid, symbol=symbol, market=spec.session_market, quote_unit=spec.quote_unit,
            condition_type=cond, direction=direction, threshold_value=dec_str(Decimal(threshold)),
            threshold_unit=unit or (spec.change_unit if cond != CONDITION_LEVEL else spec.quote_unit),
        )


# =============================================================================
# Pure helpers
# =============================================================================


class TestCatalogAndInputs(unittest.TestCase):
    def test_asset_scope_and_session_mapping(self):
        self.assertEqual(
            set(ASSET_CATALOG),
            {"KOSPI", "KOSDAQ", "KOSPI200", "SP500", "NASDAQ_COMPOSITE", "USDKRW", "WTI", "GOLD", "KR3Y", "KR10Y", "US10Y", "US2Y"},
        )
        self.assertEqual(ASSET_CATALOG["KOSPI"].session_market, "KR")
        self.assertEqual(ASSET_CATALOG["KR10Y"].session_market, "KR")
        self.assertEqual(ASSET_CATALOG["SP500"].session_market, "US")
        for s in ("USDKRW", "WTI", "GOLD", "US10Y", "US2Y"):
            self.assertEqual(ASSET_CATALOG[s].session_market, "GLOBAL")
        self.assertEqual(ASSET_CATALOG["US10Y"].change_condition, CONDITION_CHANGE_BP)
        self.assertEqual(ASSET_CATALOG["GOLD"].change_condition, CONDITION_CHANGE_PCT)

    def test_parse_level_input(self):
        self.assertEqual(parse_level_input("1,400"), Decimal("1400"))
        self.assertEqual(parse_level_input(" 3.25 "), Decimal("3.25"))
        for bad in ("0", "-5", "NaN", "inf", "Infinity", "abc", "", "1e5", "0.000"):
            with self.assertRaises(ValueError, msg=bad):
                parse_level_input(bad)

    def test_dec_str_canonical(self):
        self.assertEqual(dec_str(Decimal("2.0")), "2")
        self.assertEqual(dec_str(Decimal("1.50")), "1.5")
        self.assertEqual(dec_str(Decimal("1E+3")), "1000")

    def test_callback_data_ascii_and_64_bytes(self):
        samples = list(iter_callback_samples())
        self.assertGreater(len(samples), 50)
        for cb in samples:
            self.assertTrue(cb.isascii(), cb)
            self.assertLessEqual(len(cb.encode("utf-8")), 64, cb)

    def test_legacy_gate_defaults_off(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LEGACY_URGENT_BROADCAST_ENABLED", None)
            self.assertFalse(legacy_urgent_broadcast_enabled())
        with mock.patch.dict(os.environ, {"LEGACY_URGENT_BROADCAST_ENABLED": "true"}):
            self.assertTrue(legacy_urgent_broadcast_enabled())

    def test_brief_bot_commands_registered(self):
        cmds = {c["command"] for c in OFFICIAL_BOT_COMMANDS["brief_bot"]}
        self.assertTrue({"watch", "alert", "start", "help"} <= cmds)


class TestLiveQuoteGate(unittest.TestCase):
    def setUp(self):
        self.cal = MarketCalendar()

    def test_accepts_fresh_naver_quote_in_open_session(self):
        q = make_quote("KOSPI", 2700, 2650, T0 - timedelta(seconds=30))
        self.assertEqual(check_live_quote(q, T0, self.cal), (True, "OK"))
        q_fx = make_quote("USDKRW", 1400, 1390, T0, source="NAVER_FX")
        self.assertTrue(check_live_quote(q_fx, T0, self.cal)[0])

    def test_rejects_mock_fixture_fallback_and_delayed(self):
        for src in ("MOCK_FIXTURE", "FIXTURE:KR", "FALLBACK", "NAVER_FALLBACK", "FRED:DGS10"):
            q = make_quote("KOSPI", 2700, 2650, T0, source=src)
            self.assertFalse(check_live_quote(q, T0, self.cal)[0], src)
        delayed = make_quote("KOSPI", 2700, 2650, T0, delayed=15)
        self.assertEqual(check_live_quote(delayed, T0, self.cal), (False, "DELAYED_QUOTE"))

    def test_rejects_stale_future_and_closed_session(self):
        stale = make_quote("KOSPI", 2700, 2650, T0 - timedelta(seconds=121))
        self.assertEqual(check_live_quote(stale, T0, self.cal)[1], "STALE_QUOTE")
        future = make_quote("KOSPI", 2700, 2650, T0 + timedelta(seconds=121))
        self.assertEqual(check_live_quote(future, T0, self.cal)[1], "FUTURE_QUOTE")
        evening = datetime(2026, 11, 12, 16, 0, tzinfo=KST)
        closed = make_quote("KOSPI", 2700, 2650, evening)
        self.assertEqual(check_live_quote(closed, evening, self.cal)[1], "SESSION_CLOSED")
        saturday = datetime(2026, 11, 14, 10, 0, tzinfo=KST)
        fx_weekend = make_quote("USDKRW", 1400, 1390, saturday)
        self.assertEqual(check_live_quote(fx_weekend, saturday, self.cal)[1], "SESSION_CLOSED")


class TestStateMachine(unittest.TestCase):
    def _run(self, readings, cond=CONDITION_CHANGE_PCT, direction="BOTH", threshold="3", unit="INDEX", new_session_at=()):
        st = WatchState(False, False, direction, 0, "")
        fired = []
        for i, m in enumerate(readings):
            st, side = evaluate_reading(
                st, condition_type=cond, direction=direction, threshold=Decimal(threshold), quote_unit=unit,
                metric=Decimal(str(m)), is_new_session=i in new_session_at,
            )
            fired.append(side)
        return st, fired

    def test_baseline_never_triggers_even_if_already_breached(self):
        st, fired = self._run([-4.0, -4.1, -4.2])
        self.assertEqual(fired, [None, None, None])
        self.assertFalse(st.is_armed)

    def test_two_consecutive_confirmations_required(self):
        _, fired = self._run([0.5, 3.1, 0.2, 3.2, 3.3])
        self.assertEqual(fired, [None, None, None, None, "UP"])

    def test_hysteresis_rearm_at_half_threshold(self):
        _, fired = self._run([0.0, -3.1, -3.2, -2.0, -3.3, -3.4, -1.4, -3.1, -3.2])
        self.assertEqual(fired, [None, None, "DOWN", None, None, None, None, None, "DOWN"])

    def test_direction_filter(self):
        _, fired = self._run([0.0, -3.5, -3.6, -3.7], direction="UP")
        self.assertEqual(fired, [None, None, None, None])

    def test_level_both_crossings_with_buffer(self):
        readings = [1390, 1401, 1402, 1399, 1398, 1404, 1397, 1396]
        _, fired = self._run(readings, cond=CONDITION_LEVEL, threshold="1400", unit="FX")
        # up-cross confirmed at 1402; tiny dip to 1399/1398 is inside buffer (1400-2.8) -> no flapping;
        # 1404 >= 1400+2.8 re-arms DOWN; 1397/1396 confirms the down-cross.
        self.assertEqual(fired, [None, None, "UP", None, None, None, None, "DOWN"])

    def test_change_rearms_on_new_session(self):
        _, fired = self._run([0.0, 3.1, 3.2, 3.3, 3.4], new_session_at=(3,))
        self.assertEqual(fired, [None, None, "UP", None, "UP"])

    def test_bp_condition(self):
        _, fired = self._run([1, 10.5, 11], cond=CONDITION_CHANGE_BP, threshold="10", unit="PERCENT_YIELD")
        self.assertEqual(fired, [None, None, "UP"])


# =============================================================================
# DB / ledger
# =============================================================================


class TestWatchStorage(DbTestCase):
    def test_tables_and_hysteresis_columns_exist(self):
        with self.db._connect() as conn:
            cols = {r["name"] for r in conn.execute("PRAGMA table_info(market_volatility_watches)")}
            self.assertTrue({"armed_side", "confirm_streak", "confirm_side", "threshold_value", "condition_version"} <= cols)
            dcols = {r["name"] for r in conn.execute("PRAGMA table_info(market_watch_deliveries)")}
            self.assertTrue({"delivery_key", "status", "lease_token", "lease_expires_at", "attempt_no"} <= dcols)
            ttype = {r["name"]: r["type"] for r in conn.execute("PRAGMA table_info(market_volatility_watches)")}
            self.assertEqual(ttype["threshold_value"], "TEXT")
        MarketBriefDatabase(db_path=self.db_path)  # idempotent re-migration

    def test_duplicate_and_tier_cap_and_reactivation(self):
        self.assertTrue(self._create()["success"])
        self.assertEqual(self._create()["code"], "DUPLICATE")
        self.assertTrue(self._create(symbol="KOSDAQ")["success"])
        self.assertTrue(self._create(symbol="SP500")["success"])
        res = self._create(symbol="GOLD")
        self.assertEqual((res["code"], res["limit"]), ("LIMIT_EXCEEDED", 3))
        wid = self.db.list_market_watches(UID)[0]["watch_id"]
        self.assertTrue(self.db.deactivate_market_watch(UID, wid))
        self.assertTrue(self._create()["success"])  # partial unique index only covers active rows

    def test_owner_guard_on_deactivate(self):
        wid = self._create()["watch_id"]
        self.assertFalse(self.db.deactivate_market_watch(UID2, wid))
        self.assertTrue(self.db.get_market_watch(wid)["is_active"])

    def test_evaluable_requires_active_subscription_and_caps_tier(self):
        self._add_user(UID, "BLACK")
        for sym in ("KOSPI", "KOSDAQ", "KOSPI200", "SP500", "GOLD"):
            self.assertTrue(self._create(symbol=sym)["success"])
        self.assertEqual(len(self.db.list_evaluable_market_watches()), 5)
        self._add_user(UID, "WHITE")  # downgrade -> evaluator caps to oldest 3
        ev = self.db.list_evaluable_market_watches()
        self.assertEqual([w["symbol"] for w in ev], ["KOSPI", "KOSDAQ", "KOSPI200"])
        with self.db._connect() as conn:
            conn.execute("UPDATE subscriptions SET status='CANCELLED' WHERE telegram_user_id=?", (UID,))
        self.assertEqual(self.db.list_evaluable_market_watches(), [])

    def _claim(self, key="mw:v1:1:KR:2026-11-12:1", **kw):
        args = dict(
            delivery_key=key, watch_id=1, chat_id=UID, symbol="KOSPI", market="KR", session_id="KR:2026-11-12",
            trade_date="2026-11-12", trigger_side="UP", observed_value="2800", observed_change="3.1",
            quote_as_of=T0.isoformat(), lease_owner="t",
        )
        args.update(kw)
        return self.db.claim_market_watch_delivery(**args)

    def test_ledger_blocks_sent_inflight_unknown_and_retries_failed(self):
        ok, _, tok = self._claim()
        self.assertTrue(ok)
        self.assertEqual(self._claim()[1], "ALREADY_IN_FLIGHT")
        self.db.finalize_market_watch_delivery("mw:v1:1:KR:2026-11-12:1", tok, "SENT", "m1")
        self.assertEqual(self._claim()[1], "ALREADY_SENT")

        ok, _, tok = self._claim(key="k_unknown")
        self.db.finalize_market_watch_delivery("k_unknown", tok, "UNKNOWN", None, "timeout")
        self.assertEqual(self._claim(key="k_unknown")[1], "ALREADY_UNKNOWN")

        ok, _, tok = self._claim(key="k_failed")
        self.db.finalize_market_watch_delivery("k_failed", tok, "FAILED", None, "400")
        ok2, reason, tok2 = self._claim(key="k_failed")
        self.assertEqual((ok2, reason), (True, "RECLAIMED_FAILED"))
        self.db.finalize_market_watch_delivery("k_failed", tok2, "FAILED", None, "400")
        _, _, tok3 = self._claim(key="k_failed")
        self.db.finalize_market_watch_delivery("k_failed", tok3, "FAILED", None, "400")
        self.assertEqual(self._claim(key="k_failed")[1], "MAX_ATTEMPTS_EXCEEDED")

    def test_expired_lease_is_quarantined_to_unknown(self):
        ok, _, _ = self._claim(key="k_exp", lease_seconds=-5)
        self.assertTrue(ok)
        self.assertEqual(self._claim(key="k_exp")[1], "ALREADY_UNKNOWN")
        self.assertEqual(self.db.get_market_watch_delivery("k_exp")["status"], "UNKNOWN")


# =============================================================================
# Evaluator
# =============================================================================


class TestEvaluator(DbTestCase):
    def setUp(self):
        super().setUp()
        self.provider = FakeProvider()
        self.sender = RecordingSender()

    def _evaluator(self, sender=None):
        return MarketWatchEvaluator(
            self.db, provider=self.provider, sender=sender or self.sender, runner_id="t", news_fetcher=lambda: []
        )

    def _tick(self, minute, kospi_pct=None, fx=None, ev=None, source=None):
        at = T0 + timedelta(minutes=minute)
        if kospi_pct is not None:
            self.provider.quotes["KOSPI"] = make_quote("KOSPI", Decimal("2700") * (1 + Decimal(str(kospi_pct)) / 100), 2700, at, source=source)
        if fx is not None:
            self.provider.quotes["USDKRW"] = make_quote("USDKRW", fx, 1385, at, source=source)
        return (ev or self._evaluator()).run_once(at)

    def test_no_watches_means_no_fetch(self):
        res = self._evaluator().run_once(T0)
        self.assertEqual(res.skipped_reason, "NO_ACTIVE_WATCHES")
        self.assertEqual(self.provider.calls, [])

    def test_full_cycle_grouped_message_no_duplicate_and_restart_safe(self):
        self._create(threshold="2", direction="DOWN")
        self._create(symbol="USDKRW", cond=CONDITION_LEVEL, direction="UP", threshold="1400")
        self.assertEqual(self._tick(0, kospi_pct=-0.5, fx=1390).fired, 0)  # baseline
        self.assertEqual(self._tick(2, kospi_pct=-2.3, fx=1401).fired, 0)  # confirm 1
        # New evaluator instance == process restart: streak persisted in SQLite
        res = self._tick(4, kospi_pct=-2.6, fx=1403, ev=self._evaluator())
        self.assertEqual((res.fired, res.sent), (2, 1))
        self.assertEqual(len(self.sender.sent), 1)  # one grouped message for both watches
        html_text = self.sender.sent[0][1]
        self.assertIn("코스피", html_text)
        self.assertIn("원/달러", html_text)
        # Re-arm and re-cross in the same session -> ledger blocks a second send
        self._tick(6, kospi_pct=-0.5)
        self._tick(8, kospi_pct=-2.5)
        self._tick(10, kospi_pct=-2.7)
        self.assertEqual(len(self.sender.sent), 1)
        with self.db._connect() as conn:
            rows = conn.execute("SELECT delivery_key, status FROM market_watch_deliveries").fetchall()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(r["status"] == "SENT" and r["delivery_key"].startswith("mw:v1:") for r in rows))

    def test_same_reading_not_double_counted(self):
        self._create(threshold="2", direction="DOWN")
        self._tick(0, kospi_pct=0)
        self._tick(1, kospi_pct=-2.5)
        ev = self._evaluator()
        same_at = T0 + timedelta(minutes=1)
        self.assertEqual(ev.run_once(same_at + timedelta(seconds=30)).fired, 0)  # same as_of quote again
        w = self.db.list_market_watches(UID)[0]
        self.assertEqual(w["confirm_streak"], 1)

    def test_fixture_or_fallback_quotes_never_count(self):
        self._create(threshold="2", direction="DOWN")
        self._tick(0, kospi_pct=0)
        for i in range(1, 5):
            res = self._tick(i, kospi_pct=-5, source="FIXTURE:KR")
            self.assertEqual(res.rejected_quotes.get("KOSPI"), "REJECTED_SOURCE")
        self.assertEqual(self.sender.sent, [])
        self.assertEqual(self.db.list_market_watches(UID)[0]["confirm_streak"], 0)

    def test_unknown_is_never_resent_and_failed_is_retried(self):
        self._create(threshold="2", direction="DOWN")
        unknown_sender = RecordingSender("UNKNOWN")
        ev = self._evaluator(unknown_sender)
        self._tick(0, kospi_pct=0, ev=ev)
        self._tick(1, kospi_pct=-2.5, ev=ev)
        self._tick(2, kospi_pct=-2.6, ev=ev)
        self._tick(3, kospi_pct=0, ev=ev)
        self._tick(4, kospi_pct=-2.6, ev=ev)
        self._tick(5, kospi_pct=-2.7, ev=ev)
        self.assertEqual(len(unknown_sender.sent), 1)

        self._create(uid=UID2, threshold="2", direction="DOWN")
        failing = RecordingSender("FAILED")
        ev2 = self._evaluator(failing)
        # UID's watch is UNKNOWN-blocked; UID2 starts baseline at minute 10
        self._tick(10, kospi_pct=0, ev=ev2)
        self._tick(11, kospi_pct=-2.5, ev=ev2)
        self._tick(12, kospi_pct=-2.6, ev=ev2)
        self._tick(13, kospi_pct=-2.7, ev=ev2)
        self._tick(14, kospi_pct=-2.8, ev=ev2)
        uid2_msgs = [s for s in failing.sent if s[0] == UID2]
        self.assertEqual(len(uid2_msgs), 2)  # explicit FAILED re-armed -> re-claimed once confirmed again

    def test_inactive_or_unsubscribed_watch_is_skipped(self):
        self._create(threshold="2", direction="DOWN")
        with self.db._connect() as conn:
            conn.execute("UPDATE subscriptions SET status='CANCELLED' WHERE telegram_user_id=?", (UID,))
        self.assertEqual(self._tick(0, kospi_pct=0).skipped_reason, "NO_ACTIVE_WATCHES")

    def test_render_limits_worst_case_twenty_watches(self):
        items = []
        for i in range(20):
            sym = list(ASSET_CATALOG)[i % len(ASSET_CATALOG)]
            spec = ASSET_CATALOG[sym]
            q = Quote(sym, "ALTERNATIVE", "GLOBAL", "REFERENCE", T0.date(), T0, Decimal("123456.789"), Decimal("120000"),
                      "PRICE" if spec.quote_unit != "PERCENT_YIELD" else "PERCENT_YIELD", "NAVER:X")
            w = {"symbol": sym, "condition_type": CONDITION_LEVEL, "direction": "BOTH",
                 "threshold_value": "123456.789", "quote_unit": spec.quote_unit}
            items.append({"watch": w, "quote": q, "side": "UP"})
        text = render_watch_alert_html(items, T0)
        self.assertLessEqual(visible_len(text), MAX_VISIBLE_CHARS)
        self.assertLessEqual(len(text), MAX_HTML_CHARS)
        self.assertIn("S&amp;P500", text)
        self.assertNotIn("S&P500", text)


# =============================================================================
# Rich alert card (bounded renderer / context / news / buttons / payload adapter)
# =============================================================================


def _item(symbol, side="UP", value=None, cond=CONDITION_CHANGE_PCT, threshold="3", direction="BOTH"):
    spec = ASSET_CATALOG[symbol]
    is_yield = spec.quote_unit == "PERCENT_YIELD"
    value = value if value is not None else (Decimal("3.5") if is_yield else Decimal("2780"))
    prev = Decimal("3.3") if is_yield else Decimal("2700")
    q = make_quote(symbol, value, prev, T0)
    w = {"watch_id": 1, "symbol": symbol, "condition_type": cond, "direction": direction,
         "threshold_value": threshold, "quote_unit": spec.quote_unit}
    return {"watch": w, "quote": q, "side": side}


def _all_context_quotes():
    out = {}
    for sym, spec in ASSET_CATALOG.items():
        if spec.quote_unit == "PERCENT_YIELD":
            out[sym] = make_quote(sym, "3.512", "3.401", T0)
        else:
            out[sym] = make_quote(sym, "1401.5", "1385", T0)
    return out


def _evil_news(n=5):
    title = '<script>alert("x")</script> 코스피 급락 & "외국인" 매도 폭탄에 시장 공포 확산 ' * 4
    return [NewsItem(title, "s", "NAVER", 'https://n.news.naver.com/a?b=1&c="2"<x>', "2026-11-12") for _ in range(n)]


class TestRichAlertCard(unittest.TestCase):
    def test_bounded_with_long_news_and_twenty_watches(self):
        items = [
            _item(list(ASSET_CATALOG)[i % len(ASSET_CATALOG)], value=Decimal("123456789.987"),
                  threshold="123456.789", cond=CONDITION_LEVEL)
            for i in range(20)
        ]
        text = render_watch_alert_html(items, T0, context_quotes=_all_context_quotes(), news=_evil_news())
        self.assertLessEqual(visible_len(text), MAX_VISIBLE_CHARS)
        self.assertLessEqual(len(text), MAX_HTML_CHARS)
        self.assertIn("↳ 외 15건 도달", text)
        self.assertNotIn("<script>", text)
        self.assertNotIn('"외국인"', text)
        self.assertNotIn('c="2"', text)

    def test_news_capped_truncated_and_escaped(self):
        text = render_watch_alert_html([_item("KOSPI")], T0, news=_evil_news(5))
        self.assertIn("관련 시황 뉴스", text)
        self.assertEqual(text.count("<a href="), 2)  # max 2 headlines
        self.assertIn("&lt;script&gt;", text)
        self.assertIn("&amp;c=&quot;2&quot;&lt;x&gt;", text)
        news_lines = [ln for ln in text.splitlines() if ln.startswith("• <a href=")]
        self.assertEqual(len(news_lines), 2)
        for ln in news_lines:
            self.assertLessEqual(visible_len(ln), len("• ") + 35)

    def test_never_raises_and_degrades_news_then_context_then_watches(self):
        import market_brief.market_watch as mw

        items = [_item("KOSPI"), _item("USDKRW")]
        ctx, news = _all_context_quotes(), _evil_news(2)
        full = render_watch_alert_html(items, T0, context_quotes=ctx, news=news)
        no_news = render_watch_alert_html(items, T0, context_quotes=ctx)
        bare = render_watch_alert_html(items, T0)
        self.assertIn("관련 시황 뉴스", full)
        with mock.patch.object(mw, "MAX_VISIBLE_CHARS", visible_len(no_news)):
            out = render_watch_alert_html(items, T0, context_quotes=ctx, news=news)
        self.assertEqual(out, no_news)  # news dropped first
        self.assertIn("연관 지표", out)
        with mock.patch.object(mw, "MAX_VISIBLE_CHARS", visible_len(bare)):
            self.assertEqual(render_watch_alert_html(items, T0, context_quotes=ctx, news=news), bare)  # then context
        with mock.patch.object(mw, "MAX_VISIBLE_CHARS", visible_len(bare) - 1):
            out = render_watch_alert_html(items, T0, context_quotes=ctx, news=news)
        self.assertIn("↳ 외 1건 도달", out)  # then watch lines
        with mock.patch.object(mw, "MAX_VISIBLE_CHARS", 1), mock.patch.object(mw, "MAX_HTML_CHARS", 1):
            self.assertIn("타겟 2건 도달", render_watch_alert_html(items, T0, context_quotes=ctx, news=news))

    def test_context_matrix(self):
        self.assertEqual(set(ASSET_CONTEXT_MAP), set(ASSET_CATALOG))
        for sym, ctx in ASSET_CONTEXT_MAP.items():
            self.assertNotIn(sym, ctx)
            self.assertTrue(set(ctx) <= set(ASSET_CATALOG), sym)
        self.assertEqual(select_context_symbols([_item("KOSPI")]), ["USDKRW", "KOSDAQ", "SP500"])
        # dedup, triggered assets excluded, max 3
        self.assertEqual(select_context_symbols([_item("KOSPI"), _item("USDKRW")]), ["KOSDAQ", "SP500", "US10Y"])
        text = render_watch_alert_html([_item("KOSPI")], T0, context_quotes=_all_context_quotes())
        self.assertIn("🔗 <b>연관 지표</b>", text)
        self.assertIn("원/달러 1,401.50 (+1.19%)", text)
        self.assertIn("S&amp;P500", text)
        self.assertNotIn("연관 지표", render_watch_alert_html([_item("KOSPI")], T0, context_quotes={}))

    def test_inline_buttons_and_callback_bytes(self):
        url = "http://161.33.196.211:8002/reports/latest.html"
        single = render_watch_alert_card([_item("KOSPI")], T0, report_url=url)["inline_keyboard"]
        self.assertEqual(single[0], [{"text": "📊 실시간 웹 리포트 보기", "url": url}])
        self.assertEqual([b["callback_data"] for b in single[1]], ["mbw:s:KOSPI"])
        self.assertEqual(single[1][0]["text"], "⚙️ 코스피 타겟 변경")
        two = render_watch_alert_card([_item("KOSPI"), _item("KOSPI"), _item("USDKRW")], T0)["inline_keyboard"]
        self.assertEqual(len(two), 1)  # no report URL -> row omitted
        self.assertEqual([b["callback_data"] for b in two[0]], ["mbw:s:KOSPI", "mbw:s:USDKRW"])
        many = render_watch_alert_card([_item("KOSPI"), _item("KOSDAQ"), _item("USDKRW")], T0)["inline_keyboard"]
        self.assertEqual(many, [[{"text": "⚙️ 타겟 설정 변경", "callback_data": "mbw:m"}]])
        for sym in ASSET_CATALOG:
            for row in render_watch_alert_card([_item(sym)], T0, report_url=url)["inline_keyboard"]:
                for b in row:
                    if "callback_data" in b:
                        self.assertTrue(b["callback_data"].isascii())
                        self.assertLessEqual(len(b["callback_data"].encode("utf-8")), 64)

    def test_report_url_resolution_requires_public_absolute_url(self):
        cases = {
            "": "http://161.33.196.211:8002/reports/latest.html",
            "http://161.33.196.211:8002/": "http://161.33.196.211:8002/reports/latest.html",
            "https://reports.example.com/reports": "https://reports.example.com/reports/latest.html",
            "https://reports.example.com/x/latest.html": "https://reports.example.com/x/latest.html",
            "http://localhost:8080": None,
            "http://127.0.0.1:8002/": None,
            "/reports/latest.html": None,
        }
        for base, expected in cases.items():
            with mock.patch.dict(os.environ, {"REPORT_BASE_URL": base}):
                self.assertEqual(resolve_watch_report_url(), expected, base)


class FakeSendTransport:
    def __init__(self):
        self.calls = []

    def send_html(self, chat_id, html_text, inline_keyboard=None, reply_markup=None):
        self.calls.append((chat_id, html_text, inline_keyboard))
        return {"ok": True, "result": {"message_id": 77}}


class TestHtmlSendAdapter(unittest.TestCase):
    def test_dict_and_str_payloads(self):
        t = FakeSendTransport()
        ad = _HtmlSendAdapter(t)
        kb = [[{"text": "x", "callback_data": "mbw:m"}]]
        self.assertEqual(ad.send("1", {"html": "<b>a</b>", "inline_keyboard": kb}), "77")
        self.assertEqual(ad.send("1", "<b>legacy</b>"), "77")
        self.assertEqual(t.calls, [("1", "<b>a</b>", kb), ("1", "<b>legacy</b>", None)])

    def test_rejects_unsupported_payloads(self):
        t = FakeSendTransport()
        ad = _HtmlSendAdapter(t)
        for bad in (None, 123, b"bytes", {"text": "no html"}, {"html": 5}):
            with self.assertRaises(TypeError, msg=repr(bad)):
                ad.send("1", bad)
        self.assertEqual(t.calls, [])

    def test_telegram_watch_sender_forwards_dict(self):
        from market_brief.market_watch import TelegramWatchSender

        class _Dispatcher:
            def send_to_recipient(self, tg, recipient, payload, db=None):
                return "SENT", tg.send(recipient.chat_id, payload), None

        t = FakeSendTransport()
        sender = TelegramWatchSender("TOKEN", dispatcher=_Dispatcher(), transport=t)
        kb = [[{"text": "a", "url": "https://a.example.com"}]]
        self.assertEqual(sender("42", {"html": "hi", "inline_keyboard": kb}), ("SENT", "77", None))
        self.assertEqual(t.calls, [("42", "hi", kb)])


class TestEvaluatorRichCard(DbTestCase):
    def setUp(self):
        super().setUp()
        self.provider = FakeProvider()
        self.sender = RecordingSender()
        self.news_calls = 0

    def _news(self):
        self.news_calls += 1
        return [NewsItem("외국인 매도에 코스피 급락", "", "NAVER", "https://n.news.naver.com/1", "")]

    def _ev(self, news_fetcher=None, sender="default"):
        return MarketWatchEvaluator(
            self.db, provider=self.provider, sender=self.sender if sender == "default" else sender,
            runner_id="t", news_fetcher=news_fetcher or self._news,
        )

    def _set(self, minute, pct):
        at = T0 + timedelta(minutes=minute)
        self.provider.quotes["KOSPI"] = make_quote("KOSPI", Decimal("2700") * (1 + Decimal(str(pct)) / 100), 2700, at)
        return at

    def test_context_and_news_fetched_only_when_fired_and_shared_across_chats(self):
        self._create(uid=UID, threshold="2", direction="DOWN")
        self._create(uid=UID2, threshold="2", direction="DOWN")
        self.provider.quotes["KOSDAQ"] = make_quote("KOSDAQ", 850, 860, T0)
        self.provider.quotes["SP500"] = make_quote("SP500", 6000, 5950, T0)
        self.provider.quotes["USDKRW"] = make_quote("USDKRW", 1401, 1385, T0, source="FIXTURE:FX")  # never shown
        ev = self._ev()
        ev.run_once(self._set(0, 0))
        ev.run_once(self._set(1, -2.5))
        self.assertEqual(self.provider.calls, [["KOSPI"], ["KOSPI"]])  # no context fetch without a trigger
        self.assertEqual(self.news_calls, 0)
        res = ev.run_once(self._set(2, -2.6))
        self.assertEqual((res.fired, res.sent), (2, 2))
        context_calls = self.provider.calls[3:]
        self.assertEqual(sorted(c[0] for c in context_calls), ["KOSDAQ", "SP500", "USDKRW"])  # once per tick
        self.assertEqual(self.news_calls, 1)  # shared by both chats
        self.assertEqual(len(self.sender.payloads), 2)
        for payload in self.sender.payloads:
            self.assertIsInstance(payload, dict)
            self.assertIn("연관 지표", payload["html"])
            self.assertIn("코스닥", payload["html"])
            self.assertNotIn("원/달러", payload["html"])  # fixture context quote rejected
            self.assertIn("외국인 매도에 코스피 급락", payload["html"])
            cbs = [b.get("callback_data") for row in payload["inline_keyboard"] for b in row]
            self.assertIn("mbw:s:KOSPI", cbs)
        self.assertEqual(res.messages[0][1], self.sender.payloads[0]["html"])

    def test_news_and_context_failures_never_break_alert(self):
        self._create(threshold="2", direction="DOWN")

        def boom():
            raise RuntimeError("news down")

        class FlakyProvider(FakeProvider):
            def fetch(self, symbols):
                if symbols != ["KOSPI"]:
                    raise OSError("ctx down")
                return super().fetch(symbols)

        self.provider = FlakyProvider()
        ev = self._ev(news_fetcher=boom)
        for minute, pct in ((0, 0), (1, -2.5), (2, -2.6)):
            res = ev.run_once(self._set(minute, pct))
        self.assertEqual(res.sent, 1)
        html_text = self.sender.payloads[0]["html"]
        self.assertNotIn("연관 지표", html_text)
        self.assertNotIn("관련 시황 뉴스", html_text)
        with self.db._connect() as conn:
            statuses = [r["status"] for r in conn.execute("SELECT status FROM market_watch_deliveries")]
        self.assertEqual(statuses, ["SENT"])

    def test_shadow_mode_fetches_no_context_or_news(self):
        self._create(threshold="2", direction="DOWN")
        ev = self._ev(sender=None)
        for minute, pct in ((0, 0), (1, -2.5), (2, -2.6)):
            ev.run_once(self._set(minute, pct))
        self.assertTrue(all(c == ["KOSPI"] for c in self.provider.calls))
        self.assertEqual(self.news_calls, 0)


# =============================================================================
# Telegram UX / router / manager
# =============================================================================


class FakeTransport:
    def __init__(self):
        self.calls: list[tuple] = []

    def answer_callback(self, cq_id, text="", show_alert=False):
        self.calls.append(("answer", cq_id, text))

    def edit_message_text(self, chat_id, message_id, html_text, inline_keyboard=None):
        self.calls.append(("edit", chat_id, html_text, inline_keyboard))
        return {"ok": True}

    def send_html(self, chat_id, html_text, inline_keyboard=None, reply_markup=None):
        self.calls.append(("send", chat_id, html_text, inline_keyboard))
        return {"ok": True, "result": {"message_id": 1}}


class TestRouterAndManager(DbTestCase):
    def setUp(self):
        super().setUp()
        self.engine = SubscriptionEngine(self.db)
        self.router = MultiBotInteractionRouter(self.engine)
        self.uid = "700000099"
        self.engine.attempt_subscribe(self.uid, "brief_bot", full_name="Router")

    def _watch(self, text=None, cb=None):
        return self.router.handle_interaction("brief_bot", self.uid, text=text, callback_data=cb)

    def test_watch_command_and_preset_flow(self):
        out = self._watch(text="/watch")
        self.assertEqual(out["result_code"], "WATCH_HOME")
        self.assertEqual(self._watch(text="/alert@KwDailyMarketInfo_bot")["result_code"], "WATCH_HOME")
        self.assertEqual(self._watch(cb="mbw:s:KOSPI")["result_code"], "WATCH_PICK_THRESHOLD")
        self.assertEqual(self._watch(cb="mbw:t:KOSPI:150")["result_code"], "WATCH_PICK_DIRECTION")
        self.assertEqual(self._watch(cb="mbw:d:KOSPI:150:B")["result_code"], "WATCH_CREATED")
        self.assertEqual(self._watch(cb="mbw:d:KOSPI:999:B")["result_code"], "WATCH_PICK_THRESHOLD")  # unknown preset
        w = self.db.list_market_watches(self.uid)[0]
        self.assertEqual((w["threshold_value"], w["threshold_unit"], w["direction"]), ("1.5", "PERCENT", "BOTH"))
        for row in out["inline_keyboard"]:
            for b in row:
                if "callback_data" in b:
                    self.assertLessEqual(len(b["callback_data"].encode()), 64)

    def test_manual_level_flow_and_validation(self):
        self.assertEqual(self._watch(cb="mbw:l:US10Y")["result_code"], "WATCH_AWAIT_LEVEL")
        self.assertEqual(self._watch(text="NaN")["result_code"], "WATCH_LEVEL_INVALID")
        self.assertEqual(self._watch(text="-1")["result_code"], "WATCH_LEVEL_INVALID")
        self.assertEqual(self._watch(text="4.5")["result_code"], "WATCH_PICK_DIRECTION")
        self.assertEqual(self._watch(cb="mbw:ld:US10Y:D")["result_code"], "WATCH_CREATED")
        w = self.db.list_market_watches(self.uid)[0]
        self.assertEqual((w["condition_type"], w["threshold_value"], w["threshold_unit"]), ("LEVEL", "4.5", "PERCENT_YIELD"))
        self.assertEqual(self._watch(cb="mbw:ld:US10Y:D")["result_code"], "WATCH_INPUT_EXPIRED")  # single-use

    def test_cancel_or_navigate_clears_pending_level_input(self):
        svc = self.router.market_watch
        self.assertEqual(self._watch(cb="mbw:l:USDKRW")["result_code"], "WATCH_AWAIT_LEVEL")
        self.assertTrue(svc.has_pending_input(self.uid))
        self.assertEqual(self._watch(cb="mbw:m")["result_code"], "WATCH_HOME")  # ↩️ 입력 취소
        self.assertFalse(svc.has_pending_input(self.uid))
        self.assertNotEqual(self._watch(text="1400")["result_code"], "WATCH_PICK_DIRECTION")
        self.assertEqual(self.db.list_market_watches(self.uid), [])

        # Navigating elsewhere (asset picker / other asset) also drops pending level input.
        for nav in ("mbw:a", "mbw:s:KOSPI", "mbw:q3"):
            self._watch(cb="mbw:l:USDKRW")
            self._watch(cb=nav)
            self.assertFalse(svc.has_pending_input(self.uid), nav)

        # A parsed-but-unconfirmed value is dropped on navigation, so a later `ld` expires.
        self._watch(cb="mbw:l:USDKRW")
        self.assertEqual(self._watch(text="1400")["result_code"], "WATCH_PICK_DIRECTION")
        self._watch(cb="mbw:s:USDKRW")
        self.assertEqual(self._watch(cb="mbw:ld:USDKRW:U")["result_code"], "WATCH_INPUT_EXPIRED")
        self.assertFalse(any(w["condition_type"] == "LEVEL" for w in self.db.list_market_watches(self.uid)))

    def test_quick_default_and_delete_confirm(self):
        out = self._watch(cb="mbw:q3")
        self.assertEqual(out["result_code"], "WATCH_QUICK_DEFAULT")
        watches = self.db.list_market_watches(self.uid)
        self.assertEqual({w["symbol"] for w in watches}, {"KOSPI", "KOSDAQ"})
        wid = watches[0]["watch_id"]
        self.assertEqual(self._watch(cb=f"mbw:x:{wid}")["result_code"], "WATCH_DELETE_CONFIRM")
        self.assertEqual(self._watch(cb=f"mbw:xy:{wid}")["result_code"], "WATCH_DELETED")
        self.assertEqual(len(self.db.list_market_watches(self.uid)), 1)

    def test_daytime_asset_picker_and_legacy_callbacks(self):
        self.assertEqual(QUICK_DEFAULT_SYMBOLS, ("KOSPI", "KOSDAQ"))
        self.assertEqual(DAYTIME_ACTIVE_SYMBOLS, ("KOSPI", "KOSDAQ", "KOSPI200", "USDKRW"))
        out = self._watch(cb="mbw:a")
        self.assertEqual(out["result_code"], "WATCH_PICK_ASSET")
        kb = out["inline_keyboard"]
        self.assertEqual([[b["callback_data"] for b in row] for row in kb], [
            ["mbw:s:KOSPI", "mbw:s:KOSDAQ"], ["mbw:s:KOSPI200", "mbw:s:USDKRW"], ["mbw:m"],
        ])
        self.assertEqual(kb[0][0]["text"], "🇰🇷 코스피")
        self.assertEqual(kb[1][1]["text"], "💱 원/달러")
        self.assertEqual(kb[2][0]["text"], "⬅️ 내 타겟 목록")
        # Legacy direct callbacks for assets no longer shown in the picker keep working.
        self.assertEqual(self._watch(cb="mbw:s:SP500")["result_code"], "WATCH_PICK_THRESHOLD")
        self.assertEqual(self._watch(cb="mbw:d:US10Y:10:B")["result_code"], "WATCH_CREATED")

    def test_cannot_touch_another_users_watch(self):
        other = self._create(uid=UID)["watch_id"]
        self.assertEqual(self._watch(cb=f"mbw:x:{other}")["result_code"], "WATCH_NOT_OWNED")
        self.assertEqual(self._watch(cb=f"mbw:xy:{other}")["result_code"], "WATCH_NOT_OWNED")
        self.assertTrue(self.db.get_market_watch(other)["is_active"])

    def test_requires_subscription(self):
        out = self.router.handle_interaction("brief_bot", "700000555", text="/watch")
        self.assertEqual(out["result_code"], "WATCH_REQUIRES_SUBSCRIPTION")

    def test_stop_deactivates_and_start_does_not_revive(self):
        self._watch(cb="mbw:q3")
        self.assertEqual(len(self.db.list_market_watches(self.uid)), 2)
        self._watch(text="/stop")
        self.assertEqual(self.db.list_market_watches(self.uid), [])
        out = self._watch(text="/start")
        self.assertIn(out["result_code"], {"SUBSCRIBED", "ALREADY_SUBSCRIBED"})
        self.assertEqual(self.db.list_market_watches(self.uid), [])
        flat = [b.get("callback_data") for row in out["inline_keyboard"] for b in row]
        self.assertIn("mbw:m", flat)
        self.assertIn("mbw:q3", flat)

    def test_menu_command_is_home_alias_of_start(self):
        start = self._watch(text="/start")
        menu = self._watch(text="/menu")
        self.assertEqual(menu["result_code"], "ALREADY_SUBSCRIBED")
        self.assertEqual(menu["inline_keyboard"], start["inline_keyboard"])
        self.assertEqual(menu["reply_html"], start["reply_html"])
        self.assertEqual(self._watch(text="/menu@KwBriefBot")["result_code"], "ALREADY_SUBSCRIBED")
        # Fail-closed: `/menu` never auto-subscribes an un-subscribed caller (only `/start`/`/구독` may).
        outsider = "700000777"
        locked = self.router.handle_interaction("brief_bot", outsider, text="/menu")
        self.assertNotIn(locked["result_code"], {"SUBSCRIBED", "ALREADY_SUBSCRIBED"})
        self.assertFalse(self.engine.is_user_subscribed(outsider, "brief_bot"))
        self.assertIn("menu", {c["command"] for c in OFFICIAL_BOT_COMMANDS["brief_bot"]})

    def test_menu_unsubscribe_and_swap_deactivate(self):
        self._watch(cb="mbw:q3")
        self.router.handle_interaction("menu_bot", self.uid, callback_data="menu:unsub:brief_bot")
        self.assertEqual(self.db.list_market_watches(self.uid), [])

        self.engine.attempt_subscribe(self.uid, "brief_bot", full_name="Router")
        self._watch(cb="mbw:q3")
        res = self.router.handle_interaction("travel_bot", self.uid, callback_data="info:swap:brief_bot:travel_bot")
        self.assertEqual(res["result_code"], "SWAPPED")
        self.assertEqual(self.db.list_market_watches(self.uid), [])

    def test_manager_callback_answers_first_and_guards_private_owner(self):
        manager = DynamicBotManager(self.engine, self.router)
        t = FakeTransport()
        cq = {"id": "q1", "data": "mbw:m", "from": {"id": int(self.uid)},
              "message": {"message_id": 5, "chat": {"id": int(self.uid), "type": "private"}}}
        self.assertEqual(manager._dispatch_brief_watch_callback(t, cq), "WATCH_HOME")
        self.assertEqual(t.calls[0][0], "answer")
        self.assertEqual(t.calls[1][0], "edit")

        t2 = FakeTransport()
        group = dict(cq, message={"message_id": 5, "chat": {"id": -100123, "type": "supergroup"}})
        self.assertEqual(manager._dispatch_brief_watch_callback(t2, group), "WATCH_REJECTED_NON_PRIVATE")
        self.assertEqual([c[0] for c in t2.calls], ["answer"])

        t3 = FakeTransport()
        spoof = dict(cq, **{"from": {"id": 123}})
        self.assertEqual(manager._dispatch_brief_watch_callback(t3, spoof), "WATCH_REJECTED_NON_PRIVATE")
        self.assertEqual([c[0] for c in t3.calls], ["answer"])


# =============================================================================
# Scheduler / runner wiring
# =============================================================================


class TestSchedulerAndRunner(DbTestCase):
    def _scheduler(self, **kw):
        from market_brief.briefing_scheduler import InternalBriefingScheduler

        return InternalBriefingScheduler(
            db=self.db, sub_engine=SubscriptionEngine(self.db), root_dir=Path(self.tmp),
            get_bot_token=lambda: "TEST_TOKEN", get_report_base_url=lambda: "http://localhost:8002", **kw,
        )

    def test_legacy_urgent_is_gated_off_by_default(self):
        sched = self._scheduler()
        with mock.patch.dict(os.environ, {"LEGACY_URGENT_BROADCAST_ENABLED": "false"}), \
                mock.patch.object(sched, "_build_legacy_urgent_pipeline") as build:
            sched._run_urgent_check_sync(T0)
            build.assert_not_called()

    def test_legacy_pipeline_constructor_is_valid(self):
        sched = self._scheduler()
        pipeline = sched._build_legacy_urgent_pipeline("TEST_TOKEN")
        self.assertTrue(hasattr(pipeline, "check_and_send_urgent_alerts"))
        self.assertIs(pipeline.calendar, sched.calendar)

    def test_watch_tick_uses_injected_evaluator_only_with_active_watches(self):
        fake = mock.Mock()
        fake.run_once.return_value = mock.Mock(evaluated=1, fired=0, sent=0, failed=0, unknown=0, skipped_reason="")
        sched = self._scheduler(watch_evaluator=fake)
        self.assertIsNone(sched._run_watch_tick_sync(T0))
        fake.run_once.assert_not_called()
        self._create()
        sched._run_watch_tick_sync(T0)
        fake.run_once.assert_called_once_with(T0)
        self.assertEqual(sched.watch_interval, 120.0)
        self.assertIn("market_watch", sched.get_status())

    def test_runner_oracle_check_has_urllib_and_demo_runs(self):
        import io
        import contextlib
        import telegram_bot_runner as runner

        with mock.patch("urllib.request.urlopen", side_effect=OSError("offline")):
            self.assertFalse(runner.check_oracle_server_sent("morning"))
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {}), contextlib.redirect_stdout(buf):
            self.assertEqual(runner.run_watch_demo(), 0)
        out = buf.getvalue()
        self.assertIn("발송 시뮬레이션 1건", out)
        self.assertIn("최장 callback_data", out)

    def test_runner_watch_tick_skips_unmigrated_db(self):
        import telegram_bot_runner as runner

        missing = Path(self.tmp) / "absent.sqlite3"
        runner.run_user_watch_tick("TOKEN", T0, db_path=missing)
        self.assertFalse(missing.exists())


if __name__ == "__main__":
    unittest.main()
