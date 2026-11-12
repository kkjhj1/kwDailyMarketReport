"""auto_factory Step 3 co-supervisor: dev bot thread + worker thread under one supervisor.lock.

Usage:
    python -m auto_factory --demo                         # offline: FakeTransport + MockAdapters, temp repo
    python -m auto_factory --check  [--env-file F] --repo PATH   # read-only preflight + Telegram getMe
    python -m auto_factory --status [--db PATH] --repo PATH      # read-only (mode=ro) DB summary
    python -m auto_factory --poll-once [--db PATH] --repo PATH   # one bot step + one worker cycle
    python -m auto_factory --run [--db PATH] --repo PATH [--max-cycles N]

Live sequence (fail closed at every step):
    acquire supervisor.lock (BEFORE any DB open)
    -> open bot CoordinatorDB + worker CoordinatorDB (separate WAL connections, busy_timeout=5000)
    -> bot.boot()            (notify chat, stale-outbox quarantine, merge-intent reconcile under the lock)
    -> worker.recover_on_boot()
    -> bot thread    : dispatch_once, poll_once(timeout=5), dispatch_once
       worker thread : run_cycle, then wake_event.wait(idle_sleep)   (woken by /task, Start, /resume)
An unhandled exception in either thread or Ctrl+C sets stop_event; both threads are joined,
the bot releases its poller lease, both DBs are closed and supervisor.lock is released.
`/kill` only writes STOP.flag: the worker parks, the bot stays responsive.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping

from .db import CoordinatorDB, CoordinatorError, is_transient_db_error
from .dev_bot import (
    EXIT_LOCAL,
    EXIT_LOCKED,
    EXIT_OK,
    ConfigError,
    DevBot,
    FakeTransport,
    HttpTransport,
    RateLimiter,
    default_env_file,
    load_dev_env_file,
    run_check,
)
from .worker import PipelineWorker, print_status_readonly, resolve_state_paths
from .worktree import SUPERVISOR_LOCK_NAME, SupervisorLocked, _FileLock


class Supervisor:
    def __init__(
        self,
        *,
        repo: str | os.PathLike,
        db_path: str | os.PathLike,
        artifacts_dir: str | os.PathLike,
        worktrees_dir: str | os.PathLike,
        stop_flag: str | os.PathLike,
        transport: Any,
        admin_chat_id: int,
        adapters: Mapping[str, Any],
        lock_path: str | os.PathLike | None = None,
        state_dir: str | os.PathLike | None = None,
        gate_runner: Any = None,
        holdout_dir: str | os.PathLike | None = None,
        auto_audit_l2: bool = True,
        idle_sleep: float = 30.0,
        poll_timeout: int = 5,
        bot_idle_sleep: float = 0.0,
        busy_backoff: float = 0.5,
        max_cycles: int | None = None,
        clock: Callable[[], float] = time.time,
        limiter: RateLimiter | None = None,
        log: Callable[[str], None] | None = None,
        worker_kwargs: Mapping[str, Any] | None = None,
        bot_kwargs: Mapping[str, Any] | None = None,
    ) -> None:
        self.repo = Path(repo)
        self.db_path = Path(db_path)
        self.artifacts_dir = Path(artifacts_dir)
        self.worktrees_dir = Path(worktrees_dir)
        self.stop_flag = Path(stop_flag)
        self.state_dir = Path(state_dir) if state_dir else self.db_path.parent
        self.transport = transport
        self.admin_chat_id = admin_chat_id
        self.adapters = dict(adapters)
        self.gate_runner = gate_runner
        self.holdout_dir = holdout_dir
        self.auto_audit_l2 = auto_audit_l2
        self.idle_sleep = idle_sleep
        self.poll_timeout = poll_timeout
        self.bot_idle_sleep = bot_idle_sleep
        self.busy_backoff = busy_backoff
        self.max_cycles = max_cycles
        self.clock = clock
        self.limiter = limiter
        self.log = log or (lambda msg: None)
        self.worker_kwargs = dict(worker_kwargs or {})
        self.bot_kwargs = dict(bot_kwargs or {})
        self.lock = _FileLock(Path(lock_path) if lock_path else self.db_path.parent / SUPERVISOR_LOCK_NAME,
                              role="supervisor")
        self.stop_event = threading.Event()
        self.wake_event = threading.Event()
        self.errors: list[tuple[str, BaseException]] = []
        self.bot_db: CoordinatorDB | None = None
        self.worker_db: CoordinatorDB | None = None
        self.bot: DevBot | None = None
        self.worker: PipelineWorker | None = None
        self.threads: list[threading.Thread] = []
        self.boot_report: dict[str, Any] = {}
        self.worker_cycles = 0
        self.bot_iterations = 0
        self._closed = False

    # ---------------------------------------------------------------- boot
    def open(self) -> dict[str, Any]:
        """supervisor.lock -> DBs -> bot.boot() (merge reconcile) -> worker.recover_on_boot()."""
        self.lock.acquire()  # raises SupervisorLocked before any DB is touched
        try:
            self.bot_db = CoordinatorDB(self.db_path, clock=self.clock)
            self.worker_db = CoordinatorDB(self.db_path, clock=self.clock)
            # Q&A focus/draft/outbox rows are shared between threads: one persistent file only.
            if self.bot_db.path == ":memory:" or \
                    os.path.realpath(self.bot_db.path) != os.path.realpath(self.worker_db.path):
                raise CoordinatorError("bot_db and worker_db must share one persistent SQLite file")
            self.bot = DevBot(
                self.bot_db, self.transport, self.admin_chat_id, self.repo, artifacts_dir=self.artifacts_dir,
                worktrees_dir=self.worktrees_dir, stop_flag=self.stop_flag, limiter=self.limiter,
                on_wake=self.wake, require_supervisor_lock=True, state_dir=self.state_dir, log=self.log,
                **self.bot_kwargs,
            )
            self.worker = PipelineWorker(
                self.worker_db, self.repo, self.adapters, artifacts_dir=self.artifacts_dir,
                worktrees_dir=self.worktrees_dir, stop_flag=self.stop_flag, holdout_dir=self.holdout_dir,
                gate_runner=self.gate_runner, sleep_fn=self._sleep, auto_audit_l2=self.auto_audit_l2,
                external_stop=self.stop_event.is_set, **self.worker_kwargs,
            )
            bot_report = self.bot.boot()
            recovered = self.worker.recover_on_boot()
            self.boot_report = {"bot": bot_report, "recovered": recovered}
            return self.boot_report
        except BaseException:
            self.close()
            raise

    # ------------------------------------------------------------- control
    def wake(self) -> None:
        self.wake_event.set()

    def stop(self) -> None:
        self.stop_event.set()
        self.wake_event.set()

    def _sleep(self, seconds: float) -> None:
        self.stop_event.wait(seconds)

    # --------------------------------------------------------------- steps
    def bot_step(self, poll_timeout: int | None = None) -> int:
        self.bot.dispatch_once()
        handled = self.bot.poll_once(timeout=self.poll_timeout if poll_timeout is None else poll_timeout)
        self.bot.dispatch_once()
        return handled

    def worker_step(self) -> dict[str, Any]:
        self.wake_event.clear()
        return self.worker.run_cycle()

    def _transient(self, exc: BaseException, who: str) -> bool:
        if isinstance(exc, sqlite3.OperationalError) and is_transient_db_error(exc):
            self.log(f"{who}: transient DB contention, backing off")
            self.stop_event.wait(self.busy_backoff)
            return True
        return False

    def _bot_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                handled = self.bot_step()
            except Exception as exc:
                if self._transient(exc, "bot"):
                    continue
                raise
            self.bot_iterations += 1
            if not handled and self.bot_idle_sleep:
                self.stop_event.wait(self.bot_idle_sleep)

    def _worker_loop(self) -> None:
        while not self.stop_event.is_set():
            if self.max_cycles is not None and self.worker_cycles >= self.max_cycles:
                self.stop()
                break
            try:
                summary = self.worker_step()
            except Exception as exc:
                if self._transient(exc, "worker"):
                    continue
                raise
            self.worker_cycles += 1
            if self.stop_event.is_set():
                break
            if len(summary["processed"]) < self.worker.max_tasks_per_cycle:
                self.wake_event.wait(self.idle_sleep)  # periodic fallback when nobody wakes us

    def _guard(self, name: str, body: Callable[[], None]) -> None:
        try:
            body()
        except BaseException as exc:  # any thread failure stops the whole supervisor
            self.errors.append((name, exc))
            self.log(f"{name} thread failed: {type(exc).__name__}: {exc}")
            self.stop()

    def start(self) -> None:
        if self.bot is None:
            self.open()
        self.threads = [
            threading.Thread(target=self._guard, args=("bot", self._bot_loop), name="af-bot", daemon=True),
            threading.Thread(target=self._guard, args=("worker", self._worker_loop), name="af-worker", daemon=True),
        ]
        for t in self.threads:
            t.start()

    def join(self) -> None:
        for t in self.threads:
            t.join()

    def shutdown(self) -> None:
        self.stop()
        self.join()
        self.close()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            if self.bot is not None:
                self.bot.close()
        finally:
            for d in (self.bot_db, self.worker_db):
                if d is not None:
                    try:
                        d.close()
                    except Exception:
                        pass
            self.lock.release()

    def _main_wait(self) -> bool:
        return self.stop_event.wait(0.5)  # timed wait keeps Ctrl+C deliverable on Windows

    def run(self) -> int:
        """Blocking run until Ctrl+C, a thread failure, or `max_cycles` worker cycles."""
        self.start()
        try:
            while not self._main_wait():
                pass
        except KeyboardInterrupt:
            self.log("Ctrl+C: shutting down")
        finally:
            self.shutdown()
        return 1 if self.errors else 0

    def run_once(self) -> dict[str, Any]:
        """Synchronous single pass (no threads): bot step, one worker cycle, final dispatch."""
        if self.bot is None:
            self.open()
        handled = self.bot_step(poll_timeout=0)
        summary = self.worker_step()
        self.bot.dispatch_once()
        return {"handled": handled, "worker": summary, "boot": self.boot_report}


def run_supervisor_once(**kwargs: Any) -> dict[str, Any]:
    sup = Supervisor(**kwargs)
    try:
        return sup.run_once()
    finally:
        sup.close()


# ===================================================================== demo
def _wait_for(pred: Callable[[], bool], timeout: float = 60.0, sup: Supervisor | None = None) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sup is not None and sup.errors:
            return False
        if pred():
            return True
        time.sleep(0.02)
    return False


def run_demo(out: Callable[[str], None] = print) -> int:
    """Offline co-supervisor walkthrough: zero network, zero env/.env reads, temp sandbox only."""
    from .adapters import MockAdapter
    from .dev_bot import _InstantGates
    from .worker import build_demo_repo

    admin = 424242424
    checks: list[tuple[str, bool]] = []

    def check(name: str, ok: bool) -> None:
        checks.append((name, bool(ok)))
        out(f"[demo] {'ok  ' if ok else 'FAIL'} {name}")

    def fix(wt, contract, role):
        Path(wt, "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")

    cwd = os.getcwd()
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="af_supervisor_demo_", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        repo = root / "repo"
        build_demo_repo(repo)
        state = root / "state"
        state.mkdir()
        tr = FakeTransport()
        adapters = {
            "claude": MockAdapter("claude", [fix]),
            "codex": MockAdapter("codex"),   # auditor (default approving JSON)
            "gemini": MockAdapter("gemini"),  # reviewer
        }
        sup = Supervisor(
            repo=repo, db_path=state / "coordinator.db", artifacts_dir=state / "artifacts",
            worktrees_dir=state / "worktrees", stop_flag=state / "STOP.flag", transport=tr,
            admin_chat_id=admin, adapters=adapters, gate_runner=_InstantGates(), idle_sleep=0.2,
            poll_timeout=0, bot_idle_sleep=0.01, limiter=RateLimiter(per_chat_interval=0.0),
        )
        uid = [0]

        def push(kind: str, payload: dict) -> None:
            uid[0] += 1
            tr.updates.append({"update_id": uid[0], kind: payload})

        def msg(text: str) -> None:
            push("message", {"message_id": uid[0] + 1, "from": {"id": admin}, "chat": {"id": admin}, "text": text})

        def press(op: str, token: str) -> None:
            push("callback_query", {"id": f"cq{uid[0] + 1}", "from": {"id": admin}, "data": f"af2|{op}|{token}",
                                    "message": {"message_id": 1, "chat": {"id": admin}}})

        def state_of(tid: str) -> str:
            return sup.worker_db.get_task(tid)["state"]

        def last_buttons(source: str = "sent") -> dict[str, str]:
            msgs = tr.sent if source == "sent" else tr.edits
            return tr.buttons(source=source) if msgs else {}

        try:
            out(f"[demo] sandbox={root} (network=0, env=0, product DB=0)")
            sup.start()
            msg("/task L2 calc.py --tests=tests/test_calc.py -- fix add() so tests pass")
            check("/task created via bot thread", _wait_for(lambda: len(sup.worker_db.list_tasks()) == 1, sup=sup))
            tid = sup.worker_db.list_tasks()[0]["id"]
            check("worker woke: L2 auto-audited, parked at AWAITING_START_AUTH",
                  _wait_for(lambda: state_of(tid) == "AWAITING_START_AUTH", sup=sup))
            audit = sup.worker_db.artifacts(tid, "audit")
            check("audit artifact recorded by codex (claude never audits)",
                  len(audit) == 1 and audit[0]["producer"] == "codex" and adapters["claude"].calls == [])
            check("start card delivered", _wait_for(lambda: "s" in last_buttons(), sup=sup))
            press("s", last_buttons()["s"])
            check("Start wakes worker -> AWAITING_MERGE",
                  _wait_for(lambda: state_of(tid) == "AWAITING_MERGE", sup=sup))
            t = sup.worker_db.get_task(tid)
            check("coder != auditor and reviewer independent",
                  t["coder"] == "claude" and [a["provider"] for a in sup.worker_db.attempts(tid, "reviewer")]
                  == ["gemini"])
            check("merge card delivered", _wait_for(lambda: "m" in last_buttons(), sup=sup))
            n_edits = len(tr.edits)
            press("m", last_buttons()["m"])
            check("merge prompt", _wait_for(lambda: len(tr.edits) > n_edits and "M" in last_buttons("edits"), sup=sup))
            press("M", last_buttons("edits")["M"])
            check("2-step SHA-bound merge under supervisor.lock",
                  _wait_for(lambda: state_of(tid) == "MERGED", sup=sup)
                  and sup.bot.wtm.head_sha() == sup.worker_db.get_task(tid)["target_sha"])
            msg("/kill")
            check("/kill writes STOP.flag", _wait_for(sup.stop_flag.exists, sup=sup))
            n_sent = len(tr.sent)
            msg("/status")
            check("bot stays responsive while worker is stopped", _wait_for(lambda: len(tr.sent) > n_sent, sup=sup))
            msg("/resume")
            check("/resume removes STOP.flag", _wait_for(lambda: not sup.stop_flag.exists(), sup=sup))
            check("no thread errors", not sup.errors)
        finally:
            sup.shutdown()
            os.chdir(cwd)
        probe = CoordinatorDB(state / "coordinator.db")
        try:
            check("poller lease released on shutdown", probe.get_kv("poller_lease") is None)
        finally:
            probe.close()
    ok = all(c for _, c in checks)
    out(f"[demo] RESULT {'PASS' if ok else 'FAIL'}: {sum(c for _, c in checks)}/{len(checks)} checks "
        f"in {time.monotonic() - started:.2f}s")
    return 0 if ok else 1


# ====================================================================== CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="auto_factory")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true", help="offline co-supervisor walkthrough")
    mode.add_argument("--check", action="store_true", help="read-only preflight + Telegram getMe")
    mode.add_argument("--status", action="store_true", help="read-only (mode=ro) DB summary")
    mode.add_argument("--poll-once", action="store_true", help="one bot step + one worker cycle")
    mode.add_argument("--run", action="store_true", help="bot + worker threads until Ctrl+C")
    parser.add_argument("--db", help="coordinator DB (default: external state dir)")
    parser.add_argument("--env-file", help="strict env file (default: <repo>/.env if present)")
    parser.add_argument("--repo", default=".", help="repository root")
    parser.add_argument("--worktrees", help="worktrees dir (default: <state>/worktrees)")
    parser.add_argument("--artifacts", help="artifacts dir (default: <state>/artifacts)")
    parser.add_argument("--stop-flag", help="STOP.flag path (default: <state>/STOP.flag)")
    parser.add_argument("--max-cycles", type=int, default=None, help="stop after N worker cycles (--run)")
    parser.add_argument("--idle-sleep", type=float, default=30.0, help="worker fallback poll interval (s)")
    args = parser.parse_args(argv)

    if args.demo:  # decided before any env/.env/DB/network access
        return run_demo()
    if args.status:
        try:
            layout = resolve_state_paths(args.repo, args.db, args.artifacts, args.worktrees, args.stop_flag)
        except ValueError as exc:
            print(f"[supervisor] FAIL-CLOSED: {exc}", file=sys.stderr)
            return EXIT_LOCAL
        return print_status_readonly(layout["db"])
    env_file = default_env_file(args.repo, args.env_file)
    if args.check:
        return run_check(repo=args.repo, db=args.db, artifacts=args.artifacts, worktrees=args.worktrees,
                         stop_flag=args.stop_flag, env_file=env_file)
    try:
        cfg = load_dev_env_file(env_file)
        layout = resolve_state_paths(args.repo, args.db, args.artifacts, args.worktrees, args.stop_flag)
    except (ConfigError, ValueError) as exc:
        print(f"[supervisor] FAIL-CLOSED: {exc}", file=sys.stderr)
        return EXIT_LOCAL
    from .adapters import default_adapters

    os.environ["TELEGRAM_BOT_TOKEN"] = ""  # product token never reaches this process tree
    sup = Supervisor(
        repo=args.repo, db_path=layout["db"], artifacts_dir=layout["artifacts"],
        worktrees_dir=layout["worktrees"], stop_flag=layout["stop_flag"], lock_path=layout["lock"],
        state_dir=layout["state_dir"], transport=HttpTransport(cfg.token), admin_chat_id=cfg.admin_chat_id,
        adapters=default_adapters(), holdout_dir=Path(args.repo) / "holdout_eval", auto_audit_l2=True,
        idle_sleep=args.idle_sleep, max_cycles=args.max_cycles,
        log=lambda m: print(f"[supervisor] {m}", file=sys.stderr),
        bot_kwargs={"unlocked_presets": cfg.unlocked_presets},
    )
    try:
        sup.open()
    except SupervisorLocked as exc:
        print(f"[supervisor] {exc}", file=sys.stderr)
        return EXIT_LOCKED
    if args.poll_once:
        try:
            sup.run_once()
        finally:
            sup.close()
        return EXIT_OK
    return sup.run()


if __name__ == "__main__":
    sys.exit(main())
