"""Normalized SQLite storage for YOLO Bot (욜로봇).
Enforces GPT audit requirements:
- Single persistent connection for `:memory:` databases ([TG-06])
- `PRAGMA foreign_keys = ON`
- `yolo_users` onboarding vs active CHECK constraints
- 1NF normalized `yolo_curation_archetypes` and `yolo_curation_solo_tags`
- `UNIQUE(chat_id, week_key, dispatch_type)` with `IN_FLIGHT` / `SENT` / `FAILED` state reservation
- v2.0 visual magazine fields: additive `ALTER TABLE` migration for legacy DBs and
  ordered child tables (`yolo_curation_gallery` / `_timelines` / `_budgets`)
- Phase A: `time_slot` / `preferred_time_slot` additive columns and the
  `yolo_user_curation_logs` activity history (SEEN/SAVED/DONE/SKIP) for rotation scoring
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
from typing import Any, Iterable, Iterator, Optional, Sequence
import uuid

from yolo_brief.models import (
    ActivityMode,
    KST,
    ProfileState,
    Season,
    SettingType,
    SoloArchetype,
    TimeSlot,
    VALID_MBTI_TYPES,
    WeatherClass,
    BudgetLine,
    GalleryImage,
    TimelineEntry,
    YoloCuration,
    YoloUserProfile,
    derive_mbti_traits,
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
    preferred_time_slot TEXT NOT NULL DEFAULT 'ALL'
        CHECK(preferred_time_slot IN ('ALL', 'QUICK_2H', 'HALF_DAY', 'FULL_OR_TRIP')),
    mbti TEXT NULL,
    solo_level INTEGER NOT NULL DEFAULT 3
        CHECK(solo_level BETWEEN 1 AND 5),
    secondary_archetype TEXT NULL
        CHECK(secondary_archetype IN ('ACTIVE_CHALLENGER', 'URBAN_HIPSTER', 'ROAD_NOMAD', 'WELLNESS_DEEPDIVER') OR secondary_archetype IS NULL),
    social_energy TEXT NULL
        CHECK(social_energy IN ('I', 'E') OR social_energy IS NULL),
    pace_style TEXT NULL
        CHECK(pace_style IN ('J', 'P') OR pace_style IS NULL),
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
    setting TEXT NOT NULL DEFAULT 'HOME'
        CHECK(setting IN ('HOME', 'URBAN', 'OUTDOOR')),
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
    map_kakao_url TEXT NOT NULL DEFAULT '',
    time_slot TEXT NOT NULL DEFAULT 'ALL'
        CHECK(time_slot IN ('ALL', 'QUICK_2H', 'HALF_DAY', 'FULL_OR_TRIP')),
    solo_difficulty INTEGER NOT NULL DEFAULT 2
        CHECK(solo_difficulty BETWEEN 1 AND 5),
    social_fit TEXT NOT NULL DEFAULT 'ALL'
        CHECK(social_fit IN ('ALL', 'I', 'E')),
    pace_fit TEXT NOT NULL DEFAULT 'ALL'
        CHECK(pace_fit IN ('ALL', 'J', 'P'))
);

CREATE TABLE IF NOT EXISTS yolo_user_curation_logs (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    curation_id TEXT NOT NULL,
    action TEXT NOT NULL
        CHECK(action IN ('SEEN', 'SAVED', 'DONE', 'SKIP')),
    created_at TEXT NOT NULL,
    event_key TEXT UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_yolo_ucl_chat_time
ON yolo_user_curation_logs (chat_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_yolo_ucl_chat_cur_time
ON yolo_user_curation_logs (chat_id, curation_id, created_at DESC);

CREATE INDEX IF NOT EXISTS idx_yolo_ucl_chat_action_time
ON yolo_user_curation_logs (chat_id, action, created_at DESC);

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

CREATE TABLE IF NOT EXISTS yolo_curation_mbti_tags (
    curation_id TEXT NOT NULL,
    mbti TEXT NOT NULL,
    PRIMARY KEY (curation_id, mbti),
    FOREIGN KEY (curation_id) REFERENCES yolo_curations(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS yolo_curation_guidance (
    curation_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('HOW_TO', 'CHECKLIST')),
    sort_order INTEGER NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY (curation_id, kind, sort_order),
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


# v2.0 / Phase A / Phase B additive columns on `yolo_curations` (legacy DB auto-migration in `init_db()`)
CURATION_V2_COLUMNS: tuple[tuple[str, str], ...] = (
    (
        "setting",
        "TEXT NOT NULL DEFAULT 'HOME' CHECK(setting IN ('HOME', 'URBAN', 'OUTDOOR'))",
    ),
    ("hero_image_url", "TEXT NOT NULL DEFAULT ''"),
    ("hero_image_alt", "TEXT NOT NULL DEFAULT ''"),
    ("solo_secrets", "TEXT NOT NULL DEFAULT ''"),
    ("map_naver_url", "TEXT NOT NULL DEFAULT ''"),
    ("map_kakao_url", "TEXT NOT NULL DEFAULT ''"),
    (
        "time_slot",
        "TEXT NOT NULL DEFAULT 'ALL' CHECK(time_slot IN ('ALL', 'QUICK_2H', 'HALF_DAY', 'FULL_OR_TRIP'))",
    ),
    ("solo_difficulty", "INTEGER NOT NULL DEFAULT 2 CHECK(solo_difficulty BETWEEN 1 AND 5)"),
    ("social_fit", "TEXT NOT NULL DEFAULT 'ALL' CHECK(social_fit IN ('ALL', 'I', 'E'))"),
    ("pace_fit", "TEXT NOT NULL DEFAULT 'ALL' CHECK(pace_fit IN ('ALL', 'J', 'P'))"),
)

# Phase A / Phase B additive columns on `yolo_users` (legacy DB auto-migration in `init_db()`)
USER_PHASE_A_COLUMNS: tuple[tuple[str, str], ...] = (
    (
        "preferred_time_slot",
        "TEXT NOT NULL DEFAULT 'ALL'"
        " CHECK(preferred_time_slot IN ('ALL', 'QUICK_2H', 'HALF_DAY', 'FULL_OR_TRIP'))",
    ),
    ("mbti", "TEXT NULL"),
    ("solo_level", "INTEGER NOT NULL DEFAULT 3 CHECK(solo_level BETWEEN 1 AND 5)"),
    (
        "secondary_archetype",
        "TEXT NULL CHECK(secondary_archetype IN ('ACTIVE_CHALLENGER', 'URBAN_HIPSTER', 'ROAD_NOMAD', 'WELLNESS_DEEPDIVER') OR secondary_archetype IS NULL)",
    ),
    ("social_energy", "TEXT NULL CHECK(social_energy IN ('I', 'E') OR social_energy IS NULL)"),
    ("pace_style", "TEXT NULL CHECK(pace_style IN ('J', 'P') OR pace_style IS NULL)"),
)

CURATION_ACTIONS: frozenset[str] = frozenset({"SEEN", "SAVED", "DONE", "SKIP"})
# Rotation stats look-back: SEEN count window / "recent SAVED" window / max history scanned
SEEN_COUNT_WINDOW_DAYS = 30
SAVED_RECENT_WINDOW_DAYS = 30
STATS_LOOKBACK_DAYS = 180


def _now_iso() -> str:
    return datetime.now(KST).isoformat()


def _to_kst(value: Optional[datetime]) -> datetime:
    """Normalizes `now` to an aware KST datetime (naive values are treated as KST)."""
    if value is None:
        return datetime.now(KST)
    if value.tzinfo is None:
        return value.replace(tzinfo=KST)
    return value.astimezone(KST)


def _parse_iso(value: str) -> Optional[datetime]:
    try:
        return _to_kst(datetime.fromisoformat(str(value)))
    except (TypeError, ValueError):
        return None


def _safe_time_slot(value: object) -> TimeSlot:
    try:
        return TimeSlot(str(value or "ALL"))
    except ValueError:
        return TimeSlot.ALL


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
        sql = f"SELECT DISTINCT s.telegram_user_id FROM subscriptions s WHERE s.status = 'ACTIVE' AND s.bot_id IN ({marks})"
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
        candidates = [] if conn is not None else [own]
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
            self._migrate_user_columns(conn)

    @staticmethod
    def _add_missing_columns(
        conn: sqlite3.Connection, table: str, columns: Iterable[tuple[str, str]]
    ) -> None:
        existing = {str(r["name"]) for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for col_name, col_ddl in columns:
            if col_name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col_name} {col_ddl}")

    @classmethod
    def _migrate_curation_columns(cls, conn: sqlite3.Connection) -> None:
        """Adds v2.0 / Phase A columns to legacy `yolo_curations` tables (idempotent)."""
        cls._add_missing_columns(conn, "yolo_curations", CURATION_V2_COLUMNS)

    @classmethod
    def _migrate_user_columns(cls, conn: sqlite3.Connection) -> None:
        """Adds Phase A columns (`preferred_time_slot`) to legacy `yolo_users` tables (idempotent)."""
        cls._add_missing_columns(conn, "yolo_users", USER_PHASE_A_COLUMNS)

    def close(self) -> None:
        if self._mem_conn is not None:
            self._mem_conn.close()
            self._mem_conn = None

    # ------------------------------------------------------------------
    # User Profile CRUD
    # ------------------------------------------------------------------
    def _row_to_user(self, row: sqlite3.Row) -> YoloUserProfile:
        keys = set(row.keys())
        arc_raw = row["archetype"]
        sec_raw = row["secondary_archetype"] if "secondary_archetype" in keys else None
        mbti_raw = row["mbti"] if "mbti" in keys else None
        lvl_raw = row["solo_level"] if "solo_level" in keys and row["solo_level"] is not None else 3
        se_raw = row["social_energy"] if "social_energy" in keys else None
        ps_raw = row["pace_style"] if "pace_style" in keys else None
        return YoloUserProfile(
            chat_id=str(row["chat_id"]),
            profile_state=ProfileState(row["profile_state"]),
            archetype=SoloArchetype(arc_raw) if arc_raw else None,
            weather_area_code=str(row["weather_area_code"]),
            weather_fallback_enabled=bool(row["weather_fallback_enabled"]),
            preferred_time_slot=_safe_time_slot(row["preferred_time_slot"]),
            mbti=str(mbti_raw) if mbti_raw else None,
            solo_level=int(lvl_raw),
            secondary_archetype=SoloArchetype(sec_raw) if sec_raw else None,
            social_energy=str(se_raw) if se_raw else None,
            pace_style=str(ps_raw) if ps_raw else None,
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
                # Re-derive secondary_archetype if user has MBTI and secondary matches new primary
                sec_val = (
                    existing.secondary_archetype.value
                    if existing.secondary_archetype and existing.secondary_archetype != archetype
                    else None
                )
                if existing.mbti and sec_val is None:
                    _, _, suggested_sec = derive_mbti_traits(existing.mbti, archetype)
                    sec_val = suggested_sec.value if suggested_sec else None
                conn.execute(
                    """
                    UPDATE yolo_users
                    SET profile_state = 'ACTIVE', archetype = ?, secondary_archetype = ?, updated_at = ?
                    WHERE chat_id = ?
                    """,
                    (archetype.value, sec_val, now, cid),
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

    def set_preferred_time_slot(self, chat_id: str, time_slot: TimeSlot | str) -> YoloUserProfile:
        cid = str(chat_id).strip()
        slot = TimeSlot(time_slot)
        now = _now_iso()
        if self.get_user(cid) is None:
            self.upsert_onboarding_user(cid)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE yolo_users
                SET preferred_time_slot = ?, updated_at = ?
                WHERE chat_id = ?
                """,
                (slot.value, now, cid),
            )
        return self.get_user(cid)  # type: ignore[return-value]

    def set_user_mbti(self, chat_id: str, mbti: Optional[str]) -> YoloUserProfile:
        """Atomically updates `mbti`, `social_energy`, `pace_style`, and derived `secondary_archetype`."""
        cid = str(chat_id).strip()
        now = _now_iso()
        existing = self.get_user(cid) or self.upsert_onboarding_user(cid)
        if not mbti or not str(mbti).strip():
            with self._connect() as conn:
                conn.execute(
                    """
                    UPDATE yolo_users
                    SET mbti = NULL, social_energy = NULL, pace_style = NULL, updated_at = ?
                    WHERE chat_id = ?
                    """,
                    (now, cid),
                )
            return self.get_user(cid)  # type: ignore[return-value]

        code = str(mbti).strip().upper()
        if code not in VALID_MBTI_TYPES:
            raise ValueError(f"Invalid MBTI type: {mbti!r}")
        se, ps, suggested_sec = derive_mbti_traits(code, existing.archetype)
        sec_val = (
            existing.secondary_archetype.value
            if existing.secondary_archetype and existing.secondary_archetype != existing.archetype
            else (suggested_sec.value if suggested_sec else None)
        )
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE yolo_users
                SET mbti = ?, social_energy = ?, pace_style = ?, secondary_archetype = ?, updated_at = ?
                WHERE chat_id = ?
                """,
                (code, se, ps, sec_val, now, cid),
            )
        return self.get_user(cid)  # type: ignore[return-value]

    def set_user_solo_level(self, chat_id: str, solo_level: int) -> YoloUserProfile:
        cid = str(chat_id).strip()
        lvl = int(solo_level)
        if not 1 <= lvl <= 5:
            raise ValueError(f"solo_level must be 1..5, got {lvl}")
        now = _now_iso()
        if self.get_user(cid) is None:
            self.upsert_onboarding_user(cid)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE yolo_users
                SET solo_level = ?, updated_at = ?
                WHERE chat_id = ?
                """,
                (lvl, now, cid),
            )
        return self.get_user(cid)  # type: ignore[return-value]

    def set_user_secondary_archetype(
        self, chat_id: str, secondary_archetype: Optional[SoloArchetype | str]
    ) -> YoloUserProfile:
        cid = str(chat_id).strip()
        sec = SoloArchetype(secondary_archetype) if secondary_archetype else None
        now = _now_iso()
        if self.get_user(cid) is None:
            self.upsert_onboarding_user(cid)
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE yolo_users
                SET secondary_archetype = ?, updated_at = ?
                WHERE chat_id = ?
                """,
                (sec.value if sec else None, now, cid),
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
            # Platform SSOT: only ACTIVE users with an ACTIVE yolo_bot subscription (no-op when standalone)
            platform_ids = _platform_active_subscriber_ids(self.db_path, ("yolo_bot",), conn=conn)
            return [
                self._row_to_user(r) for r in rows
                if platform_ids is None or str(r["chat_id"]).strip() in platform_ids
            ]

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
                    setting, location_name, location_area_code, cost_or_duration, action_url,
                    is_fallback, fallback_for, priority, is_active, emoji, created_at,
                    hero_image_url, hero_image_alt, solo_secrets, map_naver_url, map_kakao_url,
                    time_slot, solo_difficulty, social_fit, pace_fit
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    title = excluded.title,
                    summary = excluded.summary,
                    season = excluded.season,
                    weather_class = excluded.weather_class,
                    activity_mode = excluded.activity_mode,
                    setting = excluded.setting,
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
                    map_kakao_url = excluded.map_kakao_url,
                    time_slot = excluded.time_slot,
                    solo_difficulty = excluded.solo_difficulty,
                    social_fit = excluded.social_fit,
                    pace_fit = excluded.pace_fit
                """,
                (
                    curation.id,
                    curation.title,
                    curation.summary,
                    curation.season.value,
                    curation.weather_class.value,
                    curation.activity_mode.value,
                    curation.setting.value,
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
                    curation.time_slot.value,
                    int(curation.solo_difficulty),
                    curation.social_fit,
                    curation.pace_fit,
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
            for table in (
                "yolo_curation_gallery",
                "yolo_curation_timelines",
                "yolo_curation_budgets",
                "yolo_curation_mbti_tags",
                "yolo_curation_guidance",
            ):
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
            for mbti_code in curation.mbti_tags:
                conn.execute(
                    "INSERT INTO yolo_curation_mbti_tags (curation_id, mbti) VALUES (?, ?)",
                    (curation.id, mbti_code),
                )
            for idx, step in enumerate(curation.how_to_steps):
                conn.execute(
                    """
                    INSERT INTO yolo_curation_guidance (curation_id, kind, sort_order, content)
                    VALUES (?, 'HOW_TO', ?, ?)
                    """,
                    (curation.id, idx, step),
                )
            for idx, item in enumerate(curation.checklist):
                conn.execute(
                    """
                    INSERT INTO yolo_curation_guidance (curation_id, kind, sort_order, content)
                    VALUES (?, 'CHECKLIST', ?, ?)
                    """,
                    (curation.id, idx, item),
                )

    def seed_curations(self, curations: Sequence[YoloCuration]) -> int:
        count = 0
        for c in curations:
            self.upsert_curation(c)
            count += 1
        return count

    def _row_to_curation(
        self, conn: sqlite3.Connection, r: sqlite3.Row
    ) -> Optional[YoloCuration]:
        cid = str(r["id"])
        keys = set(r.keys())
        arc_rows = conn.execute(
            "SELECT archetype FROM yolo_curation_archetypes WHERE curation_id = ? ORDER BY archetype ASC",
            (cid,),
        ).fetchall()
        tag_rows = conn.execute(
            "SELECT solo_tag FROM yolo_curation_solo_tags WHERE curation_id = ? ORDER BY rowid ASC",
            (cid,),
        ).fetchall()
        if not arc_rows or not tag_rows:
            return None
        gallery_rows = conn.execute(
            "SELECT url, alt FROM yolo_curation_gallery WHERE curation_id = ? ORDER BY sort_order ASC",
            (cid,),
        ).fetchall()
        timeline_rows = conn.execute(
            """
            SELECT time_label, spot_name, description FROM yolo_curation_timelines
            WHERE curation_id = ? ORDER BY sort_order ASC
            """,
            (cid,),
        ).fetchall()
        budget_rows = conn.execute(
            """
            SELECT item_name, cost_krw, category FROM yolo_curation_budgets
            WHERE curation_id = ? ORDER BY sort_order ASC
            """,
            (cid,),
        ).fetchall()
        mbti_rows = conn.execute(
            "SELECT mbti FROM yolo_curation_mbti_tags WHERE curation_id = ? ORDER BY rowid ASC",
            (cid,),
        ).fetchall()
        howto_rows = conn.execute(
            """
            SELECT content FROM yolo_curation_guidance
            WHERE curation_id = ? AND kind = 'HOW_TO' ORDER BY sort_order ASC
            """,
            (cid,),
        ).fetchall()
        check_rows = conn.execute(
            """
            SELECT content FROM yolo_curation_guidance
            WHERE curation_id = ? AND kind = 'CHECKLIST' ORDER BY sort_order ASC
            """,
            (cid,),
        ).fetchall()
        diff_val = int(r["solo_difficulty"]) if "solo_difficulty" in keys and r["solo_difficulty"] is not None else 2
        s_fit_val = str(r["social_fit"] or "ALL") if "social_fit" in keys else "ALL"
        p_fit_val = str(r["pace_fit"] or "ALL") if "pace_fit" in keys else "ALL"
        setting_raw = r["setting"] if "setting" in keys else SettingType.HOME.value
        try:
            setting_val = SettingType(setting_raw or SettingType.HOME.value)
        except ValueError:
            # Legacy databases never had this dimension.  Do not infer URBAN
            # from the old INDOOR activity value; seed data performs explicit
            # updates when a more specific setting is known.
            setting_val = SettingType.HOME
        return YoloCuration(
            id=cid,
            title=str(r["title"]),
            summary=str(r["summary"]),
            season=Season(r["season"]),
            weather_class=WeatherClass(r["weather_class"]),
            activity_mode=ActivityMode(r["activity_mode"]),
            setting=setting_val,
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
            hero_image_url=str(r["hero_image_url"] or ""),
            hero_image_alt=str(r["hero_image_alt"] or ""),
            gallery_images=tuple(
                GalleryImage(url=str(g["url"]), alt=str(g["alt"])) for g in gallery_rows
            ),
            timeline=tuple(
                TimelineEntry(
                    time=str(t["time_label"]),
                    spot=str(t["spot_name"]),
                    desc=str(t["description"]),
                )
                for t in timeline_rows
            ),
            budget_breakdown=tuple(
                BudgetLine(
                    item=str(b["item_name"]),
                    cost_krw=int(b["cost_krw"]),
                    category=str(b["category"]),
                )
                for b in budget_rows
            ),
            solo_secrets=str(r["solo_secrets"] or ""),
            map_naver_url=str(r["map_naver_url"] or ""),
            map_kakao_url=str(r["map_kakao_url"] or ""),
            time_slot=_safe_time_slot(r["time_slot"]),
            solo_difficulty=diff_val,
            social_fit=s_fit_val,
            pace_fit=p_fit_val,
            mbti_tags=tuple(str(m["mbti"]) for m in mbti_rows),
            how_to_steps=tuple(str(h["content"]) for h in howto_rows),
            checklist=tuple(str(c["content"]) for c in check_rows),
        )

    def get_curation(self, curation_id: str) -> Optional[YoloCuration]:
        cid = str(curation_id).strip()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM yolo_curations WHERE id = ? AND is_active = 1", (cid,)
            ).fetchone()
            return self._row_to_curation(conn, row) if row else None

    def list_active_curations(self) -> list[YoloCuration]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM yolo_curations WHERE is_active = 1 ORDER BY priority DESC, id ASC"
            ).fetchall()
            result: list[YoloCuration] = []
            for r in rows:
                curation = self._row_to_curation(conn, r)
                if curation is not None:
                    result.append(curation)
            return result

    # ------------------------------------------------------------------
    # User Curation Activity Logs (rotation engine & saved/done locker)
    # ------------------------------------------------------------------
    def log_curation_action(
        self,
        chat_id: str,
        curation_id: str,
        action: str,
        event_key: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> bool:
        """Appends one SEEN/SAVED/DONE/SKIP event.
        Returns False when `event_key` was already recorded (`INSERT OR IGNORE` dedupe)."""
        act = str(action).strip().upper()
        if act not in CURATION_ACTIONS:
            raise ValueError(f"Invalid curation action: {action!r}")
        cid = str(chat_id).strip()
        cur_id = str(curation_id).strip()
        if not cid or not cur_id:
            raise ValueError("chat_id and curation_id must not be empty")
        created_at = _to_kst(now).isoformat()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT OR IGNORE INTO yolo_user_curation_logs (
                    chat_id, curation_id, action, created_at, event_key
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (cid, cur_id, act, created_at, event_key),
            )
            return cur.rowcount == 1

    def get_last_seen_batch(self, chat_id: str) -> tuple[Optional[str], list[str]]:
        """Returns `(created_at, curation_ids)` of the most recent SEEN batch (the card on screen)."""
        cid = str(chat_id).strip()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT created_at FROM yolo_user_curation_logs
                WHERE chat_id = ? AND action = 'SEEN'
                ORDER BY created_at DESC, log_id DESC LIMIT 1
                """,
                (cid,),
            ).fetchone()
            if row is None:
                return None, []
            batch_at = str(row["created_at"])
            ids = conn.execute(
                """
                SELECT curation_id FROM yolo_user_curation_logs
                WHERE chat_id = ? AND action = 'SEEN' AND created_at = ?
                ORDER BY log_id ASC
                """,
                (cid, batch_at),
            ).fetchall()
            return batch_at, list(dict.fromkeys(str(r["curation_id"]) for r in ids))

    def get_user_locker(self, chat_id: str) -> dict[str, list[str]]:
        """Latest-state locker rule (`ORDER BY created_at DESC, log_id DESC` over SAVED/DONE):
        - Latest event == 'SAVED' -> in `saved`
        - Latest event == 'DONE'  -> in `done`"""
        cid = str(chat_id).strip()
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT curation_id, action FROM yolo_user_curation_logs
                WHERE chat_id = ? AND action IN ('SAVED', 'DONE')
                ORDER BY created_at DESC, log_id DESC
                """,
                (cid,),
            ).fetchall()
        latest_by_curation: dict[str, str] = {}
        saved_ids: list[str] = []
        done_ids: list[str] = []
        for r in rows:
            cur_id = str(r["curation_id"])
            if cur_id in latest_by_curation:
                continue
            act = str(r["action"])
            latest_by_curation[cur_id] = act
            if act == "SAVED":
                saved_ids.append(cur_id)
            elif act == "DONE":
                done_ids.append(cur_id)
        return {"saved": saved_ids, "done": done_ids}

    def get_user_curation_stats(
        self, chat_id: str, now: Optional[datetime] = None
    ) -> dict[str, dict[str, Any]]:
        """Per-curation rotation stats for `chat_id`:
        `seen_30d_count`, `days_since_seen`, `days_since_done`, `days_since_skip`
        (float days or None), `has_recent_saved` (bool, only when latest SAVED/DONE is SAVED),
        `last_seen_iso` (str or None)."""
        cid = str(chat_id).strip()
        ref = _to_kst(now)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT curation_id, action, created_at FROM yolo_user_curation_logs
                WHERE chat_id = ?
                ORDER BY created_at DESC, log_id DESC
                """,
                (cid,),
            ).fetchall()

        stats: dict[str, dict[str, Any]] = {}
        latest_saved_or_done: dict[str, str] = {}
        for r in rows:
            ts = _parse_iso(str(r["created_at"]))
            if ts is None:
                continue
            age_days = max(0.0, (ref - ts).total_seconds() / 86400.0)
            if age_days > STATS_LOOKBACK_DAYS:
                continue
            cur_id = str(r["curation_id"])
            entry = stats.setdefault(
                cur_id,
                {
                    "seen_30d_count": 0,
                    "days_since_seen": None,
                    "days_since_done": None,
                    "days_since_skip": None,
                    "has_recent_saved": False,
                    "last_seen_iso": None,
                },
            )
            action = str(r["action"])
            if action in ("SAVED", "DONE") and cur_id not in latest_saved_or_done:
                latest_saved_or_done[cur_id] = action

            if action == "SEEN":
                if age_days <= SEEN_COUNT_WINDOW_DAYS:
                    entry["seen_30d_count"] += 1
                if entry["days_since_seen"] is None or age_days < entry["days_since_seen"]:
                    entry["days_since_seen"] = age_days
                    entry["last_seen_iso"] = ts.isoformat()
            elif action == "DONE":
                if entry["days_since_done"] is None or age_days < entry["days_since_done"]:
                    entry["days_since_done"] = age_days
            elif action == "SKIP":
                if entry["days_since_skip"] is None or age_days < entry["days_since_skip"]:
                    entry["days_since_skip"] = age_days
            elif (
                action == "SAVED"
                and age_days <= SAVED_RECENT_WINDOW_DAYS
                and latest_saved_or_done.get(cur_id) == "SAVED"
            ):
                entry["has_recent_saved"] = True
        return stats

    def get_dispatch_status(
        self, chat_id: str, week_key: str, dispatch_type: str = "WEEKLY_REGULAR"
    ) -> Optional[str]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT sent_status FROM yolo_dispatch_logs
                WHERE chat_id = ? AND week_key = ? AND dispatch_type = ?
                """,
                (str(chat_id).strip(), week_key, dispatch_type),
            ).fetchone()
            return str(row["sent_status"]) if row else None

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
