import asyncio
import audioop
import contextlib
import json
import os
import socket
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

from livekit import rtc


def _load_repo_env() -> None:
    env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_repo_env()

LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880")
_ALLOWED_RATES = {16000, 22050, 44100, 48000}


def _resolve_stream_rate() -> int:
    raw = int(os.environ.get("PEPPER_STREAM_RATE", "16000"))
    if raw not in _ALLOWED_RATES:
        print(
            f"[listener_bridge] Unsupported PEPPER_STREAM_RATE={raw}, fallback to 16000"
        )
        return 16000
    return raw


TARGET_RATE = _resolve_stream_rate()
ATTENUATION = 0.4

TCP_HOST = os.environ.get("TCP_HOST", "127.0.0.1")
TCP_PORT = int(os.environ.get("TCP_PORT", "55555"))

DEFAULT_SESSION_FILE = (
    Path(__file__).resolve().parents[2]
    / "web"
    / "agents-playground"
    / "token-latest.json"
)
SESSION_FILE = Path(
    os.environ.get("LIVEKIT_SESSION_FILE", DEFAULT_SESSION_FILE)
)
LISTENER_IDENTITY = os.environ.get("LISTENER_IDENTITY", "listener-python")
AGENT_TRACK_IDENTITY = (os.environ.get("AGENT_TRACK_IDENTITY") or "").strip()
TOKEN_POLL_INTERVAL = float(os.environ.get("TOKEN_POLL_INTERVAL", "0.5"))


class SessionWatcher:
    def __init__(
        self,
        identity: str,
        poll_interval: float,
    ):
        self.identity = identity
        self.poll_interval = poll_interval
        self._last_token: Optional[str] = None
        self._missing_logged = False

    def _read_latest_snapshot(self) -> Optional[dict]:
        try:
            text = SESSION_FILE.read_text(encoding="utf-8")
            data = json.loads(text)
            return data
        except FileNotFoundError:
            if not self._missing_logged:
                print(
                    f"[token-watcher] Waiting for LiveKit session snapshot in {SESSION_FILE}"
                )
                self._missing_logged = True
            return None
        except json.JSONDecodeError:
            print(
                f"[token-watcher] Invalid JSON in {SESSION_FILE}, waiting for next update"
            )
            return None

    def _extract_token_info(self) -> Optional[dict]:
        snapshot = self._read_latest_snapshot()
        if not snapshot:
            return None
        if self._missing_logged:
            print(f"[token-watcher] Found session snapshot in {SESSION_FILE}")
            self._missing_logged = False
        role_data = snapshot.get("listener")
        if not isinstance(role_data, dict):
            return None
        token = role_data.get("token")
        if not token:
            return None
        return {
            "token": token,
            "roomName": snapshot.get("roomName"),
            "identity": role_data.get("identity"),
            "wsUrl": snapshot.get("wsUrl"),
            "agentIdentity": (
                (snapshot.get("agent") or {}).get("identity")
                if isinstance(snapshot.get("agent"), dict)
                else None
            ),
            "generatedAt": snapshot.get("generatedAt"),
        }

    async def wait_for_initial_token(self) -> dict:
        while True:
            info = self._extract_token_info()
            if info:
                self._last_token = info["token"]
                return info
            await asyncio.sleep(self.poll_interval)

    async def watch(
        self, on_change: Callable[[dict], Awaitable[None]]
    ) -> None:
        while True:
            info = self._extract_token_info()
            if info and info["token"] != self._last_token:
                self._last_token = info["token"]
                await on_change(info)
            await asyncio.sleep(self.poll_interval)


class ListenerPepperBridge:
    def __init__(self):
        self.livekit_url = LIVEKIT_URL
        self.token_watcher = SessionWatcher(
            LISTENER_IDENTITY, TOKEN_POLL_INTERVAL
        )
        self.target_identity = AGENT_TRACK_IDENTITY or None
        self.explicit_target_identity = bool(self.target_identity)
        self.socket: Optional[socket.socket] = None
        self.room: Optional[rtc.Room] = None
        self._connect_lock = asyncio.Lock()
        self._watch_task: Optional[asyncio.Task] = None
        self._active_stream_keys: set[str] = set()

    def _connect_bridge_socket(self) -> None:
        if self.socket is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((TCP_HOST, TCP_PORT))
        self.socket = sock
        print(f"[listener_bridge] Forwarding PCM data to {TCP_HOST}:{TCP_PORT}")

    async def _connect_room(
        self,
        token: str,
        room_name: Optional[str],
        ws_url: Optional[str] = None,
        target_identity: Optional[str] = None,
    ) -> None:
        async with self._connect_lock:
            if ws_url:
                self.livekit_url = str(ws_url).strip() or self.livekit_url
            # Respect AGENT_TRACK_IDENTITY env var as strict override.
            if target_identity and not self.explicit_target_identity:
                self.target_identity = str(target_identity).strip() or self.target_identity

            if self.room:
                try:
                    await self.room.disconnect()
                except Exception as exc:
                    print("[listener_bridge] Warning disconnecting room:", exc)
                self.room = None

            while True:
                room = rtc.Room()
                self._register_track_handler(room)
                try:
                    await room.connect(self.livekit_url, token)
                except Exception as exc:
                    print(
                        "[listener_bridge] Failed to connect to LiveKit:",
                        exc,
                        "- retrying in 3s",
                    )
                    await asyncio.sleep(3)
                    continue

                self.room = room
                identity = getattr(room.local_participant, "identity", "unknown")
                print(
                    f"[listener_bridge] Connected to room '{room.name}' as {identity}"
                )
                if self.target_identity:
                    mode = "strict" if self.explicit_target_identity else "hint"
                    print(
                        f"[listener_bridge] agent identity filter ({mode}) = '{self.target_identity}'"
                    )
                break

    def _is_agent_like_participant(self, participant, participant_identity: str) -> bool:
        if participant_identity.startswith("agent-"):
            return True
        kind = getattr(participant, "kind", None)
        kind_text = str(kind or "").upper()
        return "AGENT" in kind_text

    def _should_forward_audio(self, participant) -> tuple[bool, str]:
        participant_identity = str(getattr(participant, "identity", "") or "")
        if participant_identity == LISTENER_IDENTITY:
            return False, "skip_listener_identity"

        if self.explicit_target_identity:
            if participant_identity == self.target_identity:
                return True, "explicit_identity_match"
            return False, "explicit_identity_mismatch"

        if self.target_identity and participant_identity == self.target_identity:
            return True, "token_identity_match"

        if self._is_agent_like_participant(participant, participant_identity):
            return True, "agent_like_fallback"

        return False, "not_agent_like"

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
                print(
                    f"[listener_bridge] Ignoring audio track from '{participant_identity}' ({reason})"
                )
                return
            if stream_key in self._active_stream_keys:
                print(
                    f"[listener_bridge] Duplicate audio subscription ignored for '{participant_identity}' "
                    f"(key={stream_key})"
                )
                return
            self._active_stream_keys.add(stream_key)
            print(
                f"[listener_bridge] Forwarding audio track from '{participant_identity}' ({reason}) "
                f"key={stream_key}"
            )

            audio_stream = rtc.AudioStream.from_track(
                track=track,
                sample_rate=TARGET_RATE,
                num_channels=1,
            )

            async def stream_task():
                frame_count = 0
                bytes_sent = 0
                last_frame_ts = time.monotonic()
                start_ts = last_frame_ts
                try:
                    async for event in audio_stream:
                        frame = event.frame
                        raw = bytes(frame.data)
                        if not raw or not self.socket:
                            continue

                        now = time.monotonic()
                        inter_frame_ms = (now - last_frame_ts) * 1000.0
                        last_frame_ts = now

                        sampwidth = 2
                        mono = raw
                        mono = audioop.mul(mono, sampwidth, ATTENUATION)
                        size_bytes = len(mono).to_bytes(4, "big")
                        try:
                            frame_count += 1
                            bytes_sent += len(mono)
                            if frame_count == 1:
                                print(
                                    f"[listener_bridge] First audio frame from '{participant_identity}' "
                                    f"({len(mono)} bytes, inter={inter_frame_ms:.2f} ms)"
                                )
                            elif frame_count % 200 == 0:
                                elapsed = max(1e-6, time.monotonic() - start_ts)
                                kbps = (bytes_sent * 8.0 / 1000.0) / elapsed
                                print(
                                    f"[listener_bridge] stream heartbeat key={stream_key} "
                                    f"frames={frame_count} inter={inter_frame_ms:.2f} ms kbps={kbps:.1f}"
                                )
                            self.socket.sendall(size_bytes + mono)
                        except (BrokenPipeError, ConnectionError) as exc:
                            print("[listener_bridge] TCP send failure:", exc)
                            try:
                                if self.socket:
                                    self.socket.close()
                            finally:
                                self.socket = None
                            try:
                                self._connect_bridge_socket()
                            except Exception as reconnect_exc:
                                print("[listener_bridge] TCP reconnect failed:", reconnect_exc)
                            return
                finally:
                    self._active_stream_keys.discard(stream_key)
                    print(
                        f"[listener_bridge] Stream ended key={stream_key} frames={frame_count} bytes={bytes_sent}"
                    )

            asyncio.create_task(stream_task())

    async def _on_token_change(self, info: dict) -> None:
        room_name = info.get("roomName") or "<unknown>"
        print(
            f"[listener_bridge] Detected new listener token for room '{room_name}', reconnecting..."
        )
        await self._connect_room(
            info["token"],
            info.get("roomName"),
            ws_url=info.get("wsUrl"),
            target_identity=info.get("agentIdentity"),
        )

    async def run(self) -> None:
        self._connect_bridge_socket()
        info = await self.token_watcher.wait_for_initial_token()
        print(
            f"[listener_bridge] Using listener identity '{info.get('identity')}' for room '{info.get('roomName')}'"
        )
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
                await asyncio.sleep(1)
        finally:
            if self._watch_task:
                self._watch_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._watch_task
            if self.room:
                await self.room.disconnect()
            if self.socket:
                self.socket.close()
                self.socket = None


async def main():
    bridge = ListenerPepperBridge()
    await bridge.run()


if __name__ == "__main__":
    asyncio.run(main())
