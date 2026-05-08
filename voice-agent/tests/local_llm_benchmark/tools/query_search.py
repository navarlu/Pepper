"""query_search: hybrid Weaviate search over the FEE knowledge base."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio

from livekit.agents import RunContext, function_tool

from tools._animation import trigger_animation
from tools._common import _agent_result, _ensure_weaviate_seeded_once, _json, _run_main

from src.bridge_client import post_led_state  # noqa: E402
from src.config import QUERY_SEARCH_DEFAULT_LIMIT, WEAVIATE_HYBRID_ALPHA  # noqa: E402
from src.rag import search_vectors  # noqa: E402

_Gesture = Literal["greet", "think", "explain", "bow", "happy", "dont_know"]


@function_tool
async def query_search(
    context: RunContext,
    query: str,
    gesture: _Gesture = "think",
) -> str:
    """Search the faculty knowledge base for an answer to a factual
    question the user just asked.

    query: copy the user's factual question in their own words, 3-10
        words. Only call when the user's last message contains a
        clear question (what, where, when, who, how, why) about the
        faculty.
    gesture: Pepper body language while searching. Default 'think'.
        One of greet, think, explain, bow, happy, dont_know.
    """
    del context
    if gesture:
        asyncio.create_task(trigger_animation(gesture))
    query_text = str(query or "").strip()
    print(f"  [tool] query_search({query_text!r})")
    if not query_text:
        return _json({"error": "missing_query", "message": "query cannot be empty"})

    await asyncio.to_thread(_ensure_weaviate_seeded_once)
    await asyncio.to_thread(post_led_state, "search_pulse")
    try:
        results = await asyncio.to_thread(
            search_vectors,
            query_text,
            QUERY_SEARCH_DEFAULT_LIMIT,
            WEAVIATE_HYBRID_ALPHA,
        )
    except Exception as exc:
        return _json({"error": "query_search_failed", "message": str(exc)})
    finally:
        await asyncio.to_thread(post_led_state, "idle")

    return _json({
        "query": query_text,
        "count": len(results),
        "results": [_agent_result(item) for item in results],
    })


if __name__ == "__main__":
    QUERY = "what programmes does FEE offer"
    _run_main(query_search(None, query=QUERY))
