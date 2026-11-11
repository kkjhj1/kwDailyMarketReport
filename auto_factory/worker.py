"""PipelineWorker: drives the FSM with STOP.flag, circuit breaker, quota admission and boot recovery.

Usage:
    python -m auto_factory.worker --demo            # offline full-FSM dry-run in a temp repo
    python -m auto_factory.worker --status [--db PATH]          # read-only (mode=ro) inspection
    python -m auto_factory.worker --run [--db PATH] --repo PATH [--max-cycles N] [--auto-audit-l2]

`--db` defaults to `default_state_dir(repo)/coordinator.db` (outside the repo); STOP.flag,
artifacts, worktrees and supervisor.lock default to that state dir. `--run` holds
`supervisor.lock` for its lifetime (exit 4 if another supervisor/bot/worker holds it).

The worker never merges, never deploys and never sends Telegram messages.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .adapters import (
    COOLDOWN_CODES,
    FAILOVER_CODES,
    AuditParseError,
    HandoffContract,
    MockAdapter,
    ProviderAdapter,
    default_adapters,
    parse_audit_result,
    route_writer,
    select_reviewer,
)
from .db import (
    ACTIVE_STATES,
    AUDIT_REQUIRED_LEVELS,
    AUDITOR_PROVIDERS,
    MAIN_FLOW,
    ArtifactIntegrityError,
    CoordinatorDB,
    is_transient_db_error,
    read_db_summary_readonly,
    sha256_file,
    sha256_text,
    verify_phase1_artifacts,
)
from .guardrails import DeterministicRunner, GuardrailChecker, OscillationDetector, is_forbidden
from .win_job import build_sandbox_env, current_identity, is_same_process
from .worktree import (
    DB_FILENAME,
    HANDOFF_DIRNAME,
    STOP_FLAG_NAME,
    SupervisorLocked,
    WorktreeManager,
    _FileLock,
    _is_link_or_reparse,
    default_state_dir,
    safe_write_atomic,
    validate_state_layout,
)

CIRCUIT = "global"
DEFAULT_COOLDOWN = 900.0
RUNNABLE_STATES = MAIN_FLOW[: MAIN_FLOW.index("WORKTREE_READY") + 1] + ("RETRY_WAIT", "RECOVERY_PENDING")
FAILURE_OUTCOMES = frozenset({"BLOCKED", "FAILED", "GUARDRAIL_BLOCKED"})
SUCCESS_OUTCOMES = frozenset({"AWAITING_MERGE", "SINGLE_PROVIDER_REVIEW_REQUIRED"})


class StopRequested(Exception):
    pass


class PipelineWorker:
    def __init__(
        self,
        db: CoordinatorDB,
        repo_root: str | os.PathLike,
        adapters: dict[str, ProviderAdapter],
        artifacts_dir: str | os.PathLike,
        worktrees_dir: str | os.PathLike | None = None,
        stop_flag: str | os.PathLike | None = None,
        holdout_dir: str | os.PathLike | None = None,
        gate_runner: DeterministicRunner | None = None,
        quota_ratio: float = 0.7,
        provider_capacity: float = 100.0,
        units_per_call: float = 1.0,
        max_tasks_per_cycle: int = 5,
        circuit_threshold: int = 3,
        circuit_cooldown: float = 300.0,
        lease_ttl: float = 900.0,
        max_retries: int = 3,
        sleep_fn: Callable[[float], None] = time.sleep,
        auto_audit_l2: bool = False,
        external_stop: Callable[[], bool] | None = None,
        db_busy_retries: int = 3,
        db_busy_backoff: float = 0.5,
        audit_tmp_dir: str | os.PathLike | None = None,
    ) -> None:
        self.auto_audit_l2 = auto_audit_l2
        self.audit_tmp_dir = Path(audit_tmp_dir) if audit_tmp_dir else None
        self.external_stop = external_stop
        self.db_busy_retries = db_busy_retries
        self.db_busy_backoff = db_busy_backoff
        self.db = db
        self.adapters = adapters
        self.wtm = WorktreeManager(repo_root, worktrees_dir)
        self.guard = GuardrailChecker(self.wtm)
        self.gates = gate_runner or DeterministicRunner()
        self.artifacts_dir = Path(artifacts_dir)
        self.stop_flag = Path(stop_flag) if stop_flag else self.wtm.repo_root / "STOP.flag"
        self.holdout_dir = Path(holdout_dir) if holdout_dir else None
        self.quota_ratio = quota_ratio
        self.units = units_per_call
        self.max_tasks_per_cycle = max_tasks_per_cycle
        self.circuit_threshold = circuit_threshold
        self.circuit_cooldown = circuit_cooldown
        self.lease_ttl = lease_ttl
        self.max_retries = max_retries
        self.sleep_fn = sleep_fn
        self.pid, self.proc_start = current_identity()
        self.nonce = uuid.uuid4().hex
        self.restart_count = 0
        self.booted = False
        for name in adapters:
            db.ensure_provider(name, provider_capacity)
        self._handlers: dict[str, Callable[[dict[str, Any]], bool]] = {
            "RECEIVED": self._preflight,
            "PREFLIGHTED": self._classify,
            "CLASSIFIED": self._plan,
            "PLANNING": self._audit,
            "AUDITED": self._plan_ready,
            "PLAN_READY": self._await_start,
            "AWAITING_START_AUTH": self._start_worktree,
            "WORKTREE_READY": self._begin_implementing,
            "IMPLEMENTING": self._implement,
            "CHECKPOINTED": self._guardrails,
            "VERIFYING": self._verify,
            "RETRY_WAIT": self._retry,
            "REVIEWING": self._review,
            "RECOVERY_PENDING": self._resolve_recovery,
        }

    # ------------------------------------------------------------ plumbing
    @property
    def actor(self) -> str:
        return f"worker:{self.nonce[:8]}"

    def stop_requested(self) -> bool:
        if self.external_stop is not None and self.external_stop():
            return True
        return self.stop_flag.exists()

    def _check_stop(self) -> None:
        if self.stop_requested():
            raise StopRequested(str(self.stop_flag))

    def _move(self, task: dict[str, Any], to_state: str, reason: str = "", **fields: Any) -> None:
        self.db.transition(task["id"], task["state_version"], to_state, self.actor, reason, fields)

    def _task_dir(self, task_id: str) -> Path:
        d = self.artifacts_dir / task_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _save_artifact(self, task_id: str, kind: str, name: str, content: str | bytes, producer: str) -> Path:
        """Atomic no-follow write; the DB row is bound to the verified on-disk sha256."""
        data = content if isinstance(content, bytes) else content.encode("utf-8")
        path = safe_write_atomic(self._task_dir(task_id) / name, data)
        digest = hashlib.sha256(data).hexdigest()
        if sha256_file(path) != digest:
            raise ArtifactIntegrityError(f"artifact {name} changed after write")
        self.db.add_artifact(task_id, kind, path, producer, sha256=digest)
        return path

    def available_providers(self, excluded: set[str] = frozenset()) -> list[str]:
        now = self.db.clock()
        out = []
        for name, adapter in self.adapters.items():
            if name in excluded or not adapter.available():
                continue
            st = self.db.provider_status(name)
            if st["health"] != "ok":
                if (st["retry_after"] or 0) > now:
                    continue
                self.db.set_provider_health(name, "ok", None)
            if self.db.can_reserve(name, self.units, self.quota_ratio):
                out.append(name)
        return out

    def _cool_down(self, provider: str, result) -> None:
        delay = result.retry_after or DEFAULT_COOLDOWN
        self.db.set_provider_health(provider, result.code.value.lower(), self.db.clock() + delay)

    def _acceptance_tests(self, task: dict[str, Any]) -> list[str]:
        spec = task["acceptance_spec"]
        tests = ["python -m py_compile <changed .py>"]
        tests += [f"pytest {t}" for t in spec.get("pytest", [])]
        if self.holdout_dir:
            tests.append("holdout_eval (external)")
        return tests

    def _strict_l2(self, task: dict[str, Any]) -> bool:
        return self.auto_audit_l2 and task["risk_level"] == "L2"

    def _has_valid_audit_plan(self, task: dict[str, Any]) -> bool:
        if self._strict_l2(task):  # on-disk sha256 + schema + binding, not mere row existence
            if not self.db.has_start_approval(task["id"]):
                return False
            try:
                verify_phase1_artifacts(self.db, task)
            except ArtifactIntegrityError:
                return False
            return True
        if not self.db.artifacts(task["id"], "plan") or not self.db.has_start_approval(task["id"]):
            return False
        return task["risk_level"] not in AUDIT_REQUIRED_LEVELS or bool(self.db.artifacts(task["id"], "audit"))

    def _contract(self, task: dict[str, Any], role: str, checkpoint_sha: str, **kw: Any) -> HandoffContract:
        contract = HandoffContract(
            task_id=task["id"],
            risk_level=task["risk_level"],
            base_sha=task["base_sha"],
            checkpoint_sha=checkpoint_sha,
            allowed_paths=task["allowed_paths"],
            acceptance_tests=self._acceptance_tests(task),
            next_role=role,
            **kw,
        )
        # Summary is shrunk dynamically so the serialized contract stays <= 2048 UTF-8 bytes.
        return contract.fit_summary(task["request"], max_chars=300)

    def _candidate_binding(self, task: dict[str, Any]) -> dict[str, str]:
        """Bind a candidate to its exact commit and diff (required for any human approval)."""
        wt = task["worktree_path"]
        return {"target_sha": self.wtm.head_sha(wt), "diff_sha256": self.wtm.diff_sha256(wt, task["base_sha"])}

    def _write_contract(self, task: dict[str, Any], contract: HandoffContract, attempt_tag: str) -> Path:
        text = contract.to_json()
        self._save_artifact(task["id"], "handoff", f"{attempt_tag}.handoff.json", text, "worker")
        return contract.write(Path(task["worktree_path"]) / HANDOFF_DIRNAME / "handoff.json")

    # ------------------------------------------------------------ handlers
    def _preflight(self, task: dict[str, Any]) -> bool:
        bad = [p for p in task["allowed_paths"] if is_forbidden(p)]
        if bad:
            self._move(task, "BLOCKED", f"PREFLIGHT: forbidden allowed_paths {bad}")
            return False
        self._move(task, "PREFLIGHTED", "preflight ok", base_sha=self.wtm.head_sha())
        return True

    def _classify(self, task: dict[str, Any]) -> bool:
        if task["risk_level"] == "L3":
            self._move(task, "BLOCKED", "L3 fail-closed: deploy/DB/live-send is outside unattended scope")
            return False
        decision = route_writer(task["risk_level"], self.available_providers(), has_valid_audit_plan=True)
        if decision.outcome == "BLOCKED":
            self._move(task, "BLOCKED", f"ROUTING: {decision.reason}")
            return False
        self._move(task, "CLASSIFIED", f"risk={task['risk_level']} route={decision.outcome}:{decision.writer}")
        return True

    def _plan(self, task: dict[str, Any]) -> bool:
        if not self.db.artifacts(task["id"], "plan") and task["risk_level"] not in AUDIT_REQUIRED_LEVELS:
            plan = {
                "request": task["request"][:500],
                "allowed_paths": task["allowed_paths"],
                "acceptance_tests": self._acceptance_tests(task),
            }
            self._save_artifact(task["id"], "plan", "plan.json", json.dumps(plan, ensure_ascii=False), "deterministic")
        self._move(task, "PLANNING", "plan recorded")
        return True

    def _audit(self, task: dict[str, Any]) -> bool:
        if self._strict_l2(task):
            return self._auto_audit(task)
        if task["risk_level"] in AUDIT_REQUIRED_LEVELS and not (
            self.db.artifacts(task["id"], "audit") and self.db.artifacts(task["id"], "plan")
        ):
            self._move(task, "BLOCKED", "AUDIT_INCOMPLETE: Phase 1 plan+audit artifacts required")
            return False
        self._move(task, "AUDITED", "audit gate passed")
        return True

    # ----------------------------------------------------- automated L2 Phase 1
    def _block(self, task: dict[str, Any], reason: str) -> bool:
        self._move(self.db.get_task(task["id"]), "BLOCKED", reason[:500])
        return False

    def _auto_audit(self, task: dict[str, Any]) -> bool:
        """L2 Phase 1 audit by codex (fallback gemini; never claude) in a disposable clone."""
        if self.db.artifacts(task["id"], "audit") or self.db.artifacts(task["id"], "plan"):
            try:  # pre-existing (e.g. manual) Phase 1 artifacts must pass strict verification
                verify_phase1_artifacts(self.db, task)
            except ArtifactIntegrityError as exc:
                return self._block(task, f"AUDIT_INVALID: {exc}")
            self._move(task, "AUDITED", "pre-existing Phase 1 artifacts verified")
            return True
        # Hold merge.lock so no merge/cleanup changes main while it is snapshotted.
        lock = _FileLock(self.wtm.merge_lock_path, role="merge")
        if not lock.try_acquire():
            return False  # stay in PLANNING; retried on the next cycle
        try:
            return self._run_auditors(task)
        finally:
            lock.release()

    def _run_auditors(self, task: dict[str, Any]) -> bool:
        tid, base = task["id"], task["base_sha"]
        contract = self._contract(task, "auditor", base)
        text = contract.to_json()
        cpath = self._save_artifact(tid, "handoff", "phase1_handoff.json", text, "worker")
        for provider in AUDITOR_PROVIDERS:
            self._check_stop()
            if provider not in self.adapters or provider not in self.available_providers():
                continue
            if not self.db.reserve(provider, self.units, self.quota_ratio):
                continue
            attempt_id = self.db.add_attempt(tid, provider, "auditor", sha256_text(text))
            result, problem = self._audit_in_clone(task, provider, contract, cpath)
            code = problem.split(":", 1)[0] if problem else (result.code.value if result else "NO_RESULT")
            self.db.finish_attempt(attempt_id, failure_code=code,
                                   output_digest=sha256_text(result.stdout) if result else None)
            if problem:
                return self._block(task, problem)
            self._check_stop()
            if not result.ok:
                if result.code in COOLDOWN_CODES:
                    self._cool_down(provider, result)
                if result.code in FAILOVER_CODES:
                    continue  # capacity/availability only: try the next auditor
                return self._block(task, f"AUDIT_FAILED: auditor={provider} code={result.code.value}")
            try:
                parsed = parse_audit_result(result.stdout, tid, base, task["allowed_paths"])
            except AuditParseError as exc:  # malformed output never falls back
                return self._block(task, f"AUDIT_INVALID: auditor={provider}: {exc}")
            audit = dict(parsed, auditor=provider)
            body = json.dumps(audit, ensure_ascii=False, sort_keys=True)
            if parsed["verdict"] != "approve":
                self._save_artifact(tid, "audit_rejected", "audit_rejected.json", body, provider)
                return self._block(task, f"AUDIT_BLOCKED: auditor={provider}: {parsed['summary']}")
            audit_path = self._save_artifact(tid, "audit", "audit.json", body, provider)
            plan = {
                "schema_version": 1, "task_id": tid, "base_sha": base, "allowed_paths": task["allowed_paths"],
                "acceptance_tests": self._acceptance_tests(task), "request": task["request"][:500],
                "auditor": provider, "audit_sha256": sha256_file(audit_path),
            }
            self._save_artifact(tid, "plan", "plan.json", json.dumps(plan, ensure_ascii=False, sort_keys=True),
                                provider)
            verify_phase1_artifacts(self.db, self.db.get_task(tid))
            self._move(self.db.get_task(tid), "AUDITED", f"Phase 1 audit approved by {provider}")
            return True
        return self._block(task, "AUDIT_UNAVAILABLE: no auditor (codex/gemini) available; claude never audits")

    def _audit_in_clone(self, task: dict[str, Any], provider: str, contract: HandoffContract,
                        cpath: Path) -> tuple[Any, str | None]:
        """Run one auditor in a disposable clone; returns (result, blocking_problem)."""
        base = task["base_sha"]
        result, problem, consumed = None, None, 0.0
        main_before = self.wtm.main_snapshot()
        if self.audit_tmp_dir is not None:
            self.audit_tmp_dir.mkdir(parents=True, exist_ok=True)
        clone_dir = Path(tempfile.mkdtemp(prefix="af_audit_", dir=self.audit_tmp_dir))
        try:
            clone = clone_before = None
            try:
                clone = self.wtm.create_disposable_clone(base, clone_dir)
                clone_before = self.wtm.clone_snapshot(clone)
            except Exception as exc:
                problem = f"AUDIT_CLONE_FAILED: {type(exc).__name__}: {exc}"
            if problem is None and (clone_before["head"] != base or clone_before["status"]):
                problem = "AUDIT_CLONE_FAILED: clone is not a clean checkout of base_sha"
            if problem is None:
                consumed = self.units
                result = self.adapters[provider].invoke(
                    contract, str(clone), "auditor", str(cpath), self.stop_requested
                )
                if self.wtm.clone_snapshot(clone) != clone_before:
                    problem = f"AUDITOR_MUTATED_REPO: {provider} changed the disposable clone"
                if self.wtm.main_snapshot() != main_before:
                    problem = f"AUDITOR_MUTATED_REPO: main repository changed during audit by {provider}"
        finally:
            self.db.settle(provider, self.units, consumed)
            try:
                self.wtm.remove_disposable_clone(clone_dir)
            except Exception as exc:  # never AWAITING_START_AUTH with a leftover clone
                problem = f"AUDITOR_CLEANUP_FAILED: {type(exc).__name__}: {exc}"
        return result, problem

    def _plan_ready(self, task: dict[str, Any]) -> bool:
        self._move(task, "PLAN_READY", "plan ready")
        return True

    def _await_start(self, task: dict[str, Any]) -> bool:
        self._move(task, "AWAITING_START_AUTH", "waiting for explicit start authorization")
        return True

    def _start_worktree(self, task: dict[str, Any]) -> bool:
        if not self.db.has_start_approval(task["id"]):
            return False  # stay parked; never self-authorize
        info = self.wtm.create(task["id"], task["base_sha"])
        self._move(task, "WORKTREE_READY", f"worktree {info.branch}", worktree_path=info.path, branch=info.branch)
        return True

    def _begin_implementing(self, task: dict[str, Any]) -> bool:
        self._move(task, "IMPLEMENTING", "writer phase")
        return True

    def _last_failure(self, task: dict[str, Any]) -> dict[str, Any] | None:
        if not task["retry_count"]:
            return None
        verifies = [a for a in self.db.attempts(task["id"], "verify") if a["traceback_hash"]]
        if not verifies:
            return None
        last = verifies[-1]
        return {"code": last["failure_code"], "traceback_hash": last["traceback_hash"], "attempt": task["retry_count"]}

    def _implement(self, task: dict[str, Any]) -> bool:
        wt = task["worktree_path"]
        failure = self._last_failure(task)
        excluded: set[str] = set()
        failed_writers: set[str] = set()
        auditor: str | None = None
        if self._strict_l2(task):
            try:  # re-verified on disk right before any writer runs (tamper check)
                auditor = verify_phase1_artifacts(self.db, task)["auditor"]
            except ArtifactIntegrityError as exc:
                return self._block(task, f"AUDIT_INVALID: {exc}")
            failed_writers.add(auditor)  # auditor != coder; it may still review
        while True:
            self._check_stop()
            task = self.db.get_task(task["id"])
            decision = route_writer(
                task["risk_level"],
                self.available_providers(excluded),
                self._has_valid_audit_plan(task),
                failed_writers,
            )
            if decision.outcome == "BLOCKED":
                self._move(task, "BLOCKED", f"IMPLEMENTATION_BLOCKED: {decision.reason}")
                return False
            writer = decision.writer
            if auditor is not None and writer == auditor:
                return self._block(task, f"IMPLEMENTATION_BLOCKED: writer {writer} audited this task")
            if not self.db.reserve(writer, self.units, self.quota_ratio):
                excluded.add(writer)
                continue
            checkpoint = self.wtm.head_sha(wt)
            contract = self._contract(task, "writer", checkpoint, failure=failure)
            attempt_id = self.db.add_attempt(task["id"], writer, "writer", sha256_text(contract.to_json()))
            cpath = self._write_contract(task, contract, f"attempt{attempt_id}-{writer}")
            try:
                result = self.adapters[writer].invoke(contract, wt, "writer", str(cpath), self.stop_requested)
            finally:
                self.db.settle(writer, self.units, self.units)
            self.db.finish_attempt(
                attempt_id, failure_code=result.code.value, output_digest=sha256_text(result.stdout)
            )
            self._check_stop()
            task = self.db.get_task(task["id"])
            if result.ok:
                sha = self.wtm.commit_wip_checkpoint(wt, f"{task['id']} writer={writer} attempt={attempt_id}")
                self._move(
                    task,
                    "CHECKPOINTED",
                    f"writer={writer} ({decision.reason})",
                    checkpoint_sha=sha,
                    coder=writer,
                    single_provider=decision.outcome == "SINGLE_PROVIDER_REVIEW_REQUIRED",
                )
                return True
            # Preserve any partial work before handing off; next provider starts from this checkpoint.
            sha = self.wtm.commit_wip_checkpoint(wt, f"{task['id']} partial from {writer} ({result.code.value})")
            self.db.update_fields(
                task["id"], task["state_version"], {"checkpoint_sha": sha, "last_error": result.code.value},
                self.actor, f"WIP checkpoint after {writer} {result.code.value}",
            )
            if result.code in COOLDOWN_CODES:
                self._cool_down(writer, result)
                excluded.add(writer)
            if result.code in FAILOVER_CODES:
                failed_writers.add(writer)
                continue
            task = self.db.get_task(task["id"])
            self._move(task, "BLOCKED", f"IMPLEMENTATION_FAILED: writer={writer} code={result.code.value}")
            return False

    def _guardrails(self, task: dict[str, Any]) -> bool:
        report = self.guard.check_worktree(task["worktree_path"], task["base_sha"], task["allowed_paths"])
        body = json.dumps([v.__dict__ for v in report.violations], ensure_ascii=False)
        self._save_artifact(task["id"], "guardrail", f"guardrail-v{task['state_version']}.json", body, "deterministic")
        if not report.ok:
            self._move(task, "GUARDRAIL_BLOCKED", f"guardrail: {sorted(report.codes())}"[:500])
            return False
        self._move(task, "VERIFYING", "guardrails passed")
        return True

    def _verify(self, task: dict[str, Any]) -> bool:
        wt, base = task["worktree_path"], task["base_sha"]
        changed = self.wtm.changed_files(wt, base)
        py_files = [c.path for c in changed if c.status != "D" and c.path.endswith(".py")]
        results = [self.gates.py_compile(wt, py_files)]
        targets = task["acceptance_spec"].get("pytest", [])
        if results[-1].ok and targets:
            self._check_stop()
            results.append(self.gates.pytest(wt, targets))
        if results[-1].ok and self.holdout_dir:
            self._check_stop()
            results.append(self.gates.holdout(wt, str(self.holdout_dir)))
        report = [{"gate": r.name, "ok": r.ok, "rc": r.returncode, "hash": r.traceback_hash} for r in results]
        self._save_artifact(task["id"], "gates", f"gates-v{task['state_version']}.json", json.dumps(report), "deterministic")
        failed = next((r for r in results if not r.ok), None)
        if failed is None:
            if task["single_provider"]:
                self._move(
                    task, "SINGLE_PROVIDER_REVIEW_REQUIRED", "gates green; no independent reviewer",
                    **self._candidate_binding(task),
                )
                return False
            self._move(task, "REVIEWING", "deterministic gates green")
            return True
        attempt_id = self.db.add_attempt(task["id"], "deterministic", "verify")
        self.db.finish_attempt(
            attempt_id,
            failure_code=f"GATE_{failed.name.upper()}",
            traceback_hash=failed.traceback_hash,
            output_digest=sha256_text(failed.output_tail),
            checkpoint_sha=task["checkpoint_sha"],
            fail_count=failed.fail_count,
        )
        detector = OscillationDetector(self.max_retries)
        decision = "PASS"
        for a in self.db.attempts(task["id"], "verify"):
            decision = detector.record(a["traceback_hash"], a["fail_count"] or 0)
        if decision == "RETRY":
            self._move(
                task, "RETRY_WAIT", f"{failed.name} failed hash={failed.traceback_hash}",
                retry_count=task["retry_count"] + 1, last_error=f"{failed.name}:{failed.traceback_hash}",
            )
            return True
        self._move(task, "BLOCKED", f"SELF_HEAL_ABORT: {decision} at {failed.name}")
        return False

    def _retry(self, task: dict[str, Any]) -> bool:
        self._move(task, "IMPLEMENTING", f"retry {task['retry_count']}")
        return True

    def _review(self, task: dict[str, Any]) -> bool:
        wt, base, coder = task["worktree_path"], task["base_sha"], task["coder"]
        tried: set[str] = set()
        while True:
            self._check_stop()
            task = self.db.get_task(task["id"])
            reviewer = select_reviewer(coder, [p for p in self.available_providers() if p not in tried])
            if reviewer is None:
                self._move(
                    task, "SINGLE_PROVIDER_REVIEW_REQUIRED", f"no reviewer distinct from coder={coder}",
                    **self._candidate_binding(task),
                )
                return False
            if not self.db.reserve(reviewer, self.units, self.quota_ratio):
                tried.add(reviewer)
                continue
            target_sha = self.wtm.head_sha(wt)
            diff_sha = self.wtm.diff_sha256(wt, base)
            contract = self._contract(task, "reviewer", target_sha, diff_sha256=diff_sha)
            attempt_id = self.db.add_attempt(task["id"], reviewer, "reviewer", sha256_text(contract.to_json()))
            cpath = self._write_contract(task, contract, f"attempt{attempt_id}-{reviewer}-review")
            try:
                result = self.adapters[reviewer].invoke(contract, wt, "reviewer", str(cpath), self.stop_requested)
            finally:
                self.db.settle(reviewer, self.units, self.units)
            self.db.finish_attempt(attempt_id, failure_code=result.code.value, output_digest=sha256_text(result.stdout))
            self._check_stop()
            task = self.db.get_task(task["id"])
            if not result.ok:
                if result.code in COOLDOWN_CODES:
                    self._cool_down(reviewer, result)
                tried.add(reviewer)
                continue
            if self.wtm.is_dirty(wt) or self.wtm.head_sha(wt) != target_sha:
                self._move(task, "BLOCKED", f"REVIEWER_MUTATED_WORKTREE: {reviewer}")
                return False
            self._save_artifact(
                task["id"], "review", f"review-{attempt_id}-{reviewer}.txt",
                f"verdict={result.verdict}\n{result.stdout[-4000:]}", reviewer,
            )
            if result.verdict == "approve":
                self._move(
                    task, "AWAITING_MERGE", f"reviewer={reviewer} approved; human merge approval required",
                    target_sha=target_sha, diff_sha256=diff_sha,
                )
                return False
            self._move(task, "BLOCKED", f"REVIEW_REJECTED: reviewer={reviewer} verdict={result.verdict}")
            return False

    # ------------------------------------------------------------ recovery
    def recover_on_boot(self) -> list[str]:
        recovered = []
        for task in self.db.list_tasks(ACTIVE_STATES):
            lease = self.db.get_lease(task["id"])
            if lease and lease["owner_nonce"] != self.nonce and is_same_process(
                lease["pid"], lease["proc_start_time"]
            ):
                continue  # a live owner (same pid AND start time) still holds it
            self._move(task, "RECOVERY_PENDING", f"boot recovery from {task['state']}", recovery_from=task["state"])
            self.db.force_release_lease(task["id"])
            recovered.append(task["id"])
        self.booted = True
        return recovered

    def _resolve_recovery(self, task: dict[str, Any]) -> bool:
        src, wt = task["recovery_from"], task["worktree_path"]
        if not wt or not Path(wt).is_dir():
            self._move(task, "BLOCKED", "RECOVERY: worktree missing")
            return False
        if self.wtm.is_dirty(wt):
            patch = self.wtm.git_bytes("diff", "--binary", "HEAD", cwd=wt)
            status = self.wtm.git("status", "--porcelain", "--untracked-files=all", cwd=wt)
            self._save_artifact(task["id"], "recovery_patch", "recovery.patch", patch, "worker")
            self._save_artifact(task["id"], "recovery_status", "recovery_status.txt", status, "worker")
            self._move(task, "BLOCKED", "RECOVERY: uncommitted diff without checkpoint (preserved)")
            return False
        head = self.wtm.head_sha(wt)
        expected = task["checkpoint_sha"] or task["base_sha"]
        if head != expected:
            self._move(task, "BLOCKED", f"RECOVERY: HEAD {head[:12]} != checkpoint {expected[:12]}")
            return False
        is_desc = subprocess.run(
            ["git", "merge-base", "--is-ancestor", task["base_sha"], head],
            cwd=wt, env=build_sandbox_env(), stdin=subprocess.DEVNULL, capture_output=True,
        ).returncode == 0
        if not is_desc:
            self._move(task, "BLOCKED", "RECOVERY: checkpoint does not descend from base_sha")
            return False
        if src in ("WORKTREE_READY", "IMPLEMENTING", "RETRY_WAIT") or not task["coder"]:
            self._move(task, "WORKTREE_READY", f"resume writer from checkpoint {head[:12]}")
        else:  # verification and review are re-run, never skipped
            self._move(task, "CHECKPOINTED", f"re-verify from checkpoint {head[:12]}")
        return True

    # ------------------------------------------------------------ loop
    def process_task(self, task_id: str) -> str:
        """Drive one task; transient `database is locked/busy` errors back off and retry.

        After `db_busy_retries` the task is left in its current state ("DB_BUSY"), never
        BLOCKED for a lock contention.
        """
        delay = self.db_busy_backoff
        for attempt in range(self.db_busy_retries + 1):
            try:
                return self._process_task_once(task_id)
            except sqlite3.OperationalError as exc:
                if not is_transient_db_error(exc):
                    raise
                if attempt >= self.db_busy_retries:
                    return "DB_BUSY"
                self.sleep_fn(delay)
                delay *= 2
        return "DB_BUSY"  # pragma: no cover

    def _process_task_once(self, task_id: str) -> str:
        self._check_stop()
        if not self.db.acquire_lease(task_id, self.nonce, self.pid, self.proc_start, self.lease_ttl):
            return "LEASED_ELSEWHERE"
        try:
            while True:
                self._check_stop()
                task = self.db.get_task(task_id)
                handler = self._handlers.get(task["state"])
                if handler is None:
                    return task["state"]
                self.db.heartbeat(task_id, self.nonce, self.lease_ttl)
                if not handler(task):
                    return self.db.get_task(task_id)["state"]
        except StopRequested:
            raise
        except Exception as exc:  # fail closed: park the task for a human
            if is_transient_db_error(exc):
                raise  # lock contention is retried by process_task, never BLOCKED
            task = self.db.get_task(task_id)
            reason = f"EXCEPTION {type(exc).__name__}: {exc}"[:500]
            if task["state"] not in ("BLOCKED", "FAILED", "DISCARDED", "CANCELLED", "MERGED"):
                self.db.transition(task_id, task["state_version"], "BLOCKED", self.actor, reason, {"last_error": reason})
            return "BLOCKED"
        finally:
            try:
                self.db.release_lease(task_id, self.nonce)
            except sqlite3.OperationalError as exc:
                if not is_transient_db_error(exc):
                    raise
                # lease expires by TTL; same nonce re-acquires it on retry

    def _runnable(self) -> list[dict[str, Any]]:
        tasks = []
        for t in self.db.list_tasks(RUNNABLE_STATES):
            if t["state"] == "AWAITING_START_AUTH" and not self.db.has_start_approval(t["id"]):
                continue
            tasks.append(t)
        return tasks

    def restart(self) -> None:
        """Recycle worker identity after `max_tasks_per_cycle` tasks (bounded worker lifetime)."""
        self.restart_count += 1
        self.nonce = uuid.uuid4().hex

    def run_cycle(self) -> dict[str, Any]:
        summary: dict[str, Any] = {"processed": [], "stopped": False, "circuit_open": False, "recovered": []}
        if self.stop_requested():
            summary["stopped"] = True
            return summary
        if not self.booted:
            summary["recovered"] = self.recover_on_boot()
        if self.db.circuit_is_open(CIRCUIT):
            summary["circuit_open"] = True
            self.sleep_fn(self.circuit_cooldown)
            return summary
        for task in self._runnable()[: self.max_tasks_per_cycle]:
            try:
                final = self.process_task(task["id"])
            except StopRequested:
                summary["stopped"] = True
                break
            summary["processed"].append((task["id"], final))
            if final in FAILURE_OUTCOMES:
                state = self.db.circuit_record(CIRCUIT, False, self.circuit_threshold, self.circuit_cooldown)
                if state["open_until"]:
                    summary["circuit_open"] = True
                    self.sleep_fn(self.circuit_cooldown)
                    break
            elif final in SUCCESS_OUTCOMES:
                self.db.circuit_record(CIRCUIT, True)
        if len(summary["processed"]) >= self.max_tasks_per_cycle:
            self.restart()
        return summary

    def run_forever(self, max_cycles: int | None = None, idle_sleep: float = 30.0) -> int:
        cycles = 0
        while max_cycles is None or cycles < max_cycles:
            summary = self.run_cycle()
            cycles += 1
            if summary["stopped"]:
                break
            if not summary["processed"] and not summary["circuit_open"]:
                self.sleep_fn(idle_sleep)
        return cycles


# ---------------------------------------------------------------- demo
def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=auto_factory", "-c", "user.email=auto_factory@localhost", *args],
        cwd=str(repo), env=build_sandbox_env(), stdin=subprocess.DEVNULL, capture_output=True, check=True,
    )


def build_demo_repo(repo: Path) -> None:
    (repo / "tests").mkdir(parents=True)
    (repo / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (repo / "tests" / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n    assert add(-1, 1) == 0\n",
        encoding="utf-8",
    )
    (repo / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")
    _git(repo, "-c", "init.defaultBranch=main", "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "demo base")


def _demo_fix(wt: str, contract, role) -> None:
    Path(wt, "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")


def _demo_cheat(wt: str, contract, role) -> None:
    test = Path(wt, "tests", "test_calc.py")
    src = test.read_text(encoding="utf-8")
    test.write_text("import pytest\n" + src.replace("def test_add", "@pytest.mark.skip\ndef test_add"), encoding="utf-8")


def run_demo(out: Callable[[str], None] = print) -> int:
    os.environ["TELEGRAM_BOT_TOKEN"] = ""
    cwd = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="af_demo_", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        repo = root / "repo"
        build_demo_repo(repo)
        holdout = root / "holdout"
        holdout.mkdir()
        (holdout / "test_holdout_calc.py").write_text(
            "from calc import add\n\n\ndef test_holdout_add():\n    assert add(10, 5) == 15\n", encoding="utf-8"
        )
        db = CoordinatorDB(root / "coordinator.db")
        adapters = {
            "claude": MockAdapter("claude", ["RATE_LIMIT"]),
            "codex": MockAdapter("codex", ["TIMEOUT", None, _demo_cheat]),
            "gemini": MockAdapter("gemini", [_demo_fix]),
        }
        worker = PipelineWorker(
            db, repo, adapters, artifacts_dir=root / "artifacts", worktrees_dir=root / "worktrees",
            stop_flag=root / "STOP.flag", holdout_dir=holdout,
        )
        t1, _ = db.create_task("fix add() in calc.py", "L1", ["calc.py"], {"pytest": ["tests/test_calc.py"]})
        t2, _ = db.create_task(
            "make tests green (cheating writer)", "L1", ["calc.py", "tests/test_calc.py"],
            {"pytest": ["tests/test_calc.py"]},
        )
        t3, _ = db.create_task("migrate production DB", "L3", ["market_brief/db.py"], {"pytest": []})
        for tid in (t1, t2, t3):
            db.add_approval(tid, "start")

        out(f"[demo] sandbox={root}")
        out("[demo] TELEGRAM_BOT_TOKEN='' ; adapters=mock (no live LLM/Telegram calls)")
        expected = {t1: "AWAITING_MERGE", t2: "GUARDRAIL_BLOCKED", t3: "BLOCKED"}
        try:
            for tid in (t1, t2, t3):
                final = worker.process_task(tid)
                out(f"[demo] task {tid} -> {final}")
            for tid in (t1, t2, t3):
                out(f"[demo] events {tid}:")
                for e in db.events(tid):
                    out(f"    {e['from_state'] or '-'} -> {e['to_state']} [{e['actor']}] {e['reason']}")
            for tid in (t1, t2):
                atts = [f"{a['provider']}/{a['role']}={a['failure_code']}" for a in db.attempts(tid)]
                out(f"[demo] attempts {tid}: {', '.join(atts)}")
            task1 = db.get_task(t1)
            out(f"[demo] {t1} coder={task1['coder']} target_sha={task1['target_sha'][:12]} "
                f"diff_sha256={task1['diff_sha256'][:12]} merge_approved={db.valid_merge_approval(t1)}")
            info = worker.wtm.attach(t1, task1["base_sha"])
            patch = worker.wtm.cleanup(info, root / "artifacts")
            out(f"[demo] worktree cleaned; patch preserved ({patch.stat().st_size} bytes)")
            for tid in (t2,):
                t = db.get_task(tid)
                worker.wtm.cleanup(worker.wtm.attach(tid, t["base_sha"]), root / "artifacts")
            finals = {tid: db.get_task(tid)["state"] for tid in expected}
        finally:
            db.close()
            os.chdir(cwd)
        ok = finals == expected
        out(f"[demo] RESULT {'PASS' if ok else 'FAIL'}: {finals}")
        return 0 if ok else 1


# ---------------------------------------------------------------- CLI
def validate_db_path(raw: str | os.PathLike | None, repo_root: str | os.PathLike) -> Path:
    """Canonicalize a coordinator DB path and refuse anything that could be a product asset.

    Rejects: missing value, `*.sqlite3`, any path inside `repo_root` (product DBs, deploy
    folder), any `reports` path component, and symlinks / reparse points on the file or
    its directory (which could redirect to a production database).
    """
    if not raw:
        raise ValueError("--db is required")
    p = Path(raw)
    if p.suffix.lower() == ".sqlite3" or str(raw).lower().endswith(".sqlite3"):
        raise ValueError("--db must not be a *.sqlite3 product database")
    for cand in (p, p.parent):
        if _is_link_or_reparse(cand):
            raise ValueError(f"--db must not be (inside) a symlink/reparse point: {cand}")
    resolved = p.resolve()
    if resolved.suffix.lower() == ".sqlite3":
        raise ValueError("--db resolves to a *.sqlite3 product database")
    if any(part.lower() == "reports" for part in resolved.parts):
        raise ValueError("--db must not live under a reports/ directory")
    root = Path(repo_root).resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        return resolved
    raise ValueError("--db must live outside the repository (separate auto_factory state dir)")


def resolve_state_paths(
    repo: str | os.PathLike,
    db: str | os.PathLike | None = None,
    artifacts: str | os.PathLike | None = None,
    worktrees: str | os.PathLike | None = None,
    stop_flag: str | os.PathLike | None = None,
) -> dict[str, Path]:
    """Shared CLI layout: everything defaults into the external state dir (never the repo).

    Returns canonical `db`, `artifacts`, `worktrees`, `stop_flag`, `lock`, `state_dir`
    (raises ValueError). `merge.lock` lives next to `worktrees` (WorktreeManager), so all
    CLIs sharing a state dir also share the merge lock.
    """
    db_path = validate_db_path(db or default_state_dir(repo) / DB_FILENAME, repo)
    state = db_path.parent
    layout = validate_state_layout(
        repo, db_path,
        artifacts or state / "artifacts",
        worktrees or state / "worktrees",
        stop_flag or state / STOP_FLAG_NAME,
    )
    layout["state_dir"] = state
    return layout


def print_status_readonly(db_path: Path, out: Callable[[str], None] = print) -> int:
    """`--status`: mode=ro inspection; exit 2 if the DB is missing/invalid (never creates it)."""
    try:
        summary = read_db_summary_readonly(db_path)
    except (FileNotFoundError, ValueError, sqlite3.Error) as exc:
        out(f"[auto_factory] STATUS FAIL: {type(exc).__name__}: {exc}")
        return 2
    for t in summary["tasks"]:
        out(f"{t['id']}\t{t['risk_level']}\t{t['state']}\tv{t['state_version']}")
    counts = " ".join(f"{k}={v}" for k, v in summary["outbox"].items())
    out(f"[auto_factory] tasks={len(summary['tasks'])} open_merge_intents={summary['open_merge_intents']} "
        f"outbox {counts}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="auto_factory.worker")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true", help="offline full-FSM dry-run in a temp repo")
    mode.add_argument("--status", action="store_true", help="print task states (read-only)")
    mode.add_argument("--run", action="store_true", help="run the worker loop (real provider CLIs)")
    parser.add_argument("--db", help="coordinator DB path (default: external state dir)")
    parser.add_argument("--repo", default=".", help="repository root")
    parser.add_argument("--artifacts", help="artifacts dir (default: <state>/artifacts)")
    parser.add_argument("--worktrees", help="worktrees dir (default: <state>/worktrees)")
    parser.add_argument("--stop-flag", help="STOP.flag path (default: <state>/STOP.flag)")
    parser.add_argument("--auto-audit-l2", action="store_true", help="automated L2 Phase 1 audit (codex/gemini)")
    parser.add_argument("--max-cycles", type=int, default=1)
    args = parser.parse_args(argv)

    if args.demo:
        return run_demo()
    try:
        layout = resolve_state_paths(args.repo, args.db, args.artifacts, args.worktrees, args.stop_flag)
    except ValueError as exc:
        parser.error(str(exc))
    if args.status:
        return print_status_readonly(layout["db"])
    os.environ["TELEGRAM_BOT_TOKEN"] = ""
    lock = _FileLock(layout["lock"], role="supervisor")  # before the DB is opened
    try:
        lock.acquire()
    except SupervisorLocked as exc:
        print(f"[auto_factory] {exc}", file=sys.stderr)
        return 4
    try:
        db = CoordinatorDB(layout["db"])
        try:
            worker = PipelineWorker(
                db, args.repo, default_adapters(), artifacts_dir=layout["artifacts"],
                worktrees_dir=layout["worktrees"], stop_flag=layout["stop_flag"],
                holdout_dir=Path(args.repo) / "holdout_eval", auto_audit_l2=args.auto_audit_l2,
            )
            cycles = worker.run_forever(max_cycles=args.max_cycles, idle_sleep=0)
            print(f"[auto_factory] cycles={cycles} restarts={worker.restart_count}")
            return 0
        finally:
            db.close()
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
