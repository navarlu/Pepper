"""
Audio Bridge — subscribes to agent audio in LiveKit and streams PCM
directly into Pepper's PulseAudio daemon via a persistent ssh+paplay
subprocess.

Bypasses NAOqi's `ALAudioDevice` queue (the ~1.3 s latency source
identified in Phase 2 transport-probe work). Pepper-side latency is
~25 ms inside PA + ~100 ms end-to-end (LAN + ALSA + speaker), versus
~1300 ms via the previous TCP→bridge→sendRemoteBufferToOutput path.

Non-blocking on every axis:
  - The ssh+paplay subprocess is respawned in the background if it
    dies (SSH disconnect, paplay crash); LiveKit streams keep running
    and audio is silently dropped while the pipe is down.
  - Token rotations from the orchestrator are picked up via
    `SessionWatcher` and trigger a clean room reconnect.
  - Multiple concurrent agent streams are coalesced so only one
    feeds paplay at a time (old streams cancelled on new one).

Flush / drain semantics are entirely internal now — there is no longer
a separate robot-side bridge for audio:
  - `flush`  : close paplay stdin so PA drains immediately, then
               respawn paplay for the next utterance (used on barge-in
               and stream-end).
  - `drain`  : wait `PEPPER_DRAIN_TAIL_MS` after the last PCM write,
               then publish `pepper.speech speaker_drained` over
               LiveKit so the worker's `send_message_to_user` can
               return at true end-of-speech.
"""

from __future__ import annotations

import asyncio
import audioop
import contextlib
import shutil
import subprocess
import time

from livekit import rtc

from config import (
    AGENT_TRACK_IDENTITY,
    LISTENER_IDENTITY,
    LIVEKIT_URL,
    PEPPER_DRAIN_TAIL_MS,
    PEPPER_PAPLAY_LATENCY_MS,
    PEPPER_SSH_HOST,
    PEPPER_SSH_PASSWORD,
    PEPPER_SSH_USER,
    PEPPER_STREAM_ATTENUATION,
    PEPPER_STREAM_RATE,
    SILENCE_GATE_HANGOVER_MS,
    SILENCE_GATE_RMS,
    SILENCE_GATE_TRACE,
    TOKEN_POLL_INTERVAL,
)
from session import SessionWatcher, post_debug_event

PAPLAY_RESPAWN_BACKOFF_SEC = 2.0
TOPIC_SPEECH = "pepper.speech"


def _build_ssh_cmd() -> list[str]:
    """Construct the ssh+paplay command list.

    `paplay --raw --format=s16le` reads raw mono int16 PCM from stdin.
    `--latency-msec=<n>` is honoured by PA on Pepper. ServerAliveInterval
    keeps the SSH session alive across long idle periods so the next
    utterance reuses the same pipe.
    """
    paplay_remote = (
        f"paplay --raw --format=s16le --rate={PEPPER_STREAM_RATE} "
        f"--channels=1 --latency-msec={PEPPER_PAPLAY_LATENCY_MS}"
    )
    ssh_opts = [
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=3",
    ]
    target = f"{PEPPER_SSH_USER}@{PEPPER_SSH_HOST}"
    if PEPPER_SSH_PASSWORD:
        # Required because we authenticate by password; key-based auth
        # would skip sshpass and use the same command with no prefix.
        return ["sshpass", "-p", PEPPER_SSH_PASSWORD, "ssh", *ssh_opts, target, paplay_remote]
    return ["ssh", *ssh_opts, target, paplay_remote]


class AudioBridge:
    """Joins LiveKit as the `listener`, forwards the agent's audio
    straight into Pepper's PulseAudio via ssh+paplay.

    The single long-lived paplay subprocess is opened lazily on the
    first audio frame and respawned if it dies. Silence between
    utterances does NOT close the pipe — paplay just waits on stdin.
    The pipe is only torn down on barge-in / stream-end / shutdown.
    """

    def __init__(self):
        self.livekit_url = LIVEKIT_URL
        self.token_watcher = SessionWatcher("listener", TOKEN_POLL_INTERVAL)
        self.target_identity = AGENT_TRACK_IDENTITY or None
        self.explicit_target_identity = bool(self.target_identity)
        self.room: rtc.Room | None = None
        self._connect_lock = asyncio.Lock()
        self._paplay_lock = asyncio.Lock()
        self._watch_task: asyncio.Task | None = None
        self._active_stream_keys: set[str] = set()
        self._stream_tasks: dict[str, asyncio.Task] = {}
        # Persistent ssh+paplay subprocess. `None` until first audio
        # frame, and between respawns.
        self._paplay_proc: subprocess.Popen | None = None
        self._paplay_warn_logged = False
        # Monotonic timestamp of the last PCM byte handed to paplay.
        # Used by the drain handler to compute when PA has actually
        # finished playing the buffered tail.
        self._last_write_ts: float = 0.0
        # Monotonic timestamp of the first PCM byte of the current
        # utterance. Reset to None on each stream-end / drain so the
        # next utterance triggers a fresh `speaker_first_sound`
        # publish. Used by the experiment recorder to measure
        # `pepper_first_sound` latency end-to-end.
        self._first_sound_ts: float | None = None
        # Post-drain cooldown — after a drain completes, silence
        # gate hangover frames can leak through and would otherwise
        # spuriously fire `speaker_first_sound` immediately. Frames
        # arriving within this window are still played (don't break
        # tail audio) but don't reset the first-sound marker.
        self._first_sound_suppress_until: float = 0.0

    # ── paplay subprocess management ────────────────────────────────

    def _spawn_paplay(self) -> bool:
        """Launch one ssh+paplay subprocess. Returns True on success.

        Logs the first failure verbosely; subsequent failures are quiet
        until the next successful spawn (avoids log spam when Pepper
        is off).
        """
        if self._paplay_proc is not None and self._paplay_proc.poll() is None:
            return True

        if not PEPPER_SSH_PASSWORD and shutil.which("ssh") is None:
            print("[audio-bridge] ERROR: ssh binary missing and no password configured")
            return False
        if PEPPER_SSH_PASSWORD and shutil.which("sshpass") is None:
            print(
                "[audio-bridge] ERROR: sshpass binary missing. "
                "Install with `apt install sshpass` or remove "
                "PEPPER_SSH_PASSWORD to use key auth."
            )
            return False

        cmd = _build_ssh_cmd()
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                # stderr=PIPE so we can surface SSH errors on respawn;
                # PIPE buffers fill fast though, so we drain in a
                # background task.
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except FileNotFoundError as exc:
            if not self._paplay_warn_logged:
                print(f"[audio-bridge] ssh/sshpass not found: {exc!r}")
                self._paplay_warn_logged = True
            return False
        except Exception as exc:
            if not self._paplay_warn_logged:
                print(f"[audio-bridge] paplay spawn failed: {exc!r}")
                self._paplay_warn_logged = True
            return False

        self._paplay_proc = proc
        self._paplay_warn_logged = False
        print(
            f"[audio-bridge] paplay spawned pid={proc.pid} "
            f"target={PEPPER_SSH_USER}@{PEPPER_SSH_HOST} "
            f"latency_ms={PEPPER_PAPLAY_LATENCY_MS}"
        )
        # Drain stderr so a chatty SSH banner doesn't block paplay.
        asyncio.create_task(self._drain_stderr(proc))
        return True

    async def _drain_stderr(self, proc: subprocess.Popen) -> None:
        """Read paplay/ssh stderr in a background thread executor and
        log meaningful lines. Stops when the proc exits."""
        loop = asyncio.get_running_loop()
        try:
            while proc.poll() is None:
                line = await loop.run_in_executor(
                    None, proc.stderr.readline if proc.stderr else (lambda: b"")
                )
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if not text:
                    continue
                # SSH host-key chatter is noisy and expected on first
                # connect; downgrade to a single info line.
                if "Permanently added" in text or "Warning: " in text:
                    continue
                print(f"[audio-bridge] paplay stderr: {text}")
        except Exception as exc:
            print(f"[audio-bridge] paplay stderr drain failed: {exc!r}")

    async def _ensure_paplay(self) -> bool:
        """Make sure paplay is up. Called before each PCM write."""
        proc = self._paplay_proc
        if proc is not None and proc.poll() is None:
            return True
        # Subprocess is missing or died — try one quick respawn.
        async with self._paplay_lock:
            proc = self._paplay_proc
            if proc is not None and proc.poll() is None:
                return True
            if proc is not None:
                # Reap the dead process.
                with contextlib.suppress(Exception):
                    proc.wait(timeout=0.1)
                self._paplay_proc = None
            return self._spawn_paplay()

    async def _write_paplay(self, pcm: bytes) -> bool:
        """Write one chunk of mono int16 PCM to paplay. Returns False
        if the pipe died and a respawn didn't recover."""
        if not pcm:
            return True
        if not await self._ensure_paplay():
            return False
        proc = self._paplay_proc
        if proc is None or proc.stdin is None:
            return False
        try:
            proc.stdin.write(pcm)
            proc.stdin.flush()
            now = time.monotonic()
            self._last_write_ts = now
            # Only fire `speaker_first_sound` if (a) this is the first
            # byte since the last drain, AND (b) we're past the
            # post-drain cooldown window. The cooldown rejects
            # silence-gate hangover frames that otherwise produce a
            # spurious `pepper_first_sound` immediately after each
            # `pepper_drain_ack`.
            first_of_utterance = (
                self._first_sound_ts is None
                and now >= self._first_sound_suppress_until
            )
            if first_of_utterance:
                self._first_sound_ts = now
                asyncio.create_task(self._publish_speaker_first_sound(len(pcm)))
            return True
        except (BrokenPipeError, OSError) as exc:
            print(f"[audio-bridge] paplay pipe broken: {exc!r} — will respawn")
            with contextlib.suppress(Exception):
                proc.kill()
            self._paplay_proc = None
            return False

    async def _close_paplay(self, reason: str) -> None:
        """Close paplay's stdin so PA drains, then wait briefly and
        kill the subprocess if it doesn't exit on its own. Used on
        flush, stream-cancel, shutdown."""
        async with self._paplay_lock:
            proc = self._paplay_proc
            if proc is None:
                return
            self._paplay_proc = None
            # Closing the pipe ends the current utterance — the next
            # one starts a fresh paplay process and must re-publish
            # `speaker_first_sound`.
            self._first_sound_ts = None
            print(f"[audio-bridge] closing paplay reason={reason} pid={proc.pid}")
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except Exception:
                pass
            loop = asyncio.get_running_loop()
            try:
                await asyncio.wait_for(
                    loop.run_in_executor(None, proc.wait), timeout=2.0
                )
            except asyncio.TimeoutError:
                with contextlib.suppress(Exception):
                    proc.kill()
            except Exception:
                with contextlib.suppress(Exception):
                    proc.kill()

    async def _flush_audio(self, reason: str) -> None:
        """Drop everything in-flight. Closing+respawning paplay flushes
        PA's ~30 ms buffer instantly and gives us a fresh stream for
        the next utterance.

        Cheap: ssh handshake stays warm via ControlMaster-free reuse
        on Pepper's sshd, paplay cold-start is <100 ms in practice."""
        await self._close_paplay(reason)

    async def _publish_speaker_first_sound(self, first_chunk_bytes: int) -> None:
        """Tell the worker that PA has accepted the first PCM byte of
        the current agent utterance. Published over LiveKit so the
        experiment recorder can timestamp `pepper_first_sound`."""
        if self.room is None:
            return
        import json
        try:
            payload = json.dumps(
                {
                    "kind": "speaker_first_sound",
                    "ts": time.time(),
                    "first_chunk_bytes": first_chunk_bytes,
                },
                ensure_ascii=False,
            ).encode("utf-8")
            await self.room.local_participant.publish_data(payload, topic=TOPIC_SPEECH)
        except Exception as exc:
            print(f"[audio-bridge] publish speaker_first_sound failed: {exc!r}")

    async def _publish_speaker_drained(self, reason: str) -> None:
        """Tell the worker that Pepper's speaker has actually drained.

        Published over LiveKit so the experiment worker's data_received
        handler (in `_pipeline.py`) can wake `send_message_to_user`. If
        the room isn't connected yet (very early in startup), the
        publish is silently skipped.
        """
        if self.room is None:
            return
        import json
        try:
            payload = json.dumps(
                {"kind": "speaker_drained", "reason": reason, "ts": time.time()},
                ensure_ascii=False,
            ).encode("utf-8")
            await self.room.local_participant.publish_data(payload, topic=TOPIC_SPEECH)
        except Exception as exc:
            print(f"[audio-bridge] publish speaker_drained failed: {exc!r}")

    async def _handle_drain_request(self) -> None:
        """Wait for paplay's buffer to drain and signal speaker_drained.

        With paplay, "drained" is just `last_write + drain_tail_ms`.
        No round-trip to Pepper needed — we own the only buffer in
        the chain (PA's 30 ms ring) so we know exactly when it's empty.
        """
        if self._paplay_proc is None or self._paplay_proc.poll() is not None:
            # Nothing playing; ack immediately.
            self._first_sound_ts = None
            await self._publish_speaker_drained(reason="no_pipe")
            return
        tail_sec = max(0.0, PEPPER_DRAIN_TAIL_MS / 1000.0)
        # Wait at least `tail_sec` past the last byte we handed paplay.
        while True:
            elapsed = time.monotonic() - self._last_write_ts
            remaining = tail_sec - elapsed
            if remaining <= 0:
                break
            await asyncio.sleep(min(remaining, 0.05))
            if self._paplay_proc is None or self._paplay_proc.poll() is not None:
                break
        # End of utterance — reset the first-sound marker so the next
        # agent utterance re-emits `speaker_first_sound`. The paplay
        # pipe stays open (no flush) to avoid the ~100 ms cold-start
        # cost on the next utterance. A 400 ms cooldown rejects any
        # silence-gate hangover frames that arrive in the post-drain
        # window — those should not re-trigger first-sound.
        self._first_sound_ts = None
        self._first_sound_suppress_until = time.monotonic() + 0.4
        await self._publish_speaker_drained(reason="paplay_tail_elapsed")

    # ── LiveKit ──

    def _is_agent_like_participant(self, participant, identity: str) -> bool:
        if identity.startswith("agent-"):
            return True
        kind_text = str(getattr(participant, "kind", "") or "").upper()
        return "AGENT" in kind_text

    def _should_forward_audio(self, participant) -> tuple[bool, str]:
        identity = str(getattr(participant, "identity", "") or "")
        if identity == LISTENER_IDENTITY:
            return False, "skip_listener_identity"
        if self.explicit_target_identity:
            if identity == self.target_identity:
                return True, "explicit_identity_match"
            return False, "explicit_identity_mismatch"
        if self.target_identity and identity == self.target_identity:
            return True, "token_identity_match"
        if self._is_agent_like_participant(participant, identity):
            return True, "agent_like_fallback"
        return False, "not_agent_like"

    async def _cancel_existing_streams(self, reason: str, keep_key: str | None = None) -> None:
        tasks_to_cancel = [
            (k, t) for k, t in list(self._stream_tasks.items())
            if keep_key is None or k != keep_key
        ]
        if not tasks_to_cancel:
            return
        print(f"[audio-bridge] Cancelling {len(tasks_to_cancel)} stale stream(s) reason={reason}")
        for _, task in tasks_to_cancel:
            task.cancel()
        for key, task in tasks_to_cancel:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
            self._stream_tasks.pop(key, None)
            self._active_stream_keys.discard(key)
        await self._flush_audio(reason)

    def _register_speech_handler(self, room: rtc.Room) -> None:
        """Listen for `pepper.speech` data messages from the worker.

        The worker publishes `{"kind": "request_drain"}` after every
        `send_message_to_user` finishes its TTS playout. With paplay
        we resolve the drain locally (last write + tail) — no round
        trip to a robot-side bridge.
        """
        import json as _json

        @room.on("data_received")
        def _on_data(packet):
            topic = str(getattr(packet, "topic", "") or "")
            if topic != TOPIC_SPEECH:
                return
            try:
                msg = _json.loads(getattr(packet, "data", b"") or b"")
            except (_json.JSONDecodeError, UnicodeDecodeError):
                return
            if str(msg.get("kind", "")).lower() != "request_drain":
                return
            asyncio.create_task(self._handle_drain_request())

    def _register_track_handler(self, room: rtc.Room) -> None:
        @room.on("track_subscribed")
        def on_track(track, publication, participant):
            if track.kind != rtc.TrackKind.KIND_AUDIO:
                return
            participant_identity = str(getattr(participant, "identity", "") or "")
            publication_sid = str(getattr(publication, "sid", "") or "")
            track_sid = str(getattr(track, "sid", "") or "")
            stream_key = f"{participant_identity}:{publication_sid or track_sid or id(track)}"

            allow, reason = self._should_forward_audio(participant)
            if not allow:
                print(f"[audio-bridge] Ignoring audio from '{participant_identity}' ({reason})")
                return
            if stream_key in self._active_stream_keys:
                return
            self._active_stream_keys.add(stream_key)
            print(f"[audio-bridge] Forwarding audio from '{participant_identity}' ({reason}) key={stream_key}")

            audio_stream = rtc.AudioStream.from_track(
                track=track,
                sample_rate=PEPPER_STREAM_RATE,
                num_channels=1,
            )

            async def stream_task():
                frame_count = 0
                bytes_sent = 0
                start_ts = time.monotonic()
                # Silence-gate state. LiveKit AgentSession publishes a
                # continuous audio track; below the threshold past
                # hangover we drop the frame so paplay's stdin doesn't
                # see continuous silence (cheap, but keeps the wire
                # quiet and makes barge-in detection more reliable).
                gate_enabled = SILENCE_GATE_RMS > 0
                gate_open = True
                last_voiced_ts = time.monotonic()
                silence_dropped = 0
                voiced_forwarded = 0
                trace_frame_idx = 0
                hangover_sec = max(0.0, SILENCE_GATE_HANGOVER_MS / 1000.0)
                try:
                    await self._cancel_existing_streams(reason="new_agent_stream", keep_key=stream_key)
                    async for event in audio_stream:
                        frame = event.frame
                        raw = bytes(frame.data)
                        if not raw:
                            continue

                        # Silence-gate (cheap RMS on raw 16-bit mono).
                        if gate_enabled:
                            rms = audioop.rms(raw, 2)
                            if SILENCE_GATE_TRACE:
                                trace_frame_idx += 1
                                if trace_frame_idx % 5 == 0:
                                    print(
                                        f"[audio-bridge] trace key={stream_key} "
                                        f"rms={rms} gate_open={gate_open} "
                                        f"silence_dropped={silence_dropped}"
                                    )
                            if rms >= SILENCE_GATE_RMS:
                                last_voiced_ts = time.monotonic()
                                if not gate_open:
                                    print(
                                        f"[audio-bridge] gate_open key={stream_key} "
                                        f"after_silence_frames={silence_dropped} rms={rms}"
                                    )
                                    silence_dropped = 0
                                gate_open = True
                            elif (time.monotonic() - last_voiced_ts) > hangover_sec:
                                gate_open = False
                            if not gate_open:
                                silence_dropped += 1
                                if silence_dropped == 1:
                                    print(
                                        f"[audio-bridge] gate_close key={stream_key} "
                                        f"hangover_ms={SILENCE_GATE_HANGOVER_MS} rms={rms}"
                                    )
                                continue

                        sampwidth = 2
                        mono = audioop.mul(raw, sampwidth, PEPPER_STREAM_ATTENUATION)

                        frame_count += 1
                        voiced_forwarded += 1
                        bytes_sent += len(mono)

                        if frame_count == 1:
                            print(
                                f"[audio-bridge] First audio frame from '{participant_identity}' "
                                f"({len(mono)} bytes) gate_rms={SILENCE_GATE_RMS}"
                            )
                            post_debug_event("agent_speaking", active=True)
                        elif frame_count % 200 == 0:
                            elapsed = max(1e-6, time.monotonic() - start_ts)
                            kbps = (bytes_sent * 8.0 / 1000.0) / elapsed
                            print(
                                f"[audio-bridge] heartbeat key={stream_key} "
                                f"voiced_frames={voiced_forwarded} silence_dropped={silence_dropped} "
                                f"kbps={kbps:.1f}"
                            )

                        ok = await self._write_paplay(mono)
                        if not ok and frame_count <= 1:
                            print(
                                f"[audio-bridge] dropping audio — paplay unavailable "
                                f"(retry on next frame)"
                            )

                except asyncio.CancelledError:
                    print(f"[audio-bridge] Cancelled stream for '{participant_identity}' key={stream_key}")
                    raise
                finally:
                    self._active_stream_keys.discard(stream_key)
                    self._stream_tasks.pop(stream_key, None)
                    print(f"[audio-bridge] Audio OFF from '{participant_identity}' frames={frame_count} bytes={bytes_sent}")
                    post_debug_event("agent_speaking", active=False)
                    # Flush on stream-end so the next utterance opens
                    # a fresh paplay pipe. Cheap (<100 ms).
                    await self._flush_audio("stream_end")

            self._stream_tasks[stream_key] = asyncio.create_task(stream_task())

    async def _connect_room(
        self,
        token: str,
        room_name: str | None,
        ws_url: str | None = None,
        target_identity: str | None = None,
    ) -> None:
        async with self._connect_lock:
            current_token = token
            current_ws_url = ws_url
            if ws_url:
                self.livekit_url = str(ws_url).strip() or self.livekit_url
            if target_identity and not self.explicit_target_identity:
                self.target_identity = str(target_identity).strip() or self.target_identity

            if self.room:
                try:
                    await self.room.disconnect()
                except Exception as exc:
                    print(f"[audio-bridge] Warning disconnecting room: {exc}")
                self.room = None

            while True:
                self.room = rtc.Room()
                self._register_speech_handler(self.room)
                self._register_track_handler(self.room)
                try:
                    await self.room.connect(self.livekit_url, current_token)
                    print(f"[audio-bridge] Connected to LiveKit room '{room_name}' via {self.livekit_url}")
                    return
                except Exception as exc:
                    print(f"[audio-bridge] LiveKit connect failed: {exc} — retrying in 3s")
                    self.room = None
                    await asyncio.sleep(3.0)

    async def _on_token_change(self, info: dict) -> None:
        print(f"[audio-bridge] Token change detected — reconnecting room")
        await self._cancel_existing_streams(reason="token_change")
        await self._connect_room(
            info["token"],
            info.get("roomName"),
            ws_url=info.get("wsUrl"),
            target_identity=info.get("agentIdentity"),
        )

    async def run(self) -> None:
        print("[audio-bridge] Starting audio bridge...")

        info = await self.token_watcher.wait_for_initial_token()
        print(f"[audio-bridge] Using identity '{info.get('identity')}' for room '{info.get('roomName')}'")
        await self._connect_room(
            info["token"],
            info.get("roomName"),
            ws_url=info.get("wsUrl"),
            target_identity=info.get("agentIdentity"),
        )
        self._watch_task = asyncio.create_task(
            self.token_watcher.watch(self._on_token_change)
        )

        try:
            # Idle loop. paplay is spawned lazily on first audio frame
            # and managed inside `_write_paplay` / `_flush_audio`; the
            # only thing we do here is keep the event loop alive and
            # let token rotations fire.
            while True:
                await asyncio.sleep(PAPLAY_RESPAWN_BACKOFF_SEC)
        finally:
            await self._cancel_existing_streams(reason="audio_bridge_shutdown")
            if self._watch_task:
                self._watch_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._watch_task
            if self.room:
                await self.room.disconnect()
            await self._close_paplay("shutdown")
            print("[audio-bridge] Stopped.")


async def main():
    bridge = AudioBridge()
    await bridge.run()


if __name__ == "__main__":
    asyncio.run(main())
