"""Czech tool surface for the experiment agent.

Re-exports the same tools as `tools/` but with Czech docstrings. All
business logic is shared by reference with the English tools — only
the schema metadata (description + parameter docs) is localized via
`_localize.localize`.

Selected at boot by setting EXPERIMENT_TOOLS_MODULE=tools_cs in
`agent_cs.py`. The English `agent.py` keeps its default `tools`
import path unchanged.

Tool argument *values* (emotion enum, activity strings, day names,
direction values) stay English — they are API contracts the LLM
emits verbatim into tool calls, not user-facing text.
"""

from __future__ import annotations

# Re-export event hooks from the English package so the worker's
# `tools.set_tool_event_listener` wiring keeps working unchanged.
from ..tools.utils._events import (  # noqa: F401
    set_tool_event_listener,
    set_tool_result_listener,
)

from ..tools import (
    send_message_to_user as _en_send_message_to_user,
    lookup_person as _en_lookup_person,
    find_path_to_room as _en_find_path_to_room,
    mensa_menu as _en_mensa_menu,
    subject_schedule as _en_subject_schedule,
    get_time as _en_get_time,
    adjust_volume as _en_adjust_volume,
)

from . import _docs as D
from ._localize import localize

send_message_to_user = localize(_en_send_message_to_user, D.SEND_MESSAGE_TO_USER)
lookup_person = localize(_en_lookup_person, D.LOOKUP_PERSON)
find_path_to_room = localize(_en_find_path_to_room, D.FIND_PATH_TO_ROOM)
mensa_menu = localize(_en_mensa_menu, D.MENSA_MENU)
subject_schedule = localize(_en_subject_schedule, D.SUBJECT_SCHEDULE)
get_time = localize(_en_get_time, D.GET_TIME)
adjust_volume = localize(_en_adjust_volume, D.ADJUST_VOLUME)

LIVEKIT_TOOLS_TOOLONLY = [
    send_message_to_user,
    find_path_to_room,
    lookup_person,
    get_time,
    mensa_menu,
    subject_schedule,
    adjust_volume,
]
