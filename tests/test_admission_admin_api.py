"""Integration tests for Admission Briefing Admin API routes in admin_server.py."""
import unittest
import os
import gc
from pathlib import Path
from unittest.mock import patch
from fastapi.testclient import TestClient
from admin_server import create_admin_app


class AdmissionAdminApiTests(unittest.TestCase):
    def setUp(self):
        self.db_path = str(Path.cwd() / "admission_admin_test.db")
        db_file = Path(self.db_path)
        if db_file.exists():
            db_file.unlink()
        self.token_patcher = patch.dict(os.environ, {"ADMIN_API_TOKEN": "admission-test-token"})
        self.token_patcher.start()
        self.app = create_admin_app(db_path=self.db_path, enable_bot_workers=False)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.token_patcher.stop()
        self.client.close()
        del self.app
        gc.collect()
        db_file = Path(self.db_path)
        if db_file.exists():
            db_file.unlink()

    def auth_headers(self):
        return {"X-Admin-Token": "admission-test-token"}

    def test_admission_auth_is_fail_closed_when_token_unset(self):
        with patch.dict(os.environ, {"ADMIN_API_TOKEN": ""}):
            res = self.client.get("/api/admission/status")
        self.assertEqual(res.status_code, 503)

    def test_admission_auth_rejects_bad_token(self):
        res = self.client.get("/api/admission/status", headers={"X-Admin-Token": "wrong"})
        self.assertEqual(res.status_code, 401)

    def test_admission_status_endpoint(self):
        res = self.client.get("/api/admission/status", headers=self.auth_headers())
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "ONLINE")
        self.assertIn("grade_breakdown", data)
        self.assertIn("region_breakdown", data)

    def test_admission_subscribers_endpoint(self):
        res = self.client.get("/api/admission/subscribers", headers=self.auth_headers())
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("subscribers", data)

    def test_admission_events_crud(self):
        # 1. Create event
        payload = {
            "title": "대치동 2028 수능 개편 심층 세미나",
            "organizer": "입시전략연구소",
            "target_grade": "MIDDLE",
            "target_region": "SEOUL",
            "is_online_live": True,
            "event_type": "BRIEFING_RECAP",
            "event_date_str": "2026-11-25 19:30",
            "booking_url": "https://example.com/seminar",
            "is_first_come": True,
            "summary_3lines": ["요약1", "요약2", "요약3"],
            "so_what": "고교 선택이 중요합니다.",
            "action_todos": ["학교알리미 비교"],
        }
        create_res = self.client.post("/api/admission/events", json=payload, headers=self.auth_headers())
        self.assertEqual(create_res.status_code, 200)
        created_event = create_res.json()["event"]
        self.assertEqual(created_event["title"], "대치동 2028 수능 개편 심층 세미나")
        self.assertTrue(created_event["is_online_live"])

        # 2. List events
        list_res = self.client.get("/api/admission/events", headers=self.auth_headers())
        self.assertEqual(list_res.status_code, 200)
        events = list_res.json()["events"]
        titles = [e["title"] for e in events]
        self.assertIn("대치동 2028 수능 개편 심층 세미나", titles)

    def test_admission_collect_endpoint(self):
        # Never rely on live internet: stub the live collector result.
        from admission_brief.collector import AdmissionCollector
        fake = {"sen_count": 2, "moe_count": 1, "public_count": 3, "total_saved": 3,
                "errors": ["moe_294: URLError: timeout"]}
        with patch.object(AdmissionCollector, "run_collection", return_value=fake) as run_mock:
            res = self.client.post("/api/admission/collect", headers=self.auth_headers())
        self.assertEqual(run_mock.call_count, 1)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertEqual(data["total_saved"], 3)
        self.assertEqual(data["sen_count"], 2)
        self.assertEqual(data["moe_count"], 1)
        self.assertEqual(data["errors"], ["moe_294: URLError: timeout"])

    def test_admission_dispatch_dry_run_endpoint(self):
        payload = {
            "target_grade": "MIDDLE",
            "target_region": "ALL",
            "dry_run": True,
            "silent": True,
        }
        res = self.client.post("/api/admission/dispatch", json=payload, headers=self.auth_headers())
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertTrue(data["dry_run"])
        self.assertIn("formatted_html", data)
        self.assertTrue(data["briefing"]["silent"])

    def test_admission_live_dispatch_requires_confirmation(self):
        res = self.client.post(
            "/api/admission/dispatch",
            json={"target_grade": "MIDDLE", "dry_run": False},
            headers=self.auth_headers(),
        )
        self.assertEqual(res.status_code, 400)

    def test_github_sync_status_all(self):
        res = self.client.get("/api/recipients/github-sync-status-all")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        if data.get("github_connected"):
            self.assertIn("bots", data)
            self.assertIn("finance", data["bots"])
            self.assertIn("travel", data["bots"])
            self.assertIn("admission", data["bots"])
            self.assertEqual(data["bots"]["admission"]["var_name"], "EDU_TELEGRAM_CHAT_ID")
            self.assertEqual(data["bots"]["travel"]["var_name"], "TRAVEL_TELEGRAM_CHAT_ID")
            self.assertEqual(data["bots"]["finance"]["var_name"], "TELEGRAM_CHAT_ID")


if __name__ == "__main__":
    unittest.main()
