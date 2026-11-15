"""Process isolation: Windows Job Object (KILL_ON_JOB_CLOSE), timeouts, sandbox env.

On Windows every provider process is created suspended, assigned to a Job Object
and only then resumed, so grandchildren can never escape the job. Closing or
terminating the job kills the whole tree. Non-Windows platforms fall back to a
POSIX process group (`start_new_session` + `killpg`).
"""
from __future__ import annotations

import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

IS_WINDOWS = sys.platform == "win32"

DEFAULT_WALL_TIMEOUT = 600.0
DEFAULT_IDLE_TIMEOUT = 180.0
_MAX_CAPTURE_BYTES = 2 * 1024 * 1024

ENV_ALLOWLIST = frozenset(
    {
        "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC",
        "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
        "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
        "COMMONPROGRAMFILES", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
        "LANG", "LC_ALL", "LC_CTYPE", "TZ", "USERNAME", "USER", "LOGNAME", "SHELL", "TERM",
    }
)
SECRET_NAME_RE = re.compile(
    r"(TOKEN|SECRET|PASSW|API_?KEY|CREDENTIAL|PRIVATE|AUTH|COOKIE|DSN|DATABASE_URL)", re.I
)


def build_sandbox_env(
    extra: Mapping[str, str] | None = None,
    passthrough: Sequence[str] = (),
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Allowlist-based child env. Secrets are never inherited; Telegram is always disabled."""
    base = os.environ if base is None else base
    env: dict[str, str] = {}
    for k, v in base.items():
        if k.upper() in ENV_ALLOWLIST and not SECRET_NAME_RE.search(k):
            env[k] = v
    for k in passthrough:
        if k in base:
            env[k] = base[k]
    for k, v in (extra or {}).items():
        if SECRET_NAME_RE.search(k) and k not in passthrough:
            raise ValueError(f"refusing secret-like env var in sandbox: {k}")
        env[k] = str(v)
    # Telegram credentials are always scrubbed, even when explicitly passed through.
    env["TELEGRAM_BOT_TOKEN"] = ""
    env["AUTO_FACTORY_DEV_BOT_TOKEN"] = ""
    env["AUTO_FACTORY_ADMIN_CHAT_ID"] = ""
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["AUTO_FACTORY_SANDBOX"] = "1"
    return env


# --------------------------------------------------------------------- Win32
if IS_WINDOWS:
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")

    JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000
    JobObjectExtendedLimitInformation = 9
    PROCESS_TERMINATE = 0x0001
    PROCESS_SET_QUOTA = 0x0100
    PROCESS_SUSPEND_RESUME = 0x0800
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    CREATE_SUSPENDED = 0x00000004
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_NO_WINDOW = 0x08000000
    STILL_ACTIVE = 259

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [
            (n, ctypes.c_ulonglong)
            for n in (
                "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                "ReadTransferCount", "WriteTransferCount", "OtherTransferCount",
            )
        ]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("PerProcessUserTimeLimit", ctypes.c_int64),
            ("PerJobUserTimeLimit", ctypes.c_int64),
            ("LimitFlags", wintypes.DWORD),
            ("MinimumWorkingSetSize", ctypes.c_size_t),
            ("MaximumWorkingSetSize", ctypes.c_size_t),
            ("ActiveProcessLimit", wintypes.DWORD),
            ("Affinity", ctypes.c_size_t),
            ("PriorityClass", wintypes.DWORD),
            ("SchedulingClass", wintypes.DWORD),
        ]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
            ("IoInfo", IO_COUNTERS),
            ("ProcessMemoryLimit", ctypes.c_size_t),
            ("JobMemoryLimit", ctypes.c_size_t),
            ("PeakProcessMemoryUsed", ctypes.c_size_t),
            ("PeakJobMemoryUsed", ctypes.c_size_t),
        ]

    _kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    _kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD
    ]
    _kernel32.SetInformationJobObject.restype = wintypes.BOOL
    _kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    _kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _kernel32.TerminateJobObject.restype = wintypes.BOOL
    _kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    _kernel32.OpenProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.CloseHandle.restype = wintypes.BOOL
    _kernel32.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    _kernel32.GetProcessTimes.restype = wintypes.BOOL
    _kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    _kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long


class JobObjectError(OSError):
    pass


class JobObject:
    """Win32 Job Object with KILL_ON_JOB_CLOSE. No-op handle on non-Windows."""

    def __init__(self) -> None:
        self.handle = None
        if not IS_WINDOWS:
            return
        handle = _kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise JobObjectError(ctypes.get_last_error(), "CreateJobObjectW failed")
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not _kernel32.SetInformationJobObject(
            handle, JobObjectExtendedLimitInformation, ctypes.byref(info), ctypes.sizeof(info)
        ):
            err = ctypes.get_last_error()
            _kernel32.CloseHandle(handle)
            raise JobObjectError(err, "SetInformationJobObject failed")
        self.handle = handle

    def assign(self, pid: int) -> None:
        if not self.handle:
            return
        ph = _kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if not ph:
            raise JobObjectError(ctypes.get_last_error(), "OpenProcess failed")
        try:
            if not _kernel32.AssignProcessToJobObject(self.handle, ph):
                raise JobObjectError(ctypes.get_last_error(), "AssignProcessToJobObject failed")
        finally:
            _kernel32.CloseHandle(ph)

    def terminate(self, exit_code: int = 1) -> bool:
        if not self.handle:
            return False
        return bool(_kernel32.TerminateJobObject(self.handle, exit_code))

    def close(self) -> None:
        if self.handle:
            _kernel32.CloseHandle(self.handle)  # KILL_ON_JOB_CLOSE reaps survivors
            self.handle = None


def _resume_process(pid: int) -> None:
    ph = _kernel32.OpenProcess(PROCESS_SUSPEND_RESUME, False, pid)
    if not ph:
        raise JobObjectError(ctypes.get_last_error(), "OpenProcess(resume) failed")
    try:
        status = _ntdll.NtResumeProcess(ph)
        if status != 0:
            raise JobObjectError(status, "NtResumeProcess failed")
    finally:
        _kernel32.CloseHandle(ph)


# ------------------------------------------------------------ process identity
def process_start_time(pid: int) -> float | None:
    """Creation time of `pid` (epoch seconds on Windows), or None if not running."""
    if pid is None or pid <= 0:
        return None
    if IS_WINDOWS:
        ph = _kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not ph:
            return None
        try:
            code = wintypes.DWORD()
            if not _kernel32.GetExitCodeProcess(ph, ctypes.byref(code)) or code.value != STILL_ACTIVE:
                return None
            times = [wintypes.FILETIME() for _ in range(4)]
            if not _kernel32.GetProcessTimes(ph, *[ctypes.byref(t) for t in times]):
                return None
            ft = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
            return (ft - 116444736000000000) / 1e7
        finally:
            _kernel32.CloseHandle(ph)
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            fields = fh.read().rsplit(b")", 1)[1].split()
        return float(fields[19])  # starttime in clock ticks since boot
    except OSError:
        try:
            os.kill(pid, 0)
        except OSError:
            return None
        return 0.0


def is_same_process(pid: int | None, start_time: float | None) -> bool:
    """True only if `pid` is alive AND is the very process recorded (no PID reuse)."""
    if pid is None:
        return False
    current = process_start_time(pid)
    if current is None:
        return False
    if start_time is None:
        return False
    return abs(current - start_time) < 0.01


def current_identity() -> tuple[int, float | None]:
    pid = os.getpid()
    return pid, process_start_time(pid)


# --------------------------------------------------------------------- runner
@dataclass
class RunResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool = False
    timeout_kind: str | None = None  # "wall" | "idle" | "stop"
    pid: int | None = None
    start_time: float | None = None
    duration: float = 0.0
    job_contained: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


class _Reader(threading.Thread):
    """Drains one pipe into a bounded buffer (last `_MAX_CAPTURE_BYTES`).

    The optional progress sink only receives in-memory counters (`sink.output`); the reader
    never performs file or DB I/O, and sink failures never stop the drain.
    """

    def __init__(self, pipe, activity: list[float], sink=None, stream: str = ""):
        super().__init__(daemon=True)
        self.pipe = pipe
        self.activity = activity
        self.chunks: deque[bytes] = deque()
        self.size = 0
        self.total = 0
        self.sink = sink
        self.stream = stream

    def run(self) -> None:
        try:
            while True:
                chunk = self.pipe.read1(65536)
                if not chunk:
                    break
                self.activity[0] = time.monotonic()
                self.chunks.append(chunk)
                self.size += len(chunk)
                self.total += len(chunk)
                while self.size > _MAX_CAPTURE_BYTES and len(self.chunks) > 1:
                    self.size -= len(self.chunks.popleft())
                if self.sink is not None:
                    try:
                        self.sink.output(self.stream, self.total)
                    except Exception:
                        pass
        except (OSError, ValueError):
            pass

    def text(self) -> str:
        return b"".join(self.chunks).decode("utf-8", errors="replace")


class JobRunner:
    def __init__(
        self,
        wall_timeout: float = DEFAULT_WALL_TIMEOUT,
        idle_timeout: float = DEFAULT_IDLE_TIMEOUT,
        poll_interval: float = 0.05,
    ) -> None:
        self.wall_timeout = wall_timeout
        self.idle_timeout = idle_timeout
        self.poll_interval = poll_interval

    def run(
        self,
        cmd: Sequence[str],
        cwd: str | os.PathLike | None = None,
        env: Mapping[str, str] | None = None,
        wall_timeout: float | None = None,
        idle_timeout: float | None = None,
        stop_check: Callable[[], bool] | None = None,
        progress_sink=None,
        progress_step: str | None = None,
        progress_redaction: str = "normal",
    ) -> RunResult:
        """Run `cmd` contained; optional `progress_sink` gets start/output/pulse/finish metadata.

        `progress_redaction="holdout"` makes the sink drop every output-derived signal. A sink
        reporting `is_lease_lost()` terminates the child (timeout_kind "lease_lost").
        """
        wall = self.wall_timeout if wall_timeout is None else wall_timeout
        idle = self.idle_timeout if idle_timeout is None else idle_timeout
        env = dict(env) if env is not None else build_sandbox_env()
        kwargs: dict = dict(
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=None if cwd is None else str(cwd),
            env=env,
        )
        if IS_WINDOWS:
            kwargs["creationflags"] = CREATE_SUSPENDED | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        else:
            kwargs["start_new_session"] = True
        sink = progress_sink
        redaction = "holdout" if progress_redaction == "holdout" else "metadata_only"

        def sink_call(name: str, *a, **k) -> None:
            if sink is None:
                return
            try:
                getattr(sink, name)(*a, **k)
            except Exception:  # monitoring never fails the run
                pass

        def lease_lost() -> bool:
            if sink is None:
                return False
            try:
                return bool(sink.is_lease_lost())
            except Exception:
                return False

        started = time.monotonic()
        job: JobObject | None = None
        proc: subprocess.Popen | None = None
        readers: list[_Reader] = []
        contained = False
        start_time: float | None = None
        kind: str | None = None
        try:
            job = JobObject() if IS_WINDOWS else None
            proc = subprocess.Popen(list(cmd), **kwargs)
            if job:
                try:
                    job.assign(proc.pid)
                    contained = True
                except JobObjectError:
                    proc.kill()  # fail closed: never run an uncontained provider
                    proc.wait()
                    raise
                _resume_process(proc.pid)
            start_time = process_start_time(proc.pid)
            sink_call("start", proc.pid, start_time, progress_step, redaction=redaction)

            activity = [time.monotonic()]
            readers = [_Reader(proc.stdout, activity, sink, "stdout"), _Reader(proc.stderr, activity, sink, "stderr")]
            for r in readers:
                r.start()

            while proc.poll() is None:
                now = time.monotonic()
                if wall and now - started > wall:
                    kind = "wall"
                elif idle and now - activity[0] > idle:
                    kind = "idle"
                elif stop_check is not None and stop_check():
                    kind = "stop"
                elif lease_lost():
                    kind = "lease_lost"  # fail closed: a worker without the lease must not keep writing
                if kind:
                    self._kill_tree(proc, job)
                    break
                sink_call("pulse")
                time.sleep(self.poll_interval)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._kill_tree(proc, job, last_resort=True)
                proc.wait(timeout=10)
        finally:
            # 1) kill the process tree first (exception paths included) ...
            if proc is not None and proc.poll() is None:
                try:
                    self._kill_tree(proc, job, last_resort=True)
                    proc.wait(timeout=10)
                except Exception:
                    pass
            # 2) ... reap grandchildren still holding pipes/files (Job handle closed on every path) ...
            if job:
                job.close()
            elif proc is not None:
                self._killpg(proc.pid)
            # 3) ... join readers (bounded) BEFORE closing pipes; a pipe whose reader is still
            # blocked in read1() is never closed (buffered-reader lock deadlock).
            for r in readers:
                if r.ident is not None:
                    r.join(timeout=5)
            pipes = [r.pipe for r in readers if not r.is_alive()]
            if proc is not None and not readers:  # no reader ever touched these pipes
                pipes = [p for p in (proc.stdout, proc.stderr) if p is not None]
            for p in pipes:
                try:
                    p.close()
                except Exception:
                    pass
            if proc is not None:
                sink_call("finish", proc.returncode, kind is not None, kind)
        return RunResult(
            returncode=proc.returncode,
            stdout=readers[0].text(),
            stderr=readers[1].text(),
            timed_out=kind is not None,
            timeout_kind=kind,
            pid=proc.pid,
            start_time=start_time,
            duration=time.monotonic() - started,
            job_contained=contained,
        )

    @staticmethod
    def _killpg(pid: int) -> None:
        if IS_WINDOWS:
            return
        try:
            os.killpg(pid, signal.SIGKILL)
        except (OSError, ProcessLookupError):
            pass

    def _kill_tree(self, proc: subprocess.Popen, job: JobObject | None, last_resort: bool = False):
        if job and job.terminate(1) and not last_resort:
            return
        if not IS_WINDOWS:
            self._killpg(proc.pid)
            return
        try:
            proc.kill()
        except OSError:
            pass
        if last_resort:  # taskkill is only a secondary fallback to the Job Object
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
