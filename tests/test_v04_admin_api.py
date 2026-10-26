"""Integration tests for FastAPI Admin Console Endpoints."""
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient

from admin_server import create_admin_app


class AdminApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_api.sqlite3"
        self.env_path = Path(self.tmp_dir.name) / "empty.env"
        self.app = create_admin_app(db_path=str(self.db_path), env_path=self.env_path)
        self.client = TestClient(self.app)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_dashboard_html_rendering(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Daily Market Brief", response.text)
        self.assertIn("원클릭 즉시 발송 센터", response.text)

    def test_status_endpoint(self):
        response = self.client.get("/api/status")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn("current_time_kst", data)
        self.assertIn("morning_status", data)
        self.assertIn("closing_status", data)

    def test_recipient_management_api(self):
        # 1. Add recipient
        payload = {
            "chat_id": "12345678",
            "name": "홍길동 팀장",
            "channel": "TELEGRAM",
            "group_tag": "VIP",
            "subscribed_topics": "FINANCE,REAL_ESTATE",
        }
        res_post = self.client.post("/api/recipients", json=payload)
        self.assertEqual(res_post.status_code, 200)
        self.assertTrue(res_post.json()["success"])

        # 2. List recipients
        res_list = self.client.get("/api/recipients")
        self.assertEqual(res_list.status_code, 200)
        items = res_list.json()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["name"], "홍길동 팀장")
        self.assertTrue(items[0]["is_active"])

        # 3. Toggle recipient inactive
        res_toggle = self.client.post("/api/recipients/12345678/toggle")
        self.assertEqual(res_toggle.status_code, 200)
        self.assertFalse(res_toggle.json()["is_active"])

        # 4. Delete recipient
        res_del = self.client.delete("/api/recipients/12345678")
        self.assertEqual(res_del.status_code, 200)
        self.assertTrue(res_del.json()["success"])
        res_list2 = self.client.get("/api/recipients")
        self.assertEqual(len(res_list2.json()), 0)

    def test_settings_api(self):
        res = self.client.get("/api/settings")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("thresholds", data)
        self.assertIn("KOSPI", data["thresholds"])

        # Update setting
        new_thresholds = data["thresholds"]
        new_thresholds["KOSPI"]["threshold"] = 2.5
        res_save = self.client.post("/api/settings", json={"thresholds": new_thresholds})
        self.assertEqual(res_save.status_code, 200)

        # Verify update persisted
        res2 = self.client.get("/api/settings")
        self.assertEqual(res2.json()["thresholds"]["KOSPI"]["threshold"], 2.5)

    def test_preview_api(self):
        res = self.client.get("/api/dispatch/preview?kind=morning")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("telegram_html", data)
        self.assertIn("06:30 장전 모닝 브리핑", data["telegram_html"])

    def test_github_sync_endpoints_without_token(self):
        # 1. Sync status without token should be graceful
        res = self.client.get("/api/recipients/github-sync-status")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertFalse(data["github_connected"])
        self.assertFalse(data["is_synced"])

        # 2. Triggering sync without token should return 400
        res_sync = self.client.post("/api/recipients/sync-github")
        self.assertEqual(res_sync.status_code, 400)


if __name__ == "__main__":
    unittest.main()
