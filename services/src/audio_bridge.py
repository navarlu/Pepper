"""
Audio Bridge — subscribes to agent audio in LiveKit and forwards PCM
to the Pepper robot via TCP.

Non-blocking: starts and joins LiveKit even if the robot bridge is
unavailable. Logs a warning and retries the TCP connection periodically.
Audio frames are silently dropped while the bridge is disconnected.
"""

import asyncio
import audioop
import contextlib
import json
import socket
import time
from typing import Optional

from livekit import rtc

try:
    from .config import (
        AGENT_TRACK_IDENTITY,
        ALLOWED_STREAM_RATES,
        LISTENER_IDENTITY,
        LIVEKIT_URL,
        PEPPER_STREAM_ATTENUATION,
        PEPPER_STREAM_RATE,
        SESSION_MANAGER_URL,
        TCP_HOST,
        TCP_PORT,
        TOKEN_POLL_INTERVAL,
    )
    from .shared import AgentActivityReporter, SessionWatcher, post_debug_event
except ImportError:
    from config import (
        AGENT_TRACK_IDENTITY,
        ALLOWED_STREAM_RATES,
        LISTENER_IDENTITY,
        LIVEKIT_URL,
        PEPPER_STREAM_ATTENUATION,
        PEPPER_STREAM_RATE,
        SESSION_MANAGER_URL,
        TCP_HOST,
        TCP_PORT,
        TOKEN_POLL_INTERVAL,
    )
    from shared import AgentActivityReporter, SessionWatcher, post_debug_event


def _resolve_stream_rate() -> int:
    raw = int(PEPPER_STREAM_RATE)
    if raw not in ALLOWED_STREAM_RATES:
        print(f"[audio-bridge] Unsupported PEPPER_STREAM_RATE={raw}, fallback to 16000")
        return 16000
    return raw


TARGET_RATE = _resolve_stream_rate()
ATTENUATION = PEPPER_STREAM_ATTENUATION
BRIDGE_RETRY_SEC = 5


class AudioBridge:
    def __init__(self):
        self.livekit_url = LIVEKIT_URL
        self.token_watcher = SessionWatcher("listener", TOKEN_POLL_INTERVAL)
        self.target_identity = AGENT_TRACK_IDENTITY or None
        self.explicit_target_identity = bool(self.target_identity)
        self.tcp_socket: Optional[socket.socket] = None
        self.room: Optional[rtc.Room] = None
        self._connect_lock = asyncio.Lock()
        self._socket_send_lock = asyncio.Lock()
        self._watch_task: Optional[asyncio.Task] = None
        self._active_stream_keys: set[str] = set()
        self._stream_tasks: dict[str, asyncio.Task] = {}
        self._activity = AgentActivityReporter()
        self._bridge_warn_logged = False

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
            return True
        except Exception as exc:
            if not self._bridge_warn_logged:
                print(
                    f"[audio-bridge] Bridge unavailable at {TCP_HOST}:{TCP_PORT} "
                    f"({exc}) — audio will not be forwarded until connected"
                )
                self._bridge_warn_logged = True
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

    async def _cancel_existing_streams(self, reason: str, keep_key: Optional[str] = None) -> None:
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
                sample_rate=TARGET_RATE,
                num_channels=1,
            )

            async def stream_task():
                frame_count = 0
                bytes_sent = 0
                start_ts = time.monotonic()
                last_heartbeat_ts = start_ts
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

                        sampwidth = 2
                        mono = audioop.mul(raw, sampwidth, ATTENUATION)
                        size_bytes = len(mono).to_bytes(4, "big")

                        frame_count += 1
                        bytes_sent += len(mono)

                        if (time.monotonic() - last_heartbeat_ts) >= 8.0:
                            last_heartbeat_ts = time.monotonic()

                        if frame_count == 1:
                            print(f"[audio-bridge] First audio frame from '{participant_identity}' ({len(mono)} bytes)")
                            self._activity.report()
                            post_debug_event("agent_speaking", active=True)
                        elif frame_count % 200 == 0:
                            elapsed = max(1e-6, time.monotonic() - start_ts)
                            kbps = (bytes_sent * 8.0 / 1000.0) / elapsed
                            print(f"[audio-bridge] heartbeat key={stream_key} frames={frame_count} kbps={kbps:.1f}")
                            self._activity.report()

                        try:
                            rms = audioop.rms(mono, 2) / 32768.0
                        except Exception:
                            rms = 0.0
                        post_debug_event("agent_level", level=rms)

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
        room_name: Optional[str],
        ws_url: Optional[str] = None,
        target_identity: Optional[str] = None,
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
