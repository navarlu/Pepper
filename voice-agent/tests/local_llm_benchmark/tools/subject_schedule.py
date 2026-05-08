"""subject_schedule: public timetable lookup for FEE subjects."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Literal
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
import re
import traceback

from livekit.agents import RunContext, function_tool

from tools._animation import trigger_animation
from tools._common import _json, _run_main

from src.timetable import fetch_subject_schedule  # noqa: E402

_SUBJECT_CODE = re.compile(r"^[A-Za-z]{2,4}\d?$")
_Gesture = Literal["greet", "think", "explain", "bow", "happy", "dont_know"]


@function_tool
async def subject_schedule(
    context: RunContext,
    subject: str,
    activity: str = "",
    day: str = "",
    gesture: _Gesture = "think",
) -> str:
    """Look up the public timetable for a FEE subject.

    Use this when the user asks when or where a subject's lecture, lab,
    exercise, or tutorial happens.

    The `subject` argument MUST be a short intranet course code: 2 to 4
    letters with an optional single trailing digit (e.g. XYZ, AB, WXYZ,
    XYZ1, AB2). Course names, full codes like Z3Z33XYZ, partial words, or
    anything else are NOT accepted. If the user gives a course name, ask
    them for the short code before calling — they can read it from the
    intranet schedule URL. Do NOT guess codes.

    `activity` is the SESSION-TYPE filter. If the user mentioned the
    KIND of session, you MUST pass the matching value:
      - "lecture"     → user said lecture / přednáška / class
      - "exercise"    → user said exercise / practice / tutorial /
                         seminar / cvičení
      - "laboratory"  → user said lab / laboratory / laboratoř
      - ""            → user did not specify a session type
    Leaving `activity` empty when the user named a kind dumps every
    session and is wrong.

    `day` is the optional weekday filter: empty, Monday, Tuesday,
    Wednesday, Thursday, or Friday.

    Always read the `instruction` field of the result and follow it.

    subject: short intranet course code, 2 to 4 letters with optional trailing digit.
    activity: session type — "lecture", "exercise", "laboratory", or "".
    day: optional weekday filter.
    gesture: Pepper body language while fetching. Default 'think'.
        One of greet, think, explain, bow, happy, dont_know.
    """
    del context
    if gesture:
        asyncio.create_task(trigger_animation(gesture))
    subject_query = str(subject or "").strip()
    activity_query = str(activity or "").strip()
    day_query = str(day or "").strip()
    print(
        f"  [tool] subject_schedule({subject_query!r}, "
        f"{activity_query!r}, {day_query!r})"
    )

    if not _SUBJECT_CODE.match(subject_query):
        payload = {
            "status": "invalid_code",
            "received": subject_query,
            "instruction": (
                "The `subject` argument must be a short intranet course "
                "code: 2 to 4 letters with an optional single trailing "
                f"digit (e.g. XYZ, AB, WXYZ, XYZ1). You passed "
                f"'{subject_query}', which is not a valid short code. Ask "
                "the user for the correct code and call subject_schedule "
                "again with that code as `subject`. Do not guess the code "
                "yourself."
            ),
        }
        print(f"  [tool] subject_schedule rejected: invalid_code received={subject_query!r}")
        return _json(payload)

    try:
        result = await asyncio.to_thread(
            fetch_subject_schedule,
            subject_query.upper(),
            activity_query,
            day_query,
        )
    except Exception as exc:
        print(
            f"  [tool] subject_schedule EXCEPTION subject={subject_query!r} "
            f"activity={activity_query!r} day={day_query!r} error={exc!r}"
        )
        traceback.print_exc()
        return _json({
            "status": "error",
            "error": "subject_schedule_failed",
            "message": str(exc),
            "instruction": (
                "The timetable service errored. Apologise briefly and ask "
                "the user to try again in a moment. Do not retry on your own."
            ),
        })

    status = result.get("status", "")
    resolution = result.get("resolution", "")

    slim: dict[str, Any] = {"status": status}

    if status == "not_found":
        slim["instruction"] = (
            f"No subject with code '{subject_query.upper()}' was found. "
            "Tell the user the code is unknown and ask them to double-check "
            "the short code on the intranet schedule URL. Do not guess."
        )
    elif status == "ambiguous":
        matches = result.get("matches") or []
        slim["candidates"] = [
            {"code": m.get("code"), "name": m.get("name")} for m in matches[:5]
        ]
        slim["instruction"] = (
            "Several subjects share that short code. Read 2–3 candidate "
            "names back to the user and ask which one they meant. Do NOT "
            "call this tool again until the user answers."
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
        unfiltered_warning = (
            ""
            if activity_query
            else (
                " The user did not specify a session type, so the list "
                "MAY contain mixed activities (lectures, exercises, "
                "laboratories). If the user's question was actually "
                "about ONE session type, mention only events whose "
                "`activity` matches what they asked about."
            )
        )
        if resolution == "multiple_codes_same_subject":
            slim["note"] = "multiple_codes_same_subject"
            #slim["instruction"] = (
                #"Multiple full subject codes share the same events for this "
                #"short code. Answer using the shared events and briefly "
                #"mention which full codes were found."
            #)
            slim["instruction"] = ("")
        elif slim["count"] == 0:
            slim["instruction"] = (
                "The subject exists but no event matches the requested filters. "
                "Tell the user that, and offer to look up another day or activity."
            )
        elif slim["count"] > 1:
            slim["instruction"] = (
                f"There are {slim['count']} distinct sessions for this subject. "
                "List EVERY session in the `events` list — for each, say "
                "the day, start time, room, and teachers. Do not skip any. "
                "Speak the start time only as the hour (e.g. 'at eleven', "
                "not '11:00 to 12:30') — your reply goes to TTS and "
                "students already know how long classes last. Do not "
                "mention end times or week ranges unless the user asks."
                + unfiltered_warning
            )
        else:
            slim["instruction"] = (
                "Mention day, start time, room, and teachers from the single "
                "matching event. Speak the start time only as the hour (e.g. "
                "'at eleven', not '11:00 to 12:30') — your reply goes to "
                "TTS and students already know how long classes last. Do "
                "not mention end times or week ranges unless the user asks."
            )
    else:
        slim["instruction"] = result.get("message", "")

    print(
        f"  [tool] subject_schedule result status={status!r} "
        f"resolution={resolution!r} events={len(slim.get('events') or [])}"
    )
    return _json(slim)


if __name__ == "__main__":
    SUBJECT = "ARI1"
    ACTIVITY = ""  # "", "lecture", "exercise", "laboratory"
    DAY = ""  # "", "Monday", ..., "Friday"
    _run_main(subject_schedule(None, subject=SUBJECT, activity=ACTIVITY, day=DAY))
