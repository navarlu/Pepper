"""subject_schedule tool — public timetable lookup by short course code."""

from __future__ import annotations

import asyncio
import re
import traceback
from typing import Any

from livekit.agents import RunContext, function_tool

from .utils._animation import trigger_animation
from .utils._emotion import Emotion
from .utils._events import _emit_tool_event, _heartbeat_or_none

from src.live.timetable import fetch_subject_schedule  # noqa: E402


_SUBJECT_CODE = re.compile(r"^[A-Za-z]{2,4}\d?$")


@function_tool(name="subject_schedule")
async def subject_schedule(
    context: RunContext,
    subject: str,
    activity: str = "",
    day: str = "",
    emotion: Emotion = "think",
    request_heartbeat: bool = True,
) -> Any:
    """Look up the public timetable for a course / subject.

    Use this when the user asks when or where a subject's lecture,
    lab, exercise, or tutorial happens.

    The `subject` argument MUST be a short course code: 2 to 4
    letters with an optional single trailing digit (e.g. XYZ, AB,
    WXYZ, XYZ1, AB2). Course names or long codes like A1B23CDE are
    NOT accepted — ask the user via send_message_to_user for the
    short code.

    activity: SESSION-TYPE filter. Pass "lecture" / "exercise" /
    "laboratory" if the user named the kind, else "".
    day: optional weekday filter — empty, Monday, ..., Friday.

    Always read the `instruction` field of the result and follow it.

    subject: short intranet course code, 2 to 4 letters with optional digit.
    activity: session-type filter or "".
    day: optional weekday filter.
    emotion: body language while fetching. Default 'think'.
    request_heartbeat: True (default) to continue.
    """
    del context
    subject_q = (subject or "").strip()
    activity_q = (activity or "").strip()
    day_q = (day or "").strip()
    print(
        f"  [tool] subject_schedule({subject_q!r}, {activity_q!r}, "
        f"{day_q!r}, emotion={emotion!r}, hb={request_heartbeat})"
    )
    _emit_tool_event("subject_schedule", {
        "subject": subject_q, "activity": activity_q, "day": day_q,
        "emotion": emotion, "request_heartbeat": request_heartbeat,
    })
    if emotion:
        asyncio.create_task(trigger_animation(emotion))

    if not _SUBJECT_CODE.match(subject_q):
        return _heartbeat_or_none({
            "status": "invalid_code",
            "received": subject_q,
            "instruction": (
                "The `subject` argument must be a short intranet "
                "course code: 2 to 4 letters with an optional single "
                f"trailing digit. You passed '{subject_q}'. Ask the "
                "user via send_message_to_user for the correct code."
            ),
        }, request_heartbeat)

    try:
        result = await asyncio.to_thread(
            fetch_subject_schedule, subject_q.upper(), activity_q, day_q,
        )
    except Exception as exc:
        traceback.print_exc()
        return _heartbeat_or_none({
            "status": "error",
            "error": "subject_schedule_failed",
            "message": str(exc),
            "instruction": (
                "The timetable service errored. Apologise via "
                "send_message_to_user and ask the user to try again."
            ),
        }, request_heartbeat)

    status = result.get("status", "")
    resolution = result.get("resolution", "")
    slim: dict[str, Any] = {"status": status}

    if status == "not_found":
        slim["instruction"] = (
            f"No subject with code '{subject_q.upper()}' was found. "
            "Tell the user via send_message_to_user the code is "
            "unknown and ask them to double-check the short code."
        )
    elif status == "ambiguous":
        matches = result.get("matches") or []
        slim["candidates"] = [
            {"code": m.get("code"), "name": m.get("name")} for m in matches[:5]
        ]
        slim["instruction"] = (
            "Several subjects share that short code. Read 2–3 "
            "candidate names back to the user via "
            "send_message_to_user and ask which one they meant."
        )
    elif status == "ok":
        events = result.get("events") or []
        slim["count"] = len(events)
        slim["events"] = [
            {
                "activity": e.get("activity"),
                "day": e.get("day"),
                "start": e.get("start"),
                "room": e.get("room"),
                "teachers": e.get("teachers") or [],
            }
            for e in events
        ]
        if resolution == "multiple_codes_same_subject":
            slim["note"] = "multiple_codes_same_subject"
            slim["instruction"] = ""
        elif slim["count"] == 0:
            slim["instruction"] = (
                "Subject exists but no event matches the filters. "
                "Tell the user via send_message_to_user."
            )
        else:
            slim["instruction"] = (
                "In your send_message_to_user, mention day, start "
                "time (only the hour), room, and teachers. No end "
                "times, no week ranges unless the user asks."
            )
    else:
        slim["instruction"] = result.get("message", "")

    return _heartbeat_or_none(slim, request_heartbeat)
