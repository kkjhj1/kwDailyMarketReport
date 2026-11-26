"""Shared Telegram UI style constants & safe formatters ([TG-02], [TG-03]).

Stdlib-only, no project imports (safe to import from any bot package without
circular-import risk). ``auto_factory`` deliberately keeps its own copy of
these constants inside ``auto_factory/dev_bot.py`` to stay independent of
``market_brief``.
"""
from __future__ import annotations

import html

__all__ = [
    "TG_DIVIDER",
    "TG_BULLET",
    "NAV_HOME",
    "NAV_BACK",
    "NAV_PORTAL",
    "BADGE_ACTIVE",
    "BADGE_WARN",
    "BADGE_PAUSED",
    "BADGE_OFF",
    "BADGE_MUTED",
    "BADGE_DONE",
    "tg_quote",
    "tg_quote_html",
    "tg_num",
    "tg_hint",
]

# 14-char divider: fits a 360px mobile bubble without wrapping.
TG_DIVIDER = "──────────────"
TG_BULLET = "•"

# Standard navigation button labels.
NAV_HOME = "🏠 홈"
NAV_BACK = "◀ 이전"
NAV_PORTAL = "🧭 통합 메뉴봇"

# Standard status badges.
BADGE_ACTIVE = "🟢"
BADGE_WARN = "🟡"
BADGE_PAUSED = "⏸️"
BADGE_OFF = "🔴"
BADGE_MUTED = "🔕"
BADGE_DONE = "✅"


def tg_quote(text: object) -> str:
    """Wrap *plain text* in a plain ``<blockquote>`` (escaped by default)."""
    return f"<blockquote>{html.escape(str(text), quote=False)}</blockquote>"


def tg_quote_html(inner_html: str) -> str:
    """Wrap an already-trusted, already-escaped HTML fragment in ``<blockquote>``.

    Callers are responsible for having escaped every dynamic value inside
    ``inner_html`` ([TG-02]). Use :func:`tg_quote` for plain text.
    """
    return f"<blockquote>{inner_html}</blockquote>"


def tg_num(val: object) -> str:
    """Render a number/value as ``<code>`` (escaped)."""
    return f"<code>{html.escape(str(val), quote=False)}</code>"


def tg_hint(text: object) -> str:
    """Render a muted hint line as ``<i>`` (escaped)."""
    return f"<i>{html.escape(str(text), quote=False)}</i>"
