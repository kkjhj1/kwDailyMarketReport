"""Data models for Market Brief storage and audit trail."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class BatchStatus(str, Enum):
    PENDING = "PENDING"
    LOCKED = "LOCKED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ABORTED = "ABORTED"


class JobFinalStatus(str, Enum):
    PENDING = "PENDING"
    SENT = "SENT"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class AttemptStatus(str, Enum):
    SUCCESS = "SUCCESS"
    RATE_LIMITED = "RATE_LIMITED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


@dataclass
class Recipient:
    chat_id: str
    name: str
    channel: str = "TELEGRAM"
    group_tag: str = "GENERAL"
    subscribed_topics: str = "FINANCE"
    is_active: bool = True
    inactivation_reason: str | None = None
    created_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "name": self.name,
            "channel": self.channel,
            "group_tag": self.group_tag,
            "subscribed_topics": self.subscribed_topics.split(",") if self.subscribed_topics else ["FINANCE"],
            "is_active": bool(self.is_active),
            "inactivation_reason": self.inactivation_reason,
            "created_at": self.created_at,
        }


@dataclass
class DeliveryBatch:
    batch_key: str
    topic: str
    kind: str
    scheduled_date: str
    status: BatchStatus = BatchStatus.PENDING
    lease_owner: str | None = None
    lease_token: str | None = None
    lease_expires_at: str | None = None
    created_at: str | None = None
    finished_at: str | None = None


@dataclass
class DeliveryJob:
    job_id: str
    batch_key: str
    attempt_no: int = 1
    is_forced: bool = False
    force_reason: str | None = None
    status: JobFinalStatus = JobFinalStatus.PENDING
    total_targets: int = 0
    success_count: int = 0
    fail_count: int = 0
    unknown_count: int = 0
    report_path: str | None = None
    started_at: str | None = None
    finished_at: str | None = None


@dataclass
class DeliveryJobRecipient:
    job_id: str
    chat_id: str
    status: str = "PENDING"
    error_detail: str | None = None
    provider_msg_id: str | None = None
    updated_at: str | None = None


@dataclass
class SystemConfig:
    setting_key: str
    setting_value: str
    description: str | None = None
    updated_at: str | None = None


# =========================================================================
# Multi-Bot Subscription Platform Models (v2.0)
# =========================================================================


class UserTier(str, Enum):
    WHITE = "WHITE"  # 무료등급: 최대 1개 봇
    GREEN = "GREEN"  # 그린등급: 최대 3개 봇
    RED = "RED"      # 레드등급: 최대 5개 봇
    BLACK = "BLACK"  # 블랙등급: 모든 봇 사용 가능 (무제한)


class BotType(str, Enum):
    MENU = "MENU"    # 타이틀 안내 및 가입 유도용 허브 메뉴봇
    INFO = "INFO"    # 실제 콘텐츠 발송 및 구독 대상 정보봇


class SubscriptionStatus(str, Enum):
    ACTIVE = "ACTIVE"
    CANCELLED = "CANCELLED"
    SUSPENDED_OVER_QUOTA = "SUSPENDED_OVER_QUOTA"


TIER_DEFAULT_SPECS: dict[str, dict[str, Any]] = {
    UserTier.WHITE.value: {
        "tier_code": "WHITE",
        "name": "화이트 (무료)",
        "max_bots": 1,
        "is_unlimited": False,
        "badge_color": "#64748b",
        "description": "무료 기본 등급 · 정보봇 1개 선택 사용 가능 (타 봇 이용 시 기존 봇 탈퇴 후 전환)",
        "sort_order": 1,
    },
    UserTier.GREEN.value: {
        "tier_code": "GREEN",
        "name": "그린",
        "max_bots": 3,
        "is_unlimited": False,
        "badge_color": "#10b981",
        "description": "그린 멤버십 · 정보봇 최대 3개 동시 구독 가능",
        "sort_order": 2,
    },
    UserTier.RED.value: {
        "tier_code": "RED",
        "name": "레드",
        "max_bots": 5,
        "is_unlimited": False,
        "badge_color": "#ef4444",
        "description": "레드 멤버십 · 정보봇 최대 5개 동시 구독 가능",
        "sort_order": 3,
    },
    UserTier.BLACK.value: {
        "tier_code": "BLACK",
        "name": "블랙 (VIP)",
        "max_bots": 999999,
        "is_unlimited": True,
        "badge_color": "#0f172a",
        "description": "블랙 VIP · 운영 중인 모든 정보봇 무제한 동시 구독 가능",
        "sort_order": 4,
    },
}


def mask_bot_token(token: str | None) -> str:
    """Masks Telegram bot token safely so full secrets are never exposed in logs/UI."""
    if not token:
        return ""
    clean = token.strip()
    if len(clean) <= 10:
        return "*" * len(clean)
    return f"{clean[:6]}...{clean[-4:]}"


@dataclass
class TierPolicy:
    tier_code: str
    name: str
    max_bots: int
    badge_color: str = "#64748b"
    description: str = ""
    sort_order: int = 1

    @property
    def is_unlimited(self) -> bool:
        return self.tier_code.upper() == UserTier.BLACK.value or self.max_bots >= 999999

    def to_dict(self) -> dict[str, Any]:
        return {
            "tier_code": self.tier_code,
            "name": self.name,
            "max_bots": None if self.is_unlimited else self.max_bots,
            "max_bots_raw": self.max_bots,
            "is_unlimited": self.is_unlimited,
            "badge_color": self.badge_color,
            "description": self.description,
            "sort_order": self.sort_order,
        }


@dataclass
class BotRecord:
    bot_id: str
    bot_type: str  # 'MENU' | 'INFO'
    name: str
    telegram_username: str = ""
    token: str = ""
    category: str = "일반"
    icon_emoji: str = "🤖"
    description: str = ""
    topic_code: str = "GENERAL"
    is_active: bool = True
    sort_order: int = 10
    last_offset: int = 0
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self, include_raw_token: bool = False) -> dict[str, Any]:
        uname = (self.telegram_username or "").lstrip("@").strip()
        data: dict[str, Any] = {
            "bot_id": self.bot_id,
            "bot_type": self.bot_type,
            "name": self.name,
            "telegram_username": uname,
            "deep_link": f"https://t.me/{uname}" if uname else None,
            "has_token": bool(self.token and self.token.strip()),
            "token_masked": mask_bot_token(self.token),
            "category": self.category,
            "icon_emoji": self.icon_emoji or "🤖",
            "description": self.description,
            "topic_code": self.topic_code,
            "is_active": bool(self.is_active),
            "sort_order": self.sort_order,
            "last_offset": self.last_offset,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if include_raw_token:
            data["token"] = self.token
        return data


@dataclass
class PlatformUser:
    telegram_user_id: str
    full_name: str
    username: str = ""
    tier: str = "WHITE"
    tier_expires_at: str | None = None
    status: str = "ACTIVE"  # ACTIVE, BLOCKED, GRACE_PERIOD
    memo: str = ""
    created_at: str | None = None
    updated_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "telegram_user_id": self.telegram_user_id,
            "full_name": self.full_name,
            "username": (self.username or "").lstrip("@"),
            "tier": self.tier,
            "tier_expires_at": self.tier_expires_at,
            "status": self.status,
            "memo": self.memo or "",
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class Subscription:
    id: int
    telegram_user_id: str
    bot_id: str
    status: str = "ACTIVE"
    subscribed_at: str | None = None
    cancelled_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "telegram_user_id": self.telegram_user_id,
            "bot_id": self.bot_id,
            "status": self.status,
            "subscribed_at": self.subscribed_at,
            "cancelled_at": self.cancelled_at,
        }
