"""auto_factory Step 2: admin-only Telegram dev bot, outbox dispatcher and merge gateway.

Usage:
    python -m auto_factory.dev_bot --demo                       # offline, zero network/env/DB access
    python -m auto_factory.dev_bot --check  [--env-file F] --repo PATH   # read-only preflight + getMe
    python -m auto_factory.dev_bot --status [--db PATH] --repo PATH      # read-only (mode=ro) DB view
    python -m auto_factory.dev_bot --poll-once [--db PATH] --repo PATH
    python -m auto_factory.dev_bot --run [--db PATH] --repo PATH

Credentials may come from a strict allowlisted env file (`--env-file`, default `<repo>/.env`
if present): only AUTO_FACTORY_DEV_BOT_TOKEN / AUTO_FACTORY_ADMIN_CHAT_ID (and the optional
AUTO_FACTORY_UNLOCKED_PRESETS allowlist for `/do`, default: none) are extracted, the
process environment overrides the file, and a dev token equal to ANY other `*TOKEN*` value
(file or process env) is refused. Live modes hold `supervisor.lock` (exit 4 if held).

Security model (fail closed everywhere):
- Credentials come ONLY from AUTO_FACTORY_DEV_BOT_TOKEN / AUTO_FACTORY_ADMIN_CHAT_ID. There is
  no fallback to TELEGRAM_BOT_TOKEN or any product token. Live modes exit non-zero on a
  missing/malformed value before touching the network or the DB. `--demo` is decided first.
- Every update must satisfy `from.id == chat.id == admin_chat_id`; anything else is
  silently ignored (no reply, no data leak).
- Callback data is `af2|<op>|<22-char token>` (28 ASCII bytes). Tokens live server side
  (`tg_actions`) and bind chat/user, op, task state + state_version + target/diff SHA and an
  expiry. `answerCallbackQuery` is called FIRST; if it fails nothing else happens.
- Mutations are two-step (prompt -> confirm). Merge only runs through `MergeCoordinator`
  (merge intent journal -> live re-verification -> `git merge --ff-only` -> `finalize_merge`).
- Notifications use the transactional `tg_outbox`; only 429 is retried, timeout / reset / 5xx
  become UNKNOWN and are never resent; stale rows from a previous boot are not redelivered.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import secrets
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .db import (
    ActionRejected,
    CoordinatorDB,
    CoordinatorError,
    TERMINAL_STATES,
    read_db_summary_readonly,
)
from .guardrails import GuardrailChecker, is_forbidden
from .presets import (
    PRESET_ALIAS_RE,
    PRESET_POLICY_VERSION,
    PRESETS,
    Preset,
    decode_preset_token,
    encode_preset_token,
    parse_unlocked_presets,
    validate_unlocked,
)
from .worktree import (
    MergeConflictError,
    MergeError,
    SupervisorLocked,
    UnsafePathError,
    WorktreeInfo,
    WorktreeManager,
    _FileLock,
    assert_supervisor_lock_held,
    safe_read_bytes,
    safe_unlink,
    safe_write_atomic,
)

TOKEN_ENV = "AUTO_FACTORY_DEV_BOT_TOKEN"
CHAT_ENV = "AUTO_FACTORY_ADMIN_CHAT_ID"
UNLOCKED_PRESETS_ENV = "AUTO_FACTORY_UNLOCKED_PRESETS"
BOT_TOKEN_RE = re.compile(r"^\d{8,10}:[A-Za-z0-9_-]{35}$")
CALLBACK_PREFIX = "af2"
CALLBACK_RE = re.compile(r"^af2\|([scdrRmMxXpqez])\|([A-Za-z0-9_-]{22})$")
TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
MAX_CALLBACK_BYTES = 64
MAX_VISIBLE = 1800
MAX_HTML = 3500
MAX_REQUEST_BYTES = 2048
MAX_PATHS = 20
CIRCUIT = "global"  # mirrors worker.CIRCUIT (kept local so the bot does not import the worker)
QA_CIRCUIT = "qa_provider"  # mirrors worker.QA_CIRCUIT
FOCUS_TTL = 900.0  # a /bots pick keeps the chat focused on that preset for 15 minutes (refreshed on use)
DRAFT_TTL = 600.0  # ambiguous message -> 질문/수정 choice buttons stay valid for 10 minutes
QA_REQUEST_TTL = 1800.0
STOP_FLAG_NAME = "STOP.flag"
START_APPROVAL_TTL = 24 * 3600.0
REVIEW_APPROVAL_TTL = 24 * 3600.0
MERGE_APPROVAL_TTL = 600.0
# Product code / data the dev bot may never target (in addition to guardrails.FORBIDDEN_PATTERNS).
PROTECTED_PATHS = (
    "market_brief/", "travel_brief/", "admission_brief/", "portal_hub/", "silver_care/",
    "kids_compass/", "yolo_brief/", "data/", "reports/", "holdout_eval/", ".git/", ".github/",
    "admin_server.py", "telegram_bot_runner.py", "travel_bot_runner.py", "admission_bot_runner.py",
    "portal_bot_runner.py", "run_cron.sh", "deploy_to_oracle.ps1", "AGENTS.md",
)
PRODUCT_DB_SUFFIXES = (".sqlite3", ".sqlite", ".db", ".db-wal", ".db-shm")
_PATH_CHARS_RE = re.compile(r"^[A-Za-z0-9._/-]+$")
_GLOB_CHARS = set("*?[]")
_SHELL_CHARS = set("&|;<>`$'\"!%~(){}^ \t")


# ====================================================================== config
class ConfigError(ValueError):
    pass


@dataclass(frozen=True)
class BotConfig:
    token: str
    admin_chat_id: int
    unlocked_presets: frozenset[str] = frozenset()

    def __repr__(self) -> str:  # never leak the token into logs/tracebacks
        return (f"BotConfig(token='***', admin_chat_id={self.admin_chat_id}, "
                f"unlocked_presets={sorted(self.unlocked_presets)})")


def _load_unlocked(raw: Any) -> frozenset[str]:
    try:
        return parse_unlocked_presets(raw)
    except ValueError as exc:  # messages carry aliases only, never raw secret values
        raise ConfigError(f"{UNLOCKED_PRESETS_ENV}: {exc}") from None


def load_config(env: dict[str, str] | None = None) -> BotConfig:
    """Read ONLY the dedicated dev-bot variables. No product-token fallback, ever."""
    env = os.environ if env is None else env
    unlocked = _load_unlocked(env.get(UNLOCKED_PRESETS_ENV))
    token = (env.get(TOKEN_ENV) or "").strip()
    if not BOT_TOKEN_RE.match(token):
        raise ConfigError(f"{TOKEN_ENV} is missing or malformed")
    raw_chat = (env.get(CHAT_ENV) or "").strip()
    try:
        chat_id = int(raw_chat)
    except ValueError:
        raise ConfigError(f"{CHAT_ENV} is missing or not an integer") from None
    if chat_id == 0:
        raise ConfigError(f"{CHAT_ENV} must be non-zero")
    return BotConfig(token, chat_id, unlocked)


MAX_ENV_BYTES = 1024 * 1024
_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _env_value(raw: str, lineno: int) -> str:
    v = raw.strip()
    if "${" in v or "$(" in v or "`" in v:
        raise ConfigError(f"env line {lineno}: interpolation/command substitution is not allowed")
    if v[:1] in ("'", '"'):
        q = v[0]
        if len(v) < 2 or v[-1] != q or q in v[1:-1]:
            raise ConfigError(f"env line {lineno}: unmatched quote")
        return v[1:-1]
    if '"' in v or "'" in v:
        raise ConfigError(f"env line {lineno}: stray quote")
    return v


def parse_env_text(text: str) -> dict[str, str]:
    """Strict `KEY=VALUE` / blank / `#` comment lines only. Errors never include values."""
    out: dict[str, str] = {}
    for n, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if re.match(r"^export\s", s):
            raise ConfigError(f"env line {n}: 'export' is not supported")
        key, sep, raw = s.partition("=")
        if not sep or not _ENV_KEY_RE.match(key):
            raise ConfigError(f"env line {n}: malformed (expected KEY=VALUE)")
        if key in out:
            raise ConfigError(f"env line {n}: duplicate key {key}")
        out[key] = _env_value(raw, n)
    return out


def _norm_env(v: Any) -> str:
    s = str(v or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    return s


def load_dev_env_file(env_path: str | os.PathLike | None, process_env: Mapping[str, str] | None = None) -> BotConfig:
    """Strict allowlisted loader: extracts ONLY the dev-bot keys; never mutates os.environ.

    Keys: AUTO_FACTORY_DEV_BOT_TOKEN, AUTO_FACTORY_ADMIN_CHAT_ID, AUTO_FACTORY_UNLOCKED_PRESETS
    (the latter optional; unset or "" unlocks nothing).

    - file: no-follow regular file, <= 1 MiB, UTF-8 (BOM ok), no NUL, strict grammar
    - a key present in `process_env` (even as "") overrides the file (fail closed, no fallback)
    - the effective dev token AND the file's dev token must differ from every other `*TOKEN*`
      value in both the file and `process_env` (same unquote/strip normalization)
    """
    penv = dict(os.environ if process_env is None else process_env)
    file_vals: dict[str, str] = {}
    if env_path is not None:
        try:
            raw = safe_read_bytes(env_path, MAX_ENV_BYTES)
        except FileNotFoundError:
            raise ConfigError("env file not found") from None
        except UnsafePathError as exc:
            raise ConfigError(f"env file rejected: {exc}") from None
        except OSError as exc:
            raise ConfigError(f"env file unreadable: {type(exc).__name__}") from None
        if b"\x00" in raw:
            raise ConfigError("env file contains NUL bytes")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ConfigError("env file is not valid UTF-8") from None
        file_vals = parse_env_text(text)
    merged: dict[str, str] = {}
    for key in (TOKEN_ENV, CHAT_ENV, UNLOCKED_PRESETS_ENV):
        if key in penv:
            merged[key] = penv[key]
        elif key in file_vals:
            merged[key] = file_vals[key]
    cfg = load_config(merged)
    others: set[str] = set()
    for src in (file_vals, penv):
        for k, v in src.items():
            if "TOKEN" in k.upper() and k.upper() != TOKEN_ENV:
                nv = _norm_env(v)
                if nv:
                    others.add(nv)
    mine = {_norm_env(cfg.token)}
    if _norm_env(file_vals.get(TOKEN_ENV)):
        mine.add(_norm_env(file_vals[TOKEN_ENV]))
    if mine & others:
        raise ConfigError(f"{TOKEN_ENV} reuses the value of another *TOKEN* variable (refused)")
    return cfg


# ================================================================ /task parse
class TaskParseError(ValueError):
    pass


@dataclass(frozen=True)
class TaskSpec:
    risk_level: str
    allowed_paths: tuple[str, ...]
    tests: tuple[str, ...]
    request: str


_TASK_RE = re.compile(
    r"^/task(?:@[A-Za-z0-9_]{1,64})?[ \t]+(L[0-3])[ \t]+(\S+)(?:[ \t]+--tests=(\S+))?[ \t]+--[ \t\r\n]+(.+)$",
    re.S,
)


def sanitize_repo_path(raw: str, repo_root: str | os.PathLike, protected: Iterable[str] = PROTECTED_PATHS) -> str:
    """Return a canonical relative POSIX path inside `repo_root`, or raise TaskParseError."""
    if not raw:
        raise TaskParseError("empty path")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in raw):
        raise TaskParseError("control character in path")
    if "\\" in raw:
        raise TaskParseError("backslash in path")
    if raw.startswith("/"):
        raise TaskParseError("absolute/UNC path")
    if re.match(r"^[A-Za-z]:", raw):
        raise TaskParseError("drive-letter path")
    if any(ch in _GLOB_CHARS for ch in raw):
        raise TaskParseError("wildcard/glob in path")
    if any(ch in _SHELL_CHARS for ch in raw):
        raise TaskParseError("shell metacharacter in path")
    if not _PATH_CHARS_RE.match(raw):
        raise TaskParseError("path must be ASCII [A-Za-z0-9._/-]")
    is_dir = raw.endswith("/")
    segments = raw[:-1].split("/") if is_dir else raw.split("/")
    for seg in segments:
        if seg in ("", ".", ".."):
            raise TaskParseError("empty, '.' or '..' path segment")
        if seg.startswith("-"):
            raise TaskParseError("path segment may not start with '-'")
    rel = "/".join(segments)
    lowered = rel.lower()
    if lowered.endswith(PRODUCT_DB_SUFFIXES):
        raise TaskParseError("database files are off limits")
    for probe in (rel, rel + "/"):
        if is_forbidden(probe):
            raise TaskParseError(f"forbidden path: {rel}")
    for prot in protected:
        p = prot.rstrip("/").lower()
        if lowered == p or lowered.startswith(p + "/") or (not prot.endswith("/") and lowered == p):
            raise TaskParseError(f"protected product path: {rel}")
    root = Path(repo_root).resolve()
    resolved = (root / rel).resolve()
    try:
        canon = resolved.relative_to(root).as_posix()
    except ValueError:
        raise TaskParseError("path escapes the repository") from None
    if canon != rel:  # symlink / case games: the canonical target must be the same path
        if is_forbidden(canon) or is_forbidden(canon + "/"):
            raise TaskParseError("path resolves to a forbidden location")
        raise TaskParseError("path is not canonical (symlink or alias)")
    return rel + ("/" if is_dir else "")


def _split_paths(blob: str, repo_root, protected) -> tuple[str, ...]:
    parts = blob.split(",")
    if len(parts) > MAX_PATHS:
        raise TaskParseError(f"at most {MAX_PATHS} paths")
    return tuple(sorted({sanitize_repo_path(p, repo_root, protected) for p in parts}))


def parse_task_command(
    text: str, repo_root: str | os.PathLike, protected: Iterable[str] = PROTECTED_PATHS
) -> TaskSpec:
    """`/task <L0|L1|L2|L3> <path>[,<path>...] [--tests=p1,p2] -- <request>`.

    The request is stored as data only; it is never passed to a shell or subprocess.
    """
    if "\x00" in text:
        raise TaskParseError("NUL byte")
    m = _TASK_RE.match(text)
    if not m:
        raise TaskParseError("usage: /task <L0|L1|L2|L3> <path>[,<path>...] [--tests=a,b] -- <request>")
    risk, paths_blob, tests_blob, request = m.groups()
    request = request.strip()
    if not request:
        raise TaskParseError("empty request")
    if len(request.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise TaskParseError(f"request exceeds {MAX_REQUEST_BYTES} UTF-8 bytes")
    if any((ord(ch) < 32 and ch not in "\n\t") or ord(ch) == 127 for ch in request):
        raise TaskParseError("control character in request")
    paths = _split_paths(paths_blob, repo_root, protected)
    tests = _split_paths(tests_blob, repo_root, protected) if tests_blob else ()
    return TaskSpec(risk, paths, tests, request)


# ================================================================== /do parse
_DO_RE = re.compile(r"^/do(?:@[A-Za-z0-9_]{1,64})?\s+(\S+)(?:\s+(.*))?$", re.S)
_SEPARATOR_RE = re.compile(r"(?:^|\s)--(?:\s|$)")
_SMUGGLE_CHARS = set("/\\,*?[]")
_FILE_LIKE_RE = re.compile(r"^[A-Za-z0-9_.-]+\.[A-Za-z0-9]{1,5}$")


def _smuggle_token(tok: str) -> bool:
    """A token that looks like an option flag, a path, a glob or a file name."""
    return (
        tok.startswith("-") or "--tests" in tok or any(ch in _SMUGGLE_CHARS for ch in tok)
        or bool(_FILE_LIKE_RE.match(tok))
    )


def _check_no_smuggling(request: str) -> None:
    """`/do` has no path/flag syntax; reject `/task`-style smuggling (`--tests=`, `x.py --`, `*.py`)."""
    tokens = request.split()
    first = tokens[0]
    if first.startswith("-") or "--tests" in first or any(ch in "*?[]\\" for ch in first) \
            or _FILE_LIKE_RE.match(first) or (first.isascii() and any(ch in "/," for ch in first)):
        raise TaskParseError("/do takes no paths or flags: write the request as plain prose")
    sep = _SEPARATOR_RE.search(request)
    if sep:
        head = request[: sep.start()].split()
        if any(_smuggle_token(t) for t in head):
            raise TaskParseError("/do takes no paths or flags before '--' (paths come from the preset)")


def _verify_preset_paths(preset: Preset, repo_root: str | os.PathLike) -> None:
    """Every preset path must resolve canonically inside `repo_root` and never be forbidden.

    Mirrors `sanitize_repo_path` symlink/case safety without requiring the file to exist.
    """
    root = Path(repo_root).resolve()
    for rel in preset.allowed_paths + preset.tests:
        if is_forbidden(rel) or is_forbidden(rel + "/"):
            raise TaskParseError(f"preset {preset.alias}: forbidden path {rel}")
        resolved = (root / rel).resolve()
        try:
            canon = resolved.relative_to(root).as_posix()
        except ValueError:
            raise TaskParseError(f"preset {preset.alias}: path escapes the repository") from None
        if canon != rel:
            raise TaskParseError(f"preset {preset.alias}: path is not canonical (symlink or alias)")


def parse_do_command(
    text: str, repo_root: str | os.PathLike, unlocked_presets: Iterable[str]
) -> tuple[Preset, str]:
    """`/do <alias> <request>`: scope (paths, tests, L2) comes ONLY from the code registry."""
    if "\x00" in text:
        raise TaskParseError("NUL byte")
    m = _DO_RE.match(text)
    if not m:
        raise TaskParseError("usage: /do <preset> <request>  (see /bots)")
    alias, request = m.group(1), (m.group(2) or "").strip()
    if not PRESET_ALIAS_RE.match(alias) or alias not in PRESETS:
        raise TaskParseError("unknown preset (see /bots)")
    if alias not in frozenset(unlocked_presets):
        raise TaskParseError(f"preset {alias} is locked (add it to {UNLOCKED_PRESETS_ENV})")
    if not request:
        raise TaskParseError("empty request")
    if any((ord(ch) < 32 and ch not in "\n\t") or ord(ch) == 127 for ch in request):
        raise TaskParseError("control character in request")
    if len(request.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise TaskParseError(f"request exceeds {MAX_REQUEST_BYTES} UTF-8 bytes")
    _check_no_smuggling(request)
    preset = PRESETS[alias]
    _verify_preset_paths(preset, repo_root)
    return preset, request


# ======================================================= conversational text
_FLAG_INJECTION_RE = re.compile(r"(?:^|\s)--(?=\s|$|[A-Za-z])")
_ASK_CUES_KO = ("뭐", "무엇", "무슨", "왜", "어떻게", "어디", "언제", "누가", "어느", "몇", "알려", "설명", "궁금",
                "인가", "인지", "나요", "까요", "니까", "는지", "을까", "ㄴ가", "있어?", "있나", "되나", "맞아", "맞나")
_EDIT_CUES_KO = ("수정", "고쳐", "고치", "바꿔", "바꾸", "변경", "추가", "삭제", "제거", "만들어", "구현", "개선",
                 "리팩", "적용", "넣어", "빼줘", "빼 줘", "다듬", "업데이트", "교체", "정리해")
_ASK_CUES_EN = re.compile(r"\b(what|why|how|where|when|which|who|explain|does|is there)\b", re.I)
_EDIT_CUES_EN = re.compile(r"\b(fix|change|add|remove|delete|implement|update|refactor|rename|replace)\b", re.I)


def validate_conversational_text(text: str) -> str:
    """Normalize a plain chat message; reject control chars, oversize text and CLI-flag smuggling.

    Natural punctuation (`?`, `,`, `/`, `()`) is fine: this text is data, never a command line.
    """
    if not isinstance(text, str):
        raise ValueError("text required")
    if "\x00" in text:
        raise ValueError("NUL byte")
    s = text.replace("\r\n", "\n").replace("\r", "\n")
    if any((ord(ch) < 32 and ch not in "\n\t") or ord(ch) == 127 for ch in s):
        raise ValueError("control character in message")
    s = s.strip()
    if not s:
        raise ValueError("empty message")
    if len(s.encode("utf-8")) > MAX_REQUEST_BYTES:
        raise ValueError(f"message exceeds {MAX_REQUEST_BYTES} UTF-8 bytes")
    if "--tests=" in s.lower():
        raise ValueError("--tests= is not allowed (scope comes from the preset)")
    if _FLAG_INJECTION_RE.search(s):
        raise ValueError("'--' flags are not allowed in chat messages")
    return s


def classify_intent(text: str) -> str:
    """Deterministic routing: `ask` (read-only Q&A), `edit` (L2 preset task) or `ambiguous`."""
    s = (text or "").strip()
    ask = s.endswith(("?", "？")) or any(c in s for c in _ASK_CUES_KO) or bool(_ASK_CUES_EN.search(s))
    edit = any(c in s for c in _EDIT_CUES_KO) or bool(_EDIT_CUES_EN.search(s))
    if edit and not ask:
        return "edit"
    if ask and not edit:
        return "ask"
    return "ambiguous"


# ================================================================ formatting
class MessageTooLong(ValueError):
    pass


def validate_callback_data(data: str) -> str:
    try:
        raw = data.encode("ascii")
    except UnicodeEncodeError:
        raise ValueError("callback_data must be ASCII") from None
    if len(raw) > MAX_CALLBACK_BYTES:
        raise ValueError(f"callback_data is {len(raw)} bytes (> {MAX_CALLBACK_BYTES})")
    if not CALLBACK_RE.match(data):
        raise ValueError("callback_data does not match af2|<op>|<token>")
    return data


class SafeHtmlFormatter:
    """[TG-01]/[TG-02]: escape every dynamic value; allow only b/i/code/pre/a; enforce limits."""

    ALLOWED_TAGS = frozenset({"b", "i", "code", "pre", "a"})
    _TAG_RE = re.compile(r"<(/?)([A-Za-z0-9]+)((?:\s[^>]*)?)>")
    _HREF_RE = re.compile(r'^\s+href="https://[^"\s<>]+"$')

    def __init__(self, max_visible: int = MAX_VISIBLE, max_html: int = MAX_HTML):
        self.max_visible = max_visible
        self.max_html = max_html

    @staticmethod
    def esc(value: Any, limit: int | None = None) -> str:
        """Truncate the *plain text* first, then escape (never cuts an entity or a tag)."""
        s = "" if value is None else str(value)
        if limit is not None and len(s) > limit:
            s = s[: max(0, limit - 1)] + "…"
        return html.escape(s, quote=True)

    @staticmethod
    def normalize_plain(value: Any) -> str:
        """Unify newlines and drop C0/DEL control characters (keeps `\\n` and `\\t`)."""
        s = "" if value is None else str(value)
        s = s.replace("\r\n", "\n").replace("\r", "\n")
        return "".join(ch for ch in s if ch in "\n\t" or not (ord(ch) < 32 or ord(ch) == 127))

    @classmethod
    def esc_html_budget(cls, value: Any, max_visible: int, max_html: int) -> str:
        """Longest escaped prefix (+ "…" when cut) within BOTH the visible and the HTML budget.

        The plain text is cut before escaping, so an entity like `&lt;` is never split.
        """
        s = cls.normalize_plain(value)

        def candidate(k: int) -> str:
            return s[:k] + ("…" if k < len(s) else "")

        def fits(k: int) -> bool:
            cand = candidate(k)
            return len(cand) <= max_visible and len(html.escape(cand, quote=True)) <= max_html

        if fits(len(s)):
            return html.escape(s, quote=True)
        if not fits(0):
            return ""
        lo, hi = 0, len(s) - 1
        while lo < hi:  # escaped length is monotonic in k
            mid = (lo + hi + 1) // 2
            if fits(mid):
                lo = mid
            else:
                hi = mid - 1
        return html.escape(candidate(lo), quote=True)

    def b(self, v: Any, limit: int = 200) -> str:
        return f"<b>{self.esc(v, limit)}</b>"

    def i(self, v: Any, limit: int = 300) -> str:
        return f"<i>{self.esc(v, limit)}</i>"

    def code(self, v: Any, limit: int = 200) -> str:
        return f"<code>{self.esc(v, limit)}</code>"

    def pre(self, v: Any, limit: int = 1000) -> str:
        return f"<pre>{self.esc(v, limit)}</pre>"

    def link(self, href: str, label: Any) -> str:
        if not re.match(r"^https://[^\s\"<>]+$", href or ""):
            raise ValueError("only plain https links are allowed")
        return f'<a href="{html.escape(href, quote=True)}">{self.esc(label, 100)}</a>'

    @staticmethod
    def visible_len(text: str) -> int:
        return len(html.unescape(re.sub(r"<[^>]+>", "", text)))

    def finalize(self, text: str) -> str:
        for closing, name, attrs in self._TAG_RE.findall(text):
            if name.lower() not in self.ALLOWED_TAGS:
                raise ValueError(f"tag <{name}> not allowed")
            if attrs and not (name.lower() == "a" and not closing and self._HREF_RE.match(attrs)):
                raise ValueError(f"attributes not allowed on <{name}>")
        if self.visible_len(text) > self.max_visible:
            raise MessageTooLong(f"visible length {self.visible_len(text)} > {self.max_visible}")
        if len(text) > self.max_html:
            raise MessageTooLong(f"html length {len(text)} > {self.max_html}")
        return text

    @staticmethod
    def button(label: str, op: str, token: str) -> dict[str, str]:
        return {"text": label, "callback_data": validate_callback_data(f"{CALLBACK_PREFIX}|{op}|{token}")}

    @staticmethod
    def markup(rows: list[list[dict[str, str]]]) -> dict[str, Any] | None:
        rows = [r for r in rows if r]
        for row in rows:
            for btn in row:
                validate_callback_data(btn["callback_data"])
        return {"inline_keyboard": rows} if rows else None


# =============================================================== rate limit
class RateLimiter:
    """[TG-04]: <= 25 msg/s globally and >= 1 s between messages to the same chat.

    Shared by outbox dispatch, interactive replies AND callback answers.
    """

    def __init__(
        self,
        global_per_sec: int = 25,
        per_chat_interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.global_per_sec = global_per_sec
        self.per_chat_interval = per_chat_interval
        self.clock = clock
        self.sleep = sleep
        self._global: deque[float] = deque()
        self._chat: dict[int, float] = {}

    def wait(self, chat_id: int | None = None) -> float:
        waited = 0.0
        while True:
            now = self.clock()
            while self._global and self._global[0] <= now - 1.0:
                self._global.popleft()
            delay = 0.0
            if len(self._global) >= self.global_per_sec:
                delay = self._global[0] + 1.0 - now
            if chat_id is not None and chat_id in self._chat:
                delay = max(delay, self._chat[chat_id] + self.per_chat_interval - now)
            if delay <= 0:
                self._global.append(now)
                if chat_id is not None:
                    self._chat[chat_id] = now
                return waited
            self.sleep(delay)
            waited += delay


# ================================================================ transport
class TgError(Exception):
    pass


class TgRetryAfter(TgError):
    """HTTP 429: definitely not delivered; safe to retry after `retry_after` seconds."""

    def __init__(self, retry_after: float):
        super().__init__(f"429 retry after {retry_after}")
        self.retry_after = float(retry_after)


class TgUnknown(TgError):
    """Timeout / connection reset / 5xx: the message may or may not have been delivered."""


class TgRejected(TgError):
    """4xx (not 429): definitively rejected, never delivered."""


class HttpTransport:
    """Minimal Bot API client (stdlib only). Error texts never include the token/URL."""

    def __init__(self, token: str, timeout: float = 15.0):
        if not BOT_TOKEN_RE.match(token or ""):
            raise ConfigError("invalid dev bot token")
        self._base = f"https://api.telegram.org/bot{token}/"
        self.timeout = timeout

    def _call(self, method: str, payload: dict[str, Any], timeout: float | None = None) -> Any:
        req = urllib.request.Request(
            self._base + method,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                body = json.loads(exc.read().decode("utf-8"))
            except Exception:
                body = {}
            if exc.code == 429:
                retry = (body.get("parameters") or {}).get("retry_after") or exc.headers.get("Retry-After") or 1
                raise TgRetryAfter(float(retry)) from None
            if exc.code >= 500:
                raise TgUnknown(f"HTTP {exc.code}") from None
            raise TgRejected(f"HTTP {exc.code}: {str(body.get('description', ''))[:200]}") from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as exc:
            raise TgUnknown(type(exc).__name__) from None
        except ValueError:
            raise TgUnknown("unparseable response") from None
        if not body.get("ok"):
            raise TgRejected(str(body.get("description", "not ok"))[:200])
        return body.get("result")

    def send_message(self, chat_id: int, text: str, reply_markup: dict | None = None,
                     reply_to_message_id: int | None = None) -> int:
        payload: dict[str, Any] = {
            "chat_id": chat_id, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if reply_to_message_id is not None:
            payload["reply_parameters"] = {"message_id": int(reply_to_message_id),
                                           "allow_sending_without_reply": True}
        return int((self._call("sendMessage", payload) or {}).get("message_id", 0))

    def edit_message_text(self, chat_id: int, message_id: int, text: str, reply_markup: dict | None = None) -> None:
        payload: dict[str, Any] = {
            "chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        self._call("editMessageText", payload)

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> bool:
        return self._call("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text[:180]}) is True

    def get_updates(self, offset: int, timeout: int = 25) -> list[dict[str, Any]]:
        payload = {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        return list(self._call("getUpdates", payload, timeout=timeout + 10) or [])

    def get_me(self) -> dict[str, Any]:
        me = self._call("getMe", {})
        if not isinstance(me, dict):
            raise TgRejected("getMe returned no bot object")
        return me


class FakeTransport:
    """Offline transport for `--demo` and tests. Scripts are lists of exceptions / return values."""

    def __init__(self) -> None:
        self.send_script: list[Any] = []
        self.answer_script: list[Any] = []
        self.edit_script: list[Any] = []
        self.sent: list[dict[str, Any]] = []
        self.send_attempts = 0
        self.edits: list[dict[str, Any]] = []
        self.answers: list[str] = []
        self.updates: list[dict[str, Any]] = []
        self._mid = 1000

    @staticmethod
    def _step(script: list[Any]) -> Any:
        step = script.pop(0) if script else None
        if isinstance(step, BaseException):
            raise step
        return step

    def send_message(self, chat_id, text, reply_markup=None, reply_to_message_id: int | None = None) -> int:
        self.send_attempts += 1
        self._step(self.send_script)
        self._mid += 1
        self.sent.append({"chat_id": chat_id, "text": text, "reply_markup": reply_markup, "message_id": self._mid,
                          "reply_to_message_id": reply_to_message_id})
        return self._mid

    def edit_message_text(self, chat_id, message_id, text, reply_markup=None) -> None:
        self._step(self.edit_script)
        self.edits.append({"chat_id": chat_id, "message_id": message_id, "text": text, "reply_markup": reply_markup})

    def answer_callback_query(self, callback_query_id, text="") -> bool:
        step = self._step(self.answer_script)
        if step is False:
            return False
        self.answers.append(callback_query_id)
        return True

    def get_updates(self, offset, timeout=0) -> list[dict[str, Any]]:
        return [u for u in list(self.updates) if u["update_id"] >= offset]

    def get_me(self) -> dict[str, Any]:
        self.get_me_calls = getattr(self, "get_me_calls", 0) + 1
        return {"id": 1, "is_bot": True, "username": "af_fake_bot"}

    def buttons(self, index: int = -1, source: str = "sent") -> dict[str, str]:
        """Map op -> token for the inline keyboard of a sent/edited message (test helper)."""
        msg = (self.sent if source == "sent" else self.edits)[index]
        out: dict[str, str] = {}
        for row in (msg.get("reply_markup") or {}).get("inline_keyboard", []):
            for btn in row:
                _, op, token = btn["callback_data"].split("|")
                out[op] = token
        return out


# ============================================================ merge gateway
@dataclass
class MergeOutcome:
    ok: bool
    code: str
    detail: str = ""
    intent_id: str | None = None
    head: str | None = None
    cleanup_ok: bool | None = None


class MergeCoordinator:
    """The only path to MERGED: intent journal -> live verification -> ff-only -> finalize -> cleanup."""

    RETRYABLE = frozenset({"MAIN_DIRTY", "MERGE_LOCKED"})

    def __init__(self, db: CoordinatorDB, wtm: WorktreeManager, guard: GuardrailChecker,
                 artifacts_dir: str | os.PathLike, require_supervisor_lock: bool = False):
        self.db = db
        self.wtm = wtm
        self.guard = guard
        self.artifacts_dir = Path(artifacts_dir)
        self.require_supervisor_lock = require_supervisor_lock

    def _block_task(self, task_id: str, reason: str) -> None:
        task = self.db.get_task(task_id)
        if task["state"] not in TERMINAL_STATES and task["state"] != "BLOCKED":
            self.db.transition(task_id, task["state_version"], "BLOCKED", "merge-coordinator", reason[:500])

    def merge(self, task_id: str, actor: str) -> MergeOutcome:
        if self.require_supervisor_lock:  # lock order: supervisor.lock -> merge.lock
            assert_supervisor_lock_held()
        try:
            intent = self.db.begin_merge_intent(task_id, actor)
        except (CoordinatorError, KeyError) as exc:
            return MergeOutcome(False, "INTENT_REJECTED", str(exc))
        task = self.db.get_task(task_id)
        try:
            head = self.wtm.merge_candidate(task, self.guard)
        except MergeError as exc:
            retryable = exc.code in self.RETRYABLE
            self.db.abort_merge_intent(
                intent, str(exc), "ABORTED" if retryable or isinstance(exc, MergeConflictError) else "BLOCKED"
            )
            if not retryable:
                self._block_task(task_id, f"MERGE_{exc.code}: {exc}")
            return MergeOutcome(False, exc.code, str(exc), intent)
        except Exception as exc:  # unexpected: decide from Git's actual state, never guess
            if self.wtm.head_sha() != task["target_sha"]:
                self.db.abort_merge_intent(intent, f"unexpected: {exc}", "BLOCKED")
                self._block_task(task_id, f"MERGE_ERROR: {type(exc).__name__}")
                return MergeOutcome(False, "MERGE_ERROR", str(exc), intent)
            head = task["target_sha"]
        self.db.mark_merge_intent_git_merged(intent)
        self.db.finalize_merge(intent, head)
        cleanup_ok = True
        try:
            info = WorktreeInfo(task_id, task["worktree_path"], self.wtm.branch_for(task_id), task["base_sha"])
            self.wtm.cleanup(info, self.artifacts_dir, delete_branch=True)
        except Exception as exc:  # the merge is committed; record, never roll back
            self.db.mark_orphan_cleanup(intent, f"cleanup failed: {exc}")
            cleanup_ok = False
        return MergeOutcome(True, "MERGED", "", intent, head, cleanup_ok)


# ================================================================= renderer
_STATE_BUTTONS = {
    "AWAITING_START_AUTH": (("s", "▶️ Start"), ("c", "❌ Cancel")),
    "SINGLE_PROVIDER_REVIEW_REQUIRED": (("d", "\U0001f50d Diff"), ("r", "✅ Manual review"), ("x", "\U0001f5d1 Discard")),
    "AWAITING_MERGE": (("d", "\U0001f50d Diff"), ("m", "\U0001f500 Merge"), ("x", "\U0001f5d1 Discard")),
    "BLOCKED": (("d", "\U0001f50d Diff"), ("x", "\U0001f5d1 Discard")),
    "GUARDRAIL_BLOCKED": (("d", "\U0001f50d Diff"), ("x", "\U0001f5d1 Discard")),
}


class TaskRenderer:
    def __init__(self, db: CoordinatorDB, fmt: SafeHtmlFormatter, admin_id: int, wtm: WorktreeManager,
                 artifacts_dir: str | os.PathLike):
        self.db = db
        self.fmt = fmt
        self.admin_id = admin_id
        self.wtm = wtm
        self.artifacts_dir = Path(artifacts_dir)

    def task_card(self, task: dict[str, Any], reason: str = "", with_buttons: bool = True) -> tuple[str, dict | None]:
        f = self.fmt
        lines = [
            f"<b>auto_factory</b> · {f.code(task['id'], 64)}",
            f"State: {f.b(task['state'], 40)} (v{int(task['state_version'])}) · {f.esc(task['risk_level'], 4)}",
            f"Request: {f.esc(task['request'], 300)}",
            f"Paths: {f.code(', '.join(task['allowed_paths']), 300)}",
        ]
        if reason:
            lines.append(f"Reason: {f.i(reason, 300)}")
        if task.get("target_sha"):
            lines.append(f"target {f.code(task['target_sha'][:12], 12)} · diff {f.code((task.get('diff_sha256') or '')[:12], 12)}")
        markup = None
        if with_buttons:
            row = []
            for op, label in _STATE_BUTTONS.get(task["state"], ()):
                if op == "d" and not self._has_diff(task):
                    continue
                token = self.db.issue_action_token(
                    task["id"], op, self.admin_id, self.admin_id, expected_version=task["state_version"]
                )
                row.append(f.button(label, op, token))
            markup = f.markup([row])
        return f.finalize("\n".join(lines)), markup

    def _has_diff(self, task: dict[str, Any]) -> bool:
        wt = task.get("worktree_path")
        return bool(task.get("base_sha")) and (
            bool(wt and Path(wt).is_dir()) or (self.artifacts_dir / f"{task['id']}.patch").is_file()
        )

    def diff_view(self, task: dict[str, Any]) -> str:
        f = self.fmt
        wt, base = task.get("worktree_path"), task.get("base_sha")
        stat, patch = "", ""
        if wt and Path(wt).is_dir() and base:
            stat = self.wtm.git("diff", "--stat", base, "HEAD", cwd=wt, check=False)
            patch = self.wtm.diff_bytes(wt, base).decode("utf-8", "replace")
        else:
            p = self.artifacts_dir / f"{task['id']}.patch"
            if p.is_file():
                patch = p.read_text(encoding="utf-8", errors="replace")
        head = (
            f"<b>Diff</b> {f.code(task['id'], 64)} · target {f.code((task.get('target_sha') or '-')[:12], 12)}"
        )
        return f.finalize("\n".join([head, f.pre(stat or "(no stat)", 400), f.pre(patch or "(empty)", 1100)]))


# =========================================================== outbox dispatch
class OutboxDispatcher:
    def __init__(self, db: CoordinatorDB, transport: Any, limiter: RateLimiter, renderer: TaskRenderer,
                 admin_chat_id: int, boot_id: str | None = None, owner: str | None = None):
        self.db = db
        self.transport = transport
        self.limiter = limiter
        self.renderer = renderer
        self.admin_chat_id = admin_chat_id
        self.boot_id = boot_id or uuid.uuid4().hex
        self.owner = owner or f"dispatch:{uuid.uuid4().hex[:12]}"

    def boot(self) -> dict[str, int]:
        tg = self.db.expire_stale_outbox_on_boot(self.boot_id)
        qa = self.db.expire_stale_qa_outbox_on_boot(self.boot_id)
        return {k: tg.get(k, 0) + qa.get(k, 0) for k in set(tg) | set(qa)}

    def _marks(self, qa: bool) -> dict[str, Callable[..., bool]]:
        d = self.db
        if qa:
            return {"sent": d.mark_qa_outbox_sent, "retry": d.mark_qa_outbox_retry, "unknown": d.mark_qa_outbox_unknown,
                    "failed": d.mark_qa_outbox_failed, "expired": d.mark_qa_outbox_expired}
        return {"sent": d.mark_outbox_sent, "retry": d.mark_outbox_retry, "unknown": d.mark_outbox_unknown,
                "failed": d.mark_outbox_failed, "expired": d.mark_outbox_expired}

    def dispatch_once(self, limit: int = 10) -> dict[str, int]:
        stats = {"sent": 0, "retry": 0, "unknown": 0, "failed": 0, "expired": 0}
        for row in self.db.claim_outbox(self.owner, self.boot_id, limit):
            self._dispatch_tg_row(row, stats)
        for row in self.db.claim_qa_outbox(self.owner, self.boot_id, limit):
            self._dispatch_qa_row(row, stats)
        return stats

    def _dispatch_tg_row(self, row: dict[str, Any], stats: dict[str, int]) -> None:
        marks = self._marks(qa=False)
        rid = row["id"]
        if row["chat_id"] != self.admin_chat_id:
            marks["failed"](rid, self.owner, "chat is not the admin chat")
            stats["failed"] += 1
            return
        try:
            payload = json.loads(row["payload_json"])
            task = self.db.get_task(row["task_id"])
            if task["state_version"] != payload.get("state_version"):
                marks["expired"](rid, self.owner, "superseded by a newer state")
                stats["expired"] += 1
                return
            text, markup = self.renderer.task_card(task, payload.get("reason", ""))
        except Exception as exc:  # render failure: nothing was sent
            marks["failed"](rid, self.owner, f"render: {type(exc).__name__}: {exc}")
            stats["failed"] += 1
            return
        self._deliver(row, text, markup, marks, stats)

    def _dispatch_qa_row(self, row: dict[str, Any], stats: dict[str, int]) -> None:
        """Q&A answers/notices: rendered from the payload alone (never touches `tasks`)."""
        marks = self._marks(qa=True)
        rid = row["id"]
        if row["chat_id"] != self.admin_chat_id:
            marks["failed"](rid, self.owner, "chat is not the admin chat")
            stats["failed"] += 1
            return
        try:
            text = self._format_qa_message(row["payload_json"])
        except Exception as exc:  # render failure: nothing was sent
            marks["failed"](rid, self.owner, f"render: {type(exc).__name__}")
            stats["failed"] += 1
            return
        self._deliver(row, text, None, marks, stats)

    def _deliver(self, row: dict[str, Any], text: str, markup: dict | None,
                 marks: dict[str, Callable[..., bool]], stats: dict[str, int]) -> None:
        rid = row["id"]
        self.limiter.wait(row["chat_id"])
        try:
            self.transport.send_message(row["chat_id"], text, markup)
        except TgRetryAfter as exc:
            marks["retry"](rid, self.owner, exc.retry_after, str(exc))
            stats["retry"] += 1
        except TgRejected as exc:
            marks["failed"](rid, self.owner, str(exc))
            stats["failed"] += 1
        except Exception as exc:  # timeout / reset / 5xx / anything else: may have been sent
            marks["unknown"](rid, self.owner, f"{type(exc).__name__}: {exc}")
            stats["unknown"] += 1
        else:
            marks["sent"](rid, self.owner)
            stats["sent"] += 1

    def _format_qa_message(self, payload_json: str) -> str:
        from .guardrails import redact_secrets

        f = self.renderer.fmt
        payload = json.loads(payload_json)
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        qa_id = str(payload.get("qa_id") or "?")
        alias = payload.get("preset_alias")
        scope = PRESETS[alias].label if isinstance(alias, str) and alias in PRESETS else "저장소 전체"
        question = redact_secrets(payload.get("question") or "")
        if payload.get("kind") == "fail":
            lines = [f"<b>⚠️ 질문 처리 실패</b> · {f.code(qa_id, 24)} · {f.esc(scope, 40)}",
                     f"Q: {f.esc(f.normalize_plain(question), 200)}",
                     f.esc(f.normalize_plain(redact_secrets(payload.get("text") or "")), 500)]
            return f.finalize("\n".join(lines))
        refs = [redact_secrets(r) for r in (payload.get("refs") or []) if isinstance(r, str)][:5]
        header = "\n".join([
            f"<b>\U0001f4ac 답변</b> · {f.code(qa_id, 24)} · {f.esc(scope, 40)}",
            f"Q: {f.esc(f.normalize_plain(question), 200)}",
            "",
        ])
        footer = ("\n\n참고: " + ", ".join(f.code(r, 80) for r in refs)) if refs else ""
        frame = header + footer
        budget_visible = f.max_visible - f.visible_len(frame)
        budget_html = f.max_html - len(frame)
        body = f.esc_html_budget(redact_secrets(payload.get("answer") or ""), max(0, budget_visible),
                                 max(0, budget_html))
        return f.finalize(header + body + footer)


# ===================================================================== bot
HELP_TEXT = (
    "<b>auto_factory dev bot</b>\n"
    "/bots — 봇 프리셋 선택 (✅ unlocked / 🔒 locked)\n"
    "봇 선택 후 자연어로 질문·수정 요청 (질문=읽기 전용 답변, 수정=L2 작업)\n"
    "/ask [preset] &lt;질문&gt; — 읽기 전용 질문\n"
    "/clear — 선택한 봇 해제\n"
    "/do &lt;preset&gt; &lt;request&gt; — L2 task scoped by the preset\n"
    "/task &lt;L0|L1|L2|L3&gt; &lt;path&gt;[,&lt;path&gt;] [--tests=a,b] -- &lt;request&gt;\n"
    "/status [task_id] — overview or task card\n"
    "/kill — write STOP.flag · /resume [clear-circuit]\n"
    "Merges are 2-step and bound to task + target_sha + diff_sha256."
)


class DevBot:
    def __init__(
        self,
        db: CoordinatorDB,
        transport: Any,
        admin_chat_id: int,
        repo_root: str | os.PathLike,
        artifacts_dir: str | os.PathLike,
        worktrees_dir: str | os.PathLike | None = None,
        stop_flag: str | os.PathLike | None = None,
        limiter: RateLimiter | None = None,
        fmt: SafeHtmlFormatter | None = None,
        protected: Iterable[str] = PROTECTED_PATHS,
        boot_id: str | None = None,
        merge_approval_ttl: float = MERGE_APPROVAL_TTL,
        log: Callable[[str], None] | None = None,
        on_wake: Callable[[], None] | None = None,
        require_supervisor_lock: bool = False,
        state_dir: str | os.PathLike | None = None,
        unlocked_presets: Iterable[str] = (),
    ):
        if not isinstance(admin_chat_id, int) or isinstance(admin_chat_id, bool) or admin_chat_id == 0:
            raise ConfigError("admin_chat_id must be a non-zero int")
        try:
            self.unlocked_presets: frozenset[str] = validate_unlocked(unlocked_presets)
        except ValueError as exc:
            raise ConfigError(str(exc)) from None
        self.db = db
        self.transport = transport
        self.admin = admin_chat_id
        self.wtm = WorktreeManager(repo_root, worktrees_dir)
        self.guard = GuardrailChecker(self.wtm)
        self.artifacts_dir = Path(artifacts_dir)
        self.state_dir = Path(state_dir).resolve() if state_dir else None
        self.on_wake = on_wake
        self.require_supervisor_lock = require_supervisor_lock
        self.stop_flag = self._safe_stop_flag(stop_flag)
        self.limiter = limiter or RateLimiter()
        self.fmt = fmt or SafeHtmlFormatter()
        self.protected = tuple(protected)
        self.merge_approval_ttl = merge_approval_ttl
        self.log = log or (lambda msg: None)
        self.owner = f"bot:{uuid.uuid4().hex[:12]}"
        self.renderer = TaskRenderer(db, self.fmt, admin_chat_id, self.wtm, self.artifacts_dir)
        self.dispatcher = OutboxDispatcher(db, transport, self.limiter, self.renderer, admin_chat_id, boot_id)
        self.coordinator = MergeCoordinator(db, self.wtm, self.guard, self.artifacts_dir,
                                            require_supervisor_lock=require_supervisor_lock)

    def _safe_stop_flag(self, stop_flag: str | os.PathLike | None) -> Path:
        """STOP.flag must be named STOP.flag and live in the repo root or the state dir."""
        p = Path(stop_flag) if stop_flag else self.wtm.repo_root / STOP_FLAG_NAME
        p = p.parent.resolve() / p.name
        allowed = {self.wtm.repo_root, self.artifacts_dir.resolve().parent}
        if self.state_dir is not None:
            allowed.add(self.state_dir)
        if p.name != STOP_FLAG_NAME or p.parent not in allowed:
            raise ConfigError("stop flag must be <repo_root or state dir>/STOP.flag")
        return p

    def _fire_wake(self) -> None:
        if self.on_wake is None:
            return
        try:
            self.on_wake()
        except Exception as exc:  # a wake hook must never break the bot
            self.log(f"on_wake failed: {type(exc).__name__}")

    def close(self) -> None:
        """Release the single-poller lease so a successor can poll immediately."""
        try:
            self.db.release_poller_lease(self.owner)
        except Exception as exc:
            self.log(f"poller lease release failed: {type(exc).__name__}")

    # ---------------------------------------------------------------- boot
    def boot(self) -> dict[str, Any]:
        """Register the notify chat, quarantine stale outbox rows, reconcile merge intents."""
        self.db.set_kv("notify_chat_id", self.admin)
        outbox = self.dispatcher.boot()
        merges = self.db.reconcile_merge_intents(
            self.wtm, self.artifacts_dir, require_supervisor_lock=self.require_supervisor_lock
        )
        return {"outbox": outbox, "merges": merges}

    def dispatch_once(self, limit: int = 10) -> dict[str, int]:
        return self.dispatcher.dispatch_once(limit)

    # ------------------------------------------------------------- polling
    def poll_once(self, timeout: int = 25) -> int:
        if not self.db.acquire_poller_lease(self.owner, ttl=timeout + 60):
            return 0
        offset = self.db.get_update_offset()
        try:
            updates = self.transport.get_updates(offset + 1 if offset else 0, timeout)
        except TgError as exc:
            self.log(f"getUpdates failed: {exc}")
            return 0
        handled = 0
        for upd in sorted(updates, key=lambda u: int(u.get("update_id", 0))):
            uid = int(upd.get("update_id", 0))
            cur = self.db.get_update_offset()
            if uid <= cur:
                continue  # duplicate delivery: harmless no-op
            if not self.db.advance_update_offset(cur, uid):
                continue  # another poller claimed it first (CAS)
            self.handle_update(upd)
            handled += 1
        return handled

    # ------------------------------------------------------------ dispatch
    def _authorized(self, from_id: Any, chat_id: Any) -> bool:
        return (
            isinstance(from_id, int) and not isinstance(from_id, bool)
            and isinstance(chat_id, int) and not isinstance(chat_id, bool)
            and from_id == self.admin and chat_id == self.admin
        )

    def handle_update(self, update: dict[str, Any]) -> str:
        try:
            if "callback_query" in update:
                return self._on_callback(update["callback_query"] or {})
            if "message" in update:
                return self._on_message(update["message"] or {})
            return "ignored:kind"
        except Exception as exc:  # never crash the poll loop; never leak details to non-admins
            self.log(f"handler error: {type(exc).__name__}: {exc}")
            return f"error:{type(exc).__name__}"

    def _send(self, chat_id: int, text: str, markup: dict | None = None, reply_to: int | None = None) -> bool:
        """Interactive reply: one attempt, never retried (UNKNOWN semantics)."""
        self.limiter.wait(chat_id)
        try:
            if isinstance(reply_to, int) and not isinstance(reply_to, bool):
                self.transport.send_message(chat_id, self.fmt.finalize(text), markup, reply_to_message_id=reply_to)
            else:
                self.transport.send_message(chat_id, self.fmt.finalize(text), markup)
            return True
        except Exception as exc:
            self.log(f"reply failed: {type(exc).__name__}")
            return False

    def _edit(self, chat_id: int, message_id: Any, text: str, markup: dict | None = None) -> bool:
        if not isinstance(message_id, int):
            return self._send(chat_id, text, markup)
        self.limiter.wait(chat_id)
        try:
            self.transport.edit_message_text(chat_id, message_id, self.fmt.finalize(text), markup)
            return True
        except Exception as exc:
            self.log(f"edit failed: {type(exc).__name__}")
            return False

    # ------------------------------------------------------------ messages
    def _on_message(self, msg: dict[str, Any]) -> str:
        from_id = (msg.get("from") or {}).get("id")
        chat_id = (msg.get("chat") or {}).get("id")
        if not self._authorized(from_id, chat_id):
            return "ignored:unauthorized"
        text = msg.get("text") or ""
        if not text.startswith("/"):
            if text.lstrip().startswith("/"):
                return "ignored:leading_space_command"  # " /do ..." is never re-parsed as a command
            if not text.strip():
                return "ignored:text"
            return self._on_plain_text(chat_id, text, msg.get("message_id"))
        parts = re.split(r"\s+", text, maxsplit=1)
        head, rest = parts[0], (parts[1] if len(parts) > 1 else "")
        cmd = head.split("@", 1)[0].lower()
        arg = rest.strip()
        if cmd in ("/help", "/start"):
            self._send(chat_id, HELP_TEXT)
            return "cmd:help"
        if cmd == "/task":
            return self._cmd_task(chat_id, text)
        if cmd == "/do":
            return self._cmd_do(chat_id, text)
        if cmd == "/bots":
            return self._cmd_bots(chat_id)
        if cmd == "/ask":
            return self._cmd_ask(chat_id, arg)
        if cmd == "/clear":
            self.db.clear_chat_focus(chat_id)
            self._send(chat_id, "\U0001f9f9 선택한 봇을 해제했습니다. /bots 로 다시 선택하세요.")
            return "cmd:clear"
        if cmd == "/status":
            return self._cmd_status(chat_id, arg)
        if cmd == "/kill":
            try:  # no-follow atomic write; the bot itself keeps running
                safe_write_atomic(self.stop_flag, f"killed via dev bot at {time.time():.0f}\n")
            except (OSError, UnsafePathError) as exc:
                self.log(f"STOP.flag write refused: {type(exc).__name__}")
                self._send(chat_id, "⚠️ STOP.flag write refused (unsafe path). Stop the worker manually.")
                return "cmd:kill:failed"
            self._send(chat_id, "\U0001f6d1 STOP.flag written. Worker halts at its next checkpoint.")
            return "cmd:kill"
        if cmd == "/resume":
            if arg not in ("", "clear-circuit"):
                self._send(chat_id, "usage: /resume [clear-circuit]")
                return "cmd:resume:bad-arg"
            try:
                safe_unlink(self.stop_flag, missing_ok=True)
            except (OSError, UnsafePathError) as exc:  # fail closed: the worker stays stopped
                self.log(f"STOP.flag removal refused: {type(exc).__name__}")
                self._send(chat_id, "⚠️ STOP.flag removal refused (unsafe path); worker stays stopped.")
                return "cmd:resume:failed"
            if arg == "clear-circuit":
                self.db.circuit_reset(CIRCUIT)
                self.db.circuit_reset(QA_CIRCUIT)
            self._send(chat_id, "▶️ STOP.flag removed" + (" and circuit reset." if arg else "."))
            self._fire_wake()
            return "cmd:resume"
        self._send(chat_id, "Unknown command. /help")
        return "cmd:unknown"

    def _cmd_task(self, chat_id: int, text: str) -> str:
        try:
            spec = parse_task_command(text, self.wtm.repo_root, self.protected)
        except TaskParseError as exc:
            self._send(chat_id, f"⚠️ /task rejected: {self.fmt.esc(exc, 200)}")
            return "cmd:task:rejected"
        tid, created = self.db.create_task(
            spec.request, spec.risk_level, list(spec.allowed_paths), {"pytest": list(spec.tests)}
        )
        if not created:
            self._reply_duplicate(chat_id, tid)
            return f"cmd:task:duplicate:{tid}"
        self._fire_wake()  # after commit
        self._send(
            chat_id,
            f"\U0001f195 Task created {self.fmt.code(tid, 64)} ({self.fmt.esc(spec.risk_level, 4)}). "
            "Start buttons arrive at AWAITING_START_AUTH.",
        )
        return f"cmd:task:created:{tid}"

    def _reply_duplicate(self, chat_id: int, tid: str) -> None:
        """Same request again: explain terminal/blocked outcomes, or re-show the live card."""
        f = self.fmt
        existing = self.db.get_task(tid)
        state = existing["state"]
        if state in ("BLOCKED", "GUARDRAIL_BLOCKED"):
            self._send(chat_id, f"♻️ 이미 차단({f.esc(state, 40)})된 동일 요청입니다 (자동 재시도 안 함). "
                                f"요청 내용을 수정해 다시 보내주세요.\nTask {f.code(tid, 64)}")
            return
        if state == "MERGED":
            self._send(chat_id, f"♻️ 이미 main에 병합 완료(MERGED)된 동일 요청입니다.\nTask {f.code(tid, 64)}")
            return
        text, markup = self.renderer.task_card(existing, reason=f"duplicate of {state}")
        self._send(chat_id, text, markup)

    def _cmd_do(self, chat_id: int, text: str) -> str:
        f = self.fmt
        try:
            preset, request = parse_do_command(text, self.wtm.repo_root, self.unlocked_presets)
        except TaskParseError as exc:
            self._send(chat_id, f"⚠️ /do rejected: {f.esc(exc, 200)}")
            return "cmd:do:rejected"
        tid, created = self.db.create_task(
            request, preset.min_risk, list(preset.allowed_paths),
            {"pytest": list(preset.tests), "preset": preset.alias, "preset_policy": PRESET_POLICY_VERSION},
        )
        if not created:
            self._reply_duplicate(chat_id, tid)
            return f"cmd:do:duplicate:{tid}"
        self._fire_wake()  # after commit
        self._send(
            chat_id,
            f"\U0001f195 {f.esc(preset.label, 40)} 작업 생성 {f.code(tid, 64)} ({f.esc(preset.min_risk, 4)} · "
            f"파일 {len(preset.allowed_paths)}개 · 테스트 {len(preset.tests)}개).\n"
            "감사(audit) 후 AWAITING_START_AUTH 에서 ▶️ Start 버튼이 도착합니다.",
        )
        return f"cmd:do:created:{tid}"

    def _cmd_bots(self, chat_id: int) -> str:
        f = self.fmt
        unlocked = [p for p in PRESETS.values() if p.alias in self.unlocked_presets]
        locked = [p for p in PRESETS.values() if p.alias not in self.unlocked_presets]
        lines = [f"<b>\U0001f916 봇 프리셋</b> · 사용 가능 {len(unlocked)}/{len(PRESETS)}"]
        if unlocked:
            lines.append("<b>✅ 사용 가능</b>")
            lines += [f"✅ {f.esc(p.label, 40)} {f.code(p.alias, 22)}" for p in unlocked]
        if locked:
            lines.append("<b>\U0001f512 잠김</b>")
            lines += [f"\U0001f512 {f.code(p.alias, 22)} {f.esc(p.label, 40)}" for p in locked]
        if unlocked:
            lines.append("버튼을 누르거나 /do &lt;preset&gt; &lt;요청&gt; 으로 작업을 만드세요.")
        else:
            lines.append(f"잠금 해제: {f.code(UNLOCKED_PRESETS_ENV + '=kids,yolo', 80)} (.env)")
        buttons = [f.button(p.label, "p", encode_preset_token(p.alias)) for p in unlocked]
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        self._send(chat_id, "\n".join(lines), f.markup(rows))
        return f"cmd:bots:{len(unlocked)}"

    # --------------------------------------------------- conversational mode
    def _live_focus(self, chat_id: int) -> tuple[str, dict[str, Any]] | None:
        """(raw, focus) of a live, unlocked, registered focus for `chat_id`, else None."""
        raw = self.db.get_kv(f"focus:{int(chat_id)}")
        if raw is None:
            return None
        try:
            focus = json.loads(raw)
        except ValueError:
            return None
        if not isinstance(focus, dict) or not isinstance(focus.get("expires_at"), (int, float)):
            return None
        alias = focus.get("alias")
        if focus["expires_at"] <= self.db.clock() or alias not in PRESETS or alias not in self.unlocked_presets:
            return None
        return raw, focus

    def _prompt_pick_bot(self, chat_id: int) -> None:
        f = self.fmt
        unlocked = [p for p in PRESETS.values() if p.alias in self.unlocked_presets]
        if not unlocked:
            self._send(chat_id, "먼저 대화할 봇을 선택해야 하지만 사용 가능한 프리셋이 없습니다. "
                                f"잠금 해제: {f.code(UNLOCKED_PRESETS_ENV, 40)} (.env)")
            return
        buttons = [f.button(p.label, "p", encode_preset_token(p.alias)) for p in unlocked]
        rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
        self._send(chat_id, "\U0001f916 먼저 대화할 봇을 선택하세요 (또는 /ask &lt;질문&gt;).", f.markup(rows))

    @staticmethod
    def _qa_hash(chat_id: int, alias: str | None, question: str) -> str:
        return hashlib.sha256(json.dumps([int(chat_id), alias, question], ensure_ascii=False).encode("utf-8")).hexdigest()

    def _reply_qa_queued(self, chat_id: int, qa_id: str, alias: str | None, created: bool,
                         reply_to: int | None = None) -> None:
        f = self.fmt
        scope = PRESETS[alias].label if alias in PRESETS else "저장소 전체"
        if created:
            self._send(chat_id, f"\U0001f4ac 질문 접수 {f.code(qa_id, 24)} · {f.esc(scope, 40)}\n"
                                "읽기 전용으로 답변을 준비해 보내드립니다.", reply_to=reply_to)
        else:
            self._send(chat_id, f"♻️ 같은 질문이 이미 처리 중입니다 {f.code(qa_id, 24)}", reply_to=reply_to)

    def _enqueue_qa(self, chat_id: int, alias: str | None, question: str) -> tuple[str, bool]:
        qa_id, created = self.db.create_qa_request(chat_id, alias, question, self._qa_hash(chat_id, alias, question),
                                                   ttl=QA_REQUEST_TTL)
        if created:
            self._fire_wake()  # after commit
        return qa_id, created

    def _cmd_ask(self, chat_id: int, arg: str) -> str:
        f = self.fmt
        alias: str | None = None
        question = arg
        head = arg.split(None, 1)
        if head and head[0] in PRESETS:
            if head[0] not in self.unlocked_presets:
                self._send(chat_id, f"⚠️ /ask rejected: preset {f.code(head[0], 22)} is locked")
                return "cmd:ask:rejected"
            alias, question = head[0], (head[1] if len(head) > 1 else "")
        else:
            live = self._live_focus(chat_id)
            alias = live[1]["alias"] if live else None
        try:
            question = validate_conversational_text(question)
        except ValueError as exc:
            self._send(chat_id, f"⚠️ /ask rejected: {f.esc(exc, 200)}\nusage: /ask [preset] &lt;질문&gt;")
            return "cmd:ask:rejected"
        qa_id, created = self._enqueue_qa(chat_id, alias, question)
        self._reply_qa_queued(chat_id, qa_id, alias, created)
        return f"cmd:ask:{'queued' if created else 'duplicate'}:{qa_id}"

    def _preset_task_fields(self, alias: str, text: str) -> tuple[str, str, list[str], dict[str, Any]]:
        """Same scope policy as `/do`: paths/tests/risk come ONLY from the code registry."""
        if alias not in PRESETS or alias not in self.unlocked_presets:
            raise TaskParseError(f"preset {alias} is locked (add it to {UNLOCKED_PRESETS_ENV})")
        _check_no_smuggling(text)
        preset = PRESETS[alias]
        _verify_preset_paths(preset, self.wtm.repo_root)
        spec = {"pytest": list(preset.tests), "preset": preset.alias, "preset_policy": PRESET_POLICY_VERSION}
        return text, preset.min_risk, list(preset.allowed_paths), spec

    def _reply_preset_task(self, chat_id: int, alias: str, tid: str, created: bool) -> None:
        f = self.fmt
        if not created:
            self._reply_duplicate(chat_id, tid)
            return
        preset = PRESETS[alias]
        self._send(
            chat_id,
            f"\U0001f195 {f.esc(preset.label, 40)} 작업 생성 {f.code(tid, 64)} ({f.esc(preset.min_risk, 4)} · "
            f"파일 {len(preset.allowed_paths)}개 · 테스트 {len(preset.tests)}개).\n"
            "감사(audit) 후 AWAITING_START_AUTH 에서 ▶️ Start 버튼이 도착합니다.",
        )

    def _on_plain_text(self, chat_id: int, text: str, message_id: Any) -> str:
        f = self.fmt
        reply_to = message_id if isinstance(message_id, int) and not isinstance(message_id, bool) else None
        try:
            clean = validate_conversational_text(text)
        except ValueError as exc:
            self._send(chat_id, f"⚠️ 메시지를 처리할 수 없습니다: {f.esc(exc, 200)}", reply_to=reply_to)
            return "text:invalid"
        live = self._live_focus(chat_id)
        if live is None:
            self._prompt_pick_bot(chat_id)
            return "text:no_focus"
        raw, focus = live
        refreshed = dict(focus, expires_at=self.db.clock() + FOCUS_TTL)
        if not self.db.refresh_focus_cas(chat_id, raw, json.dumps(refreshed, sort_keys=True)):
            self._prompt_pick_bot(chat_id)  # focus changed underneath us: fail closed
            return "text:no_focus"
        alias = focus["alias"]
        intent = classify_intent(clean)
        if intent == "ask":
            qa_id, created = self._enqueue_qa(chat_id, alias, clean)
            self._reply_qa_queued(chat_id, qa_id, alias, created, reply_to=reply_to)
            return f"text:ask:{'queued' if created else 'duplicate'}:{qa_id}"
        if intent == "edit":
            try:
                request, risk, paths, spec = self._preset_task_fields(alias, clean)
            except TaskParseError as exc:
                self._send(chat_id, f"⚠️ 수정 요청 거부: {f.esc(exc, 200)}", reply_to=reply_to)
                return "text:edit:rejected"
            tid, created = self.db.create_task(request, risk, paths, spec)
            if created:
                self._fire_wake()  # after commit
            self._reply_preset_task(chat_id, alias, tid, created)
            return f"text:edit:{'created' if created else 'duplicate'}:{tid}"
        token = secrets.token_urlsafe(16)
        draft = {"token": token, "alias": alias, "focus_ver": focus.get("ver"), "text": clean,
                 "expires_at": self.db.clock() + DRAFT_TTL}
        self.db.set_kv(f"draft:{int(chat_id)}", json.dumps(draft, ensure_ascii=False, sort_keys=True))
        markup = f.markup([
            [f.button("\U0001f4ac 질문 답변", "q", token), f.button("\U0001f6e0 수정 작업", "e", token)],
            [f.button("❌ 취소", "z", token)],
        ])
        self._send(chat_id, f"\U0001f914 {f.esc(PRESETS[alias].label, 40)}: 질문인가요, 수정 요청인가요?\n"
                            f"{f.i(clean, 300)}", markup, reply_to=reply_to)
        return "text:ambiguous"

    def _cmd_status(self, chat_id: int, arg: str) -> str:
        f = self.fmt
        if arg:
            if not TASK_ID_RE.match(arg):
                self._send(chat_id, "invalid task id")
                return "cmd:status:bad-id"
            try:
                task = self.db.get_task(arg)
            except KeyError:
                self._send(chat_id, f"unknown task {f.code(arg, 64)}")
                return "cmd:status:unknown"
            text, markup = self.renderer.task_card(task)
            self._send(chat_id, text, markup)
            return "cmd:status:task"
        counts = self.db.outbox_counts()
        tasks = [t for t in self.db.list_tasks() if t["state"] not in TERMINAL_STATES]
        lines = [
            "<b>auto_factory status</b>",
            f"STOP.flag: {f.b('ON' if self.stop_flag.exists() else 'off', 8)} · "
            f"circuit: {f.b('OPEN' if self.db.circuit_is_open(CIRCUIT) else 'closed', 8)}",
            f"outbox pending {counts['PENDING']} · unknown {counts['UNKNOWN']}",
            f"open tasks: {len(tasks)}",
        ]
        if self.db.circuit_is_open(QA_CIRCUIT):
            lines.insert(2, f"qa_circuit: {f.b('OPEN', 8)}")
        for t in tasks[:15]:
            lines.append(f"{f.code(t['id'], 64)} {f.esc(t['state'], 40)} {f.esc(t['risk_level'], 4)}")
        self._send(chat_id, "\n".join(lines))
        return "cmd:status"

    # ----------------------------------------------------------- callbacks
    def _on_callback(self, cq: dict[str, Any]) -> str:
        from_id = (cq.get("from") or {}).get("id")
        msg = cq.get("message") or {}
        chat_id = (msg.get("chat") or {}).get("id")
        if not self._authorized(from_id, chat_id):
            return "ignored:unauthorized"
        # [TG-03] answer FIRST; if that fails there are zero DB/Git side effects.
        self.limiter.wait(None)
        try:
            answered = self.transport.answer_callback_query(str(cq.get("id", "")))
        except Exception:
            answered = False
        if answered is not True:
            return "callback:answer_failed"
        m = CALLBACK_RE.match(cq.get("data") or "")
        if not m:
            return "callback:malformed"
        op, token = m.groups()
        if op == "p":  # never touches tg_actions / tasks (only the chat focus in bot_kv)
            return self._cb_preset(token, chat_id)
        if op in ("q", "e", "z"):  # ambiguous-message draft: single-tx CAS consume in the DB
            try:
                return self._cb_draft(op, token, chat_id, msg.get("message_id"))
            except ActionRejected as exc:
                self._send(chat_id, f"⚠️ 선택이 만료되었거나 이미 처리되었습니다 ({self.fmt.code(exc.code, 40)}). "
                                    "메시지를 다시 보내주세요.")
                return f"callback:rejected:{exc.code}"
        handler = {
            "s": self._cb_start, "c": self._cb_cancel, "d": self._cb_diff,
            "r": self._cb_prompt, "m": self._cb_prompt, "x": self._cb_prompt,
            "R": self._cb_review_confirm, "M": self._cb_merge_confirm, "X": self._cb_discard_confirm,
        }[op]
        try:
            return handler(op, token, chat_id, from_id, msg.get("message_id"))
        except ActionRejected as exc:
            self._send(chat_id, f"⚠️ Action rejected: {self.fmt.code(exc.code, 40)}. Use /status for fresh buttons.")
            return f"callback:rejected:{exc.code}"

    @staticmethod
    def _actor(user_id: int) -> str:
        return f"human:tg:{user_id}"

    def _cb_draft(self, op: str, token: str, chat_id: int, message_id: Any) -> str:
        f = self.fmt
        raw = self.db.get_kv(f"draft:{int(chat_id)}")
        if raw is None:
            raise ActionRejected("DRAFT_MISSING", "draft already used or cleared")
        if op == "z":
            self.db.consume_draft_cancel(chat_id, raw, token)
            self._edit(chat_id, message_id, "❌ 취소했습니다.")
            return "callback:draft:cancel"
        try:
            draft = json.loads(raw)
        except ValueError:
            raise ActionRejected("DRAFT_INVALID") from None
        if not isinstance(draft, dict):
            raise ActionRejected("DRAFT_INVALID")
        if op == "q":
            alias, question = draft.get("alias"), str(draft.get("text") or "")
            qa_id, alias, created = self.db.consume_draft_for_qa(
                chat_id, raw, token, self.unlocked_presets, question, self._qa_hash(chat_id, alias, question),
                ttl=QA_REQUEST_TTL,
            )
            if created:
                self._fire_wake()  # after commit
            label = "질문 접수" if created else "같은 질문이 이미 처리 중"
            self._edit(chat_id, message_id, f"\U0001f4ac {label} {f.code(qa_id, 24)} · {f.esc(PRESETS[alias].label, 40)}")
            return f"callback:draft:ask:{qa_id}"
        try:
            tid, alias, created = self.db.consume_draft_for_task(
                chat_id, raw, token, self.unlocked_presets, self._preset_task_fields
            )
        except TaskParseError as exc:  # rolled back: the draft is untouched
            self._edit(chat_id, message_id, f"⚠️ 수정 요청 거부: {f.esc(exc, 200)}")
            return "callback:draft:rejected"
        if created:
            self._fire_wake()  # after commit
        self._edit(chat_id, message_id, f"\U0001f6e0 수정 작업 {'생성' if created else '중복'} {f.code(tid, 64)}")
        self._reply_preset_task(chat_id, alias, tid, created)
        return f"callback:draft:edit:{tid}"

    def _cb_preset(self, token: str, chat_id: int) -> str:
        """Preset info card + chat focus: no action token is issued/consumed and no task is created."""
        f = self.fmt
        try:
            alias = decode_preset_token(token)
        except ValueError:
            alias = ""
        if alias not in PRESETS or alias not in self.unlocked_presets:
            self._send(chat_id, "⚠️ 사용할 수 없는 프리셋입니다 (잠김 또는 미등록). /bots 로 다시 확인하세요.")
            return "callback:preset:unavailable"
        p = PRESETS[alias]
        lines = [
            f"<b>{f.esc(p.label, 40)}</b> · {f.code(p.alias, 22)} · {f.esc(p.min_risk, 4)}",
            f.esc(p.description, 200),
            f"파일 {len(p.allowed_paths)}개 · 테스트 스위트:",
        ]
        lines += [f"• {f.code(t, 80)}" for t in p.tests]
        lines.append("이제 이 채팅에 질문이나 수정 요청을 그대로 입력하세요 (15분 유지 · /clear 해제).")
        lines.append("또는 아래 문장을 복사해 내용을 바꿔 보내세요:")
        lines.append(f.code(f"/do {p.alias} 원하는 작업 내용", 120))
        focus = {"alias": alias, "ver": secrets.token_urlsafe(8), "expires_at": self.db.clock() + FOCUS_TTL}
        self.db.set_focus_and_clear_draft(chat_id, json.dumps(focus, sort_keys=True))
        markup = {"force_reply": True, "selective": False,
                  "input_field_placeholder": f"{p.label} · 질문 또는 수정 요청 입력"[:64]}
        self._send(chat_id, "\n".join(lines), markup)
        return f"callback:preset:{alias}"

    def _merge_summary(self, task: dict[str, Any], head: str | None) -> list[str]:
        """Human-friendly `Files:` / `Tests:` lines; any failure degrades to a neutral note."""
        f = self.fmt
        stat = ""
        if task.get("base_sha") and head:
            try:
                stat = self.wtm.git("diff", "--shortstat", task["base_sha"], head, check=False).strip()
            except Exception as exc:
                self.log(f"merge summary shortstat failed: {type(exc).__name__}")
        gates_txt = "(no gate report)"
        rows = self.db.artifacts(task["id"], "gates")
        if rows:
            try:
                raw = safe_read_bytes(rows[-1]["path"], 64 * 1024)
                if rows[-1].get("sha256") and hashlib.sha256(raw).hexdigest() != rows[-1]["sha256"]:
                    raise ValueError("gate report sha256 mismatch")
                report = json.loads(raw.decode("utf-8"))
                parts = [f"{str(g.get('gate', '?'))[:20]} {'PASS' if g.get('ok') is True else 'FAIL'}"
                         for g in report if isinstance(g, dict)]
                gates_txt = " · ".join(parts[:6]) or gates_txt
            except Exception as exc:
                self.log(f"merge summary gates unreadable: {type(exc).__name__}")
                gates_txt = "(gate report unreadable)"
        return [f"Files: {f.esc(stat or '(no changes)', 200)}", f"Tests: {f.esc(gates_txt, 200)}"]

    def _cb_start(self, op, token, chat_id, user_id, message_id) -> str:
        def effect(c, action, task):
            self.db._insert_approval(
                c, task["id"], "start", ttl=START_APPROVAL_TTL, actor_user_id=user_id, actor_chat_id=chat_id,
                action_token=token, idempotency_key=f"tok:{token}",
            )

        _, task, _ = self.db.consume_action_token(token, op, chat_id, user_id, effect)
        self._fire_wake()  # approval committed: let the worker pick it up now
        self._edit(chat_id, message_id, f"▶️ Start approved for {self.fmt.code(task['id'], 64)}.")
        return "callback:start"

    def _cb_cancel(self, op, token, chat_id, user_id, message_id) -> str:
        def effect(c, action, task):
            self.db._transition_in_tx(c, task["id"], task["state_version"], "CANCELLED", self._actor(user_id),
                                      "cancelled via dev bot")

        _, task, _ = self.db.consume_action_token(token, op, chat_id, user_id, effect)
        self._edit(chat_id, message_id, f"❌ Cancelled {self.fmt.code(task['id'], 64)}.")
        return "callback:cancel"

    def _cb_diff(self, op, token, chat_id, user_id, message_id) -> str:
        _, task, _ = self.db.consume_action_token(token, op, chat_id, user_id)
        self._send(chat_id, self.renderer.diff_view(task))
        return "callback:diff"

    _PROMPTS = {
        "r": ("R", "✅ Confirm manual review", "Confirm you reviewed this exact diff (single-provider candidate)."),
        "m": ("M", "✅ Confirm Merge", "Confirm fast-forward merge of this exact candidate into main."),
        "x": ("X", "\U0001f5d1 Confirm Discard", "Confirm discard (worktree removed, patch preserved)."),
    }

    def _cb_prompt(self, op, token, chat_id, user_id, message_id) -> str:
        """Step 1 of 2: consume the prompt token and edit the message in place with a confirm token."""
        _, task, _ = self.db.consume_action_token(token, op, chat_id, user_id)
        confirm_op, label, blurb = self._PROMPTS[op]
        confirm = self.db.issue_action_token(task["id"], confirm_op, chat_id, user_id,
                                             expected_version=task["state_version"])
        f = self.fmt
        text = "\n".join([
            f"<b>{f.esc(blurb, 120)}</b>",
            f"task {f.code(task['id'], 64)} · {f.esc(task['state'], 40)} v{int(task['state_version'])}",
            f"target_sha {f.code(task.get('target_sha') or '-', 40)}",
            f"diff_sha256 {f.code(task.get('diff_sha256') or '-', 64)}",
        ])
        self._edit(chat_id, message_id, text, f.markup([[f.button(label, confirm_op, confirm)]]))
        return f"callback:prompt:{op}"

    def _cb_review_confirm(self, op, token, chat_id, user_id, message_id) -> str:
        def effect(c, action, task):
            self.db._insert_approval(
                c, task["id"], "manual_review", task["target_sha"], task["diff_sha256"], ttl=REVIEW_APPROVAL_TTL,
                actor_user_id=user_id, actor_chat_id=chat_id, action_token=token, idempotency_key=f"tok:{token}",
            )
            self.db._transition_in_tx(c, task["id"], task["state_version"], "AWAITING_MERGE", self._actor(user_id),
                                      "manual review confirmed via dev bot")

        _, task, _ = self.db.consume_action_token(token, op, chat_id, user_id, effect)
        self._edit(chat_id, message_id,
                   f"✅ Manual review recorded for {self.fmt.code(task['id'], 64)}; awaiting merge approval.")
        return "callback:review"

    def _cb_merge_confirm(self, op, token, chat_id, user_id, message_id) -> str:
        def effect(c, action, task):
            self.db._insert_approval(
                c, task["id"], "merge", task["target_sha"], task["diff_sha256"], ttl=self.merge_approval_ttl,
                actor_user_id=user_id, actor_chat_id=chat_id, action_token=token, idempotency_key=f"tok:{token}",
            )

        _, task, _ = self.db.consume_action_token(token, op, chat_id, user_id, effect)
        outcome = self.coordinator.merge(task["id"], self._actor(user_id))
        f = self.fmt
        if outcome.ok:
            note = "" if outcome.cleanup_ok else " (cleanup pending: ORPHAN_CLEANUP)"
            summary = "\n".join(self._merge_summary(task, outcome.head))
            self._edit(chat_id, message_id,
                       f"\U0001f500 Merged {f.code(task['id'], 64)} → main {f.code((outcome.head or '')[:12], 12)}"
                       f"{f.esc(note, 60)}\n{summary}")
            return "callback:merged"
        self._edit(chat_id, message_id,
                   f"⛔ Merge refused for {f.code(task['id'], 64)}: {f.code(outcome.code, 40)} {f.esc(outcome.detail, 300)}")
        return f"callback:merge_failed:{outcome.code}"

    def _cb_discard_confirm(self, op, token, chat_id, user_id, message_id) -> str:
        def effect(c, action, task):
            self.db._insert_approval(
                c, task["id"], "discard", task["target_sha"], task["diff_sha256"],
                actor_user_id=user_id, actor_chat_id=chat_id, action_token=token, idempotency_key=f"tok:{token}",
            )
            self.db._transition_in_tx(c, task["id"], task["state_version"], "DISCARDED", self._actor(user_id),
                                      "discarded via dev bot")

        _, task, _ = self.db.consume_action_token(token, op, chat_id, user_id, effect)
        patch_note = ""
        if task.get("worktree_path") and task.get("base_sha"):
            try:
                info = WorktreeInfo(task["id"], task["worktree_path"], self.wtm.branch_for(task["id"]), task["base_sha"])
                patch = self.wtm.cleanup(info, self.artifacts_dir, delete_branch=False)
                patch_note = f" Patch preserved: {patch.name}"
            except Exception as exc:
                patch_note = f" Cleanup failed ({type(exc).__name__}); worktree kept."
        self._edit(chat_id, message_id, f"\U0001f5d1 Discarded {self.fmt.code(task['id'], 64)}.{self.fmt.esc(patch_note, 200)}")
        return "callback:discarded"


# ===================================================================== demo
class _DemoClock:
    def __init__(self, t: float = 1_700_000_000.0):
        self.t = t

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.t += max(0.0, s)


class _InstantGates:
    """Demo-only gate stub (the real gates are exercised by `worker --demo` and the tests)."""

    def __init__(self):
        from .guardrails import GateResult

        self._r = GateResult

    def py_compile(self, wt, files):
        return self._r("py_compile", True, 0)

    def pytest(self, wt, targets, name="pytest"):
        return self._r(name, True, 0)

    def holdout(self, wt, holdout_dir):
        return self._r("holdout", True, 0)


def run_demo(out: Callable[[str], None] = print) -> int:
    """Offline end-to-end walkthrough. Zero network, zero env/.env reads, temp sandbox only."""
    from .adapters import MockAdapter
    from .worker import PipelineWorker, build_demo_repo

    started = time.monotonic()
    admin, intruder = 424242424, 999999999
    checks: list[tuple[str, bool]] = []

    def check(name: str, ok: bool) -> None:
        checks.append((name, bool(ok)))
        out(f"[demo] {'ok  ' if ok else 'FAIL'} {name}")

    cwd = os.getcwd()
    with tempfile.TemporaryDirectory(prefix="af_devbot_demo_", ignore_cleanup_errors=True) as tmp:
        root = Path(tmp)
        repo = root / "repo"
        build_demo_repo(repo)
        clock = _DemoClock()
        (root / "state").mkdir()
        db = CoordinatorDB(root / "state" / "coordinator.db", clock=clock)
        tr = FakeTransport()
        bot = DevBot(
            db, tr, admin, repo, artifacts_dir=root / "state" / "artifacts", worktrees_dir=root / "wts",
            stop_flag=repo / STOP_FLAG_NAME, limiter=RateLimiter(clock=clock, sleep=clock.sleep),
        )
        worker = PipelineWorker(
            db, repo, {"claude": MockAdapter("claude", [_demo_fix]),
                       "codex": MockAdapter("codex", is_available=False),
                       "gemini": MockAdapter("gemini", is_available=False)},
            artifacts_dir=root / "state" / "artifacts", worktrees_dir=root / "wts",
            stop_flag=repo / STOP_FLAG_NAME, gate_runner=_InstantGates(), sleep_fn=clock.sleep,
        )
        uid = [0]

        def msg(text: str, frm: int = admin, chat: int = admin) -> str:
            uid[0] += 1
            return bot.handle_update({"update_id": uid[0], "message": {
                "message_id": uid[0], "from": {"id": frm}, "chat": {"id": chat}, "text": text}})

        def press(op: str, token: str, frm: int = admin, message_id: int = 1) -> str:
            uid[0] += 1
            return bot.handle_update({"update_id": uid[0], "callback_query": {
                "id": f"cq{uid[0]}", "from": {"id": frm}, "data": f"af2|{op}|{token}",
                "message": {"message_id": message_id, "chat": {"id": admin}}}})

        try:
            out(f"[demo] sandbox={root} (network=0, env=0, product DB=0)")
            bot.boot()
            check("intruder message ignored silently", msg("/status", frm=intruder) == "ignored:unauthorized"
                  and tr.send_attempts == 0)
            check("traversal path rejected", msg("/task L1 ../etc/passwd -- pwn") == "cmd:task:rejected")
            check("product DB path rejected", msg("/task L1 data/admission_brief.db -- x") == "cmd:task:rejected")
            res = msg("/task L1 calc.py --tests=tests/test_calc.py -- fix add() so tests pass")
            check("valid /task creates task", res.startswith("cmd:task:created:"))
            tid = res.rsplit(":", 1)[1]

            worker.process_task(tid)
            check("worker parks at AWAITING_START_AUTH", db.get_task(tid)["state"] == "AWAITING_START_AUTH")
            check("outbox row enqueued in same tx", len(db.outbox(tid, "PENDING")) == 1)

            tr.send_script = [TgRetryAfter(3)]
            st = bot.dispatch_once()
            check("429 keeps row PENDING with backoff", st["retry"] == 1 and len(db.outbox(tid, "PENDING")) == 1)
            check("no resend before Retry-After", bot.dispatch_once()["sent"] == 0)
            clock.t += 3.5
            check("sent after Retry-After", bot.dispatch_once()["sent"] == 1)
            start_btns = tr.buttons()
            check("start card has s/c buttons <=64B", set(start_btns) == {"s", "c"})

            check("intruder callback ignored", press("s", start_btns["s"], frm=intruder) == "ignored:unauthorized")
            tr.answer_script = [False]
            check("answerCallbackQuery failure => zero mutation",
                  press("s", start_btns["s"]) == "callback:answer_failed" and not db.has_start_approval(tid))
            check("start approved", press("s", start_btns["s"]) == "callback:start" and db.has_start_approval(tid))
            check("replayed start rejected", press("s", start_btns["s"]) == "callback:rejected:REPLAY")

            worker.process_task(tid)
            task = db.get_task(tid)
            check("single provider => SINGLE_PROVIDER_REVIEW_REQUIRED (SHA-bound)",
                  task["state"] == "SINGLE_PROVIDER_REVIEW_REQUIRED" and len(task["target_sha"] or "") == 40)

            tr.send_script = [TgUnknown("timeout")]
            check("timeout => UNKNOWN", bot.dispatch_once()["unknown"] == 1)
            before = tr.send_attempts
            clock.t += 3600
            bot.dispatch_once()
            check("UNKNOWN never auto-resent", tr.send_attempts == before and len(db.outbox(tid, "UNKNOWN")) == 1)

            msg(f"/status {tid}")
            sprr = tr.buttons()
            check("/status re-issues fresh buttons", set(sprr) == {"d", "r", "x"})
            check("diff view rendered", press("d", sprr["d"]) == "callback:diff" and "return a + b" in tr.sent[-1]["text"])
            check("manual-review prompt edits in place", press("r", sprr["r"]) == "callback:prompt:r")
            confirm_r = tr.buttons(source="edits")["R"]
            check("manual review confirmed => AWAITING_MERGE",
                  press("R", confirm_r) == "callback:review" and db.get_task(tid)["state"] == "AWAITING_MERGE")
            check("stale SPRR discard token rejected", press("x", sprr["x"]) == "callback:rejected:STALE")
            check("forged token rejected", press("M", "A" * 22) == "callback:rejected:UNKNOWN_TOKEN")

            check("AWAITING_MERGE notification sent", bot.dispatch_once()["sent"] == 1)
            am = tr.buttons()
            check("merge prompt", press("m", am["m"]) == "callback:prompt:m")
            confirm_m = tr.buttons(source="edits")["M"]
            check("2-step SHA-bound merge", press("M", confirm_m) == "callback:merged")
            task = db.get_task(tid)
            check("task MERGED and main HEAD == target_sha",
                  task["state"] == "MERGED" and bot.wtm.head_sha() == task["target_sha"])
            check("merge intent COMPLETED, worktree cleaned",
                  [i["status"] for i in db.merge_intents(tid)] == ["COMPLETED"]
                  and not Path(task["worktree_path"]).exists())
            check("replayed merge confirm rejected", press("M", confirm_m) == "callback:rejected:REPLAY")
            check("/kill writes STOP.flag", msg("/kill") == "cmd:kill" and bot.stop_flag.exists())
            check("/resume removes STOP.flag", msg("/resume") == "cmd:resume" and not bot.stop_flag.exists())
            for m in tr.sent + tr.edits:
                SafeHtmlFormatter().finalize(m["text"])
            check("all messages pass HTML/length validation", True)
        finally:
            db.close()
            os.chdir(cwd)
    elapsed = time.monotonic() - started
    ok = all(c for _, c in checks)
    out(f"[demo] RESULT {'PASS' if ok else 'FAIL'}: {sum(c for _, c in checks)}/{len(checks)} checks in {elapsed:.2f}s")
    return 0 if ok else 1


def _demo_fix(wt: str, contract, role) -> None:
    Path(wt, "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")


# ===================================================================== check
EXIT_OK, EXIT_ERROR, EXIT_LOCAL, EXIT_TELEGRAM, EXIT_LOCKED, EXIT_INTERRUPTED = 0, 1, 2, 3, 4, 130


def default_env_file(repo: str | os.PathLike, explicit: str | os.PathLike | None) -> Path | None:
    """`--env-file` if given, else `<repo>/.env` when present (links are rejected by the loader)."""
    if explicit:
        return Path(explicit)
    candidate = Path(repo) / ".env"
    return candidate if os.path.lexists(candidate) else None


def run_check(
    *,
    repo: str | os.PathLike,
    db: str | os.PathLike | None = None,
    artifacts: str | os.PathLike | None = None,
    worktrees: str | os.PathLike | None = None,
    stop_flag: str | os.PathLike | None = None,
    env_file: str | os.PathLike | None = None,
    process_env: Mapping[str, str] | None = None,
    transport_factory: Callable[[str], Any] | None = None,
    adapters: Mapping[str, Any] | None = None,
    out: Callable[[str], None] = print,
) -> int:
    """Read-only preflight. Local checks first; `getMe` only if they all pass.

    Never opens CoordinatorDB (existing DB is inspected via mode=ro), never boots the bot,
    never sends a message. Exit: 0 pass, 1 unexpected, 2 local failure, 3 getMe failure,
    4 supervisor.lock held, 130 Ctrl+C. Output lines never contain the token.
    """
    from .worker import resolve_state_paths

    warns = [0]

    def emit(level: str, msg: str) -> None:
        if level == "WARN":
            warns[0] += 1
        out(f"CHECK {level} {msg}")

    try:
        try:
            cfg = load_dev_env_file(env_file, process_env)
        except ConfigError as exc:
            emit("FAIL", f"config: {exc}")
            return EXIT_LOCAL
        emit("PASS", f"config admin_chat_id={cfg.admin_chat_id} token=*** env_file={'yes' if env_file else 'no'} "
                     f"token_reuse=none unlocked_presets={len(cfg.unlocked_presets)}")
        try:
            layout = resolve_state_paths(repo, db, artifacts, worktrees, stop_flag)
        except ValueError as exc:
            emit("FAIL", f"state layout: {exc}")
            return EXIT_LOCAL
        emit("PASS", f"state layout state_dir={layout['state_dir']}")
        if _FileLock(layout["lock"], role="supervisor").is_held():
            emit("FAIL", f"supervisor.lock is held: {layout['lock']}")
            return EXIT_LOCKED
        emit("PASS", "supervisor.lock free")
        try:
            wtm = WorktreeManager(repo, layout["worktrees"])
            head = wtm.head_sha()
            dirty = wtm.git("--no-optional-locks", "status", "--porcelain=v1", "--untracked-files=all") != ""
        except Exception as exc:
            emit("FAIL", f"git: {type(exc).__name__}")
            return EXIT_LOCAL
        emit("PASS", f"repo HEAD={head[:12]}")
        if dirty:
            emit("WARN", "main worktree is dirty (merges will be refused with MAIN_DIRTY)")
        if os.path.lexists(layout["stop_flag"]):
            emit("WARN", f"STOP.flag present: {layout['stop_flag']}")
        if adapters is None:
            from .adapters import default_adapters

            adapters = default_adapters()
        for name, adapter in adapters.items():
            if adapter.available():
                emit("PASS", f"cli {name} available")
            else:
                emit("WARN", f"cli {name} not available")
        if layout["db"].exists():
            try:
                summary = read_db_summary_readonly(layout["db"])
            except Exception as exc:
                emit("FAIL", f"db: {type(exc).__name__}: {exc}")
                return EXIT_LOCAL
            emit("PASS", f"db read-only tasks={len(summary['tasks'])} "
                         f"open_merge_intents={summary['open_merge_intents']}")
        else:
            emit("WARN", "db not created yet (first --run creates it)")
        factory = transport_factory or HttpTransport
        try:
            me = factory(cfg.token).get_me()
        except TgError as exc:
            emit("FAIL", f"telegram getMe: {type(exc).__name__}: {str(exc)[:120]}")
            return EXIT_TELEGRAM
        except Exception as exc:
            emit("FAIL", f"telegram getMe: {type(exc).__name__}")
            return EXIT_TELEGRAM
        if not isinstance(me, dict) or me.get("is_bot") is not True:
            emit("FAIL", "telegram getMe: not a bot account")
            return EXIT_TELEGRAM
        emit("PASS", f"telegram getMe bot=@{str(me.get('username', '?'))[:64]}")
        out(f"CHECK RESULT PASS warnings={warns[0]}")
        return EXIT_OK
    except KeyboardInterrupt:
        out("CHECK ABORTED (interrupted)")
        return EXIT_INTERRUPTED
    except Exception as exc:
        out(f"CHECK FAIL unexpected: {type(exc).__name__}")
        return EXIT_ERROR


# ====================================================================== CLI
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="auto_factory.dev_bot")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--demo", action="store_true", help="offline walkthrough (no network, env or DB)")
    mode.add_argument("--check", action="store_true", help="read-only preflight + Telegram getMe")
    mode.add_argument("--status", action="store_true", help="read-only (mode=ro) DB summary")
    mode.add_argument("--poll-once", action="store_true", help="one getUpdates + one outbox dispatch")
    mode.add_argument("--run", action="store_true", help="long-poll loop")
    parser.add_argument("--db", help="coordinator DB (default: external state dir; never *.sqlite3)")
    parser.add_argument("--repo", default=".", help="repository root")
    parser.add_argument("--artifacts", help="artifacts dir (default: <state>/artifacts)")
    parser.add_argument("--worktrees", help="worktrees dir (default: <state>/worktrees)")
    parser.add_argument("--stop-flag", help="STOP.flag path (default: <state>/STOP.flag)")
    parser.add_argument("--env-file", help="strict env file (default: <repo>/.env if present)")
    args = parser.parse_args(argv)

    if args.demo:  # decided before any env/token/DB/network access
        return run_demo()
    from .worker import print_status_readonly, resolve_state_paths

    if args.status:
        try:
            layout = resolve_state_paths(args.repo, args.db, args.artifacts, args.worktrees, args.stop_flag)
        except ValueError as exc:
            print(f"[dev_bot] FAIL-CLOSED: {exc}", file=sys.stderr)
            return EXIT_LOCAL
        return print_status_readonly(layout["db"])
    env_file = default_env_file(args.repo, args.env_file)
    if args.check:
        return run_check(repo=args.repo, db=args.db, artifacts=args.artifacts, worktrees=args.worktrees,
                         stop_flag=args.stop_flag, env_file=env_file)
    try:
        cfg = load_dev_env_file(env_file)
    except ConfigError as exc:
        print(f"[dev_bot] FAIL-CLOSED: {exc}", file=sys.stderr)
        return EXIT_LOCAL
    try:
        layout = resolve_state_paths(args.repo, args.db, args.artifacts, args.worktrees, args.stop_flag)
    except ValueError as exc:
        print(f"[dev_bot] FAIL-CLOSED: {exc}", file=sys.stderr)
        return EXIT_LOCAL
    lock = _FileLock(layout["lock"], role="supervisor")  # before the DB is opened
    try:
        lock.acquire()
    except SupervisorLocked as exc:
        print(f"[dev_bot] {exc}", file=sys.stderr)
        return EXIT_LOCKED
    try:
        db = CoordinatorDB(layout["db"])
        try:
            bot = DevBot(
                db, HttpTransport(cfg.token), cfg.admin_chat_id, args.repo,
                artifacts_dir=layout["artifacts"], worktrees_dir=layout["worktrees"],
                stop_flag=layout["stop_flag"], state_dir=layout["state_dir"], require_supervisor_lock=True,
                log=lambda m: print(f"[dev_bot] {m}", file=sys.stderr), unlocked_presets=cfg.unlocked_presets,
            )
            try:
                bot.boot()
                if args.poll_once:
                    bot.poll_once(timeout=0)
                    bot.dispatch_once()
                    return EXIT_OK
                try:
                    while True:
                        bot.poll_once()
                        bot.dispatch_once()
                except KeyboardInterrupt:
                    return EXIT_OK
            finally:
                bot.close()
        finally:
            db.close()
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
