import asyncio
import contextlib
import json
import os
import time
from pathlib import Path
from typing import Optional

import aiohttp
import numpy as np
from dotenv import load_dotenv
from livekit import rtc

try:
    from .config import (
        LIVEKIT_SESSION_FILE,
        SESSION_ACTIVITY_DEBOUNCE_SEC,
        SESSION_MANAGER_URL,
        USER_IDENTITY,
        USER_CLIENT_TEST_MODE,
        USER_MIC_BLOCKSIZE,
        USER_MIC_CHANNELS,
        USER_MIC_DEVICE,
        USER_MIC_RMS_THRESHOLD,
        USER_MIC_SAMPLE_RATE,
    )
except ImportError:
    from config import (
        LIVEKIT_SESSION_FILE,
        SESSION_ACTIVITY_DEBOUNCE_SEC,
        SESSION_MANAGER_URL,
        USER_IDENTITY,
        USER_CLIENT_TEST_MODE,
        USER_MIC_BLOCKSIZE,
        USER_MIC_CHANNELS,
        USER_MIC_DEVICE,
        USER_MIC_RMS_THRESHOLD,
        USER_MIC_SAMPLE_RATE,
    )

ROOT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
SESSION_FILE = Path(LIVEKIT_SESSION_FILE)
TOPIC_CHAT = "lk.chat"


def _load_root_env() -> None:
    if ROOT_ENV_PATH.exists():
        load_dotenv(dotenv_path=ROOT_ENV_PATH, override=True)


class SessionSnapshot:
    @staticmethod
    async def wait_for_user_snapshot() -> dict:
        missing_logged = False
        while True:
            try:
                payload = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
            except FileNotFoundError:
                if not missing_logged:
                    print("[user_client] waiting for session snapshot {}".format(SESSION_FILE))
                    missing_logged = True
                await asyncio.sleep(0.5)
                continue
            except json.JSONDecodeError:
                print("[user_client] session snapshot invalid JSON, waiting for rewrite")
                await asyncio.sleep(0.5)
                continue
            user = payload.get("user") or {}
            token = str(user.get("token") or "").strip()
            ws_url = str(payload.get("wsUrl") or "").strip()
            room_name = str(payload.get("roomName") or "").strip()
            if token and ws_url and room_name:
                print(
                    "[user_client] session snapshot ready room={} wsUrl={} identity={}".format(
                        room_name,
                        ws_url,
                        str(user.get("identity") or USER_IDENTITY),
                    )
                )
                return {
                    "token": token,
                    "wsUrl": ws_url,
                    "roomName": room_name,
                    "identity": str(user.get("identity") or USER_IDENTITY),
                }
            await asyncio.sleep(0.5)


class UserAudioClient:
    def __init__(self) -> None:
        _load_root_env()
        self.source = rtc.AudioSource(
            USER_MIC_SAMPLE_RATE,
            USER_MIC_CHANNELS,
            queue_size_ms=1500,
        )
        self.room: Optional[rtc.Room] = None
        self.http = aiohttp.ClientSession()
        self.audio_queue: asyncio.Queue[tuple[bytes, int, float]] = asyncio.Queue(maxsize=32)
        self._last_activity_post_monotonic = 0.0
        self._frames_sent = 0
        self._last_audio_log_monotonic = 0.0
        self._peak_rms = 0.0
        self._last_level_post_monotonic = 0.0
        self.test_mode = str(USER_CLIENT_TEST_MODE or "publish").strip().lower()
        self.mic_muted = False

    def _resolve_sounddevice(self):
        import sounddevice as sd

        return sd

    async def _report_activity(self, level: float) -> None:
        now = time.monotonic()
        if now - self._last_activity_post_monotonic < SESSION_ACTIVITY_DEBOUNCE_SEC:
            return
        self._last_activity_post_monotonic = now
        print("[user_client] speech activity detected rms={:.4f}".format(level))
        try:
            async with self.http.post(
                f"{SESSION_MANAGER_URL}/api/activity",
                json={"source": "user", "level": level},
                timeout=aiohttp.ClientTimeout(total=1.0),
            ) as resp:
                await resp.read()
                print("[user_client] activity POST status={}".format(resp.status))
        except Exception:
            print("[user_client] activity POST failed")

    async def _report_debug_event(self, event: str, **payload) -> None:
        body = {"event": event}
        body.update(payload)
        try:
            async with self.http.post(
                f"{SESSION_MANAGER_URL}/api/debug-event",
                json=body,
                timeout=aiohttp.ClientTimeout(total=1.0),
            ) as resp:
                await resp.read()
        except Exception:
            pass

    async def _audio_sender_loop(self) -> None:
        while True:
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
        while True:
            try:
                async with self.http.get(
                    f"{SESSION_MANAGER_URL}/api/user-client/state",
                    timeout=aiohttp.ClientTimeout(total=1.0),
                ) as resp:
                    data = await resp.json()
                self.mic_muted = bool(data.get("mic_muted"))
                for item in data.get("pending_texts", []) or []:
                    text = " ".join(str(item.get("text") or "").strip().split())
                    command_id = str(item.get("id") or "").strip()
                    if not text or not command_id or not self.room:
                        continue
                    print("[user_client] sending text input via room topic={} text={}".format(TOPIC_CHAT, text))
                    await self.room.local_participant.send_text(text, topic=TOPIC_CHAT)
                    await self._report_debug_event("transcript", speaker="User", text=text)
                    async with self.http.post(
                        f"{SESSION_MANAGER_URL}/api/user-client/ack",
                        json={"id": command_id},
                        timeout=aiohttp.ClientTimeout(total=1.0),
                    ) as resp:
                        await resp.read()
            except Exception:
                pass
            await asyncio.sleep(0.5)

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
        snapshot = await SessionSnapshot.wait_for_user_snapshot()
        print(
            "[user_client] connecting to LiveKit room={} url={} as={}".format(
                snapshot["roomName"],
                snapshot["wsUrl"],
                snapshot["identity"],
            )
        )
        room = rtc.Room()
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
        print(
            f"[user_client] connected room={snapshot['roomName']} "
            f"as={snapshot['identity']} track_sid={getattr(publication, 'sid', '') if publication else ''}"
        )

    async def run(self) -> None:
        sd = self._resolve_sounddevice()
        self._log_devices(sd)
        await self.connect()
        sender_task = asyncio.create_task(self._audio_sender_loop())
        control_task = asyncio.create_task(self._control_loop())
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
                while True:
                    await asyncio.sleep(1)
            print("[user_client] entering microphone stream context")
            with stream:
                print("[user_client] microphone stream active")
                while True:
                    await asyncio.sleep(1)
        finally:
            print("[user_client] shutting down user client")
            sender_task.cancel()
            control_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sender_task
            with contextlib.suppress(asyncio.CancelledError):
                await control_task
            if self.room:
                print("[user_client] disconnecting room")
                await self.room.disconnect()
            print("[user_client] closing http session")
            await self.http.close()


async def main() -> None:
    client = UserAudioClient()
    await client.run()


if __name__ == "__main__":
    asyncio.run(main())
