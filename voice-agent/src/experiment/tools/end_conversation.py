"""end_conversation tool — terminal farewell + feedback QR + session end.

Called by Pepper when the user clearly signals they are done (says
"bye", "thanks that is all", etc.). The tool:

  1. Renders a feedback-QR page on Pepper's tablet showing the
     participant code (T01, T02, …) and a JS-driven countdown.
     Publishes `farewell_active=True` on `pepper.state` so
     tablet-server stops posting chat re-renders that would flicker
     over the QR.
  2. Speaks a farewell line (agent-authored or a prepared default),
     waiting for the real speaker-drained ACK afterwards so the audio
     fully finishes before the session ends.
  3. Holds the page for EXPERIMENT_FAREWELL_DISPLAY_SEC seconds
     (browser ticks the countdown, no per-second re-posts from here).
  4. Clears the tablet, publishes `farewell_active=False`, and signals
     the worker to shut down so loop_launcher.py advances the
     participant counter and swaps to the next variant.

This is the SINGLE exception to the "only send_message_to_user reaches
the user" rule documented in `prompt.py` — the tool drives
`context.session.say()` directly, the same primitive
`send_message_to_user` uses, so the LLM does not need a second tool
call to speak.

Terminal tool: returns `None`, ending the turn (no further LLM step).
"""

from __future__ import annotations

import asyncio
import re
import time
from urllib.parse import quote

import segno

from livekit.agents import RunContext, function_tool

from src.live.bridge_client import (  # noqa: E402
    post_tablet_clear,
    post_tablet_farewell,
)
from src.live.config import (  # noqa: E402
    EXPERIMENT_FAREWELL_DISPLAY_SEC,
    EXPERIMENT_FEEDBACK_ID_ENTRY,
    EXPERIMENT_FEEDBACK_URL,
)

from .utils._animation import trigger_animation
from .utils._emotion import Emotion
from .utils._events import _emit_tool_event
from .utils._session_runtime import get_session_runtime


# Spoken at the start of the farewell when the LLM does not supply its
# own `text`. Kept short so the auto-appended `FEEDBACK_REMINDER` flows
# naturally after it.
DEFAULT_GOODBYE_PREFIX = "Goodbye!"

# Always spoken at the end of the farewell, after either the
# LLM-provided `text` or the `DEFAULT_GOODBYE_PREFIX`. Hard-coded so
# every participant hears the same call-to-action regardless of how
# the agent phrased its own farewell. `{conv_id}` is filled in at
# runtime with the spoken-friendly form (e.g. "T zero one").
FEEDBACK_REMINDER_TEMPLATE = (
    "Your conversation ID is {conv_id}. "
    "Please help me improve by scanning the QR code on my tablet or "
    "on the board and filling the questionnaire!"
)


def _already_mentions_reminder(text: str) -> bool:
    """Defensive check — Llama-class models sometimes ignore the
    prompt instruction not to include the QR / questionnaire phrasing
    in their own `text` and paste the reminder in themselves. If they
    do, appending the reminder would double it. Detect by looking for
    the call-to-action keywords."""
    t = (text or "").lower()
    if "questionnaire" in t:
        return True
    if "qr" in t and ("scan" in t or "code" in t):
        return True
    if "conversation id" in t:
        return True
    return False


_DIGIT_WORDS = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
}


def _speakable_conv_id(conv_id: str) -> str:
    """Convert e.g. "T01" to "T zero one" so TTS reads it
    character-by-character instead of mispronouncing it as a word."""
    parts: list[str] = []
    for ch in conv_id:
        if ch.isdigit():
            parts.append(_DIGIT_WORDS[ch])
        elif ch.isalpha():
            parts.append(ch.upper())
        # silently drop any other char (shouldn't happen for T?? format)
    return " ".join(parts) if parts else conv_id

# QR render knobs. `scale` is pixels-per-module — at scale=20 a typical
# 41-module v6 QR comes out ~820 px, which the tablet CSS scales to a
# 620 px slot with image-rendering:pixelated for crisp module edges.
# `border=4` is the QR-spec minimum quiet zone; some scanners refuse to
# read with less.
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
    # PNG renders more reliably than inline SVG on Pepper's NAOqi
    # WebView (old WebKit). png_data_uri returns the full
    # `data:image/png;base64,…` string ready to drop into <img src=…>.
    return qr.png_data_uri(scale=_QR_SCALE, border=_QR_BORDER)


async def _safe_publish_state(rt, payload: dict, label: str) -> None:
    """Best-effort update of `pepper.state` — failures here must never
    abort the farewell flow."""
    if rt is None or rt.update_state is None:
        return
    try:
        await rt.update_state(payload)
    except Exception as exc:
        print(
            f"  [tool] end_conversation {label}_publish_failed err={exc!r}",
            flush=True,
        )


async def _wait_speaker_drained(rt) -> str:
    """Same drain pattern as `send_message_to_user._wait_speaker_drained`:
    ask audio_bridge for a round-trip ACK that Pepper's speaker buffer
    has flushed, with a clean fallback when no audio-bridge is in the
    room. Without this, the session can be shut down while the last
    few hundred milliseconds of TTS audio are still in NAOqi's
    ALAudioDevice queue and the user hears the farewell get cut off."""
    if rt is None:
        return "no_runtime"
    if rt.eos_mode == "skip":
        return "skip"
    if rt.eos_mode == "auto" and not rt.audio_bridge_present:
        return "no_audio_bridge"
    if rt.request_drain is not None:
        try:
            await rt.request_drain()
        except Exception as exc:
            print(
                f"  {rt.ts()} [SPEECH] end_conversation drain_req_publish_failed "
                f"err={exc!r}",
                flush=True,
            )
    t0 = time.monotonic()
    try:
        await asyncio.wait_for(rt.speaker_drained_evt.wait(), timeout=rt.drain_timeout)
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        print(
            f"  {rt.ts()} [SPEECH] end_conversation drain_ack_received "
            f"elapsed_ms={elapsed_ms:.1f}",
            flush=True,
        )
        return "robot_ack"
    except asyncio.TimeoutError:
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        print(
            f"  {rt.ts()} [SPEECH] end_conversation drain_timeout "
            f"after_ms={elapsed_ms:.1f}",
            flush=True,
        )
        return "timeout"


@function_tool(name="end_conversation")
async def end_conversation(
    context: RunContext,
    text: str | None = None,
    emotion: Emotion | None = None,
) -> None:
    """End the conversation: speak a short farewell, then show a
    feedback QR on the tablet for a few seconds. After this the
    session ends and Pepper resets for the next user.

    Call this when the user has clearly signalled they are done —
    "bye", "thanks that is all", "I am good thanks", "see you", etc.
    Do NOT call this just because the user paused or said "thanks"
    mid-conversation; wait for an unambiguous farewell.

    text: optional short personal sign-off (1 short sentence, friendly,
        apostrophe-free per the global rule) — e.g. "See you!" or
        "Have a great day!". A fixed reminder is always appended that
        announces the conversation ID and asks the user to scan the QR
        on the tablet or on the board to fill the questionnaire, so do
        NOT mention the conversation ID, the QR, the tablet/board, or
        the questionnaire yourself. If omitted or empty, a plain
        "Goodbye!" is used as the sign-off.
    emotion: optional body language for the farewell. Defaults to
        "goodbye"; only override if a different gesture clearly fits.

    This tool ENDS the session — do not call send_message_to_user
    after it, and do not try to keep talking.
    """
    prefix = (text or "").strip() or DEFAULT_GOODBYE_PREFIX
    gesture: Emotion = emotion or "goodbye"

    rt = get_session_runtime()
    student_id = getattr(rt, "student_id", None) if rt is not None else None
    conv_id = _format_conv_id(student_id)
    ts_fn = (lambda: rt.ts()) if rt is not None else (lambda: "")

    if _already_mentions_reminder(prefix):
        # LLM ignored the prompt and pasted the call-to-action into
        # its own `text`. Don't double it — just use what it gave us.
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
        f"  {ts_fn()} [TOOL] end_conversation start conv_id={conv_id} "
        f"display_seconds={display_seconds} text_chars={len(spoken_text)} "
        f"gesture={gesture!r}",
        flush=True,
    )

    try:
        qr_data_uri = _build_qr_data_uri(qr_payload)
        print(
            f"  {ts_fn()} [TOOL] end_conversation qr_built "
            f"payload_chars={len(qr_payload)} data_uri_chars={len(qr_data_uri)}",
            flush=True,
        )
    except Exception as exc:
        print(
            f"  {ts_fn()} [TOOL] end_conversation qr_build_failed err={exc!r}",
            flush=True,
        )
        qr_data_uri = ""

    # Lock the mic muted IMMEDIATELY — before any speech, before the
    # tablet post, before anything. From this moment until session end
    # every `_set_mic_muted(False, ...)` path (agent_state=listening,
    # post-drain grace unmute, etc.) is a no-op. Doing this at the
    # very top of the tool means the user cannot trigger another
    # `user_turn` even during the speech itself, only the lock-aware
    # mute survives the whole window.
    if rt is not None and rt.mute_mic_persistent is not None:
        try:
            rt.mute_mic_persistent("end_conversation_lock_early")
            print(
                f"  {ts_fn()} [TOOL] end_conversation mic_locked_at_entry",
                flush=True,
            )
        except Exception as exc:
            print(
                f"  {ts_fn()} [TOOL] end_conversation early_mute_failed "
                f"err={exc!r}",
                flush=True,
            )

    # Tell tablet-server to stop posting chat re-renders so the QR is
    # not flickered over by transcript updates.
    await _safe_publish_state(rt, {"farewell_active": True}, "farewell_on")

    # Post the QR once — the page contains a JS countdown timer, so we
    # do not need to re-post every second (which would race with
    # tablet-server's chat renders).
    try:
        await asyncio.to_thread(
            post_tablet_farewell, conv_id, qr_data_uri, display_seconds,
        )
        print(
            f"  {ts_fn()} [TOOL] end_conversation tablet_posted",
            flush=True,
        )
    except Exception as exc:
        print(
            f"  {ts_fn()} [TOOL] end_conversation tablet_post_failed err={exc!r}",
            flush=True,
        )

    # Animation fires async — same fire-and-forget pattern as
    # send_message_to_user.
    if gesture:
        asyncio.create_task(trigger_animation(gesture))

    split = rt.split_sentences if rt is not None else True
    if rt is not None:
        rt.reset_drain()

    chunks = _split_sentences(spoken_text) if split else [spoken_text]

    print(
        f"  {ts_fn()} [SPEECH] end_conversation say_start chunks={len(chunks)} "
        f"split_sentences={split} chars={len(spoken_text)}",
        flush=True,
    )

    t0 = time.monotonic()
    for i, sentence in enumerate(chunks, start=1):
        try:
            handle = context.session.say(sentence)
            await handle.wait_for_playout()
            print(
                f"  {ts_fn()} [SPEECH] end_conversation chunk_done "
                f"i={i}/{len(chunks)} chars={len(sentence)} "
                f"elapsed_ms={(time.monotonic() - t0) * 1000.0:.1f}",
                flush=True,
            )
        except Exception as exc:
            print(
                f"  {ts_fn()} [SPEECH] end_conversation playout_error "
                f"chunk={i}/{len(chunks)} err={exc!r}",
                flush=True,
            )
            break

    # Wait for the real speaker-drained ACK so the audio is fully
    # flushed from NAOqi's buffer before we shut down the session.
    reason = await _wait_speaker_drained(rt)
    print(
        f"  {ts_fn()} [SPEECH] end_conversation end "
        f"total_elapsed_ms={(time.monotonic() - t0) * 1000.0:.1f} "
        f"eos_reason={reason}",
        flush=True,
    )

    # Single coherent JS countdown: the QR was posted at tool entry
    # with `remaining=display_seconds`, so by now the chip has already
    # ticked down by speech_duration seconds. When it hits 0 the JS
    # auto-hides the chip (see `_TABLET_FAREWELL_HTML`), so the QR
    # stays visible without a "stuck on 0" indicator until
    # loop_launcher dispatches the next session. NO re-post here — a
    # second post would visibly reset the countdown to 30 mid-flow,
    # which is more jarring than letting it run out cleanly.

    # Signal shutdown IMMEDIATELY — no in-tool sleep. The QR-hold
    # window (EXPERIMENT_FAREWELL_DISPLAY_SEC) is enforced by
    # loop_launcher AFTER the worker exits: it sleeps that long before
    # dispatching the next session. That way the worker can fully
    # tear down, no queued user_turn can race with us, and the tablet
    # keeps showing the QR for the whole window because nothing else
    # is posting to it (tablet-server is still gated by
    # `farewell_active=True`, cleared only by the next session's
    # `session_start`).
    print(
        f"  {ts_fn()} [TOOL] end_conversation signalling_shutdown",
        flush=True,
    )
    if rt is not None and rt.end_session_callback is not None:
        try:
            await rt.end_session_callback("end_conversation_tool")
        except Exception as exc:
            print(
                f"  {ts_fn()} [TOOL] end_conversation end_session_failed "
                f"err={exc!r}",
                flush=True,
            )
    else:
        print(
            f"  {ts_fn()} [TOOL] end_conversation no_end_session_callback — "
            "session will linger until external shutdown",
            flush=True,
        )

    return None
