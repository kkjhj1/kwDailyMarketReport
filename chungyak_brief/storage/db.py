"""SQLite sidecar (`chungyak_brief.sqlite3`, next to `alerts.sqlite3`) for chungyak_bot.

Tables (all surfaced in Admin via `market_brief/admin_sync_registry.py`):
- chungyak_notices         : 청약홈 공고 (content_hash + revision for 정정공고 감지)
- chungyak_user_profiles   : 사용자 자격 프로필 (chat_id 외 전 필드 nullable)
- chungyak_regular_subs    : 08:00 정기 다이제스트 구독
- chungyak_deliveries      : 발송 원장 (lease + SENT/UNKNOWN 재발송 차단, QUEUED_MORNING)

Delivery ledger invariants ([PROD-LIVE]):
- SENT / UNKNOWN are terminal: never re-reserved, and they suppress REGULAR_DAILY for the same notice revision.
- A PENDING row whose lease expired without a recorded result becomes UNKNOWN (never retried).
- Only the lease holder (matching `lease_token`) may finalize a PENDING row (fencing).
- All state transitions run inside `BEGIN IMMEDIATE` transactions.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from typing import Any, Iterator, Optional
import uuid

from chungyak_brief import BOT_ID
from chungyak_brief.models import (
    ChannelType,
    ChungyakNotice,
    DeliveryStatus,
    HouseType,
    NoticeType,
    UserProfile,
    decimal_text,
    parse_date,
    to_decimal,
)

TS_FMT = "%Y-%m-%d %H:%M:%S"
MAX_FAILED_ATTEMPTS = 3
WINDOW_MISSED = "WINDOW_MISSED"
LEASE_EXPIRED = "LEASE_EXPIRED"

SCHEMA_SQL: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS chungyak_notices (
        notice_key TEXT PRIMARY KEY,
        notice_type TEXT NOT NULL,
        house_type TEXT NOT NULL DEFAULT 'APT',
        house_manage_no TEXT NOT NULL DEFAULT '',
        pblanc_no TEXT NOT NULL DEFAULT '',
        house_name TEXT NOT NULL,
        region_sido TEXT,
        region_sigungu TEXT,
        address TEXT NOT NULL DEFAULT '',
        supply_households INTEGER,
        announce_date TEXT,
        receipt_start TEXT,
        receipt_end TEXT,
        winner_date TEXT,
        homepage_url TEXT NOT NULL DEFAULT '',
        min_area_m2 TEXT,
        max_area_m2 TEXT,
        min_price_eok TEXT,
        is_regulated INTEGER,
        special_supply_types TEXT NOT NULL DEFAULT '[]',
        is_sample INTEGER NOT NULL DEFAULT 0,
        content_hash TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1,
        first_seen_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS chungyak_user_profiles (
        chat_id TEXT PRIMARY KEY,
        residence_sido TEXT,
        residence_sigungu TEXT,
        residence_years INTEGER,
        is_homeless INTEGER,
        is_household_head INTEGER,
        account_months INTEGER,
        deposit_manwon INTEGER,
        payment_count INTEGER,
        preferred_regions TEXT,
        max_price_eok TEXT,
        desired_area_m2 TEXT,
        special_types TEXT,
        include_officetel INTEGER,
        custom_alert_enabled INTEGER,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS chungyak_regular_subs (
        chat_id TEXT PRIMARY KEY,
        is_enabled INTEGER NOT NULL DEFAULT 1,
        send_time_kst TEXT NOT NULL DEFAULT '08:00',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS chungyak_deliveries (
        delivery_key TEXT PRIMARY KEY,
        chat_id TEXT NOT NULL,
        notice_key TEXT NOT NULL,
        channel_type TEXT NOT NULL,
        status TEXT NOT NULL,
        attempt_no INTEGER NOT NULL DEFAULT 0,
        lease_token TEXT,
        lease_expires_at TEXT,
        provider_msg_id TEXT,
        error_detail TEXT,
        scheduled_at_kst TEXT,
        trigger_context TEXT,
        content_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_chungyak_deliv_chat_notice ON chungyak_deliveries(chat_id, notice_key, content_hash)",
    "CREATE INDEX IF NOT EXISTS idx_chungyak_deliv_status ON chungyak_deliveries(status, scheduled_at_kst)",
    "CREATE INDEX IF NOT EXISTS idx_chungyak_deliv_created ON chungyak_deliveries(created_at)",
)

PROFILE_COLUMNS: tuple[str, ...] = (
    "residence_sido", "residence_sigungu", "residence_years", "is_homeless", "is_household_head",
    "account_months", "deposit_manwon", "payment_count", "preferred_regions", "max_price_eok",
    "desired_area_m2", "special_types", "include_officetel", "custom_alert_enabled",
)
_BOOL_COLS = {"is_homeless", "is_household_head", "include_officetel", "custom_alert_enabled"}
_INT_COLS = {"residence_years", "account_months", "deposit_manwon", "payment_count"}
_LIST_COLS = {"preferred_regions", "special_types"}
_DEC_COLS = {"max_price_eok", "desired_area_m2"}


class SubscriptionGateBlocked(RuntimeError):
    """alerts.sqlite3 active-subscription cross-check is unavailable -> Fail-Closed (BLOCKED)."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_text(now: Optional[datetime]) -> str:
    moment = now or utc_now()
    if moment.tzinfo is not None:
        moment = moment.astimezone(timezone.utc)
    return moment.strftime(TS_FMT)


def make_delivery_key(channel: ChannelType | str, chat_id: str, notice_key: str, content_hash: str) -> str:
    ch = channel.value if isinstance(channel, ChannelType) else str(channel)
    return f"{ch}:{chat_id}:{notice_key}:{content_hash[:12]}"


def load_active_chat_ids(alerts_db_path: str | Path, bot_id: str = BOT_ID) -> set[str]:
    """Active chungyak_bot subscribers from the platform SSOT (`alerts.sqlite3`), opened read-only.

    Raises SubscriptionGateBlocked when the file/tables are missing or unreadable ([PROD-LIVE] Fail-Closed).
    """
    path = Path(alerts_db_path)
    if not path.is_file():
        raise SubscriptionGateBlocked(f"alerts DB not found: {path.name}")
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=15.0)
        rows = conn.execute(
            """SELECT s.telegram_user_id
                 FROM subscriptions s
                 JOIN users u ON u.telegram_user_id = s.telegram_user_id
                WHERE s.bot_id = ?
                  AND s.status IN ('ACTIVE', 'GRACE_PERIOD')
                  AND u.status IN ('ACTIVE', 'GRACE_PERIOD')""",
            (bot_id,),
        ).fetchall()
        return {str(r[0]).strip() for r in rows if r[0] is not None and str(r[0]).strip()}
    except (sqlite3.Error, OSError) as exc:
        raise SubscriptionGateBlocked(f"alerts DB unreadable: {type(exc).__name__}") from exc
    finally:
        if conn is not None:
            conn.close()


class ChungyakDatabase:
    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self._mem_conn: sqlite3.Connection | None = None
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # ------------------------------------------------------------------ connection
    def _open(self) -> sqlite3.Connection:
        if self.db_path == ":memory:":
            if self._mem_conn is None:
                self._mem_conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
                self._mem_conn.row_factory = sqlite3.Row
            return self._mem_conn
        conn = sqlite3.connect(self.db_path, timeout=15.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """BEGIN IMMEDIATE write transaction (serializes concurrent poll / cron writers)."""
        conn = self._open()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.execute("COMMIT")
            except BaseException:
                conn.execute("ROLLBACK")
                raise
        finally:
            if conn is not self._mem_conn:
                conn.close()

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        conn = self._open()
        try:
            yield conn
        finally:
            if conn is not self._mem_conn:
                conn.close()

    def _init_schema(self) -> None:
        with self._tx() as conn:
            for stmt in SCHEMA_SQL:
                conn.execute(stmt)

    def close(self) -> None:
        if self._mem_conn is not None:
            self._mem_conn.close()
            self._mem_conn = None

    # ------------------------------------------------------------------ notices
    @staticmethod
    def _notice_row(notice: ChungyakNotice) -> dict[str, Any]:
        return {
            "notice_key": notice.notice_key,
            "notice_type": notice.notice_type.value,
            "house_type": notice.house_type.value,
            "house_manage_no": notice.house_manage_no,
            "pblanc_no": notice.pblanc_no,
            "house_name": notice.house_name,
            "region_sido": notice.region_sido,
            "region_sigungu": notice.region_sigungu,
            "address": notice.address,
            "supply_households": notice.supply_households,
            "announce_date": notice.announce_date.isoformat() if notice.announce_date else None,
            "receipt_start": notice.receipt_start.isoformat() if notice.receipt_start else None,
            "receipt_end": notice.receipt_end.isoformat() if notice.receipt_end else None,
            "winner_date": notice.winner_date.isoformat() if notice.winner_date else None,
            "homepage_url": notice.homepage_url,
            "min_area_m2": decimal_text(notice.min_area_m2),
            "max_area_m2": decimal_text(notice.max_area_m2),
            "min_price_eok": decimal_text(notice.min_price_eok),
            "is_regulated": None if notice.is_regulated is None else int(bool(notice.is_regulated)),
            "special_supply_types": json.dumps(sorted(set(notice.special_supply_types)), ensure_ascii=False),
            "is_sample": int(bool(notice.is_sample)),
            "content_hash": notice.content_hash or notice.compute_content_hash(),
        }

    @staticmethod
    def _row_to_notice(row: sqlite3.Row) -> ChungyakNotice:
        d = dict(row)
        try:
            sp = json.loads(d.get("special_supply_types") or "[]")
        except (TypeError, ValueError):
            sp = []
        reg = d.get("is_regulated")
        return ChungyakNotice(
            notice_key=d["notice_key"],
            notice_type=NoticeType(d["notice_type"]),
            house_type=HouseType(d.get("house_type") or "APT"),
            house_manage_no=d.get("house_manage_no") or "",
            pblanc_no=d.get("pblanc_no") or "",
            house_name=d["house_name"],
            region_sido=d.get("region_sido"),
            region_sigungu=d.get("region_sigungu"),
            address=d.get("address") or "",
            supply_households=d.get("supply_households"),
            announce_date=parse_date(d.get("announce_date")),
            receipt_start=parse_date(d.get("receipt_start")),
            receipt_end=parse_date(d.get("receipt_end")),
            winner_date=parse_date(d.get("winner_date")),
            homepage_url=d.get("homepage_url") or "",
            min_area_m2=to_decimal(d.get("min_area_m2")),
            max_area_m2=to_decimal(d.get("max_area_m2")),
            min_price_eok=to_decimal(d.get("min_price_eok")),
            is_regulated=None if reg is None else bool(reg),
            special_supply_types=list(sp) if isinstance(sp, list) else [],
            is_sample=bool(d.get("is_sample")),
            content_hash=d["content_hash"],
            revision=int(d.get("revision") or 1),
        )

    def upsert_notice(self, notice: ChungyakNotice, now: Optional[datetime] = None) -> str:
        """Returns NEW / AMENDED (content_hash changed -> revision+1) / UNCHANGED."""
        notice.ensure_hash()
        row = self._notice_row(notice)
        ts = _utc_text(now)
        with self._tx() as conn:
            existing = conn.execute(
                "SELECT content_hash, revision FROM chungyak_notices WHERE notice_key = ?", (notice.notice_key,)
            ).fetchone()
            if existing is None:
                cols = list(row) + ["revision", "first_seen_at", "updated_at"]
                vals = list(row.values()) + [1, ts, ts]
                conn.execute(
                    f"INSERT INTO chungyak_notices ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})", vals
                )
                notice.revision = 1
                return "NEW"
            if existing["content_hash"] == row["content_hash"]:
                notice.revision = int(existing["revision"])
                return "UNCHANGED"
            revision = int(existing["revision"]) + 1
            sets = [c for c in row if c != "notice_key"]
            conn.execute(
                f"UPDATE chungyak_notices SET {', '.join(f'{c} = ?' for c in sets)}, revision = ?, updated_at = ? "
                "WHERE notice_key = ?",
                [row[c] for c in sets] + [revision, ts, notice.notice_key],
            )
            notice.revision = revision
            return "AMENDED"

    def get_notice(self, notice_key: str) -> Optional[ChungyakNotice]:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM chungyak_notices WHERE notice_key = ?", (notice_key,)).fetchone()
        return self._row_to_notice(row) if row else None

    def list_open_notices(self, today: date, include_samples: bool = False, limit: int = 200) -> list[ChungyakNotice]:
        """Notices whose receipt has not ended yet (or end date unknown), soonest receipt first."""
        sql = "SELECT * FROM chungyak_notices WHERE (receipt_end IS NULL OR receipt_end >= ?)"
        if not include_samples:
            sql += " AND is_sample = 0"
        sql += " ORDER BY COALESCE(receipt_start, '9999-12-31'), notice_key LIMIT ?"
        with self._read() as conn:
            rows = conn.execute(sql, (today.isoformat(), int(limit))).fetchall()
        return [self._row_to_notice(r) for r in rows]

    # ------------------------------------------------------------------ profiles
    @staticmethod
    def _encode_profile_value(col: str, value: Any) -> Any:
        if value is None:
            return None
        if col in _BOOL_COLS:
            return int(bool(value))
        if col in _INT_COLS:
            return int(value)
        if col in _LIST_COLS:
            return json.dumps(list(value), ensure_ascii=False)
        if col in _DEC_COLS:
            return decimal_text(to_decimal(value))
        return str(value)

    @staticmethod
    def _row_to_profile(row: sqlite3.Row) -> UserProfile:
        d = dict(row)
        kwargs: dict[str, Any] = {"chat_id": str(d["chat_id"])}
        for col in PROFILE_COLUMNS:
            raw = d.get(col)
            if raw is None:
                kwargs[col] = None
            elif col in _BOOL_COLS:
                kwargs[col] = bool(raw)
            elif col in _INT_COLS:
                kwargs[col] = int(raw)
            elif col in _LIST_COLS:
                try:
                    parsed = json.loads(raw)
                except (TypeError, ValueError):
                    parsed = None
                kwargs[col] = list(parsed) if isinstance(parsed, list) else None
            elif col in _DEC_COLS:
                kwargs[col] = to_decimal(raw)
            else:
                kwargs[col] = raw
        kwargs["created_at"] = d.get("created_at")
        kwargs["updated_at"] = d.get("updated_at")
        return UserProfile(**kwargs)

    def get_profile(self, chat_id: str) -> Optional[UserProfile]:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM chungyak_user_profiles WHERE chat_id = ?", (str(chat_id),)).fetchone()
        return self._row_to_profile(row) if row else None

    def ensure_profile(self, chat_id: str, now: Optional[datetime] = None) -> UserProfile:
        ts = _utc_text(now)
        with self._tx() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO chungyak_user_profiles (chat_id, created_at, updated_at) VALUES (?, ?, ?)",
                (str(chat_id), ts, ts),
            )
        profile = self.get_profile(chat_id)
        assert profile is not None
        return profile

    def update_profile(self, chat_id: str, now: Optional[datetime] = None, **fields: Any) -> UserProfile:
        unknown = set(fields) - set(PROFILE_COLUMNS)
        if unknown:
            raise ValueError(f"unknown profile fields: {sorted(unknown)}")
        self.ensure_profile(chat_id, now=now)
        if fields:
            ts = _utc_text(now)
            cols = list(fields)
            with self._tx() as conn:
                conn.execute(
                    f"UPDATE chungyak_user_profiles SET {', '.join(f'{c} = ?' for c in cols)}, updated_at = ? "
                    "WHERE chat_id = ?",
                    [self._encode_profile_value(c, fields[c]) for c in cols] + [ts, str(chat_id)],
                )
        profile = self.get_profile(chat_id)
        assert profile is not None
        return profile

    def delete_profile(self, chat_id: str) -> bool:
        with self._tx() as conn:
            cur = conn.execute("DELETE FROM chungyak_user_profiles WHERE chat_id = ?", (str(chat_id),))
            conn.execute("DELETE FROM chungyak_regular_subs WHERE chat_id = ?", (str(chat_id),))
            return cur.rowcount > 0

    def list_profiles(self) -> list[UserProfile]:
        with self._read() as conn:
            rows = conn.execute("SELECT * FROM chungyak_user_profiles ORDER BY chat_id").fetchall()
        return [self._row_to_profile(r) for r in rows]

    # ------------------------------------------------------------------ regular subs
    def set_regular_sub(self, chat_id: str, enabled: bool, now: Optional[datetime] = None) -> None:
        ts = _utc_text(now)
        with self._tx() as conn:
            conn.execute(
                """INSERT INTO chungyak_regular_subs (chat_id, is_enabled, created_at, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(chat_id) DO UPDATE SET is_enabled = excluded.is_enabled, updated_at = excluded.updated_at""",
                (str(chat_id), int(bool(enabled)), ts, ts),
            )

    def get_regular_sub(self, chat_id: str) -> Optional[bool]:
        with self._read() as conn:
            row = conn.execute(
                "SELECT is_enabled FROM chungyak_regular_subs WHERE chat_id = ?", (str(chat_id),)
            ).fetchone()
        return None if row is None else bool(row["is_enabled"])

    def list_regular_sub_chat_ids(self) -> list[str]:
        with self._read() as conn:
            rows = conn.execute(
                "SELECT chat_id FROM chungyak_regular_subs WHERE is_enabled = 1 ORDER BY chat_id"
            ).fetchall()
        return [str(r["chat_id"]) for r in rows]

    # ------------------------------------------------------------------ delivery ledger
    def is_notice_delivered(self, chat_id: str, notice_key: str, content_hash: str) -> bool:
        """True when ANY channel already holds SENT/UNKNOWN (or an in-flight/queued row) for this revision."""
        with self._read() as conn:
            row = conn.execute(
                """SELECT 1 FROM chungyak_deliveries
                    WHERE chat_id = ? AND notice_key = ? AND content_hash = ?
                      AND status IN ('SENT', 'UNKNOWN', 'PENDING', 'QUEUED_MORNING')
                    LIMIT 1""",
                (str(chat_id), notice_key, content_hash),
            ).fetchone()
        return row is not None

    def reserve_delivery(
        self,
        *,
        chat_id: str,
        notice_key: str,
        content_hash: str,
        channel: ChannelType,
        trigger_context: str,
        now: Optional[datetime] = None,
        lease_seconds: int = 120,
        queue_until_kst: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Atomically reserves a delivery slot.

        Returns `{"delivery_key", "lease_token", "status"}` (status PENDING or QUEUED_MORNING) or None when the
        notice revision is already SENT/UNKNOWN/in-flight/queued for this chat on any channel, or terminally failed.
        """
        cid = str(chat_id)
        key = make_delivery_key(channel, cid, notice_key, content_hash)
        moment = now or utc_now()
        ts = _utc_text(moment)
        with self._tx() as conn:
            self._sweep_expired_in_tx(conn, ts)
            blocking = conn.execute(
                """SELECT 1 FROM chungyak_deliveries
                    WHERE chat_id = ? AND notice_key = ? AND content_hash = ? AND delivery_key != ?
                      AND status IN ('SENT', 'UNKNOWN', 'PENDING', 'QUEUED_MORNING')
                    LIMIT 1""",
                (cid, notice_key, content_hash, key),
            ).fetchone()
            if blocking is not None:
                return None
            existing = conn.execute("SELECT * FROM chungyak_deliveries WHERE delivery_key = ?", (key,)).fetchone()
            if queue_until_kst is not None:
                new_status, token, expires, attempt = DeliveryStatus.QUEUED_MORNING.value, None, None, 0
            else:
                new_status = DeliveryStatus.PENDING.value
                token = uuid.uuid4().hex
                expires = _utc_text(moment + timedelta(seconds=lease_seconds))
                attempt = 1
            if existing is None:
                conn.execute(
                    """INSERT INTO chungyak_deliveries
                       (delivery_key, chat_id, notice_key, channel_type, status, attempt_no, lease_token,
                        lease_expires_at, scheduled_at_kst, trigger_context, content_hash, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (key, cid, notice_key, channel.value, new_status, attempt, token, expires, queue_until_kst,
                     trigger_context, content_hash, ts, ts),
                )
                return {"delivery_key": key, "lease_token": token, "status": new_status}
            status = existing["status"]
            err = str(existing["error_detail"] or "")
            retryable = (
                status == DeliveryStatus.FAILED.value
                and not err.startswith(WINDOW_MISSED)
                and int(existing["attempt_no"] or 0) < MAX_FAILED_ATTEMPTS
            )
            if not retryable:
                return None
            attempt = int(existing["attempt_no"] or 0) + (1 if new_status == DeliveryStatus.PENDING.value else 0)
            conn.execute(
                """UPDATE chungyak_deliveries
                      SET status = ?, attempt_no = ?, lease_token = ?, lease_expires_at = ?, scheduled_at_kst = ?,
                          trigger_context = ?, error_detail = NULL, updated_at = ?
                    WHERE delivery_key = ? AND status = 'FAILED'""",
                (new_status, attempt, token, expires, queue_until_kst, trigger_context, ts, key),
            )
            return {"delivery_key": key, "lease_token": token, "status": new_status}

    def claim_queued_morning(
        self,
        now_kst_text: str,
        *,
        now: Optional[datetime] = None,
        lease_seconds: int = 300,
        trigger_context: str = "CRON_TICK",
    ) -> list[dict[str, Any]]:
        """QUEUED_MORNING rows due by `now_kst_text` -> PENDING with a shared lease token (bundled send)."""
        moment = now or utc_now()
        ts = _utc_text(moment)
        token = uuid.uuid4().hex
        expires = _utc_text(moment + timedelta(seconds=lease_seconds))
        with self._tx() as conn:
            rows = conn.execute(
                """SELECT * FROM chungyak_deliveries
                    WHERE status = 'QUEUED_MORNING' AND scheduled_at_kst <= ?
                    ORDER BY chat_id, created_at, delivery_key""",
                (now_kst_text,),
            ).fetchall()
            claimed: list[dict[str, Any]] = []
            for r in rows:
                cur = conn.execute(
                    """UPDATE chungyak_deliveries
                          SET status = 'PENDING', attempt_no = attempt_no + 1, lease_token = ?, lease_expires_at = ?,
                              trigger_context = ?, updated_at = ?
                        WHERE delivery_key = ? AND status = 'QUEUED_MORNING'""",
                    (token, expires, trigger_context, ts, r["delivery_key"]),
                )
                if cur.rowcount == 1:
                    d = dict(r)
                    d.update(status="PENDING", lease_token=token)
                    claimed.append(d)
            return claimed

    def expire_missed_morning(self, cutoff_kst_text: str, now: Optional[datetime] = None) -> int:
        """QUEUED_MORNING rows scheduled at/before `cutoff_kst_text` whose window passed -> FAILED(WINDOW_MISSED).
        No compensatory redelivery ([PROD-LIVE] §2)."""
        ts = _utc_text(now)
        with self._tx() as conn:
            cur = conn.execute(
                """UPDATE chungyak_deliveries
                      SET status = 'FAILED', error_detail = ?, lease_token = NULL, lease_expires_at = NULL,
                          updated_at = ?
                    WHERE status = 'QUEUED_MORNING' AND scheduled_at_kst <= ?""",
                (f"{WINDOW_MISSED}: 08:00~08:15 KST 윈도우 경과 (보상 발송 금지)", ts, cutoff_kst_text),
            )
            return int(cur.rowcount)

    def finalize_delivery(
        self,
        delivery_key: str,
        lease_token: str,
        status: DeliveryStatus | str,
        *,
        provider_msg_id: Optional[str] = None,
        error_detail: Optional[str] = None,
        now: Optional[datetime] = None,
    ) -> bool:
        """Records the send result. Fenced: only the current lease holder of a PENDING row may finalize."""
        st = status.value if isinstance(status, DeliveryStatus) else str(status)
        if st not in (DeliveryStatus.SENT.value, DeliveryStatus.UNKNOWN.value, DeliveryStatus.FAILED.value):
            raise ValueError(f"invalid final status: {st}")
        ts = _utc_text(now)
        with self._tx() as conn:
            cur = conn.execute(
                """UPDATE chungyak_deliveries
                      SET status = ?, provider_msg_id = ?, error_detail = ?, lease_token = NULL,
                          lease_expires_at = NULL, updated_at = ?
                    WHERE delivery_key = ? AND lease_token = ? AND status = 'PENDING'""",
                (st, provider_msg_id, (error_detail or None) and str(error_detail)[:500], ts, delivery_key, lease_token),
            )
            return cur.rowcount == 1

    @staticmethod
    def _sweep_expired_in_tx(conn: sqlite3.Connection, ts: str) -> int:
        cur = conn.execute(
            """UPDATE chungyak_deliveries
                  SET status = 'UNKNOWN', error_detail = ?, lease_token = NULL, updated_at = ?
                WHERE status = 'PENDING' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?""",
            (f"{LEASE_EXPIRED}: 결과 미기록 리스 만료 (재시도 금지)", ts, ts),
        )
        return int(cur.rowcount)

    def sweep_expired_leases(self, now: Optional[datetime] = None) -> int:
        with self._tx() as conn:
            return self._sweep_expired_in_tx(conn, _utc_text(now))

    def get_delivery(self, delivery_key: str) -> Optional[dict[str, Any]]:
        with self._read() as conn:
            row = conn.execute("SELECT * FROM chungyak_deliveries WHERE delivery_key = ?", (delivery_key,)).fetchone()
        return dict(row) if row else None

    def list_deliveries(self, chat_id: Optional[str] = None, status: Optional[str] = None) -> list[dict[str, Any]]:
        where, params = ["1 = 1"], []
        if chat_id is not None:
            where.append("chat_id = ?")
            params.append(str(chat_id))
        if status is not None:
            where.append("status = ?")
            params.append(str(status))
        with self._read() as conn:
            rows = conn.execute(
                f"SELECT * FROM chungyak_deliveries WHERE {' AND '.join(where)} ORDER BY created_at, delivery_key",
                params,
            ).fetchall()
        return [dict(r) for r in rows]
