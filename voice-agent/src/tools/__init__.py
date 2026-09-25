"""Tool surface for the cascade agent (`agent.py`); `realtime/` holds the
realtime agent's two tools.

Each `@function_tool` lives in its own module here:

  - lookup_person.py             : staff directory with EN→CZ surname fallback
  - find_path_to_room.py         : Building E directions (curated + FelSight)
  - mensa_menu.py                : canteen menu fetch
  - subject_schedule.py          : public timetable by short course code
  - get_time.py                  : current local time
  - end_conversation.py          : terminal tool, plays farewell + ends session

`agent.py` imports each tool directly via `from tools.X import X`, so
this package only re-exports the event-listener hooks used by the
recorder.
"""

from __future__ import annotations

from .utils._events import (  # noqa: F401
    set_tool_event_listener,
    set_tool_result_listener,
)
