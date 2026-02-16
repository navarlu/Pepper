# --- NEW / CHANGED SECTIONS ONLY ---

import json
import os
from typing import List, Dict, Any

from openai import OpenAI
from dotenv import load_dotenv
from pathlib import Path

DEBUG_ENV = False
DEBUG_ID = False
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
ENV_PATH = ROOT_DIR / ".env"

# Where to load the animations list from
ANIMATIONS_FILE = os.getenv("ANIMATIONS_FILE", str(ROOT_DIR / "animations.json"))

load_dotenv(dotenv_path=ENV_PATH)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
CONVERSATION_BLOCK_ID = os.getenv("CONVERSATION_BLOCK_ID")
EGO_AGENT_ID = os.getenv("EGO_AGENT_ID")
MAX_RECENT_MESSAGES = int(os.getenv("MAX_RECENT_MESSAGES"))
GOAL_BLOCK_ID = os.getenv("GOAL_BLOCK_ID")
EMOTION_STATE_BLOCK_ID = os.getenv("EMOTION_STATE_BLOCK_ID")  # keep name to avoid wider changes
ROBOT_TARGET = (os.getenv("ROBOT_TARGET") or "real").strip().lower()
if ROBOT_TARGET not in {"real", "virtual"}:
    ROBOT_TARGET = "real"

from letta_io import fetch_recent_conversation, overwrite_block_text

# ⬇️ swap play_emotion → play_animation (same import location)
from motion import play_animation, wave_hand
from virtual_animations import VIRTUAL_EMOTIONS

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
client = OpenAI(api_key=OPENAI_API_KEY)

# System now talks about animations instead of emotions
TARGET_LABEL = "Pepper" if ROBOT_TARGET == "real" else "virtual Pepper"

ID_SYSTEM = (
    "You are ID, the robot's reactive controller.\n"
    "You see the recent user–assistant conversation and must decide via function tools:\n"
    "- activate_animation() to trigger a single {} animation by key from the provided list.\n"
    "- If no motion is needed, do nothing."
).format(TARGET_LABEL)

def _load_animation_keys_from_json(max_keys: int = 256) -> List[str]:
    """
    Read animations.json and return the list of keys we allow the LLM to call.
    If the file is missing or invalid, return a tiny safe fallback.
    """
    if ROBOT_TARGET == "virtual":
        return sorted(VIRTUAL_EMOTIONS.keys())
    try:
        with open(ANIMATIONS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        keys = sorted([k for k, v in data.items() if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip()])
        if not keys:
            raise ValueError("No keys found in animations.json")
        return keys[:max_keys]
    except Exception as e:
        if DEBUG_ID: print(f"[ID] animations.json load warning: {e}")
        # Fallback so the agent still works; adjust to any defaults you like
        return ["Listening_1", "CircleEyes"]

def id_tools_def():
    # Tool schema mirrors your previous one but for animations
    enum_values = _load_animation_keys_from_json()
    desc = (
        "Trigger exactly one {} animation by its key from animations.json. "
        "Examples: 'Listening_1', 'CircleEyes'."
    ).format("Pepper" if ROBOT_TARGET == "real" else "virtual Pepper")
    return [
        {
            "type": "function",
            "function": {
                "name": "activate_animation",
                "description": desc,
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "enum": enum_values,
                            "description": "Animation key to play (must match animations.json key)."
                        }
                    },
                    "required": ["name"],
                    "additionalProperties": False
                }
            }
        }
    ]

def build_convo_context(msgs: List[Dict[str, Any]]) -> str:
    lines = []
    for m in msgs:
        role = m.get("role", "unknown")
        content = m.get("content", "")
        lines.append(f"{role.upper()}: {content}")
    return "\n".join(lines)

def decide_and_act(conversation: List[Dict[str, Any]]) -> Dict[str, Any]:
    convo_txt = build_convo_context(conversation)
    user = (
        "Recent conversation (most recent last):\n"
        f"{convo_txt}\n\n"
        "If a gesture or animation is appropriate, call the tool. Otherwise, do nothing."
    )
    if DEBUG_ID: print("[IdWorker] Deciding action based on conversation...")
    resp = client.chat.completions.create(
        model=OPENAI_MODEL,
        temperature=0.0,
        messages=[
            {"role": "system", "content": ID_SYSTEM},
            {"role": "user", "content": user},
        ],
        tools=id_tools_def(),
        tool_choice="auto",
        max_tokens=128,
    )

    status = {"action_taken": False, "actions": [], "notes": ""}

    try:
        choice = resp.choices[0].message
    except Exception as e:
        status["notes"] = f"model_error: {e}"
        return status

    if not getattr(choice, "tool_calls", None):
        status["notes"] = "no_tool_called"
        return status

    for tc in choice.tool_calls:
        name = tc.function.name

        if name == "activate_animation":
            try:
                args = json.loads(tc.function.arguments or "{}")
            except Exception:
                args = {}
            which = (args.get("name") or "").strip()

            if which:
                try:
                    result = play_animation(which)  # ⬅️ swapped call
                    status["actions"].append(
                        {"tool": "activate_animation", "name": which, "result": result}
                    )
                    status["action_taken"] = True
                except Exception as e:
                    status["actions"].append(
                        {"tool": "activate_animation", "name": which, "error": str(e)}
                    )
            else:
                status["actions"].append(
                    {"tool": "activate_animation", "error": "missing name"}
                )
        else:
            if DEBUG_ID: print(f"[IdWorker] Unknown tool requested: {name}")

    if not status["actions"]:
        status["notes"] = "no_action_executed"

    return status

def update_emotion_block_once():
    """
    One-shot worker step:
    - Fetch recent conversation from Letta
    - Decide & act via tools (wave/animation)
    - Write a compact JSON state to EMOTION_STATE_BLOCK_ID (kept for compatibility)
    """
    if not EMOTION_STATE_BLOCK_ID:
        if DEBUG_ID: print("[IdWorker] EMOTION_STATE_BLOCK_ID not set; skipping.")
        return

    conversation = fetch_recent_conversation(EGO_AGENT_ID, MAX_RECENT_MESSAGES)
    status = decide_and_act(conversation)

    payload = {"last_update": "now", "status": status}
    overwrite_block_text(EMOTION_STATE_BLOCK_ID, json.dumps(payload, ensure_ascii=False, indent=2))
    if DEBUG_ID: print("[IdWorker] Updated state and executed actions.")


# --- OPTIONAL: if you also use the quick single-shot path, keep logic but switch to animations ---

def _llm_choose_and_play_animation(reasoning_prompt: str):
    """
    Same flow as your _llm_choose_and_play_emotion, but selects ONE animation key.
    """
    client = OpenAI()

    system = (
        "You are the Id. Pick and trigger ONE animation tool call immediately.\n"
        "Valid animation keys are provided in the tool schema (from animations.json).\n"
        "Do not explain; JUST call the tool with the single best animation key for this moment."
    )

    enum_values = _load_animation_keys_from_json()
    tools = [
        {
            "type": "function",
            "function": {
                "name": "activate_animation",
                "description": "Trigger exactly one Pepper animation by key from animations.json.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "enum": enum_values,
                            "description": "Animation key to play."
                        }
                    },
                    "required": ["name"],
                    "additionalProperties": False
                }
            }
        }
    ]
    if DEBUG_ID: print("[IdWorker] Deciding action based on prompt...")
    resp = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        temperature=0.3,
        tools=tools,
        tool_choice="auto",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": reasoning_prompt},
        ],
    )

    msg = resp.choices[0].message
    if msg.tool_calls:
        for tc in msg.tool_calls:
            if tc.function.name == "activate_animation":
                args = json.loads(tc.function.arguments or "{}")
                which = (args.get("name") or "").strip()
                if which:
                    try:
                        play_animation(which)
                        return {"ok": True, "animation": which}
                    except Exception as e:
                        return {"ok": False, "error": str(e), "animation": which}
    return {"ok": False, "error": "no_tool_call"}

def react_to_user_text(user_text: str):
    prompt = (
        "User just spoke. Pick ONE immediate animation to display during listening-turn transition.\n"
        f"USER_TEXT:\n{user_text}\n"
    )
    return _llm_choose_and_play_animation(prompt)

def react_to_ego_start(prompt: str):
    
    return _llm_choose_and_play_animation(prompt)



if __name__ == "__main__":
    # update_emotion_block_once()
    if DEBUG_ID:   print("ID main executed.")
