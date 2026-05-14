"""Terminal speech tool — the only path the model has to talk.

Returns None so LiveKit's `reply_required` is False and the turn ends
as soon as Pepper finishes speaking (Letta TerminalToolRule pattern).

Speech pipeline per call:
  1. Split into sentences (Mode A: Piper is non-streaming, so per-sentence
     `session.say()` pipelines first-sentence audio in ~250 ms) or send
     the whole utterance as a single say() (Mode C: openai.TTS streams
     chunks within one HTTP call). Controlled by the SPLIT_SENTENCES env
     surfaced through the per-session `SessionRuntime` from
     [_pipeline.py](../_pipeline.py).
  2. For each chunk: `session.say(chunk)` and await playout. Interruptions
     are disabled session-wide, so we no longer thread `allow_interruptions`
     through here.
  3. After the final chunk's playout, wait for the real speaker-drained
     ACK (`pepper.speech speaker_drained`) published by audio_bridge.py
     once the on-robot bridge confirms ALAudioDevice's queue drained.
     If no audio-bridge participant is in the room (laptop-only dev) or
     the drain ACK doesn't arrive within DRAIN_TIMEOUT, fall back cleanly
     so the tool doesn't hang. EOS_MODE=`auto|require|skip` (env)
     overrides the layered detection.
"""

from __future__ import annotations

import asyncio
import re
import time

from livekit.agents import RunContext, function_tool

from .utils._animation import trigger_animation
from .utils._emotion import Emotion
from .utils._events import _emit_tool_event
from .utils._session_runtime import get_session_runtime

# Sentence boundary: punctuation followed by whitespace. Per-sentence
# session.say() lets Piper push the first sentence to audio quickly
# instead of waiting for the whole utterance to synthesize.
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text: str) -> list[str]:
    parts = [p.strip() for p in _SENT_SPLIT.split(text) if p.strip()]
    return parts or [text]


async def _wait_speaker_drained(ts_fn) -> str:
    """Wait for the on-robot bridge to ACK speaker drain.

    Returns the EOS reason for logging: `robot_ack`, `no_robot_bridge`,
    `no_audio_bridge`, `skip`, or `timeout`. The pipeline sets
    `speaker_drained_evt` when it sees a `pepper.speech speaker_drained`
    message. If there's no audio-bridge participant in the room, that
    message will never come — short-circuit so the tool doesn't hang.
    """
    rt = get_session_runtime()
    if rt is None:
        # Pipeline didn't install a runtime — e.g. an external test
        # harness. Behave as if drain is skipped.
        print(f"  [SPEECH] drain_skipped reason=no_runtime", flush=True)
        return "no_runtime"

    if rt.eos_mode == "skip":
        print(f"  {rt.ts()} [SPEECH] drain_skipped reason=eos_mode_skip", flush=True)
        return "skip"

    if rt.eos_mode == "auto" and not rt.audio_bridge_present:
        # No audio-bridge container in the room → no drain ACK will ever
        # arrive. wait_for_playout() above already returned, so just
        # proceed.
        print(
            f"  {rt.ts()} [SPEECH] drain_skipped reason=no_audio_bridge",
            flush=True,
        )
        return "no_audio_bridge"

    # Ask audio_bridge (in the same room) to round-trip a drain ACK
    # with the on-robot bridge. The pipeline installs this callable.
    if rt.request_drain is not None:
        try:
            await rt.request_drain()
        except Exception as exc:
            print(f"  {rt.ts()} [SPEECH] drain_req_publish_failed err={exc!r}", flush=True)

    t0 = time.monotonic()
    print(f"  {rt.ts()} [SPEECH] drain_wait_start timeout={rt.drain_timeout:.1f}s", flush=True)
    try:
        await asyncio.wait_for(rt.speaker_drained_evt.wait(), timeout=rt.drain_timeout)
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        print(
            f"  {rt.ts()} [SPEECH] drain_ack_received elapsed_ms={elapsed_ms:.1f}",
            flush=True,
        )
        return "robot_ack"
    except asyncio.TimeoutError:
        elapsed_ms = (time.monotonic() - t0) * 1000.0
        print(
            f"  {rt.ts()} [SPEECH] drain_timeout after_ms={elapsed_ms:.1f} — "
            f"proceeding (eos_mode={rt.eos_mode})",
            flush=True,
        )
        return "timeout"


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
    _emit_tool_event("send_message_to_user", {"text": text_clean, "emotion": emotion})
    if not text_clean:
        return None

    rt = get_session_runtime()
    split = rt.split_sentences if rt is not None else True
    ts_fn = (lambda: rt.ts()) if rt is not None else (lambda: "")

    if split:
        chunks = _split_sentences(text_clean)
    else:
        chunks = [text_clean]

    print(
        f"  {ts_fn()} [SPEECH] start chunks={len(chunks)} split_sentences={split} "
        f"chars={len(text_clean)} emotion={emotion!r}",
        flush=True,
    )

    # Clear any stale drain-ack from a previous utterance BEFORE starting
    # playout — otherwise we might receive the new one out-of-order.
    if rt is not None:
        rt.reset_drain()

    # Animation is fire-and-forget — never block speech on a bounded
    # gesture queue.
    if emotion:
        asyncio.create_task(trigger_animation(emotion))

    t0 = time.monotonic()
    for i, sentence in enumerate(chunks, start=1):
        handle = context.session.say(sentence)
        try:
            await handle.wait_for_playout()
        except Exception as exc:
            print(
                f"  {ts_fn()} [SPEECH] playout_error chunk={i}/{len(chunks)} err={exc!r}",
                flush=True,
            )
            break
        print(
            f"  {ts_fn()} [SPEECH] chunk_done i={i}/{len(chunks)} "
            f"chars={len(sentence)} elapsed_ms={(time.monotonic() - t0) * 1000.0:.1f}",
            flush=True,
        )

    # Real EOS: wait until the on-robot bridge says ALAudioDevice's
    # queue actually drained. Falls back cleanly when no audio-bridge
    # or robot is present.
    reason = await _wait_speaker_drained(ts_fn)

    print(
        f"  {ts_fn()} [SPEECH] end total_elapsed_ms={(time.monotonic() - t0) * 1000.0:.1f} "
        f"eos_reason={reason}",
        flush=True,
    )

    return None  # ← Terminal: reply_required=False, loop ends.
