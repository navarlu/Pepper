"""
Audio Bridge — subscribes to agent audio in LiveKit and forwards PCM
to the Pepper robot via TCP.

Non-blocking: starts and joins LiveKit even if the robot bridge is
unavailable. Logs a warning and retries the TCP connection periodically.
Audio frames are silently dropped while the bridge is disconnected.
"""

from __future__ import annotations

import asyncio
import audioop
import contextlib
import socket
import time

from livekit import rtc

from config import (
    AGENT_TRACK_IDENTITY,
    LISTENER_IDENTITY,
    LIVEKIT_URL,
    PEPPER_STREAM_ATTENUATION,
    PEPPER_STREAM_RATE,
    SILENCE_GATE_HANGOVER_MS,
    SILENCE_GATE_RMS,
    SILENCE_GATE_TRACE,
    TCP_HOST,
    TCP_PORT,
    TOKEN_POLL_INTERVAL,
)
from session import SessionWatcher, post_debug_event

# PEPPER_STREAM_RATE is already validated against ALLOWED_STREAM_RATES by config.py.
BRIDGE_RETRY_SEC = 5

# Control frames over the bridge socket. The first two mirror
# robot/src/utils.py and are pre-existing protocol — do not change.
# DRAIN_REQ/DRAIN_ACK are new: round-trip "is Pepper's speaker really
# idle?" signal so the worker's send_message_to_user can return at
# true end-of-speech instead of when LiveKit's emitter drains.
CONTROL_FRAME_FLUSH = 0
CONTROL_FRAME_PING = 0xFFFFFFFF
CONTROL_FRAME_DRAIN_REQ = 0xFFFFFFFE   # service → robot
CONTROL_FRAME_DRAIN_ACK = 0xFFFFFFFD   # robot → service

TOPIC_SPEECH = "pepper.speech"


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    """Receive exactly n bytes from a blocking socket. Returns b'' on
    EOF or shorter buffer if the peer closed mid-frame. Run in a
    thread executor so the event loop isn't blocked.
    """
    buf = bytearray()
    while len(buf) < n:
        try:
            chunk = sock.recv(n - len(buf))
        except OSError:
            return bytes(buf)
        if not chunk:
            return bytes(buf)
        buf.extend(chunk)
    return bytes(buf)


class AudioBridge:
    """Joins LiveKit as the `listener`, forwards the agent's audio to
    the robot bridge over a long-lived TCP connection.

    Non-blocking on every axis:
      - The TCP bridge connection retries in the background; LiveKit
        streams keep running even when the bridge is offline (frames
        are silently dropped).
      - Token rotations from the orchestrator are picked up via
        `SessionWatcher` and trigger a clean room reconnect.
      - Multiple concurrent agent streams are coalesced so only one
        feeds the bridge at a time (old streams cancelled on new one).
    """

    def __init__(self):
        self.livekit_url = LIVEKIT_URL
        self.token_watcher = SessionWatcher("listener", TOKEN_POLL_INTERVAL)
        self.target_identity = AGENT_TRACK_IDENTITY or None
        self.explicit_target_identity = bool(self.target_identity)
        self.tcp_socket: socket.socket | None = None
        self.room: rtc.Room | None = None
        self._connect_lock = asyncio.Lock()
        self._socket_send_lock = asyncio.Lock()
        self._watch_task: asyncio.Task | None = None
        self._active_stream_keys: set[str] = set()
        self._stream_tasks: dict[str, asyncio.Task] = {}
        self._bridge_warn_logged = False
        # Background reader for control frames coming back from the robot
        # (currently only DRAIN_ACK). Restarted on every new socket.
        self._drain_reader_task: asyncio.Task | None = None

    # ── TCP bridge (non-blocking) ──

    def _try_connect_bridge(self) -> bool:
        """Single attempt to connect to the robot bridge. Returns True on success."""
        if self.tcp_socket is not None:
            return True
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect((TCP_HOST, TCP_PORT))
            self.tcp_socket = sock
            self._bridge_warn_logged = False
            print(f"[audio-bridge] Connected to bridge {TCP_HOST}:{TCP_PORT}")
            self._restart_drain_reader()
            return True
        except Exception as exc:
            if not self._bridge_warn_logged:
                print(
                    f"[audio-bridge] Bridge unavailable at {TCP_HOST}:{TCP_PORT} "
                    f"({exc}) — audio will not be forwarded until connected"
                )
                self._bridge_warn_logged = True
            return False

    def _restart_drain_reader(self) -> None:
        """Spawn a background reader for 4-byte control frames coming
        back from the robot. Only DRAIN_ACK is handled today; any
        other value is logged and ignored. Cancelled and restarted on
        every new socket so a stale reader can't keep reading from a
        dead fd.
        """
        if self._drain_reader_task and not self._drain_reader_task.done():
            self._drain_reader_task.cancel()
        self._drain_reader_task = asyncio.create_task(self._drain_reader_loop())

    async def _drain_reader_loop(self) -> None:
        """Read 4-byte big-endian frames from the bridge socket until
        the socket dies, publishing `pepper.speech speaker_drained`
        on DRAIN_ACK. The recv is done in a thread executor so the
        blocking socket doesn't stall the event loop.
        """
        loop = asyncio.get_running_loop()
        sock = self.tcp_socket
        if sock is None:
            return
        while self.tcp_socket is sock:
            try:
                data = await loop.run_in_executor(None, _recv_exact, sock, 4)
            except Exception as exc:
                print(f"[audio-bridge] drain_reader recv failed: {exc!r}")
                return
            if not data or len(data) < 4:
                # Socket closed by peer.
                return
            value = int.from_bytes(data, "big")
            if value == CONTROL_FRAME_DRAIN_ACK:
                print(f"[audio-bridge] drain_ack_received bytes=4")
                post_debug_event("agent_speaking", active=False)
                await self._publish_speaker_drained(reason="robot_ack")
            else:
                # Unknown control frame — log and ignore. Don't kill the
                # reader: a stray value shouldn't take EOS reporting offline.
                print(f"[audio-bridge] drain_reader unknown_frame value=0x{value:08x}")

    async def _publish_speaker_drained(self, reason: str) -> None:
        """Tell the worker that Pepper's speaker has actually drained.

        Published over LiveKit so the experiment worker's data_received
        handler (in _pipeline.py) can wake send_message_to_user. If
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

    async def _send_bridge_drain_req(self) -> bool:
        """Ask the robot bridge to ACK once ALAudioDevice's queue
        drains. The robot writes back a single 4-byte DRAIN_ACK frame
        which our background reader picks up.
        """
        if not self.tcp_socket:
            return False
        try:
            async with self._socket_send_lock:
                if not self.tcp_socket:
                    return False
                self.tcp_socket.sendall(CONTROL_FRAME_DRAIN_REQ.to_bytes(4, "big"))
            print(f"[audio-bridge] drain_req_sent socket_connected=True")
            return True
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            print(f"[audio-bridge] drain_req send failed err={exc!r}")
            self._close_socket()
            return False

    async def _send_bridge_flush(self, reason: str) -> None:
        if not self.tcp_socket:
            return
        try:
            async with self._socket_send_lock:
                if not self.tcp_socket:
                    return
                self.tcp_socket.sendall((0).to_bytes(4, "big"))
            print(f"[audio-bridge] Sent bridge flush reason={reason}")
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            print(f"[audio-bridge] Bridge flush failed reason={reason} err={exc}")
            self._close_socket()

    async def _send_bridge_ping(self) -> bool:
        if not self.tcp_socket:
            return False
        try:
            async with self._socket_send_lock:
                if not self.tcp_socket:
                    return False
                self.tcp_socket.sendall((0xFFFFFFFF).to_bytes(4, "big"))
            return True
        except (BrokenPipeError, ConnectionError, OSError) as exc:
            print(f"[audio-bridge] Bridge ping failed err={exc}")
            self._close_socket()
            return False

    def _close_socket(self) -> None:
        try:
            if self.tcp_socket:
                self.tcp_socket.close()
        finally:
            self.tcp_socket = None

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
        await self._send_bridge_flush(reason)

    def _register_speech_handler(self, room: rtc.Room) -> None:
        """Listen for `pepper.speech` data messages from the worker.

        The worker publishes `{"kind": "request_drain"}` after every
        `send_message_to_user` finishes its TTS playout. We turn that
        into a DRAIN_REQ over the bridge TCP socket so the on-robot
        bridge can ACK once ALAudioDevice's queue has actually drained.

        If the bridge socket is down, we publish speaker_drained
        immediately with reason=no_robot_bridge so the worker doesn't
        hang on the DRAIN_TIMEOUT (typical dev-mode scenario:
        audio-bridge container up, robot off).
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

            async def _handle_request() -> None:
                sent = await self._send_bridge_drain_req()
                if not sent:
                    print(
                        "[audio-bridge] no_bridge_socket — publishing "
                        "speaker_drained immediately"
                    )
                    await self._publish_speaker_drained(reason="no_robot_bridge")

            asyncio.create_task(_handle_request())

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
                # continuous audio track (silence frames between TTS
                # utterances), so without gating NAOqi receives ~1.6×
                # real-time audio and `sendRemoteBufferToOutput` backs
                # up — the audible 60-s delay before this fix.
                #
                # Strategy: forward frames whose RMS is above
                # SILENCE_GATE_RMS, plus a long hangover after the last
                # loud frame so we don't clamp shut during natural
                # within-utterance pauses. Below threshold beyond
                # hangover, drop the frame WITHOUT flushing — flushing
                # mid-stream would discard legitimate audio still on
                # its way to the speaker (an earlier version did this
                # and it cut off words).
                gate_enabled = SILENCE_GATE_RMS > 0
                gate_open = True  # forward on startup; closes after first hangover
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

                        # If bridge not connected, drop frames silently
                        if not self.tcp_socket:
                            if frame_count == 0:
                                print(f"[audio-bridge] Dropping audio — bridge not connected")
                            frame_count += 1
                            continue

                        # Silence-gate (cheap RMS on raw 16-bit mono).
                        # Compute before attenuation so the threshold
                        # is independent of PEPPER_STREAM_ATTENUATION.
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
                                # Past the hangover window — close the
                                # gate. Note: NO flush here. NAOqi will
                                # finish playing whatever it received
                                # naturally; flushing would chop the
                                # tail of the last utterance.
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
                        size_bytes = len(mono).to_bytes(4, "big")

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

                        try:
                            async with self._socket_send_lock:
                                if not self.tcp_socket:
                                    continue
                                self.tcp_socket.sendall(size_bytes + mono)
                        except (BrokenPipeError, ConnectionError, OSError) as exc:
                            print(f"[audio-bridge] TCP send failure: {exc}")
                            self._close_socket()

                except asyncio.CancelledError:
                    print(f"[audio-bridge] Cancelled stream for '{participant_identity}' key={stream_key}")
                    raise
                finally:
                    self._active_stream_keys.discard(stream_key)
                    self._stream_tasks.pop(stream_key, None)
                    print(f"[audio-bridge] Audio OFF from '{participant_identity}' frames={frame_count} bytes={bytes_sent}")
                    post_debug_event("agent_speaking", active=False)
                    await self._send_bridge_flush("stream_end")

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
                if current_ws_url:
                    self.livekit_url = str(current_ws_url).strip() or self.livekit_url
                room = rtc.Room()
                self._register_track_handler(room)
                self._register_speech_handler(room)
                try:
                    await room.connect(self.livekit_url, current_token)
                except Exception as exc:
                    print(f"[audio-bridge] Failed to connect to LiveKit: {exc} - retrying in 3s")
                    latest = self.token_watcher.latest_token_info()
                    if latest and latest.get("token") and latest["token"] != current_token:
                        current_token = latest["token"]
                        current_ws_url = latest.get("wsUrl") or current_ws_url
                    await asyncio.sleep(3)
                    continue

                self.room = room
                identity = getattr(room.local_participant, "identity", "unknown")
                print(f"[audio-bridge] Connected to room '{room.name}' as {identity}")
                if self.target_identity:
                    mode = "strict" if self.explicit_target_identity else "hint"
                    print(f"[audio-bridge] Agent identity filter ({mode}) = '{self.target_identity}'")
                break

    async def _on_token_change(self, info: dict) -> None:
        room_name = info.get("roomName") or "<unknown>"
        print(f"[audio-bridge] Detected new token for room '{room_name}', reconnecting...")
        await self._connect_room(
            info["token"],
            info.get("roomName"),
            ws_url=info.get("wsUrl"),
            target_identity=info.get("agentIdentity"),
        )

    async def run(self) -> None:
        print("[audio-bridge] Starting audio bridge...")

        # Non-blocking bridge attempt
        self._try_connect_bridge()

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
            while True:
                # Retry bridge connection periodically
                if self.tcp_socket is None:
                    self._try_connect_bridge()
                else:
                    ping_ok = await self._send_bridge_ping()
                    if not ping_ok:
                        self._try_connect_bridge()
                await asyncio.sleep(BRIDGE_RETRY_SEC)
        finally:
            await self._cancel_existing_streams(reason="audio_bridge_shutdown")
            if self._watch_task:
                self._watch_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._watch_task
            if self.room:
                await self.room.disconnect()
            self._close_socket()
            print("[audio-bridge] Stopped.")


async def main():
    bridge = AudioBridge()
    await bridge.run()


if __name__ == "__main__":
    asyncio.run(main())
