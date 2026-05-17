"""Shared mutable state between streaming workers and their tools.

The production stack uses a `SessionRuntime` singleton wired up by
`_pipeline.run_pipeline`. The streaming workers don't go through that
pipeline (they have a lighter, no-mic-mute lifecycle), so this module
re-creates the minimum shared state the streaming tools need to talk
back to the worker:

  * `student_id` — set from dispatch metadata; used by
    `end_conversation_streaming` for the QR conversation code (T01, …).
  * `end_session_callback` — async callable the worker registers so a
    tool can request graceful shutdown (`/done`-equivalent).
  * `room` — live `rtc.Room` reference so a tool can publish
    `pepper.state` updates (e.g. `farewell_active=True` to stop
    tablet-server from re-rendering chat over the QR).

Kept deliberately tiny and global — anything more elaborate (locks,
multi-session safety) would conflate concerns with `_pipeline`'s much
larger `SessionRuntime`. Streaming workers run one job at a time per
process, so module-level globals are safe.
"""
from __future__ import annotations

import json
from typing import Any, Awaitable, Callable

_state: dict[str, Any] = {
    "student_id": None,
    "end_session": None,
    "room": None,
}


def reset() -> None:
    """Clear all fields. Called at session start so the previous job's
    state cannot leak into the new one (workers re-register their
    callbacks on every entrypoint)."""
    _state["student_id"] = None
    _state["end_session"] = None
    _state["room"] = None


def set_student_id(sid: int | None) -> None:
    _state["student_id"] = sid


def get_student_id() -> int | None:
    return _state["student_id"]


def set_room(room) -> None:
    _state["room"] = room


def get_room():
    return _state["room"]


def set_end_session_callback(
    cb: Callable[[str], Awaitable[None]] | None,
) -> None:
    _state["end_session"] = cb


async def request_end_session(reason: str = "") -> None:
    """Ask the worker to start its graceful-drain shutdown path.

    No-op if the worker hasn't registered a callback (e.g. tool ran
    after the worker already torn itself down). Errors are swallowed
    because the tool's caller cannot meaningfully react to them — the
    worker process is about to die anyway.
    """
    cb = _state.get("end_session")
    if cb is None:
        return
    try:
        await cb(reason)
    except Exception:
        pass


async def publish_state(payload: dict, *, topic: str = "pepper.state") -> None:
    """Publish a `pepper.state` update from a tool, best-effort.

    Only used by the streaming variant for the two non-mic-related
    fields tablet-server already understands:
      * `farewell_active`: gates tablet-server's chat re-renders so a
        QR posted by `end_conversation_streaming` doesn't flicker.
      * `agent_mode`: drives the mode pill on Pepper's tablet.

    Deliberately does NOT publish `mic_muted` — the streaming workers
    are AEC-driven, not state-driven, and bringing the mute hack back
    would defeat the design.
    """
    room = _state.get("room")
    if room is None:
        return
    try:
        await room.local_participant.publish_data(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            topic=topic,
        )
    except Exception:
        pass
