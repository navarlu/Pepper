"""Text-chat REPL for the paper realtime agent.

Connects to the `pepper-experiment` LiveKit room as `debug-cli`, streams
agent events to stdout, and lets you drive the conversation from the
keyboard — useful when running `agent_realtime.py dev` locally without
a live mic.

Commands:
    /mute           soft-mute the user-client mic (publishes on pepper.state)
    /unmute         unmute the user-client mic
    /quit  /exit    disconnect and exit
    <anything>      publish as a typed user turn (topic pepper.text)

Run (from project root, with the paper stack up and realtime-agent
running either in compose OR locally via `agent_realtime.py dev`):

    uv run python voice-agent/src/paper/chat_client.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

from livekit import rtc

# ── Config (simple globals per CLAUDE.md) ────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parents[3]
TOKEN_FILE = PROJECT_ROOT / "services" / "data" / "token-latest.json"
TOKEN_KEY = "debugCli"

TOPIC_TEXT = "pepper.text"
TOPIC_STATE = "pepper.state"
TOPIC_EXPERIMENT = "pepper.experiment"
TOPIC_SPEECH = "pepper.speech"

SUBSCRIBE_TOPICS = {TOPIC_EXPERIMENT, TOPIC_SPEECH, TOPIC_STATE}


def _load_token() -> tuple[str, str, str]:
    if not TOKEN_FILE.exists():
        raise SystemExit(f"[chat_client] token file not found: {TOKEN_FILE}")
    data = json.loads(TOKEN_FILE.read_text())
    entry = data.get(TOKEN_KEY)
    if not entry:
        raise SystemExit(f"[chat_client] token file has no '{TOKEN_KEY}' entry")
    return data["wsUrl"], entry["identity"], entry["token"]


def _fmt_event(topic: str, payload: dict) -> str:
    kind = payload.get("kind", "?")
    if topic == TOPIC_EXPERIMENT and kind == "agent_speech":
        return f"[AGENT] {payload.get('text', '')!r}"
    if topic == TOPIC_EXPERIMENT and kind == "user_turn":
        src = payload.get("input", "?")
        return f"[USER:{src}] {payload.get('text', '')!r}"
    if topic == TOPIC_EXPERIMENT and kind == "tool_call":
        return f"[TOOL_CALL] {payload.get('name', '?')}({payload.get('args', {})})"
    if topic == TOPIC_EXPERIMENT and kind == "tool_result":
        return f"[TOOL_RESULT] {payload.get('name', '?')} -> {payload.get('result', '')!r}"
    if topic == TOPIC_EXPERIMENT and kind == "tool_failed":
        return f"[TOOL_FAILED] {payload.get('name', '?')} err={payload.get('error', '')!r}"
    if topic == TOPIC_EXPERIMENT and kind == "typed_input":
        return f"[TYPED_ECHO] {payload.get('text', '')!r} by={payload.get('by', '?')}"
    return f"[{topic}:{kind}] {payload}"


async def _stdin_loop(room: rtc.Room, stop: asyncio.Event) -> None:
    loop = asyncio.get_running_loop()
    print("[chat_client] type a message + Enter, or /mute /unmute /quit", flush=True)
    while not stop.is_set():
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            print("[chat_client] stdin closed — exiting", flush=True)
            stop.set()
            return
        line = line.rstrip("\n")
        if not line:
            continue
        cmd = line.strip().lower()
        if cmd in ("/quit", "/exit"):
            stop.set()
            return
        if cmd in ("/mute", "/unmute"):
            muted = cmd == "/mute"
            payload = json.dumps({"mic_muted": muted}).encode("utf-8")
            await room.local_participant.publish_data(payload, topic=TOPIC_STATE)
            print(f"[chat_client] published pepper.state mic_muted={muted}", flush=True)
            continue
        payload = json.dumps({"text": line, "ts": time.time()}).encode("utf-8")
        await room.local_participant.publish_data(payload, topic=TOPIC_TEXT)
        print(f"[chat_client] sent pepper.text {line!r}", flush=True)


async def main() -> None:
    ws_url, identity, token = _load_token()
    print(f"[chat_client] connecting url={ws_url} as={identity}", flush=True)

    room = rtc.Room()
    stop = asyncio.Event()

    @room.on("data_received")
    def _on_data(packet):
        topic = str(getattr(packet, "topic", "") or "")
        if topic not in SUBSCRIBE_TOPICS:
            return
        try:
            payload = json.loads(getattr(packet, "data", b"") or b"")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        print(_fmt_event(topic, payload), flush=True)

    @room.on("disconnected")
    def _on_disconnect(reason=None):
        print(f"[chat_client] room disconnected reason={reason}", flush=True)
        stop.set()

    await room.connect(ws_url, token, rtc.RoomOptions(auto_subscribe=False))
    print(f"[chat_client] connected room={room.name}", flush=True)

    try:
        await _stdin_loop(room, stop)
    finally:
        print("[chat_client] disconnecting…", flush=True)
        await room.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n[chat_client] interrupted", flush=True)
