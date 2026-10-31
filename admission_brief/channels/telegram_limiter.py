"""Rate limiter and resilient Telegram message dispatcher for Admission Briefing.
Protects against 429 Too Many Requests, implements per-chat interval and global throughput controls.
"""
from __future__ import annotations
import json
import logging
import re
import time
import urllib.error
from typing import Any, Optional

from admission_brief.channels.telegram import TelegramChannel
from admission_brief.models import AdmissionBriefing
from admission_brief.storage.db import AdmissionDatabase

logger = logging.getLogger(__name__)


class TelegramRateLimiter:
    """Enforces Telegram's official rate limits:
    - Global broadcast: <= 25 msgs/sec
    - Per individual chat: >= 1.0 sec interval
    """

    def __init__(self, max_global_rate: float = 25.0, min_chat_interval: float = 1.0):
        self.max_global_rate = max_global_rate
        self.min_global_interval = 1.0 / max_global_rate if max_global_rate > 0 else 0.04
        self.min_chat_interval = min_chat_interval
        self._last_global_send: float = 0.0
        self._last_chat_send: dict[str, float] = {}

    def wait_before_send(self, chat_id: str) -> None:
        now = time.time()
        # 1. Global throttle
        elapsed_global = now - self._last_global_send
        if elapsed_global < self.min_global_interval:
            time.sleep(self.min_global_interval - elapsed_global)

        # 2. Per-chat throttle
        now = time.time()
        last_chat = self._last_chat_send.get(chat_id, 0.0)
        elapsed_chat = now - last_chat
        if elapsed_chat < self.min_chat_interval:
            time.sleep(self.min_chat_interval - elapsed_chat)

        final_now = time.time()
        self._last_global_send = final_now
        self._last_chat_send[chat_id] = final_now


class SafeTelegramDispatcher:
    """Dispatches admission briefings with rate limits and 429 retry-after handling."""

    def __init__(
        self,
        rate_limiter: Optional[TelegramRateLimiter] = None,
        max_429_retries: int = 3,
    ):
        self.limiter = rate_limiter or TelegramRateLimiter()
        self.max_429_retries = max_429_retries

    def dispatch(
        self,
        channel: TelegramChannel,
        briefing: AdmissionBriefing,
        db: AdmissionDatabase,
        recipients: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        """Dispatches an admission briefing to matching subscribers in the database."""
        stats = {
            "total": 0,
            "success": 0,
            "skipped": 0,
            "failed": 0,
            "deactivated": 0,
            "errors": {},
        }

        # Target recipients resolution
        if recipients is not None:
            target_chat_ids = [str(r).strip() for r in recipients if str(r).strip()]
        else:
            subscribers = db.list_subscribers(
                grade=briefing.target_grade,
                region=briefing.target_region,
                is_online_live=briefing.is_online_live,
                active_only=True,
            )
            target_chat_ids = [s.chat_id for s in subscribers]

        stats["total"] = len(target_chat_ids)

        for chat_id in target_chat_ids:
            # Check duplicate dispatch
            if db.has_dispatched(briefing.briefing_id, chat_id):
                stats["skipped"] += 1
                continue

            # Rate limit guard
            self.limiter.wait_before_send(chat_id)

            attempt = 0
            while attempt <= self.max_429_retries:
                try:
                    # Silent push guard: disable notification for regular digests/recaps
                    channel.send(chat_id, briefing, disable_notification=briefing.silent)
                    # If PDF path exists, send document as well
                    if briefing.pdf_path:
                        try:
                            channel.send_document(
                                chat_id,
                                briefing.pdf_path,
                                caption=f"📄 [{briefing.headline}] 첨부 자료집",
                            )
                        except Exception as doc_err:
                            logger.warning("PDF attachment dispatch failed to %s: %s", chat_id, doc_err)

                    db.log_dispatch(briefing.briefing_id, chat_id, "SUCCESS")
                    stats["success"] += 1
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code == 429:
                        attempt += 1
                        if attempt > self.max_429_retries:
                            stats["failed"] += 1
                            stats["errors"][chat_id] = "429_EXCEEDED"
                            db.log_dispatch(briefing.briefing_id, chat_id, "FAILED_429")
                            break
                        # Parse retry_after
                        retry_sec = 2.0
                        try:
                            err_body = json.loads(exc.read().decode("utf-8", errors="ignore"))
                            retry_sec = float(err_body.get("parameters", {}).get("retry_after", 2.0))
                        except Exception:
                            pass
                        time.sleep(retry_sec)
                        continue
                    elif exc.code in {400, 403}:
                        # Blocked or deactivated user
                        stats["deactivated"] += 1
                        db.log_dispatch(briefing.briefing_id, chat_id, f"HTTP_{exc.code}")
                        break
                    else:
                        stats["failed"] += 1
                        stats["errors"][chat_id] = f"HTTP_{exc.code}"
                        db.log_dispatch(briefing.briefing_id, chat_id, f"HTTP_{exc.code}")
                        break
                except Exception as exc:
                    stats["failed"] += 1
                    stats["errors"][chat_id] = str(exc)
                    db.log_dispatch(briefing.briefing_id, chat_id, "FAILED_ERROR")
                    break

        return stats
