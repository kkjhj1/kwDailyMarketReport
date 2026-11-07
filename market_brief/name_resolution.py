"""Name resolution module for Telegram profile names and admin display names.
Combines Korean last_name + first_name, handles English order, username fallback,
and guarantees clean, non-corrupted display names.
"""
from __future__ import annotations
import re
import unicodedata
from typing import Any, Optional


def clean_text(val: Optional[str]) -> str:
    """NFC normalizes and strips whitespace from string."""
    if not val:
        return ""
    normalized = unicodedata.normalize("NFC", str(val))
    return normalized.strip()


def has_hangul(val: str) -> bool:
    """Returns True if string contains any Hangul syllable or jamo."""
    if not val:
        return False
    return any(
        ("\uac00" <= c <= "\ud7a3") or ("\u1100" <= c <= "\u11ff") or ("\u3130" <= c <= "\u318f")
        for c in val
    )


def is_placeholder_name(name: str) -> bool:
    """Checks if name is an auto-generated placeholder or corrupted string."""
    if not name:
        return True
    cleaned = clean_text(name)
    if not cleaned:
        return True

    # Corrupted unicode replacement character
    if "\ufffd" in cleaned:
        return True

    # Standard placeholders like 수신자_1234, 유저_5678, _5792, _5792
    if cleaned.startswith("수신자_") or cleaned.startswith("유저_"):
        return True

    # Pattern like _1234 or _1234
    if re.match(r"^[\ufffd_]?_\d+$", cleaned):
        return True

    return False


def resolve_telegram_display_name(
    first_name: Optional[str],
    last_name: Optional[str],
    username: Optional[str] = None,
    chat_id: Any = None,
) -> tuple[str, str]:
    """Resolves human-readable display name from Telegram profile parts.

    Returns:
        tuple[str, str]: (display_name, source)
        source is either "TELEGRAM" or "FALLBACK".
    """
    first = clean_text(first_name)
    last = clean_text(last_name)
    uname = clean_text(username).lstrip("@")
    cid_str = str(chat_id or "").strip()

    # 1. Both first and last names exist
    if last and first:
        if has_hangul(last + first):
            return f"{last}{first}", "TELEGRAM"
        return f"{first} {last}", "TELEGRAM"

    # 2. Only first name
    if first:
        return first, "TELEGRAM"

    # 3. Only last name
    if last:
        return last, "TELEGRAM"

    # 4. Username fallback
    if uname:
        return f"@{uname}", "TELEGRAM"

    # 5. Fallback with chat_id
    if cid_str:
        suffix = cid_str[-4:] if len(cid_str) >= 4 else cid_str
        return f"수신자_{suffix}", "FALLBACK"

    return "수신자", "FALLBACK"
