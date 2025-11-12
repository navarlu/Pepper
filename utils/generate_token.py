from livekit import api
import os
from pathlib import Path
from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# Make sure these are set in your environment:
LIVEKIT_API_KEY = os.getenv("LIVEKIT_API_KEY")
LIVEKIT_API_SECRET = os.getenv("LIVEKIT_API_SECRET")
print(f"[generate_token] Using LIVEKIT_API_KEY={LIVEKIT_API_KEY}")
print(f"[generate_token] Using LIVEKIT_API_SECRET={LIVEKIT_API_SECRET}")
def generate_token(room_name: str, identity: str) -> str:
    token = (
        api.AccessToken(LIVEKIT_API_KEY, LIVEKIT_API_SECRET)
        .with_identity(identity)
        .with_name(identity)
        .with_grants(
            api.VideoGrants(room_join=True, room=room_name, agent=True)
        )
        .to_jwt()
    )
    return token

if __name__ == "__main__":
    print(generate_token("test-room-9", "agent-user"))
