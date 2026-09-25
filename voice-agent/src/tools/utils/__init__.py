"""Shared helpers reused by the per-tool modules in
voice-agent/src/tools/.

  - `_common`   : diacritic-folding JSON serializer
  - `_animation`: trigger_animation / animation-name normalization
  - `_person`   : person-result slim formatter (UDB → LLM)
  - `_room_directions`: ROOM_DIRECTIONS table + path-rendering helpers
  - `udb` / `mensa` / `timetable`: FEL staff, canteen and timetable scrapers
  - `_emotion`  : the shared `Emotion` Literal
  - `_events`   : tool-event listener hook + heartbeat shim
  - `_person_lookup`: title scoring + English→Czech surname variants
"""

from __future__ import annotations
