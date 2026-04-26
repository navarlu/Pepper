"""
Interactive chat REPL against the local Qwen 2.5 7B (vLLM) endpoint.

Goal: quickly experiment with system prompts, tool definitions, return-shape
choices, and other knobs to find the most robust setting for tool chaining
(animation → search → answer, no duplicate replies). Once we find a
working config, we implement it in the real agent.

Tools (dummy mirrors of production):
  - play_pose(pose)       — fire-and-forget side-effect (like play_animation)
  - search_kb(query)      — returns canned info (like query_search)
  - get_time()            — no-param third tool

Usage:
  python tool_chat_repl.py            # default config

REPL commands (typed on a line by themselves):
  /reset                 clear conversation history
  /system <text>         replace system prompt (single line)
  /temp <float>          set temperature
  /max <int>             set max_tokens
  /tools <a,b,c>         active tool subset (any of: pose,search,time)
  /pose-return <data|none>  what play_pose returns: full body-state dict, or None
  /parallel <on|off>     parallel_tool_calls flag
  /show                  print current config
  /history               print chat history
  /quit                  exit
  (anything else)        send as user message
"""

import json
import os
import sys
import time
from datetime import datetime
from openai import OpenAI

# ── Endpoint (defaults match production) ─────────────────────────────
VLLM_BASE = os.environ.get("LOCAL_LLM_BASE_URL", "http://localhost:8000/v1")
MODEL = os.environ.get("LOCAL_LLM_MODEL", "Qwen/Qwen2.5-7B-Instruct")

# ── Default config (tunable at runtime via REPL commands) ────────────
DEFAULT_CONFIG = {
    "system_prompt": (
        "You are Pepper, a robot receptionist. "
        "Speak briefly and politely in English. "
        "If you need information to answer, call search_kb first. "
        "Then call play_pose right before your spoken reply. "
        "Never say tool names aloud."
    ),
    "temperature": 0.7,             # Qwen-recommended (was 0.3)
    "top_p": 0.8,                   # Qwen-recommended
    "repetition_penalty": 1.05,     # Qwen-recommended
    "max_tokens": 300,
    "active_tools": ["pose", "search", "time"],   # ORDER matters for Qwen reliability
    "pose_return": "data",          # "data" | "none"
    "parallel_tool_calls": False,
    "stop_tokens": [],              # extra stop strings, e.g. ["<|im_start|>"]
}


# ── Dummy tool definitions ───────────────────────────────────────────
TOOL_POSE = {
    "type": "function",
    "function": {
        "name": "play_pose",
        "description": (
            "Set the robot body posture. "
            "Returns the current body state which you need before speaking. "
            "pose must be one of: greeting, bow, explain, happy, thinking, dont_know"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pose": {
                    "type": "string",
                    "description": "Body pose name",
                },
            },
            "required": ["pose"],
        },
    },
}

TOOL_SEARCH = {
    "type": "function",
    "function": {
        "name": "search_kb",
        "description": (
            "Look up information in the knowledge base. "
            "Use this whenever the user asks about people, rooms, locations, "
            "schedules, or any factual question — do not guess."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Search query",
                },
            },
            "required": ["query"],
        },
    },
}

TOOL_TIME = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "Return the current time of day.",
        "parameters": {"type": "object", "properties": {}},
    },
}

ALL_TOOLS = {"pose": TOOL_POSE, "search": TOOL_SEARCH, "time": TOOL_TIME}


# ── Dummy tool implementations ───────────────────────────────────────
def execute_tool(name: str, args: dict, *, pose_return: str):
    """Return a value to send back as the tool result, or None to omit."""
    if name == "play_pose":
        pose = args.get("pose", "")
        if pose_return == "none":
            return None
        return {"body_state": "ready", "posture": pose}

    if name == "search_kb":
        q = (args.get("query") or "").lower()
        # Canned answers so we can verify the model uses them
        if "230" in q or "room" in q:
            return {
                "room": "E-230",
                "floor": 2,
                "directions": "Take the stairs to the second floor and turn right.",
            }
        if "dean" in q:
            return {"name": "Prof. Doe", "phone": "+420 224 35 1234"}
        if "wifi" in q:
            return {"ssid": "FEL-guest", "password": "ask reception"}
        return {"result": "No information found for: " + (args.get("query") or "")}

    if name == "get_time":
        return {"time": datetime.now().strftime("%H:%M")}

    return {"error": f"unknown_tool:{name}"}


# ── Core chat-with-tools loop (mimics OpenAI tool-calling) ───────────
def run_turn(client, history, cfg, *, verbose=True):
    """Send one user turn through the model and any tool-call cycles.

    Returns (final_assistant_text, n_llm_calls, tool_calls_made).
    """
    tools = [ALL_TOOLS[k] for k in cfg["active_tools"] if k in ALL_TOOLS]
    n_calls = 0
    tool_calls_made = []
    spoken_segments = []

    while True:
        n_calls += 1
        if verbose:
            print(f"  → llm call #{n_calls} (tools={[t['function']['name'] for t in tools]})")

        kwargs = {
            "model": MODEL,
            "messages": history,
            "temperature": cfg["temperature"],
            "max_tokens": cfg["max_tokens"],
        }
        if cfg.get("top_p") is not None:
            kwargs["top_p"] = cfg["top_p"]
        # repetition_penalty / extra_body for vLLM (not in OpenAI spec)
        extra_body = {}
        if cfg.get("repetition_penalty") is not None:
            extra_body["repetition_penalty"] = cfg["repetition_penalty"]
        if extra_body:
            kwargs["extra_body"] = extra_body
        if tools:
            kwargs["tools"] = tools
            kwargs["parallel_tool_calls"] = cfg["parallel_tool_calls"]
        if cfg.get("stop_tokens"):
            kwargs["stop"] = list(cfg["stop_tokens"])

        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as e:
            print(f"  ✗ vLLM error: {e}")
            return None, n_calls, tool_calls_made

        msg = resp.choices[0].message
        if verbose:
            preview = (msg.content or "").replace("\n", " ")[:160]
            tcs = msg.tool_calls or []
            print(f"  ← text={preview!r}  tool_calls={len(tcs)}")

        # Save assistant message in history (must include tool_calls if any)
        assistant_entry = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            assistant_entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        history.append(assistant_entry)

        if msg.content:
            spoken_segments.append(msg.content)

        # If no tool calls, we're done
        if not msg.tool_calls:
            break

        # Execute tools and append results
        all_none = True
        for tc in msg.tool_calls:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = execute_tool(name, args, pose_return=cfg["pose_return"])
            tool_calls_made.append((name, args, result))
            if verbose:
                print(f"    [tool] {name}({args}) -> {result}")

            if result is not None:
                all_none = False

            # OpenAI spec requires a tool response message for every tool_call
            tool_content = "" if result is None else json.dumps(result)
            history.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "name": name,
                "content": tool_content,
            })

        # Mirror LiveKit SDK behavior: if every tool returned None this turn,
        # do NOT re-call the LLM. The text already streamed from the prior
        # call is the final spoken reply. (livekit/agents#4554 workaround.)
        if all_none:
            if verbose:
                print("  ⏹  all tools returned None — skipping LLM re-call (matches SDK)")
            break

        # Safety: cap at 5 LLM calls per turn
        if n_calls >= 5:
            print("  ! cap reached (5 LLM calls), breaking")
            break

    return "\n".join(s for s in spoken_segments if s), n_calls, tool_calls_made


# ── REPL helpers ─────────────────────────────────────────────────────
def show_cfg(cfg):
    print("─── current config ───")
    print(f"  system_prompt: {cfg['system_prompt']!r}")
    print(f"  temperature:   {cfg['temperature']}")
    print(f"  max_tokens:    {cfg['max_tokens']}")
    print(f"  active_tools:  {sorted(cfg['active_tools'])}")
    print(f"  pose_return:   {cfg['pose_return']}")
    print(f"  parallel:      {cfg['parallel_tool_calls']}")
    print(f"  endpoint:      {VLLM_BASE}  model: {MODEL}")
    print("──────────────────────")


def show_history(history):
    print("─── chat history ───")
    for i, m in enumerate(history):
        role = m["role"]
        body = m.get("content")
        if role == "tool":
            print(f"  [{i}] tool {m['name']!r}: {body}")
        elif role == "assistant" and m.get("tool_calls"):
            print(f"  [{i}] assistant text={body!r}")
            for tc in m["tool_calls"]:
                print(f"        tool_call: {tc['function']['name']}({tc['function']['arguments']})")
        else:
            print(f"  [{i}] {role}: {body}")
    print("────────────────────")


def main():
    cfg = dict(DEFAULT_CONFIG)
    cfg["active_tools"] = set(DEFAULT_CONFIG["active_tools"])

    client = OpenAI(base_url=VLLM_BASE, api_key="dummy")

    history = [{"role": "system", "content": cfg["system_prompt"]}]

    print(f"\nQwen tool-call REPL — endpoint {VLLM_BASE}, model {MODEL}")
    print("Type a message, or /help for commands.\n")
    show_cfg(cfg)
    print()

    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue

        # Slash commands
        if line.startswith("/"):
            parts = line.split(None, 1)
            cmd, arg = parts[0], (parts[1] if len(parts) > 1 else "")

            if cmd == "/quit":
                break
            if cmd == "/help":
                print(__doc__)
                continue
            if cmd == "/show":
                show_cfg(cfg)
                continue
            if cmd == "/history":
                show_history(history)
                continue
            if cmd == "/reset":
                history = [{"role": "system", "content": cfg["system_prompt"]}]
                print("(history cleared)")
                continue
            if cmd == "/system":
                cfg["system_prompt"] = arg
                history = [{"role": "system", "content": cfg["system_prompt"]}]
                print(f"(system prompt set, history cleared)")
                continue
            if cmd == "/temp":
                cfg["temperature"] = float(arg)
                print(f"(temperature = {cfg['temperature']})")
                continue
            if cmd == "/max":
                cfg["max_tokens"] = int(arg)
                print(f"(max_tokens = {cfg['max_tokens']})")
                continue
            if cmd == "/tools":
                wanted = [t.strip() for t in arg.split(",") if t.strip()]
                bad = [t for t in wanted if t not in ALL_TOOLS]
                if bad:
                    print(f"(unknown tool keys: {bad}; valid: {sorted(ALL_TOOLS)})")
                    continue
                cfg["active_tools"] = wanted          # preserve order
                print(f"(active_tools = {cfg['active_tools']})")
                continue
            if cmd == "/pose-return":
                if arg not in ("data", "none"):
                    print("(use: /pose-return data | none)")
                    continue
                cfg["pose_return"] = arg
                print(f"(pose_return = {cfg['pose_return']})")
                continue
            if cmd == "/parallel":
                cfg["parallel_tool_calls"] = arg.lower() in ("on", "true", "1", "yes")
                print(f"(parallel_tool_calls = {cfg['parallel_tool_calls']})")
                continue
            if cmd == "/stop":
                # /stop                  -> clear stops
                # /stop <|im_start|>     -> single stop
                # /stop a,b,c            -> comma-separated
                if not arg:
                    cfg["stop_tokens"] = []
                else:
                    cfg["stop_tokens"] = [s.strip() for s in arg.split(",") if s.strip()]
                print(f"(stop_tokens = {cfg['stop_tokens']})")
                continue

            print(f"(unknown command: {cmd}; type /help)")
            continue

        # Regular user message
        history.append({"role": "user", "content": line})
        t0 = time.monotonic()
        spoken, n_calls, tools_called = run_turn(client, history, cfg, verbose=True)
        dt = time.monotonic() - t0
        print(f"\nbot> {spoken!r}")
        print(f"     (llm_calls={n_calls}  tool_calls={[c[0] for c in tools_called]}  duration={dt:.1f}s)\n")


if __name__ == "__main__":
    main()
