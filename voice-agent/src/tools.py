import asyncio
import json
import logging
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from livekit.agents import RunContext, function_tool

from .config import (
    ANIMATION_BRIDGE_URL,
    ANIMATION_TOOL_ALIASES,
    ANIMATION_TOOL_ALLOWED,
    ANIMATION_TOOL_HTTP_TIMEOUT_SEC,
    ANIMATION_TOOL_MAX_NAME_CHARS,
    ENABLE_ANIMATION_TOOL,
    ENABLE_QUERY_SEARCH,
    QUERY_SEARCH_DEFAULT_LIMIT,
    QUERY_SEARCH_MAX_CONTENT_CHARS,
    QUERY_SEARCH_MAX_LIMIT,
)
from .utils import search_vectors

logger = logging.getLogger("voice-agent")


def _compact_result(item: dict[str, Any]) -> dict[str, Any]:
    content = str(item.get("content") or "")
    if len(content) > QUERY_SEARCH_MAX_CONTENT_CHARS:
        content = content[:QUERY_SEARCH_MAX_CONTENT_CHARS].rstrip() + "..."
    return {
        "title": item.get("title"),
        "content": content,
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


def _normalize_animation_name(raw_name: str) -> str:
    clean = str(raw_name or "").strip()
    if not clean:
        return ""
    if clean in ANIMATION_TOOL_ALLOWED:
        return clean

    normalized = (
        clean.lower()
        .replace("-", "_")
        .replace(" ", "_")
    )
    normalized = "".join(ch for ch in normalized if ch.isalnum() or ch == "_")
    mapped = ANIMATION_TOOL_ALIASES.get(normalized)
    if mapped:
        return mapped
    # Accept case-insensitive direct match for allowed keys.
    for key in ANIMATION_TOOL_ALLOWED:
        if key.lower() == clean.lower():
            return key
    return ""


async def _dispatch_animation(animation_name: str) -> None:
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


def build_tools() -> list[Any]:
    @function_tool
    async def query_search(
        context: RunContext,
        query: str,
        limit: int = QUERY_SEARCH_DEFAULT_LIMIT,
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

        safe_limit = max(1, min(int(limit), QUERY_SEARCH_MAX_LIMIT))
        logger.info("query_search query=%s limit=%s", query_text, safe_limit)

        try:
            results = await asyncio.to_thread(search_vectors, query_text, safe_limit)
            payload = {
                "query": query_text,
                "count": len(results),
                "results": [_compact_result(item) for item in results],
            }
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
        """Trigger a Pepper gesture/animation via bridge.

        Use this tool directly. Do not output textual action markers.
        Allowed keys: Hey_1, BowShort_1, Explain_1, Happy_1, Thinking_1, IDontKnow_1.
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
                    "message": "Use one of the allowed animation keys.",
                    "allowed": list(ANIMATION_TOOL_ALLOWED),
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

    return [query_search, play_animation]
