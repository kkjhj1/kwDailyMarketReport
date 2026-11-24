"""Fail-closed multi-project registry (Phase A: management plane only).

Hybrid model:
- Immutable global security policy lives in code (`GLOBAL_PROTECTED_PATHS`, forbidden test
  targets, `max_risk <= L2`, `folder` projects are always read-only).
- The list of projects comes from an external `projects.json`
  (`AUTO_FACTORY_PROJECTS_FILE` or `<state_dir>/projects.json`).

Rules:
- No `projects.json` configured/present -> a safe default registry with ONLY
  `market_brief_starter` (the bot's own `repo_root`, `kind="git"`, `allow_write=True`).
- `projects.json` configured/present but missing, unreadable, corrupt, wrong schema or failing
  any validation -> `ProjectRegistryError` (fail closed; no partial registry).
- Discovery (`scan_workspace_projects`) and Git inspection (`inspect_project_git`) are strictly
  read-only and never write `projects.json` or any repository state.

Phase A never executes work in a non-default project: the worker still runs every task in
`market_brief_starter`, so the coordinator parks such tasks (see `CoordinatorDB`).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

from .win_job import build_sandbox_env
from .worktree import UnsafePathError, _assert_no_link_or_reparse_ancestors, _is_link_or_reparse

DEFAULT_PROJECT_ID = "market_brief_starter"
PROJECTS_FILE_ENV = "AUTO_FACTORY_PROJECTS_FILE"
PROJECTS_FILE_NAME = "projects.json"
PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
PROJECT_KINDS = ("git", "folder")
PROJECT_MAX_RISKS = ("L0", "L1", "L2")  # L3 is never delegated to the pipeline
MAX_PROJECTS = 32
MAX_PROJECTS_FILE_BYTES = 64 * 1024
MAX_LABEL = 64
MAX_REL_ENTRIES = 32
MAX_SCAN_ENTRIES = 64
GIT_TIMEOUT = 5.0

# Immutable global policy: always merged into every project's protected paths.
GLOBAL_PROTECTED_PATHS = (".env", ".git/", "reports/", "holdout_eval/")
GLOBAL_FORBIDDEN_SUFFIXES = (".sqlite3", ".sqlite", ".db", ".db-wal", ".db-shm")
_REL_PATH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_SPEC_KEYS = frozenset(
    {"project_id", "label", "root_path", "kind", "default_tests", "protected_paths", "allow_write", "max_risk"}
)
_REQUIRED_KEYS = frozenset({"project_id", "label", "root_path", "kind"})


class ProjectRegistryError(ValueError):
    """Invalid or unreadable project registry (always fail closed)."""


def validate_project_id(project_id: Any) -> str:
    if not isinstance(project_id, str) or not PROJECT_ID_RE.match(project_id):
        raise ProjectRegistryError("project_id must match ^[a-z0-9][a-z0-9_-]{0,31}$")
    return project_id


def _clean_label(label: Any) -> str:
    if not isinstance(label, str):
        raise ProjectRegistryError("label must be a string")
    s = "".join(ch for ch in label if not (ord(ch) < 32 or ord(ch) == 127)).strip()
    if not s or len(s) > MAX_LABEL:
        raise ProjectRegistryError(f"label must be 1..{MAX_LABEL} printable characters")
    return s


def _validate_rel(raw: Any, what: str, allow_prefix: bool) -> str:
    """Relative, ASCII path: no absolute/drive/UNC, no `..`, no wildcards, no secrets/DBs."""
    if not isinstance(raw, str) or not raw or len(raw) > 200:
        raise ProjectRegistryError(f"{what}: entries must be non-empty strings")
    if not _REL_PATH_RE.match(raw):
        raise ProjectRegistryError(f"{what}: illegal characters in {raw!r} (no wildcards/backslashes/spaces)")
    if raw.startswith("/") or ":" in raw:
        raise ProjectRegistryError(f"{what}: absolute path {raw!r}")
    body = raw[:-1] if (allow_prefix and raw.endswith("/")) else raw
    parts = body.split("/")
    if any(p in ("", ".", "..") for p in parts):
        raise ProjectRegistryError(f"{what}: traversal or empty segment in {raw!r}")
    return raw


def _validate_test_path(raw: Any) -> str:
    p = _validate_rel(raw, "default_tests", allow_prefix=False)
    low = p.lower()
    leaf = low.rsplit("/", 1)[-1]
    if leaf == ".env" or leaf.startswith(".env.") or ".sqlite" in low or low.endswith(GLOBAL_FORBIDDEN_SUFFIXES):
        raise ProjectRegistryError(f"default_tests: forbidden target {p!r}")
    if any(low == g.rstrip("/") or low.startswith(g) for g in GLOBAL_PROTECTED_PATHS if g.endswith("/")):
        raise ProjectRegistryError(f"default_tests: protected location {p!r}")
    return p


def _rel_tuple(raw: Any, what: str, validator) -> tuple[str, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple)) or len(raw) > MAX_REL_ENTRIES:
        raise ProjectRegistryError(f"{what} must be a list of at most {MAX_REL_ENTRIES} entries")
    out: list[str] = []
    for item in raw:
        v = validator(item)
        if v not in out:
            out.append(v)
    return tuple(out)


@dataclass(frozen=True)
class ProjectSpec:
    project_id: str
    label: str
    root_path: Path
    kind: str
    default_tests: tuple[str, ...] = ()
    protected_paths: tuple[str, ...] = field(default=GLOBAL_PROTECTED_PATHS)
    allow_write: bool = False
    max_risk: str = "L2"

    def __post_init__(self) -> None:
        validate_project_id(self.project_id)
        _clean_label(self.label)
        if not isinstance(self.root_path, Path) or not self.root_path.is_absolute():
            raise ProjectRegistryError("root_path must be an absolute Path")
        if self.kind not in PROJECT_KINDS:
            raise ProjectRegistryError(f"kind must be one of {PROJECT_KINDS}")
        if not isinstance(self.allow_write, bool):
            raise ProjectRegistryError("allow_write must be a bool")
        if self.kind == "folder" and self.allow_write:
            raise ProjectRegistryError("folder projects are always read-only (allow_write=False)")
        if self.max_risk not in PROJECT_MAX_RISKS:
            raise ProjectRegistryError(f"max_risk must be one of {PROJECT_MAX_RISKS}")
        if not isinstance(self.default_tests, tuple) or not isinstance(self.protected_paths, tuple):
            raise ProjectRegistryError("default_tests/protected_paths must be tuples")
        for t in self.default_tests:
            _validate_test_path(t)
        for g in GLOBAL_PROTECTED_PATHS:
            if g not in self.protected_paths:
                raise ProjectRegistryError("protected_paths must include the global policy")

    @property
    def writable(self) -> bool:
        """True only for `git` projects explicitly marked `allow_write`."""
        return self.kind == "git" and self.allow_write


def _canonical_root(raw: Any) -> Path:
    """Absolute, existing directory with no symlink/junction/reparse point on the way."""
    if not isinstance(raw, (str, os.PathLike)) or not str(raw).strip():
        raise ProjectRegistryError("root_path must be a non-empty path")
    lexical = Path(os.path.abspath(os.fspath(raw)))
    if not Path(os.fspath(raw)).is_absolute():
        raise ProjectRegistryError("root_path must be absolute")
    for cand in (lexical, *lexical.parents):
        if _is_link_or_reparse(cand):
            raise ProjectRegistryError(f"root_path has a symlink/reparse ancestor: {cand}")
    try:
        canon = _assert_no_link_or_reparse_ancestors(lexical.resolve(strict=True))
    except (OSError, RuntimeError) as exc:
        raise ProjectRegistryError(f"root_path does not exist: {type(exc).__name__}") from None
    except UnsafePathError as exc:
        raise ProjectRegistryError(str(exc)) from None
    if not canon.is_dir():
        raise ProjectRegistryError("root_path must be a directory")
    return canon


def _norm(p: Path) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def _overlaps(a: Path, b: Path) -> bool:
    na, nb = _norm(a), _norm(b)
    if na == nb:
        return True
    sep = os.sep
    return na.startswith(nb.rstrip(sep) + sep) or nb.startswith(na.rstrip(sep) + sep)


def _git(root: Path, *args: str, timeout: float = GIT_TIMEOUT) -> subprocess.CompletedProcess:
    """Read-only git call: no optional locks, no fsmonitor, sanitized env, no stdin."""
    return subprocess.run(
        ["git", "--no-optional-locks", "-c", "core.fsmonitor=false", *args],
        cwd=str(root), env=build_sandbox_env(), stdin=subprocess.DEVNULL,
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout,
    )


def _verify_git_toplevel(root: Path) -> None:
    try:
        proc = _git(root, "rev-parse", "--show-toplevel")
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProjectRegistryError(f"git rev-parse failed: {type(exc).__name__}") from None
    if proc.returncode != 0 or not proc.stdout.strip():
        raise ProjectRegistryError("root_path is not a git work tree")
    try:
        top = Path(proc.stdout.strip()).resolve()
    except (OSError, RuntimeError):
        raise ProjectRegistryError("git toplevel could not be resolved") from None
    if _norm(top) != _norm(root):
        raise ProjectRegistryError("root_path is not the git toplevel (nested or parent repository)")


def build_project(entry: Mapping[str, Any], *, verify_git: bool = True) -> ProjectSpec:
    """Validate one `projects.json` entry into a `ProjectSpec` (raises ProjectRegistryError)."""
    if not isinstance(entry, Mapping):
        raise ProjectRegistryError("each project must be an object")
    keys = set(entry)
    if not _REQUIRED_KEYS <= keys:
        raise ProjectRegistryError(f"missing keys: {sorted(_REQUIRED_KEYS - keys)}")
    if keys - _SPEC_KEYS:
        raise ProjectRegistryError(f"unknown keys: {sorted(keys - _SPEC_KEYS)}")
    pid = validate_project_id(entry["project_id"])
    kind = entry["kind"]
    if kind not in PROJECT_KINDS:
        raise ProjectRegistryError(f"{pid}: kind must be one of {PROJECT_KINDS}")
    allow_write = entry.get("allow_write", False)
    if not isinstance(allow_write, bool):
        raise ProjectRegistryError(f"{pid}: allow_write must be a bool")
    if kind == "folder":
        allow_write = False  # immutable policy: plain folders are never written
    max_risk = entry.get("max_risk", "L2")
    if max_risk not in PROJECT_MAX_RISKS:
        raise ProjectRegistryError(f"{pid}: max_risk must be one of {PROJECT_MAX_RISKS}")
    root = _canonical_root(entry["root_path"])
    tests = _rel_tuple(entry.get("default_tests"), "default_tests", _validate_test_path)
    extra = _rel_tuple(entry.get("protected_paths"), "protected_paths",
                       lambda v: _validate_rel(v, "protected_paths", allow_prefix=True))
    protected = tuple(dict.fromkeys(GLOBAL_PROTECTED_PATHS + extra))
    if kind == "git" and verify_git:
        _verify_git_toplevel(root)
    return ProjectSpec(pid, _clean_label(entry["label"]), root, kind, tests, protected, allow_write, max_risk)


class ProjectRegistry:
    """Immutable, validated set of projects. Iteration order = file order (default first)."""

    def __init__(self, projects: Iterable[ProjectSpec], source: str = "default"):
        items = tuple(projects)
        if not items or len(items) > MAX_PROJECTS:
            raise ProjectRegistryError(f"registry must hold 1..{MAX_PROJECTS} projects")
        seen: dict[str, ProjectSpec] = {}
        for p in items:
            if not isinstance(p, ProjectSpec):
                raise ProjectRegistryError("registry entries must be ProjectSpec")
            if p.project_id in seen:
                raise ProjectRegistryError(f"duplicate project_id {p.project_id}")
            for other in seen.values():
                if _overlaps(p.root_path, other.root_path):
                    raise ProjectRegistryError(
                        f"root_path of {p.project_id} overlaps/nests with {other.project_id}")
            seen[p.project_id] = p
        if DEFAULT_PROJECT_ID not in seen:
            raise ProjectRegistryError(f"registry must include {DEFAULT_PROJECT_ID}")
        self._items = items
        self._by_id = seen
        self.source = source

    def __iter__(self) -> Iterator[ProjectSpec]:
        return iter(self._items)

    def __len__(self) -> int:
        return len(self._items)

    def __contains__(self, project_id: object) -> bool:
        return project_id in self._by_id

    def ids(self) -> tuple[str, ...]:
        return tuple(p.project_id for p in self._items)

    def get(self, project_id: str) -> ProjectSpec | None:
        return self._by_id.get(project_id) if isinstance(project_id, str) else None

    def find_by_root(self, path: Path) -> ProjectSpec | None:
        n = _norm(path)
        for p in self._items:
            if _norm(p.root_path) == n:
                return p
        return None


def default_project(repo_root: str | os.PathLike, *, verify_git: bool = True) -> ProjectSpec:
    return build_project({"project_id": DEFAULT_PROJECT_ID, "label": DEFAULT_PROJECT_ID,
                          "root_path": os.fspath(repo_root), "kind": "git", "allow_write": True,
                          "max_risk": "L2"}, verify_git=verify_git)


def resolve_projects_file(state_dir: str | os.PathLike | None,
                          env: Mapping[str, str] | None = None) -> tuple[Path | None, bool]:
    """(path, explicitly_configured). The env var wins; an empty value counts as unset."""
    env = os.environ if env is None else env
    raw = (env.get(PROJECTS_FILE_ENV) or "").strip()
    if raw:
        return Path(raw), True
    if state_dir is not None:
        return Path(state_dir) / PROJECTS_FILE_NAME, False
    return None, False


def _read_projects_file(path: Path) -> Any:
    p = Path(os.path.abspath(path))
    for cand in (p, *p.parents):
        if _is_link_or_reparse(cand):
            raise ProjectRegistryError("projects.json path has a symlink/reparse point")
    try:
        st = p.stat()
        if not p.is_file():
            raise ProjectRegistryError("projects.json is not a regular file")
        if st.st_size > MAX_PROJECTS_FILE_BYTES:
            raise ProjectRegistryError("projects.json is too large")
        raw = p.read_bytes()
    except ProjectRegistryError:
        raise
    except OSError as exc:
        raise ProjectRegistryError(f"projects.json unreadable: {type(exc).__name__}") from None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ProjectRegistryError("projects.json is not valid UTF-8 JSON") from None


def load_registry(
    repo_root: str | os.PathLike,
    state_dir: str | os.PathLike | None = None,
    env: Mapping[str, str] | None = None,
    *,
    verify_git: bool = True,
) -> ProjectRegistry:
    """Default single-project registry, or the validated `projects.json` (fail closed)."""
    path, configured = resolve_projects_file(state_dir, env)
    if path is None or (not configured and not os.path.lexists(path)):
        return ProjectRegistry([default_project(repo_root, verify_git=verify_git)], "default")
    data = _read_projects_file(path)  # configured-but-missing raises here too
    if not isinstance(data, dict) or set(data) - {"version", "projects"} or data.get("version") != 1:
        raise ProjectRegistryError('projects.json must be {"version": 1, "projects": [...]}')
    entries = data.get("projects")
    if not isinstance(entries, list) or not entries or len(entries) > MAX_PROJECTS:
        raise ProjectRegistryError(f"projects must be a list of 1..{MAX_PROJECTS} objects")
    repo_canon = _canonical_root(repo_root)
    specs: list[ProjectSpec] = []
    for entry in entries:
        spec = build_project(entry, verify_git=verify_git)
        if spec.project_id == DEFAULT_PROJECT_ID and (_norm(spec.root_path) != _norm(repo_canon)
                                                      or spec.kind != "git"):
            raise ProjectRegistryError(f"{DEFAULT_PROJECT_ID} must be the git repo_root")
        specs.append(spec)
    if not any(s.project_id == DEFAULT_PROJECT_ID for s in specs):
        specs.insert(0, default_project(repo_canon, verify_git=verify_git))
    return ProjectRegistry(specs, str(path))


# ------------------------------------------------------------ read-only views
@dataclass(frozen=True)
class DiscoveredProject:
    name: str
    path: Path
    kind: str  # "git" | "folder" | "link" (skipped: never followed)
    registered_id: str | None

    @property
    def status_ko(self) -> str:
        if self.kind == "link":
            return "링크(무시)"
        return "등록됨" if self.registered_id else "미등록(읽기 전용)"


def scan_workspace_projects(base_dir: str | os.PathLike, registry: ProjectRegistry,
                            limit: int = MAX_SCAN_ENTRIES) -> list[DiscoveredProject]:
    """Classify the immediate subdirectories of `base_dir`. Read-only: never writes anything.

    Dot-directories are skipped; links/junctions are reported as `link` and never followed.
    """
    base = Path(os.path.abspath(base_dir))
    out: list[DiscoveredProject] = []
    try:
        entries = sorted(os.scandir(base), key=lambda e: e.name.lower())
    except OSError:
        return out
    for e in entries:
        if len(out) >= limit:
            break
        if e.name.startswith("."):
            continue
        p = base / e.name
        if _is_link_or_reparse(p):
            out.append(DiscoveredProject(e.name, p, "link", None))
            continue
        try:
            if not e.is_dir(follow_symlinks=False):
                continue
            kind = "git" if os.path.lexists(p / ".git") else "folder"
        except OSError:
            continue
        reg = registry.find_by_root(p)
        out.append(DiscoveredProject(e.name, p, kind, reg.project_id if reg else None))
    return out


def inspect_project_git(project: ProjectSpec, timeout: float = GIT_TIMEOUT) -> dict[str, Any]:
    """Read-only `git status --porcelain` + last commit. Never raises; errors are summarized."""
    info: dict[str, Any] = {"ok": False, "kind": project.kind, "dirty": None,
                            "last_commit_ts": None, "last_subject": "", "error": ""}
    if project.kind != "git":
        info["ok"] = True
        return info
    try:
        st = _git(project.root_path, "status", "--porcelain", timeout=timeout)
        if st.returncode != 0:
            info["error"] = "git status failed"
            return info
        info["dirty"] = sum(1 for line in st.stdout.splitlines() if line.strip())
        lg = _git(project.root_path, "log", "-1", "--format=%ct|%s", timeout=timeout)
        if lg.returncode == 0 and lg.stdout.strip():
            ts, _, subject = lg.stdout.strip().partition("|")
            try:
                info["last_commit_ts"] = float(ts)
            except ValueError:
                pass
            info["last_subject"] = subject[:120]
        info["ok"] = True
    except subprocess.TimeoutExpired:
        info["error"] = "git timeout"
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        info["error"] = f"git error ({type(exc).__name__})"
    return info
