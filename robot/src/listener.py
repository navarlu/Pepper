import asyncio
import audioop
import contextlib
import json
import socket
import time
import threading
from collections import deque
from queue import Empty, Full, Queue
from pathlib import Path
from typing import Awaitable, Callable, Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from livekit import rtc
from config import (
    AGENT_TRACK_IDENTITY,
    ALLOWED_STREAM_RATES,
    BRIDGE_URL,
    LISTENER_LOG_PARTIAL_TRANSCRIPTS,
    LISTENER_LOG_TABLET_POST,
    LISTENER_IDENTITY,
    LIVEKIT_SESSION_FILE,
    LIVEKIT_URL,
    PEPPER_STREAM_ATTENUATION,
    PEPPER_STREAM_RATE,
    TABLET_DEBUG_LISTENER_ENABLED,
    TABLET_DEBUG_MIN_INTERVAL_LISTENER,
    TABLET_STATUS_ENABLED,
    TABLET_TRANSCRIPT_MAX_LINES,
    TCP_HOST,
    TCP_PORT,
    TOKEN_POLL_INTERVAL,
)


def _resolve_stream_rate() -> int:
    raw = int(PEPPER_STREAM_RATE)
    if raw not in ALLOWED_STREAM_RATES:
        print(
            f"[listener_bridge] Unsupported PEPPER_STREAM_RATE={raw}, fallback to 16000"
        )
        return 16000
    return raw


TARGET_RATE = _resolve_stream_rate()
ATTENUATION = PEPPER_STREAM_ATTENUATION
SESSION_FILE = Path(LIVEKIT_SESSION_FILE)
TABLET_DEBUG_MIN_INTERVAL = TABLET_DEBUG_MIN_INTERVAL_LISTENER


class TabletDebugReporter:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._queue: Queue[dict] = Queue(maxsize=8)
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._last_sent_monotonic = 0.0

    def start(self) -> None:
        if not self.enabled:
            return
        if self._worker is not None:
            return
        self._worker = threading.Thread(target=self._run, name="tablet-debug-listener", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        if not self.enabled:
            return
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=1.0)
            self._worker = None

    def publish(self, title: str, body: str = "", force: bool = False) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if not force and (now - self._last_sent_monotonic) < TABLET_DEBUG_MIN_INTERVAL:
            return
        self._last_sent_monotonic = now
        text = title.strip()
        if body.strip():
            text = f"{text}\n{body.strip()}"
        payload = {
            "text": text,
            "size": 42,
            "bg": "#101820",
            "fg": "#D6F0FF",
            "align": "left",
        }
        try:
            self._queue.put_nowait(payload)
        except Full:
            try:
                _ = self._queue.get_nowait()
            except Empty:
                pass
            try:
                self._queue.put_nowait(payload)
            except Full:
                pass

    def publish_payload(self, payload: dict, force: bool = False) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if not force and (now - self._last_sent_monotonic) < TABLET_DEBUG_MIN_INTERVAL:
            return
        self._last_sent_monotonic = now
        try:
            self._queue.put_nowait(payload)
        except Full:
            try:
                _ = self._queue.get_nowait()
            except Empty:
                pass
            try:
                self._queue.put_nowait(payload)
            except Full:
                pass

    def _post(self, payload: dict) -> None:
        if not BRIDGE_URL:
            print("[listener_bridge][tablet] BRIDGE_URL empty, skipping tablet post")
            return
        url = f"{BRIDGE_URL}/tablet/text_inline"
        data = json.dumps(payload).encode("utf-8")
        req = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        resp = urlopen(req, timeout=0.35)
        body = resp.read()
        if LISTENER_LOG_TABLET_POST:
            print(
                f"[listener_bridge][tablet] POST {url} status={getattr(resp, 'status', 'n/a')} "
                f"bytes={len(body)}"
            )

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                payload = self._queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                self._post(payload)
            except URLError:
                pass
            except Exception:
                pass


class TabletPanelState:
    def __init__(self, tablet: TabletDebugReporter, max_debug_lines: int = 10):
        self._tablet = tablet
        self._debug_lines = deque(maxlen=max_debug_lines)
        self._last_user = ""
        self._last_pepper = ""
        self._active_animation = ""
        self._lock = threading.Lock()

    def _render_locked(self) -> None:
        payload = {
            "ui": "split_chat_debug",
            "user_text": self._last_user,
            "pepper_text": self._last_pepper,
            "debug_lines": list(self._debug_lines),
            "active_animation": self._active_animation,
            "bg": "#0D1522",
            "fg": "#D9F3FF",
        }
        self._tablet.publish_payload(payload, force=True)

    def add_debug(self, text: str) -> None:
        clean = " ".join(str(text).strip().split())
        if not clean:
            return
        with self._lock:
            self._debug_lines.append(clean[:180])
            self._render_locked()

    def set_user(self, text: str) -> None:
        clean = " ".join(str(text).strip().split())
        if not clean:
            return
        with self._lock:
            self._last_user = clean
            # Turn-based UI: clear previous Pepper reply once a new user turn starts.
            self._last_pepper = ""
            self._render_locked()

    def set_pepper(self, text: str) -> None:
        clean = " ".join(str(text).strip().split())
        if not clean:
            return
        with self._lock:
            self._last_pepper = clean
            self._render_locked()

    def set_active_animation(self, value: str) -> None:
        with self._lock:
            self._active_animation = " ".join(str(value).strip().split())
            self._render_locked()


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
        self.tablet = TabletDebugReporter(TABLET_DEBUG_LISTENER_ENABLED)
        self.panel = TabletPanelState(
            self.tablet,
            max_debug_lines=TABLET_TRANSCRIPT_MAX_LINES,
        )
        self.panel.add_debug("Listener initialized")

    def _push_debug(self, msg: str) -> None:
        print(f"[listener_bridge][debug] {msg}")
        self.panel.add_debug(msg)

    def _push_dialogue(self, speaker: str, text: str, source: str) -> None:
        clean = " ".join(str(text).strip().split())
        if not clean:
            return
        print(
            f"[listener_bridge][dialogue] source={source} speaker={speaker} text={clean[:120]}"
        )
        if speaker == "Pepper":
            self.panel.set_pepper(clean)
        else:
            self.panel.set_user(clean)
        self.panel.add_debug(f"{source}: {speaker} updated")

    def _publish_status(self, title: str, body: str = "", force: bool = False) -> None:
        if not TABLET_STATUS_ENABLED:
            return
        self.tablet.publish(title, body, force=force)

    def _connect_bridge_socket(self) -> None:
        if self.socket is not None:
            return
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((TCP_HOST, TCP_PORT))
        self.socket = sock
        print(f"[listener_bridge] Forwarding PCM data to {TCP_HOST}:{TCP_PORT}")
        self._push_debug(f"TCP connected {TCP_HOST}:{TCP_PORT}")
        self._publish_status(
            "Listener bridge: TCP connected",
            f"bridge={TCP_HOST}:{TCP_PORT}",
            force=True,
        )

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
                self._push_debug(f"Connected room={room.name} as={identity}")
                self._publish_status(
                    "Listener connected",
                    f"room={room.name}\nas={identity}\nagent={self.target_identity or 'auto'}",
                    force=True,
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
        def _extract_text_from_payload(topic: str, raw: bytes) -> str:
            if not raw:
                return ""
            text = raw.decode("utf-8", errors="ignore").strip()
            if not text:
                return ""
            try:
                obj = json.loads(text)
            except Exception:
                return text
            if isinstance(obj, dict):
                for key in ("text", "message", "content"):
                    value = obj.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
            return text

        async def _consume_text_stream(reader, participant_identity: str, topic: str) -> None:
            try:
                content = (await reader.read_all()).strip()
                if not content:
                    return
                print(
                    f"[listener_bridge][text_stream] topic={topic} "
                    f"participant={participant_identity} text={content[:120]}"
                )
                speaker = "Pepper" if participant_identity.startswith("agent-") else "User"
                self._push_dialogue(speaker, content, f"text:{topic}")
            except Exception as exc:
                print(
                    f"[listener_bridge][text_stream] error topic={topic} "
                    f"participant={participant_identity} err={exc}"
                )

        for topic in ("lk-chat-topic", "chat", "transcription"):
            try:
                room.register_text_stream_handler(
                    topic,
                    lambda reader, pid, _topic=topic: asyncio.create_task(
                        _consume_text_stream(reader, pid, _topic)
                    ),
                )
                print(f"[listener_bridge] text stream handler registered topic='{topic}'")
                self._push_debug(f"text handler topic={topic}")
            except Exception as exc:
                print(
                    f"[listener_bridge] text stream handler registration failed "
                    f"topic='{topic}' err={exc}"
                )
                self._push_debug(f"text handler fail topic={topic}")

        @room.on("data_received")
        def on_data(packet):
            participant = getattr(packet, "participant", None)
            participant_identity = str(getattr(participant, "identity", "") or "")
            topic = str(getattr(packet, "topic", "") or "")
            raw = getattr(packet, "data", b"") or b""
            text = _extract_text_from_payload(topic, raw)
            print(
                f"[listener_bridge][data] topic={topic or '<none>'} "
                f"participant={participant_identity or '<server>'} "
                f"bytes={len(raw)} text={text[:120] if text else '<empty>'}"
            )
            if not text:
                return
            if participant is None:
                speaker = "User"
            else:
                speaker = "Pepper" if self._should_forward_audio(participant)[0] else "User"
            self._push_dialogue(speaker, text, f"data:{topic or 'none'}")

        @room.on("transcription_received")
        def on_transcription(segments, participant, publication):
            participant_identity = str(getattr(participant, "identity", "") or "")
            speaker = (
                "Pepper"
                if self._should_forward_audio(participant)[0]
                else "User"
            )
            count = len(segments or [])
            print(
                f"[listener_bridge][transcription] participant={participant_identity} "
                f"segments={count} publication={getattr(publication, 'sid', '')}"
            )
            for segment in segments or []:
                text = str(getattr(segment, "text", "") or "").strip()
                is_final = bool(getattr(segment, "final", True))
                if is_final or LISTENER_LOG_PARTIAL_TRANSCRIPTS:
                    print(
                        f"[listener_bridge][transcription] final={is_final} text={text[:120]}"
                    )
                # Show interim user text to tablet while speaking.
                if text and (not is_final) and speaker == "User":
                    self.panel.set_user(text)
                    continue
                if not is_final or not text:
                    continue
                self._push_dialogue(speaker, text, "transcription")

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
            self._push_debug(f"Audio ON from {participant_identity}")
            self._publish_status(
                "Audio forwarding ON",
                f"room={getattr(room, 'name', '<unknown>')}\nfrom={participant_identity}",
                force=True,
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
                last_heartbeat_ts = start_ts
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
                            if (time.monotonic() - last_heartbeat_ts) >= 8.0:
                                last_heartbeat_ts = time.monotonic()
                                self._publish_status(
                                    "Audio stream active",
                                    (
                                        f"room={getattr(room, 'name', '<unknown>')}\n"
                                        f"from={participant_identity}\n"
                                        f"frames={frame_count}"
                                    ),
                                )
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
                                self._push_debug("TCP reconnect failed")
                                self._publish_status(
                                    "TCP reconnect failed",
                                    f"error={reconnect_exc}",
                                    force=True,
                                )
                            return
                finally:
                    self._active_stream_keys.discard(stream_key)
                    print(
                        "[listener_bridge] Audio forwarding OFF "
                        f"(stream ended for participant='{participant_identity}', key={stream_key})"
                    )
                    self._push_debug(f"Audio OFF from {participant_identity}")
                    self._publish_status(
                        "Audio forwarding OFF",
                        f"from={participant_identity}",
                        force=True,
                    )
                    print(
                        f"[listener_bridge] Stream ended key={stream_key} frames={frame_count} bytes={bytes_sent}"
                    )

            asyncio.create_task(stream_task())

    async def _on_token_change(self, info: dict) -> None:
        room_name = info.get("roomName") or "<unknown>"
        print(
            f"[listener_bridge] Detected new listener token for room '{room_name}', reconnecting..."
        )
        self._push_debug(f"Token update room={room_name}")
        self._publish_status(
            "Token updated",
            f"room={room_name}\nreconnecting...",
            force=True,
        )
        await self._connect_room(
            info["token"],
            info.get("roomName"),
            ws_url=info.get("wsUrl"),
            target_identity=info.get("agentIdentity"),
        )

    async def run(self) -> None:
        self.tablet.start()
        self._publish_status(
            "Listener bridge starting",
            f"session_file={SESSION_FILE}",
            force=True,
        )
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
            self._publish_status("Listener bridge stopped", force=True)
            self.tablet.stop()


async def main():
    bridge = ListenerPepperBridge()
    await bridge.run()


if __name__ == "__main__":
    asyncio.run(main())
