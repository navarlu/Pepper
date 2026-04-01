import asyncio
import json
import logging
import random
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from livekit.agents import RunContext, function_tool

from .config import (
    ANIMATION_BRIDGE_URL,
    ANIMATION_GROUPS,
    ANIMATION_TOOL_ALIASES,
    ANIMATION_TOOL_ALLOWED,
    ANIMATION_TOOL_HTTP_TIMEOUT_SEC,
    ANIMATION_TOOL_MAX_NAME_CHARS,
    DEV_CONSOLE_URL,
    ENABLE_ANIMATION_TOOL,
    ENABLE_QUERY_SEARCH,
    QUERY_SEARCH_DEFAULT_LIMIT,
    QUERY_SEARCH_MAX_LIMIT,
    SESSION_MANAGER_URL,
    WEAVIATE_HYBRID_ALPHA,
)
from .utils import search_vectors

logger = logging.getLogger("voice-agent")


def _get_runtime_settings() -> dict[str, Any]:
    """Fetch runtime query settings from dev-console. Falls back to config defaults."""
    console_url = str(DEV_CONSOLE_URL or "").rstrip("/")
    if not console_url:
        return {}
    try:
        req = Request(f"{console_url}/api/settings", method="GET")
        with urlopen(req, timeout=2.0) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        logger.debug("runtime_settings_fetch_failed error=%s", exc)
        return {}


def _post_query_log(query: str, limit: int, result_count: int, results: list,
                    duration_ms: float, alpha: float = 0.0, mode: str = "hybrid") -> None:
    """Best-effort POST to dev-console to log a live query."""
    console_url = str(DEV_CONSOLE_URL or "").rstrip("/")
    if not console_url:
        return
    try:
        payload = json.dumps({
            "query": query,
            "source": "live",
            "mode": mode,
            "alpha": alpha,
            "limit": limit,
            "result_count": result_count,
            "results": results,
            "duration_ms": duration_ms,
        }).encode()
        req = Request(
            f"{console_url}/api/log-query",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        urlopen(req, timeout=2.0)
    except Exception as exc:
        logger.warning("dev_console_log_failed error=%s", exc)


def _agent_result(item: dict[str, Any]) -> dict[str, Any]:
    """Fields returned to the LLM agent."""
    return {
        "title": item.get("title"),
        "content": item.get("content"),
        "source": item.get("source"),
        "score": item.get("score"),
    }


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
    """Push a tool-call entry into the session manager transcript (best-effort)."""
    sm_url = str(SESSION_MANAGER_URL or "").rstrip("/")
    if not sm_url:
        return
    payload = json.dumps(
        {"event": "transcript", "speaker": "Pepper", "text": text, "kind": "tool"}
    ).encode("utf-8")
    req = Request(
        "{}/api/debug-event".format(sm_url),
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(req, timeout=0.5) as resp:
            resp.read()
    except Exception:
        pass


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


def _load_room_data() -> dict:
    """Load FLOORS dict from map.py at runtime (re-read on every call, no restart needed)."""
    import importlib.util
    map_path = Path(__file__).resolve().parents[2] / "dev-console" / "data" / "map" / "map.py"
    spec = importlib.util.spec_from_file_location("building_map", map_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.FLOORS


def build_tools() -> list[Any]:
    @function_tool
    async def query_search(
        context: RunContext,
        query: str,
    ) -> str:
        """Vyhledej informace z interni znalostni baze FEL."""
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

        # Fetch runtime settings from dev-console (alpha, limit, mode).
        rt = await asyncio.to_thread(_get_runtime_settings)
        rt_alpha = float(rt.get("alpha", WEAVIATE_HYBRID_ALPHA))
        rt_limit = int(rt.get("limit", QUERY_SEARCH_DEFAULT_LIMIT))
        safe_limit = max(1, min(rt_limit, QUERY_SEARCH_MAX_LIMIT))
        logger.info("query_search query=%s limit=%s alpha=%s", query_text, safe_limit, rt_alpha)
        await asyncio.to_thread(_push_tool_transcript, "query_search({})".format(query_text))

        try:
            import time as _time
            t0 = _time.monotonic()
            results = await asyncio.to_thread(search_vectors, query_text, safe_limit, rt_alpha)
            duration_ms = (_time.monotonic() - t0) * 1000
            agent_results = [_agent_result(item) for item in results]
            payload = {
                "query": query_text,
                "count": len(results),
                "results": agent_results,
            }
            # Best-effort log to dev-console (full results for inspection).
            try:
                logger.info("dev_console_posting query=%s", query_text)
                await asyncio.to_thread(
                    _post_query_log, query_text, safe_limit, len(results), results,
                    duration_ms, rt_alpha, rt.get("mode", "hybrid"),
                )
                logger.info("dev_console_post_ok query=%s", query_text)
            except Exception as log_exc:
                logger.warning("dev_console_post_failed error=%s", log_exc)
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            logger.exception("query_search_failed error=%s", str(exc))
            return json.dumps(
                {
                    "error": "query_search_failed",
                    "message": str(exc),
                },
                ensure_ascii=False,
            )

    @function_tool
    async def play_animation(
        context: RunContext,
        animation: str,
    ) -> str:
        """Trigger a Pepper body gesture/animation.

        Use this tool directly — never write action text like [waves] in your speech.
        Pass one of: greeting, bow, explain, happy, thinking, dont_know, excited, interested, surprised.
        """
        del context

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
        if not resolved:
            return json.dumps(
                {
                    "error": "unknown_animation",
                    "message": "Use one of the allowed animation group names.",
                    "allowed": list(ANIMATION_GROUPS.keys()),
                },
                ensure_ascii=False,
            )

        logger.info("play_animation_queued animation=%s resolved=%s", animation_name, resolved)
        asyncio.create_task(_dispatch_animation(resolved))
        return json.dumps(
            {
                "ok": True,
                "status": "queued",
                "animation": resolved,
            },
            ensure_ascii=False,
        )

    @function_tool
    async def get_directions_to_room(
        context: RunContext,
        room_number: str,
    ) -> str:
        """Get directions on how to walk to a specific room in Building E.

        Call this whenever a visitor asks where a room is or how to get there.
        Returns step-by-step walking directions from the main entrance.
        """
        del context

        room_number = str(room_number or "").strip()
        logger.info("get_directions_to_room room=%s", room_number)
        await asyncio.to_thread(_push_tool_transcript, "get_directions_to_room({})".format(room_number))

        try:
            floors = await asyncio.to_thread(_load_room_data)
        except Exception as exc:
            logger.error("get_directions_to_room map_load_failed error=%s", exc)
            return json.dumps({"error": "map_unavailable"}, ensure_ascii=False)

        for floor_id, rooms_on_floor in floors.items():
            if room_number in rooms_on_floor:
                room = rooms_on_floor[room_number]
                directions = (room.get("directions") or "").strip()
                name = (room.get("name") or "").strip()

                if not directions:
                    logger.warning("get_directions_to_room room=%s directions_empty", room_number)
                    return json.dumps({
                        "error": "no_directions",
                        "message": f"Room {room_number} is known but directions are not filled in yet.",
                    }, ensure_ascii=False)

                logger.info("get_directions_to_room room=%s floor=%s found=true", room_number, floor_id)
                result = {
                    "room": room_number,
                    "floor": floor_id,
                    "directions": directions,
                }
                if name:
                    result["name"] = name
                return json.dumps(result, ensure_ascii=False)

        logger.warning("get_directions_to_room room=%s not_found", room_number)
        return json.dumps({
            "error": "room_not_found",
            "message": f"Room {room_number} is not in my map. I only know Building E rooms.",
        }, ensure_ascii=False)

    return [query_search, play_animation, get_directions_to_room]
