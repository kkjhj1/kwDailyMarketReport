"""Shared Telegram HTTP keep-alive transport + single-ack (`AckOnce`) callback guard.

1. Keep-alive routing (drop-in for `opener(req, timeout=...)`):

       if opener is not None:                                  -> exactly the injected opener
       elif urllib.request.urlopen is not _ORIGINAL_URLOPEN:   -> the patched urlopen (tests)
       else:                                                   -> thread-local `requests.Session`

   Sessions are NEVER shared across threads (one per thread, tracked in a registry so `close_all_sessions()`
   can release sockets on shutdown), but all sessions of a registry mount ONE shared, thread-safe
   `HTTPAdapter` (urllib3 `PoolManager`), so poll / lane / ack threads reuse the same warm TLS sockets.
   The session path reproduces the urllib error taxonomy: HTTP >= 400 -> `urllib.error.HTTPError` (body
   readable), timeout -> `TimeoutError`, connection failure -> `URLError`, so every caller's existing
   `except urllib.error.HTTPError` / RuntimeError mapping is unchanged. A stale keep-alive socket (peer reset /
   `RemoteDisconnected` after the server's idle timeout) is retried exactly once; timeouts are never retried.
   When `requests` is unavailable, a stdlib `http.client` keep-alive pool (`StdlibKeepAlivePool`) is used.

2. Callback acknowledgement ownership (`1 callback_query_id -> exactly 1 answerCallbackQuery`):

   `callback_ack_scope(cq_id)` opens (or joins) a scope; inside it `answer_callback_once(cq_id, send)` lets
   only the FIRST acknowledgement through, later ones are suppressed. Outside any scope it is a plain
   pass-through (standalone legacy callers keep acking inline). A scope may carry an `offload` callable
   (manager's bounded ack pool): the ack is handed off without blocking; when the pool is saturated the
   ack runs inline, still BEFORE any DB/render work ([TG-04]).
"""
from __future__ import annotations

import errno
import http.client
import io
import logging
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional

logger = logging.getLogger(__name__)

_ORIGINAL_URLOPEN = urllib.request.urlopen  # identity check: a patched urlopen keeps the urllib path

POOL_CONNECTIONS = 4  # legacy per-session adapter (`new_session(shared=False)`)
POOL_MAXSIZE = 8
SHARED_POOL_CONNECTIONS = 8  # one shared adapter per registry: poll + lane + ack threads share warm sockets
SHARED_POOL_MAXSIZE = 24
STALE_RETRIES = 1  # one transparent retry when a reused keep-alive socket was dropped by the peer


# ---------------------------------------------------------------------------
# Stale keep-alive detection
# ---------------------------------------------------------------------------
_STALE_ERRNOS = {errno.ECONNRESET, errno.EPIPE, errno.ECONNABORTED}


def _iter_error_chain(exc: BaseException) -> Iterator[BaseException]:
    """Walks an exception plus its causes / contexts / exception-valued args (requests -> urllib3 -> socket)."""
    seen: set[int] = set()
    stack: list[Any] = [exc]
    while stack:
        cur = stack.pop()
        if not isinstance(cur, BaseException) or id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        stack.append(cur.__cause__)
        stack.append(cur.__context__)
        stack.append(getattr(cur, "reason", None))
        stack.extend(a for a in getattr(cur, "args", ()) if isinstance(a, BaseException))


def is_stale_connection_error(exc: BaseException) -> bool:
    """True when `exc` looks like a dropped idle keep-alive socket (peer reset / `RemoteDisconnected` /
    urllib3 "Connection aborted."). Any timeout anywhere in the chain -> False (never retried: an ambiguous
    delivery must not be duplicated)."""
    stale = False
    for err in _iter_error_chain(exc):
        if isinstance(err, (TimeoutError, socket.timeout)) or "Timeout" in type(err).__name__:
            return False
        if isinstance(err, (http.client.RemoteDisconnected, ConnectionResetError, BrokenPipeError,
                            ConnectionAbortedError)):
            stale = True
        elif isinstance(err, OSError) and getattr(err, "errno", None) in _STALE_ERRNOS:
            stale = True
        elif type(err).__name__ == "ProtocolError" and "Connection aborted" in str(err):
            stale = True
    return stale


def call_with_stale_retry(fn: Callable[[], Any], retries: int = STALE_RETRIES, label: str = "") -> Any:
    """Runs `fn()`; when it fails on a stale keep-alive socket it is re-run (at most `retries` times)."""
    attempt = 0
    while True:
        try:
            return fn()
        except Exception as exc:
            if attempt >= retries or not is_stale_connection_error(exc):
                raise
            attempt += 1
            logger.debug("stale keep-alive connection%s -> retry %d: %s", f" ({label})" if label else "", attempt, exc)


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


_SHARED_ADAPTER_CLASSES: dict[Any, Any] = {}
_SHARED_ADAPTER_CLASSES_LOCK = threading.Lock()


def _shared_adapter_class(adapter_cls: Any) -> Any:
    """`HTTPAdapter` subclass whose `close()` (called by every `Session.close()`) is a no-op, so closing one
    thread's session never drops the warm sockets of the others; the owner calls `shutdown()`."""
    with _SHARED_ADAPTER_CLASSES_LOCK:
        cls = _SHARED_ADAPTER_CLASSES.get(adapter_cls)
        if cls is None:
            class _SharedHTTPAdapter(adapter_cls):  # type: ignore[misc, valid-type]
                def close(self) -> None:  # noqa: D401 - owned by SharedAdapter
                    return None

                def shutdown(self) -> None:
                    adapter_cls.close(self)

            cls = _SHARED_ADAPTER_CLASSES[adapter_cls] = _SharedHTTPAdapter
        return cls


class SharedAdapter:
    """Lazily-built single `HTTPAdapter` (thread-safe urllib3 `PoolManager`) shared by many sessions."""

    def __init__(self, pool_connections: int = SHARED_POOL_CONNECTIONS, pool_maxsize: int = SHARED_POOL_MAXSIZE):
        self.pool_connections = pool_connections
        self.pool_maxsize = pool_maxsize
        self._lock = threading.Lock()
        self._adapter: Any = None

    def get(self) -> Any:
        """The shared adapter, or None when `requests` is unavailable."""
        with self._lock:
            if self._adapter is None:
                _, adapter_cls = import_requests()
                if adapter_cls is None:
                    return None
                self._adapter = _shared_adapter_class(adapter_cls)(
                    pool_connections=self.pool_connections, pool_maxsize=self.pool_maxsize, max_retries=0
                )
            return self._adapter

    def close(self) -> None:
        """Drops the pooled sockets (idempotent); the next `get()` builds a fresh adapter."""
        with self._lock:
            adapter, self._adapter = self._adapter, None
        if adapter is not None:
            try:
                adapter.shutdown()
            except Exception as exc:  # noqa: BLE001
                logger.debug("shared adapter shutdown skipped: %s", exc)


_SHARED_ADAPTER = SharedAdapter()


def new_session(user_agent: str = "MultiBotPlatform/2.0", adapter: Any = None, shared: bool = True) -> Any:
    """Fresh keep-alive session with NO transparent urllib3 retries (None without `requests`).

    `adapter` given -> mounted as-is; `shared=True` (default) -> the process-wide shared adapter (8/24);
    `shared=False` -> a private bounded adapter (4/8, legacy)."""
    requests_mod, adapter_cls = import_requests()
    if requests_mod is None:
        return None
    if adapter is None and shared:
        adapter = _SHARED_ADAPTER.get()
    if adapter is None:
        adapter = adapter_cls(pool_connections=POOL_CONNECTIONS, pool_maxsize=POOL_MAXSIZE, max_retries=0)
    sess = requests_mod.Session()
    sess.mount("https://", adapter)
    sess.headers.update({"User-Agent": user_agent})
    return sess


class SessionRegistry:
    """Thread-local session per thread; every created session is tracked so `close()` releases them all.

    Without an explicit `factory`, every session of this registry mounts the registry's single shared
    `HTTPAdapter` (`shared_adapter`, default: a private `SharedAdapter`), closed together with the sessions."""

    def __init__(self, factory: Optional[Callable[[], Any]] = None, shared_adapter: Optional[SharedAdapter] = None):
        if factory is None:
            self.shared_adapter: Optional[SharedAdapter] = shared_adapter or SharedAdapter()
            holder = self.shared_adapter
            factory = lambda: new_session(adapter=holder.get())  # noqa: E731
        else:
            self.shared_adapter = shared_adapter
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
        if self.shared_adapter is not None:
            self.shared_adapter.close()
        return len(sessions)

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)


_DEFAULT_REGISTRY = SessionRegistry(shared_adapter=_SHARED_ADAPTER)


def default_registry() -> SessionRegistry:
    return _DEFAULT_REGISTRY


def close_all_sessions() -> int:
    """Manager shutdown hook: closes the shared channel sessions, the process-wide shared adapter and the
    stdlib keep-alive pool (after workers have drained)."""
    closed = _DEFAULT_REGISTRY.close()
    _SHARED_ADAPTER.close()
    _STDLIB_POOL.close()
    return closed


class _BufferedResponse(io.BytesIO):
    """urllib-like response (context manager + `.read()`) over an already-read body."""

    def __init__(self, body: bytes, status: int = 200, headers: Any = None):
        super().__init__(body or b"")
        self.status = status
        self.code = status
        self.headers = headers or {}

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.close()


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
        resp = call_with_stale_retry(
            lambda: sess.request(method, url, data=req.data, headers=headers, timeout=timeout), label=method
        )
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


# ---------------------------------------------------------------------------
# Stdlib keep-alive fallback (no `requests`)
# ---------------------------------------------------------------------------
_SSL_CONTEXT: Optional[ssl.SSLContext] = None
_SSL_CONTEXT_LOCK = threading.Lock()


def _default_ssl_context() -> ssl.SSLContext:
    global _SSL_CONTEXT
    with _SSL_CONTEXT_LOCK:
        if _SSL_CONTEXT is None:
            _SSL_CONTEXT = ssl.create_default_context()
        return _SSL_CONTEXT


class StdlibKeepAlivePool:
    """Thread-safe pool of persistent `http.client` connections keyed by (scheme, host, port).

    A connection is checked out exclusively for one request/response, then returned (unless the server
    asked to close). Idle sockets older than `IDLE_TTL_SEC` are discarded before reuse (Telegram's edge
    drops idle keep-alives after ~60s); a reused socket that turns out stale is retried once on a fresh one."""

    MAX_IDLE_PER_HOST = SHARED_POOL_MAXSIZE
    IDLE_TTL_SEC = 50.0

    def __init__(self, connection_factory: Optional[Callable[[str, str, Optional[int], float], Any]] = None):
        self._factory = connection_factory or self._default_factory
        self._lock = threading.Lock()
        self._idle: dict[tuple[str, str, Optional[int]], list[tuple[Any, float]]] = {}

    @staticmethod
    def _default_factory(scheme: str, host: str, port: Optional[int], timeout: float) -> Any:
        if scheme == "https":
            return http.client.HTTPSConnection(host, port, timeout=timeout, context=_default_ssl_context())
        return http.client.HTTPConnection(host, port, timeout=timeout)

    def _checkout(self, key: tuple[str, str, Optional[int]]) -> Any:
        now = time.monotonic()
        expired: list[Any] = []
        conn = None
        with self._lock:
            bucket = self._idle.get(key) or []
            while bucket:
                cand, ts = bucket.pop()  # most recently used first
                if now - ts <= self.IDLE_TTL_SEC:
                    conn = cand
                    break
                expired.append(cand)
        for old in expired:
            _quiet_close(old)
        return conn

    def _checkin(self, key: tuple[str, str, Optional[int]], conn: Any) -> None:
        with self._lock:
            bucket = self._idle.setdefault(key, [])
            if len(bucket) < self.MAX_IDLE_PER_HOST:
                bucket.append((conn, time.monotonic()))
                return
        _quiet_close(conn)

    def idle_count(self) -> int:
        with self._lock:
            return sum(len(b) for b in self._idle.values())

    def close(self) -> int:
        """Closes every idle socket (idempotent); the pool stays usable afterwards."""
        with self._lock:
            idle, self._idle = self._idle, {}
        n = 0
        for bucket in idle.values():
            for conn, _ts in bucket:
                _quiet_close(conn)
                n += 1
        return n

    def request(self, req: urllib.request.Request, timeout: float) -> _BufferedResponse:
        url = req.full_url
        parts = urllib.parse.urlsplit(url)
        scheme = (parts.scheme or "https").lower()
        key = (scheme, parts.hostname or "", parts.port)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        method = req.get_method()
        headers = {k: v for k, v in req.header_items()}
        lower = {k.lower() for k in headers}
        if "user-agent" not in lower:
            headers["User-Agent"] = "MultiBotPlatform/2.0"
        if req.data is not None and "content-type" not in lower:
            headers["Content-Type"] = "application/x-www-form-urlencoded"  # urllib default

        attempt = 0
        while True:
            conn = self._checkout(key)
            reused = conn is not None
            if conn is None:
                conn = self._factory(scheme, key[1], key[2], timeout)
            conn.timeout = timeout
            sock = getattr(conn, "sock", None)
            if sock is not None:
                try:
                    sock.settimeout(timeout)
                except OSError:
                    pass
            try:
                conn.request(method, path, body=req.data, headers=headers)
                resp = conn.getresponse()
                body = resp.read()
            except Exception as exc:
                _quiet_close(conn)
                if reused and attempt < STALE_RETRIES and is_stale_connection_error(exc):
                    attempt += 1
                    logger.debug("stale stdlib keep-alive socket (%s %s) -> retry: %s", method, key[1], exc)
                    continue
                if isinstance(exc, (TimeoutError, socket.timeout)):
                    raise TimeoutError(f"timed out: {exc}") from exc
                if isinstance(exc, (OSError, http.client.HTTPException)):
                    raise urllib.error.URLError(exc) from exc
                raise
            if getattr(resp, "will_close", True):
                _quiet_close(conn)
            else:
                self._checkin(key, conn)
            status = int(getattr(resp, "status", 200) or 200)
            if status >= 400:
                raise urllib.error.HTTPError(url, status, getattr(resp, "reason", "") or "", resp.headers, io.BytesIO(body))
            return _BufferedResponse(body, status, resp.headers)


def _quiet_close(conn: Any) -> None:
    try:
        conn.close()
    except Exception as exc:  # noqa: BLE001
        logger.debug("keep-alive connection close skipped: %s", exc)


_STDLIB_POOL = StdlibKeepAlivePool()


def stdlib_pool() -> StdlibKeepAlivePool:
    return _STDLIB_POOL


def stdlib_open(req: urllib.request.Request, timeout: float) -> Any:
    """`requests`-free keep-alive request. Falls back to one-shot `urlopen` for non-http(s) URLs or when an
    environment proxy is configured (http.client does not honour proxies; `urlopen` does)."""
    scheme = urllib.parse.urlsplit(req.full_url).scheme.lower()
    if scheme not in ("http", "https") or urllib.request.getproxies().get(scheme):
        return _ORIGINAL_URLOPEN(req, timeout=timeout)
    return _STDLIB_POOL.request(req, timeout)


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
        return stdlib_open(req, timeout)  # no `requests`: stdlib keep-alive pool, not one-shot urlopen
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
