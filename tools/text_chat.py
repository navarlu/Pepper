"""
Text Chat CLI — send text messages to the voice-agent via LiveKit room.

Generates its own token with a unique identity (text-cli) so it doesn't
conflict with the user-client mic publisher. Joins the room and sends
text on the `lk.chat` topic so the agent receives it through its
text_input handler.

Usage:
    uv run python tools/text_chat.py
    uv run python tools/text_chat.py --mode local   # switch orchestrator to local mode
    uv run python tools/text_chat.py --mode openai   # switch orchestrator to openai mode
"""

import asyncio
import datetime
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from livekit import api, rtc

# ── Config ──

ROOT_DIR = Path(__file__).resolve().parents[1]
ENV_PATH = ROOT_DIR / ".env"
TOKEN_FILE = ROOT_DIR / "services" / "src" / "session_manager" / "data" / "token-latest.json"
CONFIG_FILE = ROOT_DIR / "services" / "src" / "orchestrator_config.json"
TOPIC_CHAT = "lk.chat"
CLI_IDENTITY = "text-cli"


def _load_env():
    if ENV_PATH.exists():
        load_dotenv(dotenv_path=ENV_PATH, override=True)

_load_env()


def _read_token_file() -> dict:
    try:
        data = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        print(f"Token file not found: {TOKEN_FILE}")
        print("Make sure the orchestrator is running.")
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"Invalid JSON in token file: {TOKEN_FILE}")
        sys.exit(1)
    return data


def _generate_token(room_name: str) -> str:
    """Generate a fresh token for the text-cli identity."""
    api_key = os.getenv("LIVEKIT_API_KEY", "").strip()
    api_secret = os.getenv("LIVEKIT_API_SECRET", "").strip()
    if not api_key or not api_secret:
        print("Missing LIVEKIT_API_KEY or LIVEKIT_API_SECRET in .env")
        sys.exit(1)
    return (
        api.AccessToken(api_key, api_secret)
        .with_ttl(datetime.timedelta(hours=12))
        .with_identity(CLI_IDENTITY)
        .with_name(CLI_IDENTITY)
        .with_grants(api.VideoGrants(
            room_join=True,
            room=room_name,
            can_publish=False,
            can_subscribe=True,
            can_publish_data=True,
        ))
        .to_jwt()
    )


def _switch_mode(mode: str):
    """Write new mode to orchestrator config file."""
    if mode not in ("openai", "local"):
        print(f"Invalid mode '{mode}'. Use 'openai' or 'local'.")
        sys.exit(1)
    CONFIG_FILE.write_text(json.dumps({"agent_mode": mode}, indent=2), encoding="utf-8")
    print(f"Switched orchestrator to '{mode}' mode.")
    print(f"The orchestrator will detect the change within a few seconds.")


async def main():
    # Handle --mode flag
    args = sys.argv[1:]
    if "--mode" in args:
        idx = args.index("--mode")
        if idx + 1 >= len(args):
            print("Usage: --mode <openai|local>")
            sys.exit(1)
        _switch_mode(args[idx + 1])
        return

    snapshot = _read_token_file()
    room_name = snapshot.get("roomName", "")
    ws_url = snapshot.get("hostWsUrl") or snapshot.get("wsUrl") or ""

    if not room_name or not ws_url:
        print("Token file missing roomName or wsUrl")
        sys.exit(1)

    token = _generate_token(room_name)
    print(f"Connecting to room '{room_name}' at {ws_url}...")

    room = rtc.Room()

    # Print agent responses
    @room.on("transcription_received")
    def _on_transcription(participant, segments):
        identity = str(getattr(participant, "identity", "") or "")
        for seg in segments:
            text = str(getattr(seg, "text", "") or "").strip()
            is_final = getattr(seg, "final", True)
            if text and is_final:
                print(f"\n  [{identity}]: {text}")
                print("You> ", end="", flush=True)

    @room.on("data_received")
    def _on_data(packet):
        topic = str(getattr(packet, "topic", "") or "")
        participant = getattr(packet, "participant", None)
        identity = str(getattr(participant, "identity", "") or "") if participant else ""
        # Skip our own messages
        if identity == CLI_IDENTITY:
            return
        if topic == TOPIC_CHAT:
            raw = getattr(packet, "data", b"") or b""
            try:
                msg = json.loads(raw)
                text = msg.get("message", "") or msg.get("text", "") or ""
            except (json.JSONDecodeError, UnicodeDecodeError):
                text = raw.decode("utf-8", "ignore")
            if text.strip():
                print(f"\n  [{identity}]: {text.strip()}")
                print("You> ", end="", flush=True)

    try:
        options = rtc.RoomOptions(auto_subscribe=True)
        await room.connect(ws_url, token, options)
    except Exception as exc:
        print(f"Failed to connect: {exc}")
        sys.exit(1)

    print(f"Connected as '{CLI_IDENTITY}'. Type messages and press Enter.")
    print("Commands: 'quit' to exit, 'mode openai' or 'mode local' to switch agent mode.\n")

    loop = asyncio.get_running_loop()
    try:
        while True:
            print("You> ", end="", flush=True)
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:  # EOF
                break
            text = line.strip()
            if not text:
                continue
            if text.lower() in ("quit", "exit", "q"):
                break

            # Mode switching command
            if text.lower().startswith("mode "):
                mode = text.split(None, 1)[1].strip().lower()
                _switch_mode(mode)
                continue

            try:
                await room.local_participant.send_text(text, topic=TOPIC_CHAT)
            except Exception as exc:
                print(f"  [error] Failed to send: {exc}")
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        print("\nDisconnecting...")
        await room.disconnect()
        print("Bye!")


if __name__ == "__main__":
    asyncio.run(main())
