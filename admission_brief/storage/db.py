"""SQLite database manager for Admission Briefing.
Manages parent subscribers, target grades, regions, admission events, and dispatch logs.
"""
from __future__ import annotations
import json
import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

from admission_brief.models import (
    MAX_CHILDREN,
    AdmissionEvent,
    BriefingType,
    ChildProfile,
    EducationStage,
    EventCategory,
    GradeLevel,
    GradeMode,
    ParentUserProfile,
    TargetGrade,
    TargetRegion,
    legacy_target_grade_to_stage_level,
    stage_level_to_legacy_target_grade,
)

KST = ZoneInfo("Asia/Seoul")


def resolve_admission_db_path(db_path: str | Path | None = None) -> str:
    """Resolve the admission database without overriding explicit test/temporary paths."""
    if db_path is not None:
        return str(db_path)
    return os.environ.get("ADMISSION_DB_PATH", "").strip() or "alerts.sqlite3"


def _platform_active_subscriber_ids(
    db_path: str, bot_ids: tuple[str, ...], conn: Optional[sqlite3.Connection] = None
) -> Optional[set[str]]:
    """Platform subscription SSOT for outbound delivery (same DB or sibling `alerts.sqlite3`).
    None -> no platform `subscriptions` table reachable (standalone / `:memory:`): caller applies no filter.
    set  -> chat ids holding an ACTIVE subscription to `bot_ids` AND an ACTIVE/GRACE_PERIOD platform user.
    Fail-closed: a platform DB that exists but cannot be read yields an empty set."""

    def _query(c: sqlite3.Connection) -> Optional[set[str]]:
        tables = {
            r[0] for r in c.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('subscriptions', 'users')"
            ).fetchall()
        }
        if "subscriptions" not in tables:
            return None
        marks = ",".join("?" * len(bot_ids))
        sql = f"SELECT DISTINCT s.telegram_user_id FROM subscriptions s WHERE s.status IN ('ACTIVE', 'GRACE_PERIOD') AND s.bot_id IN ({marks})"
        if "users" in tables:
            sql += (
                " AND EXISTS (SELECT 1 FROM users u"
                " WHERE u.telegram_user_id = s.telegram_user_id AND u.status IN ('ACTIVE', 'GRACE_PERIOD'))"
            )
        return {str(r[0]).strip() for r in c.execute(sql, tuple(bot_ids)).fetchall()}

    try:
        if conn is not None:
            found = _query(conn)
            if found is not None:
                return found
        path = str(db_path or "").strip()
        if not path or path == ":memory:" or path.startswith("file:"):
            return None
        own = Path(path).resolve()
        # An explicit admission DB used by tests or isolated tools must not inherit a
        # sibling platform DB; unified production deployments use the same alerts DB.
        candidates = [] if conn is not None else [own]
        if conn is None:
            sibling = own.with_name("alerts.sqlite3")
            if sibling != own:
                candidates.append(sibling)
        for cand in candidates:
            if not cand.is_file():
                continue
            ext = sqlite3.connect(str(cand), timeout=5.0)
            try:
                found = _query(ext)
            finally:
                ext.close()
            if found is not None:
                return found
        return None
    except Exception:
        return set()


class AdmissionDatabase:
    """Thread-safe SQLite storage for Admission Briefing."""

    def __init__(self, db_path: str | Path | None = None):
        resolved_path = resolve_admission_db_path(db_path)
        self.db_path = Path(resolved_path)
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
                    education_stage TEXT NOT NULL DEFAULT 'ALL',
                    grade_level TEXT NOT NULL DEFAULT 'ALL',
                    username TEXT,
                    first_name TEXT,
                    alert_enabled INTEGER NOT NULL DEFAULT 1,
                    weekly_guide_enabled INTEGER NOT NULL DEFAULT 1,
                    event_dday_enabled INTEGER NOT NULL DEFAULT 1,
                    policy_breaking_enabled INTEGER NOT NULL DEFAULT 1,
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
            try:
                cursor.execute("ALTER TABLE admission_subscribers ADD COLUMN children_json TEXT NOT NULL DEFAULT '[]'")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE admission_subscribers ADD COLUMN active_child_index INTEGER NOT NULL DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE admission_subscribers ADD COLUMN education_stage TEXT NOT NULL DEFAULT 'ALL'")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE admission_subscribers ADD COLUMN grade_level TEXT NOT NULL DEFAULT 'ALL'")
            except sqlite3.OperationalError:
                pass
            for column in ("weekly_guide_enabled", "event_dday_enabled", "policy_breaking_enabled"):
                try:
                    cursor.execute(
                        f"ALTER TABLE admission_subscribers ADD COLUMN {column} INTEGER NOT NULL DEFAULT 1"
                    )
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
            try:
                cursor.execute("ALTER TABLE admission_events ADD COLUMN source_name TEXT NOT NULL DEFAULT '공식 오피셜'")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE admission_events ADD COLUMN source_url TEXT")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE admission_events ADD COLUMN urgency TEXT NOT NULL DEFAULT 'NORMAL'")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE admission_events ADD COLUMN category TEXT NOT NULL DEFAULT 'BRIEFING'")
            except sqlite3.OperationalError:
                pass
            try:
                cursor.execute("ALTER TABLE admission_events ADD COLUMN expires_at TEXT")
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
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS admission_card_acks (
                    chat_id TEXT NOT NULL,
                    content_id TEXT NOT NULL,
                    acked_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id, content_id)
                )
                """
            )
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS admission_content_sent_history (
                    chat_id TEXT NOT NULL,
                    item_id TEXT NOT NULL,
                    item_kind TEXT NOT NULL,
                    stream TEXT NOT NULL,
                    sent_at TEXT NOT NULL,
                    PRIMARY KEY(chat_id, item_id, stream)
                )
                """
            )
            conn.commit()

    # --- Subscriber Methods ---

    def upsert_subscriber(self, profile: ParentUserProfile) -> None:
        """Upsert profile data while preserving alert preferences on conflict.

        Alert preferences are intentionally excluded from the conflict update.  They
        are user-owned state and may only be changed by ``update_alert_preferences``.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO admission_subscribers (
                    chat_id, target_grade, target_region, education_stage, grade_level, username, first_name, alert_enabled,
                    weekly_guide_enabled, event_dday_enabled, policy_breaking_enabled, registered_at, last_active_at,
                    children_json, active_child_index
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(chat_id) DO UPDATE SET
                    target_grade = excluded.target_grade,
                    target_region = excluded.target_region,
                    education_stage = excluded.education_stage,
                    grade_level = excluded.grade_level,
                    username = excluded.username,
                    first_name = excluded.first_name,
                    alert_enabled = excluded.alert_enabled,
                    registered_at = excluded.registered_at,
                    last_active_at = excluded.last_active_at,
                    children_json = excluded.children_json,
                    active_child_index = excluded.active_child_index
                """,
                (
                    str(profile.chat_id),
                    profile.target_grade.value,
                    profile.target_region.value,
                    profile.education_stage.value,
                    profile.grade_level.value,
                    profile.username,
                    profile.first_name,
                    1 if profile.alert_enabled else 0,
                    1 if profile.weekly_guide_enabled else 0,
                    1 if profile.event_dday_enabled else 0,
                    1 if profile.policy_breaking_enabled else 0,
                    profile.registered_at.isoformat(),
                    profile.last_active_at.isoformat(),
                    json.dumps([c.to_dict() for c in profile.children], ensure_ascii=False),
                    profile.active_child_index,
                ),
            )
            conn.commit()

    def update_alert_preferences(
        self,
        chat_id: str,
        *,
        weekly: Optional[bool] = None,
        dday: Optional[bool] = None,
        policy: Optional[bool] = None,
    ) -> bool:
        """Change only explicitly supplied personalized-alert preferences."""
        assignments: list[str] = []
        values: list[object] = []
        for column, value in (
            ("weekly_guide_enabled", weekly),
            ("event_dday_enabled", dday),
            ("policy_breaking_enabled", policy),
        ):
            if value is not None:
                assignments.append(f"{column} = ?")
                values.append(1 if bool(value) else 0)
        if not assignments:
            return False
        values.append(str(chat_id))
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                f"UPDATE admission_subscribers SET {', '.join(assignments)}, last_active_at = ? WHERE chat_id = ?",
                (*values[:-1], datetime.now(KST).isoformat(), values[-1]),
            )
            conn.commit()
            return cursor.rowcount > 0

    def create_subscriber_if_absent(self, profile: ParentUserProfile) -> bool:
        """Create a subscriber without overwriting an existing profile."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR IGNORE INTO admission_subscribers (
                    chat_id, target_grade, target_region, education_stage, grade_level, username, first_name, alert_enabled,
                    weekly_guide_enabled, event_dday_enabled, policy_breaking_enabled,
                    registered_at, last_active_at, children_json, active_child_index
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(profile.chat_id),
                    profile.target_grade.value,
                    profile.target_region.value,
                    profile.education_stage.value,
                    profile.grade_level.value,
                    profile.username,
                    profile.first_name,
                    1 if profile.alert_enabled else 0,
                    1 if profile.weekly_guide_enabled else 0,
                    1 if profile.event_dday_enabled else 0,
                    1 if profile.policy_breaking_enabled else 0,
                    profile.registered_at.isoformat(),
                    profile.last_active_at.isoformat(),
                    json.dumps([c.to_dict() for c in profile.children], ensure_ascii=False),
                    profile.active_child_index,
                ),
            )
            conn.commit()
            return cursor.rowcount > 0

    def touch_subscriber_metadata(
        self,
        chat_id: str,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> bool:
        """Refresh Telegram metadata only; preferences and child data are immutable here."""
        stamp = (now or datetime.now()).isoformat()
        clean_username = (username or "").strip()
        clean_first_name = (first_name or "").strip()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE admission_subscribers
                SET username = CASE WHEN ? <> '' THEN ? ELSE username END,
                    first_name = CASE WHEN ? <> '' THEN ? ELSE first_name END,
                    last_active_at = ?
                WHERE chat_id = ?
                """,
                (
                    clean_username, clean_username,
                    clean_first_name, clean_first_name,
                    stamp, str(chat_id),
                ),
            )
            conn.commit()
            return cursor.rowcount > 0

    def update_subscriber_grade(self, chat_id: str, grade: TargetGrade) -> bool:
        """Updates target grade for a parent (mirrored into the active child when multi-child)."""
        return self._update_active_target(chat_id, grade=grade)

    def update_subscriber_stage_level(
        self,
        chat_id: str,
        stage: EducationStage,
        level: GradeLevel,
        grade_mode: GradeMode = GradeMode.MANUAL,
        school_entry_year: Optional[int] = None,
    ) -> bool:
        """Updates additive stage/level fields and their legacy delivery bucket."""
        try:
            stage = stage if isinstance(stage, EducationStage) else EducationStage(stage)
            level = level if isinstance(level, GradeLevel) else GradeLevel(level)
            grade_mode = grade_mode if isinstance(grade_mode, GradeMode) else GradeMode(grade_mode)
        except (TypeError, ValueError):
            return False
        target_grade = stage_level_to_legacy_target_grade(stage, level)
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM admission_subscribers WHERE chat_id = ?", (str(chat_id),))
            row = cursor.fetchone()
            if not row:
                return False
            profile = self._row_to_subscriber(row)
            profile.target_grade = target_grade
            profile.education_stage = stage
            profile.grade_level = level
            child = profile.get_active_child()
            child.grade = target_grade
            child.education_stage = stage
            child.grade_level = level
            child.grade_mode = grade_mode
            child.school_entry_year = school_entry_year
            child._extended = True
            if profile.children:
                profile.children[profile.active_child_index] = child
            cursor.execute(
                """UPDATE admission_subscribers
                   SET target_grade = ?, education_stage = ?, grade_level = ?, children_json = ?, last_active_at = ?
                   WHERE chat_id = ?""",
                (
                    target_grade.value, stage.value, level.value,
                    json.dumps([c.to_dict() for c in profile.children], ensure_ascii=False),
                    now, str(chat_id),
                ),
            )
            conn.commit()
            return cursor.rowcount > 0

    def update_subscriber_region(self, chat_id: str, region: TargetRegion) -> bool:
        """Updates target region for a parent (mirrored into the active child when multi-child)."""
        return self._update_active_target(chat_id, region=region)

    def _update_active_target(
        self,
        chat_id: str,
        grade: Optional[TargetGrade] = None,
        region: Optional[TargetRegion] = None,
    ) -> bool:
        """Writes grade/region to the legacy columns and keeps the active child in sync."""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM admission_subscribers WHERE chat_id = ?", (str(chat_id),))
            row = cursor.fetchone()
            if not row:
                return False
            profile = self._row_to_subscriber(row)
            if grade is not None:
                profile.target_grade = grade
                profile.education_stage, profile.grade_level = legacy_target_grade_to_stage_level(grade)
            if region is not None:
                profile.target_region = region
            if profile.children:
                child = profile.get_active_child()
                child.grade = profile.target_grade
                child.region = profile.target_region
                child.education_stage = profile.education_stage
                child.grade_level = profile.grade_level
            cursor.execute(
                """
                UPDATE admission_subscribers
                SET target_grade = ?, target_region = ?, education_stage = ?, grade_level = ?, children_json = ?, last_active_at = ?
                WHERE chat_id = ?
                """,
                (
                    profile.target_grade.value,
                    profile.target_region.value,
                    profile.education_stage.value,
                    profile.grade_level.value,
                    json.dumps([c.to_dict() for c in profile.children], ensure_ascii=False),
                    now,
                    str(chat_id),
                ),
            )
            conn.commit()
            return cursor.rowcount > 0

    def set_active_child(self, chat_id: str, index: int) -> bool:
        """Switches the active child and mirrors its grade/region into the legacy columns."""
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM admission_subscribers WHERE chat_id = ?", (str(chat_id),))
            row = cursor.fetchone()
            if not row:
                return False
            profile = self._row_to_subscriber(row)
            if not (0 <= index < len(profile.children)):
                return False
            child = profile.children[index]
            cursor.execute(
                """
                UPDATE admission_subscribers
                SET active_child_index = ?, target_grade = ?, target_region = ?, education_stage = ?, grade_level = ?, children_json = ?, last_active_at = ?
                WHERE chat_id = ?
                """,
                (index, child.grade.value, child.region.value, child.education_stage.value, child.grade_level.value,
                 json.dumps([c.to_dict() for c in profile.children], ensure_ascii=False), now, str(chat_id)),
            )
            conn.commit()
            return cursor.rowcount > 0

    def add_child(self, chat_id: str, child: ChildProfile) -> bool:
        """Appends a child (up to MAX_CHILDREN); the active child is unchanged.
        A legacy single-child subscriber is first seeded as 첫째 from target_grade/target_region.
        """
        now = datetime.now().isoformat()
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM admission_subscribers WHERE chat_id = ?", (str(chat_id),))
            row = cursor.fetchone()
            if not row:
                return False
            profile = self._row_to_subscriber(row)
            children = list(profile.children)
            active_index = profile.active_child_index
            if not children:
                children.append(ChildProfile(alias="첫째", grade=profile.target_grade, region=profile.target_region))
                active_index = 0
            if len(children) >= MAX_CHILDREN:
                return False
            children.append(child)
            cursor.execute(
                """
                UPDATE admission_subscribers
                SET children_json = ?, active_child_index = ?, last_active_at = ?
                WHERE chat_id = ?
                """,
                (
                    json.dumps([c.to_dict() for c in children], ensure_ascii=False),
                    active_index,
                    now,
                    str(chat_id),
                ),
            )
            conn.commit()
            return cursor.rowcount > 0

    @staticmethod
    def _row_to_subscriber(r: sqlite3.Row) -> ParentUserProfile:
        """Converts a subscriber row to ParentUserProfile (tolerates pre-migration rows and bad JSON)."""
        keys = r.keys()
        children: list[ChildProfile] = []
        if "children_json" in keys and r["children_json"]:
            try:
                raw = json.loads(r["children_json"])
            except ValueError:
                raw = []
            if isinstance(raw, list):
                children = [ChildProfile.from_dict(c) for c in raw if isinstance(c, dict)][:MAX_CHILDREN]
        active_index = 0
        if "active_child_index" in keys and r["active_child_index"] is not None:
            active_index = int(r["active_child_index"])
        if children and not (0 <= active_index < len(children)):
            active_index = 0
        try:
            row_grade = TargetGrade(r["target_grade"])
        except (ValueError, TypeError):
            row_grade = TargetGrade.ALL
        try:
            row_stage = EducationStage(r["education_stage"]) if "education_stage" in keys else None
        except (ValueError, TypeError):
            row_stage = None
        try:
            row_level = GradeLevel(r["grade_level"]) if "grade_level" in keys else None
        except (ValueError, TypeError):
            row_level = None
        if children:
            row_stage = children[active_index].education_stage
            row_level = children[active_index].grade_level
        elif row_stage is None or row_level is None or (row_stage == EducationStage.ALL and row_level == GradeLevel.ALL and row_grade != TargetGrade.ALL):
            row_stage, row_level = legacy_target_grade_to_stage_level(row_grade)
        return ParentUserProfile(
            chat_id=r["chat_id"],
            target_grade=row_grade,
            target_region=TargetRegion(r["target_region"] if "target_region" in keys else "ALL"),
            username=r["username"] or "",
            first_name=r["first_name"] or "",
            alert_enabled=bool(r["alert_enabled"]),
            registered_at=datetime.fromisoformat(r["registered_at"]),
            last_active_at=datetime.fromisoformat(r["last_active_at"]),
            children=children,
            active_child_index=active_index,
            education_stage=row_stage,
            grade_level=row_level,
            weekly_guide_enabled=bool(r["weekly_guide_enabled"]) if "weekly_guide_enabled" in keys else True,
            event_dday_enabled=bool(r["event_dday_enabled"]) if "event_dday_enabled" in keys else True,
            policy_breaking_enabled=bool(r["policy_breaking_enabled"]) if "policy_breaking_enabled" in keys else True,
        )

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
            return self._row_to_subscriber(row)

    def list_subscribers(
        self,
        grade: Optional[TargetGrade] = None,
        region: Optional[TargetRegion] = None,
        is_online_live: bool = False,
        active_only: bool = True,
    ) -> list[ParentUserProfile]:
        """Lists subscribers filtered by target grade and region.

        A multi-child parent matches when any one child's (grade, region) pair matches, so the
        parent of a 고3 and a 중등 child receives both grades' briefings.
        """
        with self._get_connection() as conn:
            cursor = conn.cursor()
            query = "SELECT * FROM admission_subscribers WHERE 1=1"
            params: list[object] = []

            if active_only:
                query += " AND alert_enabled = 1"

            cursor.execute(query, params)
            rows = cursor.fetchall()
            # Platform SSOT: outbound delivery only to ACTIVE users with an ACTIVE admission subscription
            platform_ids = (
                _platform_active_subscriber_ids(str(self.db_path), ("admission_bot", "edu_bot"), conn=conn)
                if active_only
                else None
            )

            results = []
            for r in rows:
                if platform_ids is not None and str(r["chat_id"]).strip() not in platform_ids:
                    continue
                profile = self._row_to_subscriber(r)
                targets = (
                    [(c.grade, c.region) for c in profile.children]
                    if profile.children
                    else [(profile.target_grade, profile.target_region)]
                )
                if any(self._target_matches(g, rg, grade, region, is_online_live) for g, rg in targets):
                    results.append(profile)

            return results

    def is_active_platform_subscriber(self, chat_id: str) -> Optional[bool]:
        """Return platform authorization; None means no platform subscriptions table exists."""
        with self._get_connection() as conn:
            ids = _platform_active_subscriber_ids(str(self.db_path), ("admission_bot", "edu_bot"), conn=conn)
        if ids is None:
            return None
        return str(chat_id).strip() in ids

    @staticmethod
    def _target_matches(
        p_grade: TargetGrade,
        p_reg: TargetRegion,
        grade: Optional[TargetGrade],
        region: Optional[TargetRegion],
        is_online_live: bool,
    ) -> bool:
        """Whether one child's (grade, region) target accepts a briefing for (grade, region)."""
        # 1. Grade match check
        if grade and grade != TargetGrade.ALL:
            if p_grade != TargetGrade.ALL and p_grade != grade:
                return False

        # 2. Region match check
        if region and region != TargetRegion.ALL:
            if p_reg == TargetRegion.ONLINE:
                if not is_online_live:
                    return False
            elif p_reg != TargetRegion.ALL:
                if not is_online_live and p_reg != region:
                    return False
        return True

    # --- Event Methods ---

    def _row_to_event(self, r: sqlite3.Row) -> AdmissionEvent:
        """Converts database row to AdmissionEvent domain object safely."""
        keys = r.keys()
        return AdmissionEvent(
            event_id=r["event_id"],
            title=r["title"],
            organizer=r["organizer"],
            target_grade=TargetGrade(r["target_grade"]),
            target_region=TargetRegion(r["target_region"] if "target_region" in keys else "ALL"),
            is_online_live=bool(r["is_online_live"] if "is_online_live" in keys else 0),
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
            source_name=r["source_name"] if "source_name" in keys else "공식 오피셜",
            source_url=r["source_url"] if "source_url" in keys else None,
            urgency=r["urgency"] if "urgency" in keys else "NORMAL",
            category=EventCategory(r["category"]) if "category" in keys and r["category"] else EventCategory.BRIEFING,
            expires_at=datetime.fromisoformat(r["expires_at"]) if "expires_at" in keys and r["expires_at"] else None,
            created_at=datetime.fromisoformat(r["created_at"]),
        )

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
                    credibility_score, source_name, source_url, urgency, category, expires_at,
                    status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SCHEDULED', ?)
                ON CONFLICT(event_id) DO UPDATE SET
                    title = excluded.title,
                    organizer = excluded.organizer,
                    target_grade = excluded.target_grade,
                    target_region = excluded.target_region,
                    is_online_live = excluded.is_online_live,
                    event_type = excluded.event_type,
                    event_date_str = excluded.event_date_str,
                    booking_opens_at = excluded.booking_opens_at,
                    is_first_come = excluded.is_first_come,
                    booking_url = excluded.booking_url,
                    summary_3lines_json = excluded.summary_3lines_json,
                    so_what = excluded.so_what,
                    action_todos_json = excluded.action_todos_json,
                    pdf_attachment_path = excluded.pdf_attachment_path,
                    credibility_score = excluded.credibility_score,
                    source_name = excluded.source_name,
                    source_url = excluded.source_url,
                    urgency = excluded.urgency,
                    category = excluded.category,
                    expires_at = excluded.expires_at
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
                    event.source_name,
                    event.source_url,
                    event.urgency,
                    event.category.value,
                    event.expires_at.isoformat() if event.expires_at else None,
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
            return self._row_to_event(row)

    def list_upcoming_ticketing(self, now: Optional[datetime] = None) -> list[AdmissionEvent]:
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
            now_kst = self._to_kst(now or datetime.now(KST))
            return [
                event for event in (self._row_to_event(r) for r in rows)
                if not self._is_expired(event, now_kst)
            ]

    def list_all_events(self) -> list[AdmissionEvent]:
        """Lists all registered admission events."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM admission_events ORDER BY created_at DESC")
            rows = cursor.fetchall()
            return [self._row_to_event(r) for r in rows]

    def get_hot_events(
        self,
        grade: TargetGrade = TargetGrade.ALL,
        region: TargetRegion = TargetRegion.ALL,
        limit: int = 3,
        now: Optional[datetime] = None,
    ) -> list[AdmissionEvent]:
        """Returns top N urgent or upcoming admission events matching grade & region."""
        all_events = self.list_all_events()
        now_kst = self._to_kst(now or datetime.now(KST))
        matched = [
            ev for ev in all_events
            if not self._is_expired(ev, now_kst) and self._matches_profile(ev, grade, region)
        ]

        def sort_key(e: AdmissionEvent):
            urgency_rank = 0 if e.urgency == "BREAKING" else (1 if e.urgency == "HIGH" else 2)
            return (urgency_rank, -e.credibility_score)

        matched.sort(key=sort_key)
        return matched[:limit]

    @staticmethod
    def _matches_profile(ev: AdmissionEvent, grade: TargetGrade, region: TargetRegion) -> bool:
        """Grade/region matching shared by hot-event and category listings."""
        grade_match = (grade == TargetGrade.ALL) or (ev.target_grade == TargetGrade.ALL) or (ev.target_grade == grade)
        if not grade_match:
            return False
        if region == TargetRegion.ALL:
            return True
        if region == TargetRegion.ONLINE:
            return ev.is_online_live or (ev.target_region == TargetRegion.ONLINE)
        return ev.is_online_live or (ev.target_region == TargetRegion.ALL) or (ev.target_region == region)

    @staticmethod
    def _parse_event_date(event_date_str: str) -> Optional[datetime]:
        """Parses 'YYYY-MM-DD[ HH:MM]' prefix; returns None for undated strings (e.g. TBA notices)."""
        try:
            return datetime.strptime((event_date_str or "").strip()[:10], "%Y-%m-%d")
        except ValueError:
            return None

    @staticmethod
    def _to_kst(value: datetime) -> datetime:
        """Normalize naive and aware datetimes to KST for safe comparisons."""
        if value.tzinfo is None:
            return value.replace(tzinfo=KST)
        return value.astimezone(KST)

    @classmethod
    def _is_expired(cls, ev: AdmissionEvent, now: datetime) -> bool:
        """expires_at takes precedence; otherwise an event dated before today is expired."""
        now_kst = cls._to_kst(now)
        if ev.expires_at is not None:
            return cls._to_kst(ev.expires_at) < now_kst
        ev_date = cls._parse_event_date(ev.event_date_str)
        return ev_date is not None and ev_date.date() < now_kst.date()

    def get_events_by_category(
        self,
        category: EventCategory,
        grade: TargetGrade = TargetGrade.ALL,
        region: TargetRegion = TargetRegion.ALL,
        page: int = 0,
        page_size: int = 3,
        now: Optional[datetime] = None,
    ) -> tuple[list[AdmissionEvent], int]:
        """Returns (page of upcoming events in category matching grade/region, total matched count).

        Policy notices / breaking news / weekly digests are not attendable events and are excluded.
        """
        now = self._to_kst(now or datetime.now(KST))
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM admission_events
                WHERE category = ? AND event_type NOT IN (?, ?, ?)
                """,
                (
                    category.value,
                    BriefingType.POLICY_TRANSLATE.value,
                    BriefingType.BREAKING_NEWS.value,
                    BriefingType.WEEKLY_TODO.value,
                ),
            )
            rows = cursor.fetchall()

        matched = []
        for r in rows:
            ev = self._row_to_event(r)
            if self._is_expired(ev, now) or not self._matches_profile(ev, grade, region):
                continue
            matched.append(ev)

        def sort_key(e: AdmissionEvent):
            ev_date = self._parse_event_date(e.event_date_str)
            # Dated events chronologically first, undated (TBA) after, then higher credibility
            return (ev_date is None, ev_date or datetime.max, e.event_date_str, -e.credibility_score)

        matched.sort(key=sort_key)
        total = len(matched)
        start = max(0, page) * max(1, page_size)
        return matched[start:start + max(1, page_size)], total

    # --- Dispatch Log Methods ---

    def claim_dispatch_slot(self, briefing_id: str, chat_id: str) -> bool:
        """Atomically claim one recipient slot before any network send."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR IGNORE INTO admission_dispatch_logs (
                    log_id, briefing_id, chat_id, dispatched_at, status
                ) VALUES (?, ?, ?, ?, 'CLAIMED')
                """,
                (str(uuid.uuid4()), briefing_id, str(chat_id), datetime.now(KST).isoformat()),
            )
            conn.commit()
            return cursor.rowcount > 0

    def update_dispatch_status(self, briefing_id: str, chat_id: str, status: str) -> bool:
        """Advance a claimed slot to SENT/UNKNOWN/FAILED without replacing its claim."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE admission_dispatch_logs
                SET dispatched_at = ?, status = ?
                WHERE briefing_id = ? AND chat_id = ? AND status = 'CLAIMED'
                """,
                (datetime.now(KST).isoformat(), status, briefing_id, str(chat_id)),
            )
            conn.commit()
            return cursor.rowcount > 0

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
                    datetime.now(KST).isoformat(),
                    status,
                ),
            )
            conn.commit()

    def has_dispatched(self, briefing_id: str, chat_id: str) -> bool:
        """Checks if this specific briefing was already dispatched to this user."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM admission_dispatch_logs WHERE briefing_id = ? AND chat_id = ? AND status IN ('SUCCESS', 'SENT')",
                (briefing_id, str(chat_id)),
            )
            return cursor.fetchone() is not None

    # --- Personalized push acknowledgement/history ---

    def record_card_ack(
        self, chat_id: str, content_id: str, acked_at: Optional[datetime] = None
    ) -> bool:
        """Record an owned card acknowledgement exactly once."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR IGNORE INTO admission_card_acks (chat_id, content_id, acked_at)
                VALUES (?, ?, ?)
                """,
                (str(chat_id), str(content_id), (acked_at or datetime.now(KST)).isoformat()),
            )
            conn.commit()
            return cursor.rowcount > 0

    ack_card = record_card_ack

    def is_card_acked(self, chat_id: str, content_id: str) -> bool:
        with self._get_connection() as conn:
            return conn.execute(
                "SELECT 1 FROM admission_card_acks WHERE chat_id = ? AND content_id = ?",
                (str(chat_id), str(content_id)),
            ).fetchone() is not None

    def record_content_sent(
        self,
        chat_id: str,
        item_id: str,
        item_kind: str,
        stream: str,
        sent_at: Optional[datetime] = None,
    ) -> bool:
        """Append successful personalized content delivery history only."""
        with self._get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR IGNORE INTO admission_content_sent_history
                    (chat_id, item_id, item_kind, stream, sent_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (str(chat_id), str(item_id), str(item_kind), str(stream), (sent_at or datetime.now(KST)).isoformat()),
            )
            conn.commit()
            return cursor.rowcount > 0

    record_content_sent_history = record_content_sent

    def has_content_been_sent(self, chat_id: str, item_id: str, stream: str) -> bool:
        with self._get_connection() as conn:
            return conn.execute(
                """
                SELECT 1 FROM admission_content_sent_history
                WHERE chat_id = ? AND item_id = ? AND stream = ?
                """,
                (str(chat_id), str(item_id), str(stream)),
            ).fetchone() is not None

    has_sent_content = has_content_been_sent

    def personalized_ack_summary(self) -> dict[str, int]:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS total, COUNT(DISTINCT chat_id) AS users FROM admission_card_acks"
            ).fetchone()
            sent = conn.execute(
                "SELECT COUNT(*) AS total, COUNT(DISTINCT chat_id) AS users FROM admission_content_sent_history"
            ).fetchone()
            return {
                "acks_total": int(row["total"]),
                "ack_users": int(row["users"]),
                "sent_history_total": int(sent["total"]),
                "sent_history_users": int(sent["users"]),
            }

    def personalized_user_summary(self, chat_id: str) -> dict[str, int]:
        with self._get_connection() as conn:
            ack = conn.execute(
                "SELECT COUNT(*) FROM admission_card_acks WHERE chat_id = ?", (str(chat_id),)
            ).fetchone()[0]
            sent = conn.execute(
                "SELECT COUNT(*) FROM admission_content_sent_history WHERE chat_id = ?", (str(chat_id),)
            ).fetchone()[0]
            return {"acks": int(ack), "sent_history": int(sent)}
