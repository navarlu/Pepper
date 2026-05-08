"""display_info tool — push a short value to Pepper's tablet.

No `emotion` argument: the tablet update doesn't drive an animation;
pick body language separately when the next send_message_to_user
runs.
"""

from __future__ import annotations

import asyncio
from typing import Any

from livekit.agents import RunContext, function_tool

from .utils._events import _emit_tool_event, _heartbeat_or_none

from src.live.bridge_client import post_tablet_info  # noqa: E402


@function_tool(name="display_info")
async def display_info(
    context: RunContext,
    text: str,
    request_heartbeat: bool = True,
) -> Any:
    """Show a short value on Pepper's tablet so the user can read /
    copy it down. ONLY for values the user would plausibly want to
    WRITE DOWN — phone numbers, email addresses, room codes, URLs,
    specific dates and times.

    Do NOT use for things the user only listens to: greetings, prose
    answers, opinions, meal names, dish descriptions, subject names,
    person names without contact info, opening-hour sentences, etc.
    Words that are easy to hear once and remember should not go on
    the tablet.

    The card stays visible until the next user turn, and a later
    call replaces it. You don't need to clear it.

    This tool does NOT trigger any robot animation — that's why it
    has no `emotion` argument. Pick a body language separately when
    you call `send_message_to_user` afterwards.

    text: the value to display. Plain text, kept short (≤ ~80 chars
        works best). HTML is escaped automatically. Multi-line is
        OK — just use \\n.
    request_heartbeat: True (default) to continue the loop so you can
        speak via send_message_to_user after.
    """
    del context
    text_clean = (text or "").strip()
    print(
        f"  [tool] display_info(text={text_clean!r}, hb={request_heartbeat})"
    )
    _emit_tool_event("display_info", {
        "text": text_clean, "request_heartbeat": request_heartbeat,
    })

    if not text_clean:
        return _heartbeat_or_none({
            "error": "missing_text",
            "instruction": (
                "display_info needs a non-empty `text`. Skip this "
                "tool and just speak via send_message_to_user."
            ),
        }, request_heartbeat)

    try:
        status, _ = await asyncio.to_thread(post_tablet_info, text_clean)
    except Exception as exc:
        # Bridge unreachable / Pepper offline — log and continue. Not
        # being able to draw on the tablet must never break the spoken
        # reply, so we still let the loop continue.
        print(f"  [tool] display_info bridge error: {exc!r}")
        return _heartbeat_or_none({
            "ok": False,
            "error": "tablet_bridge_unreachable",
            "message": str(exc),
            "instruction": (
                "Tablet display unavailable. Speak the value normally "
                "via send_message_to_user."
            ),
        }, request_heartbeat)

    return _heartbeat_or_none({
        "ok": status == 200,
        "status": status,
        "displayed": text_clean,
        "instruction": (
            "Now call send_message_to_user with a short spoken reply. "
            "You don't need to read the displayed value out loud "
            "verbatim — the user can see it on the tablet."
        ),
    }, request_heartbeat)
