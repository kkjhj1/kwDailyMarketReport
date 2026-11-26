"""Bot visibility SSOT: which INFO bots are publicly open vs. WIP (서비스 준비 중).

Dependency-free on purpose (stdlib only) so `portal_hub.models`, `market_brief.subscription_engine`
and `market_brief.multi_bot_manager` can all import it without circular imports.

- PUBLIC bots: listed in Menu Bot and open to self-service subscription.
- WIP bots: kept in the DB/enum for backend compatibility, but USER-initiated new subscriptions,
  swaps-in and direct-chat access are blocked (`BOT_WIP`). ADMIN assignment is still allowed and
  already-active subscribers are grandfathered. Unsubscribe is never blocked.
- Unknown/custom bot ids are neither public nor WIP (not blocked by this module).
"""
from __future__ import annotations

BOT_ID_ALIASES: dict[str, str] = {"edu_bot": "admission_bot"}

PUBLIC_BOT_IDS: frozenset[str] = frozenset({"brief_bot", "travel_bot", "kids_bot", "admission_bot", "chungyak_bot"})
WIP_BOT_IDS: frozenset[str] = frozenset({"silver_bot", "yolo_bot"})

BOT_WIP_CODE = "BOT_WIP"
BOT_WIP_MESSAGE = "🚧 현재 서비스 준비 중인 봇입니다."


def normalize_bot_id(bot_id: str) -> str:
    """Canonical platform bot id (`edu_bot` is a legacy alias of `admission_bot`)."""
    bid = str(bot_id or "").strip()
    return BOT_ID_ALIASES.get(bid, bid)


def is_public_bot_id(bot_id: str) -> bool:
    return normalize_bot_id(bot_id) in PUBLIC_BOT_IDS


def is_wip_bot_id(bot_id: str) -> bool:
    return normalize_bot_id(bot_id) in WIP_BOT_IDS


def is_admin_actor(actor: str | None) -> bool:
    return str(actor or "").strip().upper() == "ADMIN"
