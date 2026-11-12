"""Git worktree lifecycle: immutable base_sha, per-task branch, WIP checkpoints, safe cleanup.

Step 3 additions: link/reparse-safe file primitives (no-follow open, atomic write, unlink),
ordered OS file locks (`supervisor` before `merge`), the external state-dir layout and
disposable audit clones with main-repo mutation snapshots.
"""
from __future__ import annotations

import hashlib
import io
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
import uuid
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .win_job import build_sandbox_env

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_BOT_IDENTITY = ["-c", "user.name=auto_factory", "-c", "user.email=auto_factory@localhost"]
# Build/test byproducts must never be swept into a checkpoint commit.
_CHECKPOINT_EXCLUDES = [
    ":(exclude,glob)**/__pycache__/**",
    ":(exclude,glob)**/*.pyc",
    ":(exclude,glob)**/.pytest_cache/**",
    ":(exclude,glob).auto_factory/**",  # handoff contract/prompt live here, never committed
]
HANDOFF_DIRNAME = ".auto_factory"


class WorktreeError(RuntimeError):
    pass


class WorktreeConflict(WorktreeError):
    pass


class MergeError(WorktreeError):
    """Base merge failure. `code` is a stable reason (e.g. REBASE_REQUIRED, MAIN_DIRTY)."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code


class MergePreconditionError(MergeError):
    """A pre-merge check failed; Git was not touched."""


class MergeConflictError(MergeError):
    """`git merge --ff-only` failed; `git merge --abort` was attempted, main is unchanged."""


_FULL_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
FILE_ATTRIBUTE_REPARSE_POINT = 0x400
FILE_ATTRIBUTE_DIRECTORY = 0x10
STOP_FLAG_NAME = "STOP.flag"
SUPERVISOR_LOCK_NAME = "supervisor.lock"
DB_FILENAME = "coordinator.db"


class UnsafePathError(ValueError):
    """A path (or one of its ancestors) is a symlink / reparse point or not a plain file."""


# ======================================================== link / reparse checks
def _is_link_or_reparse(p: Path) -> bool:
    try:
        st = os.lstat(p)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return True
    return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _assert_no_link_or_reparse_ancestors(path: str | os.PathLike) -> Path:
    """Reject `path` if it or ANY ancestor (lexical and canonical) is a symlink/reparse point.

    Returns the canonical absolute path. Non-existent trailing components are fine.
    """
    p = Path(os.path.abspath(path))
    for cand in (p, *p.parents):
        if _is_link_or_reparse(cand):
            raise UnsafePathError(f"symlink/reparse point in path: {cand}")
    canon = p.resolve()
    for cand in (canon, *canon.parents):
        if _is_link_or_reparse(cand):
            raise UnsafePathError(f"symlink/reparse point in canonical path: {cand}")
    if os.path.normcase(str(canon)) != os.path.normcase(str(p)):
        raise UnsafePathError(f"path is not canonical (alias/junction): {p}")
    return canon


def _strip_win_prefix(p: str) -> str:
    if p.startswith("\\\\?\\UNC\\"):
        return "\\\\" + p[8:]
    if p.startswith("\\\\?\\"):
        return p[4:]
    return p


if os.name == "nt":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _GENERIC_READ, _GENERIC_WRITE = 0x80000000, 0x40000000
    _SHARE_ALL = 0x1 | 0x2 | 0x4
    _CREATE_NEW, _OPEN_EXISTING, _OPEN_ALWAYS = 1, 3, 4
    _FLAG_OPEN_REPARSE_POINT = 0x00200000
    _INVALID_HANDLE = ctypes.c_void_p(-1).value

    class _BHFI(ctypes.Structure):
        _fields_ = [
            ("dwFileAttributes", wintypes.DWORD), ("ftCreationTime", wintypes.FILETIME),
            ("ftLastAccessTime", wintypes.FILETIME), ("ftLastWriteTime", wintypes.FILETIME),
            ("dwVolumeSerialNumber", wintypes.DWORD), ("nFileSizeHigh", wintypes.DWORD),
            ("nFileSizeLow", wintypes.DWORD), ("nNumberOfLinks", wintypes.DWORD),
            ("nFileIndexHigh", wintypes.DWORD), ("nFileIndexLow", wintypes.DWORD),
        ]

    _k32.CreateFileW.restype = wintypes.HANDLE
    _k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                 wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _k32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_BHFI)]
    _k32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    _k32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]


def _win_handle_attributes(handle: int) -> tuple[int, str]:
    info = _BHFI()
    if not _k32.GetFileInformationByHandle(handle, ctypes.byref(info)):
        raise ctypes.WinError(ctypes.get_last_error())
    buf = ctypes.create_unicode_buffer(32768)
    n = _k32.GetFinalPathNameByHandleW(handle, buf, 32768, 0)
    if n == 0:
        raise ctypes.WinError(ctypes.get_last_error())
    return int(info.dwFileAttributes), _strip_win_prefix(buf.value)


def _verify_opened(fd: int, expected: Path) -> None:
    """The opened kernel object must be a plain regular file at exactly `expected`."""
    if os.name == "nt":
        attrs, final = _win_handle_attributes(msvcrt.get_osfhandle(fd))
        if attrs & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DIRECTORY):
            raise UnsafePathError(f"opened object is a reparse point/directory: {expected}")
        if os.path.normcase(final) != os.path.normcase(str(expected)):
            raise UnsafePathError(f"opened object path changed (swapped link?): {expected}")
        return
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise UnsafePathError(f"not a regular file: {expected}")
    try:
        lst = os.lstat(expected)
    except OSError as exc:
        raise UnsafePathError(f"file vanished after open: {expected}") from exc
    if (lst.st_dev, lst.st_ino) != (st.st_dev, st.st_ino):
        raise UnsafePathError(f"file swapped between open and verify: {expected}")


def safe_open_fd(path: str | os.PathLike, write: bool = False, create: str = "never") -> int:
    """Open a plain regular file without following links. `create`: never | always | new.

    POSIX uses O_NOFOLLOW (+fstat/lstat identity); Windows uses CreateFileW with
    FILE_FLAG_OPEN_REPARSE_POINT and verifies handle attributes + final path.
    """
    if create not in ("never", "always", "new"):
        raise ValueError(create)
    expected = _assert_no_link_or_reparse_ancestors(path)
    if os.name == "nt":
        access = _GENERIC_READ | (_GENERIC_WRITE if write else 0)
        disp = {"never": _OPEN_EXISTING, "always": _OPEN_ALWAYS, "new": _CREATE_NEW}[create]
        h = _k32.CreateFileW(str(expected), access, _SHARE_ALL, None, disp, 0x80 | _FLAG_OPEN_REPARSE_POINT, None)
        if h is None or h == _INVALID_HANDLE:
            err = ctypes.get_last_error()
            if err in (2, 3):
                raise FileNotFoundError(2, "No such file", str(expected))
            if err == 80:
                raise FileExistsError(17, "File exists", str(expected))
            raise ctypes.WinError(err)
        try:
            fd = msvcrt.open_osfhandle(h, (os.O_RDWR if write else os.O_RDONLY) | os.O_BINARY)
        except Exception:
            _k32.CloseHandle(h)
            raise
    else:
        flags = (os.O_RDWR if write else os.O_RDONLY) | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
        if create == "always":
            flags |= os.O_CREAT
        elif create == "new":
            flags |= os.O_CREAT | os.O_EXCL
        fd = os.open(str(expected), flags, 0o600)
    try:
        _verify_opened(fd, expected)
        _assert_no_link_or_reparse_ancestors(expected)  # ancestors re-checked after open
    except BaseException:
        os.close(fd)
        raise
    return fd


def safe_read_bytes(path: str | os.PathLike, max_bytes: int) -> bytes:
    fd = safe_open_fd(path)
    with os.fdopen(fd, "rb") as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise UnsafePathError(f"file exceeds {max_bytes} bytes: {path}")
    return data


def safe_write_atomic(path: str | os.PathLike, data: bytes | str) -> Path:
    """Write via an exclusive no-follow temp file in the same dir, then atomic rename."""
    if isinstance(data, str):
        data = data.encode("utf-8")
    target = _assert_no_link_or_reparse_ancestors(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_link_or_reparse_ancestors(target.parent)
    if _is_link_or_reparse(target) or (target.exists() and not target.is_file()):
        raise UnsafePathError(f"refusing to overwrite link/reparse/non-file: {target}")
    tmp = target.parent / f".{target.name}.{uuid.uuid4().hex[:12]}.tmp"
    fd = safe_open_fd(tmp, write=True, create="new")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if _is_link_or_reparse(target):
            raise UnsafePathError(f"target became a link/reparse point: {target}")
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return target


def safe_unlink(path: str | os.PathLike, missing_ok: bool = True) -> bool:
    """Remove a plain file; refuse links/reparse points (fail closed: the file stays)."""
    target = _assert_no_link_or_reparse_ancestors(path)
    if not os.path.lexists(target):
        if missing_ok:
            return False
        raise FileNotFoundError(str(target))
    if not target.is_file():
        raise UnsafePathError(f"refusing to unlink non-file: {target}")
    os.unlink(target)
    return True


# ================================================================== file locks
class LockOrderError(RuntimeError):
    """Lock acquired out of order (`supervisor` must precede `merge`) or a required lock is missing."""


class SupervisorLocked(RuntimeError):
    code = "SUPERVISOR_LOCKED"


_LOCK_ROLES = ("supervisor", "merge", "other")
_tls = threading.local()
_HELD_GUARD = threading.Lock()
_PROCESS_HELD: dict[str, list[str]] = {}  # canonical lock path -> roles held by this process


def _thread_stack() -> list[str]:
    st = getattr(_tls, "stack", None)
    if st is None:
        st = _tls.stack = []
    return st


def supervisor_lock_held() -> bool:
    with _HELD_GUARD:
        return any("supervisor" in roles for roles in _PROCESS_HELD.values())


def assert_supervisor_lock_held() -> None:
    if not supervisor_lock_held():
        raise LockOrderError("supervisor.lock must be held by this process for merge/reconcile")


class _FileLock:
    """Non-blocking exclusive OS file lock (released by the OS if the process dies).

    `role` enforces lock ordering per thread: `supervisor` may never be taken while the
    same thread holds `merge` (LockOrderError). The lock file is opened no-follow.
    """

    def __init__(self, path: Path, role: str = "other"):
        if role not in _LOCK_ROLES:
            raise ValueError(f"unknown lock role {role!r}")
        self.path = Path(path)
        self.role = role
        self.fh = None

    # ------------------------------------------------------------ primitives
    def _busy_error(self) -> Exception:
        if self.role == "supervisor":
            return SupervisorLocked(f"SUPERVISOR_LOCKED: another supervisor/bot/worker holds {self.path}")
        return MergePreconditionError("MERGE_LOCKED", f"another merge holds {self.path}")

    @staticmethod
    def _os_lock(fh) -> None:
        if os.name == "nt":
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _os_unlock(fh) -> None:
        if os.name == "nt":
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _open(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return os.fdopen(safe_open_fd(self.path, write=True, create="always"), "r+b")

    # -------------------------------------------------------------- public
    def acquire(self) -> "_FileLock":
        if self.fh is not None:
            raise RuntimeError("lock already acquired by this object")
        if self.role == "supervisor" and "merge" in _thread_stack():
            raise LockOrderError("lock order violation: supervisor.lock requested while merge.lock is held")
        fh = self._open()
        try:
            self._os_lock(fh)
        except OSError as exc:
            fh.close()
            raise self._busy_error() from exc
        self.fh = fh
        _thread_stack().append(self.role)
        with _HELD_GUARD:
            _PROCESS_HELD.setdefault(os.path.normcase(str(self.path.resolve())), []).append(self.role)
        return self

    def release(self) -> None:
        if self.fh is None:
            return
        try:
            self._os_unlock(self.fh)
        finally:
            self.fh.close()
            self.fh = None
            stack = _thread_stack()
            for i in range(len(stack) - 1, -1, -1):
                if stack[i] == self.role:
                    del stack[i]
                    break
            key = os.path.normcase(str(self.path.resolve()))
            with _HELD_GUARD:
                roles = _PROCESS_HELD.get(key, [])
                if self.role in roles:
                    roles.remove(self.role)
                if not roles:
                    _PROCESS_HELD.pop(key, None)

    def try_acquire(self) -> bool:
        try:
            self.acquire()
            return True
        except (SupervisorLocked, MergePreconditionError):
            return False

    def is_held(self) -> bool:
        """Non-blocking probe: True if ANY holder (including this process) has the lock.

        A missing lock file means "not held" and is never created by the probe.
        """
        if self.fh is not None:
            return True
        if not os.path.lexists(self.path):
            return False
        with _HELD_GUARD:
            if os.path.normcase(str(self.path.resolve())) in _PROCESS_HELD:
                return True
        fh = os.fdopen(safe_open_fd(self.path, write=True, create="never"), "r+b")
        try:
            try:
                self._os_lock(fh)
            except OSError:
                return True
            self._os_unlock(fh)
            return False
        finally:
            fh.close()

    def __enter__(self) -> "_FileLock":
        return self.acquire()

    def __exit__(self, *exc) -> None:
        self.release()


# ================================================================ state layout
_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")
_FORBIDDEN_STATE_PARTS = ("reports", ".env", "holdout_eval")


def default_state_dir(repo_root: str | os.PathLike) -> Path:
    """`%LOCALAPPDATA%/auto_factory/<slug>-<sha256(canonical repo)[:8]>` (or ~/.local/state/...)."""
    canon = Path(repo_root).resolve()
    slug = _SLUG_RE.sub("_", canon.name).strip("._-")[:40] or "repo"
    digest = hashlib.sha256(os.path.normcase(str(canon)).encode("utf-8")).hexdigest()[:8]
    if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
        base = Path(os.environ["LOCALAPPDATA"])
    else:
        base = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    state = base / "auto_factory" / f"{slug}-{digest}"
    if _is_within(state, canon):
        raise ValueError("default state dir resolves inside the repository")
    return state


def validate_state_layout(
    repo_root: str | os.PathLike,
    db_path: str | os.PathLike,
    artifacts_dir: str | os.PathLike,
    worktrees_dir: str | os.PathLike,
    stop_flag: str | os.PathLike,
    lock_path: str | os.PathLike | None = None,
) -> dict[str, Path]:
    """Validate the whole external state layout; returns canonical paths or raises ValueError."""
    root = Path(repo_root).resolve()
    db = Path(db_path)
    state_dir = Path(os.path.abspath(db)).parent
    paths = {
        "db": db,
        "artifacts": Path(artifacts_dir),
        "worktrees": Path(worktrees_dir),
        "stop_flag": Path(stop_flag),
        "lock": Path(lock_path) if lock_path else state_dir / SUPERVISOR_LOCK_NAME,
    }
    out: dict[str, Path] = {}
    for key, p in paths.items():
        try:
            canon = _assert_no_link_or_reparse_ancestors(p)
        except UnsafePathError as exc:
            raise ValueError(f"{key}: {exc}") from None
        lowered = [part.lower() for part in canon.parts]
        if canon.suffix.lower() == ".sqlite3" or any(part.endswith(".sqlite3") for part in lowered):
            raise ValueError(f"{key}: *.sqlite3 product database paths are forbidden")
        if any(part in _FORBIDDEN_STATE_PARTS for part in lowered):
            raise ValueError(f"{key}: reports/, .env and holdout_eval/ are forbidden")
        if canon.exists() and _is_link_or_reparse(canon):
            raise ValueError(f"{key}: symlink/reparse point")
        inside = _is_within(canon, root)
        if key == "stop_flag":
            if canon.name != STOP_FLAG_NAME:
                raise ValueError("stop_flag must be named STOP.flag")
            if canon.parent not in (root, state_dir.resolve()):
                raise ValueError("stop_flag must live in repo_root or the state dir")
        elif inside:
            raise ValueError(f"{key} must live outside the repository")
        out[key] = canon
    if out["db"].exists() and not out["db"].is_file():
        raise ValueError("db: not a regular file")
    return out


@dataclass(frozen=True)
class WorktreeInfo:
    task_id: str
    path: str
    branch: str
    base_sha: str


@dataclass(frozen=True)
class ChangedFile:
    status: str  # A, M, D, R, C, T ...
    path: str
    old_path: str | None = None


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


class WorktreeManager:
    def __init__(self, repo_root: str | os.PathLike, worktrees_dir: str | os.PathLike | None = None):
        self.repo_root = Path(repo_root).resolve()
        if worktrees_dir is None:
            worktrees_dir = self.repo_root.parent / ".auto_factory_worktrees" / self.repo_root.name
        self.worktrees_dir = Path(worktrees_dir).resolve()
        if _is_within(self.worktrees_dir, self.repo_root):
            raise WorktreeError("worktrees_dir must live outside the repository")

    # ------------------------------------------------------------------ git
    def git(self, *args: str, cwd: str | os.PathLike | None = None, check: bool = True) -> str:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.repo_root),
            env=build_sandbox_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if check and proc.returncode != 0:
            raise WorktreeError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")
        return proc.stdout

    def git_bytes(self, *args: str, cwd: str | os.PathLike | None = None) -> bytes:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd or self.repo_root),
            env=build_sandbox_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
        )
        if proc.returncode != 0:
            raise WorktreeError(f"git {' '.join(args)} failed: {proc.stderr!r}")
        return proc.stdout

    def head_sha(self, cwd: str | os.PathLike | None = None) -> str:
        return self.git("rev-parse", "HEAD", cwd=cwd).strip()

    # ------------------------------------------------------------- lifecycle
    def path_for(self, task_id: str) -> Path:
        return self.worktrees_dir / task_id

    @staticmethod
    def branch_for(task_id: str) -> str:
        return f"auto/{task_id}"

    def create(self, task_id: str, base_sha: str | None = None) -> WorktreeInfo:
        if not _TASK_ID_RE.match(task_id):
            raise WorktreeError(f"invalid task id: {task_id!r}")
        base_sha = base_sha or self.head_sha()
        path = self.path_for(task_id)
        branch = self.branch_for(task_id)
        if path.exists():
            raise WorktreeConflict(f"worktree path already exists: {path}")
        if self.git("branch", "--list", branch).strip():
            raise WorktreeConflict(f"branch already exists: {branch}")
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        # Built from the commit only: dirty changes in the main workspace never leak in.
        self.git("worktree", "add", "-b", branch, str(path), base_sha)
        actual = self.head_sha(cwd=path)
        if actual != base_sha:
            raise WorktreeError(f"worktree HEAD {actual} != base {base_sha}")
        return WorktreeInfo(task_id=task_id, path=str(path), branch=branch, base_sha=base_sha)

    def attach(self, task_id: str, base_sha: str) -> WorktreeInfo:
        """Re-open an existing worktree (boot recovery)."""
        path = self.path_for(task_id)
        if not path.is_dir():
            raise WorktreeError(f"worktree missing: {path}")
        return WorktreeInfo(task_id, str(path), self.branch_for(task_id), base_sha)

    def is_dirty(self, wt_path: str | os.PathLike) -> bool:
        out = self.git(
            "status", "--porcelain", "--untracked-files=all", "--", ".", *_CHECKPOINT_EXCLUDES, cwd=wt_path
        )
        return bool(out.strip())

    def commit_wip_checkpoint(self, wt_path: str | os.PathLike, msg: str) -> str:
        """Stage everything (minus byproducts) and commit; always yields a fresh checkpoint SHA."""
        self.git("add", "-A", "--", ".", *_CHECKPOINT_EXCLUDES, cwd=wt_path)
        self.git(
            *_BOT_IDENTITY, "commit", "--no-verify", "--allow-empty", "-q", "-m", f"WIP(auto_factory): {msg}",
            cwd=wt_path,
        )
        return self.head_sha(cwd=wt_path)

    def changed_files(self, wt_path: str | os.PathLike, base_sha: str) -> list[ChangedFile]:
        out = self.git("diff", "--name-status", "-M", "-z", base_sha, "HEAD", cwd=wt_path)
        parts = [p for p in out.split("\0") if p]
        changed: list[ChangedFile] = []
        i = 0
        while i < len(parts):
            status = parts[i]
            code = status[0]
            if code in ("R", "C"):
                changed.append(ChangedFile(code, parts[i + 2], parts[i + 1]))
                i += 3
            else:
                changed.append(ChangedFile(code, parts[i + 1]))
                i += 2
        return changed

    def diff_bytes(self, wt_path: str | os.PathLike, base_sha: str) -> bytes:
        return self.git_bytes("diff", "--binary", base_sha, "HEAD", cwd=wt_path)

    def diff_sha256(self, wt_path: str | os.PathLike, base_sha: str) -> str:
        return hashlib.sha256(self.diff_bytes(wt_path, base_sha)).hexdigest()

    def show_file(self, wt_path: str | os.PathLike, rev: str, rel_path: str) -> str | None:
        proc = subprocess.run(
            ["git", "show", f"{rev}:{rel_path}"],
            cwd=str(wt_path),
            env=build_sandbox_env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
        )
        if proc.returncode != 0:
            return None
        return proc.stdout.decode("utf-8", errors="replace")

    def list_worktrees(self) -> list[dict[str, str]]:
        entries: list[dict[str, str]] = []
        cur: dict[str, str] = {}
        for line in self.git("worktree", "list", "--porcelain").splitlines():
            if not line.strip():
                if cur:
                    entries.append(cur)
                cur = {}
                continue
            key, _, value = line.partition(" ")
            cur[key] = value
        if cur:
            entries.append(cur)
        return entries

    # ----------------------------------------------------------------- merge
    @property
    def merge_lock_path(self) -> Path:
        return self.worktrees_dir.parent / "merge.lock"

    def main_is_clean(self) -> bool:
        return self.git("status", "--porcelain=v1", "--untracked-files=all") == ""

    def merge_candidate(self, task: dict, guard_checker) -> str:
        """Fast-forward main to the approved candidate. Git only; the DB is finalized by the caller.

        Every precondition is re-verified live under an exclusive file lock; any mismatch
        fails closed *before* Git is touched. On a failed merge `git merge --abort` runs so
        no merge state is left behind. Cleanup is deliberately NOT done here: the caller
        finalizes `MERGED` first and only then cleans up (cleanup failure != merge failure).
        Returns the new main HEAD (== target_sha).
        """
        target = task.get("target_sha") or ""
        base = task.get("base_sha") or ""
        wt = task.get("worktree_path")
        if not _FULL_SHA_RE.match(target) or not _FULL_SHA_RE.match(base):
            raise MergePreconditionError("BAD_SHA", "base/target must be full 40-hex commit SHAs")
        with _FileLock(self.merge_lock_path, role="merge"):
            if not self.main_is_clean():
                raise MergePreconditionError("MAIN_DIRTY", "main worktree has uncommitted/untracked files")
            head = self.head_sha()
            if head != base:
                raise MergePreconditionError("REBASE_REQUIRED", f"main HEAD {head[:12]} != base {base[:12]}")
            if not wt or not Path(wt).is_dir():
                raise MergePreconditionError("CANDIDATE_MISSING", "candidate worktree is gone")
            if self.is_dirty(wt):
                raise MergePreconditionError("CANDIDATE_DIRTY", "candidate worktree has uncommitted changes")
            if self.head_sha(cwd=wt) != target:
                raise MergePreconditionError("TARGET_MISMATCH", "candidate HEAD != approved target_sha")
            if self.diff_sha256(wt, base) != task.get("diff_sha256"):
                raise MergePreconditionError("DIFF_MISMATCH", "live diff != approved diff_sha256")
            report = guard_checker.check_worktree(wt, base, task.get("allowed_paths"))
            if not report.ok:
                raise MergePreconditionError("GUARDRAIL", f"violations: {sorted(report.codes())}")
            proc = subprocess.run(
                ["git", "merge", "--ff-only", "--no-edit", target],
                cwd=str(self.repo_root), env=build_sandbox_env(), stdin=subprocess.DEVNULL,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            if proc.returncode != 0:
                self.git("merge", "--abort", check=False)
                raise MergeConflictError("MERGE_FAILED", proc.stderr.strip()[:300])
            new_head = self.head_sha()
            if new_head != target:
                raise MergeConflictError("POST_MERGE_MISMATCH", f"main HEAD {new_head[:12]} != target")
            return new_head

    def cleanup(
        self,
        info: WorktreeInfo,
        artifacts_dir: str | os.PathLike,
        delete_branch: bool = False,
    ) -> Path:
        """Preserve the diff as a patch artifact, step out of the worktree, then remove it."""
        wt = Path(info.path)
        artifacts = Path(artifacts_dir)
        artifacts.mkdir(parents=True, exist_ok=True)
        patch = artifacts / f"{info.task_id}.patch"
        if wt.is_dir():
            safe_write_atomic(patch, self.diff_bytes(wt, info.base_sha))
        if _is_within(Path.cwd(), wt):
            os.chdir(self.repo_root)
        if wt.is_dir():
            self.git("worktree", "remove", "--force", str(wt))
        self.git("worktree", "prune")
        if delete_branch:
            self.git("branch", "-D", info.branch, check=False)
        return patch

    # ------------------------------------------------- disposable audit clones
    def create_disposable_clone(self, base_sha: str, tmp_dir: str | os.PathLike) -> Path:
        """Fresh standalone clone at `base_sha` (NO `git worktree`, no shared object store)."""
        if not _FULL_SHA_RE.match(base_sha or ""):
            raise WorktreeError("disposable clone needs a full 40-hex base_sha")
        dest = Path(tmp_dir)
        if _is_within(dest, self.repo_root) or _is_within(self.repo_root, dest):
            raise WorktreeError("disposable clone must live outside the repository")
        if dest.exists() and any(dest.iterdir()):
            raise WorktreeError(f"disposable clone dir is not empty: {dest}")
        _assert_no_link_or_reparse_ancestors(dest)
        self.git("clone", "--quiet", "--no-hardlinks", "--no-checkout", str(self.repo_root), str(dest))
        self.git("-c", "advice.detachedHead=false", "checkout", "--quiet", "--detach", base_sha, cwd=dest)
        return dest.resolve()

    def remove_disposable_clone(self, path: str | os.PathLike) -> None:
        dest = Path(path)
        if not dest.exists():
            return
        if _is_within(dest, self.repo_root) or _is_within(self.repo_root, dest):
            raise WorktreeError("refusing to remove a path overlapping the repository")
        _rmtree_force(dest)
        if dest.exists():
            raise WorktreeError(f"disposable clone still present: {dest}")

    def export_clean_snapshot(self, base_sha: str, tmp_dir: str | os.PathLike) -> Path:
        """Materialize `base_sha` as a plain file tree WITHOUT `.git`, secrets, DBs or scratch notes.

        Built from `git archive` (committed content only), so dirty workspace files never leak.
        Any symlink / special file / Windows reparse entry in the archive fails closed.
        """
        from .guardrails import is_qa_secret_path, normalize_rel_segments

        if not _FULL_SHA_RE.match(base_sha or ""):
            raise WorktreeError("clean snapshot needs a full 40-hex base_sha")
        dest = Path(tmp_dir)
        if _is_within(dest, self.repo_root) or _is_within(self.repo_root, dest):
            raise WorktreeError("clean snapshot must live outside the repository")
        if dest.exists() and any(dest.iterdir()):
            raise WorktreeError(f"clean snapshot dir is not empty: {dest}")
        _assert_no_link_or_reparse_ancestors(dest)
        dest.mkdir(parents=True, exist_ok=True)
        root = _assert_no_link_or_reparse_ancestors(dest)
        proc = subprocess.run(
            ["git", "archive", "--format=zip", base_sha], cwd=str(self.repo_root), env=build_sandbox_env(),
            stdin=subprocess.DEVNULL, capture_output=True, check=True,
        )
        with zipfile.ZipFile(io.BytesIO(proc.stdout)) as zf:
            for zi in zf.infolist():
                _reject_unsafe_zip_entry(zi)
                raw = zi.filename
                is_dir = zi.is_dir() or raw.endswith(("/", "\\"))
                try:
                    parts = normalize_rel_segments(raw)
                except ValueError as exc:
                    raise UnsafePathError(f"unsafe archive entry {raw!r}: {exc}") from None
                rel = "/".join(parts)
                if is_qa_secret_path(rel, is_dir=is_dir):
                    continue
                if is_dir:
                    _mkdirs_plain(root, parts)
                    continue
                parent = _mkdirs_plain(root, parts[:-1])
                _write_new_plain_file(parent / parts[-1], zf.read(zi))
        for cur, dirs, files in os.walk(root, topdown=True, followlinks=False):
            for name in dirs + files:
                p = Path(cur) / name
                rel = p.relative_to(root).as_posix()
                if _is_link_or_reparse(p):
                    raise UnsafePathError(f"link/reparse point in clean snapshot: {rel}")
                if is_qa_secret_path(rel, is_dir=name in dirs):
                    raise UnsafePathError(f"forbidden path in clean snapshot: {rel}")
        return root.resolve()

    def clone_snapshot(self, clone: str | os.PathLike) -> dict[str, str]:
        return {
            "head": self.head_sha(cwd=clone),
            "status": self.git("--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=all",
                               "--ignored", cwd=clone),
        }

    def main_snapshot(self) -> dict[str, Any]:
        """HEAD + full status (incl. ignored) + hashes of Git control files (refs/config/hooks).

        Volatile `.git/index`, `.git/logs/`, `.git/worktrees/`, `objects/` and `auto/*` task
        branches are excluded (they legitimately change with worktree bookkeeping).
        """
        status = self.git("--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=all", "--ignored")
        git_dir = Path(self.git("rev-parse", "--absolute-git-dir").strip())
        common = Path(self.git("rev-parse", "--git-common-dir").strip())
        if not common.is_absolute():
            common = (self.repo_root / common).resolve()
        control: dict[str, str] = {}
        for base in dict.fromkeys([git_dir, common]):
            for name in ("HEAD", "config", "MERGE_HEAD", "ORIG_HEAD", "CHERRY_PICK_HEAD", "REVERT_HEAD", "shallow"):
                control[f"{base.name}:{name}"] = _hash_file(base / name)
            packed = base / "packed-refs"
            if packed.is_file():
                lines = [ln for ln in packed.read_bytes().splitlines() if b"refs/heads/auto/" not in ln]
                control[f"{base.name}:packed-refs"] = hashlib.sha256(b"\n".join(lines)).hexdigest()
            for sub in ("refs", "hooks", "info"):
                root = base / sub
                if not root.is_dir():
                    continue
                for f in sorted(root.rglob("*")):
                    rel = f.relative_to(base).as_posix()
                    if rel.startswith("refs/heads/auto/") or not f.is_file():
                        continue
                    control[f"{base.name}:{rel}"] = _hash_file(f)
        return {
            "head": self.head_sha(),
            "status_sha256": hashlib.sha256(status.encode("utf-8")).hexdigest(),
            "control": control,
        }


_WIN_ZIP_SYSTEMS = (0, 10, 14)  # MS-DOS/FAT, NTFS, VFAT
_SPECIAL_FILE_TYPES = frozenset({stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR, stat.S_IFBLK, stat.S_IFSOCK})


def _reject_unsafe_zip_entry(zi: zipfile.ZipInfo) -> None:
    """Symlinks, special files and DOS reparse-point entries are never extracted."""
    mode = (zi.external_attr >> 16) & 0xFFFF
    ftype = stat.S_IFMT(mode)
    if ftype == stat.S_IFLNK or stat.S_ISLNK(mode):
        raise UnsafePathError(f"symlink entry in archive: {zi.filename!r}")
    if ftype and (ftype in _SPECIAL_FILE_TYPES or ftype not in (stat.S_IFREG, stat.S_IFDIR)):
        raise UnsafePathError(f"special file entry in archive: {zi.filename!r}")
    if zi.create_system in _WIN_ZIP_SYSTEMS and (zi.external_attr & 0xFFFF) & FILE_ATTRIBUTE_REPARSE_POINT:
        raise UnsafePathError(f"reparse-point entry in archive: {zi.filename!r}")


def _mkdirs_plain(root: Path, parts: tuple[str, ...] | list[str]) -> Path:
    """Create `root/<parts>` one level at a time; every level must be a real directory."""
    cur = root
    for seg in parts:
        cur = cur / seg
        if not os.path.lexists(cur):
            os.mkdir(cur)
        if _is_link_or_reparse(cur) or not cur.is_dir():
            raise UnsafePathError(f"not a plain directory: {cur}")
    return cur


def _write_new_plain_file(path: Path, data: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(str(path), flags, 0o644)
    except FileExistsError:
        raise UnsafePathError(f"duplicate/colliding archive entry: {path}") from None
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
    if _is_link_or_reparse(path) or not path.is_file():
        raise UnsafePathError(f"written entry is not a plain file: {path}")


def snapshot_tree(root: str | os.PathLike) -> dict[str, tuple[int, str]]:
    """Deterministic `{rel_posix: (size, sha256)}` of a plain tree (dirs as `(-1, "")`).

    Fails closed (UnsafePathError) on any symlink / reparse point, including the root itself.
    """
    base = Path(root)
    if _is_link_or_reparse(base):
        raise UnsafePathError(f"snapshot root is a link/reparse point: {base}")
    out: dict[str, tuple[int, str]] = {}
    for cur, dirs, files in os.walk(base, topdown=True, followlinks=False):
        dirs.sort()
        for name in dirs:
            p = Path(cur) / name
            if _is_link_or_reparse(p):
                raise UnsafePathError(f"link/reparse point in tree: {p}")
            out[p.relative_to(base).as_posix()] = (-1, "")
        for name in sorted(files):
            p = Path(cur) / name
            if _is_link_or_reparse(p):
                raise UnsafePathError(f"link/reparse point in tree: {p}")
            data = p.read_bytes()
            out[p.relative_to(base).as_posix()] = (len(data), hashlib.sha256(data).hexdigest())
    return dict(sorted(out.items()))


def _hash_file(p: Path) -> str:
    try:
        return hashlib.sha256(p.read_bytes()).hexdigest()
    except FileNotFoundError:
        return "absent"
    except OSError as exc:
        return f"error:{type(exc).__name__}"


def _rmtree_force(path: Path) -> None:
    """rmtree that clears read-only bits (Git pack files are read-only on Windows)."""

    def retry(func, p, _exc):
        try:
            os.chmod(p, stat.S_IWRITE | stat.S_IREAD)
            func(p)
        except OSError:
            pass

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:  # pragma: no cover - older interpreters
        shutil.rmtree(path, onerror=retry)
