"""The single starter tool: find_room.

Backed by a small frozen dictionary of Building E rooms (a subset of the
manual ROOM_DIRECTIONS table used in production,
voice-agent/src/experiment/tools/utils/find_path_to_room.py). Deterministic
and offline — this is the clean stand-in. In phase 2 the network-backed tools
(lookup_person, subject_schedule, mensa_menu) follow the same shape but read
from frozen endpoint snapshots in data/snapshots/.
"""
import logging

logger = logging.getLogger(__name__)

# Frozen room -> directions table (subset of the production manual dict).
ROOM_DIRECTIONS = {
    "E-107": {"floor": 1, "directions": "Go up the main staircase to the first floor; E-107 (the Zenger auditorium) is straight ahead."},
    "E-125": {"floor": 1, "directions": "First floor — turn right at the top of the main staircase; E-125 is the study room with the two microwaves."},
    "E-220": {"floor": 2, "directions": "Second floor — turn left from the staircase; E-220 is halfway down the hall."},
    "E-301": {"floor": 3, "directions": "Take the lift or stairs to the third floor; E-301 is on your left down the corridor."},
}

# JSON schema shown to the model (OpenAI tools format).
FIND_ROOM_SCHEMA = {
    "type": "function",
    "function": {
        "name": "find_room",
        "description": (
            "Get walking directions from the reception desk to a room in "
            "building E. Call this whenever the user asks where a room is."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "room": {
                    "type": "string",
                    "description": "The room code, e.g. 'E-107'.",
                }
            },
            "required": ["room"],
            "additionalProperties": False,
        },
    },
}


def find_room(room: str) -> dict:
    """Execute the tool. Returns a prompt-friendly payload for the model."""
    key = (room or "").strip().upper()
    logger.info("find_room called with room=%r (normalized=%r)", room, key)
    entry = ROOM_DIRECTIONS.get(key)
    if entry is None:
        logger.info("find_room: room %r not found", key)
        return {
            "error": "room_not_found",
            "room": key,
            "instruction": "Tell the user you don't have directions for that room and ask them to confirm the room code.",
        }
    logger.info("find_room: found %r on floor %s", key, entry["floor"])
    return {"room": key, "floor": entry["floor"], "directions": entry["directions"]}


# Registry: tool name -> (callable, schema). The runner iterates this.
TOOLS = {"find_room": (find_room, FIND_ROOM_SCHEMA)}
