"""Production SQLite / Turso database management for Market Brief admin and runner."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3
import uuid
from typing import Any

from market_brief.storage.models import (
    AttemptStatus,
    BatchStatus,
    DeliveryBatch,
    DeliveryJob,
    DeliveryJobRecipient,
    JobFinalStatus,
    Recipient,
    SystemConfig,
)


DEFAULT_THRESHOLDS = {
    "KOSPI": {"unit": "PERCENT", "threshold": 3.0, "recovery": 1.5, "cooldown_minutes": 120, "max_daily": 2},
    "KOSDAQ": {"unit": "PERCENT", "threshold": 3.0, "recovery": 1.5, "cooldown_minutes": 120, "max_daily": 2},
    "KOSPI200": {"unit": "PERCENT", "threshold": 3.0, "recovery": 1.5, "cooldown_minutes": 120, "max_daily": 2},
    "USDKRW": {"unit": "PERCENT", "threshold": 1.2, "recovery": 0.6, "cooldown_minutes": 180, "max_daily": 2},
    "KR10Y": {"unit": "BASIS_POINTS", "threshold": 10.0, "recovery": 5.0, "cooldown_minutes": 240, "max_daily": 1},
    "US10Y": {"unit": "BASIS_POINTS", "threshold": 10.0, "recovery": 5.0, "cooldown_minutes": 240, "max_daily": 1},
    "WTI": {"unit": "PERCENT", "threshold": 4.0, "recovery": 2.0, "cooldown_minutes": 180, "max_daily": 2},
    "GOLD": {"unit": "PERCENT", "threshold": 2.5, "recovery": 1.2, "cooldown_minutes": 180, "max_daily": 2},
}


class MarketBriefDatabase:
    """Thread-safe SQLite database manager supporting ACID transactions and atomic lease locks."""

    def __init__(self, db_path: str = "alerts.sqlite3", env_path: Path | None = None):
        self.db_path = str(db_path)
        self.env_path = env_path
        self._init_schema()
        self._seed_initial_data()

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _init_schema(self) -> None:
        with self._connect() as db:
            # 1. Backward-compatible legacy tables
            db.execute(
                """CREATE TABLE IF NOT EXISTS alert_reservations (
                    key TEXT PRIMARY KEY,
                    status TEXT NOT NULL,
                    change_pct REAL,
                    symbol TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS delivery_audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    event_key TEXT NOT NULL,
                    channel TEXT NOT NULL,
                    recipient TEXT NOT NULL,
                    status TEXT NOT NULL,
                    provider_msg_id TEXT,
                    attempts INTEGER NOT NULL DEFAULT 1,
                    error_detail TEXT,
                    logged_at TEXT NOT NULL
                )"""
            )

            # 2. Modern Recipients Master
            db.execute(
                """CREATE TABLE IF NOT EXISTS recipients (
                    chat_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT 'TELEGRAM',
                    group_tag TEXT DEFAULT 'GENERAL',
                    subscribed_topics TEXT DEFAULT 'FINANCE',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    inactivation_reason TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )"""
            )

            # 3. Logical Batch Master (Hardware-level Unique batch_key & Atomic Lease)
            db.execute(
                """CREATE TABLE IF NOT EXISTS delivery_batches (
                    batch_key TEXT PRIMARY KEY,
                    topic TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    scheduled_date TEXT NOT NULL,
                    status TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_token TEXT,
                    lease_expires_at TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    finished_at TEXT
                )"""
            )

            # 4. Physical Execution Jobs
            db.execute(
                """CREATE TABLE IF NOT EXISTS delivery_jobs (
                    job_id TEXT PRIMARY KEY,
                    batch_key TEXT NOT NULL,
                    attempt_no INTEGER NOT NULL DEFAULT 1,
                    is_forced INTEGER DEFAULT 0,
                    force_reason TEXT,
                    status TEXT NOT NULL,
                    total_targets INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0,
                    fail_count INTEGER DEFAULT 0,
                    unknown_count INTEGER DEFAULT 0,
                    report_path TEXT,
                    started_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    finished_at TEXT,
                    FOREIGN KEY(batch_key) REFERENCES delivery_batches(batch_key)
                )"""
            )

            # 5. Immutable Target Recipient Snapshot per Job
            db.execute(
                """CREATE TABLE IF NOT EXISTS delivery_job_recipients (
                    job_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    error_detail TEXT,
                    provider_msg_id TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(job_id, chat_id),
                    FOREIGN KEY(job_id) REFERENCES delivery_jobs(job_id)
                )"""
            )

            # 6. Dynamic System Settings & Versioning History
            db.execute(
                """CREATE TABLE IF NOT EXISTS system_settings (
                    setting_key TEXT PRIMARY KEY,
                    setting_value TEXT NOT NULL,
                    description TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS system_config_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    config_version INTEGER NOT NULL,
                    setting_key TEXT NOT NULL,
                    setting_value TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    reason TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                )"""
            )

            # 7. Telegram Update Sync Offset
            db.execute(
                """CREATE TABLE IF NOT EXISTS telegram_sync_state (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    last_update_id INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            db.execute("INSERT OR IGNORE INTO telegram_sync_state (id, last_update_id) VALUES (1, 0)")

    def _seed_initial_data(self) -> None:
        with self._connect() as db:
            # Seed system settings
            db.execute(
                """INSERT OR IGNORE INTO system_settings (setting_key, setting_value, description)
                   VALUES ('thresholds', ?, '자산군별 긴급 알림 변동률 및 쿨다운 설정')""",
                (json.dumps(DEFAULT_THRESHOLDS, ensure_ascii=False),),
            )
            db.execute(
                """INSERT OR IGNORE INTO system_settings (setting_key, setting_value, description)
                   VALUES ('schedules', ?, '정기 브리핑 기준 발송 시각')""",
                (json.dumps({"morning": "06:30", "closing": "15:40"}),),
            )

            # Seed recipients from .env if table is empty
            row_count = db.execute("SELECT COUNT(*) FROM recipients").fetchone()[0]
            if row_count == 0 and self.env_path and self.env_path.exists():
                try:
                    for line in self.env_path.read_text(encoding="utf-8-sig").splitlines():
                        line = line.strip()
                        if line.startswith("TELEGRAM_CHAT_ID="):
                            raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                            for cid in raw.split(","):
                                cid = cid.strip()
                                if cid:
                                    db.execute(
                                        """INSERT OR IGNORE INTO recipients
                                           (chat_id, name, channel, group_tag, subscribed_topics, is_active)
                                           VALUES (?, ?, 'TELEGRAM', 'DEFAULT', 'FINANCE', 1)""",
                                        (cid, f"수신자_{cid[-4:]}"),
                                    )
                except Exception:
                    pass

    # =========================================================================
    # Atomic Idempotency & Lease Locking
    # =========================================================================

    def acquire_batch_lease(
        self,
        batch_key: str,
        topic: str,
        kind: str,
        scheduled_date: str,
        runner_id: str,
        lease_minutes: int = 15,
        is_forced: bool = False,
        force_reason: str | None = None,
    ) -> tuple[bool, str, DeliveryJob | None]:
        """Atomically acquires execution lock for the logical batch key.
        Returns: (acquired, reason, job)
        """
        now = datetime.now(timezone.utc)
        expires_at = (now + timedelta(minutes=lease_minutes)).strftime("%Y-%m-%d %H:%M:%S")
        token = uuid.uuid4().hex
        job_id = f"job_{uuid.uuid4().hex[:12]}"

        with self._connect() as db:
            row = db.execute("SELECT * FROM delivery_batches WHERE batch_key = ?", (batch_key,)).fetchone()

            if row:
                current_status = row["status"]
                lease_exp = row["lease_expires_at"]
                is_expired = lease_exp and lease_exp < now.strftime("%Y-%m-%d %H:%M:%S")

                if current_status == BatchStatus.COMPLETED and not is_forced:
                    return False, "ALREADY_COMPLETED", None

                if current_status == BatchStatus.LOCKED and not is_expired and not is_forced:
                    return False, f"ACTIVE_LEASE_HELD_BY_{row['lease_owner']}", None

                # Safe takeover / Force rerun
                cursor = db.execute(
                    """UPDATE delivery_batches
                       SET status = 'LOCKED', lease_owner = ?, lease_token = ?, lease_expires_at = ?
                       WHERE batch_key = ? AND (status != 'COMPLETED' OR ? = 1)""",
                    (runner_id, token, expires_at, batch_key, 1 if is_forced else 0),
                )
                if cursor.rowcount == 0:
                    return False, "CONCURRENT_LOCK_CONFLICT", None

                attempt_no = db.execute(
                    "SELECT COALESCE(MAX(attempt_no), 0) + 1 FROM delivery_jobs WHERE batch_key = ?",
                    (batch_key,),
                ).fetchone()[0]
            else:
                # Fresh insert
                try:
                    db.execute(
                        """INSERT INTO delivery_batches
                           (batch_key, topic, kind, scheduled_date, status, lease_owner, lease_token, lease_expires_at)
                           VALUES (?, ?, ?, ?, 'LOCKED', ?, ?, ?)""",
                        (batch_key, topic, kind, scheduled_date, runner_id, token, expires_at),
                    )
                    attempt_no = 1
                except sqlite3.IntegrityError:
                    return False, "CONCURRENT_INSERT_CONFLICT", None

            # Create physical job record
            db.execute(
                """INSERT INTO delivery_jobs
                   (job_id, batch_key, attempt_no, is_forced, force_reason, status)
                   VALUES (?, ?, ?, ?, ?, 'PENDING')""",
                (job_id, batch_key, attempt_no, 1 if is_forced else 0, force_reason),
            )

            job = DeliveryJob(
                job_id=job_id,
                batch_key=batch_key,
                attempt_no=attempt_no,
                is_forced=is_forced,
                force_reason=force_reason,
                status=JobFinalStatus.PENDING,
            )
            return True, "LEASE_ACQUIRED", job

    def snapshot_job_recipients(self, job_id: str, recipients: list[Recipient]) -> None:
        """Fixes the target recipient list for this job execution (immutable snapshot)."""
        with self._connect() as db:
            for r in recipients:
                db.execute(
                    """INSERT OR IGNORE INTO delivery_job_recipients (job_id, chat_id, status)
                       VALUES (?, ?, 'PENDING')""",
                    (job_id, r.chat_id),
                )

    def record_recipient_result(
        self,
        job_id: str,
        chat_id: str,
        status: str,
        provider_msg_id: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        """Records granular recipient delivery result."""
        with self._connect() as db:
            db.execute(
                """UPDATE delivery_job_recipients
                   SET status = ?, provider_msg_id = ?, error_detail = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE job_id = ? AND chat_id = ?""",
                (status, provider_msg_id, error_detail, job_id, chat_id),
            )

    def complete_job(
        self,
        job_id: str,
        status: JobFinalStatus,
        total: int,
        success: int,
        fail: int,
        unknown: int,
        report_path: str | None = None,
    ) -> None:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as db:
            row = db.execute("SELECT batch_key FROM delivery_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                return
            batch_key = row["batch_key"]

            db.execute(
                """UPDATE delivery_jobs
                   SET status = ?, total_targets = ?, success_count = ?, fail_count = ?,
                       unknown_count = ?, report_path = ?, finished_at = ?
                   WHERE job_id = ?""",
                (status.value, total, success, fail, unknown, report_path, now_str, job_id),
            )

            # Mark batch completed if at least partial or success
            batch_status = BatchStatus.COMPLETED if status in {JobFinalStatus.SENT, JobFinalStatus.PARTIAL} else BatchStatus.FAILED
            db.execute(
                """UPDATE delivery_batches
                   SET status = ?, finished_at = ?
                   WHERE batch_key = ?""",
                (batch_status.value, now_str, batch_key),
            )

            # Legacy compatibility mirror
            db.execute(
                """INSERT OR REPLACE INTO alert_reservations (key, status, change_pct, symbol, updated_at)
                   VALUES (?, 'sent', 0.0, 'regular', CURRENT_TIMESTAMP)""",
                (batch_key,),
            )

    def fail_job(self, job_id: str, error_message: str) -> None:
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        with self._connect() as db:
            row = db.execute("SELECT batch_key FROM delivery_jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row:
                batch_key = row["batch_key"]
                db.execute(
                    "UPDATE delivery_batches SET status = 'FAILED', finished_at = ? WHERE batch_key = ?",
                    (now_str, batch_key),
                )
            db.execute(
                "UPDATE delivery_jobs SET status = 'FAILED', finished_at = ? WHERE job_id = ?",
                (now_str, job_id),
            )

    # =========================================================================
    # Recipient Management (CRUD)
    # =========================================================================

    def get_active_recipients(self, topic: str = "FINANCE") -> list[Recipient]:
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM recipients WHERE is_active = 1 ORDER BY created_at ASC"
            ).fetchall()
            results = []
            for r in rows:
                topics = [t.strip().upper() for t in (r["subscribed_topics"] or "").split(",") if t.strip()]
                if topic.upper() in topics or "ALL" in topics:
                    results.append(
                        Recipient(
                            chat_id=r["chat_id"],
                            name=r["name"],
                            channel=r["channel"],
                            group_tag=r["group_tag"],
                            subscribed_topics=r["subscribed_topics"],
                            is_active=bool(r["is_active"]),
                            inactivation_reason=r["inactivation_reason"],
                            created_at=r["created_at"],
                        )
                    )
            return results

    def get_all_recipients(self) -> list[Recipient]:
        with self._connect() as db:
            rows = db.execute("SELECT * FROM recipients ORDER BY created_at DESC").fetchall()
            return [
                Recipient(
                    chat_id=r["chat_id"],
                    name=r["name"],
                    channel=r["channel"],
                    group_tag=r["group_tag"],
                    subscribed_topics=r["subscribed_topics"],
                    is_active=bool(r["is_active"]),
                    inactivation_reason=r["inactivation_reason"],
                    created_at=r["created_at"],
                )
                for r in rows
            ]

    def add_recipient(
        self,
        chat_id: str,
        name: str,
        channel: str = "TELEGRAM",
        group_tag: str = "GENERAL",
        subscribed_topics: str = "FINANCE",
    ) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                """INSERT OR REPLACE INTO recipients
                   (chat_id, name, channel, group_tag, subscribed_topics, is_active, inactivation_reason)
                   VALUES (?, ?, ?, ?, ?, 1, NULL)""",
                (str(chat_id).strip(), name.strip(), channel, group_tag, subscribed_topics),
            )
            return cursor.rowcount > 0

    def update_recipient_name(self, chat_id: str, new_name: str) -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE recipients SET name = ? WHERE chat_id = ?",
                (new_name.strip(), str(chat_id).strip()),
            )
            return cursor.rowcount > 0

    def deactivate_recipient(self, chat_id: str, reason: str = "MANUAL_STOP") -> bool:
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE recipients SET is_active = 0, inactivation_reason = ? WHERE chat_id = ?",
                (reason, str(chat_id).strip()),
            )
            return cursor.rowcount > 0

    def delete_recipient(self, chat_id: str) -> bool:
        with self._connect() as db:
            cursor = db.execute("DELETE FROM recipients WHERE chat_id = ?", (str(chat_id).strip(),))
            return cursor.rowcount > 0

    # =========================================================================
    # Telegram Offset Sync
    # =========================================================================

    def get_telegram_offset(self) -> int:
        with self._connect() as db:
            row = db.execute("SELECT last_update_id FROM telegram_sync_state WHERE id = 1").fetchone()
            return row["last_update_id"] if row else 0

    def update_telegram_offset(self, offset: int) -> None:
        with self._connect() as db:
            db.execute(
                "UPDATE telegram_sync_state SET last_update_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = 1",
                (offset,),
            )

    # =========================================================================
    # Settings & Audit History
    # =========================================================================

    def get_setting(self, key: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT setting_value FROM system_settings WHERE setting_key = ?", (key,)).fetchone()
            return row["setting_value"] if row else None

    def set_setting(self, key: str, value: str, updated_by: str = "ADMIN", reason: str | None = None) -> None:
        with self._connect() as db:
            db.execute(
                """INSERT OR REPLACE INTO system_settings (setting_key, setting_value, updated_at)
                   VALUES (?, ?, CURRENT_TIMESTAMP)""",
                (key, value),
            )
            # Log version history
            v = db.execute(
                "SELECT COALESCE(MAX(config_version), 0) + 1 FROM system_config_history WHERE setting_key = ?",
                (key,),
            ).fetchone()[0]
            db.execute(
                """INSERT INTO system_config_history (config_version, setting_key, setting_value, updated_by, reason)
                   VALUES (?, ?, ?, ?, ?)""",
                (v, key, value, updated_by, reason),
            )

    def get_recent_jobs(self, limit: int = 15) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT j.*, b.topic, b.kind, b.scheduled_date
                   FROM delivery_jobs j
                   JOIN delivery_batches b ON j.batch_key = b.batch_key
                   ORDER BY j.started_at DESC LIMIT ?""",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def get_job_recipients(self, job_id: str) -> list[dict[str, Any]]:
        with self._connect() as db:
            rows = db.execute(
                """SELECT jr.*, r.name, r.channel
                   FROM delivery_job_recipients jr
                   LEFT JOIN recipients r ON jr.chat_id = r.chat_id
                   WHERE jr.job_id = ?
                   ORDER BY jr.updated_at ASC""",
                (job_id,),
            ).fetchall()
            return [dict(r) for r in rows]

    # =========================================================================
    # Backward Compatibility with AlertStore
    # =========================================================================

    def is_regular_briefing_sent(self, kind: str, trade_date: date) -> bool:
        key = f"REGULAR:{trade_date.isoformat()}:{kind}"
        with self._connect() as db:
            row = db.execute(
                """SELECT 1 FROM delivery_batches
                   WHERE batch_key = ? AND status = 'COMPLETED'
                   UNION
                   SELECT 1 FROM alert_reservations WHERE key = ?""",
                (key, key),
            ).fetchone()
            return row is not None

    def reserve_regular_briefing(self, kind: str, trade_date: date) -> bool:
        key = f"REGULAR:{trade_date.isoformat()}:{kind}"
        with self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO alert_reservations (key, status, change_pct, symbol) VALUES (?, 'sent', 0.0, ?)",
                (key, kind),
            )
            return cursor.rowcount == 1
