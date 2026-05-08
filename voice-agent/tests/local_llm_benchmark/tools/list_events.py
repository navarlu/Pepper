"""list_events: Weaviate search filtered to FEE events on a given ISO date."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio

from livekit.agents import RunContext, function_tool

from tools._animation import trigger_animation
from tools._common import _agent_result, _ensure_weaviate_seeded_once, _json, _run_main

from src.config import QUERY_SEARCH_DEFAULT_LIMIT, WEAVIATE_HYBRID_ALPHA  # noqa: E402
from src.rag import search_vectors  # noqa: E402

_Gesture = Literal["greet", "think", "explain", "bow", "happy", "dont_know"]


@function_tool
async def list_events(
    context: RunContext,
    date: str,
    gesture: _Gesture = "explain",
) -> str:
    """List events at FEE on a given ISO date (YYYY-MM-DD).

    Use at most once when the user asks what is happening, scheduled, or open
    at FEE on a date. Normalise "today", "tomorrow", and "May 1st" to ISO
    before calling.

    gesture: Pepper body language while answering. Default 'explain'.
        One of greet, think, explain, bow, happy, dont_know.
    """
    del context
    if gesture:
        asyncio.create_task(trigger_animation(gesture))
    day = str(date or "").strip()
    print(f"  [tool] list_events({day!r})")
    if not day:
        return _json({"error": "missing_date", "message": "date cannot be empty"})

    await asyncio.to_thread(_ensure_weaviate_seeded_once)
    try:
        results = await asyncio.to_thread(
            search_vectors,
            f"FEE events schedule happening on {day}",
            QUERY_SEARCH_DEFAULT_LIMIT,
            WEAVIATE_HYBRID_ALPHA,
        )
    except Exception as exc:
        return _json({"error": "list_events_failed", "message": str(exc)})

    return _json({
        "date": day,
        "count": len(results),
        "results": [_agent_result(item) for item in results],
    })


if __name__ == "__main__":
    DATE = "2026-05-01"
    _run_main(list_events(None, date=DATE))
