from __future__ import annotations

import asyncio
import os
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from kids_compass.interactive_bot import dispatch_due_dose_timers
from kids_compass.storage.db import KidsCompassDatabase
from market_brief.multi_bot_manager import DynamicBotManager, run_kids_timer_tick_once


NOW = datetime(2026, 11, 21, 21, 0, 0)


@pytest.fixture
def test_dir():
    """Use the writable workspace directly; this runner's tmpdir ACL is restricted."""
    path = Path.cwd() / f".kids_timer_safety_{os.getpid()}"
    path.mkdir(exist_ok=False)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


class FakeChannel:
    def __init__(self):
        self.sent: list[tuple[str, str, dict | None]] = []

    def send_message(self, chat_id, text, reply_markup=None, disable_notification=False):
        self.sent.append((str(chat_id), text, reply_markup))
        return len(self.sent)


def _insert_timer(db: KidsCompassDatabase, uid: str, child_id: int, *,
                  due_at: datetime, expires_at: datetime, status: str = "PENDING") -> int:
    stamp = NOW.strftime("%Y-%m-%d %H:%M:%S")
    cur = db.conn.execute(
        """INSERT INTO kids_dose_timers
           (dose_event_id, telegram_user_id, child_id, given_ingredient, suggested_ingredient,
            due_at, expires_at, status, created_at, updated_at)
           VALUES (NULL, ?, ?, 'ACETAMINOPHEN', 'IBUPROFEN', ?, ?, ?, ?, ?)""",
        (uid, child_id, due_at.strftime("%Y-%m-%d %H:%M:%S"),
         expires_at.strftime("%Y-%m-%d %H:%M:%S"), status, stamp, stamp),
    )
    db.conn.commit()
    return int(cur.lastrowid)


def _manager_for(db_path: str) -> DynamicBotManager:
    class _Engine:
        db = type("Db", (), {"db_path": db_path})()

    return DynamicBotManager(_Engine(), router=object())


def test_auto_tick_missing_kids_db_skips_without_creating_file(test_dir):
    base_path = test_dir / "alerts.sqlite3"
    kids_path = test_dir / "kids_compass.sqlite3"
    manager = _manager_for(str(base_path))
    manager._running = True
    manager._kids_timer_token = "token"

    async def run_once():
        task = asyncio.create_task(manager._kids_timer_loop("token"))
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run_once())
    assert not kids_path.exists()


def test_memory_auto_tick_skips_but_explicit_worker_helper_opens_cleanly(test_dir):
    manager = _manager_for(":memory:")
    manager._running = True
    manager._kids_timer_token = "token"

    async def run_once():
        task = asyncio.create_task(manager._kids_timer_loop("token"))
        await asyncio.sleep(0.01)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run_once())

    db_path = test_dir / "kids_compass.sqlite3"
    db = KidsCompassDatabase(db_path)
    db.close()

    class NeverCalled:
        def __call__(self, *args, **kwargs):
            raise AssertionError("network opener must not be called")

    with ThreadPoolExecutor(max_workers=1) as pool:
        result = pool.submit(
            run_kids_timer_tick_once,
            "test-token",
            db_path=db_path,
            opener=NeverCalled(),
            now=NOW,
        ).result()
    assert result["total"] == 0


def test_repeated_30_second_ticks_send_same_due_timer_at_most_once():
    db = KidsCompassDatabase(":memory:")
    child_id = db.add_child("u1", "콩이", "2024-05-01", 11.0)
    _insert_timer(db, "u1", child_id, due_at=NOW - timedelta(seconds=1),
                  expires_at=NOW + timedelta(minutes=10))
    channel = FakeChannel()

    first = dispatch_due_dose_timers(db, channel, now=NOW)
    second = dispatch_due_dose_timers(db, channel, now=NOW + timedelta(seconds=30))

    assert first["sent"] == 1
    assert second["total"] == 0
    assert len(channel.sent) == 1
    db.close()


def test_expiry_boundary_is_strict_and_never_sent():
    db = KidsCompassDatabase(":memory:")
    child_id = db.add_child("u1", "콩이", "2024-05-01", 11.0)
    timer_id = _insert_timer(db, "u1", child_id, due_at=NOW - timedelta(minutes=1),
                             expires_at=NOW)
    channel = FakeChannel()

    stats = dispatch_due_dose_timers(db, channel, now=NOW)

    assert stats["total"] == 0
    assert not channel.sent
    assert db.get_dose_timer(timer_id).status == "EXPIRED"
    db.close()


def test_startup_cutoff_blocks_missed_downtime_timer():
    db = KidsCompassDatabase(":memory:")
    child_id = db.add_child("u1", "콩이", "2024-05-01", 11.0)
    timer_id = _insert_timer(db, "u1", child_id, due_at=NOW - timedelta(seconds=1),
                             expires_at=NOW + timedelta(minutes=10))
    channel = FakeChannel()

    stats = dispatch_due_dose_timers(
        db,
        channel,
        now=NOW,
        startup_cutoff="2026-11-21T21:00:00",
    )

    assert stats["total"] == 0
    assert not channel.sent
    assert db.get_dose_timer(timer_id).status == "PENDING"
    db.close()


def test_unknown_timer_is_never_resent():
    db = KidsCompassDatabase(":memory:")
    child_id = db.add_child("u1", "콩이", "2024-05-01", 11.0)
    timer_id = _insert_timer(db, "u1", child_id, due_at=NOW - timedelta(minutes=1),
                             expires_at=NOW + timedelta(minutes=10), status="UNKNOWN")
    channel = FakeChannel()

    stats = dispatch_due_dose_timers(db, channel, now=NOW)

    assert stats["total"] == 0
    assert not channel.sent
    assert db.get_dose_timer(timer_id).status == "UNKNOWN"
    db.close()


def test_sibling_alerts_subscription_filters_timer_recipients(test_dir):
    kids_path = test_dir / "kids_compass.sqlite3"
    alerts_path = test_dir / "alerts.sqlite3"
    alerts = sqlite3.connect(alerts_path)
    alerts.executescript(
        """
        CREATE TABLE users (telegram_user_id TEXT PRIMARY KEY, status TEXT NOT NULL);
        CREATE TABLE subscriptions (
            telegram_user_id TEXT NOT NULL, bot_id TEXT NOT NULL, status TEXT NOT NULL
        );
        """
    )
    alerts.executemany("INSERT INTO users VALUES (?, ?)", [("active", "ACTIVE"), ("off", "ACTIVE")])
    alerts.executemany(
        "INSERT INTO subscriptions VALUES (?, 'kids_bot', ?)",
        [("active", "ACTIVE"), ("off", "CANCELLED")],
    )
    alerts.commit()
    alerts.close()

    db = KidsCompassDatabase(kids_path)
    active_child = db.add_child("active", "활성", "2024-05-01", 11.0)
    off_child = db.add_child("off", "해지", "2024-05-01", 11.0)
    _insert_timer(db, "active", active_child, due_at=NOW - timedelta(seconds=1),
                  expires_at=NOW + timedelta(minutes=10))
    _insert_timer(db, "off", off_child, due_at=NOW - timedelta(seconds=1),
                  expires_at=NOW + timedelta(minutes=10))
    channel = FakeChannel()

    stats = dispatch_due_dose_timers(db, channel, now=NOW)

    assert stats["sent"] == 1
    assert [chat_id for chat_id, _, _ in channel.sent] == ["active"]
    assert db.conn.execute(
        "SELECT status FROM kids_dose_timers WHERE telegram_user_id = 'off'"
    ).fetchone()[0] == "PENDING"
    db.close()
