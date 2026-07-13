"""lookup_person tool — staff-directory search for the paper MVP.

Slim sibling of `src/experiment/tools/lookup_person.py`: same UDB
scrape, same EN→CZ surname-variant fanout, same title-based
tiebreaking — minus the cascade-only extras (emotion/gesture, filler
TTS, heartbeat shim; the realtime model announces the lookup itself,
the prompt tells it to).

The variant fanout was built for Whisper's English orthography
("Šebek" → "Shebek"). The realtime model usually transcribes Czech
natively (proper diacritics), but the fanout always includes the
input as-is, so it stays a harmless safety net either way.
"""

from __future__ import annotations

from typing import Any

from livekit.agents import RunContext, function_tool

from src.experiment.tools.utils._events import (
    _emit_tool_event,
    _emit_tool_result,
)
from src.experiment.tools.utils._person import _slim_match
from src.experiment.tools.utils._person_lookup import (
    _english_to_czech_variants,
    _first_name_token,
    _fold,
    _is_generic_non_name,
    _is_honorific,
    _pick_best_variant,
    _title_score,
    _udb_lookup_variants,
)
from src.live.udb import (
    NotOnCzvutNetworkError,
    lookup_person as udb_lookup_person,
)


def _done(result: dict[str, Any]) -> dict[str, Any]:
    """Emit the tool_result event and return the payload."""
    _emit_tool_result("lookup_person", result)
    return result


@function_tool(name="lookup_person")
async def lookup_person(
    context: RunContext,
    first_name: str,
    surname: str,
) -> Any:
    """Look up a staff member in the university directory.

    Call this when the user names a specific person (a proper-noun
    surname like 'Novák', 'Svoboda', 'Smith') and asks for their
    phone, email, or office.

    Example: user says "Kde najdu profesora Nováka?" → call with
    surname="Novák", first_name="" → reply with the office or phone
    the tool returns.

    Pass the surname in its base (nominative) form, exactly as the
    user said it — the tool tries common spelling variants
    automatically.

    first_name: the person's given name, or "" if not given.
    surname: the proper-noun surname the user spoke.
    """
    del context
    surname_q = (surname or "").strip()
    first_q = (first_name or "").strip()
    print(
        f"  [tool] lookup_person(first_name={first_q!r}, surname={surname_q!r})",
        flush=True,
    )
    _emit_tool_event("lookup_person", {
        "first_name": first_q, "surname": surname_q,
    })

    # Surname is the only hard requirement — UDB searches by surname.
    if not surname_q:
        return _done({
            "error": "missing_surname",
            "_agent_note": (
                "Surname missing. Ask the user (in Czech) for the "
                "person's surname."
            ),
        })
    # Backstop: reject generic words ("user", "someone", …) the model
    # occasionally passes when no specific name was said.
    if _is_generic_non_name(surname_q):
        print(f"  [tool] lookup_person rejected generic non-name {surname_q!r}")
        return _done({
            "error": "not_a_name",
            "rejected_surname": surname_q,
            "_agent_note": (
                f"Input {surname_q!r} is a generic word, not a "
                "surname. No directory call was made. Answer the user "
                "directly without looking anything up."
            ),
        })
    # Drop honorifics and surname-duplicates from first_q so they don't
    # poison the tiebreak filter.
    if _is_honorific(first_q) or first_q.lower() == surname_q.lower():
        first_q = ""

    # EN→CZ digraph fanout: the input as-is plus likely Czech-spelled
    # alternatives, all in parallel against UDB.
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

    # Surface the first network error only if NO variant succeeded.
    if all(r is None for _, r, _ in runs):
        first_err = next((err for _, _, err in runs if err), "lookup_failed")
        return _done({"error": first_err})

    picked = _pick_best_variant(surname_q, runs)
    if picked is None:
        return _done({
            "count": 0,
            "matches": [],
            "tried_variants": variants,
            "_agent_note": (
                f"No staff named {surname_q!r} found in the directory "
                f"(tried Czech variants: {variants}). Ask the user to "
                "confirm or spell the surname."
            ),
        })
    chosen_variant, result = picked

    candidates = [_slim_match(m) for m in (result.get("matches") or [])]
    if not candidates:
        return _done({"count": 0, "matches": []})

    # First-name tiebreaker (best-effort): narrow candidates IF a usable
    # first_q exists AND it actually matches at least one. If the filter
    # would empty the set, ignore it — better to answer with the
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
    alternatives = [m.get("name") for m in filtered if m is not best]
    print(
        f"  [tool] lookup_person picked {best.get('name')!r} "
        f"via variant={chosen_variant!r} "
        f"(from {len(filtered)}/{len(candidates)} candidate(s))"
    )

    if len(filtered) == 1 and filter_note is None:
        agent_note = (
            "Single directory match. Reply (in Czech) with only the "
            "field the user asked for — phone, email, or office."
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
    return _done(response)
