from livekit import api
import os
import random
import time
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")
print(f"[generate_token] Using LIVEKIT_API_KEY={LIVEKIT_API_KEY}")
print(f"[generate_token] Using LIVEKIT_API_SECRET={LIVEKIT_API_SECRET}")


def generate_unique_room_name(prefix: str = "room") -> str:
    timestamp = time.time_ns()
    random_bits = random.getrandbits(32)
    return f"{prefix}-{timestamp:x}{random_bits:08x}"


def generate_token(room_name: str, identity: str) -> str:
    token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(
            api.VideoGrants(
                room_join=True,
                room=room_name,
                agent=True,  # keep if you want agent capabilities; otherwise drop
            )
        )
        .to_jwt()
    )
    return token


if __name__ == "__main__":
    # one shared room for all 3 participants
    room_name = generate_unique_room_name("test-room")

    agent_identity = "agent-user"
    user_identity = "playground-user"
    listener_identity = "listener-python"

    agent_token = generate_token(room_name, agent_identity)
    user_token = generate_token(room_name, user_identity)
    listener_token = generate_token(room_name, listener_identity)

    print("\n=== LiveKit room/tokens ===")
    print(f"ROOM_NAME = {room_name}\n")

    print("[Agent]")
    print(f"IDENTITY = {agent_identity}")
    print(f"TOKEN    = {agent_token}\n")

    print("[User / Playground]")
    print(f"IDENTITY = {user_identity}")
    print(f"TOKEN    = {user_token}\n")

    print("[Listener (listener.py)]")
    print(f"IDENTITY = {listener_identity}")
    print(f"TOKEN    = {listener_token}")
