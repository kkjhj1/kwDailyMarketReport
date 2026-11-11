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
            "GUARDRAIL_BLOCKED", "SINGLE_PROVIDER_REVIEW_REQUIRED",
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
            d.consume_action_token(tok, "d", ADMIN, ADMIN)
            d.consume_action_token(tok, "d", ADMIN, ADMIN)
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
