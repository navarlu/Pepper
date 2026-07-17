"""Central configuration for ReceptionistBench (offline benchmark).

No CLI/argparse by project convention — edit the globals below to change
what runs. Paths are resolved relative to this file, so the runner works
regardless of the current working directory.
"""
import os
from pathlib import Path

# Optional: load a local .env if python-dotenv is installed.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

# --- Paths -------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
QUESTIONS_FILE = DATA_DIR / "questions.jsonl"
SNAPSHOTS_DIR = DATA_DIR / "snapshots"   # frozen endpoint responses (phase 2)
RESULTS_DIR = BASE_DIR / "results"

# --- Models under test (small -> big ladder) ---------------------------
# Same family so the size comparison is not confounded by vendor/tokenizer.
MODELS = [
    "gpt-5.4-nano",
    "gpt-5.4-mini",
    "gpt-5.4",
]

# --- Decoding ----------------------------------------------------------
# The thesis ran gpt-4o-mini at temperature 0.2. Some gpt-5-series models
# only accept the default temperature; set TEMPERATURE = None to omit the
# parameter entirely if a model rejects 0.2.
TEMPERATURE = 0.2
MAX_TOOL_HOPS = 5          # safety cap on the tool-calling loop per query
REQUEST_TIMEOUT_S = 60

# --- Reasoning effort --------------------------------------------------
# gpt-5.4 (nano/mini/5.4) values: "none" | "low" | "medium" | "high" | "xhigh".
# "none" turns model thinking off. Higher = slower + more (billed) reasoning
# tokens. Start low for a latency-sensitive receptionist. Set to None to omit
# the parameter entirely (falls back to the model's own default).
REASONING_EFFORT = "low"

# --- move_body (animation catalog) --------------------------------------
# Gesture experiment for chat.py. The model gets the full animation catalog
# (one line per clip) in its system prompt; execution is stubbed (logs
# instead of contacting the robot). Modes:
#   "tool"   - model calls the move_body(name) function tool
#   "inline" - model embeds [AnimationName] tags in its answer text; a
#              streaming parser strips them and fires the gesture at the
#              tag's position in the stream
#   "off"    - exact one-tool benchmark setup (set before benchmark runs!)
MOVE_BODY_MODE = "inline"
PROJECT_ROOT = BASE_DIR.parents[2]
ANIMATION_METADATA_DIR = PROJECT_ROOT / "experiments" / "animation_metadata" / "data" / "metadata"

# --- Auth --------------------------------------------------------------
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
