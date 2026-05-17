"""query_search tool — hybrid vector search against the knowledge base.

Currently NOT included in the default LIVEKIT_TOOLS_TOOLONLY surface
(see tools/__init__.py): its broad "any factual question" trigger
hijacked turn-1 with cascading search loops on bare greetings. Re-add
to the registry only after retraining the tool description for a
stricter trigger.
"""

from __future__ import annotations

import asyncio
from typing import Any

from livekit.agents import RunContext, function_tool

from .utils._animation import trigger_animation
from .utils._common import _agent_result, _ensure_weaviate_seeded_once
from .utils._emotion import Emotion
from .utils._events import _emit_tool_event, _heartbeat_or_none

from src.live.bridge_client import post_led_state  # noqa: E402
from src.live.config import QUERY_SEARCH_DEFAULT_LIMIT, WEAVIATE_HYBRID_ALPHA  # noqa: E402
from src.live.rag import search_vectors  # noqa: E402


@function_tool(name="query_search")
async def query_search(
    context: RunContext,
    query: str,
    emotion: Emotion = "think",
    request_heartbeat: bool = True,
) -> Any:
    """Search the school's internal document knowledge base.

    Call this when the user asks about university rules, study
    regulations, official procedures, deadlines, scholarships, exam
    rules, enrolment, dorm info, or any other internal-document
    topic — and ONLY when no other tool fits.

    Do NOT call this for greetings, smalltalk, opinions, or for
    questions another tool already handles (people → lookup_person,
    rooms → find_path_to_room, course timetable → subject_schedule,
    canteen → mensa_menu, current time → get_time).

    query: a short paraphrase of the user's question, 3-10 words.
    emotion: body language while searching. Default 'think'.
    request_heartbeat: True (default) to continue the turn so you
        can read the answer to the user.
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
