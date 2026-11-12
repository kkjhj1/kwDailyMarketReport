"""Deterministic guardrails: forbidden paths, AST anti-cheating, gate runner, oscillation detector.

Only these deterministic checks may declare a candidate green; LLM output never can.
"""
from __future__ import annotations

import ast
import fnmatch
import hashlib
import re
import shutil
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Iterable

from .win_job import JobRunner, build_sandbox_env
from .worktree import ChangedFile, WorktreeManager

FORBIDDEN_PATTERNS = (
    ".env",
    ".env.*",
    "*.sqlite3",
    "reports/",
    "deploy_to_oracle.ps1",
    "holdout_eval/",
    "pytest.ini",
    "conftest.py",
    "pyproject.toml",
    "STOP.flag",
    # additive (preset registry hardening): DB files, data/VCS/CI dirs, constitution, runners
    "*.sqlite",
    "*.db",
    "*.db-wal",
    "*.db-shm",
    "data/",
    ".git/",
    ".github/",
    "AGENTS.md",
    "run_cron.sh",
    "admin_server.py",
    "*bot_runner.py",
)
SECRET_PATTERNS = (
    re.compile(r"\b\d{8,10}:[A-Za-z0-9_-]{35}\b"),  # Telegram bot token
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),  # OpenAI-style key
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),  # AWS access key id
    re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"),  # Google API key
    re.compile(r"TELEGRAM_BOT_TOKEN\s*[=:]\s*['\"][^'\"\s]+['\"]"),
)
_SKIP_NAMES = frozenset({"skip", "skipif", "xfail", "skipTest", "expectedFailure", "skipIf", "skipUnless"})
_ASSERT_CALL_RE = re.compile(r"^assert[A-Z_]|^fail(Unless|If)")
MAX_RETRIES = 3


@dataclass(frozen=True)
class Violation:
    code: str
    path: str
    detail: str = ""


@dataclass
class GuardrailReport:
    violations: list[Violation] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.violations

    def codes(self) -> set[str]:
        return {v.code for v in self.violations}


# ------------------------------------------------------------------ paths
def _norm(path: str) -> str:
    p = str(PurePosixPath(path.replace("\\", "/")))
    while p.startswith("./"):
        p = p[2:]
    return "" if p == "." else p.lstrip("/")


def matches_pattern(path: str, pattern: str) -> bool:
    p = _norm(path)
    if pattern.endswith("/"):
        d = pattern.rstrip("/")
        return p == d or p.startswith(d + "/")
    name = PurePosixPath(p).name
    return fnmatch.fnmatchcase(name, pattern) or fnmatch.fnmatchcase(p, pattern)


def is_forbidden(path: str, patterns: Iterable[str] = FORBIDDEN_PATTERNS) -> bool:
    return any(matches_pattern(path, pat) for pat in patterns)


def is_allowed(path: str, allowed_paths: Iterable[str]) -> bool:
    p = _norm(path)
    for entry in allowed_paths:
        e = _norm(entry) if not entry.endswith("/") else _norm(entry.rstrip("/")) + "/"
        if e.endswith("/"):
            if p.startswith(e):
                return True
        elif p == e or fnmatch.fnmatchcase(p, e):
            return True
    return False


def check_paths(
    changed: Iterable[ChangedFile],
    allowed_paths: Iterable[str] | None = None,
    forbidden: Iterable[str] = FORBIDDEN_PATTERNS,
) -> list[Violation]:
    allowed = list(allowed_paths) if allowed_paths is not None else None
    forbidden = tuple(forbidden)
    out: list[Violation] = []
    for cf in changed:
        for p in filter(None, (cf.path, cf.old_path)):
            if is_forbidden(p, forbidden):
                out.append(Violation("FORBIDDEN_PATH", p, cf.status))
            elif allowed is not None and not is_allowed(p, allowed):
                out.append(Violation("OUTSIDE_ALLOWED_PATHS", p, cf.status))
    return out


def is_test_file(path: str) -> bool:
    p = PurePosixPath(_norm(path))
    return p.suffix == ".py" and (
        p.name.startswith("test_") or p.name.endswith("_test.py") or "tests" in p.parts[:-1]
    ) and p.name != "conftest.py"


# -------------------------------------------------------------------- AST
def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _is_skip_ref(node: ast.AST) -> bool:
    target = node.func if isinstance(node, ast.Call) else node
    name = _dotted(target)
    return bool(name) and name.split(".")[-1] in _SKIP_NAMES


def _count_asserts(fn: ast.AST) -> int:
    n = 0
    for node in ast.walk(fn):
        if isinstance(node, ast.Assert):
            n += 1
        elif isinstance(node, ast.Call):
            name = _dotted(node.func).split(".")[-1]
            if _ASSERT_CALL_RE.match(name) or name == "raises":
                n += 1
    return n


def _count_skips(tree: ast.AST) -> int:
    """Count skip/xfail/expectedFailure references (decorators, calls, pytestmark), once each."""
    call_funcs = {id(n.func) for n in ast.walk(tree) if isinstance(n, ast.Call)}
    n = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            n += _is_skip_ref(node)
        elif isinstance(node, (ast.Attribute, ast.Name)) and id(node) not in call_funcs:
            n += _is_skip_ref(node)
    return n


def count_broad_except_pass(tree: ast.AST) -> int:
    n = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.ExceptHandler):
            continue
        broad = node.type is None or _dotted(node.type) in ("Exception", "BaseException")
        body_noop = all(
            isinstance(s, (ast.Pass, ast.Continue))
            or (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
            for s in node.body
        )
        if broad and body_noop:
            n += 1
    return n


def collect_tests(tree: ast.AST) -> dict[str, int]:
    """Map `test_name` / `Class::test_name` -> assertion count."""
    tests: dict[str, int] = {}
    for node in tree.body if isinstance(tree, ast.Module) else []:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
            tests[node.name] = _count_asserts(node)
        elif isinstance(node, ast.ClassDef):
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name.startswith("test"):
                    tests[f"{node.name}::{sub.name}"] = _count_asserts(sub)
    return tests


def compare_sources(baseline: dict[str, str], candidate: dict[str, str | None]) -> list[Violation]:
    """Compare python sources at base vs candidate. `None` candidate means deleted/moved."""
    out: list[Violation] = []
    for path in sorted(set(baseline) | set(candidate)):
        base_src = baseline.get(path)
        cand_src = candidate.get(path)
        test = is_test_file(path)
        if cand_src is None:
            if base_src is not None and test:
                out.append(Violation("TEST_FILE_DELETED", path))
            continue
        try:
            cand_tree = ast.parse(cand_src)
        except SyntaxError as exc:
            out.append(Violation("SYNTAX_ERROR", path, str(exc)))
            continue
        base_tree = None
        if base_src is not None:
            try:
                base_tree = ast.parse(base_src)
            except SyntaxError:
                base_tree = None
        base_broad = count_broad_except_pass(base_tree) if base_tree else 0
        if count_broad_except_pass(cand_tree) > base_broad:
            out.append(Violation("BROAD_EXCEPT_PASS", path))
        if not test:
            continue
        base_skips = _count_skips(base_tree) if base_tree else 0
        if _count_skips(cand_tree) > base_skips:
            out.append(Violation("SKIP_ADDED", path))
        if base_tree is None:
            continue
        base_tests = collect_tests(base_tree)
        cand_tests = collect_tests(cand_tree)
        for name, n_assert in base_tests.items():
            if name not in cand_tests:
                out.append(Violation("TEST_REMOVED", path, name))
            elif cand_tests[name] < n_assert:
                out.append(
                    Violation("ASSERT_REDUCED", path, f"{name}: {n_assert} -> {cand_tests[name]}")
                )
    return out


def scan_secrets(text: str) -> list[str]:
    return [p.pattern for p in SECRET_PATTERNS if p.search(text)]


class GuardrailChecker:
    def __init__(self, wt: WorktreeManager, forbidden: Iterable[str] = FORBIDDEN_PATTERNS):
        self.wt = wt
        self.forbidden = tuple(forbidden)

    def check_worktree(
        self, wt_path: str, base_sha: str, allowed_paths: Iterable[str] | None = None
    ) -> GuardrailReport:
        report = GuardrailReport()
        changed = self.wt.changed_files(wt_path, base_sha)
        report.violations += check_paths(changed, allowed_paths, self.forbidden)

        baseline: dict[str, str] = {}
        candidate: dict[str, str | None] = {}
        for cf in changed:
            if cf.old_path and cf.old_path.endswith(".py"):
                src = self.wt.show_file(wt_path, base_sha, cf.old_path)
                if src is not None:
                    baseline[cf.old_path] = src
                    candidate.setdefault(cf.old_path, None)  # moved away == deleted
            if not cf.path.endswith(".py"):
                continue
            src = self.wt.show_file(wt_path, base_sha, cf.path)
            if src is not None:
                baseline[cf.path] = src
            candidate[cf.path] = None if cf.status == "D" else self.wt.show_file(wt_path, "HEAD", cf.path)
        report.violations += compare_sources(baseline, candidate)

        check = self.wt.git("diff", "--check", base_sha, "HEAD", cwd=wt_path, check=False)
        if check.strip():
            report.violations.append(Violation("DIFF_CHECK", "", check.strip()[:500]))
        added = "\n".join(
            line[1:]
            for line in self.wt.diff_bytes(wt_path, base_sha).decode("utf-8", "replace").splitlines()
            if line.startswith("+") and not line.startswith("+++")
        )
        for pat in scan_secrets(added):
            report.violations.append(Violation("SECRET_LEAK", "", pat))
        return report


# ------------------------------------------------------------ gate runner
_NOISE = [
    (re.compile(r"0x[0-9a-fA-F]+"), "0xADDR"),
    (re.compile(r"line \d+"), "line N"),
    (re.compile(r":\d+:"), ":N:"),
    (re.compile(r"\bin \d+(\.\d+)?s\b"), "in Ts"),
    (re.compile(r"[A-Za-z]:\\[^\s'\"]*|/(?:tmp|var|private)/[^\s'\"]*"), "<PATH>"),
]
_SIGNAL_RE = re.compile(r"^(E\s|FAILED|ERROR|\w+(Error|Exception)\b|SyntaxError|\*\*\*)")


def traceback_hash(output: str) -> str | None:
    lines = [ln.strip() for ln in output.splitlines() if _SIGNAL_RE.match(ln.strip())]
    if not lines:
        return None
    text = "\n".join(lines)
    for rx, repl in _NOISE:
        text = rx.sub(repl, text)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def failure_count(output: str) -> int:
    total = 0
    for m in re.finditer(r"(\d+) (failed|error|errors)\b", output):
        total += int(m.group(1))
    return total


@dataclass
class GateResult:
    name: str
    ok: bool
    returncode: int | None
    output_tail: str = ""
    traceback_hash: str | None = None
    fail_count: int = 0


class DeterministicRunner:
    def __init__(self, runner: JobRunner | None = None, python: str = sys.executable):
        self.runner = runner or JobRunner(wall_timeout=600, idle_timeout=180)
        self.python = python

    def _env(self, wt_path: str) -> dict[str, str]:
        return build_sandbox_env({"PYTHONPATH": wt_path, "PYTHONDONTWRITEBYTECODE": "1"})

    def _result(self, name: str, res) -> GateResult:
        out = res.stdout + "\n" + res.stderr
        ok = res.ok
        th = None if ok else (traceback_hash(out) or hashlib.sha256(out.encode()).hexdigest()[:16])
        return GateResult(name, ok, res.returncode, out[-4000:], th, 0 if ok else max(1, failure_count(out)))

    def py_compile(self, wt_path: str, files: list[str]) -> GateResult:
        if not files:
            return GateResult("py_compile", True, 0)
        code = (
            "import os,sys,tempfile,py_compile\n"
            "d=tempfile.mkdtemp()\n"
            "for i,f in enumerate(sys.argv[1:]):\n"
            "    py_compile.compile(f, cfile=os.path.join(d, '%d.pyc' % i), doraise=True)\n"
        )
        res = self.runner.run([self.python, "-c", code, *files], cwd=wt_path, env=self._env(wt_path))
        return self._result("py_compile", res)

    def pytest(self, wt_path: str, targets: list[str], name: str = "pytest") -> GateResult:
        cmd = [self.python, "-m", "pytest", "-q", "-p", "no:cacheprovider", *targets]
        res = self.runner.run(cmd, cwd=wt_path, env=self._env(wt_path))
        return self._result(name, res)

    def holdout(self, wt_path: str, holdout_dir: str) -> GateResult:
        """Run an immutable copy of the external holdout suite against the candidate."""
        src = Path(holdout_dir)
        if not src.is_dir():
            return GateResult("holdout", False, None, f"holdout dir missing: {src}", "HOLDOUT_MISSING", 1)
        with tempfile.TemporaryDirectory(prefix="af_holdout_", ignore_cleanup_errors=True) as tmp:
            dst = Path(tmp) / "holdout"
            shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            ini = Path(tmp) / "pytest.ini"
            ini.write_text("[pytest]\n", encoding="utf-8")
            cmd = [
                self.python, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                "-c", str(ini), "--rootdir", tmp, str(dst),
            ]
            res = self.runner.run(cmd, cwd=wt_path, env=self._env(wt_path))
        return self._result("holdout", res)


# ------------------------------------------------------- oscillation guard
class OscillationDetector:
    """Self-heal budget: max 3 retries, abort on any repeated hash or on regression."""

    def __init__(self, max_retries: int = MAX_RETRIES):
        self.max_retries = max_retries
        self.history: list[tuple[str, int]] = []

    def record(self, tb_hash: str | None, fail_count: int = 0) -> str:
        if tb_hash is None:
            return "PASS"
        seen = {h for h, _ in self.history}
        prev = self.history[-1][1] if self.history else None
        self.history.append((tb_hash, fail_count))
        if tb_hash in seen:
            return "ABORT_DUPLICATE"
        if prev is not None and fail_count > prev:
            return "ABORT_REGRESSION"
        if len(self.history) > self.max_retries:
            return "ABORT_MAX_RETRIES"
        return "RETRY"
