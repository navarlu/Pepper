"""Per-turn WAV audio capture for the streaming experiment.

Joins the same LiveKit room as the launcher (the recorder
participant) and writes one WAV per turn per side to
`<session-results-dir>/audio/`:

    T07_turn0_user.wav     # visitor mic, 16 kHz mono int16
    T07_turn0_agent.wav    # agent TTS output, 16 kHz mono int16
    T07_turn1_user.wav
    T07_turn1_agent.wav
    …

Used by `launcher_streaming.py`. Two pieces of state mutate from
outside this module:

  * ``close_user_turn(turn_id)`` — called by the launcher when it
    sees ``vad_user_speech_end{turn_id}``. The currently-open user
    WAV is finalised under name ``…_turn{turn_id}_user.wav`` and a
    fresh file is opened for the next turn's audio.
  * ``open_agent_turn(turn_id)`` / ``close_agent_turn(turn_id)`` —
    called on ``tts_request_start`` / ``agent_audio_saved``
    (or ``pepper_drain_ack``).

The module is best-effort: any subscription / file-write failure
prints to stderr and skips the affected frame rather than raising.
Audio capture is observability, not a hard dependency for the
session — losing one turn's WAV must never break the experiment.
"""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
import wave
from pathlib import Path
from typing import Optional

from livekit import rtc


SAMPLE_RATE = 16_000
CHANNELS = 1
SAMPLE_WIDTH = 2  # int16 → 2 bytes


class _WavTurnWriter:
    """Single-side rotating WAV writer with lazy file open.

    Frames are streamed into a session-scoped temp file as soon as
    they arrive; that temp file is renamed to its final turn-tagged
    name when ``close_and_name_as(turn_id)`` is called. No in-memory
    buffering — bytes hit disk immediately, so an arbitrarily long
    turn does not pressure RAM.

    Lazy open means the writer survives both "frames before any
    turn boundary" (first user turn) AND "multiple TTS utterances
    inside one turn" (the end-of-conversation farewell, which fires
    three ``session.say()`` calls back-to-back at the same
    ``turn_id``). Each subsequent utterance just continues to write
    into the still-open file; only the next ``close_and_name_as``
    closes and renames it.
    """

    def __init__(self, audio_dir: Path, conv_id: str, role: str) -> None:
        self._dir = audio_dir
        self._conv_id = conv_id
        self._role = role
        self._open_path: Optional[Path] = None
        self._open_wave: Optional[wave.Wave_write] = None
        self._frames_written = 0
        self._tmp_counter = 0

    def _ensure_open(self) -> bool:
        """Lazy-open: on the first frame after the last close, create
        a fresh temp wav. Returns False if the open failed."""
        if self._open_wave is not None:
            return True
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            self._tmp_counter += 1
            tmp_path = (
                self._dir
                / f".{self._conv_id}_{self._role}_capture_{self._tmp_counter}.wav"
            )
            wf = wave.open(str(tmp_path), "wb")
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(SAMPLE_WIDTH)
            wf.setframerate(SAMPLE_RATE)
            self._open_path = tmp_path
            self._open_wave = wf
            self._frames_written = 0
            return True
        except Exception as exc:
            print(
                f"[audio-capture] {self._role} lazy-open failed: {exc!r}",
                file=sys.stderr,
            )
            self._open_wave = None
            self._open_path = None
            return False

    def write_pcm(self, pcm: bytes) -> None:
        if not pcm:
            return
        if not self._ensure_open():
            return
        try:
            self._open_wave.writeframes(pcm)
            self._frames_written += len(pcm) // SAMPLE_WIDTH
        except Exception as exc:
            print(
                f"[audio-capture] {self._role} write failed: {exc!r}",
                file=sys.stderr,
            )

    def close_and_name_as(self, turn_id: int) -> Optional[Path]:
        """Close the currently-open temp wav and rename it to the
        turn-tagged final name. Returns the final path, or None if
        nothing was open / no frames were written."""
        if self._open_wave is None:
            return None
        try:
            self._open_wave.close()
        except Exception as exc:
            print(
                f"[audio-capture] {self._role} finalise failed: {exc!r}",
                file=sys.stderr,
            )
        tmp_path = self._open_path
        wrote = self._frames_written
        self._open_wave = None
        self._open_path = None
        self._frames_written = 0
        if tmp_path is None:
            return None
        target = self._dir / f"{self._conv_id}_turn{turn_id}_{self._role}.wav"
        if wrote == 0:
            # Empty capture — drop the temp rather than producing a
            # zero-sample WAV that ASR would choke on.
            try:
                tmp_path.unlink()
            except OSError:
                pass
            return None
        try:
            tmp_path.replace(target)
        except OSError as exc:
            print(
                f"[audio-capture] {self._role} rename {tmp_path}->{target} "
                f"failed: {exc!r}",
                file=sys.stderr,
            )
            return None
        return target

    def shutdown(self) -> None:
        if self._open_wave is None:
            return
        try:
            self._open_wave.close()
        except Exception:
            pass
        # Leave the temp on disk for forensics — it's a `.{conv}_…` dotfile
        # so it stays out of the way of the named per-turn files.
        self._open_wave = None
        self._open_path = None


class AudioCapture:
    """Subscribes to user-mic + agent-TTS tracks in a LiveKit room and
    writes per-turn WAVs.

    Lifecycle:
        cap = AudioCapture(room, audio_dir, conv_id, user_identity)
        cap.start()           # registers `track_subscribed` handler
        ... events flow in via close_user_turn / open_agent_turn ...
        await cap.shutdown()  # finalises all open files
    """

    def __init__(
        self,
        room: rtc.Room,
        audio_dir: Path,
        conv_id: str,
        user_identity: str,
    ) -> None:
        self._room = room
        self._dir = audio_dir
        self._conv_id = conv_id
        self._user_identity = user_identity
        self._user_writer = _WavTurnWriter(audio_dir, conv_id, "user")
        self._agent_writer = _WavTurnWriter(audio_dir, conv_id, "agent")
        # Track both the consumer task and the underlying AudioStream
        # so shutdown can `aclose()` the stream explicitly — task
        # cancellation alone doesn't reliably break out of LiveKit's
        # internal `async for` and the process otherwise hangs at end-
        # of-session waiting for the iterator to yield.
        self._streams: list = []
        self._stream_tasks: list[asyncio.Task] = []
        self._started = False
        # Track the most recently captured monotonic time for each
        # side so we can decide if an open() came in unexpectedly
        # late vs. before any audio.
        self._user_last_frame_ts: float = 0.0
        self._agent_last_frame_ts: float = 0.0

    def start(self) -> None:
        if self._started:
            return
        self._started = True

        @self._room.on("track_subscribed")
        def _on_track(track, publication, participant):  # noqa: ANN001
            if track.kind != rtc.TrackKind.KIND_AUDIO:
                return
            identity = str(getattr(participant, "identity", "") or "")
            if identity == self._user_identity:
                role = "user"
                writer = self._user_writer
            elif identity.startswith("agent-"):
                role = "agent"
                writer = self._agent_writer
            else:
                # Other participants (recorder, tablet) — ignore.
                return
            try:
                stream = rtc.AudioStream.from_track(
                    track=track,
                    sample_rate=SAMPLE_RATE,
                    num_channels=CHANNELS,
                )
            except Exception as exc:
                print(
                    f"[audio-capture] AudioStream.from_track {role} failed: {exc!r}",
                    file=sys.stderr,
                )
                return
            self._streams.append(stream)
            task = asyncio.create_task(
                self._consume(stream, writer, role),
                name=f"audio-capture-{role}",
            )
            self._stream_tasks.append(task)
            print(
                f"[audio-capture] subscribing {role} identity={identity} "
                f"sample_rate={SAMPLE_RATE}",
                flush=True,
            )

    async def _consume(self, stream, writer: _WavTurnWriter, role: str) -> None:
        try:
            async for event in stream:
                frame = event.frame
                raw = bytes(frame.data)
                if not raw:
                    continue
                writer.write_pcm(raw)
                if role == "user":
                    self._user_last_frame_ts = time.monotonic()
                else:
                    self._agent_last_frame_ts = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            print(
                f"[audio-capture] {role} consume failed: {exc!r}",
                file=sys.stderr,
            )

    # ── Launcher-driven turn rotation ────────────────────────────────

    def close_user_turn(self, turn_id: int) -> Optional[Path]:
        """Close the user WAV file as ``T<id>_turn{turn_id}_user.wav``.
        Called when the launcher sees ``vad_user_speech_end{turn_id}``."""
        path = self._user_writer.close_and_name_as(turn_id)
        if path:
            print(f"[audio-capture] user turn={turn_id} -> {path.name}", flush=True)
        return path

    def close_agent_turn(self, turn_id: int) -> Optional[Path]:
        """Close the agent WAV file as ``T<id>_turn{turn_id}_agent.wav``.
        Called by the launcher when the agent's response to turn
        ``turn_id`` has finished — either because a new user turn
        begins (``vad_user_speech_end`` of turn+1) or the session
        ends."""
        path = self._agent_writer.close_and_name_as(turn_id)
        if path:
            print(f"[audio-capture] agent turn={turn_id} -> {path.name}", flush=True)
        return path

    async def shutdown(self) -> None:
        # Close each AudioStream FIRST so its `async for` iterator
        # exits cleanly — cancelling the consumer task alone doesn't
        # break out of LiveKit's internal queue read and would hang
        # asyncio.run() at end-of-session.
        for stream in self._streams:
            try:
                await asyncio.wait_for(stream.aclose(), timeout=1.5)
            except (asyncio.TimeoutError, Exception) as exc:
                print(
                    f"[audio-capture] stream aclose failed: {exc!r}",
                    file=sys.stderr,
                )
        self._streams.clear()
        # Now the consumer tasks should fall through naturally; cancel
        # + await as a safety net.
        for t in self._stream_tasks:
            t.cancel()
        for t in self._stream_tasks:
            try:
                await asyncio.wait_for(t, timeout=1.0)
            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                pass
        self._stream_tasks.clear()
        self._user_writer.shutdown()
        self._agent_writer.shutdown()
