"""mensa_menu: Charles Square Food Counter weekly menu lookup."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio

from livekit.agents import RunContext, function_tool

from tools._animation import trigger_animation
from tools._common import _json, _run_main

from src.mensa import fetch_mensa_menu  # noqa: E402

_Gesture = Literal["greet", "think", "explain", "bow", "happy", "dont_know"]


@function_tool
async def mensa_menu(
    context: RunContext,
    gesture: _Gesture = "think",
) -> str:
    """Look up what's on the menu at the Charles Square food counter.

    Always returns every day currently published — typically this week
    and next week. Each day has a `dishes` list and each dish has a
    `category` tag like "soup", "main", "salad", "vegetarian".

    Call this when the user asks what they can eat, what's for lunch,
    what's on the menu, or about the canteen / mensa / menza / buffet.

    gesture: Pepper body language while fetching. Default 'think'.
        One of greet, think, explain, bow, happy, dont_know.
    """
    del context
    if gesture:
        asyncio.create_task(trigger_animation(gesture))
    print("  [tool] mensa_menu()")
    try:
        result = await asyncio.to_thread(fetch_mensa_menu)
    except Exception as exc:
        return _json({"error": "mensa_fetch_failed", "message": str(exc)})

    days = result.get("days") or []
    if not days:
        return _json({
            "canteen": result.get("canteen"),
            "days": [],
            "instruction": (
                "The menu has not been published yet. Tell the user "
                "the canteen has not posted the menu, and offer to "
                "check again later."
            ),
        })

    result["instruction"] = (
        "Match the user's question to the right day in `days` (by "
        "weekday name or date). Mention only 1 or 2 dishes — prefer "
        "main dishes over soups unless the user asked specifically. "
        "If the user asked for a specific category (soup, vegetarian, "
        "etc.), filter `dishes` by that category and answer briefly. "
        "Read the FULL list ONLY if the user explicitly asked for "
        "everything. Never read out the canteen's full name or any "
        "URLs."
    )
    return _json(result)


if __name__ == "__main__":
    _run_main(mensa_menu(None))
