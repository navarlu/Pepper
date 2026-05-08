"""Tool-only (Letta / MemGPT-style) tool surface for the local-LLM
benchmark.

Every tool is augmented with two parameters that are NOT in the
default tools/ surface:
  - `emotion`: Literal — Pepper body language for this action.
  - `request_heartbeat`: bool — Letta-style continue signal. True
    (the default) keeps the loop alive so the model can speak via
    `send_message_to_user`. False causes the tool to return None,
    which makes LiveKit's loop terminate without another LLM pass
    (see generation.py:814 — `reply_required = fnc_out is not None`).

The terminal tool is `send_message_to_user(text, emotion)`. It pushes
text to TTS via `session.say` and returns None, so the loop ends as
soon as Pepper finishes speaking — exactly mirroring Letta's
`send_message` + `TerminalToolRule` pattern.

Why this works in LiveKit without any framework patching:
  * LiveKit re-invokes the LLM after a tool only if at least one of
    the executed tools had `reply_required=True`. That flag is set
    automatically when the tool returns a non-None value.
  * Returning None therefore IS the "terminal" signal — same effect
    as Letta's TerminalToolRule, but native to LiveKit.

Used by `livekit_console_toolonly.py`.
"""

from __future__ import annotations

import asyncio
import contextvars
import sys
import traceback
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo

# Path setup mirrors tools/_common so we can import src.* from this file
# regardless of run mode.
_BENCHMARK_DIR = Path(__file__).resolve().parent
if str(_BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(_BENCHMARK_DIR))
_VOICE_AGENT_DIR = Path(__file__).resolve().parents[2]
if str(_VOICE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_VOICE_AGENT_DIR))

import re

from livekit.agents import RunContext, function_tool

from tools._animation import trigger_animation
from tools._common import _agent_result, _ensure_weaviate_seeded_once, _json
from tools._person import _slim_match
from tools.find_path_to_room import (
    ROOM_DIRECTIONS,
    _normalize_room,
    _query_floor,
    _render_curated,
    _render_floor_only,
)

from src.bridge_client import post_led_state, post_tablet_info  # noqa: E402
from src.config import QUERY_SEARCH_DEFAULT_LIMIT, WEAVIATE_HYBRID_ALPHA  # noqa: E402
from src.mensa import fetch_mensa_menu  # noqa: E402
from src.rag import search_vectors  # noqa: E402
from src.timetable import fetch_subject_schedule  # noqa: E402
from src.udb import (  # noqa: E402
    NotOnCzvutNetworkError,
    lookup_person as udb_lookup_person,
)


_Emotion = Literal[
    "greet", "think", "explain", "bow", "happy", "dont_know",
]


# ── Tool-event listener hook ──────────────────────────────────────────
# Lets the worker / experiment recorder observe every tool call without
# patching each tool body. Production agent.py uses the same pattern in
# voice-agent/src/tools.py (`set_tool_event_listener`).
_external_tool_listener: Any = None
_external_tool_result_listener: Any = None

# Per-task context: which tool is currently executing. Set by
# _emit_tool_event at entry, read by _heartbeat_or_none at exit. A
# contextvars.ContextVar isolates per asyncio.Task, so concurrent tool
# calls don't cross-contaminate. This avoids having to plumb the tool
# name through every _heartbeat_or_none call site.
_current_tool_name: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_tool_name", default=None,
)


def set_tool_event_listener(listener) -> None:
    """Register a callback fired on every tool CALL (entry).

    Signature: listener(name, args). Errors in the listener are
    swallowed — body language must never break the agent's reply path.
    """
    global _external_tool_listener
    _external_tool_listener = listener


def set_tool_result_listener(listener) -> None:
    """Register a callback fired on every tool RESULT (exit), with the
    payload that the tool returned to the LLM (or None if the tool was
    a terminal one like send_message_to_user). Signature:
    listener(name, result_dict_or_None)."""
    global _external_tool_result_listener
    _external_tool_result_listener = listener


def _emit_tool_event(name: str, args: dict[str, Any]) -> None:
    """Internal: forward the tool call to the registered listener AND
    remember the tool name in the contextvar so _heartbeat_or_none can
    emit a matching result event later, without per-tool plumbing."""
    _current_tool_name.set(name)
    if _external_tool_listener is None:
        return
    try:
        _external_tool_listener(name, args)
    except Exception as exc:  # noqa: BLE001
        # Cosmetic — never let a recorder crash a tool.
        print(f"  [tool-event] listener error: {exc!r}")


def _emit_tool_result(name: str, result: Any) -> None:
    """Internal: forward the tool's return payload to the result
    listener so the experiment recorder can capture what the LLM
    actually saw back from each tool."""
    if _external_tool_result_listener is None:
        return
    try:
        _external_tool_result_listener(name, result)
    except Exception as exc:  # noqa: BLE001
        print(f"  [tool-result] listener error: {exc!r}")


def _heartbeat_or_none(payload: dict[str, Any], request_heartbeat: bool) -> Any:
    """Letta-style termination shim.

    When `request_heartbeat=True` (the default), return the JSON
    payload so LiveKit re-invokes the LLM with the result.
    When False, return None so `reply_required` is False and the
    loop halts after this tool — same effect as Letta's
    TerminalToolRule but driven by the model's own kwarg.

    Side effect: emits a tool_result event with the payload (whether or
    not the loop continues), tagged with the tool name from the
    contextvar set by _emit_tool_event. This is how the experiment
    recorder captures what the LLM saw from each tool.
    """
    name = _current_tool_name.get()
    if name:
        _emit_tool_result(name, payload)
    if not request_heartbeat:
        print(f"  [heartbeat] False — halting loop (payload dropped: {list(payload.keys())})")
        return None
    return _json(payload)


# ── Title-score helpers (lifted from tools/lookup_person.py to keep
#    this module self-contained — same heuristic as the original).
_TITLE_WEIGHTS = {
    "prof.": 4.0, "doc.": 3.0, "DrSc.": 3.0, "Ph.D.": 2.0, "CSc.": 2.0,
    "RNDr.": 1.5, "MUDr.": 1.5, "JUDr.": 1.5, "PhDr.": 1.5,
    "Ing.": 1.0, "Mgr.": 1.0, "Bc.": 0.5,
}


def _name_tokens(name: str) -> list[str]:
    return (name or "").replace(",", " ").split()


def _title_score(name: str) -> float:
    return sum(_TITLE_WEIGHTS.get(tok, 0.0) for tok in _name_tokens(name))


def _first_name_token(name: str) -> str:
    for tok in _name_tokens(name):
        if tok not in _TITLE_WEIGHTS:
            return tok.lower()
    return ""


# Honorifics the LLM sometimes packs into `first_name` instead of asking
# the user for the real one ("Mr. Shebek" → first_name="Mr."). Folded,
# lowercase, no trailing punctuation. Czech: pan / paní / slečna.
_HONORIFIC_TOKENS = frozenset({
    "mr", "mrs", "ms", "miss", "sir", "madam", "madame",
    "pan", "pani", "slecna",
})


def _is_honorific(token: str) -> bool:
    return _fold(token).strip().rstrip(".,;:") in _HONORIFIC_TOKENS


def _fold(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


# ── English → Czech surname normalisation ─────────────────────────────
# Whisper transcribes Czech speech in English orthography ("Šebek" →
# "Shebek" / "Shebeck"). UDB search is diacritic-insensitive but takes
# the surname literally, so the English approximation gets 0 hits.
# We undo the common digraph approximations and try every candidate in
# parallel — first hit wins.
_ENG_TO_CZ_RULES = (
    ("sh", "s"),    # Šebek → Shebek
    ("zh", "z"),    # Železný → Zhelezny
    ("cz", "c"),    # Polish-style spelling
    ("w", "v"),     # Wagnerová → Vagnerova
    ("ck", "k"),    # Šebek written "Shebeck"
    ("ch", "c"),    # Čermák → Chermak (ch is also a real Czech digraph,
                    # so the identity candidate covers genuine ch names)
)


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[-1]


def _english_to_czech_variants(surname: str) -> list[str]:
    """Generate up to ~6 deduped Czech-spelling candidates from a folded
    English approximation. Always includes the identity (so a user who
    already said the Czech form gets a direct hit). Two passes so rules
    can stack: 'shebeck' → 'sebeck' (sh→s) + 'shebek' (ck→k) → 'sebek'."""
    base = _fold(surname).strip()
    if not base:
        return []
    variants: list[str] = [base]
    for old, new in _ENG_TO_CZ_RULES:
        if old in base:
            variants.append(base.replace(old, new))
    stacked = list(variants)
    for v in variants:
        for old, new in _ENG_TO_CZ_RULES:
            if old in v:
                stacked.append(v.replace(old, new))
    seen: set[str] = set()
    unique: list[str] = []
    for v in stacked:
        if v and v not in seen:
            seen.add(v)
            unique.append(v)
    return unique[:6]


async def _udb_lookup_variants(variants: list[str]):
    """Run UDB queries for every variant in parallel. Returns a list of
    (variant, result_or_None, error_or_None) preserving input order so
    we can deterministically pick the best match below."""
    async def _one(v: str):
        try:
            r = await asyncio.to_thread(udb_lookup_person, v)
            return (v, r, None)
        except NotOnCzvutNetworkError as exc:
            return (v, None, str(exc))
        except Exception as exc:  # noqa: BLE001
            return (v, None, str(exc))
    return await asyncio.gather(*[_one(v) for v in variants])


def _pick_best_variant(original: str, runs):
    """Pick the (variant, result) with non-empty matches, preferring
    the candidate closest to the original input by Levenshtein."""
    hits = [(v, r) for v, r, _ in runs if r and (r.get("matches") or [])]
    if not hits:
        return None
    return min(hits, key=lambda vr: _levenshtein(original, vr[0]))


_SUBJECT_CODE = re.compile(r"^[A-Za-z]{2,4}\d?$")


# ──────────────────────────────────────────────────────────────────────
# Terminal tool — only way for the model to talk to the user.
# ──────────────────────────────────────────────────────────────────────


@function_tool(name="send_message_to_user")
async def send_message_to_user(
    context: RunContext,
    text: str,
    emotion: _Emotion = "explain",
) -> None:
    """Speak to the user. THIS IS THE ONLY WAY YOUR WORDS REACH THE
    USER. Plain assistant text is never spoken — only the `text`
    argument of this tool.

    Calling this tool ENDS the current turn. After speaking you do
    not get to call another tool, so finish gathering information
    BEFORE calling this. Use exactly once per user turn, as the
    final action.

    text: the words Pepper will say. TTS reads this verbatim, so
        write plain conversational prose with no markdown, no JSON,
        no bracketed stage directions, no tool names.
    emotion: body language for this utterance — greet, think,
        explain, bow, happy, dont_know.
    """
    text_clean = (text or "").strip()
    print(f"  [tool] send_message_to_user(emotion={emotion!r}, text={text_clean!r})")
    _emit_tool_event("send_message_to_user", {"text": text_clean, "emotion": emotion})
    if not text_clean:
        # Returning None still terminates the loop, same as a normal
        # send. We just don't push empty audio to the user.
        return None

    if emotion:
        asyncio.create_task(trigger_animation(emotion))

    # session.say pushes through the TTS pipeline. Awaiting on the
    # SpeechHandle ensures we don't return (and end the loop) before
    # Pepper actually starts speaking.
    handle = context.session.say(text_clean, allow_interruptions=True)
    try:
        await handle.wait_for_playout()
    except Exception as exc:
        # If TTS errors, log but still terminate — better silent than
        # looping forever.
        print(f"  [tool] send_message_to_user playout error: {exc!r}")

    return None  # ← Terminal: reply_required=False, loop ends.


# ──────────────────────────────────────────────────────────────────────
# Non-terminal tools — emotion + heartbeat on every one.
# ──────────────────────────────────────────────────────────────────────


@function_tool(name="lookup_person")
async def lookup_person(
    context: RunContext,
    first_name: str,
    surname: str,
    emotion: _Emotion = "think",
    request_heartbeat: bool = True,
) -> Any:
    """Look up a person's contact info (phone, email, room) in the
    public staff directory. Tolerates phonetic approximations of
    unfamiliar surnames — pass exactly what the user said and the
    tool will try common spelling variants automatically.

    Surname is the actual search key. First name is OPTIONAL and used
    only as a tiebreaker when several people share a surname. If the
    user only gave a surname (or said "Mr./Mrs. <surname>"), pass
    first_name="" — the tool returns the most-senior match and lists
    the others so you can decide whether to ask the user to clarify or
    just answer.

    first_name: the person's given name. Optional ("" is fine).
        Honorifics are ignored.
    surname: the person's surname only — no titles. Required.
    emotion: body language while looking up. Default 'think'.
    request_heartbeat: True (default) to keep the loop alive so you
        can speak via send_message_to_user after this. False halts
        the loop immediately — only set False if you really want to
        end the turn without saying anything to the user.
    """
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
            "instruction": (
                "A surname is required to search the directory. "
                "Ask the user via send_message_to_user for the "
                "person's surname."
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
    runs = await _udb_lookup_variants(variants)
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
            "instruction": (
                f"No staff named {surname_q!r} found in the directory "
                f"(tried Czech variants: {variants}). Tell the user "
                "and ask them to confirm or spell the surname."
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
        instruction = (
            "Public staff directory entry. In your next "
            "send_message_to_user, mention only the field the user "
            "asked for — phone / email / room / etc."
        )
    elif filter_note:
        instruction = (
            f"{filter_note}. Answer with the requested field for the "
            "picked person. If you suspect this is the wrong person, "
            "you may briefly mention the alternatives and ask which "
            "one the user meant."
        )
    else:
        instruction = (
            f"Multiple people share surname {chosen_variant!r}. Picked "
            f"the most senior. Other candidates: {alternatives}. If the "
            "user is asking about a specific person, briefly ask which "
            "one they meant; otherwise just answer with the requested "
            "field for the picked person."
        )

    response: dict[str, Any] = {
        "count": len(filtered),
        "matches": [best],
        "instruction": instruction,
    }
    if alternatives:
        response["alternatives"] = alternatives
    if filter_note:
        response["filter_note"] = filter_note
    return _heartbeat_or_none(response, request_heartbeat)


@function_tool(name="find_path_to_room")
async def find_path_to_room(
    context: RunContext,
    room: str,
    emotion: _Emotion = "think",
    request_heartbeat: bool = True,
) -> Any:
    """Get directions to a room in this building. The user is already
    standing with you at the main entrance.

    Call this when the user asks how to get to a room or where a
    room is.

    room: copy the user's room number VERBATIM. The user's "230" is
        "230" — not "23". Examples: '101', 'A-205', 'B-310'.
    emotion: body language while looking up the route. Default
        'think'; pick whatever fits the moment (e.g. 'happy' if
        the user asked enthusiastically).
    request_heartbeat: True (default) to continue the loop so you
        can read the directions via send_message_to_user.
    """
    del context
    room_norm = _normalize_room(room)
    print(
        f"  [tool] find_path_to_room({room_norm!r}, "
        f"emotion={emotion!r}, hb={request_heartbeat})"
    )
    _emit_tool_event("find_path_to_room", {
        "room": room_norm, "emotion": emotion,
        "request_heartbeat": request_heartbeat,
    })
    if emotion:
        asyncio.create_task(trigger_animation(emotion))

    info = ROOM_DIRECTIONS.get(room_norm)
    if info:
        directions = _render_curated(room_norm, info)
        floor = int(info.get("floor") or 0)
    else:
        floor = await asyncio.to_thread(_query_floor, room_norm)
        if floor is None:
            return _heartbeat_or_none({
                "error": "room_not_found",
                "room": room_norm,
                "instruction": (
                    f"Room {room_norm} was not found in Building E. "
                    "Tell the user via send_message_to_user and ask "
                    "them to confirm the room code."
                ),
            }, request_heartbeat)
        directions = _render_floor_only(room_norm, floor)

    return _heartbeat_or_none({
        "room": room_norm,
        "floor": floor,
        "directions": directions,
        "instruction": (
            "Read the `directions` field via send_message_to_user "
            "in plain prose, naturally. The user is already with you "
            "at the entrance — don't tell them to start there."
        ),
    }, request_heartbeat)


@function_tool(name="mensa_menu")
async def mensa_menu(
    context: RunContext,
    emotion: _Emotion = "think",
    request_heartbeat: bool = True,
) -> Any:
    """Look up what's on the menu at the nearby canteen.

    Returns every day currently published — typically this week and
    next week. Each day has a `dishes` list and each dish has a
    `category` tag like "soup", "main", "salad", "vegetarian".

    Call this when the user asks what they can eat, what's for
    lunch, what's on the menu, or about the canteen / mensa /
    menza / buffet.

    emotion: body language while fetching. Default 'think'.
    request_heartbeat: True (default) to continue and speak via
        send_message_to_user with the menu.
    """
    del context
    print(f"  [tool] mensa_menu(emotion={emotion!r}, hb={request_heartbeat})")
    _emit_tool_event("mensa_menu", {
        "emotion": emotion, "request_heartbeat": request_heartbeat,
    })
    if emotion:
        asyncio.create_task(trigger_animation(emotion))

    try:
        result = await asyncio.to_thread(fetch_mensa_menu)
    except Exception as exc:
        return _heartbeat_or_none(
            {"error": "mensa_fetch_failed", "message": str(exc)},
            request_heartbeat,
        )

    days = result.get("days") or []
    if not days:
        return _heartbeat_or_none({
            "canteen": result.get("canteen"),
            "days": [],
            "instruction": (
                "The menu has not been published yet. Tell the user "
                "via send_message_to_user that the canteen has not "
                "posted the menu, and offer to check again later."
            ),
        }, request_heartbeat)

    result["instruction"] = (
        "Match the user's question to the right day in `days`. In "
        "your next send_message_to_user, mention only 1 or 2 dishes "
        "— prefer main dishes over soups unless the user asked "
        "specifically. If the user asked for a specific category "
        "(soup, vegetarian, etc.), filter `dishes` by that category. "
        "Read the FULL list ONLY if the user explicitly asked for "
        "everything. Never read out the canteen's full name or any "
        "URLs."
    )
    return _heartbeat_or_none(result, request_heartbeat)


@function_tool(name="subject_schedule")
async def subject_schedule(
    context: RunContext,
    subject: str,
    activity: str = "",
    day: str = "",
    emotion: _Emotion = "think",
    request_heartbeat: bool = True,
) -> Any:
    """Look up the public timetable for a course / subject.

    Use this when the user asks when or where a subject's lecture,
    lab, exercise, or tutorial happens.

    The `subject` argument MUST be a short course code: 2 to 4
    letters with an optional single trailing digit (e.g. XYZ, AB,
    WXYZ, XYZ1, AB2). Course names or long codes like A1B23CDE are
    NOT accepted — ask the user via send_message_to_user for the
    short code.

    activity: SESSION-TYPE filter. Pass "lecture" / "exercise" /
    "laboratory" if the user named the kind, else "".
    day: optional weekday filter — empty, Monday, ..., Friday.

    Always read the `instruction` field of the result and follow it.

    subject: short intranet course code, 2 to 4 letters with optional digit.
    activity: session-type filter or "".
    day: optional weekday filter.
    emotion: body language while fetching. Default 'think'.
    request_heartbeat: True (default) to continue.
    """
    del context
    subject_q = (subject or "").strip()
    activity_q = (activity or "").strip()
    day_q = (day or "").strip()
    print(
        f"  [tool] subject_schedule({subject_q!r}, {activity_q!r}, "
        f"{day_q!r}, emotion={emotion!r}, hb={request_heartbeat})"
    )
    _emit_tool_event("subject_schedule", {
        "subject": subject_q, "activity": activity_q, "day": day_q,
        "emotion": emotion, "request_heartbeat": request_heartbeat,
    })
    if emotion:
        asyncio.create_task(trigger_animation(emotion))

    if not _SUBJECT_CODE.match(subject_q):
        return _heartbeat_or_none({
            "status": "invalid_code",
            "received": subject_q,
            "instruction": (
                "The `subject` argument must be a short intranet "
                "course code: 2 to 4 letters with an optional single "
                f"trailing digit. You passed '{subject_q}'. Ask the "
                "user via send_message_to_user for the correct code."
            ),
        }, request_heartbeat)

    try:
        result = await asyncio.to_thread(
            fetch_subject_schedule, subject_q.upper(), activity_q, day_q,
        )
    except Exception as exc:
        traceback.print_exc()
        return _heartbeat_or_none({
            "status": "error",
            "error": "subject_schedule_failed",
            "message": str(exc),
            "instruction": (
                "The timetable service errored. Apologise via "
                "send_message_to_user and ask the user to try again."
            ),
        }, request_heartbeat)

    status = result.get("status", "")
    resolution = result.get("resolution", "")
    slim: dict[str, Any] = {"status": status}

    if status == "not_found":
        slim["instruction"] = (
            f"No subject with code '{subject_q.upper()}' was found. "
            "Tell the user via send_message_to_user the code is "
            "unknown and ask them to double-check the short code."
        )
    elif status == "ambiguous":
        matches = result.get("matches") or []
        slim["candidates"] = [
            {"code": m.get("code"), "name": m.get("name")} for m in matches[:5]
        ]
        slim["instruction"] = (
            "Several subjects share that short code. Read 2–3 "
            "candidate names back to the user via "
            "send_message_to_user and ask which one they meant."
        )
    elif status == "ok":
        events = result.get("events") or []
        slim["count"] = len(events)
        slim["events"] = [
            {
                "activity": e.get("activity"),
                "day": e.get("day"),
                "start": e.get("start"),
                "room": e.get("room"),
                "teachers": e.get("teachers") or [],
            }
            for e in events
        ]
        if resolution == "multiple_codes_same_subject":
            slim["note"] = "multiple_codes_same_subject"
            slim["instruction"] = ""
        elif slim["count"] == 0:
            slim["instruction"] = (
                "Subject exists but no event matches the filters. "
                "Tell the user via send_message_to_user."
            )
        else:
            slim["instruction"] = (
                "In your send_message_to_user, mention day, start "
                "time (only the hour), room, and teachers. No end "
                "times, no week ranges unless the user asks."
            )
    else:
        slim["instruction"] = result.get("message", "")

    return _heartbeat_or_none(slim, request_heartbeat)


@function_tool(name="get_time")
async def get_time(
    context: RunContext,
    emotion: _Emotion = "think",
    request_heartbeat: bool = True,
) -> Any:
    """Return the current local time. Use only when the user
    explicitly asks what time it is.

    emotion: body language while checking the clock. Default
        'think'; override freely if a different mood fits.
    request_heartbeat: True (default) to continue.
    """
    del context
    print(f"  [tool] get_time(emotion={emotion!r}, hb={request_heartbeat})")
    _emit_tool_event("get_time", {
        "emotion": emotion, "request_heartbeat": request_heartbeat,
    })
    if emotion:
        asyncio.create_task(trigger_animation(emotion))
    now = datetime.now(ZoneInfo("Europe/Prague"))
    payload = {"time": now.strftime("%Y-%m-%d %H:%M %Z")}
    return _heartbeat_or_none(payload, request_heartbeat)


@function_tool(name="display_info")
async def display_info(
    context: RunContext,
    text: str,
    request_heartbeat: bool = True,
) -> Any:
    """Show a short value on Pepper's tablet so the user can read /
    copy it down. ONLY for values the user would plausibly want to
    WRITE DOWN — phone numbers, email addresses, room codes, URLs,
    specific dates and times.

    Do NOT use for things the user only listens to: greetings, prose
    answers, opinions, meal names, dish descriptions, subject names,
    person names without contact info, opening-hour sentences, etc.
    Words that are easy to hear once and remember should not go on
    the tablet.

    The card stays visible until the next user turn, and a later
    call replaces it. You don't need to clear it.

    This tool does NOT trigger any robot animation — that's why it
    has no `emotion` argument. Pick a body language separately when
    you call `send_message_to_user` afterwards.

    text: the value to display. Plain text, kept short (≤ ~80 chars
        works best). HTML is escaped automatically. Multi-line is
        OK — just use \\n.
    request_heartbeat: True (default) to continue the loop so you can
        speak via send_message_to_user after.
    """
    del context
    text_clean = (text or "").strip()
    print(
        f"  [tool] display_info(text={text_clean!r}, hb={request_heartbeat})"
    )
    _emit_tool_event("display_info", {
        "text": text_clean, "request_heartbeat": request_heartbeat,
    })

    if not text_clean:
        return _heartbeat_or_none({
            "error": "missing_text",
            "instruction": (
                "display_info needs a non-empty `text`. Skip this "
                "tool and just speak via send_message_to_user."
            ),
        }, request_heartbeat)

    try:
        status, _ = await asyncio.to_thread(post_tablet_info, text_clean)
    except Exception as exc:
        # Bridge unreachable / Pepper offline — log and continue. Not
        # being able to draw on the tablet must never break the spoken
        # reply, so we still let the loop continue.
        print(f"  [tool] display_info bridge error: {exc!r}")
        return _heartbeat_or_none({
            "ok": False,
            "error": "tablet_bridge_unreachable",
            "message": str(exc),
            "instruction": (
                "Tablet display unavailable. Speak the value normally "
                "via send_message_to_user."
            ),
        }, request_heartbeat)

    return _heartbeat_or_none({
        "ok": status == 200,
        "status": status,
        "displayed": text_clean,
        "instruction": (
            "Now call send_message_to_user with a short spoken reply. "
            "You don't need to read the displayed value out loud "
            "verbatim — the user can see it on the tablet."
        ),
    }, request_heartbeat)


@function_tool(name="query_search")
async def query_search(
    context: RunContext,
    query: str,
    emotion: _Emotion = "think",
    request_heartbeat: bool = True,
) -> Any:
    """Search the knowledge base.

    query: copy the user's factual question, 3-10 words.
    emotion: body language. Default 'think'.
    request_heartbeat: True (default) to continue.
    """
    del context
    query_text = (query or "").strip()
    print(
        f"  [tool] query_search({query_text!r}, "
        f"emotion={emotion!r}, hb={request_heartbeat})"
    )
    _emit_tool_event("query_search", {
        "query": query_text, "emotion": emotion,
        "request_heartbeat": request_heartbeat,
    })
    if emotion:
        asyncio.create_task(trigger_animation(emotion))

    if not query_text:
        return _heartbeat_or_none(
            {"error": "missing_query", "message": "query cannot be empty"},
            request_heartbeat,
        )

    await asyncio.to_thread(_ensure_weaviate_seeded_once)
    await asyncio.to_thread(post_led_state, "search_pulse")
    try:
        results = await asyncio.to_thread(
            search_vectors, query_text,
            QUERY_SEARCH_DEFAULT_LIMIT, WEAVIATE_HYBRID_ALPHA,
        )
    except Exception as exc:
        return _heartbeat_or_none(
            {"error": "query_search_failed", "message": str(exc)},
            request_heartbeat,
        )
    finally:
        await asyncio.to_thread(post_led_state, "idle")

    return _heartbeat_or_none({
        "query": query_text,
        "count": len(results),
        "results": [_agent_result(item) for item in results],
    }, request_heartbeat)


# Public surface — exported list for the agent constructor.
LIVEKIT_TOOLS_TOOLONLY = [
    send_message_to_user,
    find_path_to_room,
    lookup_person,
    get_time,
    mensa_menu,
    subject_schedule,
    display_info,
    # query_search left out by default — broad trigger; re-add when needed.
]
