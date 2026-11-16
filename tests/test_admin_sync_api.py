"""Admin Sync API: custom watches / custom deliveries / travel 2-track prefs (no Telegram I/O, temp DBs only)."""
from __future__ import annotations

import os
import re
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from admin_server import create_admin_app

XSS = "<img src=x onerror=alert(1)>\"'"
BIG_DECIMAL = "2650.123456789012345678"


def _utc(delta_hours: float = 0.0) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=delta_hours)).strftime("%Y-%m-%d %H:%M:%S")


class _Base(unittest.TestCase):
    def setUp(self):
        os.environ.pop("ADMIN_API_TOKEN", None)
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "admin_sync.sqlite3"
        self.env_path = Path(self.tmp.name) / "empty.env"
        self.app = create_admin_app(db_path=str(self.db_path), env_path=self.env_path)
        self.db = self.app.state.db
        self.client = TestClient(self.app)

    def tearDown(self):
        try:
            self.tmp.cleanup()
        except Exception:
            pass

    def _sql(self, sql: str, params: tuple = ()) -> None:
        conn = sqlite3.connect(str(self.db_path))
        try:
            with conn:
                conn.execute(sql, params)
        finally:
            conn.close()

    def _tables(self) -> set[str]:
        conn = sqlite3.connect(str(self.db_path))
        try:
            return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()


class FreshMarketOnlyDbTests(_Base):
    def test_fresh_db_has_no_travel_tables_and_endpoints_return_empty(self):
        self.assertNotIn("travel_price_watches", self._tables())

        st = self.client.get("/api/status")
        self.assertEqual(st.status_code, 200)
        self.assertEqual(st.json()["active_market_watches_count"], 0)
        self.assertEqual(st.json()["recent_market_watch_deliveries_24h"], 0)

        tr = self.client.get("/api/travel/status")
        if tr.status_code != 404:  # travel module optional
            self.assertEqual(tr.status_code, 200)
            body = tr.json()
            for key in ("active_watches_count", "paused_users_count", "weekly_disabled_count", "recent_watch_deliveries_24h"):
                self.assertEqual(body[key], 0, key)
            self.assertIn("subscribers_total", body)  # legacy field preserved

        w = self.client.get("/api/platform/watches")
        self.assertEqual(w.status_code, 200)
        self.assertEqual(w.json(), {"items": [], "user_prefs": []})

        d = self.client.get("/api/platform/custom-deliveries")
        self.assertEqual(d.status_code, 200)
        self.assertEqual(d.json(), {"items": [], "limit": 50, "has_more": False})

        # Admin reads must never create travel tables.
        self.assertNotIn("travel_price_watches", self._tables())
        self.assertNotIn("travel_user_prefs", self._tables())

    def test_deactivate_travel_on_market_only_db_is_404(self):
        res = self.client.post("/api/platform/watches/travel_bot/1/deactivate")
        self.assertEqual(res.status_code, 404)
        self.assertNotIn("travel_price_watches", self._tables())

    def test_validation(self):
        self.assertEqual(self.client.get("/api/platform/watches?bot_id=evil").status_code, 400)
        self.assertEqual(self.client.get("/api/platform/custom-deliveries?bot_id=evil").status_code, 400)
        self.assertEqual(self.client.get("/api/platform/custom-deliveries?status=DROP").status_code, 400)
        self.assertEqual(self.client.post("/api/platform/watches/all/1/deactivate").status_code, 400)
        self.assertEqual(self.client.get("/api/platform/custom-deliveries?limit=999").json()["limit"], 100)
        self.assertEqual(self.client.get("/api/platform/custom-deliveries?limit=0").json()["limit"], 1)
        self.assertEqual(self.client.get("/api/platform/custom-deliveries?status=sent").status_code, 200)

    def test_history_endpoint_unchanged(self):
        res = self.client.get("/api/history")
        self.assertEqual(res.status_code, 200)
        self.assertIsInstance(res.json(), list)


class PopulatedFixtureTests(_Base):
    def setUp(self):
        super().setUp()
        from travel_brief.storage.db import TravelBriefDatabase

        TravelBriefDatabase(db_path=str(self.db_path), env_path=self.env_path)  # shared-file travel schema

        # --- market: named user + nameless user (chat_id fallback) -------------------------------
        self._sql(
            "INSERT INTO users (telegram_user_id, full_name) VALUES (?, ?)", ("1001", f"홍길동{XSS}")
        )
        r1 = self.db.create_market_watch(
            "1001", "1001", symbol="KOSPI", market="KR", quote_unit="POINT", condition_type="CHANGE_PCT",
            direction="BOTH", threshold_value="3.10", threshold_unit="PERCENT",
        )
        r2 = self.db.create_market_watch(
            "2002", "2002", symbol="USDKRW", market="FX", quote_unit="KRW", condition_type="CHANGE_PCT",
            direction="UP", threshold_value="0.000000000000000001", threshold_unit="PERCENT",
        )
        self.assertTrue(r1["success"] and r2["success"])
        self.market_watch_id = r1["watch_id"]
        ok, _, token = self.db.claim_market_watch_delivery(
            delivery_key="MW:1", watch_id=r1["watch_id"], chat_id="1001", symbol="KOSPI", market="KR",
            session_id="S1", trade_date="2026-11-16", trigger_side="UP", observed_value=BIG_DECIMAL,
            observed_change="NaN", quote_as_of="2026-11-16T10:00:00+09:00", lease_owner="test",
        )
        self.assertTrue(ok)
        self.db.finalize_market_watch_delivery("MW:1", token, "SENT", provider_msg_id="1")
        self.db.claim_market_watch_delivery(
            delivery_key="MW:2", watch_id=r2["watch_id"], chat_id="2002", symbol="USDKRW", market="FX",
            session_id="S1", trade_date="2026-11-16", trigger_side="UP", observed_value="1400.5",
            observed_change="1.25", quote_as_of=None, lease_owner="test",
        )

        # --- travel ---------------------------------------------------------------------------------
        now = _utc()
        self._sql(
            "INSERT INTO travel_price_watches (chat_id, dest_code, target_price_krw, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            ("3003", "NRT", 250000, now, now),
        )
        self._sql(
            "INSERT INTO travel_price_watches (chat_id, dest_code, target_price_krw, is_active, created_at, updated_at) "
            "VALUES (?, ?, ?, 1, ?, ?)",
            ("4004", "BKK", 300000, now, now),
        )
        conn = sqlite3.connect(str(self.db_path))
        self.travel_watch_id = conn.execute("SELECT watch_id FROM travel_price_watches WHERE chat_id='3003'").fetchone()[0]
        conn.close()
        self._sql("INSERT INTO recipients (chat_id, name) VALUES (?, ?)", ("3003", "  "))  # blank -> chat_id fallback
        self._sql(
            "INSERT INTO travel_watch_deliveries (delivery_key, watch_id, chat_id, dest_code, dep_date, ret_date, "
            "price_krw, status, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("TW:1", self.travel_watch_id, "3003", "NRT", "2026-12-01", "2026-12-04", 1234567, "SENT", _utc(1 / 3600), now),
        )
        self._sql(
            "INSERT INTO travel_watch_deliveries (delivery_key, watch_id, chat_id, dest_code, dep_date, ret_date, "
            "price_krw, status, created_at, updated_at, delivery_kind) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'DAILY')",
            ("TW:OLD", self.travel_watch_id, "3003", "NRT", "2026-12-01", "2026-12-04", 199000, "FAILED",
             "2020-01-01 00:00:00", "2020-01-01 00:00:00"),
        )
        # Prefs: 3003 explicit (weekly OFF + paused); 4004 has no row -> defaults; 5005 explicit all-default row.
        self._sql(
            "INSERT INTO travel_user_prefs (chat_id, weekly_brief_enabled, watches_paused, updated_at) VALUES (?, 0, 1, ?)",
            ("3003", now),
        )
        self._sql(
            "INSERT INTO travel_user_prefs (chat_id, weekly_brief_enabled, watches_paused, updated_at) VALUES (?, 1, 0, ?)",
            ("5005", now),
        )

    def test_status_counts(self):
        st = self.client.get("/api/status").json()
        self.assertEqual(st["active_market_watches_count"], 2)
        self.assertEqual(st["recent_market_watch_deliveries_24h"], 2)
        tr = self.client.get("/api/travel/status")
        if tr.status_code == 404:
            self.skipTest("travel module not importable")
        body = tr.json()
        self.assertEqual(body["active_watches_count"], 2)
        self.assertEqual(body["paused_users_count"], 1)
        self.assertEqual(body["weekly_disabled_count"], 1)  # only explicit weekly_brief_enabled=0 rows
        self.assertEqual(body["recent_watch_deliveries_24h"], 1)  # 2020 row excluded (UTC rolling 24h)

    def test_custom_deliveries_decimal_and_fallbacks(self):
        res = self.client.get("/api/platform/custom-deliveries").json()
        self.assertFalse(res["has_more"])
        by_key = {i["delivery_key"]: i for i in res["items"]}
        self.assertEqual(set(by_key), {"MW:1", "MW:2", "TW:1", "TW:OLD"})
        self.assertEqual(res["items"][-1]["delivery_key"], "TW:OLD")  # newest-first

        mw1 = by_key["MW:1"]
        self.assertEqual(mw1["bot_id"], "brief_bot")
        self.assertEqual(mw1["delivery_type"], "CUSTOM_WATCH")
        self.assertEqual(mw1["observed_value"], BIG_DECIMAL)  # exact Decimal text, no float rounding
        self.assertEqual(mw1["observed_change"], "NaN")  # non-finite preserved raw
        self.assertEqual(mw1["threshold_value"], "3.10")
        self.assertEqual(mw1["display_name"], f"홍길동{XSS}")  # raw in JSON; UI escapes
        self.assertEqual(by_key["MW:2"]["display_name"], "2002")  # no user / recipient -> chat_id
        self.assertEqual(by_key["MW:2"]["status"], "IN_FLIGHT")

        tw1 = by_key["TW:1"]
        self.assertEqual(tw1["bot_id"], "travel_bot")
        self.assertEqual(tw1["delivery_type"], "CUSTOM_WATCH")
        self.assertEqual(tw1["delivery_kind"], "DROP")  # column default (COALESCE guards legacy NULLs)
        self.assertEqual(tw1["price_text"], "1,234,567원")
        self.assertEqual(tw1["display_name"], "3003")  # blank recipient name -> chat_id
        self.assertEqual(by_key["TW:OLD"]["delivery_kind"], "DAILY")

        only_travel = self.client.get("/api/platform/custom-deliveries?bot_id=travel_bot&status=FAILED").json()
        self.assertEqual([i["delivery_key"] for i in only_travel["items"]], ["TW:OLD"])
        only_market = self.client.get("/api/platform/custom-deliveries?bot_id=brief_bot").json()
        self.assertTrue(all(i["bot_id"] == "brief_bot" for i in only_market["items"]))

        paged = self.client.get("/api/platform/custom-deliveries?limit=2").json()
        self.assertEqual(len(paged["items"]), 2)
        self.assertTrue(paged["has_more"])

    def test_watches_and_prefs_defaults(self):
        res = self.client.get("/api/platform/watches").json()
        bots = sorted(i["bot_id"] for i in res["items"])
        self.assertEqual(bots, ["brief_bot", "brief_bot", "travel_bot", "travel_bot"])
        tiny = next(i for i in res["items"] if i["bot_id"] == "brief_bot" and i["chat_id"] == "2002")
        self.assertEqual(tiny["threshold_value"], "0.000000000000000001")
        nrt = next(i for i in res["items"] if i["target"] == "NRT")
        self.assertEqual(nrt["target_price_text"], "250,000원")

        prefs = {p["chat_id"]: p for p in res["user_prefs"]}
        self.assertEqual(set(prefs), {"3003", "4004", "5005"})
        self.assertFalse(prefs["3003"]["weekly_brief_enabled"])
        self.assertTrue(prefs["3003"]["watches_paused"])
        self.assertTrue(prefs["4004"]["weekly_brief_enabled"])  # no row == defaults
        self.assertFalse(prefs["4004"]["watches_paused"])
        self.assertTrue(prefs["4004"]["is_default"])
        self.assertFalse(prefs["5005"]["is_default"])

        brief_only = self.client.get("/api/platform/watches?bot_id=brief_bot").json()
        self.assertEqual(brief_only["user_prefs"], [])
        self.assertTrue(all(i["bot_id"] == "brief_bot" for i in brief_only["items"]))

    def test_deactivate_requires_token_when_configured_and_audits(self):
        with patch.dict(os.environ, {"ADMIN_API_TOKEN": "s3cret"}):
            url = f"/api/platform/watches/brief_bot/{self.market_watch_id}/deactivate"
            self.assertEqual(self.client.post(url).status_code, 401)
            self.assertEqual(self.client.get("/api/platform/watches").status_code, 401)
            self.assertEqual(self.client.get("/api/platform/custom-deliveries").status_code, 401)
            self.assertTrue(self.db.get_market_watch(self.market_watch_id)["is_active"])

            hdr = {"X-Admin-Token": "s3cret"}
            res = self.client.post(url, headers=hdr)
            self.assertEqual(res.status_code, 200)
            self.assertTrue(res.json()["deactivated"])
            row = self.db.get_market_watch(self.market_watch_id)
            self.assertEqual(row["is_active"], 0)
            self.assertEqual(row["deactivated_reason"], "ADMIN_DISABLED")

            again = self.client.post(url, headers=hdr)
            self.assertEqual(again.status_code, 200)
            self.assertTrue(again.json()["already_inactive"])

            logs = self.client.get(
                "/api/platform/audit-logs?action=WATCH_ADMIN_DEACTIVATED", headers=hdr
            ).json()["logs"]
            self.assertEqual(len(logs), 1)  # idempotent repeat does not double-log
            self.assertEqual(logs[0]["telegram_user_id"], "1001")
            self.assertEqual(logs[0]["actor"], "ADMIN")

            t_url = f"/api/platform/watches/travel_bot/{self.travel_watch_id}/deactivate"
            self.assertEqual(self.client.post(t_url, headers=hdr).status_code, 200)
            self.assertEqual(self.client.post("/api/platform/watches/travel_bot/99999/deactivate", headers=hdr).status_code, 404)
            active = self.client.get("/api/platform/watches?bot_id=travel_bot", headers=hdr).json()["items"]
            self.assertEqual([i["target"] for i in active], ["BKK"])
            st = self.client.get("/api/status").json()  # public endpoint stays public
            self.assertEqual(st["active_market_watches_count"], 1)


class AdminUiXssTests(unittest.TestCase):
    """The dashboard is a static shell; user data is rendered client-side. Guard the new render code paths."""

    @classmethod
    def setUpClass(cls):
        from market_brief.admin_ui import ADMIN_DASHBOARD_HTML

        cls.html = ADMIN_DASHBOARD_HTML
        start = cls.html.index("const WATCH_BOT_LABELS")
        end = cls.html.index("(function bindCustomDispatchUi()")
        cls.js = cls.html[start:end]
        s2 = cls.html.index('<div id="subpane-custom-dispatch"')
        e2 = cls.html.index("</section>", s2)
        cls.markup = cls.html[s2:e2]

    def test_markup_present_and_touch_targets(self):
        self.assertIn('data-subpane="custom-dispatch"', self.html)
        self.assertIn("🎯 맞춤 발송 이력", self.html)
        self.assertIn("맞춤 감시·선호 현황", self.markup)
        self.assertIn("#pane-history .hist-subtabs { grid-template-columns: repeat(3, minmax(0, 1fr)); }", self.html)
        self.assertIn("#pane-history .hist-subtab-btn { min-width: 0; min-height: 44px; }", self.html)
        self.assertIn(".watch-card-actions .btn { min-height: 44px; min-width: 44px; }", self.html)

    def test_no_inline_handlers_in_new_ui(self):
        self.assertNotIn("onclick=", self.markup)
        self.assertNotIn("onchange=", self.markup)
        self.assertNotIn("onclick", self.js)
        self.assertIn("addEventListener('click'", self.html)
        self.assertIn('data-watch-id="${esc(w.watch_id)}"', self.js)
        self.assertIn('data-bot-id="${esc(w.bot_id)}"', self.js)

    def _extract_fn(self, name: str) -> str:
        start = self.html.index(f"function {name}(")
        end = self.html.index("\n    }\n", start) + len("\n    }\n")
        return self.html[start:end]

    def test_xss_payload_escaped_when_rendered(self):
        """Executes the real dashboard render functions in Node with a DOM stub and an XSS-laden API payload."""
        import json
        import shutil
        import subprocess

        node = shutil.which("node")
        if not node:
            self.skipTest("node not available")
        helpers = "\n".join(self._extract_fn(n) for n in ("esc", "utcToKst", "deliveryStatusBadge"))
        delivery = {
            "bot_id": "brief_bot", "delivery_kind": "VOLATILITY", "delivery_key": XSS, "watch_id": 1,
            "chat_id": XSS, "display_name": XSS, "target": XSS, "trigger_side": XSS, "value_text": XSS,
            "status": XSS, "error_detail": XSS, "created_at": XSS,
        }
        travel_delivery = dict(delivery, bot_id="travel_bot", delivery_kind=XSS, dep_date=XSS, ret_date=XSS)
        watch = {
            "bot_id": "brief_bot", "watch_id": XSS, "chat_id": XSS, "display_name": XSS, "target": XSS,
            "condition_type": XSS, "direction": XSS, "threshold_value": XSS, "threshold_unit": XSS,
            "last_observed_value": XSS, "created_at": XSS,
        }
        twatch = dict(watch, bot_id="travel_bot", target_price_text=XSS, last_seen_price_text=XSS,
                      watch_mode=XSS, notify_mode=XSS)
        pref = {"chat_id": XSS, "display_name": XSS, "weekly_brief_enabled": False, "watches_paused": True,
                "is_default": False, "updated_at": XSS}
        harness = f"""
const els = {{}};
const document = {{ getElementById: id => (els[id] = els[id] || {{ id, innerHTML: '', textContent: '', value: '' }}) }};
document.getElementById('customDispatchBotSel').value = 'all';
const API = {{
  '/api/platform/custom-deliveries': {{ items: {json.dumps([delivery, travel_delivery])}, limit: 50, has_more: true }},
  '/api/platform/watches': {{ items: {json.dumps([watch, twatch])}, user_prefs: {json.dumps([pref])} }},
}};
async function req(url) {{ return API[url.split('?')[0]]; }}
{helpers}
{self.js}
(async () => {{
  await loadCustomDispatchHistory();
  await loadWatchPanel();
  process.stdout.write(JSON.stringify(Object.fromEntries(Object.entries(els).map(([k, v]) => [k, v.innerHTML]))));
}})().catch(e => {{ console.error(e); process.exit(1); }});
"""
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "render.js"
            script.write_text(harness, encoding="utf-8")
            proc = subprocess.run([node, str(script)], capture_output=True, text=True, encoding="utf-8", timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        rendered = json.loads(proc.stdout)
        for el_id in ("customDispatchBody", "watchList", "watchPrefsList", "watchStats"):
            with self.subTest(el=el_id):
                html = rendered[el_id]
                self.assertTrue(html.strip())
                self.assertNotIn("<img", html)
                self.assertNotIn("onerror=alert(1)>\"", html)
                self.assertNotIn("\"'", html)
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;&quot;&#39;", rendered["customDispatchBody"])
        self.assertIn("&lt;img src=x onerror=alert(1)&gt;&quot;&#39;", rendered["watchList"])
        self.assertIn('data-watch-id="&lt;img', rendered["watchList"])
        self.assertIn("정기 OFF", rendered["watchPrefsList"])
        self.assertIn("특가 일시중지", rendered["watchPrefsList"])

    def test_numbers_not_reparsed_in_ui(self):
        self.assertNotIn("Number(it.", self.js)
        self.assertNotIn("Number(w.", self.js)
        self.assertNotIn("parseFloat", self.js)

    def test_esc_semantics_neutralize_payload(self):
        # Python mirror of the dashboard esc() (asserted verbatim below) applied to the XSS fixture.
        self.assertIn(
            "return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')"
            ".replace(/\"/g, '&quot;').replace(/'/g, '&#39;');",
            self.html,
        )
        out = XSS.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;")
        self.assertNotIn("<", out)
        self.assertNotIn('"', out)
        self.assertNotIn("'", out)


if __name__ == "__main__":
    unittest.main()
