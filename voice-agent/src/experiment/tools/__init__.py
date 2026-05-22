"""Tool surface for the streaming experiment agents.

Each `@function_tool` lives in its own module here:

  - lookup_person.py             : staff directory with EN→CZ surname fallback
  - find_path_to_room.py         : Building E directions (curated + FelSight)
  - mensa_menu.py                : canteen menu fetch
  - subject_schedule.py          : public timetable by short course code
  - get_time.py                  : current local time
  - query_search.py              : hybrid vector search over FEE docs
  - end_conversation_streaming.py: terminal tool, plays farewell + ends session

The streaming agents (`agent_streaming.py`, `agent_4o_streaming.py`)
import each tool directly via `from tools.X import X`, so this package
only needs to expose the path-glue side effect and the event-listener
hooks used by the recorder.
"""

from __future__ import annotations

# Import the path-glue side effect first so subsequent `from src.* …`
# imports inside tool modules resolve regardless of run mode.
from .utils._common import _VOICE_AGENT_DIR  # noqa: F401

from .utils._events import (  # noqa: F401
    set_tool_event_listener,
    set_tool_result_listener,
)
