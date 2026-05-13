"""play_gesture tool — fire a silent body-language animation.

Mode B (realtime) speaks natively, so the per-utterance `emotion`
argument that send_message_to_user carries in Mode A doesn't exist
here. This tool gives the realtime model an explicit way to trigger
Pepper's gestures alongside a spoken reply.

Returns a small acknowledgment payload (NOT None) so LiveKit's
`reply_required` stays True and the realtime model continues into
spoken audio after the tool call. Returning None would mark this as a
terminal tool (Letta-style) and the turn would end silently — which
is exactly the bug that bit us when the model called goodbye and
then said nothing.
"""

from __future__ import annotations

import asyncio

from livekit.agents import RunContext, function_tool

from .utils._animation import trigger_animation
from .utils._emotion import Emotion
from .utils._events import _emit_tool_event, _heartbeat_or_none


@function_tool(name="play_gesture")
async def play_gesture(
    context: RunContext,
    emotion: Emotion,
) -> dict:
    """Play a silent body-language gesture, then KEEP SPEAKING.

    USAGE CONTRACT (read carefully):
      1. Invoke this tool as a function call. NEVER speak the words
         "play_gesture", the emotion name, or any parenthetical
         like "(greet)" — the user must not hear the mechanism.
      2. After this tool returns {"ok": true, "gesture": "..."},
         IMMEDIATELY continue with your spoken reply in the same
         turn. The gesture plays in parallel with your voice; it
         does NOT replace your words. A tool call without a
         following spoken reply leaves the user in silence — that
         is broken behaviour.
      3. Use one gesture per reply. Call it just BEFORE you speak.

    Pick by INTENT of what you're about to say:
      greet         hello / welcoming a guest
      bow           formal acknowledgement, polite thank-you
      goodbye       closing the interaction, sign-off
      affirm        yes / confirming / agreeing
      deny          no / refusing
      think         looking something up, "let me check…"
      explain       delivering a factual / informational answer
      emphasis      strong point during a sentence
      whisper       discreet / lowered-voice info
      question      asking the user something back
      calm          reassuring an upset user
      offer         handing or presenting something (a value, directions)
      address_user  pointing / referring to the user
      dont_know     uncertain, "I don't have that info"
      speak_neutral default filler when nothing else fits

    Returns: {"ok": true, "gesture": "<emotion>",
              "next": "continue with your spoken reply now"}.
    """
    del context
    print(f"  [tool] play_gesture(emotion={emotion!r})")
    _emit_tool_event("play_gesture", {"emotion": emotion})
    if emotion:
        asyncio.create_task(trigger_animation(emotion))
    payload = {
        "ok": True,
        "gesture": emotion,
        "next": "continue with your spoken reply now",
    }
    return _heartbeat_or_none(payload, request_heartbeat=True)
