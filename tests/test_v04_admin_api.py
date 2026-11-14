"""Integration tests for FastAPI Admin Console Endpoints."""
import tempfile
import unittest
from pathlib import Path
from fastapi.testclient import TestClient

import os
from unittest.mock import patch
from admin_server import create_admin_app


class AdminApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp_dir.name) / "test_api.sqlite3"
        self.env_path = Path(self.tmp_dir.name) / "empty.env"
        self.app = create_admin_app(db_path=str(self.db_path), env_path=self.env_path)
        self.client = TestClient(self.app)

    def tearDown(self):
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def test_dashboard_html_rendering(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Daily Market Brief", response.text)
        self.assertIn("원클릭 즉시 발송 센터", response.text)
        self.assertIn("appSplashLoader", response.text)
        self.assertIn("splash.jpg", response.text)

        # Verify static image serving returns 200 OK
        img_resp = self.client.get("/static/splash.jpg")
        self.assertEqual(img_resp.status_code, 200)
        self.assertGreater(len(img_resp.content), 10000)

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
        self.assertIn("07:50 장전 모닝 브리핑", data["telegram_html"])

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

    def test_travel_api_endpoints(self):
        # 1. Travel status
        res_st = self.client.get("/api/travel/status")
        self.assertEqual(res_st.status_code, 200)
        data_st = res_st.json()
        self.assertEqual(data_st["status"], "ONLINE")
        self.assertEqual(data_st["bot_topic"], "TRAVEL")
        self.assertEqual(data_st["destinations_count"], 10)

        # 2. Destinations list
        res_dest = self.client.get("/api/travel/destinations")
        self.assertEqual(res_dest.status_code, 200)
        dest_list = res_dest.json()
        self.assertEqual(len(dest_list), 10)
        codes = [d["code"] for d in dest_list]
        self.assertIn("FUK", codes)
        self.assertIn("DAD", codes)

        # 3. Preview travel briefing
        res_prev = self.client.get("/api/travel/preview")
        self.assertEqual(res_prev.status_code, 200)
        prev_data = res_prev.json()
        self.assertGreater(prev_data["recommendations_count"], 0)
        self.assertIn("formatted_message", prev_data)
        self.assertIn("주말 해외여행", prev_data["formatted_message"])

        # 4. Dispatch dry-run
        res_disp = self.client.post("/api/travel/dispatch", json={"dry_run": True})
        self.assertEqual(res_disp.status_code, 200)
        self.assertTrue(res_disp.json()["success"])

    def test_admin_token_guard(self):
        import os
        from unittest.mock import patch

        with patch.dict(os.environ, {"ADMIN_API_TOKEN": "secret_token_123"}):
            # Unauthorized request without token should return 401
            res_no_token = self.client.post("/api/platform/upgrade-requests/1/approve")
            self.assertEqual(res_no_token.status_code, 401)

            # Unauthorized request with wrong token should return 401
            res_bad_token = self.client.post(
                "/api/platform/upgrade-requests/1/approve",
                headers={"X-Admin-Token": "wrong_token"},
            )
            self.assertEqual(res_bad_token.status_code, 401)

            # Authorized request with correct header token passes token check (reaches logic, returns 404 for non-existent req)
            res_ok_header = self.client.post(
                "/api/platform/upgrade-requests/999/approve",
                headers={"X-Admin-Token": "secret_token_123"},
            )
            self.assertEqual(res_ok_header.status_code, 404)

            # Authorized request with query param token also passes
            res_ok_query = self.client.post(
                "/api/platform/upgrade-requests/999/approve?admin_token=secret_token_123"
            )
            self.assertEqual(res_ok_query.status_code, 404)

    def test_sync_telegram_names_guard_and_success(self):
        import os
        from unittest.mock import patch

        # 1. Pre-seed a user with fallback name
        sub_engine = self.app.state.sub_engine
        sub_engine.upsert_user(
            telegram_user_id="77777",
            full_name="수신자_7777",
            tier="WHITE"
        )
        # Ensure brief_bot has a token for fallback path
        with sub_engine.db._connect() as conn:
            conn.execute(
                "UPDATE bots SET token = ? WHERE bot_id = ?",
                ("bot_fallback_token_999", "brief_bot")
            )

        with patch.dict(os.environ, {"ADMIN_API_TOKEN": "secret_token_123"}):
            # Unauthorized call without token returns 401
            res = self.client.post("/api/platform/users/sync-telegram-names")
            self.assertEqual(res.status_code, 401)

            # Authorized call with mock getChat and token fallback path (empty TELEGRAM_BOT_TOKEN)
            with patch("admin_server.fetch_telegram_profile_name", return_value="홍길동"):
                with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": ""}):
                    res_ok = self.client.post(
                        "/api/platform/users/sync-telegram-names",
                        headers={"X-Admin-Token": "secret_token_123"}
                    )
                    self.assertEqual(res_ok.status_code, 200)
                    data = res_ok.json()
                    self.assertTrue(data.get("success"))
                    self.assertGreaterEqual(data.get("updated", data.get("synced_count", 0)), 1)

                    # Verify database actual persistence
                    profile = sub_engine.get_user_profile("77777")
                    self.assertEqual(profile.get("full_name"), "홍길동")

    def test_user_management_ui_alias_and_no_sync_button(self):
        html = self.client.get("/").text
        self.assertIn('id="umUsername"', html)
        self.assertIn("username: uname", html)
        self.assertIn('id="userAccordionContainer"', html)

    def test_user_list_real_name_only_and_no_empty_username_bug(self):
        html = self.client.get("/").text
        # Header shows real name only (no alias / manual tag)
        self.assertIn('<span class="user-row-name">${esc(u.full_name)}</span>', html)
        self.assertNotIn("aliasHtml", html)
        self.assertNotIn('class="user-source-tag"', html)
        # Drawer: no "@없음" fallback, shows explicit placeholder instead
        self.assertNotIn("@${esc(cleanUname || '없음')}", html)
        self.assertNotIn("없음')}", html)
        self.assertIn("(사용자명 없음)", html)
        # Removed metadata / buttons / JS helpers
        self.assertNotIn("실명 출처", html)
        self.assertNotIn("프로필 실명 일괄 동기화", html)
        self.assertNotIn("triggerProfileBatchSync", html)
        self.assertNotIn("텔레그램명으로 복원", html)
        self.assertNotIn("resetUserNameToTelegram", html)

    def test_ensure_user_auto_syncs_name_unless_custom(self):
        sub_engine = self.app.state.sub_engine
        sub_engine.upsert_user(telegram_user_id="66601", full_name="옛이름", tier="WHITE")
        sub_engine.upsert_user(telegram_user_id="66602", full_name="관리자지정", tier="WHITE")
        with sub_engine.db._connect() as conn:
            conn.execute("UPDATE users SET is_name_custom = 0 WHERE telegram_user_id = '66601'")
            conn.execute("UPDATE users SET is_name_custom = 1 WHERE telegram_user_id = '66602'")
            conn.execute(
                "INSERT OR REPLACE INTO recipients (chat_id, name) VALUES ('66601', '옛이름')"
            )

        sub_engine.ensure_user("66601", full_name="  새실명  ", username="@newbie")
        sub_engine.ensure_user("66602", full_name="텔레그램명", username="")

        with sub_engine.db._connect() as conn:
            auto = conn.execute(
                "SELECT full_name, username, name_synced_at FROM users WHERE telegram_user_id = '66601'"
            ).fetchone()
            custom = conn.execute(
                "SELECT full_name, is_name_custom FROM users WHERE telegram_user_id = '66602'"
            ).fetchone()
            rec = conn.execute("SELECT name FROM recipients WHERE chat_id = '66601'").fetchone()

        self.assertEqual(auto["full_name"], "새실명")
        self.assertEqual(auto["username"], "newbie")
        self.assertTrue(auto["name_synced_at"])
        self.assertEqual(rec["name"], "새실명")
        self.assertEqual(custom["full_name"], "관리자지정")
        self.assertEqual(custom["is_name_custom"], 1)

        # Empty incoming name must never wipe an existing name
        sub_engine.ensure_user("66601", full_name="   ")
        self.assertEqual(sub_engine.get_user_profile("66601")["full_name"], "새실명")

    def test_get_job_recipients_prefers_users_full_name(self):
        sub_engine = self.app.state.sub_engine
        db = sub_engine.db
        sub_engine.upsert_user(telegram_user_id="70001", full_name="실명우선", tier="WHITE")
        with db._connect() as conn:
            conn.execute("INSERT OR REPLACE INTO recipients (chat_id, name) VALUES ('70001', '레거시명')")
            conn.execute("INSERT OR REPLACE INTO recipients (chat_id, name) VALUES ('70002', '레거시만')")
            conn.execute("INSERT OR REPLACE INTO recipients (chat_id, name) VALUES ('70003', '   ')")
            for cid in ("70001", "70002", "70003", "70004"):
                conn.execute(
                    "INSERT INTO delivery_job_recipients (job_id, chat_id, status) VALUES ('job_name_test', ?, 'SENT')",
                    (cid,),
                )

        rows = {r["chat_id"]: r for r in db.get_job_recipients("job_name_test")}
        self.assertEqual(rows["70001"]["name"], "실명우선")
        self.assertEqual(rows["70002"]["name"], "레거시만")
        self.assertEqual(rows["70003"]["name"], "수신자")
        self.assertEqual(rows["70004"]["name"], "수신자")
        self.assertEqual(rows["70001"]["channel"], "TELEGRAM")

    def test_platform_user_username_create_and_update(self):
        res_post = self.client.post(
            "/api/platform/users",
            json={"telegram_user_id": "88888", "full_name": "홍길동", "username": "kildong", "tier": "WHITE"},
        )
        self.assertEqual(res_post.status_code, 200)
        users = self.client.get("/api/platform/users").json()["users"]
        user = next(u for u in users if u["telegram_user_id"] == "88888")
        self.assertEqual(user["full_name"], "홍길동")
        self.assertEqual(user["username"], "kildong")

        res_put = self.client.put(
            "/api/platform/users/88888",
            json={"full_name": "홍길동", "username": "gildong2", "memo": ""},
        )
        self.assertEqual(res_put.status_code, 200)
        users = self.client.get("/api/platform/users").json()["users"]
        user = next(u for u in users if u["telegram_user_id"] == "88888")
        self.assertEqual(user["username"], "gildong2")

    def _audit_logs_for(self, user_id, action=None):
        sub_engine = self.app.state.sub_engine
        return sub_engine.list_audit_logs(user_id=user_id, action=action, limit=100)["logs"]

    def test_user_update_audit_diff_logging(self):
        sub_engine = self.app.state.sub_engine
        sub_engine.upsert_user(telegram_user_id="55501", full_name="수신자", tier="WHITE")

        res = self.client.put(
            "/api/platform/users/55501",
            json={"full_name": "홍길동", "username": "@kildong"},
        )
        self.assertEqual(res.status_code, 200)

        logs = self._audit_logs_for("55501", action="USER_UPDATED")
        self.assertEqual(len(logs), 1)
        changes = logs[0]["detail"]["changes"]
        self.assertEqual(changes["full_name"], {"from": "수신자", "to": "홍길동"})
        self.assertEqual(changes["username"], {"from": "", "to": "kildong"})
        self.assertNotIn("status", changes)
        self.assertNotIn("memo", changes)
        self.assertEqual(logs[0]["actor"], "ADMIN")

    def test_user_update_no_diff_skips_audit(self):
        sub_engine = self.app.state.sub_engine
        sub_engine.upsert_user(telegram_user_id="55502", full_name="홍길동", username="kildong", tier="WHITE")

        res = self.client.put(
            "/api/platform/users/55502",
            json={"full_name": "홍길동", "username": "kildong"},
        )
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self._audit_logs_for("55502", action="USER_UPDATED"), [])

        # A real change logs exactly once; repeating the same payload adds nothing.
        self.client.put("/api/platform/users/55502", json={"memo": "VIP"})
        self.client.put("/api/platform/users/55502", json={"memo": "VIP"})
        logs = self._audit_logs_for("55502", action="USER_UPDATED")
        self.assertEqual(len(logs), 1)
        self.assertEqual(logs[0]["detail"]["changes"], {"memo": {"from": "", "to": "VIP"}})

    def test_audit_logs_api(self):
        import os
        from unittest.mock import patch

        sub_engine = self.app.state.sub_engine
        sub_engine.upsert_user(telegram_user_id="55503", full_name="김철수", tier="WHITE")
        sub_engine.upsert_user(telegram_user_id="55504", full_name="이영희", tier="WHITE")
        self.client.put("/api/platform/users/55503", json={"full_name": "김철수2"})

        res = self.client.get("/api/platform/audit-logs")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("logs", data)
        self.assertGreaterEqual(data["total"], 3)

        res_user = self.client.get("/api/platform/audit-logs?user_id=55503")
        user_logs = res_user.json()["logs"]
        self.assertTrue(user_logs)
        self.assertTrue(all(l["telegram_user_id"] == "55503" for l in user_logs))

        res_action = self.client.get("/api/platform/audit-logs?action=USER_UPDATED")
        action_data = res_action.json()
        self.assertEqual(action_data["total"], 1)
        self.assertEqual(action_data["logs"][0]["telegram_user_id"], "55503")
        self.assertEqual(action_data["logs"][0]["user_name"], "김철수2")

        res_q = self.client.get("/api/platform/audit-logs?q=이영희")
        self.assertTrue(all(l["telegram_user_id"] == "55504" for l in res_q.json()["logs"]))

        res_page = self.client.get("/api/platform/audit-logs?limit=1&offset=1")
        page = res_page.json()
        self.assertEqual(len(page["logs"]), 1)
        self.assertEqual(page["total"], data["total"])

        with patch.dict(os.environ, {"ADMIN_API_TOKEN": "secret_token_123"}):
            self.assertEqual(self.client.get("/api/platform/audit-logs").status_code, 401)
            ok = self.client.get("/api/platform/audit-logs", headers={"X-Admin-Token": "secret_token_123"})
            self.assertEqual(ok.status_code, 200)

    def test_user_history_api(self):
        import os
        from unittest.mock import patch
        from market_brief.briefing_service import (
            finish_manual_broadcast_job,
            open_manual_broadcast_job,
            record_manual_broadcast_result,
        )

        sub_engine = self.app.state.sub_engine
        sub_engine.upsert_user(telegram_user_id="55505", full_name="박민수", tier="WHITE")
        self.client.put("/api/platform/users/55505", json={"full_name": "박민수2"})

        # Manual broadcast ledger entry must surface in both dispatch history and per-user history
        job_id = open_manual_broadcast_job(sub_engine, "brief_bot", ["55505", "55599"], topic="FINANCE")
        self.assertIsNotNone(job_id)
        record_manual_broadcast_result(sub_engine, job_id, "55505", "SENT", provider_msg_id="101")
        record_manual_broadcast_result(sub_engine, job_id, "55599", "FAILED", error_detail="403 blocked")
        finish_manual_broadcast_job(sub_engine, job_id, total=2, success=1, fail=1)

        res = self.client.get("/api/platform/users/55505/history")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["user_id"], "55505")
        actions = [l["action"] for l in data["audit_logs"]]
        self.assertIn("USER_UPDATED", actions)
        self.assertIn("USER_CREATED", actions)
        self.assertEqual(len(data["delivery_history"]), 1)
        h = data["delivery_history"][0]
        self.assertEqual(h["job_id"], job_id)
        self.assertEqual(h["recipient_status"], "SENT")
        self.assertEqual(h["kind"], "manual")
        self.assertEqual(h["topic"], "FINANCE")
        self.assertEqual(h["job_status"], "PARTIAL")

        jobs = self.client.get("/api/history?limit=50").json()
        job = next(j for j in jobs if j["job_id"] == job_id)
        self.assertEqual(job["kind"], "manual")
        self.assertEqual(job["success_count"], 1)
        self.assertEqual(job["fail_count"], 1)

        with patch.dict(os.environ, {"ADMIN_API_TOKEN": "secret_token_123"}):
            self.assertEqual(self.client.get("/api/platform/users/55505/history").status_code, 401)

    def test_history_center_ui_present(self):
        html = self.client.get("/").text
        self.assertIn('data-tab="history"', html)
        self.assertIn('data-bnav="history"', html)
        self.assertIn('id="pane-history"', html)
        self.assertIn('id="subpane-dispatch"', html)
        self.assertIn('id="subpane-audit"', html)
        self.assertIn("openUserHistoryModal", html)
        self.assertIn('id="userHistoryModal"', html)

    def test_pending_badge_deeplink_and_health_strip_ui_present(self):
        html = self.client.get("/").text
        self.assertIn('id="userPendingBadge"', html)
        self.assertIn('id="bnavPendingBadge"', html)
        self.assertIn('id="botHealthStrip"', html)
        self.assertIn('id="botTrafficLights"', html)
        self.assertIn('id="schedulerTimer"', html)
        self.assertIn("@keyframes pulseGold", html)
        self.assertIn("data-req-card-id", html)
        self.assertIn("applyDeepLink", html)
        self.assertIn("'/healthz'", html)
        for bot_id in ("menu_bot", "brief_bot", "travel_bot", "admission_bot", "silver_bot", "kids_bot", "yolo_bot"):
            self.assertIn(f"'{bot_id}'", html)
        # 3-item overhaul assertions
        self.assertIn('#botHealthStrip', html)
        self.assertIn('display: none !important', html)
        self.assertIn('id="userSubPaneUsers"', html)
        self.assertIn('id="userSubPaneUpgrades"', html)
        self.assertIn('id="userSubTabUpgradeBadge"', html)
        self.assertIn("switchUserSubTab", html)
        self.assertIn("📥", html)
        self.assertIn("대화 수신", html)

    def test_healthz_endpoint(self):
        """Validates that /healthz returns 200 OK for Uptime ping monitors."""
        res = self.client.get("/healthz")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data.get("status"), "HEALTHY")
        self.assertIn("active_workers", data)

    def test_six_info_bots_seeded_and_splash_badges(self):
        """Validates that all 7 bots (menu_bot + 6 INFO bots) are seeded and splash badges include all 6."""
        res = self.client.get("/api/platform/bots")
        self.assertEqual(res.status_code, 200)
        bots = res.json().get("bots", [])
        bot_ids = [b["bot_id"] for b in bots]
        for expected in ("menu_bot", "brief_bot", "travel_bot", "admission_bot", "silver_bot", "kids_bot", "yolo_bot"):
            self.assertIn(expected, bot_ids)

        by_id = {b["bot_id"]: b for b in bots}
        self.assertEqual(by_id["silver_bot"]["telegram_username"], "KwSilver_bot")
        self.assertEqual(by_id["kids_bot"]["telegram_username"], "KwKids_bot")
        self.assertEqual(by_id["yolo_bot"]["telegram_username"], "KwYolo_bot")

        html = self.client.get("/").text
        for badge in ("📈 시황", "✈️ 여행", "🎓 입시", "👵 실버", "👶 자녀", "🏄 욜로"):
            self.assertIn(badge, html)

    def test_new_bots_briefing_preview_and_token_isolation(self):
        """Validates briefing preview generation and token isolation for silver_bot, kids_bot, yolo_bot."""
        import os
        import re
        from unittest.mock import patch
        from market_brief.briefing_service import generate_bot_briefing, get_bot_presets, get_bot_token_and_chat
        from yolo_brief.channels.telegram import YoloTelegramChannel

        for bot_id in ("silver_bot", "kids_bot", "yolo_bot", "menu_bot"):
            presets = get_bot_presets(bot_id)
            self.assertGreaterEqual(len(presets), 1)
            for p in presets:
                res = generate_bot_briefing(bot_id, preset=p["id"], db=self.app.state.sub_engine)
                content = res["content_html"]
                self.assertTrue(content)
                self.assertLessEqual(len(content), 3500)
                vis = re.sub(r"<[^>]+>", "", content)
                self.assertLessEqual(len(vis), 1800)

        # Token isolation: TELEGRAM_BOT_TOKEN must NOT leak into silver/kids/yolo when their own token is empty
        with patch("market_brief.briefing_service._get_env_val") as mock_env:
            env_map = {
                "TELEGRAM_BOT_TOKEN": "market_only_token",
                "TELEGRAM_CHAT_ID": "99999",
            }
            mock_env.side_effect = lambda k, default="": env_map.get(k, default)
            for bid in ("silver_bot", "kids_bot", "yolo_bot"):
                with self.app.state.sub_engine.db._connect() as conn:
                    conn.execute("UPDATE bots SET token = '' WHERE bot_id = ?", (bid,))
                tok, _ = get_bot_token_and_chat(bid, db=self.app.state.sub_engine)
                self.assertEqual(tok, "")

        with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "market_only_token", "YOLO_TELEGRAM_BOT_TOKEN": "", "YOLO_BOT_TOKEN": ""}, clear=False):
            ch = YoloTelegramChannel(token=None, dry_run=True)
            self.assertEqual(ch.token, "")


    def test_tier_counts_in_users_api(self):
        res = self.client.get("/api/platform/users")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertIn("tier_counts", data)
        counts = data["tier_counts"]
        self.assertIn("ALL", counts)
        self.assertIn("WHITE", counts)
        self.assertIn("GREEN", counts)
        self.assertIn("RED", counts)
        self.assertIn("BLACK", counts)
        self.assertEqual(counts["ALL"], len(data["users"]))

    def test_batch_approve_upgrade_requests(self):
        sub_engine = self.app.state.sub_engine
        # 1. Create two users
        sub_engine.upsert_user("batch_u1", "배치유저1", tier="WHITE")
        sub_engine.upsert_user("batch_u2", "배치유저2", tier="WHITE")

        # 2. Insert two pending requests
        with sub_engine.db._connect() as conn:
            conn.execute(
                "INSERT INTO upgrade_requests (telegram_user_id, current_tier, requested_tier, status) VALUES (?, 'WHITE', 'GREEN', 'PENDING')",
                ("batch_u1",)
            )
            conn.execute(
                "INSERT INTO upgrade_requests (telegram_user_id, current_tier, requested_tier, status) VALUES (?, 'WHITE', 'RED', 'PENDING')",
                ("batch_u2",)
            )

        # 3. Call batch approve
        with patch.dict(os.environ, {"ADMIN_API_TOKEN": "secret_token_123"}):
            res = self.client.post(
                "/api/platform/upgrade-requests/batch-approve",
                headers={"X-Admin-Token": "secret_token_123"}
            )
            self.assertEqual(res.status_code, 200)
            data = res.json()
            self.assertTrue(data.get("success"))
            self.assertGreaterEqual(data.get("approved_count", 0), 2)

            # Verify actual tier upgrades
            p1 = sub_engine.get_user_profile("batch_u1")
            p2 = sub_engine.get_user_profile("batch_u2")
            self.assertEqual(p1["tier"], "GREEN")
            self.assertEqual(p2["tier"], "RED")

    def test_dispatch_bot_to_user_guard_and_success(self):
        sub_engine = self.app.state.sub_engine
        sub_engine.upsert_user("disp_u1", "발송유저1", tier="WHITE")
        # Subscribe to brief_bot
        sub_engine.attempt_subscribe(telegram_user_id="disp_u1", bot_id="brief_bot", actor="ADMIN")

        with patch.dict(os.environ, {"ADMIN_API_TOKEN": "secret_token_123"}):
            # Test 1: Subscribed bot dispatch
            with patch("admin_server.send_bot_test_message", return_value={"success": True, "message_id": 12345}):
                res = self.client.post(
                    "/api/platform/users/disp_u1/dispatch-bot",
                    json={"bot_id": "brief_bot"},
                    headers={"X-Admin-Token": "secret_token_123"}
                )
                self.assertEqual(res.status_code, 200)
                data = res.json()
                self.assertTrue(data.get("success"))
                self.assertEqual(data.get("bot_id"), "brief_bot")

            # Verify INDIVIDUAL_DISPATCH audit log recorded
            logs = self._audit_logs_for("disp_u1", action="INDIVIDUAL_DISPATCH")
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs[0]["bot_id"], "brief_bot")

            # Test 1b: Immediate repeat call hits cooldown (429)
            with patch("admin_server.send_bot_test_message", return_value={"success": True, "message_id": 12346}):
                res_cd = self.client.post(
                    "/api/platform/users/disp_u1/dispatch-bot",
                    json={"bot_id": "brief_bot"},
                    headers={"X-Admin-Token": "secret_token_123"}
                )
                self.assertEqual(res_cd.status_code, 429)

            # Test 2: Unsubscribed bot dispatch should fail with 400
            res_fail = self.client.post(
                "/api/platform/users/disp_u1/dispatch-bot",
                json={"bot_id": "travel_bot"},
                headers={"X-Admin-Token": "secret_token_123"}
            )
            self.assertEqual(res_fail.status_code, 400)
            self.assertIn("구독하고 있지 않습니다", res_fail.json().get("detail", ""))

            # Test 3: Missing X-Admin-Token returns 401
            res_unauth = self.client.post(
                "/api/platform/users/disp_u1/dispatch-bot",
                json={"bot_id": "brief_bot"},
            )
            self.assertEqual(res_unauth.status_code, 401)

    def test_admin_token_modal_ui_present(self):
        html = self.client.get("/").text
        self.assertIn('id="adminTokenModal"', html)
        self.assertIn("openAdminTokenModal", html)
        self.assertIn("saveAdminTokenFromModal", html)
        self.assertIn("updateAdminTokenBadge", html)
        self.assertRegex(html, r"#btnAdminToken\s*\{\s*display:\s*none\s*!important;")
        self.assertIn('id="btnAdminToken" style="display:none;" aria-hidden="true"', html)

    def test_bot_master_detail_ui_present(self):
        html = self.client.get("/").text
        self.assertIn('id="botMasterStrip"', html)
        self.assertIn('id="botDetailInspector"', html)
        self.assertIn("renderBotMasterStrip", html)
        self.assertIn("selectBot", html)
        self.assertIn("renderBotDetailInspector", html)
        self.assertIn("loadInlineBriefingPreview", html)
        self.assertIn("bot-master-tile", html)
        self.assertIn("bot-master-tile-meta", html)
        # Check compact 1/4 size bot strip labels (<= 4 chars)
        self.assertIn("label: '메뉴'", html)
        self.assertIn("label: '시황'", html)
        self.assertIn("label: '여행'", html)
        self.assertIn("label: '입시'", html)
        # Check 1-row compact KPI strip and hidden countdown
        self.assertIn("kpi-compact-strip", html)
        self.assertIn('id="schedulerTimer" style="display:none;"', html)


class ScheduleApiTests(unittest.TestCase):
    """⏰ /api/platform/schedules* with InMemoryCrontabDriver ([VERIFY-02], [PROD-LIVE])."""

    APP_DIR = "/home/ubuntu/kwDailyMarketReport"

    def setUp(self):
        from market_brief.schedule_manager import InMemoryCrontabDriver

        self.tmp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.tmp_dir.name) / "test_sched_api.sqlite3"
        env_path = Path(self.tmp_dir.name) / "empty.env"
        self.live_crontab = "\n".join([
            "SHELL=/bin/bash",
            "0 4 * * * cp -p /home/ubuntu/kwDailyMarketReport/alerts.sqlite3 /home/ubuntu/backups/",
            "# [1. 시황봇]",
            f"53 7 * * 1-5 {self.APP_DIR}/run_cron.sh market_auto",
            f"42 15 * * 1-5 {self.APP_DIR}/run_cron.sh market_auto",
            f"7,22,37,52 9-15 * * 1-5 {self.APP_DIR}/run_cron.sh market_check_urgent",
            "# [2. 여행봇]",
            f"30 7,12,18 * * * {self.APP_DIR}/run_cron.sh travel_watches",
            "",
        ])
        self.driver = InMemoryCrontabDriver(self.live_crontab, app_dir=self.APP_DIR)
        self.app = create_admin_app(db_path=str(db_path), env_path=env_path, crontab_driver=self.driver)
        self.client = TestClient(self.app)
        self.body = {"enabled": True, "times_kst": ["08:00", "13:00", "19:00"], "days_mode": "DAILY", "weekdays": []}

    def tearDown(self):
        try:
            self.tmp_dir.cleanup()
        except Exception:
            pass

    def test_get_schedules_reports_migration_required(self):
        res = self.client.get("/api/platform/schedules")
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["migration_state"], "MIGRATION_REQUIRED")
        self.assertTrue(data["db_backup_protected"])
        tasks = {t["task_id"]: t for t in data["tasks"]}
        self.assertEqual(data["tasks"][0]["task_id"], "travel_watches")
        self.assertEqual(tasks["travel_watches"]["config"]["times_kst"], ["07:30", "12:30", "18:30"])
        self.assertFalse(tasks["market_auto"]["editable"])
        self.assertEqual(self.driver.writes, [])

    def test_preview_endpoint(self):
        self.client.get("/api/platform/schedules")
        res = self.client.post("/api/platform/schedules/preview", json={"task_id": "travel_watches", **self.body})
        self.assertEqual(res.status_code, 200)
        data = res.json()
        self.assertEqual(data["status"], "OK")
        self.assertEqual(data["cron_lines"], [f"0 8,13,19 * * * {self.APP_DIR}/run_cron.sh travel_watches"])
        self.assertEqual(len(data["next_runs_kst"]), 3)
        self.assertTrue(data["diff_available"])

        bad = self.client.post("/api/platform/schedules/preview", json={"task_id": "travel_watches", **self.body, "times_kst": ["02:00"]})
        self.assertEqual(bad.status_code, 200)
        self.assertEqual(bad.json()["status"], "VALIDATION_FAILED")

        ro = self.client.post("/api/platform/schedules/preview", json={"task_id": "market_auto", **self.body})
        self.assertEqual(ro.status_code, 400)
        self.assertEqual(self.driver.writes, [])

    def test_put_read_only_validation_and_injection(self):
        self.assertEqual(self.client.put("/api/platform/schedules/market_auto", json=self.body).status_code, 400)
        self.assertEqual(self.client.put("/api/platform/schedules/market_check_urgent", json=self.body).status_code, 400)
        self.assertEqual(self.client.put("/api/platform/schedules/unknown_task", json=self.body).status_code, 404)
        night = self.client.put("/api/platform/schedules/travel_watches", json={**self.body, "times_kst": ["23:30"]})
        self.assertEqual(night.status_code, 422)
        self.assertEqual(night.json()["status"], "VALIDATION_FAILED")
        inj = self.client.put("/api/platform/schedules/travel_watches", json={**self.body, "times_kst": ["08:00; reboot"]})
        self.assertEqual(inj.status_code, 422)
        cmd = self.client.put("/api/platform/schedules/travel_watches", json={**self.body, "command": "rm -rf /"})
        self.assertEqual(cmd.status_code, 422)
        self.assertEqual(self.driver.writes, [])

    def test_put_migration_confirm_flow(self):
        res = self.client.put("/api/platform/schedules/travel_watches", json=self.body)
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["status"], "MIGRATION_REQUIRED")
        self.assertTrue(res.json()["diff"])
        self.assertEqual(self.driver.writes, [])

        ok = self.client.put("/api/platform/schedules/travel_watches", json={**self.body, "confirm_migration": True})
        self.assertEqual(ok.status_code, 200, ok.json())
        self.assertEqual(ok.json()["status"], "APPLIED")
        self.assertIn("0 4 * * * cp -p /home/ubuntu/kwDailyMarketReport/alerts.sqlite3 /home/ubuntu/backups/", self.driver.text.splitlines())
        self.assertIn(f"0 8,13,19 * * * {self.APP_DIR}/run_cron.sh travel_watches", self.driver.text)
        data = self.client.get("/api/platform/schedules").json()
        self.assertEqual(data["migration_state"], "MANAGED")
        self.assertEqual(data["sync_state"], "IN_SYNC")
        # Legacy /api/settings contract untouched
        settings = self.client.get("/api/settings").json()
        self.assertEqual(set(settings.keys()), {"thresholds", "schedules"})

    def test_conflict_and_unavailable_status_codes(self):
        from market_brief.schedule_manager import MANAGED_START

        self.driver.text = f"{MANAGED_START}\n"
        res = self.client.put("/api/platform/schedules/travel_watches", json={**self.body, "confirm_migration": True})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(res.json()["status"], "MIGRATION_CONFLICT")

        self.driver.text = ""
        self.driver.fail_write = True
        res = self.client.put("/api/platform/schedules/travel_watches", json=self.body)
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()["status"], "SYNC_FAILED")

        default_app = create_admin_app(
            db_path=str(Path(self.tmp_dir.name) / "default.sqlite3"), env_path=Path(self.tmp_dir.name) / "empty.env"
        )
        client = TestClient(default_app)
        self.assertEqual(client.get("/api/platform/schedules").json()["crontab_status"], "UNAVAILABLE")
        res = client.put("/api/platform/schedules/travel_watches", json=self.body)
        self.assertEqual(res.status_code, 503)
        self.assertEqual(res.json()["status"], "CRONTAB_UNAVAILABLE")

    def test_schedule_token_guard(self):
        with patch.dict(os.environ, {"ADMIN_API_TOKEN": "secret_token_123"}):
            self.assertEqual(self.client.get("/api/platform/schedules").status_code, 401)
            self.assertEqual(
                self.client.post("/api/platform/schedules/preview", json={"task_id": "travel_watches", **self.body}).status_code, 401
            )
            self.assertEqual(self.client.put("/api/platform/schedules/travel_watches", json=self.body).status_code, 401)
            ok = self.client.get("/api/platform/schedules", headers={"X-Admin-Token": "secret_token_123"})
            self.assertEqual(ok.status_code, 200)
        with patch.dict(os.environ, {"SCHEDULE_REQUIRE_TOKEN": "true", "ADMIN_API_TOKEN": ""}):
            res = self.client.put("/api/platform/schedules/travel_watches", json={**self.body, "confirm_migration": True})
            self.assertEqual(res.status_code, 503)
        self.assertEqual(self.driver.writes, [])

    def test_prod_live_save_executes_nothing(self):
        import market_brief.schedule_manager as sm

        with patch.object(sm.subprocess, "run", side_effect=AssertionError("subprocess forbidden")) as run_mock, \
             patch("subprocess.Popen", side_effect=AssertionError("Popen forbidden")) as popen_mock, \
             patch("os.system", side_effect=AssertionError("os.system forbidden")) as system_mock, \
             patch("admin_server.send_bot_test_message", side_effect=AssertionError("telegram forbidden")) as tg_mock, \
             patch("admin_server.SafeTelegramDispatcher", side_effect=AssertionError("dispatch forbidden")) as disp_mock:
            res = self.client.put("/api/platform/schedules/travel_watches", json={**self.body, "confirm_migration": True})
            self.assertEqual(res.status_code, 200)
            res2 = self.client.put("/api/platform/schedules/travel_watches", json={**self.body, "enabled": False})
            self.assertEqual(res2.status_code, 200)
        self.assertFalse(res.json()["executed_now"])
        for m in (run_mock, popen_mock, system_mock, tg_mock, disp_mock):
            m.assert_not_called()
        self.assertIn(f"# [DISABLED] 0 8,13,19 * * * {self.APP_DIR}/run_cron.sh travel_watches", self.driver.text)

    def test_schedule_manager_ui_present(self):
        import re

        html = self.client.get("/").text
        self.assertIn('id="scheduleManagerCard"', html)
        self.assertIn("⏰ 자동 실행 주기 관리", html)
        self.assertIn("loadSchedules", html)
        self.assertIn("loadSchedules();", html)
        self.assertIn('id="schedStatusBanner"', html)
        self.assertIn("🛡️ 04:00 DB 백업 보호됨", html)
        self.assertIn("기존 크론 편입 후 저장", html)
        self.assertIn("기본 3회 (07:30·12:30·18:30)", html)
        self.assertIn("4시간 간격", html)
        self.assertIn("2시간 간격 (7회)", html)
        self.assertIn("'일', '월', '화', '수', '목', '금', '토'", html)
        self.assertIn("READ_ONLY_DUAL_PATH", html)
        self.assertIn("/api/platform/schedules/preview", html)
        self.assertRegex(html, r"\.sched-touch \{ min-height: 44px; min-width: 44px; \}")
        self.assertRegex(html, r"\.sched-chip \{ min-height: 44px;")
        self.assertRegex(html, r"\.sched-switch \{ min-height: 44px;")
        self.assertRegex(html, r"\.sched-grid \{[^}]*minmax\(0, 1fr\)")
        # Card is placed inside #pane-bots, below #botDetailInspector
        pane = html[html.index('id="pane-bots"'):html.index('id="pane-dashboard"')]
        self.assertLess(pane.index('id="botDetailInspector"'), pane.index('id="scheduleManagerCard"'))
        self.assertIsNone(re.search(r"sched[A-Za-z]*\([^)]*run_cron", html))


if __name__ == "__main__":
    unittest.main()
