"""Telegram all-bot button speed optimization (audit `scratch/gpt_architecture_bot_speed.md` §7, items 1-16).

Offline only: every HTTP call goes through an injected opener / fake session (no network, no real token,
no production SQLite, `TELEGRAM_BOT_TOKEN` untouched).
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from market_brief import briefing_service  # noqa: E402
from market_brief.channels import http_keepalive  # noqa: E402
from market_brief.channels.http_keepalive import (  # noqa: E402
    AckOnceClient,
    SessionRegistry,
    answer_callback_once,
    callback_ack_scope,
    open_request,
)
from market_brief.multi_bot_manager import (  # noqa: E402
    LOCKED_CALLBACK_ALERT,
    DynamicBotManager,
    MultiBotInteractionRouter,
    TelegramBotTransport,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
class _FakeHTTPResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


class _RecordingOpener:
    """Thread-safe fake `urlopen`: records (method, payload, thread) and serves getUpdates from `updates`."""

    def __init__(self, updates=None, log=None):
        self.updates = list(updates or [])
        self.calls: list[tuple[str, dict, str]] = []
        self.log = log if log is not None else []
        self._lock = threading.Lock()

    def __call__(self, req, timeout=10):
        method = req.full_url.rsplit("/", 1)[1]
        payload = json.loads(req.data.decode("utf-8")) if req.data else {}
        with self._lock:
            self.calls.append((method, payload, threading.current_thread().name))
            self.log.append(method)
        if method == "getUpdates":
            result = self.updates
        else:
            result = {"message_id": 1}
        return _FakeHTTPResp(json.dumps({"ok": True, "result": result}).encode("utf-8"))

    def methods(self, name):
        with self._lock:
            return [c for c in self.calls if c[0] == name]


def _cb_update(update_id: int, chat_id: int, data: str = "x:noop", cq_id: str | None = None) -> dict:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": cq_id or f"cq{update_id}",
            "from": {"id": chat_id, "first_name": "T"},
            "message": {"message_id": 7, "chat": {"id": chat_id, "type": "private"}},
            "data": data,
        },
    }


def _fake_manager(bot_id="trend_bot", bot_type="INFO", last_offset=5):
    engine = MagicMock()
    engine.db.db_path = ":memory:"
    bot = SimpleNamespace(bot_id=bot_id, bot_type=bot_type, token="111:AAA", last_offset=last_offset)
    engine.get_bot.side_effect = lambda _bid: bot

    def _upd(_bid, off):
        bot.last_offset = off

    engine.update_bot_offset.side_effect = _upd
    router = MagicMock()
    router.get_quick_callback_toast.return_value = ""
    router.handle_interaction.return_value = {"reply_html": "ok", "inline_keyboard": [], "result_code": "OK"}
    return DynamicBotManager(engine, router=router), bot


class _FakeSession:
    def __init__(self, status=200, payload=None):
        self.status, self.payload = status, payload or {"ok": True, "result": True}
        self.requests: list[tuple[str, str, dict]] = []
        self.closed = 0

    def request(self, method, url, data=None, headers=None, timeout=None):
        self.requests.append((method, url, dict(headers or {})))
        return SimpleNamespace(
            status_code=self.status, reason="Conflict" if self.status == 409 else "OK", headers={},
            content=json.dumps(self.payload).encode("utf-8"),
        )

    def close(self):
        self.closed += 1


# ---------------------------------------------------------------------------
# 1-4: keep-alive routing
# ---------------------------------------------------------------------------
class KeepAliveRoutingTests(unittest.TestCase):
    def setUp(self):
        # Fail-closed: the shared default registry must never open a real session in this suite.
        guard = patch.object(http_keepalive, "_DEFAULT_REGISTRY",
                             SessionRegistry(factory=lambda: self.fail("real default session used")))
        guard.start()
        self.addCleanup(guard.stop)

    def test_00_empty_registry_is_still_used(self):
        reg = SessionRegistry(factory=_FakeSession)
        self.assertEqual(len(reg), 0)  # falsy, yet must be honoured
        open_request(self._req(), 5, registry=reg).read()
        self.assertEqual(len(reg), 1)

    def _req(self, method="getMe"):
        return urllib.request.Request(
            f"https://api.telegram.org/bot1:A/{method}", data=b"{}", method="POST",
            headers={"Content-Type": "application/json"},
        )

    def test_01_default_path_reuses_thread_local_session(self):
        created: list[_FakeSession] = []

        def factory():
            created.append(_FakeSession())
            return created[-1]

        reg = SessionRegistry(factory=factory)
        for _ in range(3):
            with open_request(self._req(), 5, registry=reg) as resp:
                self.assertTrue(json.loads(resp.read())["ok"])
        self.assertEqual(len(created), 1)  # same thread -> one session, TLS reused
        self.assertEqual(len(created[0].requests), 3)
        self.assertNotIn("Connection", {k.title() for k in created[0].requests[0][2]})

        t = threading.Thread(target=lambda: open_request(self._req(), 5, registry=reg).read())
        t.start()
        t.join(5)
        self.assertEqual(len(created), 2)  # never shared across threads
        self.assertEqual(reg.close(), 2)
        self.assertTrue(all(s.closed == 1 for s in created))

    def test_01b_session_http_errors_keep_urllib_taxonomy(self):
        reg = SessionRegistry(factory=lambda: _FakeSession(409, {"ok": False, "description": "Conflict: other"}))
        with self.assertRaises(urllib.error.HTTPError) as cm:
            open_request(self._req(), 5, registry=reg)
        self.assertEqual(cm.exception.code, 409)
        self.assertIn(b"Conflict", cm.exception.read())

    def test_02_patched_urlopen_is_always_honoured(self):
        reg = SessionRegistry(factory=lambda: self.fail("session must not be created"))
        opener = _RecordingOpener()
        with patch("urllib.request.urlopen", opener):
            open_request(self._req("sendMessage"), 5, registry=reg).read()
            # channel classes route through the helper too
            from portal_hub.telegram import TelegramPortalClient
            from silver_care.channels.telegram import SilverTelegramChannel
            TelegramPortalClient("1:A").send_message("9", "hi")
            SilverTelegramChannel("1:A").send_message("9", "hi")
        self.assertEqual([c[0] for c in opener.calls], ["sendMessage"] * 3)

    def test_03_injected_opener_is_always_honoured(self):
        from admission_brief.channels.telegram import TelegramChannel as AdmChannel
        from kids_compass.channels.telegram import KidsTelegramChannel
        from portal_hub.telegram import TelegramPortalClient

        opener = _RecordingOpener()
        with patch.object(http_keepalive, "_DEFAULT_REGISTRY", SessionRegistry(factory=lambda: self.fail("no session"))):
            open_request(self._req(), 5, opener=opener).read()
            client = TelegramPortalClient("1:A", opener=opener)
            client.answer_callback_query("cq1")
            AdmChannel("1:A", opener=opener).send_message("9", "hi")
            KidsTelegramChannel("1:A", opener=opener).send_message("9", "hi")
        self.assertEqual([c[0] for c in opener.calls], ["getMe", "answerCallbackQuery", "sendMessage", "sendMessage"])

    def test_03b_portal_client_drops_connection_close_header(self):
        seen: list[dict] = []

        def opener(req, timeout=10):
            seen.append({k.lower(): v for k, v in req.header_items()})
            return _FakeHTTPResp(b'{"ok": true, "result": {}}')

        from portal_hub.telegram import TelegramPortalClient
        TelegramPortalClient("1:A", opener=opener).send_message("9", "hi")
        self.assertNotIn("connection", seen[0])

    def test_04_yolo_channel_opener_and_dry_run(self):
        from yolo_brief.channels.telegram import YoloTelegramChannel

        opener = _RecordingOpener()
        ch = YoloTelegramChannel(token="1:A", opener=opener)
        self.assertFalse(ch.dry_run)
        self.assertTrue(ch.answer_callback("cq1", text="hi"))
        self.assertEqual([c[0] for c in opener.calls], ["answerCallbackQuery"])

        with patch.dict("os.environ", {"YOLO_TELEGRAM_BOT_TOKEN": "", "YOLO_BOT_TOKEN": ""}):
            dry = YoloTelegramChannel(token="", opener=opener)
        self.assertTrue(dry.dry_run)
        res = dry.send_message("9", "x")
        self.assertTrue(res["result"]["dry_run"])
        self.assertEqual(len(opener.calls), 1)  # dry-run never touches the opener
        self.assertEqual(YoloTelegramChannel(token="1:A", dry_run=True)._opener, None)


# ---------------------------------------------------------------------------
# 5-6, 16: acknowledgement ownership
# ---------------------------------------------------------------------------
class AckOwnershipTests(unittest.TestCase):
    def test_05_exactly_one_ack_per_callback(self):
        inner = MagicMock()
        with callback_ack_scope("cqA"):
            client = AckOnceClient(inner)
            client.answer_callback_query("cqA")
            client.answer_callback_query("cqA", "again", show_alert=True)
            with callback_ack_scope("cqA"):  # nested handler scope joins the manager's
                answer_callback_once("cqA", lambda: inner.answer_callback_query("cqA", "third"))
            client.send_message(1, "x")  # non-ack calls are delegated untouched
        inner.answer_callback_query.assert_called_once_with("cqA")
        inner.send_message.assert_called_once_with(1, "x")
        # outside a scope: legacy pass-through (standalone runners keep acking inline)
        AckOnceClient(inner).answer_callback_query("cqA")
        self.assertEqual(inner.answer_callback_query.call_count, 2)

    def test_05b_manager_dispatch_never_double_acks(self):
        for parallel in (False, True):
            mgr, _bot = _fake_manager()
            opener = _RecordingOpener([_cb_update(10, 101), _cb_update(11, 202)])

            def handle_interaction(**kw):
                tr = mgr._transport_for("trend_bot", "111:AAA", opener=opener)
                tr.answer_callback(f"cq{10 if kw['telegram_user_id'] == '101' else 11}", text="dup")
                return {"reply_html": "ok", "inline_keyboard": [], "result_code": "OK"}

            mgr.router.handle_interaction.side_effect = handle_interaction
            res = mgr.sync_bot_updates_once("trend_bot", opener=opener, parallel_callbacks=parallel)
            self.assertEqual(res["processed"], 2)
            acks = sorted(c[1]["callback_query_id"] for c in opener.methods("answerCallbackQuery"))
            self.assertEqual(acks, ["cq10", "cq11"], f"parallel={parallel}")
            asyncio.run(mgr.stop())

    def test_05c_parallel_unacked_callback_gets_single_blank_ack(self):
        mgr, _bot = _fake_manager()
        mgr._process_update = lambda *a: {"type": "silent"}
        opener = _RecordingOpener([_cb_update(10, 1), _cb_update(11, 2)])
        mgr.sync_bot_updates_once("trend_bot", opener=opener, parallel_callbacks=True)
        self.assertEqual(sorted(c[1]["callback_query_id"] for c in opener.methods("answerCallbackQuery")), ["cq10", "cq11"])
        asyncio.run(mgr.stop())

    def test_06_ack_pool_saturation_falls_back_inline_before_work(self):
        mgr, _bot = _fake_manager()
        log: list[tuple[str, str]] = []
        opener = _RecordingOpener([_cb_update(10, 1), _cb_update(11, 2)])

        def process(bot, transport, item, op):
            cq = item["callback_query"]["id"]
            transport.answer_callback(cq, text="t")
            # saturated -> the ack already hit the wire (inline) BEFORE this update's work starts
            acked_before = any(c[1].get("callback_query_id") == cq for c in opener.methods("answerCallbackQuery"))
            log.append((threading.current_thread().name, acked_before))
            return {"cq": cq}

        mgr._process_update = process
        # occupy every ack slot -> saturated
        mgr._ack_slots = threading.BoundedSemaphore(1)
        mgr._ack_slots.acquire()
        mgr.sync_bot_updates_once("trend_bot", opener=opener, parallel_callbacks=True)
        acks = opener.methods("answerCallbackQuery")
        self.assertEqual(len(acks), 2)
        self.assertTrue(all(a[2].startswith("tg-lane") for a in acks))  # inline on the lane thread
        self.assertEqual(len(log), 2)
        self.assertTrue(all(name.startswith("tg-lane") and acked for name, acked in log))
        mgr._ack_slots.release()

        # free slots -> acks are offloaded to the bounded ack pool
        mgr._ack_slots = threading.BoundedSemaphore(8)
        opener2 = _RecordingOpener([_cb_update(20, 1), _cb_update(21, 2)])
        mgr.sync_bot_updates_once("trend_bot", opener=opener2, parallel_callbacks=True)
        self.assertTrue(all(a[2].startswith("tg-ack") for a in opener2.methods("answerCallbackQuery")))
        asyncio.run(mgr.stop())

    def test_16_subscription_gate_acks_before_output_in_both_modes(self):
        for parallel in (False, True):
            mgr, _bot = _fake_manager()
            mgr.engine.is_user_subscribed.return_value = False
            mgr.router.render_locked_response.return_value = {
                "reply_html": "locked", "inline_keyboard": [], "result_code": "NOT_SUBSCRIBED"}
            log: list[str] = []
            opener = _RecordingOpener([_cb_update(10, 1, data="info:preview:trend_bot")], log=log)
            real_offload = mgr._offload_ack

            def offload(send):
                log.append("ack_dispatched")
                return real_offload(send)

            mgr._offload_ack = offload
            res = mgr.sync_bot_updates_once("trend_bot", opener=opener, parallel_callbacks=parallel)
            self.assertEqual(res["events"][0]["type"], "locked")
            acks = opener.methods("answerCallbackQuery")
            self.assertEqual(len(acks), 1)
            self.assertEqual(acks[0][1]["text"], LOCKED_CALLBACK_ALERT)
            self.assertTrue(acks[0][1]["show_alert"])
            first_ack = log.index("ack_dispatched") if parallel else log.index("answerCallbackQuery")
            self.assertLess(first_ack, log.index("sendMessage"))
            asyncio.run(mgr.stop())


# ---------------------------------------------------------------------------
# 7-11: lanes, ordering, offsets
# ---------------------------------------------------------------------------
class LaneAndOffsetTests(unittest.TestCase):
    def test_07_different_chats_process_concurrently(self):
        mgr, _bot = _fake_manager()
        barrier = threading.Barrier(2, timeout=5)

        def process(bot, transport, item, op):
            barrier.wait()  # deadlocks (BrokenBarrierError) unless both chats run at the same time
            return {"chat": item["callback_query"]["from"]["id"]}

        mgr._process_update = process
        res = mgr.sync_bot_updates_once(
            "trend_bot", opener=_RecordingOpener([_cb_update(10, 1), _cb_update(11, 2)]), parallel_callbacks=True)
        self.assertEqual(res["processed"], 2)
        self.assertNotIn("failed_update_ids", res)
        asyncio.run(mgr.stop())

    def test_08_same_chat_preserves_fifo_without_overlap(self):
        mgr, _bot = _fake_manager()
        seen: dict[int, list[int]] = {1: [], 2: []}
        active = {1: 0, 2: 0}
        overlap: list[int] = []
        lock = threading.Lock()

        def process(bot, transport, item, op):
            chat = item["callback_query"]["from"]["id"]
            with lock:
                active[chat] += 1
                if active[chat] > 1:
                    overlap.append(chat)
            time.sleep(0.03 if item["update_id"] % 2 else 0.005)
            with lock:
                seen[chat].append(item["update_id"])
                active[chat] -= 1
            return {"u": item["update_id"]}

        mgr._process_update = process
        ups = [_cb_update(10 + i, 1 if i % 3 else 2) for i in range(9)]
        mgr.sync_bot_updates_once("trend_bot", opener=_RecordingOpener(list(reversed(ups))), parallel_callbacks=True)
        self.assertEqual(overlap, [])
        self.assertEqual(seen[1], sorted(seen[1]))
        self.assertEqual(seen[2], sorted(seen[2]))
        self.assertEqual(len(seen[1]) + len(seen[2]), 9)
        asyncio.run(mgr.stop())

    def test_09_10_waits_for_all_work_and_events_in_update_order(self):
        mgr, _bot = _fake_manager()
        finished: list[int] = []

        def process(bot, transport, item, op):
            up = item["update_id"]
            time.sleep(0.25 if up == 10 else 0.01)  # first update finishes LAST
            finished.append(up)
            return {"u": up}

        mgr._process_update = process
        ups = [_cb_update(10, 1), _cb_update(11, 2), _cb_update(12, 3)]
        res = mgr.sync_bot_updates_once("trend_bot", opener=_RecordingOpener(ups), parallel_callbacks=True)
        self.assertEqual(sorted(finished), [10, 11, 12])  # nothing still running after return
        self.assertEqual(finished[-1], 10)
        self.assertEqual([e["u"] for e in res["events"]], [10, 11, 12])
        self.assertEqual(res["last_offset"], 12)
        asyncio.run(mgr.stop())

    def test_11_failed_update_never_advances_offset_past_it(self):
        mgr, bot = _fake_manager(last_offset=9)
        calls: list[int] = []
        fail = {11}

        def process(bot_, transport, item, op):
            up = item["update_id"]
            calls.append(up)
            if up in fail:
                raise RuntimeError("boom")
            return {"u": up}

        mgr._process_update = process
        ups = [_cb_update(10, 1), _cb_update(11, 2), _cb_update(12, 1), _cb_update(13, 2)]
        res = mgr.sync_bot_updates_once("trend_bot", opener=_RecordingOpener(ups), parallel_callbacks=True)
        self.assertEqual(res["failed_update_ids"], [11])
        self.assertEqual(res["last_offset"], 10)
        self.assertEqual(bot.last_offset, 10)
        self.assertNotIn(13, calls)  # same chat after a failure is deferred (FIFO kept)
        self.assertIn(12, calls)

        # replay: 11 now succeeds; 12 (already done) is NOT re-executed; 13 runs after 11
        fail.clear()
        calls.clear()
        res2 = mgr.sync_bot_updates_once("trend_bot", opener=_RecordingOpener(ups[1:]), parallel_callbacks=True)
        self.assertEqual(sorted(calls), [11, 13])
        self.assertEqual(calls.index(11) < calls.index(13), True)
        self.assertEqual(res2["last_offset"], 13)
        self.assertEqual(bot.last_offset, 13)
        self.assertNotIn("failed_update_ids", res2)
        asyncio.run(mgr.stop())

    def test_11b_poison_update_is_quarantined_after_max_attempts(self):
        mgr, bot = _fake_manager(last_offset=9)
        mgr._process_update = lambda b, t, item, o: (_ for _ in ()).throw(RuntimeError("poison"))
        ups = [_cb_update(10, 1)]
        for attempt in range(1, mgr.MAX_UPDATE_ATTEMPTS):
            res = mgr.sync_bot_updates_once("trend_bot", opener=_RecordingOpener(ups), parallel_callbacks=True)
            self.assertEqual((res["failed_update_ids"], res["last_offset"]), ([10], 9), attempt)
        res = mgr.sync_bot_updates_once("trend_bot", opener=_RecordingOpener(ups), parallel_callbacks=True)
        self.assertEqual(res["quarantined_update_ids"], [10])
        self.assertEqual(bot.last_offset, 10)
        asyncio.run(mgr.stop())

    def test_11c_sequential_mode_raises_after_persisting_contiguous_prefix(self):
        mgr, bot = _fake_manager(last_offset=9)

        def process(b, t, item, o):
            if item["update_id"] == 11:
                raise RuntimeError("boom")
            return {"u": item["update_id"]}

        mgr._process_update = process
        ups = [_cb_update(10, 1), _cb_update(11, 1), _cb_update(12, 1)]
        with self.assertRaises(RuntimeError):
            mgr.sync_bot_updates_once("trend_bot", opener=_RecordingOpener(ups))
        self.assertEqual(bot.last_offset, 10)

    def test_poll_lock_serializes_same_bot(self):
        mgr, _bot = _fake_manager()
        active, peak = [0], [0]
        lock = threading.Lock()

        def process(b, t, item, o):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1
            return {}

        mgr._process_update = process
        threads = [
            threading.Thread(target=mgr.sync_bot_updates_once, args=("trend_bot",),
                             kwargs={"opener": _RecordingOpener([_cb_update(100 + i, 1)])})
            for i in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        self.assertEqual(peak[0], 1)


# ---------------------------------------------------------------------------
# 12-13: transport lifecycle / shutdown
# ---------------------------------------------------------------------------
class LifecycleTests(unittest.TestCase):
    def test_12_token_change_closes_old_transport(self):
        mgr, _bot = _fake_manager()
        first = mgr._transport_for("trend_bot", "1:A")
        self.assertIs(mgr._transport_for("trend_bot", "1:A"), first)
        second = mgr._transport_for("trend_bot", "2:B")
        self.assertIsNot(second, first)
        self.assertTrue(first._closed)
        self.assertFalse(second._closed)

    def test_13_stop_drains_workers_and_closes_transports_and_sessions(self):
        mgr, _bot = _fake_manager()
        reg = SessionRegistry(factory=_FakeSession)
        sess = reg.get()
        done: list[int] = []

        def slow():
            time.sleep(0.2)
            done.append(1)

        mgr._get_worker_pool().submit(slow)
        fut = mgr._offload_ack(lambda: (time.sleep(0.1), done.append(2)))
        self.assertIsNotNone(fut)
        tr = mgr._transport_for("trend_bot", "1:A")
        with patch.object(http_keepalive, "_DEFAULT_REGISTRY", reg):
            asyncio.run(mgr.stop())
        self.assertEqual(sorted(done), [1, 2])  # drained, not abandoned
        self.assertIsNone(mgr._worker_pool)
        self.assertIsNone(mgr._ack_pool)
        self.assertTrue(tr._closed)
        self.assertEqual(mgr._transports, {})
        self.assertEqual(sess.closed, 1)


# ---------------------------------------------------------------------------
# 14: seed-once registry
# ---------------------------------------------------------------------------
class SeedOnceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.platform_db = str(Path(self.tmp.name) / "alerts.sqlite3")

    def tearDown(self):
        self.tmp.cleanup()

    def _mgr(self, path):
        engine = MagicMock()
        engine.db.db_path = path
        return DynamicBotManager(engine, router=MagicMock())

    def test_14_silver_kids_yolo_seed_once_per_db_path(self):
        msg = {"update_id": 1, "message": {"message_id": 1, "chat": {"id": 5, "type": "private"}, "text": "hello"}}
        mgr = self._mgr(self.platform_db)
        with patch("kids_compass.collector.seed_all") as kids_seed, \
             patch("yolo_brief.storage.db.YoloDatabase.seed_curations") as yolo_seed, \
             patch("silver_care.storage.db.SilverCareDatabase.upsert_benefits") as silver_seed, \
             patch("silver_care.storage.db.SilverCareDatabase.count_benefits", return_value=0):
            for _ in range(3):
                mgr._dispatch_kids_bot_update("1:A", msg)
                mgr._dispatch_yolo_bot_update("1:A", msg)
                mgr._dispatch_silver_bot_update("1:A", msg)
            self.assertEqual((kids_seed.call_count, yolo_seed.call_count, silver_seed.call_count), (1, 1, 1))

            # an equivalent (non-normalized) spelling of the same path is the same key
            alias = str(Path(self.tmp.name) / "." / "alerts.sqlite3")
            mgr.engine.db.db_path = alias
            mgr._dispatch_yolo_bot_update("1:A", msg)
            self.assertEqual(yolo_seed.call_count, 1)

            # :memory: databases are fresh instances -> always seeded
            mem = self._mgr(":memory:")
            mem._dispatch_yolo_bot_update("1:A", msg)
            mem._dispatch_yolo_bot_update("1:A", msg)
            self.assertEqual(yolo_seed.call_count, 3)

    def test_14b_kids_seed_rearms_daily(self):
        mgr = self._mgr(self.platform_db)
        ran: list[int] = []
        self.assertTrue(mgr._seed_once("kids_bot", "x.sqlite3", lambda: ran.append(1), epoch="2026-11-19"))
        self.assertFalse(mgr._seed_once("kids_bot", "x.sqlite3", lambda: ran.append(1), epoch="2026-11-19"))
        self.assertTrue(mgr._seed_once("kids_bot", "x.sqlite3", lambda: ran.append(1), epoch="2026-11-20"))
        self.assertEqual(len(ran), 2)


# ---------------------------------------------------------------------------
# 15: cache
# ---------------------------------------------------------------------------
class BriefingCacheTests(unittest.TestCase):
    def setUp(self):
        self.router = MultiBotInteractionRouter(MagicMock())
        self.bot = SimpleNamespace(bot_id="trend_bot", icon_emoji="🔥", name="트렌드", description="d")
        self.gen = MagicMock(return_value={"content_html": "<b>t</b>", "inline_keyboard": [[{"text": "a", "url": "u"}]]})

    def test_15_hit_expiry_refresh_key_and_snapshot_isolation(self):
        with patch("market_brief.briefing_service.generate_bot_briefing", self.gen):
            a = self.router._build_target_briefing(self.bot, "ALL", menu_uname="menu")
            b = self.router._build_target_briefing(self.bot, "all", menu_uname="menu")
            self.assertEqual(self.gen.call_count, 1)  # hit
            self.assertEqual(a, b)
            a["inline_keyboard"].append([{"text": "mutated"}])
            a["inline_keyboard"][0][0]["text"] = "mutated"
            c = self.router._build_target_briefing(self.bot, "ALL", menu_uname="menu")
            self.assertEqual(c, b)  # cached snapshot is immutable from the caller's side

            self.router._build_target_briefing(self.bot, "ALL", menu_uname="other_menu")
            self.assertEqual(self.gen.call_count, 2)  # menu username is part of the key
            self.router._build_target_briefing(self.bot, "ALL", menu_uname="menu", refresh=True)
            self.assertEqual(self.gen.call_count, 3)  # explicit refresh bypasses

            self.router.BRIEFING_CACHE_TTL_SEC = 0.0  # expiry
            self.router._build_target_briefing(self.bot, "ALL", menu_uname="menu")
            self.assertEqual(self.gen.call_count, 4)

    def test_15b_live_mode_is_part_of_key(self):
        with patch("market_brief.briefing_service.generate_bot_briefing", self.gen), \
             patch.object(MultiBotInteractionRouter, "_briefing_live_mode", side_effect=[False, True, True]):
            self.router._build_target_briefing(self.bot)
            self.router._build_target_briefing(self.bot)
            self.router._build_target_briefing(self.bot)
        self.assertEqual(self.gen.call_count, 2)

    def test_15c_concurrent_access(self):
        def slow(*a, **k):
            time.sleep(0.01)
            return {"content_html": "x", "inline_keyboard": [[{"text": "a", "url": "u"}]]}

        errors: list[BaseException] = []
        results: list[dict] = []

        def worker():
            try:
                for _ in range(20):
                    results.append(self.router._build_target_briefing(self.bot, "ALL", menu_uname="m"))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        with patch("market_brief.briefing_service.generate_bot_briefing", side_effect=slow):
            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 160)
        self.assertTrue(all(r == results[0] for r in results))

    def test_15d_briefing_service_opt_in_cache(self):
        briefing_service.clear_briefing_cache()
        with patch.object(briefing_service, "_generate_bot_briefing_uncached",
                          side_effect=lambda bid, preset=None, db=None: {"bot_id": bid, "kb": [[{"t": 1}]]}) as gen:
            briefing_service.generate_bot_briefing("x_bot")
            briefing_service.generate_bot_briefing("x_bot")
            self.assertEqual(gen.call_count, 2)  # default: uncached (admin previews stay fresh)
            r1 = briefing_service.generate_bot_briefing("x_bot", cache_ttl=30)
            r1["kb"][0][0]["t"] = 99
            r2 = briefing_service.generate_bot_briefing("x_bot", cache_ttl=30)
            self.assertEqual(gen.call_count, 3)
            self.assertEqual(r2["kb"][0][0]["t"], 1)
            briefing_service.generate_bot_briefing("x_bot", cache_ttl=30, refresh=True)
            briefing_service.generate_bot_briefing("x_bot", preset="p2", cache_ttl=30)
            self.assertEqual(gen.call_count, 5)
        briefing_service.clear_briefing_cache()


if __name__ == "__main__":
    unittest.main()
