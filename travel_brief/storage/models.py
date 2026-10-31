"""Data models for Travel Brief storage and audit trail."""
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
    subscribed_topics: str = "TRAVEL"
    is_active: bool = True
    inactivation_reason: str | None = None
    created_at: str | None = None

    @property
    def topic(self) -> str:
        return self.subscribed_topics

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "name": self.name,
            "channel": self.channel,
            "group_tag": self.group_tag,
            "subscribed_topics": self.subscribed_topics.split(",") if self.subscribed_topics else ["TRAVEL"],
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
