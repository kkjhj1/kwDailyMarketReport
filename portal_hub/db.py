"""Direct Database Integration with alerts.sqlite3.
Synchronizes user profiles, membership tiers, and subscriptions directly with Admin Console.
Supports categorized bot discovery, instant keyword searching, and explicit subscribe/unsubscribe operations.
"""
from __future__ import annotations
import sqlite3
from pathlib import Path
from typing import Optional

from portal_hub.models import BotCategory, BotServiceType, UserProfile, UserTier

DEFAULT_DB_PATH = Path(__file__).resolve().parent.parent / "alerts.sqlite3"


class PortalDatabase:
    """Thread-safe SQLite database adapter for alerts.sqlite3."""

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH):
        self.db_path = Path(db_path)

    def _get_conn(self) -> sqlite3.Connection:
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
