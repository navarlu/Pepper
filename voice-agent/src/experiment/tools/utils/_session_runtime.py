"""Per-session runtime handle shared between _pipeline.py and tools.

The Letta-style `send_message_to_user` tool needs access to a few
session-scoped things that don't fit naturally on `RunContext`:

  * `speaker_drained_evt` — set when the on-robot bridge ACKs that
    Pepper's speaker has finished emitting audio (real EOS).
  * `audio_bridge_present` — whether an `audio-bridge` participant is
    in the room at all. When False, no drain ACK will ever arrive, so
    the tool short-circuits the wait.
  * `eos_mode` — `auto | require | skip` (env-controlled). `auto`
    layers the two flags above; `require` always waits for the ACK
    (loud-log if no audio-bridge); `skip` always falls back to
    `wait_for_playout()` only.
  * `drain_timeout` — seconds to wait for the drain ACK before
    falling back. Tunable via env.
  * `split_sentences` — Mode-A wants per-sentence `session.say()` to
    pipeline atomically-synthesized Piper. Mode C lets `openai.TTS`
    stream a single utterance.
  * `ts0` — monotonic start of the session; used for `[SPEECH] +<x>s`
    traces so the worker tmux logs line up with the rest of the
    pipeline.

The pipeline writes this once per session via `set_session_runtime()`;
the tool reads it via `get_session_runtime()`. Worker processes only
host one dispatch at a time (num_idle_processes=0), so a module-level
slot is safe and matches the existing `set_tool_event_listener`
pattern in `_events.py`.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional


@dataclass
class SessionRuntime:
    """Per-session knobs + signalling shared with tools.

    Mutated by the pipeline as the session unfolds: `audio_bridge_present`
    flips on participant connect/disconnect; `speaker_drained_evt` is set
    each time the audio_bridge service publishes a `speaker_drained`
    message on `pepper.speech`.
    """

    eos_mode: str = "auto"
    drain_timeout: float = 5.0
    split_sentences: bool = True
    audio_bridge_present: bool = False
    speaker_drained_evt: asyncio.Event = field(default_factory=asyncio.Event)
    ts0: float = field(default_factory=time.monotonic)
    # Set by the pipeline: publishes `pepper.speech {"kind":"request_drain"}`
    # over the LiveKit data channel so audio_bridge can forward a
    # DRAIN_REQ control frame to the on-robot bridge. The tool calls
    # this after wait_for_playout() so the round-trip is per-utterance.
    request_drain: Optional[Callable[[], Awaitable[None]]] = None
    # Participant identity from the loop launcher (integer counter +
    # variant letter). Made available to tools so end_conversation can
    # build a per-participant QR payload and render the participant
    # code (`T01`, `T02`, …) on the tablet.
    student_id: Optional[int] = None
    variant: Optional[str] = None
    # Installed by the pipeline: signals the worker's shutdown_event so
    # the session ends cleanly. Used by `end_conversation` after its
    # farewell + countdown finish.
    end_session_callback: Optional[Callable[[str], Awaitable[None]]] = None
    # Installed by the pipeline: publishes a dict onto `pepper.state` so
    # tools can update UI-side flags (e.g. `farewell_active=True` to
    # tell tablet-server to stop posting chat re-renders while the
    # farewell QR is on screen).
    update_state: Optional[Callable[[dict], Awaitable[None]]] = None
    # Installed by the pipeline: marks the user mic persistently muted
    # — bumps the unmute-generation counter so any pending grace-window
    # unmute task no-ops, then publishes `mic_muted=True` on pepper.state.
    # Used by `end_conversation` to lock the mic during the QR hold so a
    # user speaking during the countdown cannot trigger a new LLM turn.
    mute_mic_persistent: Optional[Callable[[str], None]] = None

    def ts(self) -> str:
        """`+s.sss` since session start — same format used elsewhere."""
        return f"+{time.monotonic() - self.ts0:6.3f}s"

    def reset_drain(self) -> None:
        """Clear the event before starting a new speech utterance."""
        self.speaker_drained_evt.clear()


_runtime: Optional[SessionRuntime] = None


def set_session_runtime(rt: SessionRuntime) -> None:
    global _runtime
    _runtime = rt


def get_session_runtime() -> Optional[SessionRuntime]:
    return _runtime


def clear_session_runtime() -> None:
    global _runtime
    _runtime = None
