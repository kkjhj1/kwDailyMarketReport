"""SQLite storage for Kids Compass (키즈 나침반). `:memory:` fully supported ([TG-06])."""
from __future__ import annotations
import json
import secrets
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional, Union

from kids_compass.models import (
    MILESTONE_KINDS,
    MILESTONE_STATUSES,
    AgeGroup,
    BookingEvent,
    ChildProfile,
    ExperienceCategory,
    ExperienceItem,
    IngredientType,
    MedicineProduct,
    MilestoneDefinition,
    MoonlightHospital,
    PregnancyProfile,
    SubjectMilestone,
    WeatherSnapshot,
)

DEFAULT_DB_PATH = "kids_compass.sqlite3"
# Terminal delivery states (SENT / ambiguous-timeout / blocked by user) — FAILED rows are retried next run
DONE_STATES = ("SENT", "UNKNOWN", "BLOCKED")

WEEKEND_MODES = ("TOGETHER", "PRIMARY")
SUBJECT_KINDS = ("CHILD", "PREGNANCY")
SENT_ENTITY_TYPES = ("BOOKING", "EXPERIENCE", "MILESTONE")
SENT_STATUSES = ("IN_FLIGHT", "SENT", "UNKNOWN", "FAILED", "BLOCKED")
# Rows that suppress a re-send / re-claim. Only FAILED may be claimed again.
SENT_BLOCKING_STATES = ("IN_FLIGHT", "SENT", "UNKNOWN", "BLOCKED")
# Rows that count toward the place cooldown (BLOCKED/FAILED never reached the user)
SENT_COOLDOWN_STATES = ("IN_FLIGHT", "SENT", "UNKNOWN")
FEEDBACK_SUPPRESS_DAYS = {"VISITED": 28, "NOT_INTERESTED": 90}

TS_FMT = "%Y-%m-%d %H:%M:%S"
When = Union[datetime, date, str]

SCHEMA = """
CREATE TABLE IF NOT EXISTS kids_user_profiles (
    telegram_user_id TEXT PRIMARY KEY,
    default_region TEXT NOT NULL DEFAULT '서울',
    alert_weekend_brief INTEGER NOT NULL DEFAULT 1,
    alert_dday_reserve INTEGER NOT NULL DEFAULT 1,
    alert_health_dday INTEGER NOT NULL DEFAULT 1,
    alert_weekend_mode TEXT NOT NULL DEFAULT 'TOGETHER',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS kids_children (
    child_id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id TEXT NOT NULL REFERENCES kids_user_profiles(telegram_user_id) ON DELETE CASCADE,
    nickname TEXT NOT NULL,
    birth_date TEXT NOT NULL,
    weight_kg REAL,
    is_primary INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS kids_medicine_products (
    product_id TEXT PRIMARY KEY,
    product_name TEXT NOT NULL,
    ingredient TEXT NOT NULL,
    concentration_mg_ml REAL NOT NULL,
    min_age_months INTEGER NOT NULL,
    recommended_mg_kg REAL NOT NULL,
    max_single_mg REAL NOT NULL,
    interval_hours INTEGER NOT NULL,
    max_times_per_day INTEGER NOT NULL,
    max_daily_mg_kg REAL NOT NULL,
    disclaimer TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS kids_dose_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    child_id INTEGER NOT NULL REFERENCES kids_children(child_id) ON DELETE CASCADE,
    product_id TEXT NOT NULL REFERENCES kids_medicine_products(product_id),
    dose_ml REAL NOT NULL,
    dose_mg REAL NOT NULL,
    given_at TEXT NOT NULL,
    notes TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS kids_experiences (
    exp_id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    age_group TEXT NOT NULL,
    category TEXT NOT NULL,
    region_sido TEXT NOT NULL,
    region_sigungu TEXT NOT NULL DEFAULT '',
    venue_name TEXT NOT NULL DEFAULT '',
    fee_type TEXT NOT NULL DEFAULT '',
    fee_detail TEXT NOT NULL DEFAULT '',
    is_indoor INTEGER NOT NULL DEFAULT 1,
    stroller_accessible INTEGER NOT NULL DEFAULT 0,
    nursing_room INTEGER NOT NULL DEFAULT 0,
    booking_required INTEGER NOT NULL DEFAULT 0,
    booking_url TEXT NOT NULL DEFAULT '',
    booking_open_rule TEXT NOT NULL DEFAULT '',
    key_points TEXT NOT NULL DEFAULT '',
    operating_hours TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS kids_booking_events (
    booking_id INTEGER PRIMARY KEY,
    exp_id TEXT NOT NULL REFERENCES kids_experiences(exp_id) ON DELETE CASCADE,
    title TEXT NOT NULL,
    open_datetime TEXT NOT NULL,
    target_date_range TEXT NOT NULL DEFAULT '',
    booking_url TEXT NOT NULL DEFAULT '',
    memo TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS kids_notification_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id TEXT NOT NULL,
    dispatch_key TEXT NOT NULL,
    delivered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    status TEXT NOT NULL,
    UNIQUE(telegram_user_id, dispatch_key)
);
CREATE TABLE IF NOT EXISTS kids_moonlight_hospitals (
    hospital_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    region_sido TEXT NOT NULL,
    region_sigungu TEXT NOT NULL DEFAULT '',
    address TEXT NOT NULL DEFAULT '',
    tel TEXT NOT NULL DEFAULT '',
    weekday_hours TEXT NOT NULL DEFAULT '',
    weekend_hours TEXT NOT NULL DEFAULT '',
    holiday_hours TEXT NOT NULL DEFAULT '',
    paired_pharmacy TEXT NOT NULL DEFAULT '',
    paired_pharmacy_tel TEXT NOT NULL DEFAULT '',
    map_url TEXT NOT NULL DEFAULT '',
    last_verified_at TEXT NOT NULL DEFAULT '',
    is_active INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE IF NOT EXISTS kids_pregnancy_profiles (
    telegram_user_id TEXT PRIMARY KEY,
    nickname TEXT NOT NULL DEFAULT '태명',
    due_date TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS kids_onboarding_state (
    telegram_user_id TEXT PRIMARY KEY,
    flow_id TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL DEFAULT '',
    step TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    expires_at TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS kids_milestone_definitions (
    definition_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,
    code TEXT NOT NULL,
    schedule_version TEXT NOT NULL,
    age_min_months INTEGER,
    age_max_months INTEGER,
    pregnancy_week INTEGER,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS kids_milestone_states (
    milestone_id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id TEXT NOT NULL,
    subject_kind TEXT NOT NULL,
    subject_id TEXT NOT NULL,
    definition_id TEXT NOT NULL REFERENCES kids_milestone_definitions(definition_id),
    schedule_version TEXT NOT NULL,
    due_from TEXT NOT NULL,
    due_to TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'DUE',
    status_source TEXT NOT NULL DEFAULT 'SYSTEM',
    completed_at TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(subject_kind, subject_id, definition_id, schedule_version)
);
CREATE TABLE IF NOT EXISTS kids_recommendation_batches (
    batch_id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id TEXT NOT NULL,
    stream TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kids_recommendation_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id INTEGER NOT NULL REFERENCES kids_recommendation_batches(batch_id) ON DELETE CASCADE,
    callback_token TEXT NOT NULL UNIQUE,
    telegram_user_id TEXT NOT NULL,
    exp_id TEXT NOT NULL,
    rank INTEGER NOT NULL,
    coverage_json TEXT NOT NULL DEFAULT '{}',
    expires_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kids_user_place_feedbacks (
    telegram_user_id TEXT NOT NULL,
    exp_id TEXT NOT NULL,
    action TEXT NOT NULL,
    effective_until TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (telegram_user_id, exp_id)
);
CREATE TABLE IF NOT EXISTS kids_sent_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id TEXT NOT NULL,
    stream TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    dispatch_window TEXT NOT NULL,
    content_hash TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(telegram_user_id, stream, entity_type, entity_id, dispatch_window)
);
CREATE TABLE IF NOT EXISTS kids_weather_snapshots (
    region_sido TEXT NOT NULL,
    target_date TEXT NOT NULL,
    is_rain_or_snow INTEGER NOT NULL DEFAULT 0,
    is_bad_dust INTEGER NOT NULL DEFAULT 0,
    summary_text TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT '',
    fetched_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    is_failed INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (region_sido, target_date)
);
CREATE INDEX IF NOT EXISTS idx_kids_milestone_user ON kids_milestone_states(telegram_user_id, status, due_from);
CREATE INDEX IF NOT EXISTS idx_kids_rec_item_user ON kids_recommendation_items(telegram_user_id, expires_at);
CREATE INDEX IF NOT EXISTS idx_kids_sent_user ON kids_sent_history(telegram_user_id, entity_type, created_at);
CREATE INDEX IF NOT EXISTS idx_kids_exp_filter ON kids_experiences(is_active, age_group, region_sido, category);
CREATE INDEX IF NOT EXISTS idx_kids_hospital_region ON kids_moonlight_hospitals(is_active, region_sido, region_sigungu);
CREATE INDEX IF NOT EXISTS idx_kids_booking_open ON kids_booking_events(open_datetime);
"""


def _now() -> str:
    return datetime.now().strftime(TS_FMT)


def _ts(value: Optional[When]) -> str:
    """Normalise datetime/date/str to a sortable 'YYYY-MM-DD HH:MM:SS' (date -> midnight)."""
    if value is None:
        return _now()
    if isinstance(value, datetime):
        return value.strftime(TS_FMT)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day).strftime(TS_FMT)
    text = str(value).strip()
    return text + " 00:00:00" if len(text) == 10 else text


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


class KidsCompassDatabase:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path, timeout=15.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _columns(self, table: str) -> dict[str, sqlite3.Row]:
        return {r[1]: r for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _migrate(self) -> None:
        if "operating_hours" not in self._columns("kids_experiences"):
            self.conn.execute("ALTER TABLE kids_experiences ADD COLUMN operating_hours TEXT NOT NULL DEFAULT ''")
        profile_cols = self._columns("kids_user_profiles")
        if "alert_health_dday" not in profile_cols:
            self.conn.execute("ALTER TABLE kids_user_profiles ADD COLUMN alert_health_dday INTEGER NOT NULL DEFAULT 1")
        if "alert_weekend_mode" not in profile_cols:
            self.conn.execute(
                "ALTER TABLE kids_user_profiles ADD COLUMN alert_weekend_mode TEXT NOT NULL DEFAULT 'TOGETHER'"
            )
        weight = self._columns("kids_children").get("weight_kg")
        if weight is not None and int(weight[3]) == 1:  # legacy `weight_kg REAL NOT NULL`
            self._rebuild_children_nullable_weight()

    def _rebuild_children_nullable_weight(self) -> None:
        """SQLite cannot drop NOT NULL in place: FK off -> create/copy -> drop -> rename -> FK check -> FK on.
        child_id values are copied verbatim, so kids_dose_events references stay valid."""
        self.conn.commit()  # PRAGMA foreign_keys is a no-op inside a transaction
        self.conn.execute("PRAGMA foreign_keys = OFF")
        try:
            self.conn.execute("BEGIN")
            self.conn.execute("DROP TABLE IF EXISTS kids_children__new")
            self.conn.execute(
                """CREATE TABLE kids_children__new (
                    child_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id TEXT NOT NULL REFERENCES kids_user_profiles(telegram_user_id) ON DELETE CASCADE,
                    nickname TEXT NOT NULL,
                    birth_date TEXT NOT NULL,
                    weight_kg REAL,
                    is_primary INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            self.conn.execute(
                """INSERT INTO kids_children__new
                   (child_id, telegram_user_id, nickname, birth_date, weight_kg, is_primary, created_at, updated_at)
                   SELECT child_id, telegram_user_id, nickname, birth_date, weight_kg, is_primary, created_at, updated_at
                   FROM kids_children"""
            )
            self.conn.execute("DROP TABLE kids_children")
            self.conn.execute("ALTER TABLE kids_children__new RENAME TO kids_children")
            violations = (self.conn.execute("PRAGMA foreign_key_check(kids_children)").fetchall()
                          + self.conn.execute("PRAGMA foreign_key_check(kids_dose_events)").fetchall())
            if violations:
                raise sqlite3.IntegrityError(f"kids_children rebuild FK violations: {len(violations)}")
            self.conn.commit()
        except Exception:
            self.conn.rollback()
            raise
        finally:
            self.conn.execute("PRAGMA foreign_keys = ON")

    def close(self) -> None:
        self.conn.close()

    # ── user profiles ─────────────────────────────────────────
    def ensure_user(self, telegram_user_id: int | str, default_region: str = "서울") -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO kids_user_profiles (telegram_user_id, default_region) VALUES (?, ?)",
            (str(telegram_user_id), default_region),
        )
        self.conn.commit()

    def get_user_profile(self, telegram_user_id: int | str) -> Optional[dict]:
        row = self.conn.execute(
            "SELECT * FROM kids_user_profiles WHERE telegram_user_id = ?", (str(telegram_user_id),)
        ).fetchone()
        return dict(row) if row else None

    ALERT_COLUMNS = {"weekend": "alert_weekend_brief", "dday": "alert_dday_reserve", "health": "alert_health_dday"}

    def set_alert(self, telegram_user_id: int | str, kind: str, enabled: bool) -> None:
        column = self.ALERT_COLUMNS[kind]
        self.ensure_user(telegram_user_id)
        self.conn.execute(
            f"UPDATE kids_user_profiles SET {column} = ?, updated_at = ? WHERE telegram_user_id = ?",
            (1 if enabled else 0, _now(), str(telegram_user_id)),
        )
        self.conn.commit()

    def set_default_region(self, telegram_user_id: int | str, region: str) -> None:
        self.ensure_user(telegram_user_id)
        self.conn.execute(
            "UPDATE kids_user_profiles SET default_region = ?, updated_at = ? WHERE telegram_user_id = ?",
            (region, _now(), str(telegram_user_id)),
        )
        self.conn.commit()

    def set_weekend_mode(self, telegram_user_id: int | str, mode: str) -> None:
        """TOGETHER = 자녀 전체를 함께 고려한 주말 추천 / PRIMARY = 대표 자녀 기준."""
        if mode not in WEEKEND_MODES:
            raise ValueError(f"invalid weekend mode: {mode!r}")
        self.ensure_user(telegram_user_id)
        self.conn.execute(
            "UPDATE kids_user_profiles SET alert_weekend_mode = ?, updated_at = ? WHERE telegram_user_id = ?",
            (mode, _now(), str(telegram_user_id)),
        )
        self.conn.commit()

    def list_alert_subscribers(self, kind: str) -> list[dict]:
        column = self.ALERT_COLUMNS[kind]
        rows = self.conn.execute(
            f"SELECT * FROM kids_user_profiles WHERE {column} = 1 ORDER BY telegram_user_id"
        ).fetchall()
        # Platform SSOT: only ACTIVE users with an ACTIVE kids_bot subscription (no-op when standalone)
        platform_ids = _platform_active_subscriber_ids(self.db_path, ("kids_bot",), conn=self.conn)
        return [
            dict(r) for r in rows
            if platform_ids is None or str(r["telegram_user_id"]).strip() in platform_ids
        ]

    # ── children ──────────────────────────────────────────────
    def add_child(self, telegram_user_id: int | str, nickname: str, birth_date: str,
                  weight_kg: Optional[float] = None) -> int:
        """weight_kg None = 미입력 (onboarding may skip it; the fever calculator must ask first)."""
        uid = str(telegram_user_id)
        self.ensure_user(uid)
        has_primary = self.conn.execute(
            "SELECT 1 FROM kids_children WHERE telegram_user_id = ? AND is_primary = 1", (uid,)
        ).fetchone()
        cur = self.conn.execute(
            "INSERT INTO kids_children (telegram_user_id, nickname, birth_date, weight_kg, is_primary) VALUES (?, ?, ?, ?, ?)",
            (uid, nickname, birth_date, None if weight_kg is None else float(weight_kg), 0 if has_primary else 1),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def set_primary_child(self, telegram_user_id: int | str, child_id: int) -> bool:
        """Owner-scoped: exactly one primary child per user. False if the child is not the user's."""
        uid = str(telegram_user_id)
        if self.get_child(child_id, uid) is None:
            return False
        with self.conn:
            self.conn.execute(
                "UPDATE kids_children SET is_primary = CASE WHEN child_id = ? THEN 1 ELSE 0 END, updated_at = ? "
                "WHERE telegram_user_id = ?",
                (int(child_id), _now(), uid),
            )
        return True

    def has_active_profile(self, telegram_user_id: int | str) -> bool:
        """True when the user registered at least one child OR a pregnancy profile."""
        uid = str(telegram_user_id)
        row = self.conn.execute(
            "SELECT EXISTS(SELECT 1 FROM kids_children WHERE telegram_user_id = ?) "
            "OR EXISTS(SELECT 1 FROM kids_pregnancy_profiles WHERE telegram_user_id = ?)",
            (uid, uid),
        ).fetchone()
        return bool(row[0])

    @staticmethod
    def _child(row: sqlite3.Row) -> ChildProfile:
        return ChildProfile(
            child_id=row["child_id"], telegram_user_id=row["telegram_user_id"], nickname=row["nickname"],
            birth_date=row["birth_date"], weight_kg=row["weight_kg"], is_primary=bool(row["is_primary"]),
        )

    def list_children(self, telegram_user_id: int | str) -> list[ChildProfile]:
        rows = self.conn.execute(
            "SELECT * FROM kids_children WHERE telegram_user_id = ? ORDER BY is_primary DESC, child_id",
            (str(telegram_user_id),),
        ).fetchall()
        return [self._child(r) for r in rows]

    def get_child(self, child_id: int, telegram_user_id: int | str) -> Optional[ChildProfile]:
        """Owner-scoped lookup — a stale/forged callback can never read another user's child."""
        row = self.conn.execute(
            "SELECT * FROM kids_children WHERE child_id = ? AND telegram_user_id = ?",
            (int(child_id), str(telegram_user_id)),
        ).fetchone()
        return self._child(row) if row else None

    def delete_child(self, child_id: int, telegram_user_id: int | str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM kids_children WHERE child_id = ? AND telegram_user_id = ?",
            (int(child_id), str(telegram_user_id)),
        )
        self.conn.commit()
        return cur.rowcount > 0

    # ── pregnancy (separate from children — never used by the fever calculator) ──
    def set_pregnancy(self, telegram_user_id: int | str, nickname: str, due_date: str) -> None:
        uid = str(telegram_user_id)
        self.ensure_user(uid)
        self.conn.execute(
            """INSERT INTO kids_pregnancy_profiles (telegram_user_id, nickname, due_date) VALUES (?, ?, ?)
               ON CONFLICT(telegram_user_id) DO UPDATE SET
                nickname = excluded.nickname, due_date = excluded.due_date, updated_at = ?""",
            (uid, nickname, due_date, _now()),
        )
        self.conn.commit()

    def get_pregnancy(self, telegram_user_id: int | str) -> Optional[PregnancyProfile]:
        row = self.conn.execute(
            "SELECT * FROM kids_pregnancy_profiles WHERE telegram_user_id = ?", (str(telegram_user_id),)
        ).fetchone()
        if not row:
            return None
        return PregnancyProfile(telegram_user_id=row["telegram_user_id"], nickname=row["nickname"],
                                due_date=row["due_date"], created_at=row["created_at"] or "")

    def delete_pregnancy(self, telegram_user_id: int | str) -> bool:
        cur = self.conn.execute(
            "DELETE FROM kids_pregnancy_profiles WHERE telegram_user_id = ?", (str(telegram_user_id),)
        )
        self.conn.commit()
        return cur.rowcount > 0

    # ── medicines & dose log ──────────────────────────────────
    def upsert_medicines(self, products: Iterable[MedicineProduct]) -> int:
        n = 0
        for p in products:
            self.conn.execute(
                """INSERT OR REPLACE INTO kids_medicine_products
                   (product_id, product_name, ingredient, concentration_mg_ml, min_age_months, recommended_mg_kg,
                    max_single_mg, interval_hours, max_times_per_day, max_daily_mg_kg, disclaimer)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (p.product_id, p.product_name, p.ingredient.value, p.concentration_mg_ml, p.min_age_months,
                 p.recommended_mg_kg, p.max_single_mg, p.interval_hours, p.max_times_per_day,
                 p.max_daily_mg_kg, p.disclaimer),
            )
            n += 1
        self.conn.commit()
        return n

    def list_medicines(self) -> dict[str, MedicineProduct]:
        rows = self.conn.execute("SELECT * FROM kids_medicine_products ORDER BY rowid").fetchall()
        return {
            r["product_id"]: MedicineProduct(
                product_id=r["product_id"], product_name=r["product_name"], ingredient=IngredientType(r["ingredient"]),
                concentration_mg_ml=r["concentration_mg_ml"], min_age_months=r["min_age_months"],
                recommended_mg_kg=r["recommended_mg_kg"], max_single_mg=r["max_single_mg"],
                interval_hours=r["interval_hours"], max_times_per_day=r["max_times_per_day"],
                max_daily_mg_kg=r["max_daily_mg_kg"], disclaimer=r["disclaimer"],
            )
            for r in rows
        }

    def log_dose_event(self, child_id: int, product_id: str, dose_ml: float, dose_mg: float,
                       given_at: Optional[str] = None, notes: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO kids_dose_events (child_id, product_id, dose_ml, dose_mg, given_at, notes) VALUES (?, ?, ?, ?, ?, ?)",
            (int(child_id), product_id, float(dose_ml), float(dose_mg), given_at or _now(), notes),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    # ── experiences ───────────────────────────────────────────
    def upsert_experiences(self, items: Iterable[ExperienceItem]) -> int:
        n = 0
        for e in items:
            self.conn.execute(
                """INSERT INTO kids_experiences
                   (exp_id, title, age_group, category, region_sido, region_sigungu, venue_name, fee_type, fee_detail,
                    is_indoor, stroller_accessible, nursing_room, booking_required, booking_url, booking_open_rule,
                    key_points, operating_hours, is_active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                   ON CONFLICT(exp_id) DO UPDATE SET
                    title=excluded.title, age_group=excluded.age_group, category=excluded.category,
                    region_sido=excluded.region_sido, region_sigungu=excluded.region_sigungu,
                    venue_name=excluded.venue_name, fee_type=excluded.fee_type, fee_detail=excluded.fee_detail,
                    is_indoor=excluded.is_indoor, stroller_accessible=excluded.stroller_accessible,
                    nursing_room=excluded.nursing_room, booking_required=excluded.booking_required,
                    booking_url=excluded.booking_url, booking_open_rule=excluded.booking_open_rule,
                    key_points=excluded.key_points, operating_hours=excluded.operating_hours""",
                (e.exp_id, e.title, e.age_group.value, e.category.value, e.region_sido, e.region_sigungu,
                 e.venue_name, e.fee_type, e.fee_detail, int(e.is_indoor), int(e.stroller_accessible),
                 int(e.nursing_room), int(e.booking_required), e.booking_url, e.booking_open_rule, e.key_points,
                 e.operating_hours),
            )
            n += 1
        self.conn.commit()
        return n

    @staticmethod
    def _experience(r: sqlite3.Row) -> ExperienceItem:
        return ExperienceItem(
            exp_id=r["exp_id"], title=r["title"], age_group=AgeGroup(r["age_group"]),
            category=ExperienceCategory(r["category"]), region_sido=r["region_sido"],
            region_sigungu=r["region_sigungu"], venue_name=r["venue_name"], fee_type=r["fee_type"],
            fee_detail=r["fee_detail"], is_indoor=bool(r["is_indoor"]),
            stroller_accessible=bool(r["stroller_accessible"]), nursing_room=bool(r["nursing_room"]),
            booking_required=bool(r["booking_required"]), booking_url=r["booking_url"],
            booking_open_rule=r["booking_open_rule"], key_points=r["key_points"],
            operating_hours=r["operating_hours"] if "operating_hours" in r.keys() else "",
        )

    def list_experiences(
        self,
        age_group: Optional[AgeGroup] = None,
        category: Optional[ExperienceCategory] = None,
        region_sido: Optional[str] = None,
        indoor_only: bool = False,
    ) -> list[ExperienceItem]:
        """age_group filter also matches AgeGroup.ALL items; AgeGroup.ALL itself means 'no age filter'."""
        sql, params = "SELECT * FROM kids_experiences WHERE is_active = 1", []
        if age_group is not None and age_group != AgeGroup.ALL:
            sql += " AND age_group IN (?, ?)"
            params += [age_group.value, AgeGroup.ALL.value]
        if category is not None:
            sql += " AND category = ?"
            params.append(category.value)
        if region_sido:
            sql += " AND region_sido = ?"
            params.append(region_sido)
        if indoor_only:
            sql += " AND is_indoor = 1"
        sql += " ORDER BY rowid"
        return [self._experience(r) for r in self.conn.execute(sql, params).fetchall()]

    def get_experience(self, exp_id: str) -> Optional[ExperienceItem]:
        row = self.conn.execute("SELECT * FROM kids_experiences WHERE exp_id = ?", (exp_id,)).fetchone()
        return self._experience(row) if row else None

    def count_experiences(self) -> int:
        return int(self.conn.execute("SELECT COUNT(*) FROM kids_experiences WHERE is_active = 1").fetchone()[0])

    # ── booking D-Day events ──────────────────────────────────
    def upsert_booking_events(self, events: Iterable[BookingEvent]) -> int:
        n = 0
        for b in events:
            self.conn.execute(
                """INSERT OR REPLACE INTO kids_booking_events
                   (booking_id, exp_id, title, open_datetime, target_date_range, booking_url, memo)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (b.booking_id, b.exp_id, b.title, b.open_datetime, b.target_date_range, b.booking_url, b.memo),
            )
            n += 1
        self.conn.commit()
        return n

    def list_upcoming_bookings(self, start: str, end: str) -> list[BookingEvent]:
        """Events with start <= open_datetime < end ('YYYY-MM-DD HH:MM' strings sort chronologically)."""
        rows = self.conn.execute(
            "SELECT * FROM kids_booking_events WHERE open_datetime >= ? AND open_datetime < ? ORDER BY open_datetime, booking_id",
            (start, end),
        ).fetchall()
        return [
            BookingEvent(booking_id=r["booking_id"], exp_id=r["exp_id"], title=r["title"],
                         open_datetime=r["open_datetime"], target_date_range=r["target_date_range"],
                         booking_url=r["booking_url"], memo=r["memo"])
            for r in rows
        ]

    # ── moonlight hospitals ───────────────────────────────────
    def upsert_hospitals(self, hospitals: Iterable[MoonlightHospital]) -> int:
        n = 0
        for h in hospitals:
            self.conn.execute(
                """INSERT OR REPLACE INTO kids_moonlight_hospitals
                   (hospital_id, name, region_sido, region_sigungu, address, tel, weekday_hours, weekend_hours,
                    holiday_hours, paired_pharmacy, paired_pharmacy_tel, map_url, last_verified_at, is_active)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)""",
                (h.hospital_id, h.name, h.region_sido, h.region_sigungu, h.address, h.tel, h.weekday_hours,
                 h.weekend_hours, h.holiday_hours, h.paired_pharmacy, h.paired_pharmacy_tel, h.map_url,
                 h.last_verified_at),
            )
            n += 1
        self.conn.commit()
        return n

    def list_hospitals(self, region_sido: Optional[str] = None) -> list[MoonlightHospital]:
        sql, params = "SELECT * FROM kids_moonlight_hospitals WHERE is_active = 1", []
        if region_sido:
            sql += " AND region_sido = ?"
            params.append(region_sido)
        sql += " ORDER BY region_sido, region_sigungu, name"
        return [
            MoonlightHospital(
                hospital_id=r["hospital_id"], name=r["name"], region_sido=r["region_sido"],
                region_sigungu=r["region_sigungu"], address=r["address"], tel=r["tel"],
                weekday_hours=r["weekday_hours"], weekend_hours=r["weekend_hours"],
                holiday_hours=r["holiday_hours"], paired_pharmacy=r["paired_pharmacy"],
                paired_pharmacy_tel=r["paired_pharmacy_tel"], map_url=r["map_url"],
                last_verified_at=r["last_verified_at"],
            )
            for r in self.conn.execute(sql, params).fetchall()
        ]

    # ── notification idempotency ──────────────────────────────
    def has_dispatched(self, dispatch_key: str, telegram_user_id: int | str) -> bool:
        row = self.conn.execute(
            f"SELECT 1 FROM kids_notification_deliveries WHERE telegram_user_id = ? AND dispatch_key = ? "
            f"AND status IN ({','.join('?' * len(DONE_STATES))})",
            (str(telegram_user_id), dispatch_key, *DONE_STATES),
        ).fetchone()
        return row is not None

    def log_dispatch(self, dispatch_key: str, telegram_user_id: int | str, status: str) -> None:
        self.conn.execute(
            """INSERT INTO kids_notification_deliveries (telegram_user_id, dispatch_key, delivered_at, status)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(telegram_user_id, dispatch_key) DO UPDATE SET
                delivered_at = excluded.delivered_at, status = excluded.status""",
            (str(telegram_user_id), dispatch_key, _now(), status),
        )
        self.conn.commit()

    # ── onboarding state (multi-step 1:1 flows) ───────────────
    def set_onboarding_state(self, telegram_user_id: int | str, flow_id: str, kind: str, step: str,
                             payload: Optional[dict], expires_at: When) -> None:
        self.conn.execute(
            """INSERT INTO kids_onboarding_state (telegram_user_id, flow_id, kind, step, payload_json, expires_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(telegram_user_id) DO UPDATE SET
                flow_id = excluded.flow_id, kind = excluded.kind, step = excluded.step,
                payload_json = excluded.payload_json, expires_at = excluded.expires_at, updated_at = excluded.updated_at""",
            (str(telegram_user_id), flow_id, kind, step, json.dumps(payload or {}, ensure_ascii=False),
             _ts(expires_at), _now()),
        )
        self.conn.commit()

    def get_onboarding_state(self, telegram_user_id: int | str, now: Optional[When] = None) -> Optional[dict]:
        """Expired flows are treated as absent (and cleaned up)."""
        row = self.conn.execute(
            "SELECT * FROM kids_onboarding_state WHERE telegram_user_id = ?", (str(telegram_user_id),)
        ).fetchone()
        if not row:
            return None
        if row["expires_at"] and row["expires_at"] <= _ts(now):
            self.clear_onboarding_state(telegram_user_id)
            return None
        state = dict(row)
        try:
            state["payload"] = json.loads(row["payload_json"] or "{}")
        except ValueError:
            state["payload"] = {}
        return state

    def clear_onboarding_state(self, telegram_user_id: int | str) -> None:
        self.conn.execute("DELETE FROM kids_onboarding_state WHERE telegram_user_id = ?", (str(telegram_user_id),))
        self.conn.commit()

    # ── milestones (건강검진 / 예방접종 / 임신 주차) ───────────
    def upsert_milestone_definitions(self, defs: Iterable[MilestoneDefinition]) -> int:
        n = 0
        for d in defs:
            if d.kind not in MILESTONE_KINDS:
                raise ValueError(f"invalid milestone kind: {d.kind!r}")
            self.conn.execute(
                """INSERT OR REPLACE INTO kids_milestone_definitions
                   (definition_id, kind, code, schedule_version, age_min_months, age_max_months, pregnancy_week,
                    title, description, source_url)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (d.definition_id, d.kind, d.code, d.schedule_version, d.age_min_months, d.age_max_months,
                 d.pregnancy_week, d.title, d.description, d.source_url),
            )
            n += 1
        self.conn.commit()
        return n

    def list_milestone_definitions(self, kind: Optional[str] = None,
                                   schedule_version: Optional[str] = None) -> list[MilestoneDefinition]:
        sql, params = "SELECT * FROM kids_milestone_definitions WHERE 1 = 1", []
        if kind:
            sql += " AND kind = ?"
            params.append(kind)
        if schedule_version:
            sql += " AND schedule_version = ?"
            params.append(schedule_version)
        sql += " ORDER BY kind, COALESCE(age_min_months, pregnancy_week), definition_id"
        return [
            MilestoneDefinition(
                definition_id=r["definition_id"], kind=r["kind"], code=r["code"],
                schedule_version=r["schedule_version"], age_min_months=r["age_min_months"],
                age_max_months=r["age_max_months"], pregnancy_week=r["pregnancy_week"], title=r["title"],
                description=r["description"], source_url=r["source_url"],
            )
            for r in self.conn.execute(sql, params).fetchall()
        ]

    @staticmethod
    def _milestone(r: sqlite3.Row) -> SubjectMilestone:
        return SubjectMilestone(
            milestone_id=r["milestone_id"], subject_id=r["subject_id"], subject_kind=r["subject_kind"],
            definition_id=r["definition_id"], due_from=r["due_from"], due_to=r["due_to"], status=r["status"],
            status_source=r["status_source"], completed_at=r["completed_at"],
            schedule_version=r["schedule_version"], telegram_user_id=r["telegram_user_id"],
        )

    def ensure_subject_milestone(self, telegram_user_id: int | str, subject_kind: str, subject_id: int | str,
                                 definition_id: str, schedule_version: str, due_from: str, due_to: str) -> int:
        """Idempotent on (subject_kind, subject_id, definition_id, schedule_version): an existing row keeps its
        status (a user's USER_CONFIRMED / DEFERRED answer is never reset by re-sync); only the window refreshes."""
        if subject_kind not in SUBJECT_KINDS:
            raise ValueError(f"invalid subject kind: {subject_kind!r}")
        uid, sid = str(telegram_user_id), str(subject_id)
        self.conn.execute(
            """INSERT INTO kids_milestone_states
               (telegram_user_id, subject_kind, subject_id, definition_id, schedule_version, due_from, due_to,
                status, status_source, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'DUE', 'SYSTEM', ?)
               ON CONFLICT(subject_kind, subject_id, definition_id, schedule_version) DO UPDATE SET
                due_from = excluded.due_from, due_to = excluded.due_to, updated_at = excluded.updated_at
               WHERE kids_milestone_states.telegram_user_id = excluded.telegram_user_id""",
            (uid, subject_kind, sid, definition_id, schedule_version, due_from, due_to, _now()),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT milestone_id FROM kids_milestone_states WHERE subject_kind = ? AND subject_id = ? "
            "AND definition_id = ? AND schedule_version = ? AND telegram_user_id = ?",
            (subject_kind, sid, definition_id, schedule_version, uid),
        ).fetchone()
        if row is None:
            raise PermissionError("milestone subject belongs to another user")
        return int(row[0])

    def set_milestone_status(self, telegram_user_id: int | str, milestone_id: int, status: str,
                             status_source: str = "USER", now: Optional[When] = None) -> bool:
        """Owner-scoped transition. completed_at is set ONLY for USER_CONFIRMED (cleared otherwise)."""
        if status not in MILESTONE_STATUSES:
            raise ValueError(f"invalid milestone status: {status!r}")
        stamp = _ts(now)
        cur = self.conn.execute(
            "UPDATE kids_milestone_states SET status = ?, status_source = ?, completed_at = ?, updated_at = ? "
            "WHERE milestone_id = ? AND telegram_user_id = ?",
            (status, status_source, stamp if status == "USER_CONFIRMED" else None, stamp,
             int(milestone_id), str(telegram_user_id)),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_subject_milestone(self, telegram_user_id: int | str, milestone_id: int) -> Optional[SubjectMilestone]:
        row = self.conn.execute(
            "SELECT * FROM kids_milestone_states WHERE milestone_id = ? AND telegram_user_id = ?",
            (int(milestone_id), str(telegram_user_id)),
        ).fetchone()
        return self._milestone(row) if row else None

    def list_subject_milestones(self, telegram_user_id: int | str, subject_kind: Optional[str] = None,
                                subject_id: Optional[int | str] = None,
                                statuses: Optional[Iterable[str]] = None) -> list[SubjectMilestone]:
        sql, params = "SELECT * FROM kids_milestone_states WHERE telegram_user_id = ?", [str(telegram_user_id)]
        if subject_kind:
            sql += " AND subject_kind = ?"
            params.append(subject_kind)
        if subject_id is not None:
            sql += " AND subject_id = ?"
            params.append(str(subject_id))
        if statuses is not None:
            statuses = tuple(statuses)
            if not statuses:
                return []
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            params += list(statuses)
        sql += " ORDER BY due_from, milestone_id"
        return [self._milestone(r) for r in self.conn.execute(sql, params).fetchall()]

    def list_due_milestones(self, telegram_user_id: int | str, today: date,
                            lookahead_days: int = 0) -> list[SubjectMilestone]:
        """Push candidates: status == DUE only (UNKNOWN/DEFERRED/NOT_APPLICABLE/USER_CONFIRMED are never
        re-pushed) whose window overlaps [today, today + lookahead_days]."""
        start, end = today.isoformat(), (today + timedelta(days=lookahead_days)).isoformat()
        rows = self.conn.execute(
            "SELECT * FROM kids_milestone_states WHERE telegram_user_id = ? AND status = 'DUE' "
            "AND due_from <= ? AND due_to >= ? ORDER BY due_from, milestone_id",
            (str(telegram_user_id), end, start),
        ).fetchall()
        return [self._milestone(r) for r in rows]

    # ── recommendation callback tokens (ownership + expiry verified in DB) ──
    def create_rec_items(self, telegram_user_id: int | str, stream: str, items: Iterable[ExperienceItem],
                         expires_at: When, coverage: Optional[dict] = None,
                         now: Optional[When] = None) -> list[tuple[ExperienceItem, str]]:
        """Persist one recommendation batch; each item gets a short opaque ASCII token (10 hex chars) to embed in
        callback_data instead of the raw exp_id."""
        uid, exp_ts = str(telegram_user_id), _ts(expires_at)
        items = list(items)
        out: list[tuple[ExperienceItem, str]] = []
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO kids_recommendation_batches (telegram_user_id, stream, created_at, expires_at) "
                "VALUES (?, ?, ?, ?)",
                (uid, stream, _ts(now), exp_ts),
            )
            batch_id = int(cur.lastrowid)
            for rank, item in enumerate(items, start=1):
                cov = dict(coverage or {})
                cov.setdefault("region_sido", item.region_sido)
                cov.setdefault("age_group", item.age_group.value)
                cov.setdefault("is_indoor", bool(item.is_indoor))
                for _ in range(8):
                    token = secrets.token_hex(5)
                    try:
                        self.conn.execute(
                            """INSERT INTO kids_recommendation_items
                               (batch_id, callback_token, telegram_user_id, exp_id, rank, coverage_json, expires_at)
                               VALUES (?, ?, ?, ?, ?, ?, ?)""",
                            (batch_id, token, uid, item.exp_id, rank, json.dumps(cov, ensure_ascii=False), exp_ts),
                        )
                        break
                    except sqlite3.IntegrityError:
                        continue
                else:
                    raise RuntimeError("could not allocate a unique callback token")
                out.append((item, token))
        return out

    def resolve_rec_token(self, telegram_user_id: int | str, token: str,
                          now: Optional[When] = None) -> Optional[ExperienceItem]:
        """None unless the token exists, belongs to this user, is unexpired and points to an active place."""
        token = str(token or "")
        if not token.isascii() or not token.isalnum() or len(token) > 32:
            return None
        row = self.conn.execute(
            "SELECT exp_id, expires_at FROM kids_recommendation_items WHERE callback_token = ? AND telegram_user_id = ?",
            (token, str(telegram_user_id)),
        ).fetchone()
        if row is None or row["expires_at"] <= _ts(now):
            return None
        exp = self.get_experience(row["exp_id"])
        return exp if exp is not None and self._is_active_experience(exp.exp_id) else None

    def _is_active_experience(self, exp_id: str) -> bool:
        row = self.conn.execute("SELECT is_active FROM kids_experiences WHERE exp_id = ?", (exp_id,)).fetchone()
        return bool(row and row[0])

    def purge_expired_rec_items(self, now: Optional[When] = None) -> int:
        stamp = _ts(now)
        with self.conn:
            n = self.conn.execute("DELETE FROM kids_recommendation_items WHERE expires_at <= ?", (stamp,)).rowcount
            self.conn.execute("DELETE FROM kids_recommendation_batches WHERE expires_at <= ?", (stamp,))
        return n

    # ── place feedback & exclusion ────────────────────────────
    def record_place_feedback(self, telegram_user_id: int | str, exp_id: str, action: str,
                              now: Optional[When] = None) -> str:
        """VISITED suppresses the place for 28 days, NOT_INTERESTED for 90 days. Returns effective_until."""
        if action not in FEEDBACK_SUPPRESS_DAYS:
            raise ValueError(f"invalid feedback action: {action!r}")
        stamp = _ts(now)
        until = (datetime.strptime(stamp, TS_FMT) + timedelta(days=FEEDBACK_SUPPRESS_DAYS[action])).strftime(TS_FMT)
        self.conn.execute(
            """INSERT INTO kids_user_place_feedbacks (telegram_user_id, exp_id, action, effective_until, created_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(telegram_user_id, exp_id) DO UPDATE SET
                action = excluded.action, effective_until = excluded.effective_until, created_at = excluded.created_at""",
            (str(telegram_user_id), exp_id, action, until, stamp),
        )
        self.conn.commit()
        return until

    def get_excluded_exp_ids(self, telegram_user_id: int | str, today: date, cooldown_days: int = 28) -> set[str]:
        """Active feedback suppressions + places pushed (SENT/UNKNOWN/IN_FLIGHT) within the last `cooldown_days`."""
        uid = str(telegram_user_id)
        day_start = _ts(today)
        excluded = {
            r[0] for r in self.conn.execute(
                "SELECT exp_id FROM kids_user_place_feedbacks WHERE telegram_user_id = ? AND effective_until > ?",
                (uid, day_start),
            ).fetchall()
        }
        since = _ts(today - timedelta(days=max(int(cooldown_days), 0)))
        marks = ",".join("?" * len(SENT_COOLDOWN_STATES))
        excluded |= {
            r[0] for r in self.conn.execute(
                f"SELECT entity_id FROM kids_sent_history WHERE telegram_user_id = ? AND entity_type = 'EXPERIENCE' "
                f"AND status IN ({marks}) AND created_at >= ?",
                (uid, *SENT_COOLDOWN_STATES, since),
            ).fetchall()
        }
        return excluded

    # ── per-entity sent history (atomic claim → finalize) ─────
    def claim_sent_history(self, telegram_user_id: int | str, stream: str, entity_type: str, entity_id: str,
                           dispatch_window: str, content_hash: str = "", now: Optional[When] = None) -> bool:
        """Atomically take the IN_FLIGHT lease. True only for a fresh row or a FAILED retry; an existing
        IN_FLIGHT / SENT / UNKNOWN / BLOCKED row returns False (no duplicate / compensatory send)."""
        if entity_type not in SENT_ENTITY_TYPES:
            raise ValueError(f"invalid entity type: {entity_type!r}")
        cur = self.conn.execute(
            """INSERT INTO kids_sent_history
               (telegram_user_id, stream, entity_type, entity_id, dispatch_window, content_hash, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, 'IN_FLIGHT', ?)
               ON CONFLICT(telegram_user_id, stream, entity_type, entity_id, dispatch_window) DO UPDATE SET
                status = 'IN_FLIGHT', content_hash = excluded.content_hash, created_at = excluded.created_at
               WHERE kids_sent_history.status = 'FAILED'""",
            (str(telegram_user_id), stream, entity_type, str(entity_id), dispatch_window, content_hash, _ts(now)),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def finalize_sent_history(self, telegram_user_id: int | str, stream: str, entity_type: str, entity_id: str,
                              dispatch_window: str, status: str) -> bool:
        """IN_FLIGHT -> SENT / UNKNOWN / FAILED / BLOCKED. Rows not currently IN_FLIGHT are left untouched."""
        if status not in SENT_STATUSES or status == "IN_FLIGHT":
            raise ValueError(f"invalid final status: {status!r}")
        cur = self.conn.execute(
            "UPDATE kids_sent_history SET status = ? WHERE telegram_user_id = ? AND stream = ? AND entity_type = ? "
            "AND entity_id = ? AND dispatch_window = ? AND status = 'IN_FLIGHT'",
            (status, str(telegram_user_id), stream, entity_type, str(entity_id), dispatch_window),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def get_sent_status(self, telegram_user_id: int | str, stream: str, entity_type: str, entity_id: str,
                        dispatch_window: str) -> Optional[str]:
        row = self.conn.execute(
            "SELECT status FROM kids_sent_history WHERE telegram_user_id = ? AND stream = ? AND entity_type = ? "
            "AND entity_id = ? AND dispatch_window = ?",
            (str(telegram_user_id), stream, entity_type, str(entity_id), dispatch_window),
        ).fetchone()
        return row[0] if row else None

    # ── weather cache ─────────────────────────────────────────
    def upsert_weather_snapshot(self, snap: WeatherSnapshot) -> None:
        self.conn.execute(
            """INSERT OR REPLACE INTO kids_weather_snapshots
               (region_sido, target_date, is_rain_or_snow, is_bad_dust, summary_text, provider, fetched_at,
                expires_at, is_failed)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (snap.region_sido, snap.target_date, int(snap.is_rain_or_snow), int(snap.is_bad_dust), snap.summary_text,
             snap.provider, snap.fetched_at, snap.expires_at, int(snap.is_failed)),
        )
        self.conn.commit()

    def get_weather_snapshot(self, region_sido: str, target_date: str,
                             now: Optional[When] = None) -> Optional[WeatherSnapshot]:
        """Unexpired cached snapshot or None."""
        row = self.conn.execute(
            "SELECT * FROM kids_weather_snapshots WHERE region_sido = ? AND target_date = ? AND expires_at > ?",
            (region_sido, target_date, _ts(now)),
        ).fetchone()
        if not row:
            return None
        return WeatherSnapshot(
            region_sido=row["region_sido"], target_date=row["target_date"],
            is_rain_or_snow=bool(row["is_rain_or_snow"]), is_bad_dust=bool(row["is_bad_dust"]),
            summary_text=row["summary_text"], provider=row["provider"], fetched_at=row["fetched_at"],
            expires_at=row["expires_at"], is_failed=bool(row["is_failed"]),
        )
