"""User Client — publishes the RPi microphone into the LiveKit room.

Runs on the host (needs ALSA access), connects as identity `user`,
and publishes a single microphone track captured via `sounddevice`.
Also listens on the `pepper.state` LiveKit data topic for soft
mute/unmute commands from the orchestrator; muted frames are zeroed
rather than disconnecting, so the user's room presence stays stable
across mute toggles.

Resilient reconnect: the `_room_monitor_loop` watches for token
rotations (via the session snapshot), WebRTC state changes, and
stuck-in-reconnecting timeouts — any of them forces a clean
re-dispatch through `_run_once`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import time
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from livekit import rtc

from config import (
    SESSION_ACTIVITY_DEBOUNCE_SEC,
    USER_CLIENT_TEST_MODE,
    USER_MIC_BLOCKSIZE,
    USER_MIC_CHANNELS,
    USER_MIC_DEVICE,
    USER_MIC_RMS_THRESHOLD,
    USER_MIC_SAMPLE_RATE,
)
from session import SessionWatcher

ROOT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
TOPIC_CHAT = "lk.chat"


def _load_root_env() -> None:
    if ROOT_ENV_PATH.exists():
        load_dotenv(dotenv_path=ROOT_ENV_PATH, override=True)


class UserAudioClient:
    """Publishes the host microphone into the LiveKit room as `user`.

    Orchestrated entirely by the session file: on every token rotation
    or WebRTC disconnect we tear down and re-run `_run_once`. The
    microphone stream itself uses `sounddevice` → async queue →
    `rtc.AudioSource.capture_frame` so the audio callback never blocks
    on I/O.

    Mute is *soft*: `mic_muted=True` zeroes outgoing frame bytes rather
    than disconnecting. That keeps the participant present in the room
    (agent keeps its `participant_identity` binding) across toggles.
    """

    def __init__(self) -> None:
        _load_root_env()
        self._token_watcher = SessionWatcher("user")
        self.source: rtc.AudioSource | None = None
        self.room: rtc.Room | None = None
        self.audio_queue: asyncio.Queue[tuple[bytes, int, float]] | None = None
        self._last_activity_post_monotonic = 0.0
        self._frames_sent = 0
        self._last_audio_log_monotonic = 0.0
        self._peak_rms = 0.0
        self._last_level_post_monotonic = 0.0
        self.test_mode = str(USER_CLIENT_TEST_MODE or "publish").strip().lower()
        self.mic_muted = False
        self._component_state = ""
        self._component_detail = ""
        self._last_component_status_monotonic = 0.0
        self._reconnect_requested = asyncio.Event()
        self._reconnect_reason = ""
        self._snapshot_signature = ""
        self._connected_room_name = ""
        self._connected_identity = ""

    def _connection_state_name(self, room: rtc.Room | None = None) -> str:
        target = room or self.room
        if target is None:
            return "detached"
        try:
            return rtc.ConnectionState.Name(target.connection_state)
        except Exception:
            return str(getattr(target, "connection_state", "unknown"))

    def _build_snapshot_signature(self, snapshot: dict) -> str:
        return "|".join(
            [
                str(snapshot.get("roomName") or ""),
                str(snapshot.get("wsUrl") or ""),
                str(snapshot.get("token") or ""),
            ]
        )

    def _request_reconnect(self, reason: str) -> None:
        clean = " ".join(str(reason).strip().split()) or "reconnect_requested"
        if not self._reconnect_requested.is_set():
            print("[user_client] reconnect requested reason={}".format(clean))
            self._reconnect_reason = clean
        self._reconnect_requested.set()

    def _room_detail(self, extra: str = "") -> str:
        state_name = self._connection_state_name()
        parts = [
            "room={}".format(self._connected_room_name or "<none>"),
            "state={}".format(state_name),
            "identity={}".format(self._connected_identity or "<none>"),
            "mic={}".format("muted" if self.mic_muted else "live"),
            "frames={}".format(self._frames_sent),
        ]
        if self.room is not None:
            try:
                parts.append("remote={}".format(len(self.room.remote_participants)))
            except Exception:
                pass
        if extra:
            parts.append(extra)
        return " | ".join(parts)

    def _register_room_handlers(self, room: rtc.Room) -> None:
        @room.on("connected")
        def _on_connected() -> None:
            print("[user_client] room event connected")

        @room.on("connection_state_changed")
        def _on_connection_state_changed(connection_state) -> None:
            state_name = self._connection_state_name(room)
            print("[user_client] room state changed -> {}".format(state_name))
            if state_name == "CONN_DISCONNECTED":
                self._request_reconnect("livekit state disconnected")

        @room.on("reconnecting")
        def _on_reconnecting() -> None:
            print("[user_client] room event reconnecting")

        @room.on("reconnected")
        def _on_reconnected() -> None:
            print("[user_client] room event reconnected")

        @room.on("disconnected")
        def _on_disconnected(reason) -> None:
            print("[user_client] room event disconnected reason={}".format(reason))
            self._request_reconnect("livekit disconnected reason={}".format(reason))

        @room.on("data_received")
        def _on_data(packet) -> None:
            # Listen for runtime state broadcasts from the orchestrator.
            # The orchestrator is the single source of truth; we just reflect
            # the latest mic_muted it announces.
            if str(getattr(packet, "topic", "") or "") != "pepper.state":
                return
            try:
                msg = json.loads(getattr(packet, "data", b"") or b"")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return
            new_muted = bool(msg.get("mic_muted", False))
            if new_muted != self.mic_muted:
                self.mic_muted = new_muted
                print(f"[user_client] mic_muted={new_muted} (via pepper.state)")

    def _agent_ready_for_text(self) -> bool:
        if not self.room:
            return False
        remote_participants = getattr(self.room, "remote_participants", None) or {}
        values = remote_participants.values() if hasattr(remote_participants, "values") else remote_participants
        for participant in values:
            identity = str(getattr(participant, "identity", "") or "")
            kind = str(getattr(participant, "kind", "") or "")
            if identity.startswith("agent-") or "AGENT" in kind.upper():
                return True
        return False

    async def _report_component_status(
        self,
        state: str,
        detail: str,
        healthy: bool,
        force: bool = False,
    ) -> None:
        """Log component status locally (no longer POSTs to session manager)."""
        now = time.monotonic()
        if (
            not force
            and state == self._component_state
            and detail == self._component_detail
            and (now - self._last_component_status_monotonic) < 4.0
        ):
            return
        self._component_state = state
        self._component_detail = detail
        self._last_component_status_monotonic = now
        print(f"[user-client] {state} - {detail} (healthy={healthy})")

    def _reset_runtime_state(self) -> None:
        self.source = rtc.AudioSource(
            USER_MIC_SAMPLE_RATE,
            USER_MIC_CHANNELS,
            queue_size_ms=1500,
        )
        self.audio_queue = asyncio.Queue(maxsize=32)
        self.room = None
        self._last_activity_post_monotonic = 0.0
        self._frames_sent = 0
        self._last_audio_log_monotonic = 0.0
        self._peak_rms = 0.0
        self._last_level_post_monotonic = 0.0
        self._reconnect_requested = asyncio.Event()
        self._reconnect_reason = ""
        self._connected_room_name = ""
        self._connected_identity = ""

    def _resolve_sounddevice(self):
        import sounddevice as sd

        return sd

    async def _report_activity(self, level: float) -> None:
        now = time.monotonic()
        if now - self._last_activity_post_monotonic < SESSION_ACTIVITY_DEBOUNCE_SEC:
            return
        self._last_activity_post_monotonic = now
        print("[user_client] speech activity detected rms={:.4f}".format(level))

    async def _report_debug_event(self, event: str, **payload) -> None:
        details = " ".join(f"{k}={v}" for k, v in payload.items())
        print(f"[user_client] {event} {details}"[:200])

    async def _audio_sender_loop(self) -> None:
        while True:
            if self.audio_queue is None or self.source is None:
                await asyncio.sleep(0.1)
                continue
            frame_bytes, samples_per_channel, rms = await self.audio_queue.get()
            now = time.monotonic()
            if now - self._last_level_post_monotonic >= 0.25:
                self._last_level_post_monotonic = now
                await self._report_debug_event("mic_level", level=rms)
            if self.mic_muted:
                frame_bytes = bytes(len(frame_bytes))
                rms = 0.0
            await self.source.capture_frame(
                rtc.AudioFrame(
                    data=frame_bytes,
                    sample_rate=USER_MIC_SAMPLE_RATE,
                    num_channels=USER_MIC_CHANNELS,
                    samples_per_channel=samples_per_channel,
                )
            )
            self._frames_sent += 1
            self._peak_rms = max(self._peak_rms, rms)
            now = time.monotonic()
            if self._frames_sent == 1:
                print(
                    "[user_client] first audio frame sent samples={} rms={:.4f}".format(
                        samples_per_channel,
                        rms,
                    )
                )
                self._last_audio_log_monotonic = now
            elif now - self._last_audio_log_monotonic >= 5.0:
                queued_ms = self.source.queued_duration * 1000.0
                print(
                    "[user_client] audio heartbeat frames={} queue_ms={:.1f} last_rms={:.4f} peak_rms={:.4f}".format(
                        self._frames_sent,
                        queued_ms,
                        rms,
                        self._peak_rms,
                    )
                )
                self._last_audio_log_monotonic = now
                self._peak_rms = rms
            if rms >= USER_MIC_RMS_THRESHOLD:
                await self._report_activity(rms)

    async def _control_loop(self) -> None:
        # No longer polls session-manager for mic state or pending texts.
        # Kept as a no-op coroutine so the task structure stays intact.
        while True:
            await asyncio.sleep(60)

    def _log_devices(self, sd) -> None:
        try:
            devices = sd.query_devices()
            print("[user_client] available capture devices:")
            for idx, device in enumerate(devices):
                max_in = int(device.get("max_input_channels", 0) or 0)
                if max_in > 0:
                    print(
                        "[user_client]   idx={} name={} in={} out={}".format(
                            idx,
                            device.get("name", ""),
                            max_in,
                            int(device.get("max_output_channels", 0) or 0),
                        )
                    )
        except Exception as exc:
            print("[user_client] failed to query audio devices: {}".format(exc))

    async def connect(self) -> None:
        if self.source is None:
            self._reset_runtime_state()
        snapshot = await self._token_watcher.wait_for_initial_token()
        self._snapshot_signature = self._build_snapshot_signature(snapshot)
        await self._report_component_status(
            "connecting_livekit",
            "room={} | url={} | generatedAt={}".format(
                snapshot["roomName"],
                snapshot["wsUrl"],
                snapshot.get("generatedAt") or "?",
            ),
            healthy=False,
            force=True,
        )
        print(
            "[user_client] connecting to LiveKit room={} url={} as={}".format(
                snapshot["roomName"],
                snapshot["wsUrl"],
                snapshot["identity"],
            )
        )
        room = rtc.Room()
        self._register_room_handlers(room)
        connect_options = rtc.RoomOptions(auto_subscribe=False)
        print(
            "[user_client] connect options auto_subscribe={}".format(
                connect_options.auto_subscribe
            )
        )
        await room.connect(snapshot["wsUrl"], snapshot["token"], connect_options)
        local_identity = str(getattr(room.local_participant, "identity", "") or "")
        print(
            "[user_client] room.connect succeeded local_identity={}".format(
                local_identity or "<unknown>"
            )
        )
        publication = None
        if self.test_mode == "connect-only":
            print("[user_client] test mode connect-only: skipping track publish")
        else:
            print("[user_client] creating local audio track name=user-mic")
            local_track = rtc.LocalAudioTrack.create_audio_track("user-mic", self.source)
            print("[user_client] publishing local audio track")
            publish_options = rtc.TrackPublishOptions()
            publish_options.source = rtc.TrackSource.SOURCE_MICROPHONE
            publication = await room.local_participant.publish_track(
                local_track,
                publish_options,
            )
            print(
                "[user_client] publish_track succeeded sid={} source={}".format(
                    str(getattr(publication, "sid", "") or "")
                    ,
                    publish_options.source,
                )
            )
        self.room = room
        self._connected_room_name = snapshot["roomName"]
        self._connected_identity = snapshot["identity"]
        print(
            f"[user_client] connected room={snapshot['roomName']} "
            f"as={snapshot['identity']} track_sid={getattr(publication, 'sid', '') if publication else ''}"
        )
        await self._report_component_status(
            "ready_in_room",
            self._room_detail(
                "track_sid={}".format(getattr(publication, "sid", "") if publication else "")
            ),
            healthy=True,
            force=True,
        )

    async def _room_monitor_loop(self) -> None:
        _reconnecting_since: float | None = None
        RECONNECTING_TIMEOUT_SEC = 45.0
        while not self._reconnect_requested.is_set():
            await asyncio.sleep(1.0)
            latest_snapshot = self._token_watcher.latest_token_info()
            if latest_snapshot:
                latest_signature = self._build_snapshot_signature(latest_snapshot)
                if latest_signature != self._snapshot_signature:
                    self._request_reconnect(
                        "session snapshot changed generatedAt={}".format(
                            latest_snapshot.get("generatedAt") or "?"
                        )
                    )
                    continue
            room = self.room
            if room is None:
                self._request_reconnect("room handle missing")
                continue
            state_name = self._connection_state_name(room)
            if state_name == "CONN_CONNECTED":
                component_state = "streaming_audio" if self._frames_sent else "ready_in_room"
                healthy = True
                _reconnecting_since = None
            elif state_name == "CONN_RECONNECTING":
                component_state = "reconnecting_livekit"
                healthy = False
                if _reconnecting_since is None:
                    _reconnecting_since = time.monotonic()
                    print("[user_client] room entered CONN_RECONNECTING — starting timeout")
                elapsed = time.monotonic() - _reconnecting_since
                if elapsed >= RECONNECTING_TIMEOUT_SEC:
                    self._request_reconnect(
                        "stuck in CONN_RECONNECTING for {:.0f}s".format(elapsed)
                    )
                    continue
            elif state_name == "CONN_DISCONNECTED":
                component_state = "detached"
                healthy = False
                _reconnecting_since = None
            else:
                component_state = "connecting_livekit"
                healthy = False
                _reconnecting_since = None
            await self._report_component_status(
                component_state,
                self._room_detail(),
                healthy=healthy,
            )
            if not room.isconnected() or state_name == "CONN_DISCONNECTED":
                self._request_reconnect("room not connected")

    async def _run_once(self) -> None:
        sd = self._resolve_sounddevice()
        self._log_devices(sd)
        self._reset_runtime_state()
        await self.connect()
        sender_task = asyncio.create_task(self._audio_sender_loop())
        control_task = asyncio.create_task(self._control_loop())
        monitor_task = asyncio.create_task(self._room_monitor_loop())
        loop = asyncio.get_running_loop()

        def _callback(indata, frames, _time_info, status) -> None:
            if status:
                print(f"[user_client] input status={status}")
            mono = np.array(indata, copy=True).reshape(-1)
            rms = float(np.sqrt(np.mean(np.square(mono), dtype=np.float64)))
            pcm = np.clip(mono, -1.0, 1.0)
            frame_bytes = (pcm * 32767.0).astype(np.int16).tobytes()
            item = (frame_bytes, int(frames), rms)

            def _push() -> None:
                if self.audio_queue is None:
                    return
                if self.audio_queue.full():
                    try:
                        self.audio_queue.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                try:
                    self.audio_queue.put_nowait(item)
                except asyncio.QueueFull:
                    pass

            loop.call_soon_threadsafe(_push)

        stream = sd.InputStream(
            samplerate=USER_MIC_SAMPLE_RATE,
            blocksize=USER_MIC_BLOCKSIZE,
            device=USER_MIC_DEVICE,
            channels=USER_MIC_CHANNELS,
            dtype="float32",
            callback=_callback,
        )
        print("[user_client] sounddevice.InputStream created successfully")

        print(
            f"[user_client] starting microphone device={USER_MIC_DEVICE!r} "
            f"rate={USER_MIC_SAMPLE_RATE} blocksize={USER_MIC_BLOCKSIZE} "
            f"threshold={USER_MIC_RMS_THRESHOLD} test_mode={self.test_mode}"
        )
        try:
            if self.test_mode == "connect-only":
                print("[user_client] connect-only mode active; keeping room open without microphone")
                while not self._reconnect_requested.is_set():
                    await self._report_component_status(
                        "ready_in_room",
                        self._room_detail("connect-only"),
                        healthy=True,
                    )
                    await asyncio.sleep(1)
                return
            print("[user_client] entering microphone stream context")
            with stream:
                print("[user_client] microphone stream active")
                while not self._reconnect_requested.is_set():
                    await asyncio.sleep(1)
        finally:
            print("[user_client] shutting down user client")
            sender_task.cancel()
            control_task.cancel()
            monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender_task
            with contextlib.suppress(asyncio.CancelledError):
                await control_task
            with contextlib.suppress(asyncio.CancelledError):
                await monitor_task
            if self.room:
                print("[user_client] disconnecting room")
                await self.room.disconnect()
                self.room = None
            if self.audio_queue is not None:
                self.audio_queue = None
            self.source = None

    async def run(self) -> None:
        await self._report_component_status("starting", "user client booting", healthy=False, force=True)
        while True:
            try:
                await self._report_component_status(
                    "waiting_for_session",
                    "awaiting token snapshot",
                    healthy=False,
                )
                await self._run_once()
                reason = self._reconnect_reason or "connection refresh"
                await self._report_component_status(
                    "reconnecting",
                    reason,
                    healthy=False,
                    force=True,
                )
                await asyncio.sleep(1)
            except Exception as exc:
                print(f"[user_client] service loop error={exc!r} - retrying in 3s")
                await self._report_component_status(
                    "degraded",
                    str(exc),
                    healthy=False,
                    force=True,
                )
                await asyncio.sleep(3)


async def main() -> None:
    client = UserAudioClient()
    try:
        await client.run()
    finally:
        await client._report_component_status("stopping", "user client stopped", healthy=False, force=True)


if __name__ == "__main__":
    asyncio.run(main())
