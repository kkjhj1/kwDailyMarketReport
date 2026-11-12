"""auto_factory bot presets: a fixed, code-reviewed registry of product-bot work scopes.

`/do <alias> <request>` lets the admin target a product bot WITHOUT typing paths. Every preset
pins an exact list of individual `.py` files (no dirs, no globs, no DB/env/runner files) plus the
pytest suites that gate it. Presets are opt-in: only aliases listed in
`AUTO_FACTORY_UNLOCKED_PRESETS` are usable; everything else stays locked (fail closed).

The worker re-validates the binding at preflight (`PRESET_POLICY_VERSION`, exact paths, exact
tests, risk level), so a tampered DB row can never widen a preset's scope.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from types import MappingProxyType
from typing import Iterable, Mapping

from .guardrails import is_forbidden

PRESET_POLICY_VERSION = "v1"
# Hyphen is NOT allowed: `-` pads callback tokens to 22 ASCII chars.
PRESET_ALIAS_RE = re.compile(r"^[a-z0-9_]{1,22}$")
CALLBACK_TOKEN_LEN = 22
_PRESET_PATH_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./]*\.py$")
_BANNED_SUFFIXES = (".sqlite", ".sqlite3", ".db", ".db-wal", ".db-shm")


def _check_preset_path(path: str) -> None:
    if not isinstance(path, str) or not path:
        raise ValueError("preset path must be a non-empty str")
    if path.endswith("/"):
        raise ValueError(f"preset path may not be a directory: {path}")
    if any(ch in path for ch in "*?[]"):
        raise ValueError(f"preset path may not contain wildcards: {path}")
    if not _PRESET_PATH_RE.match(path):
        raise ValueError(f"preset path must be an individual ASCII .py file: {path}")
    segments = path.split("/")
    if any(seg in ("", ".", "..") or seg.startswith(".") or seg.startswith("-") for seg in segments):
        raise ValueError(f"preset path has an unsafe segment: {path}")
    name = segments[-1].lower()
    if name.startswith(".env") or ".env." in path.lower() or path.lower().endswith(_BANNED_SUFFIXES):
        raise ValueError(f"preset path targets env/DB files: {path}")
    if name.endswith("bot_runner.py") or name == "admin_server.py":
        raise ValueError(f"preset path targets a runner/entrypoint: {path}")
    if is_forbidden(path) or is_forbidden(path + "/"):
        raise ValueError(f"preset path is forbidden: {path}")


@dataclass(frozen=True)
class Preset:
    alias: str
    label: str
    description: str
    allowed_paths: tuple[str, ...]
    tests: tuple[str, ...]
    min_risk: str = "L2"
    is_product: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.alias, str) or not PRESET_ALIAS_RE.match(self.alias):
            raise ValueError("preset alias must match ^[a-z0-9_]{1,22}$")
        if self.min_risk != "L2":
            raise ValueError("preset min_risk must be L2")
        if not isinstance(self.allowed_paths, tuple) or not self.allowed_paths:
            raise ValueError("preset allowed_paths must be a non-empty tuple")
        if not isinstance(self.tests, tuple) or not self.tests:
            raise ValueError("preset tests must be a non-empty tuple")
        if len(set(self.allowed_paths)) != len(self.allowed_paths) or len(set(self.tests)) != len(self.tests):
            raise ValueError("preset paths must be unique")
        for p in self.allowed_paths + self.tests:
            _check_preset_path(p)


def _registry(*presets: Preset) -> Mapping[str, Preset]:
    out: dict[str, Preset] = {}
    for p in presets:
        if p.alias in out:
            raise ValueError(f"duplicate preset alias: {p.alias}")
        out[p.alias] = p
    return MappingProxyType(out)


PRESETS: Mapping[str, Preset] = _registry(
    Preset(
        "brief", "📊 시황 브리핑", "시황 브리핑 핵심 로직·스케줄러·웹리포트",
        ("market_brief/core.py", "market_brief/pipeline.py", "market_brief/briefing_service.py",
         "market_brief/briefing_scheduler.py", "market_brief/web_report.py", "market_brief/link_gate.py",
         "tests/test_core.py", "tests/test_briefing_scheduler.py", "tests/test_link_gate.py"),
        ("tests/test_core.py", "tests/test_briefing_scheduler.py", "tests/test_link_gate.py"),
    ),
    Preset(
        "travel", "✈️ 트래블", "트래블 항공·환율·가격워치·인터랙티브",
        ("travel_brief/city_catalog.py", "travel_brief/budget_calculator.py", "travel_brief/flight_engine.py",
         "travel_brief/interactive.py", "travel_brief/models.py", "travel_brief/watch_engine.py",
         "travel_brief/web_report.py", "tests/test_travel_budget.py", "tests/test_travel_flight_engine.py",
         "tests/test_travel_interactive.py", "tests/test_travel_models.py", "tests/test_travel_watch.py",
         "tests/test_travel_web_report.py"),
        ("tests/test_travel_budget.py", "tests/test_travel_flight_engine.py", "tests/test_travel_interactive.py",
         "tests/test_travel_models.py", "tests/test_travel_watch.py", "tests/test_travel_web_report.py"),
    ),
    Preset(
        "admission", "🎓 입시 나침반", "입시 나침반 수집·큐레이션·대화형 봇",
        ("admission_brief/ai_curator.py", "admission_brief/collector.py", "admission_brief/interactive_bot.py",
         "admission_brief/models.py", "tests/test_admission_bot.py", "tests/test_admission_collector.py",
         "tests/test_admission_curator.py", "tests/test_admission_models.py", "tests/test_admission_multichild.py"),
        ("tests/test_admission_bot.py", "tests/test_admission_collector.py", "tests/test_admission_curator.py",
         "tests/test_admission_models.py", "tests/test_admission_multichild.py"),
    ),
    Preset(
        "portal", "🌐 포털 허브", "통합 포털 허브 메뉴·모델·구독연동",
        ("portal_hub/interactive.py", "portal_hub/models.py", "portal_hub/telegram.py",
         "tests/test_portal_hub_integration.py", "tests/test_v20_bot_subscription_platform.py"),
        ("tests/test_portal_hub_integration.py", "tests/test_v20_bot_subscription_platform.py"),
    ),
    Preset(
        "silver", "👵 실버케어", "실버케어 복지 매칭·카드·주간브리핑",
        ("silver_care/channels/telegram_card.py", "silver_care/interactive.py", "silver_care/matching.py",
         "silver_care/models.py", "silver_care/weekly.py", "silver_care/web_report.py", "tests/test_silver_care.py"),
        ("tests/test_silver_care.py",),
    ),
    Preset(
        "kids", "👶 키즈 나침반", "자녀 해열제·소아과·주말체험 봇",
        ("kids_compass/collector.py", "kids_compass/fever_engine.py", "kids_compass/interactive_bot.py",
         "kids_compass/models.py", "tests/test_kids_compass.py"),
        ("tests/test_kids_compass.py",),
    ),
    Preset(
        "yolo", "🏄 욜로 라이프", "1인 라이프 큐레이션·날씨·인터랙티브",
        ("yolo_brief/channels/telegram_card.py", "yolo_brief/interactive.py", "yolo_brief/matching.py",
         "yolo_brief/models.py", "yolo_brief/weather_engine.py", "yolo_brief/web_report.py", "tests/test_yolo_bot.py"),
        ("tests/test_yolo_bot.py",),
    ),
    Preset(
        "scripts", "🧪 품질도구", "테스트 품질 감사 스크립트·테스트",
        ("scripts/audit_test_quality.py", "tests/test_audit_test_quality.py"),
        ("tests/test_audit_test_quality.py",),
        is_product=False,
    ),
)


def parse_unlocked_presets(raw: str | None, registry: Mapping[str, Preset] = PRESETS) -> frozenset[str]:
    """Strict `alias,alias` list. Errors never echo the raw value (only regex-valid aliases)."""
    if raw is None or raw == "":
        return frozenset()
    if not isinstance(raw, str):
        raise ValueError("unlocked presets must be a string")
    if not raw.isascii():
        raise ValueError("unlocked presets must be ASCII")
    if any(ch.isspace() for ch in raw):
        raise ValueError("unlocked presets may not contain whitespace (use a,b)")
    seen: set[str] = set()
    for item in raw.split(","):
        if not item:
            raise ValueError("unlocked presets contain an empty item")
        if not PRESET_ALIAS_RE.match(item):
            raise ValueError("unlocked presets contain an invalid alias")
        if item in seen:
            raise ValueError(f"unlocked presets contain a duplicate alias: {item}")
        if item not in registry:
            raise ValueError(f"unknown preset alias: {item}")
        seen.add(item)
    return frozenset(seen)


def validate_unlocked(names: Iterable[str], registry: Mapping[str, Preset] = PRESETS) -> frozenset[str]:
    """Validate an already-parsed iterable of aliases (DevBot constructor path)."""
    if isinstance(names, str):
        raise ValueError("unlocked presets must be an iterable of aliases, not a string")
    out: set[str] = set()
    for n in names:
        if not isinstance(n, str) or not PRESET_ALIAS_RE.match(n) or n not in registry:
            raise ValueError("unknown or invalid preset alias in unlocked presets")
        out.add(n)
    return frozenset(out)


def encode_preset_token(alias: str) -> str:
    if not isinstance(alias, str) or not PRESET_ALIAS_RE.match(alias):
        raise ValueError("invalid preset alias")
    return alias.ljust(CALLBACK_TOKEN_LEN, "-")


def decode_preset_token(token: str) -> str:
    if not isinstance(token, str) or len(token) != CALLBACK_TOKEN_LEN:
        raise ValueError("invalid preset token length")
    alias = token.rstrip("-")
    if not PRESET_ALIAS_RE.match(alias) or encode_preset_token(alias) != token:
        raise ValueError("invalid preset token")
    return alias
