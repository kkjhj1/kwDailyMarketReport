"""SQLite database manager for Admission Briefing.
Manages parent subscribers, target grades, regions, admission events, and dispatch logs.
"""
from __future__ import annotations
import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from admission_brief.models import (
    AdmissionEvent,
    BriefingType,
    ParentUserProfile,
    TargetGrade,
    TargetRegion,
)


class AdmissionDatabase:
    """Thread-safe SQLite storage for Admission Briefing."""

    def __init__(self, db_path: str | Path = "data/admission_brief.db"):
        self.db_path = Path(db_path)
        self._is_memory = str(self.db_path) == ":memory:"
        self._shared_conn: Optional[sqlite3.Connection] = None
        if not self._is_memory:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            self._shared_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._shared_conn.row_factory = sqlite3.Row
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        if self._is_memory and self._shared_conn:
            return self._shared_conn
        conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            cursor = conn.cursor()
            # 1. Subscribers table
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS admission_subscribers (
                    chat_id TEXT PRIMARY KEY,
                    target_grade TEXT NOT NULL DEFAULT 'ALL',
                    target_region TEXT NOT NULL DEFAULT 'ALL',
                    username TEXT,
                    first_name TEXT,
                    alert_enabled INTEGER NOT NULL DEFAULT 1,
                    registered_at TEXT NOT NULL,
                    last_active_at TEXT NOT NULL
                )
                """
            )
            # Safe migration for existing DB
            try:
                cursor.execute("ALTER TABLE admission_subscribers ADD COLUMN target_region TEXT NOT NULL DEFAULT 'ALL'")
            except sqlite3.OperationalError:
                pass

            # 2. Admission events table
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS admission_events (
                    event_id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    organizer TEXT NOT NULL,
                    target_grade TEXT NOT NULL,
                    target_region TEXT NOT NULL DEFAULT 'ALL',
                    is_online_live INTEGER NOT NULL DEFAULT 0,
                    event_type TEXT NOT NULL,
                    event_date_str TEXT NOT NULL,
                    booking_opens_at TEXT,
                    is_first_come INTEGER NOT NULL DEFAULT 0,
                    booking_url TEXT,
                    summary_3lines_json TEXT,
                    so_what TEXT,
                    action_todos_json TEXT,
                    pdf_attachment_path TEXT,
                    credibility_score INTEGER NOT NULL DEFAULT 80,
                    status TEXT NOT NULL DEFAULT 'SCHEDULED',
                    created_at TEXT NOT NULL
                )
                """
            )
            try:
                cursor.execute("ALTER TABLE admission_events ADD COLUMN target_region TEXT NOT NULL DEFAULT 'ALL'")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE admission_events ADD COLUMN is_online_live INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass

            # 3. Dispatch log table (prevents duplicated alerts)
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS admission_dispatch_logs (
                    log_id TEXT PRIMARY KEY,
                    briefing_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    dispatched_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'SUCCESS',
                    UNIQUE(briefing_id, chat_id)
                )
                """
            )
            conn.commit()

    # --- Subscriber Methods ---

    def upsert_subscriber(self, profile: ParentUserProfile) -> None:
        """Inserts or updates parent profile."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO admission_subscribers (
                    chat_id, target_grade, target_region, username, first_name, alert_enabled, registered_at, last_active_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    username = excluded.username,
                    first_name = excluded.first_name,
                    last_active_at = excluded.last_active_at
                """,
                (
                    str(profile.chat_id),
                    profile.target_grade.value,
                    profile.target_region.value,
                    profile.username,
                    profile.first_name,
                    1 if profile.alert_enabled else 0,
                    profile.registered_at.isoformat(),
                    profile.last_active_at.isoformat(),
                ),
            )
            conn.commit()

    def update_subscriber_grade(self, chat_id: str, grade: TargetGrade) -> bool:
        """Updates target grade for a parent."""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE admission_subscribers
                SET target_grade = ?, last_active_at = ?
                WHERE chat_id = ?
                """,
                (grade.value, now, str(chat_id)),
            )
            conn.commit()
            return cursor.rowcount > 0

    def update_subscriber_region(self, chat_id: str, region: TargetRegion) -> bool:
        """Updates target region for a parent."""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE admission_subscribers
                SET target_region = ?, last_active_at = ?
                WHERE chat_id = ?
                """,
                (region.value, now, str(chat_id)),
            )
            conn.commit()
            return cursor.rowcount > 0

    def get_subscriber(self, chat_id: str) -> Optional[ParentUserProfile]:
        """Retrieves subscriber profile by chat_id."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM admission_subscribers WHERE chat_id = ?",
                (str(chat_id),),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return ParentUserProfile(
                chat_id=row["chat_id"],
                target_grade=TargetGrade(row["target_grade"]),
                target_region=TargetRegion(row["target_region"] if "target_region" in row.keys() else "ALL"),
                username=row["username"] or "",
                first_name=row["first_name"] or "",
                alert_enabled=bool(row["alert_enabled"]),
                registered_at=datetime.fromisoformat(row["registered_at"]),
                last_active_at=datetime.fromisoformat(row["last_active_at"]),
            )

    def list_subscribers(
        self,
        grade: Optional[TargetGrade] = None,
        region: Optional[TargetRegion] = None,
        is_online_live: bool = False,
        active_only: bool = True,
    ) -> list[ParentUserProfile]:
        """Lists subscribers filtered by target grade and region."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT * FROM admission_subscribers WHERE 1=1"
            params: list[object] = []

            if active_only:
                query += " AND alert_enabled = 1"

            cursor.execute(query, params)
            rows = cursor.fetchall()

            results = []
            for r in rows:
                p_grade = TargetGrade(r["target_grade"])
                p_reg = TargetRegion(r["target_region"] if "target_region" in r.keys() else "ALL")

                # 1. Grade match check
                if grade and grade != TargetGrade.ALL:
                    if p_grade != TargetGrade.ALL and p_grade != grade:
                        continue

                # 2. Region match check
                if region and region != TargetRegion.ALL:
                    if p_reg == TargetRegion.ONLINE:
                        if not is_online_live:
                            continue
                    elif p_reg != TargetRegion.ALL:
                        if not is_online_live and p_reg != region:
                            continue

                results.append(
                    ParentUserProfile(
                        chat_id=r["chat_id"],
                        target_grade=p_grade,
                        target_region=p_reg,
                        username=r["username"] or "",
                        first_name=r["first_name"] or "",
                        alert_enabled=bool(r["alert_enabled"]),
                        registered_at=datetime.fromisoformat(r["registered_at"]),
                        last_active_at=datetime.fromisoformat(r["last_active_at"]),
                    )
                )

            return results

    # --- Event Methods ---

    def save_event(self, event: AdmissionEvent) -> None:
        """Saves or updates an admission event."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO admission_events (
                    event_id, title, organizer, target_grade, target_region, is_online_live,
                    event_type, event_date_str, booking_opens_at, is_first_come, booking_url,
                    summary_3lines_json, so_what, action_todos_json, pdf_attachment_path,
                    credibility_score, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SCHEDULED', ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    title = excluded.title,
                    organizer = excluded.organizer,
                    target_grade = excluded.target_grade,
                    target_region = excluded.target_region,
                    is_online_live = excluded.is_online_live,
                    booking_opens_at = excluded.booking_opens_at,
                    booking_url = excluded.booking_url,
                    summary_3lines_json = excluded.summary_3lines_json,
                    so_what = excluded.so_what,
                    action_todos_json = excluded.action_todos_json,
                    pdf_attachment_path = excluded.pdf_attachment_path
                """,
                (
                    event.event_id,
                    event.title,
                    event.organizer,
                    event.target_grade.value,
                    event.target_region.value,
                    1 if event.is_online_live else 0,
                    event.event_type.value,
                    event.event_date_str,
                    event.booking_opens_at.isoformat() if event.booking_opens_at else None,
                    1 if event.is_first_come else 0,
                    event.booking_url,
                    json.dumps(event.summary_3lines, ensure_ascii=False),
                    event.so_what,
                    json.dumps(event.action_todos, ensure_ascii=False),
                    event.pdf_attachment_path,
                    event.credibility_score,
                    event.created_at.isoformat(),
                ),
            )
            conn.commit()

    def get_event(self, event_id: str) -> Optional[AdmissionEvent]:
        """Fetches single event by event_id."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM admission_events WHERE event_id = ?", (event_id,))
            row = cursor.fetchone()
            if not row:
                return None
            return AdmissionEvent(
                event_id=row["event_id"],
                title=row["title"],
                organizer=row["organizer"],
                target_grade=TargetGrade(row["target_grade"]),
                target_region=TargetRegion(row["target_region"] if "target_region" in row.keys() else "ALL"),
                is_online_live=bool(row["is_online_live"] if "is_online_live" in row.keys() else 0),
                event_type=BriefingType(row["event_type"]),
                event_date_str=row["event_date_str"],
                booking_opens_at=datetime.fromisoformat(row["booking_opens_at"]) if row["booking_opens_at"] else None,
                is_first_come=bool(row["is_first_come"]),
                booking_url=row["booking_url"],
                summary_3lines=json.loads(row["summary_3lines_json"] or "[]"),
                so_what=row["so_what"] or "",
                action_todos=json.loads(row["action_todos_json"] or "[]"),
                pdf_attachment_path=row["pdf_attachment_path"],
                credibility_score=row["credibility_score"],
                created_at=datetime.fromisoformat(row["created_at"]),
            )

    def list_upcoming_ticketing(self) -> list[AdmissionEvent]:
        """Lists active ticketing events whose booking opens soon and is first-come."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM admission_events
                WHERE is_first_come = 1 AND booking_url IS NOT NULL
                ORDER BY created_at DESC
                """
            )
            rows = cursor.fetchall()
            events = []
            for r in rows:
                events.append(
                    AdmissionEvent(
                        event_id=r["event_id"],
                        title=r["title"],
                        organizer=r["organizer"],
                        target_grade=TargetGrade(r["target_grade"]),
                        target_region=TargetRegion(r["target_region"] if "target_region" in r.keys() else "ALL"),
                        is_online_live=bool(r["is_online_live"] if "is_online_live" in r.keys() else 0),
                        event_type=BriefingType(r["event_type"]),
                        event_date_str=r["event_date_str"],
                        booking_opens_at=datetime.fromisoformat(r["booking_opens_at"]) if r["booking_opens_at"] else None,
                        is_first_come=bool(r["is_first_come"]),
                        booking_url=r["booking_url"],
                        summary_3lines=json.loads(r["summary_3lines_json"] or "[]"),
                        so_what=r["so_what"] or "",
                        action_todos=json.loads(r["action_todos_json"] or "[]"),
                        pdf_attachment_path=r["pdf_attachment_path"],
                        credibility_score=r["credibility_score"],
                        created_at=datetime.fromisoformat(r["created_at"]),
                    )
                )
            return events

    def list_all_events(self) -> list[AdmissionEvent]:
        """Lists all registered admission events."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM admission_events ORDER BY created_at DESC")
            rows = cursor.fetchall()
            return [
                AdmissionEvent(
                    event_id=r["event_id"],
                    title=r["title"],
                    organizer=r["organizer"],
                    target_grade=TargetGrade(r["target_grade"]),
                    target_region=TargetRegion(r["target_region"] if "target_region" in r.keys() else "ALL"),
                    is_online_live=bool(r["is_online_live"] if "is_online_live" in r.keys() else 0),
                    event_type=BriefingType(r["event_type"]),
                    event_date_str=r["event_date_str"],
                    booking_opens_at=datetime.fromisoformat(r["booking_opens_at"]) if r["booking_opens_at"] else None,
                    is_first_come=bool(r["is_first_come"]),
                    booking_url=r["booking_url"],
                    summary_3lines=json.loads(r["summary_3lines_json"] or "[]"),
                    so_what=r["so_what"] or "",
                    action_todos=json.loads(r["action_todos_json"] or "[]"),
                    pdf_attachment_path=r["pdf_attachment_path"],
                    credibility_score=r["credibility_score"],
                    created_at=datetime.fromisoformat(r["created_at"]),
                )
                for r in rows
            ]

    # --- Dispatch Log Methods ---

    def log_dispatch(self, briefing_id: str, chat_id: str, status: str = "SUCCESS") -> None:
        """Records successful or failed dispatch to prevent re-sending."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO admission_dispatch_logs (
                    log_id, briefing_id, chat_id, dispatched_at, status
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(uuid.uuid4()),
                    briefing_id,
                    str(chat_id),
                    datetime.now().isoformat(),
                    status,
                ),
            )
            conn.commit()

    def has_dispatched(self, briefing_id: str, chat_id: str) -> bool:
        """Checks if this specific briefing was already dispatched to this user."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM admission_dispatch_logs WHERE briefing_id = ? AND chat_id = ? AND status = 'SUCCESS'",
                (briefing_id, str(chat_id)),
            )
            return cursor.fetchone() is not None
