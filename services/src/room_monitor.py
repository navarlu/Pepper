"""
Room Monitor — joins the LiveKit room and handles transcripts, text streams,
data channels, and session-manager communication.

No dependency on the TCP bridge or robot hardware.
Runs independently so the UI works even without Pepper connected.
"""

import asyncio
import contextlib
import json
import threading
import time
from collections import deque
from queue import Empty, Full, Queue
from typing import Optional
from urllib.error import URLError
from urllib.request import Request, urlopen

from livekit import rtc

try:
    from .config import (
        BRIDGE_URL,
        LISTENER_LOG_PARTIAL_TRANSCRIPTS,
        LISTENER_LOG_TABLET_POST,
        LIVEKIT_URL,
        MONITOR_IDENTITY,
        SESSION_MANAGER_URL,
        TABLET_DEBUG_LISTENER_ENABLED,
        TABLET_DEBUG_MIN_INTERVAL_LISTENER,
        TABLET_STATUS_ENABLED,
        TABLET_TRANSCRIPT_MAX_LINES,
        TOKEN_POLL_INTERVAL,
    )
    from .shared import SessionWatcher, post_debug_event
except ImportError:
    from config import (
        BRIDGE_URL,
        LISTENER_LOG_PARTIAL_TRANSCRIPTS,
        LISTENER_LOG_TABLET_POST,
        LIVEKIT_URL,
        MONITOR_IDENTITY,
        SESSION_MANAGER_URL,
        TABLET_DEBUG_LISTENER_ENABLED,
        TABLET_DEBUG_MIN_INTERVAL_LISTENER,
        TABLET_STATUS_ENABLED,
        TABLET_TRANSCRIPT_MAX_LINES,
        TOKEN_POLL_INTERVAL,
    )
    from shared import SessionWatcher, post_debug_event
TABLET_DEBUG_MIN_INTERVAL = TABLET_DEBUG_MIN_INTERVAL_LISTENER


# ── Tablet helpers (copied from listener, needed for panel updates) ──

class TabletDebugReporter:
    def __init__(self, enabled: bool):
        self.enabled = enabled
        self._queue: Queue[dict] = Queue(maxsize=8)
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._last_sent_monotonic = 0.0

    def start(self) -> None:
        if not self.enabled or self._worker is not None:
            return
        self._worker = threading.Thread(target=self._run, name="tablet-debug-monitor", daemon=True)
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
        payload = {"text": text, "size": 42, "bg": "#101820", "fg": "#D6F0FF", "align": "left"}
        self._enqueue(payload)

    def publish_payload(self, payload: dict, force: bool = False) -> None:
        if not self.enabled:
            return
        now = time.monotonic()
        if not force and (now - self._last_sent_monotonic) < TABLET_DEBUG_MIN_INTERVAL:
            return
        self._last_sent_monotonic = now
        self._enqueue(payload)

    def _enqueue(self, payload: dict) -> None:
        try:
            self._queue.put_nowait(payload)
        except Full:
            try:
                self._queue.get_nowait()
            except Empty:
                pass
            try:
                self._queue.put_nowait(payload)
            except Full:
                pass

    def _post(self, payload: dict) -> None:
        if not BRIDGE_URL:
            return
        url = f"{BRIDGE_URL}/tablet/text_inline"
        data = json.dumps(payload).encode("utf-8")
        req = Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
        resp = urlopen(req, timeout=0.35)
        body = resp.read()
        if LISTENER_LOG_TABLET_POST:
            print(f"[room-monitor][tablet] POST {url} status={getattr(resp, 'status', 'n/a')} bytes={len(body)}")

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                payload = self._queue.get(timeout=0.2)
            except Empty:
                continue
            try:
                self._post(payload)
            except (URLError, Exception):
                pass


class TabletPanelState:
    def __init__(self, tablet: TabletDebugReporter, max_debug_lines: int = 10):
        self._tablet = tablet
        self._debug_lines: deque = deque(maxlen=max_debug_lines)
        self._last_user = ""
        self._last_pepper = ""
        self._session_state = ""
        self._transcript_items: list = []
        self._lock = threading.Lock()

    def _render_locked(self) -> None:
        payload = {
            "ui": "chat_history",
            "transcript_items": self._transcript_items,
            "session_state": self._session_state,
        }
        self._tablet.publish_payload(payload, force=True)

    def add_debug(self, text: str) -> None:
        clean = " ".join(str(text).strip().split())
        if not clean:
            return
        with self._lock:
            self._debug_lines.append(clean[:180])

    def set_user(self, text: str) -> None:
        clean = " ".join(str(text).strip().split())
        if not clean:
            return
        with self._lock:
            self._last_user = clean

    def set_pepper(self, text: str) -> None:
        clean = " ".join(str(text).strip().split())
        if not clean:
            return
        with self._lock:
            self._last_pepper = clean

    def set_session_status(self, state: str, idle_countdown: str) -> None:
        with self._lock:
            self._session_state = " ".join(str(state).strip().split())
            self._render_locked()

    def set_transcript_items(self, items: list) -> None:
        with self._lock:
            all_items = items or []
            last_ended_idx = -1
            for i, item in enumerate(all_items):
                if (
                    isinstance(item, dict)
                    and item.get("kind") == "session"
                    and "Session ended" in str(item.get("text", ""))
                ):
                    last_ended_idx = i
            if last_ended_idx >= 0:
                all_items = all_items[last_ended_idx + 1:]
            if (
                all_items
                and isinstance(all_items[0], dict)
                and all_items[0].get("kind") == "session"
                and "New session" in str(all_items[0].get("text", ""))
            ):
                all_items = all_items[1:]
            max_items = int(TABLET_TRANSCRIPT_MAX_LINES)
            if len(all_items) > max_items:
                all_items = all_items[-max_items:]
            self._transcript_items = all_items
            self._render_locked()


# ── Room Monitor service ──

class RoomMonitor:
    def __init__(self):
        self.livekit_url = LIVEKIT_URL
        self.token_watcher = SessionWatcher("monitor", TOKEN_POLL_INTERVAL)
        self.room: Optional[rtc.Room] = None
        self._connect_lock = asyncio.Lock()
        self._watch_task: Optional[asyncio.Task] = None
        self.tablet = TabletDebugReporter(TABLET_DEBUG_LISTENER_ENABLED)
        self.panel = TabletPanelState(self.tablet, max_debug_lines=TABLET_TRANSCRIPT_MAX_LINES)
        self.panel.add_debug("Room monitor initialized")

    def _is_agent_participant(self, participant) -> bool:
        identity = str(getattr(participant, "identity", "") or "")
        if identity.startswith("agent-"):
            return True
        kind_text = str(getattr(participant, "kind", "") or "").upper()
        return "AGENT" in kind_text

    def _push_dialogue(self, speaker: str, text: str, source: str) -> None:
        clean = " ".join(str(text).strip().split())
        if not clean:
            return
        print(f"[room-monitor][dialogue] source={source} speaker={speaker} text={clean[:120]}")
        if speaker == "Pepper":
            self.panel.set_pepper(clean)
        else:
            self.panel.set_user(clean)
        self.panel.add_debug(f"{source}: {speaker} updated")

    def _publish_status(self, title: str, body: str = "", force: bool = False) -> None:
        if not TABLET_STATUS_ENABLED:
            return
        self.tablet.publish(title, body, force=force)

    def _register_handlers(self, room: rtc.Room) -> None:
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
                    f"[room-monitor][text_stream] topic={topic} "
                    f"participant={participant_identity} text={content[:120]}"
                )
                speaker = "Pepper" if participant_identity.startswith("agent-") else "User"
                self._push_dialogue(speaker, content, f"text:{topic}")
            except Exception as exc:
                print(f"[room-monitor][text_stream] error topic={topic} err={exc}")

        for topic in ("lk-chat-topic", "chat", "transcription"):
            try:
                room.register_text_stream_handler(
                    topic,
                    lambda reader, pid, _topic=topic: asyncio.create_task(
                        _consume_text_stream(reader, pid, _topic)
                    ),
                )
                print(f"[room-monitor] text stream handler registered topic='{topic}'")
            except Exception as exc:
                print(f"[room-monitor] text stream handler failed topic='{topic}' err={exc}")

        @room.on("data_received")
        def on_data(packet):
            participant = getattr(packet, "participant", None)
            participant_identity = str(getattr(participant, "identity", "") or "")
            topic = str(getattr(packet, "topic", "") or "")
            raw = getattr(packet, "data", b"") or b""
            text = _extract_text_from_payload(topic, raw)
            print(
                f"[room-monitor][data] topic={topic or '<none>'} "
                f"participant={participant_identity or '<server>'} "
                f"bytes={len(raw)} text={text[:120] if text else '<empty>'}"
            )
            if not text:
                return
            if participant is None:
                speaker = "User"
            else:
                speaker = "Pepper" if self._is_agent_participant(participant) else "User"
            self._push_dialogue(speaker, text, f"data:{topic or 'none'}")

        @room.on("transcription_received")
        def on_transcription(segments, participant, publication):
            participant_identity = str(getattr(participant, "identity", "") or "")
            speaker = "Pepper" if self._is_agent_participant(participant) else "User"
            count = len(segments or [])
            print(
                f"[room-monitor][transcription] participant={participant_identity} "
                f"segments={count} publication={getattr(publication, 'sid', '')}"
            )
            for segment in segments or []:
                text = str(getattr(segment, "text", "") or "").strip()
                is_final = bool(getattr(segment, "final", True))
                if is_final or LISTENER_LOG_PARTIAL_TRANSCRIPTS:
                    print(f"[room-monitor][transcription] final={is_final} text={text[:120]}")
                if text and (not is_final) and speaker == "User":
                    self.panel.set_user(text)
                    continue
                if not is_final or not text:
                    continue
                self._push_dialogue(speaker, text, "transcription")
                post_debug_event("transcript", speaker=speaker, text=text)

    async def _connect_room(
        self,
        token: str,
        room_name: Optional[str],
        ws_url: Optional[str] = None,
    ) -> None:
        async with self._connect_lock:
            current_token = token
            current_ws_url = ws_url
            if ws_url:
                self.livekit_url = str(ws_url).strip() or self.livekit_url

            if self.room:
                try:
                    await self.room.disconnect()
                except Exception as exc:
                    print(f"[room-monitor] Warning disconnecting room: {exc}")
                self.room = None

            while True:
                if current_ws_url:
                    self.livekit_url = str(current_ws_url).strip() or self.livekit_url
                room = rtc.Room()
                self._register_handlers(room)
                try:
                    await room.connect(self.livekit_url, current_token)
                except Exception as exc:
                    print(f"[room-monitor] Failed to connect to LiveKit: {exc} - retrying in 3s")
                    latest = self.token_watcher.latest_token_info()
                    if latest and latest.get("token") and latest["token"] != current_token:
                        current_token = latest["token"]
                        current_ws_url = latest.get("wsUrl") or current_ws_url
                        print(f"[room-monitor] Detected fresher token, switching")
                    await asyncio.sleep(3)
                    continue

                self.room = room
                identity = getattr(room.local_participant, "identity", "unknown")
                print(f"[room-monitor] Connected to room '{room.name}' as {identity}")
                break

    async def _on_token_change(self, info: dict) -> None:
        room_name = info.get("roomName") or "<unknown>"
        print(f"[room-monitor] Detected new token for room '{room_name}', reconnecting...")
        await self._connect_room(
            info["token"],
            info.get("roomName"),
            ws_url=info.get("wsUrl"),
        )

    async def run(self) -> None:
        self.tablet.start()
        print("[room-monitor] Starting room monitor...")

        info = await self.token_watcher.wait_for_initial_token()
        print(
            f"[room-monitor] Using identity '{info.get('identity')}' for room '{info.get('roomName')}'"
        )
        await self._connect_room(
            info["token"],
            info.get("roomName"),
            ws_url=info.get("wsUrl"),
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
            self.tablet.stop()
            print("[room-monitor] Stopped.")


async def main():
    monitor = RoomMonitor()
    await monitor.run()


if __name__ == "__main__":
    asyncio.run(main())
