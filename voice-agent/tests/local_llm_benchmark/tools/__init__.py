"""LiveKit tool surface for the local-LLM benchmark.

Each tool lives in its own module and exposes a single `@function_tool`-
decorated coroutine. Run any tool file directly to invoke it with
hand-edited arguments and inspect the JSON return — see the
`if __name__ == "__main__":` block at the bottom of each file.
"""

from __future__ import annotations

from tools.find_path_to_room import find_path_to_room
from tools.get_time import get_time
from tools.list_events import list_events
from tools.lookup_person import lookup_person
from tools.mensa_menu import mensa_menu
from tools.play_animation import play_animation
from tools.query_search import query_search
from tools.subject_schedule import subject_schedule

LIVEKIT_TOOLS = [
    # query_search is dropped — its broad "any factual question" trigger
    # was hijacking turn-1 with cascading search loops on bare greetings.
    # Re-add only after retraining the tool description for a stricter
    # trigger.
    find_path_to_room,
    lookup_person,
    # play_animation is not LLM-visible here — it's fired by
    # AnimationDirector via a forced one-tool LLM pass on every agent
    # reply, see animation_director.py.
    get_time,
    mensa_menu,
    subject_schedule,
]
