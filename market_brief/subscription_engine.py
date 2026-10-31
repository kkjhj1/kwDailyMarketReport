"""Atomic Multi-Bot Subscription & Tier Quota Enforcement Engine (v2.0).

Key Guarantees:
1. Serializable / Immediate SQLite locking (`BEGIN IMMEDIATE`) prevents race-condition
   double subscriptions when a WHITE (max 1) user triggers `/start` on two bots simultaneously.
2. Tier Quotas:
   - WHITE (Free): max 1 INFO bot
   - GREEN: max 3 INFO bots
   - RED: max 5 INFO bots
   - BLACK: Unlimited (all INFO bots)
   - MENU bot is a hub/directory portal and never consumes user bot quota.
3. Automatic Tier Expiry Enforcement (`tier_expires_at`) with safe Grace Period handling.
4. Atomic One-Click Bot Swap (`atomic_swap_subscription`) for zero-friction switching.
5. Zero-Breaking Legacy Sync (`LegacyRecipientAdapter`) keeping `recipients` synchronized with `brief_bot`.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime
import json
import sqlite3
from typing import Any
from zoneinfo import ZoneInfo

from market_brief.storage.db import MarketBriefDatabase
from market_brief.storage.models import (
    BotRecord,
    BotType,
    PlatformUser,
    Recipient,
    SubscriptionStatus,
    TIER_DEFAULT_SPECS,
    TierPolicy,
    UserTier,
)

KST = ZoneInfo("Asia/Seoul")


def now_kst_str() -> str:
    return datetime.now(KST).strftime("%Y-%m-%d %H:%M:%S")


def normalize_expiry_str(expiry: str | None) -> str | None:
    if not expiry or not str(expiry).strip():
        return None
    val = str(expiry).strip().replace("T", " ")
    if len(val) == 10:  # YYYY-MM-DD -> end of day KST
        return f"{val} 23:59:59"
    if len(val) == 16:  # YYYY-MM-DD HH:MM
        return f"{val}:59"
    return val[:19]


class SubscriptionEngine:
    """Central business logic engine for Multi-Bot Subscriptions, Quotas, Swaps, and Tier Management."""

    def __init__(self, db: MarketBriefDatabase):
        self.db = db
        self.db_path = db.db_path

    @contextmanager
    def _exclusive_tx(self):
        """Acquires an immediate write lock (`BEGIN IMMEDIATE`) to prevent race conditions."""
        conn = sqlite3.connect(self.db_path, timeout=15.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    @contextmanager
    def _read_conn(self):
        conn = sqlite3.connect(self.db_path, timeout=15.0)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    # =========================================================================
    # Internal Helpers (run inside active transaction)
    # =========================================================================

    def _get_tier_policy_in_tx(self, conn: sqlite3.Connection, tier_code: str) -> TierPolicy:
        code = (tier_code or "WHITE").upper()
        row = conn.execute("SELECT * FROM tier_policies WHERE tier_code = ?", (code,)).fetchone()
        if row:
            return TierPolicy(
                tier_code=row["tier_code"],
                name=row["name"],
                max_bots=int(row["max_bots"]),
                badge_color=row["badge_color"],
                description=row["description"] or "",
                sort_order=int(row["sort_order"]),
            )
        spec = TIER_DEFAULT_SPECS.get(code, TIER_DEFAULT_SPECS["WHITE"])
        return TierPolicy(
            tier_code=spec["tier_code"],
            name=spec["name"],
            max_bots=spec["max_bots"],
            badge_color=spec["badge_color"],
            description=spec["description"],
            sort_order=spec["sort_order"],
        )

    def _sync_legacy_brief_recipient_in_tx(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        full_name: str,
        is_brief_active: bool = True,
        reason: str = "PLATFORM_SYNC",
    ) -> None:
        """Keeps legacy `recipients` table 100% synchronized with all active INFO bot topics (FINANCE, TRAVEL, etc.)."""
        uid = str(user_id).strip()
        topic_rows = conn.execute(
            """SELECT DISTINCT b.topic_code
               FROM subscriptions s
               JOIN bots b ON s.bot_id = b.bot_id
               WHERE s.telegram_user_id = ? AND s.status = 'ACTIVE' AND b.bot_type = 'INFO'""",
            (uid,),
        ).fetchall()
        active_topics = sorted({(r["topic_code"] or "GENERAL").strip().upper() for r in topic_rows if r["topic_code"]})
        existing = conn.execute("SELECT * FROM recipients WHERE chat_id = ?", (uid,)).fetchone()

        if active_topics:
            topics_csv = ",".join(active_topics)
            if existing:
                conn.execute(
                    """UPDATE recipients
                       SET name = ?, subscribed_topics = ?, is_active = 1, inactivation_reason = NULL
                       WHERE chat_id = ?""",
                    (full_name or existing["name"], topics_csv, uid),
                )
            else:
                conn.execute(
                    """INSERT INTO recipients
                       (chat_id, name, channel, group_tag, subscribed_topics, is_active, inactivation_reason)
                       VALUES (?, ?, 'TELEGRAM', 'GENERAL', ?, 1, NULL)""",
                    (uid, full_name or f"수신자_{uid[-4:]}", topics_csv),
                )
        else:
            if existing and existing["is_active"]:
                conn.execute(
                    "UPDATE recipients SET is_active = 0, inactivation_reason = ? WHERE chat_id = ?",
                    (reason, uid),
                )

    def _log_audit_in_tx(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        bot_id: str | None,
        action: str,
        actor: str = "USER",
        detail: dict[str, Any] | str | None = None,
    ) -> None:
        detail_str = (
            json.dumps(detail, ensure_ascii=False)
            if isinstance(detail, dict)
            else (str(detail) if detail is not None else None)
        )
        conn.execute(
            """INSERT INTO subscription_audit_logs (telegram_user_id, bot_id, action, actor, detail, logged_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (str(user_id), bot_id, action, actor, detail_str, now_kst_str()),
        )

    def _enforce_user_expiry_in_tx(self, conn: sqlite3.Connection, user_row: sqlite3.Row) -> sqlite3.Row:
        """Checks if user's paid tier has expired; if so, rolls back to WHITE with Grace Period protection."""
        exp = user_row["tier_expires_at"]
        tier = (user_row["tier"] or "WHITE").upper()
        if not exp or tier == UserTier.WHITE.value:
            return user_row

        now_str = now_kst_str()
        norm_exp = normalize_expiry_str(exp)
        if not norm_exp or norm_exp >= now_str:
            return user_row

        uid = user_row["telegram_user_id"]
        # Roll back tier to WHITE (max 1 bot)
        active_info_subs = conn.execute(
            """SELECT s.id, s.bot_id
               FROM subscriptions s
               JOIN bots b ON s.bot_id = b.bot_id
               WHERE s.telegram_user_id = ? AND s.status = 'ACTIVE' AND b.bot_type = 'INFO'
               ORDER BY s.subscribed_at DESC, s.id DESC""",
            (uid,),
        ).fetchall()

        new_status = user_row["status"] if user_row["status"] == "BLOCKED" else "ACTIVE"
        suspended_bots: list[str] = []
        if len(active_info_subs) > 1:
            new_status = "GRACE_PERIOD"
            # Keep the most recent 1 bot active, suspend excess bots until user chooses
            for extra in active_info_subs[1:]:
                conn.execute(
                    "UPDATE subscriptions SET status = 'SUSPENDED_OVER_QUOTA' WHERE id = ?",
                    (extra["id"],),
                )
                suspended_bots.append(extra["bot_id"])
                if extra["bot_id"] == "brief_bot":
                    self._sync_legacy_brief_recipient_in_tx(
                        conn, uid, user_row["full_name"], False, reason="TIER_EXPIRED_OVER_QUOTA"
                    )

        conn.execute(
            """UPDATE users
               SET tier = 'WHITE', tier_expires_at = NULL, status = ?, updated_at = ?
               WHERE telegram_user_id = ?""",
            (new_status, now_str, uid),
        )
        self._log_audit_in_tx(
            conn,
            uid,
            None,
            "TIER_EXPIRED",
            actor="SYSTEM",
            detail={"previous_tier": tier, "expired_at": norm_exp, "suspended_bots": suspended_bots},
        )
        return conn.execute("SELECT * FROM users WHERE telegram_user_id = ?", (uid,)).fetchone()

    def _get_active_info_subs_in_tx(self, conn: sqlite3.Connection, user_id: str) -> list[dict[str, Any]]:
        rows = conn.execute(
            """SELECT s.id, s.bot_id, s.status, s.subscribed_at,
                      b.name AS bot_name, b.icon_emoji, b.category, b.telegram_username, b.is_active AS bot_is_active
               FROM subscriptions s
               JOIN bots b ON s.bot_id = b.bot_id
               WHERE s.telegram_user_id = ? AND s.status = 'ACTIVE' AND b.bot_type = 'INFO'
               ORDER BY b.sort_order ASC, s.subscribed_at ASC""",
            (str(user_id),),
        ).fetchall()
        return [dict(r) for r in rows]

    # =========================================================================
    # Tier Policies & Expiry Sweep
    # =========================================================================

    def get_tiers(self) -> list[dict[str, Any]]:
        with self._read_conn() as conn:
            rows = conn.execute("SELECT * FROM tier_policies ORDER BY sort_order ASC").fetchall()
            return [
                TierPolicy(
                    tier_code=r["tier_code"],
                    name=r["name"],
                    max_bots=int(r["max_bots"]),
                    badge_color=r["badge_color"],
                    description=r["description"] or "",
                    sort_order=int(r["sort_order"]),
                ).to_dict()
                for r in rows
            ]

    def run_expiry_sweep(self) -> int:
        """Sweeps all users whose `tier_expires_at` is in the past and downgrades them safely."""
        now_str = now_kst_str()
        expired_count = 0
        with self._exclusive_tx() as conn:
            candidates = conn.execute(
                "SELECT * FROM users WHERE tier != 'WHITE' AND tier_expires_at IS NOT NULL"
            ).fetchall()
            for u in candidates:
                norm_exp = normalize_expiry_str(u["tier_expires_at"])
                if norm_exp and norm_exp < now_str:
                    self._enforce_user_expiry_in_tx(conn, u)
                    expired_count += 1
        return expired_count

    # =========================================================================
    # Bot Master Management (MENU + INFO Bots)
    # =========================================================================

    def list_bots(self, include_raw_token: bool = False) -> list[dict[str, Any]]:
        with self._read_conn() as conn:
            rows = conn.execute(
                """SELECT b.*,
                          (SELECT COUNT(*) FROM subscriptions s
                           JOIN users u ON s.telegram_user_id = u.telegram_user_id
                           WHERE s.bot_id = b.bot_id AND s.status = 'ACTIVE' AND u.status != 'BLOCKED') AS active_subscribers
                   FROM bots b
                   ORDER BY CASE WHEN b.bot_type = 'MENU' THEN 0 ELSE 1 END ASC, b.sort_order ASC, b.created_at ASC"""
            ).fetchall()
            results = []
            for r in rows:
                rec = BotRecord(
                    bot_id=r["bot_id"],
                    bot_type=r["bot_type"],
                    name=r["name"],
                    telegram_username=r["telegram_username"] or "",
                    token=r["token"] or "",
                    category=r["category"] or "일반",
                    icon_emoji=r["icon_emoji"] or "🤖",
                    description=r["description"] or "",
                    topic_code=r["topic_code"] or "GENERAL",
                    is_active=bool(r["is_active"]),
                    sort_order=int(r["sort_order"]),
                    last_offset=int(r["last_offset"] or 0),
                    created_at=r["created_at"],
                    updated_at=r["updated_at"],
                )
                d = rec.to_dict(include_raw_token=include_raw_token)
                d["active_subscribers"] = int(r["active_subscribers"] or 0)
                results.append(d)
            return results

    def get_bot(self, bot_id: str) -> BotRecord | None:
        with self._read_conn() as conn:
            r = conn.execute("SELECT * FROM bots WHERE bot_id = ?", (str(bot_id).strip(),)).fetchone()
            if not r:
                return None
            return BotRecord(
                bot_id=r["bot_id"],
                bot_type=r["bot_type"],
                name=r["name"],
                telegram_username=r["telegram_username"] or "",
                token=r["token"] or "",
                category=r["category"] or "일반",
                icon_emoji=r["icon_emoji"] or "🤖",
                description=r["description"] or "",
                topic_code=r["topic_code"] or "GENERAL",
                is_active=bool(r["is_active"]),
                sort_order=int(r["sort_order"]),
                last_offset=int(r["last_offset"] or 0),
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )

    def get_menu_bot(self) -> BotRecord | None:
        with self._read_conn() as conn:
            r = conn.execute(
                "SELECT * FROM bots WHERE bot_type = 'MENU' AND is_active = 1 ORDER BY sort_order ASC LIMIT 1"
            ).fetchone()
            if not r:
                return None
            return BotRecord(
                bot_id=r["bot_id"],
                bot_type=r["bot_type"],
                name=r["name"],
                telegram_username=r["telegram_username"] or "",
                token=r["token"] or "",
                category=r["category"] or "포털",
                icon_emoji=r["icon_emoji"] or "🧭",
                description=r["description"] or "",
                topic_code=r["topic_code"] or "PORTAL",
                is_active=bool(r["is_active"]),
                sort_order=int(r["sort_order"]),
                last_offset=int(r["last_offset"] or 0),
                created_at=r["created_at"],
                updated_at=r["updated_at"],
            )

    def upsert_bot(
        self,
        bot_id: str,
        name: str,
        bot_type: str = "INFO",
        telegram_username: str = "",
        token: str | None = None,
        clear_token: bool = False,
        category: str = "일반",
        icon_emoji: str = "🤖",
        description: str = "",
        topic_code: str = "GENERAL",
        is_active: bool = True,
        sort_order: int = 10,
    ) -> dict[str, Any]:
        clean_id = str(bot_id).strip().lower().replace(" ", "_")
        if not clean_id:
            raise ValueError("bot_id는 필수입니다.")
        btype = bot_type.upper() if bot_type.upper() in {BotType.MENU.value, BotType.INFO.value} else BotType.INFO.value
        uname = (telegram_username or "").strip().lstrip("@")
        now_str = now_kst_str()

        with self._exclusive_tx() as conn:
            existing = conn.execute("SELECT * FROM bots WHERE bot_id = ?", (clean_id,)).fetchone()
            if clear_token:
                final_token = ""
            elif token is not None and token.strip() and token.strip() != "__KEEP__":
                final_token = token.strip()
            elif existing:
                final_token = existing["token"] or ""
            else:
                final_token = ""

            if existing:
                conn.execute(
                    """UPDATE bots
                       SET bot_type = ?, name = ?, telegram_username = ?, token = ?,
                           category = ?, icon_emoji = ?, description = ?, topic_code = ?,
                           is_active = ?, sort_order = ?, updated_at = ?
                       WHERE bot_id = ?""",
                    (
                        btype,
                        name.strip(),
                        uname,
                        final_token,
                        category.strip() or "일반",
                        icon_emoji.strip() or "🤖",
                        description.strip(),
                        topic_code.strip().upper() or "GENERAL",
                        1 if is_active else 0,
                        int(sort_order),
                        now_str,
                        clean_id,
                    ),
                )
            else:
                conn.execute(
                    """INSERT INTO bots
                       (bot_id, bot_type, name, telegram_username, token, category, icon_emoji,
                        description, topic_code, is_active, sort_order, last_offset, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?)""",
                    (
                        clean_id,
                        btype,
                        name.strip(),
                        uname,
                        final_token,
                        category.strip() or "일반",
                        icon_emoji.strip() or "🤖",
                        description.strip(),
                        topic_code.strip().upper() or "GENERAL",
                        1 if is_active else 0,
                        int(sort_order),
                        now_str,
                        now_str,
                    ),
                )
        updated = self.get_bot(clean_id)
        return updated.to_dict() if updated else {"bot_id": clean_id}

    def toggle_bot(self, bot_id: str) -> dict[str, Any]:
        clean_id = str(bot_id).strip()
        with self._exclusive_tx() as conn:
            row = conn.execute("SELECT * FROM bots WHERE bot_id = ?", (clean_id,)).fetchone()
            if not row:
                raise KeyError(f"봇을 찾을 수 없습니다: {clean_id}")
            new_active = 0 if row["is_active"] else 1
            conn.execute(
                "UPDATE bots SET is_active = ?, updated_at = ? WHERE bot_id = ?",
                (new_active, now_kst_str(), clean_id),
            )
        return {"bot_id": clean_id, "is_active": bool(new_active)}

    def delete_bot(self, bot_id: str) -> bool:
        clean_id = str(bot_id).strip()
        if clean_id == "brief_bot":
            raise ValueError("기본 시황 브리핑봇(brief_bot)은 하위 호환성을 위해 삭제할 수 없습니다. 비활성화를 사용하세요.")
        with self._exclusive_tx() as conn:
            conn.execute("DELETE FROM subscriptions WHERE bot_id = ?", (clean_id,))
            cur = conn.execute("DELETE FROM bots WHERE bot_id = ?", (clean_id,))
            return cur.rowcount > 0

    def update_bot_offset(self, bot_id: str, last_offset: int) -> None:
        with self._exclusive_tx() as conn:
            conn.execute(
                "UPDATE bots SET last_offset = ?, updated_at = ? WHERE bot_id = ?",
                (int(last_offset), now_kst_str(), str(bot_id).strip()),
            )

    # =========================================================================
    # User & Tier Management (Admin Requirement #5)
    # =========================================================================

    def list_users_with_subscriptions(
        self,
        q: str = "",
        tier_filter: str = "",
        status_filter: str = "",
    ) -> list[dict[str, Any]]:
        self.run_expiry_sweep()
        with self._read_conn() as conn:
            tiers_map = {
                r["tier_code"]: TierPolicy(
                    tier_code=r["tier_code"],
                    name=r["name"],
                    max_bots=int(r["max_bots"]),
                    badge_color=r["badge_color"],
                    description=r["description"] or "",
                    sort_order=int(r["sort_order"]),
                )
                for r in conn.execute("SELECT * FROM tier_policies").fetchall()
            }

            sql = "SELECT * FROM users WHERE 1=1"
            params: list[Any] = []
            if q and q.strip():
                kw = f"%{q.strip()}%"
                sql += " AND (telegram_user_id LIKE ? OR full_name LIKE ? OR username LIKE ? OR memo LIKE ?)"
                params.extend([kw, kw, kw, kw])
            if tier_filter and tier_filter.strip():
                sql += " AND tier = ?"
                params.append(tier_filter.strip().upper())
            if status_filter and status_filter.strip():
                sql += " AND status = ?"
                params.append(status_filter.strip().upper())
            sql += " ORDER BY created_at DESC, telegram_user_id ASC"

            user_rows = conn.execute(sql, tuple(params)).fetchall()

            # Load all INFO bot subscriptions in one query (avoid N+1)
            sub_rows = conn.execute(
                """SELECT s.*, b.name AS bot_name, b.icon_emoji, b.category, b.bot_type, b.sort_order
                   FROM subscriptions s
                   JOIN bots b ON s.bot_id = b.bot_id
                   WHERE b.bot_type = 'INFO'
                   ORDER BY b.sort_order ASC, s.subscribed_at ASC"""
            ).fetchall()

            subs_by_user: dict[str, list[dict[str, Any]]] = {}
            for s in sub_rows:
                subs_by_user.setdefault(s["telegram_user_id"], []).append(dict(s))

            results = []
            for u in user_rows:
                uid = u["telegram_user_id"]
                tier_code = (u["tier"] or "WHITE").upper()
                policy = tiers_map.get(tier_code) or TierPolicy("WHITE", "화이트 (무료)", 1)
                u_subs = subs_by_user.get(uid, [])
                active_bots = [
                    {
                        "bot_id": s["bot_id"],
                        "bot_name": s["bot_name"],
                        "icon_emoji": s["icon_emoji"] or "🤖",
                        "category": s["category"],
                        "subscribed_at": s["subscribed_at"],
                    }
                    for s in u_subs
                    if s["status"] == SubscriptionStatus.ACTIVE.value
                ]
                suspended_bots = [
                    {
                        "bot_id": s["bot_id"],
                        "bot_name": s["bot_name"],
                        "icon_emoji": s["icon_emoji"] or "🤖",
                    }
                    for s in u_subs
                    if s["status"] == SubscriptionStatus.SUSPENDED_OVER_QUOTA.value
                ]
                used_slots = len(active_bots)
                is_over = (not policy.is_unlimited) and (used_slots > policy.max_bots)
                remaining = None if policy.is_unlimited else max(0, policy.max_bots - used_slots)

                user_obj = PlatformUser(
                    telegram_user_id=uid,
                    full_name=u["full_name"],
                    username=u["username"] or "",
                    tier=tier_code,
                    tier_expires_at=u["tier_expires_at"],
                    status=u["status"] or "ACTIVE",
                    memo=u["memo"] or "",
                    created_at=u["created_at"],
                    updated_at=u["updated_at"],
                ).to_dict()
                user_obj.update(
                    {
                        "tier_name": policy.name,
                        "tier_badge_color": policy.badge_color,
                        "is_unlimited": policy.is_unlimited,
                        "max_slots": None if policy.is_unlimited else policy.max_bots,
                        "used_slots": used_slots,
                        "remaining_slots": remaining,
                        "is_over_quota": is_over or len(suspended_bots) > 0,
                        "active_subscriptions": active_bots,
                        "suspended_subscriptions": suspended_bots,
                    }
                )
                results.append(user_obj)
            return results

    def get_user_profile(self, telegram_user_id: str) -> dict[str, Any] | None:
        uid = str(telegram_user_id).strip()
        items = self.list_users_with_subscriptions()
        for item in items:
            if item["telegram_user_id"] == uid:
                return item
        return None

    def upsert_user(
        self,
        telegram_user_id: str,
        full_name: str,
        username: str = "",
        tier: str = "WHITE",
        tier_expires_at: str | None = None,
        status: str = "ACTIVE",
        memo: str = "",
        actor: str = "ADMIN",
    ) -> dict[str, Any]:
        uid = str(telegram_user_id).strip()
        if not uid:
            raise ValueError("텔레그램 유저 ID는 필수입니다.")
        tier_code = tier.strip().upper()
        if tier_code not in TIER_DEFAULT_SPECS:
            raise ValueError(f"유효하지 않은 등급입니다: {tier_code}")
        norm_exp = normalize_expiry_str(tier_expires_at)
        now_str = now_kst_str()
        clean_uname = (username or "").strip().lstrip("@")
        clean_name = (full_name or "").strip() or f"유저_{uid[-4:]}"

        with self._exclusive_tx() as conn:
            existing = conn.execute("SELECT * FROM users WHERE telegram_user_id = ?", (uid,)).fetchone()
            if existing:
                conn.execute(
                    """UPDATE users
                       SET full_name = ?, username = ?, tier = ?, tier_expires_at = ?,
                           status = ?, memo = ?, updated_at = ?
                       WHERE telegram_user_id = ?""",
                    (clean_name, clean_uname, tier_code, norm_exp, status, memo or "", now_str, uid),
                )
            else:
                conn.execute(
                    """INSERT INTO users
                       (telegram_user_id, username, full_name, tier, tier_expires_at, status, memo, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (uid, clean_uname, clean_name, tier_code, norm_exp, status, memo or "", now_str, now_str),
                )
                self._log_audit_in_tx(
                    conn, uid, None, "USER_CREATED", actor=actor, detail={"tier": tier_code, "name": clean_name}
                )
        return self.get_user_profile(uid) or {"telegram_user_id": uid}

    def update_user_details(
        self,
        telegram_user_id: str,
        full_name: str | None = None,
        username: str | None = None,
        status: str | None = None,
        memo: str | None = None,
    ) -> dict[str, Any]:
        uid = str(telegram_user_id).strip()
        with self._exclusive_tx() as conn:
            existing = conn.execute("SELECT * FROM users WHERE telegram_user_id = ?", (uid,)).fetchone()
            if not existing:
                raise KeyError(f"유저를 찾을 수 없습니다: {uid}")
            new_name = full_name.strip() if full_name is not None and full_name.strip() else existing["full_name"]
            new_uname = username.strip().lstrip("@") if username is not None else (existing["username"] or "")
            new_status = status.strip().upper() if status is not None else (existing["status"] or "ACTIVE")
            new_memo = memo if memo is not None else (existing["memo"] or "")
            conn.execute(
                """UPDATE users
                   SET full_name = ?, username = ?, status = ?, memo = ?, updated_at = ?
                   WHERE telegram_user_id = ?""",
                (new_name, new_uname, new_status, new_memo, now_kst_str(), uid),
            )
            if new_name != existing["full_name"]:
                conn.execute("UPDATE recipients SET name = ? WHERE chat_id = ?", (new_name, uid))
        return self.get_user_profile(uid) or {"telegram_user_id": uid}

    def update_user_tier(
        self,
        telegram_user_id: str,
        new_tier: str,
        tier_expires_at: str | None = None,
        downgrade_mode: str = "GRACE",  # 'GRACE' | 'KEEP_RECENT' | 'KEEP_SELECTED' | 'CANCEL_ALL'
        keep_bot_ids: list[str] | None = None,
        memo: str | None = None,
        actor: str = "ADMIN",
    ) -> dict[str, Any]:
        """Updates a user's tier and safely reconciles active subscriptions against the new quota."""
        uid = str(telegram_user_id).strip()
        target_tier = new_tier.strip().upper()
        if target_tier not in TIER_DEFAULT_SPECS:
            raise ValueError(f"유효하지 않은 등급입니다: {target_tier}")
        norm_exp = normalize_expiry_str(tier_expires_at) if target_tier != UserTier.WHITE.value else None
        now_str = now_kst_str()

        with self._exclusive_tx() as conn:
            user = conn.execute("SELECT * FROM users WHERE telegram_user_id = ?", (uid,)).fetchone()
            if not user:
                raise KeyError(f"유저를 찾을 수 없습니다: {uid}")

            old_tier = user["tier"]
            policy = self._get_tier_policy_in_tx(conn, target_tier)
            max_allowed = policy.max_bots

            active_subs = conn.execute(
                """SELECT s.id, s.bot_id, s.status
                   FROM subscriptions s
                   JOIN bots b ON s.bot_id = b.bot_id
                   WHERE s.telegram_user_id = ? AND s.status = 'ACTIVE' AND b.bot_type = 'INFO'
                   ORDER BY s.subscribed_at DESC, s.id DESC""",
                (uid,),
            ).fetchall()

            suspended_subs = conn.execute(
                """SELECT s.id, s.bot_id
                   FROM subscriptions s
                   JOIN bots b ON s.bot_id = b.bot_id
                   WHERE s.telegram_user_id = ? AND s.status = 'SUSPENDED_OVER_QUOTA' AND b.bot_type = 'INFO'
                   ORDER BY s.subscribed_at DESC, s.id DESC""",
                (uid,),
            ).fetchall()

            new_user_status = "BLOCKED" if user["status"] == "BLOCKED" else "ACTIVE"
            affected_bots: list[str] = []

            if len(active_subs) > max_allowed:
                # Downgrade over-quota reconciliation
                mode = (downgrade_mode or "GRACE").upper()
                if mode == "KEEP_SELECTED" and keep_bot_ids is not None:
                    allowed_keep = set(keep_bot_ids[:max_allowed])
                    for s in active_subs:
                        if s["bot_id"] not in allowed_keep:
                            conn.execute(
                                "UPDATE subscriptions SET status = 'CANCELLED', cancelled_at = ? WHERE id = ?",
                                (now_str, s["id"]),
                            )
                            affected_bots.append(s["bot_id"])
                            if s["bot_id"] == "brief_bot":
                                self._sync_legacy_brief_recipient_in_tx(
                                    conn, uid, user["full_name"], False, reason="TIER_DOWNGRADE"
                                )
                    for s in suspended_subs:
                        conn.execute(
                            "UPDATE subscriptions SET status = 'CANCELLED', cancelled_at = ? WHERE id = ?",
                            (now_str, s["id"]),
                        )
                elif mode == "KEEP_RECENT":
                    for s in active_subs[max_allowed:]:
                        conn.execute(
                            "UPDATE subscriptions SET status = 'CANCELLED', cancelled_at = ? WHERE id = ?",
                            (now_str, s["id"]),
                        )
                        affected_bots.append(s["bot_id"])
                        if s["bot_id"] == "brief_bot":
                            self._sync_legacy_brief_recipient_in_tx(
                                conn, uid, user["full_name"], False, reason="TIER_DOWNGRADE"
                            )
                    for s in suspended_subs:
                        conn.execute(
                            "UPDATE subscriptions SET status = 'CANCELLED', cancelled_at = ? WHERE id = ?",
                            (now_str, s["id"]),
                        )
                elif mode == "CANCEL_ALL":
                    for s in active_subs:
                        conn.execute(
                            "UPDATE subscriptions SET status = 'CANCELLED', cancelled_at = ? WHERE id = ?",
                            (now_str, s["id"]),
                        )
                        affected_bots.append(s["bot_id"])
                        if s["bot_id"] == "brief_bot":
                            self._sync_legacy_brief_recipient_in_tx(
                                conn, uid, user["full_name"], False, reason="TIER_DOWNGRADE"
                            )
                else:
                    # GRACE mode: keep most recent `max_allowed` active, suspend excess safely
                    new_user_status = "GRACE_PERIOD" if new_user_status != "BLOCKED" else "BLOCKED"
                    for s in active_subs[max_allowed:]:
                        conn.execute(
                            "UPDATE subscriptions SET status = 'SUSPENDED_OVER_QUOTA' WHERE id = ?",
                            (s["id"],),
                        )
                        affected_bots.append(s["bot_id"])
                        if s["bot_id"] == "brief_bot":
                            self._sync_legacy_brief_recipient_in_tx(
                                conn, uid, user["full_name"], False, reason="TIER_DOWNGRADE_GRACE"
                            )
            else:
                # Upgrade or within quota: restore any previously suspended bots up to new limit
                slots_left = max_allowed - len(active_subs)
                for s in suspended_subs[:slots_left]:
                    conn.execute(
                        "UPDATE subscriptions SET status = 'ACTIVE', cancelled_at = NULL WHERE id = ?",
                        (s["id"],),
                    )
                    affected_bots.append(s["bot_id"])
                    if s["bot_id"] == "brief_bot":
                        self._sync_legacy_brief_recipient_in_tx(conn, uid, user["full_name"], True)
                remaining_suspended = len(suspended_subs) - min(len(suspended_subs), slots_left)
                if remaining_suspended > 0 and new_user_status != "BLOCKED":
                    new_user_status = "GRACE_PERIOD"

            final_memo = memo if memo is not None else user["memo"]
            conn.execute(
                """UPDATE users
                   SET tier = ?, tier_expires_at = ?, status = ?, memo = ?, updated_at = ?
                   WHERE telegram_user_id = ?""",
                (target_tier, norm_exp, new_user_status, final_memo or "", now_str, uid),
            )
            self._log_audit_in_tx(
                conn,
                uid,
                None,
                "TIER_CHANGE",
                actor=actor,
                detail={
                    "old_tier": old_tier,
                    "new_tier": target_tier,
                    "expires_at": norm_exp,
                    "downgrade_mode": downgrade_mode,
                    "affected_bots": affected_bots,
                },
            )

        return self.get_user_profile(uid) or {"telegram_user_id": uid}

    def delete_user(self, telegram_user_id: str) -> bool:
        uid = str(telegram_user_id).strip()
        with self._exclusive_tx() as conn:
            conn.execute("DELETE FROM subscriptions WHERE telegram_user_id = ?", (uid,))
            conn.execute("DELETE FROM recipients WHERE chat_id = ?", (uid,))
            cur = conn.execute("DELETE FROM users WHERE telegram_user_id = ?", (uid,))
            if cur.rowcount > 0:
                self._log_audit_in_tx(conn, uid, None, "USER_DELETED", actor="ADMIN")
            return cur.rowcount > 0

    # =========================================================================
    # Atomic Subscription, Quota Verification & One-Click Swap (#2, #3, #4)
    # =========================================================================

    def ensure_user(self, telegram_user_id: str, full_name: str = "", username: str = "") -> dict[str, Any]:
        """Registers a new Telegram visitor as WHITE tier if not found, or updates profile name."""
        uid = str(telegram_user_id).strip()
        now_str = now_kst_str()
        clean_uname = (username or "").strip().lstrip("@")
        with self._exclusive_tx() as conn:
            user = conn.execute("SELECT * FROM users WHERE telegram_user_id = ?", (uid,)).fetchone()
            if not user:
                name_to_use = (full_name or "").strip() or (f"@{clean_uname}" if clean_uname else f"유저_{uid[-4:]}")
                conn.execute(
                    """INSERT INTO users
                       (telegram_user_id, username, full_name, tier, status, created_at, updated_at)
                       VALUES (?, ?, ?, 'WHITE', 'ACTIVE', ?, ?)""",
                    (uid, clean_uname, name_to_use, now_str, now_str),
                )
                self._log_audit_in_tx(conn, uid, None, "USER_JOINED", actor="USER", detail={"name": name_to_use})
                user = conn.execute("SELECT * FROM users WHERE telegram_user_id = ?", (uid,)).fetchone()
            else:
                user = self._enforce_user_expiry_in_tx(conn, user)
                if clean_uname and clean_uname != (user["username"] or ""):
                    conn.execute(
                        "UPDATE users SET username = ?, updated_at = ? WHERE telegram_user_id = ?",
                        (clean_uname, now_str, uid),
                    )
        return self.get_user_profile(uid) or {"telegram_user_id": uid}

    def attempt_subscribe(
        self,
        telegram_user_id: str,
        bot_id: str,
        full_name: str = "",
        username: str = "",
        actor: str = "USER",
        bypass_quota: bool = False,
    ) -> dict[str, Any]:
        """Atomically verifies user tier quota and subscribes user to an INFO bot if allowed."""
        uid = str(telegram_user_id).strip()
        bid = str(bot_id).strip()
        now_str = now_kst_str()
        clean_uname = (username or "").strip().lstrip("@")

        with self._exclusive_tx() as conn:
            # 1. Ensure user exists & enforce tier expiry
            user = conn.execute("SELECT * FROM users WHERE telegram_user_id = ?", (uid,)).fetchone()
            if not user:
                name_to_use = (full_name or "").strip() or (f"@{clean_uname}" if clean_uname else f"유저_{uid[-4:]}")
                conn.execute(
                    """INSERT INTO users
                       (telegram_user_id, username, full_name, tier, status, created_at, updated_at)
                       VALUES (?, ?, ?, 'WHITE', 'ACTIVE', ?, ?)""",
                    (uid, clean_uname, name_to_use, now_str, now_str),
                )
                self._log_audit_in_tx(conn, uid, bid, "USER_JOINED", actor=actor, detail={"via_bot": bid})
                user = conn.execute("SELECT * FROM users WHERE telegram_user_id = ?", (uid,)).fetchone()
            else:
                user = self._enforce_user_expiry_in_tx(conn, user)
                if clean_uname and clean_uname != (user["username"] or ""):
                    conn.execute("UPDATE users SET username = ? WHERE telegram_user_id = ?", (clean_uname, uid))

            if user["status"] == "BLOCKED":
                return {
                    "success": False,
                    "code": "USER_BLOCKED",
                    "message": "이용이 제한된 계정입니다. 관리자에게 문의해 주세요.",
                }

            # 2. Validate Target Bot
            bot = conn.execute("SELECT * FROM bots WHERE bot_id = ?", (bid,)).fetchone()
            if not bot:
                return {"success": False, "code": "BOT_NOT_FOUND", "message": f"존재하지 않는 봇입니다 ({bid})."}
            if not bot["is_active"] and not bypass_quota:
                return {
                    "success": False,
                    "code": "BOT_INACTIVE",
                    "message": f"현재 [{bot['icon_emoji']} {bot['name']}] 서비스는 점검/비활성 상태입니다.",
                }

            policy = self._get_tier_policy_in_tx(conn, user["tier"])
            menu_bot_row = conn.execute(
                "SELECT * FROM bots WHERE bot_type = 'MENU' AND is_active = 1 ORDER BY sort_order ASC LIMIT 1"
            ).fetchone()
            menu_bot_info = dict(menu_bot_row) if menu_bot_row else None

            if bot["bot_type"] == BotType.MENU.value:
                active_subs = self._get_active_info_subs_in_tx(conn, uid)
                return {
                    "success": True,
                    "code": "MENU_PORTAL",
                    "message": "통합 메뉴봇 포털에 접속하셨습니다.",
                    "tier": policy.to_dict(),
                    "used_slots": len(active_subs),
                    "active_subscriptions": active_subs,
                }

            # 3. Check if already subscribed to this INFO bot
            existing_sub = conn.execute(
                "SELECT * FROM subscriptions WHERE telegram_user_id = ? AND bot_id = ?",
                (uid, bid),
            ).fetchone()
            active_subs = self._get_active_info_subs_in_tx(conn, uid)

            if existing_sub and existing_sub["status"] == SubscriptionStatus.ACTIVE.value:
                return {
                    "success": True,
                    "code": "ALREADY_SUBSCRIBED",
                    "message": f"이미 [{bot['icon_emoji']} {bot['name']}]을 구독 중입니다!",
                    "bot": {"bot_id": bot["bot_id"], "name": bot["name"], "icon_emoji": bot["icon_emoji"]},
                    "tier": policy.to_dict(),
                    "used_slots": len(active_subs),
                    "max_slots": None if policy.is_unlimited else policy.max_bots,
                    "active_subscriptions": active_subs,
                    "menu_bot": {
                        "bot_id": menu_bot_info["bot_id"],
                        "name": menu_bot_info["name"],
                        "telegram_username": (menu_bot_info["telegram_username"] or "").lstrip("@"),
                    }
                    if menu_bot_info
                    else None,
                }

            # 4. Enforce Tier Quota!
            if not bypass_quota and (not policy.is_unlimited) and len(active_subs) >= policy.max_bots:
                active_names = ", ".join([f"{s['icon_emoji']} {s['bot_name']}" for s in active_subs])
                self._log_audit_in_tx(
                    conn,
                    uid,
                    bid,
                    "QUOTA_REJECTED",
                    actor=actor,
                    detail={
                        "tier": policy.tier_code,
                        "max_bots": policy.max_bots,
                        "current_active": [s["bot_id"] for s in active_subs],
                    },
                )
                return {
                    "success": False,
                    "code": "QUOTA_EXCEEDED",
                    "message": (
                        f"현재 회원님은 [{policy.name}] 등급으로 최대 {policy.max_bots}개의 봇만 사용할 수 있습니다. "
                        f"(현재 구독 중: {active_names}) "
                        f"[{bot['icon_emoji']} {bot['name']}]을 이용하시려면 기존 봇을 먼저 탈퇴(해지)하거나 등급을 업그레이드해 주세요."
                    ),
                    "bot": {"bot_id": bot["bot_id"], "name": bot["name"], "icon_emoji": bot["icon_emoji"]},
                    "tier": policy.to_dict(),
                    "used_slots": len(active_subs),
                    "max_slots": policy.max_bots,
                    "active_subscriptions": active_subs,
                    "menu_bot": {
                        "bot_id": menu_bot_info["bot_id"],
                        "name": menu_bot_info["name"],
                        "telegram_username": (menu_bot_info["telegram_username"] or "").lstrip("@"),
                    }
                    if menu_bot_info
                    else None,
                }

            # 5. Claim slot & activate subscription atomically
            conn.execute(
                """INSERT INTO subscriptions (telegram_user_id, bot_id, status, subscribed_at, cancelled_at)
                   VALUES (?, ?, 'ACTIVE', ?, NULL)
                   ON CONFLICT(telegram_user_id, bot_id) DO UPDATE SET
                     status = 'ACTIVE', subscribed_at = excluded.subscribed_at, cancelled_at = NULL""",
                (uid, bid, now_str),
            )
            self._sync_legacy_brief_recipient_in_tx(conn, uid, user["full_name"], True)

            updated_subs = self._get_active_info_subs_in_tx(conn, uid)
            self._log_audit_in_tx(
                conn,
                uid,
                bid,
                "SUBSCRIBE",
                actor=actor,
                detail={"tier": policy.tier_code, "used_slots": len(updated_subs), "bypass_quota": bypass_quota},
            )

            return {
                "success": True,
                "code": "SUBSCRIBED",
                "message": f"🎉 [{bot['icon_emoji']} {bot['name']}] 구독이 완료되었습니다!",
                "bot": {"bot_id": bot["bot_id"], "name": bot["name"], "icon_emoji": bot["icon_emoji"]},
                "tier": policy.to_dict(),
                "used_slots": len(updated_subs),
                "max_slots": None if policy.is_unlimited else policy.max_bots,
                "active_subscriptions": updated_subs,
                "menu_bot": {
                    "bot_id": menu_bot_info["bot_id"],
                    "name": menu_bot_info["name"],
                    "telegram_username": (menu_bot_info["telegram_username"] or "").lstrip("@"),
                }
                if menu_bot_info
                else None,
            }

    def atomic_swap_subscription(
        self,
        telegram_user_id: str,
        cancel_bot_id: str,
        new_bot_id: str,
        actor: str = "USER",
    ) -> dict[str, Any]:
        """Atomically unsubscribes `cancel_bot_id` and subscribes `new_bot_id` in one locked transaction."""
        uid = str(telegram_user_id).strip()
        old_bid = str(cancel_bot_id).strip()
        new_bid = str(new_bot_id).strip()
        if old_bid == new_bid:
            return {"success": False, "code": "SAME_BOT", "message": "동일한 봇으로는 교체할 수 없습니다."}

        now_str = now_kst_str()
        with self._exclusive_tx() as conn:
            user = conn.execute("SELECT * FROM users WHERE telegram_user_id = ?", (uid,)).fetchone()
            if not user:
                return {"success": False, "code": "USER_NOT_FOUND", "message": "가입 정보를 찾을 수 없습니다."}
            user = self._enforce_user_expiry_in_tx(conn, user)
            if user["status"] == "BLOCKED":
                return {"success": False, "code": "USER_BLOCKED", "message": "이용이 제한된 계정입니다."}

            old_bot = conn.execute("SELECT * FROM bots WHERE bot_id = ?", (old_bid,)).fetchone()
            new_bot = conn.execute("SELECT * FROM bots WHERE bot_id = ?", (new_bid,)).fetchone()
            if not old_bot or not new_bot or new_bot["bot_type"] != BotType.INFO.value:
                return {"success": False, "code": "INVALID_BOT", "message": "유효하지 않은 정보봇입니다."}
            if not new_bot["is_active"]:
                return {"success": False, "code": "BOT_INACTIVE", "message": f"[{new_bot['name']}]은 현재 비활성 상태입니다."}

            old_sub = conn.execute(
                "SELECT * FROM subscriptions WHERE telegram_user_id = ? AND bot_id = ? AND status = 'ACTIVE'",
                (uid, old_bid),
            ).fetchone()
            if not old_sub:
                return {
                    "success": False,
                    "code": "OLD_SUB_NOT_ACTIVE",
                    "message": f"[{old_bot['name']}]이 현재 구독 중이 아니어서 교체할 수 없습니다.",
                }

            # 1. Cancel old bot
            conn.execute(
                "UPDATE subscriptions SET status = 'CANCELLED', cancelled_at = ? WHERE id = ?",
                (now_str, old_sub["id"]),
            )
            if old_bid == "brief_bot":
                self._sync_legacy_brief_recipient_in_tx(conn, uid, user["full_name"], False, reason="SWAPPED_OUT")

            # 2. Verify quota after cancellation
            policy = self._get_tier_policy_in_tx(conn, user["tier"])
            remaining_active = self._get_active_info_subs_in_tx(conn, uid)
            if (not policy.is_unlimited) and len(remaining_active) >= policy.max_bots:
                # Rollback entire swap if user is still over quota (e.g., admin downgraded 3->1)
                raise ValueError(
                    f"등급 한도({policy.name}: 최대 {policy.max_bots}개)를 여전히 초과하여 교체할 수 없습니다. 메뉴봇에서 추가 해지해 주세요."
                )

            # 3. Activate new bot
            conn.execute(
                """INSERT INTO subscriptions (telegram_user_id, bot_id, status, subscribed_at, cancelled_at)
                   VALUES (?, ?, 'ACTIVE', ?, NULL)
                   ON CONFLICT(telegram_user_id, bot_id) DO UPDATE SET
                     status = 'ACTIVE', subscribed_at = excluded.subscribed_at, cancelled_at = NULL""",
                (uid, new_bid, now_str),
            )
            self._sync_legacy_brief_recipient_in_tx(conn, uid, user["full_name"], True)

            updated_subs = self._get_active_info_subs_in_tx(conn, uid)
            self._log_audit_in_tx(
                conn,
                uid,
                new_bid,
                "SWAP",
                actor=actor,
                detail={"cancelled_bot": old_bid, "subscribed_bot": new_bid, "tier": policy.tier_code},
            )

            return {
                "success": True,
                "code": "SWAPPED",
                "message": (
                    f"✅ [{old_bot['icon_emoji']} {old_bot['name']}] 탈퇴 처리 및 "
                    f"[{new_bot['icon_emoji']} {new_bot['name']}] 신규 구독 전환이 완료되었습니다!"
                ),
                "cancelled_bot": {"bot_id": old_bot["bot_id"], "name": old_bot["name"], "icon_emoji": old_bot["icon_emoji"]},
                "subscribed_bot": {"bot_id": new_bot["bot_id"], "name": new_bot["name"], "icon_emoji": new_bot["icon_emoji"]},
                "tier": policy.to_dict(),
                "used_slots": len(updated_subs),
                "max_slots": None if policy.is_unlimited else policy.max_bots,
                "active_subscriptions": updated_subs,
            }

    def unsubscribe_bot(
        self,
        telegram_user_id: str,
        bot_id: str,
        actor: str = "USER",
        reason: str = "USER_STOP",
    ) -> dict[str, Any]:
        """Unsubscribes user from an INFO bot and releases the quota slot immediately."""
        uid = str(telegram_user_id).strip()
        bid = str(bot_id).strip()
        now_str = now_kst_str()

        with self._exclusive_tx() as conn:
            user = conn.execute("SELECT * FROM users WHERE telegram_user_id = ?", (uid,)).fetchone()
            bot = conn.execute("SELECT * FROM bots WHERE bot_id = ?", (bid,)).fetchone()
            if not bot:
                return {"success": False, "code": "BOT_NOT_FOUND", "message": "봇을 찾을 수 없습니다."}

            sub = conn.execute(
                "SELECT * FROM subscriptions WHERE telegram_user_id = ? AND bot_id = ?",
                (uid, bid),
            ).fetchone()
            if not sub or sub["status"] == SubscriptionStatus.CANCELLED.value:
                active_subs = self._get_active_info_subs_in_tx(conn, uid)
                return {
                    "success": False,
                    "code": "NOT_SUBSCRIBED",
                    "message": f"현재 [{bot['icon_emoji']} {bot['name']}]을 구독하고 있지 않습니다.",
                    "used_slots": len(active_subs),
                    "active_subscriptions": active_subs,
                }

            conn.execute(
                "UPDATE subscriptions SET status = 'CANCELLED', cancelled_at = ? WHERE id = ?",
                (now_str, sub["id"]),
            )
            if user:
                self._sync_legacy_brief_recipient_in_tx(conn, uid, user["full_name"], False, reason=reason)

            # Reconcile GRACE_PERIOD if user freed up slots
            policy = self._get_tier_policy_in_tx(conn, user["tier"] if user else "WHITE")
            active_subs = self._get_active_info_subs_in_tx(conn, uid)
            if user and user["status"] == "GRACE_PERIOD":
                susp = conn.execute(
                    "SELECT COUNT(*) FROM subscriptions WHERE telegram_user_id = ? AND status = 'SUSPENDED_OVER_QUOTA'",
                    (uid,),
                ).fetchone()[0]
                if susp == 0 and (policy.is_unlimited or len(active_subs) <= policy.max_bots):
                    conn.execute(
                        "UPDATE users SET status = 'ACTIVE', updated_at = ? WHERE telegram_user_id = ?",
                        (now_str, uid),
                    )

            self._log_audit_in_tx(
                conn,
                uid,
                bid,
                "UNSUBSCRIBE",
                actor=actor,
                detail={"reason": reason, "remaining_used_slots": len(active_subs)},
            )

            return {
                "success": True,
                "code": "UNSUBSCRIBED",
                "message": f"👋 [{bot['icon_emoji']} {bot['name']}] 구독 탈퇴(해지)가 완료되었습니다.",
                "bot": {"bot_id": bot["bot_id"], "name": bot["name"], "icon_emoji": bot["icon_emoji"]},
                "tier": policy.to_dict(),
                "used_slots": len(active_subs),
                "max_slots": None if policy.is_unlimited else policy.max_bots,
                "active_subscriptions": active_subs,
            }

    # =========================================================================
    # Broadcast Target Resolution, Matrix & Analytics Overview
    # =========================================================================

    def get_bot_active_recipients(self, bot_id: str) -> list[Recipient]:
        """Returns active, non-blocked subscribers for a given `bot_id` as `Recipient` objects."""
        self.run_expiry_sweep()
        bid = str(bot_id).strip()
        with self._read_conn() as conn:
            rows = conn.execute(
                """SELECT u.telegram_user_id, u.full_name, u.tier, b.topic_code
                   FROM subscriptions s
                   JOIN users u ON s.telegram_user_id = u.telegram_user_id
                   JOIN bots b ON s.bot_id = b.bot_id
                   WHERE s.bot_id = ? AND s.status = 'ACTIVE' AND u.status != 'BLOCKED'
                   ORDER BY s.subscribed_at ASC""",
                (bid,),
            ).fetchall()
            return [
                Recipient(
                    chat_id=r["telegram_user_id"],
                    name=r["full_name"],
                    channel="TELEGRAM",
                    group_tag=r["tier"],
                    subscribed_topics=r["topic_code"] or "GENERAL",
                    is_active=True,
                )
                for r in rows
            ]

    def get_subscription_matrix(self) -> dict[str, Any]:
        """Builds the User x Info-Bot cross matrix for the Admin Matrix view."""
        bots = [b for b in self.list_bots() if b["bot_type"] == BotType.INFO.value]
        users = self.list_users_with_subscriptions()
        matrix_rows = []
        for u in users:
            active_set = {s["bot_id"] for s in u["active_subscriptions"]}
            suspended_set = {s["bot_id"] for s in u["suspended_subscriptions"]}
            cells = {}
            for b in bots:
                bid = b["bot_id"]
                if bid in active_set:
                    cells[bid] = "ACTIVE"
                elif bid in suspended_set:
                    cells[bid] = "SUSPENDED"
                else:
                    cells[bid] = "NONE"
            matrix_rows.append(
                {
                    "telegram_user_id": u["telegram_user_id"],
                    "full_name": u["full_name"],
                    "username": u["username"],
                    "tier": u["tier"],
                    "tier_name": u["tier_name"],
                    "tier_badge_color": u["tier_badge_color"],
                    "used_slots": u["used_slots"],
                    "max_slots": u["max_slots"],
                    "is_unlimited": u["is_unlimited"],
                    "is_over_quota": u["is_over_quota"],
                    "status": u["status"],
                    "cells": cells,
                }
            )
        return {"bots": bots, "rows": matrix_rows}

    def get_overview_metrics(self, audit_limit: int = 25) -> dict[str, Any]:
        """Returns comprehensive KPIs, tier distribution, bot popularity, and audit feed."""
        self.run_expiry_sweep()
        bots = self.list_bots()
        users = self.list_users_with_subscriptions()
        tiers = self.get_tiers()

        menu_bots = [b for b in bots if b["bot_type"] == BotType.MENU.value]
        info_bots = [b for b in bots if b["bot_type"] == BotType.INFO.value]

        tier_counts = {t["tier_code"]: 0 for t in tiers}
        over_quota_users = 0
        for u in users:
            tier_counts[u["tier"]] = tier_counts.get(u["tier"], 0) + 1
            if u["is_over_quota"]:
                over_quota_users += 1

        tier_distribution = [
            {
                **t,
                "user_count": tier_counts.get(t["tier_code"], 0),
            }
            for t in tiers
        ]

        total_active_subs = sum(b["active_subscribers"] for b in info_bots)

        with self._read_conn() as conn:
            log_rows = conn.execute(
                """SELECT l.*, u.full_name, b.name AS bot_name, b.icon_emoji
                   FROM subscription_audit_logs l
                   LEFT JOIN users u ON l.telegram_user_id = u.telegram_user_id
                   LEFT JOIN bots b ON l.bot_id = b.bot_id
                   ORDER BY l.id DESC LIMIT ?""",
                (audit_limit,),
            ).fetchall()
            audit_logs = []
            for lr in log_rows:
                parsed_detail = lr["detail"]
                if parsed_detail:
                    try:
                        parsed_detail = json.loads(parsed_detail)
                    except Exception:
                        pass
                audit_logs.append(
                    {
                        "id": lr["id"],
                        "telegram_user_id": lr["telegram_user_id"],
                        "user_name": lr["full_name"] or lr["telegram_user_id"],
                        "bot_id": lr["bot_id"],
                        "bot_name": f"{lr['icon_emoji'] or ''} {lr['bot_name']}".strip() if lr["bot_name"] else None,
                        "action": lr["action"],
                        "actor": lr["actor"],
                        "detail": parsed_detail,
                        "logged_at": lr["logged_at"],
                    }
                )

        return {
            "total_users": len(users),
            "over_quota_users": over_quota_users,
            "total_bots": len(bots),
            "info_bots_count": len(info_bots),
            "active_info_bots_count": sum(1 for b in info_bots if b["is_active"]),
            "configured_token_bots_count": sum(1 for b in bots if b["has_token"]),
            "menu_bot": menu_bots[0] if menu_bots else None,
            "total_active_subscriptions": total_active_subs,
            "tier_distribution": tier_distribution,
            "bot_rankings": sorted(info_bots, key=lambda x: (-x["active_subscribers"], x["sort_order"])),
            "recent_audit_logs": audit_logs,
        }
