from __future__ import annotations

import pytest
from fastapi import HTTPException

from admin_server import _require_strict_admin_token
from kids_compass.collector import seed_all
from kids_compass.interactive_bot import (
    build_dev_guide_keyboard,
    build_food_detail_keyboard,
    build_home_play_keyboard,
    get_kids_guide_url,
)
from kids_compass.storage.db import KidsCompassDatabase
from market_brief.admin_sync_registry import ADMIN_SYNC_REGISTRY
from market_brief.admin_ui import ADMIN_DASHBOARD_HTML


class _Request:
    def __init__(self, headers: dict[str, str] | None = None):
        self.headers = headers or {}


def _url_buttons(keyboard: dict) -> list[dict]:
    return [button for row in keyboard["inline_keyboard"] for button in row if button.get("url")]


def test_strict_admin_auth_is_header_only_and_fail_closed(monkeypatch):
    monkeypatch.delenv("ADMIN_API_TOKEN", raising=False)
    with pytest.raises(HTTPException) as missing:
        _require_strict_admin_token(_Request({"X-Admin-Token": "anything"}))
    assert missing.value.status_code == 503

    monkeypatch.setenv("ADMIN_API_TOKEN", "secret")
    with pytest.raises(HTTPException) as query_only:
        _require_strict_admin_token(_Request({}))
    assert query_only.value.status_code == 401
    _require_strict_admin_token(_Request({"X-Admin-Token": "secret"}))


def test_guide_url_rejects_private_or_invalid_bases(monkeypatch):
    monkeypatch.setenv("KIDS_WEB_BASE_URL", "http://localhost:8002")
    assert get_kids_guide_url("play_1") is None
    monkeypatch.setenv("KIDS_WEB_BASE_URL", "http://192.168.1.4:8002")
    assert get_kids_guide_url("play_1") is None
    monkeypatch.setenv("KIDS_WEB_BASE_URL", "ftp://public.example")
    assert get_kids_guide_url("play_1") is None
    monkeypatch.setenv("KIDS_WEB_BASE_URL", "https://kids.example/base")
    assert get_kids_guide_url("play_1") == "https://kids.example/base/kids/guide/play_1"
    assert get_kids_guide_url("BAD-ID") is None


def test_catalog_keyboards_add_url_buttons_only_for_valid_content(monkeypatch):
    monkeypatch.setenv("KIDS_WEB_BASE_URL", "https://kids.example")
    db = KidsCompassDatabase(":memory:")
    seed_all(db)
    try:
        assert _url_buttons(build_home_play_keyboard("M0_11", db=db))
        assert _url_buttons(build_dev_guide_keyboard("D0_3", db=db))
        assert _url_buttons(build_food_detail_keyboard("honey"))
    finally:
        db.close()

    monkeypatch.setenv("KIDS_WEB_BASE_URL", "http://127.0.0.1:8002")
    assert not _url_buttons(build_food_detail_keyboard("honey"))


def test_admin_sync_and_ui_catalog_contract():
    entry = ADMIN_SYNC_REGISTRY["kids_content_catalog"]
    assert entry["admin_api"] == ["GET /api/kids/catalog", "POST /api/kids/catalog/import"]
    assert entry["db_reader"] == "KidsCompassDatabase.list_catalog_items"
    assert "kidsCatalogList" in ADMIN_DASHBOARD_HTML
    assert "kids/catalog/import" in ADMIN_DASHBOARD_HTML
