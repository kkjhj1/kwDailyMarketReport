"""SQLite storage for SilverCare (실버케어).
Tables: silver_benefits, silver_parents, silver_bookmarks, silver_sessions, silver_dispatch_log.
Supports ':memory:' for token-free demo / tests ([TG-06]: no production file writes).
"""
from __future__ import annotations
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional

from silver_care.matching import evaluate_benefit, match_benefits
from silver_care.models import BenefitMatch, ParentProfile, SilverBenefit

_SCHEMA = """
CREATE TABLE IF NOT EXISTS silver_benefits (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    jurisdiction_type TEXT NOT NULL,
    category TEXT NOT NULL,
    min_age INTEGER NOT NULL,
    target_income TEXT NOT NULL,
    target_living TEXT NOT NULL,
    support_amount TEXT NOT NULL,
    is_pre_approval_required INTEGER NOT NULL DEFAULT 0,
    requires_prescription INTEGER NOT NULL DEFAULT 0,
    region_code TEXT NOT NULL DEFAULT 'ALL',
    agency_name TEXT NOT NULL,
    application_period TEXT NOT NULL,
    detail_guide TEXT NOT NULL,
    phone_script TEXT NOT NULL,
    parent_msg_template TEXT NOT NULL,
    extra_json TEXT NOT NULL DEFAULT '{}',
    is_active INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS silver_parents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id INTEGER NOT NULL,
    alias TEXT NOT NULL,
    birth_year INTEGER NOT NULL,
    sido_code TEXT NOT NULL,
    sigungu_name TEXT NOT NULL DEFAULT '',
    living_type TEXT NOT NULL,
    is_pension_receiver INTEGER NOT NULL DEFAULT -1,
    is_low_income INTEGER NOT NULL DEFAULT -1,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_silver_parents_chat ON silver_parents(chat_id);
CREATE TABLE IF NOT EXISTS silver_bookmarks (
    chat_id INTEGER NOT NULL,
    parent_id INTEGER NOT NULL,
    benefit_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (chat_id, parent_id, benefit_id)
);
CREATE TABLE IF NOT EXISTS silver_sessions (
    chat_id INTEGER PRIMARY KEY,
    draft_json TEXT NOT NULL DEFAULT '{}',
    weekly_opt_in INTEGER NOT NULL DEFAULT 1,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS silver_dispatch_log (
    dispatch_key TEXT NOT NULL,
    chat_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    sent_at TEXT NOT NULL,
    PRIMARY KEY (dispatch_key, chat_id)
);
"""

_BENEFIT_COLS = (
    "id", "title", "jurisdiction_type", "category", "min_age", "target_income", "target_living",
    "support_amount", "is_pre_approval_required", "requires_prescription", "region_code",
    "agency_name", "application_period", "detail_guide", "phone_script", "parent_msg_template",
)


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class SilverCareDatabase:
    """Single-connection SQLite store (thread-safe via lock) so ':memory:' persists across calls."""

    def __init__(self, db_path: str = "silver_care.sqlite3"):
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(_SCHEMA)
            try:
                self._conn.execute("ALTER TABLE silver_benefits ADD COLUMN extra_json TEXT NOT NULL DEFAULT '{}'")
            except Exception:
                pass
            self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def _exec(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, tuple(params))
            self._conn.commit()
            return cur

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    # ── Benefits ──────────────────────────────────────────────
    def upsert_benefits(self, benefits: Iterable[SilverBenefit]) -> int:
        def _extra(b: SilverBenefit) -> str:
            return json.dumps({
                "documents": b.documents,
                "application_steps": b.application_steps,
                "rejection_response": b.rejection_response,
                "warning_notes": b.warning_notes,
                "source_url": b.source_url,
                "source_agency": b.source_agency,
                "verified_at": b.verified_at,
            }, ensure_ascii=False)

        rows = [
            tuple(int(v) if isinstance(v, bool) else v for v in (getattr(b, c) for c in _BENEFIT_COLS)) + (_extra(b), _now())
            for b in benefits
        ]
        cols = ", ".join(_BENEFIT_COLS) + ", extra_json"
        marks = ", ".join("?" for _ in range(len(_BENEFIT_COLS) + 1))
        updates = ", ".join(f"{c}=excluded.{c}" for c in _BENEFIT_COLS if c != "id") + ", extra_json=excluded.extra_json"
        with self._lock:
            self._conn.executemany(
                f"INSERT INTO silver_benefits ({cols}, updated_at) VALUES ({marks}, ?) "
                f"ON CONFLICT(id) DO UPDATE SET {updates}, updated_at=excluded.updated_at, is_active=1",
                rows,
            )
            self._conn.commit()
        return len(rows)

    @staticmethod
    def _row_to_benefit(row: sqlite3.Row) -> SilverBenefit:
        data = {c: row[c] for c in _BENEFIT_COLS}
        data["is_pre_approval_required"] = bool(data["is_pre_approval_required"])
        data["requires_prescription"] = bool(data["requires_prescription"])
        if "extra_json" in row.keys() and row["extra_json"]:
            try:
                extra = json.loads(row["extra_json"])
                if isinstance(extra, dict):
                    data.update({
                        "documents": extra.get("documents", []),
                        "application_steps": extra.get("application_steps", []),
                        "rejection_response": extra.get("rejection_response", ""),
                        "warning_notes": extra.get("warning_notes", []),
                        "source_url": extra.get("source_url", ""),
                        "source_agency": extra.get("source_agency", ""),
                        "verified_at": extra.get("verified_at", "2026-11-01"),
                    })
            except Exception:
                pass
        return SilverBenefit(**data)

    def list_benefits(self, active_only: bool = True) -> list[SilverBenefit]:
        sql = "SELECT * FROM silver_benefits" + (" WHERE is_active = 1" if active_only else "") + " ORDER BY id"
        return [self._row_to_benefit(r) for r in self._query(sql)]

    def get_benefit(self, benefit_id: int) -> Optional[SilverBenefit]:
        rows = self._query("SELECT * FROM silver_benefits WHERE id = ? AND is_active = 1", (benefit_id,))
        return self._row_to_benefit(rows[0]) if rows else None

    def count_benefits(self) -> int:
        return self._query("SELECT COUNT(*) AS n FROM silver_benefits WHERE is_active = 1")[0]["n"]

    # ── Parents ───────────────────────────────────────────────
    @staticmethod
    def _row_to_parent(row: sqlite3.Row) -> ParentProfile:
        return ParentProfile(
            id=row["id"], chat_id=row["chat_id"], alias=row["alias"], birth_year=row["birth_year"],
            sido_code=row["sido_code"], sigungu_name=row["sigungu_name"], living_type=row["living_type"],
            is_pension_receiver=row["is_pension_receiver"], is_low_income=row["is_low_income"],
        )

    def save_parent(self, parent: ParentProfile) -> int:
        """Inserts (id <= 0) or updates the parent profile. Returns the row id."""
        values = (
            parent.chat_id, parent.alias, parent.birth_year, parent.sido_code, parent.sigungu_name,
            parent.living_type, parent.is_pension_receiver, parent.is_low_income,
        )
        if parent.id and parent.id > 0:
            self._exec(
                "UPDATE silver_parents SET chat_id=?, alias=?, birth_year=?, sido_code=?, sigungu_name=?, "
                "living_type=?, is_pension_receiver=?, is_low_income=? WHERE id=?",
                values + (parent.id,),
            )
            return parent.id
        cur = self._exec(
            "INSERT INTO silver_parents (chat_id, alias, birth_year, sido_code, sigungu_name, living_type, "
            "is_pension_receiver, is_low_income, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values + (_now(),),
        )
        parent.id = int(cur.lastrowid)
        return parent.id

    def get_parent(self, parent_id: int, chat_id: Optional[int] = None) -> Optional[ParentProfile]:
        """Fetches a parent; when chat_id is given, only returns it if owned by that chat."""
        rows = self._query("SELECT * FROM silver_parents WHERE id = ?", (parent_id,))
        if not rows:
            return None
        parent = self._row_to_parent(rows[0])
        if chat_id is not None and int(parent.chat_id) != int(chat_id):
            return None
        return parent

    def list_parents(self, chat_id: int) -> list[ParentProfile]:
        rows = self._query("SELECT * FROM silver_parents WHERE chat_id = ? ORDER BY id", (chat_id,))
        return [self._row_to_parent(r) for r in rows]

    def delete_parent(self, parent_id: int, chat_id: int) -> bool:
        with self._lock:
            cur = self._conn.execute("DELETE FROM silver_parents WHERE id = ? AND chat_id = ?", (parent_id, chat_id))
            if cur.rowcount:
                self._conn.execute(
                    "DELETE FROM silver_bookmarks WHERE parent_id = ? AND chat_id = ?", (parent_id, chat_id)
                )
            self._conn.commit()
            return cur.rowcount > 0

    # ── Matching ──────────────────────────────────────────────
    def match_for_parent(self, parent: ParentProfile, ref_year: Optional[int] = None) -> list[BenefitMatch]:
        return match_benefits(self.list_benefits(), parent, ref_year)

    def evaluate(self, parent: ParentProfile, benefit_id: int, ref_year: Optional[int] = None) -> Optional[BenefitMatch]:
        benefit = self.get_benefit(benefit_id)
        return evaluate_benefit(benefit, parent, ref_year) if benefit else None

    # ── Bookmarks ─────────────────────────────────────────────
    def toggle_bookmark(self, chat_id: int, parent_id: int, benefit_id: int) -> bool:
        """Returns True when the bookmark is now ON."""
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM silver_bookmarks WHERE chat_id = ? AND parent_id = ? AND benefit_id = ?",
                (chat_id, parent_id, benefit_id),
            )
            on = cur.rowcount == 0
            if on:
                self._conn.execute(
                    "INSERT INTO silver_bookmarks (chat_id, parent_id, benefit_id, created_at) VALUES (?, ?, ?, ?)",
                    (chat_id, parent_id, benefit_id, _now()),
                )
            self._conn.commit()
            return on

    def is_bookmarked(self, chat_id: int, parent_id: int, benefit_id: int) -> bool:
        return bool(self._query(
            "SELECT 1 FROM silver_bookmarks WHERE chat_id = ? AND parent_id = ? AND benefit_id = ?",
            (chat_id, parent_id, benefit_id),
        ))

    def list_bookmarks(self, chat_id: int) -> list[tuple[int, int]]:
        """Returns [(parent_id, benefit_id), ...] in saved order."""
        rows = self._query(
            "SELECT parent_id, benefit_id FROM silver_bookmarks WHERE chat_id = ? ORDER BY created_at, benefit_id",
            (chat_id,),
        )
        return [(r["parent_id"], r["benefit_id"]) for r in rows]

    # ── Sessions (wizard draft + weekly opt-in) ───────────────
    def _ensure_session(self, chat_id: int) -> None:
        self._exec(
            "INSERT OR IGNORE INTO silver_sessions (chat_id, draft_json, weekly_opt_in, updated_at) VALUES (?, '{}', 1, ?)",
            (chat_id, _now()),
        )

    def get_draft(self, chat_id: int) -> dict[str, Any]:
        rows = self._query("SELECT draft_json FROM silver_sessions WHERE chat_id = ?", (chat_id,))
        if not rows:
            return {}
        try:
            return json.loads(rows[0]["draft_json"] or "{}")
        except json.JSONDecodeError:
            return {}

    def set_draft(self, chat_id: int, draft: dict[str, Any]) -> None:
        self._ensure_session(chat_id)
        self._exec(
            "UPDATE silver_sessions SET draft_json = ?, updated_at = ? WHERE chat_id = ?",
            (json.dumps(draft, ensure_ascii=False), _now(), chat_id),
        )

    def clear_draft(self, chat_id: int) -> None:
        self.set_draft(chat_id, {})

    def is_weekly_opt_in(self, chat_id: int) -> bool:
        rows = self._query("SELECT weekly_opt_in FROM silver_sessions WHERE chat_id = ?", (chat_id,))
        return bool(rows[0]["weekly_opt_in"]) if rows else True

    def set_weekly_opt_in(self, chat_id: int, on: bool) -> None:
        self._ensure_session(chat_id)
        self._exec(
            "UPDATE silver_sessions SET weekly_opt_in = ?, updated_at = ? WHERE chat_id = ?",
            (int(on), _now(), chat_id),
        )

    def list_weekly_chat_ids(self) -> list[int]:
        """Chats that registered at least one parent and did not opt out of the weekly briefing."""
        rows = self._query(
            "SELECT DISTINCT p.chat_id FROM silver_parents p "
            "LEFT JOIN silver_sessions s ON s.chat_id = p.chat_id "
            "WHERE COALESCE(s.weekly_opt_in, 1) = 1 ORDER BY p.chat_id"
        )
        return [r["chat_id"] for r in rows]

    # ── Dispatch log (weekly briefing idempotency) ────────────
    def has_dispatched(self, dispatch_key: str, chat_id: int) -> bool:
        return bool(self._query(
            "SELECT 1 FROM silver_dispatch_log WHERE dispatch_key = ? AND chat_id = ? AND status = 'SUCCESS'",
            (dispatch_key, chat_id),
        ))

    def log_dispatch(self, dispatch_key: str, chat_id: int, status: str) -> None:
        self._exec(
            "INSERT INTO silver_dispatch_log (dispatch_key, chat_id, status, sent_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(dispatch_key, chat_id) DO UPDATE SET status = excluded.status, sent_at = excluded.sent_at",
            (dispatch_key, chat_id, status, _now()),
        )
