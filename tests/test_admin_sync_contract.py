"""Contract: every bot DB table is either surfaced in the Admin Console or explicitly exempted ([ADMIN-SYNC])."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

from market_brief.admin_sync_registry import (
    ADMIN_USER_CONTEXT_REGISTRY,
    ADMIN_EXEMPT_TABLES,
    ADMIN_SYNC_REGISTRY,
    SCHEMA_SOURCES,
    all_registered_tables,
)

ROOT = Path(__file__).resolve().parents[1]
_CREATE_RE = re.compile(r"CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS\s+([A-Za-z_][A-Za-z0-9_]*)", re.IGNORECASE)

MUST_BE_COVERED = (
    "market_volatility_watches",
    "market_watch_deliveries",
    "travel_price_watches",
    "travel_watch_deliveries",
    "travel_user_prefs",
    "chungyak_notices",
    "chungyak_user_profiles",
    "chungyak_regular_subs",
    "chungyak_deliveries",
)
KIDS_CATALOG_SCHEMA_SOURCE = "kids_compass/storage/db.py"

CHUNGYAK_TABLES = MUST_BE_COVERED[-4:]


def _schema_tables() -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for rel in (*SCHEMA_SOURCES, KIDS_CATALOG_SCHEMA_SOURCE):
        text = (ROOT / rel).read_text(encoding="utf-8")
        tables = set(_CREATE_RE.findall(text))
        # Kids Compass has many user-state tables that are intentionally outside
        # the legacy Market/Travel admin contract. Part B adds only the shared
        # content catalogue to this contract; its Admin coverage is registered
        # in ADMIN_SYNC_REGISTRY above.
        found[rel] = tables if rel != KIDS_CATALOG_SCHEMA_SOURCE else {"kids_content_catalog"} & tables
    return found


class AdminSyncContractTests(unittest.TestCase):
    def test_user_context_registry_covers_new_bot_tables(self):
        expected = {
            "admission_subscribers",
            "kids_user_profiles",
            "kids_children",
            "kids_pregnancy_profiles",
            "kids_dose_timers",
            "yolo_users",
            "silver_parents",
        }
        self.assertEqual(expected, set(ADMIN_USER_CONTEXT_REGISTRY))
        for table, meta in ADMIN_USER_CONTEXT_REGISTRY.items():
            with self.subTest(table=table):
                self.assertTrue(str(meta.get("bot_id", "")).strip())
                self.assertTrue(str(meta.get("db_file", "")).strip())
                self.assertEqual(meta.get("admin_api"), ["GET /api/platform/users/{user_id}/bot-context"])
                self.assertEqual(meta.get("db_reader"), "MarketBriefDatabase.get_user_bot_context")
                self.assertTrue(str(meta.get("description", "")).strip())

    def test_schema_sources_parse_non_empty(self):
        per_file = _schema_tables()
        for rel, tables in per_file.items():
            self.assertTrue(tables, f"{rel}: CREATE TABLE 선언을 하나도 찾지 못했습니다.")

    def test_every_schema_table_is_registered_or_exempt(self):
        registered = all_registered_tables()
        missing = sorted(
            f"{rel}:{t}" for rel, tables in _schema_tables().items() for t in tables if t not in registered
        )
        self.assertEqual(
            missing,
            [],
            "Admin 미커버 테이블 발견 — market_brief/admin_sync_registry.py의 ADMIN_SYNC_REGISTRY 또는 "
            "ADMIN_EXEMPT_TABLES(사유 필수)에 등록하세요 (.agents/rules/admin-sync.md).",
        )

    def test_every_registered_table_exists_in_schemas(self):
        all_tables = set().union(*_schema_tables().values())
        stale = sorted(t for t in all_registered_tables() if t not in all_tables)
        self.assertEqual(stale, [], "레지스트리에 실제 스키마에 없는 테이블이 등록되어 있습니다.")

    def test_registry_and_exemptions_are_disjoint(self):
        self.assertEqual(sorted(set(ADMIN_SYNC_REGISTRY) & set(ADMIN_EXEMPT_TABLES)), [])

    def test_registry_entries_are_described(self):
        for table, meta in ADMIN_SYNC_REGISTRY.items():
            with self.subTest(table=table):
                self.assertTrue(str(meta.get("description", "")).strip(), "description 누락")
                self.assertTrue(str(meta.get("db_reader", "")).strip(), "db_reader 누락")
                apis = meta.get("admin_api")
                self.assertIsInstance(apis, list)
                self.assertTrue(apis and all(str(a).strip() for a in apis), "admin_api 누락")

    def test_exemptions_have_reasons(self):
        for table, reason in ADMIN_EXEMPT_TABLES.items():
            with self.subTest(table=table):
                self.assertIsInstance(reason, str)
                self.assertGreaterEqual(len(reason.strip()), 10, "예외 사유가 비어 있거나 너무 짧습니다.")

    def test_custom_watch_tables_are_covered_not_exempt(self):
        for table in MUST_BE_COVERED:
            with self.subTest(table=table):
                self.assertIn(table, ADMIN_SYNC_REGISTRY)
                self.assertNotIn(table, ADMIN_EXEMPT_TABLES)

    def test_chungyak_tables_registered_from_sidecar_schema(self):
        self.assertIn("chungyak_brief/storage/db.py", SCHEMA_SOURCES)
        self.assertEqual(_schema_tables()["chungyak_brief/storage/db.py"], set(CHUNGYAK_TABLES))
        for table in CHUNGYAK_TABLES:
            with self.subTest(table=table):
                meta = ADMIN_SYNC_REGISTRY[table]
                self.assertEqual(meta["bot_id"], "chungyak_bot")
                self.assertEqual(meta["db_file"], "chungyak_brief.sqlite3")
                self.assertTrue(all(a.startswith("GET /api/chungyak/") for a in meta["admin_api"]))
                self.assertTrue(meta["db_reader"].startswith("MarketBriefDatabase.list_chungyak_"))
                from market_brief.storage.db import MarketBriefDatabase

                self.assertTrue(callable(getattr(MarketBriefDatabase, meta["db_reader"].split(".", 1)[1], None)))

    def test_chungyak_pii_endpoint_is_fail_closed(self):
        import os
        import tempfile
        from unittest.mock import patch

        from fastapi.testclient import TestClient

        from admin_server import create_admin_app
        from market_brief.admin_sync_registry import ADMIN_FAIL_CLOSED_PII_APIS

        self.assertIn("GET /api/chungyak/subscribers", ADMIN_FAIL_CLOSED_PII_APIS)
        self.assertIn("GET /api/chungyak/subscribers", ADMIN_SYNC_REGISTRY["chungyak_regular_subs"]["admin_api"])
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("ADMIN_API_TOKEN", None)
                app = create_admin_app(db_path=str(Path(tmp) / "a.sqlite3"), env_path=Path(tmp) / "empty.env")
                client = TestClient(app)
                for api in ADMIN_FAIL_CLOSED_PII_APIS:
                    path = api.split(" ", 1)[1]
                    with self.subTest(api=api, token="unset"):
                        self.assertEqual(client.get(path).status_code, 503)
                    with patch.dict(os.environ, {"ADMIN_API_TOKEN": "k3y"}):
                        with self.subTest(api=api, token="missing"):
                            self.assertEqual(client.get(path).status_code, 401)
                        with self.subTest(api=api, token="valid"):
                            res = client.get(path, headers={"X-Admin-Token": "k3y"})
                            self.assertEqual((res.status_code, res.json()), (200, []))
                # Non-PII chungyak endpoints: 200 + [] with no sidecar, and the sidecar is never created.
                for ep in ("notices", "watches", "deliveries"):
                    with self.subTest(ep=ep):
                        res = client.get(f"/api/chungyak/{ep}")
                        self.assertEqual((res.status_code, res.json()), (200, []))
                self.assertFalse((Path(tmp) / "chungyak_brief.sqlite3").exists())

    def test_registered_admin_apis_exist_on_app(self):
        import tempfile

        from admin_server import create_admin_app

        with tempfile.TemporaryDirectory() as tmp:
            app = create_admin_app(db_path=str(Path(tmp) / "c.sqlite3"), env_path=Path(tmp) / "empty.env")
            routes = {
                f"{m} {r.path}" for r in app.routes for m in (getattr(r, "methods", None) or ()) if m != "HEAD"
            }
        for table, meta in ADMIN_SYNC_REGISTRY.items():
            for api in meta["admin_api"]:
                if api.startswith("GET /api/travel/") and not any("/api/travel/" in r for r in routes):
                    continue  # travel module optional in this environment
                with self.subTest(table=table, api=api):
                    self.assertIn(api, routes)


if __name__ == "__main__":
    unittest.main()
