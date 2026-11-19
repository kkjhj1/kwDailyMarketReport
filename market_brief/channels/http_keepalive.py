"""Shared Telegram HTTP keep-alive transport + single-ack (`AckOnce`) callback guard.

1. Keep-alive routing (drop-in for `opener(req, timeout=...)`):

       if opener is not None:                                  -> exactly the injected opener
       elif urllib.request.urlopen is not _ORIGINAL_URLOPEN:   -> the patched urlopen (tests)
       else:                                                   -> thread-local `requests.Session`

   Sessions are NEVER shared across threads (one per thread, tracked in a registry so `close_all_sessions()`
   can release sockets on shutdown). The session path reproduces the urllib error taxonomy: HTTP >= 400 ->
   `urllib.error.HTTPError` (body readable), timeout -> `TimeoutError`, connection failure -> `URLError`,
   so every caller's existing `except urllib.error.HTTPError` / RuntimeError mapping is unchanged.
   When `requests` is unavailable, the stdlib `urlopen` is used.

2. Callback acknowledgement ownership (`1 callback_query_id -> exactly 1 answerCallbackQuery`):

   `callback_ack_scope(cq_id)` opens (or joins) a scope; inside it `answer_callback_once(cq_id, send)` lets
   only the FIRST acknowledgement through, later ones are suppressed. Outside any scope it is a plain
   pass-through (standalone legacy callers keep acking inline). A scope may carry an `offload` callable
   (manager's bounded ack pool): the ack is handed off without blocking; when the pool is saturated the
   ack runs inline, still BEFORE any DB/render work ([TG-04]).
"""
from __future__ import annotations

import io
import logging
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger(__name__)

_ORIGINAL_URLOPEN = urllib.request.urlopen  # identity check: a patched urlopen keeps the urllib path

POOL_CONNECTIONS = 4
POOL_MAXSIZE = 8


# ---------------------------------------------------------------------------
# Keep-alive sessions
# ---------------------------------------------------------------------------
def import_requests():
    """(requests module, HTTPAdapter) or (None, None) when `requests` is not installed."""
    try:
        import requests  # noqa: WPS433
        from requests.adapters import HTTPAdapter
    except ImportError:
        return None, None
    return requests, HTTPAdapter


def new_session(user_agent: str = "MultiBotPlatform/2.0") -> Any:
    """Fresh keep-alive session with a bounded pool and NO transparent retries (None without `requests`)."""
    requests_mod, adapter_cls = import_requests()
    if requests_mod is None:
        return None
    sess = requests_mod.Session()
    sess.mount("https://", adapter_cls(pool_connections=POOL_CONNECTIONS, pool_maxsize=POOL_MAXSIZE, max_retries=0))
    sess.headers.update({"User-Agent": user_agent})
    return sess


class SessionRegistry:
    """Thread-local session per thread; every created session is tracked so `close()` releases them all."""

    def __init__(self, factory: Callable[[], Any] = new_session):
        self._factory = factory
        self._local = threading.local()
        self._lock = threading.Lock()
        self._sessions: list[Any] = []
        self._generation = 0

    def get(self) -> Any:
        gen = self._generation
        sess = getattr(self._local, "session", None)
        if sess is not None and getattr(self._local, "generation", -1) == gen:
            return sess
        with self._lock:
            sess = self._factory()
            if sess is None:
                return None
            self._sessions.append(sess)
            gen = self._generation
        self._local.session = sess
        self._local.generation = gen
        return sess

    def close(self) -> int:
        """Closes every tracked session (idempotent). Threads lazily get a fresh one on next use."""
        with self._lock:
            sessions, self._sessions = self._sessions, []
            self._generation += 1
        for sess in sessions:
            try:
                sess.close()
            except Exception as exc:  # noqa: BLE001
                logger.debug("keep-alive session close skipped: %s", exc)
        return len(sessions)

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)


_DEFAULT_REGISTRY = SessionRegistry()


def default_registry() -> SessionRegistry:
    return _DEFAULT_REGISTRY


def close_all_sessions() -> int:
    """Manager shutdown hook: closes the shared channel sessions (after workers have drained)."""
    return _DEFAULT_REGISTRY.close()


class _SessionResponse(io.BytesIO):
    """urllib-like response (context manager + `.read()`) built from a `requests.Response`."""

    def __init__(self, resp: Any):
        super().__init__(resp.content or b"")
        self.status = getattr(resp, "status_code", 200)
        self.code = self.status
        self.headers = getattr(resp, "headers", None) or {}

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


def session_request(sess: Any, req: urllib.request.Request, timeout: float) -> _SessionResponse:
    """Executes a `urllib.request.Request` on a `requests.Session`, normalizing errors to urllib's taxonomy."""
    requests_mod, _ = import_requests()
    url = req.full_url
    method = req.get_method()
    headers = {k: v for k, v in req.header_items()}
    try:
        resp = sess.request(method, url, data=req.data, headers=headers, timeout=timeout)
    except Exception as exc:
        if requests_mod is not None and isinstance(exc, requests_mod.exceptions.Timeout):
            raise TimeoutError(f"timed out: {exc}") from exc
        if requests_mod is not None and isinstance(exc, requests_mod.exceptions.ConnectionError):
            raise urllib.error.URLError(exc) from exc
        raise
    status = int(getattr(resp, "status_code", 200) or 200)
    if status >= 400:
        raise urllib.error.HTTPError(
            url, status, getattr(resp, "reason", "") or "", getattr(resp, "headers", None) or {},
            io.BytesIO(resp.content or b""),
        )
    return _SessionResponse(resp)


def open_request(
    req: urllib.request.Request,
    timeout: float,
    opener: Optional[Callable[..., Any]] = None,
    registry: Optional[SessionRegistry] = None,
) -> Any:
    """Drop-in for `opener(req, timeout=timeout)` applying the exact routing rule (see module doc)."""
    if opener is not None:
        return opener(req, timeout=timeout)
    if urllib.request.urlopen is not _ORIGINAL_URLOPEN:
        return urllib.request.urlopen(req, timeout=timeout)
    # `is not None`: an empty registry is falsy (`__len__`), it must still be the one used.
    sess = (registry if registry is not None else _DEFAULT_REGISTRY).get()
    if sess is None:
        return _ORIGINAL_URLOPEN(req, timeout=timeout)
    return session_request(sess, req, timeout)


# ---------------------------------------------------------------------------
# Single-ack guard
# ---------------------------------------------------------------------------
class AckOnce:
    """First-claim-wins acknowledgement state for one callback_query id."""

    def __init__(self, cq_id: str, offload: Optional[Callable[[Callable[[], Any]], Any]] = None,
                 already_answered: bool = False):
        self.cq_id = cq_id
        self._offload = offload
        self._lock = threading.Lock()
        self.claimed = bool(already_answered)
        self.suppressed = 0
        self.future: Any = None
        self.refs = 0

    def claim(self) -> bool:
        with self._lock:
            if self.claimed:
                self.suppressed += 1
                return False
            self.claimed = True
            return True

    def dispatch(self, send: Callable[[], Any]) -> Any:
        if self._offload is not None:
            fut = self._offload(send)
            if fut is not None:
                self.future = fut
                return True
        return send()  # no pool / saturated -> inline, still before DB/render work

    def wait(self, timeout: float = 10.0) -> None:
        fut = self.future
        if fut is None:
            return
        try:
            fut.result(timeout=timeout)
        except Exception as exc:  # noqa: BLE001 - ack failures never break the flow
            logger.debug("offloaded answerCallbackQuery for %s ended with %s", self.cq_id, exc)


_ACK_LOCK = threading.Lock()
_ACTIVE_ACKS: dict[str, AckOnce] = {}


@contextmanager
def callback_ack_scope(
    cq_id: Any,
    offload: Optional[Callable[[Callable[[], Any]], Any]] = None,
    already_answered: bool = False,
) -> Iterator[Optional[AckOnce]]:
    """Opens (outermost) or joins (nested) the single-ack scope for `cq_id`. Nested scopes share state;
    only the outermost scope's `offload` / `already_answered` apply."""
    key = str(cq_id or "")
    if not key:
        yield None
        return
    with _ACK_LOCK:
        entry = _ACTIVE_ACKS.get(key)
        if entry is None:
            entry = AckOnce(key, offload=offload, already_answered=already_answered)
            _ACTIVE_ACKS[key] = entry
        entry.refs += 1
    try:
        yield entry
    finally:
        with _ACK_LOCK:
            entry.refs -= 1
            if entry.refs <= 0 and _ACTIVE_ACKS.get(key) is entry:
                _ACTIVE_ACKS.pop(key, None)


def active_ack(cq_id: Any) -> Optional[AckOnce]:
    with _ACK_LOCK:
        return _ACTIVE_ACKS.get(str(cq_id or ""))


def answer_callback_once(cq_id: Any, send: Callable[[], Any], suppressed_result: Any = True) -> Any:
    """Sends the acknowledgement via `send()` unless this callback id was already answered in the active
    scope (then returns `suppressed_result` without any HTTP request). No scope -> plain `send()`."""
    entry = active_ack(cq_id)
    if entry is None:
        return send()
    if not entry.claim():
        logger.debug("duplicate answerCallbackQuery suppressed for %s", entry.cq_id)
        return suppressed_result
    return entry.dispatch(send)


class AckOnceClient:
    """Proxy around a Telegram client whose `answer_callback_query` / `answer_callback` go through
    `answer_callback_once`; every other attribute is delegated untouched."""

    _ACK_METHODS = ("answer_callback_query", "answer_callback")

    def __init__(self, inner: Any):
        object.__setattr__(self, "_inner", inner)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._inner, name)
        if name in self._ACK_METHODS and callable(attr):
            def _guarded(callback_query_id: Any = None, *args: Any, **kwargs: Any) -> Any:
                if callback_query_id is None:
                    callback_query_id = kwargs.pop("callback_query_id", "")
                return answer_callback_once(
                    callback_query_id, lambda: attr(callback_query_id, *args, **kwargs)
                )
            return _guarded
        return attr

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._inner, name, value)
