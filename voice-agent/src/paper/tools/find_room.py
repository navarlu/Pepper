"""find_room tool — directions from the Building E entrance to any
room in the building. Slim paper-MVP wrapper: same curated table +
FelSight floor fallback as the experiment tool
(`src/experiment/tools/find_path_to_room.py`), minus the cascade-only
extras (emotion/gesture, filler TTS, heartbeat shim).
"""

from __future__ import annotations

import asyncio
from typing import Any

from livekit.agents import RunContext, function_tool

from src.experiment.tools.utils._events import (
    _emit_tool_event,
    _emit_tool_result,
)
from src.experiment.tools.utils.find_path_to_room import (
    ROOM_DIRECTIONS,
    _normalize_room,
    _query_floor,
    _render_curated,
    _render_floor_only,
)


@function_tool(name="find_room")
async def find_room(context: RunContext, room: str) -> Any:
    """Get directions to a room in this building.

    Call this when the user asks where a room is, or how to get to
    one. The user is already standing with you at the main entrance.

    Copy the room number VERBATIM — the user's "230" is "230", not
    "23". Examples: '101', 'E-205', 'S-109'.

    room: room number exactly as the user said it.
    """
    del context
    room_norm = _normalize_room(room)
    print(f"  [tool] find_room({room!r} -> {room_norm!r})", flush=True)
    _emit_tool_event("find_room", {"room": room_norm, "room_raw": room})

    info = ROOM_DIRECTIONS.get(room_norm)
    if info:
        directions = _render_curated(room_norm, info)
        floor = int(info.get("floor") or 0)
    else:
        floor = await asyncio.to_thread(_query_floor, room_norm)
        if floor is None:
            result = {
                "error": "room_not_found",
                "room": room_norm,
                "instruction": (
                    f"Room {room_norm} was not found in Building E. "
                    "Tell the user (in Czech) and ask them to confirm "
                    "the room code."
                ),
            }
            _emit_tool_result("find_room", result)
            return result
        directions = _render_floor_only(room_norm, floor)

    result = {
        "room": room_norm,
        "floor": floor,
        "directions": directions,
        "instruction": (
            "Read the `directions` field to the user in natural Czech "
            "prose (translate — the directions are in English). The "
            "user is already with you at the entrance — don't tell "
            "them to start there."
        ),
    }
    _emit_tool_result("find_room", result)
    return result
