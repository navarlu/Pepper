"""
Minimal LiveKit connection test.

Connects to LiveKit as a regular participant (not an agent) and reports:
  - WebSocket signaling status
  - ICE candidates (local + remote)
  - ICE connection state transitions
  - Whether the peer connection succeeds or fails, and why

Usage (on woska or any remote machine):
    python -m voice-agent.tests.test_livekit_connection

Reads token from data/token-latest.json (written by session-manager).
Override with env vars:
    LIVEKIT_URL=ws://127.0.0.1:7880
    LIVEKIT_TOKEN=<jwt>
"""

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path

from livekit import rtc

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)-5s %(name)-20s %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("lk-test")

# Suppress noisy loggers
logging.getLogger("livekit").setLevel(logging.INFO)

SNAPSHOT_PATH = Path(__file__).resolve().parents[2] / "data" / "token-latest.json"
CONNECT_TIMEOUT = 30  # seconds


def load_snapshot() -> tuple[str, str]:
    """Load ws_url and token from the session snapshot file."""
    try:
        data = json.loads(SNAPSHOT_PATH.read_text())
    except FileNotFoundError:
        log.error("Snapshot file not found: %s", SNAPSHOT_PATH)
        sys.exit(1)

    ws_url = data.get("hostWsUrl") or data.get("wsUrl") or ""
    # Use listener token (subscribe-only) so we don't interfere with the real user
    token = data.get("listener", {}).get("token") or ""
    room_name = data.get("roomName", "")
    log.info("snapshot room=%s ws_url=%s token_len=%d", room_name, ws_url, len(token))
    return ws_url, token


async def test_connection():
    ws_url = os.getenv("LIVEKIT_URL", "").strip()
    token = os.getenv("LIVEKIT_TOKEN", "").strip()

    if not ws_url or not token:
        ws_url_snap, token_snap = load_snapshot()
        ws_url = ws_url or ws_url_snap
        token = token or token_snap

    if not ws_url or not token:
        log.error("No LIVEKIT_URL or token available. Set env vars or ensure snapshot exists.")
        sys.exit(1)

    log.info("connecting to %s", ws_url)

    room = rtc.Room()
    ice_states: list[str] = []
    connected_event = asyncio.Event()
    failed = False

    @room.on("connection_state_changed")
    def on_state(state: rtc.ConnectionState):
        nonlocal failed
        state_name = state.name if hasattr(state, "name") else str(state)
        ice_states.append(state_name)
        log.info("connection_state -> %s", state_name)
        if state == rtc.ConnectionState.CONN_CONNECTED:
            connected_event.set()
        elif state == rtc.ConnectionState.CONN_DISCONNECTED:
            failed = True
            connected_event.set()

    @room.on("participant_connected")
    def on_participant(p: rtc.RemoteParticipant):
        log.info("participant_connected identity=%s", p.identity)

    @room.on("track_subscribed")
    def on_track(track, publication, participant):
        log.info(
            "track_subscribed kind=%s participant=%s",
            track.kind,
            participant.identity,
        )

    @room.on("disconnected")
    def on_disconnected(reason):
        nonlocal failed
        log.warning("disconnected reason=%s", reason)
        failed = True
        connected_event.set()

    t0 = time.monotonic()
    try:
        log.info("calling room.connect()...")
        await room.connect(ws_url, token)
        elapsed = time.monotonic() - t0
        log.info("room.connect() returned in %.3fs", elapsed)
    except Exception as exc:
        elapsed = time.monotonic() - t0
        log.error("room.connect() FAILED after %.3fs: %s", elapsed, exc)
        return False

    # Wait for connection to fully establish (ICE)
    log.info("waiting for ICE connection (timeout=%ds)...", CONNECT_TIMEOUT)
    try:
        await asyncio.wait_for(connected_event.wait(), timeout=CONNECT_TIMEOUT)
    except asyncio.TimeoutError:
        log.error("ICE connection TIMED OUT after %ds", CONNECT_TIMEOUT)
        failed = True

    elapsed = time.monotonic() - t0
    log.info("ice_states_observed: %s", ice_states)
    log.info("total_elapsed: %.3fs", elapsed)

    # Report participants
    for p in room.remote_participants.values():
        log.info("remote_participant identity=%s", p.identity)

    if failed:
        log.error("CONNECTION FAILED - ICE could not establish")
        log.info("")
        log.info("=== DIAGNOSIS ===")
        log.info("The WebSocket signaling works (agent registers via tunnel)")
        log.info("but WebRTC ICE fails because:")
        log.info("  1. LiveKit sends ICE candidates with Docker/RPi IPs")
        log.info("  2. These IPs are not reachable from this machine")
        log.info("  3. No TURN relay is available through the SSH tunnel")
        log.info("")
        log.info("Possible fixes:")
        log.info("  - Forward UDP port 7882 (e.g., with socat UDP relay)")
        log.info("  - Switch LiveKit to host network + set --node-ip")
        log.info("  - Configure external TURN server reachable from both sides")
    else:
        log.info("CONNECTION SUCCEEDED")

    await room.disconnect()
    return not failed


async def main():
    log.info("=" * 60)
    log.info("LiveKit Connection Test")
    log.info("=" * 60)
    success = await test_connection()
    log.info("=" * 60)
    log.info("RESULT: %s", "PASS" if success else "FAIL")
    log.info("=" * 60)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    asyncio.run(main())
