"""Unit / migration / sync-journal tests for market_brief.schedule_manager ([VERIFY-02]: no real crontab)."""
from dataclasses import replace
from datetime import datetime
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from market_brief import schedule_manager as sm
from market_brief.schedule_manager import (
    KST,
    MANAGED_END,
    MANAGED_START,
    TASK_CATALOG,
    InMemoryCrontabDriver,
    LinuxCrontabDriver,
    ScheduleManager,
    UnavailableCrontabDriver,
    command_for,
    compile_cron_exprs,
    compile_cron_lines,
    cron_dow_to_python_weekday,
    next_runs_kst,
    normalize_payload,
    parse_crontab,
)
from market_brief.storage.db import MarketBriefDatabase

APP_DIR = "/home/ubuntu/kwDailyMarketReport"
DB_BACKUP_LINE = (
    r'0 4 * * * cp -p /home/ubuntu/kwDailyMarketReport/alerts.sqlite3 '
    r'/home/ubuntu/backups/alerts_$(date +\%Y\%m\%d).sqlite3 && '
    r'find /home/ubuntu/backups/ -name "alerts_*.sqlite3" -mtime +7 -delete'
)
LIVE_OCI_CRONTAB = "\n".join(
    [
        "SHELL=/bin/bash",
        "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "# m h  dom mon dow   command",
        DB_BACKUP_LINE,
        "",
        "# [1. 시황봇]",
        f"53 7 * * 1-5 {APP_DIR}/run_cron.sh market_auto",
        f"42 15 * * 1-5 {APP_DIR}/run_cron.sh market_auto",
        f"7,22,37,52 9-15 * * 1-5 {APP_DIR}/run_cron.sh market_check_urgent",
        "# [2. 여행봇]",
        f"0 12 * * 4 {APP_DIR}/run_cron.sh travel_weekly",
        f"30 7,12,18 * * * {APP_DIR}/run_cron.sh travel_watches",
        "# [3. 입시봇]",
        f"30 8 * * * {APP_DIR}/run_cron.sh admission_collect",
        f"0 20 * * 0 {APP_DIR}/run_cron.sh admission_weekly",
        "",
    ]
)
# Wednesday 2026-11-11 10:00 KST
FIXED_NOW = datetime(2026, 11, 11, 10, 0, tzinfo=KST)


def _payload(times, days_mode="DAILY", weekdays=None, enabled=True):
    return {"enabled": enabled, "times_kst": times, "days_mode": days_mode, "weekdays": weekdays or []}


class CronCompilerTests(unittest.TestCase):
    def test_weekday_encoding_cron_sun0_to_python(self):
        self.assertEqual(cron_dow_to_python_weekday(0), 6)  # Sun
        self.assertEqual(cron_dow_to_python_weekday(1), 0)  # Mon
        self.assertEqual(cron_dow_to_python_weekday(4), 3)  # Thu
        self.assertEqual(cron_dow_to_python_weekday(6), 5)  # Sat

    def test_next_runs_weekday_accuracy(self):
        thu = next_runs_kst(["0 12 * * 4"], FIXED_NOW, 2)
        self.assertEqual(thu[0], "2026-11-12 (Thu) 12:00 KST")
        self.assertEqual(thu[1], "2026-11-19 (Thu) 12:00 KST")
        sun = next_runs_kst(["0 20 * * 0"], FIXED_NOW, 1)
        self.assertEqual(sun[0], "2026-11-15 (Sun) 20:00 KST")
        wk = next_runs_kst(["53 7 * * 1-5"], datetime(2026, 11, 13, 8, 0, tzinfo=KST), 1)  # Fri after slot
        self.assertEqual(wk[0], "2026-11-16 (Mon) 07:53 KST")

    def test_next_runs_strictly_future_no_compensation(self):
        runs = next_runs_kst(["30 7,12,18 * * *"], datetime(2026, 11, 11, 12, 30, tzinfo=KST), 3)
        self.assertEqual(
            runs, ["2026-11-11 (Wed) 18:30 KST", "2026-11-12 (Thu) 07:30 KST", "2026-11-12 (Thu) 12:30 KST"]
        )

    def test_mixed_minute_grouping(self):
        cfg = {"enabled": True, "times_kst": ["18:30", "07:30", "12:15", "07:30"], "days_mode": "DAILY", "weekdays": []}
        self.assertEqual(compile_cron_exprs(cfg), ["30 7,18 * * *", "15 12 * * *"])
        cfg["days_mode"] = "WEEKDAYS"
        self.assertEqual(compile_cron_exprs(cfg)[0], "30 7,18 * * 1-5")
        cfg.update(days_mode="CUSTOM", weekdays=[5, 0, 3, 3])
        self.assertEqual(compile_cron_exprs(cfg)[0], "30 7,18 * * 0,3,5")

    def test_compile_lines_disabled_and_command_template(self):
        cfg = {"enabled": False, "times_kst": ["07:30"], "days_mode": "DAILY", "weekdays": []}
        self.assertEqual(
            compile_cron_lines("travel_watches", cfg, APP_DIR),
            [f"# [DISABLED] 30 7 * * * {APP_DIR}/run_cron.sh travel_watches"],
        )

    def test_shell_injection_rejected(self):
        for bad in ["07:30; rm -rf /", "07:30\n* * * * * curl evil", "7:30", "24:00", "07:60", "$(id)"]:
            _, errors, _ = normalize_payload("travel_weekly", _payload([bad]))
            self.assertTrue(errors, bad)
        _, errors, _ = normalize_payload("travel_weekly", _payload(["12:00"], "CUSTOM", [7]))
        self.assertTrue(errors)
        _, errors, _ = normalize_payload("travel_weekly", _payload(["12:00"], "CUSTOM", [True]))
        self.assertTrue(errors)
        _, errors, _ = normalize_payload("travel_weekly", _payload(["12:00"], "rm -rf"))
        self.assertTrue(errors)
        with self.assertRaises(ValueError):
            command_for("travel_watches;reboot", APP_DIR)
        with self.assertRaises(ValueError):
            command_for("travel_watches", "/tmp/x; reboot")
        with self.assertRaises(ValueError):
            ScheduleManager(None, InMemoryCrontabDriver(), app_dir="/tmp/$(id)")


class TravelWatchGuardrailTests(unittest.TestCase):
    def test_defaults_ok(self):
        cfg, errors, warnings = normalize_payload("travel_watches", _payload(["07:30", "12:30", "18:30"]))
        self.assertEqual(errors, [])
        self.assertEqual(warnings, [])
        self.assertEqual(cfg["weekdays"], [0, 1, 2, 3, 4, 5, 6])

    def test_night_hours_rejected_and_22_warned(self):
        for t in ["23:00", "00:30", "06:59", "03:00"]:
            _, errors, _ = normalize_payload("travel_watches", _payload([t]))
            self.assertTrue(any("야간" in e for e in errors), t)
        _, errors, warnings = normalize_payload("travel_watches", _payload(["07:00", "22:00"]))
        self.assertEqual(errors, [])
        self.assertTrue(any("22시" in w for w in warnings))

    def test_min_interval_120(self):
        _, errors, _ = normalize_payload("travel_watches", _payload(["07:30", "09:29"]))
        self.assertTrue(any("120분" in e for e in errors))
        _, errors, _ = normalize_payload("travel_watches", _payload(["07:30", "09:30"]))
        self.assertEqual(errors, [])

    def test_min_interval_weekly_wraparound(self):
        relaxed = replace(TASK_CATALOG["travel_watches"], reject_hours=frozenset(), warn_hours=frozenset())
        with patch.dict(TASK_CATALOG, {"travel_watches": relaxed}):
            # Sat 23:30 -> Sun 00:30 crosses the cron week boundary (Sat=6 -> Sun=0): 60 min.
            _, errors, _ = normalize_payload("travel_watches", _payload(["00:30", "23:30"], "CUSTOM", [0, 6]))
            self.assertTrue(any("최소 60분" in e for e in errors), errors)
            # Daily 23:30 -> next day 00:30 wrap
            _, errors, _ = normalize_payload("travel_watches", _payload(["00:30", "23:30"], "DAILY"))
            self.assertTrue(any("120분" in e for e in errors))
            # Single day only: no cross-day neighbour
            _, errors, _ = normalize_payload("travel_watches", _payload(["00:30", "23:30"], "CUSTOM", [3]))
            self.assertEqual(errors, [])

    def test_max_runs_and_warning(self):
        relaxed = replace(TASK_CATALOG["travel_watches"], min_interval_min=None)
        with patch.dict(TASK_CATALOG, {"travel_watches": relaxed}):
            thirteen = [f"{h:02d}:00" for h in range(7, 20)]
            _, errors, _ = normalize_payload("travel_watches", _payload(thirteen))
            self.assertTrue(any("최대 12회" in e for e in errors))
        seven = ["08:00", "10:00", "12:00", "14:00", "16:00", "18:00", "20:00"]
        _, errors, warnings = normalize_payload("travel_watches", _payload(seven))
        self.assertEqual(errors, [])
        self.assertTrue(any("권장치" in w for w in warnings))

    def test_empty_times_rejected(self):
        _, errors, _ = normalize_payload("travel_watches", _payload([]))
        self.assertTrue(any("최소 1개" in e for e in errors))


class _ManagerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = MarketBriefDatabase(db_path=str(Path(self.tmp.name) / "sched.sqlite3"), env_path=Path(self.tmp.name) / "none.env")
        self.driver = InMemoryCrontabDriver(LIVE_OCI_CRONTAB, app_dir=APP_DIR)
        self.mgr = ScheduleManager(self.db, self.driver, app_dir=APP_DIR, now_fn=lambda: FIXED_NOW)

    def tearDown(self):
        try:
            self.tmp.cleanup()
        except Exception:
            pass

    def _history_count(self):
        with self.db._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM system_config_history WHERE setting_key='bot_schedules'").fetchone()[0]


class MigrationTests(_ManagerCase):
    def test_get_is_read_only_and_imports_live_schedules(self):
        ov = self.mgr.get_overview()
        self.assertEqual(self.driver.writes, [])
        self.assertEqual(ov["migration_state"], "MIGRATION_REQUIRED")
        self.assertTrue(ov["db_backup_protected"])
        self.assertEqual(len(ov["unmanaged_task_lines"]), 7)
        tasks = {t["task_id"]: t for t in ov["tasks"]}
        self.assertEqual(tasks["travel_watches"]["source"], "CRONTAB_UNMANAGED")
        self.assertEqual(tasks["travel_watches"]["config"]["times_kst"], ["07:30", "12:30", "18:30"])
        self.assertEqual(tasks["market_auto"]["cron_exprs"], ["53 7 * * 1-5", "42 15 * * 1-5"])
        self.assertFalse(tasks["market_auto"]["editable"])
        self.assertEqual(tasks["market_auto"]["control_mode"], "READ_ONLY_DUAL_PATH")
        self.assertFalse(tasks["market_auto"]["mismatch"])
        self.assertEqual(tasks["market_check_urgent"]["cron_exprs"], ["7,22,37,52 9-15 * * 1-5"])
        self.assertEqual(tasks["travel_weekly"]["cron_exprs"], ["0 12 * * 4"])
        self.assertEqual(tasks["admission_weekly"]["cron_exprs"], ["0 20 * * 0"])
        self.assertEqual(tasks["travel_weekly"]["next_runs_kst"][0], "2026-11-12 (Thu) 12:00 KST")
        joined = " ".join(ov["policy_warnings"])
        self.assertIn("15:37", joined)
        self.assertIn("15:52", joined)
        self.assertTrue(ov["migration_preview_diff"])
        self.assertIsNone(self.db.get_setting("bot_schedules"))

    def test_put_requires_confirm_migration(self):
        res = self.mgr.apply("travel_watches", _payload(["08:00", "13:00", "19:00"]))
        self.assertEqual(res["status"], "MIGRATION_REQUIRED")
        self.assertTrue(res["diff"])
        self.assertEqual(self.driver.writes, [])
        self.assertIsNone(self.db.get_latest_schedule_journal())

    def test_confirmed_migration_preserves_protected_bytes(self):
        res = self.mgr.apply("travel_watches", _payload(["08:00", "13:00", "19:00"]), confirm_migration=True)
        self.assertEqual(res["status"], "APPLIED", res)
        self.assertFalse(res["executed_now"])
        new = self.driver.text
        new_lines = new.splitlines()
        # Protected lines byte-identical
        self.assertIn(DB_BACKUP_LINE, new_lines)
        self.assertEqual(new_lines[:4], LIVE_OCI_CRONTAB.splitlines()[:4])
        # Legacy section headers stripped, no unmanaged allowlisted lines remain
        for header in ("# [1. 시황봇]", "# [2. 여행봇]", "# [3. 입시봇]"):
            self.assertNotIn(header, new_lines)
        parsed = parse_crontab(new)
        self.assertEqual(parsed.state, "MANAGED")
        self.assertEqual(parsed.unmanaged, [])
        self.assertEqual(new.count(MANAGED_START), 1)
        block = parsed.managed_block_text()
        # Read-only market lines imported verbatim; others keep live times
        self.assertIn(f"53 7 * * 1-5 {APP_DIR}/run_cron.sh market_auto", block)
        self.assertIn(f"42 15 * * 1-5 {APP_DIR}/run_cron.sh market_auto", block)
        self.assertIn(f"7,22,37,52 9-15 * * 1-5 {APP_DIR}/run_cron.sh market_check_urgent", block)
        self.assertIn(f"0 12 * * 4 {APP_DIR}/run_cron.sh travel_weekly", block)
        self.assertIn(f"30 8 * * * {APP_DIR}/run_cron.sh admission_collect", block)
        self.assertIn(f"0 20 * * 0 {APP_DIR}/run_cron.sh admission_weekly", block)
        self.assertIn(f"0 8,13,19 * * * {APP_DIR}/run_cron.sh travel_watches", block)
        self.assertNotIn("30 7,12,18", block)
        # DB SSOT + history + journal committed
        canonical = json.loads(self.db.get_setting("bot_schedules"))
        self.assertEqual(canonical["tasks"]["travel_watches"]["times_kst"], ["08:00", "13:00", "19:00"])
        self.assertEqual(self._history_count(), 1)
        self.assertEqual(self.db.get_latest_schedule_journal()["status"], "APPLIED")
        # Second GET: managed & in sync
        ov = self.mgr.get_overview()
        self.assertEqual(ov["migration_state"], "MANAGED")
        self.assertEqual(ov["sync_state"], "IN_SYNC")
        self.assertTrue(ov["db_backup_protected"])

    def test_second_put_replaces_block_in_place(self):
        self.mgr.apply("travel_watches", _payload(["08:00"]), confirm_migration=True)
        res = self.mgr.apply("admission_collect", _payload(["09:15"]))
        self.assertEqual(res["status"], "APPLIED", res)
        text = self.driver.text
        self.assertEqual(text.count(MANAGED_START), 1)
        self.assertEqual(text.count(MANAGED_END), 1)
        self.assertIn(f"15 9 * * * {APP_DIR}/run_cron.sh admission_collect", text)
        self.assertIn(f"0 8 * * * {APP_DIR}/run_cron.sh travel_watches", text)
        self.assertIn(DB_BACKUP_LINE, text.splitlines())
        self.assertEqual(self._history_count(), 2)

    def test_disabled_rendered_as_comment_and_not_reimported_as_unmanaged(self):
        res = self.mgr.apply("travel_watches", _payload(["07:30"], enabled=False), confirm_migration=True)
        self.assertEqual(res["status"], "APPLIED")
        self.assertEqual(res["next_runs_kst"], [])
        self.assertIn(f"# [DISABLED] 30 7 * * * {APP_DIR}/run_cron.sh travel_watches", self.driver.text)
        parsed = parse_crontab(self.driver.text)
        self.assertEqual(parsed.state, "MANAGED")
        self.assertFalse(parsed.imported_configs()["travel_watches"]["enabled"])

    def test_non_allowlisted_and_inexact_lines_are_preserved(self):
        extra = [
            f"*/5 * * * * {APP_DIR}/run_cron.sh market_morning >> /tmp/x.log 2>&1",
            f"0 9 * * * {APP_DIR}/run_cron.sh travel_watches --dry-run",
            "# operator note",
        ]
        self.driver.text = LIVE_OCI_CRONTAB + "\n".join(extra) + "\n"
        res = self.mgr.apply("travel_watches", _payload(["08:00"]), confirm_migration=True)
        self.assertEqual(res["status"], "APPLIED")
        for line in extra:
            self.assertIn(line, self.driver.text.splitlines())

    def test_empty_crontab_appends_block(self):
        self.driver.text = ""
        res = self.mgr.apply("travel_watches", _payload(["07:30", "12:30", "18:30"]))
        self.assertEqual(res["status"], "APPLIED")
        self.assertTrue(self.driver.text.startswith(MANAGED_START))
        # Tasks never registered stay OFF (no silent activation)
        self.assertIn(f"# [DISABLED] 30 8 * * * {APP_DIR}/run_cron.sh admission_collect", self.driver.text)

    def test_migration_conflict_refuses_overwrite(self):
        cases = {
            "duplicate": f"{MANAGED_START}\n{MANAGED_START}\n{MANAGED_END}\n",
            "unclosed": f"{DB_BACKUP_LINE}\n{MANAGED_START}\n",
            "inverted": f"{MANAGED_END}\n{MANAGED_START}\n",
        }
        for name, text in cases.items():
            self.driver.text = text
            self.assertEqual(self.mgr.get_overview()["migration_state"], "MIGRATION_CONFLICT", name)
            res = self.mgr.apply("travel_watches", _payload(["08:00"]), confirm_migration=True)
            self.assertEqual(res["status"], "MIGRATION_CONFLICT", name)
            self.assertEqual(self.driver.text, text)
        self.assertEqual(self.driver.writes, [])

    def test_read_only_and_unknown_tasks(self):
        self.assertEqual(self.mgr.apply("market_auto", _payload(["07:50"]))["status"], "READ_ONLY")
        self.assertEqual(self.mgr.apply("market_check_urgent", _payload(["09:00"]))["status"], "READ_ONLY")
        self.assertEqual(self.mgr.apply("rm -rf", _payload(["09:00"]))["status"], "UNKNOWN_TASK")
        self.assertEqual(self.driver.writes, [])

    def test_preview_is_pure(self):
        self.mgr.get_overview()  # caches snapshot
        with patch.object(sm.subprocess, "run", side_effect=AssertionError("subprocess forbidden")):
            with patch.object(self.driver, "read", side_effect=AssertionError("preview must not read")):
                res = self.mgr.preview("travel_watches", _payload(["08:00", "20:00"]))
        self.assertEqual(res["status"], "OK")
        self.assertEqual(res["cron_lines"], [f"0 8,20 * * * {APP_DIR}/run_cron.sh travel_watches"])
        self.assertEqual(res["next_runs_kst"][0], "2026-11-11 (Wed) 20:00 KST")
        self.assertTrue(res["diff_available"])
        self.assertTrue(any(p["is_db_backup"] for p in res["protected_lines"]))
        self.assertEqual(self.driver.writes, [])
        self.assertIsNone(self.db.get_latest_schedule_journal())


class SyncJournalTests(_ManagerCase):
    def test_crontab_unavailable(self):
        mgr = ScheduleManager(self.db, UnavailableCrontabDriver(), app_dir=APP_DIR)
        self.assertEqual(mgr.apply("travel_watches", _payload(["08:00"]))["status"], "CRONTAB_UNAVAILABLE")
        ov = mgr.get_overview()
        self.assertEqual(ov["crontab_status"], "UNAVAILABLE")
        self.assertEqual(ov["migration_state"], "UNKNOWN")

    def test_sync_failed_on_write_leaves_db_untouched(self):
        self.driver.fail_write = True
        res = self.mgr.apply("travel_watches", _payload(["08:00"]), confirm_migration=True)
        self.assertEqual(res["status"], "SYNC_FAILED")
        self.assertEqual(self.driver.text, LIVE_OCI_CRONTAB)
        self.assertIsNone(self.db.get_setting("bot_schedules"))
        self.assertEqual(self._history_count(), 0)
        self.assertEqual(self.db.get_latest_schedule_journal()["status"], "SYNC_FAILED")

    def test_sync_verify_mismatch_restores_snapshot(self):
        self.driver.corrupt_writes = 1
        res = self.mgr.apply("travel_watches", _payload(["08:00"]), confirm_migration=True)
        self.assertEqual(res["status"], "SYNC_FAILED")
        self.assertEqual(self.driver.text, LIVE_OCI_CRONTAB)
        self.assertIsNone(self.db.get_setting("bot_schedules"))
        journal = self.db.get_latest_schedule_journal()
        self.assertEqual(journal["status"], "SYNC_FAILED")
        self.assertIn("SHA-256", journal["error_detail"])

    def _fail_canonical_commit(self):
        original = self.db.finalize_schedule_journal

        def flaky(journal_id, status, error_detail=None, canonical_value=None, **kw):
            if canonical_value is not None:
                raise RuntimeError("disk I/O error")
            return original(journal_id, status, error_detail=error_detail, canonical_value=None, **kw)

        return patch.object(self.db, "finalize_schedule_journal", side_effect=flaky)

    def test_db_failure_rolls_back_crontab(self):
        with self._fail_canonical_commit():
            res = self.mgr.apply("travel_watches", _payload(["08:00"]), confirm_migration=True)
        self.assertEqual(res["status"], "DB_COMMIT_FAILED")
        self.assertEqual(self.driver.text, LIVE_OCI_CRONTAB)
        self.assertIsNone(self.db.get_setting("bot_schedules"))
        self.assertEqual(self.db.get_latest_schedule_journal()["status"], "ROLLED_BACK")

    def test_db_failure_with_rollback_failure_is_diverged_and_blocks(self):
        self.driver.fail_write_after = 1  # first (target) write OK, rollback write fails
        with self._fail_canonical_commit():
            res = self.mgr.apply("travel_watches", _payload(["08:00"]), confirm_migration=True)
        self.assertEqual(res["status"], "DIVERGED")
        journal = self.db.get_latest_schedule_journal()
        self.assertEqual(journal["status"], "DIVERGED")
        self.assertEqual(journal["snapshot_crontab"], LIVE_OCI_CRONTAB)
        self.assertIsNone(self.db.get_setting("bot_schedules"))
        self.assertTrue(self.mgr.get_overview()["diverged"])
        self.driver.fail_write_after = None
        blocked = self.mgr.apply("travel_watches", _payload(["09:00"]))
        self.assertEqual(blocked["status"], "DIVERGED")
        self.assertEqual(len(self.driver.writes), 1)

    def test_apply_never_spawns_processes(self):
        with patch.object(sm.subprocess, "run", side_effect=AssertionError("subprocess forbidden")) as run_mock, \
             patch("subprocess.Popen", side_effect=AssertionError("Popen forbidden")) as popen_mock, \
             patch("os.system", side_effect=AssertionError("os.system forbidden")) as system_mock:
            res = self.mgr.apply("travel_watches", _payload(["08:00"]), confirm_migration=True)
        self.assertEqual(res["status"], "APPLIED")
        run_mock.assert_not_called()
        popen_mock.assert_not_called()
        system_mock.assert_not_called()


class LinuxDriverTests(unittest.TestCase):
    def test_linux_driver_uses_argv_without_shell(self):
        calls = []

        class _Proc:
            def __init__(self, rc=0, out="", err=""):
                self.returncode, self.stdout, self.stderr = rc, out, err

        def fake_run(args, **kwargs):
            calls.append((args, kwargs))
            return _Proc(out="SHELL=/bin/bash\n") if args == ["crontab", "-l"] else _Proc()

        drv = LinuxCrontabDriver(APP_DIR)
        with patch.object(sm.subprocess, "run", side_effect=fake_run):
            self.assertEqual(drv.read(), "SHELL=/bin/bash\n")
            drv.write("x\n")
        self.assertEqual(calls[0][0], ["crontab", "-l"])
        self.assertEqual(calls[1][0], ["crontab", "-"])
        self.assertTrue(all(kw.get("shell") is False for _, kw in calls))
        self.assertEqual(calls[1][1]["input"], "x\n")

    def test_linux_driver_no_crontab_is_empty(self):
        class _Proc:
            returncode, stdout, stderr = 1, "", "no crontab for ubuntu"

        with patch.object(sm.subprocess, "run", return_value=_Proc()):
            self.assertEqual(LinuxCrontabDriver(APP_DIR).read(), "")


if __name__ == "__main__":
    unittest.main()
