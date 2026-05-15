"""Tool surface for the experiment agent.

Each `@function_tool` lives in its own module here:

  - send_message_to_user.py  : terminal tool (only way Pepper speaks)
  - lookup_person.py         : staff directory with EN→CZ surname fallback
  - find_path_to_room.py     : Building E directions (curated + FelSight)
  - mensa_menu.py            : canteen menu fetch
  - subject_schedule.py      : public timetable by short course code
  - get_time.py              : current local time
  - adjust_volume.py         : step Pepper's speaker volume ±20 via state.json
  - display_info.py          : (DISABLED — transcript is on the tablet already)
  - query_search.py          : hybrid vector search (NOT in default surface)

Shared helpers live in tools/utils/. The exported list
`LIVEKIT_TOOLS_TOOLONLY` is what the agent passes to LiveKit. The
event-listener hooks (`set_tool_event_listener`,
`set_tool_result_listener`) are re-exported here for convenience.
"""

from __future__ import annotations

# Import the path-glue side effect first so subsequent `from src.* …`
# imports inside tool modules resolve regardless of run mode.
from .utils._common import _VOICE_AGENT_DIR  # noqa: F401

from .utils._events import (  # noqa: F401
    set_tool_event_listener,
    set_tool_result_listener,
)

from .send_message_to_user import send_message_to_user
from .lookup_person import lookup_person
from .find_path_to_room import find_path_to_room
from .mensa_menu import mensa_menu
from .subject_schedule import subject_schedule
from .get_time import get_time
from .adjust_volume import adjust_volume
from .end_conversation import end_conversation
# query_search intentionally NOT in the default surface — its broad
# trigger hijacks turn-1 with cascading search loops on bare greetings.
# Re-add by importing and listing it below once the trigger is tightened.

LIVEKIT_TOOLS_TOOLONLY = [
    send_message_to_user,
    find_path_to_room,
    lookup_person,
    get_time,
    mensa_menu,
    subject_schedule,
    adjust_volume,
    end_conversation,
]
