"""Phase 2/3 contract tests for the additive admission profile and guide catalog."""
import json
from datetime import date, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from admission_brief.catalog import CatalogValidationError, KnowledgeCatalog
from admission_brief.interactive_bot import AdmissionInteractiveBot, cb
from admission_brief.models import (
    ChildProfile,
    EducationStage,
    GradeLevel,
    GradeMode,
    ParentUserProfile,
    TargetGrade,
    TargetRegion,
)
from admission_brief.channels.telegram import TelegramChannel
from admission_brief.storage.db import AdmissionDatabase


def _callback(data: str) -> dict:
    return {
        "id": "phase23-cb",
        "data": data,
        "from": {"id": 123},
        "message": {"message_id": 44, "chat": {"id": 123}},
    }


def test_legacy_mapping_never_guesses_a_specific_grade():
    middle = ChildProfile.from_dict({"alias": "첫째", "grade": "MIDDLE", "region": "ALL"})
    high3 = ChildProfile.from_dict({"alias": "둘째", "grade": "HIGH_3_N", "region": "ALL"})
    assert (middle.education_stage, middle.grade_level) == (EducationStage.MIDDLE, GradeLevel.LEGACY)
    assert (high3.education_stage, high3.grade_level) == (EducationStage.HIGH, GradeLevel.LEGACY)
    assert ChildProfile.from_dict(middle.to_dict()).grade_level == GradeLevel.LEGACY


def test_auto_promotion_uses_march_1_kst_and_is_idempotent():
    child = ChildProfile(
        education_stage=EducationStage.ELEMENTARY,
        grade_level=GradeLevel.E1,
        grade_mode=GradeMode.AUTO,
        school_entry_year=2026,
    )
    assert child.promote_if_due(datetime(2027, 2, 28, 23, 59)) is False
    assert child.promote_if_due(datetime(2027, 3, 1, 0, 1)) is True
    assert (child.education_stage, child.grade_level, child.last_promoted_academic_year) == (
        EducationStage.ELEMENTARY, GradeLevel.E2, 2027
    )
    assert child.promote_if_due(datetime(2027, 12, 31, 23, 0)) is False


def test_callback_validator_rejects_unicode_and_overflow():
    assert cb("adm:guide:guide-elementary-e12") == "adm:guide:guide-elementary-e12"
    assert len(cb("a" * 64).encode("utf-8")) == 64
    with pytest.raises(ValueError):
        cb("가이드")
    with pytest.raises(ValueError):
        cb("a" * 65)


def test_catalog_filters_expiry_season_stage_and_legacy():
    catalog = KnowledgeCatalog()
    assert catalog.filter(EducationStage.ELEMENTARY, GradeLevel.E1, month=7, today=date(2026, 7, 1), card_type="STUDY_GUIDE")[0].content_id == "guide-elementary-e12"
    assert catalog.filter(EducationStage.HIGH, GradeLevel.H3, month=7, today=date(2026, 7, 1), card_type="WEEKLY_TODO")[0].content_id == "todo-high3-year"
    assert not any(card.content_id == "todo-high3-year" for card in catalog.filter(EducationStage.HIGH, GradeLevel.LEGACY, month=7, today=date(2026, 7, 1), card_type="WEEKLY_TODO"))
    assert catalog.filter(EducationStage.MIDDLE, GradeLevel.LEGACY, month=7, today=date(2029, 1, 1), card_type="WEEKLY_TODO") == ()


def test_catalog_schema_https_and_fail_closed():
    raw = json.loads((Path("admission_brief/fixtures/knowledge_cards.json")).read_text(encoding="utf-8"))
    raw[0]["source_url"] = "http://not-allowed.example"
    path = Path(".phase23-invalid-catalog.json")
    try:
        path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        with pytest.raises(CatalogValidationError):
            KnowledgeCatalog(path)
        safe = KnowledgeCatalog(path, strict=False)
        assert safe.cards == () and not safe.is_valid
    finally:
        path.unlink(missing_ok=True)


def test_db_stage_level_keeps_legacy_columns_and_children_json_in_sync():
    db = AdmissionDatabase(":memory:")
    db.create_subscriber_if_absent(ParentUserProfile(chat_id="123"))
    assert db.update_subscriber_stage_level("123", EducationStage.ELEMENTARY, GradeLevel.E1)
    profile = db.get_subscriber("123")
    assert profile.target_grade == TargetGrade.ALL
    assert (profile.education_stage, profile.grade_level) == (EducationStage.ELEMENTARY, GradeLevel.E1)
    assert db.update_subscriber_grade("123", TargetGrade.MIDDLE)
    profile = db.get_subscriber("123")
    assert (profile.education_stage, profile.grade_level) == (EducationStage.MIDDLE, GradeLevel.LEGACY)


def test_stage_level_and_guide_callbacks_are_routable():
    channel = MagicMock(spec=TelegramChannel)
    channel.edit_message_text.return_value = True
    channel.answer_callback_query.return_value = True
    bot = AdmissionInteractiveBot(channel, AdmissionDatabase(":memory:"))
    bot.handle_update({"callback_query": _callback("adm:stage:ELEMENTARY")})
    level_markup = channel.edit_message_text.call_args.kwargs["reply_markup"]
    assert "adm:level:E1" in [button["callback_data"] for row in level_markup["inline_keyboard"] for button in row]
    channel.reset_mock()
    bot.handle_update({"callback_query": _callback("adm:level:E1")})
    assert bot.db.get_subscriber("123").grade_level == GradeLevel.E1
    channel.reset_mock()
    bot.handle_update({"callback_query": _callback("adm:guide:menu")})
    assert channel.edit_message_text.called

