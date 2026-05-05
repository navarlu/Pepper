"""Tool definitions exposed to the LLM.

Tools, all built by `build_tools(agent_mode)`:

  - `query_search(query)` — RAG over the FEL knowledge base in
    Weaviate. Gated by `ENABLE_QUERY_SEARCH` (default off in local
    mode after the benchmark validation: its broad trigger cascaded
    on bare greetings).
  - `lookup_person(first_name, surname)` — UDB staff directory lookup
    with first-name disambiguation + most-credentialed pick. Gated
    by `ENABLE_LOOKUP_PERSON_TOOL`.
  - `find_path_to_room(room)` — curated Building E directions with a
    FelSight floor-only fallback for uncurated rooms.
  - `mensa_menu(day)` — Charles Square food counter weekly menu.
  - `subject_schedule(subject, activity, day)` — public timetable.
  - `play_animation(name)` — Pepper body gesture, off by default in
    local mode (`ENABLE_ANIMATION_TOOL=False`); the
    `AnimationDirector` is the only path to animations there.
  - `look_around(purpose)` — JPEG + VL caption. Gated by
    `ENABLE_LOOK_AROUND_TOOL`.

All HTTP plumbing lives in `bridge_client`; this module is purely
tool logic + a tool-event hook used by the debug CLI.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from typing import Any, Literal

from livekit.agents import RunContext, function_tool

from .bridge_client import (
    describe_image_with_vl,
    fetch_camera_snapshot,
    post_animation,
    post_led_state,
)
from .config import (
    AGENT_VERSION,
    ANIMATION_GROUPS,
    ANIMATION_TOOL_ALIASES,
    ANIMATION_TOOL_ALLOWED,
    ANIMATION_TOOL_MAX_NAME_CHARS,
    ENABLE_ANIMATION_TOOL,
    ENABLE_LOOK_AROUND_TOOL,
    ENABLE_LOOKUP_PERSON_TOOL,
    ENABLE_QUERY_SEARCH,
    QUERY_SEARCH_DEFAULT_LIMIT,
    WEAVIATE_HYBRID_ALPHA,
)
from ._person_helpers import find_person
from ._room_directions import compute_room_directions
from .rag import search_vectors
from .mensa import fetch_mensa_menu
from .timetable import fetch_subject_schedule

logger = logging.getLogger("voice-agent")

def _agent_result(item: dict[str, Any]) -> dict[str, Any]:
    """Shrink a Weaviate search hit to the fields the LLM actually reads."""
    return {
        "title": item.get("title"),
        "content": item.get("content"),
        "source": item.get("source"),
        "score": item.get("score"),
    }


def _pick_from_group(group_name: str) -> str:
    """Pick a random animation variant from a group."""
    variants = ANIMATION_GROUPS.get(group_name, [])
    if not variants:
        return ""
    return random.choice(variants)


# region: normalize_animation_name
def _normalize_animation_name(raw_name: str) -> str:
    """Resolve an animation name to a concrete Pepper animation key.

    Accepts:
    - Group names (e.g. "greeting") → picks a random variant from the group.
    - Natural-language aliases (e.g. "hello") → mapped to a group, then randomized.
    - Direct animation keys (e.g. "Hey_1") → passed through as-is.
    """
    clean = str(raw_name or "").strip()
    if not clean:
        return ""

    # 1. Direct match against a group name (case-insensitive).
    clean_lower = clean.lower().replace("-", "_").replace(" ", "_")
    clean_lower = "".join(ch for ch in clean_lower if ch.isalnum() or ch == "_")

    if clean_lower in ANIMATION_GROUPS:
        return _pick_from_group(clean_lower)

    # 2. Alias lookup → resolves to a group name.
    mapped_group = ANIMATION_TOOL_ALIASES.get(clean_lower)
    if mapped_group and mapped_group in ANIMATION_GROUPS:
        return _pick_from_group(mapped_group)

    # 3. Direct animation key (exact or case-insensitive).
    if clean in ANIMATION_TOOL_ALLOWED:
        return clean
    for key in ANIMATION_TOOL_ALLOWED:
        if key.lower() == clean.lower():
            return key

    return ""
# endregion


def _push_tool_transcript(text: str) -> None:
    """Log a tool-call transcript entry to terminal."""
    logger.info("tool_transcript text=%s", text[:120])


_external_tool_listener: Any = None


def set_tool_event_listener(listener) -> None:
    """Register a callback invoked on every tool call (used by debug bridge)."""
    global _external_tool_listener
    _external_tool_listener = listener


def _post_tool_event(
    tool_name: str,
    args: dict[str, Any],
    result: Any,
    duration_ms: float,
    error: str | None = None,
) -> None:
    """Log a structured tool-call event to terminal and notify external listener."""
    logger.info(
        "tool_call tool=%s args=%s duration_ms=%.0f error=%s",
        tool_name,
        json.dumps(args, default=str)[:150],
        duration_ms,
        error,
    )
    if _external_tool_listener is not None:
        try:
            _external_tool_listener(tool_name, args, result, duration_ms, error)
        except Exception as exc:
            logger.debug("tool_event_listener_failed err=%s", exc)


async def _dispatch_animation(animation_name: str) -> None:
    """Fire-and-forget POST to the bridge; log result, swallow errors."""
    await asyncio.to_thread(_push_tool_transcript, f"play_animation({animation_name})")
    try:
        status, body = await asyncio.to_thread(post_animation, animation_name)
        if 200 <= status < 300:
            logger.info("play_animation_dispatched animation=%s status=%s", animation_name, status)
        else:
            logger.warning(
                "play_animation_failed animation=%s status=%s body=%s",
                animation_name,
                status,
                (body or "")[:220],
            )
    except Exception as exc:
        logger.warning("play_animation_failed animation=%s error=%s", animation_name, str(exc))


def _animation_success_payload(resolved: str) -> dict[str, Any]:
    """LLM-facing result for a valid animation request.

    Bridge/Pepper failures are logged separately. The model should not spend a
    conversational turn explaining robot-body transport issues to the user.
    """
    return {
        "ok": True,
        "status": "queued",
        "animation": resolved,
    }


async def trigger_animation(animation_name: str) -> bool:
    """Public entry point for code-driven animations (not a tool call).

    Used when the agent code — not the LLM — wants to fire a gesture
    (e.g. a greeting animation at startup). Resolves the name the same
    way `play_animation` does and dispatches. Returns False if the
    name couldn't be resolved.
    """
    resolved = _normalize_animation_name(animation_name)
    if not resolved:
        logger.warning("trigger_animation_failed animation=%s error=unknown_animation", animation_name)
        return False
    logger.info("trigger_animation animation=%s resolved=%s", animation_name, resolved)
    await _dispatch_animation(resolved)
    return True


def build_tools(agent_mode: str) -> list[Any]:
    @function_tool
    async def query_search(
        context: RunContext,
        query: str,
    ) -> str:
        """Search the FEL knowledge base for specific factual questions about
        programmes, courses, departments, schedules, rules, the campus, or
        the building. Returns short result snippets — quote them in your reply.

        Only call this when the user has actually asked a concrete factual
        question about FEL. Never call it for greetings, chit-chat, personal
        opinions, identity questions, arithmetic, jokes, or anything not
        about FEL specifics.

        query: paraphrase of what the user just asked, in English.
        """
        del context

        if not ENABLE_QUERY_SEARCH:
            return json.dumps(
                {"error": "query_search_disabled"},
                ensure_ascii=False,
            )

        query_text = str(query or "").strip()
        if not query_text:
            return json.dumps(
                {"error": "missing_query", "message": "query nesmi byt prazdny"},
                ensure_ascii=False,
            )

        # Room-number routing was removed when src/rooms.py was retired
        # in favour of the dedicated `find_path_to_room` tool.

        rt_alpha = WEAVIATE_HYBRID_ALPHA
        safe_limit = QUERY_SEARCH_DEFAULT_LIMIT
        logger.info("query_search query=%s limit=%s alpha=%s", query_text, safe_limit, rt_alpha)
        await asyncio.to_thread(_push_tool_transcript, "query_search({})".format(query_text))

        await asyncio.to_thread(post_led_state, "search_pulse")
        t0 = time.monotonic()
        try:
            try:
                results = await asyncio.to_thread(search_vectors, query_text, safe_limit, rt_alpha)
                duration_ms = (time.monotonic() - t0) * 1000
                agent_results = [_agent_result(item) for item in results]
                payload = {
                    "query": query_text,
                    "count": len(results),
                    "results": agent_results,
                }
                logger.info(
                    "query_search_done query=%s results=%d duration_ms=%.1f",
                    query_text, len(results), duration_ms,
                )
                await asyncio.to_thread(
                    _post_tool_event, "query_search",
                    {"query": query_text, "limit": safe_limit, "alpha": rt_alpha},
                    payload, duration_ms,
                )
                return json.dumps(payload, ensure_ascii=False)
            except Exception as exc:
                duration_ms = (time.monotonic() - t0) * 1000
                logger.exception("query_search_failed error=%s", str(exc))
                await asyncio.to_thread(
                    _post_tool_event, "query_search",
                    {"query": query_text, "limit": safe_limit, "alpha": rt_alpha},
                    None, duration_ms, error=str(exc),
                )
                return json.dumps(
                    {
                        "error": "query_search_failed",
                        "message": str(exc),
                    },
                    ensure_ascii=False,
                )
        finally:
            await asyncio.to_thread(post_led_state, "idle")

    # region: play_animation_impl
    async def _play_animation_impl(animation: str) -> str:
        """Shared implementation for play_animation (both modes)."""
        t0 = time.monotonic()

        if not ENABLE_ANIMATION_TOOL:
            return json.dumps(
                {"error": "play_animation_disabled"},
                ensure_ascii=False,
            )

        animation_name = str(animation or "").strip()
        if not animation_name:
            return json.dumps(
                {"error": "missing_animation", "message": "animation name cannot be empty"},
                ensure_ascii=False,
            )
        if len(animation_name) > ANIMATION_TOOL_MAX_NAME_CHARS:
            return json.dumps(
                {
                    "error": "animation_name_too_long",
                    "max_chars": int(ANIMATION_TOOL_MAX_NAME_CHARS),
                },
                ensure_ascii=False,
            )

        resolved = _normalize_animation_name(animation_name)
        allowed = list(ANIMATION_GROUPS.keys())
        error_message = "Use one of the allowed animation group names."

        if not resolved:
            result_payload = {
                "error": "unknown_animation",
                "message": error_message,
                "allowed": allowed,
            }
            duration_ms = (time.monotonic() - t0) * 1000
            await asyncio.to_thread(
                _post_tool_event, "play_animation",
                {"animation": animation_name}, result_payload, duration_ms,
                error="unknown_animation",
            )
            return json.dumps(result_payload, ensure_ascii=False)

        logger.info("play_animation_queued animation=%s resolved=%s", animation_name, resolved)
        asyncio.create_task(_dispatch_animation(resolved))

        duration_ms = (time.monotonic() - t0) * 1000

        result_payload = _animation_success_payload(resolved)
        await asyncio.to_thread(
            _post_tool_event, "play_animation",
            {"animation": animation_name, "resolved": resolved},
            result_payload, duration_ms,
        )
        return json.dumps(result_payload, ensure_ascii=False)
    # endregion

    # Single registration name `play_animation` for both modes — works with
    # Llama 3.1 (llama3_json parser) and OpenAI Realtime. Qwen 2.5 needed
    # the `play_pose` workaround due to hermes-parser token leakage; that
    # workaround was dropped when we moved to Llama 3.1 8B AWQ. See
    # voice-agent/tests/local_llm_benchmark/ for validation.
    @function_tool(name="play_animation")
    async def play_animation(
        context: RunContext,
        name: Literal[
            "greeting", "bow", "explain", "happy", "thinking", "dont_know",
        ],
    ) -> str:
        """Trigger a Pepper gesture in parallel with your reasoning.

        Returns "OK" instantly and does not affect your answer. Call this
        at most once per user message, as your first action; once called
        for the current message, do not call again — proceed to the next
        tool or to your spoken reply.

        Pick the group that matches the meaning of your reply:
          - greeting:  hello/welcome
          - bow:       thanks, goodbye
          - explain:   factual/informational answer
          - happy:     enthusiastic affirmation
          - thinking:  clarifying question or expressing uncertainty
          - dont_know: apology or 'couldn't find'

        name: one of greeting, bow, explain, happy, thinking, dont_know.
        """
        del context
        return await _play_animation_impl(name)

    @function_tool(name="lookup_person")
    async def lookup_person(
        context: RunContext,
        first_name: str = "",
        surname: str = "",
    ) -> str:
        """Look up a person's contact info (phone, email, room) in the
        public staff directory.

        Both `first_name` and `surname` are required. If the user gave
        only a surname or only a first name, ask them in conversation
        for the missing part BEFORE calling this tool.

        The schema accepts empty strings only so a misfire doesn't
        crash the agent — when either arg is empty, the tool returns
        `error: "missing_first_name"` with an instruction telling you
        to ask the user for the missing part.

        first_name: the person's given name. Required.
        surname: the person's surname only — no titles. Required.
        """
        del context
        if not ENABLE_LOOKUP_PERSON_TOOL:
            return json.dumps({"error": "lookup_person_disabled"}, ensure_ascii=False)

        first_q = str(first_name or "").strip()
        surname_q = str(surname or "").strip()
        t0 = time.monotonic()
        await asyncio.to_thread(
            _push_tool_transcript,
            f"lookup_person(first_name={first_q!r}, surname={surname_q!r})",
        )
        result = await find_person(first_q, surname_q)
        duration_ms = (time.monotonic() - t0) * 1000

        # Pull a short summary for the structured tool log.
        if "error" in result:
            log_status = result["error"]
        else:
            picked = (result.get("matches") or [{}])[0].get("name", "?")
            log_status = f"ok picked={picked!r}"
        logger.info(
            "lookup_person_done first=%r surname=%r status=%s duration_ms=%.0f",
            first_q, surname_q, log_status, duration_ms,
        )
        await asyncio.to_thread(
            _post_tool_event, "lookup_person",
            {"first_name": first_q, "surname": surname_q},
            result, duration_ms,
            error=result.get("error") if "error" in result else None,
        )
        return json.dumps(result, ensure_ascii=False)

    @function_tool(name="find_path_to_room")
    async def find_path_to_room(
        context: RunContext,
        room: str,
    ) -> str:
        """Get directions to a room in Building E. The user is already
        standing with you at the main entrance.

        Call this when the user asks how to get to a room or where a
        room is.

        room: copy the user's room number VERBATIM, including every
            digit and any trailing zero. The user's "230" is "230",
            not "23". Examples: '230', 'E-230', '107', 'E-107',
            'S-109'.
        """
        del context
        room_q = str(room or "").strip()
        t0 = time.monotonic()
        await asyncio.to_thread(_push_tool_transcript, f"find_path_to_room({room_q!r})")
        if not room_q:
            payload = {
                "error": "missing_room",
                "instruction": (
                    "Ask the user which room they need directions to."
                ),
            }
            duration_ms = (time.monotonic() - t0) * 1000
            await asyncio.to_thread(
                _post_tool_event, "find_path_to_room",
                {"room": room_q}, payload, duration_ms, error="missing_room",
            )
            return json.dumps(payload, ensure_ascii=False)

        result = await compute_room_directions(room_q)
        duration_ms = (time.monotonic() - t0) * 1000
        logger.info(
            "find_path_to_room_done room=%r resolved=%r floor=%s duration_ms=%.0f",
            room_q, result.get("room"), result.get("floor"), duration_ms,
        )
        await asyncio.to_thread(
            _post_tool_event, "find_path_to_room",
            {"room": room_q}, result, duration_ms,
            error=result.get("error") if "error" in result else None,
        )
        return json.dumps(result, ensure_ascii=False)

    @function_tool
    async def mensa_menu(
        context: RunContext,
        day: str = "",
    ) -> str:
        """Look up what meals are available at the Charles Square CTU food counter.

        Use this when the user asks what they can eat, buy for lunch, or what is
        on the menu at the mensa/menza/canteen. The optional day can be empty,
        today, tomorrow, an ISO date like 2026-04-30, or a weekday. The result is
        in English and includes suggested_meals; in your spoken reply, mention
        only one or two meals unless the user asks for the full menu.

        day: optional day filter.
        """
        del context
        requested_day = str(day or "").strip()
        t0 = time.monotonic()
        await asyncio.to_thread(_push_tool_transcript, f"mensa_menu({requested_day or '-'})")
        try:
            result = await asyncio.to_thread(fetch_mensa_menu, requested_day)
        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            logger.warning("mensa_menu_failed day=%s error=%s", requested_day, exc)
            payload = {
                "status": "error",
                "error": "mensa_fetch_failed",
                "message": str(exc),
            }
            await asyncio.to_thread(
                _post_tool_event,
                "mensa_menu",
                {"day": requested_day},
                payload,
                duration_ms,
                error=str(exc),
            )
            return json.dumps(payload, ensure_ascii=False)

        duration_ms = (time.monotonic() - t0) * 1000
        await asyncio.to_thread(
            _post_tool_event,
            "mensa_menu",
            {"day": requested_day},
            result,
            duration_ms,
        )
        return json.dumps(result, ensure_ascii=False)

    @function_tool
    async def subject_schedule(
        context: RunContext,
        subject: str,
        activity: str = "",
        day: str = "",
    ) -> str:
        """Look up the public timetable for a FEE subject.

        Use this when the user asks when or where a subject's lecture, lab,
        exercise, or tutorial starts. Prefer passing the subject code if the
        user says one, for example B3B35ARI1. activity can be empty, lecture,
        laboratory, or exercise. day can be empty, Monday, Tuesday, Wednesday,
        Thursday, or Friday. If the result contains
        resolution="multiple_codes_same_subject", answer using those shared
        events and mention which subject codes were found. If the result is
        ambiguous without shared events, ask for the subject code.

        subject: subject code or subject name.
        activity: optional event type filter.
        day: optional weekday filter.
        """
        del context
        subject_query = str(subject or "").strip()
        activity_query = str(activity or "").strip()
        day_query = str(day or "").strip()
        t0 = time.monotonic()
        await asyncio.to_thread(
            _push_tool_transcript,
            f"subject_schedule({subject_query or '-'}, {activity_query or '-'}, {day_query or '-'})",
        )
        try:
            result = await asyncio.to_thread(
                fetch_subject_schedule,
                subject_query,
                activity_query,
                day_query,
            )
        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            logger.warning(
                "subject_schedule_failed subject=%s activity=%s day=%s error=%s",
                subject_query, activity_query, day_query, exc,
            )
            payload = {
                "status": "error",
                "error": "subject_schedule_failed",
                "message": str(exc),
            }
            await asyncio.to_thread(
                _post_tool_event,
                "subject_schedule",
                {"subject": subject_query, "activity": activity_query, "day": day_query},
                payload,
                duration_ms,
                error=str(exc),
            )
            return json.dumps(payload, ensure_ascii=False)

        duration_ms = (time.monotonic() - t0) * 1000
        await asyncio.to_thread(
            _post_tool_event,
            "subject_schedule",
            {"subject": subject_query, "activity": activity_query, "day": day_query},
            result,
            duration_ms,
        )
        return json.dumps(result, ensure_ascii=False)

    @function_tool
    async def look_around(
        context: RunContext,
        purpose: str = "",
    ) -> str:
        """Check what Pepper's camera currently sees. Returns a short text
        description of the scene which you need before speaking a reply
        that mentions the surroundings, people, objects, colours, or signs.

        After receiving the description, you MUST produce a spoken reply
        to the user in the same turn — never end a turn with only this
        tool call and no words.

        purpose: short free-text describing WHY you are looking (e.g.
            "identify the person in front of me", "count the chairs",
            "read the sign"). Forwarded to the describer as a focus hint.
        """
        del context
        if not ENABLE_LOOK_AROUND_TOOL:
            return json.dumps({"error": "look_around_disabled"}, ensure_ascii=False)

        reason = str(purpose or "").strip()
        t0 = time.monotonic()
        await asyncio.to_thread(_push_tool_transcript, "look_around({})".format(reason or "-"))

        try:
            jpeg_bytes = await asyncio.to_thread(fetch_camera_snapshot)
        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            logger.warning("look_around_fetch_failed error=%s", exc)
            await asyncio.to_thread(
                _post_tool_event, "look_around",
                {"purpose": reason}, None, duration_ms, error=str(exc),
            )
            return json.dumps(
                {"error": "camera_unavailable", "message": str(exc)},
                ensure_ascii=False,
            )

        image_kb = len(jpeg_bytes) // 1024
        snapshot_ms = (time.monotonic() - t0) * 1000

        try:
            description = await asyncio.to_thread(describe_image_with_vl, jpeg_bytes, reason)
        except Exception as exc:
            duration_ms = (time.monotonic() - t0) * 1000
            logger.warning("look_around_describe_failed error=%s", exc)
            await asyncio.to_thread(
                _post_tool_event, "look_around",
                {"purpose": reason, "snapshot_ms": round(snapshot_ms, 1)},
                None, duration_ms, error=str(exc),
            )
            return json.dumps(
                {"error": "describer_failed", "message": str(exc)},
                ensure_ascii=False,
            )

        total_ms = (time.monotonic() - t0) * 1000
        describe_ms = total_ms - snapshot_ms
        logger.info(
            "look_around_done purpose=%s bytes=%dKB snapshot_ms=%.0f describe_ms=%.0f total_ms=%.0f",
            reason, image_kb, snapshot_ms, describe_ms, total_ms,
        )
        result_payload = {
            "ok": True,
            "description": description,
            "image_size_kb": image_kb,
            "snapshot_ms": round(snapshot_ms, 1),
            "describe_ms": round(describe_ms, 1),
        }
        await asyncio.to_thread(
            _post_tool_event, "look_around",
            {"purpose": reason}, result_payload, total_ms,
        )

        # Return plain text, not JSON. Qwen 7B skims JSON keys but reads
        # flat text aggressively — this makes the model actually use the
        # description in its spoken reply.
        return (
            "CAMERA VIEW: " + description + "\n\n"
            "Reply to the user now in this same turn. Mention specific "
            "details from the CAMERA VIEW above. Do not end the turn silent."
        )

    # Room directions are now a dedicated tool (find_path_to_room).
    # query_search is gated by ENABLE_QUERY_SEARCH (off after the
    # benchmark cascade fix). play_animation is gated separately:
    # local mode uses the AnimationDirector (no LLM-visible tool),
    # openai mode keeps the LLM-driven tool until we have a
    # Realtime-compatible director.
    tools: list[Any] = [find_path_to_room, mensa_menu, subject_schedule]
    if ENABLE_QUERY_SEARCH:
        tools.append(query_search)
    if ENABLE_ANIMATION_TOOL or agent_mode != "local":
        tools.append(play_animation)
    if ENABLE_LOOKUP_PERSON_TOOL:
        tools.append(lookup_person)
    if ENABLE_LOOK_AROUND_TOOL:
        tools.append(look_around)

    logger.info(
        "build_tools version=%s agent_mode=%s tool_count=%d names=%s",
        AGENT_VERSION, agent_mode, len(tools),
        [getattr(t, "name", str(t)) for t in tools],
    )
    return tools
