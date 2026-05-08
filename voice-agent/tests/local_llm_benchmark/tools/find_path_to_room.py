"""find_path_to_room: directions from Pepper at the Building E entrance
to any room in the building.

Curated directions live in `ROOM_DIRECTIONS` below — extend by floor as
new groups are described. The FelSight GraphQL API
(https://navigate.fel.cvut.cz/graphql) is used only as a fallback to
verify the floor of rooms that aren't in the curated table yet.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio

import requests
from livekit.agents import RunContext, function_tool

from tools._animation import trigger_animation
from tools._common import _json, _run_main

_Gesture = Literal["greet", "think", "explain", "bow", "happy", "dont_know"]


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
# Use bare turn words: "left", "right", "straight" — no compass.
# Floors: 0 = ground (where Pepper stands), -1 = basement, 1/2/3 = upstairs.
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
    # (assumed range 111–116 — confirm with Lucas)
    **{f"E-{n}": {"floor": 1, "turns": ["left", "left"]}
       for n in (111, 112, 113, 114, 115, 116)},
    # South wing: turn around at the mid-staircase landing, then left
    **{f"E-{n}": {"floor": 1, "via": "midpoint_turnaround", "turns": ["left"]}
       for n in (117, 118, 119, 120, 121, 122, 123, 124, 125)},
    # South wing: turn around at the mid-staircase landing, then right
    **{f"E-{n}": {"floor": 1, "via": "midpoint_turnaround", "turns": ["right"]}
       for n in (126, 127, 128, 129, 130, 131, 132)},

    # ── Second floor (2) ───────────────────────────────────────────────
    # Up the main staircase, then right
    **{f"E-{n}": {"floor": 2, "turns": ["right"]}
       for n in (224, 225, 226, 227, 228, 229, 230)},
    # Up the main staircase, then left
    **{f"E-{n}": {"floor": 2, "turns": ["left"]}
       for n in (216, 217, 218, 219, 220, 221, 222)},
    # Up the main staircase, then two left turns
    **{f"E-{n}": {"floor": 2, "turns": ["left", "left"]}
       for n in (209, 210, 211, 212, 213, 214, 215)},
    # Right at the top of the staircase
    **{f"E-{n}": {"floor": 2, "location": "right above the main staircase"}
       for n in (201, 202, 203, 204, 205)},

    # ── Third floor (3) ────────────────────────────────────────────────
    # Up to the third floor, then right
    **{f"E-{n}": {"floor": 3, "turns": ["right"]}
       for n in (327, 328, 329, 330, 331, 332, 333)},
    # Up to the third floor, then left
    **{f"E-{n}": {"floor": 3, "turns": ["left"]}
       for n in (319, 320, 321, 322, 323, 324, 325, 326)},
    # Up to the third floor, then two left turns
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
    # Up to the fourth floor, then right
    **{f"E-{n}": {"floor": 4, "turns": ["right"]}
       for n in (428, 429, 430, 431, 432, 433, 434, 435, 440)},
    # Up to the fourth floor, then left
    **{f"E-{n}": {"floor": 4, "turns": ["left"]}
       for n in (419, 420, 421, 422, 423, 424, 425, 426, 427)},
    # Up to the fourth floor, then two left turns
    **{f"E-{n}": {"floor": 4, "turns": ["left", "left"]}
       for n in (409, 410)},
    # Right in front of the staircase on the fourth floor (incl. E-500)
    **{f"E-{n}": {"floor": 4, "location": "right in front of the staircase"}
       for n in (401, 402, 403, 404, 405, 406, 407, 408, 500)},
}


def _normalize_room(raw: str) -> str:
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
        # via=on_staircase: the room is reached mid-climb; the `location`
        # text describes the full route, so no "head up" preamble.
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
    """Render a body-relative turn list as a sentence fragment.
    ['left']                  → 'go to your left'
    ['straight']              → 'walk straight ahead'
    ['left', 'right']         → 'go left, then right'
    ['left', 'right', 'right']→ 'go left, then right, then right'
    """
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


def _query_floor(room: str) -> int | None:
    """Ask the FelSight API just for the floor of a room. Returns None on
    any failure (room missing, API down, etc.)."""
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


@function_tool
async def find_path_to_room(
    context: RunContext,
    room: str,
    gesture: _Gesture = "explain",
) -> str:
    """Get directions to a room in Building E. The user is already standing
    with you at the main entrance.

    Call this when the user asks how to get to a room or where a room is.

    room: copy the user's room number VERBATIM, including every digit
        and any trailing zero. The user's "230" is "230" — not "23".
        Examples: '230', 'E-230', '107', 'E-107', 'S-109'.
    gesture: Pepper body language while answering. Default 'explain'.
        One of greet, think, explain, bow, happy, dont_know.
    """
    del context
    if gesture:
        asyncio.create_task(trigger_animation(gesture))
    room_norm = _normalize_room(room)
    print(f"  [tool] find_path_to_room({room_norm!r})")

    info = ROOM_DIRECTIONS.get(room_norm)
    if info:
        directions = _render_curated(room_norm, info)
        floor = int(info.get("floor") or 0)
    else:
        floor = await asyncio.to_thread(_query_floor, room_norm)
        if floor is None:
            return _json({
                "error": "room_not_found",
                "room": room_norm,
                "instruction": (
                    f"Room {room_norm} was not found in Building E. "
                    "Tell the user that and ask them to confirm the "
                    "room code."
                ),
            })
        directions = _render_floor_only(room_norm, floor)

    # Map URL is intentionally not exposed to the LLM; the user-facing app
    # can build it from `room` for the tablet view:
    #   _MAP_URL_FMT.format(room=room_norm)
    return _json({
        "room": room_norm,
        "floor": floor,
        "directions": directions,
        "instruction": (
            "Read the `directions` field to the user in plain prose, "
            "naturally. The user is already standing with you at the "
            "entrance, so don't tell them to start from the entrance."
        ),
    })


if __name__ == "__main__":
    ROOM = "E-22"
    _run_main(find_path_to_room(None, room=ROOM))
