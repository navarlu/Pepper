"""mensa_menu tool — fetch the canteen menu (this week + next)."""

from __future__ import annotations

import asyncio
from typing import Any

from livekit.agents import RunContext, function_tool

from .utils._animation import trigger_animation
from .utils._emotion import Emotion
from .utils._events import _emit_tool_event, _heartbeat_or_none

from src.live.mensa import fetch_mensa_menu  # noqa: E402


@function_tool(name="mensa_menu")
async def mensa_menu(
    context: RunContext,
    emotion: Emotion = "think",
    request_heartbeat: bool = True,
) -> Any:
    """Look up what's on the menu at the nearby canteen.

    Returns every day currently published — typically this week and
    next week. Each day has a `dishes` list and each dish has a
    `category` tag like "soup", "main", "salad", "vegetarian".

    Call this when the user asks what they can eat, what's for
    lunch, what's on the menu, or about the canteen / mensa /
    menza / buffet.

    emotion: body language while fetching. Default 'think'.
    request_heartbeat: True (default) to continue and speak via
        send_message_to_user with the menu.
    """
    del context
    print(f"  [tool] mensa_menu(emotion={emotion!r}, hb={request_heartbeat})")
    _emit_tool_event("mensa_menu", {
        "emotion": emotion, "request_heartbeat": request_heartbeat,
    })
    if emotion:
        asyncio.create_task(trigger_animation(emotion))

    try:
        result = await asyncio.to_thread(fetch_mensa_menu)
    except Exception as exc:
        return _heartbeat_or_none(
            {"error": "mensa_fetch_failed", "message": str(exc)},
            request_heartbeat,
        )

    days = result.get("days") or []
    if not days:
        return _heartbeat_or_none({
            "canteen": result.get("canteen"),
            "days": [],
            "instruction": (
                "The menu has not been published yet. Tell the user "
                "via send_message_to_user that the canteen has not "
                "posted the menu, and offer to check again later."
            ),
        }, request_heartbeat)

    result["instruction"] = (
        "Match the user's question to the right day in `days`. In "
        "your next send_message_to_user, mention only 1 or 2 dishes "
        "— prefer main dishes over soups unless the user asked "
        "specifically. If the user asked for a specific category "
        "(soup, vegetarian, etc.), filter `dishes` by that category. "
        "Read the FULL list ONLY if the user explicitly asked for "
        "everything. Never read out the canteen's full name or any "
        "URLs."
    )
    return _heartbeat_or_none(result, request_heartbeat)
