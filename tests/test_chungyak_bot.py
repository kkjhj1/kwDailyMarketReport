"""chungyak_bot: 3-State eligibility, ledger/lease invariants, quiet hours & 08:00 window, [TG-01/02/04/05/06],
alerts.sqlite3 subscription gate, Admin sidecar read-only APIs. No network, no Telegram I/O, temp DBs only."""
from __future__ import annotations

import contextlib
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import io
import json
import os
from pathlib import Path
import socket
import sqlite3
import tempfile
import threading
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from chungyak_brief.channels.telegram import (
    MAX_HTML_CHARS,
    MAX_VISIBLE_CHARS,
    ChungyakTelegramChannel,
    callback_ok,
    measure_message,
    render_notice_block,
    split_blocks,
    within_limits,
)
from chungyak_brief.channels.telegram_limiter import SafeTelegramDispatcher, TelegramRateLimiter
from chungyak_brief.collector import ChungyakCollector, sample_notices
from chungyak_brief.eligibility import EligibilityEngine, should_notify
from chungyak_brief.interactive_bot import (
    ChungyakAlertService,
    ChungyakInteractiveBot,
    _btn,
    build_demo_dispatcher,
    in_morning_window,
    is_quiet_hours,
    next_morning_slot,
)
from chungyak_brief.models import (
    ChannelType,
    ChungyakNotice,
    DeliveryStatus,
    EligibilityStatus,
    HouseType,
    NoticeType,
    TriggerContext,
    UserProfile,
)
from chungyak_brief.storage.db import ChungyakDatabase, SubscriptionGateBlocked, load_active_chat_ids

KST = ZoneInfo("Asia/Seoul")
ROOT = Path(__file__).resolve().parents[1]
XSS = "<script>alert(1)</script>&\"'"


def kst(y: int, mo: int, d: int, h: int, mi: int) -> datetime:
    return datetime(y, mo, d, h, mi, tzinfo=KST)


def make_notice(key: str = "T1", **kw) -> ChungyakNotice:
    base = dict(
        notice_key=f"APT_GENERAL:{key}:{key}",
        notice_type=NoticeType.APT_GENERAL,
        house_name=f"테스트 단지 {key}",
        region_sido="경기",
        region_sigungu="화성시",
        is_regulated=False,
        min_price_eok=Decimal("5.2"),
        min_area_m2=Decimal("74.5"),
        receipt_start=date(2026, 12, 2),
        receipt_end=date(2026, 12, 31),
        homepage_url="https://www.applyhome.co.kr",
    )
    base.update(kw)
    return ChungyakNotice(**base).ensure_hash()


ELIGIBLE_FIELDS = dict(
    residence_sido="경기", residence_years=3, is_homeless=True, is_household_head=True,
    account_months=24, deposit_manwon=600, payment_count=24, max_price_eok=Decimal("6"),
)


def eligible_profile(chat_id: str = "c1", **kw) -> UserProfile:
    fields = dict(ELIGIBLE_FIELDS)
    fields.update(kw)
    return UserProfile(chat_id=chat_id, **fields)


def sends(channel: ChungyakTelegramChannel) -> list[dict]:
    return [p for m, p in channel.call_log if m == "sendMessage"]


def fast_dispatcher(sleeps: list | None = None) -> SafeTelegramDispatcher:
    rec = sleeps if sleeps is not None else []
    limiter = TelegramRateLimiter(max_global_rate=1000.0, min_chat_interval=0.0, sleep=lambda _s: None)
    return SafeTelegramDispatcher(rate_limiter=limiter, sleep=rec.append)


# =========================================================================================================
# 3-State eligibility
# =========================================================================================================
class EligibilityTests(unittest.TestCase):
    def setUp(self):
        self.engine = EligibilityEngine()
        self.notice = make_notice()

    def test_eligible(self):
        res = self.engine.evaluate(self.notice, eligible_profile())
        self.assertEqual(res.status, EligibilityStatus.ELIGIBLE, res)
        self.assertEqual(res.unknowns, [])
        self.assertTrue(should_notify(res, eligible_profile()))

    def test_partial_unknown_when_answer_missing(self):
        res = self.engine.evaluate(self.notice, eligible_profile(account_months=None))
        self.assertEqual(res.status, EligibilityStatus.PARTIAL_UNKNOWN)
        self.assertIn("청약통장 가입기간", res.unknowns)

    def test_ineligible_hard_fail(self):
        res = self.engine.evaluate(self.notice, eligible_profile(max_price_eok=Decimal("5.1")))
        self.assertEqual(res.status, EligibilityStatus.INELIGIBLE)
        self.assertFalse(should_notify(res, eligible_profile()))
        res2 = self.engine.evaluate(self.notice, eligible_profile(account_months=6))
        self.assertEqual(res2.status, EligibilityStatus.INELIGIBLE)

    def test_all_null_profile_is_partial_unknown_and_not_notified(self):
        empty = UserProfile(chat_id="e1")
        self.assertTrue(empty.is_empty())
        for notice in [self.notice] + sample_notices():
            if notice.house_type != HouseType.APT:
                continue
            with self.subTest(notice=notice.notice_key):
                res = self.engine.evaluate(notice, empty)
                self.assertEqual(res.status, EligibilityStatus.PARTIAL_UNKNOWN)
                self.assertIn("프로필 미입력", res.unknowns)
                self.assertFalse(should_notify(res, empty))  # no region interest -> no nationwide spam

    def test_partial_unknown_notifies_only_with_region_interest(self):
        p = UserProfile(chat_id="r1", preferred_regions=["경기"])
        res = self.engine.evaluate(self.notice, p)
        self.assertEqual(res.status, EligibilityStatus.PARTIAL_UNKNOWN)
        self.assertTrue(should_notify(res, p))

    def test_max_price_decimal_boundary_and_roundtrip(self):
        equal = self.engine.evaluate(self.notice, eligible_profile(max_price_eok=Decimal("5.20")))
        self.assertEqual(equal.status, EligibilityStatus.ELIGIBLE)
        below = self.engine.evaluate(self.notice, eligible_profile(max_price_eok=Decimal("5.19999999999999999")))
        self.assertEqual(below.status, EligibilityStatus.INELIGIBLE)
        db = ChungyakDatabase(":memory:")
        try:
            prof = db.update_profile("d1", max_price_eok=Decimal("5.20"))
            self.assertIsInstance(prof.max_price_eok, Decimal)
            self.assertEqual(prof.max_price_eok, Decimal("5.2"))
            with db._read() as conn:
                raw = conn.execute("SELECT max_price_eok, typeof(max_price_eok) t FROM chungyak_user_profiles").fetchone()
            self.assertEqual((raw[0], raw[1]), ("5.2", "text"))
        finally:
            db.close()

    def test_officetel_excluded_by_default(self):
        ofc = make_notice("O1", house_type=HouseType.OFFICETEL)
        res = self.engine.evaluate(ofc, eligible_profile())
        self.assertEqual(res.status, EligibilityStatus.INELIGIBLE)
        self.assertTrue(any("오피스텔" in r for r in res.reasons))
        opted = self.engine.evaluate(ofc, eligible_profile(include_officetel=True))
        self.assertFalse(any("오피스텔" in r for r in opted.reasons))
        self.assertNotIn("URBTY_OFCTL", ChungyakCollector().sources(include_officetel=False))
        self.assertIn("URBTY_OFCTL", ChungyakCollector().sources(include_officetel=True))


# =========================================================================================================
# Storage: content_hash amendment, atomic lease, UNKNOWN terminal
# =========================================================================================================
class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.db = ChungyakDatabase(":memory:")

    def tearDown(self):
        self.db.close()

    def test_content_hash_amendment(self):
        n = make_notice()
        self.assertEqual(self.db.upsert_notice(replace(n)), "NEW")
        self.assertEqual(self.db.upsert_notice(replace(n)), "UNCHANGED")
        amended = replace(n, receipt_end=date(2027, 1, 5))
        self.assertEqual(self.db.upsert_notice(amended), "AMENDED")
        stored = self.db.get_notice(n.notice_key)
        self.assertEqual(stored.revision, 2)
        self.assertNotEqual(stored.content_hash, n.content_hash)
        self.assertEqual(n.compute_content_hash(), replace(n).compute_content_hash())  # deterministic
        # a SENT old revision does not suppress the amended revision
        lease = self.db.reserve_delivery(chat_id="c1", notice_key=n.notice_key, content_hash=n.content_hash,
                                         channel=ChannelType.CUSTOM_MATCH, trigger_context="CRON_TICK")
        self.db.finalize_delivery(lease["delivery_key"], lease["lease_token"], DeliveryStatus.SENT)
        self.assertTrue(self.db.is_notice_delivered("c1", n.notice_key, n.content_hash))
        self.assertFalse(self.db.is_notice_delivered("c1", n.notice_key, stored.content_hash))

    def test_reserve_is_exclusive_across_channels_and_fenced(self):
        n = make_notice()
        first = self.db.reserve_delivery(chat_id="c1", notice_key=n.notice_key, content_hash=n.content_hash,
                                         channel=ChannelType.CUSTOM_MATCH, trigger_context="CRON_TICK")
        self.assertEqual(first["status"], "PENDING")
        again = self.db.reserve_delivery(chat_id="c1", notice_key=n.notice_key, content_hash=n.content_hash,
                                         channel=ChannelType.CUSTOM_MATCH, trigger_context="CRON_TICK")
        self.assertIsNone(again)
        other = self.db.reserve_delivery(chat_id="c1", notice_key=n.notice_key, content_hash=n.content_hash,
                                         channel=ChannelType.REGULAR_DAILY, trigger_context="CRON_TICK")
        self.assertIsNone(other)
        self.assertFalse(self.db.finalize_delivery(first["delivery_key"], "wrong-token", DeliveryStatus.SENT))
        self.assertTrue(self.db.finalize_delivery(first["delivery_key"], first["lease_token"], DeliveryStatus.SENT))
        self.assertFalse(self.db.finalize_delivery(first["delivery_key"], first["lease_token"], DeliveryStatus.FAILED))

    def test_atomic_lease_claim_under_concurrency(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "chungyak_brief.sqlite3"
            ChungyakDatabase(path)
            n = make_notice()
            results: list = []
            barrier = threading.Barrier(8)

            def worker():
                db = ChungyakDatabase(path)
                barrier.wait()
                results.append(db.reserve_delivery(
                    chat_id="c1", notice_key=n.notice_key, content_hash=n.content_hash,
                    channel=ChannelType.CUSTOM_MATCH, trigger_context="CRON_TICK"))

            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(30)
            self.assertEqual(sum(1 for r in results if r is not None), 1)

            # QUEUED_MORNING claim: a second claimer gets nothing.
            db = ChungyakDatabase(path)
            m = make_notice("Q1")
            db.reserve_delivery(chat_id="c2", notice_key=m.notice_key, content_hash=m.content_hash,
                                channel=ChannelType.CUSTOM_MATCH, trigger_context="CRON_TICK",
                                queue_until_kst="2026-11-25 08:00:00")
            claimed = db.claim_queued_morning("2026-11-25 08:00:00")
            self.assertEqual(len(claimed), 1)
            self.assertEqual(db.claim_queued_morning("2026-11-25 08:00:00"), [])

    def test_unknown_is_terminal_and_lease_expiry_becomes_unknown(self):
        n = make_notice()
        lease = self.db.reserve_delivery(chat_id="c1", notice_key=n.notice_key, content_hash=n.content_hash,
                                         channel=ChannelType.CUSTOM_MATCH, trigger_context="CRON_TICK")
        self.db.finalize_delivery(lease["delivery_key"], lease["lease_token"], DeliveryStatus.UNKNOWN, error_detail="timeout")
        for ch in ChannelType:
            self.assertIsNone(self.db.reserve_delivery(chat_id="c1", notice_key=n.notice_key, content_hash=n.content_hash,
                                                       channel=ch, trigger_context="CRON_TICK"))
        t0 = datetime(2026, 11, 25, 0, 0, tzinfo=timezone.utc)
        m = make_notice("L1")
        lease2 = self.db.reserve_delivery(chat_id="c1", notice_key=m.notice_key, content_hash=m.content_hash,
                                          channel=ChannelType.CUSTOM_MATCH, trigger_context="CRON_TICK", now=t0)
        self.assertEqual(self.db.sweep_expired_leases(now=t0 + timedelta(seconds=200)), 1)
        self.assertEqual(self.db.get_delivery(lease2["delivery_key"])["status"], "UNKNOWN")
        self.assertIsNone(self.db.reserve_delivery(chat_id="c1", notice_key=m.notice_key, content_hash=m.content_hash,
                                                   channel=ChannelType.CUSTOM_MATCH, trigger_context="CRON_TICK"))

    def test_failed_is_retryable_but_bounded(self):
        n = make_notice()
        for attempt in range(1, 4):
            lease = self.db.reserve_delivery(chat_id="c1", notice_key=n.notice_key, content_hash=n.content_hash,
                                             channel=ChannelType.CUSTOM_MATCH, trigger_context="CRON_TICK")
            self.assertIsNotNone(lease, attempt)
            self.db.finalize_delivery(lease["delivery_key"], lease["lease_token"], DeliveryStatus.FAILED, error_detail="400")
        self.assertIsNone(self.db.reserve_delivery(chat_id="c1", notice_key=n.notice_key, content_hash=n.content_hash,
                                                   channel=ChannelType.CUSTOM_MATCH, trigger_context="CRON_TICK"))


# =========================================================================================================
# Alert service: quiet hours, 08:00~08:15 window, STARTUP_INIT, UNKNOWN no-retry, regular dedup
# =========================================================================================================
class _ServiceBase(unittest.TestCase):
    def setUp(self):
        self.db = ChungyakDatabase(":memory:")
        self.db.upsert_notice(make_notice())
        self.db.update_profile("c1", **ELIGIBLE_FIELDS)
        self.channel = ChungyakTelegramChannel(token="", dry_run=True)
        self.active = {"c1"}

    def tearDown(self):
        self.db.close()

    def service(self, channel=None, provider="default") -> ChungyakAlertService:
        prov = (lambda: self.active) if provider == "default" else provider
        return ChungyakAlertService(db=self.db, channel=channel or self.channel, dispatcher=fast_dispatcher(),
                                    active_chat_ids_provider=prov)


class QuietHoursTests(_ServiceBase):
    def test_time_policy_boundaries(self):
        cases = {(21, 59): False, (22, 0): True, (7, 29): True, (7, 30): False, (7, 59): False, (8, 0): False}
        for (h, m), quiet in cases.items():
            with self.subTest(t=f"{h:02d}:{m:02d}"):
                self.assertEqual(is_quiet_hours(kst(2026, 11, 25, h, m)), quiet)
        windows = {(7, 59): False, (8, 0): True, (8, 14): True, (8, 15): False}
        for (h, m), inside in windows.items():
            with self.subTest(w=f"{h:02d}:{m:02d}"):
                self.assertEqual(in_morning_window(kst(2026, 11, 25, h, m)), inside)
        self.assertEqual(next_morning_slot(kst(2026, 11, 24, 22, 0)), kst(2026, 11, 25, 8, 0))
        self.assertEqual(next_morning_slot(kst(2026, 11, 25, 7, 29)), kst(2026, 11, 25, 8, 0))

    def test_2159_sends_immediately(self):
        st = self.service().run_tick(TriggerContext.CRON_TICK, now=kst(2026, 11, 24, 21, 59))
        self.assertEqual(st["custom"]["sent"], 1)
        self.assertEqual(len(sends(self.channel)), 1)

    def test_2200_queues_then_morning_window_sequence(self):
        svc = self.service()
        st = svc.run_tick(TriggerContext.CRON_TICK, now=kst(2026, 11, 24, 22, 0))
        self.assertEqual(st["custom"]["queued_morning"], 1)
        self.assertEqual(sends(self.channel), [])
        rows = self.db.list_deliveries(status="QUEUED_MORNING")
        self.assertEqual([r["scheduled_at_kst"] for r in rows], ["2026-11-25 08:00:00"])
        for h, m in ((7, 29), (7, 30), (7, 59)):
            svc.run_tick(TriggerContext.CRON_TICK, now=kst(2026, 11, 25, h, m))
            self.assertEqual(sends(self.channel), [], f"{h}:{m}")
        st8 = svc.run_tick(TriggerContext.CRON_TICK, now=kst(2026, 11, 25, 8, 0))
        self.assertEqual(st8["morning_bundle"]["sent"], 1)
        self.assertEqual(len(sends(self.channel)), 1)
        self.assertEqual(self.db.list_deliveries()[0]["status"], "SENT")
        svc.run_tick(TriggerContext.CRON_TICK, now=kst(2026, 11, 25, 8, 5))
        self.assertEqual(len(sends(self.channel)), 1)  # idempotent

    def test_0814_still_sends(self):
        svc = self.service()
        svc.run_tick(TriggerContext.CRON_TICK, now=kst(2026, 11, 24, 23, 10))
        st = svc.run_tick(TriggerContext.CRON_TICK, now=kst(2026, 11, 25, 8, 14))
        self.assertEqual(st["morning_bundle"]["sent"], 1)

    def test_0815_window_missed_no_compensation(self):
        svc = self.service()
        svc.run_tick(TriggerContext.CRON_TICK, now=kst(2026, 11, 24, 23, 10))
        st = svc.run_tick(TriggerContext.CRON_TICK, now=kst(2026, 11, 25, 8, 15))
        self.assertEqual(st["window_missed"], 1)
        self.assertEqual(sends(self.channel), [])
        row = self.db.list_deliveries()[0]
        self.assertEqual(row["status"], "FAILED")
        self.assertTrue(row["error_detail"].startswith("WINDOW_MISSED"))
        svc.run_tick(TriggerContext.CRON_TICK, now=kst(2026, 11, 25, 8, 16))
        svc.run_tick(TriggerContext.CRON_TICK, now=kst(2026, 11, 26, 8, 1))
        self.assertEqual(sends(self.channel), [])

    def test_startup_init_never_sends(self):
        svc = self.service()
        svc.run_tick(TriggerContext.CRON_TICK, now=kst(2026, 11, 24, 23, 10))
        inside = svc.run_tick(TriggerContext.STARTUP_INIT, now=kst(2026, 11, 25, 8, 5))
        self.assertEqual(sends(self.channel), [])
        self.assertEqual(inside["morning_bundle"]["sent"], 0)
        outside = svc.run_tick(TriggerContext.STARTUP_INIT, now=kst(2026, 11, 25, 8, 20))
        self.assertEqual(outside["window_missed"], 1)
        self.assertEqual(sends(self.channel), [])
        # A fresh match at STARTUP_INIT during the day is not sent either.
        self.db.upsert_notice(make_notice("S2"))
        svc.run_tick(TriggerContext.STARTUP_INIT, now=kst(2026, 11, 25, 14, 0))
        self.assertEqual(sends(self.channel), [])

    def test_manual_trigger_does_not_flush_morning_queue(self):
        svc = self.service()
        svc.run_tick(TriggerContext.CRON_TICK, now=kst(2026, 11, 24, 23, 10))
        svc.run_tick(TriggerContext.MANUAL, now=kst(2026, 11, 25, 8, 5))
        self.assertEqual(sends(self.channel), [])


class _TimeoutChannel(ChungyakTelegramChannel):
    def __init__(self):
        super().__init__(token="", dry_run=True)
        self.attempts = 0

    def send_message(self, chat_id, text, reply_markup=None):
        self.attempts += 1
        raise TimeoutError("read timed out")


class DedupTests(_ServiceBase):
    def test_custom_sent_suppresses_regular_digest(self):
        self.db.set_regular_sub("c1", True)
        svc = self.service()
        svc.run_tick(TriggerContext.CRON_TICK, now=kst(2026, 11, 25, 14, 0))
        self.assertEqual(len(sends(self.channel)), 1)
        st = svc.run_tick(TriggerContext.CRON_TICK, now=kst(2026, 11, 26, 8, 1))
        self.assertEqual(st["regular_daily"]["chats"], 0)
        self.assertEqual(len(sends(self.channel)), 1)

    def test_regular_digest_once_then_custom_suppressed(self):
        self.db.set_regular_sub("c1", True)
        svc = self.service()
        st = svc.run_tick(TriggerContext.CRON_TICK, now=kst(2026, 11, 26, 8, 1))
        self.assertEqual(st["regular_daily"]["sent"], 1)
        self.assertEqual(st["custom"]["matched"], 0)
        svc.run_tick(TriggerContext.CRON_TICK, now=kst(2026, 11, 26, 8, 2))
        svc.run_tick(TriggerContext.CRON_TICK, now=kst(2026, 11, 27, 8, 1))
        self.assertEqual(len(sends(self.channel)), 1)
        self.assertEqual([r["channel_type"] for r in self.db.list_deliveries()], ["REGULAR_DAILY"])

    def test_timeout_unknown_never_retried(self):
        self.db.set_regular_sub("c1", True)
        ch = _TimeoutChannel()
        svc = self.service(channel=ch)
        st = svc.run_tick(TriggerContext.CRON_TICK, now=kst(2026, 11, 25, 14, 0))
        self.assertEqual(st["custom"]["unknown"], 1)
        self.assertEqual(self.db.list_deliveries()[0]["status"], "UNKNOWN")
        svc.run_tick(TriggerContext.CRON_TICK, now=kst(2026, 11, 25, 14, 5))
        svc.run_tick(TriggerContext.CRON_TICK, now=kst(2026, 11, 26, 8, 1))
        self.assertEqual(ch.attempts, 1)

    def test_inactive_subscriber_gets_nothing(self):
        self.active = set()
        self.db.set_regular_sub("c1", True)
        svc = self.service()
        svc.run_tick(TriggerContext.CRON_TICK, now=kst(2026, 11, 25, 14, 0))
        svc.run_tick(TriggerContext.CRON_TICK, now=kst(2026, 11, 26, 8, 1))
        self.assertEqual(sends(self.channel), [])

    def test_dry_run_plans_without_writes(self):
        svc = ChungyakAlertService(db=self.db, channel=self.channel, dispatcher=fast_dispatcher(),
                                   active_chat_ids_provider=lambda: self.active, dry_run=True)
        st = svc.run_tick(TriggerContext.CRON_TICK, now=kst(2026, 11, 25, 14, 0))
        self.assertEqual(len(st["planned"]), 1)
        self.assertEqual(self.db.list_deliveries(), [])
        self.assertEqual(sends(self.channel), [])


# =========================================================================================================
# alerts.sqlite3 active-subscription cross-check (Fail-Closed)
# =========================================================================================================
class SubscriptionGateTests(unittest.TestCase):
    def _alerts_db(self, path: Path) -> None:
        conn = sqlite3.connect(str(path))
        try:
            conn.executescript(
                """CREATE TABLE users (telegram_user_id TEXT PRIMARY KEY, status TEXT);
                   CREATE TABLE subscriptions (telegram_user_id TEXT, bot_id TEXT, status TEXT);"""
            )
            conn.executemany("INSERT INTO users VALUES (?, ?)",
                             [("c1", "ACTIVE"), ("c2", "ACTIVE"), ("c3", "ACTIVE"), ("c4", "SUSPENDED"), ("c5", "GRACE_PERIOD")])
            conn.executemany("INSERT INTO subscriptions VALUES (?, ?, ?)", [
                ("c1", "chungyak_bot", "ACTIVE"), ("c2", "chungyak_bot", "CANCELLED"), ("c3", "brief_bot", "ACTIVE"),
                ("c4", "chungyak_bot", "ACTIVE"), ("c5", "chungyak_bot", "GRACE_PERIOD"),
            ])
            conn.commit()
        finally:
            conn.close()

    def test_cross_check_and_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            alerts = Path(tmp) / "alerts.sqlite3"
            with self.assertRaises(SubscriptionGateBlocked):
                load_active_chat_ids(alerts)
            sqlite3.connect(str(alerts)).close()  # empty file: tables missing
            with self.assertRaises(SubscriptionGateBlocked):
                load_active_chat_ids(alerts)
            alerts.unlink()
            self._alerts_db(alerts)
            self.assertEqual(load_active_chat_ids(alerts), {"c1", "c5"})

    def test_service_blocked_when_gate_unavailable(self):
        db = ChungyakDatabase(":memory:")
        try:
            db.upsert_notice(make_notice())
            db.update_profile("c1", **ELIGIBLE_FIELDS)
            ch = ChungyakTelegramChannel(token="", dry_run=True)

            def boom():
                raise SubscriptionGateBlocked("alerts DB not found")

            for provider in (boom, None):
                svc = ChungyakAlertService(db=db, channel=ch, dispatcher=fast_dispatcher(),
                                           active_chat_ids_provider=provider)
                st = svc.run_tick(TriggerContext.CRON_TICK, now=kst(2026, 11, 25, 14, 0))
                self.assertTrue(st["blocked"])
            self.assertEqual(sends(ch), [])
            self.assertEqual(db.list_deliveries(), [])
        finally:
            db.close()


# =========================================================================================================
# [TG-04] Interactive bot: ACK first, 64-byte callbacks, in-place confirm, commands
# =========================================================================================================
class InteractiveTests(unittest.TestCase):
    def setUp(self):
        self.db = ChungyakDatabase(":memory:")
        for n in sample_notices():
            self.db.upsert_notice(n)
        self.channel = ChungyakTelegramChannel(token="", dry_run=True)
        self.bot = ChungyakInteractiveBot(db=self.db, channel=self.channel, include_samples=True,
                                          today_provider=lambda: date(2026, 11, 24))

    def tearDown(self):
        self.db.close()

    def cb(self, data: str, cq_id: str = "q1", chat: str = "u1"):
        return self.bot.handle_update({"callback_query": {"id": cq_id, "data": data,
                                                          "message": {"chat": {"id": chat}, "message_id": 7}}})

    def test_ack_precedes_any_db_work_and_is_sent_once(self):
        order: list[str] = []
        orig_ensure, orig_update = self.db.ensure_profile, self.db.update_profile

        def spy(name, fn):
            def inner(*a, **k):
                order.append(name)
                self.assertIn("answerCallbackQuery", [m for m, _ in self.channel.call_log], "ACK must come first")
                return fn(*a, **k)
            return inner

        self.db.ensure_profile = spy("ensure", orig_ensure)
        self.db.update_profile = spy("update", orig_update)
        self.assertEqual(self.cb("cy:sido:SEL", "q-ack"), "sido")
        acks = [p for m, p in self.channel.call_log if m == "answerCallbackQuery"]
        self.assertEqual([a["callback_query_id"] for a in acks], ["q-ack"])
        self.assertEqual(self.channel.call_log[0][0], "answerCallbackQuery")
        self.assertIn("update", order)
        self.assertEqual(self.db.get_profile("u1").residence_sido, "서울")

    def test_ack_even_for_foreign_or_locked_callbacks(self):
        self.bot.access_checker = lambda _c: False
        self.assertEqual(self.cb("cy:home", "q-locked"), "locked")
        self.assertIsNone(self.cb("zz:other", "q-foreign"))
        acked = [p["callback_query_id"] for m, p in self.channel.call_log if m == "answerCallbackQuery"]
        self.assertEqual(acked, ["q-locked", "q-foreign"])
        self.assertIsNone(self.db.get_profile("u1"))  # locked user: no state mutation

    def test_every_callback_is_ascii_and_within_64_bytes(self):
        profile = self.db.update_profile("u1", preferred_regions=["서울", "경기"], special_types=["NEWLYWED"])
        views = [self.bot.render_home(profile), self.bot.render_settings(profile), self.bot.render_regular(profile),
                 self.bot.render_list(profile)]
        views += [self.bot.render_question(profile, f) for f in ("sido", "rg", "sp", "ry", "hl", "hh", "acct", "dep", "pay", "price")]
        count = 0
        for _text, markup in views:
            for row in markup["inline_keyboard"]:
                for btn in row:
                    data = btn["callback_data"]
                    count += 1
                    self.assertTrue(data.startswith("cy:"), data)
                    self.assertTrue(callback_ok(data), data)
                    self.assertLessEqual(len(data.encode("ascii")), 64)
        self.assertGreater(count, 40)
        with self.assertRaises(ValueError):
            _btn("x", "cy:" + "a" * 62)
        with self.assertRaises(ValueError):
            _btn("x", "cy:지역")

    def test_reset_requires_in_place_confirm(self):
        self.db.update_profile("u1", residence_sido="서울")
        self.cb("cy:reset:ask", "r1")
        self.assertEqual(self.db.get_profile("u1").residence_sido, "서울")
        confirm = [p for m, p in self.channel.call_log if m == "editMessageText"][-1]
        datas = [b["callback_data"] for row in confirm["reply_markup"]["inline_keyboard"] for b in row]
        self.assertEqual(datas, ["cy:reset:do", "cy:home"])
        self.cb("cy:reset:do", "r2")
        self.assertIsNone(self.db.get_profile("u1").residence_sido)

    def test_regular_off_requires_confirm(self):
        self.db.set_regular_sub("u1", True)
        self.cb("cy:reg:off", "g1")
        self.assertTrue(self.db.get_regular_sub("u1"))
        self.cb("cy:reg:offok", "g2")
        self.assertFalse(self.db.get_regular_sub("u1"))
        self.cb("cy:reg:on", "g3")
        self.assertTrue(self.db.get_regular_sub("u1"))

    def test_commands_start_custom_regular_today(self):
        for text, expect in (("/start", "/start"), ("/custom", "/custom"), ("/regular@ChungyakBot", "/regular"),
                             ("/today", "/today"), ("/help", "/help"), ("hello", None)):
            with self.subTest(cmd=text):
                self.assertEqual(self.bot.handle_update({"message": {"chat": {"id": "u2"}, "text": text}}), expect)
        msgs = sends(self.channel)
        self.assertEqual(len(msgs), 5)
        self.assertIn("내 청약 조건", msgs[1]["text"])
        self.assertIn("정기 청약 다이제스트", msgs[2]["text"])
        self.assertIn("진행중 공고", msgs[3]["text"])
        for m in msgs:
            self.assertTrue(within_limits(m["text"]))

    def test_edit_not_modified_is_ignored(self):
        ch = ChungyakTelegramChannel(token="", dry_run=True)

        def not_modified(method, payload, timeout=None):
            ch.call_log.append((method, payload))
            if method == "editMessageText":
                raise RuntimeError("Telegram HTTP 400: Bad Request: message is not modified")
            return {"ok": True, "result": {"message_id": 1}}

        ch._api_call = not_modified
        res = ch.safe_edit_or_send("u1", 5, "same")
        self.assertTrue(res.get("not_modified"))
        self.assertEqual([m for m, _ in ch.call_log], ["editMessageText"])


# =========================================================================================================
# [TG-01/02] HTML size split & escape
# =========================================================================================================
class TemplateTests(unittest.TestCase):
    def test_split_respects_limits_and_keeps_every_block_once(self):
        notices = [make_notice(f"X{i}", house_name=f"{XSS} 단지 {i}" * 2, region_sigungu=XSS,
                               homepage_url="https://www.applyhome.co.kr/?a=1&b=<2>") for i in range(40)]
        blocks = [render_notice_block(n) for n in notices]
        parts = split_blocks("📋 <b>헤더</b>", blocks, "ℹ️ 꼬리말")
        self.assertGreater(len(parts), 1)
        seen = []
        for html_text, idxs in parts:
            visible, raw = measure_message(html_text)
            self.assertLessEqual(visible, MAX_VISIBLE_CHARS)
            self.assertLessEqual(raw, MAX_HTML_CHARS)
            self.assertNotIn("<script>", html_text)
            self.assertIn("&lt;script&gt;", html_text)
            seen.extend(idxs)
        self.assertEqual(sorted(seen), list(range(40)))

    def test_only_allowed_tags_and_https_links(self):
        n = make_notice("J1", house_name=XSS, homepage_url="javascript:alert(1)")
        html_text = render_notice_block(n, EligibilityEngine().evaluate(n, UserProfile(chat_id="z")))
        self.assertNotIn("<a ", html_text)
        import re
        tags = set(re.findall(r"</?([a-zA-Z]+)", html_text))
        self.assertTrue(tags <= {"b", "i", "code", "pre", "a"}, tags)
        ok = render_notice_block(make_notice("J2"))
        self.assertIn('<a href="https://www.applyhome.co.kr">', ok)


# =========================================================================================================
# [TG-05] Safe dispatcher: 429 retry, timeout UNKNOWN
# =========================================================================================================
class _ScriptedChannel:
    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def send_message(self, chat_id, text, reply_markup=None):
        self.calls += 1
        step = self.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        return step


class DispatcherTests(unittest.TestCase):
    def test_429_retry_after_then_sent(self):
        sleeps: list = []
        ch = _ScriptedChannel([RuntimeError('Telegram HTTP 429: {"ok":false,"parameters":{"retry_after":1}}'),
                               {"ok": True, "result": {"message_id": 77}}])
        status, msg_id, err = fast_dispatcher(sleeps).send_one(ch, "c1", "hi")
        self.assertEqual((status, msg_id, err), ("SENT", "77", None))
        self.assertEqual(ch.calls, 2)
        self.assertEqual(sleeps, [1.5])

    def test_429_exhausted_is_failed(self):
        ch = _ScriptedChannel([RuntimeError("Telegram HTTP 429: Too Many Requests")] * 3)
        status, _, err = fast_dispatcher().send_one(ch, "c1", "hi")
        self.assertEqual(status, "FAILED")
        self.assertEqual(ch.calls, 3)
        self.assertIn("429", err)

    def test_timeouts_are_unknown_without_retry(self):
        for exc in (TimeoutError("x"), socket.timeout("x"), RuntimeError("The read operation timed out"),
                    RuntimeError("Connection reset by peer")):
            with self.subTest(exc=repr(exc)):
                ch = _ScriptedChannel([exc, {"ok": True}])
                status, _, _ = fast_dispatcher().send_one(ch, "c1", "hi")
                self.assertEqual(status, "UNKNOWN")
                self.assertEqual(ch.calls, 1)

    def test_rate_limiter_spacing(self):
        clock = [0.0]
        slept: list = []

        def sleep(s):
            slept.append(round(s, 3))
            clock[0] += s

        lim = TelegramRateLimiter(sleep=sleep, clock=lambda: clock[0])
        lim.wait_before_send("a")
        lim.wait_before_send("a")
        self.assertEqual(slept, [1.0])
        lim.wait_before_send("b")
        self.assertEqual(slept[-1], 0.04)


# =========================================================================================================
# [TG-06] --demo: zero file writes, zero network
# =========================================================================================================
class DemoTests(unittest.TestCase):
    def test_demo_is_memory_only_and_offline(self):
        import chungyak_bot_runner as runner

        paths: list[str] = []
        real_db = runner.ChungyakDatabase

        def recording_db(path=":memory:"):
            paths.append(str(path))
            return real_db(path)

        before_root = {p.name for p in ROOT.iterdir()}
        with tempfile.TemporaryDirectory() as tmp:
            cwd = os.getcwd()
            os.chdir(tmp)
            out = io.StringIO()
            try:
                with patch.object(runner, "ChungyakDatabase", side_effect=recording_db), \
                        patch.object(socket.socket, "connect", side_effect=AssertionError("network used")), \
                        patch("urllib.request.urlopen", side_effect=AssertionError("network used")), \
                        contextlib.redirect_stdout(out):
                    rc = runner.main(["--demo"])
            finally:
                os.chdir(cwd)
            self.assertEqual(os.listdir(tmp), [])
        self.assertEqual(rc, 0)
        self.assertEqual(paths, [":memory:"])
        self.assertEqual({p.name for p in ROOT.iterdir()}, before_root)
        text = out.getvalue()
        self.assertIn("STARTUP_INIT", text)
        self.assertIn("Chungyak Demo", text)


# =========================================================================================================
# Admin sidecar (read-only) + catalogue wiring
# =========================================================================================================
class AdminChungyakApiTests(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        from admin_server import create_admin_app

        os.environ.pop("ADMIN_API_TOKEN", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.app = create_admin_app(db_path=str(self.dir / "alerts.sqlite3"), env_path=self.dir / "empty.env")
        self.client = TestClient(self.app)
        self.sidecar = self.dir / "chungyak_brief.sqlite3"

    def tearDown(self):
        try:
            self.tmp.cleanup()
        except Exception:
            pass

    def test_absent_sidecar_returns_empty_and_is_never_created(self):
        for ep in ("notices", "watches", "deliveries"):
            with self.subTest(ep=ep):
                res = self.client.get(f"/api/chungyak/{ep}")
                self.assertEqual(res.status_code, 200)
                self.assertEqual(res.json(), [])
        with patch.dict(os.environ, {"ADMIN_API_TOKEN": "t0k"}):
            res = self.client.get("/api/chungyak/subscribers", headers={"X-Admin-Token": "t0k"})
            self.assertEqual((res.status_code, res.json()), (200, []))
        self.assertFalse(self.sidecar.exists())
        self.assertEqual(self.client.get("/api/chungyak/deliveries?status=BOGUS").status_code, 400)

    def test_subscribers_pii_endpoint_fails_closed(self):
        self.assertEqual(self.client.get("/api/chungyak/subscribers").status_code, 503)
        with patch.dict(os.environ, {"ADMIN_API_TOKEN": "t0k"}):
            self.assertEqual(self.client.get("/api/chungyak/subscribers").status_code, 401)
            self.assertEqual(self.client.get("/api/chungyak/subscribers", headers={"X-Admin-Token": "bad"}).status_code, 401)
            self.assertEqual(self.client.get("/api/chungyak/subscribers", headers={"X-Admin-Token": "t0k"}).status_code, 200)

    def test_populated_sidecar_masking_decimal_and_status_labels(self):
        cdb = ChungyakDatabase(self.sidecar)
        big = Decimal("12.34567890123456789")
        n1 = make_notice("A1", house_name=XSS, min_price_eok=big)
        n2 = make_notice("A2")
        cdb.upsert_notice(n1)
        cdb.upsert_notice(n2)
        cdb.update_profile("123456789", residence_sido="서울", max_price_eok=Decimal("9.50"), preferred_regions=["서울"])
        cdb.set_regular_sub("123456789", True)
        cdb.reserve_delivery(chat_id="123456789", notice_key=n1.notice_key, content_hash=n1.content_hash,
                             channel=ChannelType.CUSTOM_MATCH, trigger_context="CRON_TICK",
                             queue_until_kst="2026-11-25 08:00:00")
        lease = cdb.reserve_delivery(chat_id="123456789", notice_key=n2.notice_key, content_hash=n2.content_hash,
                                     channel=ChannelType.CUSTOM_MATCH, trigger_context="CRON_TICK")
        cdb.finalize_delivery(lease["delivery_key"], lease["lease_token"], DeliveryStatus.UNKNOWN, error_detail="timeout")
        cdb.close()

        notices = self.client.get("/api/chungyak/notices").json()
        by_key = {n["notice_key"]: n for n in notices}
        self.assertEqual(by_key[n1.notice_key]["min_price_eok"], "12.34567890123456789")
        self.assertEqual(by_key[n1.notice_key]["house_name"], XSS)  # raw JSON; UI esc()

        watches = self.client.get("/api/chungyak/watches").json()
        self.assertEqual(len(watches), 1)
        self.assertEqual(watches[0]["chat_id_masked"], "12*****89")
        self.assertEqual(watches[0]["max_price_eok"], "9.5")
        self.assertIsNone(watches[0]["is_homeless"])  # unknown stays unknown (not False)

        deliveries = self.client.get("/api/chungyak/deliveries").json()
        statuses = {d["status"]: d for d in deliveries}
        self.assertEqual(set(statuses), {"QUEUED_MORNING", "UNKNOWN"})
        self.assertNotEqual(statuses["QUEUED_MORNING"]["status_label"], statuses["UNKNOWN"]["status_label"])
        self.assertEqual(statuses["QUEUED_MORNING"]["scheduled_at_kst"], "2026-11-25 08:00:00")
        only_unknown = self.client.get("/api/chungyak/deliveries?status=unknown").json()
        self.assertEqual([d["status"] for d in only_unknown], ["UNKNOWN"])
        for payload in (watches, deliveries):
            self.assertNotIn("123456789", json.dumps(payload, ensure_ascii=False))

        with patch.dict(os.environ, {"ADMIN_API_TOKEN": "t0k"}):
            subs = self.client.get("/api/chungyak/subscribers", headers={"X-Admin-Token": "t0k"}).json()
        self.assertEqual([(s["chat_id"], s["is_enabled"]) for s in subs], [("123456789", True)])

    def test_admin_never_instantiates_chungyak_database(self):
        for rel in ("admin_server.py", "market_brief/storage/db.py"):
            self.assertNotIn("ChungyakDatabase", (ROOT / rel).read_text(encoding="utf-8"), rel)

    def test_admin_ui_panel_markup(self):
        from market_brief.admin_ui import ADMIN_DASHBOARD_HTML as html

        s = html.index('<div id="subpane-custom-dispatch"')
        e = html.index("</section>", s)
        markup = html[s:e]
        self.assertIn('id="chungyakPanel"', markup)
        self.assertNotIn("onclick=", markup)
        js = html[html.index("function chungyakStatusBadge("):html.index("(function bindChungyakUi()")]
        self.assertIn("QUEUED_MORNING", js)
        self.assertIn("UNKNOWN", js)
        self.assertNotIn("Number(", js)
        self.assertNotIn("parseFloat", js)
        self.assertNotIn("onclick", js)
        self.assertIn("addEventListener", html[html.index("(function bindChungyakUi()"):])


class CatalogueWiringTests(unittest.TestCase):
    def test_portal_enum_is_hidden_wip(self):
        from portal_hub.models import BotServiceType

        bot = BotServiceType("chungyak_bot")
        self.assertFalse(bot.is_public)
        self.assertTrue(bot.title and bot.short_name and bot.icon and bot.one_line_desc)

    def test_portal_runner_registers_inactive_idempotently(self):
        import portal_bot_runner
        from portal_hub import PortalDatabase

        db = PortalDatabase(":memory:")
        self.assertTrue(portal_bot_runner.ensure_chungyak_bot_registered(db))
        self.assertFalse(portal_bot_runner.ensure_chungyak_bot_registered(db))
        with db._get_conn() as conn:
            row = conn.execute("SELECT is_active FROM bots WHERE bot_id = 'chungyak_bot'").fetchone()
        self.assertEqual(row[0], 0)

    def test_manager_commands_route_to_interactive_bot(self):
        from chungyak_brief.interactive_bot import COMMANDS_ALL
        from market_brief.multi_bot_manager import OFFICIAL_BOT_COMMANDS, DynamicBotManager

        cmds = {"/" + c["command"] for c in OFFICIAL_BOT_COMMANDS["chungyak_bot"]}
        self.assertTrue(cmds <= set(COMMANDS_ALL), cmds - set(COMMANDS_ALL))
        self.assertTrue(callable(getattr(DynamicBotManager, "_dispatch_chungyak_bot_update", None)))


if __name__ == "__main__":
    unittest.main()
