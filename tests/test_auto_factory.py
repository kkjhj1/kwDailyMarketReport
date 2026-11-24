"""auto_factory Step 1 MVP tests. Offline only: MockAdapter, tmp_path git repos, TELEGRAM_BOT_TOKEN=""."""
from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

import auto_factory.adapters as adapters_mod
from auto_factory.adapters import (
    ClaudeAdapter,
    CodexAdapter,
    ContractTooLarge,
    FailureCode,
    GeminiAdapter,
    HandoffContract,
    MockAdapter,
    classify_failure,
    parse_retry_after,
    parse_verdict,
    route_writer,
    select_reviewer,
)
from auto_factory.db import (
    EXCEPTION_STATES,
    MAIN_FLOW,
    CASConflict,
    CoordinatorDB,
    GuardViolation,
    InvalidTransition,
)
from auto_factory.guardrails import (
    GuardrailChecker,
    OscillationDetector,
    check_paths,
    compare_sources,
    traceback_hash,
)
from auto_factory.win_job import (
    IS_WINDOWS,
    JobRunner,
    build_sandbox_env,
    is_same_process,
    process_start_time,
)
from auto_factory.worker import CIRCUIT, PipelineWorker, build_demo_repo
from auto_factory.worktree import ChangedFile, WorktreeConflict, WorktreeManager

REPO_ROOT = Path(__file__).resolve().parents[1]
FIX = "def add(a, b):\n    return a + b\n"


def _fake_os(name: str, access=None):
    """Stand-in for `adapters.os` so os.name can vary without breaking pathlib globally."""
    import types

    return types.SimpleNamespace(name=name, access=access or os.access, X_OK=os.X_OK)


class _RecordingRunner:
    """Fake JobRunner: records cmds, returns success, or raises a launch error."""

    def __init__(self, raise_exc: BaseException | None = None):
        self.cmds: list[list[str]] = []
        self.raise_exc = raise_exc

    def run(self, cmd, cwd=None, env=None, stop_check=None, **_):
        self.cmds.append(list(cmd))
        if self.raise_exc is not None:
            raise self.raise_exc
        from auto_factory.win_job import RunResult

        return RunResult(returncode=0, stdout="done", stderr="")


@pytest.fixture(autouse=True)
def _no_telegram(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")


class Clock:
    def __init__(self, t: float = 1_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def db(tmp_path):
    d = CoordinatorDB(tmp_path / "coord.db")
    yield d
    d.close()


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    build_demo_repo(r)
    return r


def _fix(wt, contract, role):
    Path(wt, "calc.py").write_text(FIX, encoding="utf-8")


def _advance(db, tid, states, actor="worker"):
    for s in states:
        v = db.get_task(tid)["state_version"]
        db.transition(tid, v, s, actor)


def make_worker(tmp_path, repo, db, claude=(), codex=(), gemini=(), **kw):
    holdout = tmp_path / "holdout"
    if not holdout.exists():
        holdout.mkdir()
        (holdout / "test_holdout_calc.py").write_text(
            "from calc import add\n\n\ndef test_holdout():\n    assert add(10, 5) == 15\n", encoding="utf-8"
        )
    adapters = {
        "claude": MockAdapter("claude", list(claude)),
        "codex": MockAdapter("codex", list(codex)),
        "gemini": MockAdapter("gemini", list(gemini)),
    }
    sleeps: list[float] = []
    kw.setdefault("sleep_fn", sleeps.append)
    w = PipelineWorker(
        db, repo, adapters, artifacts_dir=tmp_path / "artifacts", worktrees_dir=tmp_path / "wts",
        stop_flag=tmp_path / "STOP.flag", holdout_dir=holdout, **kw,
    )
    w.sleeps = sleeps
    return w


def submit(db, request="fix add", risk="L1", allowed=("calc.py",), start=True):
    tid, _ = db.create_task(request, risk, list(allowed), {"pytest": ["tests/test_calc.py"]})
    if start:
        db.add_approval(tid, "start")
    return tid


def assert_event_chain(db, tid):
    events = db.events(tid)
    assert events[0]["from_state"] is None and events[0]["to_state"] == "RECEIVED"
    for prev, cur in zip(events, events[1:]):
        assert cur["from_state"] == prev["to_state"]
    assert events[-1]["to_state"] == db.get_task(tid)["state"]


# ============================================================ DB / FSM
class TestCoordinatorDB:
    def test_wal_busy_timeout_and_tables(self, db):
        assert db.journal_mode.lower() == "wal"
        assert db._query("PRAGMA busy_timeout")[0][0] == 5000
        names = {r[0] for r in db._query("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"tasks", "attempts", "leases", "provider_budget", "artifacts", "events", "approvals",
                "circuit_state"} <= names

    def test_fsm_has_14_steps_and_exception_states(self):
        assert len(MAIN_FLOW) == 14
        assert set(EXCEPTION_STATES) == {
            "RETRY_WAIT", "RECOVERY_PENDING", "BLOCKED", "FAILED", "DISCARDED", "CANCELLED",
            "GUARDRAIL_BLOCKED", "SINGLE_PROVIDER_REVIEW_REQUIRED", "CANCEL_REQUESTED",
        }

    def test_cas_transition_writes_event_atomically(self, db):
        tid, created = db.create_task("r", "L1", ["a.py"], {})
        assert created
        assert db.transition(tid, 0, "PREFLIGHTED", "w") == 1
        with pytest.raises(CASConflict):
            db.transition(tid, 0, "CLASSIFIED", "w")
        assert [e["to_state"] for e in db.events(tid)] == ["RECEIVED", "PREFLIGHTED"]
        assert db.get_task(tid)["state"] == "PREFLIGHTED"

    def test_cas_race_between_two_connections(self, tmp_path):
        a = CoordinatorDB(tmp_path / "race.db")
        b = CoordinatorDB(tmp_path / "race.db")
        try:
            tid, _ = a.create_task("r", "L1", [], {})
            v = b.get_task(tid)["state_version"]
            a.transition(tid, v, "PREFLIGHTED", "a")
            with pytest.raises(CASConflict):
                b.transition(tid, v, "PREFLIGHTED", "b")
            assert len(a.events(tid)) == 2
        finally:
            a.close()
            b.close()

    def test_invalid_transition_leaves_no_event(self, db):
        tid, _ = db.create_task("r", "L1", [], {})
        with pytest.raises(InvalidTransition):
            db.transition(tid, 0, "MERGED", "human:x")
        assert len(db.events(tid)) == 1
        assert db.get_task(tid)["state_version"] == 0

    def test_idempotent_create(self, db):
        t1, c1 = db.create_task("same", "L1", ["a.py"], {"pytest": []})
        t2, c2 = db.create_task("same", "L1", ["a.py"], {"pytest": []})
        assert (t1, c1, c2) == (t2, True, False)
        assert len(db.list_tasks()) == 1

    def test_plan_ready_requires_audit_for_l2(self, db):
        tid, _ = db.create_task("r", "L2", [], {})
        _advance(db, tid, ["PREFLIGHTED", "CLASSIFIED", "PLANNING", "AUDITED"])
        with pytest.raises(GuardViolation, match="AUDIT_INCOMPLETE"):
            _advance(db, tid, ["PLAN_READY"])
        db.add_artifact(tid, "audit", "gpt_architecture.md", "codex", sha256="x")
        _advance(db, tid, ["PLAN_READY"])
        assert db.get_task(tid)["state"] == "PLAN_READY"

    def test_start_and_merge_approvals_are_bound(self, db):
        tid, _ = db.create_task("r", "L1", [], {})
        _advance(db, tid, ["PREFLIGHTED", "CLASSIFIED", "PLANNING", "AUDITED", "PLAN_READY", "AWAITING_START_AUTH"])
        with pytest.raises(GuardViolation, match="start authorization"):
            _advance(db, tid, ["WORKTREE_READY"])
        db.add_approval(tid, "start")
        _advance(db, tid, ["WORKTREE_READY", "IMPLEMENTING", "CHECKPOINTED", "VERIFYING", "REVIEWING"])
        v = db.get_task(tid)["state_version"]
        db.transition(tid, v, "AWAITING_MERGE", "w",
                      fields={"target_sha": "a" * 40, "diff_sha256": "d1", "base_sha": "b" * 40})
        with pytest.raises(GuardViolation, match="human"):
            _advance(db, tid, ["MERGED"], actor="worker")
        # Step 2: a direct transition to MERGED is always refused, approved or not.
        with pytest.raises(GuardViolation, match="finalize_merge"):
            _advance(db, tid, ["MERGED"], actor="human:owner")
        with pytest.raises(GuardViolation, match="merge approval"):
            db.begin_merge_intent(tid, "human:owner")
        db.add_approval(tid, "merge", target_sha="a" * 40, diff_sha256="STALE")
        assert not db.valid_merge_approval(tid)
        with pytest.raises(GuardViolation):
            db.begin_merge_intent(tid, "human:owner")
        db.add_approval(tid, "merge", target_sha="a" * 40, diff_sha256="d1")
        assert db.valid_merge_approval(tid)
        with pytest.raises(GuardViolation, match="finalize_merge"):
            _advance(db, tid, ["MERGED"], actor="human:owner")
        with pytest.raises(GuardViolation, match="human"):
            db.begin_merge_intent(tid, "worker")
        intent = db.begin_merge_intent(tid, "human:owner")
        with pytest.raises(GuardViolation, match="GIT_MERGED"):
            db.finalize_merge(intent, "a" * 40)  # Git not verified yet
        db.mark_merge_intent_git_merged(intent)
        with pytest.raises(GuardViolation, match="main HEAD"):
            db.finalize_merge(intent, "b" * 40)
        db.finalize_merge(intent, "a" * 40)
        assert db.get_task(tid)["state"] == "MERGED"
        assert db.get_merge_intent(intent)["status"] == "COMPLETED"
        assert_event_chain(db, tid)

    def test_lease_exclusive_until_expiry(self, tmp_path):
        clock = Clock()
        d = CoordinatorDB(tmp_path / "l.db", clock=clock)
        try:
            tid, _ = d.create_task("r", "L1", [], {})
            assert d.acquire_lease(tid, "A", 1, 1.0, ttl=10)
            assert not d.acquire_lease(tid, "B", 2, 2.0, ttl=10)
            clock.t += 11
            assert d.acquire_lease(tid, "B", 2, 2.0, ttl=10)
            assert not d.heartbeat(tid, "A")
        finally:
            d.close()


# ============================================================ quota
class TestQuota:
    def test_70_percent_cap_and_window_roll(self, tmp_path):
        clock = Clock()
        d = CoordinatorDB(tmp_path / "q.db", clock=clock)
        try:
            d.ensure_provider("claude", capacity_units=10, window_seconds=100)
            assert all(d.reserve("claude", 1) for _ in range(7))
            assert not d.reserve("claude", 1)  # 30% kept for the human
            d.settle("claude", 7, 7)
            assert not d.can_reserve("claude", 1)
            clock.t += 101
            assert d.reserve("claude", 1)
        finally:
            d.close()

    def test_cooldown_blocks_reservation(self, tmp_path):
        clock = Clock()
        d = CoordinatorDB(tmp_path / "q.db", clock=clock)
        try:
            d.ensure_provider("codex", 100)
            d.set_provider_health("codex", "rate_limit", clock.t + 60)
            assert not d.reserve("codex", 1)
            clock.t += 61
            assert d.reserve("codex", 1)
        finally:
            d.close()

    def test_worker_skips_provider_over_70_percent(self, tmp_path, repo, db):
        w = make_worker(tmp_path, repo, db, codex=[_fix])
        db.settle("claude", 0, 70)  # claude already at the automation ceiling
        tid = submit(db)
        assert w.process_task(tid) == "AWAITING_MERGE"
        assert db.get_task(tid)["coder"] == "codex"
        assert w.adapters["claude"].calls == []


# ============================================================ routing
class TestRouting:
    ALL = ["claude", "codex", "gemini"]

    def test_failover_chain_claude_codex_gemini(self):
        d = route_writer("L1", self.ALL)
        assert (d.outcome, d.writer, d.reviewer, d.failover) == ("PROCEED", "claude", "gemini", False)
        d = route_writer("L1", ["codex", "gemini"])
        assert (d.writer, d.reviewer, d.failover) == ("codex", "gemini", True)
        d = route_writer("L2", ["codex", "gemini"], failed_writers={"codex"})
        assert (d.writer, d.reviewer) == ("gemini", "codex")

    def test_coder_never_reviews_own_work(self):
        import itertools

        for n in (2, 3):
            for avail in itertools.combinations(self.ALL, n):
                for coder in avail:
                    assert select_reviewer(coder, avail) not in (coder, None)
        assert select_reviewer("claude", ["claude"]) is None

    def test_l3_fail_closed(self):
        assert route_writer("L3", ["claude", "codex"]).outcome == "BLOCKED"
        assert route_writer("L3", self.ALL, failed_writers={"claude"}).outcome == "BLOCKED"
        d = route_writer("L3", self.ALL)
        assert (d.outcome, d.writer) == ("PROCEED", "claude")

    def test_single_survivor_boundary(self):
        assert route_writer("L0", ["gemini"]).outcome == "SINGLE_PROVIDER_REVIEW_REQUIRED"
        assert route_writer("L1", ["gemini"]).outcome == "BLOCKED"
        assert route_writer("L2", ["codex"]).outcome == "BLOCKED"
        assert route_writer("L1", ["claude"], has_valid_audit_plan=False).outcome == "BLOCKED"
        d = route_writer("L2", ["claude"], has_valid_audit_plan=True)
        assert (d.outcome, d.writer, d.reviewer) == ("SINGLE_PROVIDER_REVIEW_REQUIRED", "claude", None)
        assert route_writer("L1", []).outcome == "BLOCKED"

    def test_failure_classification(self):
        assert classify_failure(1, "", "HTTP 429 Too Many Requests") == FailureCode.RATE_LIMIT
        assert classify_failure(1, "", "You have hit your usage limit") == FailureCode.QUOTA
        assert classify_failure(1, "RESOURCE_EXHAUSTED 429", "") == FailureCode.QUOTA
        assert classify_failure(1, "", "Error: not logged in") == FailureCode.AUTH
        assert classify_failure(1, "", "Traceback ... KeyError") == FailureCode.INTERNAL
        assert classify_failure(None, timed_out=True) == FailureCode.TIMEOUT
        assert classify_failure(0) == FailureCode.OK
        assert classify_failure(1, '{"error":{"status":"UNAUTHENTICATED"}}', "") == FailureCode.AUTH
        assert classify_failure(1, "", "status: UNAUTHENTICATED") == FailureCode.AUTH
        assert classify_failure(1, "DEADLINE_EXCEEDED", "") == FailureCode.TIMEOUT
        assert classify_failure(1, "", "error: Deadline_Exceeded") == FailureCode.TIMEOUT
        assert classify_failure(1, "Print_Timeout reached", "") == FailureCode.TIMEOUT
        assert classify_failure(1, "", "PRINT_TIMEOUT") == FailureCode.TIMEOUT
        assert parse_retry_after("retry-after: 42") == 42
        assert parse_retry_after("please try again in 5 minutes") == 300
        assert parse_verdict('blah\n{"verdict": "approve", "reason": "ok"}') == "approve"
        assert parse_verdict("no json here") is None

    def test_handoff_contract_roundtrip_and_2kb_cap(self):
        c = HandoffContract("t1", "L1", "a" * 40, "b" * 40, ["calc.py"], ["pytest tests"], "writer",
                            failure={"code": "GATE_PYTEST", "traceback_hash": "h", "attempt": 1}, diff_sha256="d")
        text = c.to_json()
        assert len(text.encode()) <= 2048
        assert HandoffContract.from_json(text) == c
        with pytest.raises(ContractTooLarge):
            HandoffContract("t", "L1", "a", "b", ["x" * 2100], [], "writer").to_json()

    def test_real_adapter_commands_and_missing_cli(self, tmp_path, monkeypatch):
        monkeypatch.setattr(adapters_mod, "os", _fake_os("posix"))
        assert CodexAdapter().build_command("writer", "p")[-3:] == ["-s", "workspace-write", "p"]
        assert CodexAdapter().build_command("reviewer", "p")[-3:] == ["-s", "read-only", "p"]
        assert GeminiAdapter().build_command("writer", "p")[3:5] == ["--mode", "accept-edits"]
        assert GeminiAdapter().build_command("reviewer", "p")[3:5] == ["--mode", "plan"]
        reviewer = ClaudeAdapter().build_command("reviewer", "p")
        assert "Read,Grep,Glob" in reviewer
        assert reviewer[reviewer.index("--permission-mode") + 1] == "plan"
        assert "--allowedTools" not in reviewer and "--max-budget-usd" not in reviewer
        assert "Edit" not in reviewer[reviewer.index("--tools") + 1]
        writer = ClaudeAdapter().build_command("writer", "p")
        assert writer[writer.index("--permission-mode") + 1] == "acceptEdits"
        assert writer[writer.index("--allowedTools") + 1] == (
            "Read,Grep,Glob,Edit,Write,Bash(python *),Bash(pytest *)"
        )
        assert writer[writer.index("--max-budget-usd") + 1] == "2.0"
        assert "--max-budget-usd" not in ClaudeAdapter(max_budget_usd=None).build_command("writer", "p")
        assert ClaudeAdapter(max_budget_usd=0.5).build_command("writer", "p")[
            writer.index("--max-budget-usd") + 1
        ] == "0.5"
        missing = ClaudeAdapter(executable="definitely-not-installed-cli-xyz")
        assert not missing.available()
        c = HandoffContract("t", "L1", "a", "b", [], [], "writer")
        assert missing.invoke(c, str(tmp_path), "writer", "x.json").code == FailureCode.UNAVAILABLE

    @pytest.mark.parametrize("osname", ["nt", "posix"])
    def test_codex_command_windows_sandbox_flag(self, monkeypatch, osname):
        monkeypatch.setattr(adapters_mod, "os", _fake_os(osname))
        for role, sandbox in (("writer", "workspace-write"), ("reviewer", "read-only")):
            cmd = CodexAdapter().build_command(role, "PROMPT")
            assert cmd[:5] == ["codex", "exec", "--skip-git-repo-check", "-s", sandbox]
            assert cmd[-1] == "PROMPT"
            if osname == "nt":
                assert cmd[5:] == ["-c", "windows.sandbox=unelevated", "PROMPT"]
            else:
                assert cmd[5:] == ["PROMPT"]
                assert "windows.sandbox=unelevated" not in cmd

    def test_gemini_agy_command(self):
        import json

        for role, mode in (("writer", "accept-edits"), ("reviewer", "plan")):
            cmd = GeminiAdapter().build_command(role, "PROMPT")
            assert cmd[:3] == ["agy", "-p", "PROMPT"]
            assert cmd[cmd.index("--mode") + 1] == mode
            assert "--approval-mode" not in cmd
            raw = cmd[cmd.index("--settings") + 1]
            assert "\n" not in raw and ", " not in raw and ": " not in raw
            perms = json.loads(raw)["permissions"]
            assert perms["allow"] == ["command(regex:.*)"]
            assert perms["deny"] == ["command(regex:.*git\\s+(push|commit|merge|reset|checkout|clean).*)"]
            assert cmd[-2:] == ["--model", "gemini-3.8-flash-medium"]
        assert GeminiAdapter(model="gemini-pro-x").build_command("writer", "p")[-2:] == ["--model", "gemini-pro-x"]
        assert "--model" not in GeminiAdapter(model=None).build_command("writer", "p")
        assert GeminiAdapter(model=None).build_command("writer", "p")[-2] == "--settings"

    def test_gemini_resolve_executable_path(self, monkeypatch, tmp_path):
        monkeypatch.setattr(adapters_mod.shutil, "which", lambda name: "/usr/bin/agy" if name == "agy" else None)
        monkeypatch.setattr(adapters_mod.Path, "home", classmethod(lambda cls: tmp_path))
        g = GeminiAdapter()
        assert g.resolve_executable() == "/usr/bin/agy"
        assert g.available()

    @pytest.mark.parametrize("osname,exe_name", [("nt", "agy.exe"), ("posix", "agy")])
    def test_gemini_resolve_executable_fallback(self, monkeypatch, tmp_path, osname, exe_name):
        monkeypatch.setattr(adapters_mod.shutil, "which", lambda name: None)
        monkeypatch.setattr(adapters_mod.Path, "home", classmethod(lambda cls: tmp_path))
        executable_bit = {"value": True}
        access_calls = []

        def fake_access(path, mode):
            access_calls.append((Path(path), mode))
            return executable_bit["value"]

        monkeypatch.setattr(adapters_mod, "os", _fake_os(osname, access=fake_access))
        runner = _RecordingRunner()
        g = GeminiAdapter(runner)
        c = HandoffContract("t", "L1", "a", "b", [], [], "writer")
        candidate = tmp_path / ".gemini" / "bin" / exe_name

        # Missing on PATH and fallback -> unavailable, runner never called.
        assert g.resolve_executable() is None
        assert not g.available()
        assert g.invoke(c, str(tmp_path), "writer", "x.json").code == FailureCode.UNAVAILABLE
        assert runner.cmds == []

        candidate.parent.mkdir(parents=True)
        candidate.write_bytes(b"")
        assert g.resolve_executable() == str(candidate)
        assert g.available()
        res = g.invoke(c, str(tmp_path), "writer", "x.json")
        assert res.code == FailureCode.OK
        assert runner.cmds[-1][0] == str(candidate)
        assert runner.cmds[-1][1] == "-p"

        if osname == "posix":
            assert access_calls and access_calls[-1] == (candidate, adapters_mod.os.X_OK)
            executable_bit["value"] = False
            assert g.resolve_executable() is None
            assert not g.available()
            assert g.invoke(c, str(tmp_path), "writer", "x.json").code == FailureCode.UNAVAILABLE
        else:
            assert access_calls == []

    @pytest.mark.parametrize("exc", [FileNotFoundError("gone"), PermissionError("denied")])
    def test_invoke_launch_errors_are_unavailable(self, monkeypatch, tmp_path, exc):
        monkeypatch.setattr(adapters_mod.shutil, "which", lambda name: f"/bin/{name}")
        adapter = CodexAdapter(_RecordingRunner(raise_exc=exc))
        c = HandoffContract("t", "L1", "a", "b", [], [], "writer")
        res = adapter.invoke(c, str(tmp_path), "writer", "x.json")
        assert res.code == FailureCode.UNAVAILABLE
        assert str(exc) in res.stderr


# ============================================================ guardrails
BASE = "import pytest\n\n\ndef test_a():\n    assert 1 == 1\n    assert 2 == 2\n\n\nclass TestX:\n    def test_b(self):\n        self.assertEqual(1, 1)\n"


class TestGuardrails:
    def test_forbidden_and_allowed_paths(self):
        changed = [
            ChangedFile("M", ".env"), ChangedFile("A", "data/x.sqlite3"), ChangedFile("M", "reports/a.html"),
            ChangedFile("M", "deploy_to_oracle.ps1"), ChangedFile("M", "holdout_eval/t.py"),
            ChangedFile("M", "pytest.ini"), ChangedFile("A", "pkg/conftest.py"),
            ChangedFile("M", "calc.py"), ChangedFile("M", "other.py"),
            ChangedFile("R", "calc2.py", old_path="reports/old.py"),
        ]
        v = check_paths(changed, ["calc.py", "calc2.py"])
        forb = {x.path for x in v if x.code == "FORBIDDEN_PATH"}
        assert forb == {".env", "data/x.sqlite3", "reports/a.html", "deploy_to_oracle.ps1", "holdout_eval/t.py",
                        "pytest.ini", "pkg/conftest.py", "reports/old.py"}
        assert {x.path for x in v if x.code == "OUTSIDE_ALLOWED_PATHS"} == {"other.py"}

    @pytest.mark.parametrize("cand, code", [
        (BASE.replace("def test_a", "@pytest.mark.skip\ndef test_a"), "SKIP_ADDED"),
        (BASE.replace("def test_a", "@pytest.mark.xfail(reason='x')\ndef test_a"), "SKIP_ADDED"),
        (BASE.replace("    assert 1 == 1\n", "    pytest.skip('later')\n    assert 1 == 1\n"), "SKIP_ADDED"),
        (BASE + "\npytestmark = pytest.mark.skip\n", "SKIP_ADDED"),
        (BASE.replace("    def test_b", "    @unittest.expectedFailure\n    def test_b"), "SKIP_ADDED"),
        (BASE.replace("    assert 2 == 2\n", ""), "ASSERT_REDUCED"),
        (BASE.replace("self.assertEqual(1, 1)", "pass"), "ASSERT_REDUCED"),
        (BASE.replace("def test_a", "def helper_a"), "TEST_REMOVED"),
        (BASE + "\ntry:\n    x = 1\nexcept Exception:\n    pass\n", "BROAD_EXCEPT_PASS"),
        (None, "TEST_FILE_DELETED"),
        ("def broken(:\n", "SYNTAX_ERROR"),
    ])
    def test_ast_anti_cheating(self, cand, code):
        violations = compare_sources({"tests/test_m.py": BASE}, {"tests/test_m.py": cand})
        assert code in {v.code for v in violations}

    def test_unchanged_or_strengthened_tests_pass(self):
        stronger = BASE.replace("    assert 2 == 2\n", "    assert 2 == 2\n    assert 3 == 3\n")
        assert compare_sources({"tests/test_m.py": BASE}, {"tests/test_m.py": stronger}) == []
        assert compare_sources({}, {"tests/test_new.py": BASE}) == []

    def test_broad_except_in_source_file(self):
        src = "def f():\n    return 1\n"
        bad = "def f():\n    try:\n        return 1\n    except:\n        pass\n"
        assert {v.code for v in compare_sources({"m.py": src}, {"m.py": bad})} == {"BROAD_EXCEPT_PASS"}

    def test_oscillation_detector(self):
        d = OscillationDetector(max_retries=3)
        assert [d.record(h, 2) for h in ("a", "b")] == ["RETRY", "RETRY"]
        assert d.record("a", 2) == "ABORT_DUPLICATE"  # A-B-A
        d = OscillationDetector()
        assert d.record("a", 1) == "RETRY"
        assert d.record("b", 3) == "ABORT_REGRESSION"
        d = OscillationDetector(max_retries=3)
        assert [d.record(h, 1) for h in "abcd"] == ["RETRY", "RETRY", "RETRY", "ABORT_MAX_RETRIES"]
        assert OscillationDetector().record(None) == "PASS"

    def test_traceback_hash_normalizes_noise(self):
        a = 'E   AssertionError at 0x7ff1\nFAILED tests/test_calc.py::test_add - assert -1 == 5\nin 0.12s'
        b = 'E   AssertionError at 0x1234\nFAILED tests/test_calc.py::test_add - assert -1 == 5\nin 9.99s'
        assert traceback_hash(a) == traceback_hash(b) is not None
        assert traceback_hash("FAILED tests/test_calc.py::test_other") != traceback_hash(a)
        assert traceback_hash("all good") is None

    def test_checker_on_real_worktree(self, tmp_path, repo):
        wtm = WorktreeManager(repo, tmp_path / "wts")
        info = wtm.create("g1")
        Path(info.path, ".env").write_text("X=1\n", encoding="utf-8")
        Path(info.path, "calc.py").write_text(
            FIX + 'TELEGRAM_BOT_TOKEN = "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"\n', encoding="utf-8"
        )
        wtm.commit_wip_checkpoint(info.path, "cheat")
        report = GuardrailChecker(wtm).check_worktree(info.path, info.base_sha, ["calc.py"])
        assert {"FORBIDDEN_PATH", "SECRET_LEAK"} <= report.codes()
        assert not report.ok


# ============================================================ worktree
class TestWorktree:
    def test_isolated_worktree_checkpoint_and_cleanup(self, tmp_path, repo, monkeypatch):
        (repo / "calc.py").write_text("DIRTY = True\n", encoding="utf-8")  # dirty main workspace
        wtm = WorktreeManager(repo, tmp_path / "wts")
        info = wtm.create("t-wt1")
        assert info.branch == "auto/t-wt1"
        assert info.base_sha == wtm.head_sha()
        assert "DIRTY" not in Path(info.path, "calc.py").read_text(encoding="utf-8")
        with pytest.raises(WorktreeConflict):
            wtm.create("t-wt1")
        Path(info.path, "calc.py").write_text(FIX, encoding="utf-8")
        (Path(info.path) / ".auto_factory").mkdir()
        Path(info.path, ".auto_factory", "handoff.json").write_text("{}", encoding="utf-8")
        assert wtm.is_dirty(info.path)
        sha = wtm.commit_wip_checkpoint(info.path, "first")
        assert sha != info.base_sha and not wtm.is_dirty(info.path)
        assert [c.path for c in wtm.changed_files(info.path, info.base_sha)] == ["calc.py"]
        assert wtm.commit_wip_checkpoint(info.path, "empty ok") != sha
        monkeypatch.chdir(info.path)
        patch = wtm.cleanup(info, tmp_path / "artifacts")
        assert Path.cwd().resolve() == repo.resolve()
        assert not Path(info.path).exists()
        assert b"return a + b" in patch.read_bytes()
        assert "auto/t-wt1" in wtm.git("branch", "--list", "auto/t-wt1")

    def test_worktrees_dir_must_be_outside_repo(self, repo):
        from auto_factory.worktree import WorktreeError

        with pytest.raises(WorktreeError):
            WorktreeManager(repo, repo / "wts")


# ============================================================ pipeline
class TestPipeline:
    def test_failover_to_awaiting_merge_with_independent_review(self, tmp_path, repo, db):
        w = make_worker(tmp_path, repo, db, claude=["RATE_LIMIT"], codex=["TIMEOUT"], gemini=[_fix])
        tid = submit(db)
        assert w.process_task(tid) == "AWAITING_MERGE"
        t = db.get_task(tid)
        assert t["coder"] == "gemini"
        writers = [(a["provider"], a["failure_code"]) for a in db.attempts(tid, "writer")]
        assert writers == [("claude", "RATE_LIMIT"), ("codex", "TIMEOUT"), ("gemini", "OK")]
        reviewers = [a["provider"] for a in db.attempts(tid, "reviewer")]
        assert reviewers == ["codex"] and t["coder"] not in reviewers
        assert db.provider_status("claude")["health"] == "rate_limit"
        assert t["target_sha"] == w.wtm.head_sha(t["worktree_path"])
        assert t["diff_sha256"] == w.wtm.diff_sha256(t["worktree_path"], t["base_sha"])
        assert not db.valid_merge_approval(tid)  # never auto-merged
        assert_event_chain(db, tid)
        assert len(db.artifacts(tid, "handoff")) == 4
        assert db.get_lease(tid) is None

    def test_cheating_writer_is_guardrail_blocked(self, tmp_path, repo, db):
        def cheat(wt, contract, role):
            p = Path(wt, "tests", "test_calc.py")
            p.write_text("import pytest\n" + p.read_text(encoding="utf-8").replace(
                "def test_add", "@pytest.mark.skip\ndef test_add"), encoding="utf-8")

        w = make_worker(tmp_path, repo, db, claude=[cheat])
        tid = submit(db, allowed=("calc.py", "tests/test_calc.py"))
        assert w.process_task(tid) == "GUARDRAIL_BLOCKED"
        assert "SKIP_ADDED" in db.events(tid)[-1]["reason"]

    def test_self_heal_aborts_on_duplicate_traceback(self, tmp_path, repo, db):
        w = make_worker(tmp_path, repo, db)  # writers never fix the bug
        tid = submit(db)
        assert w.process_task(tid) == "BLOCKED"
        t = db.get_task(tid)
        assert t["retry_count"] == 1
        assert "ABORT_DUPLICATE" in db.events(tid)[-1]["reason"]
        assert "RETRY_WAIT" in [e["to_state"] for e in db.events(tid)]
        verifies = db.attempts(tid, "verify")
        assert len(verifies) == 2 and verifies[0]["traceback_hash"] == verifies[1]["traceback_hash"]
        writer_contracts = db.artifacts(tid, "handoff")
        assert len(writer_contracts) == 2

    def test_holdout_catches_overfit_candidate(self, tmp_path, repo, db):
        def overfit(wt, contract, role):
            Path(wt, "calc.py").write_text(
                "def add(a, b):\n    return 0 if (a, b) == (10, 5) else a + b\n", encoding="utf-8")

        w = make_worker(tmp_path, repo, db, claude=[overfit])
        tid = submit(db)
        assert w.process_task(tid) == "BLOCKED"
        assert db.attempts(tid, "verify")[0]["failure_code"] == "GATE_HOLDOUT"

    def test_internal_failure_blocks_without_substitute(self, tmp_path, repo, db):
        w = make_worker(tmp_path, repo, db, claude=["INTERNAL"])
        tid = submit(db)
        assert w.process_task(tid) == "BLOCKED"
        assert "IMPLEMENTATION_FAILED" in db.events(tid)[-1]["reason"]
        assert w.adapters["codex"].calls == [] and w.adapters["gemini"].calls == []

    def test_l3_and_unaudited_l2_are_blocked(self, tmp_path, repo, db):
        w = make_worker(tmp_path, repo, db)
        t3 = submit(db, "deploy", risk="L3")
        t2 = submit(db, "logic change", risk="L2")
        assert w.process_task(t3) == "BLOCKED"
        assert w.process_task(t2) == "BLOCKED"
        assert "AUDIT_INCOMPLETE" in db.events(t2)[-1]["reason"]
        assert all(a.calls == [] for a in w.adapters.values())

    def test_audited_l2_proceeds(self, tmp_path, repo, db):
        w = make_worker(tmp_path, repo, db, claude=[_fix])
        tid = submit(db, "logic change", risk="L2")
        db.add_artifact(tid, "plan", "plan.md", "gemini", sha256="p")
        db.add_artifact(tid, "audit", "gpt_architecture.md", "codex", sha256="a")
        assert w.process_task(tid) == "AWAITING_MERGE"

    def test_single_provider_review_required(self, tmp_path, repo, db):
        w = make_worker(tmp_path, repo, db, claude=[_fix])
        w.adapters["codex"].is_available = False
        w.adapters["gemini"].is_available = False
        tid = submit(db)
        assert w.process_task(tid) == "SINGLE_PROVIDER_REVIEW_REQUIRED"
        t = db.get_task(tid)
        assert t["single_provider"] is True
        assert db.attempts(tid, "reviewer") == []
        # Step 2: the candidate is bound to its exact commit + diff for the manual review.
        assert t["target_sha"] == w.wtm.head_sha(t["worktree_path"])
        assert t["diff_sha256"] == w.wtm.diff_sha256(t["worktree_path"], t["base_sha"])

    def test_waits_for_start_authorization(self, tmp_path, repo, db):
        w = make_worker(tmp_path, repo, db)
        tid = submit(db, start=False)
        assert w.process_task(tid) == "AWAITING_START_AUTH"
        assert db.get_task(tid)["worktree_path"] is None
        assert w._runnable() == []


# ============================================================ worker control
class TestWorkerControl:
    def test_stop_flag_prevents_any_work(self, tmp_path, repo, db):
        w = make_worker(tmp_path, repo, db)
        tid = submit(db)
        (tmp_path / "STOP.flag").write_text("stop", encoding="utf-8")
        assert w.run_cycle()["stopped"] is True
        assert db.get_task(tid)["state"] == "RECEIVED"

    def test_stop_mid_run_then_boot_recovery_resumes_once(self, tmp_path, repo, db):
        def stop_now(wt, contract, role):
            (tmp_path / "STOP.flag").write_text("stop", encoding="utf-8")

        w1 = make_worker(tmp_path, repo, db, claude=[stop_now])
        tid = submit(db)
        s = w1.run_cycle()
        assert s["stopped"] is True
        assert db.get_task(tid)["state"] == "IMPLEMENTING"
        assert db.get_lease(tid) is None

        (tmp_path / "STOP.flag").unlink()
        w2 = make_worker(tmp_path, repo, db, claude=[_fix])  # fresh boot, new nonce
        s = w2.run_cycle()
        assert s["recovered"] == [tid]
        assert db.get_task(tid)["state"] == "AWAITING_MERGE"
        states = [e["to_state"] for e in db.events(tid)]
        assert states.count("RECOVERY_PENDING") == 1
        assert len(db.attempts(tid, "writer")) == 2  # interrupted + resumed, no duplicate
        assert_event_chain(db, tid)

    def test_recovery_with_dirty_uncheckpointed_diff_blocks(self, tmp_path, repo, db):
        def crash_mid_edit(wt, contract, role):
            Path(wt, "calc.py").write_text("half-written", encoding="utf-8")
            (tmp_path / "STOP.flag").write_text("stop", encoding="utf-8")

        w1 = make_worker(tmp_path, repo, db, claude=[crash_mid_edit])
        tid = submit(db)
        w1.run_cycle()
        (tmp_path / "STOP.flag").unlink()
        w2 = make_worker(tmp_path, repo, db)
        w2.run_cycle()
        assert db.get_task(tid)["state"] == "BLOCKED"
        patch = db.artifacts(tid, "recovery_patch")
        assert patch and b"half-written" in Path(patch[0]["path"]).read_bytes()

    def test_recovery_skips_live_lease_owner(self, tmp_path, repo, db):
        w = make_worker(tmp_path, repo, db)
        tid = submit(db)
        _advance(db, tid, ["PREFLIGHTED", "CLASSIFIED", "PLANNING", "AUDITED", "PLAN_READY",
                           "AWAITING_START_AUTH", "WORKTREE_READY", "IMPLEMENTING"])
        pid = os.getpid()
        assert db.acquire_lease(tid, "other-live-worker", pid, process_start_time(pid))
        assert w.recover_on_boot() == []
        db.force_release_lease(tid)
        assert db.acquire_lease(tid, "dead-worker", pid, 12345.0)  # pid reused: start time differs
        assert w.recover_on_boot() == [tid]

    def test_circuit_breaker_opens_after_3_failures(self, tmp_path, repo, db):
        w = make_worker(tmp_path, repo, db, circuit_cooldown=300)
        for i in range(4):
            submit(db, f"deploy {i}", risk="L3")
        s = w.run_cycle()
        assert len(s["processed"]) == 3 and s["circuit_open"] is True
        assert w.sleeps == [300]
        assert db.circuit_is_open(CIRCUIT)
        s = w.run_cycle()
        assert s["processed"] == [] and s["circuit_open"] is True
        assert w.sleeps == [300, 300]

    def test_max_tasks_per_cycle_restarts_worker(self, tmp_path, repo, db):
        w = make_worker(tmp_path, repo, db, circuit_threshold=100)
        for i in range(6):
            submit(db, f"deploy {i}", risk="L3")
        nonce = w.nonce
        s = w.run_cycle()
        assert len(s["processed"]) == 5
        assert w.restart_count == 1 and w.nonce != nonce
        assert len(w.run_cycle()["processed"]) == 1
        assert w.restart_count == 1


# ============================================================ win_job
def _py(code: str) -> list[str]:
    return [sys.executable, "-c", textwrap.dedent(code)]


class TestJobRunner:
    def test_env_allowlist_and_telegram_disabled(self):
        base = {"PATH": "p", "SYSTEMROOT": "s", "OPENAI_API_KEY": "sk-x", "TELEGRAM_BOT_TOKEN": "1:x",
                "AWS_SECRET_ACCESS_KEY": "y", "RANDOM_VAR": "z"}
        env = build_sandbox_env(base=base)
        assert env["TELEGRAM_BOT_TOKEN"] == ""
        assert "OPENAI_API_KEY" not in env and "AWS_SECRET_ACCESS_KEY" not in env and "RANDOM_VAR" not in env
        assert env["PATH"] == "p"
        with pytest.raises(ValueError):
            build_sandbox_env({"GITHUB_TOKEN": "x"}, base=base)
        assert build_sandbox_env(passthrough=["OPENAI_API_KEY"], base=base)["OPENAI_API_KEY"] == "sk-x"

    def test_run_captures_output_with_devnull_stdin(self, monkeypatch):
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "leaked")
        res = JobRunner().run(_py("""
            import os, sys
            print(repr(sys.stdin.read()), repr(os.environ.get("TELEGRAM_BOT_TOKEN")))
        """))
        assert res.ok and res.stdout.strip() == "'' ''"
        assert res.job_contained is IS_WINDOWS

    def test_wall_timeout_kills_whole_tree(self, tmp_path):
        pidfile = tmp_path / "grandchild.pid"
        res = JobRunner(poll_interval=0.05).run(_py(f"""
            import subprocess, sys, time
            p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
            open(r"{pidfile}", "w").write(str(p.pid))
            while True:
                print("tick", flush=True)
                time.sleep(0.2)
        """), wall_timeout=4, idle_timeout=60)
        assert res.timed_out and res.timeout_kind == "wall"
        gpid = int(pidfile.read_text())
        deadline = time.time() + 10
        while process_start_time(gpid) is not None and time.time() < deadline:
            time.sleep(0.2)
        assert process_start_time(gpid) is None, "grandchild escaped the job"

    def test_idle_timeout(self):
        res = JobRunner().run(_py("import time; time.sleep(60)"), wall_timeout=30, idle_timeout=1.5)
        assert res.timeout_kind == "idle" and not res.ok
        assert res.duration < 20

    def test_stop_check_kills(self, tmp_path):
        flag = tmp_path / "STOP.flag"
        res = JobRunner().run(_py(f"""
            import time
            open(r"{flag}", "w").write("x")
            time.sleep(60)
        """), stop_check=flag.exists, wall_timeout=30, idle_timeout=30)
        assert res.timeout_kind == "stop"

    def test_process_identity(self):
        pid = os.getpid()
        start = process_start_time(pid)
        assert start is not None
        assert is_same_process(pid, start)
        assert not is_same_process(pid, start - 1000)
        p = subprocess.Popen([sys.executable, "-c", "pass"])
        p.wait()
        assert process_start_time(p.pid) is None
        assert not is_same_process(p.pid, start)


# ============================================================ CLI
def test_demo_cli_runs_offline():
    env = build_sandbox_env({"PYTHONPATH": str(REPO_ROOT)})
    proc = subprocess.run(
        [sys.executable, "-m", "auto_factory.worker", "--demo"], cwd=str(REPO_ROOT), env=env,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "RESULT PASS" in proc.stdout
    assert "AWAITING_MERGE" in proc.stdout and "GUARDRAIL_BLOCKED" in proc.stdout


# ######################################################################## STEP 2
import json as _json
import sqlite3 as _sqlite3

import auto_factory.dev_bot as dev_bot_mod
from auto_factory.db import ActionRejected, MergeIntentError
from auto_factory.dev_bot import (
    BotConfig,
    ConfigError,
    DevBot,
    FakeTransport,
    HttpTransport,
    MergeCoordinator,
    MessageTooLong,
    RateLimiter,
    SafeHtmlFormatter,
    TaskParseError,
    TgRejected,
    TgRetryAfter,
    TgUnknown,
    load_config,
    parse_task_command,
    validate_callback_data,
)
from auto_factory.worker import validate_db_path
from auto_factory.worktree import MergeConflictError, MergePreconditionError, _FileLock

ADMIN = 123456789
OTHER = 987654321
VALID_TOKEN = "123456789:" + "A" * 35


def _no_limit() -> RateLimiter:
    return RateLimiter(global_per_sec=10**6, per_chat_interval=0.0, sleep=lambda s: None)


def make_bot(tmp_path, repo, db, boot=True, **kw):
    tr = FakeTransport()
    bot = DevBot(
        db, tr, ADMIN, repo, artifacts_dir=tmp_path / "artifacts", worktrees_dir=tmp_path / "wts",
        stop_flag=tmp_path / "STOP.flag", limiter=kw.pop("limiter", _no_limit()), **kw,
    )
    if boot:
        bot.boot()
    return bot, tr


_UPD = [0]


def upd_msg(text, frm=ADMIN, chat=ADMIN):
    _UPD[0] += 1
    return {"update_id": _UPD[0], "message": {"message_id": _UPD[0], "from": {"id": frm}, "chat": {"id": chat},
                                              "text": text}}


def upd_cb(op, token, frm=ADMIN, chat=ADMIN, message_id=7):
    _UPD[0] += 1
    return {"update_id": _UPD[0], "callback_query": {
        "id": f"cq{_UPD[0]}", "from": {"id": frm}, "data": f"af2|{op}|{token}",
        "message": {"message_id": message_id, "chat": {"id": chat}}}}


def _to_state(db, tid, state):
    path = {
        "AWAITING_START_AUTH": ["PREFLIGHTED", "CLASSIFIED", "PLANNING", "AUDITED", "PLAN_READY", "AWAITING_START_AUTH"],
    }[state]
    _advance(db, tid, path)


def _fake_awaiting_merge(db, tid, target="a" * 40, diff="d" * 64):
    _to_state(db, tid, "AWAITING_START_AUTH")
    db.add_approval(tid, "start")
    _advance(db, tid, ["WORKTREE_READY", "IMPLEMENTING", "CHECKPOINTED", "VERIFYING", "REVIEWING"])
    v = db.get_task(tid)["state_version"]
    db.transition(tid, v, "AWAITING_MERGE", "w", fields={"target_sha": target, "diff_sha256": diff})


def awaiting_merge(tmp_path, repo, db):
    w = make_worker(tmp_path, repo, db, claude=[_fix])
    tid = submit(db)
    assert w.process_task(tid) == "AWAITING_MERGE"
    return w, tid


def approve_merge(db, tid, ttl=600):
    t = db.get_task(tid)
    db.add_approval(tid, "merge", t["target_sha"], t["diff_sha256"], ttl=ttl)


def _git_main(repo, *args):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@localhost", *args], cwd=str(repo),
                   env=build_sandbox_env(), stdin=subprocess.DEVNULL, capture_output=True, check=True)


# ============================================================ config / auth
class TestDevBotConfig:
    def test_requires_dedicated_credentials_no_product_fallback(self):
        with pytest.raises(ConfigError):
            load_config({})
        with pytest.raises(ConfigError):  # product token is never used as a fallback
            load_config({"TELEGRAM_BOT_TOKEN": VALID_TOKEN, "AUTO_FACTORY_ADMIN_CHAT_ID": str(ADMIN)})
        for bad in ("123:abc", "abc:" + "A" * 35, "1234567:" + "A" * 35, VALID_TOKEN + "x"):
            with pytest.raises(ConfigError):
                load_config({"AUTO_FACTORY_DEV_BOT_TOKEN": bad, "AUTO_FACTORY_ADMIN_CHAT_ID": str(ADMIN)})
        for chat in ("", "0", "abc", "1.5"):
            with pytest.raises(ConfigError):
                load_config({"AUTO_FACTORY_DEV_BOT_TOKEN": VALID_TOKEN, "AUTO_FACTORY_ADMIN_CHAT_ID": chat})
        cfg = load_config({"AUTO_FACTORY_DEV_BOT_TOKEN": VALID_TOKEN, "AUTO_FACTORY_ADMIN_CHAT_ID": str(ADMIN)})
        assert cfg.admin_chat_id == ADMIN and VALID_TOKEN not in repr(cfg)

    @pytest.mark.parametrize("mode", ["--poll-once", "--run"])
    def test_live_modes_fail_closed_before_db_or_network(self, tmp_path, monkeypatch, mode):
        monkeypatch.delenv("AUTO_FACTORY_DEV_BOT_TOKEN", raising=False)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", VALID_TOKEN)
        monkeypatch.setenv("AUTO_FACTORY_ADMIN_CHAT_ID", str(ADMIN))
        monkeypatch.setattr(dev_bot_mod.urllib.request, "urlopen", lambda *a, **k: pytest.fail("network"))
        dbp = tmp_path / "state" / "c.db"
        assert dev_bot_mod.main([mode, "--db", str(dbp), "--repo", str(tmp_path / "nowhere")]) == 2
        assert not dbp.exists()

    def test_demo_reads_no_env_and_no_network(self, monkeypatch, capsys):
        monkeypatch.setattr(dev_bot_mod, "load_config", lambda *a, **k: pytest.fail("env read in demo"))
        monkeypatch.setattr(dev_bot_mod.urllib.request, "urlopen", lambda *a, **k: pytest.fail("network in demo"))
        monkeypatch.setattr(dev_bot_mod, "HttpTransport", lambda *a, **k: pytest.fail("transport in demo"))
        assert dev_bot_mod.main(["--demo"]) == 0
        assert "RESULT PASS" in capsys.readouterr().out

    def test_sandbox_env_scrubs_dev_bot_credentials(self):
        base = {"PATH": "p", "AUTO_FACTORY_DEV_BOT_TOKEN": VALID_TOKEN, "AUTO_FACTORY_ADMIN_CHAT_ID": "5"}
        env = build_sandbox_env(passthrough=["AUTO_FACTORY_DEV_BOT_TOKEN", "AUTO_FACTORY_ADMIN_CHAT_ID"], base=base)
        assert env["AUTO_FACTORY_DEV_BOT_TOKEN"] == "" and env["AUTO_FACTORY_ADMIN_CHAT_ID"] == ""
        assert env["TELEGRAM_BOT_TOKEN"] == ""

    def test_only_admin_in_admin_chat_is_served(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        for frm, chat in ((OTHER, ADMIN), (ADMIN, OTHER), (OTHER, OTHER), (ADMIN, -100123), (True, ADMIN)):
            assert bot.handle_update(upd_msg("/kill", frm, chat)) == "ignored:unauthorized"
            assert bot.handle_update(upd_cb("s", "A" * 22, frm, chat)) == "ignored:unauthorized"
        assert tr.send_attempts == 0 and tr.answers == [] and not (tmp_path / "STOP.flag").exists()
        assert bot.handle_update(upd_msg("/help")) == "cmd:help" and tr.send_attempts == 1


# ============================================================ /task parser
class TestTaskParser:
    @pytest.mark.parametrize("path", [
        "../x.py", "a/../b.py", "/etc/passwd", "C:/x.py", "C:x.py", "//server/share/x.py", "a\\b.py", "a/./b.py",
        "a//b.py", "./a.py", "*.py", "a?.py", "a[1].py", "a;rm.py", "a|b", "a&b", "a$b", "a`b", "a>b", "a<b",
        ".env", "x/.env", ".env.local", "db.sqlite3", "data/admission_brief.db", "x.sqlite", "reports/a.html",
        "reports", "holdout_eval/t.py", "market_brief/core.py", "silver_care/models.py", "deploy_to_oracle.ps1",
        "-rf", "a/-x.py", "\u00e9.py", "STOP.flag", "tests/conftest.py", "pytest.ini", "a\x01b.py", ".git/config",
    ])
    def test_rejects_unsafe_paths(self, repo, path):
        with pytest.raises(TaskParseError):
            parse_task_command(f"/task L1 {path} -- do it", repo)

    def test_rejects_unsafe_test_paths_and_bad_grammar(self, repo):
        for text in (
            "/task L1 calc.py --tests=../x.py -- r", "/task L1 calc.py --tests=holdout_eval/t.py -- r",
            "/task L4 calc.py -- r", "/task L1 calc.py r", "/task L1 calc.py --   ", "/task L1 -- r",
            "/task L1 calc.py -- " + "\u00e9" * 1100, "/task L1 calc.py -- a\x00b", "/task L1 calc.py -- bell\x07",
            "/task L1 a\nb.py -- r", "/task L1 " + ",".join(f"f{i}.py" for i in range(21)) + " -- r",
        ):
            with pytest.raises(TaskParseError):
                parse_task_command(text, repo)

    def test_accepts_and_canonicalizes(self, repo):
        spec = parse_task_command(
            "/task@AfDevBot L2 tests/test_calc.py,calc.py,calc.py,auto_factory/ --tests=tests/test_calc.py -- "
            "Fix add(); keep `rm -rf /` && $(x) as plain text\nline2", repo)
        assert spec.risk_level == "L2"
        assert spec.allowed_paths == ("auto_factory/", "calc.py", "tests/test_calc.py")
        assert spec.tests == ("tests/test_calc.py",)
        assert spec.request.startswith("Fix add(); keep `rm -rf /`") and "line2" in spec.request
        assert parse_task_command("/task L0 calc.py -- " + "x" * 2048, repo).request == "x" * 2048

    def test_symlink_escape_rejected(self, tmp_path, repo):
        outside = tmp_path / "outside"
        outside.mkdir()
        try:
            os.symlink(outside, repo / "link", target_is_directory=True)
        except (OSError, NotImplementedError):
            pytest.skip("symlink creation not permitted on this host")
        with pytest.raises(TaskParseError):
            parse_task_command("/task L1 link/x.py -- r", repo)

    def test_task_command_creates_idempotent_task(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        r1 = bot.handle_update(upd_msg("/task L1 calc.py --tests=tests/test_calc.py -- fix add"))
        r2 = bot.handle_update(upd_msg("/task L1 calc.py,calc.py --tests=tests/test_calc.py -- fix add"))
        assert r1.startswith("cmd:task:created:") and r2.startswith("cmd:task:duplicate:")
        tid = r1.rsplit(":", 1)[1]
        t = db.get_task(tid)
        assert t["allowed_paths"] == ["calc.py"] and t["acceptance_spec"] == {"pytest": ["tests/test_calc.py"]}
        assert bot.handle_update(upd_msg("/task L1 ../x -- <b>x</b>")) == "cmd:task:rejected"
        assert len(db.list_tasks()) == 1


# ============================================================ HTML / buttons
class TestSafeHtml:
    def test_escapes_and_truncates_before_escaping(self):
        f = SafeHtmlFormatter()
        assert f.esc('<script>&"\'') == "&lt;script&gt;&amp;&quot;&#x27;"
        assert f.esc("<" * 10, 5) == "&lt;" * 4 + "\u2026"
        assert f.code("<b>") == "<code>&lt;b&gt;</code>"
        assert f.finalize(f.b("x") + f.i("y") + f.pre("z") + f.link("https://ex.com/a?b=1", "<l>"))

    @pytest.mark.parametrize("bad", [
        "<script>x</script>", '<b onclick="x">y</b>', '<a href="javascript:x">y</a>', "<u>x</u>", "<img src=x>",
    ])
    def test_tag_whitelist(self, bad):
        with pytest.raises(ValueError):
            SafeHtmlFormatter().finalize(bad)

    def test_length_limits(self):
        f = SafeHtmlFormatter()
        f.finalize("x" * 1800)
        with pytest.raises(MessageTooLong):
            f.finalize("x" * 1801)
        with pytest.raises(MessageTooLong):  # 1000 visible chars but 5000 raw HTML chars
            f.finalize(f.esc("&" * 1000))
        with pytest.raises(ValueError):
            f.link("http://insecure", "x")

    def test_callback_data_bounds(self):
        btn = SafeHtmlFormatter.button("go", "M", "A" * 22)
        assert btn["callback_data"] == "af2|M|" + "A" * 22 and len(btn["callback_data"].encode("ascii")) == 28
        for bad in ("af2|M|" + "A" * 60, "af2|Z|" + "A" * 22, "af2|M|" + "\u00e9" * 22, "x" * 65):
            with pytest.raises(ValueError):
                validate_callback_data(bad)

    def test_task_card_with_hostile_request_stays_in_limits(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        tid, _ = db.create_task("<b>" * 2000 + "&" * 2000, "L1", ["calc.py"], {})
        _to_state(db, tid, "AWAITING_START_AUTH")
        text, markup = bot.renderer.task_card(db.get_task(tid))
        assert len(text) <= 3500 and SafeHtmlFormatter.visible_len(text) <= 1800
        assert "<b><b>" not in text
        assert all(len(b["callback_data"].encode("ascii")) <= 64 for b in markup["inline_keyboard"][0])

    def test_rate_limiter_global_and_per_chat(self):
        clock = Clock(0.0)
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            clock.t += s

        rl = RateLimiter(global_per_sec=25, per_chat_interval=1.0, clock=clock, sleep=sleep)
        for i in range(25):
            rl.wait(i)
        assert sleeps == []
        rl.wait(999)  # 26th message in the same second must wait
        assert clock.t >= 1.0
        t0 = clock.t
        rl.wait(5)
        rl.wait(5)
        assert clock.t - t0 >= 1.0


# ============================================================ action tokens
class TestActionTokens:
    def _setup(self, tmp_path):
        clock = Clock()
        d = CoordinatorDB(tmp_path / "a.db", clock=clock)
        tid, _ = d.create_task("r", "L1", ["calc.py"], {})
        _fake_awaiting_merge(d, tid)
        return d, clock, tid

    def test_token_shape_and_binding(self, tmp_path):
        d, clock, tid = self._setup(tmp_path)
        try:
            tok = d.issue_action_token(tid, "m", ADMIN, ADMIN)
            assert len(tok) == 22 and len(f"af2|m|{tok}".encode("ascii")) <= 64
            with pytest.raises(ActionRejected) as e:
                d.consume_action_token(tok, "M", ADMIN, ADMIN)
            assert e.value.code == "OP_MISMATCH"
            with pytest.raises(ActionRejected) as e:
                d.consume_action_token(tok, "m", OTHER, ADMIN)
            assert e.value.code == "IDENTITY_MISMATCH"
            with pytest.raises(ActionRejected) as e:
                d.consume_action_token(tok, "m", ADMIN, OTHER)
            assert e.value.code == "IDENTITY_MISMATCH"
            d.consume_action_token(tok, "m", ADMIN, ADMIN)
            with pytest.raises(ActionRejected) as e:
                d.consume_action_token(tok, "m", ADMIN, ADMIN)
            assert e.value.code == "REPLAY"
            with pytest.raises(ActionRejected) as e:
                d.consume_action_token("B" * 22, "m", ADMIN, ADMIN)
            assert e.value.code == "UNKNOWN_TOKEN"
            with pytest.raises(ActionRejected) as e:
                d.issue_action_token(tid, "s", ADMIN, ADMIN)  # start is not offered at AWAITING_MERGE
            assert e.value.code == "BAD_STATE"
        finally:
            d.close()

    def test_expiry_stale_version_and_stale_sha(self, tmp_path):
        d, clock, tid = self._setup(tmp_path)
        try:
            old = d.issue_action_token(tid, "M", ADMIN, ADMIN, ttl=10)
            clock.t += 11
            with pytest.raises(ActionRejected) as e:
                d.consume_action_token(old, "M", ADMIN, ADMIN)
            assert e.value.code == "EXPIRED"
            tok = d.issue_action_token(tid, "M", ADMIN, ADMIN)
            d._conn.execute("UPDATE tg_actions SET diff_sha256='old' WHERE token=?", (tok,))  # bound to old diff
            with pytest.raises(ActionRejected) as e:
                d.consume_action_token(tok, "M", ADMIN, ADMIN)
            assert e.value.code == "STALE_SHA"
            tok2 = d.issue_action_token(tid, "X", ADMIN, ADMIN)
            t = d.get_task(tid)
            d.update_fields(tid, t["state_version"], {"last_error": "moved"}, "w")
            with pytest.raises(ActionRejected) as e:
                d.consume_action_token(tok2, "X", ADMIN, ADMIN)
            assert e.value.code == "STALE"
            assert d.get_task(tid)["state"] == "AWAITING_MERGE"
        finally:
            d.close()

    def test_read_only_diff_token_reusable_but_mutating_single_use(self, tmp_path):
        d, clock, tid = self._setup(tmp_path)
        try:
            tok = d.issue_action_token(tid, "d", ADMIN, ADMIN)
            for _ in range(2):  # read-only diff token: reusable, no state change
                action, task, _ = d.consume_action_token(tok, "d", ADMIN, ADMIN)
                assert action["action"] == "d" and task["id"] == tid and task["state"] == "AWAITING_MERGE"
            assert d.get_task(tid)["state"] == "AWAITING_MERGE"
            xtok = d.issue_action_token(tid, "X", ADMIN, ADMIN)

            def discard(c, action, task):
                d._transition_in_tx(c, task["id"], task["state_version"], "DISCARDED", "human:x")

            d.consume_action_token(xtok, "X", ADMIN, ADMIN, discard)
            assert d.get_task(tid)["state"] == "DISCARDED"
            with pytest.raises(ActionRejected) as e:  # mutating token is single-use
                d.consume_action_token(xtok, "X", ADMIN, ADMIN, discard)
            assert e.value.code == "REPLAY"
        finally:
            d.close()

    def test_effect_failure_rolls_back_token_use(self, tmp_path):
        d, clock, tid = self._setup(tmp_path)
        try:
            tok = d.issue_action_token(tid, "X", ADMIN, ADMIN)

            def boom(c, action, task):
                d._transition_in_tx(c, task["id"], task["state_version"], "DISCARDED", "human:x")
                raise RuntimeError("crash")

            with pytest.raises(RuntimeError):
                d.consume_action_token(tok, "X", ADMIN, ADMIN, boom)
            assert d.get_task(tid)["state"] == "AWAITING_MERGE"
            assert d._query("SELECT used_at FROM tg_actions WHERE token=?", (tok,))[0][0] is None
            d.consume_action_token(tok, "X", ADMIN, ADMIN)  # still usable once
        finally:
            d.close()

    def test_token_collision_retries(self, tmp_path, monkeypatch):
        d, clock, tid = self._setup(tmp_path)
        try:
            seq = iter(["C" * 22, "C" * 22, "D" * 22])
            monkeypatch.setattr("auto_factory.db.secrets.token_urlsafe", lambda n: next(seq))
            assert d.issue_action_token(tid, "d", ADMIN, ADMIN) == "C" * 22
            assert d.issue_action_token(tid, "d", ADMIN, ADMIN) == "D" * 22
        finally:
            d.close()


# ============================================================ approvals / migration / FSM
class TestStep2Approvals:
    def test_decision_enum_idempotency_and_identity(self, db):
        tid, _ = db.create_task("r", "L1", [], {})
        with pytest.raises(ValueError):
            db.add_approval(tid, "yolo")
        a = db.add_approval(tid, "start", actor_user_id=ADMIN, actor_chat_id=ADMIN, action_token="t" * 22,
                            idempotency_key="k1")
        assert db.add_approval(tid, "start", idempotency_key="k1") == a
        with pytest.raises(GuardViolation):
            db.add_approval(tid, "merge", idempotency_key="k1")
        rows = db.approvals(tid)
        assert len(rows) == 1 and rows[0]["actor_user_id"] == ADMIN and rows[0]["action_token"] == "t" * 22

    def test_start_approval_expiry(self, tmp_path):
        clock = Clock()
        d = CoordinatorDB(tmp_path / "s.db", clock=clock)
        try:
            tid, _ = d.create_task("r", "L1", [], {})
            _to_state(d, tid, "AWAITING_START_AUTH")
            d.add_approval(tid, "start", ttl=10)
            assert d.has_start_approval(tid)
            clock.t += 11
            assert not d.has_start_approval(tid)
            with pytest.raises(GuardViolation, match="expired"):
                _advance(d, tid, ["WORKTREE_READY"])
        finally:
            d.close()

    def test_step1_db_is_migrated_in_place(self, tmp_path):
        path = tmp_path / "legacy.db"
        con = _sqlite3.connect(path)
        con.executescript(
            "CREATE TABLE approvals (id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,"
            " decision TEXT NOT NULL, target_sha TEXT, diff_sha256 TEXT, review_digest TEXT,"
            " expires_at REAL, created_at REAL NOT NULL);"
            "INSERT INTO approvals (task_id, decision, created_at) VALUES ('t1', 'start', 1.0);"
        )
        con.commit()
        con.close()
        for _ in range(2):  # idempotent
            d = CoordinatorDB(path)
            cols = {r["name"] for r in d._query("PRAGMA table_info(approvals)")}
            assert {"actor_user_id", "actor_chat_id", "action_token", "idempotency_key"} <= cols
            assert d._query("SELECT decision FROM approvals")[0][0] == "start"
            idx = {r[1] for r in d._query("PRAGMA index_list(approvals)")}
            assert "idx_approvals_idem" in idx
            names = {r[0] for r in d._query("SELECT name FROM sqlite_master WHERE type='table'")}
            assert {"tg_outbox", "tg_actions", "bot_kv", "merge_intents"} <= names
            d.close()

    def test_single_provider_requires_bound_manual_review(self, db):
        tid, _ = db.create_task("r", "L2", [], {})
        _advance(db, tid, ["PREFLIGHTED", "CLASSIFIED", "PLANNING", "AUDITED"])
        db.add_artifact(tid, "audit", "a.md", "codex", sha256="x")
        _advance(db, tid, ["PLAN_READY", "AWAITING_START_AUTH"])
        db.add_approval(tid, "start")
        _advance(db, tid, ["WORKTREE_READY", "IMPLEMENTING", "CHECKPOINTED", "VERIFYING"])
        v = db.get_task(tid)["state_version"]
        db.transition(tid, v, "SINGLE_PROVIDER_REVIEW_REQUIRED", "w",
                      fields={"target_sha": "a" * 40, "diff_sha256": "d1"})
        with pytest.raises(GuardViolation, match="human"):
            _advance(db, tid, ["AWAITING_MERGE"], actor="worker")
        with pytest.raises(GuardViolation, match="manual_review"):
            _advance(db, tid, ["AWAITING_MERGE"], actor="human:owner")
        db.add_approval(tid, "manual_review", "a" * 40, "STALE")
        assert not db.has_manual_review_approval(tid)
        with pytest.raises(GuardViolation, match="manual_review"):
            _advance(db, tid, ["AWAITING_MERGE"], actor="human:owner")
        db.add_approval(tid, "manual_review", "a" * 40, "d1")
        assert db.has_manual_review_approval(tid)
        v = db.get_task(tid)["state_version"]
        with pytest.raises(GuardViolation, match="may not change"):
            db.transition(tid, v, "AWAITING_MERGE", "human:owner", fields={"diff_sha256": "other"})
        _advance(db, tid, ["AWAITING_MERGE"], actor="human:owner")
        assert db.get_task(tid)["state"] == "AWAITING_MERGE"


# ============================================================ outbox
class TestOutbox:
    def _db(self, tmp_path, notify=ADMIN):
        clock = Clock()
        return CoordinatorDB(tmp_path / "o.db", clock=clock, notify_chat_id=notify), clock

    def test_enqueue_is_atomic_with_transition(self, tmp_path):
        d, clock = self._db(tmp_path)
        try:
            tid, _ = d.create_task("r", "L1", [], {})
            _to_state(d, tid, "AWAITING_START_AUTH")
            rows = d.outbox(tid)
            v = d.get_task(tid)["state_version"]
            assert [r["idempotency_key"] for r in rows] == [f"af2:{tid}:{v}:AWAITING_START_AUTH"]
            assert rows[0]["status"] == "PENDING" and rows[0]["chat_id"] == ADMIN and rows[0]["boot_id"] is None
            n_events = len(d.events(tid))
            with pytest.raises(RuntimeError):
                with d._tx() as c:
                    d._transition_in_tx(c, tid, v, "BLOCKED", "w", "x")
                    raise RuntimeError("crash after enqueue")
            assert d.get_task(tid)["state"] == "AWAITING_START_AUTH"
            assert len(d.outbox(tid)) == 1 and len(d.events(tid)) == n_events
            with d._tx() as c:  # same key twice -> one row
                task = c.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()
                d._enqueue_state_notification(c, task, "AWAITING_START_AUTH", v, "", clock.t)
            assert len(d.outbox(tid)) == 1
        finally:
            d.close()

    def test_no_chat_configured_means_no_rows_and_kv_fallback(self, tmp_path):
        d = CoordinatorDB(tmp_path / "n.db")
        try:
            t1, _ = d.create_task("r1", "L1", [], {})
            _to_state(d, t1, "AWAITING_START_AUTH")
            assert d.outbox() == []
            d.set_kv("notify_chat_id", ADMIN)  # written by the dev bot; worker process picks it up
            t2, _ = d.create_task("r2", "L1", [], {})
            _to_state(d, t2, "AWAITING_START_AUTH")
            assert len(d.outbox(t2)) == 1
        finally:
            d.close()

    def test_delivery_semantics(self, tmp_path, repo):
        d, clock = self._db(tmp_path)
        try:
            bot, tr = make_bot(tmp_path, repo, d)
            tids = []
            for i in range(5):
                tid, _ = d.create_task(f"r{i}", "L1", ["calc.py"], {})
                _to_state(d, tid, "AWAITING_START_AUTH")
                tids.append(tid)
            tr.send_script = [TgRetryAfter(30), TgUnknown("timeout"), TgUnknown("HTTP 502"),
                              ConnectionResetError("reset"), TgRejected("HTTP 400")]
            st = bot.dispatch_once()
            assert st == {"sent": 0, "retry": 1, "unknown": 3, "failed": 1, "expired": 0}
            status = {t: d.outbox(t)[0]["status"] for t in tids}
            assert status == {tids[0]: "PENDING", tids[1]: "UNKNOWN", tids[2]: "UNKNOWN", tids[3]: "UNKNOWN",
                              tids[4]: "FAILED"}
            assert d.outbox(tids[0])[0]["next_retry_at"] == clock.t + 30
            attempts = tr.send_attempts
            assert bot.dispatch_once()["sent"] == 0 and tr.send_attempts == attempts  # backoff + no UNKNOWN resend
            clock.t += 31
            assert bot.dispatch_once()["sent"] == 1
            clock.t += 10_000
            assert bot.dispatch_once() == {"sent": 0, "retry": 0, "unknown": 0, "failed": 0, "expired": 0}
            assert tr.send_attempts == attempts + 1  # SENT and UNKNOWN never resent
            counts = d.outbox_counts()
            assert counts["SENT"] == 1 and counts["UNKNOWN"] == 3 and counts["FAILED"] == 1
        finally:
            d.close()

    def test_restart_does_not_storm(self, tmp_path, repo):
        d, clock = self._db(tmp_path)
        try:
            ids = []
            for i in range(3):
                tid, _ = d.create_task(f"r{i}", "L1", [], {})
                _to_state(d, tid, "AWAITING_START_AUTH")
                ids.append(tid)
            claimed = d.claim_outbox("old-owner", "boot-1", limit=1)  # crashed mid-send
            assert len(claimed) == 1 and claimed[0]["attempts"] == 1
            d.claim_outbox("o2", "boot-1", limit=1)
            d.mark_outbox_sent(d.outbox(ids[1])[0]["id"], "o2")
            clock.t += 5
            bot, tr = make_bot(tmp_path, repo, d)  # boot-2 expires everything from before
            assert {t: d.outbox(t)[0]["status"] for t in ids} == {ids[0]: "UNKNOWN", ids[1]: "SENT",
                                                                  ids[2]: "EXPIRED"}
            assert bot.dispatch_once()["sent"] == 0 and tr.send_attempts == 0
            t4, _ = d.create_task("after boot", "L1", ["calc.py"], {})
            _to_state(d, t4, "AWAITING_START_AUTH")
            assert bot.dispatch_once()["sent"] == 1
        finally:
            d.close()

    def test_lapsed_lease_becomes_unknown(self, tmp_path):
        d, clock = self._db(tmp_path)
        try:
            tid, _ = d.create_task("r", "L1", [], {})
            _to_state(d, tid, "AWAITING_START_AUTH")
            d.claim_outbox("A", "b", lease_ttl=10)
            clock.t += 11
            assert d.claim_outbox("B", "b") == []
            assert d.outbox(tid)[0]["status"] == "UNKNOWN"
        finally:
            d.close()

    def test_superseded_notification_expires(self, tmp_path, repo):
        d, clock = self._db(tmp_path)
        try:
            bot, tr = make_bot(tmp_path, repo, d)
            tid, _ = d.create_task("r", "L1", ["calc.py"], {})
            _to_state(d, tid, "AWAITING_START_AUTH")
            d.add_approval(tid, "start")
            _advance(d, tid, ["WORKTREE_READY"])
            assert bot.dispatch_once()["expired"] == 1 and tr.send_attempts == 0
        finally:
            d.close()

    def test_http_transport_error_mapping(self, monkeypatch):
        import io
        import socket
        import urllib.error

        tr = HttpTransport(VALID_TOKEN)

        def raiser(exc):
            def f(*a, **k):
                raise exc
            return f

        def http_err(code, body):
            return urllib.error.HTTPError("https://x", code, "m", {}, io.BytesIO(_json.dumps(body).encode()))

        cases = [
            (http_err(429, {"ok": False, "parameters": {"retry_after": 7}}), TgRetryAfter),
            (http_err(500, {}), TgUnknown), (http_err(502, {}), TgUnknown),
            (http_err(400, {"description": "bad"}), TgRejected), (http_err(403, {}), TgRejected),
            (urllib.error.URLError("down"), TgUnknown), (socket.timeout("t"), TgUnknown),
            (ConnectionResetError("r"), TgUnknown), (TimeoutError("t"), TgUnknown),
        ]
        for exc, expected in cases:
            monkeypatch.setattr(dev_bot_mod.urllib.request, "urlopen", raiser(exc))
            with pytest.raises(expected) as e:
                tr.send_message(ADMIN, "x")
            assert VALID_TOKEN not in str(e.value)
            if expected is TgRetryAfter:
                assert e.value.retry_after == 7
        with pytest.raises(ConfigError):
            HttpTransport("bad")


# ============================================================ polling
class TestPolling:
    def test_offset_cas_duplicates_and_single_poller(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        tr.updates = [upd_msg("/help"), upd_msg("/status")]
        tr.get_updates = lambda offset, timeout=0: list(tr.updates)  # Telegram re-delivers everything
        assert bot.poll_once(timeout=0) == 2
        assert bot.poll_once(timeout=0) == 0  # redelivered updates are harmless no-ops
        assert tr.send_attempts == 2
        assert db.get_update_offset() == tr.updates[-1]["update_id"]
        off = db.get_update_offset()
        assert not db.advance_update_offset(off - 1, off + 5)  # lost CAS
        assert not db.advance_update_offset(off, off)  # never moves backwards / sideways
        bot2, tr2 = make_bot(tmp_path, repo, db, boot=False)
        tr2.updates = [upd_msg("/help")]
        assert bot2.poll_once(timeout=0) == 0  # bot1 still holds the poller lease
        db.release_poller_lease(bot.owner)
        assert bot2.poll_once(timeout=0) == 1


# ============================================================ callbacks via bot
class TestCallbacks:
    def _start_card(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        tid = submit(db, start=False)
        _to_state(db, tid, "AWAITING_START_AUTH")
        assert bot.dispatch_once()["sent"] == 1
        return bot, tr, tid, tr.buttons()

    @pytest.mark.parametrize("failure", [False, RuntimeError("net"), TgUnknown("timeout")])
    def test_answer_failure_means_zero_side_effects(self, tmp_path, repo, db, monkeypatch, failure):
        bot, tr, tid, btns = self._start_card(tmp_path, repo, db)
        calls = []
        monkeypatch.setattr(db, "consume_action_token", lambda *a, **k: calls.append(a))
        monkeypatch.setattr(bot.coordinator, "merge", lambda *a, **k: calls.append(a))
        monkeypatch.setattr(bot.wtm, "git", lambda *a, **k: calls.append(a))
        tr.answer_script = [failure]
        assert bot.handle_update(upd_cb("s", btns["s"])) == "callback:answer_failed"
        assert calls == [] and not db.has_start_approval(tid)

    def test_start_identity_replay_and_cancel(self, tmp_path, repo, db):
        bot, tr, tid, btns = self._start_card(tmp_path, repo, db)
        assert bot.handle_update(upd_cb("s", btns["s"])) == "callback:start"
        assert bot.handle_update(upd_cb("s", btns["s"])) == "callback:rejected:REPLAY"
        rows = db.approvals(tid, "start")
        assert len(rows) == 1 and (rows[0]["actor_user_id"], rows[0]["actor_chat_id"]) == (ADMIN, ADMIN)
        assert rows[0]["action_token"] == btns["s"] and rows[0]["expires_at"] is not None
        assert bot.handle_update(upd_cb("c", btns["c"])) == "callback:cancel"
        assert db.get_task(tid)["state"] == "CANCELLED"
        assert db.events(tid)[-1]["actor"] == f"human:tg:{ADMIN}"

    def test_malformed_callback_after_answer_is_noop(self, tmp_path, repo, db):
        bot, tr, tid, btns = self._start_card(tmp_path, repo, db)
        u = upd_cb("s", btns["s"])
        u["callback_query"]["data"] = "af2|s|short"
        assert bot.handle_update(u) == "callback:malformed"
        assert not db.has_start_approval(tid)

    def test_full_single_provider_flow_and_two_step_merge(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        w = make_worker(tmp_path, repo, db, claude=[_fix])
        w.adapters["codex"].is_available = False
        w.adapters["gemini"].is_available = False
        tid = submit(db, start=False)
        assert w.process_task(tid) == "AWAITING_START_AUTH"
        bot.dispatch_once()
        assert bot.handle_update(upd_cb("s", tr.buttons()["s"])) == "callback:start"
        assert w.process_task(tid) == "SINGLE_PROVIDER_REVIEW_REQUIRED"
        bot.dispatch_once()
        sprr = tr.buttons()
        assert set(sprr) == {"d", "r", "x"}
        assert bot.handle_update(upd_cb("R", sprr["r"])) == "callback:rejected:OP_MISMATCH"  # no 1-step skip
        assert bot.handle_update(upd_cb("r", sprr["r"])) == "callback:prompt:r"
        edit = tr.edits[-1]
        assert db.get_task(tid)["target_sha"] in edit["text"] and set(tr.buttons(source="edits")) == {"R"}
        assert bot.handle_update(upd_cb("R", tr.buttons(source="edits")["R"])) == "callback:review"
        assert db.get_task(tid)["state"] == "AWAITING_MERGE" and db.has_manual_review_approval(tid)
        assert not db.valid_merge_approval(tid)  # review is not a merge approval
        bot.dispatch_once()
        am = tr.buttons()
        assert bot.handle_update(upd_cb("m", am["m"])) == "callback:prompt:m"
        m_confirm = tr.buttons(source="edits")["M"]
        assert bot.handle_update(upd_cb("M", m_confirm)) == "callback:merged"
        t = db.get_task(tid)
        assert t["state"] == "MERGED" and w.wtm.head_sha() == t["target_sha"]
        assert "return a + b" in (repo / "calc.py").read_text(encoding="utf-8")
        assert bot.handle_update(upd_cb("M", m_confirm)) == "callback:rejected:REPLAY"
        merge_rows = db.approvals(tid, "merge")
        assert len(merge_rows) == 1 and merge_rows[0]["target_sha"] == t["target_sha"]

    def test_discard_preserves_patch(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        w, tid = awaiting_merge(tmp_path, repo, db)
        t = db.get_task(tid)
        x = db.issue_action_token(tid, "x", ADMIN, ADMIN)
        assert bot.handle_update(upd_cb("x", x)) == "callback:prompt:x"
        assert bot.handle_update(upd_cb("X", tr.buttons(source="edits")["X"])) == "callback:discarded"
        assert db.get_task(tid)["state"] == "DISCARDED"
        assert not Path(t["worktree_path"]).exists()
        assert b"return a + b" in (tmp_path / "artifacts" / f"{tid}.patch").read_bytes()
        assert w.wtm.head_sha() == t["base_sha"]  # main untouched

    def test_diff_view_escapes_content(self, tmp_path, repo, db):
        def evil(wt, contract, role):
            Path(wt, "calc.py").write_text("def add(a, b):\n    return a + b  # <script>&</script>\n",
                                           encoding="utf-8")

        bot, tr = make_bot(tmp_path, repo, db)
        w = make_worker(tmp_path, repo, db, claude=[evil])
        tid = submit(db)
        assert w.process_task(tid) == "AWAITING_MERGE"
        d = db.issue_action_token(tid, "d", ADMIN, ADMIN)
        assert bot.handle_update(upd_cb("d", d)) == "callback:diff"
        text = tr.sent[-1]["text"]
        assert "&lt;script&gt;&amp;" in text and "<script>" not in text


# ============================================================ merge gateway
class TestMergeGateway:
    def _coord(self, w, db, tmp_path):
        return MergeCoordinator(db, w.wtm, w.guard, tmp_path / "artifacts")

    def test_happy_path_merges_then_cleans(self, tmp_path, repo, db):
        w, tid = awaiting_merge(tmp_path, repo, db)
        approve_merge(db, tid)
        t = db.get_task(tid)
        out = self._coord(w, db, tmp_path).merge(tid, "human:owner")
        assert out.ok and out.cleanup_ok and out.head == t["target_sha"] == w.wtm.head_sha()
        assert db.get_task(tid)["state"] == "MERGED"
        assert db.get_merge_intent(out.intent_id)["status"] == "COMPLETED"
        assert not Path(t["worktree_path"]).exists()
        assert w.wtm.git("branch", "--list", f"auto/{tid}").strip() == ""
        assert w.wtm.main_is_clean()

    def test_requires_valid_unexpired_approval(self, tmp_path, repo):
        clock = Clock(time.time())
        d = CoordinatorDB(tmp_path / "m.db", clock=clock)
        try:
            w, tid = awaiting_merge(tmp_path, repo, d)
            base = w.wtm.head_sha()
            out = self._coord(w, d, tmp_path).merge(tid, "human:owner")
            assert not out.ok and out.code == "INTENT_REJECTED"
            approve_merge(d, tid, ttl=60)
            clock.t += 61
            assert self._coord(w, d, tmp_path).merge(tid, "human:owner").code == "INTENT_REJECTED"
            assert w.wtm.head_sha() == base and d.merge_intents(tid) == []
        finally:
            d.close()

    @pytest.mark.parametrize("scenario,code,task_state", [
        ("dirty_main", "MAIN_DIRTY", "AWAITING_MERGE"),
        ("base_drift", "REBASE_REQUIRED", "BLOCKED"),
        ("dirty_candidate", "CANDIDATE_DIRTY", "BLOCKED"),
        ("target_moved", "TARGET_MISMATCH", "BLOCKED"),
        ("diff_mismatch", "DIFF_MISMATCH", "BLOCKED"),
        ("guardrail", "GUARDRAIL", "BLOCKED"),
        ("missing_worktree", "CANDIDATE_MISSING", "BLOCKED"),
    ])
    def test_preconditions_fail_closed(self, tmp_path, repo, db, scenario, code, task_state):
        w, tid = awaiting_merge(tmp_path, repo, db)
        t = db.get_task(tid)
        wt = t["worktree_path"]
        if scenario == "dirty_main":
            (repo / "untracked.txt").write_text("x", encoding="utf-8")
        elif scenario == "base_drift":
            (repo / "other.txt").write_text("x", encoding="utf-8")
            _git_main(repo, "add", "other.txt")
            _git_main(repo, "commit", "-q", "-m", "drift")
        elif scenario == "dirty_candidate":
            Path(wt, "calc.py").write_text("tampered\n", encoding="utf-8")
        elif scenario == "target_moved":
            Path(wt, "calc.py").write_text(FIX + "# later\n", encoding="utf-8")
            w.wtm.commit_wip_checkpoint(wt, "post-approval change")
        elif scenario == "diff_mismatch":
            db.update_fields(tid, t["state_version"], {"diff_sha256": "f" * 64}, "test")
        elif scenario == "guardrail":
            Path(wt, ".env").write_text("SECRET=1\n", encoding="utf-8")
            sha = w.wtm.commit_wip_checkpoint(wt, "sneak .env")
            db.update_fields(tid, t["state_version"],
                             {"target_sha": sha, "diff_sha256": w.wtm.diff_sha256(wt, t["base_sha"])}, "test")
        elif scenario == "missing_worktree":
            w.wtm.git("worktree", "remove", "--force", wt)
        approve_merge(db, tid)
        base_head = w.wtm.head_sha()
        out = self._coord(w, db, tmp_path).merge(tid, "human:owner")
        assert not out.ok and out.code == code, out
        assert db.get_task(tid)["state"] == task_state
        assert w.wtm.head_sha() == base_head  # main never moved
        assert db.get_merge_intent(out.intent_id)["status"] in ("ABORTED", "BLOCKED")
        assert not (repo / ".git" / "MERGE_HEAD").exists()

    def test_non_fast_forward_aborts_cleanly(self, tmp_path, repo, db):
        w, tid = awaiting_merge(tmp_path, repo, db)
        t = db.get_task(tid)
        wt = t["worktree_path"]
        tree = w.wtm.git("rev-parse", "HEAD^{tree}", cwd=wt).strip()
        orphan = w.wtm.git("-c", "user.name=t", "-c", "user.email=t@l", "commit-tree", tree, "-m", "unrelated",
                           cwd=wt).strip()
        w.wtm.git("reset", "-q", "--hard", orphan, cwd=wt)
        db.update_fields(tid, t["state_version"],
                         {"target_sha": orphan, "diff_sha256": w.wtm.diff_sha256(wt, t["base_sha"])}, "test")
        approve_merge(db, tid)
        out = self._coord(w, db, tmp_path).merge(tid, "human:owner")
        assert not out.ok and out.code == "MERGE_FAILED"
        assert db.get_task(tid)["state"] == "BLOCKED"
        assert db.get_merge_intent(out.intent_id)["status"] == "ABORTED"
        assert w.wtm.head_sha() == t["base_sha"] and w.wtm.main_is_clean()
        assert not (repo / ".git" / "MERGE_HEAD").exists()

    def test_merge_lock_and_bad_sha(self, tmp_path, repo, db):
        w, tid = awaiting_merge(tmp_path, repo, db)
        approve_merge(db, tid)
        with _FileLock(w.wtm.merge_lock_path):
            out = self._coord(w, db, tmp_path).merge(tid, "human:owner")
        assert out.code == "MERGE_LOCKED" and db.get_task(tid)["state"] == "AWAITING_MERGE"
        with pytest.raises(MergePreconditionError, match="BAD_SHA"):
            w.wtm.merge_candidate(dict(db.get_task(tid), target_sha="abc"), w.guard)
        assert self._coord(w, db, tmp_path).merge(tid, "human:owner").ok  # retry after lock released

    def test_cleanup_failure_does_not_roll_back_merge(self, tmp_path, repo, db, monkeypatch):
        w, tid = awaiting_merge(tmp_path, repo, db)
        approve_merge(db, tid)

        def broken_cleanup(*a, **k):
            raise OSError("file in use")

        real_cleanup = w.wtm.cleanup
        monkeypatch.setattr(w.wtm, "cleanup", broken_cleanup)
        out = self._coord(w, db, tmp_path).merge(tid, "human:owner")
        assert out.ok and out.cleanup_ok is False
        assert db.get_task(tid)["state"] == "MERGED"
        assert db.get_merge_intent(out.intent_id)["status"] == "ORPHAN_CLEANUP"
        monkeypatch.setattr(w.wtm, "cleanup", real_cleanup)
        res = db.reconcile_merge_intents(w.wtm, tmp_path / "artifacts")  # next boot retries cleanup
        assert (out.intent_id, "CLEANED") in res

    def test_second_open_intent_refused(self, tmp_path, repo, db):
        w, tid = awaiting_merge(tmp_path, repo, db)
        approve_merge(db, tid)
        db.begin_merge_intent(tid, "human:a")
        with pytest.raises(MergeIntentError):
            db.begin_merge_intent(tid, "human:b")


class TestMergeRecovery:
    def test_crash_after_git_merge_before_db(self, tmp_path, repo, db):
        w, tid = awaiting_merge(tmp_path, repo, db)
        approve_merge(db, tid)
        intent = db.begin_merge_intent(tid, "human:owner")
        w.wtm.merge_candidate(db.get_task(tid), w.guard)  # ... process dies here
        assert db.get_task(tid)["state"] == "AWAITING_MERGE"
        bot, tr = make_bot(tmp_path, repo, db, boot=False)
        report = bot.boot()
        assert (intent, "FINALIZED") in report["merges"] and (intent, "CLEANED") in report["merges"]
        assert db.get_task(tid)["state"] == "MERGED"
        assert db.get_merge_intent(intent)["status"] == "COMPLETED"
        assert db.events(tid)[-1]["actor"] == "human:owner"

    def test_crash_before_git_merge(self, tmp_path, repo, db):
        w, tid = awaiting_merge(tmp_path, repo, db)
        approve_merge(db, tid)
        intent = db.begin_merge_intent(tid, "human:owner")
        assert db.reconcile_merge_intents(w.wtm) == [(intent, "ABORTED")]
        assert db.get_task(tid)["state"] == "AWAITING_MERGE"

    def test_unexpected_main_head_blocks(self, tmp_path, repo, db):
        w, tid = awaiting_merge(tmp_path, repo, db)
        approve_merge(db, tid)
        intent = db.begin_merge_intent(tid, "human:owner")
        (repo / "x.txt").write_text("x", encoding="utf-8")
        _git_main(repo, "add", "x.txt")
        _git_main(repo, "commit", "-q", "-m", "someone else")
        assert db.reconcile_merge_intents(w.wtm) == [(intent, "BLOCKED")]
        assert db.get_task(tid)["state"] == "BLOCKED"
        assert "MERGE_RECONCILE" in db.events(tid)[-1]["reason"]

    def test_crash_after_db_before_cleanup(self, tmp_path, repo, db):
        w, tid = awaiting_merge(tmp_path, repo, db)
        approve_merge(db, tid)
        intent = db.begin_merge_intent(tid, "human:owner")
        head = w.wtm.merge_candidate(db.get_task(tid), w.guard)
        db.mark_merge_intent_git_merged(intent)
        db.finalize_merge(intent, head)  # ... dies before cleanup
        wt = db.get_task(tid)["worktree_path"]
        assert Path(wt).exists()
        assert db.reconcile_merge_intents(w.wtm, tmp_path / "artifacts") == [(intent, "CLEANED")]
        assert not Path(wt).exists() and db.get_task(tid)["state"] == "MERGED"


# ============================================================ safety commands
class TestSafetyCommands:
    def test_kill_resume_and_circuit(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        assert bot.handle_update(upd_msg("/kill")) == "cmd:kill" and (tmp_path / "STOP.flag").exists()
        w = make_worker(tmp_path, repo, db)
        assert w.stop_requested()
        for _ in range(3):
            db.circuit_record(CIRCUIT, False)
        assert db.circuit_is_open(CIRCUIT)
        assert bot.handle_update(upd_msg("/resume now")) == "cmd:resume:bad-arg"
        assert (tmp_path / "STOP.flag").exists()
        assert bot.handle_update(upd_msg("/resume")) == "cmd:resume"
        assert not (tmp_path / "STOP.flag").exists() and db.circuit_is_open(CIRCUIT)
        assert bot.handle_update(upd_msg("/resume clear-circuit")) == "cmd:resume"
        assert not db.circuit_is_open(CIRCUIT)

    def test_stop_flag_containment(self, tmp_path, repo, db):
        for bad in (tmp_path / "elsewhere" / "STOP.flag", tmp_path / "artifacts" / "STOP.flag", repo / "stop.txt"):
            with pytest.raises(ConfigError):
                DevBot(db, FakeTransport(), ADMIN, repo, artifacts_dir=tmp_path / "artifacts",
                       worktrees_dir=tmp_path / "wts", stop_flag=bad)
        DevBot(db, FakeTransport(), ADMIN, repo, artifacts_dir=tmp_path / "artifacts",
               worktrees_dir=tmp_path / "wts", stop_flag=repo / "STOP.flag")
        with pytest.raises(ConfigError):
            DevBot(db, FakeTransport(), 0, repo, artifacts_dir=tmp_path / "artifacts")

    def test_status_overview_and_task_card(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        tid = submit(db, start=False)
        _to_state(db, tid, "AWAITING_START_AUTH")
        assert bot.handle_update(upd_msg("/status")) == "cmd:status"
        assert "outbox pending 1" in tr.sent[-1]["text"] and tid in tr.sent[-1]["text"]
        assert bot.handle_update(upd_msg(f"/status {tid}")) == "cmd:status:task"
        assert set(tr.buttons()) == {"s", "c"}
        assert bot.handle_update(upd_msg("/status ../etc")) == "cmd:status:bad-id"
        assert bot.handle_update(upd_msg("/status tnope")) == "cmd:status:unknown"


# ============================================================ worker hardening / handoff
class TestStep2Worker:
    def test_validate_db_path(self, tmp_path, repo):
        state = tmp_path / "state"
        state.mkdir()
        assert validate_db_path(state / "c.db", repo) == (state / "c.db").resolve()
        for bad in (None, "", state / "prod.sqlite3", state / "PROD.SQLITE3", repo / "c.db", repo / "sub" / "c.db",
                    tmp_path / "reports" / "c.db"):
            with pytest.raises(ValueError):
                validate_db_path(bad, repo)
        link = tmp_path / "linkdir"
        try:
            os.symlink(state, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            return
        with pytest.raises(ValueError, match="symlink"):
            validate_db_path(link / "c.db", repo)

    def test_worker_cli_rejects_db_inside_repo(self, repo):
        from auto_factory.worker import main as worker_main

        with pytest.raises(SystemExit):
            worker_main(["--status", "--db", str(repo / "coord.db"), "--repo", str(repo)])
        assert not (repo / "coord.db").exists()

    def test_handoff_summary_legacy_and_cap(self):
        c = HandoffContract("t1", "L1", "a" * 40, "b" * 40, ["calc.py"], [], "writer")
        legacy = _json.loads(c.to_json())
        legacy.pop("summary")
        assert HandoffContract.from_json(_json.dumps(legacy)).summary == ""
        c.fit_summary("\ud55c\uae00 \uc694\uccad " * 400, max_chars=10_000)
        text = c.to_json()
        assert len(text.encode("utf-8")) <= 2048 and len(c.summary) > 100
        assert HandoffContract.from_json(text) == c
        big = HandoffContract("t", "L1", "a", "b", ["x" * 2100], [], "writer").fit_summary("hello")
        assert big.summary == ""
        with pytest.raises(ContractTooLarge):
            big.to_json()
        with pytest.raises(ValueError):
            HandoffContract.from_json(_json.dumps(dict(legacy, summary=5)))

    def test_worker_contract_carries_summary(self, tmp_path, repo, db):
        w = make_worker(tmp_path, repo, db, claude=[_fix])
        tid = submit(db, request="fix add() \u2014 " + "\uc694" * 1000)
        assert w.process_task(tid) == "AWAITING_MERGE"
        for art in db.artifacts(tid, "handoff"):
            raw = Path(art["path"]).read_bytes()
            assert len(raw) <= 2048
            data = _json.loads(raw)
            assert data["summary"].startswith("fix add()") and len(data["summary"]) <= 300


def test_dev_bot_demo_cli_runs_offline():
    env = build_sandbox_env({"PYTHONPATH": str(REPO_ROOT)})
    env["AUTO_FACTORY_DEV_BOT_TOKEN"] = "garbage"  # demo must not even look at it
    proc = subprocess.run(
        [sys.executable, "-m", "auto_factory.dev_bot", "--demo"], cwd=str(REPO_ROOT), env=env,
        stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "RESULT PASS" in proc.stdout and "FAIL" not in proc.stdout.replace("RESULT PASS", "")


# ######################################################################## STEP 3
import hashlib as _hashlib
import threading as _threading
import types as _types

import auto_factory.db as db_mod
import auto_factory.supervisor as sup_mod
import auto_factory.worker as worker_mod
import auto_factory.worktree as wt_mod
from auto_factory.adapters import (
    _SAFE_SHIM_ARG_RE,
    AdapterResult,
    AuditParseError,
    parse_audit_result,
    render_prompt,
)
from auto_factory.db import ArtifactIntegrityError, read_db_summary_readonly, sha256_file, verify_phase1_artifacts
from auto_factory.dev_bot import _InstantGates, load_dev_env_file, parse_env_text, run_check
from auto_factory.supervisor import Supervisor, run_supervisor_once
from auto_factory.worker import resolve_state_paths
from auto_factory.worktree import (
    LockOrderError,
    SupervisorLocked,
    UnsafePathError,
    _assert_no_link_or_reparse_ancestors,
    default_state_dir,
    safe_open_fd,
    safe_read_bytes,
    safe_unlink,
    safe_write_atomic,
    supervisor_lock_held,
    validate_state_layout,
)

TOKEN2 = "987654321:" + "B" * 35
CHAT = str(ADMIN)


def _env_file(tmp_path, body: bytes | str, name=".env.dev"):
    p = tmp_path / name
    p.write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))
    return p


def _good_env(token=VALID_TOKEN, chat=CHAT, extra=""):
    return f"AUTO_FACTORY_DEV_BOT_TOKEN={token}\nAUTO_FACTORY_ADMIN_CHAT_ID={chat}\n{extra}"


@pytest.fixture
def no_leaked_locks():
    yield
    assert not supervisor_lock_held(), "a test leaked supervisor.lock"


# ============================================================ strict .env loader
class TestEnvLoader:
    def test_missing_and_absent_file(self, tmp_path):
        with pytest.raises(ConfigError, match="not found"):
            load_dev_env_file(tmp_path / "nope.env", {})
        cfg = load_dev_env_file(None, {"AUTO_FACTORY_DEV_BOT_TOKEN": VALID_TOKEN, "AUTO_FACTORY_ADMIN_CHAT_ID": CHAT})
        assert cfg.admin_chat_id == ADMIN
        with pytest.raises(ConfigError):
            load_dev_env_file(None, {})

    def test_bom_crlf_quotes_and_comments(self, tmp_path):
        body = ("﻿# dev bot\r\n\r\n   # indented comment\r\nAUTO_FACTORY_DEV_BOT_TOKEN=\"" + VALID_TOKEN
                + "\"\r\nAUTO_FACTORY_ADMIN_CHAT_ID='" + CHAT + "'\r\nPLAIN=hello world\r\nEMPTY=\r\n")
        cfg = load_dev_env_file(_env_file(tmp_path, body.encode("utf-8")), {})
        assert (cfg.token, cfg.admin_chat_id) == (VALID_TOKEN, ADMIN)
        assert parse_env_text("A='x y'\nB=\"\"\n") == {"A": "x y", "B": ""}

    @pytest.mark.parametrize("line", [
        "export X=1", "X=${HOME}", "X=$(whoami)", "X=`id`", 'X="unterminated', "X='mixed\"", 'X=a"b',
        "JUSTAKEY", " =x", "A B=1", "1A=2", "X='a'b'", 'X="a\nb"',
    ])
    def test_rejects_bad_grammar(self, tmp_path, line):
        with pytest.raises(ConfigError) as e:
            load_dev_env_file(_env_file(tmp_path, _good_env(extra=line + "\n")), {})
        assert VALID_TOKEN not in str(e.value)

    def test_duplicate_nul_oversize_and_bad_utf8(self, tmp_path):
        for body, match in (
            (_good_env() + "AUTO_FACTORY_ADMIN_CHAT_ID=5\n", "duplicate"),
            (_good_env().encode() + b"X=a\x00b\n", "NUL"),
            (_good_env().encode() + b"X=\xff\xfe\n", "UTF-8"),
            (_good_env().encode() + b"# " + b"x" * (1024 * 1024) + b"\n", "exceeds|rejected"),
        ):
            with pytest.raises(ConfigError, match=match):
                load_dev_env_file(_env_file(tmp_path, body), {})

    def test_process_env_overrides_without_mutation(self, tmp_path, monkeypatch):
        monkeypatch.delenv("AUTO_FACTORY_DEV_BOT_TOKEN", raising=False)
        f = _env_file(tmp_path, _good_env())
        before = dict(os.environ)
        cfg = load_dev_env_file(f, {"AUTO_FACTORY_DEV_BOT_TOKEN": TOKEN2})
        assert cfg.token == TOKEN2 and cfg.admin_chat_id == ADMIN
        assert dict(os.environ) == before and "AUTO_FACTORY_DEV_BOT_TOKEN" not in os.environ
        # present-but-empty (e.g. build_sandbox_env scrubbing) is an explicit override: fail closed
        for scrub in ({"AUTO_FACTORY_DEV_BOT_TOKEN": ""}, {"AUTO_FACTORY_ADMIN_CHAT_ID": ""}):
            with pytest.raises(ConfigError):
                load_dev_env_file(f, scrub)
        with pytest.raises(ConfigError):
            load_dev_env_file(f, build_sandbox_env(base={"PATH": "p"}))

    def test_token_reuse_rejected_across_file_and_process_env(self, tmp_path):
        cases = [
            (_good_env(extra=f"TELEGRAM_BOT_TOKEN={VALID_TOKEN}\n"), {}),
            (_good_env(extra=f"travel_bot_token='{VALID_TOKEN}'\n"), {}),
            (_good_env(), {"TELEGRAM_BOT_TOKEN": f'  "{VALID_TOKEN}"  '}),
            (_good_env(), {"PORTAL_TOKEN": VALID_TOKEN}),
            # effective token comes from process env, but the FILE dev token reuses a product token
            (_good_env(extra=f"ADMISSION_TOKEN={VALID_TOKEN}\n"), {"AUTO_FACTORY_DEV_BOT_TOKEN": TOKEN2}),
            # effective (process) token reuses a token that only lives in the file
            (_good_env(extra=f"OTHER_TOKEN={TOKEN2}\n"), {"AUTO_FACTORY_DEV_BOT_TOKEN": TOKEN2}),
        ]
        for body, penv in cases:
            with pytest.raises(ConfigError, match="reuses") as e:
                load_dev_env_file(_env_file(tmp_path, body), penv)
            assert VALID_TOKEN not in str(e.value) and TOKEN2 not in str(e.value)
        ok = load_dev_env_file(_env_file(tmp_path, _good_env(extra=f"TELEGRAM_BOT_TOKEN={TOKEN2}\n")), {})
        assert ok.token == VALID_TOKEN

    def test_symlink_or_reparse_env_rejected(self, tmp_path, monkeypatch):
        real = _env_file(tmp_path, _good_env())
        link = tmp_path / "linked.env"
        try:
            os.symlink(real, link)
        except (OSError, NotImplementedError):
            link = real  # no symlink privilege: simulate a reparse point via mocked attributes
            target = os.path.normcase(str(real))
            orig = wt_mod._is_link_or_reparse
            monkeypatch.setattr(wt_mod, "_is_link_or_reparse",
                                lambda p: os.path.normcase(str(p)) == target or orig(p))
        with pytest.raises(ConfigError, match="rejected"):
            load_dev_env_file(link, {})

    def test_demo_never_reads_env_file(self, monkeypatch, capsys):
        monkeypatch.setattr(dev_bot_mod, "load_dev_env_file", lambda *a, **k: pytest.fail("env read in demo"))
        monkeypatch.setattr(dev_bot_mod.urllib.request, "urlopen", lambda *a, **k: pytest.fail("network in demo"))
        assert dev_bot_mod.main(["--demo"]) == 0
        assert "RESULT PASS" in capsys.readouterr().out


# ============================================================ path / TOCTOU hardening
class TestPathHardening:
    def test_default_state_dir_canonical_hash(self, tmp_path, monkeypatch):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "lad"))
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
        a, b = tmp_path / "x" / "proj", tmp_path / "y" / "proj"
        a.mkdir(parents=True)
        b.mkdir(parents=True)
        da, dbb = default_state_dir(a), default_state_dir(b)
        assert da != dbb and da.name.startswith("proj-") and dbb.name.startswith("proj-")
        digest = _hashlib.sha256(os.path.normcase(str(a.resolve())).encode("utf-8")).hexdigest()[:8]
        assert da.name == f"proj-{digest}"
        assert default_state_dir(a / "." / ".." / "proj") == da
        base = tmp_path / ("lad" if os.name == "nt" else "xdg")
        assert da.parent == base / "auto_factory"
        assert not str(da.resolve()).startswith(str(a.resolve()))

    def test_ancestor_reparse_detected_with_mocked_attributes(self, tmp_path, monkeypatch):
        junction = tmp_path / "junction"
        junction.mkdir()
        real_lstat = os.lstat

        def fake_lstat(p, *a, **k):
            st = real_lstat(p, *a, **k)
            if os.path.normcase(os.fspath(p)) == os.path.normcase(str(junction)):
                return _types.SimpleNamespace(st_mode=st.st_mode, st_file_attributes=0x400)
            return st

        monkeypatch.setattr(wt_mod.os, "lstat", fake_lstat)
        try:
            assert wt_mod._is_link_or_reparse(junction)
            for p in (junction, junction / "deep" / "STOP.flag"):
                with pytest.raises(UnsafePathError):
                    _assert_no_link_or_reparse_ancestors(p)
            with pytest.raises(ValueError, match="reparse"):
                validate_state_layout(tmp_path / "repo", junction / "c.db", tmp_path / "a", tmp_path / "w",
                                      junction / "STOP.flag")
        finally:
            monkeypatch.undo()

    def test_no_follow_open_and_handle_verification(self, tmp_path, monkeypatch):
        a, b = tmp_path / "a.txt", tmp_path / "b.txt"
        a.write_text("A", encoding="utf-8")
        b.write_text("B", encoding="utf-8")
        assert safe_read_bytes(a, 10) == b"A"
        with pytest.raises(UnsafePathError, match="exceeds"):
            safe_read_bytes(a, 0)
        fd = safe_open_fd(a)
        try:  # the opened kernel object is not the expected path -> swapped
            with pytest.raises(UnsafePathError):
                wt_mod._verify_opened(fd, b.resolve())
        finally:
            os.close(fd)
        with pytest.raises((UnsafePathError, OSError)):
            safe_open_fd(tmp_path)  # a directory is never a plain file
        with pytest.raises(FileExistsError):
            safe_open_fd(a, write=True, create="new")
        if os.name == "nt":  # handle-level attributes say reparse point -> refused
            monkeypatch.setattr(wt_mod, "_win_handle_attributes", lambda h: (0x400, str(a.resolve())))
            with pytest.raises(UnsafePathError, match="reparse"):
                safe_open_fd(a)
            monkeypatch.undo()
        link = tmp_path / "swapped.txt"
        try:
            os.symlink(a, link)
        except (OSError, NotImplementedError):
            return
        with pytest.raises(UnsafePathError):
            safe_open_fd(link)
        # race: link swapped in AFTER the path check -> O_NOFOLLOW / FILE_FLAG_OPEN_REPARSE_POINT still refuses
        monkeypatch.setattr(wt_mod, "_assert_no_link_or_reparse_ancestors", lambda p: Path(os.path.abspath(p)))
        with pytest.raises((UnsafePathError, OSError)):
            safe_open_fd(link)

    def test_atomic_write_and_safe_unlink(self, tmp_path):
        p = tmp_path / "s" / "STOP.flag"
        safe_write_atomic(p, "one")
        safe_write_atomic(p, b"two")
        assert p.read_bytes() == b"two" and sorted(os.listdir(p.parent)) == ["STOP.flag"]
        (tmp_path / "dir_target").mkdir()
        with pytest.raises(UnsafePathError):
            safe_write_atomic(tmp_path / "dir_target", "x")
        assert safe_unlink(p) is True and not p.exists() and safe_unlink(p) is False
        with pytest.raises(UnsafePathError):
            safe_unlink(tmp_path / "dir_target")

    def test_validate_state_layout(self, tmp_path, repo):
        st = tmp_path / "state"
        good = validate_state_layout(repo, st / "c.db", st / "art", st / "wts", st / "STOP.flag")
        assert good["lock"] == (st / "supervisor.lock").resolve()
        validate_state_layout(repo, st / "c.db", st / "art", st / "wts", repo / "STOP.flag")  # repo root ok
        bad = [
            (repo / "c.db", st / "art", st / "wts", st / "STOP.flag"),
            (st / "c.db", repo / "art", st / "wts", st / "STOP.flag"),
            (st / "c.db", st / "art", repo / "wts", st / "STOP.flag"),
            (st / "x.sqlite3", st / "art", st / "wts", st / "STOP.flag"),
            (st / "c.db", tmp_path / "reports" / "art", st / "wts", st / "STOP.flag"),
            (st / "c.db", tmp_path / "holdout_eval", st / "wts", st / "STOP.flag"),
            (st / "c.db", tmp_path / ".env" / "a", st / "wts", st / "STOP.flag"),
            (st / "c.db", st / "art", st / "wts", st / "stop.txt"),
            (st / "c.db", st / "art", st / "wts", tmp_path / "elsewhere" / "STOP.flag"),
        ]
        for args in bad:
            with pytest.raises(ValueError):
                validate_state_layout(repo, *args)
        lay = resolve_state_paths(repo, st / "c.db")
        assert lay["stop_flag"] == (st / "STOP.flag").resolve() and lay["state_dir"] == st.resolve()


# ============================================================ supervisor.lock / ordering
class TestLocks:
    def test_exclusive_probe_and_try_acquire(self, tmp_path, no_leaked_locks):
        path = tmp_path / "state" / "supervisor.lock"
        assert not _FileLock(path, "supervisor").is_held() and not path.exists()  # probe never creates
        a = _FileLock(path, "supervisor").acquire()
        try:
            assert supervisor_lock_held() and _FileLock(path, "supervisor").is_held()
            with pytest.raises(SupervisorLocked):
                _FileLock(path, "supervisor").acquire()
            assert _FileLock(path, "supervisor").try_acquire() is False
        finally:
            a.release()
        assert not _FileLock(path, "supervisor").is_held()
        b = _FileLock(path, "supervisor")
        assert b.try_acquire() is True
        b.release()

    def test_lock_order_merge_then_supervisor_raises(self, tmp_path, no_leaked_locks):
        with _FileLock(tmp_path / "merge.lock", role="merge"):
            with pytest.raises(LockOrderError):
                _FileLock(tmp_path / "supervisor.lock", role="supervisor").acquire()
        with _FileLock(tmp_path / "supervisor.lock", role="supervisor"):
            with _FileLock(tmp_path / "merge.lock", role="merge"):
                pass  # supervisor -> merge is the allowed order
        with pytest.raises(ValueError):
            _FileLock(tmp_path / "x.lock", role="bogus")

    def test_cross_process_lock_blocks_standalone_entrypoints(self, tmp_path, repo, monkeypatch,
                                                              no_leaked_locks):
        st = tmp_path / "state"
        lock = st / "supervisor.lock"
        code = ("import sys; from pathlib import Path; from auto_factory.worktree import _FileLock; "
                "l = _FileLock(Path(sys.argv[1]), role='supervisor'); l.acquire(); print('locked', flush=True); "
                "sys.stdin.read()")
        proc = subprocess.Popen(
            [sys.executable, "-c", code, str(lock)], cwd=str(REPO_ROOT),
            env=build_sandbox_env({"PYTHONPATH": str(REPO_ROOT)}), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True,
        )
        try:
            assert proc.stdout.readline().strip() == "locked"
            assert _FileLock(lock, "supervisor").is_held()
            with pytest.raises(SupervisorLocked):
                _FileLock(lock, "supervisor").acquire()
            env_file = _env_file(tmp_path, _good_env())
            monkeypatch.delenv("AUTO_FACTORY_DEV_BOT_TOKEN", raising=False)
            monkeypatch.delenv("AUTO_FACTORY_ADMIN_CHAT_ID", raising=False)
            monkeypatch.setattr(dev_bot_mod.urllib.request, "urlopen", lambda *a, **k: pytest.fail("network"))
            common = ["--db", str(st / "c.db"), "--repo", str(repo)]
            from auto_factory.worker import main as worker_main

            assert worker_main(["--run", *common]) == 4
            assert dev_bot_mod.main(["--run", *common, "--env-file", str(env_file)]) == 4
            assert dev_bot_mod.main(["--poll-once", *common, "--env-file", str(env_file)]) == 4
            assert sup_mod.main(["--run", *common, "--env-file", str(env_file)]) == 4
            assert not (st / "c.db").exists()  # lock is taken BEFORE any DB open
            assert run_check(repo=repo, db=st / "c.db", env_file=env_file, process_env={},
                             transport_factory=lambda t: pytest.fail("network before local checks"),
                             adapters={}, out=lambda s: None) == 4
        finally:
            proc.stdin.close()
            proc.wait(timeout=20)

    def test_merge_and_reconcile_require_supervisor_lock(self, tmp_path, repo, db, no_leaked_locks):
        w, tid = awaiting_merge(tmp_path, repo, db)
        approve_merge(db, tid)
        coord = MergeCoordinator(db, w.wtm, w.guard, tmp_path / "artifacts", require_supervisor_lock=True)
        with pytest.raises(LockOrderError):
            coord.merge(tid, "human:owner")
        assert db.merge_intents(tid) == [] and db.get_task(tid)["state"] == "AWAITING_MERGE"
        with pytest.raises(LockOrderError):
            db.reconcile_merge_intents(w.wtm, require_supervisor_lock=True)
        bot, _ = make_bot(tmp_path, repo, db, boot=False, require_supervisor_lock=True)
        with pytest.raises(LockOrderError):
            bot.boot()
        with _FileLock(tmp_path / "state" / "supervisor.lock", role="supervisor"):
            assert bot.boot()["merges"] == []
            assert coord.merge(tid, "human:owner").ok
        assert db.get_task(tid)["state"] == "MERGED"


# ============================================================ DB retry / WAL
class _FlakyConn:
    def __init__(self, real, fails, msg="database is locked"):
        self.real, self.fails, self.msg, self.begins = real, fails, msg, 0

    def execute(self, sql, *a):
        if sql == "BEGIN IMMEDIATE":
            self.begins += 1
            if self.fails:
                self.fails -= 1
                raise _sqlite3.OperationalError(self.msg)
        return self.real.execute(sql, *a)

    def __getattr__(self, name):
        return getattr(self.real, name)


class TestDbRetry:
    def test_begin_immediate_retries_only_acquisition(self, db, monkeypatch):
        real = db._conn
        try:
            db._conn = flaky = _FlakyConn(real, fails=2)
            db.set_kv("k", "v")
            assert flaky.begins == 3 and db.get_kv("k") == "v"
            db._conn = flaky = _FlakyConn(real, fails=0)
            with pytest.raises(_sqlite3.OperationalError):
                with db._tx():
                    raise _sqlite3.OperationalError("database is locked")  # body errors are never retried
            assert flaky.begins == 1
            db._conn = flaky = _FlakyConn(real, fails=1, msg="no such table: x")
            with pytest.raises(_sqlite3.OperationalError, match="no such table"):
                db.set_kv("k", "w")
            assert flaky.begins == 1
            monkeypatch.setattr(db_mod, "TX_RETRY_BUDGET", 0.3)
            db._conn = flaky = _FlakyConn(real, fails=10**6, msg="database is busy")
            t0 = time.monotonic()
            with pytest.raises(_sqlite3.OperationalError, match="busy"):
                db.set_kv("k", "x")
            assert time.monotonic() - t0 < 2.0 and flaky.begins > 1
        finally:
            db._conn = real
        assert db.get_kv("k") == "v"

    def test_real_writer_contention_is_retried(self, db):
        other = _sqlite3.connect(db.path, timeout=5, isolation_level=None, check_same_thread=False)
        try:
            db._conn.execute("PRAGMA busy_timeout=0")  # force SQLITE_BUSY so the retry loop does the waiting
            other.execute("BEGIN IMMEDIATE")
            timer = _threading.Timer(0.3, lambda: other.execute("COMMIT"))
            timer.start()
            t0 = time.monotonic()
            db.set_kv("contended", 1)
            assert time.monotonic() - t0 >= 0.2 and db.get_kv("contended") == "1"
            timer.join()
        finally:
            other.close()

    def test_worker_backs_off_instead_of_blocking(self, tmp_path, repo, db, monkeypatch):
        w = make_worker(tmp_path, repo, db, claude=[_fix])
        tid = submit(db)
        real_preflight, calls = w._handlers["RECEIVED"], [0]

        def flaky_preflight(task):
            calls[0] += 1
            if calls[0] <= 2:
                raise _sqlite3.OperationalError("database is locked")
            return real_preflight(task)

        monkeypatch.setitem(w._handlers, "RECEIVED", flaky_preflight)
        assert w.process_task(tid) == "AWAITING_MERGE"
        assert w.sleeps == [0.5, 1.0] and "BLOCKED" not in [e["to_state"] for e in db.events(tid)]
        t2 = submit(db, "always locked")

        def always_locked(task):
            raise _sqlite3.OperationalError("database is locked")

        monkeypatch.setitem(w._handlers, "RECEIVED", always_locked)
        assert w.process_task(t2) == "DB_BUSY"
        assert db.get_task(t2)["state"] == "RECEIVED" and db.get_lease(t2) is None
        summary = w.run_cycle()
        assert (t2, "DB_BUSY") in summary["processed"] and not db.circuit_is_open(CIRCUIT)


# ============================================================ read-only --status / --check
class _GetMeTransport:
    def __init__(self, result=None, exc=None):
        self.calls, self.result, self.exc = 0, result, exc
        self.sent = []

    def __call__(self, token):
        assert token == VALID_TOKEN
        return self

    def get_me(self):
        self.calls += 1
        if self.exc is not None:
            raise self.exc
        return self.result if self.result is not None else {"id": 1, "is_bot": True, "username": "af_dev_bot"}

    def send_message(self, *a, **k):
        pytest.fail("--check must never send a message")


def _mock_clis(available=True):
    return {n: MockAdapter(n, is_available=available) for n in ("claude", "codex", "gemini")}


class TestReadOnlyCheckAndStatus:
    def test_read_db_summary_never_creates_or_migrates(self, tmp_path):
        missing = tmp_path / "state" / "c.db"
        with pytest.raises(FileNotFoundError):
            read_db_summary_readonly(missing)
        assert not missing.exists() and not missing.parent.exists()
        legacy = tmp_path / "legacy.db"
        con = _sqlite3.connect(legacy)
        con.executescript("CREATE TABLE approvals (id INTEGER PRIMARY KEY, task_id TEXT, decision TEXT);")
        con.commit()
        con.close()
        with pytest.raises(ValueError, match="not a coordinator DB"):
            read_db_summary_readonly(legacy)
        con = _sqlite3.connect(legacy)
        cols = {r[1] for r in con.execute("PRAGMA table_info(approvals)")}
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        con.close()
        assert cols == {"id", "task_id", "decision"} and tables == {"approvals"}  # untouched
        missing.parent.mkdir()
        d = CoordinatorDB(missing)
        tid, _ = d.create_task("r", "L1", [], {})
        d.close()
        s = read_db_summary_readonly(missing)
        assert s["tasks"][0]["id"] == tid and s["by_state"] == {"RECEIVED": 1} and s["open_merge_intents"] == 0

    def test_status_cli_is_read_only(self, tmp_path, repo, monkeypatch, capsys):
        dbp = tmp_path / "state" / "c.db"
        from auto_factory.worker import main as worker_main

        for entry in (dev_bot_mod.main, worker_main, sup_mod.main):
            assert entry(["--status", "--db", str(dbp), "--repo", str(repo)]) == 2
            assert not dbp.exists()
        dbp.parent.mkdir()
        d = CoordinatorDB(dbp)
        tid, _ = d.create_task("r", "L1", [], {})
        d.close()
        monkeypatch.setattr(dev_bot_mod, "CoordinatorDB", lambda *a, **k: pytest.fail("RW DB opened"))
        monkeypatch.setattr(worker_mod, "CoordinatorDB", lambda *a, **k: pytest.fail("RW DB opened"))
        monkeypatch.setattr(sup_mod, "CoordinatorDB", lambda *a, **k: pytest.fail("RW DB opened"))
        capsys.readouterr()
        for entry in (dev_bot_mod.main, worker_main, sup_mod.main):
            assert entry(["--status", "--db", str(dbp), "--repo", str(repo)]) == 0
            assert tid in capsys.readouterr().out

    def test_check_pass_is_read_only_and_calls_getme_once(self, tmp_path, repo, monkeypatch):
        st = tmp_path / "state"
        st.mkdir()
        d = CoordinatorDB(st / "c.db")
        tid, _ = d.create_task("r", "L1", [], {})
        d.close()
        before = (st / "c.db").read_bytes()
        monkeypatch.setattr(dev_bot_mod, "CoordinatorDB", lambda *a, **k: pytest.fail("CoordinatorDB in --check"))
        monkeypatch.setattr(DevBot, "boot", lambda *a, **k: pytest.fail("boot in --check"))
        tr, lines = _GetMeTransport(), []
        rc = run_check(repo=repo, db=st / "c.db", env_file=_env_file(tmp_path, _good_env()), process_env={},
                       transport_factory=tr, adapters=_mock_clis(), out=lines.append)
        assert rc == 0 and tr.calls == 1, lines
        text = "\n".join(lines)
        assert VALID_TOKEN not in text and "CHECK RESULT PASS" in text and "@af_dev_bot" in text
        assert all(ln.startswith("CHECK ") for ln in lines)
        assert (st / "c.db").read_bytes() == before
        assert not (st / "supervisor.lock").exists()

    def test_check_warnings_and_exit_codes(self, tmp_path, repo):
        st = tmp_path / "state"
        env_ok = _env_file(tmp_path, _good_env())

        def check(**kw):
            lines = []
            kw.setdefault("env_file", env_ok)
            kw.setdefault("transport_factory", _GetMeTransport())
            kw.setdefault("repo", repo)
            kw.setdefault("db", st / "c.db")
            rc = run_check(process_env={}, adapters=kw.pop("adapters", _mock_clis()), out=lines.append, **kw)
            assert VALID_TOKEN not in "\n".join(lines)
            return rc, lines

        (repo / "dirty.txt").write_text("x", encoding="utf-8")
        rc, lines = check(adapters=_mock_clis(available=False))
        assert rc == 0 and sum("CHECK WARN" in ln for ln in lines) >= 4  # dirty, 3 CLIs, no DB
        (repo / "dirty.txt").unlink()
        local_failures = [
            dict(env_file=_env_file(tmp_path, _good_env(extra=f"TELEGRAM_BOT_TOKEN={VALID_TOKEN}\n"), "reuse.env")),
            dict(env_file=None),
            dict(db=repo / "c.db"),
            dict(repo=tmp_path / "not_a_repo", db=tmp_path / "s2" / "c.db"),
        ]
        (tmp_path / "not_a_repo").mkdir()
        for kw in local_failures:
            tr = _GetMeTransport()
            rc, lines = check(transport_factory=tr, **kw)
            assert rc == 2 and tr.calls == 0, (kw, lines)  # network skipped on local failure
        bad_db = tmp_path / "s3" / "c.db"
        bad_db.parent.mkdir()
        bad_db.write_bytes(b"not sqlite at all")
        assert check(db=bad_db)[0] == 2
        for exc, result in ((TgUnknown("timeout"), None), (TgRejected("HTTP 401: Unauthorized"), None),
                            (None, {"id": 1, "is_bot": False})):
            assert check(transport_factory=_GetMeTransport(result=result, exc=exc))[0] == 3
        assert check(transport_factory=_GetMeTransport(exc=KeyboardInterrupt()))[0] == 130
        with _FileLock(st / "supervisor.lock", "supervisor"):
            tr = _GetMeTransport()
            assert check(transport_factory=tr)[0] == 4 and tr.calls == 0

    def test_check_cli_wiring(self, tmp_path, repo, monkeypatch, capsys):
        monkeypatch.delenv("AUTO_FACTORY_DEV_BOT_TOKEN", raising=False)
        monkeypatch.delenv("AUTO_FACTORY_ADMIN_CHAT_ID", raising=False)
        tr = _GetMeTransport()
        monkeypatch.setattr(dev_bot_mod, "HttpTransport", tr)
        monkeypatch.setattr(dev_bot_mod.urllib.request, "urlopen", lambda *a, **k: pytest.fail("network"))
        env_file = _env_file(tmp_path, _good_env())
        args = ["--check", "--db", str(tmp_path / "state" / "c.db"), "--repo", str(repo), "--env-file", str(env_file)]
        assert dev_bot_mod.main(args) == 0
        assert sup_mod.main(args) == 0
        out = capsys.readouterr().out
        assert tr.calls == 2 and out.count("CHECK RESULT PASS") == 2 and VALID_TOKEN not in out
        assert not (tmp_path / "state" / "c.db").exists()


# ============================================================ co-supervisor
def make_sup(tmp_path, repo, adapters=None, **kw):
    state = tmp_path / "state"
    tr = FakeTransport()
    if adapters is None:
        adapters = {"claude": MockAdapter("claude", [_fix] * 20), "codex": MockAdapter("codex"),
                    "gemini": MockAdapter("gemini")}
    params = dict(
        repo=repo, db_path=state / "coordinator.db", artifacts_dir=state / "artifacts",
        worktrees_dir=state / "worktrees", stop_flag=state / "STOP.flag", transport=tr, admin_chat_id=ADMIN,
        adapters=adapters, gate_runner=_InstantGates(), idle_sleep=0.05, poll_timeout=0, bot_idle_sleep=0.005,
        limiter=_no_limit(), worker_kwargs={"audit_tmp_dir": tmp_path / "clones"},
    )
    params.update(kw)
    return Supervisor(**params), tr


def _card_token(tr, tid, op, source="sent"):
    for m in reversed(list(tr.sent if source == "sent" else tr.edits)):
        if tid not in m["text"] or not m.get("reply_markup"):
            continue
        for row in m["reply_markup"]["inline_keyboard"]:
            for b in row:
                _, bop, tok = b["callback_data"].split("|")
                if bop == op:
                    return tok
    return None


_wait = sup_mod._wait_for


class TestSupervisor:
    def test_boot_order_lock_and_clean_close(self, tmp_path, repo, monkeypatch, no_leaked_locks):
        order = []
        real_boot, real_recover = DevBot.boot, PipelineWorker.recover_on_boot

        def boot(self):
            assert supervisor_lock_held()  # lock taken before boot
            order.append("bot.boot")
            return real_boot(self)

        def recover(self):
            order.append("worker.recover_on_boot")
            return real_recover(self)

        monkeypatch.setattr(DevBot, "boot", boot)
        monkeypatch.setattr(PipelineWorker, "recover_on_boot", recover)
        sup, tr = make_sup(tmp_path, repo)
        sup.open()
        try:
            assert order == ["bot.boot", "worker.recover_on_boot"]
            assert sup.bot_db is not sup.worker_db and sup.bot_db.journal_mode.lower() == "wal"
            assert sup.worker_db._query("PRAGMA busy_timeout")[0][0] == 5000
            assert sup.bot.require_supervisor_lock and sup.worker.auto_audit_l2
            other, _ = make_sup(tmp_path, repo)
            with pytest.raises(SupervisorLocked):
                other.open()
            assert other.bot_db is None
            sup.bot.poll_once(timeout=0)
            assert sup.bot_db.get_kv("poller_lease") is not None
        finally:
            sup.close()
        probe = CoordinatorDB(tmp_path / "state" / "coordinator.db")
        try:
            assert probe.get_kv("poller_lease") is None
        finally:
            probe.close()

    def test_boot_reconciles_merge_before_worker_recovery(self, tmp_path, repo, no_leaked_locks):
        st = tmp_path / "state"
        st.mkdir()
        d = CoordinatorDB(st / "coordinator.db")
        w = PipelineWorker(d, repo, {"claude": MockAdapter("claude", [_fix]), "codex": MockAdapter("codex"),
                                     "gemini": MockAdapter("gemini")},
                           artifacts_dir=st / "artifacts", worktrees_dir=st / "worktrees",
                           stop_flag=st / "STOP.flag", gate_runner=_InstantGates())
        tid = submit(d)
        assert w.process_task(tid) == "AWAITING_MERGE"
        approve_merge(d, tid)
        intent = d.begin_merge_intent(tid, "human:owner")
        w.wtm.merge_candidate(d.get_task(tid), w.guard)  # crash after git merge, before DB
        d.close()
        sup, tr = make_sup(tmp_path, repo)
        sup.open()
        try:
            assert (intent, "FINALIZED") in sup.boot_report["bot"]["merges"]
            assert sup.worker_db.get_task(tid)["state"] == "MERGED"
        finally:
            sup.close()

    def test_threads_wake_kill_resume_and_shutdown(self, tmp_path, repo, no_leaked_locks):
        sup, tr = make_sup(tmp_path, repo, idle_sleep=30.0)  # progress within seconds proves wake-ups
        sup.start()
        try:
            tr.updates.append(upd_msg("/task L1 calc.py --tests=tests/test_calc.py -- fix add one"))
            assert _wait(lambda: len(sup.worker_db.list_tasks()) == 1, 10, sup)
            tid = sup.worker_db.list_tasks()[0]["id"]
            assert _wait(lambda: sup.worker_db.get_task(tid)["state"] == "AWAITING_START_AUTH", 15, sup)
            assert _wait(lambda: _card_token(tr, tid, "s") is not None, 10, sup)
            tr.updates.append(upd_cb("s", _card_token(tr, tid, "s")))
            assert _wait(lambda: sup.worker_db.get_task(tid)["state"] == "AWAITING_MERGE", 15, sup)
            tr.updates.append(upd_msg("/kill"))
            assert _wait(sup.stop_flag.exists, 10, sup)
            tr.updates.append(upd_msg("/task L1 calc.py -- fix add two"))
            assert _wait(lambda: len(sup.worker_db.list_tasks()) == 2, 10, sup)
            t2 = [t["id"] for t in sup.worker_db.list_tasks() if t["id"] != tid][0]
            n = len(tr.sent)
            tr.updates.append(upd_msg("/status"))
            assert _wait(lambda: len(tr.sent) > n and "STOP.flag: <b>ON</b>" in tr.sent[-1]["text"], 10, sup)
            time.sleep(0.3)
            assert sup.worker_db.get_task(t2)["state"] == "RECEIVED"  # worker paused, bot responsive
            tr.updates.append(upd_msg("/resume"))
            assert _wait(lambda: sup.worker_db.get_task(t2)["state"] == "AWAITING_START_AUTH", 15, sup)
            assert not sup.stop_flag.exists() and not sup.errors
        finally:
            sup.shutdown()
        assert not any(t.is_alive() for t in sup.threads)

    def test_periodic_fallback_without_wake(self, tmp_path, repo, no_leaked_locks):
        sup, tr = make_sup(tmp_path, repo, idle_sleep=0.1)
        sup.start()
        try:
            side = CoordinatorDB(tmp_path / "state" / "coordinator.db")
            try:
                tid = submit(side, start=False)  # no /task, no wake: only the periodic cycle sees it
            finally:
                side.close()
            assert _wait(lambda: sup.worker_db.get_task(tid)["state"] == "AWAITING_START_AUTH", 15, sup)
        finally:
            sup.shutdown()

    def test_thread_failure_and_ctrl_c_shut_down_cleanly(self, tmp_path, repo, monkeypatch, no_leaked_locks):
        sup, tr = make_sup(tmp_path, repo)
        sup.open()

        def boom():
            raise RuntimeError("worker exploded")

        monkeypatch.setattr(sup.worker, "run_cycle", boom)
        assert sup.run() == 1
        assert [n for n, _ in sup.errors] == ["worker"] and not any(t.is_alive() for t in sup.threads)
        sup2, _ = make_sup(tmp_path, repo)

        def ctrl_c():
            raise KeyboardInterrupt

        monkeypatch.setattr(sup2, "_main_wait", ctrl_c)
        assert sup2.run() == 0 and sup2.stop_event.is_set() and not any(t.is_alive() for t in sup2.threads)

    def test_transient_db_error_in_thread_is_retried(self, tmp_path, repo, monkeypatch, no_leaked_locks):
        sup, tr = make_sup(tmp_path, repo, busy_backoff=0.01)
        sup.open()
        real, n = sup.bot.dispatch_once, [0]

        def flaky(limit=10):
            n[0] += 1
            if n[0] <= 3:
                raise _sqlite3.OperationalError("database is locked")
            return real(limit)

        monkeypatch.setattr(sup.bot, "dispatch_once", flaky)
        sup.start()
        try:
            assert _wait(lambda: sup.bot_iterations >= 3, 10, sup)
            assert not sup.errors
        finally:
            sup.shutdown()

    def test_run_supervisor_once(self, tmp_path, repo, no_leaked_locks):
        sup, tr = make_sup(tmp_path, repo)
        tr.updates.append(upd_msg("/task L1 calc.py -- once"))
        kw = dict(repo=sup.repo, db_path=sup.db_path, artifacts_dir=sup.artifacts_dir, worktrees_dir=sup.worktrees_dir,
                  stop_flag=sup.stop_flag, transport=tr, admin_chat_id=ADMIN, adapters=sup.adapters,
                  gate_runner=_InstantGates(), limiter=_no_limit())
        res = run_supervisor_once(**kw)
        assert res["handled"] == 1 and len(res["worker"]["processed"]) == 1
        tid, final = res["worker"]["processed"][0]
        assert final == "AWAITING_START_AUTH" and _card_token(tr, tid, "s")

    def test_soak_concurrent_tasks_approvals_outbox_and_merge(self, tmp_path, repo, no_leaked_locks):
        sup, tr = make_sup(tmp_path, repo)
        sup.start()
        try:
            texts = [f"/task L1 calc.py --tests=tests/test_calc.py -- soak fix {i}" for i in range(4)]
            texts.append("/task L2 calc.py --tests=tests/test_calc.py -- soak audited change")
            for t in texts:
                tr.updates.append(upd_msg(t))
            assert _wait(lambda: len(sup.worker_db.list_tasks()) == 5, 20, sup)
            ids = [t["id"] for t in sup.worker_db.list_tasks()]
            assert _wait(lambda: all(_card_token(tr, i, "s") for i in ids), 60, sup), \
                [sup.worker_db.get_task(i)["state"] for i in ids]
            for i in ids:
                tr.updates.append(upd_cb("s", _card_token(tr, i, "s")))
            assert _wait(lambda: all(sup.worker_db.get_task(i)["state"] == "AWAITING_MERGE" for i in ids), 90, sup), \
                [sup.worker_db.get_task(i)["state"] for i in ids]
            first, second = ids[0], ids[1]
            assert _wait(lambda: _card_token(tr, first, "m") and _card_token(tr, second, "m"), 20, sup)
            for tid, expect in ((first, "MERGED"), (second, "BLOCKED")):
                tr.updates.append(upd_cb("m", _card_token(tr, tid, "m")))
                assert _wait(lambda: _card_token(tr, tid, "M", "edits") is not None, 10, sup)
                tr.updates.append(upd_cb("M", _card_token(tr, tid, "M", "edits")))
                assert _wait(lambda: sup.worker_db.get_task(tid)["state"] == expect, 20, sup)
            assert "REBASE_REQUIRED" in sup.worker_db.events(second)[-1]["reason"]
            assert _wait(lambda: sup.worker_db.outbox_counts()["PENDING"] == 0, 10, sup)
            assert sup.bot.wtm.head_sha() == sup.worker_db.get_task(first)["target_sha"]
            l2 = [i for i in ids if sup.worker_db.get_task(i)["risk_level"] == "L2"][0]
            auditor = verify_phase1_artifacts(sup.worker_db, sup.worker_db.get_task(l2))["auditor"]
            assert auditor == "codex" and sup.worker_db.get_task(l2)["coder"] != auditor
            for i in ids:
                assert_event_chain(sup.worker_db, i)
            assert not sup.errors
        finally:
            sup.shutdown()

    def test_supervisor_demo_cli_runs_offline(self):
        env = build_sandbox_env({"PYTHONPATH": str(REPO_ROOT)})
        env["AUTO_FACTORY_DEV_BOT_TOKEN"] = "garbage"  # demo must not even look at it
        proc = subprocess.run(
            [sys.executable, "-m", "auto_factory", "--demo"], cwd=str(REPO_ROOT), env=env,
            stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=180,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "RESULT PASS" in proc.stdout and "FAIL" not in proc.stdout.replace("RESULT PASS", "")


# ============================================================ automated L2 Phase 1
def make_auto_worker(tmp_path, repo, db, **kw):
    return make_worker(tmp_path, repo, db, auto_audit_l2=True, audit_tmp_dir=tmp_path / "clones", **kw)


def _audit_out(**over):
    def step(wt, contract, role):
        data = MockAdapter.default_audit(contract)
        data.update(over)
        return data
    return step


class _OsNameProxy:
    """Stand-in for `worker_mod.os` that overrides only `name` (patching the real os.name breaks pathlib)."""

    def __init__(self, name):
        self.name = name

    def __getattr__(self, attr):
        return getattr(os, attr)


def _clones_left(tmp_path):
    d = tmp_path / "clones"
    return sorted(os.listdir(d)) if d.exists() else []


def write_phase1(db, tmp_path, tid, auditor="codex", **audit_over):
    t = db.get_task(tid)
    d = tmp_path / "manual" / tid
    d.mkdir(parents=True)
    audit = {"schema_version": 1, "task_id": tid, "verdict": "approve", "summary": "manual audit",
             "base_sha": t["base_sha"], "allowed_paths": t["allowed_paths"], "auditor": auditor}
    audit.update(audit_over)
    ap = d / "audit.json"
    ap.write_bytes(_json.dumps(audit).encode())
    plan = {"schema_version": 1, "task_id": tid, "base_sha": t["base_sha"], "allowed_paths": t["allowed_paths"],
            "acceptance_tests": [], "request": "r", "auditor": auditor, "audit_sha256": sha256_file(ap)}
    pp = d / "plan.json"
    pp.write_bytes(_json.dumps(plan).encode())
    db.add_artifact(tid, "audit", ap, auditor)
    db.add_artifact(tid, "plan", pp, auditor)
    return ap, pp


def submit_l2(db, repo, request="logic change", start=True):
    tid = submit(db, request, risk="L2", start=start)
    head = WorktreeManager(repo).head_sha()
    db.update_fields(tid, db.get_task(tid)["state_version"], {"base_sha": head}, "test")
    return tid


class TestAutoAuditL2:
    def test_prompt_is_shim_safe_and_parser_is_strict(self):
        prompt = render_prompt("auditor", r"C:\Users\x\AppData\Local\auto_factory\s-1\artifacts\t1\phase1_handoff.json")
        assert _SAFE_SHIM_ARG_RE.match(prompt) and "phase1_handoff.json" in prompt
        assert not any(ch in prompt for ch in '"()<>%!^&|')
        good = {"schema_version": 1, "task_id": "t1", "verdict": "approve", "summary": "ok", "base_sha": "a" * 40,
                "allowed_paths": ["calc.py"]}
        assert parse_audit_result("```json\n" + _json.dumps(good) + "\n```", "t1", "a" * 40, ["calc.py"]) == good
        assert parse_audit_result(_json.dumps(dict(good, verdict="block")), "t1", "a" * 40, ["calc.py"])["verdict"] \
            == "block"
        bad = ["", "no json", _json.dumps(good) + _json.dumps(good), _json.dumps(dict(good, extra=1)),
               _json.dumps({k: v for k, v in good.items() if k != "summary"}), _json.dumps(dict(good, task_id="t2")),
               _json.dumps(dict(good, base_sha="b" * 40)), _json.dumps(dict(good, allowed_paths=["x.py"])),
               _json.dumps(dict(good, schema_version="1")), _json.dumps(dict(good, schema_version=True)),
               _json.dumps(dict(good, verdict="reject")), _json.dumps(dict(good, summary="s" * 301)), "[1, 2]",
               '{"verdict": "approve"', _json.dumps(dict(good, auditor="codex"))]
        for out in bad:
            with pytest.raises(AuditParseError):
                parse_audit_result(out, "t1", "a" * 40, ["calc.py"])
        assert parse_verdict('{"verdict": "approve"}') == "approve"  # reviewer parsing unchanged

    def test_approve_flows_to_awaiting_merge_with_independent_roles(self, tmp_path, repo, db):
        w = make_auto_worker(tmp_path, repo, db, claude=[_fix])
        tid = submit(db, "logic change", risk="L2")
        assert w.process_task(tid) == "AWAITING_MERGE"
        t = db.get_task(tid)
        info = verify_phase1_artifacts(db, t)
        assert info["auditor"] == "codex" and t["coder"] == "claude"
        assert w.adapters["codex"].calls == [("auditor", tid)]
        assert [a["provider"] for a in db.attempts(tid, "reviewer")] == ["gemini"]
        assert ("auditor", tid) not in w.adapters["claude"].calls
        assert (tmp_path / "artifacts" / tid / "phase1_handoff.json").is_file()
        contract = _json.loads((tmp_path / "artifacts" / tid / "phase1_handoff.json").read_text(encoding="utf-8"))
        assert contract["next_role"] == "auditor" and contract["base_sha"] == t["base_sha"]
        assert _clones_left(tmp_path) == [] and w.wtm.main_is_clean()
        assert "phase1" not in "".join(w.wtm.git("worktree", "list"))  # never a git worktree

    def test_block_verdict_stops_before_any_writer(self, tmp_path, repo, db):
        w = make_auto_worker(tmp_path, repo, db, codex=[_audit_out(verdict="block", summary="unsafe design")])
        tid = submit(db, "logic change", risk="L2")
        assert w.process_task(tid) == "BLOCKED"
        assert "AUDIT_BLOCKED" in db.events(tid)[-1]["reason"] and "unsafe design" in db.events(tid)[-1]["reason"]
        assert db.artifacts(tid, "audit") == [] and db.artifacts(tid, "audit_rejected")
        assert w.adapters["claude"].calls == [] and w.adapters["gemini"].calls == []

    @pytest.mark.parametrize("output", [
        "I approve this", '{"verdict": "approve"}', "two", _audit_out(extra="x"), _audit_out(base_sha="0" * 40),
        _audit_out(allowed_paths=["other.py"]), _audit_out(task_id="t-other"), _audit_out(summary="s" * 301),
        _audit_out(auditor="codex"),
    ])
    def test_malformed_audit_blocks_without_fallback(self, tmp_path, repo, db, output):
        if output == "two":
            def output(wt, contract, role):
                one = _json.dumps(MockAdapter.default_audit(contract))
                return one + "\n" + one
        elif isinstance(output, str):
            text = output

            def output(wt, contract, role):
                return text
        w = make_auto_worker(tmp_path, repo, db, codex=[output])
        tid = submit(db, "logic change", risk="L2")
        assert w.process_task(tid) == "BLOCKED"
        assert "AUDIT_INVALID" in db.events(tid)[-1]["reason"]
        assert w.adapters["gemini"].calls == [] and w.adapters["claude"].calls == []
        assert db.artifacts(tid, "audit") == [] and _clones_left(tmp_path) == []

    @pytest.mark.parametrize("codex_state", ["unavailable", "RATE_LIMIT", "QUOTA", "TIMEOUT"])
    def test_codex_failover_to_gemini(self, tmp_path, repo, db, codex_state):
        w = make_auto_worker(tmp_path, repo, db, claude=[_fix],
                             codex=[] if codex_state == "unavailable" else [codex_state])
        if codex_state == "unavailable":
            w.adapters["codex"].is_available = False
        tid = submit(db, "logic change", risk="L2")
        assert w.process_task(tid) == "AWAITING_MERGE"
        t = db.get_task(tid)
        assert verify_phase1_artifacts(db, t)["auditor"] == "gemini" and t["coder"] == "claude"
        assert [a["provider"] for a in db.attempts(tid, "auditor")] == (
            ["gemini"] if codex_state == "unavailable" else ["codex", "gemini"])

    def test_internal_auditor_failure_does_not_fall_back(self, tmp_path, repo, db):
        w = make_auto_worker(tmp_path, repo, db, codex=["INTERNAL"])
        tid = submit(db, "logic change", risk="L2")
        assert w.process_task(tid) == "BLOCKED"
        assert "AUDIT_FAILED" in db.events(tid)[-1]["reason"] and w.adapters["gemini"].calls == []

    def test_no_auditor_available_and_claude_never_audits(self, tmp_path, repo, db):
        w = make_auto_worker(tmp_path, repo, db, claude=[_fix])
        w.adapters["codex"].is_available = False
        w.adapters["gemini"].is_available = False
        tid = submit(db, "logic change", risk="L2")
        assert w.process_task(tid) == "BLOCKED"
        assert "AUDIT_UNAVAILABLE" in db.events(tid)[-1]["reason"]
        assert w.adapters["claude"].calls == [] and db.attempts(tid, "auditor") == []

    def test_auditor_never_writes_its_own_audit(self, tmp_path, repo, db):
        w = make_auto_worker(tmp_path, repo, db, gemini=[_fix])
        tid = submit(db, "logic change", risk="L2", start=False)
        assert w.process_task(tid) == "AWAITING_START_AUTH"
        assert verify_phase1_artifacts(db, db.get_task(tid))["auditor"] == "codex"
        w.adapters["claude"].is_available = False  # codex would be next writer in WRITER_ORDER
        db.add_approval(tid, "start")
        assert w.process_task(tid) == "AWAITING_MERGE"
        t = db.get_task(tid)
        assert t["coder"] == "gemini" and ("writer", tid) not in w.adapters["codex"].calls
        assert [a["provider"] for a in db.attempts(tid, "reviewer")] == ["codex"]  # auditor may still review
        # sole survivor == auditor -> may not implement its own audit
        t2 = submit(db, "second change", risk="L2", start=False)
        assert w.process_task(t2) == "AWAITING_START_AUTH"
        assert verify_phase1_artifacts(db, db.get_task(t2))["auditor"] == "codex"
        w.adapters["gemini"].is_available = False
        db.add_approval(t2, "start")
        assert w.process_task(t2) == "BLOCKED"
        assert "IMPLEMENTATION_BLOCKED" in db.events(t2)[-1]["reason"]
        assert ("writer", t2) not in w.adapters["codex"].calls

    def test_clone_mutation_blocks(self, tmp_path, repo, db):
        def mutate_clone(wt, contract, role):
            Path(wt, "calc.py").write_text("hacked\n", encoding="utf-8")
            return MockAdapter.default_audit(contract)

        w = make_auto_worker(tmp_path, repo, db, codex=[mutate_clone])
        tid = submit(db, "logic change", risk="L2")
        assert w.process_task(tid) == "BLOCKED"
        assert "AUDITOR_MUTATED_REPO" in db.events(tid)[-1]["reason"]
        assert db.artifacts(tid, "audit") == [] and _clones_left(tmp_path) == []
        assert "hacked" not in (repo / "calc.py").read_text(encoding="utf-8")

    @pytest.mark.parametrize("mutation", ["ignored_file", "untracked_file", "git_config", "new_branch", "hook",
                                          "commit"])
    def test_main_repo_mutation_blocks_and_is_not_reverted(self, tmp_path, repo, db, mutation):
        def mutate_main(wt, contract, role):
            if mutation == "ignored_file":
                (repo / "__pycache__").mkdir(exist_ok=True)
                (repo / "__pycache__" / "evil.pyc").write_bytes(b"x")
            elif mutation == "untracked_file":
                (repo / "evil.txt").write_text("x", encoding="utf-8")
            elif mutation == "git_config":
                _git_main(repo, "config", "--local", "core.hooksPath", "evil-hooks")
            elif mutation == "new_branch":
                _git_main(repo, "branch", "evil")
            elif mutation == "hook":
                (repo / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            elif mutation == "commit":
                _git_main(repo, "commit", "--allow-empty", "-q", "-m", "evil")
            return MockAdapter.default_audit(contract)

        w = make_auto_worker(tmp_path, repo, db, codex=[mutate_main])
        tid = submit(db, "logic change", risk="L2")
        assert w.process_task(tid) == "BLOCKED"
        assert "AUDITOR_MUTATED_REPO" in db.events(tid)[-1]["reason"]
        assert w.adapters["gemini"].calls == [] and db.artifacts(tid, "audit") == []
        if mutation == "new_branch":  # evidence preserved for a human, never auto-reverted
            assert w.wtm.git("branch", "--list", "evil").strip()

    def test_cleanup_failure_blocks(self, tmp_path, repo, db, monkeypatch):
        w = make_auto_worker(tmp_path, repo, db)

        def broken(path):
            raise OSError("file in use")

        monkeypatch.setattr(w.wtm, "remove_disposable_clone", broken)
        tid = submit(db, "logic change", risk="L2")
        assert w.process_task(tid) == "BLOCKED"
        assert "AUDITOR_CLEANUP_FAILED" in db.events(tid)[-1]["reason"]
        assert db.artifacts(tid, "audit") == []
        assert "AWAITING_START_AUTH" not in [e["to_state"] for e in db.events(tid)]

    def test_merge_lock_busy_keeps_task_in_planning(self, tmp_path, repo, db):
        w = make_auto_worker(tmp_path, repo, db)
        tid = submit(db, "logic change", risk="L2", start=False)
        with _FileLock(w.wtm.merge_lock_path, role="merge"):
            assert w.process_task(tid) == "PLANNING"
            assert w.adapters["codex"].calls == []
        assert w.process_task(tid) == "AWAITING_START_AUTH"

    def test_preexisting_valid_vs_tampered_artifacts(self, tmp_path, repo, db):
        w = make_auto_worker(tmp_path, repo, db, claude=[_fix])
        good = submit_l2(db, repo, "manual phase1 ok")
        write_phase1(db, tmp_path, good)
        assert w.process_task(good) == "AWAITING_MERGE"
        assert all(role != "auditor" for a in w.adapters.values() for role, _ in a.calls)  # verified, not re-run
        assert db.get_task(good)["coder"] == "claude"
        tampered = submit_l2(db, repo, "manual phase1 tampered")
        ap, _ = write_phase1(db, tmp_path, tampered)
        ap.write_bytes(ap.read_bytes().replace(b"manual audit", b"edited audit"))
        assert w.process_task(tampered) == "BLOCKED"
        assert "AUDIT_INVALID" in db.events(tampered)[-1]["reason"]
        for name, over in (("wrong sha", {"base_sha": "f" * 40}), ("claude auditor", {"auditor": "claude"}),
                           ("rejected", {"verdict": "block"})):
            tid = submit_l2(db, repo, f"manual phase1 {name}")
            write_phase1(db, tmp_path, tid, **over)
            assert w.process_task(tid) == "BLOCKED", name
        legacy = submit(db, "legacy artifacts", risk="L2")
        db.add_artifact(legacy, "plan", "plan.md", "gemini", sha256="p")
        db.add_artifact(legacy, "audit", "gpt_architecture.md", "codex", sha256="a")
        assert w.process_task(legacy) == "BLOCKED"  # strict mode refuses unverifiable legacy rows

    def test_tamper_after_audit_blocks_before_writer(self, tmp_path, repo, db):
        w = make_auto_worker(tmp_path, repo, db, claude=[_fix])
        tid = submit(db, "logic change", risk="L2", start=False)
        assert w.process_task(tid) == "AWAITING_START_AUTH"
        plan = Path(db.artifacts(tid, "plan")[-1]["path"])
        plan.write_bytes(plan.read_bytes().replace(b"logic change", b"logic CHANGE"))
        db.add_approval(tid, "start")
        assert w.process_task(tid) == "BLOCKED"
        assert "AUDIT_INVALID" in db.events(tid)[-1]["reason"]
        assert all(role != "writer" for a in w.adapters.values() for role, _ in a.calls)

    def test_legacy_mode_default_unchanged(self, tmp_path, repo, db):
        w = make_worker(tmp_path, repo, db)
        assert w.auto_audit_l2 is False
        tid = submit(db, "logic change", risk="L2")
        assert w.process_task(tid) == "BLOCKED" and "AUDIT_INCOMPLETE" in db.events(tid)[-1]["reason"]
        assert all(a.calls == [] for a in w.adapters.values())

    # ---- disposable audit clone dir: Windows must keep inherited ACLs (codex unelevated sandbox reads)
    def test_make_audit_clone_dir_unique_under_base_and_cleaned(self, tmp_path, repo):
        base = tmp_path / "clones" / "nested"  # parent created on demand
        made = [worker_mod._make_audit_clone_dir(base) for _ in range(5)]
        assert len({p.name for p in made}) == 5
        for p in made:
            assert p.is_dir() and p.name.startswith("af_audit_") and os.listdir(p) == []
            assert os.path.normcase(str(p.resolve().parent)) == os.path.normcase(str(base.resolve()))
        wtm = WorktreeManager(repo)
        for p in made:
            wtm.remove_disposable_clone(p)
        assert os.listdir(base) == []

    def test_make_audit_clone_dir_posix_branch_uses_mkdtemp(self, tmp_path, monkeypatch):
        calls = []
        real_mkdtemp = worker_mod.tempfile.mkdtemp

        def spy(prefix=None, dir=None, **kw):
            calls.append((prefix, dir))
            return real_mkdtemp(prefix=prefix, dir=dir)

        monkeypatch.setattr(worker_mod.tempfile, "mkdtemp", spy)
        monkeypatch.setattr(worker_mod, "os", _OsNameProxy("posix"))  # only worker's view of os.name
        p = worker_mod._make_audit_clone_dir(tmp_path)
        monkeypatch.undo()
        assert calls == [("af_audit_", tmp_path)] and p.is_dir() and p.parent == tmp_path
        if sys.platform != "win32":
            assert (os.stat(p).st_mode & 0o777) == 0o700

    def test_make_audit_clone_dir_windows_branch_retries_collision(self, tmp_path, monkeypatch):
        names = iter(["aaaaaaaaaaaa", "aaaaaaaaaaaa", "bbbbbbbbbbbb"])

        def no_mkdtemp(*a, **k):
            raise AssertionError("mkdtemp must not be used on nt")

        monkeypatch.setattr(worker_mod, "os", _OsNameProxy("nt"))
        monkeypatch.setattr(worker_mod.tempfile, "mkdtemp", no_mkdtemp)
        monkeypatch.setattr(worker_mod.uuid, "uuid4", lambda: _types.SimpleNamespace(hex=next(names) + "0" * 20))
        first = worker_mod._make_audit_clone_dir(tmp_path)
        second = worker_mod._make_audit_clone_dir(tmp_path)  # collides once, then succeeds
        assert (first.name, second.name) == ("af_audit_aaaaaaaaaaaa", "af_audit_bbbbbbbbbbbb")
        monkeypatch.setattr(worker_mod.uuid, "uuid4", lambda: _types.SimpleNamespace(hex="a" * 32))
        with pytest.raises(FileExistsError):
            worker_mod._make_audit_clone_dir(tmp_path)  # exhausted -> raises, never reuses an existing dir
        assert sorted(os.listdir(tmp_path)) == ["af_audit_aaaaaaaaaaaa", "af_audit_bbbbbbbbbbbb"]

    def test_make_audit_clone_dir_rejects_unsafe_parent(self, tmp_path, monkeypatch):
        calls = []

        def unsafe(path):
            calls.append(Path(path))
            raise UnsafePathError(f"symlink/reparse point in path: {path}")

        monkeypatch.setattr(worker_mod, "os", _OsNameProxy("nt"))
        monkeypatch.setattr(worker_mod, "_assert_no_link_or_reparse_ancestors", unsafe)
        with pytest.raises(UnsafePathError):
            worker_mod._make_audit_clone_dir(tmp_path / "clones")
        assert calls == [tmp_path / "clones"] and os.listdir(tmp_path / "clones") == []

    def test_clone_dir_failure_blocks_as_clone_failed(self, tmp_path, repo, db, monkeypatch):
        w = make_auto_worker(tmp_path, repo, db)
        removed = []

        def boom(base_dir):
            raise UnsafePathError("symlink/reparse point in path: x")

        monkeypatch.setattr(worker_mod, "_make_audit_clone_dir", boom)
        monkeypatch.setattr(w.wtm, "remove_disposable_clone", lambda p: removed.append(p))
        tid = submit(db, "logic change", risk="L2")
        assert w.process_task(tid) == "BLOCKED"  # no crash of the worker loop
        reason = db.events(tid)[-1]["reason"]
        assert "AUDIT_CLONE_FAILED" in reason and "UnsafePathError" in reason
        assert removed == [] and w.adapters["codex"].calls == [] and db.artifacts(tid, "audit") == []

    def test_audit_clone_dir_keeps_inherited_acl_on_windows(self, tmp_path):
        base = tmp_path / "clones"
        clone_dir = worker_mod._make_audit_clone_dir(base)
        stripped = Path(worker_mod.tempfile.mkdtemp(prefix="af_strip_", dir=base))
        assert clone_dir.is_dir() and stripped.is_dir()
        if sys.platform == "win32":
            def aces(p):
                out = subprocess.run(["icacls", str(p)], capture_output=True, text=True, errors="replace",
                                     timeout=30)
                assert out.returncode == 0, out.stdout + out.stderr
                return [ln for ln in out.stdout.splitlines() if ":(" in ln]

            ours, theirs = aces(clone_dir), aces(stripped)
            assert ours and all("(I)" in ace for ace in ours)  # inherited from parent, not re-written
            assert theirs and not any("(I)" in ace for ace in theirs)  # mkdtemp(0o700) strips inheritance
        else:
            assert (os.stat(clone_dir).st_mode & 0o777) == 0o700


# ######################################################################## Direction A: bot presets
from auto_factory.dev_bot import UNLOCKED_PRESETS_ENV, parse_do_command
from auto_factory.guardrails import is_forbidden
from auto_factory.presets import (
    CALLBACK_TOKEN_LEN,
    PRESET_POLICY_VERSION,
    PRESETS,
    Preset,
    decode_preset_token,
    encode_preset_token,
    parse_unlocked_presets,
)

ALL_PRESETS = ("brief", "travel", "admission", "portal", "silver", "kids", "yolo", "scripts")


def _tg_action_count(d):
    return d._query("SELECT COUNT(*) FROM tg_actions")[0][0]


class TestPresetRegistry:
    def test_registry_has_all_bots_and_real_files(self):
        assert tuple(PRESETS) == ALL_PRESETS
        for alias, p in PRESETS.items():
            assert p.alias == alias and p.min_risk == "L2"
            assert p.is_product is (alias != "scripts")
            assert set(p.tests) <= set(p.allowed_paths)
            for rel in p.allowed_paths + p.tests:
                assert (REPO_ROOT / rel).is_file(), rel  # real workspace repo
                assert rel.endswith(".py") and not rel.endswith("/") and "*" not in rel
                assert not is_forbidden(rel) and not is_forbidden(rel + "/")
                assert not rel.endswith("bot_runner.py") and ".sqlite" not in rel and not rel.endswith(".db")

    def test_preset_validation_rejects_unsafe_definitions(self):
        good = dict(alias="ok", label="L", description="d", allowed_paths=("a/b.py",), tests=("tests/test_b.py",))
        assert Preset(**good).alias == "ok"
        for over in (
            {"alias": "Bad"}, {"alias": "kid-s"}, {"alias": "a" * 23}, {"alias": ""}, {"min_risk": "L1"},
            {"allowed_paths": ()}, {"tests": ()}, {"allowed_paths": ("pkg/",)}, {"allowed_paths": ("pkg/*.py",)},
            {"allowed_paths": ("data/x.py",)}, {"allowed_paths": ("x.db",)}, {"allowed_paths": ("x.sqlite3",)},
            {"allowed_paths": (".env",)}, {"allowed_paths": ("telegram_bot_runner.py",)},
            {"allowed_paths": ("admin_server.py",)}, {"allowed_paths": ("../x.py",)},
            {"allowed_paths": ("holdout_eval/t.py",)}, {"allowed_paths": ("tests/conftest.py",)},
            {"allowed_paths": ("reports/a.py",)}, {"allowed_paths": ("a.py", "a.py")}, {"tests": ("x.txt",)},
        ):
            with pytest.raises(ValueError):
                Preset(**dict(good, **over))

    def test_guardrails_additive_patterns(self):
        for p in ("x.sqlite", "data/a.db", "a.db-wal", "a.db-shm", "data/x.py", ".git/config", ".github/w.yml",
                  "AGENTS.md", "run_cron.sh", "admin_server.py", "telegram_bot_runner.py", "portal_bot_runner.py",
                  ".env", "db.sqlite3", "reports/a.html", "holdout_eval/t.py", "STOP.flag"):
            assert is_forbidden(p), p
        for p in ("auto_factory/worker.py", "kids_compass/models.py", "tests/test_kids_compass.py", "calc.py"):
            assert not is_forbidden(p), p

    def test_preset_token_roundtrip(self):
        for alias in ALL_PRESETS:
            tok = encode_preset_token(alias)
            assert len(tok) == CALLBACK_TOKEN_LEN and decode_preset_token(tok) == alias
            assert len(f"af2|p|{tok}".encode("ascii")) <= 64
            assert validate_callback_data(f"af2|p|{tok}") == f"af2|p|{tok}"
        for bad in ("kids", "kids" + "-" * 19, "-" * 22, "-kids" + "-" * 17, "KIDS" + "-" * 18, "kids-x" + "-" * 16):
            with pytest.raises(ValueError):
                decode_preset_token(bad)
        with pytest.raises(ValueError):
            encode_preset_token("kid-s")

    def test_task_parser_default_still_rejects_product_paths(self, repo):
        for path in ("market_brief/core.py", "silver_care/models.py", "kids_compass/models.py",
                     "yolo_brief/models.py", "travel_brief/models.py", "admin_server.py", "AGENTS.md"):
            with pytest.raises(TaskParseError):
                parse_task_command(f"/task L2 {path} -- r", repo)
        assert "market_brief/" in dev_bot_mod.PROTECTED_PATHS and "silver_care/" in dev_bot_mod.PROTECTED_PATHS


class TestPresetConfig:
    def test_parse_unlocked_presets_strict(self):
        assert parse_unlocked_presets(None) == frozenset() and parse_unlocked_presets("") == frozenset()
        assert parse_unlocked_presets("kids,yolo") == {"kids", "yolo"}
        for bad in ("kids, yolo", " kids", "kids ", "kids,,yolo", "kids,", ",kids", "kids,kids", "nope",
                    "키즈", "KIDS", "kid-s", "kids\tyolo", "kids\nyolo"):
            with pytest.raises(ValueError):
                parse_unlocked_presets(bad)

    def test_env_file_and_process_override(self, tmp_path):
        base = {"AUTO_FACTORY_DEV_BOT_TOKEN": VALID_TOKEN, "AUTO_FACTORY_ADMIN_CHAT_ID": CHAT}
        assert load_dev_env_file(_env_file(tmp_path, _good_env()), {}).unlocked_presets == frozenset()
        f = _env_file(tmp_path, _good_env(extra=f"{UNLOCKED_PRESETS_ENV}=kids,yolo\n"))
        assert load_dev_env_file(f, {}).unlocked_presets == {"kids", "yolo"}
        assert load_dev_env_file(f, {UNLOCKED_PRESETS_ENV: ""}).unlocked_presets == frozenset()  # "" wins
        assert load_dev_env_file(f, {UNLOCKED_PRESETS_ENV: "brief"}).unlocked_presets == {"brief"}
        cfg = load_config(dict(base, **{UNLOCKED_PRESETS_ENV: "silver"}))
        assert cfg.unlocked_presets == {"silver"} and "silver" in repr(cfg) and VALID_TOKEN not in repr(cfg)
        for bad in ("kids, yolo", " kids", "kids,kids", "unknown", "키즈", "kids,,yolo"):
            with pytest.raises(ConfigError) as e:
                load_dev_env_file(f, {UNLOCKED_PRESETS_ENV: bad})
            assert VALID_TOKEN not in str(e.value) and UNLOCKED_PRESETS_ENV in str(e.value)
        with pytest.raises(ConfigError):
            load_dev_env_file(_env_file(tmp_path, _good_env(extra=f"{UNLOCKED_PRESETS_ENV}='kids, yolo'\n")), {})

    def test_run_check_reports_unlocked_count(self, tmp_path, repo):
        st = tmp_path / "st"
        st.mkdir()
        lines = []
        f = _env_file(tmp_path, _good_env(extra=f"{UNLOCKED_PRESETS_ENV}=kids,yolo\n"))
        rc = run_check(repo=repo, db=st / "c.db", env_file=f, process_env={}, transport_factory=_GetMeTransport(),
                       adapters=_mock_clis(), out=lines.append)
        assert rc == 0, lines
        assert any(ln.startswith("CHECK PASS config") and "unlocked_presets=2" in ln for ln in lines)

    def test_devbot_validates_unlocked_and_demo_unlocks_nothing(self, tmp_path, repo, db, monkeypatch):
        with pytest.raises(ConfigError):
            make_bot(tmp_path, repo, db, boot=False, unlocked_presets={"nope"})
        with pytest.raises(ConfigError):
            make_bot(tmp_path, repo, db, boot=False, unlocked_presets="kids")
        bot, _ = make_bot(tmp_path, repo, db, boot=False, unlocked_presets=["kids", "kids"])
        assert bot.unlocked_presets == frozenset({"kids"})
        seen = []
        real_init = DevBot.__init__

        def spy(self, *a, **k):
            real_init(self, *a, **k)
            seen.append(self.unlocked_presets)

        monkeypatch.setattr(DevBot, "__init__", spy)
        assert dev_bot_mod.run_demo(out=lambda s: None) == 0
        assert seen == [frozenset()]

    def test_supervisor_main_wires_unlocked_presets(self, tmp_path, monkeypatch):
        captured = {}

        class _StopSup:
            def __init__(self, **kw):
                captured.update(kw)

            def open(self):
                raise SupervisorLocked("stop here")

        f = _env_file(tmp_path, _good_env(extra=f"{UNLOCKED_PRESETS_ENV}=kids\n"))
        for k in ("AUTO_FACTORY_DEV_BOT_TOKEN", "AUTO_FACTORY_ADMIN_CHAT_ID", UNLOCKED_PRESETS_ENV):
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setattr(sup_mod, "Supervisor", _StopSup)
        monkeypatch.setattr(sup_mod, "HttpTransport", lambda token: object())
        (tmp_path / "repo").mkdir()
        rc = sup_mod.main(["--run", "--env-file", str(f), "--repo", str(tmp_path / "repo"),
                           "--db", str(tmp_path / "state" / "c.db")])
        assert rc == dev_bot_mod.EXIT_LOCKED
        assert captured["bot_kwargs"] == {"unlocked_presets": frozenset({"kids"})}


class _AliasingPath(type(Path())):
    """Path whose resolve() maps `kids_compass` elsewhere (symlink/case-alias stand-in, no privilege)."""

    target: "Path | None" = None
    rename_case = False

    def resolve(self, strict=False):
        r = super().resolve(strict)
        if "kids_compass" in r.parts:
            if self.rename_case:
                return type(self)(str(r).replace("kids_compass", "Kids_Compass"))
            return type(self)(self.target) / r.name
        return r


class TestDoCommand:
    @pytest.mark.parametrize("text", [
        "/do zzz 고쳐줘", "/do yolo 고쳐줘", "/do Kids 고쳐줘", "/do kids",
        "/do kids    ", "/do", "/do kids --tests=tests/x.py -- r", "/do kids evil.py -- r", "/do kids *.py -- r",
        "/do kids calc.py,x.py -- r", "/do kids ../x.py 고쳐", "/do kids -- r", "/do kids -x 고쳐",
        "/do kids 문구 tests/x.py -- r", "/do kids 문구 --tests=a.py -- r", "/do kids a\x00b",
        "/do kids bell\x07", "/do kids a\\b.py 수정", "/do kids [x] -- r", "/do kids fever.py 고쳐",
        "/do kids-x 고쳐", "/do kids " + "가" * 683,
    ])
    def test_rejects(self, repo, text):
        with pytest.raises(TaskParseError):
            parse_do_command(text, repo, {"kids"})

    def test_accepts_prose_and_byte_limit(self, repo):
        p, req = parse_do_command("/do@AfDevBot kids 해열제 안내 문구 다듬어줘\n둘째 줄", repo, {"kids"})
        assert p is PRESETS["kids"] and req == "해열제 안내 문구 다듬어줘\n둘째 줄"
        assert parse_do_command("/do kids\n해열제 문구 -- 짧게", repo, {"kids"})[1] == "해열제 문구 -- 짧게"
        big = "가" * 682 + "aa"
        assert len(big.encode("utf-8")) == 2048
        assert parse_do_command(f"/do kids {big}", repo, ["kids"])[1] == big
        assert parse_do_command("/do kids fix the fever dosage table", repo, {"kids"})[0].alias == "kids"

    def test_rejects_aliased_preset_paths(self, tmp_path, repo, monkeypatch):
        outside = tmp_path / "outside"
        outside.mkdir()
        monkeypatch.setattr(_AliasingPath, "target", outside)
        monkeypatch.setattr(dev_bot_mod, "Path", _AliasingPath)
        with pytest.raises(TaskParseError, match="escapes"):
            parse_do_command("/do kids 고쳐줘", repo, {"kids"})
        monkeypatch.setattr(_AliasingPath, "rename_case", True)
        with pytest.raises(TaskParseError, match="not canonical"):
            parse_do_command("/do kids 고쳐줘", repo, {"kids"})
        assert parse_do_command("/do yolo 고쳐줘", repo, {"yolo"})[0].alias == "yolo"  # unaffected preset

    def test_do_creates_exact_preset_task(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db, unlocked_presets={"kids"})
        assert bot.handle_update(upd_msg("/do yolo 고쳐줘")) == "cmd:do:rejected"
        assert "locked" in tr.sent[-1]["text"]
        assert bot.handle_update(upd_msg("/do nope 고쳐줘")) == "cmd:do:rejected"
        assert "unknown preset" in tr.sent[-1]["text"]
        assert bot.handle_update(upd_msg("/do kids --tests=tests/x.py -- r")) == "cmd:do:rejected"
        assert db.list_tasks() == []
        r = bot.handle_update(upd_msg("/do kids 해열제 안내 문구 다듬어줘"))
        assert r.startswith("cmd:do:created:")
        t = db.get_task(r.rsplit(":", 1)[1])
        kids = PRESETS["kids"]
        assert t["risk_level"] == "L2" and t["state"] == "RECEIVED"
        assert sorted(t["allowed_paths"]) == sorted(kids.allowed_paths)
        assert t["acceptance_spec"] == {"pytest": list(kids.tests), "preset": "kids",
                                        "preset_policy": PRESET_POLICY_VERSION}
        assert t["request"] == "해열제 안내 문구 다듬어줘" and len(db.list_tasks()) == 1
        assert "키즈" in tr.sent[-1]["text"]

    def test_duplicate_blocked_merged_and_active(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db, unlocked_presets={"kids", "yolo", "silver"})
        tid = bot.handle_update(upd_msg("/do kids 차단될 요청")).rsplit(":", 1)[1]
        _advance(db, tid, ["BLOCKED"])
        assert bot.handle_update(upd_msg("/do kids 차단될 요청")) == f"cmd:do:duplicate:{tid}"
        text = tr.sent[-1]["text"]
        assert "이미 차단(BLOCKED)" in text and "자동 재시도 안 함" in text and tid in text
        assert tr.sent[-1]["reply_markup"] is None

        tid2 = bot.handle_update(upd_msg("/do yolo 진행중 요청")).rsplit(":", 1)[1]
        db.add_artifact(tid2, "audit", tmp_path / "audit.json", "codex")
        _to_state(db, tid2, "AWAITING_START_AUTH")
        before = _tg_action_count(db)
        assert bot.handle_update(upd_msg("/do yolo 진행중 요청")) == f"cmd:do:duplicate:{tid2}"
        assert "duplicate of AWAITING_START_AUTH" in tr.sent[-1]["text"]
        assert set(tr.buttons()) == {"s", "c"} and _tg_action_count(db) == before + 2

        tid3 = bot.handle_update(upd_msg("/do silver 병합된 요청")).rsplit(":", 1)[1]
        db._conn.execute("UPDATE tasks SET state='MERGED' WHERE id=?", (tid3,))  # terminal fixture
        assert bot.handle_update(upd_msg("/do silver 병합된 요청")) == f"cmd:do:duplicate:{tid3}"
        assert "MERGED" in tr.sent[-1]["text"] and "병합 완료" in tr.sent[-1]["text"]
        assert len(db.list_tasks()) == 3

    def test_task_duplicate_guardrail_blocked_message(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        tid = bot.handle_update(upd_msg("/task L1 calc.py -- fix add")).rsplit(":", 1)[1]
        db._conn.execute("UPDATE tasks SET state='GUARDRAIL_BLOCKED' WHERE id=?", (tid,))  # fixture
        assert bot.handle_update(upd_msg("/task L1 calc.py -- fix add")) == f"cmd:task:duplicate:{tid}"
        assert "이미 차단(GUARDRAIL_BLOCKED)" in tr.sent[-1]["text"]


class TestBotsAndPresetCallback:
    @staticmethod
    def _buttons(msg):
        return [b for row in (msg.get("reply_markup") or {}).get("inline_keyboard", []) for b in row]

    def test_bots_lists_unlocked_buttons_and_locked_text(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db, unlocked_presets={"kids", "yolo", "silver"})
        before = _tg_action_count(db)
        assert bot.handle_update(upd_msg("/bots")) == "cmd:bots:3"
        msg = tr.sent[-1]
        text = msg["text"]
        assert SafeHtmlFormatter().finalize(text) == text
        btns = self._buttons(msg)
        assert [b["callback_data"] for b in btns] == [f"af2|p|{encode_preset_token(a)}"
                                                      for a in ("silver", "kids", "yolo")]
        assert all(len(b["callback_data"].encode("ascii")) <= 64 for b in btns)
        assert [len(r) for r in msg["reply_markup"]["inline_keyboard"]] == [2, 1]
        for a in ("brief", "travel", "admission", "portal", "scripts"):
            assert f"\U0001f512 <code>{a}</code>" in text
        assert "✅ 👶 키즈 나침반" in text and _tg_action_count(db) == before

    def test_bots_with_nothing_unlocked_and_help(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        assert bot.handle_update(upd_msg("/bots")) == "cmd:bots:0"
        assert tr.sent[-1]["reply_markup"] is None and UNLOCKED_PRESETS_ENV in tr.sent[-1]["text"]
        help_text = dev_bot_mod.HELP_TEXT
        assert len(help_text) < 800 and "/bots" in help_text and "/do" in help_text
        assert bot.handle_update(upd_msg("/help")) == "cmd:help"

    def test_preset_callback_is_stateless(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db, unlocked_presets={"kids"})
        before = _tg_action_count(db)
        tok = encode_preset_token("kids")
        assert bot.handle_update(upd_cb("p", tok)) == "callback:preset:kids"
        text = tr.sent[-1]["text"]
        assert "/do kids 원하는 작업 내용" in text and "tests/test_kids_compass.py" in text and "파일 5개" in text
        assert bot.handle_update(upd_cb("p", tok)) == "callback:preset:kids"  # reusable: nothing consumed
        assert _tg_action_count(db) == before and db.list_tasks() == [] and len(tr.answers) == 2
        focus = _json.loads(db.get_kv(f"focus:{ADMIN}"))  # only the chat focus is written
        assert focus["alias"] == "kids" and focus["expires_at"] > time.time() and focus["ver"]
        markup = tr.sent[-1]["reply_markup"]
        assert markup["force_reply"] is True and markup["selective"] is False
        assert len(markup["input_field_placeholder"]) <= 64 and "inline_keyboard" not in markup
        assert db.list_qa_requests() == []

    def test_preset_callback_rejects_unauthorized_locked_and_forged(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db, unlocked_presets={"kids"})
        assert bot.handle_update(upd_cb("p", encode_preset_token("kids"), frm=OTHER)) == "ignored:unauthorized"
        assert bot.handle_update(upd_cb("p", encode_preset_token("kids"), chat=OTHER)) == "ignored:unauthorized"
        assert tr.answers == [] and tr.send_attempts == 0
        assert bot.handle_update(upd_cb("p", encode_preset_token("yolo"))) == "callback:preset:unavailable"
        assert "사용할 수 없는" in tr.sent[-1]["text"]
        assert bot.handle_update(upd_cb("p", "-kids" + "-" * 17)) == "callback:preset:unavailable"
        assert bot.handle_update(upd_cb("p", "nope" + "-" * 18)) == "callback:preset:unavailable"
        tr.answer_script = [False]
        n = tr.send_attempts
        assert bot.handle_update(upd_cb("p", encode_preset_token("kids"))) == "callback:answer_failed"
        assert tr.send_attempts == n and _tg_action_count(db) == 0


def _preset_spec(alias, **over):
    p = PRESETS[alias]
    spec = {"pytest": list(p.tests), "preset": alias, "preset_policy": PRESET_POLICY_VERSION}
    spec.update(over)
    return spec


class TestPresetPreflight:
    def test_valid_preset_passes_preflight(self, tmp_path, repo, db):
        w = make_worker(tmp_path, repo, db)
        p = PRESETS["kids"]
        tid, _ = db.create_task("r", "L2", list(p.allowed_paths), _preset_spec("kids"))
        assert w._preflight(db.get_task(tid)) is True
        assert db.get_task(tid)["state"] == "PREFLIGHTED"

    @pytest.mark.parametrize("case", ["paths", "pytest", "risk", "unknown", "policy", "missing_policy"])
    def test_tampered_preset_blocks(self, tmp_path, repo, db, case):
        w = make_worker(tmp_path, repo, db)
        p = PRESETS["kids"]
        paths, risk, spec = list(p.allowed_paths), "L2", _preset_spec("kids")
        if case == "paths":
            paths = paths + ["auto_factory/worker.py"]
        elif case == "pytest":
            spec["pytest"] = ["tests/test_core.py"]
        elif case == "risk":
            risk = "L1"
        elif case == "unknown":
            spec["preset"] = "nope"
        elif case == "policy":
            spec["preset_policy"] = "v0"
        else:
            del spec["preset_policy"]
        tid, _ = db.create_task("r", risk, paths, spec)
        assert w.process_task(tid) == "BLOCKED"
        assert "PREFLIGHT_PRESET" in db.events(tid)[-1]["reason"]
        assert w.adapters["claude"].calls == [] and w.adapters["codex"].calls == []

    def test_non_preset_task_unaffected(self, tmp_path, repo, db):
        w = make_worker(tmp_path, repo, db)
        tid = submit(db, start=False)
        assert w._preflight(db.get_task(tid)) is True


class TestPresetEndToEnd:
    def test_preset_task_merges_with_human_summary(self, tmp_path, repo, db):
        (repo / "scripts").mkdir()
        (repo / "scripts" / "audit_test_quality.py").write_text("def score():\n    return 1\n", encoding="utf-8")
        (repo / "tests" / "test_audit_test_quality.py").write_text(
            "from scripts.audit_test_quality import score\n\n\ndef test_score():\n    assert score() == 2\n",
            encoding="utf-8")
        _git_main(repo, "add", "-A")
        _git_main(repo, "commit", "-m", "add scripts preset files")

        def fix_scripts(wt, contract, role):
            Path(wt, "scripts", "audit_test_quality.py").write_text("def score():\n    return 2\n", encoding="utf-8")

        bot, tr = make_bot(tmp_path, repo, db, unlocked_presets={"scripts"})
        w = make_auto_worker(tmp_path, repo, db, claude=[fix_scripts], gate_runner=_InstantGates())
        r = bot.handle_update(upd_msg("/do scripts score 함수가 2를 반환하도록 고쳐줘"))
        assert r.startswith("cmd:do:created:")
        tid = r.rsplit(":", 1)[1]
        assert w.process_task(tid) == "AWAITING_START_AUTH"
        bot.dispatch_once()
        assert bot.handle_update(upd_cb("s", tr.buttons()["s"])) == "callback:start"
        assert w.process_task(tid) == "AWAITING_MERGE"
        t = db.get_task(tid)
        assert t["coder"] == "claude" and verify_phase1_artifacts(db, t)["auditor"] == "codex"
        bot.dispatch_once()
        assert bot.handle_update(upd_cb("m", tr.buttons()["m"])) == "callback:prompt:m"
        assert bot.handle_update(upd_cb("M", tr.buttons(source="edits")["M"])) == "callback:merged"
        t = db.get_task(tid)
        assert t["state"] == "MERGED" and w.wtm.head_sha() == t["target_sha"]
        assert "return 2" in (repo / "scripts" / "audit_test_quality.py").read_text(encoding="utf-8")
        text = tr.edits[-1]["text"]
        assert "Files: 1 file changed, 1 insertion(+), 1 deletion(-)" in text
        assert "Tests: py_compile PASS · pytest PASS · holdout PASS" in text
        assert len(text) <= 3500 and SafeHtmlFormatter.visible_len(text) <= 1800
        assert bot.handle_update(upd_msg("/do scripts score 함수가 2를 반환하도록 고쳐줘")) == f"cmd:do:duplicate:{tid}"
        assert "병합 완료(MERGED)" in tr.sent[-1]["text"]

    def test_merge_summary_degrades_on_tampered_gate_report(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        tid, _ = db.create_task("r", "L1", ["calc.py"], {})
        bad = tmp_path / "gates.json"
        bad.write_bytes(b'[{"gate": "pytest", "ok": true}]')
        db.add_artifact(tid, "gates", bad, "deterministic")
        assert bot._merge_summary(db.get_task(tid), None) == ["Files: (no changes)", "Tests: pytest PASS"]
        bad.write_bytes(b'[{"gate": "pytest", "ok": false}]')  # tampered after hashing
        assert bot._merge_summary(db.get_task(tid), None) == ["Files: (no changes)",
                                                              "Tests: (gate report unreadable)"]


# ######################################################################## Method 1: conversational Q&A
import io as _io
import stat as _stat
import zipfile as _zipfile

from auto_factory.adapters import QAContract, QAParseError, parse_qa_result, render_prompt
from auto_factory.db import CoordinatorError
from auto_factory.dev_bot import classify_intent, validate_conversational_text
from auto_factory.guardrails import is_qa_secret_path, normalize_rel_segments, redact_secrets
from auto_factory.worker import QA_CIRCUIT, QA_REQUEST_FILENAME
from auto_factory.worktree import UnsafePathError, snapshot_tree

TG_SECRET = "123456789:" + "B" * 35
QA_HASH = "h" * 64


def _qa_db(tmp_path, t=1_000_000.0):
    clk = Clock(t)
    return CoordinatorDB(tmp_path / "qa.db", clock=clk), clk


def _claim(d, owner="w1", ttl=300.0):
    return d.claim_next_qa_request(owner, os.getpid(), 1.0, ttl)


def _focused_bot(tmp_path, repo, db, alias="kids", **kw):
    bot, tr = make_bot(tmp_path, repo, db, unlocked_presets=kw.pop("unlocked", {alias}), **kw)
    assert bot.handle_update(upd_cb("p", encode_preset_token(alias))) == f"callback:preset:{alias}"
    return bot, tr


class TestQASnapshotPolicy:
    def test_is_qa_secret_path_matrix(self):
        for p in (".env", ".ENV", ".env.local", "sub/.env", ".env/secret", "cache.db/token", "x.db", "x.db-wal",
                  "x.db-shm", "x.db-journal", "a.sqlite3", "a.SQLITE", "a.sqlite3-wal", "a.sqlite-journal",
                  "scratch/n.md", "Scratch/n.md", ".git/config", ".auto_factory/h.json", "STOP.flag",
                  "a\\.env", "a\\..\\x.py", "../x.py", "/abs.py", "\\\\srv\\share\\x", "C:/x.py", "C:x.py",
                  "a/./b.py", "", "a\x00b.py", "a/b:stream"):
            assert is_qa_secret_path(p), p
        assert is_qa_secret_path(".env.example", is_dir=True)  # a DIRECTORY named like a template is forbidden
        assert is_qa_secret_path(".env.example/inner.txt")
        for p in (".env.example", ".env.template", "calc.py", "tests/test_calc.py", "docs\\guide.md", "dbx/a.py"):
            assert not is_qa_secret_path(p), p
        assert normalize_rel_segments("a\\b/c.py") == ("a", "b", "c.py")
        for bad in ("", "/a", "\\a", "C:/a", "a/../b", "a//b", "a\x00"):
            with pytest.raises(ValueError):
                normalize_rel_segments(bad)

    def test_export_clean_snapshot_strips_secrets_dbs_and_git(self, tmp_path, repo):
        files = {".env": "TOKEN=x", ".env.example": "TOKEN=", "cache.db/token": "t", "a.db-wal": "w",
                 "a.db-shm": "s", "p.sqlite3": "d", "scratch/notes.md": "n", "sub/.ENV.local": "e",
                 ".env.template/inner.txt": "i", "docs/ok.md": "ok"}
        for rel, body in files.items():
            (repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (repo / rel).write_text(body, encoding="utf-8")
        _git_main(repo, "add", "-A", "-f")
        _git_main(repo, "commit", "-q", "-m", "secrets fixture")
        (repo / "dirty_untracked.py").write_text("leak", encoding="utf-8")  # never committed
        wtm = WorktreeManager(repo, tmp_path / "wts")
        snap = wtm.export_clean_snapshot(wtm.head_sha(), tmp_path / "snap")
        got = sorted(p.relative_to(snap).as_posix() for p in snap.rglob("*") if p.is_file())
        assert got == [".env.example", ".gitignore", "calc.py", "docs/ok.md", "tests/test_calc.py"]
        assert not (snap / ".git").exists() and not (snap / "scratch").exists()
        assert (snap / "calc.py").read_text(encoding="utf-8") == (repo / "calc.py").read_text(encoding="utf-8")
        tree = snapshot_tree(snap)
        assert tree == snapshot_tree(snap) and tree["calc.py"][0] == (snap / "calc.py").stat().st_size
        (snap / "calc.py").write_text("changed", encoding="utf-8")
        assert snapshot_tree(snap) != tree
        with pytest.raises(wt_mod.WorktreeError):
            wtm.export_clean_snapshot(wtm.head_sha(), snap)  # not empty
        with pytest.raises(wt_mod.WorktreeError):
            wtm.export_clean_snapshot("HEAD", tmp_path / "snap2")  # full SHA only
        with pytest.raises(wt_mod.WorktreeError):
            wtm.export_clean_snapshot(wtm.head_sha(), repo / "inside")  # never inside the repo

    @staticmethod
    def _zip(entries):
        buf = _io.BytesIO()
        with _zipfile.ZipFile(buf, "w") as zf:
            for name, mode, system, dos in entries:
                zi = _zipfile.ZipInfo(name)
                zi.create_system = system
                zi.external_attr = (mode << 16) | dos
                zf.writestr(zi, b"x")
        return buf.getvalue()

    @pytest.mark.parametrize("entry", [
        ("link.py", _stat.S_IFLNK | 0o777, 3, 0),
        ("fifo", _stat.S_IFIFO | 0o644, 3, 0),
        ("chr", _stat.S_IFCHR | 0o644, 3, 0),
        ("sock", _stat.S_IFSOCK | 0o644, 3, 0),
        ("junction", 0, 0, 0x400),
        ("ntfs_reparse", 0, 10, 0x400 | 0x20),
        ("../evil.py", _stat.S_IFREG | 0o644, 3, 0),
        ("a\\..\\..\\evil.py", _stat.S_IFREG | 0o644, 3, 0),
        ("C:/evil.py", _stat.S_IFREG | 0o644, 3, 0),
        ("/abs.py", _stat.S_IFREG | 0o644, 3, 0),
    ])
    def test_export_rejects_unsafe_archive_entries(self, tmp_path, repo, monkeypatch, entry):
        data = self._zip([("ok.py", _stat.S_IFREG | 0o644, 3, 0), entry])
        fake = _types.SimpleNamespace(
            DEVNULL=subprocess.DEVNULL, run=lambda *a, **k: _types.SimpleNamespace(stdout=data, returncode=0))
        monkeypatch.setattr(wt_mod, "subprocess", fake)
        wtm = WorktreeManager(repo, tmp_path / "wts")
        with pytest.raises(UnsafePathError):
            wtm.export_clean_snapshot("a" * 40, tmp_path / "snap")
        assert not (tmp_path / "evil.py").exists() and not (tmp_path.parent / "evil.py").exists()

    def test_export_skips_forbidden_archive_dirs(self, tmp_path, repo, monkeypatch):
        data = self._zip([("ok.py", _stat.S_IFREG | 0o644, 3, 0), (".env.example/", _stat.S_IFDIR | 0o755, 3, 0x10),
                          (".env.example/in.txt", _stat.S_IFREG | 0o644, 3, 0),
                          (".env.example", 0, 0, 0x20)])
        fake = _types.SimpleNamespace(
            DEVNULL=subprocess.DEVNULL, run=lambda *a, **k: _types.SimpleNamespace(stdout=data, returncode=0))
        monkeypatch.setattr(wt_mod, "subprocess", fake)
        snap = WorktreeManager(repo, tmp_path / "wts").export_clean_snapshot("a" * 40, tmp_path / "snap")
        assert sorted(p.name for p in snap.iterdir()) == [".env.example", "ok.py"]
        assert (snap / ".env.example").is_file()

    def test_snapshot_tree_rejects_links_and_reparse_points(self, tmp_path, monkeypatch):
        root = tmp_path / "t"
        (root / "d").mkdir(parents=True)
        (root / "d" / "f.txt").write_text("x", encoding="utf-8")
        assert snapshot_tree(root) == {"d": (-1, ""), "d/f.txt": (1, _hashlib.sha256(b"x").hexdigest())}
        real = wt_mod._is_link_or_reparse
        for target in (root, root / "d", root / "d" / "f.txt"):  # root, dir and file are all checked
            monkeypatch.setattr(wt_mod, "_is_link_or_reparse", lambda p, _t=target: Path(p) == _t or real(p))
            with pytest.raises(UnsafePathError):
                snapshot_tree(root)


class TestRedaction:
    def test_redact_secrets_patterns(self):
        raw = (f"tg {TG_SECRET} key sk-ant-{'a' * 24} openai sk-{'b' * 24} aws AKIA{'C' * 16} "
               f"auth: Bearer {'d' * 24} API_KEY = 'hunter2secret' DATABASE_URL=postgres://u:p@h/db "
               "-----BEGIN RSA PRIVATE KEY-----\nMIIabc\n-----END RSA PRIVATE KEY----- safe text")
        out = redact_secrets(raw)
        for leaked in (TG_SECRET, "sk-ant-", "b" * 24, "AKIA", "d" * 24, "hunter2secret", "postgres://", "MIIabc"):
            assert leaked not in out, leaked
        assert "API_KEY = '[REDACTED]'" in out and "DATABASE_URL=[REDACTED]" in out and "safe text" in out
        assert redact_secrets("db_password: 'abcdefg'") == "db_password: '[REDACTED]'"
        assert redact_secrets("MY_TOKEN=short") == "MY_TOKEN=short"  # < 6 chars is not treated as a secret
        assert redact_secrets(None) == "" and redact_secrets("plain 안녕") == "plain 안녕"


class TestQAAdapters:
    def test_qa_prompt_is_ascii_and_shim_safe(self):
        p = render_prompt("qa", QA_REQUEST_FILENAME)
        assert p.isascii() and adapters_mod._SAFE_SHIM_ARG_RE.match(p) and QA_REQUEST_FILENAME in p
        assert "schema_version" in p and "refs" in p

    def test_invoke_qa_enforces_ascii_and_metachar_free_prompt(self, monkeypatch):
        runner = _RecordingRunner()
        a = adapters_mod.CodexAdapter(runner, executable=sys.executable)
        res = a.invoke(QAContract("q1", "x"), ".", "qa", QA_REQUEST_FILENAME)
        assert res.ok and runner.cmds and "read-only" in runner.cmds[-1]
        for bad in ("질문 prompt", "a & b", "a | b"):
            monkeypatch.setattr(adapters_mod, "render_prompt", lambda role, path, _b=bad: _b)
            n = len(runner.cmds)
            res = a.invoke(QAContract("q1", "x"), ".", "qa", QA_REQUEST_FILENAME)
            assert res.code == FailureCode.INTERNAL and len(runner.cmds) == n

    def test_gemini_qa_denies_all_commands_and_writer_unchanged(self):
        g = adapters_mod.GeminiAdapter(model=None)
        qa = g.build_command("qa", "p")
        settings = _json.loads(qa[qa.index("--settings") + 1])
        assert settings == {"permissions": {"allow": [], "deny": ["command(regex:.*)"]}}
        assert qa[qa.index("--mode") + 1] == "plan"
        w = g.build_command("writer", "p")
        assert w[w.index("--mode") + 1] == "accept-edits"
        assert _json.loads(w[w.index("--settings") + 1])["permissions"]["allow"] == ["command(regex:.*)"]
        r = g.build_command("reviewer", "p")
        assert _json.loads(r[r.index("--settings") + 1])["permissions"]["allow"] == ["command(regex:.*)"]

    def test_parse_qa_result_strict(self):
        good = {"schema_version": 1, "qa_id": "q1", "answer": " 답 ", "refs": ["a/b.py", "c\\d.py"]}
        assert parse_qa_result("noise " + _json.dumps(good, ensure_ascii=False), "q1") == {
            "schema_version": 1, "qa_id": "q1", "answer": "답", "refs": ["a/b.py", "c/d.py"]}
        bads = [dict(good, qa_id="q2"), dict(good, schema_version=True), dict(good, schema_version="1"),
                dict(good, answer=" "), dict(good, refs=["a"] * 6), dict(good, refs=["../x.py"]),
                dict(good, refs=["/etc/passwd"]), dict(good, refs=["C:/x"]), dict(good, refs=[".env"]),
                dict(good, refs=["db/app.sqlite3"]), dict(good, refs=[1]), dict(good, extra=1)]
        for bad in bads:
            with pytest.raises(QAParseError):
                parse_qa_result(_json.dumps(bad), "q1")
        with pytest.raises(QAParseError):
            parse_qa_result(_json.dumps(good) + _json.dumps(good), "q1")
        with pytest.raises(QAParseError):
            parse_qa_result("no json", "q1")

    def test_mock_adapter_qa_calls(self):
        m = MockAdapter("codex")
        res = m.invoke(QAContract("q9", "x"), ".", "qa", QA_REQUEST_FILENAME)
        assert m.calls == [("qa", "q9")] and parse_qa_result(res.stdout, "q9")["answer"] == "mock qa answer"


class TestQADatabase:
    def test_create_dedupe_claim_and_complete(self, tmp_path):
        d, clk = _qa_db(tmp_path)
        try:
            qid, created = d.create_qa_request(ADMIN, "kids", "질문?", QA_HASH)
            assert created and qid.startswith("q") and d.create_qa_request(ADMIN, "kids", "질문?", QA_HASH) == (qid, False)
            row = _claim(d)
            assert row["id"] == qid and row["status"] == "RUNNING" and row["attempt_count"] == 1
            assert d.create_qa_request(ADMIN, "kids", "질문?", QA_HASH) == (qid, False)  # RUNNING still dedupes
            assert _claim(d, "w2") is None
            assert d.heartbeat_qa_request(qid, "w1", 1) and not d.heartbeat_qa_request(qid, "w2", 1)
            assert not d.complete_qa_answer(qid, "w2", 1, "codex", "a" * 40, "x", [])  # stale worker
            assert not d.complete_qa_answer(qid, "w1", 2, "codex", "a" * 40, "x", [])  # stale attempt
            assert d.complete_qa_answer(qid, "w1", 1, "codex", "a" * 40, f"답 {TG_SECRET}", [f"x {TG_SECRET}"])
            r = d.get_qa_request(qid)
            assert r["status"] == "ANSWERED" and r["lease_owner"] is None and TG_SECRET not in r["answer"]
            assert r["refs"] == ["x [REDACTED]"]
            out = d.qa_outbox(qid)
            assert len(out) == 1 and out[0]["idempotency_key"] == f"qa:answer:{qid}" and TG_SECRET not in out[0]["payload_json"]
            assert not d.complete_qa_answer(qid, "w1", 1, "codex", "a" * 40, "again", [])
            assert len(d.qa_outbox(qid)) == 1 and d.qa_outbox_counts()["PENDING"] == 1
            qid2, created2 = d.create_qa_request(ADMIN, "kids", "질문?", QA_HASH)  # answered -> new request
            assert created2 and qid2 != qid
            with pytest.raises(KeyError):
                d.get_qa_request("qnope")
        finally:
            d.close()

    def test_dual_expiration_fencing(self, tmp_path):
        d, clk = _qa_db(tmp_path)
        try:
            qid, _ = d.create_qa_request(ADMIN, None, "q", QA_HASH, ttl=100)
            _claim(d, ttl=300)
            clk.t += 150  # request TTL passed, lease still live
            assert d.complete_qa_answer(qid, "w1", 1, "codex", "a" * 40, "late", []) is False
            r = d.get_qa_request(qid)
            assert r["status"] == "EXPIRED" and r["lease_owner"] is None and d.qa_outbox() == []
            qid2, _ = d.create_qa_request(ADMIN, None, "q2", "g" * 64, ttl=1000)
            _claim(d, ttl=10)
            clk.t += 20  # lease lapsed, request still valid
            assert d.complete_qa_answer(qid2, "w1", 1, "codex", "a" * 40, "x", []) is False
            assert d.fail_qa_request(qid2, "w1", 1, "e", notify_text="n") is False
            assert d.get_qa_request(qid2)["status"] == "RUNNING" and d.qa_outbox() == []
            row = _claim(d, "w2")  # lapsed lease is reaped and re-claimed
            assert row["id"] == qid2 and row["attempt_count"] == 2 and row["lease_owner"] == "w2"
            assert d.fail_qa_request(qid2, "w1", 1, "stale", retryable=True) is False
            clk.t += 2000  # both expired
            assert d.fail_qa_request(qid2, "w2", 2, "x", notify_text="n") is False
            assert d.get_qa_request(qid2)["status"] == "RUNNING"
            assert _claim(d, "w3") is None and d.get_qa_request(qid2)["status"] == "EXPIRED"
        finally:
            d.close()

    def test_fail_retryable_then_terminal_with_redacted_notice(self, tmp_path):
        d, clk = _qa_db(tmp_path)
        try:
            qid, _ = d.create_qa_request(ADMIN, "kids", "q", QA_HASH, max_attempts=2)
            _claim(d)
            assert d.fail_qa_request(qid, "w1", 1, f"boom {TG_SECRET}", notify_text="n", retryable=True)
            r = d.get_qa_request(qid)
            assert r["status"] == "PENDING" and r["lease_owner"] is None and r["pid"] is None
            assert r["lease_expires_at"] is None and TG_SECRET not in r["error"] and d.qa_outbox() == []
            _claim(d)
            assert d.fail_qa_request(qid, "w1", 2, "boom", notify_text=f"실패 {TG_SECRET}", retryable=True)
            r = d.get_qa_request(qid)
            assert r["status"] == "FAILED" and r["lease_owner"] is None
            out = d.qa_outbox(qid)
            assert [o["idempotency_key"] for o in out] == [f"qa:fail:{qid}"] and TG_SECRET not in out[0]["payload_json"]
            assert _claim(d) is None
            qid2, _ = d.create_qa_request(ADMIN, None, "q2", "f" * 64)
            _claim(d)
            assert d.fail_qa_request(qid2, "w1", 1, "fatal", retryable=False)  # no notify_text: no outbox
            assert d.get_qa_request(qid2)["status"] == "FAILED" and d.qa_outbox(qid2) == []
        finally:
            d.close()

    def test_claim_expires_due_pending_and_lists(self, tmp_path):
        d, clk = _qa_db(tmp_path)
        try:
            old, _ = d.create_qa_request(ADMIN, None, "old", "a" * 64, ttl=10)
            new, _ = d.create_qa_request(ADMIN, None, "new", "b" * 64, ttl=1000)
            clk.t += 20
            assert d.has_due_qa()
            assert _claim(d)["id"] == new and d.get_qa_request(old)["status"] == "EXPIRED"
            assert [r["id"] for r in d.list_qa_requests(["EXPIRED"])] == [old]
            assert len(d.list_qa_requests()) == 2 and not d.has_due_qa()
        finally:
            d.close()

    def test_boot_recovery_outcomes(self, tmp_path):
        d, clk = _qa_db(tmp_path)
        try:
            ids = []
            for i, (q, maxa) in enumerate((("live", 3), ("dead", 3), ("exhausted", 1), ("expired", 3))):
                clk.t += 1  # distinct created_at -> deterministic FIFO claim order
                ids.append(d.create_qa_request(ADMIN, None, q, f"{i}" * 64, ttl=1000, max_attempts=maxa)[0])
            a, b, c, e = ids
            for pid in (111, 222, 333, 444):
                assert d.claim_next_qa_request(f"owner-{pid}", pid, 1.0)["pid"] == pid
            d._conn.execute("UPDATE qa_requests SET expires_at=? WHERE id=?", (clk.t - 1, e))
            res = d.recover_stale_qa_requests("me", lambda pid, st: pid == 111)
            assert res == {"expired": [e], "failed": [c], "requeued": [b], "skipped": [a]}
            assert d.get_qa_request(a)["status"] == "RUNNING"
            assert d.get_qa_request(b)["status"] == "PENDING" and d.get_qa_request(b)["lease_owner"] is None
            assert d.get_qa_request(c)["status"] == "FAILED" and d.qa_outbox(c)[0]["kind"] == "fail"
            assert d.get_qa_request(e)["status"] == "EXPIRED" and d.qa_outbox(e) == []
            res2 = d.recover_stale_qa_requests("owner-111", lambda pid, st: True)  # own nonce: never "live"
            assert res2["requeued"] == [a]
        finally:
            d.close()

    def test_outbox_helpers_are_table_allowlisted(self, tmp_path):
        d, clk = _qa_db(tmp_path)
        try:
            with pytest.raises(ValueError):
                d._claim_outbox_table("tasks; DROP TABLE tasks", "o", "b", 1, 1.0)
            with pytest.raises(ValueError):
                d._outbox_counts_table("events")
            assert d.outbox_counts() == d.qa_outbox_counts() == {s: 0 for s in db_mod.OUTBOX_STATUSES}
        finally:
            d.close()

    def test_kv_focus_helpers(self, tmp_path):
        d, clk = _qa_db(tmp_path)
        try:
            d.set_kv(f"draft:{ADMIN}", "{}")
            d.set_focus_and_clear_draft(ADMIN, '{"alias": "kids"}')
            assert d.get_kv(f"draft:{ADMIN}") is None and d.get_kv(f"focus:{ADMIN}") == '{"alias": "kids"}'
            assert not d.refresh_focus_cas(ADMIN, "wrong", "{}") and d.refresh_focus_cas(ADMIN, '{"alias": "kids"}', "{}")
            assert not d.cas_delete_kv(f"focus:{ADMIN}", "nope") and d.get_kv(f"focus:{ADMIN}") == "{}"
            d.set_kv(f"draft:{ADMIN}", "x")
            assert d.clear_chat_focus(ADMIN) and not d.clear_chat_focus(ADMIN)
            assert d.get_kv(f"focus:{ADMIN}") is None and d.get_kv(f"draft:{ADMIN}") is None
            d.set_kv("k", "v")
            assert d.cas_delete_kv("k", "v") and not d.delete_kv("k")
        finally:
            d.close()


class TestQAWorker:
    def _qa(self, db, question="calc 는 무엇을 하나요?", alias=None):
        qid, _ = db.create_qa_request(ADMIN, alias, question, _hashlib.sha256(question.encode()).hexdigest())
        return qid

    def test_answer_from_clean_snapshot_and_cleanup(self, tmp_path, repo, db):
        seen = {}

        def look(wt, contract, role):
            root = Path(wt)
            seen["git"] = (root / ".git").exists()
            seen["files"] = sorted(p.name for p in root.iterdir())
            seen["request"] = _json.loads((root / QA_REQUEST_FILENAME).read_text(encoding="utf-8"))
            return {"schema_version": 1, "qa_id": contract.qa_id, "answer": "calc.add 는 덧셈입니다", "refs": ["calc.py"]}

        w = make_worker(tmp_path, repo, db, codex=[look], audit_tmp_dir=tmp_path / "clones")
        qid = self._qa(db, alias="kids")
        s = w.run_cycle()
        assert s["qa_processed"] == [(qid, "ANSWERED")] and s["processed"] == []
        r = db.get_qa_request(qid)
        assert r["status"] == "ANSWERED" and r["provider"] == "codex" and r["base_sha"] == w.wtm.head_sha()
        assert r["refs"] == ["calc.py"] and r["attempt_count"] == 1
        assert seen["git"] is False and QA_REQUEST_FILENAME in seen["files"] and "calc.py" in seen["files"]
        assert seen["request"]["qa_id"] == qid and seen["request"]["preset_alias"] == "kids"
        assert seen["request"]["allowed_paths"] == list(PRESETS["kids"].allowed_paths)
        assert w.adapters["codex"].calls == [("qa", qid)] and w.adapters["claude"].calls == []
        assert db.qa_outbox(qid)[0]["status"] == "PENDING"
        assert list((tmp_path / "clones").iterdir()) == [] and w.qa_cleanup_errors == []
        assert w.wtm.main_is_clean()

    def test_merge_lock_contention_defers_without_consuming_attempt(self, tmp_path, repo, db, no_leaked_locks):
        w = make_worker(tmp_path, repo, db, audit_tmp_dir=tmp_path / "clones")
        qid = self._qa(db)
        lock = _FileLock(w.wtm.merge_lock_path, role="merge")
        lock.acquire()
        try:
            s = w.run_cycle()
        finally:
            lock.release()
        assert s["qa_processed"] == []
        r = db.get_qa_request(qid)
        assert r["status"] == "PENDING" and r["attempt_count"] == 0 and w.adapters["codex"].calls == []
        assert w.run_cycle()["qa_processed"] == [(qid, "ANSWERED")]

    def test_tamper_detected_even_when_provider_raises(self, tmp_path, repo, db):
        def vandal(wt, contract, role):
            Path(wt, "calc.py").write_text("pwned", encoding="utf-8")
            raise RuntimeError(f"crash {TG_SECRET}")

        w = make_worker(tmp_path, repo, db, codex=[vandal], audit_tmp_dir=tmp_path / "clones")
        qid = self._qa(db)
        s = w.run_cycle()
        assert s["qa_processed"] and s["qa_processed"][0][0] == qid
        r = db.get_qa_request(qid)
        assert r["status"] == "FAILED" and r["error"].startswith("TAMPER_DETECTED") and r["answer"] is None
        out = db.qa_outbox(qid)
        assert [o["kind"] for o in out] == ["fail"] and "TAMPER_DETECTED" in out[0]["payload_json"]
        assert w.adapters["claude"].calls == []  # no failover after tampering
        assert list((tmp_path / "clones").iterdir()) == []

    def test_tamper_with_valid_answer_discards_stdout(self, tmp_path, repo, db):
        def sneaky(wt, contract, role):
            Path(wt, "new.py").write_text("x", encoding="utf-8")
            return {"schema_version": 1, "qa_id": contract.qa_id, "answer": "looks fine", "refs": []}

        def main_writer(wt, contract, role):
            (repo / "planted.txt").write_text("x", encoding="utf-8")
            return None

        w = make_worker(tmp_path, repo, db, codex=[sneaky, main_writer], audit_tmp_dir=tmp_path / "clones")
        q1 = self._qa(db, "첫 질문?")
        assert w.run_cycle()["qa_processed"] == [(q1, "TAMPER_DETECTED")]
        assert db.get_qa_request(q1)["answer"] is None and db.get_qa_request(q1)["status"] == "FAILED"
        q2 = self._qa(db, "두번째 질문?")
        assert w.run_cycle()["qa_processed"] == [(q2, "TAMPER_DETECTED")]  # main repo mutation too

    def test_failover_on_format_error_and_unavailable(self, tmp_path, repo, db):
        w = make_worker(tmp_path, repo, db, codex=["RATE_LIMIT"], claude=[lambda wt, c, r: "not json"],
                        audit_tmp_dir=tmp_path / "clones")
        qid = self._qa(db)
        assert w.run_cycle()["qa_processed"] == [(qid, "ANSWERED")]
        assert db.get_qa_request(qid)["provider"] == "gemini"
        assert db.provider_status("codex")["health"] == "rate_limit"

    def test_exhausted_providers_retry_then_fail_and_open_qa_circuit(self, tmp_path, repo, db):
        fails = ["INTERNAL"] * 3
        w = make_worker(tmp_path, repo, db, codex=fails, claude=list(fails), gemini=list(fails),
                        audit_tmp_dir=tmp_path / "clones")
        qid = self._qa(db)
        s = w.run_cycle()
        assert [o for _, o in s["qa_processed"]] == ["RETRY", "RETRY", "FAILED"]
        r = db.get_qa_request(qid)
        assert r["status"] == "FAILED" and r["attempt_count"] == 3 and r["error"].startswith("QA_UNAVAILABLE")
        assert [o["kind"] for o in db.qa_outbox(qid)] == ["fail"]
        assert db.circuit_is_open(QA_CIRCUIT) and not db.circuit_is_open(CIRCUIT)
        q2 = self._qa(db, "다음 질문?")
        assert w.run_cycle()["qa_processed"] == [] and db.get_qa_request(q2)["status"] == "PENDING"

    def test_coding_circuit_open_does_not_stall_qa(self, tmp_path, repo, db):
        w = make_worker(tmp_path, repo, db, circuit_cooldown=300, audit_tmp_dir=tmp_path / "clones")
        for _ in range(3):
            db.circuit_record(CIRCUIT, False)
        assert db.circuit_is_open(CIRCUIT)
        qid = self._qa(db)
        s = w.run_cycle()
        assert s["circuit_open"] is True and s["qa_processed"] == [(qid, "ANSWERED")] and w.sleeps == []
        s = w.run_cycle()
        assert s["qa_processed"] == [] and w.sleeps == [300]
        q2 = self._qa(db, "또 질문?")
        cycles = w.run_forever(max_cycles=1, idle_sleep=77)
        assert cycles == 1 and 77 not in w.sleeps and db.get_qa_request(q2)["status"] == "ANSWERED"

    def test_stop_requested_releases_claimed_row(self, tmp_path, repo, db):
        def stopper(wt, contract, role):
            (tmp_path / "STOP.flag").write_text("stop", encoding="utf-8")
            return None

        w = make_worker(tmp_path, repo, db, codex=[stopper], audit_tmp_dir=tmp_path / "clones")
        qid = self._qa(db)
        assert w.run_cycle()["stopped"] is True
        r = db.get_qa_request(qid)
        assert r["status"] == "PENDING" and r["lease_owner"] is None and r["attempt_count"] == 1
        assert list((tmp_path / "clones").iterdir()) == []

    def test_worker_boot_recovers_stale_qa(self, tmp_path, repo, db):
        qid = self._qa(db)
        db.claim_next_qa_request("crashed", 999999, 1.0)
        w = make_worker(tmp_path, repo, db, audit_tmp_dir=tmp_path / "clones")
        assert w.recover_on_boot() == []
        assert w.last_qa_recovered["requeued"] == [qid] and db.get_qa_request(qid)["status"] == "PENDING"


class TestSupervisorSharedDb:
    def test_memory_db_rejected(self, tmp_path, repo, no_leaked_locks):
        sup, _ = make_sup(tmp_path, repo, db_path=":memory:", lock_path=tmp_path / "state" / "supervisor.lock",
                          state_dir=tmp_path / "state")
        with pytest.raises(CoordinatorError, match="share one persistent"):
            sup.open()
        assert sup.bot is None and not sup.lock.fh

    def test_mismatched_paths_rejected(self, tmp_path, repo, monkeypatch, no_leaked_locks):
        real = sup_mod.CoordinatorDB
        made = []

        def fake(path, **kw):
            made.append(path)
            return real(path if len(made) == 1 else Path(str(path) + ".other"), **kw)

        monkeypatch.setattr(sup_mod, "CoordinatorDB", fake)
        sup, _ = make_sup(tmp_path, repo)
        with pytest.raises(CoordinatorError):
            sup.open()
        assert len(made) == 2 and sup.bot is None


class TestConversationalParsing:
    @pytest.mark.parametrize("text,intent", [
        ("해열제 안내는 어디에 있어?", "ask"), ("이 함수는 왜 필요한가요", "ask"), ("what does calc do", "ask"),
        ("해열제 안내 문구 다듬어줘", "edit"), ("버튼 색을 파란색으로 바꿔줘", "edit"), ("please fix the typo", "edit"),
        ("해열제 문구", "ambiguous"), ("어떻게 고쳐야 할까?", "ambiguous"),
    ])
    def test_classify_intent(self, text, intent):
        assert classify_intent(text) == intent

    def test_validate_conversational_text(self):
        assert validate_conversational_text("  질문 (괄호), a/b 가능?\r\n둘째 줄 ") == "질문 (괄호), a/b 가능?\n둘째 줄"
        for bad in ("", "   ", "a\x00b", "bell\x07", "x --tests=tests/a.py", "고쳐 -- 줘", "--force 고쳐",
                    "do it --dry-run", "가" * 683):
            with pytest.raises(ValueError):
                validate_conversational_text(bad)

    def test_esc_html_budget(self):
        import html as _html

        f = SafeHtmlFormatter
        s = f.esc_html_budget("<<<<" * 2000 + "\x01\x7f", 1000, 1500)
        vis = _html.unescape(s)
        assert len(vis) <= 1000 and len(s) <= 1500 and vis.endswith("…")
        assert s == "&lt;" * 374 + "…"  # HTML budget binds first: 374*4 + 1 = 1497 <= 1500, never a cut entity
        assert set(vis[:-1]) == {"<"} and "\x01" not in vis and "\x7f" not in vis
        tight = f.esc_html_budget("<" * 50, 1000, 10)
        assert tight == "&lt;&lt;…" and f.esc_html_budget("<" * 50, 3, 1000) == "&lt;&lt;…"
        assert f.esc_html_budget("a\r\nb\rc\x00\td", 100, 100) == "a\nb\nc\td"
        assert f.esc_html_budget("hello", 3, 100) == "he…" and f.esc_html_budget("hello", 0, 0) == ""
        assert f.esc_html_budget("&&&&", 100, 9) == "&amp;…" and f.esc_html_budget("ok", 2, 2) == "ok"


class TestConversationalBot:
    def test_plain_text_without_focus_prompts_bot_pick(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db, unlocked_presets={"kids", "yolo"})
        assert bot.handle_update(upd_msg("해열제 안내는 어디에 있어?")) == "text:no_focus"
        btns = [b["callback_data"] for row in tr.sent[-1]["reply_markup"]["inline_keyboard"] for b in row]
        assert btns == [f"af2|p|{encode_preset_token(a)}" for a in ("kids", "yolo")]
        assert db.list_qa_requests() == [] and db.list_tasks() == []
        bot2, tr2 = make_bot(tmp_path, repo, db)
        assert bot2.handle_update(upd_msg("질문?")) == "text:no_focus" and tr2.sent[-1]["reply_markup"] is None

    def test_focused_ask_enqueues_qa_and_dedupes(self, tmp_path, repo, db):
        woke = []
        bot, tr = _focused_bot(tmp_path, repo, db, on_wake=lambda: woke.append(1))
        r = bot.handle_update(upd_msg("해열제 안내는 어디에 있어?"))
        assert r.startswith("text:ask:queued:q") and woke == [1]
        qid = r.rsplit(":", 1)[1]
        row = db.get_qa_request(qid)
        assert row["preset_alias"] == "kids" and row["question"] == "해열제 안내는 어디에 있어?" and row["chat_id"] == ADMIN
        assert tr.sent[-1]["reply_to_message_id"] == _UPD[0]
        assert bot.handle_update(upd_msg("해열제 안내는 어디에 있어?")) == f"text:ask:duplicate:{qid}"
        assert "이미 처리 중" in tr.sent[-1]["text"] and woke == [1] and db.list_tasks() == []
        focus = _json.loads(db.get_kv(f"focus:{ADMIN}"))
        assert focus["expires_at"] > time.time() + 800  # refreshed on use

    def test_focused_edit_creates_exact_preset_task(self, tmp_path, repo, db):
        bot, tr = _focused_bot(tmp_path, repo, db)
        r = bot.handle_update(upd_msg("해열제 안내 문구 다듬어줘"))
        assert r.startswith("text:edit:created:")
        t = db.get_task(r.rsplit(":", 1)[1])
        kids = PRESETS["kids"]
        assert t["risk_level"] == "L2" and sorted(t["allowed_paths"]) == sorted(kids.allowed_paths)
        assert t["acceptance_spec"] == {"pytest": list(kids.tests), "preset": "kids", "preset_policy": PRESET_POLICY_VERSION}
        assert bot.handle_update(upd_msg("/do kids 해열제 안내 문구 다듬어줘")) == f"cmd:do:duplicate:{t['id']}"
        assert bot.handle_update(upd_msg("해열제 안내 문구 다듬어줘")) == f"text:edit:duplicate:{t['id']}"
        assert bot.handle_update(upd_msg("fever.py 고쳐줘")) == "text:edit:rejected"
        assert len(db.list_tasks()) == 1 and db.list_qa_requests() == []

    def test_ambiguous_draft_buttons_and_single_use_ask(self, tmp_path, repo, db):
        bot, tr = _focused_bot(tmp_path, repo, db)
        assert bot.handle_update(upd_msg("해열제 문구")) == "text:ambiguous"
        msg = tr.sent[-1]
        assert msg["reply_to_message_id"] == _UPD[0]
        btns = tr.buttons()
        assert set(btns) == {"q", "e", "z"} and len(set(btns.values())) == 1
        for row in msg["reply_markup"]["inline_keyboard"]:
            assert all(len(b["callback_data"].encode("ascii")) <= 64 for b in row)
        assert db.get_kv(f"draft:{ADMIN}") is not None
        r = bot.handle_update(upd_cb("q", btns["q"]))
        assert r.startswith("callback:draft:ask:q") and db.get_kv(f"draft:{ADMIN}") is None
        assert db.get_qa_request(r.rsplit(":", 1)[1])["question"] == "해열제 문구"
        n_answers = len(tr.answers)
        assert bot.handle_update(upd_cb("q", btns["q"])) == "callback:rejected:DRAFT_MISSING"  # double click
        assert bot.handle_update(upd_cb("e", btns["e"])) == "callback:rejected:DRAFT_MISSING"
        assert len(tr.answers) == n_answers + 2 and len(db.list_qa_requests()) == 1 and db.list_tasks() == []

    def test_ambiguous_edit_and_cancel(self, tmp_path, repo, db):
        bot, tr = _focused_bot(tmp_path, repo, db)
        bot.handle_update(upd_msg("해열제 문구"))
        r = bot.handle_update(upd_cb("e", tr.buttons()["e"]))
        assert r.startswith("callback:draft:edit:")
        t = db.get_task(r.rsplit(":", 1)[1])
        assert t["request"] == "해열제 문구" and t["acceptance_spec"]["preset"] == "kids"
        bot.handle_update(upd_msg("다른 문구"))
        btns = tr.buttons()
        bot.handle_update(upd_msg("세번째 문구"))  # replaces the draft: older buttons are stale
        fresh = tr.buttons()
        assert fresh["z"] != btns["z"]
        assert bot.handle_update(upd_cb("z", btns["z"])) == "callback:rejected:TOKEN_MISMATCH"
        assert db.get_kv(f"draft:{ADMIN}") is not None  # rejected press mutates nothing
        assert bot.handle_update(upd_cb("z", fresh["z"])) == "callback:draft:cancel"
        assert db.get_kv(f"draft:{ADMIN}") is None and len(db.list_tasks()) == 1 and db.list_qa_requests() == []

    def test_focus_switch_rejects_old_draft_with_zero_mutation(self, tmp_path, repo, db):
        bot, tr = _focused_bot(tmp_path, repo, db, unlocked={"kids", "yolo"})
        bot.handle_update(upd_msg("해열제 문구"))
        btns = tr.buttons()
        raw = db.get_kv(f"draft:{ADMIN}")
        focus = _json.loads(db.get_kv(f"focus:{ADMIN}"))
        db.set_kv(f"focus:{ADMIN}", _json.dumps(dict(focus, ver="changed")))
        assert bot.handle_update(upd_cb("q", btns["q"])) == "callback:rejected:FOCUS_CHANGED"
        assert bot.handle_update(upd_cb("e", btns["e"])) == "callback:rejected:FOCUS_CHANGED"
        assert db.get_kv(f"draft:{ADMIN}") == raw and db.list_qa_requests() == [] and db.list_tasks() == []
        assert bot.handle_update(upd_cb("p", encode_preset_token("yolo"))) == "callback:preset:yolo"
        assert db.get_kv(f"draft:{ADMIN}") is None  # a new pick clears the draft atomically
        assert bot.handle_update(upd_cb("q", btns["q"])) == "callback:rejected:DRAFT_MISSING"

    def test_db_consume_rejects_expired_and_locked(self, tmp_path):
        d, clk = _qa_db(tmp_path)
        try:
            d.set_kv(f"focus:{ADMIN}", _json.dumps({"alias": "kids", "ver": "v", "expires_at": clk.t + 100}))
            raw = _json.dumps({"token": "T" * 22, "alias": "kids", "focus_ver": "v", "text": "x", "expires_at": clk.t + 50})
            d.set_kv(f"draft:{ADMIN}", raw)
            with pytest.raises(ActionRejected, match="PRESET_LOCKED"):
                d.consume_draft_for_qa(ADMIN, raw, "T" * 22, {"yolo"}, "x", QA_HASH)
            with pytest.raises(ActionRejected, match="TOKEN_MISMATCH"):
                d.consume_draft_for_qa(ADMIN, raw, "U" * 22, {"kids"}, "x", QA_HASH)
            with pytest.raises(ActionRejected, match="DRAFT_STALE"):
                d.consume_draft_for_task(ADMIN, raw + " ", "T" * 22, {"kids"}, lambda a, t: ("x", "L2", [], {}))
            clk.t += 60
            with pytest.raises(ActionRejected, match="DRAFT_EXPIRED"):
                d.consume_draft_for_qa(ADMIN, raw, "T" * 22, {"kids"}, "x", QA_HASH)
            assert d.get_kv(f"draft:{ADMIN}") == raw and d.list_qa_requests() == []
            assert d.consume_draft_cancel(ADMIN, raw, "T" * 22) and d.get_kv(f"draft:{ADMIN}") is None
            with pytest.raises(ActionRejected, match="DRAFT_MISSING"):
                d.consume_draft_cancel(ADMIN, raw, "T" * 22)
        finally:
            d.close()

    def test_leading_space_command_and_invalid_text(self, tmp_path, repo, db):
        bot, tr = _focused_bot(tmp_path, repo, db)
        n = tr.send_attempts
        assert bot.handle_update(upd_msg(" /do kids 해열제 고쳐줘")) == "ignored:leading_space_command"
        assert bot.handle_update(upd_msg("\t/ask 질문")) == "ignored:leading_space_command"
        assert tr.send_attempts == n and db.list_tasks() == [] and db.list_qa_requests() == []
        assert bot.handle_update(upd_msg("고쳐줘 --tests=tests/x.py")) == "text:invalid"
        assert bot.handle_update(upd_msg("고쳐줘 -- 지금")) == "text:invalid"
        assert db.list_tasks() == [] and db.list_qa_requests() == []

    def test_ask_and_clear_commands(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db, unlocked_presets={"kids"})
        r = bot.handle_update(upd_msg("/ask 저장소 구조가 어떻게 되나요?"))
        assert r.startswith("cmd:ask:queued:") and db.get_qa_request(r.rsplit(":", 1)[1])["preset_alias"] is None
        r = bot.handle_update(upd_msg("/ask kids 해열제 용량은?"))
        assert db.get_qa_request(r.rsplit(":", 1)[1])["preset_alias"] == "kids"
        assert bot.handle_update(upd_msg("/ask yolo 질문?")) == "cmd:ask:rejected"
        assert bot.handle_update(upd_msg("/ask")) == "cmd:ask:rejected"
        assert bot.handle_update(upd_msg("/ask x --tests=a.py")) == "cmd:ask:rejected"
        assert bot.handle_update(upd_cb("p", encode_preset_token("kids"))) == "callback:preset:kids"
        r = bot.handle_update(upd_msg("/ask 포커스 기본값?"))
        assert db.get_qa_request(r.rsplit(":", 1)[1])["preset_alias"] == "kids"
        assert bot.handle_update(upd_msg("/clear")) == "cmd:clear" and db.get_kv(f"focus:{ADMIN}") is None
        assert bot.handle_update(upd_msg("질문?")) == "text:no_focus"
        assert len(db.list_qa_requests()) == 3 and db.list_tasks() == []

    def test_expired_focus_and_unlocked_change(self, tmp_path, repo):
        d, clk = _qa_db(tmp_path)
        try:
            bot, tr = make_bot(tmp_path, repo, d, unlocked_presets={"kids"})
            bot.handle_update(upd_cb("p", encode_preset_token("kids")))
            clk.t += 901
            assert bot.handle_update(upd_msg("질문?")) == "text:no_focus"
            bot.handle_update(upd_cb("p", encode_preset_token("kids")))
            bot2, _ = make_bot(tmp_path, repo, d, unlocked_presets={"yolo"})
            assert bot2.handle_update(upd_msg("질문?")) == "text:no_focus"  # alias no longer unlocked
        finally:
            d.close()

    def test_help_status_and_clear_circuit(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        h = dev_bot_mod.HELP_TEXT
        assert len(h) < 800 and SafeHtmlFormatter.visible_len(h) < 800
        for needle in ("/bots", "/ask", "/clear", "/do", "자연어"):
            assert needle in h
        assert SafeHtmlFormatter().finalize(h) == h
        for _ in range(3):
            db.circuit_record(QA_CIRCUIT, False)
            db.circuit_record(CIRCUIT, False)
        bot.handle_update(upd_msg("/status"))
        assert "qa_circuit: <b>OPEN</b>" in tr.sent[-1]["text"]
        assert bot.handle_update(upd_msg("/resume clear-circuit")) == "cmd:resume"
        assert not db.circuit_is_open(CIRCUIT) and not db.circuit_is_open(QA_CIRCUIT)
        bot.handle_update(upd_msg("/status"))
        assert "qa_circuit" not in tr.sent[-1]["text"]

    def test_http_transport_reply_parameters(self, monkeypatch):
        t = HttpTransport(VALID_TOKEN)
        calls = []
        monkeypatch.setattr(t, "_call", lambda method, payload, timeout=None: calls.append(payload) or {"message_id": 5})
        assert t.send_message(ADMIN, "x", {"force_reply": True}, reply_to_message_id=9) == 5
        assert calls[-1]["reply_parameters"] == {"message_id": 9, "allow_sending_without_reply": True}
        t.send_message(ADMIN, "x")
        assert "reply_parameters" not in calls[-1]


class TestQAOutboxDispatch:
    def _answered(self, d, answer, refs=("calc.py",), alias="kids"):
        qid, _ = d.create_qa_request(ADMIN, alias, "질문 <b>?", QA_HASH)
        d.claim_next_qa_request("w", 1, 1.0)
        assert d.complete_qa_answer(qid, "w", 1, "codex", "a" * 40, answer, list(refs))
        return qid

    def test_answer_delivery_never_touches_tasks(self, tmp_path, repo, db, monkeypatch):
        bot, tr = make_bot(tmp_path, repo, db)
        qid = self._answered(db, f"답변 <script> {TG_SECRET}")

        def boom(*a, **k):
            raise AssertionError("_dispatch_qa_row must not call get_task")

        monkeypatch.setattr(db, "get_task", boom)
        assert bot.dispatch_once()["sent"] == 1
        text = tr.sent[-1]["text"]
        assert qid in text and "&lt;script&gt;" in text and "질문 &lt;b&gt;?" in text and TG_SECRET not in text
        assert "<code>calc.py</code>" in text and SafeHtmlFormatter().finalize(text) == text
        assert db.qa_outbox(qid)[0]["status"] == "SENT" and bot.dispatch_once()["sent"] == 0

    def test_huge_answer_fits_limits(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        self._answered(db, "<<<<" * 2000 + "\x01\x02 끝", refs=["a/b.py"] * 5)
        assert bot.dispatch_once()["sent"] == 1
        text = tr.sent[-1]["text"]
        assert len(text) <= 3500 and SafeHtmlFormatter.visible_len(text) <= 1800 and "…" in text
        assert "\x01" not in text and text.endswith("<code>a/b.py</code>")

    def test_fail_notice_and_429_and_unknown(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        qid, _ = db.create_qa_request(ADMIN, None, "q", QA_HASH)
        db.claim_next_qa_request("w", 1, 1.0)
        db.fail_qa_request(qid, "w", 1, "x", notify_text="답변 실패 안내", retryable=False)
        tr.send_script = [TgRetryAfter(3)]
        assert bot.dispatch_once()["retry"] == 1 and db.qa_outbox(qid)[0]["status"] == "PENDING"
        db._conn.execute("UPDATE qa_outbox SET next_retry_at=0")
        tr.send_script = [TgUnknown("timeout")]
        assert bot.dispatch_once()["unknown"] == 1 and db.qa_outbox(qid)[0]["status"] == "UNKNOWN"
        assert bot.dispatch_once() == {"sent": 0, "retry": 0, "unknown": 0, "failed": 0, "expired": 0}
        qid2 = self._answered(db, "ok", alias=None)
        assert bot.dispatch_once()["sent"] == 1 and "저장소 전체" in tr.sent[-1]["text"]
        assert db.qa_outbox(qid2)[0]["status"] == "SENT"
        assert db.claim_next_qa_request("w", 1, 1.0) is None  # nothing pending

    def test_fail_notice_text(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        qid, _ = db.create_qa_request(ADMIN, "kids", "q", QA_HASH)
        db.claim_next_qa_request("w", 1, 1.0)
        db.fail_qa_request(qid, "w", 1, "x", notify_text="답변 실패 안내", retryable=False)
        assert bot.dispatch_once()["sent"] == 1
        assert "질문 처리 실패" in tr.sent[-1]["text"] and "답변 실패 안내" in tr.sent[-1]["text"]

    def test_boot_quarantines_stale_qa_rows_and_non_admin_chat(self, tmp_path, repo, db):
        qid = self._answered(db, "old answer")
        db._conn.execute("UPDATE qa_outbox SET created_at=created_at-100")
        bot, tr = make_bot(tmp_path, repo, db)  # boot: no compensatory redelivery
        assert db.qa_outbox(qid)[0]["status"] == "EXPIRED" and bot.dispatch_once()["sent"] == 0
        q2, _ = db.create_qa_request(OTHER, None, "x", "z" * 64)
        db.claim_next_qa_request("w", 1, 1.0)
        db.complete_qa_answer(q2, "w", 1, "codex", "a" * 40, "x", [])
        assert bot.dispatch_once()["failed"] == 1 and tr.sent == []


# ######################################################################## LIVE PROGRESS
import re as _re

import auto_factory.live_progress as lp_mod
import auto_factory.win_job as win_job_mod
from auto_factory.adapters import FAILOVER_CODES as _FAILOVER_CODES
from auto_factory.db import heartbeat_nowait, read_runtime_snapshot_readonly
from auto_factory.dev_bot import STATELESS_TASK_CB_RE, stateless_task_button
from auto_factory.guardrails import DeterministicRunner
from auto_factory.live_progress import (
    LeaseKeeper,
    LeaseLostError,
    LiveProgressSink,
    ProgressSink,
    build_runtime_snapshot,
    classify_liveness,
    read_live_log_tail,
    read_live_sidecar,
    stage_label,
)
from auto_factory.win_job import RunResult
from auto_factory.worker import call_with_progress


class _RecSink(ProgressSink):
    def __init__(self, lost_after: float | None = None):
        super().__init__()
        self.calls: list[tuple] = []
        self.totals: dict[str, int] = {}
        self.lost_after = lost_after
        self._t0 = None
        self.lock = _threading.Lock()

    def start(self, pid, proc_start_time, step=None, provider=None, role=None, attempt_id=None, *, redaction=None):
        self._t0 = time.monotonic()
        self.calls.append(("start", pid, proc_start_time, step, redaction))

    def output(self, stream, bytes_seen, last_output_at=None):
        assert _threading.current_thread() is not _threading.main_thread()  # reader threads only
        with self.lock:
            self.totals[stream] = bytes_seen

    def pulse(self):
        self.calls.append(("pulse",))

    def finish(self, returncode, timed_out=False, timeout_kind=None):
        self.calls.append(("finish", returncode, timed_out, timeout_kind))

    def is_lease_lost(self):
        return self.lost_after is not None and self._t0 is not None and time.monotonic() - self._t0 > self.lost_after


class _BoomSink:
    def __getattr__(self, name):
        def boom(*a, **k):
            raise RuntimeError("sink exploded")
        return boom


def _live_files_text(art, tid) -> str:
    d = Path(art) / tid
    return "".join(p.read_text(encoding="utf-8", errors="replace") for p in d.glob("live.*"))


def _make_sink(tmp_path, tid="t1", clock=None, **kw):
    return LiveProgressSink(tmp_path / "artifacts", tid, clock=clock or Clock(1_000_000.0), **kw)


def _try_dir_link(link: Path, target: Path) -> bool:
    target.mkdir(parents=True, exist_ok=True)
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if IS_WINDOWS:
        try:
            import _winapi

            _winapi.CreateJunction(str(target), str(link))
            return True
        except OSError:
            return False
    return False


# ============================================================ sidecar sink
class TestLiveProgressSink:
    def test_lifecycle_fields_and_counters(self, tmp_path):
        clock = Clock(1_000_000.0)
        s = _make_sink(tmp_path, clock=clock)
        assert s.begin_run("IMPLEMENTING") == 1
        s.set_stage("implement", "claude", "writer", 7)
        clock.t += 3
        s.start(4242, 123.5, "writer")
        s.output("stdout", 10)
        s.output("stdout", 25)
        s.output("stderr", 5)
        s.flush()
        live = read_live_sidecar(tmp_path / "artifacts", "t1")
        assert set(live) == {
            "schema_version", "task_id", "generation", "state", "step", "provider", "role", "attempt_id", "pid",
            "proc_start_time", "started_at", "last_heartbeat_at", "last_output_at", "bytes_seen", "redaction",
            "running", "finished_at", "lease_lost"}
        assert live["schema_version"] == 1 and live["task_id"] == "t1" and live["generation"] == 1
        assert (live["step"], live["provider"], live["role"], live["attempt_id"]) == ("writer", "claude", "writer", 7)
        assert live["pid"] == 4242 and live["proc_start_time"] == 123.5 and live["bytes_seen"] == 30
        assert live["running"] is True and live["redaction"] == "metadata_only" and live["lease_lost"] is False
        assert live["started_at"] == 1_000_000.0 and live["last_output_at"] == 1_000_003.0  # db clock, not wall
        clock.t += 2
        s.finish(0, False, None)
        s.close("CHECKPOINTED")
        live = read_live_sidecar(tmp_path / "artifacts", "t1")
        assert live["running"] is False and live["finished_at"] == 1_000_005.0 and live["state"] == "CHECKPOINTED"
        log = read_live_log_tail(tmp_path / "artifacts", "t1", 50)
        assert any("event=start" in ln and "provider=claude" in ln and "pid=4242" in ln for ln in log)
        assert any("event=finish" in ln and "status=exit:0" in ln for ln in log)
        assert all(_re.match(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ gen=1 ", ln) for ln in log)

    def test_output_is_memory_only(self, tmp_path):
        s = _make_sink(tmp_path)
        s.begin_run("IMPLEMENTING")
        writes = []
        s._writer = lambda p, d: writes.append(p)
        for i in range(500):
            s.output("stdout", i)
        assert writes == [] and s.snapshot()["bytes_seen"] == 499

    def test_pulse_is_throttled(self, tmp_path):
        mono = [100.0]
        s = _make_sink(tmp_path, monotonic=lambda: mono[0], pulse_interval=5.0)
        s.begin_run("IMPLEMENTING")
        writes = []
        real = s._writer
        s._writer = lambda p, d: (writes.append(p), real(p, d))
        for _ in range(50):
            s.pulse()
        assert writes == []
        mono[0] += 5.1
        s.pulse()
        assert len(writes) >= 1

    def test_generation_monotonic_blocks_stale_overwrite(self, tmp_path):
        old = _make_sink(tmp_path)
        assert old.begin_run("IMPLEMENTING") == 1
        new = _make_sink(tmp_path)
        assert new.begin_run("IMPLEMENTING") == 2
        new.set_stage("review", "gemini", "reviewer", 9)
        old.set_stage("implement", "claude", "writer", 3)  # stale owner must not win
        old.close("BLOCKED")
        live = read_live_sidecar(tmp_path / "artifacts", "t1")
        assert live["generation"] == 2 and live["provider"] == "gemini" and old.superseded
        third = _make_sink(tmp_path)
        assert third.begin_run() == 3

    def test_windows_sharing_violation_retry_and_preserve(self, tmp_path):
        from auto_factory.worktree import safe_write_atomic

        s = _make_sink(tmp_path, retry_delays=(0.0, 0.0, 0.0), sleep=lambda x: None)
        s.begin_run("IMPLEMENTING")
        good = read_live_sidecar(tmp_path / "artifacts", "t1")
        fails = [PermissionError(32, "sharing violation"), OSError(13, "access denied")]

        def flaky(p, d):
            if fails:
                raise fails.pop(0)
            return safe_write_atomic(p, d)

        s._writer = flaky
        s.set_stage("pytest", "deterministic", "verify")
        assert read_live_sidecar(tmp_path / "artifacts", "t1")["step"] == "pytest" and s.write_errors == 0

        def always(p, d):
            raise PermissionError(32, "sharing violation")

        s._writer = always
        s.set_stage("holdout", "deterministic", "verify")  # never raises
        after = read_live_sidecar(tmp_path / "artifacts", "t1")
        assert after["step"] == "pytest" and after["generation"] == good["generation"] and s.write_errors >= 1
        assert not [p for p in (tmp_path / "artifacts" / "t1").iterdir() if p.name.endswith(".tmp")]

    def test_invalid_task_ids_rejected(self, tmp_path):
        for bad in ("../x", "a/b", "", ".hidden", "a\\b", "x" * 65, None):
            with pytest.raises(ValueError):
                LiveProgressSink(tmp_path / "artifacts", bad)
            assert read_live_sidecar(tmp_path / "artifacts", bad) is None
            assert read_live_log_tail(tmp_path / "artifacts", bad) is None
        assert not (tmp_path / "x").exists()

    def test_symlink_or_junction_task_dir_rejected(self, tmp_path):
        art = tmp_path / "artifacts"
        outside = tmp_path / "outside"
        if not _try_dir_link(art / "tlink", outside):
            pytest.skip("cannot create symlink/junction here")
        (outside / "live.json").write_text(_json.dumps({"schema_version": 1, "task_id": "tlink", "generation": 5}))
        (outside / "live.log").write_text("planted\n")
        with pytest.raises(ValueError):  # UnsafePathError
            LiveProgressSink(art, "tlink")
        assert read_live_sidecar(art, "tlink") is None and read_live_log_tail(art, "tlink") is None
        assert (outside / "live.log").read_text() == "planted\n"

    def test_corrupt_or_mismatched_sidecar_ignored(self, tmp_path):
        d = tmp_path / "artifacts" / "t1"
        d.mkdir(parents=True)
        for body in ("{not json", "[]", '{"schema_version": 2, "task_id": "t1", "generation": 1}',
                     '{"schema_version": 1, "task_id": "t2", "generation": 1}',
                     '{"schema_version": 1, "task_id": "t1", "generation": true}', "x" * 20000):
            (d / "live.json").write_text(body)
            assert read_live_sidecar(tmp_path / "artifacts", "t1") is None
        s = _make_sink(tmp_path)
        assert s.begin_run() == 1  # corrupt file never blocks a new run

    def test_log_is_bounded(self, tmp_path):
        s = _make_sink(tmp_path, max_log_bytes=4096)
        s.begin_run("IMPLEMENTING")
        for i in range(400):
            s.set_stage(f"step{i}", "claude", "writer", i)
        raw = (tmp_path / "artifacts" / "t1" / "live.log").read_bytes()
        assert len(raw) <= 4096 and raw.endswith(b"\n")
        assert all(_re.match(rb"^\d{4}-", ln) for ln in raw.splitlines())
        s2 = _make_sink(tmp_path)
        s2.begin_run()
        for i in range(1500):
            s2.set_stage(f"s{i}", "codex", "writer", i)
        assert (tmp_path / "artifacts" / "t1" / "live.log").stat().st_size <= lp_mod.MAX_LOG_BYTES

    def test_holdout_redaction_freezes_output_signals(self, tmp_path):
        clock = Clock(1_000_000.0)
        s = _make_sink(tmp_path, clock=clock)
        s.begin_run("VERIFYING")
        s.set_stage("holdout", "deterministic", "verify", redaction="holdout")
        s.start(99, 1.0, "holdout", redaction="holdout")
        clock.t += 7
        s.output("stdout", 12345)
        s.finish(1, False, None)
        live = read_live_sidecar(tmp_path / "artifacts", "t1")
        assert live["redaction"] == "holdout" and live["bytes_seen"] == 0 and live["last_output_at"] is None
        hold = [ln for ln in read_live_log_tail(tmp_path / "artifacts", "t1", 50) if "step=holdout" in ln]
        assert hold and all("provider=" not in ln and "pid=" not in ln and "bytes_seen" not in ln for ln in hold)
        assert any(_re.search(r"step=holdout status=exit:1 elapsed=7s$", ln) for ln in hold)


# ============================================================ JobRunner + sink
class TestJobRunnerProgress:
    def test_sink_lifecycle(self):
        sink = _RecSink()
        res = JobRunner(poll_interval=0.02).run(_py("""
            import sys, time
            sys.stdout.write("a" * 1000); sys.stdout.flush()
            sys.stderr.write("b" * 300); sys.stderr.flush()
            time.sleep(0.3)
        """), progress_sink=sink, progress_step="writer")
        assert res.ok
        kinds = [c[0] for c in sink.calls]
        assert kinds[0] == "start" and kinds[-1] == "finish" and "pulse" in kinds
        start = sink.calls[0]
        assert start[1] == res.pid and start[2] == res.start_time and start[3] == "writer"
        assert start[4] == "metadata_only"
        assert sink.totals == {"stdout": 1000, "stderr": 300}
        assert sink.calls[-1] == ("finish", 0, False, None)

    def test_defaults_unchanged_without_sink(self):
        res = JobRunner().run(_py("print('x')"))
        assert res.ok and res.stdout.strip() == "x"

    def test_sink_exceptions_never_fail_the_run(self):
        res = JobRunner(poll_interval=0.02).run(_py("print('hello'); import time; time.sleep(0.2)"),
                                                progress_sink=_BoomSink(), progress_step="writer")
        assert res.ok and res.stdout.strip() == "hello" and not res.timed_out

    def test_lease_lost_terminates_child(self):
        sink = _RecSink(lost_after=0.3)
        res = JobRunner(poll_interval=0.02).run(_py("import time; time.sleep(60)"), wall_timeout=60,
                                                idle_timeout=60, progress_sink=sink)
        assert res.timed_out and res.timeout_kind == "lease_lost" and res.duration < 20
        assert sink.calls[-1][0] == "finish" and sink.calls[-1][3] == "lease_lost"
        assert process_start_time(res.pid) is None

    def test_bounded_memory_and_full_byte_count(self):
        sink = _RecSink()
        res = JobRunner().run(_py("""
            import sys
            chunk = b"z" * 65536
            for _ in range(48):
                sys.stdout.buffer.write(chunk)
            sys.stdout.flush()
        """), progress_sink=sink)
        assert res.ok and sink.totals["stdout"] == 48 * 65536
        assert len(res.stdout) <= win_job_mod._MAX_CAPTURE_BYTES + 65536

    @pytest.mark.skipif(not IS_WINDOWS, reason="Job Objects are Windows-only")
    def test_job_handle_closed_on_spawn_failure_and_timeout(self, tmp_path, monkeypatch):
        made = []

        class _TrackedJob(win_job_mod.JobObject):
            def __init__(self):
                super().__init__()
                self.closed = False
                made.append(self)

            def close(self):
                self.closed = True
                super().close()

        monkeypatch.setattr(win_job_mod, "JobObject", _TrackedJob)
        with pytest.raises(OSError):
            JobRunner().run([str(tmp_path / "definitely-missing.exe")], progress_sink=_RecSink())
        assert len(made) == 1 and made[0].closed and made[0].handle is None
        sink = _RecSink()
        res = JobRunner(poll_interval=0.02).run(_py("import time; time.sleep(60)"), wall_timeout=30,
                                                idle_timeout=1.0, progress_sink=sink)
        assert res.timeout_kind == "idle" and made[-1].closed and made[-1].handle is None
        assert sink.calls[-1] == ("finish", res.returncode, True, "idle")

    def test_adapter_maps_lease_lost_to_non_failover(self, tmp_path):
        class _LostRunner:
            def run(self, cmd, cwd=None, env=None, stop_check=None, **kw):
                self.kw = kw
                return RunResult(returncode=1, stdout="", stderr="", timed_out=True, timeout_kind="lease_lost")

        runner = _LostRunner()
        a = CodexAdapter(runner)
        a.resolve_executable = lambda: "codex"
        c = HandoffContract("t1", "L1", "a" * 40, "a" * 40, ["calc.py"], ["pytest"], "writer")
        res = a.invoke(c, str(tmp_path), "writer", "x.json", progress_sink=_RecSink())
        assert res.code == FailureCode.INTERNAL and res.code not in _FAILOVER_CODES
        assert runner.kw["progress_step"] == "writer" and isinstance(runner.kw["progress_sink"], _RecSink)

    def test_adapter_without_sink_keeps_runner_signature(self, tmp_path):
        class _StrictRunner:
            def run(self, cmd, cwd=None, env=None, stop_check=None):
                return RunResult(returncode=0, stdout="ok", stderr="")

        a = CodexAdapter(_StrictRunner())
        a.resolve_executable = lambda: "codex"
        c = HandoffContract("t1", "L1", "a" * 40, "a" * 40, ["calc.py"], ["pytest"], "writer")
        assert a.invoke(c, str(tmp_path), "writer", "x.json").ok

    def test_call_with_progress_tolerates_legacy_signatures(self):
        seen = []
        assert call_with_progress(lambda a, b: seen.append((a, b)) or "ok", 1, 2, progress_sink=_RecSink()) == "ok"
        assert call_with_progress(lambda a, progress_sink=None: progress_sink, 1, progress_sink="S") == "S"
        assert call_with_progress(lambda a, **kw: kw, 1, progress_sink="S") == {"progress_sink": "S"}
        assert call_with_progress(lambda a, **kw: kw, 1, progress_sink=None) == {}
        assert seen == [(1, 2)]


class TestHoldoutZeroLeak:
    def test_holdout_gate_sidecar_has_no_names_paths_or_assertions(self, tmp_path):
        hold = tmp_path / "secret_holdout_dir_zebra"
        hold.mkdir()
        (hold / "test_secret_zebra_name.py").write_text(
            "def test_hidden_quokka_case():\n    assert 1 == 2, 'QUOKKA_ASSERT_MSG'\n", encoding="utf-8")
        wt = tmp_path / "wt"
        wt.mkdir()
        sink = _make_sink(tmp_path, tid="th1")
        sink.begin_run("VERIFYING")
        sink.set_stage("holdout", "deterministic", "verify", redaction="holdout")
        res = DeterministicRunner().holdout(str(wt), str(hold), progress_sink=sink)
        sink.close("VERIFYING")
        assert not res.ok and "QUOKKA_ASSERT_MSG" in res.output_tail  # it really was in the output
        blob = _live_files_text(tmp_path / "artifacts", "th1")
        for needle in ("zebra", "quokka", "QUOKKA", "test_secret", "test_hidden", "secret_holdout",
                       str(tmp_path), "pytest", "assert"):
            assert needle not in blob, needle
        live = read_live_sidecar(tmp_path / "artifacts", "th1")
        assert live["bytes_seen"] == 0 and live["last_output_at"] is None
        assert "step=holdout" in blob


# ============================================================ lease keeper
class TestLeaseKeeper:
    def _leased(self, tmp_path, ttl=60.0):
        clock = Clock(1_000_000.0)
        d = CoordinatorDB(tmp_path / "lk.db", clock=clock)
        tid, _ = d.create_task("r", "L1", ["calc.py"], {})
        assert d.acquire_lease(tid, "owner-nonce", os.getpid(), process_start_time(os.getpid()), ttl=ttl)
        return d, clock, tid

    def test_interval_formula(self):
        for ttl, want in ((900.0, 15.0), (12.0, 5.0), (60.0, 10.0), (30.0, 5.0)):
            assert LeaseKeeper("x.db", "t1", "n", ttl).interval == want

    def test_busy_is_skipped_then_renews(self, tmp_path):
        d, clock, tid = self._leased(tmp_path)
        try:
            sink = LiveProgressSink(tmp_path / "artifacts", tid, clock=clock)
            sink.begin_run("IMPLEMENTING")
            k = LeaseKeeper(d.path, tid, "owner-nonce", 60.0, clock=clock, sink=sink)
            blocker = _sqlite3.connect(d.path, timeout=0, isolation_level=None)
            blocker.execute("BEGIN IMMEDIATE")
            t0 = time.monotonic()
            assert k.tick() is True and k.busy_skips == 1 and not k.is_lease_lost()
            assert time.monotonic() - t0 < 2.0  # busy_timeout=0: never blocks
            blocker.execute("ROLLBACK")
            blocker.close()
            clock.t += 20
            assert k.tick() is True and k.beats == 1
            lease = d.get_lease(tid)
            assert lease["heartbeat_at"] == 1_000_020.0 and lease["expires_at"] == 1_000_080.0
            assert "owner-nonce" not in repr(k) and "owner-nonce" not in _live_files_text(tmp_path / "artifacts", tid)
        finally:
            d.close()

    def test_prolonged_busy_past_margin_is_lease_lost(self, tmp_path):
        d, clock, tid = self._leased(tmp_path)
        try:
            sink = LiveProgressSink(tmp_path / "artifacts", tid, clock=clock)
            sink.begin_run("IMPLEMENTING")
            k = LeaseKeeper(d.path, tid, "owner-nonce", 60.0, clock=clock, sink=sink)
            blocker = _sqlite3.connect(d.path, timeout=0, isolation_level=None)
            blocker.execute("BEGIN IMMEDIATE")
            try:
                clock.t += 60 - k.safety_margin - 1
                assert k.tick() is True and not k.is_lease_lost()
                clock.t += 2
                assert k.tick() is False and k.is_lease_lost() and sink.is_lease_lost()
            finally:
                blocker.execute("ROLLBACK")
                blocker.close()
            assert k.tick() is False  # latched: never silently recovers
            live = read_live_sidecar(tmp_path / "artifacts", tid)
            assert live["lease_lost"] is True
            assert any("LEASE_DEGRADED" in ln for ln in read_live_log_tail(tmp_path / "artifacts", tid, 20))
        finally:
            d.close()

    def test_taken_lease_is_lost_immediately(self, tmp_path):
        d, clock, tid = self._leased(tmp_path)
        try:
            d.force_release_lease(tid)
            assert d.acquire_lease(tid, "thief", 1, 1.0, ttl=60)
            k = LeaseKeeper(d.path, tid, "owner-nonce", 60.0, clock=clock)
            assert k.tick() is False and k.is_lease_lost()
            assert d.get_lease(tid)["owner_nonce"] == "thief"  # never touched the new owner's row
        finally:
            d.close()

    def test_heartbeat_nowait_never_creates_db(self, tmp_path):
        missing = tmp_path / "nope" / "c.db"
        with pytest.raises(_sqlite3.OperationalError):
            heartbeat_nowait(missing, "t1", "n", 10.0, now=1.0)
        assert not missing.exists()

    def test_keeper_thread_kills_child_when_lease_stolen(self, tmp_path):
        d, clock, tid = self._leased(tmp_path)
        try:
            sink = LiveProgressSink(tmp_path / "artifacts", tid, clock=clock)
            sink.begin_run("IMPLEMENTING")
            k = LeaseKeeper(d.path, tid, "owner-nonce", 60.0, clock=clock, sink=sink, interval=0.1).start()
            d.force_release_lease(tid)
            assert d.acquire_lease(tid, "thief", 1, 1.0, ttl=60)
            res = JobRunner(poll_interval=0.02).run(_py("import time; time.sleep(60)"), wall_timeout=60,
                                                    idle_timeout=60, progress_sink=sink)
            k.stop()
            assert res.timeout_kind == "lease_lost" and res.duration < 20
            assert read_live_sidecar(tmp_path / "artifacts", tid)["lease_lost"] is True
        finally:
            d.close()


# ============================================================ worker wiring
class TestWorkerLiveProgress:
    def test_worker_writes_metadata_only_sidecar(self, tmp_path, repo, db):
        w = make_worker(tmp_path, repo, db, claude=[_fix])
        tid = submit(db, request="fix add REQUEST_TEXT_MARKER")
        assert w.process_task(tid) == "AWAITING_MERGE"
        live = read_live_sidecar(tmp_path / "artifacts", tid)
        assert live["generation"] == 1 and live["running"] is False and live["state"] == "AWAITING_MERGE"
        assert live["lease_lost"] is False and db.get_lease(tid) is None
        log = "\n".join(read_live_log_tail(tmp_path / "artifacts", tid, 500))
        for needle in ("step=implement provider=claude role=writer", "step=py_compile", "step=pytest",
                       "step=holdout", "step=review provider=", "event=run_end"):
            assert needle in log, needle
        blob = _live_files_text(tmp_path / "artifacts", tid)
        task = db.get_task(tid)
        for secret in ("REQUEST_TEXT_MARKER", w.nonce, str(task["worktree_path"]), str(repo), "handoff",
                       "test_holdout"):
            assert secret not in blob, secret
        assert w.progress_errors == []

    def test_generation_increments_per_run(self, tmp_path, repo, db):
        w = make_worker(tmp_path, repo, db)
        tid = submit(db, start=False)
        assert w.process_task(tid) == "AWAITING_START_AUTH"
        assert read_live_sidecar(tmp_path / "artifacts", tid)["generation"] == 1
        db.add_approval(tid, "start")
        w.process_task(tid)
        assert read_live_sidecar(tmp_path / "artifacts", tid)["generation"] == 2

    def test_lease_lost_aborts_without_mutating_task(self, tmp_path, repo, db):
        tid = submit(db)

        def steal(wt, contract, role):
            Path(wt, "calc.py").write_text(FIX, encoding="utf-8")
            db.force_release_lease(tid)
            assert db.acquire_lease(tid, "thief-nonce", 1, 1.0, ttl=900)

        w = make_worker(tmp_path, repo, db, claude=[steal])
        assert not issubclass(LeaseLostError, worker_mod.StopRequested)
        assert w.process_task(tid) == "LEASE_LOST"
        task = db.get_task(tid)
        assert task["state"] == "IMPLEMENTING" and task["last_error"] is None
        assert [e["to_state"] for e in db.events(tid)][-1] == "IMPLEMENTING"
        writer = db.attempts(tid, "writer")
        assert len(writer) == 1 and writer[0]["finished_at"] is None  # not settled as TIMEOUT/failover
        assert w.adapters["codex"].calls == [] and w.adapters["gemini"].calls == []
        assert db.get_lease(tid)["owner_nonce"] == "thief-nonce"  # new owner untouched
        live = read_live_sidecar(tmp_path / "artifacts", tid)
        assert live["lease_lost"] is True and live["running"] is False

    def test_sink_failures_never_fail_the_worker(self, tmp_path, repo, db, monkeypatch):
        def boom(self, *a, **k):
            raise RuntimeError("disk on fire")

        monkeypatch.setattr(LiveProgressSink, "set_stage", boom)
        monkeypatch.setattr(LiveProgressSink, "close", boom)
        w = make_worker(tmp_path, repo, db, claude=[_fix])
        tid = submit(db)
        assert w.process_task(tid) == "AWAITING_MERGE" and w.progress_errors

    def test_sink_construction_failure_keeps_running(self, tmp_path, repo, db, monkeypatch):
        def bad_init(self, *a, **k):
            raise OSError("cannot create sidecar dir")

        monkeypatch.setattr(LiveProgressSink, "__init__", bad_init)
        w = make_worker(tmp_path, repo, db, claude=[_fix])
        tid = submit(db)
        assert w.process_task(tid) == "AWAITING_MERGE"
        assert any(e.startswith("sink:") for e in w.progress_errors)

    def test_live_progress_can_be_disabled(self, tmp_path, repo, db):
        w = make_worker(tmp_path, repo, db, claude=[_fix], live_progress=False)
        tid = submit(db)
        assert w.process_task(tid) == "AWAITING_MERGE"
        assert read_live_sidecar(tmp_path / "artifacts", tid) is None


# ============================================================ stage / liveness
class TestLiveness:
    NOW = 1_000_000.0

    def _lease(self, **over):
        d = {"pid": 11, "proc_start_time": 5.0, "heartbeat_at": self.NOW - 5, "expires_at": self.NOW + 300}
        d.update(over)
        return d

    def _live(self, **over):
        d = {"task_id": "t1", "generation": 1, "running": True, "pid": 22, "proc_start_time": 6.0,
             "last_heartbeat_at": self.NOW - 2, "last_output_at": self.NOW - 3, "started_at": self.NOW - 100,
             "redaction": "metadata_only", "lease_lost": False}
        d.update(over)
        return d

    def test_stage_map(self):
        assert stage_label("RECEIVED") == "01/14 RECEIVED" and stage_label("MERGED") == "14/14 MERGED"
        assert stage_label("IMPLEMENTING") == "09/14 IMPLEMENTING"
        assert stage_label("RETRY_WAIT") == "09R/14" and stage_label("RECOVERY_PENDING") == "08R/14"
        assert stage_label("SINGLE_PROVIDER_REVIEW_REQUIRED") == "EXCEPTION/REVIEW"
        for s in ("BLOCKED", "FAILED", "CANCELLED", "DISCARDED", "GUARDRAIL_BLOCKED"):
            assert stage_label(s) == f"EXCEPTION/{s}"
        assert not any("%" in stage_label(s) for s in MAIN_FLOW + EXCEPTION_STATES)

    def test_four_state_matrix(self):
        def alive(pid, st):
            return True

        def dead(pid, st):
            return False

        n = self.NOW
        assert classify_liveness("IMPLEMENTING", self._lease(), self._live(), n, same_process=alive) == "ACTIVE"
        assert classify_liveness("IMPLEMENTING", self._lease(), self._live(last_output_at=n - 600), n,
                                 same_process=alive) == "QUIET"
        assert classify_liveness("IMPLEMENTING", self._lease(), self._live(), n, same_process=dead) == "STALLED"
        assert classify_liveness("IMPLEMENTING", self._lease(expires_at=n - 1), self._live(), n,
                                 same_process=alive) == "STALLED"
        assert classify_liveness("IMPLEMENTING", self._lease(heartbeat_at=n - 900),
                                 self._live(last_heartbeat_at=n - 900), n, same_process=alive) == "STALLED"
        assert classify_liveness("IMPLEMENTING", None, None, n, same_process=alive) == "STALLED"
        assert classify_liveness("IMPLEMENTING", self._lease(), self._live(lease_lost=True), n,
                                 same_process=alive) == "STALLED"
        for s in ("AWAITING_START_AUTH", "AWAITING_MERGE", "SINGLE_PROVIDER_REVIEW_REQUIRED", "BLOCKED",
                  "GUARDRAIL_BLOCKED"):
            assert classify_liveness(s, None, None, n, same_process=dead) == "WAITING_HUMAN"
        assert classify_liveness("MERGED", None, None, n) == "DONE"
        for s in ("FAILED", "CANCELLED", "DISCARDED"):
            assert classify_liveness(s, None, None, n) == "FAILED"
        assert classify_liveness("PREFLIGHTED", None, None, n) == "QUEUED"
        assert classify_liveness("IMPLEMENTING", self._lease(), None, n, same_process=alive) == "ACTIVE"

    def test_child_dead_only_stalls_after_grace(self):
        n = self.NOW

        def owner_only(pid, st):
            return pid == 11

        assert classify_liveness("IMPLEMENTING", self._lease(), self._live(last_heartbeat_at=n - 1), n,
                                 same_process=owner_only) != "STALLED"
        assert classify_liveness("IMPLEMENTING", self._lease(), self._live(last_heartbeat_at=n - 100), n,
                                 same_process=owner_only) == "STALLED"

    def test_pid_reuse_defense_with_real_process_identity(self):
        pid = os.getpid()
        st = process_start_time(pid)
        n = time.time()
        lease = {"pid": pid, "proc_start_time": st, "heartbeat_at": n, "expires_at": n + 60}
        assert classify_liveness("IMPLEMENTING", lease, None, n) == "ACTIVE"
        reused = dict(lease, proc_start_time=st - 1000)  # same PID, different process start time
        assert classify_liveness("IMPLEMENTING", reused, None, n) == "STALLED"

    def test_holdout_snapshot_hides_output_age(self):
        row = {"id": "t1", "state": "VERIFYING", "state_version": 3, "risk_level": "L1",
               "state_entered_at": self.NOW - 50, "lease": self._lease(), "last_attempt": None}
        s = build_runtime_snapshot(row, self._live(redaction="holdout", last_output_at=self.NOW - 999),
                                   self.NOW, same_process=lambda p, t: True)
        assert s.output_age is None and s.liveness == "ACTIVE" and s.stage == "11/14 VERIFYING"
        assert s.state_age == 50 and s.elapsed == 100 and s.attempt == "-"
        bad = build_runtime_snapshot(row, self._live(task_id="other"), self.NOW, same_process=lambda p, t: True)
        assert bad.generation is None and bad.step is None

    def test_runtime_snapshot_readonly_never_creates_db(self, tmp_path):
        missing = tmp_path / "s" / "c.db"
        with pytest.raises(FileNotFoundError):
            read_runtime_snapshot_readonly(missing)
        assert not missing.parent.exists()
        d = CoordinatorDB(tmp_path / "c.db")
        tid, _ = d.create_task("r", "L1", [], {})
        d.add_attempt(tid, "claude", "writer")
        d.acquire_lease(tid, "secret-nonce", 1, 2.0, ttl=30)
        d.close()
        snap = read_runtime_snapshot_readonly(tmp_path / "c.db", tid)
        row = snap["tasks"][0]
        assert row["last_attempt"]["provider"] == "claude" and row["attempt_count"] == 1
        assert "owner_nonce" not in row["lease"] and "secret-nonce" not in repr(snap)
        assert read_runtime_snapshot_readonly(tmp_path / "c.db", "tnope")["tasks"] == []


# ============================================================ dev bot views
def _implementing(db, tid):
    _to_state(db, tid, "AWAITING_START_AUTH")
    db.add_approval(tid, "start")
    _advance(db, tid, ["WORKTREE_READY", "IMPLEMENTING"])


def _write_log(tmp_path, tid, lines):
    d = tmp_path / "artifacts" / tid
    d.mkdir(parents=True, exist_ok=True)
    (d / "live.log").write_text("".join(ln + "\n" for ln in lines), encoding="utf-8")


class TestDevBotLive:
    def test_validate_callback_data_stateless(self):
        assert validate_callback_data("af2|v|t-abc.1") and validate_callback_data("af2|l|" + "a" * 58)
        for bad in ("af2|v|../x", "af2|v|" + "a" * 59, "af2|l|-x", "af2|v|a b", "af2|w|tid", "af2|v|"):
            with pytest.raises(ValueError):
                validate_callback_data(bad)
        assert stateless_task_button("x", "v", "a" * 59) is None
        assert stateless_task_button("x", "l", "t1")["callback_data"] == "af2|l|t1"
        assert not STATELESS_TASK_CB_RE.match("af2|q|" + "A" * 22)  # q stays draft-ask

    def test_help_mentions_live_and_logs(self):
        h = dev_bot_mod.HELP_TEXT
        assert len(h) < 800 and "/live" in h and "/logs" in h and SafeHtmlFormatter().finalize(h) == h

    def test_status_overview_has_runtime_columns(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        tid = submit(db, start=False)
        _implementing(db, tid)
        assert bot.handle_update(upd_msg("/status")) == "cmd:status"
        text = tr.sent[-1]["text"]
        assert "09/14 IMPLEMENTING" in text and "STALLED" in text and "hb " in text  # no lease => stalled

    def test_status_overview_budget_plus_more(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        for i in range(80):
            db.create_task(f"req {i} <b>&", "L1", ["calc.py"], {}, task_id=f"t{i:03d}-" + "x" * 50)
        assert bot.handle_update(upd_msg("/status")) == "cmd:status"
        text = tr.sent[-1]["text"]
        assert _re.search(r"\+\d+ more", text)
        assert SafeHtmlFormatter.visible_len(text) <= 1800 and len(text) <= 3500

    def test_status_task_buttons_by_state(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        tid = submit(db, start=False)
        _implementing(db, tid)
        before = _tg_action_count(db)
        assert bot.handle_update(upd_msg(f"/status {tid}")) == "cmd:status:task"
        assert tr.buttons() == {"v": tid, "l": tid, "tc": tid} and _tg_action_count(db) == before
        text = tr.sent[-1]["text"]
        for needle in ("Stage:", "09/14 IMPLEMENTING", "Liveness:", "Elapsed:", "Attempt:", "hb_age", "output_age"):
            assert needle in text
        tid2 = submit(db, request="gated", start=False)
        _to_state(db, tid2, "AWAITING_START_AUTH")
        bot.handle_update(upd_msg(f"/status {tid2}"))
        assert set(tr.buttons()) == {"s", "c"}  # workflow buttons untouched, no extra row
        assert "WAITING_HUMAN" in tr.sent[-1]["text"]

    def test_live_command_is_read_only(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        tid = submit(db, start=False)
        _implementing(db, tid)
        before, ver = _tg_action_count(db), db.get_task(tid)["state_version"]
        assert bot.handle_update(upd_msg(f"/live {tid}")) == "cmd:live"
        assert tr.buttons() == {"v": tid, "l": tid, "tc": tid} and _tg_action_count(db) == before
        assert db.get_task(tid)["state_version"] == ver and "09/14 IMPLEMENTING" in tr.sent[-1]["text"]
        assert bot.handle_update(upd_msg("/live ../etc")) == "cmd:live:bad-id"
        assert bot.handle_update(upd_msg("/live tnope")) == "cmd:live:unknown"
        assert bot.handle_update(upd_msg("/live")) == "cmd:live:bad-id"
        long_id = "t" + "a" * 60
        db.create_task("long", "L1", ["calc.py"], {}, task_id=long_id)
        assert bot.handle_update(upd_msg(f"/live {long_id}")) == "cmd:live"
        assert tr.sent[-1]["reply_markup"] is None and f"/logs {long_id}" in tr.sent[-1]["text"]

    def test_logs_command_escape_caps_and_missing(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        tid = submit(db, start=False)
        assert bot.handle_update(upd_msg(f"/logs {tid}")) == "cmd:logs"
        assert "live log unavailable" in tr.sent[-1]["text"] and tr.buttons() == {"v": tid, "l": tid}
        _write_log(tmp_path, tid, [f"line{i} <b>&amp; x" for i in range(5)])
        assert bot.handle_update(upd_msg(f"/logs {tid} 2")) == "cmd:logs"
        text = tr.sent[-1]["text"]
        assert "last 2" in text and "line4 &lt;b&gt;&amp;amp; x" in text and "line2" not in text
        assert "<pre>" in text and SafeHtmlFormatter().finalize(text) == text
        _write_log(tmp_path, tid, [f"{i:05d} " + "&" * 200 for i in range(300)])
        assert bot.handle_update(upd_msg(f"/logs {tid} 500")) == "cmd:logs"
        text = tr.sent[-1]["text"]
        assert "last 80" in text and len(text) <= 3500 and SafeHtmlFormatter.visible_len(text) <= 1800
        assert "00299 " in text and "00219 " not in text  # newest kept, clamp to 80
        assert bot.handle_update(upd_msg(f"/logs {tid} 0")) == "cmd:logs"
        assert "last 1" in tr.sent[-1]["text"]
        for bad, code in ((f"/logs {tid} x", "cmd:logs:bad-arg"), ("/logs ../x", "cmd:logs:bad-id"),
                          ("/logs tnope", "cmd:logs:unknown"), (f"/logs {tid} 1 2", "cmd:logs:bad-arg")):
            assert bot.handle_update(upd_msg(bad)) == code

    def test_logs_holdout_is_coarse(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        tid = submit(db, start=False)
        s = LiveProgressSink(tmp_path / "artifacts", tid, clock=db.clock)
        s.begin_run("VERIFYING")
        s.set_stage("holdout", "deterministic", "verify", redaction="holdout")
        s.start(4321, 1.0, "holdout", redaction="holdout")
        bot.handle_update(upd_msg(f"/logs {tid}"))
        text = tr.sent[-1]["text"]
        assert "holdout: coarse metadata only" in text and "step=holdout status=running" in text
        assert "pid=4321" not in text

    def test_stateless_callbacks_edit_in_place(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        tid = submit(db, start=False)
        _implementing(db, tid)
        _write_log(tmp_path, tid, ["2026-01-01T00:00:00Z gen=1 event=start step=implement"])
        before, n_sent = _tg_action_count(db), len(tr.sent)
        order = []
        real_answer, real_edit = tr.answer_callback_query, tr.edit_message_text

        def answer(*a, **k):
            order.append("answer")
            return real_answer(*a, **k)

        def edit(*a, **k):
            order.append("edit")
            return real_edit(*a, **k)

        tr.answer_callback_query, tr.edit_message_text = answer, edit
        assert bot.handle_update(upd_cb("v", tid, message_id=55)) == "callback:live:edited"
        assert order == ["answer", "edit"] and tr.edits[-1]["message_id"] == 55
        assert "09/14 IMPLEMENTING" in tr.edits[-1]["text"]
        assert tr.buttons(source="edits") == {"v": tid, "l": tid, "tc": tid}
        assert bot.handle_update(upd_cb("l", tid, message_id=55)) == "callback:logs:edited"
        assert "event=start step=implement" in tr.edits[-1]["text"]
        tr.edit_script = [TgRejected("HTTP 400: Bad Request: message is not modified")]
        assert bot.handle_update(upd_cb("v", tid, message_id=55)) == "callback:live:unchanged"
        assert len(tr.sent) == n_sent and _tg_action_count(db) == before  # never a duplicate message
        assert bot.handle_update(upd_cb("v", "tnope", message_id=55)) == "callback:live:unknown:edited"
        assert len(tr.sent) == n_sent

    def test_stateless_callback_auth_and_answer_first(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        tid = submit(db, start=False)
        assert bot.handle_update(upd_cb("v", tid, frm=OTHER)) == "ignored:unauthorized"
        assert bot.handle_update(upd_cb("l", tid, chat=OTHER)) == "ignored:unauthorized"
        assert tr.answers == [] and tr.edits == [] and tr.sent == []
        tr.answer_script = [False]
        assert bot.handle_update(upd_cb("v", tid)) == "callback:answer_failed"
        assert tr.edits == [] and tr.sent == []
        u = upd_cb("v", tid)
        u["callback_query"]["data"] = "af2|v|" + "a" * 70
        assert bot.handle_update(u) == "callback:malformed" and tr.edits == []

    def test_dev_bot_demo_still_passes(self):
        lines = []
        assert dev_bot_mod.run_demo(out=lines.append) == 0, "\n".join(lines)


# ============================================================ status CLIs
class TestStatusCliLive:
    def _entries(self):
        from auto_factory.worker import main as worker_main

        return (dev_bot_mod.main, worker_main, sup_mod.main)

    def test_missing_db_exit_2_never_created(self, tmp_path, repo):
        dbp = tmp_path / "state" / "c.db"
        for entry in self._entries():
            for extra in ([], ["--task", "t1"], ["--logs", "t1", "--lines", "5"]):
                assert entry(["--status", "--db", str(dbp), "--repo", str(repo), *extra]) == 2
                assert not dbp.exists()

    def test_status_task_and_logs_read_only(self, tmp_path, repo, monkeypatch, capsys):
        dbp = tmp_path / "state" / "c.db"
        dbp.parent.mkdir()
        d = CoordinatorDB(dbp)
        tid, _ = d.create_task("r", "L1", ["calc.py"], {})
        d.close()
        s = LiveProgressSink(tmp_path / "state" / "artifacts", tid)
        s.begin_run("RECEIVED")
        for i in range(10):
            s.set_stage(f"stepno{i}", "claude", "writer", i)
        before = dbp.read_bytes()
        for mod in (dev_bot_mod, worker_mod, sup_mod):
            monkeypatch.setattr(mod, "CoordinatorDB", lambda *a, **k: pytest.fail("RW DB opened"))
        capsys.readouterr()
        for entry in self._entries():
            args = ["--db", str(dbp), "--repo", str(repo)]
            assert entry(["--status", *args]) == 0
            out = capsys.readouterr().out
            assert tid in out and "stage=01/14 RECEIVED" in out and "liveness=QUEUED" in out and "hb_age=" in out
            assert entry(["--status", "--task", tid, *args]) == 0
            out = capsys.readouterr().out
            assert "liveness=QUEUED" in out and "generation=1" in out and "step=stepno9" in out
            assert entry(["--status", "--logs", tid, "--lines", "3", *args]) == 0
            out = capsys.readouterr().out.splitlines()
            assert len(out) == 4 and "stepno9" in out[-1] and "stepno6" not in "\n".join(out)
            assert entry(["--status", "--task", "tnope", *args]) == 2
            assert entry(["--status", "--task", "../x", *args]) == 2
            assert entry(["--status", "--logs", "tnope", *args]) == 2
            capsys.readouterr()
        assert dbp.read_bytes() == before

    def test_print_status_one_arg_compat_and_missing_log(self, tmp_path):
        dbp = tmp_path / "c.db"
        d = CoordinatorDB(dbp)
        tid, _ = d.create_task("r", "L1", [], {})
        d.close()
        lines = []
        assert worker_mod.print_status_readonly(dbp, lines.append) == 0
        assert lines[0].startswith(f"{tid}\tL1\tRECEIVED\tv0\t") and lines[-1].startswith("[auto_factory] tasks=1")
        lines.clear()
        assert worker_mod.print_status_readonly(dbp, lines.append, logs_task=tid) == 0
        assert lines[-1] == "live log unavailable"


# ######################################################################## BUTTON-FIRST UI (af3)
from auto_factory.adapters import AdapterResult as _AdapterResult
from auto_factory.db import TRANSITIONS as _TRANSITIONS
from auto_factory.db import cancel_requested_nowait
from auto_factory.dev_bot import AF3_CALLBACK_RE, CALLBACK_RE, af3_button, status_badge
from auto_factory.win_job import ProcessInspector, ProcessSample, terminate_verified


def upd_raw(data, frm=ADMIN, chat=ADMIN, message_id=7):
    _UPD[0] += 1
    return {"update_id": _UPD[0], "callback_query": {
        "id": f"cq{_UPD[0]}", "from": {"id": frm}, "data": data,
        "message": {"message_id": message_id, "chat": {"id": chat}}}}


def _cbs(msg) -> list[str]:
    return [b["callback_data"] for row in (msg.get("reply_markup") or {}).get("inline_keyboard", []) for b in row]


def _af3_tok(msg, op) -> str:
    hits = [d.split("|")[2] for d in _cbs(msg) if d.startswith(f"af3|{op}|")]
    assert hits, (op, _cbs(msg))
    return hits[0]


def _af3_used(db, token):
    return db._query("SELECT used_at FROM af3_actions WHERE token=?", (token,))[0]["used_at"]


def _all_html_ok(tr):
    for m in tr.sent + tr.edits:
        assert SafeHtmlFormatter().finalize(m["text"]) == m["text"]
        assert SafeHtmlFormatter.visible_len(m["text"]) <= 1800 and len(m["text"]) <= 3500
        for d in _cbs(m):
            assert len(d.encode("ascii")) <= 64


class TestAf3Protocol:
    def test_af2_regexes_untouched_and_af3_grammar(self):
        assert CALLBACK_RE.pattern == r"^af2\|([scdrRmMxXpqez])\|([A-Za-z0-9_-]{22})$"
        assert STATELESS_TASK_CB_RE.pattern == r"^af2\|([vl])\|([A-Za-z0-9][A-Za-z0-9._-]{0,57})$"
        assert AF3_CALLBACK_RE.pattern == r"^af3\|([A-Za-z0-9_]{1,8})\|([A-Za-z0-9_-]{1,48})$"
        assert validate_callback_data("af3|nav|hub") and validate_callback_data("af3|TC|" + "A" * 22)
        for bad in ("af3|nav|", "af3||hub", "af3|toolong12|x", "af3|nav|a.b", "af3|nav|" + "a" * 49, "af3|n v|x"):
            with pytest.raises(ValueError):
                validate_callback_data(bad)
        assert af3_button("x", "tc", "t.dotted") is None and af3_button("x", "tc", "t" * 49) is None

    def test_af3_token_single_use_identity_expiry(self, tmp_path):
        clock = Clock()
        d = CoordinatorDB(tmp_path / "a.db", clock=clock)
        try:
            tok = d.issue_af3_token("PZ", ADMIN, ADMIN, {"action": "pause"}, ttl=60)
            assert len(tok) == 22 and AF3_CALLBACK_RE.match(f"af3|PZ|{tok}")
            for args, code in (((tok, "RS", ADMIN, ADMIN), "OP_MISMATCH"), ((tok, "PZ", ADMIN, OTHER), "IDENTITY_MISMATCH"),
                               (("nope", "PZ", ADMIN, ADMIN), "UNKNOWN_TOKEN")):
                with pytest.raises(ActionRejected) as ei:
                    d.consume_af3_token(*args)
                assert ei.value.code == code
            assert _af3_used(d, tok) is None

            def boom(c, payload):
                raise ActionRejected("STALE")

            with pytest.raises(ActionRejected):
                d.consume_af3_token(tok, "PZ", ADMIN, ADMIN, boom)
            assert _af3_used(d, tok) is None  # effect failure rolls the consume back
            assert d.consume_af3_token(tok, "PZ", ADMIN, ADMIN)[0] == {"action": "pause"}
            with pytest.raises(ActionRejected) as ei:
                d.consume_af3_token(tok, "PZ", ADMIN, ADMIN)
            assert ei.value.code == "REPLAY"
            late = d.issue_af3_token("RS", ADMIN, ADMIN, ttl=60)
            clock.t += 61
            with pytest.raises(ActionRejected) as ei:
                d.consume_af3_token(late, "RS", ADMIN, ADMIN)
            assert ei.value.code == "EXPIRED"
            with pytest.raises(ValueError):
                d.issue_af3_token("bad|op", ADMIN, ADMIN)
        finally:
            d.close()


class TestMainHub:
    def test_start_and_menu_render_hub(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        for cmd in ("/start", "/menu"):
            assert bot.handle_update(upd_msg(cmd)) == "cmd:menu"
            kb = tr.sent[-1]["reply_markup"]["inline_keyboard"]
            assert [b["callback_data"] for b in kb[0]] == ["af3|nav|tasks", "af3|nav|sess"]
            assert [b["callback_data"] for b in kb[1]] == ["af3|nav|bots", "af3|pz|all", "af3|nav|help"]
            assert "⏸️ 전체 일시정지" in [b["text"] for b in kb[1]]
        assert bot.handle_update(upd_msg("/help")) == "cmd:help" and tr.sent[-1]["text"] == dev_bot_mod.HELP_TEXT
        h = dev_bot_mod.HELP_TEXT
        assert len(h) < 800 and "/menu" in h and "/sessions" in h
        _all_html_ok(tr)

    def test_two_step_pause_and_resume(self, tmp_path, repo, db):
        wakes = []
        bot, tr = make_bot(tmp_path, repo, db, on_wake=lambda: wakes.append(1))
        bot.handle_update(upd_msg("/menu"))
        assert bot.handle_update(upd_raw("af3|pz|all", message_id=77)) == "callback:af3:pause_prompt:edited"
        assert not bot.stop_flag.exists()  # step 1 never writes STOP.flag
        prompt = tr.edits[-1]
        assert prompt["message_id"] == 77 and "af3|nav|hub" in _cbs(prompt)
        assert "⚠️ 정말 일시정지할까요?" in [b["text"] for row in prompt["reply_markup"]["inline_keyboard"] for b in row]
        pz = _af3_tok(prompt, "PZ")
        assert bot.handle_update(upd_raw(f"af3|PZ|{pz}", message_id=77)) == "callback:af3:paused"
        assert bot.stop_flag.exists()
        hub = tr.edits[-1]
        assert "개발봇 일시정지됨" in hub["text"] and hub["reply_markup"]["inline_keyboard"][0][0]["text"] == "▶️ 지금 재개하기"
        assert "af3|pz|all" not in _cbs(hub)
        assert bot.handle_update(upd_raw(f"af3|PZ|{pz}")) == "callback:af3:rejected:REPLAY"
        rs = _af3_tok(hub, "RS")
        assert bot.handle_update(upd_raw(f"af3|RS|{rs}", message_id=77)) == "callback:af3:resumed"
        assert not bot.stop_flag.exists() and wakes == [1]
        assert "개발봇 일시정지됨" not in tr.edits[-1]["text"]
        _all_html_ok(tr)

    def test_status_banner_one_click_resume(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        assert bot.handle_update(upd_msg("/kill")) == "cmd:kill"
        assert bot.handle_update(upd_msg("/status")) == "cmd:status"
        msg = tr.sent[-1]
        assert msg["text"].startswith("⏸️ <b>개발봇 일시정지됨</b>") and "STOP.flag: <b>ON</b>" in msg["text"]
        assert msg["reply_markup"]["inline_keyboard"][0][0]["text"] == "▶️ 지금 재개하기"
        assert bot.handle_update(upd_raw(f"af3|RS|{_af3_tok(msg, 'RS')}")) == "callback:af3:resumed"
        assert not bot.stop_flag.exists()
        bot.handle_update(upd_msg("/status"))
        assert tr.sent[-1]["reply_markup"] is None and "개발봇 일시정지됨" not in tr.sent[-1]["text"]

    def test_af3_ack_first_unauthorized_and_ack_failure(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        tok = db.issue_af3_token("PZ", ADMIN, ADMIN, {"action": "pause"})
        assert bot.handle_update(upd_raw(f"af3|PZ|{tok}", frm=OTHER)) == "ignored:unauthorized"
        assert bot.handle_update(upd_raw(f"af3|PZ|{tok}", chat=OTHER)) == "ignored:unauthorized"
        assert tr.answers == [] and tr.edits == [] and not bot.stop_flag.exists()
        tr.answer_script = [False]
        assert bot.handle_update(upd_raw(f"af3|PZ|{tok}")) == "callback:answer_failed"
        tr.answer_script = [TgUnknown("timeout")]
        assert bot.handle_update(upd_raw(f"af3|PZ|{tok}")) == "callback:answer_failed"
        assert not bot.stop_flag.exists() and _af3_used(db, tok) is None and tr.edits == []
        order = []
        real_answer = tr.answer_callback_query
        bot.limiter.wait = lambda chat=None: order.append(("wait", chat)) or 0.0
        tr.answer_callback_query = lambda *a, **k: order.append("answer") or real_answer(*a, **k)
        assert bot.handle_update(upd_raw(f"af3|PZ|{tok}")) == "callback:af3:paused"
        assert order[0] == "answer" and ("wait", None) not in order  # af3 ACK never waits on the limiter
        forged = db.issue_af3_token("RS", ADMIN, OTHER)
        assert bot.handle_update(upd_raw(f"af3|RS|{forged}")) == "callback:af3:rejected:IDENTITY_MISMATCH"
        assert bot.stop_flag.exists()
        assert bot.handle_update(upd_raw("af3|RS|" + "A" * 22)) == "callback:af3:rejected:UNKNOWN_TOKEN"
        assert bot.handle_update(upd_raw("af3|nav|nowhere")) == "callback:malformed"
        assert bot.handle_update(upd_raw("af3|zz|x")) == "callback:malformed"

    def test_nav_views_edit_in_place(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db, process_inspector=_FakeInspector([]), session_sleep=lambda s: None)
        tid = submit(db, start=False)
        _implementing(db, tid)
        assert bot.handle_update(upd_raw("af3|nav|tasks", message_id=9)) == "callback:af3:tasks:edited"
        view = tr.edits[-1]
        assert tid in view["text"] and f"af2|v|{tid}" in _cbs(view) and "af3|nav|hub" in _cbs(view)
        assert bot.handle_update(upd_raw(f"af2|v|{tid}", message_id=9)) == "callback:live:edited"
        assert f"af3|tc|{tid}" in _cbs(tr.edits[-1])
        assert bot.handle_update(upd_raw("af3|nav|help", message_id=9)) == "callback:af3:help:edited"
        assert tr.edits[-1]["text"] == dev_bot_mod.HELP_TEXT
        assert bot.handle_update(upd_raw("af3|nav|sess", message_id=9)) == "callback:af3:sessions:edited"
        assert bot.handle_update(upd_raw("af3|nav|hub", message_id=9)) == "callback:af3:hub:edited"
        assert bot.handle_update(upd_raw("af3|nav|bots")) == "callback:af3:bots"
        _all_html_ok(tr)


class TestStatusBadges:
    def test_badge_translation(self):
        assert status_badge("CANCELLED", "FAILED")[:2] == ("⚪", "취소됨")
        assert status_badge("FAILED", "FAILED")[:2] == ("❌", "실패")
        assert status_badge("MERGED", "DONE")[:2] == ("✅", "완료")
        assert status_badge("CANCEL_REQUESTED", "ACTIVE")[:2] == ("⛔", "중지 요청됨")
        assert status_badge("IMPLEMENTING", "ACTIVE")[:2] == ("\U0001f7e2", "작업 중")
        assert status_badge("IMPLEMENTING", "QUIET")[:2] == ("\U0001f7e1", "응답 대기 중")
        assert status_badge("VERIFYING", "STALLED")[:2] == ("\U0001f534", "멈춤 의심")
        assert status_badge("AWAITING_START_AUTH", "WAITING_HUMAN")[:2] == ("\U0001f7e3", "승인 대기")
        assert status_badge("RECEIVED", "QUEUED")[1] == "대기열"
        for state in ("CANCELLED", "FAILED", "IMPLEMENTING", "AWAITING_MERGE"):
            assert status_badge(state)[2]  # always a one-line explanation

    def test_cancelled_is_not_rendered_as_failure(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        tid = submit(db, start=False)
        _advance(db, tid, ["CANCELLED"])
        bot.handle_update(upd_msg(f"/status {tid}"))
        text = tr.sent[-1]["text"]
        assert "⚪ <b>취소됨</b>" in text and "❌" not in text and "실패 아님" in text
        assert "af3|tc|" not in "".join(_cbs(tr.sent[-1]))
        tid2 = submit(db, request="other", start=False)
        _advance(db, tid2, ["FAILED"])
        bot.handle_update(upd_msg(f"/live {tid2}"))
        assert "❌ <b>실패</b>" in tr.sent[-1]["text"] and "취소됨" not in tr.sent[-1]["text"]

    def test_live_card_badges_and_gated_buttons_unchanged(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        tid = submit(db, start=False)
        _implementing(db, tid)
        bot.handle_update(upd_msg(f"/live {tid}"))
        msg = tr.sent[-1]
        assert "\U0001f534 <b>멈춤 의심</b>" in msg["text"] and "단계: 코드 작성" in msg["text"]
        assert _cbs(msg) == [f"af2|v|{tid}", f"af2|l|{tid}", f"af3|tc|{tid}"]
        assert "\U0001f6d1 이 작업 중지" in msg["reply_markup"]["inline_keyboard"][1][0]["text"]
        bot.handle_update(upd_msg("/status"))
        assert "\U0001f534 멈춤 의심" in tr.sent[-1]["text"]
        # human-gated cards keep exactly their af2 workflow buttons (no 🛑 row; run_demo checks SPRR {d, r, x})
        sp = submit(db, request="gated", start=False)
        _fake_awaiting_merge(db, sp)
        bot.handle_update(upd_msg(f"/status {sp}"))
        assert all(d.startswith("af2|") for d in _cbs(tr.sent[-1])) and set(tr.buttons()) == {"m", "x"}
        _all_html_ok(tr)


class TestTaskCancel:
    def test_fsm_edges(self):
        assert _TRANSITIONS["CANCEL_REQUESTED"] == frozenset({"CANCELLED", "FAILED", "BLOCKED"})
        for s in ("RECEIVED", "PLANNING", "AWAITING_START_AUTH", "WORKTREE_READY", "IMPLEMENTING", "CHECKPOINTED",
                  "VERIFYING", "REVIEWING", "RETRY_WAIT", "RECOVERY_PENDING", "BLOCKED", "GUARDRAIL_BLOCKED",
                  "SINGLE_PROVIDER_REVIEW_REQUIRED", "AWAITING_MERGE"):
            assert "CANCEL_REQUESTED" in _TRANSITIONS[s], s
        for s in ("MERGED", "FAILED", "DISCARDED", "CANCELLED"):
            assert "CANCEL_REQUESTED" not in _TRANSITIONS[s]

    def _prompt(self, bot, tr, tid):
        assert bot.handle_update(upd_raw(f"af3|tc|{tid}", message_id=5)) == "callback:af3:cancel_prompt:edited"
        prompt = tr.edits[-1]
        assert "⚠️ 정말 이 작업을 중지할까요?" in [b["text"] for r in prompt["reply_markup"]["inline_keyboard"] for b in r]
        assert f"af2|v|{tid}" in _cbs(prompt)  # [취소] goes back to the live card
        return _af3_tok(prompt, "TC")

    def test_parked_task_cancels_directly_and_replay_rejected(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        tid = submit(db, start=False)
        _to_state(db, tid, "AWAITING_START_AUTH")
        v = db.get_task(tid)["state_version"]
        tok = self._prompt(bot, tr, tid)
        assert db.get_task(tid)["state_version"] == v  # step 1 never mutates the task
        assert bot.handle_update(upd_raw(f"af3|TC|{tok}", message_id=5)) == "callback:af3:cancelled"
        assert db.get_task(tid)["state"] == "CANCELLED" and "취소됨" in tr.edits[-1]["text"]
        assert bot.handle_update(upd_raw(f"af3|TC|{tok}")) == "callback:af3:rejected:REPLAY"
        assert bot.handle_update(upd_raw(f"af3|tc|{tid}")) == "callback:af3:cancel:not_cancellable"

    def test_active_or_leased_task_only_requests_cancel(self, tmp_path, repo, db):
        wakes = []
        bot, tr = make_bot(tmp_path, repo, db, on_wake=lambda: wakes.append(1))
        tid = submit(db, start=False)
        _implementing(db, tid)
        tok = self._prompt(bot, tr, tid)
        assert bot.handle_update(upd_raw(f"af3|TC|{tok}")) == "callback:af3:cancel_requested"
        assert db.get_task(tid)["state"] == "CANCEL_REQUESTED" and wakes == [1]
        assert f"af3|tc|{tid}" not in _cbs(tr.edits[-1]) and "⛔" in tr.edits[-1]["text"]
        # a parked state whose lease is held: CANCEL_REQUESTED only, the lease row is never touched
        tid2 = submit(db, request="leased", start=False)
        _to_state(db, tid2, "AWAITING_START_AUTH")
        assert db.acquire_lease(tid2, "worker-nonce", 1, 1.0, ttl=600)
        tok2 = self._prompt(bot, tr, tid2)
        assert bot.handle_update(upd_raw(f"af3|TC|{tok2}")) == "callback:af3:cancel_requested"
        assert db.get_task(tid2)["state"] == "CANCEL_REQUESTED"
        assert db.get_lease(tid2)["owner_nonce"] == "worker-nonce"

    def test_stale_confirm_rolls_back(self, tmp_path, repo, db):
        bot, tr = make_bot(tmp_path, repo, db)
        tid = submit(db, start=False)
        _to_state(db, tid, "AWAITING_START_AUTH")
        tok = self._prompt(bot, tr, tid)
        db.add_approval(tid, "start")
        _advance(db, tid, ["WORKTREE_READY"])  # the task moved after the prompt was shown
        assert bot.handle_update(upd_raw(f"af3|TC|{tok}")) == "callback:af3:rejected:STALE"
        assert db.get_task(tid)["state"] == "WORKTREE_READY" and _af3_used(db, tok) is None

    def test_worker_finalizes_active_cancel_without_failover(self, tmp_path, repo, db):
        tid = submit(db)

        def cancelled_mid_run(wt, contract, role):
            Path(wt, "calc.py").write_text("def add(a, b):\n    return a + b  # partial\n", encoding="utf-8")
            t = db.get_task(tid)
            db.transition(tid, t["state_version"], "CANCEL_REQUESTED", "human:tg:1", "cancel via test")
            return _AdapterResult("claude", role, FailureCode.TIMEOUT, stderr="killed")  # looks like a timeout

        w = make_worker(tmp_path, repo, db, claude=[cancelled_mid_run])
        assert w.process_task(tid) == "CANCELLED"
        task = db.get_task(tid)
        assert task["state"] == "CANCELLED" and task["last_error"] is None and task["checkpoint_sha"]
        assert w.adapters["codex"].calls == [] and w.adapters["gemini"].calls == []  # no failover
        writer = db.attempts(tid, "writer")
        assert len(writer) == 1 and writer[0]["failure_code"] == "CANCELLED"  # never recorded as TIMEOUT
        assert db.attempts(tid, "verify") == [] and db.attempts(tid, "reviewer") == []
        assert [e["to_state"] for e in db.events(tid)][-2:] == ["CANCEL_REQUESTED", "CANCELLED"]
        assert db.get_lease(tid) is None
        show = subprocess.run(["git", "show", task["checkpoint_sha"], "--", "calc.py"], cwd=task["worktree_path"],
                              capture_output=True, text=True, env=build_sandbox_env())
        assert "# partial" in show.stdout  # partial work preserved as a WIP commit
        assert not db.circuit_is_open(CIRCUIT)

    def test_cancel_racing_a_successful_writer_wins(self, tmp_path, repo, db):
        tid = submit(db)

        def ok_then_cancel(wt, contract, role):
            Path(wt, "calc.py").write_text(FIX, encoding="utf-8")
            t = db.get_task(tid)
            db.transition(tid, t["state_version"], "CANCEL_REQUESTED", "human:tg:1")

        w = make_worker(tmp_path, repo, db, claude=[ok_then_cancel])
        assert w.process_task(tid) == "CANCELLED"
        assert "CHECKPOINTED" not in [e["to_state"] for e in db.events(tid)]

    def test_orphaned_cancel_request_is_finalized(self, tmp_path, repo, db):
        tid = submit(db, start=False)
        _implementing(db, tid)
        t = db.get_task(tid)
        db.transition(tid, t["state_version"], "CANCEL_REQUESTED", "human:tg:1")
        w = make_worker(tmp_path, repo, db)
        assert tid in [x["id"] for x in w._runnable()]
        summary = w.run_cycle()
        assert (tid, "CANCELLED") in summary["processed"] and db.get_task(tid)["state"] == "CANCELLED"
        assert w.adapters["claude"].calls == []

    def test_job_runner_cancel_kind(self):
        res = JobRunner(poll_interval=0.02).run(_py("import time; time.sleep(60)"), wall_timeout=60, idle_timeout=60,
                                                progress_sink=ProgressSink(cancel_signal=lambda: True))
        assert res.timeout_kind == "cancel" and res.duration < 20

    def test_lease_keeper_latches_cancel(self, tmp_path):
        clock = Clock(1_000_000.0)
        d = CoordinatorDB(tmp_path / "lk.db", clock=clock)
        try:
            tid, _ = d.create_task("r", "L1", ["calc.py"], {})
            assert d.acquire_lease(tid, "owner-nonce", os.getpid(), process_start_time(os.getpid()), ttl=60)
            sink = LiveProgressSink(tmp_path / "artifacts", tid, clock=clock)
            sink.begin_run("RECEIVED")
            k = LeaseKeeper(d.path, tid, "owner-nonce", 60.0, clock=clock, sink=sink)
            assert k.tick() is True and not k.is_cancel_requested() and not sink.is_cancel_requested()
            d.transition(tid, 0, "CANCEL_REQUESTED", "human:tg:1")
            assert cancel_requested_nowait(d.path, tid) is True
            assert k.tick() is True and k.is_cancel_requested() and sink.is_cancel_requested()
            assert not k.is_lease_lost() and d.get_lease(tid)["owner_nonce"] == "owner-nonce"
            assert any("event=CANCEL_REQUESTED" in ln for ln in read_live_log_tail(tmp_path / "artifacts", tid, 20))
        finally:
            d.close()
        missing = tmp_path / "nope" / "c.db"
        with pytest.raises(_sqlite3.OperationalError):
            cancel_requested_nowait(missing, "t1")
        assert not missing.exists()


# --------------------------------------------------------------------- sessions
class _FakeInspector:
    def __init__(self, procs):
        self.procs = list(procs)
        self.killed: list[tuple] = []
        self.result = "TERMINATED"

    def snapshot(self):
        return list(self.procs)

    def terminate(self, pid, creation_time, image_name):
        self.killed.append((pid, creation_time, image_name))
        return self.result


def _proc(pid, img="codex.exe", ct=1234.5, cpu=1.0, accessible=True, can_terminate=True, ancestry=()):
    return ProcessSample(pid, img, ct if accessible else None, cpu if accessible else None, accessible,
                         can_terminate, tuple(ancestry))


class TestSessions:
    def _setup(self, tmp_path, repo, db, procs, recorded_pid=5000, recorded_ct=1234.5):
        now = [100.0]
        insp = _FakeInspector(procs)
        sleeps = []

        def sleep(s):
            sleeps.append(s)
            now[0] += s

        bot, tr = make_bot(tmp_path, repo, db, process_inspector=insp, session_clock=lambda: now[0],
                           session_sleep=sleep, session_quiet_window=120.0)
        tid = submit(db, start=False)
        _implementing(db, tid)
        assert db.acquire_lease(tid, "worker-nonce", 4242, 1.0, ttl=3600)
        sink = LiveProgressSink(tmp_path / "artifacts", tid, clock=db.clock)
        sink.begin_run("IMPLEMENTING")
        sink.start(recorded_pid, recorded_ct, "implement", "codex", "writer")
        return bot, tr, insp, now, sleeps, tid

    def test_whitelist_classification_and_readonly(self, tmp_path, repo, db):
        procs = [_proc(5000), _proc(7000, "claude.exe", ct=50.0), _proc(8000, "claude.exe", accessible=False),
                 _proc(9000, "node.exe")]
        bot, tr, insp, now, sleeps, tid = self._setup(tmp_path, repo, db, procs)
        assert bot.handle_update(upd_msg("/sessions")) == "cmd:sessions"
        text = tr.sent[-1]["text"]
        assert sleeps == [1.0]  # first sighting: one short 2-sample window, never "stalled" from it
        assert "node.exe" not in text and "PID 9000" not in text
        assert "codex.exe (PID 5000)" in text and "\U0001f7e1 조용함" in text and "\U0001f534" not in text.split("\n", 2)[2]
        assert "claude.exe (PID 8000)" in text and "⚪ 읽기 전용 · 종료 불가" in text
        assert f"작업 <code>{tid}</code>" in text and "외부 세션 · 종료 불가" in text
        assert not any(d.startswith("af3|sk|") for d in _cbs(tr.sent[-1]))
        insp.procs = [_proc(5000, cpu=5.0), _proc(7000, "claude.exe", ct=50.0)]
        now[0] += 10
        bot.handle_update(upd_msg("/sessions"))
        text = tr.sent[-1]["text"]
        assert "\U0001f7e2 활동 중" in text and sleeps == [1.0]  # cached delta: no extra sleep
        now[0] += 130  # 130 s with zero CPU-time delta
        bot.handle_update(upd_msg("/sessions"))
        msg = tr.sent[-1]
        assert msg["text"].count("\U0001f534 무응답 멈춤 의심") == 2
        sk = [d for d in _cbs(msg) if d.startswith("af3|sk|")]
        assert len(sk) == 1  # only the owned + terminable one (PID 7000 is an external session)
        for secret in ("--", str(repo), "worker-nonce", str(tmp_path)):
            assert secret not in msg["text"]
        _all_html_ok(tr)

    def _stalled(self, bot, tr, now):
        bot.handle_update(upd_msg("/sessions"))
        now[0] += 130
        bot.handle_update(upd_msg("/sessions"))
        return _af3_tok(tr.sent[-1], "sk")

    def test_two_step_kill_and_replay(self, tmp_path, repo, db):
        bot, tr, insp, now, _, tid = self._setup(tmp_path, repo, db, [_proc(5000)])
        sk = self._stalled(bot, tr, now)
        assert bot.handle_update(upd_raw(f"af3|sk|{sk}", message_id=3)) == "callback:af3:kill_prompt:edited"
        assert insp.killed == []  # step 1 never terminates
        prompt = tr.edits[-1]
        assert "⚠️ 정말 종료할까요?" in prompt["text"] and "트리 종료 없음" in prompt["text"]
        SK = _af3_tok(prompt, "SK")
        assert bot.handle_update(upd_raw(f"af3|SK|{SK}", message_id=3)) == "callback:af3:kill:TERMINATED"
        assert insp.killed == [(5000, 1234.5, "codex.exe")]
        assert "\U0001f6d1 종료 완료" in tr.edits[-1]["text"]
        assert bot.handle_update(upd_raw(f"af3|SK|{SK}")) == "callback:af3:rejected:REPLAY"
        assert bot.handle_update(upd_raw(f"af3|sk|{sk}")) == "callback:af3:rejected:REPLAY"
        assert len(insp.killed) == 1 and db.get_task(tid)["state"] == "IMPLEMENTING"
        _all_html_ok(tr)

    def test_pid_reuse_and_not_stalled_and_not_owned_rejected(self, tmp_path, repo, db):
        bot, tr, insp, now, _, tid = self._setup(tmp_path, repo, db, [_proc(5000)])
        sk = self._stalled(bot, tr, now)
        bot.handle_update(upd_raw(f"af3|sk|{sk}"))
        SK = _af3_tok(tr.edits[-1], "SK")
        insp.procs = [_proc(5000, ct=9999.0)]  # same PID, different creation time => reused PID
        assert bot.handle_update(upd_raw(f"af3|SK|{SK}")) == "callback:af3:kill:refused:GONE_OR_REUSED"
        payload = {"pid": 5000, "ct": 1234.5, "img": "codex.exe", "tag": tid}
        insp.procs = [_proc(5000, cpu=99.0)]  # CPU moved: no longer stalled
        tok = db.issue_af3_token("SK", ADMIN, ADMIN, payload)
        assert bot.handle_update(upd_raw(f"af3|SK|{tok}")) == "callback:af3:kill:refused:NOT_STALLED"
        insp.procs = [_proc(5000, cpu=99.0, can_terminate=False)]
        tok = db.issue_af3_token("SK", ADMIN, ADMIN, payload)
        assert bot.handle_update(upd_raw(f"af3|SK|{tok}")) == "callback:af3:kill:refused:NOT_PERMITTED"
        insp.procs = [_proc(5000, cpu=99.0)]
        now[0] += 500
        db.force_release_lease(tid)  # ownership can no longer be proven
        tok = db.issue_af3_token("SK", ADMIN, ADMIN, payload)
        assert bot.handle_update(upd_raw(f"af3|SK|{tok}")) == "callback:af3:kill:refused:NOT_OWNED"
        tok = db.issue_af3_token("SK", ADMIN, ADMIN, dict(payload, img="python.exe"))
        assert bot.handle_update(upd_raw(f"af3|SK|{tok}")) == "callback:af3:kill:refused:BAD_PAYLOAD"
        assert insp.killed == []

    def test_unprovable_or_inaccessible_never_offered_kill(self, tmp_path, repo, db):
        procs = [_proc(6000, "claude.exe", ct=2000.0, can_terminate=False, ancestry=[(5000, 1234.5)]),
                 _proc(6100, "claude.exe", ct=2000.0)]
        bot, tr, insp, now, _, tid = self._setup(tmp_path, repo, db, procs)
        bot.handle_update(upd_msg("/sessions"))
        now[0] += 130
        bot.handle_update(upd_msg("/sessions"))
        text = tr.sent[-1]["text"]
        assert f"작업 <code>{tid}</code>" in text  # owned via verified shim ancestry
        assert "권한 없음 · 종료 불가" in text and "외부 세션 · 종료 불가" in text
        assert not any(d.startswith("af3|sk|") for d in _cbs(tr.sent[-1]))

    def test_owned_via_shim_ancestry_can_be_killed(self, tmp_path, repo, db):
        procs = [_proc(6000, "claude.exe", ct=2000.0, ancestry=[(5000, 1234.5)])]
        bot, tr, insp, now, _, tid = self._setup(tmp_path, repo, db, procs)
        sk = self._stalled(bot, tr, now)
        bot.handle_update(upd_raw(f"af3|sk|{sk}"))
        assert bot.handle_update(upd_raw(f"af3|SK|{_af3_tok(tr.edits[-1], 'SK')}")) == "callback:af3:kill:TERMINATED"
        assert insp.killed == [(6000, 2000.0, "claude.exe")]

    def test_real_inspector_is_read_only_and_refuses_non_whitelisted(self):
        samples = ProcessInspector().snapshot()
        assert all(isinstance(s, ProcessSample) and s.image_name in ("codex.exe", "claude.exe") for s in samples)
        me, ct = os.getpid(), process_start_time(os.getpid())
        if IS_WINDOWS:
            assert terminate_verified(me, ct, "python.exe") == "MISMATCH"  # not whitelisted: never touched
            assert terminate_verified(me, ct, "codex.exe") == "MISMATCH"  # image differs: never touched
            assert terminate_verified(me, (ct or 0) + 5, "codex.exe") == "MISMATCH"
        else:
            assert terminate_verified(me, ct, "codex.exe") == "UNSUPPORTED"
        assert process_start_time(me) == ct  # we are still alive


# ######################################################################## MULTI-PROJECT (Phase A)
import json as _pjson
import types as _ptypes

import auto_factory.projects as projects_mod
from auto_factory.db import PHASE_B_PARK_REASON, SCOPE_PARK_REASON, sha256_text
from auto_factory.projects import (
    DEFAULT_PROJECT_ID,
    GLOBAL_PROTECTED_PATHS,
    PROJECTS_FILE_ENV,
    ProjectRegistry,
    ProjectRegistryError,
    ProjectSpec,
    build_project,
    inspect_project_git,
    load_registry,
    scan_workspace_projects,
)


def _git_init(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    return path


def _entry(pid, root, kind="git", **kw):
    d = {"project_id": pid, "label": kw.pop("label", pid), "root_path": str(root), "kind": kind}
    d.update(kw)
    return d


def _write_projects(tmp_path, entries, raw=None, name="projects.json"):
    p = tmp_path / "pcfg" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        p.write_bytes(raw if isinstance(raw, bytes) else raw.encode("utf-8"))
    else:
        p.write_text(_pjson.dumps({"version": 1, "projects": entries}), encoding="utf-8")
    return p


def _penv(path) -> dict[str, str]:
    return {PROJECTS_FILE_ENV: str(path)}


def _spec(pid, root, kind="git", allow_write=True, max_risk="L2", tests=()):
    return ProjectSpec(pid, pid, Path(root).resolve(), kind, tuple(tests), GLOBAL_PROTECTED_PATHS,
                       allow_write if kind == "git" else False, max_risk)


class TestProjectRegistry:
    def test_default_registry_only_home_project(self, tmp_path, repo):
        for state_dir in (None, tmp_path / "state"):
            reg = load_registry(repo, state_dir, env={})
            assert reg.ids() == (DEFAULT_PROJECT_ID,) and reg.source == "default"
            p = reg.get(DEFAULT_PROJECT_ID)
            assert p.kind == "git" and p.allow_write is True and p.writable and p.max_risk == "L2"
            assert p.root_path == repo.resolve()
            assert set(GLOBAL_PROTECTED_PATHS) <= set(p.protected_paths)
        with pytest.raises(Exception):
            p.allow_write = False  # frozen dataclass

    def test_slug_validation(self, tmp_path):
        folder = tmp_path / "f"
        folder.mkdir()
        for bad in ("", "A", "-x", "_x", "a" * 33, "a.b", "한글", "a b", "../x", "a/b", None, 7):
            with pytest.raises(ProjectRegistryError):
                build_project(_entry(bad, folder, kind="folder"))
        assert build_project(_entry("my-proj_1", folder, kind="folder")).project_id == "my-proj_1"
        assert build_project(_entry("a" * 32, folder, kind="folder")).project_id == "a" * 32

    def test_folder_is_always_read_only(self, tmp_path):
        folder = tmp_path / "notes"
        folder.mkdir()
        p = build_project(_entry("notes", folder, kind="folder", allow_write=True, max_risk="L2"))
        assert p.allow_write is False and p.writable is False and p.kind == "folder"
        with pytest.raises(ProjectRegistryError):
            ProjectSpec("notes", "notes", folder.resolve(), "folder", (), GLOBAL_PROTECTED_PATHS, True, "L2")
        with pytest.raises(ProjectRegistryError):  # global policy cannot be dropped
            ProjectSpec("notes", "notes", folder.resolve(), "folder", (), (".env",), False, "L2")

    def test_schema_fields_rejected(self, tmp_path, repo):
        folder = tmp_path / "f"
        folder.mkdir()
        bad_entries = [
            _entry("x", folder, kind="svn"),
            _entry("x", folder, kind="folder", max_risk="L3"),
            _entry("x", folder, kind="folder", allow_write="yes"),
            _entry("x", folder, kind="folder", extra=1),
            {"project_id": "x", "kind": "folder", "root_path": str(folder)},  # label missing
            _entry("x", "relative/path", kind="folder"),
            _entry("x", tmp_path / "missing", kind="folder"),
            _entry("x", folder, kind="folder", label="\x00\x01"),
            _entry("x", folder, kind="folder", label="a" * 65),
        ]
        for tests in (["../x.py"], ["/abs/test.py"], ["C:/x.py"], ["tests/*.py"], [".env"], ["cfg/.env"],
                      ["data/x.sqlite3"], ["data/x.sqlite"], ["tests\\a.py"], ["reports/x.py"], ["holdout_eval/t.py"],
                      "tests/a.py", [""]):
            bad_entries.append(_entry("x", folder, kind="folder", default_tests=tests))
        for prot in (["../up"], ["/abs"], ["a/*"], ["a//b"]):
            bad_entries.append(_entry("x", folder, kind="folder", protected_paths=prot))
        for e in bad_entries:
            with pytest.raises(ProjectRegistryError):
                build_project(e)
        ok = build_project(_entry("x", folder, kind="folder", default_tests=["tests/test_a.py"],
                                  protected_paths=["docs/", "secret.txt"]))
        assert ok.default_tests == ("tests/test_a.py",)
        assert ok.protected_paths == GLOBAL_PROTECTED_PATHS + ("docs/", "secret.txt")

    def test_git_kind_requires_exact_toplevel(self, tmp_path, repo):
        plain = tmp_path / "plain"
        plain.mkdir()
        with pytest.raises(ProjectRegistryError):
            build_project(_entry("plain", plain, kind="git"))
        sub = repo / "sub"
        sub.mkdir()
        with pytest.raises(ProjectRegistryError):  # nested dir of another repo is not its toplevel
            build_project(_entry("sub", sub, kind="git"))
        other = _git_init(tmp_path / "other")
        assert build_project(_entry("other", other, kind="git", allow_write=True)).writable

    def test_overlap_and_duplicates_rejected(self, tmp_path, repo):
        inner = repo / "docs"
        inner.mkdir()
        sibling = tmp_path / "sib"
        sibling.mkdir()
        for entries in (
            [_entry("docs", inner, kind="folder")],  # nested inside the home repo
            [_entry("ws", tmp_path, kind="folder")],  # contains the home repo
            [_entry("sib", sibling, kind="folder"), _entry("sib2", sibling, kind="folder")],  # same root
            [_entry("sib", sibling, kind="folder"), _entry("sib", tmp_path / "pcfg", kind="folder")],
        ):
            (tmp_path / "pcfg").mkdir(exist_ok=True)
            path = _write_projects(tmp_path, entries)
            with pytest.raises(ProjectRegistryError):
                load_registry(repo, env=_penv(path))
        with pytest.raises(ProjectRegistryError):
            ProjectRegistry([_spec("only", sibling, kind="folder")])  # home project is mandatory

    def test_corrupt_or_unreadable_file_fails_closed(self, tmp_path, repo):
        good = [_entry("sib", tmp_path, kind="folder")]
        raws = ["{not json", "\xff", "[]", '{"version": 2, "projects": []}', '{"version": 1, "projects": []}',
                '{"version": 1}', '{"version": 1, "projects": [], "x": 1}', '{"version": 1, "projects": [1]}']
        for raw in raws:
            path = _write_projects(tmp_path, None, raw=raw)
            with pytest.raises(ProjectRegistryError):
                load_registry(repo, env=_penv(path))
        path = _write_projects(tmp_path, None, raw=b"\xff\xfe{}")
        with pytest.raises(ProjectRegistryError):
            load_registry(repo, env=_penv(path))
        with pytest.raises(ProjectRegistryError):  # configured but missing => fail closed (no default)
            load_registry(repo, env=_penv(tmp_path / "nope.json"))
        big = _write_projects(tmp_path, None, raw="{" + " " * (64 * 1024 + 1) + "}")
        with pytest.raises(ProjectRegistryError):
            load_registry(repo, env=_penv(big))
        state = tmp_path / "state"
        state.mkdir()
        (state / "projects.json").write_text("{broken", encoding="utf-8")
        with pytest.raises(ProjectRegistryError):  # present in state_dir => configured => fail closed
            load_registry(repo, state, env={})
        other = tmp_path / "elsewhere"
        other.mkdir()
        path = _write_projects(tmp_path, [_entry(DEFAULT_PROJECT_ID, other, kind="git")])
        with pytest.raises(ProjectRegistryError):  # the home id cannot be re-pointed
            load_registry(repo, env=_penv(path))
        assert good  # keeps flake quiet

    def test_valid_file_env_override_and_home_prepended(self, tmp_path, repo):
        other = _git_init(tmp_path / "ws" / "other")
        notes = tmp_path / "ws" / "notes"
        notes.mkdir()
        path = _write_projects(tmp_path, [
            _entry("other", other, label="Other <b>", allow_write=True, max_risk="L1", default_tests=["t/test_x.py"]),
            _entry("notes", notes, kind="folder", allow_write=True),
        ])
        state = tmp_path / "state"
        state.mkdir()
        (state / "projects.json").write_text("{broken", encoding="utf-8")  # env var wins over state_dir
        reg = load_registry(repo, state, env=_penv(path))
        assert reg.ids() == (DEFAULT_PROJECT_ID, "other", "notes") and reg.source == str(path)
        assert reg.get("other").writable and reg.get("other").max_risk == "L1"
        assert reg.get("notes").allow_write is False and "notes" in reg and "zzz" not in reg
        assert reg.find_by_root(notes) is reg.get("notes") and reg.get(None) is None
        path2 = _write_projects(tmp_path, [_entry(DEFAULT_PROJECT_ID, repo, label="Home"),
                                           _entry("notes", notes, kind="folder")], name="p2.json")
        reg2 = load_registry(repo, env=_penv(path2))
        assert reg2.ids() == (DEFAULT_PROJECT_ID, "notes") and reg2.get(DEFAULT_PROJECT_ID).label == "Home"

    def test_symlink_or_junction_rejected(self, tmp_path, repo):
        real = tmp_path / "real"
        (real / "sub").mkdir(parents=True)
        link = tmp_path / "lnk"
        if not _try_dir_link(link, real):
            pytest.skip("cannot create a directory symlink/junction here")
        for target in (link, link / "sub"):
            with pytest.raises(ProjectRegistryError):
                build_project(_entry("lnk", target, kind="folder"))
        path = _write_projects(tmp_path, [_entry("lnk", link, kind="folder")])
        with pytest.raises(ProjectRegistryError):
            load_registry(repo, env=_penv(path))
        ws = scan_workspace_projects(tmp_path, load_registry(repo, env={}))
        assert [d.kind for d in ws if d.name == "lnk"] == ["link"]

    def test_scan_is_read_only_and_classifies(self, tmp_path, repo):
        ws = tmp_path / "ws"
        _git_init(ws / "gitproj")
        (ws / "plain").mkdir(parents=True)
        (ws / ".hidden").mkdir()
        (ws / "file.txt").write_text("x", encoding="utf-8")
        path = _write_projects(tmp_path, [_entry("plain", ws / "plain", kind="folder")])
        before = (path.read_bytes(), path.stat().st_mtime_ns, sorted(os.listdir(ws)))
        reg = load_registry(repo, env=_penv(path))
        found = {d.name: d for d in scan_workspace_projects(ws, reg)}
        assert set(found) == {"gitproj", "plain"}
        assert found["gitproj"].kind == "git" and found["gitproj"].registered_id is None
        assert found["gitproj"].status_ko == "미등록(읽기 전용)"
        assert found["plain"].kind == "folder" and found["plain"].registered_id == "plain"
        assert found["plain"].status_ko == "등록됨"
        assert (path.read_bytes(), path.stat().st_mtime_ns, sorted(os.listdir(ws))) == before
        assert scan_workspace_projects(tmp_path / "missing", reg) == []

    def test_inspect_project_git_read_only_and_never_raises(self, tmp_path, repo, monkeypatch):
        p = build_project(_entry("home", repo, kind="git", allow_write=True))
        info = inspect_project_git(p)
        assert info["ok"] and info["dirty"] == 0 and info["last_subject"] and info["last_commit_ts"] > 0
        (repo / "new_file.txt").write_text("x", encoding="utf-8")
        assert inspect_project_git(p)["dirty"] == 1
        assert not (repo / ".git" / "index.lock").exists()
        folder = tmp_path / "f"
        folder.mkdir()
        fi = inspect_project_git(build_project(_entry("f", folder, kind="folder")))
        assert fi["ok"] and fi["kind"] == "folder" and fi["dirty"] is None

        def timeout(*a, **k):
            raise subprocess.TimeoutExpired("git", 5)

        monkeypatch.setattr(projects_mod, "_git", timeout)
        bad = inspect_project_git(p)
        assert bad["ok"] is False and bad["error"] == "git timeout"
        monkeypatch.setattr(projects_mod, "_git", lambda *a, **k: (_ for _ in ()).throw(OSError("x")))
        assert inspect_project_git(p)["error"].startswith("git error")


class TestProjectDB:
    def test_project_id_migration_is_idempotent(self, tmp_path):
        path = tmp_path / "legacy.db"
        legacy_ddl = db_mod._SCHEMA.split("CREATE TABLE IF NOT EXISTS attempts")[0]
        con = _sqlite3.connect(str(path))
        con.executescript(legacy_ddl)
        con.execute("INSERT INTO tasks (id, request_hash, request, risk_level, state, allowed_paths, acceptance_spec,"
                    " created_at, updated_at) VALUES ('told', 'h', 'r', 'L1', 'RECEIVED', '[]', '{}', 1, 1)")
        con.commit()
        con.close()
        for _ in range(2):
            d = CoordinatorDB(path)
            try:
                cols = [r["name"] for r in d._query("PRAGMA table_info(tasks)")]
                assert cols.count("project_id") == 1
                assert d.get_task("told")["project_id"] == DEFAULT_PROJECT_ID
                idx = {r["name"] for r in d._query("PRAGMA index_list(tasks)")}
                assert "idx_tasks_project_state" in idx
                bidx = {r["name"] for r in d._query("PRAGMA index_list(backlog_items)")}
                assert "idx_backlog_project" in bidx
            finally:
                d.close()

    def test_create_task_backward_compatible_and_project_isolated(self, db):
        spec = {"pytest": ["tests/test_calc.py"]}
        tid, created = db.create_task("fix add", "L1", ["calc.py"], spec, None, None)  # legacy positional call
        canonical = _json.dumps(["fix add", "L1", ["calc.py"], spec], sort_keys=True)
        assert created and tid == f"t{sha256_text(canonical)[:12]}"  # legacy hash unchanged
        assert db.get_task(tid)["project_id"] == DEFAULT_PROJECT_ID
        assert db.create_task("fix add", "L1", ["calc.py"], spec) == (tid, False)
        assert db.create_task("fix add", "L1", ["calc.py"], spec, project_id=DEFAULT_PROJECT_ID) == (tid, False)
        other, created = db.create_task("fix add", "L1", ["calc.py"], spec, project_id="other")
        assert created and other != tid and db.get_task(other)["project_id"] == "other"
        assert db.create_task("fix add", "L1", ["calc.py"], spec, project_id="other") == (other, False)
        k1, _ = db.create_task("a", "L1", ["x.py"], {}, "same-key")
        k2, c2 = db.create_task("a", "L1", ["x.py"], {}, "same-key", project_id="other")
        assert c2 and k1 != k2
        for bad in ("", "Bad", "../x", "a" * 33):
            with pytest.raises(ValueError):
                db.create_task("z", "L1", ["x.py"], {}, project_id=bad)
        with pytest.raises(TypeError):  # project_id is keyword-only
            db.create_task("z", "L1", ["x.py"], {}, None, None, "other")
        assert db.open_task_counts_by_project() == {DEFAULT_PROJECT_ID: 2, "other": 2}

    def test_backlog_crud(self, db):
        a = db.add_backlog_item("proj", "  first \x07 item ", "desc\r\nline", priority=3)
        b = db.add_backlog_item("proj", "urgent", priority=0)
        c = db.add_backlog_item("other", "elsewhere")
        assert db.get_backlog_item(a)["title"] == "first  item" and db.get_backlog_item(a)["description"] == "desc\nline"
        assert [i["id"] for i in db.list_backlog_items("proj")] == [b, a]
        assert [i["id"] for i in db.list_backlog_items()] == [b, c, a]
        assert all(i["status"] == "TODO" and i["linked_task_id"] is None for i in db.list_backlog_items())
        assert db.set_backlog_status(a, "BLOCKED") and db.get_backlog_item(a)["status"] == "BLOCKED"
        assert not db.set_backlog_status(a, "DONE", expected_status="TODO")  # CAS miss
        assert db.set_backlog_status(a, "DONE", expected_status="BLOCKED")
        assert [i["id"] for i in db.list_backlog_items("proj", "DONE")] == [a]
        assert [i["id"] for i in db.list_backlog_items("proj", ("TODO", "BLOCKED"))] == [b]
        assert db.count_backlog_items("proj") == 2 and db.count_backlog_items("proj", "TODO") == 1
        assert not db.set_backlog_status(999, "DONE")
        for bad_call in (lambda: db.set_backlog_status(a, "NOPE"), lambda: db.set_backlog_status(a, "IN_PROGRESS"),
                         lambda: db.add_backlog_item("proj", "  "), lambda: db.add_backlog_item("proj", "x" * 201),
                         lambda: db.add_backlog_item("Bad!", "t"), lambda: db.add_backlog_item("proj", "t", priority=10),
                         lambda: db.add_backlog_item("proj", "t", priority=True),
                         lambda: db.list_backlog_items("proj", "NOPE")):
            with pytest.raises(ValueError):
                bad_call()
        with pytest.raises(_sqlite3.IntegrityError):  # CHECK constraint is enforced by SQLite itself
            with db._tx() as con:
                con.execute("UPDATE backlog_items SET status='WAT' WHERE id=?", (b,))
        with pytest.raises(KeyError):
            db.get_backlog_item(12345)

    def test_start_backlog_task_single_tx(self, tmp_path, db):
        home = _spec(DEFAULT_PROJECT_ID, tmp_path, tests=("tests/test_calc.py",))
        item = db.add_backlog_item(DEFAULT_PROJECT_ID, "add feature", "details")
        res = db.start_backlog_task_in_tx(item, home, "human:tg:1", allowed_paths=["calc.py"])
        assert res["state"] == "RECEIVED" and res["parked"] == "" and res["project_id"] == DEFAULT_PROJECT_ID
        t = db.get_task(res["task_id"])
        assert t["project_id"] == DEFAULT_PROJECT_ID and t["risk_level"] == "L2" and t["allowed_paths"] == ["calc.py"]
        assert t["acceptance_spec"]["pytest"] == ["tests/test_calc.py"] and "add feature" in t["request"]
        row = db.get_backlog_item(item)
        assert row["status"] == "IN_PROGRESS" and row["linked_task_id"] == res["task_id"]
        n_tasks = len(db.list_tasks())
        with pytest.raises(ActionRejected) as ei:  # replay / duplicate start
            db.start_backlog_task_in_tx(item, home, "human:tg:1", allowed_paths=["calc.py"])
        assert ei.value.code == "ALREADY_STARTED" and len(db.list_tasks()) == n_tasks
        db.set_backlog_status(item, "TODO")  # even if someone resets the status, the link blocks a restart
        with pytest.raises(ActionRejected):
            db.start_backlog_task_in_tx(item, home, "human:tg:1", allowed_paths=["calc.py"])

        noscope = db.add_backlog_item(DEFAULT_PROJECT_ID, "no scope")
        r2 = db.start_backlog_task_in_tx(noscope, home, "human:tg:1")
        assert r2["state"] == "BLOCKED" and r2["parked"] == SCOPE_PARK_REASON
        assert db.get_task(r2["task_id"])["state"] == "BLOCKED"

        other = _spec("other", tmp_path / "o", max_risk="L1")
        oi = db.add_backlog_item("other", "same title")
        r3 = db.start_backlog_task_in_tx(oi, other, "human:tg:1", allowed_paths=["calc.py"])
        t3 = db.get_task(r3["task_id"])
        assert r3["state"] == "BLOCKED" and r3["parked"] == PHASE_B_PARK_REASON  # never runs in the home repo
        assert t3["project_id"] == "other" and t3["risk_level"] == "L1"
        assert [e["to_state"] for e in db.events(r3["task_id"])] == ["RECEIVED", "BLOCKED"]

    def test_start_backlog_rejections_leave_no_trace(self, tmp_path, db, monkeypatch):
        folder = _spec("notes", tmp_path / "n", kind="folder")
        ro_git = _spec("ro", tmp_path / "r", allow_write=False)
        home = _spec(DEFAULT_PROJECT_ID, tmp_path)
        fi = db.add_backlog_item("notes", "read only")
        ri = db.add_backlog_item("ro", "read only git")
        hi = db.add_backlog_item(DEFAULT_PROJECT_ID, "home item")
        low = _spec(DEFAULT_PROJECT_ID, tmp_path, max_risk="L0")
        cases = [((fi, folder), "PROJECT_READ_ONLY"), ((ri, ro_git), "PROJECT_READ_ONLY"),
                 ((hi, folder), "PROJECT_READ_ONLY"), ((hi, _spec("other", tmp_path / "o")), "PROJECT_MISMATCH"),
                 ((424242, home), "BACKLOG_UNKNOWN"), ((hi, _ptypes.SimpleNamespace(project_id="Bad!")), "BAD_PROJECT")]
        for (iid, proj), code in cases:
            with pytest.raises(ActionRejected) as ei:
                db.start_backlog_task_in_tx(iid, proj, "human:tg:1", allowed_paths=["calc.py"])
            assert ei.value.code == code
        with pytest.raises(ActionRejected) as ei:
            db.start_backlog_task_in_tx(hi, low, "human:tg:1", risk_level="L2", allowed_paths=["calc.py"])
        assert ei.value.code == "RISK_TOO_HIGH"
        boom = RuntimeError("crash mid-tx")
        monkeypatch.setattr(db, "_transition_in_tx", lambda *a, **k: (_ for _ in ()).throw(boom))
        with pytest.raises(RuntimeError):
            db.start_backlog_task_in_tx(hi, home, "human:tg:1")  # parks => transition => crash => rollback
        assert db.list_tasks() == []
        assert all(i["status"] == "TODO" and i["linked_task_id"] is None for i in db.list_backlog_items())


def _proj_bot(tmp_path, repo, db, registry_file=None, **kw):
    env = _penv(registry_file) if registry_file else {}
    kw.setdefault("workspace_dir", tmp_path / "ws")
    (tmp_path / "ws").mkdir(exist_ok=True)
    return make_bot(tmp_path, repo, db, projects_env=env, **kw)


def _many_projects(tmp_path, n_folders):
    ws = tmp_path / "ws"
    entries = []
    for i in range(n_folders):
        (ws / f"f{i}").mkdir(parents=True, exist_ok=True)
        entries.append(_entry(f"f{i}", ws / f"f{i}", kind="folder", label=f"Folder <{i}>"))
    return _write_projects(tmp_path, entries)


def _all_cbs_safe(tr, forbidden_text=()):
    for m in tr.sent + tr.edits:
        for d in _cbs(m):
            assert len(d.encode("ascii")) <= 64 and validate_callback_data(d) == d
            assert not any(t in d for t in forbidden_text)
            assert "\\" not in d and ":" not in d


class TestProjectCommands:
    def test_hub_and_help_have_project_entries(self, tmp_path, repo, db):
        bot, tr = _proj_bot(tmp_path, repo, db)
        assert bot.handle_update(upd_msg("/menu")) == "cmd:menu"
        kb = tr.sent[-1]["reply_markup"]["inline_keyboard"]
        assert [b["callback_data"] for b in kb[2]] == ["af3|nav|proj", "af3|nav|board"]
        assert [b["text"] for b in kb[2]] == ["\U0001f4c1 프로젝트", "\U0001f4cb 보드·백로그"]
        h = dev_bot_mod.HELP_TEXT
        assert len(h) < 800 and all(x in h for x in ("/projects", "/board", "/backlog"))
        bot.handle_update(upd_msg("/kill"))
        bot.handle_update(upd_msg("/menu"))
        assert "af3|nav|proj" in _cbs(tr.sent[-1])
        _all_html_ok(tr)

    def test_projects_pagination_and_focus(self, tmp_path, repo, db):
        bot, tr = _proj_bot(tmp_path, repo, db, registry_file=_many_projects(tmp_path, 6))
        assert bot.handle_update(upd_msg("/projects")) == "cmd:projects"
        first = tr.sent[-1]
        assert "등록 7개" in first["text"] and "projects.json" in first["text"]
        assert "Folder &lt;0&gt;" in first["text"]  # labels are escaped
        pf = [d for d in _cbs(first) if d.startswith("af3|pf|")]
        assert pf == [f"af3|pf|{DEFAULT_PROJECT_ID}", "af3|pf|f0", "af3|pf|f1", "af3|pf|f2", "af3|pf|f3"]
        assert "af3|pp|1" in _cbs(first) and "af3|pp|-1" not in _cbs(first)
        assert bot.handle_update(upd_raw("af3|pp|1", message_id=5)) == "callback:af3:projects:edited"
        assert [d for d in _cbs(tr.edits[-1]) if d.startswith("af3|pf|")] == ["af3|pf|f4", "af3|pf|f5"]
        assert bot.handle_update(upd_raw("af3|pp|99", message_id=5)) == "callback:af3:projects:edited"  # clamped
        assert bot.handle_update(upd_raw("af3|pf|f4", message_id=5)) == "callback:af3:project:focus:edited"
        assert db.get_kv(f"focus_project:{ADMIN}") == "f4" and "포커스 변경" in tr.edits[-1]["text"]
        assert bot.handle_update(upd_raw("af3|pf|ghost", message_id=5)) == "callback:af3:project:unknown"
        assert db.get_kv(f"focus_project:{ADMIN}") == "f4"
        for bad in ("af3|pf|Bad", "af3|pp|x", "af3|pp|1234", "af3|bs|abc", "af3|ps|-1"):
            assert bot.handle_update(upd_raw(bad)) == "callback:malformed"
        assert bot.handle_update(upd_raw("af3|nav|proj", message_id=5)) == "callback:af3:projects:edited"
        assert bot.handle_update(upd_msg("/projects nope")) == "cmd:projects:bad-arg"
        _all_html_ok(tr)
        _all_cbs_safe(tr, forbidden_text=("Folder", str(tmp_path)))

    def test_projects_scan_never_writes(self, tmp_path, repo, db):
        path = _many_projects(tmp_path, 1)
        _git_init(tmp_path / "ws" / "unreg_git")
        (tmp_path / "ws" / "unreg_dir").mkdir()
        before = path.read_bytes(), path.stat().st_mtime_ns
        bot, tr = _proj_bot(tmp_path, repo, db, registry_file=path)
        assert bot.handle_update(upd_msg("/projects scan")) == "cmd:projects:scan"
        text = tr.sent[-1]["text"]
        assert "unreg_git" in text and "미등록(읽기 전용)" in text and "등록됨" in text and "f0" in text
        assert bot.handle_update(upd_raw("af3|ps|0", message_id=3)) == "callback:af3:scan:edited"
        assert (path.read_bytes(), path.stat().st_mtime_ns) == before
        assert not (tmp_path / "state" / "projects.json").exists()
        _all_html_ok(tr)

    def test_board_summary_and_per_project_isolation(self, tmp_path, repo, db, monkeypatch):
        other = _git_init(tmp_path / "ws" / "other")
        (tmp_path / "ws" / "notes").mkdir(parents=True)
        path = _write_projects(tmp_path, [_entry("other", other, label="Other", allow_write=True),
                                          _entry("notes", tmp_path / "ws" / "notes", kind="folder", label="Notes"),
                                          _entry("broken", _git_init(tmp_path / "ws" / "broken"), label="Broken")])
        bot, tr = _proj_bot(tmp_path, repo, db, registry_file=path)
        tid = submit(db, start=False)
        db.create_task("x", "L1", ["a.py"], {}, project_id="other")
        db.create_task("y", "L1", ["a.py"], {}, project_id="ghost")
        real = dev_bot_mod.inspect_project_git

        def flaky(p, *a, **k):
            if p.project_id == "broken":
                raise RuntimeError("boom")
            return real(p, *a, **k)

        monkeypatch.setattr(dev_bot_mod, "inspect_project_git", flaky)
        monkeypatch.setattr(bot, "_runtime", lambda task_id=None: {tid: _ptypes.SimpleNamespace(liveness="STALLED")})
        bot.handle_update(upd_msg("/kill"))
        assert bot.handle_update(upd_msg("/board")) == "cmd:board"
        text = tr.sent[-1]["text"]
        lines = text.split("\n")
        assert lines[0].startswith("⏸️ <b>개발봇 일시정지됨</b>")
        home = next(l for l in lines if DEFAULT_PROJECT_ID in l)
        assert "\U0001f534 STALLED 1" in home and "작업 1개" in home and "변경 0개" in home
        assert any("Other" in l and "작업 1개" in l and "git(쓰기 가능)" in l for l in lines)
        assert any("Notes" in l and "folder(읽기전용)" in l and "변경" not in l for l in lines)
        assert any("Broken" in l and "상태 확인 실패" in l for l in lines)
        assert "미등록 프로젝트의 열린 작업 1개" in text
        assert 0 < SafeHtmlFormatter.visible_len(text) <= 1000
        assert bot.handle_update(upd_raw("af3|nav|board", message_id=4)) == "callback:af3:board:edited"
        _all_html_ok(tr)

    def test_corrupt_registry_fails_closed_everywhere(self, tmp_path, repo, db):
        path = _write_projects(tmp_path, None, raw="{oops")
        bot, tr = _proj_bot(tmp_path, repo, db, registry_file=path)
        for cmd in ("/projects", "/projects scan", "/board", "/backlog"):
            bot.handle_update(upd_msg(cmd))
            assert "프로젝트 레지스트리 오류" in tr.sent[-1]["text"]
        assert bot.handle_update(upd_msg("/backlog add 새 항목")) == "cmd:backlog:registry_error"
        assert db.list_backlog_items() == []
        assert bot.handle_update(upd_raw("af3|pf|market_brief_starter")) == "callback:af3:project:registry_error"
        assert db.get_kv(f"focus_project:{ADMIN}") is None
        assert bot.handle_update(upd_msg("/menu")) == "cmd:menu"  # the rest of the bot keeps working
        _all_html_ok(tr)

    def test_backlog_add_start_two_step(self, tmp_path, repo, db):
        bot, tr = _proj_bot(tmp_path, repo, db)
        assert bot.handle_update(upd_msg("/backlog")) == "cmd:backlog"
        assert "열린 백로그가 없습니다" in tr.sent[-1]["text"]
        res = bot.handle_update(upd_msg("/backlog add 로그인 <b>버그</b> & 수정"))
        assert res.startswith("cmd:backlog:added:")
        iid = int(res.rsplit(":", 1)[1])
        view = tr.sent[-1]
        assert "로그인 &lt;b&gt;버그&lt;/b&gt; &amp; 수정" in view["text"] and "<b>버그</b>" not in view["text"]
        assert f"af3|bs|{iid}" in _cbs(view) and _af3_tok(view, "BD") and _af3_tok(view, "BH")
        assert db.get_backlog_item(iid)["project_id"] == DEFAULT_PROJECT_ID
        assert bot.handle_update(upd_raw(f"af3|bs|{iid}", message_id=8)) == "callback:af3:backlog:start_prompt:edited"
        prompt = tr.edits[-1]
        assert db.list_tasks() == [] and db.get_backlog_item(iid)["status"] == "TODO"  # step 1: no mutation
        bs = _af3_tok(prompt, "BS")
        tr.answer_script = [False]
        assert bot.handle_update(upd_raw(f"af3|BS|{bs}")) == "callback:answer_failed"
        assert db.list_tasks() == [] and _af3_used(db, bs) is None
        assert bot.handle_update(upd_raw(f"af3|BS|{bs}", frm=OTHER)) == "ignored:unauthorized"
        assert bot.handle_update(upd_raw(f"af3|BS|{bs}", message_id=8)) == "callback:af3:backlog:started:BLOCKED"
        item = db.get_backlog_item(iid)
        task = db.get_task(item["linked_task_id"])
        assert item["status"] == "IN_PROGRESS" and task["state"] == "BLOCKED" and task["project_id"] == DEFAULT_PROJECT_ID
        assert "작업 생성" in tr.edits[-1]["text"] and f"af3|bs|{iid}" not in _cbs(tr.edits[-1])
        assert bot.handle_update(upd_raw(f"af3|BS|{bs}")) == "callback:af3:rejected:REPLAY"
        assert bot.handle_update(upd_raw(f"af3|bs|{iid}")) == "callback:af3:backlog:not_todo"
        assert len(db.list_tasks()) == 1
        assert bot.handle_update(upd_msg("/backlog add")) == "cmd:backlog:bad-arg"
        assert bot.handle_update(upd_msg("/backlog remove 1")) == "cmd:backlog:bad-arg"
        assert bot.handle_update(upd_msg("/backlog add " + "x" * 300)) == "cmd:backlog:rejected"
        _all_html_ok(tr)
        _all_cbs_safe(tr, forbidden_text=("로그인", "버그"))

    def test_backlog_folder_project_cannot_start(self, tmp_path, repo, db):
        bot, tr = _proj_bot(tmp_path, repo, db, registry_file=_many_projects(tmp_path, 1))
        bot.handle_update(upd_raw("af3|pf|f0"))
        res = bot.handle_update(upd_msg("/backlog add 문서 정리"))
        iid = int(res.rsplit(":", 1)[1])
        view = tr.sent[-1]
        assert db.get_backlog_item(iid)["project_id"] == "f0" and "읽기 전용 프로젝트" in view["text"]
        assert not any(d.startswith("af3|bs|") for d in _cbs(view))
        assert bot.handle_update(upd_raw(f"af3|bs|{iid}")) == "callback:af3:backlog:read_only"
        forged = db.issue_af3_token("BS", ADMIN, ADMIN, {"item_id": iid, "project_id": "f0"})
        assert bot.handle_update(upd_raw(f"af3|BS|{forged}")) == "callback:af3:rejected:PROJECT_READ_ONLY"
        mismatch = db.issue_af3_token("BS", ADMIN, ADMIN, {"item_id": iid, "project_id": DEFAULT_PROJECT_ID})
        assert bot.handle_update(upd_raw(f"af3|BS|{mismatch}")) == "callback:af3:rejected:PROJECT_MISMATCH"
        ghost = db.issue_af3_token("BS", ADMIN, ADMIN, {"item_id": iid, "project_id": "ghost"})
        assert bot.handle_update(upd_raw(f"af3|BS|{ghost}")) == "callback:af3:rejected:PROJECT_UNKNOWN"
        assert _af3_used(db, forged) is None and db.list_tasks() == []
        assert db.get_backlog_item(iid)["status"] == "TODO"
        _all_html_ok(tr)

    def test_backlog_non_home_git_project_parks_task(self, tmp_path, repo, db):
        other = _git_init(tmp_path / "ws" / "other")
        bot, tr = _proj_bot(tmp_path, repo, db,
                            registry_file=_write_projects(tmp_path, [_entry("other", other, allow_write=True)]))
        bot.handle_update(upd_raw("af3|pf|other"))
        iid = int(bot.handle_update(upd_msg("/backlog add 기능")).rsplit(":", 1)[1])
        bot.handle_update(upd_raw(f"af3|bs|{iid}", message_id=2))
        assert "Phase B" in tr.edits[-1]["text"]
        bs = _af3_tok(tr.edits[-1], "BS")
        assert bot.handle_update(upd_raw(f"af3|BS|{bs}", message_id=2)) == "callback:af3:backlog:started:BLOCKED"
        task = db.get_task(db.get_backlog_item(iid)["linked_task_id"])
        assert task["project_id"] == "other" and task["state"] == "BLOCKED"
        assert db.events(task["id"])[-1]["reason"] == PHASE_B_PARK_REASON

    def test_backlog_done_hold_and_stale_tokens(self, tmp_path, repo, db):
        bot, tr = _proj_bot(tmp_path, repo, db)
        iid = int(bot.handle_update(upd_msg("/backlog add 보류할 항목")).rsplit(":", 1)[1])
        view = tr.sent[-1]
        bd_old, bh = _af3_tok(view, "BD"), _af3_tok(view, "BH")
        assert bot.handle_update(upd_raw(f"af3|BH|{bh}", message_id=3)) == "callback:af3:backlog:blocked"
        assert db.get_backlog_item(iid)["status"] == "BLOCKED"
        held = tr.edits[-1]
        assert "↩️" in " ".join(b["text"] for row in held["reply_markup"]["inline_keyboard"] for b in row)
        assert bot.handle_update(upd_raw(f"af3|BD|{bd_old}")) == "callback:af3:rejected:STALE"  # CAS on status
        assert _af3_used(db, bd_old) is None
        assert bot.handle_update(upd_raw(f"af3|BH|{_af3_tok(held, 'BH')}", message_id=3)) == "callback:af3:backlog:todo"
        assert db.get_backlog_item(iid)["status"] == "TODO"
        assert bot.handle_update(upd_raw(f"af3|BD|{_af3_tok(tr.edits[-1], 'BD')}", message_id=3)) == "callback:af3:backlog:done"
        assert db.get_backlog_item(iid)["status"] == "DONE" and "열린 백로그가 없습니다" in tr.edits[-1]["text"]
        assert bot.handle_update(upd_raw(f"af3|BH|{bh}")) == "callback:af3:rejected:REPLAY"
        _all_html_ok(tr)

    def test_backlog_pagination_and_nav(self, tmp_path, repo, db):
        bot, tr = _proj_bot(tmp_path, repo, db)
        for i in range(7):
            db.add_backlog_item(DEFAULT_PROJECT_ID, f"item {i}", priority=i % 3)
        assert bot.handle_update(upd_raw("af3|nav|blog", message_id=6)) == "callback:af3:backlog:edited"
        first = tr.edits[-1]
        assert "열린 항목 7개" in first["text"] and first["text"].count("\n#") + first["text"].count("\U0001f535 #") >= 5
        assert "af3|bp|1" in _cbs(first)
        assert bot.handle_update(upd_raw("af3|bp|1", message_id=6)) == "callback:af3:backlog:edited"
        second = tr.edits[-1]
        assert second["text"].count("\U0001f535 #") == 2 and "af3|bp|0" in _cbs(second)
        _all_html_ok(tr)
        _all_cbs_safe(tr, forbidden_text=("item",))
