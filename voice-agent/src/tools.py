"""Tool definitions exposed to the LLM.

Three tools, all built by `build_tools(agent_mode)`:

  - `query_search(query)` — RAG over the FEL knowledge base in
    Weaviate, with a deterministic fast path for room-number
    questions (`rooms.try_room_lookup`).
  - `play_animation(animation)` — pick a Pepper gesture and POST it to
    the robot bridge. Two thin wrappers with different docstrings for
    the OpenAI vs local (Qwen) model quirks, sharing one
    implementation.
  - `look_around(purpose)` — grab a JPEG snapshot from Pepper's top
    camera, caption it with a side VL model, return text. Gated by
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
from typing import Any

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
    ENABLE_QUERY_SEARCH,
    QUERY_SEARCH_DEFAULT_LIMIT,
    WEAVIATE_HYBRID_ALPHA,
)
from .rag import search_vectors
from .rooms import try_room_lookup

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
        """Search the FEL knowledge base. Also returns room locations and directions."""
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

        # Check if this is a room/directions query — route to building map.
        room_result = await asyncio.to_thread(try_room_lookup, query_text)
        if room_result is not None:
            logger.info("query_search routed_to=room_lookup query=%s result_type=%s",
                        query_text, room_result.get("type", "directions"))
            await asyncio.to_thread(_push_tool_transcript, "query_search({}) [room]".format(query_text))
            t0 = time.monotonic()
            duration_ms = (time.monotonic() - t0) * 1000
            await asyncio.to_thread(
                _post_tool_event, "query_search",
                {"query": query_text, "routed": "room_lookup"},
                room_result, duration_ms,
            )
            return json.dumps(room_result, ensure_ascii=False)

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

        if agent_mode == "local":
            # Local mode: return None so the LiveKit SDK does NOT re-call the LLM.
            # Qwen generates text + tool_call in the same response, so text is
            # already being sent to TTS. Returning data here would trigger a
            # second LLM call (livekit/agents#4554).
            result_payload = {"body_state": "ready", "posture": resolved}
            await asyncio.to_thread(
                _post_tool_event, "play_animation",
                {"animation": animation_name, "resolved": resolved},
                result_payload, duration_ms,
            )
            return None
        else:
            result_payload = {
                "ok": True,
                "status": "queued",
                "animation": resolved,
            }
            await asyncio.to_thread(
                _post_tool_event, "play_animation",
                {"animation": animation_name, "resolved": resolved},
                result_payload, duration_ms,
            )
            return json.dumps(result_payload, ensure_ascii=False)
    # endregion

    # OpenAI mode: side-effect description (works with larger models)
    @function_tool(name="play_animation")
    async def play_animation_openai(
        context: RunContext,
        animation: str,
    ) -> str:
        """Move Pepper's robot body. Call exactly once per reply.

        animation must be one of: greeting, bow, explain, happy, thinking, dont_know

        Use greeting when user says hello. Use explain when giving information.
        Use bow for thanks or goodbye. Use happy for positive replies.
        Use thinking when searching. Use dont_know when unsure.
        """
        del context
        return await _play_animation_impl(animation)

    # Local mode: description framed as returning needed data (Qwen 7B needs this)
    @function_tool(name="play_animation")
    async def play_animation_local(
        context: RunContext,
        animation: str,
    ) -> str:
        """Check and set the robot body posture. Returns the current body state which you need before speaking. animation must be one of: greeting, bow, explain, happy, thinking, dont_know"""
        del context
        return await _play_animation_impl(animation)

    play_animation = play_animation_local if agent_mode == "local" else play_animation_openai

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

    # Unified 2-tool set for both modes. Room directions are handled inside
    # query_search via rooms.try_room_lookup(), so a dedicated tool is not needed.
    tools: list[Any] = [query_search, play_animation]
    if ENABLE_LOOK_AROUND_TOOL:
        tools.append(look_around)

    logger.info(
        "build_tools version=%s agent_mode=%s tool_count=%d names=%s",
        AGENT_VERSION, agent_mode, len(tools),
        [getattr(t, "name", str(t)) for t in tools],
    )
    return tools
