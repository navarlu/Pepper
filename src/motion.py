# motion.py
import time
import os
import requests
from dotenv import load_dotenv
from pathlib import Path
DEBUG_ENV = False
DEBUG_ID = False
ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT_DIR / ".env"
DEBUG = False
load_dotenv(dotenv_path=ENV_PATH)
# e.g., export PEPPER_API_URL="http://127.0.0.1:5000"
BRIDGE_URL = os.getenv("BRIDGE_URL")
if DEBUG_ENV: print(f"[MOTION][ENV] Using BRIDGE_URL={BRIDGE_URL}")

def wave_hand():
    if DEBUG: print("[TOOL] wave_hand(): Waving hand!")
    time.sleep(0.3)
    return {"ok": True, "tool": "wave_hand"}

def play_animation(name: str, timeout: int = 8):
    """
    Trigger an animation on the robot by POSTing to /animation/<name>.
    The LLM passes ONLY the animation key string from animations.json (e.g., 'Listening_1', 'CircleEyes').
    """
    print(f"\n[MOTION] play_animation called with name='{name}'")
    if not name or not isinstance(name, str):
        raise ValueError("name must be a non-empty string")
    cleaned = name.strip()
    url = f"{BRIDGE_URL}/animation/{cleaned}"
    if DEBUG: print(f"[TOOL] play_animation(): Triggering '{cleaned}' via {url}")
    r = requests.post(url, json={}, timeout=timeout)  # server defaults to non-blocking
    r.raise_for_status()
    return r.json()
