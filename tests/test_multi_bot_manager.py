"""`TelegramBotTransport` keep-alive lifecycle + `DynamicBotManager` per-bot transport reuse.

Offline only: sessions are injected through `TelegramBotTransport._new_session` (no network, no real token).
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
import threading
import types
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from market_brief import multi_bot_manager as mbm  # noqa: E402
from market_brief.multi_bot_manager import DynamicBotManager, TelegramBotTransport  # noqa: E402


class _FakeSessionResp:
    def __init__(self, status: int, payload: dict, reason: str = "OK", headers: dict | None = None):
        self.status_code, self.reason = status, reason
        self.headers = headers or {"Content-Type": "application/json"}
        self.content = json.dumps(payload).encode("utf-8")


class _FakeHTTPResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def _ok(result) -> _FakeSessionResp:
    return _FakeSessionResp(200, {"ok": True, "result": result})


class _SessionFactory:
    """Records every session the transport creates; each session answers with `responder(method)`."""

    def __init__(self, responder=None):
        self.created: list[MagicMock] = []
        self.responder = responder or (lambda method: _ok([]))

    def __call__(self, _transport_self):
        sess = MagicMock()

        def _post(url, **_kw):
            return self.responder(url.rsplit("/", 1)[1])

        sess.post.side_effect = _post
        sess.get.side_effect = lambda url, **_kw: self.responder(url.rsplit("/", 1)[1])
        self.created.append(sess)
        return sess


def _fake_manager(token: str = "111:AAA", last_offset: int = 5):
    engine = MagicMock()
    bot = SimpleNamespace(bot_id="trend_bot", bot_type="INFO", token=token, last_offset=last_offset)
    engine.get_bot.side_effect = lambda bot_id: bot
    return DynamicBotManager(engine, router=MagicMock()), bot


class TelegramBotTransportSessionTests(unittest.TestCase):
    def test_default_path_reuses_thread_local_session_and_closes_cleanly(self):
        factory = _SessionFactory(lambda m: _ok({"username": "kw_bot"} if m == "getMe" else True))
        tr = TelegramBotTransport(" 123:abc ")
        with patch.object(TelegramBotTransport, "_new_session", autospec=True, side_effect=factory):
            self.assertEqual(tr.get_me(), {"username": "kw_bot"})
            tr.answer_callback("cq1", text="ok")
            self.assertEqual(len(factory.created), 1)  # same thread -> one session (TLS reused)
            sess = factory.created[0]
            self.assertTrue(sess.get.call_args[0][0].endswith("/bot123:abc/getMe"))  # GET for payload-less calls
            args, kwargs = sess.post.call_args
            self.assertTrue(args[0].endswith("/answerCallbackQuery"))
            self.assertEqual(json.loads(kwargs["data"].decode("utf-8"))["callback_query_id"], "cq1")
            self.assertEqual(kwargs["headers"], {"Content-Type": "application/json"})
            self.assertEqual(kwargs["timeout"], 5)

            out: list = []
            t = threading.Thread(target=lambda: out.append(tr.get_me()))
            t.start()
            t.join(5)
            self.assertEqual(out, [{"username": "kw_bot"}])
            self.assertEqual(len(factory.created), 2)  # another thread never shares the mutable Session

            tr.close()
            for s in factory.created:
                s.close.assert_called_once()
            self.assertIsNone(tr._get_session())  # closed transport never re-creates a session
            tr.close()  # idempotent
            for s in factory.created:
                s.close.assert_called_once()

    def test_http_errors_are_reconstructed_as_urllib_httperror(self):
        def responder(method):
            if method == "getUpdates":
                return _FakeSessionResp(409, {"ok": False, "description": "Conflict: terminated by other getUpdates"},
                                        reason="Conflict")
            if method == "sendMessage":
                return _FakeSessionResp(502, {}, reason="Bad Gateway")
            return _FakeSessionResp(200, {"ok": False, "description": "nope"})

        factory = _SessionFactory(responder)
        tr = TelegramBotTransport("123:abc")
        with patch.object(TelegramBotTransport, "_new_session", autospec=True, side_effect=factory):
            with self.assertRaises(urllib.error.HTTPError) as cm409:
                tr.get_updates(offset=1, timeout=1)
            self.assertEqual(cm409.exception.code, 409)
            self.assertEqual(str(cm409.exception), "HTTP Error 409: Conflict")
            self.assertIn(b"Conflict", cm409.exception.read())

            with self.assertRaises(urllib.error.HTTPError) as cm502:
                tr.send_html("c1", "<b>x</b>")
            self.assertEqual((cm502.exception.code, str(cm502.exception)), (502, "HTTP Error 502: Bad Gateway"))

            # HTTP 200 + ok=false keeps the RuntimeError contract
            with self.assertRaises(RuntimeError) as cm:
                tr.get_me()
            self.assertEqual(str(cm.exception), "Telegram API error (getMe): nope")

        # identical contract on the legacy urllib path (injected opener raising HTTPError)
        def opener(req, timeout=10):
            raise urllib.error.HTTPError(req.full_url, 409, "Conflict", {}, io.BytesIO(b"{}"))

        with self.assertRaises(urllib.error.HTTPError) as legacy:
            TelegramBotTransport("123:abc", opener=opener).get_updates(offset=1, timeout=1)
        self.assertEqual(str(legacy.exception), str(cm409.exception))

    def test_ok_false_conflict_on_get_updates_still_backs_off(self):
        factory = _SessionFactory(lambda m: _FakeSessionResp(200, {"ok": False, "description": "Conflict: 409"}))
        tr = TelegramBotTransport("123:abc")
        with patch.object(TelegramBotTransport, "_new_session", autospec=True, side_effect=factory):
            self.assertEqual(tr.get_updates(offset=1, timeout=1), [])

    def test_network_errors_keep_urllib_taxonomy(self):
        class _Timeout(Exception):
            pass

        class _ConnErr(Exception):
            pass

        fake_requests = types.SimpleNamespace(exceptions=types.SimpleNamespace(Timeout=_Timeout, ConnectionError=_ConnErr))
        sess = MagicMock()
        tr = TelegramBotTransport("123:abc")
        with patch.object(TelegramBotTransport, "_new_session", autospec=True, return_value=sess), \
             patch.object(mbm, "_import_requests", return_value=(fake_requests, object)):
            sess.post.side_effect = _Timeout("Read timed out. (read timeout=10)")
            with self.assertRaises(TimeoutError) as cm:  # dispatcher -> UNKNOWN (never a duplicate resend)
                tr.send_html("c1", "x")
            self.assertIn("timed out", str(cm.exception))
            sess.post.side_effect = _ConnErr("Connection aborted")
            with self.assertRaises(urllib.error.URLError):
                tr.send_html("c1", "x")

    def test_injected_opener_and_patched_urlopen_bypass_sessions(self):
        calls: list = []

        def opener(req, timeout=10):
            calls.append(req.full_url.rsplit("/", 1)[1])
            return _FakeHTTPResp(json.dumps({"ok": True, "result": {"id": 1}}).encode("utf-8"))

        with patch.object(TelegramBotTransport, "_new_session", side_effect=AssertionError("no session")):
            tr = TelegramBotTransport("123:abc", opener=opener)
            self.assertTrue(tr._has_custom_opener)
            self.assertEqual(tr.get_me(), {"id": 1})
            self.assertEqual(calls, ["getMe"])

            late = TelegramBotTransport("123:abc")  # created BEFORE the urlopen patch
            self.assertFalse(late._has_custom_opener)
            with patch("urllib.request.urlopen", side_effect=AssertionError("network forbidden")) as guard:
                with self.assertRaises(AssertionError):
                    late.get_me()  # a later patch is honoured (resolved at call time), session never used
                with self.assertRaises(AssertionError):
                    TelegramBotTransport("123:abc").get_me()
            self.assertEqual(guard.call_count, 2)


class DynamicBotManagerTransportReuseTests(unittest.TestCase):
    def test_transport_and_session_reused_across_polls_then_closed(self):
        factory = _SessionFactory(lambda m: _ok([]))
        mgr, bot = _fake_manager()
        with patch.object(TelegramBotTransport, "_new_session", autospec=True, side_effect=factory):
            res1 = mgr.sync_bot_updates_once("trend_bot", timeout=1)
            first = mgr._transports["trend_bot"][1]
            res2 = mgr.sync_bot_updates_once("trend_bot", timeout=1)
            self.assertIs(mgr._transports["trend_bot"][1], first)  # one transport per (bot_id, token)
            self.assertEqual((res1["processed"], res2["processed"]), (0, 0))
            self.assertEqual(len(factory.created), 1)  # same poll thread -> the keep-alive session is reused
            self.assertEqual(factory.created[0].post.call_count, 2)
            self.assertTrue(all(c[0][0].endswith("/bot111:AAA/getUpdates") for c in factory.created[0].post.call_args_list))

            # token change -> new transport, the old one's sessions are closed
            bot.token = "222:BBB"
            mgr.sync_bot_updates_once("trend_bot", timeout=1)
            second = mgr._transports["trend_bot"][1]
            self.assertIsNot(second, first)
            self.assertEqual(mgr._transports["trend_bot"][0], "222:BBB")
            factory.created[0].close.assert_called_once()
            self.assertTrue(first._closed)
            self.assertEqual(len(factory.created), 2)

            # stop() closes every cached transport
            asyncio.run(mgr.stop())
            factory.created[1].close.assert_called_once()
            self.assertTrue(second._closed)
            self.assertEqual(mgr._transports, {})

    def test_reload_bots_closes_removed_or_token_changed_transports(self):
        mgr, _bot = _fake_manager()
        keep, drop, changed = TelegramBotTransport("1:A"), TelegramBotTransport("2:B"), TelegramBotTransport("3:C")
        mgr._transports = {"keep_bot": ("1:A", keep), "gone_bot": ("2:B", drop), "chg_bot": ("3:C", changed)}
        mgr._close_transports(keep={"keep_bot": "1:A", "chg_bot": "3:NEW"})
        self.assertEqual(set(mgr._transports), {"keep_bot"})
        self.assertFalse(keep._closed)
        self.assertTrue(drop._closed and changed._closed)

    def test_injected_opener_polls_are_never_cached(self):
        calls: list = []

        def opener(req, timeout=10):
            calls.append(req.full_url.rsplit("/", 1)[1])
            return _FakeHTTPResp(json.dumps({"ok": True, "result": []}).encode("utf-8"))

        mgr, _bot = _fake_manager()
        with patch.object(TelegramBotTransport, "_new_session", side_effect=AssertionError("no session")):
            mgr.sync_bot_updates_once("trend_bot", opener=opener, timeout=1)
            mgr.sync_bot_updates_once("trend_bot", opener=opener, timeout=1)
        self.assertEqual(calls, ["getUpdates", "getUpdates"])
        self.assertEqual(mgr._transports, {})


if __name__ == "__main__":
    unittest.main()
