import asyncio
import json
import logging
from typing import Any

from livekit.agents import RunContext, function_tool

from .config import (
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

    return [query_search]
