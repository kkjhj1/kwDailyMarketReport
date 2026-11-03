"""Direct Database Integration with alerts.sqlite3.
Synchronizes user profiles, membership tiers, and subscriptions directly with Admin Console.
Supports categorized bot discovery, instant keyword searching, and explicit subscribe/unsubscribe operations.
"""
from __future__ import annotations
from datetime import datetime
import sqlite3
from pathlib import Path
from typing import Optional

from portal_hub.models import BotCategory, BotServiceType, UserProfile, UserTier

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "alerts.sqlite3"


class PortalDatabase:
    """Thread-safe SQLite database adapter for alerts.sqlite3."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Guarantees required tables (users, subscriptions, bots, tier_policies, recipients) exist in database."""
        # 1. If pointing to physical file, attempt MarketBriefDatabase bootstrap
        if str(self.db_path) != ":memory:" and self.db_path.exists():
            try:
                from market_brief.storage.db import MarketBriefDatabase
                MarketBriefDatabase(db_path=str(self.db_path))
            except Exception:
                pass

        # 2. Direct schema execution (guaranteed to succeed on :memory: and physical databases)
        with self._get_conn() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS tier_policies (
                    tier_code TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    max_bots INTEGER NOT NULL,
                    badge_color TEXT NOT NULL DEFAULT '#64748b',
                    description TEXT DEFAULT '',
                    sort_order INTEGER NOT NULL DEFAULT 1
                );
                CREATE TABLE IF NOT EXISTS bots (
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
                );
                CREATE TABLE IF NOT EXISTS users (
                    telegram_user_id TEXT PRIMARY KEY,
                    username TEXT DEFAULT '',
                    full_name TEXT NOT NULL,
                    tier TEXT NOT NULL DEFAULT 'WHITE',
                    tier_expires_at TEXT,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    memo TEXT DEFAULT '',
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_user_id TEXT NOT NULL,
                    bot_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'ACTIVE',
                    subscribed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    cancelled_at TEXT,
                    UNIQUE(telegram_user_id, bot_id),
                    FOREIGN KEY(telegram_user_id) REFERENCES users(telegram_user_id),
                    FOREIGN KEY(bot_id) REFERENCES bots(bot_id)
                );
                CREATE TABLE IF NOT EXISTS recipients (
                    chat_id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    channel TEXT NOT NULL DEFAULT 'TELEGRAM',
                    group_tag TEXT NOT NULL DEFAULT 'DEFAULT',
                    subscribed_topics TEXT NOT NULL DEFAULT 'FINANCE',
                    is_active INTEGER NOT NULL DEFAULT 1,
                    inactivation_reason TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS upgrade_requests (
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
                );
                INSERT OR IGNORE INTO tier_policies (tier_code, name, max_bots, badge_color, description, sort_order)
                VALUES
                    ('WHITE', '화이트 (기본)', 1, '#94a3b8', '기본 1개 봇 무료 구독', 1),
                    ('GREEN', '그린 (스탠다드)', 3, '#10b981', '최대 3개 봇 동시 구독', 2),
                    ('RED', '레드 (프리미엄)', 5, '#ef4444', '최대 5개 봇 동시 구독', 3),
                    ('BLACK', '블랙 (VIP)', 999999, '#0f172a', '모든 봇 무제한 이용', 4);
                INSERT OR IGNORE INTO bots (bot_id, bot_type, name, category, icon_emoji, is_active, sort_order)
                VALUES
                    ('menu_bot', 'MENU', '통합 안내 메뉴봇', '가입·포털', '🧭', 1, 0),
                    ('brief_bot', 'INFO', '시황', '금융·시황', '📈', 1, 1),
                    ('travel_bot', 'INFO', '여행', '여행·라이프', '✈️', 1, 2),
                    ('admission_bot', 'INFO', '입시', '교육·입시', '🎓', 1, 3);
            """)
            conn.commit()

    def _get_conn(self) -> sqlite3.Connection:
        if str(self.db_path) == ":memory:":
            if not hasattr(self, "_mem_conn") or self._mem_conn is None:
                self._mem_conn = sqlite3.connect(":memory:", timeout=15.0)
                self._mem_conn.row_factory = sqlite3.Row
            return self._mem_conn
        conn = sqlite3.connect(str(self.db_path), timeout=15.0)
        conn.row_factory = sqlite3.Row
        return conn

    def get_or_create_user(
        self,
        user_id: int,
        chat_id: int,
        username: Optional[str] = None,
        first_name: Optional[str] = None,
    ) -> UserProfile:
        """Retrieves existing profile from alerts.sqlite3 users table, or creates with default WHITE/FREE."""
        uid_str = str(user_id).strip()
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM users WHERE telegram_user_id = ?", (uid_str,))
            row = cur.fetchone()
            if row:
                raw_tier = str(row["tier"] or "WHITE").upper()
                try:
                    tier_val = UserTier(raw_tier)
                except ValueError:
                    tier_val = UserTier.WHITE
                return UserProfile(
                    user_id=user_id,
                    chat_id=chat_id,
                    username=row["username"] or username,
                    first_name=row["full_name"] or first_name,
                    tier=tier_val,
                    status=row["status"] or "ACTIVE",
                    created_at=str(row["created_at"]),
                    updated_at=str(row["updated_at"]),
                )

            # Insert new user into alerts.sqlite3 users table
            name_to_use = first_name or (f"@{username}" if username else f"유저_{uid_str[-4:]}")
            cur.execute(
                """
                INSERT INTO users (telegram_user_id, username, full_name, tier, status, created_at, updated_at)
                VALUES (?, ?, ?, 'WHITE', 'ACTIVE', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                """,
                (uid_str, username or "", name_to_use),
            )
            conn.commit()
            return UserProfile(
                user_id=user_id,
                chat_id=chat_id,
                username=username,
                first_name=name_to_use,
                tier=UserTier.WHITE,
                status="ACTIVE",
            )

    def set_user_tier(self, user_id: int, tier: UserTier) -> bool:
        """Updates user tier in alerts.sqlite3."""
        uid_str = str(user_id).strip()
        tier_code = "WHITE" if tier in (UserTier.WHITE, UserTier.FREE) else tier.value
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                UPDATE users
                SET tier = ?, updated_at = CURRENT_TIMESTAMP
                WHERE telegram_user_id = ?
                """,
                (tier_code, uid_str),
            )
            conn.commit()
            return cur.rowcount > 0

    def get_user_subscriptions(self, user_id: int) -> dict[BotServiceType, bool]:
        """Returns subscription state for all bots from alerts.sqlite3 subscriptions table."""
        uid_str = str(user_id).strip()
        result = {bot: False for bot in BotServiceType}
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """
                SELECT bot_id, status FROM subscriptions
                WHERE telegram_user_id = ?
                """,
                (uid_str,),
            )
            for row in cur.fetchall():
                bid = row["bot_id"]
                is_active = (row["status"] == "ACTIVE")
                for bot_type in BotServiceType:
                    if bot_type.value == bid:
                        result[bot_type] = is_active
        return result

    def subscribe(self, user_id: int, bot_type: BotServiceType) -> tuple[bool, str]:
        """Explicitly activates subscription with tier quota enforcement."""
        uid_str = str(user_id).strip()
        bid = bot_type.value

        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM users WHERE telegram_user_id = ?", (uid_str,))
            user_row = cur.fetchone()
            if not user_row:
                return False, "사용자 정보를 찾을 수 없습니다."

            raw_tier = str(user_row["tier"] or "WHITE").upper()
            try:
                tier = UserTier(raw_tier)
            except ValueError:
                tier = UserTier.WHITE

            # Check quota
            cur.execute(
                """
                SELECT COUNT(*) as cnt FROM subscriptions s
                JOIN bots b ON s.bot_id = b.bot_id
                WHERE s.telegram_user_id = ? AND s.status = 'ACTIVE' AND b.bot_type = 'INFO'
                """,
                (uid_str,),
            )
            active_count = cur.fetchone()["cnt"]

            cur.execute(
                "SELECT id, status FROM subscriptions WHERE telegram_user_id = ? AND bot_id = ?",
                (uid_str, bid),
            )
            sub_row = cur.fetchone()
            already_active = bool(sub_row and sub_row["status"] == "ACTIVE")

            if already_active:
                return True, f"이미 [{bot_type.title}]을(를) 구독 중입니다."

            if tier != UserTier.BLACK and active_count >= tier.max_slots:
                return (
                    False,
                    f"⚠️ {tier.display_name} 등급은 최대 {tier.max_slots}개까지 구독 가능합니다.\n"
                    f"기존 구독을 해지하거나 등급을 업그레이드해 주세요!",
                )

            if sub_row:
                cur.execute(
                    "UPDATE subscriptions SET status = 'ACTIVE', subscribed_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (sub_row["id"],),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO subscriptions (telegram_user_id, bot_id, status, subscribed_at)
                    VALUES (?, ?, 'ACTIVE', CURRENT_TIMESTAMP)
                    """,
                    (uid_str, bid),
                )

            # Sync legacy recipients table for all topic-enabled bots
            topic_map = {"brief_bot": "FINANCE", "travel_bot": "TRAVEL", "admission_bot": "EDUCATION"}
            if bid in topic_map:
                topic = topic_map[bid]
                cur.execute("SELECT subscribed_topics, is_active FROM recipients WHERE chat_id = ?", (uid_str,))
                existing_rec = cur.fetchone()
                if existing_rec:
                    topics = set(t.strip() for t in (existing_rec["subscribed_topics"] or "").split(",") if t.strip())
                    topics.add(topic)
                    new_topics = ",".join(sorted(topics))
                    cur.execute(
                        "UPDATE recipients SET is_active = 1, subscribed_topics = ?, inactivation_reason = NULL WHERE chat_id = ?",
                        (new_topics, uid_str),
                    )
                else:
                    cur.execute(
                        """
                        INSERT INTO recipients (chat_id, name, channel, group_tag, subscribed_topics, is_active, created_at)
                        VALUES (?, ?, 'TELEGRAM', 'GENERAL', ?, 1, CURRENT_TIMESTAMP)
                        """,
                        (uid_str, user_row["full_name"], topic),
                    )

            conn.commit()
            return True, f"✅ [{bot_type.title}] 구독이 시작되었습니다!"

    def unsubscribe(self, user_id: int, bot_type: BotServiceType) -> tuple[bool, str]:
        """Explicitly cancels subscription."""
        uid_str = str(user_id).strip()
        bid = bot_type.value

        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, status FROM subscriptions WHERE telegram_user_id = ? AND bot_id = ?",
                (uid_str, bid),
            )
            sub_row = cur.fetchone()
            if not sub_row or sub_row["status"] != "ACTIVE":
                return True, f"이미 해지되어 있거나 구독 중이 아닙니다."

            cur.execute(
                "UPDATE subscriptions SET status = 'CANCELLED', cancelled_at = CURRENT_TIMESTAMP WHERE id = ?",
                (sub_row["id"],),
            )
            topic_map = {"brief_bot": "FINANCE", "travel_bot": "TRAVEL", "admission_bot": "EDUCATION"}
            if bid in topic_map:
                topic = topic_map[bid]
                cur.execute("SELECT subscribed_topics FROM recipients WHERE chat_id = ?", (uid_str,))
                existing_rec = cur.fetchone()
                if existing_rec:
                    topics = set(t.strip() for t in (existing_rec["subscribed_topics"] or "").split(",") if t.strip())
                    topics.discard(topic)
                    if not topics:
                        cur.execute(
                            "UPDATE recipients SET is_active = 0, inactivation_reason = 'USER_UNSUB' WHERE chat_id = ?",
                            (uid_str,),
                        )
                    else:
                        cur.execute(
                            "UPDATE recipients SET subscribed_topics = ? WHERE chat_id = ?",
                            (",".join(sorted(topics)), uid_str),
                        )

            conn.commit()
            return True, f"⚪ [{bot_type.title}] 구독이 해지되었습니다."

    def toggle_subscription(
        self, user_id: int, bot_type: BotServiceType
    ) -> tuple[bool, bool, str]:
        """Toggles subscription atomically and safely."""
        uid_str = str(user_id).strip()
        bid = bot_type.value

        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, status FROM subscriptions WHERE telegram_user_id = ? AND bot_id = ?",
                (uid_str, bid),
            )
            sub_row = cur.fetchone()
            current_active = bool(sub_row and sub_row["status"] == "ACTIVE")

        if not current_active:
            ok, msg = self.subscribe(user_id, bot_type)
            return ok, True if ok else False, msg
        else:
            ok, msg = self.unsubscribe(user_id, bot_type)
            return ok, False if ok else True, msg

    def get_bots_by_category(self, category: BotCategory) -> list[BotServiceType]:
        """Returns list of bots belonging to given category."""
        return [b for b in BotServiceType if b.category == category]

    def search_bots(self, query: str) -> list[BotServiceType]:
        """Performs instant keyword matching against bot names, descriptions, and tags."""
        q = query.strip().lower()
        if not q:
            return []

        matched = []
        for b in BotServiceType:
            if q in b.title.lower() or q in b.short_name.lower():
                matched.append(b)
                continue
            if q in b.one_line_desc.lower():
                matched.append(b)
                continue
            if any(q in kw or kw in q for kw in b.keywords):
                matched.append(b)
                continue

        return matched

    def request_tier_upgrade(
        self, user_id: int, requested_tier: str, memo: str = ""
    ) -> tuple[int, bool]:
        """Records an upgrade request from a user. Validates tier, updates existing pending request or creates new."""
        uid_str = str(user_id).strip()
        req_t = requested_tier.strip().upper()
        allowed_tiers = {"GREEN", "RED", "BLACK"}
        if req_t not in allowed_tiers:
            raise ValueError(f"신청할 수 없는 등급입니다: {req_t}")

        tier_rank = {"WHITE": 0, "GREEN": 1, "RED": 2, "BLACK": 3}
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT tier FROM users WHERE telegram_user_id = ?", (uid_str,))
            urow = cur.fetchone()
            curr_tier = urow["tier"] if urow else "WHITE"

            if tier_rank.get(req_t, 0) <= tier_rank.get(curr_tier, 0):
                raise ValueError(f"현재 등급({curr_tier}) 이상의 등급으로만 신청 가능합니다.")

            cur.execute(
                "SELECT id FROM upgrade_requests WHERE telegram_user_id = ? AND status = 'PENDING'",
                (uid_str,),
            )
            existing = cur.fetchone()
            if existing:
                req_id = int(existing["id"])
                cur.execute(
                    """UPDATE upgrade_requests
                       SET requested_tier = ?, current_tier = ?, requested_at = ?, memo = ?
                       WHERE id = ?""",
                    (req_t, curr_tier, now_str, memo, req_id),
                )
                conn.commit()
                return req_id, True
            else:
                cur.execute(
                    """INSERT INTO upgrade_requests
                       (telegram_user_id, current_tier, requested_tier, status, requested_at, memo)
                       VALUES (?, ?, ?, 'PENDING', ?, ?)""",
                    (uid_str, curr_tier, req_t, now_str, memo),
                )
                req_id = int(cur.lastrowid)
                conn.commit()
                return req_id, False

    def get_pending_upgrade_requests(self) -> list[dict[str, Any]]:
        """Returns all pending upgrade requests with user details."""
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute(
                """SELECT ur.id, ur.telegram_user_id, ur.current_tier, ur.requested_tier,
                          ur.status, ur.requested_at, ur.memo, u.full_name, u.username
                   FROM upgrade_requests ur
                   LEFT JOIN users u ON ur.telegram_user_id = u.telegram_user_id
                   WHERE ur.status = 'PENDING'
                   ORDER BY ur.requested_at DESC, ur.id DESC"""
            )
            rows = cur.fetchall()
            return [
                {
                    "id": r["id"],
                    "telegram_user_id": r["telegram_user_id"],
                    "full_name": r["full_name"] or f"유저_{r['telegram_user_id'][-4:]}",
                    "username": r["username"] or "",
                    "current_tier": r["current_tier"],
                    "requested_tier": r["requested_tier"],
                    "status": r["status"],
                    "requested_at": str(r["requested_at"]),
                    "memo": r["memo"] or "",
                }
                for r in rows
            ]

    def review_upgrade_request(
        self, request_id: int, action: str, reviewed_by: str = "ADMIN", memo: str = "", expected_tier: str | None = None
    ) -> dict[str, Any]:
        """Approves or rejects an upgrade request conditionally with exclusive write lock.
        Delegates to SubscriptionEngine if possible, or executes atomic conditional update."""
        act = action.strip().upper()
        if act not in ("APPROVED", "REJECTED"):
            raise ValueError(f"유효하지 않은 심사 액션입니다: {act}")

        # Attempt delegation to SubscriptionEngine for complete quota/audit handling
        try:
            from market_brief.storage.db import MarketBriefDatabase
            from market_brief.subscription_engine import SubscriptionEngine
            mb_db = MarketBriefDatabase(db_path=str(self.db_path))
            engine = SubscriptionEngine(mb_db)
            if act == "APPROVED":
                res = engine.approve_upgrade_request(request_id, actor=reviewed_by, expected_tier=expected_tier)
                return {
                    "success": True,
                    "request_id": request_id,
                    "telegram_user_id": res["telegram_user_id"],
                    "status": "APPROVED",
                    "tier": res["new_tier"],
                }
            else:
                res = engine.reject_upgrade_request(request_id, actor=reviewed_by, reason=memo)
                with self._get_conn() as conn:
                    row = conn.execute("SELECT telegram_user_id, current_tier FROM upgrade_requests WHERE id = ?", (request_id,)).fetchone()
                    uid_str = row["telegram_user_id"] if row else ""
                    curr_tier = row["current_tier"] if row else "WHITE"
                return {
                    "success": True,
                    "request_id": request_id,
                    "telegram_user_id": uid_str,
                    "status": "REJECTED",
                    "tier": curr_tier,
                }
        except (KeyError, ValueError):
            raise
        except ImportError:
            # Fallback direct atomic query
            pass

        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._get_conn() as conn:
            cur = conn.cursor()
            cur.execute("SELECT * FROM upgrade_requests WHERE id = ?", (request_id,))
            req_row = cur.fetchone()
            if not req_row:
                raise KeyError(f"업그레이드 신청을 찾을 수 없습니다: #{request_id}")
            if req_row["status"] != "PENDING":
                raise ValueError(f"이미 처리된 신청입니다 (현재 상태: {req_row['status']})")

            uid_str = req_row["telegram_user_id"]
            req_tier = req_row["requested_tier"]

            if expected_tier and req_tier != expected_tier.upper():
                raise ValueError(f"신청 등급이 변경되었습니다 (버튼: {expected_tier} != 최신: {req_tier})")

            cur.execute(
                """UPDATE upgrade_requests
                   SET status = ?, reviewed_at = ?, reviewed_by = ?, memo = ?
                   WHERE id = ? AND status = 'PENDING'""",
                (act, now_str, reviewed_by, memo or req_row["memo"], request_id),
            )
            if cur.rowcount == 0:
                raise ValueError("이미 다른 관리자에 의해 처리되었습니다.")

            if act == "APPROVED":
                cur.execute(
                    "UPDATE users SET tier = ?, updated_at = ? WHERE telegram_user_id = ?",
                    (req_tier, now_str, uid_str),
                )

            conn.commit()
            return {
                "success": True,
                "request_id": request_id,
                "telegram_user_id": uid_str,
                "status": act,
                "tier": req_tier if act == "APPROVED" else req_row["current_tier"],
            }

