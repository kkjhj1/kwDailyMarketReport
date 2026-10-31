"""Integration tests for Admission Briefing Admin API routes in admin_server.py."""
import unittest
from fastapi.testclient import TestClient
from admin_server import create_admin_app


class AdmissionAdminApiTests(unittest.TestCase):
    def setUp(self):
        self.app = create_admin_app(enable_bot_workers=False)
        self.client = TestClient(self.app)

    def test_admission_status_endpoint(self):
        res = self.client.get("/api/admission/status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "ONLINE")
        self.assertIn("grade_breakdown", data)
        self.assertIn("region_breakdown", data)

    def test_admission_subscribers_endpoint(self):
        res = self.client.get("/api/admission/subscribers")
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
        create_res = self.client.post("/api/admission/events", json=payload)
        self.assertEqual(create_res.status_code, 200)
        created_event = create_res.json()["event"]
        self.assertEqual(created_event["title"], "대치동 2028 수능 개편 심층 세미나")
        self.assertTrue(created_event["is_online_live"])

        # 2. List events
        list_res = self.client.get("/api/admission/events")
        self.assertEqual(list_res.status_code, 200)
        events = list_res.json()["events"]
        titles = [e["title"] for e in events]
        self.assertIn("대치동 2028 수능 개편 심층 세미나", titles)

    def test_admission_collect_endpoint(self):
        res = self.client.post("/api/admission/collect")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertGreaterEqual(data["total_saved"], 1)

    def test_admission_dispatch_dry_run_endpoint(self):
        payload = {
            "target_grade": "MIDDLE",
            "target_region": "ALL",
            "dry_run": True,
            "silent": True,
        }
        res = self.client.post("/api/admission/dispatch", json=payload)
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertTrue(data["success"])
        self.assertTrue(data["dry_run"])
        self.assertIn("formatted_html", data)
        self.assertTrue(data["briefing"]["silent"])

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
