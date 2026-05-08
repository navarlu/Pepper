"""lookup_person: public staff directory search by name.

When several people share a surname, pick the most-credentialed one
(highest sum of academic title weights). This is a deliberate heuristic
to avoid asking the user to disambiguate over voice.
"""

from __future__ import annotations

import sys
import unicodedata
from pathlib import Path
from typing import Literal
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio

from livekit.agents import RunContext, function_tool

from tools._animation import trigger_animation
from tools._common import _json, _run_main
from tools._person import _slim_match

from src.udb import NotOnCzvutNetworkError, lookup_person as udb_lookup_person  # noqa: E402

_Gesture = Literal["greet", "think", "explain", "bow", "happy", "dont_know"]


_TITLE_WEIGHTS = {
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


@function_tool
async def lookup_person(
    context: RunContext,
    first_name: str,
    surname: str,
    gesture: _Gesture = "think",
) -> str:
    """Look up a person's contact info (phone, email, room) in the
    public staff directory.

    Both `first_name` and `surname` are required. If the user gave
    only a surname or only a first name, ask them in conversation
    for the missing part BEFORE calling this tool.

    first_name: the person's given name. Required.
    surname: the person's surname only — no titles. Required.
    gesture: Pepper body language while looking up. Default 'think'.
        One of greet, think, explain, bow, happy, dont_know.
    """
    del context
    if gesture:
        asyncio.create_task(trigger_animation(gesture))
    surname_q = str(surname or "").strip()
    first_q = str(first_name or "").strip()
    if (
        not surname_q
        or not first_q
        or first_q.lower() == surname_q.lower()
    ):
        return _json({
            "error": "missing_first_name",
            "instruction": (
                "A real first name AND surname are required, and "
                "they must be different. Ask the user — in plain "
                "conversation — what the person's first name is, "
                "and call this tool again only after the user "
                "answers."
            ),
        })

    print(
        f"  [tool] lookup_person(first_name={first_q!r}, "
        f"surname={surname_q!r})"
    )
    try:
        result = await asyncio.to_thread(udb_lookup_person, surname_q)
    except NotOnCzvutNetworkError as exc:
        return _json({"error": str(exc)})
    except Exception as exc:
        return _json({"error": str(exc)})

    candidates = [_slim_match(m) for m in (result.get("matches") or [])]
    if not candidates:
        return _json({"count": 0, "matches": []})

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
            return _json({
                "error": "first_name_not_found",
                "instruction": (
                    f"No {surname_q} matches first name {first_q!r}. "
                    "Tell the user and ask them to confirm or correct "
                    "the first name. Do not call this tool again "
                    "until they do."
                ),
            })
    else:
        return _json({
            "error": "first_name_not_found",
            "instruction": (
                f"No person named {first_q!r} {surname_q!r} exists "
                "in the directory. Tell the user that name was not "
                "found and ask them to confirm or correct the first "
                "name. Do not call this tool again until they do."
            ),
        })

    best = max(candidates, key=lambda m: _title_score(m.get("name") or ""))
    print(
        f"  [tool] lookup_person picked {best.get('name')!r} "
        f"(score={_title_score(best.get('name') or ''):.1f}, "
        f"from {len(candidates)} candidate(s))"
    )
    return _json({
        "count": 1,
        "matches": [best],
        "instruction": (
            "Public staff directory entry. Reply with only the field "
            "the user asked for — if they asked for a phone, say just "
            "the phone; if they asked for an email, say just the email; "
            "if they asked where the person is, say just the room. "
            "Use the person's surname or 'professor X' / 'doctor X' "
            "form, never the full title chain."
        ),
    })


if __name__ == "__main__":
    FIRST_NAME = "mistr"
    SURNAME = "Hoffmann"
    _run_main(lookup_person(None, first_name=FIRST_NAME, surname=SURNAME))
    test = "can you find me phone number for Hoffmann?"
