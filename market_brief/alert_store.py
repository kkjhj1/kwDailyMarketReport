"""Durable one-per-market-per-trading-day reservation and transactional outbox for alerts."""
from contextlib import contextmanager
import sqlite3
from datetime import date, datetime, timezone
from market_brief.core import alert_key, is_alert_threshold_met


class AlertStore:
    """Manages market-day alert deduplication and per-channel delivery audit state."""

    def __init__(self, path="alerts.sqlite3"):
        self.path = path
        with self._connect() as db:
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

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def reserve_if_threshold(self, market: str, market_trade_date: date, change_pct: float,
                             *, symbol: str | None = None) -> bool:
        if not is_alert_threshold_met(change_pct):
            return False
        key = alert_key(market, market_trade_date)
        with self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO alert_reservations (key, status, change_pct, symbol) VALUES (?, 'reserved', ?, ?)",
                (key, float(change_pct), symbol),
            )
            return cursor.rowcount == 1

    def reserve_regular_briefing(self, kind: str, trade_date: date) -> bool:
        """Atomically reserves a regular briefing (morning/closing) for the given trading day.
        Returns True if this is the first time today for this briefing kind, False if already sent.
        """
        key = f"REGULAR:{trade_date.isoformat()}:{kind}"
        with self._connect() as db:
            cursor = db.execute(
                "INSERT OR IGNORE INTO alert_reservations (key, status, change_pct, symbol) VALUES (?, 'sent', 0.0, ?)",
                (key, kind),
            )
            return cursor.rowcount == 1

    def is_regular_briefing_sent(self, kind: str, trade_date: date) -> bool:
        """Checks if a regular briefing (morning/closing) has already been recorded for today."""
        key = f"REGULAR:{trade_date.isoformat()}:{kind}"
        with self._connect() as db:
            row = db.execute("SELECT 1 FROM alert_reservations WHERE key = ?", (key,)).fetchone()
            return row is not None

    def update_reservation_status(self, key: str, status: str) -> None:
        if status not in {"reserved", "sent", "failed_dead_letter"}:
            raise ValueError(f"Invalid status: {status}")
        with self._connect() as db:
            db.execute(
                "UPDATE alert_reservations SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE key = ?",
                (status, key),
            )

    def get_reservation_status(self, key: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT status FROM alert_reservations WHERE key = ?", (key,)).fetchone()
            return row[0] if row else None

    def log_delivery(self, event_key: str, channel: str, recipient: str, status: str,
                     *, provider_msg_id: str | None = None, attempts: int = 1,
                     error_detail: str | None = None) -> None:
        now_iso = datetime.now(timezone.utc).isoformat()
        with self._connect() as db:
            db.execute(
                """INSERT INTO delivery_audit_logs
                   (event_key, channel, recipient, status, provider_msg_id, attempts, error_detail, logged_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_key, channel, recipient, status, provider_msg_id, attempts, error_detail, now_iso),
            )

    def list_delivery_logs(self, event_key: str | None = None) -> list[dict]:
        with self._connect() as db:
            db.row_factory = sqlite3.Row
            if event_key:
                rows = db.execute(
                    "SELECT * FROM delivery_audit_logs WHERE event_key = ? ORDER BY id ASC",
                    (event_key,),
                ).fetchall()
            else:
                rows = db.execute("SELECT * FROM delivery_audit_logs ORDER BY id ASC").fetchall()
            return [dict(r) for r in rows]
