"""Room directory for Building E, FEL ČVUT (Karlovo náměstí).

Hand-curated directions used by the `query_search` tool when a user
asks "where is room 107?" style questions. We don't put this in
Weaviate because:
  - the answer is fixed, short, and doesn't benefit from semantic search
  - we want a deterministic lookup (keyword + regex), not a best-effort
    similarity match

Update by editing `BUILDING_ROOMS` below — no migration, no reindex.
"""

from __future__ import annotations

import re

# floor_id → { room_number → { "directions": str, ...optional fields } }
BUILDING_ROOMS: dict[str, dict[str, dict]] = {
    "ground_floor": {
        "23": {"directions": "go left, room 23 is in the middle of the corridor on the left side"},
        "24": {"directions": "go left, room 24 is in the middle of the corridor on the left side"},
        "26": {"directions": "go left, room 26 is at the end of the corridor"},
    },
    "1st_floor": {
        "107": {"directions": "go up the stairs, and when you see toilet go there and then right, room 107 is at the end of the corridor"},
        "112": {"directions": ""},
        "125": {"directions": "go up the stairs, turn on the landing and continue up, room 125 will be right in front of you"},
        "126": {"directions": "go up the stairs, turn on the landing and continue up, room 126 will be right in front of you"},
        "127": {"directions": "go up the stairs, turn on the landing and continue up, then turn right, room 127 will be on left side"},
        "128": {"directions": "go up the stairs, turn on the landing and continue up, then turn right, room 128 will be on left side"},
        "129": {"directions": "go up the stairs, turn on the landing and continue up, then turn right, room 129 will be on left side"},
        "130": {"directions": "go up the stairs, turn on the landing and continue up, then turn right, room 130 will be on left side"},
        "132": {"directions": "go up the stairs, turn on the landing and continue up, then turn right, room 132 will be at the end of the corridor"},
    },
    "2nd_floor": {
        "230": {"directions": "go up to the second floor, turn right, room 230 will be at the end of the corridor"},
    },
    "3rd_floor": {
        "301": {"directions": ""},
        "307": {"directions": ""},
        "310": {"directions": ""},
        "311": {"directions": ""},
        "327": {"directions": ""},
        "328": {"directions": ""},
        "329": {"directions": ""},
        "331": {"directions": ""},
        "332": {"directions": ""},
        "333a": {"directions": ""},
        "333b": {"directions": ""},
        "333c": {"directions": ""},
        "333d": {"directions": ""},
        "396": {"directions": ""},
    },
}


_ROOM_NUMBER_RE = re.compile(r"\b(\d{2,4}[a-zA-Z]?)\b")
_ROOM_KEYWORDS = (
    "room", "direction", "where", "how to get", "find", "navigate",
    "located", "location", "místnost", "kam", "kudy",
)


# region: try_room_lookup
def try_room_lookup(query: str) -> dict | None:
    """Try to answer a room/directions question from `BUILDING_ROOMS`.

    Two gates before we commit to a directions answer:
      1. The query must contain a room-number-like token (2-4 digits,
         optional letter suffix, matched by `_ROOM_NUMBER_RE`).
      2. It must also contain at least one room-related keyword
         (English or Czech). This prevents "what is 2025" or similar
         numeric questions from being hijacked.

    Returns:
      - `{"type": "directions", "room": ..., "floor": ..., "directions": ...}`
        on a full hit.
      - A `{"type": "directions", "error": ...}` dict if the number is
        a known room but directions are empty, or if the number isn't
        on our map at all.
      - `None` if the gates didn't match — caller should fall through
        to the Weaviate knowledge-base search.
    """
    match = _ROOM_NUMBER_RE.search(query)
    if not match:
        return None
    query_lower = query.lower()
    if not any(kw in query_lower for kw in _ROOM_KEYWORDS):
        return None

    room_number = match.group(1)

    for floor_id, rooms_on_floor in BUILDING_ROOMS.items():
        if room_number in rooms_on_floor:
            room = rooms_on_floor[room_number]
            directions = (room.get("directions") or "").strip()
            name = (room.get("name") or "").strip()
            if not directions:
                return {
                    "type": "directions",
                    "error": "no_directions",
                    "message": f"Room {room_number} is known but directions are not filled in yet.",
                }
            result = {
                "type": "directions",
                "room": room_number,
                "floor": floor_id,
                "directions": directions,
            }
            if name:
                result["name"] = name
            return result

    return {
        "type": "directions",
        "error": "room_not_found",
        "message": f"Room {room_number} is not in my map. I only know Building E rooms.",
    }
# endregion
