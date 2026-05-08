"""Streaming filter that strips inline animation tags from an LLM
text stream and dispatches `trigger_animation` for each closed `<xxx>`.

Path A in the dual-channel design: the model writes
    <explain> The mensa is on the second floor.
We yank `<explain> ` out before it reaches TTS and fire the matching
gesture asynchronously.

Wired in by overriding `tts_node` on the GreetingAgent. Pattern follows
LiveKit's simple_content_filter recipe:
https://docs.livekit.io/recipes/simple_content_filter/

Why angle brackets, not square: vLLM's `--tool-call-parser llama3_json`
treats `[name(args)]` and even `[name]` as a function call (Llama
3.1's legacy tool-call syntax). Square brackets at the start of an
assistant turn turned `[greet] Hello` into a tool call to a function
named `greet` — which doesn't exist, and the LLM never produced
spoken text. Angle brackets sidestep that grammar entirely.
"""

from __future__ import annotations

import asyncio
from typing import AsyncIterable

from tools._animation import trigger_animation


# Max bytes we'll buffer while waiting for `>`. Sized for the longest
# legitimate tag (`<dont_know>` = 11 chars) plus slack. If a `<` opens
# and no `>` arrives within this window, we treat the buffer as not-a-tag
# and flush it verbatim — handles user-quoted text like "<3 m wide>".
_MAX_TAG_LEN = 24


def _dispatch(name: str) -> None:
    """Fire-and-forget. Schedule the trigger on the running loop so the
    parser yields downstream text immediately without awaiting the POST."""
    try:
        asyncio.get_running_loop().create_task(trigger_animation(name))
    except RuntimeError:
        # No running loop (shouldn't happen inside tts_node); skip silently.
        pass


async def filter_animation_tags(
    text_stream: AsyncIterable[str],
) -> AsyncIterable[str]:
    """Async generator: consume `text_stream` chunks, yield cleaned text.

    State machine:
      OUTSIDE  → on '<' switch to INSIDE, start buffering
      INSIDE   → on '>' close, dispatch animation, drop buffer
               → on overflow (>_MAX_TAG_LEN since '<') flush buffer
                 verbatim and resume OUTSIDE
    """
    buffer = ""           # inside-bracket accumulator (excludes the `<`)
    inside = False

    async for chunk in text_stream:
        if not chunk:
            continue
        out: list[str] = []
        for ch in chunk:
            if not inside:
                if ch == "<":
                    inside = True
                    buffer = ""
                else:
                    out.append(ch)
                continue
            # inside == True
            if ch == ">":
                # Closed tag — dispatch if it resolves, otherwise drop
                # silently (silent no-op fallback per plan §"Fallback").
                if buffer:
                    _dispatch(buffer)
                inside = False
                buffer = ""
                continue
            if ch == "<":
                # Nested `<` while still inside an unclosed one: flush the
                # previous buffer as literal text (it wasn't a tag) and
                # start a fresh buffer at the new `<`.
                out.append("<")
                out.append(buffer)
                buffer = ""
                continue
            buffer += ch
            if len(buffer) > _MAX_TAG_LEN:
                # Overflow: not a tag, flush as literal text.
                out.append("<")
                out.append(buffer)
                inside = False
                buffer = ""
        if out:
            yield "".join(out)

    # Stream ended. If we're still inside an unclosed bracket, flush it
    # verbatim so partial tokens never silently disappear.
    if inside and buffer:
        yield "<" + buffer
    elif inside:
        yield "<"
