"""UDB person-record cleaning helpers used by lookup_person."""

from __future__ import annotations

import re
from typing import Any

_DEPT_CODE_RE = re.compile(r"\s*\(\d+\)\s*$")
_ROOM_CODE_RE = re.compile(r"\b([A-Z]-\d{1,4}[a-z]?)\b")
_BUILDING_HINTS = ("karlovo", "resslova", "praha", "technick", "jugoslav", "dejvic")


def _clean_department(value: str | None) -> str:
    """Strip the trailing "(NNNN)" department-code suffix UDB appends."""
    if not value:
        return ""
    cleaned = _DEPT_CODE_RE.sub("", value).strip()
    lower = cleaned.lower()
    if any(token in lower for token in _BUILDING_HINTS):
        return ""
    return cleaned


def _extract_room_code(room: str | None) -> str:
    """Pull every room code (e.g. 'E-211') from the noisy address string.

    Some staff are listed in more than one building (e.g. 'A-327' and
    'E-17'); preserve all of them, comma-separated, so the agent can
    mention every location.
    """
    if not room:
        return ""
    matches = _ROOM_CODE_RE.findall(room)
    return ", ".join(matches)


def _slim_match(m: dict[str, Any]) -> dict[str, Any]:
    """Compact contact record returned to the LLM."""
    return {
        "name": m.get("name"),
        "department": _clean_department(m.get("department")) or None,
        "room": _extract_room_code(m.get("room")) or None,
        "phone": m.get("phone"),
        "email": m.get("email"),
    }
