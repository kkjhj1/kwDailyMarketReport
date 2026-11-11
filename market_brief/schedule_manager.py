"""Admin-managed OS cron schedule manager (L2: ⏰ 자동 실행 주기 관리).

Design (see scratch/gpt_architecture.md):
- DB SSOT: ``system_settings("bot_schedules")`` + ``system_config_history``.
- OS crontab is synced through a ``# [ADMIN-MANAGED]`` marker block only. Every line outside
  the block (``0 4 * * *`` DB backup, ``SHELL=``/``PATH=``, unknown cron/comment lines) is
  preserved byte-for-byte.
- Recoverable sync journal (``bot_schedules_pending``): PENDING -> crontab write -> verify
  -> canonical DB commit (APPLIED). DB failure -> crontab rollback; rollback failure -> DIVERGED.
- [PROD-LIVE]: saving/toggling NEVER executes ``run_cron.sh``, never spawns Naver/Playwright,
  never sends Telegram, never compensates missed slots. Only ``crontab -l`` / ``crontab -`` are
  ever invoked, and only through ``LinuxCrontabDriver``.
- [VERIFY-02]: tests / Windows use ``InMemoryCrontabDriver`` / ``UnavailableCrontabDriver``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import difflib
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Protocol, runtime_checkable
from zoneinfo import ZoneInfo

KST = ZoneInfo("Asia/Seoul")

MANAGED_START = "# [ADMIN-MANAGED]"
MANAGED_END = "# [/ADMIN-MANAGED]"
DISABLED_PREFIX = "# [DISABLED] "
LEGACY_SECTION_HEADERS = frozenset({"# [1. 시황봇]", "# [2. 여행봇]", "# [3. 입시봇]"})
SETTING_KEY = "bot_schedules"

TIME_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
CRON_LINE_RE = re.compile(
    r"^\s*(?P<expr>\S+\s+\S+\s+\S+\s+\S+\s+\S+)\s+(?P<cmd>\S*run_cron\.sh)\s+(?P<task>[A-Za-z0-9_]+)\s*$"
)
DB_BACKUP_RE = re.compile(r"^\s*0\s+4\s+\*\s+\*\s+\*\s+.*alerts\.sqlite3")
# Characters that must never appear in a rendered command path (zero shell-injection surface).
_UNSAFE_PATH_CHARS = set(" \t\r\n;&|$`<>(){}[]'\"\\*?!#%~")

DAYS_MODES = ("DAILY", "WEEKDAYS", "CUSTOM")
WEEKDAY_LABELS_KO = ("일", "월", "화", "수", "목", "금", "토")  # cron 0=Sun .. 6=Sat
WEEKDAYS_MON_FRI = [1, 2, 3, 4, 5]
URGENT_INTERNAL_CLOSE_MIN = 15 * 60 + 30


# =========================================================================
# Task Catalog (strict allowlist)
# =========================================================================


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    bot_id: str
    label: str
    description: str
    editable: bool = True
    control_mode: str = "OS_CRON_ONLY"
    priority: int = 10
    default_times: tuple[str, ...] = ()
    days_mode: str = "DAILY"
    weekdays: tuple[int, ...] = ()
    raw_cron_expr: str | None = None
    min_interval_min: int | None = None
    max_daily_runs: int = 24
    warn_daily_runs: int | None = None
    reject_hours: frozenset[int] = frozenset()
    warn_hours: frozenset[int] = frozenset()
    internal_scheduler_note: str = ""

    def to_public(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "bot_id": self.bot_id,
            "label": self.label,
            "description": self.description,
            "editable": self.editable,
            "control_mode": self.control_mode,
            "priority": self.priority,
            "default_times": list(self.default_times),
            "default_days_mode": self.days_mode,
            "default_weekdays": list(self.weekdays),
            "default_raw_cron_expr": self.raw_cron_expr,
            "guardrails": {
                "min_interval_min": self.min_interval_min,
                "max_daily_runs": self.max_daily_runs,
                "warn_daily_runs": self.warn_daily_runs,
                "reject_hours": sorted(self.reject_hours),
                "warn_hours": sorted(self.warn_hours),
            },
            "internal_scheduler_note": self.internal_scheduler_note,
        }


TASK_CATALOG: dict[str, TaskSpec] = {
    "travel_watches": TaskSpec(
        task_id="travel_watches",
        bot_id="travel_bot",
        label="여행 특가 감시 알림",
        description="도시별 가격 관심목록을 확인해 하락 시 알림합니다 (OS cron 전용).",
        priority=1,
        default_times=("07:30", "12:30", "18:30"),
        days_mode="DAILY",
        min_interval_min=120,
        max_daily_runs=12,
        warn_daily_runs=6,
        reject_hours=frozenset({23, 0, 1, 2, 3, 4, 5, 6}),
        warn_hours=frozenset({22}),
    ),
    "travel_weekly": TaskSpec(
        task_id="travel_weekly",
        bot_id="travel_bot",
        label="여행 주간 특가 브리핑",
        description="주간 해외여행 특가 브리핑을 발송합니다.",
        priority=2,
        default_times=("12:00",),
        days_mode="CUSTOM",
        weekdays=(4,),
    ),
    "admission_collect": TaskSpec(
        task_id="admission_collect",
        bot_id="admission_bot",
        label="입시 공지 수집",
        description="입시 설명회·공지를 수집합니다.",
        priority=3,
        default_times=("08:30",),
        days_mode="DAILY",
    ),
    "admission_weekly": TaskSpec(
        task_id="admission_weekly",
        bot_id="admission_bot",
        label="입시 주간 To-Do 브리핑",
        description="수집 후 주간 입시 To-Do 브리핑을 발송합니다.",
        priority=4,
        default_times=("20:00",),
        days_mode="CUSTOM",
        weekdays=(0,),
    ),
    "market_auto": TaskSpec(
        task_id="market_auto",
        bot_id="brief_bot",
        label="시황 정규 브리핑 (OS cron fallback)",
        description="내부 데몬(06:30·15:40)과 OS cron fallback이 함께 동작합니다.",
        editable=False,
        control_mode="READ_ONLY_DUAL_PATH",
        priority=10,
        default_times=("06:33", "15:42"),
        days_mode="WEEKDAYS",
        weekdays=tuple(WEEKDAYS_MON_FRI),
        internal_scheduler_note="내부 데몬: 평일 06:30~06:45 모닝 / 15:40~15:55 마감 시작 윈도우",
    ),
    "market_check_urgent": TaskSpec(
        task_id="market_check_urgent",
        bot_id="brief_bot",
        label="시황 ±3% 긴급변동 감시 (OS cron fallback)",
        description="내부 데몬 15분 감시와 OS cron fallback이 함께 동작합니다.",
        editable=False,
        control_mode="READ_ONLY_DUAL_PATH",
        priority=11,
        days_mode="WEEKDAYS",
        weekdays=tuple(WEEKDAYS_MON_FRI),
        raw_cron_expr="7,22,37,52 9-15 * * 1-5",
        internal_scheduler_note="내부 데몬: 평일 09:00~15:30 15분 주기 감시",
    ),
}


def command_for(task_id: str, app_dir: str) -> str:
    """Server-side command template. Clients can never supply a command."""
    if task_id not in TASK_CATALOG:
        raise ValueError(f"허용되지 않은 작업입니다: {task_id}")
    _assert_safe_app_dir(app_dir)
    return f"{app_dir.rstrip('/')}/run_cron.sh {task_id}"


def _assert_safe_app_dir(app_dir: str) -> None:
    if not app_dir or any(ch in _UNSAFE_PATH_CHARS for ch in app_dir):
        raise ValueError(f"안전하지 않은 앱 경로입니다: {app_dir!r}")


# =========================================================================
# Weekday encoding & cron expressions
# =========================================================================


def cron_dow_to_python_weekday(dow: int) -> int:
    """Cron 0=Sun..6=Sat -> Python datetime.weekday() 0=Mon..6=Sun."""
    return (int(dow) - 1) % 7


def python_weekday_to_cron_dow(weekday: int) -> int:
    return (int(weekday) + 1) % 7


def weekdays_for_mode(days_mode: str, weekdays: list[int] | tuple[int, ...] | None) -> list[int]:
    if days_mode == "DAILY":
        return list(range(7))
    if days_mode == "WEEKDAYS":
        return list(WEEKDAYS_MON_FRI)
    return sorted({int(d) for d in (weekdays or [])})


def dow_field(days_mode: str, weekdays: list[int] | tuple[int, ...] | None) -> str:
    if days_mode == "DAILY":
        return "*"
    if days_mode == "WEEKDAYS":
        return "1-5"
    days = weekdays_for_mode("CUSTOM", weekdays)
    if not days:
        raise ValueError("직접 요일 모드에는 최소 1개 요일이 필요합니다.")
    return ",".join(str(d) for d in days)


def compile_cron_exprs(config: dict[str, Any]) -> list[str]:
    """Mixed-minute compiler: group sorted HH:MM by minute, join hours per minute."""
    raw = config.get("raw_cron_exprs")
    if raw:
        return list(raw)
    times = sorted(set(config.get("times_kst") or []))
    if not times:
        return []
    dow = dow_field(config.get("days_mode", "DAILY"), config.get("weekdays"))
    by_minute: dict[int, set[int]] = {}
    for t in times:
        hh, mm = t.split(":")
        by_minute.setdefault(int(mm), set()).add(int(hh))
    return [
        f"{mm} {','.join(str(h) for h in sorted(hours))} * * {dow}"
        for mm, hours in sorted(by_minute.items(), key=lambda kv: (min(kv[1]), kv[0]))
    ]


def compile_cron_lines(task_id: str, config: dict[str, Any], app_dir: str) -> list[str]:
    cmd = command_for(task_id, app_dir)
    lines = [f"{expr} {cmd}" for expr in compile_cron_exprs(config)]
    if not config.get("enabled", True):
        lines = [DISABLED_PREFIX + line for line in lines]
    return lines


def _parse_cron_field(token: str, lo: int, hi: int, *, is_dow: bool = False) -> set[int]:
    values: set[int] = set()
    for part in token.split(","):
        if not part:
            raise ValueError(f"잘못된 cron 필드: {token}")
        step = 1
        if "/" in part:
            part, step_s = part.split("/", 1)
            step = int(step_s)
            if step <= 0:
                raise ValueError(f"잘못된 cron step: {token}")
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(part)
        top = 7 if is_dow else hi
        if start < lo or end > top or start > end:
            raise ValueError(f"cron 범위 초과: {token}")
        for v in range(start, end + 1, step):
            values.add(0 if (is_dow and v == 7) else v)
    return values


@dataclass
class CronExpr:
    expr: str
    minutes: set[int]
    hours: set[int]
    doms: set[int]
    months: set[int]
    dows: set[int]
    dom_star: bool
    dow_star: bool

    @classmethod
    def parse(cls, expr: str) -> "CronExpr":
        fields = expr.split()
        if len(fields) != 5:
            raise ValueError(f"cron 표현식은 5개 필드여야 합니다: {expr}")
        m, h, dom, mon, dow = fields
        return cls(
            expr=" ".join(fields),
            minutes=_parse_cron_field(m, 0, 59),
            hours=_parse_cron_field(h, 0, 23),
            doms=_parse_cron_field(dom, 1, 31),
            months=_parse_cron_field(mon, 1, 12),
            dows=_parse_cron_field(dow, 0, 6, is_dow=True),
            dom_star=dom == "*",
            dow_star=dow == "*",
        )

    def matches_date(self, d) -> bool:
        if d.month not in self.months:
            return False
        py_weekdays = {cron_dow_to_python_weekday(x) for x in self.dows}
        dom_ok = d.day in self.doms
        dow_ok = d.weekday() in py_weekdays
        if self.dom_star or self.dow_star:
            return dom_ok and dow_ok
        return dom_ok or dow_ok


def next_runs_kst(exprs: list[str], now: datetime | None = None, count: int = 3) -> list[str]:
    """Next N future KST run times (strictly after ``now``). Pure computation."""
    if not exprs or count <= 0:
        return []
    now = (now or datetime.now(KST)).astimezone(KST)
    parsed = [CronExpr.parse(e) for e in exprs]
    results: list[datetime] = []
    start_day = now.date()
    for offset in range(0, 400):
        day = start_day + timedelta(days=offset)
        stamps: set[datetime] = set()
        for ce in parsed:
            if not ce.matches_date(day):
                continue
            for h in ce.hours:
                for m in ce.minutes:
                    dt = datetime(day.year, day.month, day.day, h, m, tzinfo=KST)
                    if dt > now:
                        stamps.add(dt)
        results.extend(sorted(stamps))
        if len(results) >= count:
            break
    return [dt.strftime("%Y-%m-%d (%a) %H:%M KST") for dt in results[:count]]


# =========================================================================
# Validation
# =========================================================================


class ScheduleValidationError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _minutes(t: str) -> int:
    hh, mm = t.split(":")
    return int(hh) * 60 + int(mm)


def normalize_payload(task_id: str, payload: dict[str, Any]) -> tuple[dict[str, Any], list[str], list[str]]:
    """Validates a client payload for an editable task. Returns (config, errors, warnings)."""
    spec = TASK_CATALOG.get(task_id)
    errors: list[str] = []
    warnings: list[str] = []
    if spec is None:
        return {}, [f"허용되지 않은 작업입니다: {task_id}"], warnings

    enabled = payload.get("enabled", True)
    if not isinstance(enabled, bool):
        errors.append("enabled 값은 true/false여야 합니다.")
        enabled = bool(enabled)

    raw_times = payload.get("times_kst")
    if not isinstance(raw_times, list):
        errors.append("times_kst는 HH:MM 문자열 배열이어야 합니다.")
        raw_times = []
    times: set[str] = set()
    for t in raw_times:
        if not isinstance(t, str) or not TIME_RE.match(t):
            errors.append(f"잘못된 시각 형식입니다 (HH:MM 24시간): {t!r}")
            continue
        times.add(t)
    times_sorted = sorted(times)

    days_mode = payload.get("days_mode", spec.days_mode)
    if days_mode not in DAYS_MODES:
        errors.append(f"days_mode는 {', '.join(DAYS_MODES)} 중 하나여야 합니다: {days_mode!r}")
        days_mode = "DAILY"
    raw_weekdays = payload.get("weekdays") or []
    weekdays: list[int] = []
    if not isinstance(raw_weekdays, list):
        errors.append("weekdays는 0(일)~6(토) 정수 배열이어야 합니다.")
    else:
        for d in raw_weekdays:
            if isinstance(d, bool) or not isinstance(d, int) or not 0 <= d <= 6:
                errors.append(f"요일 값은 0(일)~6(토) 정수여야 합니다: {d!r}")
            else:
                weekdays.append(d)
    weekdays = weekdays_for_mode(days_mode, weekdays)
    if days_mode == "CUSTOM" and not weekdays:
        errors.append("직접 요일 모드에는 최소 1개 요일을 선택해야 합니다.")

    if not times_sorted and not any("시각 형식" in e for e in errors):
        errors.append("최소 1개 이상의 실행 시각이 필요합니다.")
    if len(times_sorted) > spec.max_daily_runs:
        errors.append(f"하루 최대 {spec.max_daily_runs}회까지만 실행할 수 있습니다 (현재 {len(times_sorted)}회).")
    elif spec.warn_daily_runs and len(times_sorted) > spec.warn_daily_runs:
        warnings.append(f"하루 {len(times_sorted)}회 실행은 권장치({spec.warn_daily_runs}회)를 초과합니다. 차단 위험에 유의하세요.")

    for t in times_sorted:
        hour = int(t[:2])
        if hour in spec.reject_hours:
            errors.append(f"{t}: 23:00~06:59 KST 야간 시간대에는 실행할 수 없습니다.")
        elif hour in spec.warn_hours:
            warnings.append(f"{t}: 22시대 늦은 시각 알림입니다. 수신자 방해에 유의하세요.")

    if spec.min_interval_min and times_sorted and weekdays:
        week_slots = sorted(
            {d * 1440 + _minutes(t) for d in weekdays for t in times_sorted}
        )
        if len(week_slots) > 1:
            gaps = [b - a for a, b in zip(week_slots, week_slots[1:])]
            gaps.append(week_slots[0] + 7 * 1440 - week_slots[-1])  # weekly wrap-around
            min_gap = min(gaps)
            if min_gap < spec.min_interval_min:
                errors.append(
                    f"실행 간격은 최소 {spec.min_interval_min}분 이상이어야 합니다 "
                    f"(요일 경계 포함 최소 {min_gap}분)."
                )

    config = {
        "enabled": enabled,
        "times_kst": times_sorted,
        "days_mode": days_mode,
        "weekdays": weekdays,
    }
    return config, errors, warnings


def default_config(spec: TaskSpec, enabled: bool = False) -> dict[str, Any]:
    cfg: dict[str, Any] = {
        "enabled": enabled,
        "times_kst": list(spec.default_times),
        "days_mode": spec.days_mode,
        "weekdays": weekdays_for_mode(spec.days_mode, spec.weekdays),
    }
    if spec.raw_cron_expr:
        cfg["raw_cron_exprs"] = [spec.raw_cron_expr]
        cfg["times_kst"] = []
    return cfg


def policy_warnings_for(task_id: str, config: dict[str, Any]) -> list[str]:
    """Non-blocking policy checks against the internal daemon invariants."""
    warnings: list[str] = []
    if task_id == "market_check_urgent":
        late: set[str] = set()
        for expr in compile_cron_exprs(config):
            try:
                ce = CronExpr.parse(expr)
            except ValueError:
                continue
            for h in ce.hours:
                for m in ce.minutes:
                    if h * 60 + m > URGENT_INTERNAL_CLOSE_MIN:
                        late.add(f"{h:02d}:{m:02d}")
        if late:
            warnings.append(
                f"OS cron 긴급감시가 내부 장마감 15:30 이후 {', '.join(sorted(late))}에도 실행됩니다 "
                "(내부 데몬 정책과 불일치, 자동 수정하지 않음)."
            )
    return warnings


# =========================================================================
# Crontab parsing
# =========================================================================


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def import_config_from_exprs(task_id: str, exprs: list[str], enabled: bool = True) -> dict[str, Any]:
    """Converts live cron expressions to a config WITHOUT replacing them by catalog defaults."""
    spec = TASK_CATALOG[task_id]
    if spec.raw_cron_expr is None:
        try:
            parsed = [CronExpr.parse(e) for e in exprs]
            dow_tokens = {e.split()[4] for e in exprs}
            simple = all(
                p.dom_star and e.split()[3] == "*" and re.fullmatch(r"[\d,]+", e.split()[0]) and re.fullmatch(r"[\d,]+", e.split()[1])
                for p, e in zip(parsed, exprs)
            )
            if simple and len(dow_tokens) == 1:
                dow_token = dow_tokens.pop()
                times = sorted({f"{h:02d}:{m:02d}" for p in parsed for h in p.hours for m in p.minutes})
                if dow_token == "*":
                    mode, days = "DAILY", list(range(7))
                elif sorted(parsed[0].dows) == WEEKDAYS_MON_FRI:
                    mode, days = "WEEKDAYS", list(WEEKDAYS_MON_FRI)
                else:
                    mode, days = "CUSTOM", sorted(parsed[0].dows)
                if len(times) <= 24:
                    return {"enabled": enabled, "times_kst": times, "days_mode": mode, "weekdays": days}
        except ValueError:
            pass
    return {
        "enabled": enabled,
        "times_kst": [],
        "days_mode": spec.days_mode,
        "weekdays": weekdays_for_mode(spec.days_mode, spec.weekdays),
        "raw_cron_exprs": list(exprs),
    }


@dataclass
class ParsedCrontab:
    text: str
    lines: list[str]
    state: str  # READY | MANAGED | MIGRATION_REQUIRED | MIGRATION_CONFLICT
    conflict_reason: str = ""
    block_start: int | None = None
    block_end: int | None = None
    unmanaged: list[dict[str, Any]] = field(default_factory=list)  # {index, task_id, expr, line}
    managed_entries: dict[str, dict[str, list[str]]] = field(default_factory=dict)  # task -> {enabled, disabled}

    @property
    def has_block(self) -> bool:
        return self.block_start is not None

    def managed_block_text(self) -> str:
        if self.block_start is None or self.block_end is None:
            return ""
        return "\n".join(self.lines[self.block_start : self.block_end + 1])

    def managed_block_hash(self) -> str:
        return _sha256(self.managed_block_text())

    def protected_lines(self) -> list[dict[str, Any]]:
        stripped = {u["index"] for u in self.unmanaged}
        stripped |= _attached_legacy_headers(self.lines, stripped)
        out = []
        for i, line in enumerate(self.lines):
            if self.block_start is not None and self.block_end is not None and self.block_start <= i <= self.block_end:
                continue
            if i in stripped or not line.strip():
                continue
            out.append({"line": line, "is_db_backup": bool(DB_BACKUP_RE.match(line))})
        return out

    def imported_configs(self) -> dict[str, dict[str, Any]]:
        """Live configs from managed block entries + unmanaged lines (unmanaged wins)."""
        out: dict[str, dict[str, Any]] = {}
        for task_id, entry in self.managed_entries.items():
            if entry["enabled"]:
                out[task_id] = import_config_from_exprs(task_id, entry["enabled"], enabled=True)
            elif entry["disabled"]:
                out[task_id] = import_config_from_exprs(task_id, entry["disabled"], enabled=False)
        grouped: dict[str, list[str]] = {}
        for u in self.unmanaged:
            grouped.setdefault(u["task_id"], []).append(u["expr"])
        for task_id, exprs in grouped.items():
            out[task_id] = import_config_from_exprs(task_id, exprs, enabled=True)
        return out


def _match_task_line(line: str) -> tuple[str, str] | None:
    m = CRON_LINE_RE.match(line)
    if not m or m.group("task") not in TASK_CATALOG:
        return None
    try:
        CronExpr.parse(m.group("expr"))
    except ValueError:
        return None
    return m.group("task"), " ".join(m.group("expr").split())


def _attached_legacy_headers(lines: list[str], stripped_idx: set[int]) -> set[int]:
    headers: set[int] = set()
    for i in stripped_idx:
        j = i - 1
        if j >= 0 and j not in stripped_idx and lines[j].strip() in LEGACY_SECTION_HEADERS:
            headers.add(j)
    return headers


def parse_crontab(text: str) -> ParsedCrontab:
    lines = text.splitlines()
    starts = [i for i, ln in enumerate(lines) if ln.strip() == MANAGED_START]
    ends = [i for i, ln in enumerate(lines) if ln.strip() == MANAGED_END]

    if starts or ends:
        reason = ""
        if len(starts) != 1 or len(ends) != 1:
            reason = f"관리 마커 개수 이상 (시작 {len(starts)}개 / 종료 {len(ends)}개)"
        elif ends[0] < starts[0]:
            reason = "관리 마커 순서가 뒤집혀 있습니다."
        if reason:
            return ParsedCrontab(text=text, lines=lines, state="MIGRATION_CONFLICT", conflict_reason=reason)

    block_start = starts[0] if starts else None
    block_end = ends[0] if ends else None
    parsed = ParsedCrontab(text=text, lines=lines, state="READY", block_start=block_start, block_end=block_end)

    for i, line in enumerate(lines):
        inside = block_start is not None and block_end is not None and block_start < i < block_end
        if inside:
            body = line.strip()
            disabled = body.startswith(DISABLED_PREFIX.strip())
            if disabled:
                body = body[len(DISABLED_PREFIX.strip()) :].strip()
            hit = _match_task_line(body)
            if hit:
                task_id, expr = hit
                entry = parsed.managed_entries.setdefault(task_id, {"enabled": [], "disabled": []})
                entry["disabled" if disabled else "enabled"].append(expr)
            continue
        if block_start is not None and i in (block_start, block_end):
            continue
        hit = _match_task_line(line)
        if hit:
            parsed.unmanaged.append({"index": i, "task_id": hit[0], "expr": hit[1], "line": line})

    if parsed.unmanaged:
        parsed.state = "MIGRATION_REQUIRED"
    elif parsed.has_block:
        parsed.state = "MANAGED"
    return parsed


def render_managed_block(configs: dict[str, dict[str, Any]], app_dir: str) -> list[str]:
    block = [MANAGED_START, "# 관리자 콘솔에서 자동 생성됨 - 직접 수정하지 마세요 (run_cron.sh 허용 작업만)"]
    for task_id in sorted(TASK_CATALOG, key=lambda t: TASK_CATALOG[t].priority):
        cfg = configs.get(task_id)
        if not cfg:
            continue
        block.extend(compile_cron_lines(task_id, cfg, app_dir))
    block.append(MANAGED_END)
    return block


def render_crontab(parsed: ParsedCrontab, configs: dict[str, dict[str, Any]], app_dir: str) -> str:
    """Rebuilds crontab text. Lines outside the managed block are copied verbatim, except
    unmanaged allowlisted ``run_cron.sh <task>`` lines (+ attached legacy headers)."""
    if parsed.state == "MIGRATION_CONFLICT":
        raise ValueError("MIGRATION_CONFLICT")
    strip_idx = {u["index"] for u in parsed.unmanaged}
    strip_idx |= _attached_legacy_headers(parsed.lines, strip_idx)
    block = render_managed_block(configs, app_dir)

    out: list[str] = []
    if parsed.has_block:
        for i, line in enumerate(parsed.lines):
            if i == parsed.block_start:
                out.extend(block)
            elif parsed.block_start < i <= parsed.block_end or i in strip_idx:  # type: ignore[operator]
                continue
            else:
                out.append(line)
    else:
        out = [line for i, line in enumerate(parsed.lines) if i not in strip_idx]
        out.extend(block)
    return "\n".join(out) + "\n"


def crontab_diff(before: str, after: str) -> list[str]:
    return list(
        difflib.unified_diff(before.splitlines(), after.splitlines(), "crontab (현재)", "crontab (적용 후)", lineterm="")
    )


# =========================================================================
# Crontab drivers ([VERIFY-02])
# =========================================================================


class CrontabUnavailableError(RuntimeError):
    pass


@runtime_checkable
class CrontabDriver(Protocol):
    def is_available(self) -> bool: ...

    def read(self) -> str: ...

    def write(self, text: str) -> None: ...


class LinuxCrontabDriver:
    """Real OS crontab for the service user. Only ``crontab -l`` / ``crontab -`` (shell=False)."""

    def __init__(self, app_dir: Path | str, timeout: float = 10.0):
        self.app_dir = Path(app_dir).as_posix()
        self.timeout = timeout

    def is_available(self) -> bool:
        return sys.platform.startswith("linux") and shutil.which("crontab") is not None

    def read(self) -> str:
        proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True, shell=False, timeout=self.timeout)
        if proc.returncode != 0:
            if "no crontab" in (proc.stderr or "").lower():
                return ""
            raise CrontabUnavailableError(f"crontab -l 실패: {(proc.stderr or '').strip()}")
        return proc.stdout

    def write(self, text: str) -> None:
        proc = subprocess.run(["crontab", "-"], input=text, capture_output=True, text=True, shell=False, timeout=self.timeout)
        if proc.returncode != 0:
            raise CrontabUnavailableError(f"crontab - 실패: {(proc.stderr or '').strip()}")


class UnavailableCrontabDriver:
    """Windows dev / pytest default: never touches the OS."""

    app_dir: str | None = None

    def is_available(self) -> bool:
        return False

    def read(self) -> str:
        raise CrontabUnavailableError("이 환경에서는 OS crontab을 사용할 수 없습니다.")

    def write(self, text: str) -> None:
        raise CrontabUnavailableError("이 환경에서는 OS crontab을 사용할 수 없습니다.")


class InMemoryCrontabDriver:
    """Test double with optional fault injection."""

    def __init__(self, text: str = "", app_dir: str = "/home/ubuntu/kwDailyMarketReport"):
        self.text = text
        self.app_dir = app_dir
        self.writes: list[str] = []
        self.fail_write = False
        self.fail_write_after: int | None = None  # allow N writes, then fail
        self.corrupt_writes = 0  # corrupt the next N stored writes (simulates SYNC_VERIFY mismatch)

    def is_available(self) -> bool:
        return True

    def read(self) -> str:
        return self.text

    def write(self, text: str) -> None:
        if self.fail_write or (self.fail_write_after is not None and len(self.writes) >= self.fail_write_after):
            raise CrontabUnavailableError("injected write failure")
        self.writes.append(text)
        if self.corrupt_writes > 0:
            self.corrupt_writes -= 1
            self.text = text.replace(MANAGED_END, "# injected drift\n" + MANAGED_END) + "# injected drift\n"
        else:
            self.text = text


# =========================================================================
# Schedule Manager (journaled sync)
# =========================================================================


STATUS_HTTP = {
    "APPLIED": 200,
    "VALIDATION_FAILED": 422,
    "READ_ONLY": 400,
    "UNKNOWN_TASK": 404,
    "MIGRATION_REQUIRED": 409,
    "MIGRATION_CONFLICT": 409,
    "DIVERGED": 409,
    "CRONTAB_UNAVAILABLE": 503,
    "SYNC_FAILED": 503,
    "DB_COMMIT_FAILED": 500,
}


class ScheduleManager:
    def __init__(self, db: Any, driver: Any, app_dir: str, now_fn=None):
        _assert_safe_app_dir(app_dir)
        self.db = db
        self.driver = driver
        self.app_dir = app_dir.rstrip("/")
        self._now_fn = now_fn or (lambda: datetime.now(KST))
        self._last_snapshot: str | None = None  # cached for pure (subprocess-free) preview

    # ---------------------------------------------------------------- helpers
    def _canonical(self) -> dict[str, dict[str, Any]]:
        raw = self.db.get_setting(SETTING_KEY)
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        tasks = data.get("tasks", {}) if isinstance(data, dict) else {}
        return {k: v for k, v in tasks.items() if k in TASK_CATALOG and isinstance(v, dict)}

    def _read_crontab(self) -> tuple[str | None, str]:
        if not self.driver.is_available():
            return None, "UNAVAILABLE"
        try:
            text = self.driver.read()
        except Exception:
            return None, "READ_FAILED"
        self._last_snapshot = text
        return text, "OK"

    def _effective_configs(self, parsed: ParsedCrontab | None) -> dict[str, dict[str, Any]]:
        """Precedence: live unmanaged lines > DB canonical > managed-block import > default (OFF)."""
        canonical = self._canonical()
        imported = parsed.imported_configs() if parsed and parsed.state != "MIGRATION_CONFLICT" else {}
        unmanaged_tasks = {u["task_id"] for u in parsed.unmanaged} if parsed else set()
        out: dict[str, dict[str, Any]] = {}
        for task_id, spec in TASK_CATALOG.items():
            if task_id in unmanaged_tasks:
                out[task_id] = {**imported[task_id], "source": "CRONTAB_UNMANAGED"}
            elif task_id in canonical:
                out[task_id] = {**canonical[task_id], "source": "DB"}
            elif task_id in imported:
                out[task_id] = {**imported[task_id], "source": "CRONTAB_MANAGED"}
            else:
                out[task_id] = {**default_config(spec, enabled=False), "source": "CATALOG_DEFAULT"}
        return out

    @staticmethod
    def _strip_source(configs: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        return {k: {kk: vv for kk, vv in v.items() if kk != "source"} for k, v in configs.items()}

    def _journal_public(self) -> dict[str, Any] | None:
        row = self.db.get_latest_schedule_journal()
        if not row:
            return None
        return {k: v for k, v in row.items() if k != "snapshot_crontab"}

    def _diverged_unresolved(self, parsed: ParsedCrontab | None) -> bool:
        row = self.db.get_latest_schedule_journal()
        if not row or row.get("status") != "DIVERGED":
            return False
        if parsed is None or parsed.state == "MIGRATION_CONFLICT":
            return True
        # Resolved only when the live managed block equals what the DB canonical would render.
        canonical = self._canonical()
        if not canonical:
            return True
        expected = "\n".join(render_managed_block(canonical, self.app_dir))
        return parsed.managed_block_text() != expected

    def _task_view(self, task_id: str, cfg: dict[str, Any], live: dict[str, dict[str, Any]], now: datetime) -> dict[str, Any]:
        spec = TASK_CATALOG[task_id]
        exprs = compile_cron_exprs(cfg)
        live_cfg = live.get(task_id)
        live_exprs = compile_cron_exprs(live_cfg) if live_cfg else []
        default_exprs = compile_cron_exprs(default_config(spec, enabled=True))
        view = {
            **spec.to_public(),
            "config": {k: v for k, v in cfg.items() if k != "source"},
            "source": cfg.get("source"),
            "cron_exprs": exprs,
            "cron_lines": compile_cron_lines(task_id, cfg, self.app_dir) if exprs else [],
            "next_runs_kst": next_runs_kst(exprs, now, 3) if cfg.get("enabled") else [],
            "live_cron_exprs": live_exprs,
            "live_enabled": bool(live_cfg and live_cfg.get("enabled")),
            "policy_warnings": policy_warnings_for(task_id, cfg),
        }
        if not spec.editable:
            view["internal_scheduler"] = "ACTIVE"
            view["os_cron"] = "FALLBACK_COMPATIBILITY"
            view["mismatch"] = bool(live_exprs) and live_exprs != default_exprs
        return view

    # ---------------------------------------------------------------- GET
    def get_overview(self) -> dict[str, Any]:
        now = self._now_fn()
        text, crontab_status = self._read_crontab()
        parsed = parse_crontab(text) if text is not None else None
        configs = self._effective_configs(parsed)
        live = parsed.imported_configs() if parsed and parsed.state != "MIGRATION_CONFLICT" else {}

        if parsed is None:
            migration_state = "UNKNOWN"
        else:
            migration_state = parsed.state
        sync_state = "UNKNOWN"
        if parsed is not None and parsed.state in ("MANAGED", "READY"):
            canonical = self._canonical()
            if not canonical:
                sync_state = "NOT_APPLIED"
            else:
                expected = "\n".join(render_managed_block(canonical, self.app_dir))
                sync_state = "IN_SYNC" if parsed.managed_block_text() == expected else "DRIFT"

        protected = parsed.protected_lines() if parsed and parsed.state != "MIGRATION_CONFLICT" else []
        policy: list[str] = []
        for task_id, cfg in configs.items():
            policy.extend(policy_warnings_for(task_id, cfg))

        migration_preview: list[str] = []
        if parsed is not None and parsed.state == "MIGRATION_REQUIRED":
            new_text = render_crontab(parsed, self._strip_source(configs), self.app_dir)
            migration_preview = crontab_diff(parsed.text, new_text)

        tasks = [
            self._task_view(task_id, configs[task_id], live, now)
            for task_id in sorted(TASK_CATALOG, key=lambda t: TASK_CATALOG[t].priority)
        ]
        return {
            "driver_available": crontab_status == "OK",
            "crontab_status": crontab_status,
            "migration_state": migration_state,
            "conflict_reason": parsed.conflict_reason if parsed else "",
            "sync_state": sync_state,
            "diverged": self._diverged_unresolved(parsed),
            "db_backup_protected": any(p["is_db_backup"] for p in protected),
            "protected_lines": protected,
            "unmanaged_task_lines": [
                {"task_id": u["task_id"], "expr": u["expr"], "line": u["line"]} for u in (parsed.unmanaged if parsed else [])
            ],
            "migration_preview_diff": migration_preview,
            "policy_warnings": policy,
            "journal": self._journal_public(),
            "now_kst": now.strftime("%Y-%m-%d %H:%M KST"),
            "tasks": tasks,
        }

    # ---------------------------------------------------------------- preview
    def preview(self, task_id: str, payload: dict[str, Any], count: int = 3) -> dict[str, Any]:
        """Pure computation: no subprocess, no DB write. Uses the cached crontab snapshot."""
        spec = TASK_CATALOG.get(task_id)
        if spec is None:
            return {"status": "UNKNOWN_TASK", "errors": [f"허용되지 않은 작업입니다: {task_id}"]}
        if not spec.editable:
            return {"status": "READ_ONLY", "errors": ["시황봇 작업은 읽기 전용(내부 데몬 + OS cron 이중 경로)입니다."]}
        count = max(1, min(int(count or 3), 10))
        cfg, errors, warnings = normalize_payload(task_id, payload)
        result: dict[str, Any] = {
            "status": "OK" if not errors else "VALIDATION_FAILED",
            "task_id": task_id,
            "config": cfg,
            "errors": errors,
            "warnings": warnings,
            "cron_exprs": [],
            "cron_lines": [],
            "next_runs_kst": [],
            "diff": [],
            "diff_available": False,
            "protected_lines": [],
        }
        if errors:
            return result
        exprs = compile_cron_exprs(cfg)
        result["cron_exprs"] = exprs
        result["cron_lines"] = compile_cron_lines(task_id, cfg, self.app_dir)
        result["next_runs_kst"] = next_runs_kst(exprs, self._now_fn(), count) if cfg["enabled"] else []
        if self._last_snapshot is not None:
            parsed = parse_crontab(self._last_snapshot)
            result["migration_state"] = parsed.state
            if parsed.state != "MIGRATION_CONFLICT":
                configs = self._strip_source(self._effective_configs(parsed))
                configs[task_id] = cfg
                result["diff"] = crontab_diff(parsed.text, render_crontab(parsed, configs, self.app_dir))
                result["diff_available"] = True
                result["protected_lines"] = parsed.protected_lines()
        return result

    # ---------------------------------------------------------------- PUT
    def apply(
        self,
        task_id: str,
        payload: dict[str, Any],
        confirm_migration: bool = False,
        updated_by: str = "ADMIN_WEB",
    ) -> dict[str, Any]:
        spec = TASK_CATALOG.get(task_id)
        if spec is None:
            return {"status": "UNKNOWN_TASK", "errors": [f"허용되지 않은 작업입니다: {task_id}"]}
        if not spec.editable:
            return {"status": "READ_ONLY", "errors": ["시황봇 작업은 이 화면에서 변경할 수 없습니다 (READ_ONLY_DUAL_PATH)."]}
        cfg, errors, warnings = normalize_payload(task_id, payload)
        if errors:
            return {"status": "VALIDATION_FAILED", "errors": errors, "warnings": warnings}

        # 1) Preconditions
        if not self.driver.is_available():
            return {"status": "CRONTAB_UNAVAILABLE", "errors": ["OS crontab을 사용할 수 없는 환경입니다 (저장되지 않음)."]}
        try:
            snapshot = self.driver.read()  # 2) snapshot
        except Exception as exc:
            return {"status": "CRONTAB_UNAVAILABLE", "errors": [f"crontab 조회 실패: {exc}"]}
        self._last_snapshot = snapshot
        parsed = parse_crontab(snapshot)
        if parsed.state == "MIGRATION_CONFLICT":
            return {"status": "MIGRATION_CONFLICT", "errors": [parsed.conflict_reason or "관리 마커 손상"]}
        if self._diverged_unresolved(parsed):
            return {
                "status": "DIVERGED",
                "errors": ["이전 동기화가 DIVERGED 상태입니다. crontab과 DB를 수동 점검한 뒤 다시 시도하세요."],
            }

        configs = self._strip_source(self._effective_configs(parsed))
        configs[task_id] = cfg
        new_text = render_crontab(parsed, configs, self.app_dir)
        diff = crontab_diff(snapshot, new_text)
        if parsed.state == "MIGRATION_REQUIRED" and not confirm_migration:
            return {
                "status": "MIGRATION_REQUIRED",
                "errors": ["기존 unmanaged cron 라인을 관리 블록으로 편입해야 합니다. 변경 내용을 확인 후 편입을 승인하세요."],
                "unmanaged_task_lines": [u["line"] for u in parsed.unmanaged],
                "diff": diff,
            }

        target_parsed = parse_crontab(new_text)
        target_hash = target_parsed.managed_block_hash()
        # 3) Journal PENDING
        journal_id = self.db.record_schedule_pending(
            task_id=task_id,
            payload_json=json.dumps({"task_id": task_id, "config": cfg, "all": configs}, ensure_ascii=False),
            snapshot_crontab=snapshot,
            snapshot_hash=_sha256(snapshot),
            target_hash=target_hash,
        )

        # 4) Write + SYNC_VERIFY
        try:
            self.driver.write(new_text)
        except Exception as exc:
            self.db.finalize_schedule_journal(journal_id, "SYNC_FAILED", error_detail=f"write: {exc}")
            return {"status": "SYNC_FAILED", "errors": [f"crontab 적용 실패: {exc}"], "journal_id": journal_id}
        verify_err = ""
        try:
            reread = self.driver.read()
            if parse_crontab(reread).managed_block_hash() != target_hash:
                verify_err = "관리 블록 SHA-256 불일치"
        except Exception as exc:
            verify_err = f"재조회 실패: {exc}"
        if verify_err:
            restored = self._restore(snapshot)
            if not restored:
                self.db.finalize_schedule_journal(journal_id, "DIVERGED", error_detail=f"verify: {verify_err}; rollback failed")
                return {"status": "DIVERGED", "errors": [f"검증 실패 후 복구도 실패했습니다: {verify_err}"], "journal_id": journal_id}
            self.db.finalize_schedule_journal(journal_id, "SYNC_FAILED", error_detail=f"verify: {verify_err}; restored")
            return {"status": "SYNC_FAILED", "errors": [f"crontab 검증 실패 ({verify_err}), 이전 crontab으로 복구했습니다."], "journal_id": journal_id}

        # 5) Canonical commit (settings + history + journal APPLIED in one transaction)
        canonical_value = json.dumps({"version": 1, "tasks": configs}, ensure_ascii=False, sort_keys=True)
        try:
            self.db.finalize_schedule_journal(
                journal_id,
                "APPLIED",
                canonical_value=canonical_value,
                updated_by=updated_by,
                reason=f"schedule:{task_id}" + (" (migration)" if parsed.state == "MIGRATION_REQUIRED" else ""),
            )
        except Exception as exc:
            if self._restore(snapshot):
                self._safe_mark(journal_id, "ROLLED_BACK", f"db: {exc}")
                return {"status": "DB_COMMIT_FAILED", "errors": [f"DB 저장 실패로 crontab을 원복했습니다: {exc}"], "journal_id": journal_id}
            self._safe_mark(journal_id, "DIVERGED", f"db: {exc}; rollback failed")
            return {"status": "DIVERGED", "errors": [f"DB 저장 실패 및 crontab 원복 실패: {exc}"], "journal_id": journal_id}

        self._last_snapshot = new_text
        exprs = compile_cron_exprs(cfg)
        return {
            "status": "APPLIED",
            "task_id": task_id,
            "config": cfg,
            "warnings": warnings,
            "migrated": parsed.state == "MIGRATION_REQUIRED",
            "cron_lines": compile_cron_lines(task_id, cfg, self.app_dir),
            "next_runs_kst": next_runs_kst(exprs, self._now_fn(), 3) if cfg["enabled"] else [],
            "diff": diff,
            "journal_id": journal_id,
            "executed_now": False,  # [PROD-LIVE] saving never runs the task
        }

    def _restore(self, snapshot: str) -> bool:
        try:
            self.driver.write(snapshot)
            return self.driver.read().splitlines() == snapshot.splitlines()
        except Exception:
            return False

    def _safe_mark(self, journal_id: int, status: str, detail: str) -> None:
        try:
            self.db.finalize_schedule_journal(journal_id, status, error_detail=detail)
        except Exception:
            pass
