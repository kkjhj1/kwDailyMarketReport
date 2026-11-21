"""Read-only, validated KnowledgeCard catalog for the admission guide.

The JSON file is deliberately content-only.  Runtime matching and expiry checks
live here so bot handlers never need to embed educational copy or policy facts.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

from admission_brief.models import EducationStage, GradeLevel

KST = ZoneInfo("Asia/Seoul")
_SLUG = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_CARD_TYPES = {"STUDY_GUIDE", "WEEKLY_TODO", "POLICY", "FAQ"}
_STATUSES = {"PUBLISHED", "DRAFT"}
_FIELDS = {
    "content_id", "card_type", "target_stages", "grade_levels", "season_months",
    "topic", "headline", "summary_3lines", "so_what", "action_todos",
    "source_name", "source_url", "reviewed_at", "valid_until", "status",
}


class CatalogValidationError(ValueError):
    """Raised when a card or catalog violates the content contract."""


def _as_date(value: Any, field: str) -> date:
    if not isinstance(value, str):
        raise CatalogValidationError(f"{field} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise CatalogValidationError(f"{field} must be YYYY-MM-DD") from exc


def _string_list(value: Any, field: str, *, exact_length: Optional[int] = None) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
        raise CatalogValidationError(f"{field} must be a non-empty string list")
    if exact_length is not None and len(value) != exact_length:
        raise CatalogValidationError(f"{field} must contain exactly {exact_length} items")
    return tuple(item.strip() for item in value)


@dataclass(frozen=True)
class KnowledgeCard:
    content_id: str
    card_type: str
    target_stages: tuple[EducationStage, ...]
    grade_levels: tuple[GradeLevel, ...]
    season_months: tuple[int, ...]
    topic: str
    headline: str
    summary_3lines: tuple[str, str, str]
    so_what: str
    action_todos: tuple[str, ...]
    source_name: str
    source_url: str
    reviewed_at: date
    valid_until: date
    status: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "KnowledgeCard":
        if not isinstance(raw, dict) or set(raw) != _FIELDS:
            missing = sorted(_FIELDS - set(raw or {})) if isinstance(raw, dict) else sorted(_FIELDS)
            extra = sorted(set(raw or {}) - _FIELDS) if isinstance(raw, dict) else []
            raise CatalogValidationError(f"card schema mismatch; missing={missing}, extra={extra}")
        content_id = raw["content_id"]
        if not isinstance(content_id, str) or not content_id.isascii() or len(content_id) > 24 or not _SLUG.fullmatch(content_id):
            raise CatalogValidationError("content_id must be an ASCII slug of at most 24 characters")
        card_type = raw["card_type"]
        if card_type not in _CARD_TYPES:
            raise CatalogValidationError(f"invalid card_type: {card_type!r}")
        try:
            stages = tuple(EducationStage(item) for item in _string_list(raw["target_stages"], "target_stages"))
            levels = tuple(GradeLevel(item) for item in _string_list(raw["grade_levels"], "grade_levels"))
        except ValueError as exc:
            raise CatalogValidationError(f"invalid stage or grade level: {exc}") from exc
        months = raw["season_months"]
        if not isinstance(months, list) or any(type(item) is not int or not 1 <= item <= 12 for item in months):
            raise CatalogValidationError("season_months must contain integers from 1 to 12")
        if len(set(months)) != len(months):
            raise CatalogValidationError("season_months must not contain duplicates")
        topic = raw["topic"]
        if not isinstance(topic, str) or not topic.strip():
            raise CatalogValidationError("topic must be a non-empty string")
        headline = raw["headline"]
        if not isinstance(headline, str) or not headline.strip():
            raise CatalogValidationError("headline must be a non-empty string")
        summary = _string_list(raw["summary_3lines"], "summary_3lines", exact_length=3)
        so_what = raw["so_what"]
        if not isinstance(so_what, str) or not so_what.strip():
            raise CatalogValidationError("so_what must be a non-empty string")
        todos = _string_list(raw["action_todos"], "action_todos")
        source_name = raw["source_name"]
        if not isinstance(source_name, str) or not source_name.strip():
            raise CatalogValidationError("source_name must be a non-empty string")
        source_url = raw["source_url"]
        if not isinstance(source_url, str) or not source_url.startswith("https://"):
            raise CatalogValidationError("source_url must use https://")
        reviewed_at = _as_date(raw["reviewed_at"], "reviewed_at")
        valid_until = _as_date(raw["valid_until"], "valid_until")
        if valid_until < reviewed_at:
            raise CatalogValidationError("valid_until must not precede reviewed_at")
        status = raw["status"]
        if status not in _STATUSES:
            raise CatalogValidationError(f"invalid status: {status!r}")
        return cls(
            content_id=content_id, card_type=card_type, target_stages=stages,
            grade_levels=levels, season_months=tuple(months), topic=topic.strip(),
            headline=headline.strip(), summary_3lines=summary, so_what=so_what.strip(),
            action_todos=todos, source_name=source_name.strip(), source_url=source_url,
            reviewed_at=reviewed_at, valid_until=valid_until, status=status,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "content_id": self.content_id,
            "card_type": self.card_type,
            "target_stages": [item.value for item in self.target_stages],
            "grade_levels": [item.value for item in self.grade_levels],
            "season_months": list(self.season_months),
            "topic": self.topic,
            "headline": self.headline,
            "summary_3lines": list(self.summary_3lines),
            "so_what": self.so_what,
            "action_todos": list(self.action_todos),
            "source_name": self.source_name,
            "source_url": self.source_url,
            "reviewed_at": self.reviewed_at.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "status": self.status,
        }


class KnowledgeCatalog:
    """Immutable in-memory view over a validated JSON catalog."""

    def __init__(self, path: str | Path | None = None, *, strict: bool = True):
        self.path = Path(path) if path is not None else Path(__file__).with_name("fixtures") / "knowledge_cards.json"
        self.load_error: Optional[str] = None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, list):
                raise CatalogValidationError("catalog root must be a list")
            cards = tuple(KnowledgeCard.from_dict(item) for item in payload)
            ids = [card.content_id for card in cards]
            if len(ids) != len(set(ids)):
                raise CatalogValidationError("content_id values must be unique")
            self._cards = cards
            self.is_valid = True
        except (OSError, json.JSONDecodeError, CatalogValidationError, TypeError) as exc:
            self._cards = ()
            self.is_valid = False
            self.load_error = str(exc)
            if strict:
                if isinstance(exc, CatalogValidationError):
                    raise
                raise CatalogValidationError(f"could not load catalog: {exc}") from exc

    @classmethod
    def load(cls, path: str | Path | None = None, *, strict: bool = True) -> "KnowledgeCatalog":
        """Named loader for callers that prefer an explicit read operation."""
        return cls(path, strict=strict)

    @property
    def cards(self) -> tuple[KnowledgeCard, ...]:
        return self._cards

    @staticmethod
    def _today_kst() -> date:
        return datetime.now(KST).date()

    @staticmethod
    def _matches(card: KnowledgeCard, stage: EducationStage, level: GradeLevel) -> bool:
        stage_match = EducationStage.ALL in card.target_stages or stage == EducationStage.ALL and EducationStage.ALL in card.target_stages or stage in card.target_stages
        if not stage_match:
            return False
        card_levels = set(card.grade_levels)
        if level == GradeLevel.LEGACY:
            # Unknown legacy levels only see stage-wide cards, never a guessed E/M/H grade.
            return bool(card_levels & {GradeLevel.LEGACY, GradeLevel.ALL})
        if level == GradeLevel.ALL:
            return GradeLevel.ALL in card_levels
        return bool(card_levels & {level, GradeLevel.LEGACY, GradeLevel.ALL})

    def filter(
        self,
        stage: EducationStage,
        level: GradeLevel,
        *,
        month: Optional[int] = None,
        today: Optional[date] = None,
        card_type: Optional[str] = None,
    ) -> tuple[KnowledgeCard, ...]:
        """Returns published, live cards matching stage/level and season."""
        try:
            stage = stage if isinstance(stage, EducationStage) else EducationStage(stage)
            level = level if isinstance(level, GradeLevel) else GradeLevel(level)
        except (TypeError, ValueError):
            return ()
        current_month = month if month is not None else (today or self._today_kst()).month
        if current_month < 1 or current_month > 12:
            return ()
        as_of = today or self._today_kst()
        return tuple(
            card for card in self._cards
            if card.status == "PUBLISHED" and card.valid_until >= as_of
            and (not card.season_months or current_month in card.season_months)
            and (card_type is None or card.card_type == card_type)
            and self._matches(card, stage, level)
        )

    # Explicit aliases keep the catalog convenient for handlers and tests.
    matching_cards = filter
    get_matching_cards = filter

    def get(self, content_id: str) -> Optional[KnowledgeCard]:
        for card in self._cards:
            if card.content_id == content_id:
                return card
        return None

    def for_child(self, child: Any, *, month: Optional[int] = None, today: Optional[date] = None, card_type: Optional[str] = None) -> tuple[KnowledgeCard, ...]:
        return self.filter(child.education_stage, child.grade_level, month=month, today=today, card_type=card_type)

    def for_profile(self, profile: Any, *, month: Optional[int] = None, today: Optional[date] = None, card_type: Optional[str] = None) -> tuple[KnowledgeCard, ...]:
        return self.for_child(profile.get_active_child(), month=month, today=today, card_type=card_type)

