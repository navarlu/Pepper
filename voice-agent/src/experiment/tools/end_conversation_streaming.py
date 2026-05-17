"""Streaming-mode end_conversation tool.

Strips the production [`end_conversation`](end_conversation.py)
machinery down to what the streaming workers actually need:

  1. Build the participant-code QR (uses `EXPERIMENT_FEEDBACK_URL` +
     student id from `_streaming_runtime`).
  2. Publish `farewell_active=True` on `pepper.state` so tablet-server
     stops posting chat re-renders over the QR. (No `mic_muted` field
     is ever published — see `_streaming_runtime.publish_state`.)
  3. POST the QR HTML to the bridge tablet endpoint.
  4. Speak a short farewell + the fixed conversation-ID reminder via
     `context.session.say()` and `await wait_for_playout()` — this
     is the natural streaming-TTS path, no `send_message_to_user`
     wrapping needed.
  5. Call `_streaming_runtime.request_end_session()` which flips the
     worker's shutdown_mode to "drain" + sets its shutdown_event.
     The worker's discriminated teardown then runs
     `session.shutdown(drain=True)` followed by `aclose()`.

The "QR stays visible" window is enforced by tablet-server's own
auto-clear timer + `loop_launcher_streaming.py`'s
`--inter-session-pause` between sessions. We do not sleep inside the
tool — that would needlessly keep the worker alive after audio is
done.

Registered with the same tool name (`end_conversation`) as the
production tool, so the prompt and the LLM tool-call convention stay
identical across modes; the streaming workers just import THIS file
instead of `end_conversation.py`.
"""

from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import quote

import segno

from livekit.agents import RunContext, function_tool

from src.live.bridge_client import post_tablet_farewell  # noqa: E402
from src.live.config import (  # noqa: E402
    EXPERIMENT_FAREWELL_DISPLAY_SEC,
    EXPERIMENT_FEEDBACK_ID_ENTRY,
    EXPERIMENT_FEEDBACK_URL,
)

from .utils._animation import trigger_animation
from .utils._emotion import Emotion
from .utils._events import _emit_tool_event

# Streaming-mode shared state lives one level up from the tools package.
# Absolute import (rather than a relative ..) because tools are loaded
# both as `experiment.tools.end_conversation_streaming` (when the
# package is imported) and as `tools.end_conversation_streaming` (when
# the workers run from inside src/experiment/). The path glue in
# `tools.utils._common` ensures `_streaming_runtime` resolves either
# way.
import _streaming_runtime  # noqa: E402


DEFAULT_GOODBYE_PREFIX = "Goodbye!"

# Always appended after the LLM-provided farewell so every participant
# hears the same call-to-action regardless of how the agent phrased
# its own sign-off. `{conv_id}` is the spoken-friendly form
# (e.g. "T zero one") so TTS reads it character-by-character.
FEEDBACK_REMINDER_TEMPLATE = (
    "Your conversation ID is {conv_id}. "
    "Please help me improve by scanning the QR code on my tablet or "
    "on the board and filling the questionnaire!"
)

_DIGIT_WORDS = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
}


def _speakable_conv_id(conv_id: str) -> str:
    """Convert "T01" to "T zero one" so TTS reads it character-by-
    character instead of trying to pronounce it as a word."""
    parts: list[str] = []
    for ch in conv_id:
        if ch.isdigit():
            parts.append(_DIGIT_WORDS[ch])
        elif ch.isalpha():
            parts.append(ch.upper())
    return " ".join(parts) if parts else conv_id


def _already_mentions_reminder(text: str) -> bool:
    """Llama-class models sometimes paste the call-to-action into
    their own `text` instead of just providing a sign-off. Detect that
    so we don't double up."""
    t = (text or "").lower()
    if "questionnaire" in t:
        return True
    if "qr" in t and ("scan" in t or "code" in t):
        return True
    if "conversation id" in t:
        return True
    return False


_QR_SCALE = 20
_QR_BORDER = 4

_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT_SPLIT.split(text) if p.strip()]
    return parts or [text]


def _format_conv_id(student_id: int | None) -> str:
    if student_id is None:
        return "T??"
    try:
        return f"T{int(student_id):02d}"
    except (TypeError, ValueError):
        return "T??"


def _build_qr_data_uri(payload: str) -> str:
    qr = segno.make(payload, error="m")
    return qr.png_data_uri(scale=_QR_SCALE, border=_QR_BORDER)


@function_tool(name="end_conversation")
async def end_conversation_streaming(
    context: RunContext,
    text: str | None = None,
    emotion: Emotion | None = None,
) -> None:
    """End the conversation: speak a brief farewell + show feedback QR.

    Call this ONLY when the user has clearly signalled they are done —
    "bye", "thanks that is all", "see you", "goodbye". Do NOT call
    this on greetings, mid-conversation pauses, or a passing "thanks".
    Wait for an unambiguous farewell.

    text: optional short personal sign-off (1 short sentence,
        friendly) — e.g. "See you!" or "Have a great day!". A fixed
        reminder is appended that announces the conversation ID and
        asks the user to scan the QR; do NOT mention the
        conversation ID, the QR, the tablet/board, or the
        questionnaire yourself. If omitted, a plain "Goodbye!" is
        used as the sign-off.
    emotion: optional body language for the farewell. Defaults to
        "goodbye"; only override if a different gesture clearly fits.
    """
    prefix = (text or "").strip() or DEFAULT_GOODBYE_PREFIX
    gesture: Emotion = emotion or "goodbye"

    student_id = _streaming_runtime.get_student_id()
    conv_id = _format_conv_id(student_id)

    if _already_mentions_reminder(prefix):
        spoken_text = prefix
    else:
        reminder = FEEDBACK_REMINDER_TEMPLATE.format(
            conv_id=_speakable_conv_id(conv_id),
        )
        spoken_text = f"{prefix} {reminder}"

    _emit_tool_event(
        "end_conversation",
        {"text": spoken_text, "emotion": gesture, "conv_id": conv_id},
    )

    display_seconds = max(1, int(EXPERIMENT_FAREWELL_DISPLAY_SEC))
    qr_payload = (
        f"{EXPERIMENT_FEEDBACK_URL}?usp=pp_url&"
        f"{EXPERIMENT_FEEDBACK_ID_ENTRY}={quote(conv_id, safe='')}"
    )

    print(
        f"  [TOOL] end_conversation start conv_id={conv_id} "
        f"display_seconds={display_seconds} text_chars={len(spoken_text)} "
        f"gesture={gesture!r}",
        flush=True,
    )

    try:
        qr_data_uri = _build_qr_data_uri(qr_payload)
        print(
            f"  [TOOL] end_conversation qr_built "
            f"payload_chars={len(qr_payload)} data_uri_chars={len(qr_data_uri)}",
            flush=True,
        )
    except Exception as exc:
        print(
            f"  [TOOL] end_conversation qr_build_failed err={exc!r}",
            flush=True,
        )
        qr_data_uri = ""

    # Tell tablet-server to stop posting chat re-renders so the QR is
    # not flickered over by transcript updates. ONLY field published —
    # no mic_muted, no agent_state, no agent_mode.
    await _streaming_runtime.publish_state({"farewell_active": True})

    # Post the QR once — the page contains a JS countdown timer.
    try:
        await asyncio.to_thread(
            post_tablet_farewell, conv_id, qr_data_uri, display_seconds,
        )
        print(
            f"  [TOOL] end_conversation tablet_posted",
            flush=True,
        )
    except Exception as exc:
        print(
            f"  [TOOL] end_conversation tablet_post_failed err={exc!r}",
            flush=True,
        )

    # Animation is fire-and-forget — same pattern as production
    # send_message_to_user.
    if gesture:
        asyncio.create_task(trigger_animation(gesture))

    # Speak the farewell — split into sentences so streaming-TTS can
    # start the first one while the second is still being synthesized.
    chunks = _split_sentences(spoken_text)
    print(
        f"  [SPEECH] end_conversation say_start chunks={len(chunks)} "
        f"chars={len(spoken_text)}",
        flush=True,
    )

    t0 = time.monotonic()
    for i, sentence in enumerate(chunks, start=1):
        try:
            handle = context.session.say(sentence)
            await handle.wait_for_playout()
            print(
                f"  [SPEECH] end_conversation chunk_done "
                f"i={i}/{len(chunks)} chars={len(sentence)} "
                f"elapsed_ms={(time.monotonic() - t0) * 1000.0:.1f}",
                flush=True,
            )
        except Exception as exc:
            print(
                f"  [SPEECH] end_conversation playout_error "
                f"chunk={i}/{len(chunks)} err={exc!r}",
                flush=True,
            )
            break

    print(
        f"  [SPEECH] end_conversation say_done "
        f"total_elapsed_ms={(time.monotonic() - t0) * 1000.0:.1f}",
        flush=True,
    )

    # Signal the worker to start its graceful-drain shutdown path.
    # The QR-hold window (EXPERIMENT_FAREWELL_DISPLAY_SEC) is enforced
    # by `loop_launcher_streaming.py`'s inter-session pause AFTER the
    # worker exits — that lets the worker tear down promptly while
    # the tablet keeps showing the QR (because tablet-server still
    # has `farewell_active=True` until the next session_start).
    print(
        f"  [TOOL] end_conversation signalling_shutdown",
        flush=True,
    )
    await _streaming_runtime.request_end_session("end_conversation_tool")

    return None
