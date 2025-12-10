import asyncio
import audioop
import contextlib
import json
import os
import socket
from pathlib import Path
from typing import Awaitable, Callable, Optional

from livekit import rtc

LIVEKIT_URL = os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880")
TARGET_RATE = 48000
ATTENUATION = 0.4

TCP_HOST = os.environ.get("TCP_HOST", "127.0.0.1")
TCP_PORT = int(os.environ.get("TCP_PORT", "55555"))

DEFAULT_SESSION_FILE = (
    Path(__file__).resolve().parents[1]
    / "web"
    / "agents-playground"
    / "token-latest.json"
)
SESSION_FILE = Path(
    os.environ.get("LIVEKIT_SESSION_FILE", DEFAULT_SESSION_FILE)
)
LISTENER_IDENTITY = os.environ.get("LISTENER_IDENTITY", "listener-python")
TOKEN_POLL_INTERVAL = float(os.environ.get("TOKEN_POLL_INTERVAL", "2.0"))


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
        self.socket: Optional[socket.socket] = None
        self.room: Optional[rtc.Room] = None
        self._connect_lock = asyncio.Lock()
        self._watch_task: Optional[asyncio.Task] = None

    def _connect_bridge_socket(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((TCP_HOST, TCP_PORT))
        self.socket = sock
        print(f"[listener_bridge] Forwarding PCM data to {TCP_HOST}:{TCP_PORT}")

    async def _connect_room(self, token: str, room_name: Optional[str]) -> None:
        async with self._connect_lock:
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
                break

    def _register_track_handler(self, room: rtc.Room) -> None:
        @room.on("track_subscribed")
        def on_track(track, publication, participant):
            if track.kind != rtc.TrackKind.KIND_AUDIO:
                return

            audio_stream = rtc.AudioStream(track)

            async def stream_task():
                state = None
                async for event in audio_stream:
                    frame = event.frame
                    raw = bytes(frame.data)
                    if not raw or not self.socket:
                        continue

                    sampwidth = 2
                    nch = frame.num_channels

                    if nch == 2:
                        mono = audioop.tomono(raw, sampwidth, 0.5, 0.5)
                    else:
                        mono = raw

                    if frame.sample_rate != TARGET_RATE:
                        mono, state = audioop.ratecv(
                            mono,
                            sampwidth,
                            1,
                            frame.sample_rate,
                            TARGET_RATE,
                            state,
                        )

                    mono = audioop.mul(mono, sampwidth, ATTENUATION)
                    size_bytes = len(mono).to_bytes(4, "big")
                    try:
                        self.socket.sendall(size_bytes + mono)
                    except (BrokenPipeError, ConnectionError) as exc:
                        print("[listener_bridge] TCP send failure:", exc)
                        return

            asyncio.create_task(stream_task())

    async def _on_token_change(self, info: dict) -> None:
        room_name = info.get("roomName") or "<unknown>"
        print(
            f"[listener_bridge] Detected new listener token for room '{room_name}', reconnecting..."
        )
        await self._connect_room(info["token"], info.get("roomName"))

    async def run(self) -> None:
        self._connect_bridge_socket()
        info = await self.token_watcher.wait_for_initial_token()
        print(
            f"[listener_bridge] Using listener identity '{info.get('identity')}' for room '{info.get('roomName')}'"
        )
        await self._connect_room(info["token"], info.get("roomName"))
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
