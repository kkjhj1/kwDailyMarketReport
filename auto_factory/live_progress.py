"""Live progress monitoring: metadata-only sidecars, lease keeper and liveness classifier.

Sidecars live strictly inside the external state dir:
    <artifacts_dir>/<task_id>/live.json   current snapshot (atomic replace, schema_version=1)
    <artifacts_dir>/<task_id>/live.log    bounded (64 KiB) metadata-only event log

Nothing here ever records raw stdout/stderr, command lines, cwd/worktree paths, contract or
request text, the lease owner nonce, or holdout test names/paths/assertions. During the holdout
gate only a coarse heartbeat (`step=holdout status=running elapsed=Ns`) is emitted.

Sinks are passed explicitly (no thread-locals). `output()` only touches in-memory counters, so
it is safe to call from pipe reader threads; all file I/O happens in `flush()` (serialized).
Any sink failure is swallowed: monitoring must never fail a worker. Lease loss is the one
signal that is *not* swallowed: `is_lease_lost()` tells `JobRunner` to kill the child.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .db import ACTIVE_STATES, MAIN_FLOW, TERMINAL_STATES, heartbeat_nowait, is_transient_db_error
from .worktree import (
    UnsafePathError,
    _assert_no_link_or_reparse_ancestors,
    _is_link_or_reparse,
    safe_read_bytes,
    safe_write_atomic,
)

SCHEMA_VERSION = 1
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
LIVE_JSON = "live.json"
LIVE_LOG = "live.log"
MAX_LOG_BYTES = 64 * 1024
MAX_JSON_BYTES = 16 * 1024
DEFAULT_PULSE_INTERVAL = 5.0
DEFAULT_LOG_INTERVAL = 30.0
REDACTION_METADATA = "metadata_only"
REDACTION_HOLDOUT = "holdout"
_SAFE_TOKEN_RE = re.compile(r"[^A-Za-z0-9._:/-]")
_RETRY_DELAYS = (0.02, 0.05, 0.1)


class LeaseLostError(RuntimeError):
    """The worker no longer owns the task lease. Deliberately NOT a StopRequested / TIMEOUT."""


def validate_task_id(task_id: Any) -> str:
    if not isinstance(task_id, str) or not TASK_ID_RE.match(task_id):
        raise ValueError("invalid task_id")
    return task_id


def _token(value: Any, limit: int = 40) -> str:
    """Log-safe token: ASCII identifier chars only, bounded."""
    if value is None:
        return "-"
    s = _SAFE_TOKEN_RE.sub("_", str(value))[:limit]
    return s or "-"


def iso_utc(t: float | None) -> str:
    if t is None:
        return "-"
    return datetime.fromtimestamp(float(t), tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sidecar_paths(artifacts_dir: str | Path, task_id: str) -> tuple[Path, Path]:
    validate_task_id(task_id)
    d = Path(artifacts_dir) / task_id
    return d / LIVE_JSON, d / LIVE_LOG


# ===================================================================== readers
def read_live_sidecar(artifacts_dir: str | Path | None, task_id: str) -> dict[str, Any] | None:
    """Validated `live.json` or None (missing / link / reparse / oversized / corrupt / mismatched)."""
    if artifacts_dir is None:
        return None
    try:
        jpath, _ = sidecar_paths(artifacts_dir, task_id)
        raw = safe_read_bytes(jpath, MAX_JSON_BYTES)
        data = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):  # UnsafePathError is a ValueError
        return None
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        return None
    if data.get("task_id") != task_id:
        return None
    gen = data.get("generation")
    if not isinstance(gen, int) or isinstance(gen, bool) or gen < 1:
        return None
    return data


def read_live_log_tail(artifacts_dir: str | Path | None, task_id: str, lines: int = 40) -> list[str] | None:
    """Last `lines` lines of `live.log` (clamped 1..500) or None if unavailable/unsafe."""
    if artifacts_dir is None:
        return None
    lines = max(1, min(int(lines), 500))
    try:
        _, lpath = sidecar_paths(artifacts_dir, task_id)
        raw = safe_read_bytes(lpath, MAX_LOG_BYTES + 4096)
    except (OSError, ValueError):
        return None
    text = raw.decode("utf-8", errors="replace")
    return [ln for ln in text.splitlines() if ln.strip()][-lines:]


# ======================================================================= sinks
class ProgressSink:
    """No-op sink. `lease_signal` (optional) lets a bare sink still carry the lease stop signal."""

    def __init__(self, lease_signal: Callable[[], bool] | None = None) -> None:
        self.lease_signal = lease_signal
        self._lease_lost = False

    def start(self, pid: int | None, proc_start_time: float | None, step: str | None = None,
              provider: str | None = None, role: str | None = None, attempt_id: int | None = None,
              *, redaction: str | None = None) -> None:
        pass

    def output(self, stream: str, bytes_seen: int, last_output_at: float | None = None) -> None:
        pass

    def pulse(self) -> None:
        pass

    def finish(self, returncode: int | None, timed_out: bool = False, timeout_kind: str | None = None) -> None:
        pass

    def mark_lease_lost(self, reason: str = "") -> None:
        self._lease_lost = True

    def is_lease_lost(self) -> bool:
        if self._lease_lost:
            return True
        if self.lease_signal is not None:
            try:
                return bool(self.lease_signal())
            except Exception:
                return False
        return False


class LiveProgressSink(ProgressSink):
    """Single-writer sidecar for one task run (one lease acquisition = one generation)."""

    def __init__(
        self,
        artifacts_dir: str | Path,
        task_id: str,
        *,
        clock: Callable[[], float] = time.time,
        pulse_interval: float = DEFAULT_PULSE_INTERVAL,
        log_interval: float = DEFAULT_LOG_INTERVAL,
        max_log_bytes: int = MAX_LOG_BYTES,
        writer: Callable[[Path, bytes], Any] | None = None,
        retry_delays: tuple[float, ...] = _RETRY_DELAYS,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        lease_signal: Callable[[], bool] | None = None,
    ) -> None:
        super().__init__(lease_signal)
        self.task_id = validate_task_id(task_id)
        self.artifacts_dir = Path(artifacts_dir)
        self.json_path, self.log_path = sidecar_paths(self.artifacts_dir, task_id)
        self.clock = clock
        self.pulse_interval = float(pulse_interval)
        self.log_interval = float(log_interval)
        self.max_log_bytes = int(max_log_bytes)
        self._writer = writer or safe_write_atomic
        self.retry_delays = tuple(retry_delays)
        self._sleep = sleep
        self._mono = monotonic
        self._state_lock = threading.Lock()
        self._io_lock = threading.RLock()
        self.generation = 0
        self.superseded = False
        self.write_errors = 0
        self._log_buf = b""
        self._last_flush = float("-inf")
        self._last_log = float("-inf")
        self._streams: dict[str, int] = {}
        self._f: dict[str, Any] = {
            "state": None, "step": None, "provider": None, "role": None, "attempt_id": None,
            "pid": None, "proc_start_time": None, "started_at": None, "last_heartbeat_at": None,
            "last_output_at": None, "bytes_seen": 0, "redaction": REDACTION_METADATA, "running": False,
            "finished_at": None, "child_started_at": None,
        }
        self._ensure_dir()

    # ------------------------------------------------------------ filesystem
    def _ensure_dir(self) -> None:
        d = self.json_path.parent
        _assert_no_link_or_reparse_ancestors(d)
        d.mkdir(parents=True, exist_ok=True)
        canon = _assert_no_link_or_reparse_ancestors(d)
        if not canon.is_dir() or _is_link_or_reparse(canon):
            raise UnsafePathError(f"live sidecar dir is not a plain directory: {d}")

    def _write(self, path: Path, data: bytes) -> bool:
        for delay in (0.0,) + self.retry_delays:
            if delay:
                self._sleep(delay)
            try:
                self._writer(path, data)
                return True
            except UnsafePathError:  # link/reparse planted: never retried, never followed
                break
            except OSError:  # Windows sharing violation etc.: short retry, previous file kept
                continue
        self.write_errors += 1
        return False

    # ------------------------------------------------------------ lifecycle
    def begin_run(self, state: str | None = None) -> int:
        """New generation (> any generation already on disk) for a fresh lease acquisition."""
        with self._io_lock:
            existing = read_live_sidecar(self.artifacts_dir, self.task_id)
            prev = int(existing["generation"]) if existing else 0
            self.generation = max(prev, self.generation) + 1
            self.superseded = False
            try:
                raw = safe_read_bytes(self.log_path, self.max_log_bytes + 4096)
                self._log_buf = raw[-self.max_log_bytes:]
            except (OSError, ValueError):
                self._log_buf = b""
            now = self.clock()
            with self._state_lock:
                self._f.update(state=state, started_at=now, last_heartbeat_at=now, running=False,
                               finished_at=None, step=None, provider=None, role=None, attempt_id=None,
                               pid=None, proc_start_time=None, bytes_seen=0, last_output_at=None,
                               redaction=REDACTION_METADATA, child_started_at=None)
                self._streams.clear()
            self._flush("run_begin", force=True)
            return self.generation

    def set_state(self, state: str) -> None:
        with self._state_lock:
            changed = self._f["state"] != state
            self._f["state"] = state
        if changed:
            self._flush("state", force=True)

    def set_stage(self, step: str, provider: str | None = None, role: str | None = None,
                  attempt_id: int | None = None, redaction: str | None = None) -> None:
        with self._state_lock:
            self._f.update(step=step, provider=provider, role=role, attempt_id=attempt_id)
            if redaction is not None:
                self._f["redaction"] = REDACTION_HOLDOUT if redaction == REDACTION_HOLDOUT else REDACTION_METADATA
        self._flush("stage", force=True)

    def start(self, pid, proc_start_time, step=None, provider=None, role=None, attempt_id=None, *,
              redaction=None) -> None:
        now = self.clock()
        with self._state_lock:
            if step is not None:
                self._f["step"] = step
            if provider is not None:
                self._f["provider"] = provider
            if role is not None:
                self._f["role"] = role
            if attempt_id is not None:
                self._f["attempt_id"] = attempt_id
            if redaction is not None:
                self._f["redaction"] = REDACTION_HOLDOUT if redaction == REDACTION_HOLDOUT else REDACTION_METADATA
            self._streams.clear()
            self._f.update(pid=pid, proc_start_time=proc_start_time, running=True, finished_at=None,
                           child_started_at=now, bytes_seen=0)
        self._flush("start", force=True)

    def output(self, stream: str, bytes_seen: int, last_output_at: float | None = None) -> None:
        """In-memory only (called from reader threads): NEVER file or DB I/O here."""
        with self._state_lock:
            if self._f["redaction"] == REDACTION_HOLDOUT:
                return  # holdout: no output-derived signal at all
            self._streams[str(stream)] = max(int(bytes_seen), self._streams.get(str(stream), 0))
            self._f["bytes_seen"] = sum(self._streams.values())
            self._f["last_output_at"] = self.clock() if last_output_at is None else last_output_at

    def pulse(self) -> None:
        now = self._mono()
        if now - self._last_flush < self.pulse_interval:
            return
        event = None
        if now - self._last_log >= self.log_interval:
            event = "heartbeat"
        self._flush(event)

    def finish(self, returncode, timed_out=False, timeout_kind=None) -> None:
        with self._state_lock:
            self._f.update(running=False, finished_at=self.clock())
        status = "lease_lost" if timeout_kind == "lease_lost" else (
            f"timeout:{timeout_kind}" if timed_out else f"exit:{returncode}")
        self._flush("finish", force=True, status=status)

    def mark_lease_lost(self, reason: str = "") -> None:
        first = not self._lease_lost
        self._lease_lost = True
        if first:
            self._flush("LEASE_DEGRADED", force=True, status=_token(reason or "lease_lost"))

    def close(self, state: str | None = None) -> None:
        """Final flush (running=false) before the lease is released."""
        with self._state_lock:
            if state is not None:
                self._f["state"] = state
            if self._f["running"]:
                self._f["finished_at"] = self.clock()
            self._f["running"] = False
        self._flush("run_end", force=True)

    # ------------------------------------------------------------ snapshot
    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            f = dict(self._f)
        now = self.clock()
        return {
            "schema_version": SCHEMA_VERSION,
            "task_id": self.task_id,
            "generation": self.generation,
            "state": f["state"],
            "step": f["step"],
            "provider": f["provider"],
            "role": f["role"],
            "attempt_id": f["attempt_id"],
            "pid": f["pid"],
            "proc_start_time": f["proc_start_time"],
            "started_at": f["started_at"],
            "last_heartbeat_at": now,
            "last_output_at": f["last_output_at"],
            "bytes_seen": f["bytes_seen"],
            "redaction": f["redaction"],
            "running": bool(f["running"]),
            "finished_at": f["finished_at"],
            "lease_lost": bool(self._lease_lost),
        }

    def _log_line(self, event: str, snap: dict[str, Any], status: str | None) -> str:
        now = snap["last_heartbeat_at"]
        base = self._f.get("child_started_at") or snap["started_at"] or now
        elapsed = max(0, int(now - base))
        ts = iso_utc(now)
        if snap["redaction"] == REDACTION_HOLDOUT:
            st = status or ("running" if snap["running"] else "idle")
            if event == "LEASE_DEGRADED":
                return f"{ts} gen={self.generation} LEASE_DEGRADED step=holdout elapsed={elapsed}s"
            return f"{ts} gen={self.generation} event={_token(event)} step=holdout status={_token(st)} elapsed={elapsed}s"
        st = status or ("running" if snap["running"] else "idle")
        parts = [ts, f"gen={self.generation}"]
        parts.append("LEASE_DEGRADED" if event == "LEASE_DEGRADED" else f"event={_token(event)}")
        parts += [
            f"state={_token(snap['state'])}", f"step={_token(snap['step'])}",
            f"provider={_token(snap['provider'])}", f"role={_token(snap['role'])}",
            f"pid={_token(snap['pid'])}", f"bytes_seen={int(snap['bytes_seen'] or 0)}",
            f"elapsed={elapsed}s", f"status={_token(st)}",
        ]
        return " ".join(parts)

    def _append_log(self, line: str) -> None:
        buf = self._log_buf + line.encode("ascii", errors="replace") + b"\n"
        if len(buf) > self.max_log_bytes:
            buf = buf[-self.max_log_bytes:]
            nl = buf.find(b"\n")
            buf = buf[nl + 1:] if nl >= 0 else b""
        self._log_buf = buf

    def flush(self, event: str | None = None) -> bool:
        return self._flush(event, force=True)

    def _flush(self, event: str | None, force: bool = False, status: str | None = None) -> bool:
        """Serialized atomic write. Never raises (monitoring must not fail the worker)."""
        try:
            with self._io_lock:
                if self.generation < 1 or self.superseded:
                    return False
                existing = read_live_sidecar(self.artifacts_dir, self.task_id)
                if existing is not None and int(existing["generation"]) > self.generation:
                    self.superseded = True  # a newer run owns the sidecar: never overwrite it
                    return False
                snap = self.snapshot()
                self._last_flush = self._mono()
                if event is not None:
                    self._append_log(self._log_line(event, snap, status))
                    self._last_log = self._last_flush
                    self._write(self.log_path, self._log_buf)
                body = json.dumps(snap, ensure_ascii=True, sort_keys=True).encode("ascii")
                return self._write(self.json_path, body)
        except Exception:
            self.write_errors += 1
            return False


# ================================================================= lease keeper
class LeaseKeeper:
    """Daemon thread renewing the task lease with non-blocking (`busy_timeout=0`) heartbeats.

    Transient BUSY/LOCKED: skipped, retried next tick. Lost lease (rowcount 0) or failure that
    lasts past `expires_at - safety_margin`: `lease_lost` is latched, the sink records a
    LEASE_DEGRADED marker and `JobRunner` terminates the child (fail closed: no split brain).
    """

    def __init__(
        self,
        db_path: str | Path,
        task_id: str,
        owner_nonce: str,
        lease_ttl: float,
        *,
        clock: Callable[[], float] = time.time,
        sink: ProgressSink | None = None,
        interval: float | None = None,
        safety_margin: float | None = None,
        expires_at: float | None = None,
        heartbeat_fn: Callable[..., bool] = heartbeat_nowait,
    ) -> None:
        self.db_path = str(db_path)
        self.task_id = task_id
        self._nonce = owner_nonce
        self.ttl = float(lease_ttl)
        self.clock = clock
        self.sink = sink
        self.interval = interval if interval is not None else min(max(5.0, self.ttl / 6.0), 15.0)
        if safety_margin is None:
            safety_margin = min(max(2 * self.interval, self.ttl * 0.1), self.ttl / 2.0)
        self.safety_margin = float(safety_margin)
        self.expires_at = float(expires_at) if expires_at is not None else clock() + self.ttl
        self._heartbeat = heartbeat_fn
        self._lost = False
        self.busy_skips = 0
        self.errors = 0
        self.beats = 0
        self._tick_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __repr__(self) -> str:  # never leak the owner nonce
        return f"LeaseKeeper(task_id={self.task_id!r}, lost={self._lost})"

    def is_lease_lost(self) -> bool:
        return self._lost

    def _declare_lost(self, reason: str) -> None:
        if self._lost:
            return
        self._lost = True
        if self.sink is not None:
            try:
                self.sink.mark_lease_lost(reason)
            except Exception:
                pass

    def tick(self) -> bool:
        """One heartbeat attempt; returns True while the lease is held."""
        with self._tick_lock:
            if self._lost:
                return False
            now = self.clock()
            try:
                ok = self._heartbeat(self.db_path, self.task_id, self._nonce, self.ttl, now=now)
            except sqlite3.OperationalError as exc:
                if is_transient_db_error(exc):
                    self.busy_skips += 1
                else:
                    self.errors += 1
                ok = None
            except Exception:
                self.errors += 1
                ok = None
            if ok is True:
                self.beats += 1
                self.expires_at = now + self.ttl
                return True
            if ok is False:
                self._declare_lost("lease_taken")
                return False
            if now >= self.expires_at - self.safety_margin:
                self._declare_lost("heartbeat_expired")
                return False
            return True

    check_now = tick

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            if not self.tick():
                break

    def start(self) -> "LeaseKeeper":
        self._thread = threading.Thread(target=self._run, name=f"af-lease-{self.task_id[:16]}", daemon=True)
        self._thread.start()
        return self

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout)


# ========================================================== stage / liveness
WAITING_HUMAN_STATES = frozenset(
    {"AWAITING_START_AUTH", "AWAITING_MERGE", "SINGLE_PROVIDER_REVIEW_REQUIRED", "BLOCKED", "GUARDRAIL_BLOCKED"}
)
DEFAULT_STALE_AFTER = 120.0
DEFAULT_QUIET_AFTER = 60.0
CHILD_GRACE = 30.0


def stage_label(state: str) -> str:
    """Exact pipeline position. No fake percentages."""
    total = len(MAIN_FLOW)
    if state in MAIN_FLOW:
        return f"{MAIN_FLOW.index(state) + 1:02d}/{total} {state}"
    if state == "RETRY_WAIT":
        return f"09R/{total}"
    if state == "RECOVERY_PENDING":
        return f"08R/{total}"
    if state == "SINGLE_PROVIDER_REVIEW_REQUIRED":
        return "EXCEPTION/REVIEW"
    return f"EXCEPTION/{state}"


def _age(now: float, t: Any) -> float | None:
    if t is None or isinstance(t, bool):
        return None
    try:
        return max(0.0, float(now) - float(t))
    except (TypeError, ValueError):
        return None


def classify_liveness(
    state: str,
    lease: dict[str, Any] | None,
    live: dict[str, Any] | None,
    now: float,
    *,
    same_process: Callable[[Any, Any], bool] | None = None,
    stale_after: float = DEFAULT_STALE_AFTER,
    quiet_after: float = DEFAULT_QUIET_AFTER,
) -> str:
    """DONE | FAILED | WAITING_HUMAN | ACTIVE | QUIET | STALLED | QUEUED."""
    if same_process is None:
        from .win_job import is_same_process as same_process
    if state == "MERGED":
        return "DONE"
    if state in TERMINAL_STATES:
        return "FAILED"
    if state in WAITING_HUMAN_STATES:
        return "WAITING_HUMAN"
    if not lease:
        return "STALLED" if state in ACTIVE_STATES else "QUEUED"
    exp = lease.get("expires_at")
    if exp is not None and float(exp) < now:
        return "STALLED"  # lease expired: nobody is keeping it
    if not same_process(lease.get("pid"), lease.get("proc_start_time")):
        return "STALLED"  # owner dead, or PID reused by another process (start time differs)
    beats = [lease.get("heartbeat_at")]
    if live and not live.get("lease_lost"):
        beats.append(live.get("last_heartbeat_at"))
    ages = [a for a in (_age(now, b) for b in beats) if a is not None]
    if not ages or min(ages) > stale_after:
        return "STALLED"
    if live and live.get("lease_lost"):
        return "STALLED"
    if live and live.get("running"):
        child_ok = same_process(live.get("pid"), live.get("proc_start_time"))
        hb = _age(now, live.get("last_heartbeat_at"))
        if not child_ok and (hb is None or hb > CHILD_GRACE):
            return "STALLED"
        if live.get("redaction") == REDACTION_HOLDOUT:
            return "ACTIVE"  # holdout exposes no output signal; a live, beating gate is active
        out_age = _age(now, live.get("last_output_at") or live.get("started_at"))
        if out_age is not None and out_age > quiet_after:
            return "QUIET"  # silence != hung
    return "ACTIVE"


@dataclass
class RuntimeSnapshot:
    task_id: str
    state: str
    state_version: int
    risk_level: str
    stage: str
    liveness: str
    state_age: float | None
    elapsed: float | None
    attempt: str
    hb_age: float | None
    output_age: float | None
    step: str | None = None
    provider: str | None = None
    role: str | None = None
    redaction: str | None = None
    running: bool = False
    generation: int | None = None


def build_runtime_snapshot(
    row: dict[str, Any],
    live: dict[str, Any] | None,
    now: float,
    *,
    same_process: Callable[[Any, Any], bool] | None = None,
    stale_after: float = DEFAULT_STALE_AFTER,
    quiet_after: float = DEFAULT_QUIET_AFTER,
) -> RuntimeSnapshot:
    """Combine one `read_runtime_snapshot_readonly` row with its (validated) live sidecar."""
    lease = row.get("lease")
    if live is not None and live.get("task_id") != row["id"]:
        live = None
    liveness = classify_liveness(row["state"], lease, live, now, same_process=same_process,
                                 stale_after=stale_after, quiet_after=quiet_after)
    att = row.get("last_attempt")
    if att:
        attempt = f"#{att['seq']} {_token(att['provider'], 16)}/{_token(att['role'], 16)}"
        if att.get("finished_at") is None:
            attempt += " running"
        elif att.get("failure_code"):
            attempt += f" {_token(att['failure_code'], 24)}"
    else:
        attempt = "-"
    beats = []
    if lease:
        beats.append(lease.get("heartbeat_at"))
    if live:
        beats.append(live.get("last_heartbeat_at"))
    hb_ages = [a for a in (_age(now, b) for b in beats) if a is not None]
    holdout = bool(live and live.get("redaction") == REDACTION_HOLDOUT)
    out_age = None if (not live or holdout) else _age(now, live.get("last_output_at"))
    running = bool(live and live.get("running"))
    elapsed = _age(now, live.get("started_at")) if live and running else None
    return RuntimeSnapshot(
        task_id=row["id"], state=row["state"], state_version=int(row["state_version"]),
        risk_level=row.get("risk_level") or "-", stage=stage_label(row["state"]), liveness=liveness,
        state_age=_age(now, row.get("state_entered_at") or row.get("updated_at")), elapsed=elapsed,
        attempt=attempt, hb_age=min(hb_ages) if hb_ages else None, output_age=out_age,
        step=(live or {}).get("step"), provider=(live or {}).get("provider"), role=(live or {}).get("role"),
        redaction=(live or {}).get("redaction"), running=running, generation=(live or {}).get("generation"),
    )


def fmt_age(sec: float | None) -> str:
    if sec is None:
        return "-"
    s = int(sec)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


def runtime_snapshots(
    db_path: str | Path,
    artifacts_dir: str | Path | None,
    now: float,
    task_id: str | None = None,
    *,
    same_process: Callable[[Any, Any], bool] | None = None,
) -> tuple[list[RuntimeSnapshot], dict[str, Any]]:
    """Read-only: `mode=ro` DB rows + validated sidecars -> snapshots (and the raw DB summary)."""
    from .db import read_runtime_snapshot_readonly

    data = read_runtime_snapshot_readonly(db_path, task_id)
    out = []
    for row in data["tasks"]:
        live = read_live_sidecar(artifacts_dir, row["id"]) if TASK_ID_RE.match(row["id"]) else None
        out.append(build_runtime_snapshot(row, live, now, same_process=same_process))
    return out, data
