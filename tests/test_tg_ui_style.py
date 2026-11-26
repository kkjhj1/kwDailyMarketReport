"""Unified Telegram UI style: shared helper, plain <blockquote> contract, /menu command convention,
and absence of long Telegram divider literals across all 8 bots."""
from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

import pytest

from market_brief.channels import tg_ui_style as ui

ROOT = Path(__file__).resolve().parents[1]

BOT_SOURCES = [
    "portal_hub/interactive.py",
    "portal_bot_runner.py",
    "market_brief/multi_bot_manager.py",
    "market_brief/market_watch.py",
    "market_brief/channels/telegram.py",
    "market_brief/link_gate.py",
    "travel_brief/interactive.py",
    "kids_compass/interactive_bot.py",
    "kids_compass/channels/telegram.py",
    "admission_brief/interactive_bot.py",
    "admission_brief/channels/telegram.py",
    "silver_care/interactive.py",
    "silver_care/channels/telegram_card.py",
    "yolo_brief/interactive.py",
    "yolo_brief/channels/telegram_card.py",
    "auto_factory/dev_bot.py",
]


# ── shared helper ─────────────────────────────────────────────
def test_constants():
    assert ui.TG_DIVIDER == "─" * 14 and len(ui.TG_DIVIDER) == 14
    assert ui.TG_BULLET == "•"
    assert (ui.NAV_HOME, ui.NAV_BACK, ui.NAV_PORTAL) == ("🏠 홈", "◀ 이전", "🧭 통합 메뉴봇")
    assert (ui.BADGE_ACTIVE, ui.BADGE_WARN, ui.BADGE_PAUSED, ui.BADGE_OFF, ui.BADGE_MUTED, ui.BADGE_DONE) == (
        "🟢", "🟡", "⏸️", "🔴", "🔕", "✅")


def test_formatters_escape_by_default():
    assert ui.tg_quote("<b>x</b> & y") == "<blockquote>&lt;b&gt;x&lt;/b&gt; &amp; y</blockquote>"
    assert ui.tg_quote_html("<b>ok</b>") == "<blockquote><b>ok</b></blockquote>"
    assert ui.tg_num(1234) == "<code>1234</code>" and ui.tg_num("<1>") == "<code>&lt;1&gt;</code>"
    assert ui.tg_hint("a<b") == "<i>a&lt;b</i>"


def test_helper_is_stdlib_only():
    src = (ROOT / "market_brief/channels/tg_ui_style.py").read_text(encoding="utf-8")
    imports = re.findall(r"^\s*(?:from|import)\s+([\w.]+)", src, re.M)
    assert set(imports) <= {"__future__", "html"}, imports


# ── admission: plain <blockquote> allowed, attributes stripped, chunking ─────
def test_admission_blockquote_measured_as_tag_and_attrs_stripped():
    from admission_brief.channels.telegram import (
        _tag_state,
        measure_message,
        sanitize_admission_html,
        split_admission_html,
    )

    assert _tag_state("<blockquote>") == ("blockquote", False)
    assert _tag_state("</blockquote>") == ("blockquote", True)
    assert _tag_state("<blockquote expandable>") is None
    assert sanitize_admission_html("<blockquote expandable>x</blockquote>") == "<blockquote>x</blockquote>"
    assert sanitize_admission_html('<BLOCKQUOTE class="q">x</BLOCKQUOTE>') == "<blockquote>x</BLOCKQUOTE>"
    assert split_admission_html("<blockquote expandable>hi</blockquote>") == ["<blockquote>hi</blockquote>"]
    assert measure_message("<blockquote>abc</blockquote>")[1] == 3


def test_admission_blockquote_chunking_keeps_tags_balanced():
    from admission_brief.channels.telegram import measure_message, split_admission_html

    body = "<b>제목</b>\n<blockquote>" + ("가나다라마 " * 60) + "</blockquote>\n끝"
    chunks = split_admission_html(body, max_visible=100, max_raw=400)
    assert len(chunks) > 1
    for c in chunks:
        raw, visible = measure_message(c)
        assert raw <= 400 and visible <= 100
        assert c.count("<blockquote>") == c.count("</blockquote>")
        assert "expandable" not in c
    joined_visible = "".join(re.sub(r"<[^>]+>", "", c) for c in chunks)
    assert joined_visible == re.sub(r"<[^>]+>", "", body)


# ── commands & /menu convention ───────────────────────────────
PUBLIC_BOTS = ("menu_bot", "brief_bot", "travel_bot", "kids_bot")


@pytest.mark.parametrize("bot_id", PUBLIC_BOTS)
def test_public_bots_register_start_menu_help(bot_id):
    from market_brief.multi_bot_manager import OFFICIAL_BOT_COMMANDS

    cmds = [c["command"] for c in OFFICIAL_BOT_COMMANDS[bot_id]]
    assert {"start", "menu", "help"} <= set(cmds)
    assert cmds[0] == "start" and cmds[1] == "menu"


def test_all_official_descriptions_lead_with_emoji():
    from market_brief.multi_bot_manager import OFFICIAL_BOT_COMMANDS
    from kids_compass.channels.telegram import KIDS_COMMANDS
    from auto_factory.dev_bot import BOT_COMMANDS

    lists = list(OFFICIAL_BOT_COMMANDS.values()) + [KIDS_COMMANDS, list(BOT_COMMANDS)]
    for cmds in lists:
        for c in cmds:
            assert re.fullmatch(r"[a-z0-9_]{1,32}", c["command"])
            assert not c["description"][0].isalnum(), c  # leading emoji, not a letter/digit


def test_portal_runner_commands_match_official_menu_bot():
    import portal_bot_runner
    from market_brief.multi_bot_manager import OFFICIAL_BOT_COMMANDS

    assert list(portal_bot_runner.PORTAL_BOT_COMMANDS) == OFFICIAL_BOT_COMMANDS["menu_bot"]
    assert portal_bot_runner.match_menu_action("/menu") == portal_bot_runner.match_menu_action("/start") == "START"
    assert portal_bot_runner.match_menu_action("🏠 홈") == "START"


def test_dev_bot_shortcut_bar_preserved():
    from auto_factory import dev_bot

    kb = dev_bot.SHORTCUT_REPLY_KEYBOARD
    assert kb["is_persistent"] is True and len(kb["keyboard"]) == 1 and len(kb["keyboard"][0]) == 3


# ── no long Telegram divider literals ─────────────────────────
_LONG_DIVIDER = re.compile(r"[━─═]{15,}|-{10,}|={10,}")


def _string_tokens(path: Path):
    src = path.read_text(encoding="utf-8")
    kinds = {tokenize.STRING}
    if hasattr(tokenize, "FSTRING_MIDDLE"):
        kinds.add(tokenize.FSTRING_MIDDLE)
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type in kinds:
            yield tok


@pytest.mark.parametrize("rel", BOT_SOURCES)
def test_no_long_divider_string_literals(rel):
    offenders = [(t.start[0], t.string[:40]) for t in _string_tokens(ROOT / rel) if _LONG_DIVIDER.search(t.string)]
    assert offenders == [], f"{rel}: long Telegram divider literal(s) — use TG_DIVIDER: {offenders}"
