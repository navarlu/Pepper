import asyncio
import base64
import json
import logging
import random
import re
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen  # still used by _post_animation

from livekit.agents import RunContext, function_tool

from .config import (
    AGENT_VERSION,
    ANIMATION_BRIDGE_URL,
    ANIMATION_GROUPS,
    ANIMATION_TOOL_ALIASES,
    ANIMATION_TOOL_ALLOWED,
    ANIMATION_TOOL_HTTP_TIMEOUT_SEC,
    ANIMATION_TOOL_MAX_NAME_CHARS,
    ENABLE_ANIMATION_TOOL,
    ENABLE_LOOK_AROUND_TOOL,
    ENABLE_QUERY_SEARCH,
    LOOK_AROUND_HTTP_TIMEOUT_SEC,
    LOOK_AROUND_VISION_BASE_URL,
    LOOK_AROUND_VISION_MAX_TOKENS,
    LOOK_AROUND_VISION_MODEL,
    LOOK_AROUND_VISION_PROMPT,
    LOOK_AROUND_VISION_TEMPERATURE,
    LOOK_AROUND_VISION_TIMEOUT_SEC,
    QUERY_SEARCH_DEFAULT_LIMIT,
    QUERY_SEARCH_MAX_LIMIT,
    WEAVIATE_HYBRID_ALPHA,
)
from .utils import search_vectors

logger = logging.getLogger("voice-agent")

def _agent_result(item: dict[str, Any]) -> dict[str, Any]:
    """Fields returned to the LLM agent."""
    return {
        "title": item.get("title"),
        "content": item.get("content"),
        "source": item.get("source"),
        "score": item.get("score"),
    }


def _fetch_camera_snapshot() -> bytes:
    """Blocking POST to bridge /camera/snapshot. Returns JPEG bytes or raises."""
    bridge_url = str(ANIMATION_BRIDGE_URL or "").rstrip("/")
    if not bridge_url:
        raise RuntimeError("animation_bridge_url_missing")
    endpoint = "{}/camera/snapshot".format(bridge_url)
    body = b'{"pause_awareness": true}'
    req = Request(endpoint, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=float(LOOK_AROUND_HTTP_TIMEOUT_SEC)) as response:
        status = int(getattr(response, "status", response.getcode()))
        content_type = response.headers.get("Content-Type", "")
        data = response.read()
    if status != 200:
        raise RuntimeError("snapshot_http_{}: {}".format(status, data[:200]))
    if not content_type.startswith("image/"):
        raise RuntimeError("snapshot_bad_content_type: {}".format(content_type))
    return data


def _describe_image_with_vl(jpeg_bytes: bytes, extra_purpose: str = "") -> str:
    """POST the JPEG to the side VL captioner and return a short text description.

    This isolates vision reasoning from the main LLM so the main chat model can
    stay text-only (avoids Qwen2.5-VL tool-calling chat-template issues).
    """
    base = str(LOOK_AROUND_VISION_BASE_URL or "").rstrip("/")
    if not base:
        raise RuntimeError("look_around_vision_base_url_missing")
    endpoint = "{}/chat/completions".format(base)

    b64 = base64.b64encode(jpeg_bytes).decode("ascii")
    data_url = "data:image/jpeg;base64," + b64

    user_text = LOOK_AROUND_VISION_PROMPT
    if extra_purpose:
        user_text = user_text + "\nFocus hint: " + extra_purpose

    payload = {
        "model": LOOK_AROUND_VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": user_text},
                ],
            }
        ],
        "max_tokens": int(LOOK_AROUND_VISION_MAX_TOKENS),
        "temperature": float(LOOK_AROUND_VISION_TEMPERATURE),
    }
    req = Request(endpoint, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=float(LOOK_AROUND_VISION_TIMEOUT_SEC)) as response:
        raw = response.read()
    data = json.loads(raw)
    try:
        return str(data["choices"][0]["message"]["content"]).strip()
    except Exception as exc:
        raise RuntimeError("vl_unparsable_response: {}".format(str(data)[:300])) from exc


def _post_led_state(mode: str) -> None:
    """Best-effort POST /leds/state. Logs on failure, never raises."""
    bridge_url = str(ANIMATION_BRIDGE_URL or "").rstrip("/")
    if not bridge_url:
        return
    endpoint = "{}/leds/state".format(bridge_url)
    body = json.dumps({"mode": mode}).encode("utf-8")
    req = Request(endpoint, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=1.5) as response:
            _ = response.read()
    except Exception as exc:
        logger.debug("led_state_post_failed mode=%s error=%s", mode, exc)


def _post_animation(animation_name: str) -> tuple[int, str]:
    bridge_url = str(ANIMATION_BRIDGE_URL or "").rstrip("/")
    if not bridge_url:
        raise RuntimeError("animation_bridge_url_missing")

    endpoint = "{}/animation/{}".format(
        bridge_url,
        quote(animation_name, safe=""),
    )
    req = Request(endpoint, data=b"", method="POST")
    try:
        with urlopen(req, timeout=float(ANIMATION_TOOL_HTTP_TIMEOUT_SEC)) as response:
            status = int(getattr(response, "status", response.getcode()))
            body = response.read().decode("utf-8", "ignore")
            return status, body
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        return int(exc.code), body
    except URLError as exc:
        raise RuntimeError("animation_bridge_unreachable: {}".format(exc)) from exc


def _pick_from_group(group_name: str) -> str:
    """Pick a random animation variant from a group."""
    variants = ANIMATION_GROUPS.get(group_name, [])
    if not variants:
        return ""
    return random.choice(variants)


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
    await asyncio.to_thread(_push_tool_transcript, "play_animation({})".format(animation_name))
    try:
        status, body = await asyncio.to_thread(_post_animation, animation_name)
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
    """Best-effort dispatch for code-driven animations."""
    resolved = _normalize_animation_name(animation_name)
    if not resolved:
        logger.warning("trigger_animation_failed animation=%s error=unknown_animation", animation_name)
        return False
    logger.info("trigger_animation animation=%s resolved=%s", animation_name, resolved)
    await _dispatch_animation(resolved)
    return True


# Building E — FEL ČVUT, Karlovo náměstí
# Room directions data. To update, just edit this dict directly.
BUILDING_ROOMS: dict[str, dict[str, dict]] = {
    "ground_floor": {
        "23": {"directions": "go left, room 23 is in the middle of the corridor on the left side"},
        "24": {"directions": "go left, room 24 is in the middle of the corridor on the left side"},
        "26": {"directions": "go left, room 26 is at the end of the corridor"},
    },
    "1st_floor": {
        "107": {"directions": "go up the stairs, and when you see toilet go there and then right, room 107 is at the end of the corridor"},
        "112": {"directions": ""},
        "125": {"directions": "go up the stairs, turn on the landing and continue up, room 125 will be right in front of you"},
        "126": {"directions": "go up the stairs, turn on the landing and continue up, room 126 will be right in front of you"},
        "127": {"directions": "go up the stairs, turn on the landing and continue up, then turn right, room 127 will be on left side"},
        "128": {"directions": "go up the stairs, turn on the landing and continue up, then turn right, room 128 will be on left side"},
        "129": {"directions": "go up the stairs, turn on the landing and continue up, then turn right, room 129 will be on left side"},
        "130": {"directions": "go up the stairs, turn on the landing and continue up, then turn right, room 130 will be on left side"},
        "132": {"directions": "go up the stairs, turn on the landing and continue up, then turn right, room 132 will be at the end of the corridor"},
    },
    "2nd_floor": {
        "230": {"directions": "go up to the second floor, turn right, room 230 will be at the end of the corridor"},
    },
    "3rd_floor": {
        "301": {"directions": ""},
        "307": {"directions": ""},
        "310": {"directions": ""},
        "311": {"directions": ""},
        "327": {"directions": ""},
        "328": {"directions": ""},
        "329": {"directions": ""},
        "331": {"directions": ""},
        "332": {"directions": ""},
        "333a": {"directions": ""},
        "333b": {"directions": ""},
        "333c": {"directions": ""},
        "333d": {"directions": ""},
        "396": {"directions": ""},
    },
}


_ROOM_NUMBER_RE = re.compile(r'\b(\d{2,4}[a-zA-Z]?)\b')
_ROOM_KEYWORDS = [
    "room", "direction", "where", "how to get", "find", "navigate",
    "located", "location", "místnost", "kam", "kudy",
]


def _try_room_lookup(query: str) -> dict | None:
    """If query looks like a room/directions question, look up from BUILDING_ROOMS.

    Returns a result dict if a room number was found in the query AND the
    query contains a room-related keyword.  Returns None otherwise so the
    caller can fall through to the normal Weaviate search.
    """
    match = _ROOM_NUMBER_RE.search(query)
    if not match:
        return None
    query_lower = query.lower()
    if not any(kw in query_lower for kw in _ROOM_KEYWORDS):
        return None

    room_number = match.group(1)

    for floor_id, rooms_on_floor in BUILDING_ROOMS.items():
        if room_number in rooms_on_floor:
            room = rooms_on_floor[room_number]
            directions = (room.get("directions") or "").strip()
            name = (room.get("name") or "").strip()
            if not directions:
                return {
                    "type": "directions",
                    "error": "no_directions",
                    "message": f"Room {room_number} is known but directions are not filled in yet.",
                }
            result = {
                "type": "directions",
                "room": room_number,
                "floor": floor_id,
                "directions": directions,
            }
            if name:
                result["name"] = name
            return result

    return {
        "type": "directions",
        "error": "room_not_found",
        "message": f"Room {room_number} is not in my map. I only know Building E rooms.",
    }


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
        room_result = await asyncio.to_thread(_try_room_lookup, query_text)
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

        await asyncio.to_thread(_post_led_state, "search_pulse")
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
            await asyncio.to_thread(_post_led_state, "idle")

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
            jpeg_bytes = await asyncio.to_thread(_fetch_camera_snapshot)
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
            description = await asyncio.to_thread(_describe_image_with_vl, jpeg_bytes, reason)
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
    # query_search via _try_room_lookup(), so a dedicated tool is not needed.
    tools: list[Any] = [query_search, play_animation]
    if ENABLE_LOOK_AROUND_TOOL:
        tools.append(look_around)

    logger.info(
        "build_tools version=%s agent_mode=%s tool_count=%d names=%s",
        AGENT_VERSION, agent_mode, len(tools),
        [getattr(t, "name", str(t)) for t in tools],
    )
    return tools
