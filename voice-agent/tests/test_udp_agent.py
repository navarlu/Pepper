"""
Standalone test: verify that a LiveKit agent connects over UDP and that
audio frames actually flow bidirectionally.

Two roles:
  - Agent (woska): livekit-agents worker that publishes a 440 Hz sine wave
    and counts audio frames received from the user participant.
  - User (RPi): raw livekit-rtc participant that publishes a 880 Hz sine wave
    and counts audio frames received from the agent.

Both sides log frame counts every 5s for 60s. If frames are received,
audio is flowing. Combined with LiveKit server logs showing
connectionType=udp, this proves end-to-end UDP media transport.

== Prerequisites ==

    pip install livekit livekit-agents livekit-api python-dotenv numpy

    Environment variables (or .env file):
        LIVEKIT_URL        — LiveKit WS URL    (default: ws://127.0.0.1:7880)
        LIVEKIT_API_KEY    — LiveKit API key    (default: devkey)
        LIVEKIT_API_SECRET — LiveKit secret     (default: secretsecretsecretsecretsecretsecret)

== Step-by-step ==

  RPi terminal 1 — start test LiveKit:
    docker compose -f docker/docker-compose.yml down
    docker compose -f docker/docker-compose.yml up -d redis
    docker run --rm --network host --name livekit-test \
      -v $(pwd)/docker/livekit:/livekit:ro \
      livekit/livekit-server:v1.9.11 \
      --config=/livekit/livekit-test.yaml \
      --keys="devkey: secretsecretsecretsecretsecretsecret"

  RPi terminal 2 — start reverse tunnel (if not already running):
    ssh -R 7880:127.0.0.1:7880 -J navarlu2@ptak.felk.cvut.cz navarlu2@horn -N

  Woska — start the agent (keep running):
    LIVEKIT_URL=ws://127.0.0.1:7880 \
    LIVEKIT_API_KEY=devkey \
    LIVEKIT_API_SECRET=secretsecretsecretsecretsecretsecret \
    python -m voice-agent.tests.test_udp_agent dev

  RPi terminal 3 — dispatch + join as user:
    LIVEKIT_API_KEY=devkey \
    LIVEKIT_API_SECRET=secretsecretsecretsecretsecretsecret \
    uv run python -m voice-agent.tests.test_udp_agent --user

  Wait 60s. Both sides print frame counts. Look for:
    - Agent (woska): "FRAMES RECEIVED FROM USER: XXXX" (> 0 = audio flowing)
    - User (RPi): "FRAMES RECEIVED FROM AGENT: XXXX" (> 0 = audio flowing)
    - LiveKit server (RPi terminal 1): "connectionType": "udp"

== Cleanup ==

    docker stop livekit-test
    docker compose -f docker/docker-compose.yml up -d
"""

import asyncio
import logging
import math
import os
import struct
import subprocess
import sys
import datetime

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(name)-20s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("udp-test")
logging.getLogger("livekit").setLevel(logging.WARNING)

LIVEKIT_URL = os.getenv("LIVEKIT_URL", "ws://127.0.0.1:7880")
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY", "devkey")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET", "secretsecretsecretsecretsecretsecret")
ROOM_NAME = "udp-test-room"
AGENT_NAME = "udp-test-agent"

SAMPLE_RATE = 48000
NUM_CHANNELS = 1
FRAME_DURATION_MS = 10  # 10ms frames = 100fps
SAMPLES_PER_FRAME = SAMPLE_RATE * FRAME_DURATION_MS // 1000  # 480

RPI_LAN_IP = "192.168.210.78"
RPI_RTC_PORT = 7882
TEST_DURATION_SEC = 60


# ---------------------------------------------------------------------------
# Synthetic audio generation
# ---------------------------------------------------------------------------

def generate_sine_frame(freq_hz: float, frame_index: int) -> bytes:
    """Generate a single 10ms audio frame of a sine wave (16-bit PCM)."""
    samples = []
    for i in range(SAMPLES_PER_FRAME):
        t = (frame_index * SAMPLES_PER_FRAME + i) / SAMPLE_RATE
        sample = int(16000 * math.sin(2 * math.pi * freq_hz * t))
        sample = max(-32768, min(32767, sample))
        samples.append(sample)
    return struct.pack(f"<{len(samples)}h", *samples)


# ---------------------------------------------------------------------------
# UDP socket check
# ---------------------------------------------------------------------------

def check_udp_sockets() -> list[str]:
    """Check for UDP sockets, return lines mentioning the RPi IP."""
    try:
        out = subprocess.check_output(["ss", "-unp"], text=True, timeout=5)
        return [line.strip() for line in out.splitlines() if RPI_LAN_IP in line]
    except Exception:
        try:
            out = subprocess.check_output(
                ["netstat", "-unp"], text=True, timeout=5, stderr=subprocess.DEVNULL
            )
            return [line.strip() for line in out.splitlines() if RPI_LAN_IP in line]
        except Exception:
            return []


# ---------------------------------------------------------------------------
# Audio frame counter (used by both agent and user)
# ---------------------------------------------------------------------------

async def count_incoming_frames(track, label: str, duration: int) -> int:
    """Subscribe to an audio track and count frames for `duration` seconds."""
    from livekit import rtc

    frame_count = 0
    stream = rtc.AudioStream(track)
    deadline = asyncio.get_event_loop().time() + duration

    log.info("listening for audio from %s for %ds...", label, duration)

    async for event in stream:
        frame_count += 1
        now = asyncio.get_event_loop().time()
        if frame_count % 500 == 0:  # log every ~5s at 100fps
            remaining = max(0, deadline - now)
            log.info("  %s: %d frames received (%.0fs remaining)", label, frame_count, remaining)
        if now >= deadline:
            break

    return frame_count


# ---------------------------------------------------------------------------
# Audio publisher (used by both agent and user)
# ---------------------------------------------------------------------------

async def publish_sine(room, freq_hz: float, duration: int):
    """Publish a sine wave to the room for `duration` seconds."""
    from livekit import rtc

    source = rtc.AudioSource(SAMPLE_RATE, NUM_CHANNELS)
    track = rtc.LocalAudioTrack.create_audio_track("test-tone", source)
    options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    await room.local_participant.publish_track(track, options)
    log.info("publishing %d Hz sine wave for %ds", freq_hz, duration)

    frame_index = 0
    end_time = asyncio.get_event_loop().time() + duration
    while asyncio.get_event_loop().time() < end_time:
        pcm = generate_sine_frame(freq_hz, frame_index)
        frame = rtc.AudioFrame(
            data=pcm,
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
            samples_per_channel=SAMPLES_PER_FRAME,
        )
        await source.capture_frame(frame)
        frame_index += 1
        await asyncio.sleep(FRAME_DURATION_MS / 1000)

    log.info("stopped publishing after %d frames", frame_index)


# ---------------------------------------------------------------------------
# Agent entrypoint (runs on woska)
# ---------------------------------------------------------------------------

async def entrypoint(ctx):
    from livekit.agents import AutoSubscribe
    from livekit import rtc

    log.info("=" * 60)
    log.info("UDP TEST AGENT — connected to room: %s", ctx.room.name)
    log.info("=" * 60)

    await ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_ALL)
    log.info("connected, waiting for user to join...")

    # Wait for a user participant with an audio track
    rx_task = None
    rx_count = 0

    @ctx.room.on("track_subscribed")
    def on_track(track: rtc.Track, publication: rtc.RemoteTrackPublication,
                 participant: rtc.RemoteParticipant):
        nonlocal rx_task
        if track.kind == rtc.TrackKind.KIND_AUDIO and rx_task is None:
            log.info("subscribed to audio from %s", participant.identity)
            rx_task = asyncio.create_task(
                count_incoming_frames(track, participant.identity, TEST_DURATION_SEC)
            )

    # Start publishing sine wave immediately
    tx_task = asyncio.create_task(publish_sine(ctx.room, 440.0, TEST_DURATION_SEC + 10))

    # Wait for user to join (up to 120s)
    for i in range(120):
        if rx_task is not None:
            break
        await asyncio.sleep(1)
        if i % 10 == 9:
            log.info("still waiting for user... (%ds)", i + 1)
    else:
        log.warning("no user joined within 120s")
        tx_task.cancel()
        return

    # Check UDP sockets while audio is flowing
    await asyncio.sleep(3)
    udp_lines = check_udp_sockets()
    log.info("=" * 60)
    log.info("UDP SOCKET CHECK (ss -unp | grep %s):", RPI_LAN_IP)
    if udp_lines:
        for line in udp_lines:
            log.info("  %s", line)
        has_rtc_port = any(str(RPI_RTC_PORT) in line for line in udp_lines)
        log.info("  UDP to %s:%s: %s",
                 RPI_LAN_IP, RPI_RTC_PORT,
                 "CONFIRMED" if has_rtc_port else "not on expected port")
    else:
        log.warning("  no UDP sockets to %s found", RPI_LAN_IP)
    log.info("=" * 60)

    # Wait for rx to finish
    rx_count = await rx_task
    tx_task.cancel()

    log.info("=" * 60)
    log.info("RESULT: FRAMES RECEIVED FROM USER: %d", rx_count)
    if rx_count > 0:
        log.info("  AUDIO IS FLOWING (%d frames = ~%.1fs of audio)",
                 rx_count, rx_count * FRAME_DURATION_MS / 1000)
    else:
        log.warning("  NO AUDIO RECEIVED — media may not be flowing")
    log.info("=" * 60)

    # Keep alive a bit for clean shutdown
    await asyncio.sleep(2)
    log.info("agent test complete")


# ---------------------------------------------------------------------------
# User mode (runs on RPi) — dispatch + join + publish + count
# ---------------------------------------------------------------------------

async def run_as_user():
    """Dispatch agent, join room as user, publish audio, count received frames."""
    from livekit import api, rtc
    from livekit.protocol.room import CreateRoomRequest
    from livekit.protocol.agent_dispatch import CreateAgentDispatchRequest

    http_url = LIVEKIT_URL.replace("ws://", "http://").replace("wss://", "https://")
    lk = api.LiveKitAPI(url=http_url, api_key=LIVEKIT_API_KEY, api_secret=LIVEKIT_API_SECRET)

    # Create room + dispatch agent
    room_info = await lk.room.create_room(CreateRoomRequest(name=ROOM_NAME))
    log.info("created room: %s (sid=%s)", room_info.name, room_info.sid)

    dispatch_req = CreateAgentDispatchRequest(agent_name=AGENT_NAME, room=ROOM_NAME)
    result = await lk.agent_dispatch.create_dispatch(dispatch_req)
    log.info("dispatched agent: %s (id=%s)", AGENT_NAME, result.id)

    # Generate user token
    token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity("test-user")
        .with_grants(api.VideoGrants(
            room_join=True, room=ROOM_NAME,
            can_publish=True, can_subscribe=True,
        ))
        .with_ttl(datetime.timedelta(hours=1))
        .to_jwt()
    )

    # Connect as user
    room = rtc.Room()
    rx_task = None

    @room.on("track_subscribed")
    def on_track(track: rtc.Track, publication: rtc.RemoteTrackPublication,
                 participant: rtc.RemoteParticipant):
        nonlocal rx_task
        if track.kind == rtc.TrackKind.KIND_AUDIO and rx_task is None:
            log.info("subscribed to audio from %s", participant.identity)
            rx_task = asyncio.create_task(
                count_incoming_frames(track, participant.identity, TEST_DURATION_SEC)
            )

    log.info("connecting as user to %s ...", LIVEKIT_URL)
    await room.connect(LIVEKIT_URL, token)
    log.info("user connected to room")

    # Publish audio
    tx_task = asyncio.create_task(publish_sine(room, 880.0, TEST_DURATION_SEC + 10))

    # Wait for agent's audio
    for i in range(60):
        if rx_task is not None:
            break
        await asyncio.sleep(1)
        if i % 10 == 9:
            log.info("waiting for agent audio... (%ds)", i + 1)
    else:
        log.warning("no audio from agent within 60s")
        tx_task.cancel()
        await room.disconnect()
        await lk.aclose()
        return

    rx_count = await rx_task
    tx_task.cancel()

    log.info("=" * 60)
    log.info("RESULT: FRAMES RECEIVED FROM AGENT: %d", rx_count)
    if rx_count > 0:
        log.info("  AUDIO IS FLOWING (%d frames = ~%.1fs of audio)",
                 rx_count, rx_count * FRAME_DURATION_MS / 1000)
    else:
        log.warning("  NO AUDIO RECEIVED — media may not be flowing")
    log.info("=" * 60)

    await room.disconnect()
    await lk.aclose()
    log.info("user test complete")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    if "--user" in sys.argv:
        asyncio.run(run_as_user())
        return

    if "--dispatch" in sys.argv:
        # Keep backward compat — dispatch-only mode
        async def dispatch_only():
            from livekit import api
            from livekit.protocol.room import CreateRoomRequest
            from livekit.protocol.agent_dispatch import CreateAgentDispatchRequest

            http_url = LIVEKIT_URL.replace("ws://", "http://").replace("wss://", "https://")
            lk = api.LiveKitAPI(url=http_url, api_key=LIVEKIT_API_KEY, api_secret=LIVEKIT_API_SECRET)
            room = await lk.room.create_room(CreateRoomRequest(name=ROOM_NAME))
            log.info("created room: %s", room.name)
            dispatch_req = CreateAgentDispatchRequest(agent_name=AGENT_NAME, room=ROOM_NAME)
            result = await lk.agent_dispatch.create_dispatch(dispatch_req)
            log.info("dispatched: %s", result.id)
            await lk.aclose()

        asyncio.run(dispatch_only())
        return

    from livekit.agents import cli, WorkerOptions

    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            agent_name=AGENT_NAME,
            num_idle_processes=1,
        ),
    )


if __name__ == "__main__":
    main()
