"""Lookup_person disambiguation logic — title-score heuristic + initial
fallback, ported from voice-agent/tests/local_llm_benchmark/.

Used by the `lookup_person` tool in tools.py. The shape is specifically
designed for the agent flow:
  - both first_name and surname are required (LLM must ask the user
    for the missing part before calling).
  - if the surname matches but the first name doesn't, return a
    `first_name_not_found` error with an `instruction` field telling
    the agent how to reply.
  - if multiple staff share the surname AND the user-supplied first
    name, the most-credentialed one wins (highest title score) — no
    voice-disambiguation prompt.
  - the result always has count == 1 plus an `instruction` field
    telling the agent to reply with only the field the user asked for
    (just the phone, just the email, etc.).

The data source is `voice-agent/src/udb.py:lookup_person` (the live
UDB scraper). This module is purely the post-filter + presentation
layer.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from typing import Any

from .udb import NotOnCzvutNetworkError, lookup_person as udb_lookup_person


_TITLE_WEIGHTS: dict[str, float] = {
    "prof.": 4.0,
    "doc.": 3.0,
    "DrSc.": 3.0,
    "Ph.D.": 2.0,
    "CSc.": 2.0,
    "RNDr.": 1.5,
    "MUDr.": 1.5,
    "JUDr.": 1.5,
    "PhDr.": 1.5,
    "Ing.": 1.0,
    "Mgr.": 1.0,
    "Bc.": 0.5,
}

_DEPT_CODE_RE = re.compile(r"\s*\(\d+\)\s*$")
_ROOM_CODE_RE = re.compile(r"\b([A-Z]-\d{1,4}[a-z]?)\b")
_BUILDING_HINTS = ("karlovo", "resslova", "praha", "technick", "jugoslav", "dejvic")


def _name_tokens(name: str) -> list[str]:
    return (name or "").replace(",", " ").split()


def _title_score(name: str) -> float:
    return sum(_TITLE_WEIGHTS.get(tok, 0.0) for tok in _name_tokens(name))


def _first_name_token(name: str) -> str:
    """First token in the name that is not an academic title."""
    for tok in _name_tokens(name):
        if tok not in _TITLE_WEIGHTS:
            return tok.lower()
    return ""


def _fold(text: str) -> str:
    """Lowercase + strip diacritics for forgiving comparison."""
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _clean_department(value: str | None) -> str:
    """Strip the trailing '(NNNN)' department-code suffix UDB appends."""
    if not value:
        return ""
    cleaned = _DEPT_CODE_RE.sub("", value).strip()
    lower = cleaned.lower()
    if any(token in lower for token in _BUILDING_HINTS):
        return ""
    return cleaned


def _extract_room_code(room: str | None) -> str:
    """Pull every room code (e.g. 'E-211') from the noisy address string.
    Some staff are listed in more than one building; preserve all of
    them, comma-separated, so the agent can mention every location."""
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


_REPLY_INSTRUCTION = (
    "Public staff directory entry. Reply with only the field the user "
    "asked for — if they asked for a phone, say just the phone; if "
    "they asked for an email, say just the email; if they asked where "
    "the person is, say just the room. Use the person's surname or "
    "'professor X' / 'doctor X' form, never the full title chain."
)


async def find_person(first_name: str, surname: str) -> dict[str, Any]:
    """Run the staff-directory lookup with first-name disambiguation.

    Always returns either:
      - {"count": 1, "matches": [<one record>], "instruction": ...}
      - {"error": "missing_first_name" | "first_name_not_found" |
                  "off_network" | "fetch_failed", "instruction": ...}
      - {"count": 0, "matches": []}     (surname not in the directory)
    """
    surname_q = (surname or "").strip()
    first_q = (first_name or "").strip()
    if (
        not surname_q
        or not first_q
        or first_q.lower() == surname_q.lower()
    ):
        return {
            "error": "missing_first_name",
            "instruction": (
                "A real first name AND surname are required, and they "
                "must be different. Ask the user — in plain "
                "conversation — what the person's first name is, and "
                "call this tool again only after the user answers."
            ),
        }

    try:
        result = await asyncio.to_thread(udb_lookup_person, surname_q)
    except NotOnCzvutNetworkError as exc:
        return {
            "error": "off_network",
            "message": str(exc),
            "instruction": (
                "The staff directory is unreachable (off network). "
                "Apologise briefly and offer to take a message or "
                "help with something else."
            ),
        }
    except Exception as exc:
        return {
            "error": "fetch_failed",
            "message": str(exc),
            "instruction": (
                "The staff directory lookup failed. Apologise briefly "
                "and offer to try again later."
            ),
        }

    candidates = [_slim_match(m) for m in (result.get("matches") or [])]
    if not candidates:
        return {"count": 0, "matches": []}

    wanted = _fold(first_q).rstrip(".").strip()

    def _candidate_first(m: dict) -> str:
        return _fold(_first_name_token(m.get("name") or ""))

    exact = [m for m in candidates if _candidate_first(m) == wanted]
    if exact:
        candidates = exact
    elif len(wanted) == 1:
        # Timetables and other sources sometimes give just an initial
        # ("Hoffmann M."). Fall back to first-letter match silently.
        initial = [m for m in candidates if _candidate_first(m).startswith(wanted)]
        if initial:
            candidates = initial
        else:
            return {
                "error": "first_name_not_found",
                "instruction": (
                    f"No {surname_q} matches first name {first_q!r}. "
                    "Tell the user and ask them to confirm or correct "
                    "the first name. Do not call this tool again "
                    "until they do."
                ),
            }
    else:
        return {
            "error": "first_name_not_found",
            "instruction": (
                f"No person named {first_q!r} {surname_q!r} exists in "
                "the directory. Tell the user that name was not found "
                "and ask them to confirm or correct the first name. "
                "Do not call this tool again until they do."
            ),
        }

    best = max(candidates, key=lambda m: _title_score(m.get("name") or ""))
    return {
        "count": 1,
        "matches": [best],
        "instruction": _REPLY_INSTRUCTION,
    }
