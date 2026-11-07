"""SQLite storage for Kids Compass (키즈 나침반). `:memory:` fully supported ([TG-06])."""
from __future__ import annotations
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Iterable, Optional

from kids_compass.models import (
    AgeGroup,
    BookingEvent,
    ChildProfile,
    ExperienceCategory,
    ExperienceItem,
    IngredientType,
    MedicineProduct,
    MoonlightHospital,
    PregnancyProfile,
)

DEFAULT_DB_PATH = "kids_compass.sqlite3"
# Terminal delivery states (SENT / ambiguous-timeout / blocked by user) — FAILED rows are retried next run
DONE_STATES = ("SENT", "UNKNOWN", "BLOCKED")

SCHEMA = """
CREATE TABLE IF NOT EXISTS kids_user_profiles (
    telegram_user_id TEXT PRIMARY KEY,
    default_region TEXT NOT NULL DEFAULT '서울',
    alert_weekend_brief INTEGER NOT NULL DEFAULT 1,
    alert_dday_reserve INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS kids_children (
    child_id INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_user_id TEXT NOT NULL REFERENCES kids_user_profiles(telegram_user_id) ON DELETE CASCADE,
    nickname TEXT NOT NULL,
    birth_date TEXT NOT NULL,
    weight_kg REAL NOT NULL,
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
CREATE INDEX IF NOT EXISTS idx_kids_exp_filter ON kids_experiences(is_active, age_group, region_sido, category);
CREATE INDEX IF NOT EXISTS idx_kids_hospital_region ON kids_moonlight_hospitals(is_active, region_sido, region_sigungu);
CREATE INDEX IF NOT EXISTS idx_kids_booking_open ON kids_booking_events(open_datetime);
"""


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class KidsCompassDatabase:
    def __init__(self, db_path: str | Path = DEFAULT_DB_PATH):
        self.db_path = str(db_path)
        self.conn = sqlite3.connect(self.db_path, timeout=15.0)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.conn.commit()

    def _migrate(self) -> None:
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(kids_experiences)").fetchall()}
        if "operating_hours" not in cols:
            self.conn.execute("ALTER TABLE kids_experiences ADD COLUMN operating_hours TEXT NOT NULL DEFAULT ''")

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

    def set_alert(self, telegram_user_id: int | str, kind: str, enabled: bool) -> None:
        column = {"weekend": "alert_weekend_brief", "dday": "alert_dday_reserve"}[kind]
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

    def list_alert_subscribers(self, kind: str) -> list[dict]:
        column = {"weekend": "alert_weekend_brief", "dday": "alert_dday_reserve"}[kind]
        rows = self.conn.execute(
            f"SELECT * FROM kids_user_profiles WHERE {column} = 1 ORDER BY telegram_user_id"
        ).fetchall()
        return [dict(r) for r in rows]

    # ── children ──────────────────────────────────────────────
    def add_child(self, telegram_user_id: int | str, nickname: str, birth_date: str, weight_kg: float) -> int:
        uid = str(telegram_user_id)
        self.ensure_user(uid)
        has_primary = self.conn.execute(
            "SELECT 1 FROM kids_children WHERE telegram_user_id = ? AND is_primary = 1", (uid,)
        ).fetchone()
        cur = self.conn.execute(
            "INSERT INTO kids_children (telegram_user_id, nickname, birth_date, weight_kg, is_primary) VALUES (?, ?, ?, ?, ?)",
            (uid, nickname, birth_date, float(weight_kg), 0 if has_primary else 1),
        )
        self.conn.commit()
        return int(cur.lastrowid)

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
