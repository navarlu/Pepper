"""
Test different get_directions_to_room descriptions to find one that
doesn't break Qwen 7B's tool calling when 3 tools are present.

Usage:
  uv run python voice-agent/tests/test_directions_variants.py
"""

import json
import time
from openai import OpenAI

VLLM_BASE = "http://localhost:8000/v1"
MODEL = "Qwen/Qwen2.5-7B-Instruct"
TEMPERATURE = 0.3
MAX_TOKENS = 300
RUNS = 3

SYSTEM_PROMPT = (
    "You are Pepper, a robot receptionist at CTU FEE Prague. "
    "Speak briefly and politely in English. "
    "Use query_search to find information — do not guess. "
    "Call play_animation to check your body state before every reply. "
    "Use get_directions_to_room when someone asks where a room is. "
    "Never say tool names aloud."
)

TOOL_QUERY = {
    "type": "function",
    "function": {
        "name": "query_search",
        "description": "Search the internal FEL knowledge base.",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}

TOOL_ANIM = {
    "type": "function",
    "function": {
        "name": "play_animation",
        "description": (
            "Check and set the robot body posture. "
            "Returns the current body state which you need before speaking. "
            "animation must be one of: greeting, bow, explain, happy, thinking, dont_know"
        ),
        "parameters": {
            "type": "object",
            "properties": {"animation": {"type": "string"}},
            "required": ["animation"],
        },
    },
}

# ── Variants of get_directions_to_room ────────────────────────────────────────

VARIANTS = {
    "V1_minimal": {
        "type": "function",
        "function": {
            "name": "get_directions_to_room",
            "description": "Get walking directions to a room. Returns route from the main entrance.",
            "parameters": {
                "type": "object",
                "properties": {"room_number": {"type": "string"}},
                "required": ["room_number"],
            },
        },
    },
    "V2_short": {
        "type": "function",
        "function": {
            "name": "get_directions_to_room",
            "description": "Look up directions to a room by its number.",
            "parameters": {
                "type": "object",
                "properties": {"room_number": {"type": "string"}},
                "required": ["room_number"],
            },
        },
    },
    "V3_data_framed": {
        "type": "function",
        "function": {
            "name": "get_directions_to_room",
            "description": "Returns floor and step-by-step directions for a given room number.",
            "parameters": {
                "type": "object",
                "properties": {"room_number": {"type": "string"}},
                "required": ["room_number"],
            },
        },
    },
    "V4_original": {
        "type": "function",
        "function": {
            "name": "get_directions_to_room",
            "description": (
                "Get directions on how to walk to a specific room in Building E. "
                "Call this whenever a visitor asks where a room is or how to get there. "
                "Returns step-by-step walking directions from the main entrance."
            ),
            "parameters": {
                "type": "object",
                "properties": {"room_number": {"type": "string"}},
                "required": ["room_number"],
            },
        },
    },
    "V5_renamed_short": {
        "type": "function",
        "function": {
            "name": "find_room",
            "description": "Look up directions to a room by number. Returns floor and route.",
            "parameters": {
                "type": "object",
                "properties": {"room_number": {"type": "string"}},
                "required": ["room_number"],
            },
        },
    },
}

FAKE_RESULTS = {
    "play_animation": lambda a: json.dumps({"body_state": "ready", "posture": a.get("animation", "?")}),
    "query_search": lambda a: json.dumps({
        "query": a.get("query", ""), "count": 1,
        "results": [{"title": "Dean", "content": "Dean phone: +420 224 352 850.", "source": "web", "score": 0.9}],
    }),
    "get_directions_to_room": lambda a: json.dumps({
        "room": a.get("room_number", "?"), "floor": "2nd",
        "directions": "Enter the main door, take the stairs to 2nd floor, turn right, room is the third door on the left.",
    }),
    "find_room": lambda a: json.dumps({
        "room": a.get("room_number", "?"), "floor": "2nd",
        "directions": "Enter the main door, take the stairs to 2nd floor, turn right, room is the third door on the left.",
    }),
}

SCENARIO = [
    {"user": "Hello!", "expect": ["play_animation"], "label": "greeting"},
    {"user": "What is the phone number of the dean?", "expect": ["query_search"], "label": "query"},
    {"user": "How do I get to room 230?", "expect": ["get_directions_to_room"], "label": "directions"},
    {"user": "Thank you, goodbye!", "expect": ["play_animation"], "label": "goodbye"},
]

# For V5 (renamed tool), adjust expected tool name
SCENARIO_RENAMED = [
    {"user": "Hello!", "expect": ["play_animation"], "label": "greeting"},
    {"user": "What is the phone number of the dean?", "expect": ["query_search"], "label": "query"},
    {"user": "How do I get to room 230?", "expect": ["find_room"], "label": "directions"},
    {"user": "Thank you, goodbye!", "expect": ["play_animation"], "label": "goodbye"},
]


def run_turn(client, messages, user_text, tools):
    messages.append({"role": "user", "content": user_text})
    tools_called = []
    final_text = ""
    for _ in range(6):
        resp = client.chat.completions.create(
            model=MODEL, messages=messages, tools=tools,
            temperature=TEMPERATURE, max_tokens=MAX_TOKENS,
            parallel_tool_calls=False,
        )
        msg = resp.choices[0].message
        if msg.tool_calls:
            messages.append(msg)
            for tc in msg.tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments)
                except json.JSONDecodeError:
                    args = {}
                tools_called.append(name)
                result = FAKE_RESULTS.get(name, lambda a: '{"ok":true}')(args)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            continue
        final_text = (msg.content or "").strip()
        messages.append({"role": "assistant", "content": final_text})
        break
    return tools_called, final_text


def run_scenario(client, tools, scenario):
    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
    step_results = []
    for step in scenario:
        tc, text = run_turn(client, msgs, step["user"], tools)
        expected = step["expect"]
        ok = all(t in tc for t in expected)
        leak_kws = ["tool_call", "<tool_call>", "play_animation", "query_search",
                     "get_directions", "find_room", "function", '{"animation', "body_state", "<|im_start"]
        leaked = any(kw in text.lower() for kw in leak_kws)
        step_results.append({"label": step["label"], "ok": ok, "leaked": leaked, "tools": tc, "text": text[:120]})
    return step_results


def main():
    client = OpenAI(base_url=VLLM_BASE, api_key="not-needed")
    client.models.list()
    print(f"Model: {MODEL}  temp={TEMPERATURE}  runs={RUNS}\n")

    summary = {}

    for vname, vtool in VARIANTS.items():
        tools = [TOOL_QUERY, TOOL_ANIM, vtool]
        scenario = SCENARIO_RENAMED if vname == "V5_renamed_short" else SCENARIO

        print(f"\n{'=' * 65}")
        desc = vtool["function"]["description"]
        print(f"  {vname}: \"{desc[:80]}\"")
        print(f"{'=' * 65}")

        passes = 0
        leaks = 0
        for run_i in range(RUNS):
            results = run_scenario(client, tools, scenario)
            all_ok = all(r["ok"] for r in results)
            any_leak = any(r["leaked"] for r in results)
            if all_ok:
                passes += 1
            if any_leak:
                leaks += 1
            mark = "PASS" if all_ok else "FAIL"
            leak_s = " LEAK" if any_leak else ""
            steps = " | ".join(f"{r['label']}:{'->'.join(r['tools']) or 'NONE'}" for r in results)
            print(f"  run{run_i+1}: [{mark}]{leak_s}  {steps}")
            # Print text for failed steps
            for r in results:
                if not r["ok"] or r["leaked"]:
                    print(f"         {r['label']} text: {r['text'][:100]}")

        summary[vname] = {"pass": passes, "leak": leaks}

    print(f"\n{'=' * 65}")
    print("SUMMARY")
    print(f"{'=' * 65}")
    for vname, s in summary.items():
        desc = VARIANTS[vname]["function"]["description"][:50]
        rate = s["pass"] / RUNS * 100
        print(f"  {vname:20s}  pass={s['pass']}/{RUNS} ({rate:.0f}%)  leak={s['leak']}  \"{desc}...\"")


if __name__ == "__main__":
    main()
