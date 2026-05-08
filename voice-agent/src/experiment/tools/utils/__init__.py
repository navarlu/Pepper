"""Shared helpers reused by the per-tool modules in
voice-agent/src/experiment/tools/.

  - `_common`   : sys.path glue, JSON debug print, weaviate seeder
  - `_animation`: trigger_animation / animation-name normalization
  - `_person`   : person-result slim formatter (UDB → LLM)
  - `find_path_to_room`: ROOM_DIRECTIONS table + path-rendering helpers
  - `_emotion`  : the shared `Emotion` Literal
  - `_events`   : tool-event listener hook + heartbeat shim
  - `_person_lookup`: title scoring + English→Czech surname variants
"""

from __future__ import annotations
