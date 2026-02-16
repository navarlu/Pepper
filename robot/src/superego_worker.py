import json
from typing import List, Dict, Any

from letta_io import fetch_recent_conversation, overwrite_block_text
from openai import OpenAI
import os
from dotenv import load_dotenv
from pathlib import Path
DEBUG = False
DEBUG_ENV = False
# Resolve project root (two levels up from this file)
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
ENV_PATH = ROOT_DIR / ".env"

# Load .env from root folder
load_dotenv(dotenv_path=ENV_PATH)
if DEBUG_ENV: print(f"[ENV] Loaded .env from {ENV_PATH}")
if DEBUG_ENV: print(f"[ENV] OPENAI_API_KEY present: {'OPENAI_API_KEY' in os.environ}")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
CONVERSATION_BLOCK_ID = os.getenv("CONVERSATION_BLOCK_ID")
EGO_AGENT_ID = os.getenv("EGO_AGENT_ID")
if DEBUG_ENV: print(f"[ENV] Using CONVERSATION_BLOCK_ID={CONVERSATION_BLOCK_ID}")
if DEBUG_ENV: print(f"[ENV] Using EGO_AGENT_ID={EGO_AGENT_ID}")
MAX_RECENT_MESSAGES = int(os.getenv("MAX_RECENT_MESSAGES"))
if DEBUG_ENV: print(f"[ENV] Using MAX_RECENT_MESSAGES={MAX_RECENT_MESSAGES}")
GOAL_BLOCK_ID = os.getenv("GOAL_BLOCK_ID")
if DEBUG_ENV: print(f"[ENV] Using GOAL_BLOCK_ID={GOAL_BLOCK_ID}")
client = OpenAI(api_key=OPENAI_API_KEY)

SYS = """
You are the Superego planner (strategist/coach). Your single purpose is to help the user make their dreams come true.
From the RECENT CONVERSATION, produce a tiny “cue card” for the Ego that is EASY to follow in regular conversation.

DESIGN GOALS
- Be minimal: one concrete next prompt for Ego, plus at most two micro-directives.
- Stay conversational and human. Let Ego ask exactly ONE question at a time.
- Progress the coaching arc: vision → reality → obstacles → resources → options → commitment → review.
- Keep tracking the durable objective, but don’t drown Ego in details.

OUTPUT STRICT JSON ONLY (no extra text). Keep total under 250 tokens.
Required keys:
- ultimate: string                // durable higher-order objective in one sentence
- stage: string                   // one of ["vision","reality","obstacles","resources","options","commitment","review"]
- next_prompt: string             // EXACTLY ONE short question/request Ego should say next
- ego_directives: {               // tiny, optional nudges to keep it natural
    "tone": "warm|curious|concise|supportive",
    "max_words": 40,              // soft cap for Ego's next response
    "ack": "short acknowledgment to say before the question"  // e.g., "Got it —", "" if none
  }
- info_targets: [string]          // 0–2 micro-questions Ego can use as follow-ups later (not now)
- subgoals: [                     // 0–2 compact items for internal tracking
  { "task": string, "done_if": string, "priority": 1|2|3 }
]
- ttl_turns: 1|2|3                // how many user turns to keep reusing this cue if no new info arrives

CONSTRAINTS
- Default to natural small talk if the user goes off-topic; keep the plan in the background.
- Avoid multi-part questions. Avoid interrogations; be friendly and paced.
- If sensitive info would help, offer an easy “skip” in the next_prompt wording.
"""

def build_convo_context(msgs: List[Dict[str, Any]]) -> str:
    lines = []
    for m in msgs:
        role = m.get("role", "unknown")
        content = m.get("content", "")
        lines.append(f"{role.upper()}: {content}")
    return "\n".join(lines)

def propose_goals(conversation: List[Dict[str, Any]]) -> Dict[str, Any]:
    convo_txt = build_convo_context(conversation)
    user = f"""Recent conversation (most recent last):
{convo_txt}

Return ONLY the JSON (no commentary)."""
    if DEBUG: print("[Superego] Proposing goals based on conversation...")
    resp = client.chat.completions.create(
        model="gpt-4o-mini",  # any capable, low-latency model is fine
        messages=[
            {"role": "system", "content": SYS},
            {"role": "user", "content": user},
        ],
        temperature=0.2,
    )
    text = resp.choices[0].message.content.strip()
    # Be defensive: ensure valid JSON
    start = text.find("{")
    end = text.rfind("}")
    json_text = text[start:end+1] if start != -1 and end != -1 else '{"ultimate":"","subgoals":[],"next_prompt":""}'
    return json.loads(json_text)

def update_goal_block_once():
    conversation = fetch_recent_conversation(EGO_AGENT_ID, MAX_RECENT_MESSAGES)
    goals = propose_goals(conversation)
    normalized = json.dumps(goals, ensure_ascii=False, indent=2)
    if DEBUG: print(f"[Superego] Proposed goals:\n{normalized}")
    overwrite_block_text(GOAL_BLOCK_ID, normalized)
    if DEBUG: print("[Superego] Updated goal block.")

def _should_update_and_propose_goals(conversation: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Ask the LLM whether to update goals now. If yes, return a new plan.
    Output JSON strictly:
    {
      "update": true|false,
      "goals": {
        "ultimate": "...",
        "subgoals": ["...", "..."],
        "next_prompt": "..."
      }
    }
    """
    client = OpenAI()
    convo_text = ""
    for m in conversation:
        role = m.get("role", "user")
        content = m.get("content", "")
        convo_text += f"{role.upper()}: {content}\n"

    sys_prompt = (
        "You are the Superego (high-level strategist). Decide if the long-term goal/state "
        "needs updating NOW, given the conversation so far. Only refresh if it meaningfully "
        "steers the dialogue. If not needed, say update=false.\n\n"
        "Then, if updating, produce a concise goal plan. Output strict JSON."
    )
    user_prompt = (
        "Conversation (newest last):\n"
        f"{convo_text}\n\n"
        "Return JSON: {\"update\": bool, \"goals\": {\"ultimate\": str, \"subgoals\": [str], \"next_prompt\": str}}.\n"
        "If update=false, still include an empty 'goals': {\"ultimate\":\"\",\"subgoals\":[],\"next_prompt\":\"\"}."
    )
    if DEBUG: print(f"[Superego] Deciding on goal update with conversation:\n{convo_text}")
    if DEBUG:print("[Superego] Deciding whether to update goals...")
    resp = client.chat.completions.create(
        model=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        temperature=0.2,
        messages=[
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_prompt},
        ],
    )

    text = resp.choices[0].message.content or "{}"
    # Extract JSON (in case of extras)
    start = text.find("{")
    end = text.rfind("}")
    json_text = text[start:end+1] if start != -1 and end != -1 else '{"update":false,"goals":{"ultimate":"","subgoals":[],"next_prompt":""}}'
    return json.loads(json_text)

def maybe_update_goal_block_once():
    conversation = fetch_recent_conversation(EGO_AGENT_ID, MAX_RECENT_MESSAGES)
    decision = _should_update_and_propose_goals(conversation)
    if decision.get("update"):
        normalized = json.dumps(decision.get("goals", {}), ensure_ascii=False, indent=2)
        if DEBUG: print(f"[Superego] Updating goals:\n{normalized}")
        overwrite_block_text(GOAL_BLOCK_ID, normalized)
        if DEBUG: print("[Superego] Updated goal block.")
    else:
        if DEBUG: print("[Superego] Skipped goal update (model chose update=false).")

if __name__ == "__main__":
    maybe_update_goal_block_once()

if __name__ == "__main__":
    #update_goal_block_once()
    maybe_update_goal_block_once()
