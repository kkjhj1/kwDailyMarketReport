"""SQLite Coordinator DB: WAL, CAS state machine, leases, quota, approvals, circuit.

Every state change is a compare-and-swap on `tasks.state_version` and writes its
`events` row inside the same `BEGIN IMMEDIATE` transaction.

Step 2 additions (dev bot / merge gateway):
- `tg_outbox`: notifications are enqueued in the *same* transaction as the state
  transition that causes them (no lost / phantom notifications). Delivery is
  at-most-once: only HTTP 429 is retried; SENT / UNKNOWN / EXPIRED are never resent.
- `tg_actions`: server-side single-use callback tokens bound to chat/user, op,
  task state + state_version + target_sha + diff_sha256 and an expiry.
- `bot_kv`: small CAS key/value store (poller lease, update offset, notify chat id).
- `merge_intents`: journal that makes the Git merge + DB `MERGED` transition crash-safe.
  `transition(..., "MERGED")` is refused; only `finalize_merge()` may enter MERGED.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterable

MAIN_FLOW = (
    "RECEIVED",
    "PREFLIGHTED",
    "CLASSIFIED",
    "PLANNING",
    "AUDITED",
    "PLAN_READY",
    "AWAITING_START_AUTH",
    "WORKTREE_READY",
    "IMPLEMENTING",
    "CHECKPOINTED",
    "VERIFYING",
    "REVIEWING",
    "AWAITING_MERGE",
    "MERGED",
)
EXCEPTION_STATES = (
    "RETRY_WAIT",
    "RECOVERY_PENDING",
    "BLOCKED",
    "FAILED",
    "DISCARDED",
    "CANCELLED",
    "GUARDRAIL_BLOCKED",
    "SINGLE_PROVIDER_REVIEW_REQUIRED",
)
ALL_STATES = MAIN_FLOW + EXCEPTION_STATES
TERMINAL_STATES = frozenset({"MERGED", "FAILED", "DISCARDED", "CANCELLED"})
# States in which a worker is (or was) actively mutating a worktree.
ACTIVE_STATES = frozenset(
    {"WORKTREE_READY", "IMPLEMENTING", "CHECKPOINTED", "VERIFYING", "REVIEWING", "RETRY_WAIT"}
)
# States that wait for a human; the worker never advances them on its own.
HUMAN_GATED_STATES = frozenset(
    {"BLOCKED", "GUARDRAIL_BLOCKED", "SINGLE_PROVIDER_REVIEW_REQUIRED", "AWAITING_MERGE"}
)
RISK_LEVELS = ("L0", "L1", "L2", "L3")
AUDIT_REQUIRED_LEVELS = frozenset({"L2", "L3"})
HUMAN_ONLY_EDGES = frozenset(
    {("SINGLE_PROVIDER_REVIEW_REQUIRED", "AWAITING_MERGE"), ("AWAITING_MERGE", "MERGED")}
)
TASK_FIELDS = frozenset(
    {
        "base_sha",
        "target_sha",
        "diff_sha256",
        "worktree_path",
        "branch",
        "checkpoint_sha",
        "coder",
        "single_provider",
        "retry_count",
        "recovery_from",
        "last_error",
    }
)
APPROVAL_DECISIONS = ("start", "manual_review", "merge", "discard")
# Entering one of these states enqueues an admin notification (same transaction).
NOTIFY_STATES = frozenset(
    {"AWAITING_START_AUTH", "AWAITING_MERGE", "SINGLE_PROVIDER_REVIEW_REQUIRED", "BLOCKED",
     "GUARDRAIL_BLOCKED", "FAILED"}
)
OUTBOX_STATUSES = ("PENDING", "SENT", "UNKNOWN", "FAILED", "EXPIRED")
OUTBOX_TTL = 6 * 3600.0
# Callback ops: lower-case = prompt / read-only, upper-case = mutating confirmation.
ACTION_OPS = frozenset("scdrRmMxX")
READ_ONLY_OPS = frozenset("d")  # may be re-used until expiry
ACTION_OPS_BY_STATE: dict[str, frozenset[str]] = {
    "AWAITING_START_AUTH": frozenset("sc"),
    "SINGLE_PROVIDER_REVIEW_REQUIRED": frozenset("drRxX"),
    "AWAITING_MERGE": frozenset("dmMxX"),
    "BLOCKED": frozenset("dxX"),
    "GUARDRAIL_BLOCKED": frozenset("dxX"),
}
ACTION_TTL = 3600.0
MERGE_INTENT_STATUSES = ("PREPARING", "GIT_MERGED", "COMPLETED", "ABORTED", "BLOCKED", "ORPHAN_CLEANUP")
OPEN_MERGE_INTENT = ("PREPARING", "GIT_MERGED")


def _build_transitions() -> dict[str, frozenset[str]]:
    edges: dict[str, set[str]] = {s: set() for s in ALL_STATES}
    for src, dst in zip(MAIN_FLOW, MAIN_FLOW[1:]):
        edges[src].add(dst)
    for s in ALL_STATES:
        if s not in TERMINAL_STATES:
            edges[s] |= {"BLOCKED", "FAILED", "CANCELLED", "DISCARDED"}
    for s in ACTIVE_STATES:
        edges[s].add("RECOVERY_PENDING")
    edges["CHECKPOINTED"].add("GUARDRAIL_BLOCKED")
    edges["VERIFYING"] |= {"RETRY_WAIT", "GUARDRAIL_BLOCKED", "SINGLE_PROVIDER_REVIEW_REQUIRED"}
    edges["REVIEWING"].add("SINGLE_PROVIDER_REVIEW_REQUIRED")
    edges["RETRY_WAIT"].add("IMPLEMENTING")
    edges["RECOVERY_PENDING"] |= {"WORKTREE_READY", "CHECKPOINTED"}
    edges["SINGLE_PROVIDER_REVIEW_REQUIRED"].add("AWAITING_MERGE")
    for s in ALL_STATES:
        edges[s].discard(s)
    return {s: frozenset(v) for s, v in edges.items()}


TRANSITIONS = _build_transitions()

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    request_hash TEXT NOT NULL UNIQUE,
    request TEXT NOT NULL,
    risk_level TEXT NOT NULL,
    state TEXT NOT NULL,
    state_version INTEGER NOT NULL DEFAULT 0,
    allowed_paths TEXT NOT NULL,
    acceptance_spec TEXT NOT NULL,
    base_sha TEXT, target_sha TEXT, diff_sha256 TEXT,
    worktree_path TEXT, branch TEXT, checkpoint_sha TEXT,
    coder TEXT, single_provider INTEGER NOT NULL DEFAULT 0,
    retry_count INTEGER NOT NULL DEFAULT 0,
    recovery_from TEXT, last_error TEXT,
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    seq INTEGER NOT NULL,
    provider TEXT NOT NULL, role TEXT NOT NULL,
    input_digest TEXT, output_digest TEXT, checkpoint_sha TEXT,
    failure_code TEXT, traceback_hash TEXT, fail_count INTEGER,
    started_at REAL NOT NULL, finished_at REAL,
    UNIQUE(task_id, seq)
);
CREATE TABLE IF NOT EXISTS leases (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id),
    owner_nonce TEXT NOT NULL,
    pid INTEGER, proc_start_time REAL,
    heartbeat_at REAL NOT NULL, expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS provider_budget (
    provider TEXT PRIMARY KEY,
    window_start REAL NOT NULL,
    window_seconds REAL NOT NULL,
    capacity_units REAL NOT NULL,
    reserved_units REAL NOT NULL DEFAULT 0,
    consumed_units REAL NOT NULL DEFAULT 0,
    health TEXT NOT NULL DEFAULT 'ok',
    retry_after REAL
);
CREATE TABLE IF NOT EXISTS artifacts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    kind TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT,
    producer TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    from_state TEXT, to_state TEXT NOT NULL,
    actor TEXT NOT NULL, reason TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    decision TEXT NOT NULL,
    target_sha TEXT, diff_sha256 TEXT, review_digest TEXT,
    expires_at REAL, created_at REAL NOT NULL,
    actor_user_id INTEGER, actor_chat_id INTEGER, action_token TEXT, idempotency_key TEXT
);
CREATE TABLE IF NOT EXISTS circuit_state (
    name TEXT PRIMARY KEY,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    open_until REAL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tg_outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT NOT NULL UNIQUE,
    chat_id INTEGER NOT NULL,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('PENDING','SENT','UNKNOWN','FAILED','EXPIRED')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_retry_at REAL NOT NULL,
    last_error TEXT,
    lease_owner TEXT,
    lease_until REAL,
    boot_id TEXT,
    sent_at REAL,
    unknown_at REAL,
    expires_at REAL NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS tg_actions (
    token TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    action TEXT NOT NULL,
    expected_state TEXT NOT NULL,
    expected_state_version INTEGER NOT NULL,
    target_sha TEXT,
    diff_sha256 TEXT,
    chat_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    used_at REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS bot_kv (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS merge_intents (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    base_sha TEXT NOT NULL,
    target_sha TEXT NOT NULL,
    diff_sha256 TEXT NOT NULL,
    actor TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN
        ('PREPARING','GIT_MERGED','COMPLETED','ABORTED','BLOCKED','ORPHAN_CLEANUP')),
    error TEXT,
    created_at REAL,
    updated_at REAL
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tg_outbox_due ON tg_outbox(status, next_retry_at);
CREATE INDEX IF NOT EXISTS idx_merge_intents_task ON merge_intents(task_id, status);
"""
# Columns added to `approvals` in Step 2 (idempotent ALTER TABLE for Step 1 databases).
_APPROVAL_MIGRATIONS = (
    ("actor_user_id", "INTEGER"),
    ("actor_chat_id", "INTEGER"),
    ("action_token", "TEXT"),
    ("idempotency_key", "TEXT"),
)
_POST_MIGRATION = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_approvals_idem ON approvals(idempotency_key)
    WHERE idempotency_key IS NOT NULL;
"""


class CoordinatorError(RuntimeError):
    pass


class InvalidTransition(CoordinatorError):
    pass


class GuardViolation(InvalidTransition):
    pass


class CASConflict(CoordinatorError):
    pass


class ActionRejected(CoordinatorError):
    """A callback token failed validation; nothing was mutated. `code` is a short reason."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class MergeIntentError(CoordinatorError):
    pass


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


TX_RETRY_BUDGET = 2.0


def is_transient_db_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


# ------------------------------------------------------------ read-only view
_REQUIRED_TABLES = frozenset({"tasks", "events", "approvals", "tg_outbox", "merge_intents"})


def read_db_summary_readonly(db_path: str | Path) -> dict[str, Any]:
    """Inspect an EXISTING coordinator DB via `mode=ro` (no creation, schema or migration)."""
    p = Path(db_path)
    if not p.is_file():
        raise FileNotFoundError(f"coordinator DB not found: {p}")
    conn = sqlite3.connect(p.resolve().as_uri() + "?mode=ro", uri=True, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA query_only=1")
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        missing = _REQUIRED_TABLES - tables
        if missing:
            raise ValueError(f"not a coordinator DB (missing tables: {sorted(missing)})")
        by_state = {r["state"]: r["n"] for r in conn.execute("SELECT state, COUNT(*) AS n FROM tasks GROUP BY state")}
        tasks = [dict(r) for r in conn.execute(
            "SELECT id, risk_level, state, state_version FROM tasks ORDER BY created_at, id")]
        outbox = {s: 0 for s in OUTBOX_STATUSES}
        for r in conn.execute("SELECT status, COUNT(*) AS n FROM tg_outbox GROUP BY status"):
            outbox[r["status"]] = r["n"]
        open_intents = conn.execute(
            "SELECT COUNT(*) FROM merge_intents WHERE status IN ('PREPARING','GIT_MERGED')").fetchone()[0]
        return {"tasks": tasks, "by_state": by_state, "outbox": outbox, "open_merge_intents": open_intents}
    finally:
        conn.close()


# ---------------------------------------------------- Phase 1 artifact integrity
AUDITOR_PROVIDERS = ("codex", "gemini")  # claude is never an auditor
AUDIT_KEYS = frozenset({"schema_version", "task_id", "verdict", "summary", "base_sha", "allowed_paths", "auditor"})
PLAN_KEYS = frozenset({"schema_version", "task_id", "base_sha", "allowed_paths", "acceptance_tests", "request",
                       "auditor", "audit_sha256"})
MAX_ARTIFACT_BYTES = 256 * 1024


class ArtifactIntegrityError(CoordinatorError):
    pass


def _load_bound_json(row: dict[str, Any], keys: frozenset[str]) -> tuple[dict[str, Any], str]:
    from .worktree import UnsafePathError, safe_read_bytes

    try:
        raw = safe_read_bytes(row["path"], MAX_ARTIFACT_BYTES)
    except (OSError, UnsafePathError) as exc:
        raise ArtifactIntegrityError(f"{row['kind']} artifact unreadable: {type(exc).__name__}") from None
    digest = hashlib.sha256(raw).hexdigest()
    if not row.get("sha256") or digest != row["sha256"]:
        raise ArtifactIntegrityError(f"{row['kind']} artifact sha256 mismatch (tampered)")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ArtifactIntegrityError(f"{row['kind']} artifact is not JSON") from None
    if not isinstance(data, dict) or set(data) != keys:
        raise ArtifactIntegrityError(f"{row['kind']} artifact schema mismatch")
    if data["schema_version"] != 1 or isinstance(data["schema_version"], bool):
        raise ArtifactIntegrityError(f"{row['kind']} artifact schema_version must be 1")
    return data, digest


def verify_phase1_artifacts(
    db: "CoordinatorDB", task: dict[str, Any], writer: str | None = None
) -> dict[str, Any]:
    """Strict on-disk verification of the latest `audit` + `plan` artifacts for `task`.

    Checks sha256 binding to the DB row, exact JSON schema, task_id/base_sha/allowed_paths
    binding, verdict == approve, auditor in (codex, gemini) and `auditor != writer`.
    """
    audits, plans = db.artifacts(task["id"], "audit"), db.artifacts(task["id"], "plan")
    if not audits or not plans:
        raise ArtifactIntegrityError("AUDIT_INCOMPLETE: plan+audit artifacts required")
    audit, audit_sha = _load_bound_json(audits[-1], AUDIT_KEYS)
    plan, plan_sha = _load_bound_json(plans[-1], PLAN_KEYS)
    for name, data in (("audit", audit), ("plan", plan)):
        if data["task_id"] != task["id"]:
            raise ArtifactIntegrityError(f"{name} task_id mismatch")
        if data["base_sha"] != task["base_sha"]:
            raise ArtifactIntegrityError(f"{name} base_sha mismatch")
        if data["allowed_paths"] != task["allowed_paths"]:
            raise ArtifactIntegrityError(f"{name} allowed_paths mismatch")
    if audit["verdict"] != "approve":
        raise ArtifactIntegrityError("audit verdict is not approve")
    if not isinstance(audit["summary"], str) or len(audit["summary"]) > 300:
        raise ArtifactIntegrityError("audit summary invalid")
    auditor = audit["auditor"]
    if auditor not in AUDITOR_PROVIDERS or plan["auditor"] != auditor or audits[-1]["producer"] != auditor:
        raise ArtifactIntegrityError("auditor identity invalid (claude may never audit)")
    if plan["audit_sha256"] != audit_sha:
        raise ArtifactIntegrityError("plan is not bound to the audit digest")
    if writer is not None and writer == auditor:
        raise ArtifactIntegrityError("auditor may not be the writer")
    return {"auditor": auditor, "audit_sha256": audit_sha, "plan_sha256": plan_sha}


class CoordinatorDB:
    def __init__(
        self,
        path: str | Path,
        clock: Callable[[], float] = time.time,
        notify_chat_id: int | None = None,
        boot_id: str | None = None,
    ):
        self.path = str(path)
        self.clock = clock
        # Admin chat for outbox notifications. If None, falls back to bot_kv["notify_chat_id"]
        # (written by the dev bot) so a separate worker process enqueues too.
        self.notify_chat_id = notify_chat_id
        self.boot_id = boot_id or uuid.uuid4().hex
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(
            self.path, timeout=5.0, isolation_level=None, check_same_thread=False
        )
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self.journal_mode = self._conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        # executescript commits implicitly, so it runs outside an explicit tx.
        self._conn.executescript(_SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        """Idempotent Step 1 -> Step 2 migration (adds nullable approvals columns)."""
        with self._tx() as c:
            cols = {r["name"] for r in c.execute("PRAGMA table_info(approvals)").fetchall()}
            for name, typ in _APPROVAL_MIGRATIONS:
                if name not in cols:
                    c.execute(f"ALTER TABLE approvals ADD COLUMN {name} {typ}")
        self._conn.executescript(_POST_MIGRATION)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _begin_immediate(self) -> None:
        """BEGIN IMMEDIATE with bounded exponential backoff on transient lock/busy errors.

        Only the acquisition is retried; nothing inside the transaction body ever is.
        """
        deadline = time.monotonic() + TX_RETRY_BUDGET
        delay = 0.02
        while True:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                return
            except sqlite3.OperationalError as exc:
                if not is_transient_db_error(exc) or time.monotonic() + delay > deadline:
                    raise
                time.sleep(delay)
                delay = min(delay * 2, 0.4)

    @contextmanager
    def _tx(self):
        with self._lock:
            self._begin_immediate()
            try:
                yield self._conn
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            else:
                self._conn.execute("COMMIT")

    def _query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).fetchall()

    # ------------------------------------------------------------------ tasks
    def create_task(
        self,
        request: str,
        risk_level: str,
        allowed_paths: list[str],
        acceptance_spec: dict[str, Any],
        idempotency_key: str | None = None,
        task_id: str | None = None,
    ) -> tuple[str, bool]:
        if risk_level not in RISK_LEVELS:
            raise ValueError(f"unknown risk level: {risk_level}")
        canonical = json.dumps(
            [request, risk_level, sorted(allowed_paths), acceptance_spec], sort_keys=True
        )
        request_hash = sha256_text(idempotency_key or canonical)
        tid = task_id or f"t{request_hash[:12]}"
        now = self.clock()
        with self._tx() as c:
            row = c.execute("SELECT id FROM tasks WHERE request_hash=?", (request_hash,)).fetchone()
            if row:
                return row["id"], False
            c.execute(
                "INSERT INTO tasks (id, request_hash, request, risk_level, state, state_version,"
                " allowed_paths, acceptance_spec, created_at, updated_at)"
                " VALUES (?,?,?,?, 'RECEIVED', 0, ?,?,?,?)",
                (
                    tid,
                    request_hash,
                    request,
                    risk_level,
                    json.dumps(allowed_paths),
                    json.dumps(acceptance_spec),
                    now,
                    now,
                ),
            )
            c.execute(
                "INSERT INTO events (task_id, from_state, to_state, actor, reason, created_at)"
                " VALUES (?, NULL, 'RECEIVED', 'intake', 'task created', ?)",
                (tid, now),
            )
        return tid, True

    @staticmethod
    def _task_dict(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["allowed_paths"] = json.loads(d["allowed_paths"])
        d["acceptance_spec"] = json.loads(d["acceptance_spec"])
        d["single_provider"] = bool(d["single_provider"])
        return d

    def get_task(self, task_id: str) -> dict[str, Any]:
        rows = self._query("SELECT * FROM tasks WHERE id=?", (task_id,))
        if not rows:
            raise KeyError(task_id)
        return self._task_dict(rows[0])

    def list_tasks(self, states: Iterable[str] | None = None) -> list[dict[str, Any]]:
        if states is None:
            rows = self._query("SELECT * FROM tasks ORDER BY created_at, id")
        else:
            states = list(states)
            marks = ",".join("?" * len(states))
            rows = self._query(
                f"SELECT * FROM tasks WHERE state IN ({marks}) ORDER BY created_at, id", states
            )
        return [self._task_dict(r) for r in rows]

    def _check_guards(
        self,
        c: sqlite3.Connection,
        task: sqlite3.Row,
        to_state: str,
        actor: str,
        fields: dict[str, Any],
        merge_intent: sqlite3.Row | None = None,
    ):
        src = task["state"]
        now = self.clock()
        if (src, to_state) in HUMAN_ONLY_EDGES and not actor.startswith("human"):
            raise GuardViolation(f"{src}->{to_state} requires a human actor")
        if to_state == "PLAN_READY" and task["risk_level"] in AUDIT_REQUIRED_LEVELS:
            if not c.execute(
                "SELECT 1 FROM artifacts WHERE task_id=? AND kind='audit'", (task["id"],)
            ).fetchone():
                raise GuardViolation("AUDIT_INCOMPLETE: audit artifact required before PLAN_READY")
        if src == "AWAITING_START_AUTH" and to_state == "WORKTREE_READY":
            if not self._approval_ok(c, task["id"], "start", now):
                raise GuardViolation("start authorization missing or expired")
        if (src, to_state) in HUMAN_ONLY_EDGES and (
            fields.get("target_sha", task["target_sha"]) != task["target_sha"]
            or fields.get("diff_sha256", task["diff_sha256"]) != task["diff_sha256"]
        ):
            raise GuardViolation("target_sha/diff_sha256 may not change on a human-approval edge")
        if src == "SINGLE_PROVIDER_REVIEW_REQUIRED" and to_state == "AWAITING_MERGE":
            # Manual code review is a separate, SHA-bound human approval for every risk level
            # (the Phase 1 architecture audit is not an independent review of this diff).
            if not task["target_sha"] or not task["diff_sha256"]:
                raise GuardViolation("single-provider candidate has no target_sha/diff_sha256")
            if not self._approval_ok(
                c, task["id"], "manual_review", now, task["target_sha"], task["diff_sha256"]
            ):
                raise GuardViolation("manual_review approval missing or not bound to current target/diff")
        if to_state == "MERGED":
            if merge_intent is None:
                raise GuardViolation("use finalize_merge() via merge coordinator")
            if (
                merge_intent["task_id"] != task["id"]
                or merge_intent["status"] != "GIT_MERGED"
                or merge_intent["target_sha"] != task["target_sha"]
                or merge_intent["diff_sha256"] != task["diff_sha256"]
            ):
                raise GuardViolation("merge intent is not git-verified for the current target/diff")
            # Expiry was enforced when the intent began; once Git has merged we must record it.
            if not self._approval_ok(
                c, task["id"], "merge", None, task["target_sha"], task["diff_sha256"]
            ):
                raise GuardViolation("merge approval missing or not bound to current target/diff")

    def _notify_chat(self, c: sqlite3.Connection) -> int | None:
        if self.notify_chat_id:
            return int(self.notify_chat_id)
        row = c.execute("SELECT v FROM bot_kv WHERE k='notify_chat_id'").fetchone()
        try:
            return int(row["v"]) if row and int(row["v"]) != 0 else None
        except (TypeError, ValueError):
            return None

    def _enqueue_state_notification(
        self, c: sqlite3.Connection, task: sqlite3.Row, to_state: str, version: int, reason: str, now: float
    ) -> None:
        chat_id = self._notify_chat(c)
        if chat_id is None or to_state not in NOTIFY_STATES:
            return
        payload = {
            "task_id": task["id"],
            "state": to_state,
            "state_version": version,
            "risk_level": task["risk_level"],
            "reason": (reason or "")[:300],
            "request": task["request"][:300],
        }
        # boot_id stays NULL until a dispatcher claims the row (see expire_stale_outbox_on_boot).
        c.execute(
            "INSERT OR IGNORE INTO tg_outbox (idempotency_key, chat_id, task_id, kind, payload_json,"
            " status, attempts, next_retry_at, expires_at, created_at, updated_at)"
            " VALUES (?,?,?,?,?, 'PENDING', 0, ?,?,?,?)",
            (
                f"af2:{task['id']}:{version}:{to_state}",
                chat_id,
                task["id"],
                "state",
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                now,
                now + OUTBOX_TTL,
                now,
                now,
            ),
        )

    def _transition_in_tx(
        self,
        c: sqlite3.Connection,
        task_id: str,
        expected_version: int,
        to_state: str,
        actor: str,
        reason: str = "",
        fields: dict[str, Any] | None = None,
        merge_intent: sqlite3.Row | None = None,
    ) -> int:
        """CAS transition + event + outbox row, all inside the caller's transaction."""
        if to_state not in TRANSITIONS:
            raise InvalidTransition(f"unknown state {to_state}")
        fields = dict(fields or {})
        bad = set(fields) - TASK_FIELDS
        if bad:
            raise ValueError(f"non-updatable fields: {sorted(bad)}")
        now = self.clock()
        task = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if task is None:
            raise KeyError(task_id)
        if task["state_version"] != expected_version:
            raise CASConflict(
                f"{task_id}: expected v{expected_version}, found v{task['state_version']}"
            )
        src = task["state"]
        if to_state not in TRANSITIONS[src]:
            raise InvalidTransition(f"{src} -> {to_state} not allowed")
        self._check_guards(c, task, to_state, actor, fields, merge_intent)
        sets = ["state=?", "state_version=state_version+1", "updated_at=?"]
        params: list[Any] = [to_state, now]
        for k, v in fields.items():
            sets.append(f"{k}=?")
            params.append(int(v) if isinstance(v, bool) else v)
        params += [task_id, expected_version]
        cur = c.execute(
            f"UPDATE tasks SET {', '.join(sets)} WHERE id=? AND state_version=?", params
        )
        if cur.rowcount != 1:
            raise CASConflict(f"{task_id}: concurrent update")
        c.execute(
            "INSERT INTO events (task_id, from_state, to_state, actor, reason, created_at)"
            " VALUES (?,?,?,?,?,?)",
            (task_id, src, to_state, actor, reason, now),
        )
        self._enqueue_state_notification(c, task, to_state, expected_version + 1, reason, now)
        return expected_version + 1

    def transition(
        self,
        task_id: str,
        expected_version: int,
        to_state: str,
        actor: str,
        reason: str = "",
        fields: dict[str, Any] | None = None,
    ) -> int:
        with self._tx() as c:
            return self._transition_in_tx(c, task_id, expected_version, to_state, actor, reason, fields)

    def update_fields(
        self, task_id: str, expected_version: int, fields: dict[str, Any], actor: str, reason: str = ""
    ) -> int:
        """CAS field update without a state change; still journaled in `events`."""
        bad = set(fields) - TASK_FIELDS
        if bad or not fields:
            raise ValueError(f"non-updatable fields: {sorted(bad)}")
        now = self.clock()
        with self._tx() as c:
            task = c.execute("SELECT state FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(task_id)
            sets = ["state_version=state_version+1", "updated_at=?"]
            params: list[Any] = [now]
            for k, v in fields.items():
                sets.append(f"{k}=?")
                params.append(int(v) if isinstance(v, bool) else v)
            cur = c.execute(
                f"UPDATE tasks SET {', '.join(sets)} WHERE id=? AND state_version=?",
                params + [task_id, expected_version],
            )
            if cur.rowcount != 1:
                raise CASConflict(f"{task_id}: expected v{expected_version}")
            c.execute(
                "INSERT INTO events (task_id, from_state, to_state, actor, reason, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (task_id, task["state"], task["state"], actor, reason or f"fields: {sorted(fields)}", now),
            )
        return expected_version + 1

    def force_release_lease(self, task_id: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM leases WHERE task_id=?", (task_id,))

    def events(self, task_id: str) -> list[dict[str, Any]]:
        return [
            dict(r)
            for r in self._query("SELECT * FROM events WHERE task_id=? ORDER BY id", (task_id,))
        ]

    # --------------------------------------------------------------- attempts
    def add_attempt(self, task_id: str, provider: str, role: str, input_digest: str = "") -> int:
        with self._tx() as c:
            seq = c.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 FROM attempts WHERE task_id=?", (task_id,)
            ).fetchone()[0]
            cur = c.execute(
                "INSERT INTO attempts (task_id, seq, provider, role, input_digest, started_at)"
                " VALUES (?,?,?,?,?,?)",
                (task_id, seq, provider, role, input_digest, self.clock()),
            )
            return int(cur.lastrowid)

    def finish_attempt(
        self,
        attempt_id: int,
        failure_code: str | None = None,
        traceback_hash: str | None = None,
        output_digest: str | None = None,
        checkpoint_sha: str | None = None,
        fail_count: int | None = None,
    ) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE attempts SET failure_code=?, traceback_hash=?, output_digest=?,"
                " checkpoint_sha=?, fail_count=?, finished_at=? WHERE id=?",
                (
                    failure_code,
                    traceback_hash,
                    output_digest,
                    checkpoint_sha,
                    fail_count,
                    self.clock(),
                    attempt_id,
                ),
            )

    def attempts(self, task_id: str, role: str | None = None) -> list[dict[str, Any]]:
        if role is None:
            rows = self._query("SELECT * FROM attempts WHERE task_id=? ORDER BY seq", (task_id,))
        else:
            rows = self._query(
                "SELECT * FROM attempts WHERE task_id=? AND role=? ORDER BY seq", (task_id, role)
            )
        return [dict(r) for r in rows]

    # -------------------------------------------------------------- artifacts
    def add_artifact(
        self, task_id: str, kind: str, path: str | Path, producer: str, sha256: str | None = None
    ) -> int:
        if sha256 is None and Path(path).is_file():
            sha256 = sha256_file(path)
        with self._tx() as c:
            cur = c.execute(
                "INSERT INTO artifacts (task_id, kind, path, sha256, producer, created_at)"
                " VALUES (?,?,?,?,?,?)",
                (task_id, kind, str(path), sha256, producer, self.clock()),
            )
            return int(cur.lastrowid)

    def artifacts(self, task_id: str, kind: str | None = None) -> list[dict[str, Any]]:
        if kind is None:
            rows = self._query("SELECT * FROM artifacts WHERE task_id=? ORDER BY id", (task_id,))
        else:
            rows = self._query(
                "SELECT * FROM artifacts WHERE task_id=? AND kind=? ORDER BY id", (task_id, kind)
            )
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------- leases
    def acquire_lease(
        self,
        task_id: str,
        owner_nonce: str,
        pid: int | None,
        proc_start_time: float | None,
        ttl: float = 300.0,
    ) -> bool:
        now = self.clock()
        with self._tx() as c:
            row = c.execute("SELECT * FROM leases WHERE task_id=?", (task_id,)).fetchone()
            if row and row["owner_nonce"] != owner_nonce and row["expires_at"] > now:
                return False
            c.execute(
                "INSERT OR REPLACE INTO leases (task_id, owner_nonce, pid, proc_start_time,"
                " heartbeat_at, expires_at) VALUES (?,?,?,?,?,?)",
                (task_id, owner_nonce, pid, proc_start_time, now, now + ttl),
            )
            return True

    def heartbeat(self, task_id: str, owner_nonce: str, ttl: float = 300.0) -> bool:
        now = self.clock()
        with self._tx() as c:
            cur = c.execute(
                "UPDATE leases SET heartbeat_at=?, expires_at=? WHERE task_id=? AND owner_nonce=?",
                (now, now + ttl, task_id, owner_nonce),
            )
            return cur.rowcount == 1

    def release_lease(self, task_id: str, owner_nonce: str) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM leases WHERE task_id=? AND owner_nonce=?", (task_id, owner_nonce))

    def get_lease(self, task_id: str) -> dict[str, Any] | None:
        rows = self._query("SELECT * FROM leases WHERE task_id=?", (task_id,))
        return dict(rows[0]) if rows else None

    # ----------------------------------------------------------------- budget
    def ensure_provider(
        self, provider: str, capacity_units: float, window_seconds: float = 5 * 3600
    ) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO provider_budget (provider, window_start, window_seconds,"
                " capacity_units) VALUES (?,?,?,?)",
                (provider, self.clock(), window_seconds, capacity_units),
            )

    def _roll_window(self, c: sqlite3.Connection, provider: str, now: float) -> sqlite3.Row:
        row = c.execute("SELECT * FROM provider_budget WHERE provider=?", (provider,)).fetchone()
        if row is None:
            raise KeyError(f"provider not registered: {provider}")
        if now - row["window_start"] >= row["window_seconds"]:
            c.execute(
                "UPDATE provider_budget SET window_start=?, consumed_units=0 WHERE provider=?",
                (now, provider),
            )
            row = c.execute("SELECT * FROM provider_budget WHERE provider=?", (provider,)).fetchone()
        return row

    def reserve(self, provider: str, units: float, ratio: float = 0.7) -> bool:
        """Atomically reserve quota; automation may use at most `ratio` of capacity."""
        now = self.clock()
        with self._tx() as c:
            row = self._roll_window(c, provider, now)
            if row["health"] != "ok" and (row["retry_after"] or 0) > now:
                return False
            used = row["reserved_units"] + row["consumed_units"]
            if used + units > row["capacity_units"] * ratio + 1e-9:
                return False
            c.execute(
                "UPDATE provider_budget SET reserved_units=reserved_units+? WHERE provider=?",
                (units, provider),
            )
            return True

    def can_reserve(self, provider: str, units: float, ratio: float = 0.7) -> bool:
        now = self.clock()
        with self._tx() as c:
            row = self._roll_window(c, provider, now)
            if row["health"] != "ok" and (row["retry_after"] or 0) > now:
                return False
            used = row["reserved_units"] + row["consumed_units"]
            return used + units <= row["capacity_units"] * ratio + 1e-9

    def settle(self, provider: str, reserved: float, consumed: float) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE provider_budget SET reserved_units=MAX(0, reserved_units-?),"
                " consumed_units=consumed_units+? WHERE provider=?",
                (reserved, consumed, provider),
            )

    def set_provider_health(
        self, provider: str, health: str, retry_after: float | None = None
    ) -> None:
        with self._tx() as c:
            c.execute(
                "UPDATE provider_budget SET health=?, retry_after=? WHERE provider=?",
                (health, retry_after, provider),
            )

    def provider_status(self, provider: str) -> dict[str, Any]:
        rows = self._query("SELECT * FROM provider_budget WHERE provider=?", (provider,))
        if not rows:
            raise KeyError(provider)
        return dict(rows[0])

    # -------------------------------------------------------------- approvals
    def add_approval(
        self,
        task_id: str,
        decision: str,
        target_sha: str | None = None,
        diff_sha256: str | None = None,
        review_digest: str | None = None,
        ttl: float | None = None,
        actor_user_id: int | None = None,
        actor_chat_id: int | None = None,
        action_token: str | None = None,
        idempotency_key: str | None = None,
    ) -> int:
        """Record an approval. Same `idempotency_key` twice returns the original row id."""
        with self._tx() as c:
            return self._insert_approval(
                c, task_id, decision, target_sha, diff_sha256, review_digest, ttl,
                actor_user_id, actor_chat_id, action_token, idempotency_key,
            )

    def _insert_approval(
        self,
        c: sqlite3.Connection,
        task_id: str,
        decision: str,
        target_sha: str | None = None,
        diff_sha256: str | None = None,
        review_digest: str | None = None,
        ttl: float | None = None,
        actor_user_id: int | None = None,
        actor_chat_id: int | None = None,
        action_token: str | None = None,
        idempotency_key: str | None = None,
    ) -> int:
        if decision not in APPROVAL_DECISIONS:
            raise ValueError(f"unknown approval decision: {decision!r}")
        now = self.clock()
        if idempotency_key is not None:
            row = c.execute(
                "SELECT id, task_id, decision FROM approvals WHERE idempotency_key=?", (idempotency_key,)
            ).fetchone()
            if row:
                if (row["task_id"], row["decision"]) != (task_id, decision):
                    raise GuardViolation("approval idempotency_key reused for a different decision")
                return int(row["id"])
        cur = c.execute(
            "INSERT INTO approvals (task_id, decision, target_sha, diff_sha256, review_digest,"
            " expires_at, created_at, actor_user_id, actor_chat_id, action_token, idempotency_key)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                task_id, decision, target_sha, diff_sha256, review_digest,
                now + ttl if ttl else None, now,
                actor_user_id, actor_chat_id, action_token, idempotency_key,
            ),
        )
        return int(cur.lastrowid)

    @staticmethod
    def _approval_ok(
        c: sqlite3.Connection,
        task_id: str,
        decision: str,
        now: float | None,
        target_sha: str | None = None,
        diff_sha256: str | None = None,
        bound: bool = False,
    ) -> bool:
        """Unexpired approval of `decision`; SHA-bound when target/diff are given.

        `now=None` skips the expiry check (only used once Git has already merged).
        """
        sql = "SELECT 1 FROM approvals WHERE task_id=? AND decision=?"
        params: list[Any] = [task_id, decision]
        if target_sha is not None or diff_sha256 is not None or bound:
            if not target_sha or not diff_sha256:
                return False
            sql += " AND target_sha=? AND diff_sha256=?"
            params += [target_sha, diff_sha256]
        if now is not None:
            sql += " AND (expires_at IS NULL OR expires_at>?)"
            params.append(now)
        return bool(c.execute(sql, params).fetchone())

    def has_start_approval(self, task_id: str) -> bool:
        with self._lock:
            return self._approval_ok(self._conn, task_id, "start", self.clock())

    def has_manual_review_approval(self, task_id: str) -> bool:
        """Unexpired manual_review approval bound to the task's *current* target/diff."""
        with self._lock:
            task = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return bool(task) and self._approval_ok(
                self._conn, task_id, "manual_review", self.clock(),
                task["target_sha"], task["diff_sha256"], bound=True,
            )

    def _merge_approval_ok(self, c: sqlite3.Connection, task: sqlite3.Row, now: float | None) -> bool:
        return self._approval_ok(
            c, task["id"], "merge", now, task["target_sha"], task["diff_sha256"], bound=True
        )

    def valid_merge_approval(self, task_id: str) -> bool:
        with self._lock:
            task = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            return bool(task) and self._merge_approval_ok(self._conn, task, self.clock())

    def approvals(self, task_id: str, decision: str | None = None) -> list[dict[str, Any]]:
        if decision is None:
            rows = self._query("SELECT * FROM approvals WHERE task_id=? ORDER BY id", (task_id,))
        else:
            rows = self._query(
                "SELECT * FROM approvals WHERE task_id=? AND decision=? ORDER BY id", (task_id, decision)
            )
        return [dict(r) for r in rows]

    # ---------------------------------------------------------------- bot_kv
    def get_kv(self, key: str, default: str | None = None) -> str | None:
        rows = self._query("SELECT v FROM bot_kv WHERE k=?", (key,))
        return rows[0]["v"] if rows else default

    def set_kv(self, key: str, value: Any) -> None:
        with self._tx() as c:
            c.execute(
                "INSERT INTO bot_kv (k, v, updated_at) VALUES (?,?,?)"
                " ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
                (key, str(value), self.clock()),
            )

    def cas_kv(self, key: str, expected: str | None, new: Any) -> bool:
        """Set `key` to `new` only if its current value equals `expected` (None = absent)."""
        now = self.clock()
        with self._tx() as c:
            if expected is None:
                cur = c.execute(
                    "INSERT OR IGNORE INTO bot_kv (k, v, updated_at) VALUES (?,?,?)", (key, str(new), now)
                )
            else:
                cur = c.execute(
                    "UPDATE bot_kv SET v=?, updated_at=? WHERE k=? AND v=?", (str(new), now, key, expected)
                )
            return cur.rowcount == 1

    def acquire_poller_lease(self, owner: str, ttl: float = 60.0) -> bool:
        """Single-poller lease (prevents two getUpdates loops from splitting the update stream)."""
        now = self.clock()
        with self._tx() as c:
            row = c.execute("SELECT v FROM bot_kv WHERE k='poller_lease'").fetchone()
            if row:
                try:
                    cur = json.loads(row["v"])
                except ValueError:
                    cur = {}
                if cur.get("owner") != owner and float(cur.get("until", 0)) > now:
                    return False
            c.execute(
                "INSERT INTO bot_kv (k, v, updated_at) VALUES ('poller_lease', ?, ?)"
                " ON CONFLICT(k) DO UPDATE SET v=excluded.v, updated_at=excluded.updated_at",
                (json.dumps({"owner": owner, "until": now + ttl}), now),
            )
            return True

    def release_poller_lease(self, owner: str) -> None:
        with self._tx() as c:
            row = c.execute("SELECT v FROM bot_kv WHERE k='poller_lease'").fetchone()
            if row and json.loads(row["v"]).get("owner") == owner:
                c.execute("DELETE FROM bot_kv WHERE k='poller_lease'")

    def get_update_offset(self) -> int:
        return int(self.get_kv("update_offset", "0") or 0)

    def advance_update_offset(self, expected: int, new: int) -> bool:
        """CAS the Telegram update offset forward; returns False if another poller won."""
        if new <= expected:
            return False
        if self.get_kv("update_offset") is None:
            return self.cas_kv("update_offset", None, new) if expected == 0 else False
        return self.cas_kv("update_offset", str(expected), new)

    # --------------------------------------------------------------- outbox
    def outbox(self, task_id: str | None = None, status: str | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM tg_outbox WHERE 1=1", []
        if task_id is not None:
            sql += " AND task_id=?"
            params.append(task_id)
        if status is not None:
            sql += " AND status=?"
            params.append(status)
        return [dict(r) for r in self._query(sql + " ORDER BY id", params)]

    def outbox_counts(self) -> dict[str, int]:
        counts = {s: 0 for s in OUTBOX_STATUSES}
        for r in self._query("SELECT status, COUNT(*) AS n FROM tg_outbox GROUP BY status"):
            counts[r["status"]] = r["n"]
        return counts

    def expire_stale_outbox_on_boot(self, current_boot_id: str, boot_time: float | None = None) -> dict[str, int]:
        """Prevent restart notification storms (no compensatory redelivery).

        PENDING rows claimed by a previous dispatcher boot, or enqueued before this boot,
        become EXPIRED (never attempted) or UNKNOWN (may already have been delivered).
        `/status` is the canonical way to recover the current picture.
        """
        boot_time = self.clock() if boot_time is None else boot_time
        now = self.clock()
        stale = "status='PENDING' AND ((boot_id IS NOT NULL AND boot_id<>?) OR (boot_id IS NULL AND created_at<?))"
        with self._tx() as c:
            unknown = c.execute(
                f"UPDATE tg_outbox SET status='UNKNOWN', unknown_at=?, updated_at=?, lease_owner=NULL,"
                f" lease_until=NULL, last_error='stale on boot' WHERE {stale} AND attempts>0",
                (now, now, current_boot_id, boot_time),
            ).rowcount
            expired = c.execute(
                f"UPDATE tg_outbox SET status='EXPIRED', updated_at=?, lease_owner=NULL, lease_until=NULL,"
                f" last_error='stale on boot' WHERE {stale} AND attempts=0",
                (now, current_boot_id, boot_time),
            ).rowcount
        return {"expired": expired, "unknown": unknown}

    def claim_outbox(
        self, owner: str, boot_id: str, limit: int = 10, lease_ttl: float = 60.0
    ) -> list[dict[str, Any]]:
        """Lease due PENDING rows to `owner`; `attempts` is bumped *before* any network call."""
        now = self.clock()
        with self._tx() as c:
            c.execute(
                "UPDATE tg_outbox SET status='EXPIRED', updated_at=?, last_error='ttl expired'"
                " WHERE status='PENDING' AND expires_at<=?",
                (now, now),
            )
            # A lease that lapsed mid-send may have been delivered: quarantine, never resend.
            c.execute(
                "UPDATE tg_outbox SET status='UNKNOWN', unknown_at=?, updated_at=?, lease_owner=NULL,"
                " last_error='lease lapsed during send' WHERE status='PENDING' AND lease_owner IS NOT NULL"
                " AND lease_until<=?",
                (now, now, now),
            )
            rows = c.execute(
                "SELECT id FROM tg_outbox WHERE status='PENDING' AND lease_owner IS NULL"
                " AND next_retry_at<=? ORDER BY id LIMIT ?",
                (now, limit),
            ).fetchall()
            ids = [r["id"] for r in rows]
            for i in ids:
                c.execute(
                    "UPDATE tg_outbox SET lease_owner=?, lease_until=?, boot_id=?, attempts=attempts+1,"
                    " updated_at=? WHERE id=? AND status='PENDING' AND lease_owner IS NULL",
                    (owner, now + lease_ttl, boot_id, now, i),
                )
            if not ids:
                return []
            marks = ",".join("?" * len(ids))
            return [
                dict(r)
                for r in c.execute(
                    f"SELECT * FROM tg_outbox WHERE id IN ({marks}) AND lease_owner=? ORDER BY id",
                    ids + [owner],
                ).fetchall()
            ]

    def _settle_outbox(self, row_id: int, owner: str, sets: str, params: list[Any]) -> bool:
        now = self.clock()
        with self._tx() as c:
            cur = c.execute(
                f"UPDATE tg_outbox SET {sets}, lease_owner=NULL, lease_until=NULL, updated_at=?"
                " WHERE id=? AND status='PENDING' AND lease_owner=?",
                params + [now, row_id, owner],
            )
            return cur.rowcount == 1

    def mark_outbox_sent(self, row_id: int, owner: str) -> bool:
        return self._settle_outbox(row_id, owner, "status='SENT', sent_at=?", [self.clock()])

    def mark_outbox_retry(self, row_id: int, owner: str, retry_after: float, error: str = "429") -> bool:
        """HTTP 429 only: stays PENDING with `next_retry_at` pushed out by Retry-After."""
        delay = max(1.0, float(retry_after or 1.0))
        return self._settle_outbox(
            row_id, owner, "next_retry_at=?, last_error=?", [self.clock() + delay, error[:300]]
        )

    def mark_outbox_unknown(self, row_id: int, owner: str, error: str) -> bool:
        """Timeout / connection reset / 5xx: delivery state unknown; never retried automatically."""
        return self._settle_outbox(
            row_id, owner, "status='UNKNOWN', unknown_at=?, last_error=?", [self.clock(), error[:300]]
        )

    def mark_outbox_failed(self, row_id: int, owner: str, error: str) -> bool:
        return self._settle_outbox(row_id, owner, "status='FAILED', last_error=?", [error[:300]])

    def mark_outbox_expired(self, row_id: int, owner: str, error: str) -> bool:
        return self._settle_outbox(row_id, owner, "status='EXPIRED', last_error=?", [error[:300]])

    # -------------------------------------------------------------- actions
    def issue_action_token(
        self,
        task_id: str,
        action: str,
        chat_id: int,
        user_id: int,
        ttl: float = ACTION_TTL,
        expected_version: int | None = None,
    ) -> str:
        """Issue a 22-char single-use callback token bound to the task's live state + SHAs."""
        if action not in ACTION_OPS:
            raise ValueError(f"unknown action op {action!r}")
        now = self.clock()
        with self._tx() as c:
            task = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(task_id)
            if expected_version is not None and task["state_version"] != expected_version:
                raise ActionRejected("STALE", "task moved before token issue")
            if action not in ACTION_OPS_BY_STATE.get(task["state"], frozenset()):
                raise ActionRejected("BAD_STATE", f"{action} not offered in {task['state']}")
            for _ in range(5):  # collision is astronomically unlikely; PK still guards it
                token = secrets.token_urlsafe(16)
                try:
                    c.execute(
                        "INSERT INTO tg_actions (token, task_id, action, expected_state,"
                        " expected_state_version, target_sha, diff_sha256, chat_id, user_id,"
                        " expires_at, used_at, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,NULL,?)",
                        (
                            token, task_id, action, task["state"], task["state_version"],
                            task["target_sha"], task["diff_sha256"], int(chat_id), int(user_id),
                            now + ttl, now,
                        ),
                    )
                    return token
                except sqlite3.IntegrityError:
                    continue
            raise CoordinatorError("could not allocate a unique action token")

    def consume_action_token(
        self,
        token: str,
        op: str,
        chat_id: int,
        user_id: int,
        effect: Callable[[sqlite3.Connection, dict[str, Any], dict[str, Any]], Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], Any]:
        """Validate + consume a callback token and run `effect(conn, action, task)` atomically.

        Checks exact op, chat_id, user_id, unused, unexpired and the live task row
        (state, state_version, target_sha, diff_sha256). Mutating ops are single-use via a
        CAS on `used_at IS NULL`; read-only ops (`d`) stay reusable until expiry. Any
        failure (including inside `effect`) rolls back, leaving zero side effects.
        """
        now = self.clock()
        with self._tx() as c:
            row = c.execute("SELECT * FROM tg_actions WHERE token=?", (token,)).fetchone()
            if row is None:
                raise ActionRejected("UNKNOWN_TOKEN")
            if row["action"] != op:
                raise ActionRejected("OP_MISMATCH")
            if row["chat_id"] != int(chat_id) or row["user_id"] != int(user_id):
                raise ActionRejected("IDENTITY_MISMATCH")
            if row["used_at"] is not None:
                raise ActionRejected("REPLAY", "token already used")
            if row["expires_at"] <= now:
                raise ActionRejected("EXPIRED")
            task = c.execute("SELECT * FROM tasks WHERE id=?", (row["task_id"],)).fetchone()
            if task is None:
                raise ActionRejected("UNKNOWN_TASK")
            if (task["state"], task["state_version"]) != (row["expected_state"], row["expected_state_version"]):
                raise ActionRejected("STALE", f"task is now {task['state']} v{task['state_version']}")
            if (task["target_sha"], task["diff_sha256"]) != (row["target_sha"], row["diff_sha256"]):
                raise ActionRejected("STALE_SHA", "candidate target/diff changed")
            if op not in READ_ONLY_OPS:
                cur = c.execute(
                    "UPDATE tg_actions SET used_at=? WHERE token=? AND used_at IS NULL", (now, token)
                )
                if cur.rowcount != 1:
                    raise ActionRejected("REPLAY", "concurrent use")
            action, task_d = dict(row), self._task_dict(task)
            result = effect(c, action, task_d) if effect is not None else None
            return action, task_d, result

    # -------------------------------------------------------- merge intents
    def begin_merge_intent(self, task_id: str, actor: str) -> str:
        """Journal an immutable merge intent *before* touching Git."""
        if not actor.startswith("human"):
            raise GuardViolation("merge intents require a human actor")
        now = self.clock()
        with self._tx() as c:
            task = c.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
            if task is None:
                raise KeyError(task_id)
            if task["state"] != "AWAITING_MERGE":
                raise MergeIntentError(f"task is {task['state']}, not AWAITING_MERGE")
            if not task["base_sha"] or not task["target_sha"] or not task["diff_sha256"]:
                raise MergeIntentError("task has no base/target/diff binding")
            if not self._merge_approval_ok(c, task, now):
                raise GuardViolation("merge approval missing, expired or not bound to current target/diff")
            if c.execute(
                "SELECT 1 FROM merge_intents WHERE task_id=? AND status IN ('PREPARING','GIT_MERGED')",
                (task_id,),
            ).fetchone():
                raise MergeIntentError("another merge intent is already open for this task")
            intent_id = f"mi-{uuid.uuid4().hex[:16]}"
            c.execute(
                "INSERT INTO merge_intents (id, task_id, base_sha, target_sha, diff_sha256, actor, status,"
                " created_at, updated_at) VALUES (?,?,?,?,?,?, 'PREPARING', ?, ?)",
                (intent_id, task_id, task["base_sha"], task["target_sha"], task["diff_sha256"], actor, now, now),
            )
            return intent_id

    def get_merge_intent(self, intent_id: str) -> dict[str, Any]:
        rows = self._query("SELECT * FROM merge_intents WHERE id=?", (intent_id,))
        if not rows:
            raise KeyError(intent_id)
        return dict(rows[0])

    def merge_intents(self, task_id: str | None = None, statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
        sql, params = "SELECT * FROM merge_intents WHERE 1=1", []
        if task_id is not None:
            sql += " AND task_id=?"
            params.append(task_id)
        if statuses is not None:
            statuses = list(statuses)
            sql += f" AND status IN ({','.join('?' * len(statuses))})"
            params += statuses
        return [dict(r) for r in self._query(sql + " ORDER BY created_at, id", params)]

    def _set_intent_status(self, intent_id: str, from_statuses: Iterable[str], to: str, error: str | None) -> None:
        from_statuses = list(from_statuses)
        with self._tx() as c:
            cur = c.execute(
                f"UPDATE merge_intents SET status=?, error=COALESCE(?, error), updated_at=? WHERE id=?"
                f" AND status IN ({','.join('?' * len(from_statuses))})",
                [to, error, self.clock(), intent_id] + from_statuses,
            )
            if cur.rowcount != 1:
                raise MergeIntentError(f"intent {intent_id} not in {from_statuses}")

    def mark_merge_intent_git_merged(self, intent_id: str) -> None:
        self._set_intent_status(intent_id, ["PREPARING"], "GIT_MERGED", None)

    def abort_merge_intent(self, intent_id: str, error: str, status: str = "ABORTED") -> None:
        if status not in ("ABORTED", "BLOCKED"):
            raise ValueError(status)
        self._set_intent_status(intent_id, OPEN_MERGE_INTENT, status, error[:500])

    def mark_orphan_cleanup(self, intent_id: str, error: str) -> None:
        """Cleanup after a committed merge failed: record it, never roll the merge back."""
        self._set_intent_status(intent_id, ["COMPLETED", "ORPHAN_CLEANUP"], "ORPHAN_CLEANUP", error[:500])

    def finalize_merge(self, intent_id: str, main_head: str, actor: str | None = None) -> int:
        """AWAITING_MERGE -> MERGED, only for a GIT_MERGED intent whose target is main's HEAD."""
        with self._tx() as c:
            intent = c.execute("SELECT * FROM merge_intents WHERE id=?", (intent_id,)).fetchone()
            if intent is None:
                raise KeyError(intent_id)
            if intent["status"] != "GIT_MERGED":
                raise GuardViolation(f"merge intent is {intent['status']}, not GIT_MERGED")
            if main_head != intent["target_sha"]:
                raise GuardViolation("main HEAD does not match the intent target_sha")
            task = c.execute("SELECT * FROM tasks WHERE id=?", (intent["task_id"],)).fetchone()
            version = self._transition_in_tx(
                c, intent["task_id"], task["state_version"], "MERGED", actor or intent["actor"],
                f"merged {intent['target_sha'][:12]} via intent {intent_id}", merge_intent=intent,
            )
            c.execute(
                "UPDATE merge_intents SET status='COMPLETED', updated_at=? WHERE id=?", (self.clock(), intent_id)
            )
            return version

    def reconcile_merge_intents(
        self, wtm: Any, artifacts_dir: str | Path | None = None, require_supervisor_lock: bool = False
    ) -> list[tuple[str, str]]:
        """Boot crash recovery for the Git <-> DB merge boundary.

        - open intent and main HEAD == target_sha -> Git merged before the crash: finalize MERGED
        - open intent and main HEAD == base_sha   -> merge never happened: ABORTED
        - open intent and any other HEAD          -> intent BLOCKED, task BLOCKED (manual reconciliation)
        - COMPLETED intent whose worktree still exists -> retry cleanup, else ORPHAN_CLEANUP

        `require_supervisor_lock=True` (live entrypoints) refuses to run unless this process
        holds `supervisor.lock` (raises LockOrderError).
        """
        if require_supervisor_lock:
            from .worktree import assert_supervisor_lock_held

            assert_supervisor_lock_held()
        results: list[tuple[str, str]] = []
        head = wtm.head_sha()
        for intent in self.merge_intents(statuses=OPEN_MERGE_INTENT):
            iid = intent["id"]
            if head == intent["target_sha"]:
                if intent["status"] == "PREPARING":
                    self.mark_merge_intent_git_merged(iid)
                self.finalize_merge(iid, head)
                results.append((iid, "FINALIZED"))
            elif head == intent["base_sha"]:
                self.abort_merge_intent(iid, "reconcile: main HEAD still at base")
                results.append((iid, "ABORTED"))
            else:
                self.abort_merge_intent(iid, f"reconcile: unexpected main HEAD {head[:12]}", "BLOCKED")
                task = self.get_task(intent["task_id"])
                if task["state"] not in TERMINAL_STATES and "BLOCKED" in TRANSITIONS[task["state"]]:
                    self.transition(
                        task["id"], task["state_version"], "BLOCKED", "merge-reconciler",
                        "MERGE_RECONCILE: main HEAD matches neither base nor target",
                    )
                results.append((iid, "BLOCKED"))
        if artifacts_dir is not None:
            for intent in self.merge_intents(statuses=("COMPLETED", "ORPHAN_CLEANUP")):
                task = self.get_task(intent["task_id"])
                wt = task["worktree_path"]
                if not wt or not Path(wt).is_dir():
                    continue
                try:
                    wtm.cleanup(wtm.attach(task["id"], task["base_sha"]), artifacts_dir, delete_branch=True)
                    results.append((intent["id"], "CLEANED"))
                except Exception as exc:  # recorded, never rolls back the merge
                    self.mark_orphan_cleanup(intent["id"], f"cleanup failed: {exc}")
                    results.append((intent["id"], "ORPHAN_CLEANUP"))
        return results

    # ---------------------------------------------------------------- circuit
    def circuit_record(
        self, name: str, ok: bool, threshold: int = 3, cooldown: float = 300.0
    ) -> dict[str, Any]:
        now = self.clock()
        with self._tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO circuit_state (name, consecutive_failures, updated_at)"
                " VALUES (?, 0, ?)",
                (name, now),
            )
            if ok:
                c.execute(
                    "UPDATE circuit_state SET consecutive_failures=0, open_until=NULL, updated_at=?"
                    " WHERE name=?",
                    (now, name),
                )
            else:
                c.execute(
                    "UPDATE circuit_state SET consecutive_failures=consecutive_failures+1,"
                    " updated_at=? WHERE name=?",
                    (now, name),
                )
                fails = c.execute(
                    "SELECT consecutive_failures FROM circuit_state WHERE name=?", (name,)
                ).fetchone()[0]
                if fails >= threshold:
                    c.execute(
                        "UPDATE circuit_state SET open_until=? WHERE name=?", (now + cooldown, name)
                    )
            return dict(c.execute("SELECT * FROM circuit_state WHERE name=?", (name,)).fetchone())

    def circuit_is_open(self, name: str) -> bool:
        rows = self._query("SELECT open_until FROM circuit_state WHERE name=?", (name,))
        return bool(rows) and rows[0]["open_until"] is not None and rows[0]["open_until"] > self.clock()

    def circuit_reset(self, name: str) -> None:
        self.circuit_record(name, ok=True)
