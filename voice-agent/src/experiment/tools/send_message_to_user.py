"""Terminal tool — only way for the model to talk to the user.

Returns None so LiveKit's `reply_required` is False and the loop ends
as soon as Pepper finishes speaking — Letta TerminalToolRule pattern,
native to LiveKit (see generation.py:814).
"""

from __future__ import annotations

import asyncio
import os
import re

from livekit.agents import RunContext, function_tool

from .utils._animation import trigger_animation
from .utils._emotion import Emotion
from .utils._events import _emit_tool_event

# Flip via env to A/B without code changes. Default = robust mode: user
# cannot interrupt Pepper mid-utterance (kills false barge-in from
# Pepper's own audio in the mic + from premature user re-talking).
ALLOW_INTERRUPTIONS = os.environ.get("ALLOW_INTERRUPTIONS", "0").strip().lower() in (
    "1", "true", "yes", "on",
)

# Sentence boundary: punctuation followed by whitespace. Speak each
# sentence as its own session.say() so the first sentence reaches
# audio in ~250ms instead of waiting for the whole utterance to synth.
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT_SPLIT.split(text) if p.strip()]
    return parts or [text]


@function_tool(name="send_message_to_user")
async def send_message_to_user(
    context: RunContext,
    text: str,
    emotion: Emotion = "explain",
) -> None:
    """Speak to the user. THIS IS THE ONLY WAY YOUR WORDS REACH THE
    USER. Plain assistant text is never spoken — only the `text`
    argument of this tool.

    Calling this tool ENDS the current turn. After speaking you do
    not get to call another tool, so finish gathering information
    BEFORE calling this. Use exactly once per user turn, as the
    final action.

    text: the words Pepper will say. TTS reads this verbatim, so
        write plain conversational prose with no markdown, no JSON,
        no bracketed stage directions, no tool names.
    emotion: body language for this utterance — greet, think,
        explain, bow, happy, dont_know.
    """
    text_clean = (text or "").strip()
    print(f"  [tool] send_message_to_user(emotion={emotion!r}, text={text_clean!r})")
    _emit_tool_event("send_message_to_user", {"text": text_clean, "emotion": emotion})
    if not text_clean:
        return None

    if emotion:
        asyncio.create_task(trigger_animation(emotion))

    for sentence in _split_sentences(text_clean):
        handle = context.session.say(sentence, allow_interruptions=ALLOW_INTERRUPTIONS)
        try:
            await handle.wait_for_playout()
        except Exception as exc:
            print(f"  [tool] send_message_to_user playout error: {exc!r}")
            break

    return None  # ← Terminal: reply_required=False, loop ends.
