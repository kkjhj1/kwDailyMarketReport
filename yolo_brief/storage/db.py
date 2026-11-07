"""Normalized SQLite storage for YOLO Bot (욜로봇).
Enforces GPT audit requirements:
- Single persistent connection for `:memory:` databases ([TG-06])
- `PRAGMA foreign_keys = ON`
- `yolo_users` onboarding vs active CHECK constraints
- 1NF normalized `yolo_curation_archetypes` and `yolo_curation_solo_tags`
- `UNIQUE(chat_id, week_key, dispatch_type)` with `IN_FLIGHT` / `SENT` / `FAILED` state reservation
- v2.0 visual magazine fields: additive `ALTER TABLE` migration for legacy DBs and
  ordered child tables (`yolo_curation_gallery` / `_timelines` / `_budgets`)
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
import sqlite3
from typing import Iterator, Optional, Sequence
import uuid

from yolo_brief.models import (
    ActivityMode,
    KST,
    ProfileState,
    Season,
    SoloArchetype,
    WeatherClass,
    BudgetLine,
    GalleryImage,
    TimelineEntry,
    YoloCuration,
    YoloUserProfile,
)


SCHEMA_DDL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS yolo_users (
    chat_id TEXT PRIMARY KEY,
    profile_state TEXT NOT NULL DEFAULT 'ONBOARDING'
        CHECK(profile_state IN ('ONBOARDING', 'ACTIVE', 'UNSUBSCRIBED')),
    archetype TEXT NULL
        CHECK(archetype IN ('ACTIVE_CHALLENGER', 'URBAN_HIPSTER', 'ROAD_NOMAD', 'WELLNESS_DEEPDIVER') OR archetype IS NULL),
    weather_area_code TEXT NOT NULL DEFAULT '1100000000',
    weather_fallback_enabled INTEGER NOT NULL DEFAULT 1
        CHECK(weather_fallback_enabled IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK(
        (profile_state = 'ONBOARDING' AND archetype IS NULL)
        OR (profile_state = 'ACTIVE' AND archetype IS NOT NULL)
        OR (profile_state = 'UNSUBSCRIBED')
    )
);

CREATE TABLE IF NOT EXISTS yolo_curations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    season TEXT NOT NULL
        CHECK(season IN ('ALL', 'SPRING', 'SUMMER', 'AUTUMN', 'WINTER')),
    weather_class TEXT NOT NULL DEFAULT 'ALL'
        CHECK(weather_class IN ('ALL', 'CLEAR', 'RAIN', 'SNOW', 'HEAT', 'COLD', 'DUST', 'FOG', 'UNKNOWN')),
    activity_mode TEXT NOT NULL
        CHECK(activity_mode IN ('INDOOR', 'OUTDOOR')),
    location_name TEXT NOT NULL,
    location_area_code TEXT NOT NULL DEFAULT '1100000000',
    cost_or_duration TEXT NOT NULL,
    action_url TEXT NOT NULL,
    is_fallback INTEGER NOT NULL DEFAULT 0
        CHECK(is_fallback IN (0, 1)),
    fallback_for TEXT,
    priority INTEGER NOT NULL DEFAULT 100,
    is_active INTEGER NOT NULL DEFAULT 1
        CHECK(is_active IN (0, 1)),
    emoji TEXT NOT NULL DEFAULT '✨',
    created_at TEXT NOT NULL,
    hero_image_url TEXT NOT NULL DEFAULT '',
    hero_image_alt TEXT NOT NULL DEFAULT '',
    solo_secrets TEXT NOT NULL DEFAULT '',
    map_naver_url TEXT NOT NULL DEFAULT '',
    map_kakao_url TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS yolo_curation_archetypes (
    curation_id TEXT NOT NULL,
    archetype TEXT NOT NULL
        CHECK(archetype IN ('ACTIVE_CHALLENGER', 'URBAN_HIPSTER', 'ROAD_NOMAD', 'WELLNESS_DEEPDIVER')),
    PRIMARY KEY (curation_id, archetype),
    FOREIGN KEY (curation_id) REFERENCES yolo_curations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS yolo_curation_solo_tags (
    curation_id TEXT NOT NULL,
    solo_tag TEXT NOT NULL,
    PRIMARY KEY (curation_id, solo_tag),
    FOREIGN KEY (curation_id) REFERENCES yolo_curations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS yolo_curation_gallery (
    curation_id TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    url TEXT NOT NULL,
    alt TEXT NOT NULL,
    PRIMARY KEY (curation_id, sort_order),
    FOREIGN KEY (curation_id) REFERENCES yolo_curations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS yolo_curation_timelines (
    curation_id TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    time_label TEXT NOT NULL,
    spot_name TEXT NOT NULL,
    description TEXT NOT NULL,
    PRIMARY KEY (curation_id, sort_order),
    FOREIGN KEY (curation_id) REFERENCES yolo_curations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS yolo_curation_budgets (
    curation_id TEXT NOT NULL,
    sort_order INTEGER NOT NULL,
    item_name TEXT NOT NULL,
    cost_krw INTEGER NOT NULL CHECK(cost_krw >= 0),
    category TEXT NOT NULL DEFAULT 'etc',
    PRIMARY KEY (curation_id, sort_order),
    FOREIGN KEY (curation_id) REFERENCES yolo_curations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS yolo_dispatch_logs (
    dispatch_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    week_key TEXT NOT NULL,
    dispatch_type TEXT NOT NULL DEFAULT 'WEEKLY_REGULAR',
    archetype TEXT NOT NULL,
    sent_status TEXT NOT NULL
        CHECK(sent_status IN ('IN_FLIGHT', 'SENT', 'FAILED', 'UNKNOWN')),
    attempt_count INTEGER NOT NULL DEFAULT 1,
    last_error TEXT,
    content_hash TEXT,
    sent_at TEXT NOT NULL,
    UNIQUE(chat_id, week_key, dispatch_type)
);

CREATE INDEX IF NOT EXISTS idx_yolo_curation_filter
ON yolo_curations (is_active, season, weather_class, activity_mode, priority);

CREATE INDEX IF NOT EXISTS idx_yolo_curation_arc
ON yolo_curation_archetypes (archetype, curation_id);

CREATE INDEX IF NOT EXISTS idx_yolo_curation_tag
ON yolo_curation_solo_tags (solo_tag, curation_id);
"""


# v2.0 additive columns on `yolo_curations` (legacy DB auto-migration in `init_db()`)
CURATION_V2_COLUMNS: tuple[tuple[str, str], ...] = (
    ("hero_image_url", "TEXT NOT NULL DEFAULT ''"),
    ("hero_image_alt", "TEXT NOT NULL DEFAULT ''"),
    ("solo_secrets", "TEXT NOT NULL DEFAULT ''"),
    ("map_naver_url", "TEXT NOT NULL DEFAULT ''"),
    ("map_kakao_url", "TEXT NOT NULL DEFAULT ''"),
)


def _now_iso() -> str:
    return datetime.now(KST).isoformat()


class YoloDatabase:
    """SQLite repository for YOLO Bot users, curations, and dispatch logs."""

    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self._is_memory = self.db_path == ":memory:"
        self._mem_conn: Optional[sqlite3.Connection] = None
        if self._is_memory:
            self._mem_conn = sqlite3.connect(":memory:", check_same_thread=False)
            self._mem_conn.row_factory = sqlite3.Row
            self._mem_conn.execute("PRAGMA foreign_keys = ON;")
        self.init_db()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        if self._is_memory and self._mem_conn is not None:
            self._mem_conn.execute("PRAGMA foreign_keys = ON;")
            try:
                yield self._mem_conn
                self._mem_conn.commit()
            except Exception:
                self._mem_conn.rollback()
                raise
            return

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(SCHEMA_DDL)
            self._migrate_curation_columns(conn)

    @staticmethod
    def _migrate_curation_columns(conn: sqlite3.Connection) -> None:
        """Adds v2.0 columns to legacy `yolo_curations` tables (idempotent)."""
        existing = {
            str(r["name"]) for r in conn.execute("PRAGMA table_info(yolo_curations)").fetchall()
        }
        for col_name, col_ddl in CURATION_V2_COLUMNS:
            if col_name not in existing:
                conn.execute(f"ALTER TABLE yolo_curations ADD COLUMN {col_name} {col_ddl}")

    def close(self) -> None:
        if self._mem_conn is not None:
            self._mem_conn.close()
            self._mem_conn = None

    # ------------------------------------------------------------------
    # User Profile CRUD
    # ------------------------------------------------------------------
    def _row_to_user(self, row: sqlite3.Row) -> YoloUserProfile:
        arc_raw = row["archetype"]
        return YoloUserProfile(
            chat_id=str(row["chat_id"]),
            profile_state=ProfileState(row["profile_state"]),
            archetype=SoloArchetype(arc_raw) if arc_raw else None,
            weather_area_code=str(row["weather_area_code"]),
            weather_fallback_enabled=bool(row["weather_fallback_enabled"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
        )

    def get_user(self, chat_id: str) -> Optional[YoloUserProfile]:
        cid = str(chat_id).strip()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM yolo_users WHERE chat_id = ?", (cid,)
            ).fetchone()
            return self._row_to_user(row) if row else None

    def upsert_onboarding_user(
        self, chat_id: str, weather_area_code: str = "1100000000"
    ) -> YoloUserProfile:
        cid = str(chat_id).strip()
        now = _now_iso()
        existing = self.get_user(cid)
        if existing is not None:
            if existing.profile_state == ProfileState.UNSUBSCRIBED:
                with self._connect() as conn:
                    conn.execute(
                        """
                        UPDATE yolo_users
                        SET profile_state = 'ONBOARDING', archetype = NULL, updated_at = ?
                        WHERE chat_id = ?
                        """,
                        (now, cid),
                    )
                return self.get_user(cid)  # type: ignore[return-value]
            return existing

        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO yolo_users (
                    chat_id, profile_state, archetype, weather_area_code,
                    weather_fallback_enabled, created_at, updated_at
                ) VALUES (?, 'ONBOARDING', NULL, ?, 1, ?, ?)
                """,
                (cid, weather_area_code, now, now),
            )
        return self.get_user(cid)  # type: ignore[return-value]

    def set_user_archetype(
        self, chat_id: str, archetype: SoloArchetype
    ) -> YoloUserProfile:
        cid = str(chat_id).strip()
        now = _now_iso()
        existing = self.get_user(cid)
        with self._connect() as conn:
            if existing is None:
                conn.execute(
                    """
                    INSERT INTO yolo_users (
                        chat_id, profile_state, archetype, weather_area_code,
                        weather_fallback_enabled, created_at, updated_at
                    ) VALUES (?, 'ACTIVE', ?, '1100000000', 1, ?, ?)
                    """,
                    (cid, archetype.value, now, now),
                )
            else:
                conn.execute(
                    """
                    UPDATE yolo_users
                    SET profile_state = 'ACTIVE', archetype = ?, updated_at = ?
                    WHERE chat_id = ?
                    """,
                    (archetype.value, now, cid),
                )
        return self.get_user(cid)  # type: ignore[return-value]

    def set_weather_fallback(self, chat_id: str, enabled: bool) -> YoloUserProfile:
        cid = str(chat_id).strip()
        now = _now_iso()
        if self.get_user(cid) is None:
            self.upsert_onboarding_user(cid)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE yolo_users
                SET weather_fallback_enabled = ?, updated_at = ?
                WHERE chat_id = ?
                """,
                (1 if enabled else 0, now, cid),
            )
        return self.get_user(cid)  # type: ignore[return-value]

    def set_weather_area_code(self, chat_id: str, area_code: str) -> YoloUserProfile:
        cid = str(chat_id).strip()
        now = _now_iso()
        if self.get_user(cid) is None:
            self.upsert_onboarding_user(cid, weather_area_code=area_code)
            return self.get_user(cid)  # type: ignore[return-value]
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE yolo_users
                SET weather_area_code = ?, updated_at = ?
                WHERE chat_id = ?
                """,
                (area_code, now, cid),
            )
        return self.get_user(cid)  # type: ignore[return-value]

    def unsubscribe_user(self, chat_id: str) -> YoloUserProfile:
        cid = str(chat_id).strip()
        now = _now_iso()
        if self.get_user(cid) is None:
            self.upsert_onboarding_user(cid)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE yolo_users
                SET profile_state = 'UNSUBSCRIBED', updated_at = ?
                WHERE chat_id = ?
                """,
                (now, cid),
            )
        return self.get_user(cid)  # type: ignore[return-value]

    def list_active_users(self) -> list[YoloUserProfile]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM yolo_users WHERE profile_state = 'ACTIVE' ORDER BY created_at ASC"
            ).fetchall()
            return [self._row_to_user(r) for r in rows]

    # ------------------------------------------------------------------
    # Curation CRUD (Normalized 1NF)
    # ------------------------------------------------------------------
    def upsert_curation(self, curation: YoloCuration) -> None:
        now = _now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO yolo_curations (
                    id, title, summary, season, weather_class, activity_mode,
                    location_name, location_area_code, cost_or_duration, action_url,
                    is_fallback, fallback_for, priority, is_active, emoji, created_at,
                    hero_image_url, hero_image_alt, solo_secrets, map_naver_url, map_kakao_url
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    summary = excluded.summary,
                    season = excluded.season,
                    weather_class = excluded.weather_class,
                    activity_mode = excluded.activity_mode,
                    location_name = excluded.location_name,
                    location_area_code = excluded.location_area_code,
                    cost_or_duration = excluded.cost_or_duration,
                    action_url = excluded.action_url,
                    is_fallback = excluded.is_fallback,
                    fallback_for = excluded.fallback_for,
                    priority = excluded.priority,
                    is_active = excluded.is_active,
                    emoji = excluded.emoji,
                    hero_image_url = excluded.hero_image_url,
                    hero_image_alt = excluded.hero_image_alt,
                    solo_secrets = excluded.solo_secrets,
                    map_naver_url = excluded.map_naver_url,
                    map_kakao_url = excluded.map_kakao_url
                """,
                (
                    curation.id,
                    curation.title,
                    curation.summary,
                    curation.season.value,
                    curation.weather_class.value,
                    curation.activity_mode.value,
                    curation.location_name,
                    curation.location_area_code,
                    curation.cost_or_duration,
                    curation.action_url,
                    1 if curation.is_fallback else 0,
                    curation.fallback_for,
                    curation.priority,
                    1 if curation.is_active else 0,
                    curation.emoji,
                    now,
                    curation.hero_image_url,
                    curation.hero_image_alt,
                    curation.solo_secrets,
                    curation.map_naver_url,
                    curation.map_kakao_url,
                ),
            )
            conn.execute(
                "DELETE FROM yolo_curation_archetypes WHERE curation_id = ?",
                (curation.id,),
            )
            for arc in curation.archetypes:
                conn.execute(
                    "INSERT INTO yolo_curation_archetypes (curation_id, archetype) VALUES (?, ?)",
                    (curation.id, arc.value),
                )
            conn.execute(
                "DELETE FROM yolo_curation_solo_tags WHERE curation_id = ?",
                (curation.id,),
            )
            for tag in curation.solo_tags:
                conn.execute(
                    "INSERT INTO yolo_curation_solo_tags (curation_id, solo_tag) VALUES (?, ?)",
                    (curation.id, tag),
                )
            for table in ("yolo_curation_gallery", "yolo_curation_timelines", "yolo_curation_budgets"):
                conn.execute(f"DELETE FROM {table} WHERE curation_id = ?", (curation.id,))
            for idx, img in enumerate(curation.gallery_images):
                conn.execute(
                    "INSERT INTO yolo_curation_gallery (curation_id, sort_order, url, alt) VALUES (?, ?, ?, ?)",
                    (curation.id, idx, img.url, img.alt),
                )
            for idx, entry in enumerate(curation.timeline):
                conn.execute(
                    """
                    INSERT INTO yolo_curation_timelines (
                        curation_id, sort_order, time_label, spot_name, description
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (curation.id, idx, entry.time, entry.spot, entry.desc),
                )
            for idx, line in enumerate(curation.budget_breakdown):
                conn.execute(
                    """
                    INSERT INTO yolo_curation_budgets (
                        curation_id, sort_order, item_name, cost_krw, category
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (curation.id, idx, line.item, int(line.cost_krw), line.category),
                )

    def seed_curations(self, curations: Sequence[YoloCuration]) -> int:
        count = 0
        for c in curations:
            self.upsert_curation(c)
            count += 1
        return count

    def list_active_curations(self) -> list[YoloCuration]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM yolo_curations WHERE is_active = 1 ORDER BY priority DESC, id ASC"
            ).fetchall()
            result: list[YoloCuration] = []
            for r in rows:
                cid = str(r["id"])
                arc_rows = conn.execute(
                    "SELECT archetype FROM yolo_curation_archetypes WHERE curation_id = ? ORDER BY archetype ASC",
                    (cid,),
                ).fetchall()
                tag_rows = conn.execute(
                    "SELECT solo_tag FROM yolo_curation_solo_tags WHERE curation_id = ? ORDER BY rowid ASC",
                    (cid,),
                ).fetchall()
                if not arc_rows or not tag_rows:
                    continue
                result.append(
                    YoloCuration(
                        id=cid,
                        title=str(r["title"]),
                        summary=str(r["summary"]),
                        season=Season(r["season"]),
                        weather_class=WeatherClass(r["weather_class"]),
                        activity_mode=ActivityMode(r["activity_mode"]),
                        location_name=str(r["location_name"]),
                        location_area_code=str(r["location_area_code"]),
                        cost_or_duration=str(r["cost_or_duration"]),
                        action_url=str(r["action_url"]),
                        archetypes=tuple(SoloArchetype(a["archetype"]) for a in arc_rows),
                        solo_tags=tuple(str(t["solo_tag"]) for t in tag_rows),
                        is_fallback=bool(r["is_fallback"]),
                        fallback_for=r["fallback_for"],
                        priority=int(r["priority"]),
                        is_active=bool(r["is_active"]),
                        emoji=str(r["emoji"]),
                    )
                )
            return result

    # ------------------------------------------------------------------
    # Weekly Dispatch Deduplication & Idempotency ([TG-05])
    # ------------------------------------------------------------------
    def reserve_dispatch(
        self,
        chat_id: str,
        week_key: str,
        archetype: str,
        dispatch_type: str = "WEEKLY_REGULAR",
        content_hash: str = "",
    ) -> Optional[str]:
        """Attempts to reserve a weekly dispatch slot.
        Returns `dispatch_id` if reserved (new or retrying from FAILED),
        or `None` if already `SENT`, `IN_FLIGHT`, or `UNKNOWN` (timeout).
        """
        cid = str(chat_id).strip()
        now = _now_iso()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT dispatch_id, sent_status, attempt_count
                FROM yolo_dispatch_logs
                WHERE chat_id = ? AND week_key = ? AND dispatch_type = ?
                """,
                (cid, week_key, dispatch_type),
            ).fetchone()

            if row is not None:
                status = str(row["sent_status"])
                if status in ("SENT", "IN_FLIGHT", "UNKNOWN"):
                    return None
                # Retry from FAILED
                dispatch_id = str(row["dispatch_id"])
                conn.execute(
                    """
                    UPDATE yolo_dispatch_logs
                    SET sent_status = 'IN_FLIGHT',
                        attempt_count = attempt_count + 1,
                        archetype = ?,
                        content_hash = ?,
                        sent_at = ?
                    WHERE dispatch_id = ?
                    """,
                    (archetype, content_hash, now, dispatch_id),
                )
                return dispatch_id

            dispatch_id = f"yolo-{week_key}-{cid}-{uuid.uuid4().hex[:8]}"
            conn.execute(
                """
                INSERT INTO yolo_dispatch_logs (
                    dispatch_id, chat_id, week_key, dispatch_type,
                    archetype, sent_status, attempt_count, content_hash, sent_at
                ) VALUES (?, ?, ?, ?, ?, 'IN_FLIGHT', 1, ?, ?)
                """,
                (dispatch_id, cid, week_key, dispatch_type, archetype, content_hash, now),
            )
            return dispatch_id

    def mark_dispatch_sent(self, dispatch_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE yolo_dispatch_logs SET sent_status = 'SENT', last_error = NULL, sent_at = ? WHERE dispatch_id = ?",
                (_now_iso(), dispatch_id),
            )

    def mark_dispatch_failed(
        self, dispatch_id: str, error_msg: str, status: str = "FAILED"
    ) -> None:
        final_status = status if status in ("FAILED", "UNKNOWN") else "FAILED"
        with self._connect() as conn:
            conn.execute(
                "UPDATE yolo_dispatch_logs SET sent_status = ?, last_error = ?, sent_at = ? WHERE dispatch_id = ?",
                (final_status, error_msg, _now_iso(), dispatch_id),
            )
