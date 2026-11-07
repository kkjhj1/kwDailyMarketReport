"""Unit tests for name resolution and admin custom name protection invariants."""
from __future__ import annotations
import pytest
from pathlib import Path

from market_brief.name_resolution import (
    clean_text,
    has_hangul,
    is_placeholder_name,
    resolve_telegram_display_name,
)
from portal_hub.db import PortalDatabase
from portal_hub.models import UserTier
from market_brief.storage.db import MarketBriefDatabase
from market_brief.subscription_engine import SubscriptionEngine


def test_clean_text_and_has_hangul():
    assert clean_text("  홍길동\n ") == "홍길동"
    assert clean_text(None) == ""
    assert has_hangul("홍길동") is True
    assert has_hangul("김") is True
    assert has_hangul("John") is False
    assert has_hangul("1234") is False
    assert has_hangul("") is False


def test_resolve_telegram_display_name_korean():
    name, src = resolve_telegram_display_name(first_name="길동", last_name="홍", username="gildong")
    assert name == "홍길동"
    assert src == "TELEGRAM"


def test_resolve_telegram_display_name_english():
    name, src = resolve_telegram_display_name(first_name="John", last_name="Doe", username="johndoe")
    assert name == "John Doe"
    assert src == "TELEGRAM"


def test_resolve_telegram_display_name_single_names():
    name1, src1 = resolve_telegram_display_name(first_name="철수", last_name=None)
    assert name1 == "철수"
    assert src1 == "TELEGRAM"

    name2, src2 = resolve_telegram_display_name(first_name=None, last_name="이")
    assert name2 == "이"
    assert src2 == "TELEGRAM"


def test_resolve_telegram_display_name_fallback():
    # Username only
    name_u, src_u = resolve_telegram_display_name(None, None, username="@super_user")
    assert name_u == "@super_user"
    assert src_u == "TELEGRAM"

    # Chat ID only
    name_c, src_c = resolve_telegram_display_name(None, None, None, chat_id=123456789)
    assert name_c == "수신자_6789"
    assert src_c == "FALLBACK"

    # Nothing
    name_none, src_none = resolve_telegram_display_name(None, None, None, None)
    assert name_none == "수신자"
    assert src_none == "FALLBACK"


def test_is_placeholder_name():
    assert is_placeholder_name("수신자_1234") is True
    assert is_placeholder_name("유저_5678") is True
    assert is_placeholder_name("_5792") is True
    assert is_placeholder_name("\ufffd_5792") is True
    assert is_placeholder_name("") is True
    assert is_placeholder_name("홍길동") is False
    assert is_placeholder_name("김철수") is False
    assert is_placeholder_name("@myuser") is False
    assert is_placeholder_name("John Doe") is False


def test_portal_db_get_or_create_user_with_korean_name(tmp_path: Path):
    db_file = tmp_path / "test_alerts.sqlite3"
    db = PortalDatabase(db_file)

    user = db.get_or_create_user(
        user_id=999888,
        chat_id=999888,
        username="kildong",
        first_name="길동",
        last_name="홍",
    )
    assert user.first_name == "홍길동"
    assert user.full_name == "홍길동"
    assert user.is_name_custom == 0
    assert user.name_source == "TELEGRAM"

    # Healing existing placeholder
    user2 = db.get_or_create_user(
        user_id=111222,
        chat_id=111222,
        username="",
        first_name="",
        last_name="",
    )
    assert user2.full_name.startswith("수신자_")

    # Call again with real name -> auto heal
    user2_healed = db.get_or_create_user(
        user_id=111222,
        chat_id=111222,
        username="chulsoo",
        first_name="철수",
        last_name="김",
    )
    assert user2_healed.full_name == "김철수"
    assert user2_healed.name_source == "TELEGRAM"


def test_custom_name_protection_in_subscription_engine(tmp_path: Path):
    db_file = tmp_path / "test_sub.sqlite3"
    # Bootstrap schema
    PortalDatabase(db_file)
    mb_db = MarketBriefDatabase(db_path=str(db_file))
    engine = SubscriptionEngine(db=mb_db)

    # 1. Create a user
    user = engine.upsert_user(
        telegram_user_id="777001",
        full_name="이순신",
        username="sunshin",
        tier="GREEN",
    )
    assert user["full_name"] == "이순신"

    # 2. Admin manually modifies the name
    updated = engine.update_user_details(
        telegram_user_id="777001",
        full_name="충무공 이순신 장군님",
    )
    assert updated["full_name"] == "충무공 이순신 장군님"

    # Verify is_name_custom is 1
    users = engine.list_users_with_subscriptions(q="777001")
    assert len(users) == 1
    assert users[0]["is_name_custom"] == 1
    assert users[0]["name_source"] == "MANUAL"
