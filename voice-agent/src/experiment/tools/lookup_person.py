"""lookup_person tool — staff-directory search with English→Czech
phonetic fallback (Whisper transcribes Czech speech in English
orthography). Helpers for title scoring and surname variants live in
`tools/utils/_person_lookup.py`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from livekit.agents import RunContext, function_tool

from .utils._animation import trigger_animation
from .utils._emotion import Emotion
from .utils._events import _emit_tool_event, _heartbeat_or_none
from .utils._filler import _speak_filler
from .utils._person import _slim_match
from .utils._person_lookup import (
    _english_to_czech_variants,
    _first_name_token,
    _fold,
    _is_generic_non_name,
    _is_honorific,
    _pick_best_variant,
    _title_score,
    _udb_lookup_variants,
)

from src.live.udb import (  # noqa: E402
    NotOnCzvutNetworkError,
    lookup_person as udb_lookup_person,
)


@function_tool(name="lookup_person")
async def lookup_person(
    context: RunContext,
    first_name: str,
    surname: str,
    emotion: Emotion = "think",
    request_heartbeat: bool = True,
) -> Any:
    """Look up a staff member in the directory.

    Call this when the user names a specific person (a proper-noun
    surname like 'Novák', 'Svoboda', 'Smith') and asks for their
    phone, email, or office.

    Example: user says "Where can I find professor Novák?" → call
    with surname="Novák", first_name="" → reply with the office or
    phone the tool returns.

    Pass surname exactly as the user said it — the tool tries common
    Czech spelling variants automatically (Whisper renders Czech in
    English orthography, e.g. "Šebek" → "Shebek").

    first_name: the person's given name, or "" if not given.
    surname: the proper-noun surname the user spoke.
    emotion: body language while looking up. Default 'think'.
    request_heartbeat: True (default) to continue the turn after the
        lookup so you can reply to the user.
    """
    asyncio.create_task(_speak_filler(context, "Let me look that up in the directory."))
    del context
    print(
        f"  [tool] lookup_person(first_name={first_name!r}, "
        f"surname={surname!r}, emotion={emotion!r}, hb={request_heartbeat})"
    )
    _emit_tool_event("lookup_person", {
        "first_name": first_name, "surname": surname,
        "emotion": emotion, "request_heartbeat": request_heartbeat,
    })
    if emotion:
        asyncio.create_task(trigger_animation(emotion))

    surname_q = (surname or "").strip()
    first_q = (first_name or "").strip()
    # Surname is the only hard requirement — UDB searches by surname.
    if not surname_q:
        return _heartbeat_or_none({
            "error": "missing_surname",
            "_agent_note": (
                "Surname missing. Ask the user for the person's surname."
            ),
        }, request_heartbeat)
    # Backstop: reject generic words ("user", "human", "someone", …)
    # the model occasionally passes when no specific name was said.
    # The model never sees the blocklist — only the rejection result.
    if _is_generic_non_name(surname_q):
        print(
            f"  [tool] lookup_person rejected generic non-name "
            f"{surname_q!r}"
        )
        return _heartbeat_or_none({
            "error": "not_a_name",
            "rejected_surname": surname_q,
            "_agent_note": (
                f"Input {surname_q!r} is a generic word, not a "
                "surname. No directory call was made. Answer the user "
                "directly without looking anything up."
            ),
        }, request_heartbeat)
    # Drop honorifics and surname-duplicates from first_q so they don't
    # poison the tiebreak filter. UDB never sees first_q anyway —
    # we use it only to narrow multi-candidate hits.
    if _is_honorific(first_q) or first_q.lower() == surname_q.lower():
        first_q = ""

    # English → Czech digraph fanout: try the input as-is plus the most
    # likely Czech-spelled alternatives, all in parallel against UDB.
    variants = _english_to_czech_variants(surname_q)
    print(f"  [tool] lookup_person variants={variants!r}")
    runs = await _udb_lookup_variants(
        variants, udb_lookup_person, NotOnCzvutNetworkError,
    )
    for v, r, err in runs:
        if err:
            print(f"  [tool] udb({v!r}) error: {err}")
        else:
            n = len(r.get("matches") or []) if r else 0
            print(f"  [tool] udb({v!r}) -> {n} match(es)")

    # Surface the first network error if NO variant succeeded — keeps
    # the existing NotOnCzvutNetworkError UX intact.
    if all(r is None for _, r, _ in runs):
        first_err = next((err for _, _, err in runs if err), "lookup_failed")
        return _heartbeat_or_none({"error": first_err}, request_heartbeat)

    picked = _pick_best_variant(surname_q, runs)
    if picked is None:
        return _heartbeat_or_none({
            "count": 0,
            "matches": [],
            "tried_variants": variants,
            "_agent_note": (
                f"No staff named {surname_q!r} found in the directory "
                f"(tried Czech variants: {variants}). Ask the user to "
                "confirm or spell the surname."
            ),
        }, request_heartbeat)
    chosen_variant, result = picked

    candidates = [_slim_match(m) for m in (result.get("matches") or [])]
    if not candidates:
        return _heartbeat_or_none(
            {"count": 0, "matches": []}, request_heartbeat,
        )

    # First-name tiebreaker (best-effort): narrow candidates IF a usable
    # first_q exists AND it actually matches at least one. If the filter
    # would empty the set, ignore it — the user might have given a
    # wrong/approximated first name; still better to answer with the
    # highest-titled person than to refuse.
    filter_note: str | None = None
    filtered = candidates
    if first_q:
        wanted = _fold(first_q).rstrip(".").strip()

        def _candidate_first(m: dict) -> str:
            return _fold(_first_name_token(m.get("name") or ""))

        exact = [m for m in candidates if _candidate_first(m) == wanted]
        if exact:
            filtered = exact
        elif len(wanted) == 1:
            initial = [
                m for m in candidates if _candidate_first(m).startswith(wanted)
            ]
            if initial:
                filtered = initial
            else:
                filter_note = (
                    f"first_name {first_q!r} did not match any "
                    f"{chosen_variant!r}; falling back to highest-titled "
                    "from all surname matches"
                )
        else:
            filter_note = (
                f"first_name {first_q!r} did not match any "
                f"{chosen_variant!r}; falling back to highest-titled "
                "from all surname matches"
            )

    best = max(filtered, key=lambda m: _title_score(m.get("name") or ""))
    alternatives = [
        m.get("name") for m in filtered if m is not best
    ]
    print(
        f"  [tool] lookup_person picked {best.get('name')!r} "
        f"via variant={chosen_variant!r} "
        f"(score={_title_score(best.get('name') or ''):.1f}, "
        f"from {len(filtered)}/{len(candidates)} candidate(s))"
    )

    if len(filtered) == 1 and filter_note is None:
        agent_note = (
            "Single directory match. Reply with only the field the "
            "user asked for — phone, email, or office."
        )
    elif filter_note:
        agent_note = (
            f"{filter_note}. Reply with the requested field for the "
            "picked person. If this looks like the wrong person, "
            "briefly mention the alternatives and ask which one the "
            "user meant."
        )
    else:
        agent_note = (
            f"Multiple people share surname {chosen_variant!r}. The "
            f"most senior was picked. Other candidates: {alternatives}. "
            "If the user asked about a specific person, briefly ask "
            "which one they meant; otherwise reply with the requested "
            "field for the picked person."
        )

    response: dict[str, Any] = {
        "count": len(filtered),
        "matches": [best],
        "_agent_note": agent_note,
    }
    if alternatives:
        response["alternatives"] = alternatives
    if filter_note:
        response["filter_note"] = filter_note
    return _heartbeat_or_none(response, request_heartbeat)
