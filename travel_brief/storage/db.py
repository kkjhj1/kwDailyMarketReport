"""Production SQLite / Turso database management for Market Brief admin and runner."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta
import json
import logging
from pathlib import Path
import sqlite3
import threading
import uuid
from typing import Any, Optional

from travel_brief.storage.models import (
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

logger = logging.getLogger(__name__)

WATCH_MAX_ACTIVE_PER_CHAT = 10
WATCH_PRESET_KINDS = ("DEAL", "FLOOR", "CUSTOM")
WATCH_DAY_PATTERNS = ("ANY", "FRI_MON", "THU_SUN")
WATCH_DELIVERY_STATUSES = ("IN_FLIGHT", "SENT", "UNKNOWN", "FAILED")
# Statuses that count as "delivered" for duplicate / cooldown purposes (UNKNOWN is never auto-retried).
WATCH_BLOCKING_STATUSES = ("SENT", "UNKNOWN", "IN_FLIGHT")
WATCH_COOLDOWN_DAYS = 7
WATCH_MIN_REDROP_KRW = 10_000
TRAVEL_BOT_ID = "travel_bot"


class WatchLimitError(Exception):
    """Raised when activating another watch would exceed the per-chat active watch limit."""

    def __init__(self, limit: int, active: int):
        super().__init__(f"limit_exceeded: {active}/{limit} active watches")
        self.limit = limit
        self.active = active


@dataclass(frozen=True)
class WatchClaim:
    claimed: bool
    reason: str
    delivery_key: str
    lease_token: Optional[str] = None
    attempt_no: int = 0


def _utc_str(dt: Optional[datetime] = None) -> str:
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _topics_of(raw: Optional[str]) -> list[str]:
    return [t.strip().upper() for t in (raw or "").split(",") if t.strip()]


def watch_cooldown_block_reason(
    recent_rows: list[Any], dep_date: str, ret_date: str, price_krw: int, min_drop_krw: int = WATCH_MIN_REDROP_KRW
) -> Optional[str]:
    """7-day anti-spam rule over recent SENT/UNKNOWN/IN_FLIGHT rows of ONE watch.
    Same (dep, ret) slot -> `slot_cooldown`; otherwise the price must undercut the cheapest recent alert by
    `min_drop_krw` -> else `insufficient_drop`. Returns None when the alert may be claimed.
    """
    if not recent_rows:
        return None
    if any(r["dep_date"] == dep_date and r["ret_date"] == ret_date for r in recent_rows):
        return "slot_cooldown"
    floor = min(int(r["price_krw"]) for r in recent_rows)
    if int(price_krw) > floor - int(min_drop_krw):
        return "insufficient_drop"
    return None


def make_watch_delivery_key(
    watch_id: int, chat_id: str, dest_code: str, dep_date: str, ret_date: str, price_krw: int
) -> str:
    """Ledger key always embeds `watch_id` so two watches can never collide on the same candidate."""
    return f"watch:{int(watch_id)}:{str(chat_id).strip()}:{dest_code.upper()}:{dep_date}:{ret_date}:{int(price_krw)}"


class TravelBriefDatabase:
    """Thread-safe SQLite database manager supporting ACID transactions and atomic lease locks."""

    def __init__(self, db_path: str = "alerts.sqlite3", env_path: Path | None = None):
        self.db_path = str(db_path)
        self.env_path = env_path
        self._lock = threading.RLock()
        self._uri = self.db_path.startswith("file:")
        self._shared_conn: Optional[sqlite3.Connection] = None
        if self.db_path == ":memory:" or (self._uri and "mode=memory" in self.db_path):
            # One shared handle for the process lifetime: each `:memory:` connect would be a new empty DB.
            self._shared_conn = sqlite3.connect(
                self.db_path, timeout=15.0, check_same_thread=False, uri=self._uri
            )
            self._shared_conn.row_factory = sqlite3.Row
        else:
            self._enable_wal()
        self._init_schema()
        self._seed_initial_data()

    @property
    def is_memory(self) -> bool:
        return self._shared_conn is not None

    def _open_file_conn(self, autocommit: bool = False) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=15.0, uri=self._uri)
        if autocommit:
            conn.isolation_level = None
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    def _enable_wal(self) -> None:
        try:
            conn = self._open_file_conn(autocommit=True)
            try:
                conn.execute("PRAGMA journal_mode=WAL")
            finally:
                conn.close()
        except sqlite3.Error as exc:
            logger.warning("WAL journal mode could not be enabled for %s: %s", self.db_path, exc)

    def journal_mode(self) -> str:
        with self._connect() as db:
            return str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower()

    @contextmanager
    def _connect(self):
        if self._shared_conn is not None:
            with self._lock:
                with self._shared_conn:
                    yield self._shared_conn
            return
        conn = self._open_file_conn()
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    @contextmanager
    def _immediate_tx(self):
        """`BEGIN IMMEDIATE` write transaction (cross-process safe for file DBs, RLock for `:memory:`)."""
        if self._shared_conn is not None:
            with self._lock:
                conn = self._shared_conn
                if conn.in_transaction:
                    conn.commit()
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                except BaseException:
                    conn.rollback()
                    raise
                else:
                    conn.commit()
            return
        conn = self._open_file_conn(autocommit=True)
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            else:
                conn.execute("COMMIT")
        finally:
            conn.close()

    def close(self) -> None:
        if self._shared_conn is not None:
            with self._lock:
                self._shared_conn.close()
                self._shared_conn = None

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
                    subscribed_topics TEXT DEFAULT 'TRAVEL',
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

            # 8. Travel price watches (settings only; delivery state lives in the ledger below)
            db.execute(
                """CREATE TABLE IF NOT EXISTS travel_price_watches (
                    watch_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id TEXT NOT NULL,
                    dest_code TEXT NOT NULL,
                    target_price_krw INTEGER NOT NULL,
                    preset_kind TEXT NOT NULL DEFAULT 'DEAL',
                    day_pattern TEXT NOT NULL DEFAULT 'ANY',
                    include_peak INTEGER NOT NULL DEFAULT 1,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    last_seen_price_krw INTEGER,
                    last_seen_slot TEXT,
                    last_seen_at TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(chat_id, dest_code)
                )"""
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_travel_watches_active ON travel_price_watches (is_active, dest_code)"
            )

            # 9. Watch alert delivery ledger (SSOT for SENT/UNKNOWN, keyed by watch_id)
            db.execute(
                """CREATE TABLE IF NOT EXISTS travel_watch_deliveries (
                    delivery_key TEXT PRIMARY KEY,
                    watch_id INTEGER NOT NULL,
                    chat_id TEXT NOT NULL,
                    dest_code TEXT NOT NULL,
                    dep_date TEXT NOT NULL,
                    ret_date TEXT NOT NULL,
                    price_krw INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    lease_token TEXT,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    attempt_no INTEGER NOT NULL DEFAULT 1,
                    provider_msg_id INTEGER,
                    error_detail TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )"""
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_travel_watch_deliveries_watch ON travel_watch_deliveries (watch_id, created_at)"
            )

            # 10. Singleton tick lease (manager vs cron cross-process guard)
            db.execute(
                """CREATE TABLE IF NOT EXISTS travel_watch_tick_leases (
                    lease_name TEXT PRIMARY KEY,
                    lease_token TEXT NOT NULL,
                    lease_owner TEXT NOT NULL,
                    acquired_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                )"""
            )

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
                                           VALUES (?, ?, 'TELEGRAM', 'DEFAULT', 'TRAVEL', 1)""",
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

    def get_all_recipients(self, topic: str | None = None) -> list[Recipient]:
        with self._connect() as db:
            if topic:
                rows = db.execute("SELECT * FROM recipients WHERE subscribed_topics LIKE ? ORDER BY created_at DESC", (f"%{topic}%",)).fetchall()
            else:
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
        subscribed_topics: str = "TRAVEL",
        topic: str | None = None,
    ) -> bool:
        effective_topic = (topic or subscribed_topics).strip().upper()
        cid = str(chat_id).strip()
        clean_name = name.strip()
        with self._connect() as db:
            row = db.execute("SELECT subscribed_topics FROM recipients WHERE chat_id = ?", (cid,)).fetchone()
            if row:
                existing_topics = set(t.strip() for t in (row["subscribed_topics"] or "").split(",") if t.strip())
                for t in effective_topic.split(","):
                    if t.strip():
                        existing_topics.add(t.strip().upper())
                merged_topics = ",".join(sorted(existing_topics))
                cursor = db.execute(
                    "UPDATE recipients SET is_active = 1, subscribed_topics = ?, inactivation_reason = NULL WHERE chat_id = ?",
                    (merged_topics, cid),
                )
            else:
                cursor = db.execute(
                    """INSERT INTO recipients
                       (chat_id, name, channel, group_tag, subscribed_topics, is_active, inactivation_reason)
                       VALUES (?, ?, ?, ?, ?, 1, NULL)""",
                    (cid, clean_name, channel, group_tag, effective_topic),
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
    # Travel Price Watches
    # =========================================================================

    def upsert_travel_watch(
        self,
        chat_id: str,
        dest_code: str,
        target_price_krw: int,
        preset_kind: str = "DEAL",
        day_pattern: Optional[str] = None,
        include_peak: Optional[int] = None,
        max_active: int = WATCH_MAX_ACTIVE_PER_CHAT,
    ) -> dict[str, Any]:
        """Creates/updates/re-activates the `(chat_id, dest_code)` watch atomically.
        `day_pattern` / `include_peak` default to ANY / 1 for new watches and keep existing toggles otherwise.
        Raises `WatchLimitError` when a new or re-activated watch would exceed `max_active`.
        """
        cid = str(chat_id).strip()
        code = (dest_code or "").strip().upper()
        target = int(target_price_krw)
        kind = (preset_kind or "DEAL").upper()
        if not cid or not code or code == "ALL":
            raise ValueError("watch requires a chat_id and a concrete destination code")
        if target <= 0:
            raise ValueError("target_price_krw must be positive")
        if kind not in WATCH_PRESET_KINDS:
            raise ValueError(f"invalid preset_kind: {preset_kind}")
        if day_pattern is not None and day_pattern not in WATCH_DAY_PATTERNS:
            raise ValueError(f"invalid day_pattern: {day_pattern}")
        now = _utc_str()

        with self._immediate_tx() as db:
            row = db.execute(
                "SELECT * FROM travel_price_watches WHERE chat_id = ? AND dest_code = ?", (cid, code)
            ).fetchone()
            if row is None or not row["is_active"]:
                active = db.execute(
                    "SELECT COUNT(*) FROM travel_price_watches WHERE chat_id = ? AND is_active = 1", (cid,)
                ).fetchone()[0]
                if active >= max_active:
                    raise WatchLimitError(max_active, active)
            if row is None:
                db.execute(
                    """INSERT INTO travel_price_watches
                       (chat_id, dest_code, target_price_krw, preset_kind, day_pattern, include_peak,
                        is_active, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)""",
                    (cid, code, target, kind, day_pattern or "ANY",
                     1 if include_peak is None else (1 if include_peak else 0), now, now),
                )
            else:
                db.execute(
                    """UPDATE travel_price_watches
                       SET target_price_krw = ?, preset_kind = ?, day_pattern = ?, include_peak = ?,
                           is_active = 1, updated_at = ?
                       WHERE watch_id = ?""",
                    (target, kind, day_pattern or row["day_pattern"],
                     row["include_peak"] if include_peak is None else (1 if include_peak else 0),
                     now, row["watch_id"]),
                )
            out = db.execute(
                "SELECT * FROM travel_price_watches WHERE chat_id = ? AND dest_code = ?", (cid, code)
            ).fetchone()
            return dict(out)

    def list_travel_watches(self, chat_id: Optional[str] = None, active_only: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM travel_price_watches WHERE 1 = 1"
        params: list[Any] = []
        if chat_id is not None:
            sql += " AND chat_id = ?"
            params.append(str(chat_id).strip())
        if active_only:
            sql += " AND is_active = 1"
        sql += " ORDER BY created_at ASC, watch_id ASC"
        with self._connect() as db:
            return [dict(r) for r in db.execute(sql, params).fetchall()]

    def get_travel_watch(self, chat_id: str, dest_code: str) -> Optional[dict[str, Any]]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM travel_price_watches WHERE chat_id = ? AND dest_code = ?",
                (str(chat_id).strip(), (dest_code or "").strip().upper()),
            ).fetchone()
            return dict(row) if row else None

    def deactivate_travel_watch(self, chat_id: str, dest_code: str) -> bool:
        with self._immediate_tx() as db:
            cur = db.execute(
                """UPDATE travel_price_watches SET is_active = 0, updated_at = ?
                   WHERE chat_id = ? AND dest_code = ? AND is_active = 1""",
                (_utc_str(), str(chat_id).strip(), (dest_code or "").strip().upper()),
            )
            return cur.rowcount > 0

    @staticmethod
    def _deactivate_watches_in_tx(conn: sqlite3.Connection, chat_id: str) -> int:
        cur = conn.execute(
            "UPDATE travel_price_watches SET is_active = 0, updated_at = ? WHERE chat_id = ? AND is_active = 1",
            (_utc_str(), str(chat_id).strip()),
        )
        return cur.rowcount

    def deactivate_all_watches_for_chat(self, chat_id: str) -> int:
        with self._immediate_tx() as db:
            return self._deactivate_watches_in_tx(db, chat_id)

    def deactivate_recipient_and_watches(
        self,
        chat_id: str,
        reason: str = "USER_STOP_COMMAND",
        topic: str = "TRAVEL",
        cancel_subscription: bool = False,
        bot_id: str = TRAVEL_BOT_ID,
    ) -> dict[str, Any]:
        """Travel `/stop` in ONE `BEGIN IMMEDIATE` transaction: removes the TRAVEL topic from the recipient
        (`is_active=0` once no other topic remains, so a shared FINANCE subscriber keeps their market brief)
        and deactivates every travel watch for the chat. With `cancel_subscription=True` (standalone runner)
        an existing platform `subscriptions` row is cancelled in the same transaction; the manager path
        leaves it to `SubscriptionEngine.unsubscribe_bot` (quota / audit reconciliation).
        """
        cid = str(chat_id).strip()
        drop = topic.strip().upper()
        recipient_deactivated = False
        subscription_cancelled = False
        with self._immediate_tx() as db:
            if cancel_subscription and db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'subscriptions'"
            ).fetchone():
                kst_now = (datetime.now(timezone.utc) + timedelta(hours=9)).strftime("%Y-%m-%d %H:%M:%S")
                cur = db.execute(
                    """UPDATE subscriptions SET status = 'CANCELLED', cancelled_at = ?
                       WHERE telegram_user_id = ? AND bot_id = ? AND status != 'CANCELLED'""",
                    (kst_now, cid, bot_id),
                )
                subscription_cancelled = cur.rowcount > 0
                if subscription_cancelled and db.execute(
                    "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'subscription_audit_logs'"
                ).fetchone():
                    db.execute(
                        """INSERT INTO subscription_audit_logs
                           (telegram_user_id, bot_id, action, actor, detail, logged_at)
                           VALUES (?, ?, 'UNSUBSCRIBE', 'USER', ?, ?)""",
                        (cid, bot_id, json.dumps({"reason": reason}, ensure_ascii=False), kst_now),
                    )
            row = db.execute("SELECT subscribed_topics FROM recipients WHERE chat_id = ?", (cid,)).fetchone()
            if row is not None:
                remaining = [t for t in _topics_of(row["subscribed_topics"]) if t != drop]
                if remaining:
                    db.execute(
                        "UPDATE recipients SET subscribed_topics = ? WHERE chat_id = ?",
                        (",".join(remaining), cid),
                    )
                else:
                    db.execute(
                        "UPDATE recipients SET is_active = 0, inactivation_reason = ? WHERE chat_id = ?",
                        (reason, cid),
                    )
                    recipient_deactivated = True
            watches = self._deactivate_watches_in_tx(db, cid)
        return {
            "recipient_found": row is not None,
            "recipient_deactivated": recipient_deactivated,
            "watches_deactivated": watches,
            "subscription_cancelled": subscription_cancelled,
        }

    def toggle_travel_watch_option(self, chat_id: str, dest_code: str, field: str) -> Optional[dict[str, Any]]:
        """`day_pattern`: ANY -> FRI_MON -> THU_SUN -> ANY; `include_peak`: 1 <-> 0. Active watches only."""
        key = {"day": "day_pattern", "pk": "include_peak"}.get(field, field)
        if key not in ("day_pattern", "include_peak"):
            raise ValueError(f"invalid watch option: {field}")
        cid = str(chat_id).strip()
        code = (dest_code or "").strip().upper()
        with self._immediate_tx() as db:
            row = db.execute(
                "SELECT * FROM travel_price_watches WHERE chat_id = ? AND dest_code = ? AND is_active = 1",
                (cid, code),
            ).fetchone()
            if row is None:
                return None
            if key == "day_pattern":
                cur = row["day_pattern"] if row["day_pattern"] in WATCH_DAY_PATTERNS else "ANY"
                value: Any = WATCH_DAY_PATTERNS[(WATCH_DAY_PATTERNS.index(cur) + 1) % len(WATCH_DAY_PATTERNS)]
            else:
                value = 0 if row["include_peak"] else 1
            db.execute(
                f"UPDATE travel_price_watches SET {key} = ?, updated_at = ? WHERE watch_id = ?",
                (value, _utc_str(), row["watch_id"]),
            )
            return dict(db.execute(
                "SELECT * FROM travel_price_watches WHERE watch_id = ?", (row["watch_id"],)
            ).fetchone())

    def update_watch_observation(
        self, watch_id: int, price_krw: int, slot_key: str, observed_at: Optional[str] = None
    ) -> None:
        with self._connect() as db:
            db.execute(
                """UPDATE travel_price_watches
                   SET last_seen_price_krw = ?, last_seen_slot = ?, last_seen_at = ?
                   WHERE watch_id = ?""",
                (int(price_krw), slot_key, observed_at or _utc_str(), int(watch_id)),
            )

    def list_deliverable_watches(self, bot_id: str = TRAVEL_BOT_ID) -> tuple[list[dict[str, Any]], bool]:
        """Subscription SSOT filter: active watch AND active TRAVEL recipient AND (if the platform
        `subscriptions` table lives in this DB) an ACTIVE `travel_bot` subscription.
        Returns (watches, subscriptions_checked).
        """
        with self._connect() as db:
            has_subs = db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'subscriptions'"
            ).fetchone() is not None
            sql = """SELECT w.*, r.subscribed_topics AS recipient_topics
                     FROM travel_price_watches w
                     JOIN recipients r ON r.chat_id = w.chat_id
                     WHERE w.is_active = 1 AND r.is_active = 1"""
            params: list[Any] = []
            if has_subs:
                sql += """ AND EXISTS (
                             SELECT 1 FROM subscriptions s
                             WHERE s.telegram_user_id = w.chat_id AND s.bot_id = ? AND s.status = 'ACTIVE')"""
                params.append(bot_id)
            sql += " ORDER BY w.dest_code ASC, w.watch_id ASC"
            rows = db.execute(sql, params).fetchall()
        out = []
        for r in rows:
            topics = _topics_of(r["recipient_topics"])
            if "TRAVEL" in topics or "ALL" in topics:
                out.append(dict(r))
        return out, has_subs

    # =========================================================================
    # Watch Delivery Ledger & Leases
    # =========================================================================

    @staticmethod
    def _expire_stale_in_tx(conn: sqlite3.Connection, now_str: str) -> int:
        cur = conn.execute(
            """UPDATE travel_watch_deliveries
               SET status = 'UNKNOWN', updated_at = ?,
                   error_detail = COALESCE(error_detail, 'LEASE_EXPIRED_QUARANTINED')
               WHERE status = 'IN_FLIGHT' AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?""",
            (now_str, now_str),
        )
        return cur.rowcount

    def expire_stale_watch_in_flight(self, now_iso: Optional[str] = None) -> int:
        """Quarantines expired IN_FLIGHT claims as UNKNOWN (never auto-retried, [PROD-LIVE])."""
        with self._immediate_tx() as db:
            return self._expire_stale_in_tx(db, now_iso or _utc_str())

    @staticmethod
    def _blocking_rows_in_tx(conn: sqlite3.Connection, watch_id: int, since_str: str) -> list[dict[str, Any]]:
        placeholders = ",".join("?" for _ in WATCH_BLOCKING_STATUSES)
        rows = conn.execute(
            f"""SELECT dep_date, ret_date, price_krw, status FROM travel_watch_deliveries
                WHERE watch_id = ? AND status IN ({placeholders})
                  AND (created_at >= ? OR status = 'IN_FLIGHT')""",
            (int(watch_id), *WATCH_BLOCKING_STATUSES, since_str),
        ).fetchall()
        return [dict(r) for r in rows]

    def recent_blocking_watch_deliveries(
        self, watch_id: int, days: int = WATCH_COOLDOWN_DAYS, now: Optional[datetime] = None
    ) -> list[dict[str, Any]]:
        """Read-only view of the rows `claim_watch_delivery` evaluates (used for dry-run previews)."""
        since = _utc_str((now or datetime.now(timezone.utc)) - timedelta(days=days))
        with self._connect() as db:
            return self._blocking_rows_in_tx(db, watch_id, since)

    def is_watch_delivery_blocked(self, delivery_key: str) -> Optional[str]:
        row = self.get_watch_delivery(delivery_key)
        if row is not None and row["status"] != "FAILED":
            return f"duplicate_{row['status'].lower()}"
        return None

    def get_watch_delivery(self, delivery_key: str) -> Optional[dict[str, Any]]:
        with self._connect() as db:
            row = db.execute(
                "SELECT * FROM travel_watch_deliveries WHERE delivery_key = ?", (delivery_key,)
            ).fetchone()
            return dict(row) if row else None

    def claim_watch_delivery(
        self,
        watch_id: int,
        chat_id: str,
        dest_code: str,
        dep_date: str,
        ret_date: str,
        price_krw: int,
        lease_owner: str,
        lease_ttl_seconds: int = 300,
        cooldown_days: int = WATCH_COOLDOWN_DAYS,
        min_drop_krw: int = WATCH_MIN_REDROP_KRW,
        now: Optional[datetime] = None,
    ) -> WatchClaim:
        """Atomically claims an IN_FLIGHT lease for one watch alert.
        Rules (inside one `BEGIN IMMEDIATE`):
        1. Expired IN_FLIGHT rows are quarantined to UNKNOWN first.
        2. Same delivery_key already SENT / UNKNOWN / IN_FLIGHT -> rejected (only FAILED may be re-claimed).
        3. Within `cooldown_days` of any SENT/UNKNOWN/IN_FLIGHT for this watch: the same (dep, ret) slot is
           rejected, and a new slot must be at least `min_drop_krw` cheaper than the cheapest recent alert.
        """
        now_dt = now or datetime.now(timezone.utc)
        now_str = _utc_str(now_dt)
        since_str = _utc_str(now_dt - timedelta(days=cooldown_days))
        key = make_watch_delivery_key(watch_id, chat_id, dest_code, dep_date, ret_date, price_krw)
        price = int(price_krw)

        with self._immediate_tx() as db:
            self._expire_stale_in_tx(db, now_str)
            existing = db.execute(
                "SELECT * FROM travel_watch_deliveries WHERE delivery_key = ?", (key,)
            ).fetchone()
            if existing is not None and existing["status"] != "FAILED":
                return WatchClaim(False, f"duplicate_{existing['status'].lower()}", key)

            recent = self._blocking_rows_in_tx(db, watch_id, since_str)
            blocked = watch_cooldown_block_reason(recent, dep_date, ret_date, price, min_drop_krw)
            if blocked:
                return WatchClaim(False, blocked, key)

            token = uuid.uuid4().hex
            expires = _utc_str(now_dt + timedelta(seconds=lease_ttl_seconds))
            if existing is None:
                attempt_no = 1
                db.execute(
                    """INSERT INTO travel_watch_deliveries
                       (delivery_key, watch_id, chat_id, dest_code, dep_date, ret_date, price_krw, status,
                        lease_token, lease_owner, lease_expires_at, attempt_no, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'IN_FLIGHT', ?, ?, ?, 1, ?, ?)""",
                    (key, int(watch_id), str(chat_id).strip(), dest_code.upper(), dep_date, ret_date, price,
                     token, lease_owner, expires, now_str, now_str),
                )
            else:
                attempt_no = int(existing["attempt_no"] or 1) + 1
                cur = db.execute(
                    """UPDATE travel_watch_deliveries
                       SET status = 'IN_FLIGHT', lease_token = ?, lease_owner = ?, lease_expires_at = ?,
                           attempt_no = ?, error_detail = NULL, provider_msg_id = NULL,
                           created_at = ?, updated_at = ?
                       WHERE delivery_key = ? AND status = 'FAILED'""",
                    (token, lease_owner, expires, attempt_no, now_str, now_str, key),
                )
                if cur.rowcount == 0:
                    return WatchClaim(False, "concurrent_claim_conflict", key)
            return WatchClaim(True, "claimed", key, token, attempt_no)

    def finalize_watch_delivery(
        self,
        delivery_key: str,
        lease_token: str,
        status: str,
        provider_msg_id: Any = None,
        error_detail: Optional[str] = None,
    ) -> bool:
        """IN_FLIGHT -> SENT/UNKNOWN/FAILED, only for the lease holder. A lease that was already quarantined
        to UNKNOWN may still be upgraded to SENT by its holder (never downgraded or retried)."""
        final = (status or "").upper()
        if final not in ("SENT", "UNKNOWN", "FAILED"):
            raise ValueError(f"invalid final status: {status}")
        try:
            msg_id = int(provider_msg_id) if provider_msg_id is not None else None
        except (TypeError, ValueError):
            msg_id = None
        allowed_from = ("IN_FLIGHT", "UNKNOWN") if final == "SENT" else ("IN_FLIGHT",)
        with self._immediate_tx() as db:
            cur = db.execute(
                f"""UPDATE travel_watch_deliveries
                    SET status = ?, provider_msg_id = ?, error_detail = ?, updated_at = ?, lease_expires_at = NULL
                    WHERE delivery_key = ? AND lease_token = ? AND status IN ({",".join("?" for _ in allowed_from)})""",
                (final, msg_id, (error_detail or None) and str(error_detail)[:500], _utc_str(),
                 delivery_key, lease_token, *allowed_from),
            )
            return cur.rowcount > 0

    def acquire_watch_tick_lease(
        self, lease_name: str, lease_owner: str, ttl_seconds: int = 600, now: Optional[datetime] = None
    ) -> Optional[str]:
        """Singleton tick lease shared by every process on this DB. Returns the token, or None if held."""
        now_dt = now or datetime.now(timezone.utc)
        now_str = _utc_str(now_dt)
        with self._immediate_tx() as db:
            row = db.execute(
                "SELECT * FROM travel_watch_tick_leases WHERE lease_name = ?", (lease_name,)
            ).fetchone()
            if row is not None and row["expires_at"] > now_str:
                return None
            token = uuid.uuid4().hex
            db.execute(
                """INSERT OR REPLACE INTO travel_watch_tick_leases
                   (lease_name, lease_token, lease_owner, acquired_at, expires_at)
                   VALUES (?, ?, ?, ?, ?)""",
                (lease_name, token, lease_owner, now_str, _utc_str(now_dt + timedelta(seconds=ttl_seconds))),
            )
            return token

    def release_watch_tick_lease(self, lease_name: str, lease_token: str) -> bool:
        with self._immediate_tx() as db:
            cur = db.execute(
                "DELETE FROM travel_watch_tick_leases WHERE lease_name = ? AND lease_token = ?",
                (lease_name, lease_token),
            )
            return cur.rowcount > 0

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


MarketBriefDatabase = TravelBriefDatabase
