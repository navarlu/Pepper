"""adjust_volume tool — make Pepper speak louder or quieter.

POSTs `/audio/volume {"delta": +VOLUME_STEP|-VOLUME_STEP}` to the
bridge. The bridge applies the change via `ALAudioDevice`, clamps
to 0..100, and writes its own `state.json`. Calling the bridge over
HTTP is the only correct option — the agent runs on woska while
`state.json` lives on the rpi, so a local file write would never
reach Pepper.

No `emotion` argument: changing volume isn't a body-language moment.
The agent should pick a gesture (often `affirm` or `speak_neutral`)
on the next `send_message_to_user`.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

from livekit.agents import RunContext, function_tool

from .utils._events import _emit_tool_event, _heartbeat_or_none

from src.live.bridge_client import post_volume_delta  # noqa: E402
from src.live.config import VOLUME_STEP  # noqa: E402


@function_tool(name="adjust_volume")
async def adjust_volume(
    context: RunContext,
    direction: Literal["louder", "quieter"],
    request_heartbeat: bool = True,
) -> Any:
    """Make Pepper speak louder or quieter for future replies.

    Each call steps the speaker volume by 20 (clamped to 0..100). The
    change applies within about one second — your NEXT
    `send_message_to_user` will be at the new volume; the current
    utterance is unaffected.

    Use when the user explicitly asks ("speak up", "louder please",
    "too loud", "can you be quieter"). Don't volunteer it.

    direction: 'louder' to step volume up, 'quieter' to step it down.
    request_heartbeat: True (default) so you can speak via
        send_message_to_user after.
    """
    del context
    print(f"  [tool] adjust_volume(direction={direction!r}, hb={request_heartbeat})")
    _emit_tool_event(
        "adjust_volume",
        {"direction": direction, "request_heartbeat": request_heartbeat},
    )

    if direction not in ("louder", "quieter"):
        return _heartbeat_or_none(
            {
                "error": "invalid_direction",
                "instruction": (
                    "direction must be 'louder' or 'quieter'. Skip and "
                    "speak via send_message_to_user."
                ),
            },
            request_heartbeat,
        )

    delta = int(VOLUME_STEP) if direction == "louder" else -int(VOLUME_STEP)

    try:
        status, body = await asyncio.to_thread(post_volume_delta, delta)
    except Exception as exc:
        print(f"  [tool] adjust_volume bridge error: {exc!r}")
        return _heartbeat_or_none(
            {
                "ok": False,
                "error": "bridge_unreachable",
                "message": str(exc),
                "instruction": (
                    "Couldn't change the volume — speak normally via "
                    "send_message_to_user without mentioning the failure."
                ),
            },
            request_heartbeat,
        )

    if status != 200 or not body.get("ok"):
        print(f"  [tool] adjust_volume bridge bad-response status={status} body={body!r}")
        return _heartbeat_or_none(
            {
                "ok": False,
                "status": status,
                "error": body.get("error", "bridge_error"),
                "instruction": (
                    "Couldn't change the volume — speak normally via "
                    "send_message_to_user without mentioning the failure."
                ),
            },
            request_heartbeat,
        )

    previous = int(body.get("previous", 0))
    new_volume = int(body.get("volume", 0))
    clamped = bool(body.get("clamped", False))
    changed = new_volume != previous

    if not changed:
        instruction = (
            "Volume already at the {} (={}). Briefly tell the user via "
            "send_message_to_user that it's already as {} as it goes."
        ).format(
            "maximum" if direction == "louder" else "minimum",
            new_volume,
            "loud" if direction == "louder" else "quiet",
        )
    else:
        instruction = (
            "Volume changed to {} (was {}). Briefly acknowledge via "
            "send_message_to_user — keep it short, one sentence."
        ).format(new_volume, previous)

    return _heartbeat_or_none(
        {
            "ok": True,
            "previous": previous,
            "volume": new_volume,
            "changed": changed,
            "clamped": clamped,
            "instruction": instruction,
        },
        request_heartbeat,
    )
