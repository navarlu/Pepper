"""Curated Building E room directions + FelSight fallback.

Used by the `find_path_to_room` tool in tools.py. Pepper stands at the
main Building E entrance (`entryE` in FelSight's nav graph), so all
directions are written from that viewpoint — no compass headings, just
body-relative left/right/straight cues.

The curated `ROOM_DIRECTIONS` dict is the authoritative dataset Lucas
described over voice during benchmark development. Rooms not in the
dict fall back to the FelSight GraphQL API at
https://navigate.fel.cvut.cz/graphql, which at least returns the
floor number so the agent can give partial directions.
"""

from __future__ import annotations

import asyncio
from typing import Any

import requests

_GRAPHQL_URL = "https://navigate.fel.cvut.cz/graphql"
_BUILDING = "KN"  # Building E is `KN` in FelSight (Karlovo náměstí)
_START_ID = "entryE"
_MAP_URL_FMT = (
    "https://navigate.fel.cvut.cz/static/demo/index.html"
    "?start=entryE&end=KN:{room}"
)
_REQUEST_TIMEOUT_S = 8.0

_FIND_PATH_QUERY = """
query($startId: String!, $endId: String!) {
  findPath(startId: $startId, endId: $endId) {
    end { __typename ... on Classroom { id floor } }
  }
}
"""


# ── Curated room directions ─────────────────────────────────────────────
# Each room maps to either:
#   {"floor": int, "turns": [str, ...]}   → body-relative turns ("left",
#                                            "right", "straight") taken
#                                            once you arrive on that floor
#   {"floor": int, "location": str}       → free-text location phrase
#                                            (used when "turns" doesn't fit)
# Optional: "via": "midpoint_turnaround"  → reach the floor by turning
#                                            around at the mid-staircase
#                                            landing instead of going
#                                            straight up the main stairs
# Optional: "via": "on_staircase"         → the room is encountered mid-
#                                            climb; the `location` text
#                                            describes the full route
# Use bare turn words: "left", "right", "straight" — no compass.
# Floors: 0 = ground (where Pepper stands), -1 = basement, 1/2/3/4 = upstairs.
ROOM_DIRECTIONS: dict[str, dict] = {
    # ── Ground floor (0) — user is already standing here at the entrance
    **{f"E-{n}": {"floor": 0, "turns": ["left"]}
       for n in (22, 23, 24, 25, 26)},
    **{f"E-{n}": {"floor": 0, "turns": ["right"]}
       for n in (14, 15, 16, 17, 18, 19, 20)},
    **{f"E-{n}": {"floor": 0, "turns": ["left", "right"]}
       for n in (8, 9, 10, 11, 12, 13)},
    **{f"E-{n}": {"floor": 0, "location": "right behind the main staircase"}
       for n in (2, 4, 5, 6, 7)},

    # ── Basement (-1) ──────────────────────────────────────────────────
    "S-109": {"floor": -1, "turns": ["left", "right", "right"]},

    # ── First floor (1) ────────────────────────────────────────────────
    # Up the main staircase, then straight along the corridor
    **{f"E-{n}": {"floor": 1, "turns": ["straight"]}
       for n in (105, 106, 107, 108)},
    # Up the main staircase, then two left turns onto a side corridor
    **{f"E-{n}": {"floor": 1, "turns": ["left", "left"]}
       for n in (111, 112, 113, 114, 115, 116)},
    # South wing: turn around at the mid-staircase landing, then left
    **{f"E-{n}": {"floor": 1, "via": "midpoint_turnaround", "turns": ["left"]}
       for n in (117, 118, 119, 120, 121, 122, 123, 124, 125)},
    # South wing: turn around at the mid-staircase landing, then right
    **{f"E-{n}": {"floor": 1, "via": "midpoint_turnaround", "turns": ["right"]}
       for n in (126, 127, 128, 129, 130, 131, 132)},

    # ── Second floor (2) ───────────────────────────────────────────────
    **{f"E-{n}": {"floor": 2, "turns": ["right"]}
       for n in (224, 225, 226, 227, 228, 229, 230)},
    **{f"E-{n}": {"floor": 2, "turns": ["left"]}
       for n in (216, 217, 218, 219, 220, 221, 222)},
    **{f"E-{n}": {"floor": 2, "turns": ["left", "left"]}
       for n in (209, 210, 211, 212, 213, 214, 215)},
    **{f"E-{n}": {"floor": 2, "location": "right above the main staircase"}
       for n in (201, 202, 203, 204, 205)},

    # ── Third floor (3) ────────────────────────────────────────────────
    **{f"E-{n}": {"floor": 3, "turns": ["right"]}
       for n in (327, 328, 329, 330, 331, 332, 333)},
    **{f"E-{n}": {"floor": 3, "turns": ["left"]}
       for n in (319, 320, 321, 322, 323, 324, 325, 326)},
    **{f"E-{n}": {"floor": 3, "turns": ["left", "left"]}
       for n in (313, 314, 315, 316, 317, 318)},
    # Reached on the way up the staircase, before arriving on the third floor
    "E-301": {
        "floor": 3,
        "via": "on_staircase",
        "location": "right next to the staircase, on the way up to the third floor",
    },
    # Right at the top of the staircase, on the third floor
    **{f"E-{n}": {"floor": 3, "location": "right at the top of the main staircase"}
       for n in (302, 303, 304, 305, 306)},

    # ── Fourth floor (4) ───────────────────────────────────────────────
    **{f"E-{n}": {"floor": 4, "turns": ["right"]}
       for n in (428, 429, 430, 431, 432, 433, 434, 435, 440)},
    **{f"E-{n}": {"floor": 4, "turns": ["left"]}
       for n in (419, 420, 421, 422, 423, 424, 425, 426, 427)},
    **{f"E-{n}": {"floor": 4, "turns": ["left", "left"]}
       for n in (409, 410)},
    # Right in front of the staircase on the fourth floor (incl. E-500)
    **{f"E-{n}": {"floor": 4, "location": "right in front of the staircase"}
       for n in (401, 402, 403, 404, 405, 406, 407, 408, 500)},
}


def normalize_room(raw: str) -> str:
    """Accept '107', 'e-107', 'KN:E-220', 'S-109' → return 'X-NNN'."""
    text = (raw or "").strip().upper().replace(" ", "")
    if text.startswith("KN:"):
        text = text[3:]
    if "-" in text:
        return text
    if text and text[0].isalpha():
        return f"{text[0]}-{text[1:]}"
    return f"E-{text}"


def _floor_word(floor: int) -> str:
    if floor == 0:
        return "the ground floor"
    if floor < 0:
        return "the basement"
    ordinals = {1: "first", 2: "second", 3: "third", 4: "fourth", 5: "fifth"}
    return f"the {ordinals.get(floor, str(floor))} floor"


def _floor_prefix(floor: int, via: str | None = None) -> str:
    """Phrase the user has to do BEFORE the floor-local turns apply."""
    if floor == 0 or via == "on_staircase":
        return ""
    if floor < 0:
        return "Head down to the basement. "
    if via == "midpoint_turnaround":
        return (
            f"Walk to the staircase, and at the mid-staircase landing "
            f"turn around and continue up to {_floor_word(floor)}. "
        )
    return f"Head up to {_floor_word(floor)}. "


def _join_turns(turns: list[str]) -> str:
    """Render a body-relative turn list as a sentence fragment."""
    if not turns:
        return ""
    if turns == ["straight"]:
        return "walk straight ahead"
    if len(turns) == 1 and turns[0] in ("left", "right"):
        return f"go to your {turns[0]}"
    head, *tail = turns
    head_phrase = "walk straight" if head == "straight" else f"go {head}"
    return f"{head_phrase}, then " + ", then ".join(tail)


def _render_curated(room: str, info: dict) -> str:
    floor = int(info.get("floor") or 0)
    via = info.get("via")
    prefix = _floor_prefix(floor, via)
    if "location" in info:
        return f"{prefix}Room {room} is {info['location']}."
    turns = info.get("turns") or []
    if not turns:
        return f"{prefix}Room {room} is along the corridor."
    return f"{prefix}{_join_turns(turns).capitalize()} — Room {room} is along that corridor."


def _render_floor_only(room: str, floor: int) -> str:
    """Fallback when the room isn't curated yet — give just the floor."""
    if floor == 0:
        return f"Room {room} is on the ground floor along the corridor."
    if floor < 0:
        return f"Head down to the basement — Room {room} is on that level."
    return f"Head up to {_floor_word(floor)} — Room {room} is on that floor."


def _query_floor_blocking(room: str) -> int | None:
    """Sync FelSight call. Wrap in asyncio.to_thread from async code."""
    payload = {
        "query": _FIND_PATH_QUERY,
        "variables": {"startId": _START_ID, "endId": f"{_BUILDING}:{room}"},
    }
    try:
        resp = requests.post(
            _GRAPHQL_URL,
            json=payload,
            headers={"content-type": "application/json"},
            timeout=_REQUEST_TIMEOUT_S,
        )
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError):
        return None
    path = (data.get("data") or {}).get("findPath")
    if not path:
        return None
    end = path.get("end") or {}
    floor = end.get("floor")
    return int(floor) if floor is not None else None


async def compute_room_directions(room: str) -> dict[str, Any]:
    """Return the full directions payload for the `find_path_to_room` tool.

    Curated rooms hit the local dict; uncurated rooms try the FelSight
    API for a floor-only fallback. Returns `{"error": "room_not_found",
    ...}` if the room isn't anywhere.
    """
    room_norm = normalize_room(room)
    info = ROOM_DIRECTIONS.get(room_norm)
    if info:
        floor = int(info.get("floor") or 0)
        return {
            "room": room_norm,
            "floor": floor,
            "directions": _render_curated(room_norm, info),
            "instruction": (
                "Read the `directions` field to the user in plain prose, "
                "naturally. The user is already standing with you at the "
                "entrance, so don't tell them to start from the entrance."
            ),
        }

    floor = await asyncio.to_thread(_query_floor_blocking, room_norm)
    if floor is None:
        return {
            "error": "room_not_found",
            "room": room_norm,
            "instruction": (
                f"Room {room_norm} was not found in Building E. "
                "Tell the user that and ask them to confirm the "
                "room code."
            ),
        }
    return {
        "room": room_norm,
        "floor": floor,
        "directions": _render_floor_only(room_norm, floor),
        "instruction": (
            "Read the `directions` field to the user in plain prose, "
            "naturally. The user is already standing with you at the "
            "entrance, so don't tell them to start from the entrance."
        ),
    }


def map_url_for(room: str) -> str:
    """Build the FelSight tablet-view URL for a normalized room code.
    Intentionally not in the LLM-facing payload; UI surfaces only."""
    return _MAP_URL_FMT.format(room=normalize_room(room))
