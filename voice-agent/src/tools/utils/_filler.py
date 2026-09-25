"""Pre-canned filler TTS for slow tools.

Speaks a short uninterruptible utterance via the active session so the
user gets immediate audio feedback while the real tool work runs in
parallel. Fire-and-forget: the caller schedules it with
`asyncio.create_task(_speak_filler(context, "..."))` and continues
straight into the tool's network call.

allow_interruptions is hardcoded False — the filler must not be
cancelled by Pepper's own audio bleeding into the mic, which is the
whole reason this layer exists.
"""

from __future__ import annotations

import os
import time

from livekit.agents import RunContext

from ._events import emit_experiment_event, _current_tool_name

# Master switch — set TOOL_FILLERS=0 to silence every tool filler at
# once without touching the call sites. Default: enabled.
TOOL_FILLERS_ENABLED = os.environ.get("TOOL_FILLERS", "1").strip().lower() in (
    "1", "true", "yes", "on",
)


async def _speak_filler(context: RunContext, text: str) -> None:
    if not TOOL_FILLERS_ENABLED:
        return
    text = (text or "").strip()
    if not text:
        return
    source_tool = _current_tool_name.get()
    started = time.monotonic()
    try:
        handle = context.session.say(text, allow_interruptions=False)
        await handle.wait_for_playout()
        emit_experiment_event("filler_spoken", {
            "tool": source_tool,
            "filler_text": text,
            "playout_ms": int((time.monotonic() - started) * 1000),
        })
    except Exception as exc:
        print(f"  [filler] playout error: {exc!r}")
        emit_experiment_event("error", {
            "component": "filler",
            "message": f"playout error: {exc!r}",
            "tool": source_tool,
            "recovered": True,
        })
