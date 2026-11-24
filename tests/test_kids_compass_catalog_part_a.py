from __future__ import annotations

import pytest

from kids_compass.collector import seed_all
from kids_compass.food_safety import FOOD_ITEMS, get_food_item, search_foods
from kids_compass.home_play import HOME_PLAY_ITEMS, get_dev_guides, pick_home_plays
from kids_compass.storage.db import (
    KidsCompassDatabase,
    PROJECT_ROOT,
    read_catalog_item_readonly,
    resolve_kids_db_path,
)
from kids_compass.web_guide import render_guide_html, render_not_found_html, validate_catalog_import_payload


def test_resolver_precedence_and_memory(monkeypatch):
    monkeypatch.delenv("KIDS_DB_PATH", raising=False)
    assert resolve_kids_db_path(":memory:") == ":memory:"
    assert resolve_kids_db_path("ignored.sqlite3").endswith("kids_compass.sqlite3")
    monkeypatch.setenv("KIDS_DB_PATH", "env.sqlite3")
    assert resolve_kids_db_path("other.sqlite3") == str((PROJECT_ROOT / "env.sqlite3").resolve())
    monkeypatch.setenv("KIDS_DB_PATH", ":memory:")
    assert resolve_kids_db_path("other.sqlite3") == ":memory:"


def test_catalog_seed_schema_and_db_priority():
    db = KidsCompassDatabase(":memory:")
    counts = seed_all(db)
    assert counts["catalog"] == len(HOME_PLAY_ITEMS) + 8 + len(FOOD_ITEMS)
    columns = {row[1] for row in db.conn.execute("PRAGMA table_info(kids_content_catalog)")}
    assert {"content_id", "domain", "detail_json", "source", "updated_at"} <= columns
    assert db.conn.execute("PRAGMA index_list(kids_content_catalog)").fetchall()
    assert all(len(item.steps) in (3, 4) for item in HOME_PLAY_ITEMS)
    assert len(pick_home_plays("M0_11", db=db, limit=20)) == 5
    assert get_dev_guides("D0_3", db=db)[0].guide_id == "d0_social"

    food = db.get_catalog_item("honey")
    assert food["domain"] == "FOOD"
    assert get_food_item("honey", db=db).name == "꿀"
    assert search_foods("꿀", db=db)[0].food_id == "honey"
    db.close()


def test_seed_never_overwrites_import_and_import_reports_rejections():
    db = KidsCompassDatabase(":memory:")
    row = {
        "content_id": "custom_play",
        "domain": "PLAY",
        "stage_code": "M0_11",
        "title": "원본",
        "summary": "한 줄",
        "detail": {"steps": ["하나", "둘", "셋"]},
    }
    assert db.import_catalog_items([row]) == {"inserted": 1, "updated": 0, "rejected": 0}
    assert db.seed_content_catalog([{**row, "title": "시드 덮어쓰기 금지"}]) == 0
    assert db.get_catalog_item("custom_play")["title"] == "원본"
    assert db.import_catalog_items([{**row, "title": "가져오기 수정"}]) == {"inserted": 0, "updated": 1, "rejected": 0}
    assert db.get_catalog_item("custom_play")["title"] == "가져오기 수정"

    seeded = {"content_id": "honey", "domain": "FOOD", "stage_code": "FORBIDDEN",
              "title": "가짜", "summary": "가짜", "detail": {}}
    assert db.seed_content_catalog([seeded]) == 1
    assert db.import_catalog_items([seeded])["rejected"] == 1
    db.close()


def test_import_validator_rejects_unsafe_or_ambiguous_payloads():
    valid = {"content_id": "safe_item", "domain": "FOOD", "stage_code": "PROTEIN",
             "title": "안전", "summary": "요약", "detail": {"aliases": ["별칭"]},
             "source": "SEED"}
    normalized = validate_catalog_import_payload({"items": [valid]})
    assert normalized[0]["source"] == "IMPORT"
    with pytest.raises(ValueError):
        validate_catalog_import_payload({"items": [valid, valid]})
    with pytest.raises(ValueError):
        validate_catalog_import_payload({"items": [{**valid, "content_id": "bad-id"}]})
    with pytest.raises(ValueError):
        validate_catalog_import_payload({"items": [{**valid, "detail": {"extra": "no"}}]})
    with pytest.raises(ValueError):
        validate_catalog_import_payload({"items": [{**valid, "title": "<script>alert(1)</script>"}]})


def test_web_renderer_escapes_dynamic_content_and_has_mobile_guards():
    html = render_guide_html({
        "title": "<img src=x>", "summary": "a & b", "safety_note": "<script>x</script>",
        "detail": {"materials": ["큰 컵"], "steps": ["첫 단계"], "checkpoints": ["확인"],
                   "risk_level": "SAFE"},
    })
    assert "<img src=x>" not in html and "&lt;img src=x&gt;" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert "overflow-x:hidden" in html and "minmax(0,1fr)" in html and "min-height:44px" in html
    assert "준비물" in html and "단계별 실행법" in html and "체크리스트" in html and "안전 주의사항" in html
    assert "면책 안내" in html and "일반적인 참고 안내" in html
    assert "가이드를 찾을 수 없어요" in render_not_found_html()


def test_readonly_missing_path_does_not_create_file():
    path = PROJECT_ROOT / "__catalog_part_a_missing_readonly__.sqlite3"
    assert not path.exists()
    assert read_catalog_item_readonly(str(path), "missing") is None
    assert not path.exists()
