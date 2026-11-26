"""Production SQLite / Turso database management for Market Brief admin and runner."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
import sqlite3
import uuid
from typing import Any

from market_brief.storage.models import (
    AttemptStatus,
    BatchStatus,
    BotRecord,
    BotType,
    DeliveryBatch,
    DeliveryJob,
    DeliveryJobRecipient,
    JobFinalStatus,
    PlatformUser,
    Recipient,
    Subscription,
    SubscriptionStatus,
    SystemConfig,
    TIER_DEFAULT_SPECS,
    TierPolicy,
    UserTier,
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


# Active intraday volatility target-watch caps per tier (separate from `tier_policies.max_bots`).
MARKET_WATCH_TIER_LIMITS: dict[str, int] = {"WHITE": 3, "GREEN": 5, "RED": 10, "BLACK": 20}


class MarketBriefDatabase:
    """Thread-safe SQLite database manager supporting ACID transactions and atomic lease locks."""

    def __init__(self, db_path: str = "alerts.sqlite3", env_path: Path | None = None):
        self.db_path = str(db_path)
        self.env_path = env_path
        self._memory_connection: sqlite3.Connection | None = None
        if self.db_path.strip() == ":memory:" or self.db_path.strip().startswith("file::memory:"):
            self._memory_connection = sqlite3.connect(
                self.db_path,
                uri=self.db_path.strip().startswith("file::memory:"),
                timeout=15.0,
            )
            self._memory_connection.row_factory = sqlite3.Row
        self._init_schema()
        self._seed_initial_data()

    @contextmanager
    def _connect(self):
        shared_memory = self._memory_connection
        conn = shared_memory or sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            if shared_memory is None:
                conn.close()

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
                    subscribed_topics TEXT DEFAULT 'FINANCE',
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
            db.execute(
                """CREATE INDEX IF NOT EXISTS idx_delivery_recipient_history
                   ON delivery_job_recipients (chat_id, updated_at DESC)"""
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

            # 6-1. Admin cron schedule sync journal (PENDING -> APPLIED / SYNC_FAILED / ROLLED_BACK / DIVERGED)
            db.execute(
                """CREATE TABLE IF NOT EXISTS bot_schedules_pending (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    payload_json TEXT NOT NULL,
                    snapshot_crontab TEXT,
                    snapshot_hash TEXT,
                    target_hash TEXT,
                    error_detail TEXT,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
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

            # 8. Multi-Bot Subscription Platform Schema (v2.0)
            db.execute(
                """CREATE TABLE IF NOT EXISTS tier_policies (
                    tier_code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    max_bots INTEGER NOT NULL,
                    badge_color TEXT NOT NULL DEFAULT '#64748b',
                    description TEXT DEFAULT '',
                    sort_order INTEGER NOT NULL DEFAULT 1
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS bots (
                    bot_id TEXT PRIMARY KEY,
                    bot_type TEXT NOT NULL DEFAULT 'INFO',
                    name TEXT NOT NULL,
                    telegram_username TEXT DEFAULT '',
                    token TEXT DEFAULT '',
                    category TEXT DEFAULT '일반',
                    icon_emoji TEXT DEFAULT '🤖',
                    description TEXT DEFAULT '',
                    topic_code TEXT DEFAULT 'GENERAL',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    sort_order INTEGER NOT NULL DEFAULT 10,
                    last_offset INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS users (
                    telegram_user_id TEXT PRIMARY KEY,
                    username TEXT DEFAULT '',
                    full_name TEXT NOT NULL,
                    tier TEXT NOT NULL DEFAULT 'WHITE',
                    tier_expires_at TEXT,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    memo TEXT DEFAULT '',
                    is_name_custom INTEGER NOT NULL DEFAULT 0,
                    name_source TEXT DEFAULT 'TELEGRAM',
                    name_synced_at TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id TEXT NOT NULL,
                    bot_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    subscribed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    cancelled_at TEXT,
                    UNIQUE(telegram_user_id, bot_id),
                    FOREIGN KEY(telegram_user_id) REFERENCES users(telegram_user_id),
                    FOREIGN KEY(bot_id) REFERENCES bots(bot_id)
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS subscription_audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id TEXT NOT NULL,
                    bot_id TEXT,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL DEFAULT 'USER',
                    detail TEXT,
                    logged_at TEXT DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS upgrade_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id TEXT NOT NULL,
                    current_tier TEXT NOT NULL,
                    requested_tier TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    requested_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    reviewed_at TEXT,
                    reviewed_by TEXT,
                    memo TEXT DEFAULT '',
                    FOREIGN KEY(telegram_user_id) REFERENCES users(telegram_user_id)
                )"""
            )
            # Idempotent user column migrations
            existing_user_cols = {row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()}
            if "is_name_custom" not in existing_user_cols:
                db.execute("ALTER TABLE users ADD COLUMN is_name_custom INTEGER NOT NULL DEFAULT 0")
            if "name_source" not in existing_user_cols:
                db.execute("ALTER TABLE users ADD COLUMN name_source TEXT DEFAULT 'TELEGRAM'")
            if "name_synced_at" not in existing_user_cols:
                db.execute("ALTER TABLE users ADD COLUMN name_synced_at TEXT")

            # 9. User-configured intraday volatility target watches (brief_bot, additive migration).
            #    All numeric thresholds/observations are normalized Decimal strings (TEXT), never REAL.
            db.execute(
                """CREATE TABLE IF NOT EXISTS market_volatility_watches (
                    watch_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id TEXT NOT NULL,
                    chat_id TEXT NOT NULL,
                    bot_id TEXT NOT NULL DEFAULT 'brief_bot',
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    quote_unit TEXT NOT NULL,
                    condition_type TEXT NOT NULL,
                    direction TEXT NOT NULL DEFAULT 'BOTH',
                    threshold_value TEXT NOT NULL,
                    threshold_unit TEXT NOT NULL,
                    condition_version INTEGER NOT NULL DEFAULT 1,
                    is_active INTEGER NOT NULL DEFAULT 1,
                    is_armed INTEGER NOT NULL DEFAULT 0,
                    armed_side TEXT NOT NULL DEFAULT 'BOTH',
                    confirm_streak INTEGER NOT NULL DEFAULT 0,
                    confirm_side TEXT NOT NULL DEFAULT '',
                    last_observed_value TEXT,
                    last_observed_change TEXT,
                    last_observed_as_of TEXT,
                    last_observed_trade_date TEXT,
                    last_triggered_at TEXT,
                    cooldown_until TEXT,
                    deactivated_reason TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            existing_watch_cols = {
                row["name"] for row in db.execute("PRAGMA table_info(market_volatility_watches)").fetchall()
            }
            for col_name, col_ddl in (
                ("armed_side", "TEXT NOT NULL DEFAULT 'BOTH'"),
                ("confirm_streak", "INTEGER NOT NULL DEFAULT 0"),
                ("confirm_side", "TEXT NOT NULL DEFAULT ''"),
                ("deactivated_reason", "TEXT"),
            ):
                if col_name not in existing_watch_cols:
                    db.execute(f"ALTER TABLE market_volatility_watches ADD COLUMN {col_name} {col_ddl}")
            db.execute(
                """CREATE UNIQUE INDEX IF NOT EXISTS ux_market_watch_active_dup
                   ON market_volatility_watches
                   (telegram_user_id, symbol, condition_type, direction, threshold_unit, threshold_value)
                   WHERE is_active = 1"""
            )
            db.execute(
                """CREATE INDEX IF NOT EXISTS idx_market_watch_active
                   ON market_volatility_watches (is_active, symbol)"""
            )
            db.execute(
                """CREATE TABLE IF NOT EXISTS market_watch_deliveries (
                    delivery_key TEXT PRIMARY KEY,
                    watch_id INTEGER NOT NULL,
                    chat_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    market TEXT NOT NULL,
                    session_id TEXT NOT NULL,
                    trade_date TEXT NOT NULL,
                    trigger_side TEXT NOT NULL,
                    observed_value TEXT,
                    observed_change TEXT,
                    quote_as_of TEXT,
                    status TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_token TEXT,
                    lease_expires_at TEXT,
                    attempt_no INTEGER NOT NULL DEFAULT 1,
                    provider_msg_id TEXT,
                    error_detail TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                )"""
            )
            db.execute(
                """CREATE INDEX IF NOT EXISTS idx_market_watch_deliveries_watch
                   ON market_watch_deliveries (watch_id, status)"""
            )

    @contextmanager
    def _immediate_tx(self):
        """`BEGIN IMMEDIATE` write transaction (cross-process safe write lock for check-then-write sequences)."""
        conn = sqlite3.connect(self.db_path, timeout=15.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

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
                (json.dumps({"morning": "07:50", "closing": "15:40"}),),
            )
            # Migration: legacy 06:30 morning schedule -> 07:50 (additive, idempotent)
            db.execute(
                "UPDATE system_settings SET setting_value = ? WHERE setting_key = 'schedules' AND setting_value LIKE '%06:30%'",
                (json.dumps({"morning": "07:50", "closing": "15:40"}),),
            )

            # Seed recipients and tokens from .env
            env_brief_token = ""
            env_menu_token = ""
            env_travel_token = ""
            env_edu_token = ""
            env_silver_token = ""
            env_kids_token = ""
            env_yolo_token = ""
            env_p = Path(self.env_path) if self.env_path else None
            if env_p and env_p.exists():
                try:
                    env_lines = env_p.read_text(encoding="utf-8-sig").splitlines()
                    for line in env_lines:
                        line = line.strip()
                        if line.startswith("TELEGRAM_BOT_TOKEN="):
                            env_brief_token = line.split("=", 1)[1].strip().strip('"').strip("'")
                        elif (
                            line.startswith("PORTAL_TELEGRAM_BOT_TOKEN=")
                            or line.startswith("PORTAL_BOT_TOKEN=")
                            or line.startswith("MENU_BOT_TOKEN=")
                            or line.startswith("MENU_TELEGRAM_BOT_TOKEN=")
                        ):
                            env_menu_token = line.split("=", 1)[1].strip().strip('"').strip("'")
                        elif line.startswith("TRAVEL_TELEGRAM_BOT_TOKEN=") or line.startswith("TRAVEL_BOT_TOKEN="):
                            env_travel_token = line.split("=", 1)[1].strip().strip('"').strip("'")
                        elif line.startswith("EDU_TELEGRAM_BOT_TOKEN=") or line.startswith("EDU_BOT_TOKEN=") or line.startswith("ADMISSION_TELEGRAM_BOT_TOKEN="):
                            env_edu_token = line.split("=", 1)[1].strip().strip('"').strip("'")
                        elif line.startswith("SILVER_TELEGRAM_BOT_TOKEN=") or line.startswith("SILVER_BOT_TOKEN="):
                            env_silver_token = line.split("=", 1)[1].strip().strip('"').strip("'")
                        elif line.startswith("KIDS_TELEGRAM_BOT_TOKEN=") or line.startswith("KIDS_BOT_TOKEN="):
                            env_kids_token = line.split("=", 1)[1].strip().strip('"').strip("'")
                        elif line.startswith("YOLO_TELEGRAM_BOT_TOKEN=") or line.startswith("YOLO_BOT_TOKEN="):
                            env_yolo_token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    row_count = db.execute("SELECT COUNT(*) FROM recipients").fetchone()[0]
                    if row_count == 0:
                        for line in env_lines:
                            line = line.strip()
                            if line.startswith("TELEGRAM_CHAT_ID="):
                                raw = line.split("=", 1)[1].strip().strip('"').strip("'")
                                for cid in raw.split(","):
                                    cid = cid.strip()
                                    if cid:
                                        db.execute(
                                            """INSERT OR IGNORE INTO recipients
                                               (chat_id, name, channel, group_tag, subscribed_topics, is_active)
                                               VALUES (?, ?, 'TELEGRAM', 'DEFAULT', 'FINANCE', 1)""",
                                            (cid, f"수신자_{cid[-4:]}"),
                                        )
                except Exception:
                    pass

            # Seed 4 Tier Policies (WHITE=1, GREEN=3, RED=5, BLACK=Unlimited)
            for spec in TIER_DEFAULT_SPECS.values():
                db.execute(
                    """INSERT OR IGNORE INTO tier_policies
                       (tier_code, name, max_bots, badge_color, description, sort_order)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (
                        spec["tier_code"],
                        spec["name"],
                        spec["max_bots"],
                        spec["badge_color"],
                        spec["description"],
                        spec["sort_order"],
                    ),
                )

            # Seed Default Multi-Bot Catalog (1 Menu Portal + 7 Real Info Bots: 시황, 여행, 입시, 실버케어, 자녀, 욜로, 청약)
            default_bots = [
                (
                    "menu_bot",
                    BotType.MENU.value,
                    "통합 안내 메뉴봇",
                    "KwMenu_bot",
                    env_menu_token,
                    "가입·포털",
                    "🧭",
                    "전체 정보봇 타이틀 안내, 내 등급·슬롯 확인 및 구독할 정보봇을 선택하는 공식 메뉴봇입니다.",
                    "PORTAL",
                    1,
                    0,
                ),
                (
                    "brief_bot",
                    BotType.INFO.value,
                    "시황",
                    "KwDailyMarketInfo_bot",
                    env_brief_token,
                    "금융·시황",
                    "📈",
                    "장전 07:50 모닝 시황, 15:40 장마감 브리핑 및 ±3% 급변동 긴급 알림을 발송합니다.",
                    "FINANCE",
                    1,
                    1,
                ),
                (
                    "travel_bot",
                    BotType.INFO.value,
                    "여행",
                    "KwTravelLowPrice_bot",
                    env_travel_token,
                    "여행·라이프",
                    "✈️",
                    "전 세계 특가 항공권·숙소 핫딜 알림과 AI 기반 맞춤 여행지 가이드를 제공합니다.",
                    "TRAVEL",
                    1,
                    2,
                ),
                (
                    "admission_bot",
                    BotType.INFO.value,
                    "입시",
                    "KwEduInfo_bot",
                    env_edu_token,
                    "입시·교육",
                    "🎓",
                    "학년별 주간 대입·고입 큐레이션 및 학부모 설명회 티켓팅 오픈 알림을 제공합니다.",
                    "EDUCATION",
                    1,
                    3,
                ),
                (
                    "silver_bot",
                    BotType.INFO.value,
                    "실버케어",
                    "KwSilver_bot",
                    env_silver_token,
                    "건강·실버",
                    "👵",
                    "부모님 맞춤 동사무소·보건소 복지 혜택 및 주간 안심 효도 브리핑을 제공합니다.",
                    "SILVER",
                    1,
                    4,
                ),
                (
                    "kids_bot",
                    BotType.INFO.value,
                    "자녀",
                    "KwKids_bot",
                    env_kids_token,
                    "육아·자녀",
                    "👶",
                    "주말 연령별 맞춤 나들이 체험/예약 D-Day 및 해열제·달빛병원 응급 가이드를 제공합니다.",
                    "KIDS",
                    1,
                    5,
                ),
                (
                    "yolo_bot",
                    BotType.INFO.value,
                    "욜로",
                    "KwYolo_bot",
                    env_yolo_token,
                    "여가·라이프",
                    "🏄",
                    "나홀로 성향별 주말 핫플레이스, 액티비티 및 힐링 라이프스타일 큐레이션을 제공합니다.",
                    "YOLO",
                    1,
                    6,
                ),
                (
                    "chungyak_bot",
                    BotType.INFO.value,
                    "청약",
                    "KwAptApply_bot",
                    "",
                    "부동산·청약",
                    "🏠",
                    "내 조건 맞춤 청약 공고 3단계 자격 판정 및 08:00 정기 다이제스트를 제공합니다.",
                    "CHUNGYAK",
                    1,
                    7,
                ),
            ]

            bot_count = db.execute("SELECT COUNT(*) FROM bots").fetchone()[0]
            if bot_count == 0:
                for b in default_bots:
                    db.execute(
                        """INSERT OR IGNORE INTO bots
                           (bot_id, bot_type, name, telegram_username, token, category, icon_emoji, description, topic_code, is_active, sort_order)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        b,
                    )
            else:
                # Migration: Clean up unimplemented bots if no subscriptions
                for legacy_bot_id in ("trend_bot", "music_bot"):
                    sub_count = db.execute(
                        "SELECT COUNT(*) FROM subscriptions WHERE bot_id = ?", (legacy_bot_id,)
                    ).fetchone()[0]
                    if sub_count == 0:
                        db.execute("DELETE FROM bots WHERE bot_id = ?", (legacy_bot_id,))

                # Migration: Idempotently ensure all default bots are registered without overwriting existing rows
                for b in default_bots:
                    db.execute(
                        """INSERT OR IGNORE INTO bots
                           (bot_id, bot_type, name, telegram_username, token, category, icon_emoji, description, topic_code, is_active, sort_order)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        b,
                    )

                # Backfill verified usernames and missing tokens from .env
                bot_updates = [
                    ("menu_bot", "KwMenu_bot", env_menu_token),
                    ("brief_bot", "KwDailyMarketInfo_bot", env_brief_token),
                    ("travel_bot", "KwTravelLowPrice_bot", env_travel_token),
                    ("admission_bot", "KwEduInfo_bot", env_edu_token),
                    ("silver_bot", "KwSilver_bot", env_silver_token),
                    ("kids_bot", "KwKids_bot", env_kids_token),
                    ("yolo_bot", "KwYolo_bot", env_yolo_token),
                ]
                # One-shot promotion of the legacy WIP chungyak seed row (is_active=0, blank username).
                # Must run BEFORE the username backfill below; later ADMIN deactivation is kept.
                db.execute(
                    "UPDATE bots SET is_active = 1, telegram_username = 'KwAptApply_bot' "
                    "WHERE bot_id = 'chungyak_bot' AND (telegram_username IS NULL OR telegram_username = '')"
                )
                for bid, un, tok in bot_updates:
                    db.execute(
                        "UPDATE bots SET telegram_username = ? WHERE bot_id = ? AND (telegram_username IS NULL OR telegram_username = '')",
                        (un, bid),
                    )
                    if tok:
                        db.execute(
                            "UPDATE bots SET token = ? WHERE bot_id = ? AND (token IS NULL OR token = '')",
                            (tok, bid),
                        )

                # Migration: legacy 06:30 morning label -> 07:50 (idempotent)
                db.execute(
                    "UPDATE bots SET description = replace(description, '06:30', '07:50') WHERE bot_id = 'brief_bot'"
                )

            # Migrate existing legacy `recipients` seamlessly into `users` and `brief_bot` subscriptions
            legacy_rows = db.execute("SELECT * FROM recipients").fetchall()
            for r in legacy_rows:
                cid = str(r["chat_id"]).strip()
                if not cid:
                    continue
                initial_tier = UserTier.BLACK.value if (r["group_tag"] or "").upper() == "VIP" else UserTier.WHITE.value
                db.execute(
                    """INSERT OR IGNORE INTO users (telegram_user_id, full_name, tier, status)
                       VALUES (?, ?, ?, 'ACTIVE')""",
                    (cid, r["name"] or f"유저_{cid[-4:]}", initial_tier),
                )
                topics = [t.strip().upper() for t in (r["subscribed_topics"] or "").split(",") if t.strip()]
                if r["is_active"] and ("FINANCE" in topics or "ALL" in topics):
                    db.execute(
                        """INSERT OR IGNORE INTO subscriptions (telegram_user_id, bot_id, status)
                           VALUES (?, 'brief_bot', 'ACTIVE')""",
                        (cid,),
                    )

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

    def get_all_recipients(self) -> list[Recipient]:
        with self._connect() as db:
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
        subscribed_topics: str = "FINANCE",
    ) -> bool:
        cid = str(chat_id).strip()
        clean_name = name.strip()
        with self._connect() as db:
            cursor = db.execute(
                """INSERT OR REPLACE INTO recipients
                   (chat_id, name, channel, group_tag, subscribed_topics, is_active, inactivation_reason)
                   VALUES (?, ?, ?, ?, ?, 1, NULL)""",
                (cid, clean_name, channel, group_tag, subscribed_topics),
            )
            # LegacyRecipientAdapter: sync into Platform Users & brief_bot subscription
            db.execute(
                """INSERT INTO users (telegram_user_id, full_name, tier, status)
                   VALUES (?, ?, 'WHITE', 'ACTIVE')
                   ON CONFLICT(telegram_user_id) DO UPDATE SET full_name = excluded.full_name, updated_at = CURRENT_TIMESTAMP""",
                (cid, clean_name),
            )
            topics = [t.strip().upper() for t in (subscribed_topics or "").split(",") if t.strip()]
            if "FINANCE" in topics or "ALL" in topics:
                db.execute(
                    """INSERT INTO subscriptions (telegram_user_id, bot_id, status, subscribed_at, cancelled_at)
                       VALUES (?, 'brief_bot', 'ACTIVE', CURRENT_TIMESTAMP, NULL)
                       ON CONFLICT(telegram_user_id, bot_id) DO UPDATE SET
                         status = 'ACTIVE', subscribed_at = CURRENT_TIMESTAMP, cancelled_at = NULL""",
                    (cid,),
                )
            return cursor.rowcount > 0

    def update_recipient_name(self, chat_id: str, new_name: str) -> bool:
        cid = str(chat_id).strip()
        clean_name = new_name.strip()
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE recipients SET name = ? WHERE chat_id = ?",
                (clean_name, cid),
            )
            db.execute(
                "UPDATE users SET full_name = ?, updated_at = CURRENT_TIMESTAMP WHERE telegram_user_id = ?",
                (clean_name, cid),
            )
            return cursor.rowcount > 0

    def deactivate_recipient(self, chat_id: str, reason: str = "MANUAL_STOP") -> bool:
        cid = str(chat_id).strip()
        with self._connect() as db:
            cursor = db.execute(
                "UPDATE recipients SET is_active = 0, inactivation_reason = ? WHERE chat_id = ?",
                (reason, cid),
            )
            db.execute(
                """UPDATE subscriptions
                   SET status = 'CANCELLED', cancelled_at = CURRENT_TIMESTAMP
                   WHERE telegram_user_id = ? AND bot_id = 'brief_bot' AND status = 'ACTIVE'""",
                (cid,),
            )
            return cursor.rowcount > 0

    def delete_recipient(self, chat_id: str) -> bool:
        cid = str(chat_id).strip()
        with self._connect() as db:
            cursor = db.execute("DELETE FROM recipients WHERE chat_id = ?", (cid,))
            db.execute(
                """UPDATE subscriptions
                   SET status = 'CANCELLED', cancelled_at = CURRENT_TIMESTAMP
                   WHERE telegram_user_id = ? AND bot_id = 'brief_bot'""",
                (cid,),
            )
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

    # =========================================================================
    # Admin Cron Schedule Sync Journal
    # =========================================================================

    def record_schedule_pending(
        self,
        task_id: str,
        payload_json: str,
        snapshot_crontab: str,
        snapshot_hash: str,
        target_hash: str,
    ) -> int:
        with self._connect() as db:
            cursor = db.execute(
                """INSERT INTO bot_schedules_pending
                   (task_id, status, payload_json, snapshot_crontab, snapshot_hash, target_hash, updated_at)
                   VALUES (?, 'PENDING', ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
                (task_id, payload_json, snapshot_crontab, snapshot_hash, target_hash),
            )
            return int(cursor.lastrowid)

    def finalize_schedule_journal(
        self,
        journal_id: int,
        status: str,
        error_detail: str | None = None,
        canonical_value: str | None = None,
        updated_by: str = "ADMIN_WEB",
        reason: str | None = None,
    ) -> None:
        """Updates the journal; when canonical_value is given, the canonical `bot_schedules`
        setting, its config history row and the journal status commit in one transaction."""
        with self._connect() as db:
            if canonical_value is not None:
                db.execute(
                    """INSERT OR REPLACE INTO system_settings (setting_key, setting_value, description, updated_at)
                       VALUES ('bot_schedules', ?, '관리자 콘솔 자동 실행 주기 (ADMIN-MANAGED crontab SSOT)', CURRENT_TIMESTAMP)""",
                    (canonical_value,),
                )
                v = db.execute(
                    "SELECT COALESCE(MAX(config_version), 0) + 1 FROM system_config_history WHERE setting_key = 'bot_schedules'"
                ).fetchone()[0]
                db.execute(
                    """INSERT INTO system_config_history (config_version, setting_key, setting_value, updated_by, reason)
                       VALUES (?, 'bot_schedules', ?, ?, ?)""",
                    (v, canonical_value, updated_by, reason),
                )
            db.execute(
                """UPDATE bot_schedules_pending
                   SET status = ?, error_detail = ?, updated_at = CURRENT_TIMESTAMP
                   WHERE id = ?""",
                (status, error_detail, journal_id),
            )

    def get_latest_schedule_journal(self) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM bot_schedules_pending ORDER BY id DESC LIMIT 1").fetchone()
            return dict(row) if row else None

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
                """SELECT jr.*,
                          COALESCE(
                              NULLIF(TRIM(u.full_name), ''),
                              NULLIF(TRIM(r.name), ''),
                              '수신자'
                          ) AS name,
                          r.channel
                   FROM delivery_job_recipients jr
                   LEFT JOIN users u ON jr.chat_id = u.telegram_user_id
                   LEFT JOIN recipients r ON jr.chat_id = r.chat_id
                   WHERE jr.job_id = ?
                   ORDER BY jr.updated_at ASC""",
                (job_id,),
            ).fetchall()
            return [dict(r) for r in rows]

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

    # =========================================================================
    # Market Volatility Target Watches (brief_bot) & Delivery Ledger
    # =========================================================================

    @staticmethod
    def _utc_now_str() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    def get_market_watch_tier_limit(self, tier_code: str | None) -> int:
        return MARKET_WATCH_TIER_LIMITS.get((tier_code or "WHITE").upper(), MARKET_WATCH_TIER_LIMITS["WHITE"])

    def create_market_watch(
        self,
        telegram_user_id: str,
        chat_id: str,
        *,
        symbol: str,
        market: str,
        quote_unit: str,
        condition_type: str,
        direction: str,
        threshold_value: str,
        threshold_unit: str,
        bot_id: str = "brief_bot",
    ) -> dict[str, Any]:
        """Creates an active watch inside one `BEGIN IMMEDIATE` (tier cap + active duplicate guard).

        Returns {"success", "code", "watch_id", "active_count", "limit"}. A new watch starts DISARMED with no
        baseline: the first valid observation only records the baseline (never triggers retroactively).
        """
        uid = str(telegram_user_id).strip()
        with self._immediate_tx() as db:
            user = db.execute("SELECT tier, status FROM users WHERE telegram_user_id = ?", (uid,)).fetchone()
            tier = (user["tier"] if user else "WHITE") or "WHITE"
            limit = self.get_market_watch_tier_limit(tier)
            if user and user["status"] == "BLOCKED":
                return {"success": False, "code": "USER_BLOCKED", "watch_id": None, "active_count": 0, "limit": limit}
            active_count = db.execute(
                "SELECT COUNT(*) FROM market_volatility_watches WHERE telegram_user_id = ? AND is_active = 1",
                (uid,),
            ).fetchone()[0]
            dup = db.execute(
                """SELECT watch_id FROM market_volatility_watches
                   WHERE telegram_user_id = ? AND symbol = ? AND condition_type = ? AND direction = ?
                     AND threshold_unit = ? AND threshold_value = ? AND is_active = 1""",
                (uid, symbol, condition_type, direction, threshold_unit, threshold_value),
            ).fetchone()
            if dup:
                return {
                    "success": False,
                    "code": "DUPLICATE",
                    "watch_id": int(dup["watch_id"]),
                    "active_count": int(active_count),
                    "limit": limit,
                }
            if active_count >= limit:
                return {"success": False, "code": "LIMIT_EXCEEDED", "watch_id": None, "active_count": int(active_count), "limit": limit}
            now_str = self._utc_now_str()
            try:
                cur = db.execute(
                    """INSERT INTO market_volatility_watches
                       (telegram_user_id, chat_id, bot_id, symbol, market, quote_unit, condition_type, direction,
                        threshold_value, threshold_unit, condition_version, is_active, is_armed, armed_side,
                        confirm_streak, confirm_side, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, 0, ?, 0, '', ?, ?)""",
                    (
                        uid,
                        str(chat_id).strip(),
                        bot_id,
                        symbol,
                        market,
                        quote_unit,
                        condition_type,
                        direction,
                        threshold_value,
                        threshold_unit,
                        direction,
                        now_str,
                        now_str,
                    ),
                )
            except sqlite3.IntegrityError:
                return {"success": False, "code": "DUPLICATE", "watch_id": None, "active_count": int(active_count), "limit": limit}
            return {
                "success": True,
                "code": "CREATED",
                "watch_id": int(cur.lastrowid),
                "active_count": int(active_count) + 1,
                "limit": limit,
            }

    def get_market_watch(self, watch_id: int) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM market_volatility_watches WHERE watch_id = ?", (int(watch_id),)).fetchone()
            return dict(row) if row else None

    def list_market_watches(self, telegram_user_id: str, active_only: bool = True) -> list[dict[str, Any]]:
        uid = str(telegram_user_id).strip()
        sql = "SELECT * FROM market_volatility_watches WHERE telegram_user_id = ?"
        if active_only:
            sql += " AND is_active = 1"
        sql += " ORDER BY created_at ASC, watch_id ASC"
        with self._connect() as db:
            return [dict(r) for r in db.execute(sql, (uid,)).fetchall()]

    def deactivate_market_watch(self, telegram_user_id: str, watch_id: int, reason: str = "USER_DELETE") -> bool:
        """Owner-guarded single watch deactivation (`telegram_user_id` must own `watch_id`)."""
        with self._immediate_tx() as db:
            cur = db.execute(
                """UPDATE market_volatility_watches
                   SET is_active = 0, is_armed = 0, confirm_streak = 0, confirm_side = '',
                       deactivated_reason = ?, updated_at = ?
                   WHERE watch_id = ? AND telegram_user_id = ? AND is_active = 1""",
                (reason, self._utc_now_str(), int(watch_id), str(telegram_user_id).strip()),
            )
            return cur.rowcount == 1

    def deactivate_all_market_watches(self, telegram_user_id: str, reason: str = "BRIEF_UNSUBSCRIBED") -> int:
        """Deactivates every active watch of the user (`/stop`, menu unsubscribe, bot swap-out).
        `/start` never revives them: re-subscription leaves rows inactive."""
        with self._immediate_tx() as db:
            cur = db.execute(
                """UPDATE market_volatility_watches
                   SET is_active = 0, is_armed = 0, confirm_streak = 0, confirm_side = '',
                       deactivated_reason = ?, updated_at = ?
                   WHERE telegram_user_id = ? AND is_active = 1""",
                (reason, self._utc_now_str(), str(telegram_user_id).strip()),
            )
            return int(cur.rowcount)

    def count_active_market_watches(self) -> int:
        with self._connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM market_volatility_watches WHERE is_active = 1").fetchone()[0])

    def list_evaluable_market_watches(self) -> list[dict[str, Any]]:
        """Active watches of ACTIVE `brief_bot` subscribers whose platform user is ACTIVE or GRACE_PERIOD
        (fail-closed: BLOCKED / BANNED / missing user rows are excluded), capped per current tier oldest-first."""
        with self._connect() as db:
            rows = db.execute(
                """SELECT w.*, COALESCE(u.tier, 'WHITE') AS user_tier
                   FROM market_volatility_watches w
                   JOIN subscriptions s
                     ON s.telegram_user_id = w.telegram_user_id AND s.bot_id = 'brief_bot' AND s.status = 'ACTIVE'
                   JOIN users u ON u.telegram_user_id = w.telegram_user_id
                   WHERE w.is_active = 1 AND w.bot_id = 'brief_bot'
                     AND u.status IN ('ACTIVE', 'GRACE_PERIOD')
                   ORDER BY w.telegram_user_id ASC, w.created_at ASC, w.watch_id ASC"""
            ).fetchall()
        result: list[dict[str, Any]] = []
        per_user: dict[str, int] = {}
        for r in rows:
            d = dict(r)
            uid = d["telegram_user_id"]
            used = per_user.get(uid, 0)
            if used >= self.get_market_watch_tier_limit(d.get("user_tier")):
                continue
            per_user[uid] = used + 1
            result.append(d)
        return result

    def update_market_watch_state(
        self,
        watch_id: int,
        condition_version: int,
        *,
        is_armed: bool,
        armed_side: str,
        confirm_streak: int,
        confirm_side: str,
        last_observed_value: str | None,
        last_observed_change: str | None,
        last_observed_as_of: str | None,
        last_observed_trade_date: str | None,
        last_triggered_at: str | None = None,
    ) -> bool:
        """Persists evaluator state (restart-safe hysteresis/confirmation). Guarded by `condition_version`."""
        with self._connect() as db:
            cur = db.execute(
                """UPDATE market_volatility_watches
                   SET is_armed = ?, armed_side = ?, confirm_streak = ?, confirm_side = ?,
                       last_observed_value = ?, last_observed_change = ?, last_observed_as_of = ?,
                       last_observed_trade_date = ?,
                       last_triggered_at = COALESCE(?, last_triggered_at),
                       updated_at = ?
                   WHERE watch_id = ? AND condition_version = ? AND is_active = 1""",
                (
                    1 if is_armed else 0,
                    armed_side,
                    int(confirm_streak),
                    confirm_side,
                    last_observed_value,
                    last_observed_change,
                    last_observed_as_of,
                    last_observed_trade_date,
                    last_triggered_at,
                    self._utc_now_str(),
                    int(watch_id),
                    int(condition_version),
                ),
            )
            return cur.rowcount == 1

    def claim_market_watch_delivery(
        self,
        *,
        delivery_key: str,
        watch_id: int,
        chat_id: str,
        symbol: str,
        market: str,
        session_id: str,
        trade_date: str,
        trigger_side: str,
        observed_value: str | None,
        observed_change: str | None,
        quote_as_of: str | None,
        lease_owner: str,
        lease_seconds: int = 120,
        max_attempts: int = 3,
    ) -> tuple[bool, str, str | None]:
        """Atomic lease claim inside one `BEGIN IMMEDIATE`.

        - Expired `IN_FLIGHT` leases are quarantined to `UNKNOWN` first (never auto-resent).
        - `SENT` / `UNKNOWN` / `IN_FLIGHT` for the key -> rejected.
        - Only an explicit `FAILED` row may be re-claimed (bounded by `max_attempts`).
        Returns (claimed, reason, lease_token).
        """
        now = datetime.now(timezone.utc)
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        expires = (now + timedelta(seconds=lease_seconds)).strftime("%Y-%m-%d %H:%M:%S")
        token = uuid.uuid4().hex
        with self._immediate_tx() as db:
            db.execute(
                """UPDATE market_watch_deliveries
                   SET status = 'UNKNOWN', error_detail = COALESCE(error_detail, 'LEASE_EXPIRED_QUARANTINE'),
                       updated_at = ?
                   WHERE status = 'IN_FLIGHT' AND lease_expires_at IS NOT NULL AND lease_expires_at < ?""",
                (now_str, now_str),
            )
            row = db.execute(
                "SELECT status, attempt_no FROM market_watch_deliveries WHERE delivery_key = ?",
                (delivery_key,),
            ).fetchone()
            if row:
                status = row["status"]
                if status != "FAILED":
                    return False, f"ALREADY_{status}", None
                if int(row["attempt_no"]) >= max_attempts:
                    return False, "MAX_ATTEMPTS_EXCEEDED", None
                db.execute(
                    """UPDATE market_watch_deliveries
                       SET status = 'IN_FLIGHT', lease_owner = ?, lease_token = ?, lease_expires_at = ?,
                           attempt_no = attempt_no + 1, trigger_side = ?, observed_value = ?, observed_change = ?,
                           quote_as_of = ?, error_detail = NULL, updated_at = ?
                       WHERE delivery_key = ? AND status = 'FAILED'""",
                    (lease_owner, token, expires, trigger_side, observed_value, observed_change, quote_as_of, now_str, delivery_key),
                )
                return True, "RECLAIMED_FAILED", token
            db.execute(
                """INSERT INTO market_watch_deliveries
                   (delivery_key, watch_id, chat_id, symbol, market, session_id, trade_date, trigger_side,
                    observed_value, observed_change, quote_as_of, status, lease_owner, lease_token,
                    lease_expires_at, attempt_no, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'IN_FLIGHT', ?, ?, ?, 1, ?, ?)""",
                (
                    delivery_key,
                    int(watch_id),
                    str(chat_id),
                    symbol,
                    market,
                    session_id,
                    trade_date,
                    trigger_side,
                    observed_value,
                    observed_change,
                    quote_as_of,
                    lease_owner,
                    token,
                    expires,
                    now_str,
                    now_str,
                ),
            )
            return True, "CLAIMED", token

    def finalize_market_watch_delivery(
        self,
        delivery_key: str,
        lease_token: str,
        status: str,
        provider_msg_id: str | None = None,
        error_detail: str | None = None,
    ) -> bool:
        """Moves an owned `IN_FLIGHT` lease to SENT / FAILED / UNKNOWN (token-guarded)."""
        final = status if status in {"SENT", "FAILED", "UNKNOWN"} else "UNKNOWN"
        with self._connect() as db:
            cur = db.execute(
                """UPDATE market_watch_deliveries
                   SET status = ?, provider_msg_id = ?, error_detail = ?, lease_expires_at = NULL, updated_at = ?
                   WHERE delivery_key = ? AND lease_token = ? AND status = 'IN_FLIGHT'""",
                (final, provider_msg_id, error_detail, self._utc_now_str(), delivery_key, lease_token),
            )
            return cur.rowcount == 1

    def get_market_watch_delivery(self, delivery_key: str) -> dict[str, Any] | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM market_watch_deliveries WHERE delivery_key = ?", (delivery_key,)).fetchone()
            return dict(row) if row else None

    # =========================================================================
    # Admin Sync: Custom Watches / Custom Deliveries (read-mostly, cross-bot)
    #   Travel tables live in the same SQLite file only when travel_bot shares it; Admin never instantiates
    #   TravelBriefDatabase (that would CREATE the travel tables), so every travel query is `_table_exists`
    #   guarded and degrades to empty results / zero counts on a market-only DB.
    # =========================================================================

    @staticmethod
    def _table_exists(db: sqlite3.Connection, table_name: str) -> bool:
        row = db.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (str(table_name),)
        ).fetchone()
        return row is not None

    @staticmethod
    def _table_columns(db: sqlite3.Connection, table_name: str) -> set[str]:
        # table_name is always an internal constant (never user input).
        return {row[1] for row in db.execute(f"PRAGMA table_info({table_name})").fetchall()}

    @staticmethod
    def _fmt_decimal_text(value: Any) -> str | None:
        """Decimal-safe display text for TEXT numerics (never `float()`); invalid/NaN/Inf keep the raw text."""
        if value is None:
            return None
        raw = str(value).strip()
        if raw == "":
            return None
        try:
            dec = Decimal(raw)
        except (InvalidOperation, ValueError):
            return raw
        if not dec.is_finite():
            return raw
        return format(dec, "f")

    def _open_readonly_sidecar(self, filename: str) -> sqlite3.Connection | None:
        """Open a sibling bot database without creating it or bypassing its WAL."""
        db_path = str(self.db_path).strip()
        if db_path == ":memory:" or db_path.startswith("file::memory:"):
            return None
        sidecar = Path(self.db_path).resolve().with_name(filename)
        if not sidecar.is_file():
            return None
        conn: sqlite3.Connection | None = None
        try:
            conn = sqlite3.connect(f"{sidecar.as_uri()}?mode=ro", uri=True, timeout=15.0)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only = ON")
            return conn
        except (sqlite3.Error, OSError):
            if conn is not None:
                conn.close()
            return None

    def get_user_bot_context(self, user_id: str) -> dict[str, Any]:
        """Return an admin-safe, cross-bot snapshot for one platform user.

        The Admin process owns only the market database. Other bot databases are
        optional sibling sidecars and are therefore read-only and best-effort.
        Missing tables/files produce an empty section rather than creating schema
        or turning a user drawer request into a 500 response.
        """
        uid = str(user_id).strip()
        context: dict[str, Any] = {
            "brief_bot": {"available": True, "watches": []},
            "travel_bot": {
                "available": True,
                "watches": [],
                "prefs": {"weekly_enabled": True, "drop_paused": False},
            },
            "admission_bot": {"available": True, "subscribers": [], "subscriptions": []},
            "kids_bot": {
                "available": False,
                "profile": None,
                "children": [],
                "pregnancy": None,
                "active_dose_timers": 0,
            },
            "yolo_bot": {"available": False, "profile": None},
            "silver_bot": {"available": False, "parents": []},
        }

        def rows_for_user(db: sqlite3.Connection, table: str, preferred_keys: tuple[str, ...]) -> list[dict[str, Any]]:
            if not self._table_exists(db, table):
                return []
            columns = self._table_columns(db, table)
            key = next((candidate for candidate in preferred_keys if candidate in columns), None)
            if not key:
                return []
            return [dict(row) for row in db.execute(f"SELECT * FROM {table} WHERE {key} = ?", (uid,)).fetchall()]

        with self._connect() as db:
            if self._table_exists(db, "market_volatility_watches"):
                try:
                    for row in rows_for_user(db, "market_volatility_watches", ("user_id", "telegram_user_id", "chat_id")):
                        context["brief_bot"]["watches"].append(
                            {
                                "id": row.get("id", row.get("watch_id")),
                                "watch_id": row.get("watch_id", row.get("id")),
                                "symbol": row.get("symbol"),
                                "display_name": row.get("display_name") or row.get("symbol"),
                                "threshold_pct": self._fmt_decimal_text(
                                    row.get("threshold_pct", row.get("threshold_value"))
                                ),
                                "is_active": bool(row.get("is_active")),
                                "last_triggered_at": row.get("last_triggered_at"),
                            }
                        )
                except sqlite3.Error:
                    context["brief_bot"] = {"available": False, "watches": []}

            if self._table_exists(db, "travel_price_watches"):
                try:
                    travel_cols = self._table_columns(db, "travel_price_watches")
                    travel_rows = rows_for_user(db, "travel_price_watches", ("chat_id", "telegram_user_id"))
                    for row in travel_rows:
                        destination = row.get("destination", row.get("dest_code"))
                        target_price = row.get("target_price", row.get("target_price_krw"))
                        context["travel_bot"]["watches"].append(
                            {
                                "id": row.get("id", row.get("watch_id")),
                                "watch_id": row.get("watch_id", row.get("id")),
                                "destination": destination,
                                "dest_code": row.get("dest_code", destination),
                                "target_price": target_price,
                                "target_price_krw": row.get("target_price_krw", target_price),
                                "theme": row.get("theme", row.get("preset_kind")),
                                "is_active": bool(row.get("is_active")),
                                "last_alert_at": row.get("last_alert_at", row.get("last_seen_at")),
                                **{
                                    key: row.get(key)
                                    for key in (
                                        "preset_kind", "day_pattern", "include_peak", "last_seen_price_krw",
                                        "last_seen_slot", "last_seen_at", "created_at", "updated_at",
                                    )
                                    if key in travel_cols
                                },
                            }
                        )
                except sqlite3.Error:
                    context["travel_bot"]["available"] = False

            if self._table_exists(db, "travel_user_prefs"):
                try:
                    prefs = rows_for_user(db, "travel_user_prefs", ("chat_id", "telegram_user_id"))
                    if prefs:
                        pref = prefs[0]
                        weekly = pref.get("weekly_enabled", pref.get("weekly_brief_enabled", True))
                        paused = pref.get("drop_paused", pref.get("watches_paused", False))
                        context["travel_bot"]["prefs"] = {
                            "weekly_enabled": bool(weekly),
                            "drop_paused": bool(paused),
                        }
                except sqlite3.Error:
                    context["travel_bot"]["available"] = False

            if self._table_exists(db, "admission_subscribers"):
                try:
                    subscribers = rows_for_user(
                        db, "admission_subscribers", ("telegram_user_id", "chat_id")
                    )
                    context["admission_bot"]["subscribers"] = subscribers
                    context["admission_bot"]["subscriptions"] = subscribers
                except sqlite3.Error:
                    context["admission_bot"] = {"available": False, "subscribers": [], "subscriptions": []}

        def sidecar_failure(defaults: dict[str, Any]) -> dict[str, Any]:
            return {**defaults, "available": False}

        sidecars_enabled = not (
            str(self.db_path).strip() == ":memory:"
            or str(self.db_path).strip().startswith("file::memory:")
        )
        kids_conn = self._open_readonly_sidecar("kids_compass.sqlite3") if sidecars_enabled else None
        if kids_conn is not None:
            try:
                profile_rows = rows_for_user(
                    kids_conn, "kids_user_profiles", ("telegram_user_id", "chat_id")
                )
                child_rows = rows_for_user(kids_conn, "kids_children", ("telegram_user_id", "chat_id"))
                pregnancy_rows = rows_for_user(
                    kids_conn, "kids_pregnancy_profiles", ("telegram_user_id", "chat_id")
                )
                timer_count = 0
                if self._table_exists(kids_conn, "kids_dose_timers"):
                    timer_cols = self._table_columns(kids_conn, "kids_dose_timers")
                    if "telegram_user_id" in timer_cols and "status" in timer_cols:
                        timer_count = int(
                            kids_conn.execute(
                                "SELECT COUNT(*) FROM kids_dose_timers WHERE telegram_user_id = ? "
                                "AND status IN ('PENDING', 'IN_FLIGHT')",
                                (uid,),
                            ).fetchone()[0]
                        )
                profile = profile_rows[0] if profile_rows else None
                if profile:
                    allowed = (
                        "default_region", "alert_weekend_brief", "alert_dday_reserve",
                        "alert_health_dday", "alert_weekend_mode",
                    )
                    profile = {key: profile.get(key) for key in allowed if key in profile}
                children = []
                for child in child_rows:
                    children.append(
                        {key: child.get(key) for key in ("child_id", "nickname", "birth_date", "weight_kg", "is_primary") if key in child}
                    )
                pregnancy = None
                if pregnancy_rows:
                    pregnancy = {
                        key: pregnancy_rows[0].get(key)
                        for key in ("nickname", "due_date")
                        if key in pregnancy_rows[0]
                    }
                context["kids_bot"] = {
                    "available": True,
                    "profile": profile,
                    "children": children,
                    "pregnancy": pregnancy,
                    "active_dose_timers": timer_count,
                }
            except (sqlite3.Error, OSError):
                context["kids_bot"] = sidecar_failure(
                    {"profile": None, "children": [], "pregnancy": None, "active_dose_timers": 0}
                )
            finally:
                kids_conn.close()

        yolo_conn = self._open_readonly_sidecar("yolo_brief.sqlite3") if sidecars_enabled else None
        if yolo_conn is not None:
            try:
                rows = rows_for_user(yolo_conn, "yolo_users", ("telegram_user_id", "chat_id"))
                context["yolo_bot"] = {"available": True, "profile": rows[0] if rows else None}
            except (sqlite3.Error, OSError):
                context["yolo_bot"] = sidecar_failure({"profile": None})
            finally:
                yolo_conn.close()

        silver_conn = self._open_readonly_sidecar("silver_care.sqlite3") if sidecars_enabled else None
        if silver_conn is not None:
            try:
                rows = rows_for_user(silver_conn, "silver_parents", ("telegram_user_id", "chat_id"))
                context["silver_bot"] = {"available": True, "parents": rows}
            except (sqlite3.Error, OSError):
                context["silver_bot"] = sidecar_failure({"parents": []})
            finally:
                silver_conn.close()

        return context

    # =========================================================================
    # Admin Sync: Chungyak bot sidecar (`chungyak_brief.sqlite3`) — strictly read-only.
    #   Admin never instantiates the chungyak sidecar DB class (that would CREATE its tables). Missing file/table -> [].
    # =========================================================================

    CHUNGYAK_DB_FILE = "chungyak_brief.sqlite3"
    CHUNGYAK_DELIVERY_STATUSES = ("PENDING", "SENT", "UNKNOWN", "FAILED", "QUEUED_MORNING")
    _CHUNGYAK_STATUS_LABELS = {
        "QUEUED_MORNING": "야간 대기 (08:00 발송 예약)",
        "UNKNOWN": "결과 불명 (재발송 금지)",
        "PENDING": "발송 중 (리스 보유)",
        "SENT": "발송 성공",
        "FAILED": "발송 실패",
    }

    @staticmethod
    def _mask_chat_id(value: Any) -> str:
        raw = "" if value is None else str(value).strip()
        if len(raw) <= 4:
            return "*" * len(raw)
        return raw[:2] + "*" * (len(raw) - 4) + raw[-2:]

    @staticmethod
    def _json_list(value: Any) -> list[Any] | None:
        if value is None:
            return None
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return None
        return list(parsed) if isinstance(parsed, list) else None

    @staticmethod
    def _tri_bool(value: Any) -> bool | None:
        return None if value is None else bool(value)

    def _chungyak_query(self, table: str, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Runs one read-only query on the chungyak sidecar; [] when the file / table is absent or unreadable."""
        conn = self._open_readonly_sidecar(self.CHUNGYAK_DB_FILE)
        if conn is None:
            return []
        try:
            if not self._table_exists(conn, table):
                return []
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def list_chungyak_notices(self, limit: int = 50, include_samples: bool = False) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 50), 200))
        where = "" if include_samples else "WHERE is_sample = 0"
        rows = self._chungyak_query(
            "chungyak_notices",
            f"SELECT * FROM chungyak_notices {where} ORDER BY updated_at DESC, notice_key LIMIT ?",
            (limit,),
        )
        items = []
        for r in rows:
            items.append({
                "notice_key": r.get("notice_key"),
                "notice_type": r.get("notice_type"),
                "house_type": r.get("house_type"),
                "house_name": r.get("house_name"),
                "region_sido": r.get("region_sido"),
                "region_sigungu": r.get("region_sigungu"),
                "supply_households": r.get("supply_households"),
                "receipt_start": r.get("receipt_start"),
                "receipt_end": r.get("receipt_end"),
                "winner_date": r.get("winner_date"),
                "min_price_eok": self._fmt_decimal_text(r.get("min_price_eok")),
                "min_area_m2": self._fmt_decimal_text(r.get("min_area_m2")),
                "max_area_m2": self._fmt_decimal_text(r.get("max_area_m2")),
                "is_regulated": self._tri_bool(r.get("is_regulated")),
                "special_supply_types": self._json_list(r.get("special_supply_types")) or [],
                "is_sample": bool(r.get("is_sample")),
                "revision": int(r.get("revision") or 1),
                "content_hash": str(r.get("content_hash") or "")[:12],
                "first_seen_at": r.get("first_seen_at"),
                "updated_at": r.get("updated_at"),
            })
        return items

    def list_chungyak_user_profiles(self, limit: int = 100) -> list[dict[str, Any]]:
        """Custom-match conditions per user. chat_id is masked (full ids only via the fail-closed PII API)."""
        limit = max(1, min(int(limit or 100), 500))
        rows = self._chungyak_query(
            "chungyak_user_profiles",
            "SELECT * FROM chungyak_user_profiles ORDER BY updated_at DESC, chat_id LIMIT ?",
            (limit,),
        )
        answer_cols = (
            "residence_sido", "residence_years", "is_homeless", "is_household_head", "account_months",
            "deposit_manwon", "payment_count", "preferred_regions", "max_price_eok", "desired_area_m2", "special_types",
        )
        regular_by_chat: dict[str, dict[str, Any]] = {}
        if rows:
            for s in self._chungyak_query("chungyak_regular_subs", "SELECT * FROM chungyak_regular_subs"):
                regular_by_chat[str(s.get("chat_id"))] = {
                    "is_enabled": bool(s.get("is_enabled")),
                    **self._chungyak_regular_prefs(s),
                }
        items = []
        for r in rows:
            regions = self._json_list(r.get("preferred_regions"))
            specials = self._json_list(r.get("special_types"))
            answered = sum(1 for c in answer_cols if r.get(c) not in (None, "", "[]"))
            items.append({
                "chat_id_masked": self._mask_chat_id(r.get("chat_id")),
                "residence_sido": r.get("residence_sido"),
                "residence_years": r.get("residence_years"),
                "is_homeless": self._tri_bool(r.get("is_homeless")),
                "is_household_head": self._tri_bool(r.get("is_household_head")),
                "account_months": r.get("account_months"),
                "deposit_manwon": r.get("deposit_manwon"),
                "payment_count": r.get("payment_count"),
                "preferred_regions": regions,
                "max_price_eok": self._fmt_decimal_text(r.get("max_price_eok")),
                "desired_area_m2": self._fmt_decimal_text(r.get("desired_area_m2")),
                "special_types": specials,
                "include_officetel": self._tri_bool(r.get("include_officetel")),
                "custom_alert_enabled": r.get("custom_alert_enabled") is None or bool(r.get("custom_alert_enabled")),
                "answered_fields": answered,
                "is_empty": answered == 0,
                "regular": regular_by_chat.get(str(r.get("chat_id"))),  # None = no regular-digest row
                "updated_at": r.get("updated_at"),
            })
        return items

    def list_chungyak_regular_subs(self, limit: int = 200) -> list[dict[str, Any]]:
        """Regular digest subscribers with raw chat_id (PII) — served only behind the fail-closed token."""
        limit = max(1, min(int(limit or 200), 1000))
        # SELECT * (not a column list): pre-migration sidecars lack the sub_* filter columns -> safe defaults below.
        rows = self._chungyak_query(
            "chungyak_regular_subs",
            "SELECT * FROM chungyak_regular_subs ORDER BY updated_at DESC, chat_id LIMIT ?",
            (limit,),
        )
        names: dict[str, str] = {}
        if rows:
            try:
                with self._connect() as db:
                    if self._table_exists(db, "users"):
                        ids = [str(r["chat_id"]) for r in rows]
                        marks = ",".join("?" for _ in ids)
                        for u in db.execute(
                            f"SELECT telegram_user_id, full_name FROM users WHERE telegram_user_id IN ({marks})", ids
                        ).fetchall():
                            if u["full_name"] and str(u["full_name"]).strip():
                                names[str(u["telegram_user_id"])] = str(u["full_name"]).strip()
            except sqlite3.Error:
                names = {}
        return [
            {
                "chat_id": str(r.get("chat_id")),
                "display_name": names.get(str(r.get("chat_id"))) or str(r.get("chat_id")),
                "is_enabled": bool(r.get("is_enabled")),
                "send_time_kst": r.get("send_time_kst"),
                **self._chungyak_regular_prefs(r),
                "created_at": r.get("created_at"),
                "updated_at": r.get("updated_at"),
            }
            for r in rows
        ]

    _CHUNGYAK_AREA_BUCKETS = ("59", "84", "85+")

    @classmethod
    def _chungyak_regular_prefs(cls, r: dict[str, Any]) -> dict[str, Any]:
        """Regular-digest filter columns with legacy defaults (old sidecar: all types ON, all areas, no D-1)."""

        def flag(col: str, default: int) -> bool:
            v = r.get(col)
            return bool(default if v is None else v)

        areas = cls._json_list(r.get("sub_areas_json"))
        areas = [a for a in cls._CHUNGYAK_AREA_BUCKETS if a in {str(x) for x in (areas or [])}] or None
        return {
            "sub_moosunwi": flag("sub_moosunwi", 1),
            "sub_public": flag("sub_public", 1),
            "sub_general": flag("sub_general", 1),
            "sub_areas_json": areas,
            "sub_deadline_d1": flag("sub_deadline_d1", 0),
        }

    def list_chungyak_deliveries(
        self, status: str | None = None, limit: int = 50
    ) -> tuple[list[dict[str, Any]], bool]:
        """Newest-first chungyak delivery ledger (masked chat_id). Returns (items, has_more)."""
        limit = max(1, min(int(limit or 50), 100))
        status_norm = str(status).strip().upper() if status and str(status).strip() else None
        where, params = "", []
        if status_norm:
            where = "WHERE d.status = ?"
            params.append(status_norm)
        conn = self._open_readonly_sidecar(self.CHUNGYAK_DB_FILE)
        if conn is None:
            return [], False
        try:
            if not self._table_exists(conn, "chungyak_deliveries"):
                return [], False
            join_name = self._table_exists(conn, "chungyak_notices")
            name_sql = "n.house_name" if join_name else "NULL"
            join_sql = "LEFT JOIN chungyak_notices n ON n.notice_key = d.notice_key" if join_name else ""
            rows = conn.execute(
                f"""SELECT d.*, {name_sql} AS house_name
                      FROM chungyak_deliveries d {join_sql}
                      {where}
                     ORDER BY d.created_at DESC, d.delivery_key DESC
                     LIMIT ?""",
                (*params, limit + 1),
            ).fetchall()
        except sqlite3.Error:
            return [], False
        finally:
            conn.close()
        has_more = len(rows) > limit
        items = []
        for row in rows[:limit]:
            r = dict(row)
            st = str(r.get("status") or "").upper()
            items.append({
                "bot_id": "chungyak_bot",
                "delivery_key_short": str(r.get("delivery_key") or "").split(":")[0],
                "chat_id_masked": self._mask_chat_id(r.get("chat_id")),
                "notice_key": r.get("notice_key"),
                "house_name": r.get("house_name"),
                "channel_type": r.get("channel_type"),
                "status": st,
                "status_label": self._CHUNGYAK_STATUS_LABELS.get(st, st),
                "attempt_no": r.get("attempt_no"),
                "scheduled_at_kst": r.get("scheduled_at_kst"),
                "trigger_context": r.get("trigger_context"),
                "error_detail": r.get("error_detail"),
                "created_at": r.get("created_at"),
                "updated_at": r.get("updated_at"),
            })
        return items, has_more

    @staticmethod
    def _fmt_krw(value: Any) -> str | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return f"{int(value):,}원"
        except (TypeError, ValueError):
            return str(value)

    _NAME_SQL = "COALESCE(NULLIF(TRIM(u.full_name), ''), NULLIF(TRIM(r.name), ''), {chat})"

    def list_custom_watch_deliveries(
        self, bot_id: str = "all", status: str | None = None, limit: int = 50
    ) -> tuple[list[dict[str, Any]], bool]:
        """Newest-first custom watch deliveries across brief_bot + travel_bot. Returns (items, has_more)."""
        limit = max(1, min(int(limit or 50), 100))
        fetch_n = limit + 1
        status_norm = str(status).strip().upper() if status and str(status).strip() else None
        items: list[dict[str, Any]] = []
        with self._connect() as db:
            if bot_id in ("all", "brief_bot") and self._table_exists(db, "market_watch_deliveries"):
                where = ["w.bot_id = 'brief_bot'"]
                params: list[Any] = []
                if status_norm:
                    where.append("d.status = ?")
                    params.append(status_norm)
                rows = db.execute(
                    f"""SELECT d.*, w.telegram_user_id, w.condition_type, w.direction, w.threshold_value,
                               w.threshold_unit, {self._NAME_SQL.format(chat='d.chat_id')} AS display_name
                        FROM market_watch_deliveries d
                        JOIN market_volatility_watches w ON w.watch_id = d.watch_id
                        LEFT JOIN users u ON u.telegram_user_id = w.telegram_user_id
                        LEFT JOIN recipients r ON r.chat_id = d.chat_id
                        WHERE {' AND '.join(where)}
                        ORDER BY d.created_at DESC, d.delivery_key DESC
                        LIMIT ?""",
                    (*params, fetch_n),
                ).fetchall()
                for r in rows:
                    d = dict(r)
                    observed = self._fmt_decimal_text(d.get("observed_value"))
                    change = self._fmt_decimal_text(d.get("observed_change"))
                    items.append(
                        {
                            "bot_id": "brief_bot",
                            "delivery_type": "CUSTOM_WATCH",
                            "delivery_kind": "VOLATILITY",
                            "delivery_key": d["delivery_key"],
                            "watch_id": d["watch_id"],
                            "chat_id": d["chat_id"],
                            "telegram_user_id": d.get("telegram_user_id"),
                            "display_name": d.get("display_name") or d["chat_id"],
                            "target": d["symbol"],
                            "market": d.get("market"),
                            "trigger_side": d.get("trigger_side"),
                            "trade_date": d.get("trade_date"),
                            "condition_type": d.get("condition_type"),
                            "direction": d.get("direction"),
                            "threshold_value": self._fmt_decimal_text(d.get("threshold_value")),
                            "threshold_unit": d.get("threshold_unit"),
                            "observed_value": observed,
                            "observed_change": change,
                            "quote_as_of": d.get("quote_as_of"),
                            "value_text": " / ".join(x for x in (observed, change) if x) or None,
                            "status": d["status"],
                            "attempt_no": d.get("attempt_no"),
                            "error_detail": d.get("error_detail"),
                            "created_at": d.get("created_at"),
                            "updated_at": d.get("updated_at"),
                        }
                    )

            if bot_id in ("all", "travel_bot") and self._table_exists(db, "travel_watch_deliveries"):
                cols = self._table_columns(db, "travel_watch_deliveries")
                kind_sql = "COALESCE(d.delivery_kind, 'DROP')" if "delivery_kind" in cols else "'DROP'"
                kst_sql = "d.kst_date" if "kst_date" in cols else "NULL"
                has_watches = self._table_exists(db, "travel_price_watches")
                target_sql = "w.target_price_krw" if has_watches else "NULL"
                watch_join = (
                    "LEFT JOIN travel_price_watches w ON w.watch_id = d.watch_id" if has_watches else ""
                )
                where = ["1 = 1"]
                params = []
                if status_norm:
                    where.append("d.status = ?")
                    params.append(status_norm)
                rows = db.execute(
                    f"""SELECT d.*, {kind_sql} AS delivery_kind_norm, {kst_sql} AS kst_date_norm,
                               {target_sql} AS target_price_krw,
                               {self._NAME_SQL.format(chat='d.chat_id')} AS display_name
                        FROM travel_watch_deliveries d
                        {watch_join}
                        LEFT JOIN users u ON u.telegram_user_id = d.chat_id
                        LEFT JOIN recipients r ON r.chat_id = d.chat_id
                        WHERE {' AND '.join(where)}
                        ORDER BY d.created_at DESC, d.delivery_key DESC
                        LIMIT ?""",
                    (*params, fetch_n),
                ).fetchall()
                for r in rows:
                    d = dict(r)
                    price_text = self._fmt_krw(d.get("price_krw"))
                    items.append(
                        {
                            "bot_id": "travel_bot",
                            "delivery_type": "CUSTOM_WATCH",
                            "delivery_kind": d.get("delivery_kind_norm") or "DROP",
                            "delivery_key": d["delivery_key"],
                            "watch_id": d["watch_id"],
                            "chat_id": d["chat_id"],
                            "telegram_user_id": None,
                            "display_name": d.get("display_name") or d["chat_id"],
                            "target": d["dest_code"],
                            "dep_date": d.get("dep_date"),
                            "ret_date": d.get("ret_date"),
                            "kst_date": d.get("kst_date_norm"),
                            "price_krw": d.get("price_krw"),
                            "price_text": price_text,
                            "target_price_krw": d.get("target_price_krw"),
                            "target_price_text": self._fmt_krw(d.get("target_price_krw")),
                            "value_text": price_text,
                            "status": d["status"],
                            "attempt_no": d.get("attempt_no"),
                            "error_detail": d.get("error_detail"),
                            "created_at": d.get("created_at"),
                            "updated_at": d.get("updated_at"),
                        }
                    )

        items.sort(key=lambda x: (str(x.get("created_at") or ""), str(x.get("delivery_key") or "")), reverse=True)
        has_more = len(items) > limit
        return items[:limit], has_more

    @staticmethod
    def _fmt_travel_time_window(row: dict[str, Any]) -> str:
        """`출국 09:00~12:00 · 귀국도착 18:00~23:59` ("" when no / invalid / pre-migration time columns)."""
        def _hm(v: Any) -> "str | None":
            try:
                m = int(v)
            except (TypeError, ValueError):
                return None
            return f"{m // 60:02d}:{m % 60:02d}" if 0 <= m <= 1439 else None

        parts = []
        for label, f_key, t_key in (("출국", "dep_time_from_min", "dep_time_to_min"),
                                    ("귀국도착", "ret_arr_from_min", "ret_arr_to_min")):
            f_txt, t_txt = _hm(row.get(f_key)), _hm(row.get(t_key))
            if f_txt and t_txt:
                parts.append(f"{label} {f_txt}~{t_txt}")
        return " · ".join(parts)

    def list_all_watches(self, bot_id: str = "all", active_only: bool = True) -> dict[str, Any]:
        """Cross-bot custom watch listing + travel 2-track prefs (no row / no table == defaults)."""
        items: list[dict[str, Any]] = []
        user_prefs: list[dict[str, Any]] = []
        with self._connect() as db:
            if bot_id in ("all", "brief_bot") and self._table_exists(db, "market_volatility_watches"):
                sql = f"""SELECT w.*, {self._NAME_SQL.format(chat='w.chat_id')} AS display_name
                          FROM market_volatility_watches w
                          LEFT JOIN users u ON u.telegram_user_id = w.telegram_user_id
                          LEFT JOIN recipients r ON r.chat_id = w.chat_id
                          WHERE w.bot_id = 'brief_bot'"""
                if active_only:
                    sql += " AND w.is_active = 1"
                sql += " ORDER BY w.created_at DESC, w.watch_id DESC"
                for r in db.execute(sql).fetchall():
                    d = dict(r)
                    items.append(
                        {
                            "bot_id": "brief_bot",
                            "watch_id": d["watch_id"],
                            "chat_id": d["chat_id"],
                            "telegram_user_id": d.get("telegram_user_id"),
                            "display_name": d.get("display_name") or d["chat_id"],
                            "target": d["symbol"],
                            "market": d.get("market"),
                            "condition_type": d.get("condition_type"),
                            "direction": d.get("direction"),
                            "threshold_value": self._fmt_decimal_text(d.get("threshold_value")),
                            "threshold_unit": d.get("threshold_unit"),
                            "last_observed_value": self._fmt_decimal_text(d.get("last_observed_value")),
                            "last_observed_change": self._fmt_decimal_text(d.get("last_observed_change")),
                            "is_active": bool(d.get("is_active")),
                            "is_armed": bool(d.get("is_armed")),
                            "deactivated_reason": d.get("deactivated_reason"),
                            "last_triggered_at": d.get("last_triggered_at"),
                            "created_at": d.get("created_at"),
                            "updated_at": d.get("updated_at"),
                        }
                    )

            if bot_id in ("all", "travel_bot"):
                has_prefs = self._table_exists(db, "travel_user_prefs")
                travel_chat_ids: list[str] = []
                if self._table_exists(db, "travel_price_watches"):
                    cols = self._table_columns(db, "travel_price_watches")
                    extra = ", ".join(
                        f"w.{c}" if c in cols else f"NULL AS {c}"
                        for c in ("watch_mode", "horizon_months", "weekday_option", "fixed_dep_date",
                                  "fixed_ret_date", "notify_mode", "initial_price_krw",
                                  "dep_time_from_min", "dep_time_to_min", "ret_arr_from_min", "ret_arr_to_min")
                    )
                    sql = f"""SELECT w.watch_id, w.chat_id, w.dest_code, w.target_price_krw, w.is_active,
                                     w.last_seen_price_krw, w.last_seen_at, w.created_at, w.updated_at, {extra},
                                     {self._NAME_SQL.format(chat='w.chat_id')} AS display_name
                              FROM travel_price_watches w
                              LEFT JOIN users u ON u.telegram_user_id = w.chat_id
                              LEFT JOIN recipients r ON r.chat_id = w.chat_id"""
                    if active_only:
                        sql += " WHERE w.is_active = 1"
                    sql += " ORDER BY w.created_at DESC, w.watch_id DESC"
                    for r in db.execute(sql).fetchall():
                        d = dict(r)
                        travel_chat_ids.append(str(d["chat_id"]))
                        items.append(
                            {
                                "bot_id": "travel_bot",
                                "watch_id": d["watch_id"],
                                "chat_id": d["chat_id"],
                                "telegram_user_id": None,
                                "display_name": d.get("display_name") or d["chat_id"],
                                "target": d["dest_code"],
                                "target_price_krw": d.get("target_price_krw"),
                                "target_price_text": self._fmt_krw(d.get("target_price_krw")),
                                "last_seen_price_krw": d.get("last_seen_price_krw"),
                                "last_seen_price_text": self._fmt_krw(d.get("last_seen_price_krw")),
                                "watch_mode": d.get("watch_mode"),
                                "horizon_months": d.get("horizon_months"),
                                "weekday_option": d.get("weekday_option"),
                                "fixed_dep_date": d.get("fixed_dep_date"),
                                "fixed_ret_date": d.get("fixed_ret_date"),
                                "notify_mode": d.get("notify_mode"),
                                "dep_time_from_min": d.get("dep_time_from_min"),
                                "dep_time_to_min": d.get("dep_time_to_min"),
                                "ret_arr_from_min": d.get("ret_arr_from_min"),
                                "ret_arr_to_min": d.get("ret_arr_to_min"),
                                "time_window_text": self._fmt_travel_time_window(d),
                                "is_active": bool(d.get("is_active")),
                                "last_seen_at": d.get("last_seen_at"),
                                "created_at": d.get("created_at"),
                                "updated_at": d.get("updated_at"),
                            }
                        )

                explicit: dict[str, dict[str, Any]] = {}
                if has_prefs:
                    for r in db.execute(
                        f"""SELECT p.chat_id, p.weekly_brief_enabled, p.watches_paused, p.updated_at,
                                   {self._NAME_SQL.format(chat='p.chat_id')} AS display_name
                            FROM travel_user_prefs p
                            LEFT JOIN users u ON u.telegram_user_id = p.chat_id
                            LEFT JOIN recipients r ON r.chat_id = p.chat_id
                            ORDER BY p.updated_at DESC, p.chat_id ASC"""
                    ).fetchall():
                        d = dict(r)
                        explicit[str(d["chat_id"])] = {
                            "chat_id": d["chat_id"],
                            "display_name": d.get("display_name") or d["chat_id"],
                            "weekly_brief_enabled": bool(d.get("weekly_brief_enabled")),
                            "watches_paused": bool(d.get("watches_paused")),
                            "is_default": False,
                            "updated_at": d.get("updated_at"),
                        }
                user_prefs.extend(explicit.values())
                seen = set(explicit)
                for item in items:
                    cid = str(item["chat_id"])
                    if item["bot_id"] != "travel_bot" or cid in seen:
                        continue
                    seen.add(cid)
                    user_prefs.append(
                        {
                            "chat_id": item["chat_id"],
                            "display_name": item["display_name"],
                            "weekly_brief_enabled": True,
                            "watches_paused": False,
                            "is_default": True,
                            "updated_at": None,
                        }
                    )
        return {"items": items, "user_prefs": user_prefs}

    def get_custom_watch_stats_24h(self) -> dict[str, int]:
        """UTC rolling-24h delivery counts + active counts (missing travel tables == 0)."""
        stats = {
            "active_market_watches_count": 0,
            "recent_market_watch_deliveries_24h": 0,
            "active_travel_watches_count": 0,
            "recent_travel_watch_deliveries_24h": 0,
            "paused_users_count": 0,
            "weekly_disabled_count": 0,
        }
        since = "datetime(created_at) >= datetime('now', '-24 hours')"
        with self._connect() as db:
            if self._table_exists(db, "market_volatility_watches"):
                stats["active_market_watches_count"] = int(
                    db.execute("SELECT COUNT(*) FROM market_volatility_watches WHERE is_active = 1").fetchone()[0]
                )
            if self._table_exists(db, "market_watch_deliveries"):
                stats["recent_market_watch_deliveries_24h"] = int(
                    db.execute(f"SELECT COUNT(*) FROM market_watch_deliveries WHERE {since}").fetchone()[0]
                )
            if self._table_exists(db, "travel_price_watches"):
                stats["active_travel_watches_count"] = int(
                    db.execute("SELECT COUNT(*) FROM travel_price_watches WHERE is_active = 1").fetchone()[0]
                )
            if self._table_exists(db, "travel_watch_deliveries"):
                stats["recent_travel_watch_deliveries_24h"] = int(
                    db.execute(f"SELECT COUNT(*) FROM travel_watch_deliveries WHERE {since}").fetchone()[0]
                )
            if self._table_exists(db, "travel_user_prefs"):
                row = db.execute(
                    """SELECT COALESCE(SUM(CASE WHEN watches_paused = 1 THEN 1 ELSE 0 END), 0),
                              COALESCE(SUM(CASE WHEN weekly_brief_enabled = 0 THEN 1 ELSE 0 END), 0)
                       FROM travel_user_prefs"""
                ).fetchone()
                stats["paused_users_count"] = int(row[0])
                stats["weekly_disabled_count"] = int(row[1])
        return stats

    def deactivate_watch_admin(
        self, bot_id: str, watch_id: int, reason: str = "ADMIN_DISABLED"
    ) -> dict[str, Any]:
        """Admin-forced single watch deactivation (no owner guard). Idempotent.

        Returns {"found", "deactivated", "bot_id", "watch_id", "owner_id", "target"}; `found=False` -> 404.
        """
        wid = int(watch_id)
        result: dict[str, Any] = {
            "found": False, "deactivated": False, "bot_id": bot_id, "watch_id": wid, "owner_id": None, "target": None
        }
        with self._immediate_tx() as db:
            if bot_id == "brief_bot":
                if not self._table_exists(db, "market_volatility_watches"):
                    return result
                row = db.execute(
                    "SELECT telegram_user_id, chat_id, symbol, is_active FROM market_volatility_watches "
                    "WHERE watch_id = ? AND bot_id = 'brief_bot'",
                    (wid,),
                ).fetchone()
                if not row:
                    return result
                result.update(found=True, owner_id=row["telegram_user_id"] or row["chat_id"], target=row["symbol"])
                cur = db.execute(
                    """UPDATE market_volatility_watches
                       SET is_active = 0, is_armed = 0, confirm_streak = 0, confirm_side = '',
                           deactivated_reason = ?, updated_at = ?
                       WHERE watch_id = ? AND is_active = 1""",
                    (reason, self._utc_now_str(), wid),
                )
                result["deactivated"] = cur.rowcount == 1
            elif bot_id == "travel_bot":
                if not self._table_exists(db, "travel_price_watches"):
                    return result
                row = db.execute(
                    "SELECT chat_id, dest_code, is_active FROM travel_price_watches WHERE watch_id = ?", (wid,)
                ).fetchone()
                if not row:
                    return result
                result.update(found=True, owner_id=row["chat_id"], target=row["dest_code"])
                cur = db.execute(
                    "UPDATE travel_price_watches SET is_active = 0, updated_at = ? WHERE watch_id = ? AND is_active = 1",
                    (self._utc_now_str(), wid),
                )
                result["deactivated"] = cur.rowcount == 1
        return result
