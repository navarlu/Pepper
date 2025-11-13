#!/usr/bin/env python3
"""
Bootstrap the Ego (conversational) agent for the hybrid architecture.

Creates an Ego agent with:
- Steering & knowledge blocks: goal, info_targets, world_model, people, environment
- Style & rules: persona, policies (read-only)
- Runtime status: ego_status, id_status, emotion_state
- Transcript sink: conversation_log (append-only by the APP, Ego only reads)

Usage:
  python bootstrap_letta_agent_ego.py --name Pepper-Ego-v003
  python bootstrap_letta_agent_ego.py --name Pepper-Ego-v003 --force-recreate --set-goal "first goal" --print-ids

Defaults:
  BASE_URL = http://localhost:8283/v1
  TOKEN    = AdaPass123!   (override with --token or LETTA_TOKEN)
"""

import argparse
import json
import os
import sys
import textwrap
from typing import Any, Dict, List, Optional

import requests

# ================== CONFIG ==================

DEFAULT_BASE_URL = os.getenv("LETTA_BASE_URL", "http://localhost:8283/v1").rstrip("/")
DEFAULT_TOKEN    = os.getenv("LETTA_TOKEN", "AdaPass123!")

DEFAULT_AGENT_NAME = "Pepper-Ego-v005"

SYSTEM_PROMPT = textwrap.dedent("""
You are Pepper (Ego), the user's friendly, concise, and efficient conversational partner.

You SEE these core memory blocks:
- goal: Superego's short guidance and subgoals (authoritative for planning what to ask next).
- info_targets: prioritized, actionable questions to ask next (can be empty; treat as hints).
- world_model: distilled facts about the user and environment (authoritative reference).
- people: directory of people and durable facts about them (merge new facts carefully).
- environment: brief context (location, timezone, participants, setting).
- persona: style and identity guidance.
- policies: read-only operational & safety rules.
- conversation_log (read-only): the chronological transcript maintained by the app. Do NOT write raw transcript yourself.

Behavioral rules:
1) Let 'goal' and 'info_targets' steer you. Ask at most one focused question at a time.
2) Keep replies brief, human, and helpful. Never fabricate facts; ask to confirm.
3) When you learn a durable fact about the current human, propose a precise update to 'people' as a short JSON patch in your reply (under a line 'PEOPLE_UPDATE:'), otherwise skip it.
4) When you infer context (timezone, participants, setting), propose a tiny patch for 'environment' similarly ('ENV_UPDATE:').
5) Follow 'policies' strictly. If a policy would be violated, refuse briefly and redirect safely.
6) Do NOT write to 'conversation_log'; the hosting app handles logging each turn.
""").strip()

LLM_CONFIG: Dict[str, Any] = {
    "model_endpoint_type": "openai",
    "model": os.getenv("LETTA_MODEL", "gpt-4o-mini"),
    "temperature": float(os.getenv("LETTA_TEMPERATURE", "0.01")),
}

EMBED_CONFIG: Dict[str, Any] = {
    "embedding_endpoint_type": "openai",
    "embedding_model": os.getenv("LETTA_EMBED_MODEL", "text-embedding-3-large"),
    "embedding_dim": int(os.getenv("LETTA_EMBED_DIM", "1536")),
}

# Memory blocks for hybrid orchestration.
# Tip: leave values small/empty; your workers/app will populate/append as needed.
MEMORY_BLOCKS: List[Dict[str, Any]] = [
    # Steering
    {
        "label": "goal",
        "description": "Superego guidance: durable objective + short-term subgoals.",
        "value": "",
        "limit": 4000,
    },
    {
        "label": "info_targets",
        "description": "Checklist of focused questions to fulfill the current goal (one per line, top=highest priority).",
        "value": "",
        "limit": 3000,
    },

    # Knowledge
    {
        "label": "world_model",
        "description": "Distilled facts about the active user/world, curated by Superego (JSON or compact text).",
        "value": "",
        "limit": 10000,
    },
    {
        "label": "people",
        "description": "JSON directory of people with durable facts for future conversations.",
        "value": textwrap.dedent("""
            {
              "_schema": "v1",
              "current_human": null,
              "entries": { }
            }
        """).strip(),
        "limit": 12000,
    },
    {
        "label": "environment",
        "description": "Brief situational context: location, timezone, participants, setting, notes.",
        "value": textwrap.dedent("""
            {
              "_schema": "v1",
              "location": null,
              "timezone": null,
              "participants": [],
              "setting": null,
              "notes": ""
            }
        """).strip(),
        "limit": 4000,
    },

    # Style & rules
    {
        "label": "persona",
        "description": "Pepper's identity and style guide.",
        "value": "Pepper is warm, curious, and efficient; prioritizes learning while being respectful and clear.",
        "limit": 2000,
    },
    {
        "label": "policies",
        "description": "Non-negotiable operational & safety rules.",
        "value": textwrap.dedent("""
            - Respect privacy; get consent before storing sensitive data.
            - Be transparent about capabilities and limitations.
            - Avoid commitments on behalf of the user without confirmation.
            - Safety-first; stop or escalate if there is risk of harm.
        """).strip(),
        "limit": 3000,
        "read_only": True,
    },

    # Runtime status (for dashboards/coordination)
    {
        "label": "ego_status",
        "description": "Ephemeral state of Ego (e.g., awaiting_user, asking_followup).",
        "value": "",
        "limit": 1000,
    },
    {
        "label": "id_status",
        "description": "Last actuation/status from Id (for observability).",
        "value": "",
        "limit": 1000,
    },
    {
        "label": "emotion_state",
        "description": "Current emotion as inferred/expressed by Id (JSON).",
        "value": "",
        "limit": 2000,
    },

    # Transcript sink
    {
        "label": "conversation_log",
        "description": "Append-only transcript (maintained by the host app). Ego only READS this block.",
        "value": "",
        "limit": 20000,
        # Letta servers often treat read_only as advisory; app will enforce append-only semantics.
        "read_only": True
    },
]

TOOLS: List[Dict[str, Any]] = [
    # You can register future tools here (e.g., structured JSON patch writer),
    # or keep Ego tool-free and let the app perform memory updates it approves.
]

# ================== HTTP helpers ==================

def _session(token: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    return s

def _json_or_raise(r: requests.Response) -> Any:
    ctype = r.headers.get("content-type", "")
    if "application/json" not in ctype.lower():
        raise RuntimeError(f"Non-JSON response ({ctype}) {r.status_code}: {r.text[:400]!r}")
    try:
        return r.json()
    except Exception as e:
        raise RuntimeError(f"Failed to parse JSON: {r.text[:400]!r}") from e

# ================== Core API calls ==================

def list_agents(base: str, sess: requests.Session, limit: int = 100) -> List[Dict[str, Any]]:
    r = sess.get(f"{base}/agents/", params={"limit": limit, "query_text": ""}, timeout=20)
    r.raise_for_status()
    return _json_or_raise(r)

def delete_agent(base: str, sess: requests.Session, agent_id: str) -> None:
    r = sess.delete(f"{base}/agents/{agent_id}/", timeout=20)
    if r.status_code not in (200, 204):
        raise RuntimeError(f"Delete failed {r.status_code}: {r.text[:400]}")

def create_agent(
    base: str,
    sess: requests.Session,
    name: str,
    system: str,
    llm_config: Dict[str, Any],
    embedding_config: Dict[str, Any],
    memory_blocks: Optional[List[Dict[str, Any]]] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    agent_type: Optional[str] = None,          # NEW
    enable_sleeptime: Optional[bool] = None,   # NEW
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "name": name,
        "system": system,
        "llm_config": llm_config,
        "embedding_config": embedding_config,
    }

    if agent_type is not None:
        payload["agent_type"] = agent_type

    if enable_sleeptime is not None:
        payload["enable_sleeptime"] = enable_sleeptime  # IMPORTANT

    # Optional but recommended for low-latency agents:
    # payload["initial_message_sequence"] = []

    if memory_blocks:
        payload["memory_blocks"] = memory_blocks
    if tools:
        payload["tools"] = tools

    r = sess.post(f"{base}/agents/", data=json.dumps(payload), timeout=45)
    if r.status_code >= 400:
        raise RuntimeError(f"Create failed {r.status_code}: {r.text[:800]}")
    return _json_or_raise(r)

def set_core_memory_block(base: str, sess: requests.Session, agent_id: str, label: str, value: str) -> Dict[str, Any]:
    r = sess.patch(
        f"{base}/agents/{agent_id}/core-memory/blocks/{label}",
        data=json.dumps({"value": value}),
        timeout=20,
    )
    r.raise_for_status()
    return _json_or_raise(r)

def get_agent_detail(base: str, sess: requests.Session, agent_id: str) -> Dict[str, Any]:
    r = sess.get(f"{base}/agents/{agent_id}/", timeout=20)
    r.raise_for_status()
    return _json_or_raise(r)

# ================== CLI ==================

def main():
    ap = argparse.ArgumentParser(description="Bootstrap Ego agent (hybrid orchestration).")
    ap.add_argument("--base", default=DEFAULT_BASE_URL, help="API base, e.g., http://localhost:8283/v1")
    ap.add_argument("--token", default=DEFAULT_TOKEN, help="Bearer token (or set LETTA_TOKEN)")
    ap.add_argument("--name", default=DEFAULT_AGENT_NAME, help="Agent name")
    ap.add_argument("--force-recreate", action="store_true", help="Delete existing same-name agent first")
    ap.add_argument("--set-goal", default=None, help="Optional: seed the 'goal' block after creation")
    ap.add_argument("--print-ids", action="store_true", help="Print a compact ID mapping for copy/paste")
    args = ap.parse_args()

    print(f"[bootstrap] Base: {args.base}")
    print(f"[bootstrap] Name: {args.name}")
    print(f"[bootstrap] Token: (set)" if args.token else "[bootstrap] Token: (missing)")

    sess = _session(args.token)

    # 1) Existing or not?
    try:
        existing = list_agents(args.base, sess, limit=200)
    except Exception as e:
        print(f"[error] listing agents failed: {e}")
        sys.exit(1)

    agent = next((a for a in existing if a.get("name") == args.name), None)

    # 2) Delete if requested
    if agent and args.force_recreate:
        print(f"[bootstrap] Deleting existing agent {agent['id']} ({args.name}) ...")
        try:
            delete_agent(args.base, sess, agent["id"])
            agent = None
        except Exception as e:
            print(f"[error] delete failed: {e}")
            sys.exit(1)

    # 3) Create if missing
    if not agent:
        print("[bootstrap] Creating agent...")
        try:
            agent = create_agent(
                base=args.base,
                sess=sess,
                name=args.name,
                system=SYSTEM_PROMPT,
                llm_config=LLM_CONFIG,
                embedding_config=EMBED_CONFIG,
                memory_blocks=MEMORY_BLOCKS,
                tools=TOOLS or None,
                agent_type="voice_convo_agent",
                enable_sleeptime=True,
            )
        except Exception as e:
            print(f"[error] create failed: {e}")
            sys.exit(1)
        print(f"[bootstrap] Created agent id: {agent.get('id')}")
    else:
        print(f"[bootstrap] Reusing existing agent id: {agent['id']}")

    agent_id = agent["id"]

    # 4) Optionally seed the goal
    if args.set_goal is not None:
        try:
            print(f"[bootstrap] Setting 'goal' -> {args.set_goal!r}")
            _ = set_core_memory_block(args.base, sess, agent_id, "goal", args.set_goal)
            print("[bootstrap] Goal updated.")
        except Exception as e:
            print(f"[warn] failed to set goal: {e}")

    # 5) Fetch details to expose block IDs
    try:
        detail = get_agent_detail(args.base, sess, agent_id)
    except Exception as e:
        print(f"[warn] failed to fetch agent detail: {e}")
        detail = agent

    # Build a {label: id} lookup for memory blocks
    blocks = detail.get("core_memory", {}).get("blocks", []) or detail.get("memory_blocks", []) or []
    label_to_id = {}
    for b in blocks:
        # servers may return id as 'id' or 'block_id'
        bid = b.get("id") or b.get("block_id")
        label = b.get("label")
        if bid and label:
            label_to_id[label] = bid

    # 6) Print summary
    summary = {
        "id": agent_id,
        "name": args.name,
        "base": args.base,
        "tools_count": len(TOOLS),
        "memory_blocks": sorted(label_to_id.keys()),
        "block_ids": label_to_id,
        "note": "Use these IDs in your hybrid workers (e.g., GOAL_BLOCK_ID, EMOTION_STATE_BLOCK_ID).",
    }
    print(json.dumps(summary, indent=2, ensure_ascii=False))

    if args.print_ids:
        print("\n# === Copy/paste IDs ===")
        print(f'EGO_AGENT_ID="{agent_id}"')
        for k in [
            "goal", "info_targets", "world_model", "people", "environment",
            "persona", "policies", "ego_status", "id_status", "emotion_state",
            "conversation_log"
        ]:
            if k in label_to_id:
                print(f'{k.upper()}_BLOCK_ID="{label_to_id[k]}"')

if __name__ == "__main__":
    main()
