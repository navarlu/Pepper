"""find_path_to_room tool — directions from the Building E entrance to
any room in the building. Curated table + FelSight floor fallback live
in `tools/utils/find_path_to_room.py`.
"""

from __future__ import annotations

import asyncio
from typing import Any

from livekit.agents import RunContext, function_tool

from .utils._animation import trigger_animation
from .utils._emotion import Emotion
from .utils._events import _emit_tool_event, _heartbeat_or_none
from .utils._filler import _speak_filler
from .utils.find_path_to_room import (
    ROOM_DIRECTIONS,
    _normalize_room,
    _query_floor,
    _render_curated,
    _render_floor_only,
)


@function_tool(name="find_path_to_room")
async def find_path_to_room(
    context: RunContext,
    room: str,
    emotion: Emotion = "think",
    request_heartbeat: bool = True,
) -> Any:
    """Get directions to a room in this building. The user is already
    standing with you at the main entrance.

    Call this when the user asks how to get to a room or where a
    room is.

    room: copy the user's room number VERBATIM. The user's "230" is
        "230" — not "23". Examples: '101', 'A-205', 'B-310'.
    emotion: body language while looking up the route. Default
        'think'; pick whatever fits the moment (e.g. 'happy' if
        the user asked enthusiastically).
    request_heartbeat: True (default) to continue the loop so you
        can read the directions via send_message_to_user.
    """
    asyncio.create_task(_speak_filler(context, "Let me find that room for you."))
    del context
    room_norm = _normalize_room(room)
    print(
        f"  [tool] find_path_to_room({room_norm!r}, "
        f"emotion={emotion!r}, hb={request_heartbeat})"
    )
    _emit_tool_event("find_path_to_room", {
        "room": room_norm, "emotion": emotion,
        "request_heartbeat": request_heartbeat,
    })
    if emotion:
        asyncio.create_task(trigger_animation(emotion))

    info = ROOM_DIRECTIONS.get(room_norm)
    if info:
        directions = _render_curated(room_norm, info)
        floor = int(info.get("floor") or 0)
    else:
        floor = await asyncio.to_thread(_query_floor, room_norm)
        if floor is None:
            return _heartbeat_or_none({
                "error": "room_not_found",
                "room": room_norm,
                "instruction": (
                    f"Room {room_norm} was not found in Building E. "
                    "Tell the user via send_message_to_user and ask "
                    "them to confirm the room code."
                ),
            }, request_heartbeat)
        directions = _render_floor_only(room_norm, floor)

    return _heartbeat_or_none({
        "room": room_norm,
        "floor": floor,
        "directions": directions,
        "instruction": (
            "Read the `directions` field via send_message_to_user "
            "in plain prose, naturally. The user is already with you "
            "at the entrance — don't tell them to start there."
        ),
    }, request_heartbeat)
