"""get_time tool — current local time in Europe/Prague."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from livekit.agents import RunContext, function_tool

from .utils._animation import trigger_animation
from .utils._emotion import Emotion
from .utils._events import _emit_tool_event, _heartbeat_or_none


@function_tool(name="get_time")
async def get_time(
    context: RunContext,
    emotion: Emotion = "think",
    request_heartbeat: bool = True,
) -> Any:
    """Return the current local time. Use only when the user
    explicitly asks what time it is.

    emotion: body language while checking the clock. Default
        'think'; override freely if a different mood fits.
    request_heartbeat: True (default) to continue.
    """
    #context.session.say(
        #"Let me check the time for you.",
        #add_to_chat_ctx=False,
    #)
    del context
    print(f"  [tool] get_time(emotion={emotion!r}, hb={request_heartbeat})")
    _emit_tool_event("get_time", {
        "emotion": emotion, "request_heartbeat": request_heartbeat,
    })
    if emotion:
        asyncio.create_task(trigger_animation(emotion))
    now = datetime.now(ZoneInfo("Europe/Prague"))
    payload = {"time": now.strftime("%Y-%m-%d %H:%M %Z")}
    return _heartbeat_or_none(payload, request_heartbeat)
