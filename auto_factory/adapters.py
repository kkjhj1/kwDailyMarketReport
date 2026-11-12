"""Provider adapters, failure classification, handoff contract and AGENTS.md section 6 router."""
from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

from .win_job import JobRunner, build_sandbox_env

MAX_CONTRACT_BYTES = 2048
SCHEMA_VERSION = 1
WRITER_ORDER = ("claude", "codex", "gemini")
REVIEWER_ORDER = ("gemini", "codex", "claude")
DEFAULT_FORBIDDEN = [".env", "*.sqlite3", "reports/", "deploy_to_oracle.ps1", "holdout_eval/"]
DEFAULT_INVARIANTS = ["no live telegram", "tests must not be weakened", "no auto merge"]
# cmd.exe metacharacters: refuse to pass these through a .cmd/.bat shim.
_SAFE_SHIM_ARG_RE = re.compile(r'^[^&|<>^%!"()\r\n]*$')


class FailureCode(str, Enum):
    OK = "OK"
    RATE_LIMIT = "RATE_LIMIT"
    QUOTA = "QUOTA"
    TIMEOUT = "TIMEOUT"
    AUTH = "AUTH"
    UNAVAILABLE = "UNAVAILABLE"
    INTERNAL = "INTERNAL"


# Writer may hand off to the next provider only for capacity/availability failures.
FAILOVER_CODES = frozenset(
    {FailureCode.RATE_LIMIT, FailureCode.QUOTA, FailureCode.TIMEOUT, FailureCode.AUTH, FailureCode.UNAVAILABLE}
)
# These take the provider out of rotation entirely (cooldown), not just for this task.
COOLDOWN_CODES = frozenset(
    {FailureCode.RATE_LIMIT, FailureCode.QUOTA, FailureCode.AUTH, FailureCode.UNAVAILABLE}
)

_QUOTA_RE = re.compile(
    r"quota|usage limit|limit reached|insufficient_quota|resource_exhausted|exceeded your", re.I
)
_RATE_RE = re.compile(r"\b429\b|rate[ _-]?limit|too many requests", re.I)
_AUTH_RE = re.compile(
    r"\b401\b|\b403\b|unauthori[sz]ed|unauthenticated|not logged in|login required|authentication"
    r"|invalid api key",
    re.I,
)
_TIMEOUT_RE = re.compile(r"deadline_exceeded|print_timeout", re.I)


def classify_failure(
    returncode: int | None, stdout: str = "", stderr: str = "", timed_out: bool = False
) -> FailureCode:
    if timed_out:
        return FailureCode.TIMEOUT
    if returncode == 0:
        return FailureCode.OK
    text = f"{stdout}\n{stderr}"
    if _QUOTA_RE.search(text):
        return FailureCode.QUOTA
    if _RATE_RE.search(text):
        return FailureCode.RATE_LIMIT
    if _AUTH_RE.search(text):
        return FailureCode.AUTH
    if _TIMEOUT_RE.search(text):
        return FailureCode.TIMEOUT
    return FailureCode.INTERNAL


def parse_retry_after(text: str) -> float | None:
    m = re.search(r"retry[-_ ]after[\"':= ]+(\d+(?:\.\d+)?)", text, re.I)
    if m:
        return float(m.group(1))
    m = re.search(r"try again in (\d+(?:\.\d+)?)\s*(s|sec|seconds?|m|min|minutes?|h|hours?)\b", text, re.I)
    if m:
        unit = m.group(2).lower()[0]
        return float(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[unit]
    return None


# ------------------------------------------------------------------ contract
class ContractTooLarge(ValueError):
    pass


@dataclass
class HandoffContract:
    task_id: str
    risk_level: str
    base_sha: str
    checkpoint_sha: str
    allowed_paths: list[str]
    acceptance_tests: list[str]
    next_role: str
    forbidden_paths: list[str] = field(default_factory=lambda: list(DEFAULT_FORBIDDEN))
    failure: dict[str, Any] | None = None
    diff_sha256: str = ""
    invariants: list[str] = field(default_factory=lambda: list(DEFAULT_INVARIANTS))
    schema_version: int = SCHEMA_VERSION
    # Optional human-readable request summary (Step 2). Legacy JSON without it still loads.
    summary: str = ""

    def fit_summary(self, text: str, max_chars: int = 300) -> "HandoffContract":
        """Set `summary` to the longest prefix of `text[:max_chars]` that keeps the contract <= 2KB.

        Only the summary is shrunk; if the contract is oversized for other reasons it stays
        oversized and `to_json()` still raises `ContractTooLarge` (fail closed).
        """
        text = (text or "")[:max_chars]
        self.summary = ""
        base = len(self._dump().encode("utf-8"))
        if base > MAX_CONTRACT_BYTES:
            return self
        lo, hi = 0, len(text)
        while lo < hi:  # binary search on the prefix length (JSON escaping is not linear)
            mid = (lo + hi + 1) // 2
            self.summary = text[:mid]
            if len(self._dump().encode("utf-8")) <= MAX_CONTRACT_BYTES:
                lo = mid
            else:
                hi = mid - 1
        self.summary = text[:lo]
        return self

    def _dump(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":"), sort_keys=True)

    def to_json(self) -> str:
        text = self._dump()
        size = len(text.encode("utf-8"))
        if size > MAX_CONTRACT_BYTES:
            raise ContractTooLarge(f"handoff contract is {size} bytes (> {MAX_CONTRACT_BYTES})")
        return text

    @classmethod
    def from_json(cls, text: str) -> "HandoffContract":
        if len(text.encode("utf-8")) > MAX_CONTRACT_BYTES:
            raise ContractTooLarge("handoff contract exceeds 2KB")
        data = json.loads(text)
        if data.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"unsupported schema_version: {data.get('schema_version')}")
        if not isinstance(data.get("summary", ""), str):
            raise ValueError("summary must be a string")
        return cls(**data)

    def write(self, path: str | Path) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.to_json(), encoding="utf-8")
        return path


QA_SCHEMA_VERSION = 1
QA_RESULT_KEYS = frozenset({"schema_version", "qa_id", "answer", "refs"})
QA_MAX_REFS = 5
QA_MAX_ANSWER_CHARS = 6000


@dataclass
class QAContract:
    """Read-only Q&A request handed to a provider (written as `__af_qa_request__.json`)."""

    qa_id: str
    question: str
    preset_alias: str | None = None
    allowed_paths: list[str] = field(default_factory=list)
    base_sha: str = ""
    schema_version: int = QA_SCHEMA_VERSION

    def to_json(self) -> str:
        return json.dumps(
            {"schema_version": self.schema_version, "qa_id": self.qa_id, "preset_alias": self.preset_alias,
             "allowed_paths": list(self.allowed_paths), "question": self.question},
            ensure_ascii=False,
        )


class QAParseError(ValueError):
    pass


def parse_qa_result(stdout: str, expected_qa_id: str) -> dict[str, Any]:
    """Strictly parse the provider's single Q&A JSON object; any deviation raises QAParseError."""
    from .guardrails import is_qa_secret_path, normalize_rel_segments

    objs = [o for o in _json_objects(stdout or "") if isinstance(o, dict)]
    if len(objs) != 1:
        raise QAParseError(f"expected exactly one JSON object, found {len(objs)}")
    data = objs[0]
    if set(data) != QA_RESULT_KEYS:
        raise QAParseError("qa keys mismatch")
    if type(data["schema_version"]) is not int or data["schema_version"] != QA_SCHEMA_VERSION:
        raise QAParseError("schema_version must be the integer 1")
    if data["qa_id"] != expected_qa_id:
        raise QAParseError("qa_id mismatch")
    answer = data["answer"]
    if not isinstance(answer, str) or not answer.strip():
        raise QAParseError("answer must be a non-empty string")
    refs = data["refs"]
    if not isinstance(refs, list) or len(refs) > QA_MAX_REFS or not all(isinstance(r, str) for r in refs):
        raise QAParseError(f"refs must be a list of <= {QA_MAX_REFS} strings")
    clean: list[str] = []
    for ref in refs:
        if not ref or "\x00" in ref or ":" in ref or ref.startswith(("/", "\\")):
            raise QAParseError("ref must be a relative repository path")
        try:
            parts = normalize_rel_segments(ref)
        except ValueError as exc:
            raise QAParseError(f"invalid ref: {exc}") from None
        if is_qa_secret_path(ref):
            raise QAParseError("ref points at a forbidden path")
        clean.append("/".join(parts))
    return {"schema_version": 1, "qa_id": data["qa_id"], "answer": answer.strip()[:QA_MAX_ANSWER_CHARS],
            "refs": clean}


def render_prompt(role: str, contract_path: str) -> str:
    if role == "qa":
        # Pure US-ASCII prose without cmd.exe metacharacters (checked again in `invoke`).
        return (
            "You are a read-only question answering assistant for this repository snapshot. "
            f"Read the JSON request file at {contract_path} and answer its question field in Korean, "
            "focusing on allowed_paths when that list is not empty. "
            "Only read and search files. Never modify, create or delete any file and never run any command "
            "that writes. Never open env files, database files or anything under the scratch directory. "
            "Reply on stdout with exactly one JSON object and nothing else. "
            "The object must have exactly four keys: schema_version set to the integer 1, "
            "qa_id copied from the request file, answer as a non-empty string, "
            "and refs as a list of at most five relative file paths you relied on. "
            "Do not add any other key."
        )
    if role == "auditor":
        # Plain prose only: no cmd.exe metacharacters, so it passes `_SAFE_SHIM_ARG_RE` for .cmd shims.
        return (
            "You are the independent read-only Phase 1 auditor for an auto_factory task. "
            f"Read the JSON handoff contract at {contract_path} and audit the requested change "
            "against the repository at its base commit in this directory. "
            "Do not modify, create or delete any file and do not run any command that writes. "
            "Reply on stdout with exactly one JSON object and nothing else. "
            "The object must have exactly six keys: schema_version set to the integer 1, "
            "task_id copied from the contract, verdict set to approve or block, "
            "summary as a string of at most 300 characters, base_sha copied from the contract, "
            "and allowed_paths copied verbatim from the contract as a list of strings. "
            "Do not add any other key."
        )
    if role == "writer":
        return (
            "You are the single writer for an auto_factory task. "
            f"Read the JSON handoff contract at {contract_path} and implement it in this worktree. "
            "Edit only files under allowed_paths and never touch forbidden_paths. "
            "Never delete, skip or weaken tests. Do not commit, push, merge or call Telegram. "
            "If failure is set, fix that failure starting from the current checkpoint."
        )
    return (
        f"You are an independent read-only reviewer. Read the JSON handoff contract at {contract_path} "
        "and review git diff base_sha..HEAD in this worktree against it. Do not modify any file. "
        "Finish with one JSON line with key verdict set to approve or reject plus a short reason."
    )


def parse_verdict(stdout: str) -> str | None:
    for line in reversed(stdout.strip().splitlines()):
        line = line.strip().strip("`")
        if "verdict" not in line:
            continue
        try:
            start = line.index("{")
            data = json.loads(line[start : line.rindex("}") + 1])
        except ValueError:
            continue
        verdict = str(data.get("verdict", "")).lower()
        if verdict in ("approve", "reject"):
            return verdict
    return None


class AuditParseError(ValueError):
    pass


AUDIT_RESULT_KEYS = frozenset({"schema_version", "task_id", "verdict", "summary", "base_sha", "allowed_paths"})
_HEX40_RE = re.compile(r"^[0-9a-f]{40}$")


def _json_objects(text: str) -> list[Any]:
    """Every top-level JSON object embedded in `text` (nested objects are not re-reported)."""
    dec = json.JSONDecoder()
    found, i = [], 0
    while True:
        i = text.find("{", i)
        if i < 0:
            return found
        try:
            obj, end = dec.raw_decode(text, i)
        except ValueError:
            i += 1
            continue
        found.append(obj)
        i = end


def parse_audit_result(
    stdout: str, expected_task_id: str, expected_base_sha: str, expected_allowed_paths: Sequence[str]
) -> dict[str, Any]:
    """Strictly parse the auditor's single JSON object; any deviation raises AuditParseError."""
    objs = [o for o in _json_objects(stdout or "") if isinstance(o, dict)]
    if not objs:
        raise AuditParseError("missing audit JSON object")
    if len(objs) > 1:
        raise AuditParseError(f"expected exactly one JSON object, found {len(objs)}")
    data = objs[0]
    if set(data) != AUDIT_RESULT_KEYS:
        raise AuditParseError(f"audit keys mismatch: extra={sorted(set(data) - AUDIT_RESULT_KEYS)} "
                              f"missing={sorted(AUDIT_RESULT_KEYS - set(data))}")
    if type(data["schema_version"]) is not int or data["schema_version"] != 1:
        raise AuditParseError("schema_version must be the integer 1")
    if data["task_id"] != expected_task_id:
        raise AuditParseError("task_id mismatch")
    if data["verdict"] not in ("approve", "block"):
        raise AuditParseError("verdict must be approve or block")
    if not isinstance(data["summary"], str) or len(data["summary"]) > 300:
        raise AuditParseError("summary must be a string of <= 300 chars")
    if not isinstance(data["base_sha"], str) or not _HEX40_RE.match(data["base_sha"]) \
            or data["base_sha"] != expected_base_sha:
        raise AuditParseError("base_sha mismatch")
    paths = data["allowed_paths"]
    if not isinstance(paths, list) or not all(isinstance(p, str) for p in paths) \
            or paths != list(expected_allowed_paths):
        raise AuditParseError("allowed_paths mismatch")
    return dict(data)


# ------------------------------------------------------------------ adapters
@dataclass
class AdapterResult:
    provider: str
    role: str
    code: FailureCode
    stdout: str = ""
    stderr: str = ""
    retry_after: float | None = None
    verdict: str | None = None
    duration: float = 0.0

    @property
    def ok(self) -> bool:
        return self.code == FailureCode.OK


class ProviderAdapter:
    provider = ""
    executable = ""

    def __init__(
        self,
        runner: JobRunner | None = None,
        executable: str | None = None,
        env_passthrough: Iterable[str] = (),
    ) -> None:
        self.runner = runner or JobRunner()
        self.executable = executable or self.executable
        self.env_passthrough = tuple(env_passthrough)

    def resolve_executable(self) -> str | None:
        return shutil.which(self.executable)

    def available(self) -> bool:
        return self.resolve_executable() is not None

    def build_command(self, role: str, prompt: str) -> list[str]:
        raise NotImplementedError

    def invoke(
        self,
        contract: HandoffContract,
        wt_path: str,
        role: str,
        contract_path: str,
        stop_check: Callable[[], bool] | None = None,
    ) -> AdapterResult:
        exe = self.resolve_executable()
        if exe is None:
            return AdapterResult(self.provider, role, FailureCode.UNAVAILABLE, stderr="executable not found")
        prompt = render_prompt(role, contract_path)
        if role == "qa" and not (_SAFE_SHIM_ARG_RE.match(prompt) and prompt.isascii()):
            return AdapterResult(self.provider, role, FailureCode.INTERNAL, stderr="unsafe qa prompt")
        if exe.lower().endswith((".cmd", ".bat")) and not _SAFE_SHIM_ARG_RE.match(prompt):
            return AdapterResult(self.provider, role, FailureCode.INTERNAL, stderr="unsafe prompt for shim")
        cmd = self.build_command(role, prompt)
        cmd[0] = exe
        try:
            res = self.runner.run(
                cmd, cwd=wt_path, env=build_sandbox_env(passthrough=self.env_passthrough), stop_check=stop_check
            )
        except (FileNotFoundError, PermissionError) as exc:
            return AdapterResult(self.provider, role, FailureCode.UNAVAILABLE, stderr=str(exc))
        code = classify_failure(res.returncode, res.stdout, res.stderr, res.timed_out)
        return AdapterResult(
            provider=self.provider,
            role=role,
            code=code,
            stdout=res.stdout[-8000:],
            stderr=res.stderr[-8000:],
            retry_after=parse_retry_after(res.stdout + res.stderr),
            verdict=parse_verdict(res.stdout) if role == "reviewer" and code == FailureCode.OK else None,
            duration=res.duration,
        )


class ClaudeAdapter(ProviderAdapter):
    provider = "claude"
    executable = "claude"

    WRITER_ALLOWED_TOOLS = "Read,Grep,Glob,Edit,Write,Bash(python *),Bash(pytest *)"

    def __init__(
        self,
        runner: JobRunner | None = None,
        executable: str | None = None,
        env_passthrough: Iterable[str] = (),
        max_budget_usd: float | None = 2.0,
    ) -> None:
        super().__init__(runner, executable, env_passthrough)
        self.max_budget_usd = max_budget_usd

    def build_command(self, role: str, prompt: str) -> list[str]:
        cmd = [self.executable, "-p", prompt]
        if role == "writer":
            # Without an explicit edit permission, headless claude prints code instead of editing.
            cmd += [
                "--tools", "Read,Grep,Glob,Edit,Write,Bash",
                "--permission-mode", "acceptEdits",
                "--allowedTools", self.WRITER_ALLOWED_TOOLS,
            ]
            if self.max_budget_usd is not None:
                cmd += ["--max-budget-usd", str(self.max_budget_usd)]
        else:
            cmd += ["--tools", "Read,Grep,Glob", "--permission-mode", "plan"]
        return cmd + ["--permission-prompts", "none", "--no-session-persistence"]


class CodexAdapter(ProviderAdapter):
    provider = "codex"
    executable = "codex"

    def build_command(self, role: str, prompt: str) -> list[str]:
        sandbox = "workspace-write" if role == "writer" else "read-only"
        cmd = [self.executable, "exec", "--skip-git-repo-check", "-s", sandbox]
        if os.name == "nt":
            # Elevated Windows sandbox fails under non-admin Job Objects; must stay unquoted for codex.CMD.
            cmd += ["-c", "windows.sandbox=unelevated"]
        return cmd + [prompt]


class GeminiAdapter(ProviderAdapter):
    """Antigravity CLI (`agy`). Legacy `gemini` CLI is blocked on free-tier OAuth (UNSUPPORTED_CLIENT)."""

    provider = "gemini"
    executable = "agy"

    _ALLOW_COMMANDS = ["command(regex:.*)"]
    _DENY_COMMANDS = ["command(regex:.*git\\s+(push|commit|merge|reset|checkout|clean).*)"]

    def __init__(
        self,
        runner: JobRunner | None = None,
        env_passthrough: Sequence[str] = (),
        model: str | None = "gemini-3.8-flash-medium",
    ) -> None:
        super().__init__(runner, env_passthrough=env_passthrough)
        self.model = model

    def resolve_executable(self) -> str | None:
        exe = shutil.which(self.executable)
        if exe is not None:
            return exe
        candidate = Path.home() / ".gemini" / "bin" / ("agy.exe" if os.name == "nt" else "agy")
        if candidate.is_file() and (os.name == "nt" or os.access(candidate, os.X_OK)):
            return str(candidate)
        return None

    def build_command(self, role: str, prompt: str) -> list[str]:
        # `--mode auto` is unavailable on this account; "plan" is read-only and non-interactive.
        mode = "accept-edits" if role == "writer" else "plan"
        if role == "qa":  # read-only Q&A: no shell command at all
            permissions = {"allow": [], "deny": ["command(regex:.*)"]}
        else:
            # Allow local verification (pytest etc.) but deny git history/remote mutation.
            permissions = {"allow": self._ALLOW_COMMANDS, "deny": self._DENY_COMMANDS}
        settings = json.dumps({"permissions": permissions}, separators=(",", ":"))
        cmd = [self.executable, "-p", prompt, "--mode", mode, "--settings", settings]
        if self.model:
            cmd += ["--model", self.model]
        return cmd


MockStep = Any  # FailureCode | str | Callable[[str, HandoffContract, str], Any]


class MockAdapter(ProviderAdapter):
    """Offline adapter for tests/--demo. Each invoke consumes one scripted step.

    A step is a FailureCode/str (simulated failure), or a callable
    `(wt_path, contract, role) -> None | "approve" | "reject" | AdapterResult`.
    An exhausted script means: writer succeeds without changes, reviewer approves.
    """

    def __init__(self, provider: str, script: Iterable[MockStep] = (), is_available: bool = True):
        super().__init__(runner=None)
        self.provider = provider
        self.executable = f"mock-{provider}"
        self.script = list(script)
        self.is_available = is_available
        self.calls: list[tuple[str, str]] = []

    def available(self) -> bool:
        return self.is_available

    def build_command(self, role: str, prompt: str) -> list[str]:
        return [self.executable, role]

    @staticmethod
    def default_audit(contract, verdict: str = "approve") -> dict[str, Any]:
        return {"schema_version": 1, "task_id": contract.task_id, "verdict": verdict, "summary": "mock audit",
                "base_sha": contract.base_sha, "allowed_paths": list(contract.allowed_paths)}

    @staticmethod
    def default_qa(contract, answer: str = "mock qa answer", refs: Sequence[str] = ("calc.py",)) -> dict[str, Any]:
        return {"schema_version": 1, "qa_id": contract.qa_id, "answer": answer, "refs": list(refs)}

    def invoke(self, contract, wt_path, role, contract_path, stop_check=None) -> AdapterResult:
        self.calls.append((role, getattr(contract, "qa_id", None) or getattr(contract, "task_id", "")))
        step = self.script.pop(0) if self.script else None
        if step is None:
            if role == "auditor":  # exhausted script: a valid approving audit
                return AdapterResult(self.provider, role, FailureCode.OK,
                                     stdout=json.dumps(self.default_audit(contract)))
            if role == "qa":  # exhausted script: a valid answer
                return AdapterResult(self.provider, role, FailureCode.OK,
                                     stdout=json.dumps(self.default_qa(contract), ensure_ascii=False))
            verdict = "approve" if role == "reviewer" else None
            return AdapterResult(self.provider, role, FailureCode.OK, verdict=verdict)
        if isinstance(step, (FailureCode, str)) and not callable(step):
            code = FailureCode(step)
            msg = {"RATE_LIMIT": "429 Too Many Requests; retry-after: 60"}.get(code.value, code.value)
            return AdapterResult(self.provider, role, code, stderr=msg, retry_after=parse_retry_after(msg))
        out = step(wt_path, contract, role)
        if isinstance(out, AdapterResult):
            return out
        if role in ("auditor", "qa"):  # callable returns raw stdout (str) or a dict serialized as JSON
            if out is None and role == "qa":
                out = self.default_qa(contract)
            stdout = json.dumps(out, ensure_ascii=False) if isinstance(out, dict) else ("" if out is None else str(out))
            return AdapterResult(self.provider, role, FailureCode.OK, stdout=stdout)
        verdict = out if out in ("approve", "reject") else ("approve" if role == "reviewer" else None)
        return AdapterResult(self.provider, role, FailureCode.OK, verdict=verdict)


def default_adapters(runner: JobRunner | None = None) -> dict[str, ProviderAdapter]:
    return {a.provider: a for a in (ClaudeAdapter(runner), CodexAdapter(runner), GeminiAdapter(runner))}


# -------------------------------------------------------------------- router
@dataclass(frozen=True)
class RouteDecision:
    outcome: str  # "PROCEED" | "SINGLE_PROVIDER_REVIEW_REQUIRED" | "BLOCKED"
    writer: str | None
    reviewer: str | None
    reason: str
    failover: bool = False


def select_reviewer(coder: str, available: Iterable[str]) -> str | None:
    """Coder != Reviewer invariant."""
    avail = set(available)
    for p in REVIEWER_ORDER:
        if p != coder and p in avail:
            return p
    return None


def route_writer(
    risk_level: str,
    available: Iterable[str],
    has_valid_audit_plan: bool = False,
    failed_writers: Iterable[str] = (),
) -> RouteDecision:
    """AGENTS.md section 6 writer routing.

    `available` = providers healthy and within quota; `failed_writers` = providers that
    already failed as writer for this task (excluded as writer, may still review).
    """
    avail = [p for p in WRITER_ORDER if p in set(available)]
    failed = set(failed_writers)
    if risk_level == "L3":
        if set(WRITER_ORDER) - set(avail):
            return RouteDecision("BLOCKED", None, None, "L3 fail-closed: every provider must be available")
        if "claude" in failed:
            return RouteDecision("BLOCKED", None, None, "L3 fail-closed: substitute writer forbidden")
        return RouteDecision("PROCEED", "claude", select_reviewer("claude", avail), "L3 primary writer")
    if risk_level not in ("L0", "L1", "L2"):
        return RouteDecision("BLOCKED", None, None, f"unknown risk level {risk_level}")
    if not avail:
        return RouteDecision("BLOCKED", None, None, "no provider available")
    if len(avail) == 1:
        sole = avail[0]
        if sole in failed:
            return RouteDecision("BLOCKED", None, None, f"sole survivor {sole} already failed as writer")
        if risk_level == "L0" and sole == "gemini":
            return RouteDecision(
                "SINGLE_PROVIDER_REVIEW_REQUIRED", "gemini", None, "L0 gemini solo", sole != "claude"
            )
        if sole == "claude" and (has_valid_audit_plan or risk_level == "L0"):
            return RouteDecision(
                "SINGLE_PROVIDER_REVIEW_REQUIRED", "claude", None, "claude sole survivor; human review required"
            )
        return RouteDecision("BLOCKED", None, None, f"{sole} sole survivor may not start {risk_level} work")
    for p in avail:
        if p not in failed:
            reviewer = select_reviewer(p, avail)
            return RouteDecision("PROCEED", p, reviewer, f"writer={p}", p != "claude")
    return RouteDecision("BLOCKED", None, None, "every available writer already failed")
