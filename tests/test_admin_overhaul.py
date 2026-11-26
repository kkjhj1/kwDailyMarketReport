from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from admin_server import create_admin_app
from market_brief.storage.db import MarketBriefDatabase


class UserBotContextDatabaseTests(unittest.TestCase):
    def setUp(self):
        fd, name = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        self.db_path = Path(name)
        self.root = self.db_path.parent
        self.db = MarketBriefDatabase(str(self.db_path))

    def tearDown(self):
        for name in ("kids_compass.sqlite3", "yolo_brief.sqlite3", "silver_care.sqlite3"):
            (self.root / name).unlink(missing_ok=True)
        self.db_path.unlink(missing_ok=True)

    def test_missing_sidecars_are_empty_without_file_creation(self):
        context = self.db.get_user_bot_context("missing-user")
        self.assertEqual(context["kids_bot"]["available"], False)
        self.assertEqual(context["yolo_bot"]["available"], False)
        self.assertEqual(context["silver_bot"]["available"], False)
        for name in ("kids_compass.sqlite3", "yolo_brief.sqlite3", "silver_care.sqlite3"):
            self.assertFalse((self.root / name).exists())

    def test_memory_database_skips_sidecar_lookup(self):
        db = MarketBriefDatabase(":memory:")
        with patch.object(db, "_open_readonly_sidecar", side_effect=AssertionError("sidecar lookup")):
            context = db.get_user_bot_context("memory-user")
        self.assertFalse(context["kids_bot"]["available"])

    def test_wal_sidecar_reads_committed_rows_while_writer_is_open(self):
        sidecar = self.root / "yolo_brief.sqlite3"
        writer = sqlite3.connect(sidecar)
        writer.execute("PRAGMA journal_mode=WAL")
        writer.execute("CREATE TABLE yolo_users (chat_id TEXT PRIMARY KEY, profile_state TEXT, archetype TEXT)")
        writer.execute("INSERT INTO yolo_users VALUES ('wal-user', 'ACTIVE', 'URBAN_HIPSTER')")
        writer.commit()
        writer.execute("BEGIN")
        writer.execute("UPDATE yolo_users SET archetype='ROAD_NOMAD' WHERE chat_id='wal-user'")
        context = self.db.get_user_bot_context("wal-user")
        self.assertEqual(context["yolo_bot"]["profile"]["archetype"], "URBAN_HIPSTER")
        writer.rollback()
        writer.close()

    def test_kids_active_dose_timer_count(self):
        sidecar = self.root / "kids_compass.sqlite3"
        conn = sqlite3.connect(sidecar)
        conn.executescript(
            """
            CREATE TABLE kids_user_profiles (telegram_user_id TEXT PRIMARY KEY, default_region TEXT);
            CREATE TABLE kids_dose_timers (telegram_user_id TEXT, status TEXT);
            INSERT INTO kids_user_profiles VALUES ('kid-user', '서울');
            INSERT INTO kids_dose_timers VALUES ('kid-user', 'PENDING');
            INSERT INTO kids_dose_timers VALUES ('kid-user', 'IN_FLIGHT');
            INSERT INTO kids_dose_timers VALUES ('kid-user', 'SENT');
            INSERT INTO kids_dose_timers VALUES ('other', 'PENDING');
            """
        )
        conn.commit()
        conn.close()
        self.assertEqual(self.db.get_user_bot_context("kid-user")["kids_bot"]["active_dose_timers"], 2)

    def test_travel_prefs_defaults_without_row(self):
        with self.db._connect() as conn:
            conn.execute(
                "CREATE TABLE travel_user_prefs (chat_id TEXT PRIMARY KEY, weekly_brief_enabled INTEGER, watches_paused INTEGER)"
            )
        prefs = self.db.get_user_bot_context("travel-user")["travel_bot"]["prefs"]
        self.assertEqual(prefs, {"weekly_enabled": True, "drop_paused": False})


class AdminOverhaulApiTests(unittest.TestCase):
    def setUp(self):
        fd, name = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        self.db_path = Path(name)
        self.app = create_admin_app(db_path=str(self.db_path), env_path=self.db_path.with_name("empty.env"))
        self.client = TestClient(self.app)

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)

    def test_bot_visibility_is_additive(self):
        response = self.client.get("/api/platform/bots")
        self.assertEqual(response.status_code, 200)
        by_id = {bot["bot_id"]: bot for bot in response.json()["bots"]}
        public_ids = {"brief_bot", "travel_bot", "kids_bot", "admission_bot", "chungyak_bot"}
        wip_ids = {"silver_bot", "yolo_bot"}
        for bot_id in public_ids:
            self.assertIn(bot_id, by_id)
            self.assertEqual(by_id[bot_id]["visibility"], "PUBLIC")
        for bot_id in wip_ids:
            self.assertIn(bot_id, by_id)
            self.assertEqual(by_id[bot_id]["visibility"], "WIP")
        self.assertEqual({b for b, v in by_id.items() if v["visibility"] == "PUBLIC"}, public_ids)
        self.assertEqual({b for b, v in by_id.items() if v["visibility"] == "WIP"}, wip_ids)
        self.assertEqual(by_id["menu_bot"]["visibility"], "CUSTOM")
        self.assertIn("name", by_id["brief_bot"])

    def test_user_context_route_auth_and_success(self):
        with patch.dict(os.environ, {"ADMIN_API_TOKEN": "context-secret"}):
            self.assertEqual(self.client.get("/api/platform/users/context-user/bot-context").status_code, 401)
            self.assertEqual(
                self.client.get(
                    "/api/platform/users/context-user/bot-context", headers={"X-Admin-Token": "wrong"}
                ).status_code,
                401,
            )
            response = self.client.get(
                "/api/platform/users/context-user/bot-context", headers={"X-Admin-Token": "context-secret"}
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["user_id"], "context-user")
        self.assertIn("kids_bot", response.json())

    def test_admin_ui_static_contract(self):
        html = self.client.get("/").text
        self.assertNotIn("github-sync-status", html)
        self.assertNotIn("GitHub Actions 크론", html)
        for marker in (
            "user-bot-ctx-box",
            "data-bot-visibility",
            "min(280px, 100%)",
            ".drawer-actions .btn",
            "min-height: 44px",
            "addEventListener",
            "data-ctx-deactivate-bot",
        ):
            self.assertIn(marker, html)
        self.assertIn("visibility", html)


if __name__ == "__main__":
    unittest.main()
