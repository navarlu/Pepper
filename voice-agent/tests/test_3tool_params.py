"""
Isolate the exact cause: is it having 3 tools with string params?
Or can we find a 3rd tool format that works?

Usage:
  uv run python voice-agent/tests/test_3tool_params.py
"""

import json
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

# ── Different 3rd tool configs to test ────────────────────────────────────────

THIRD_TOOLS = {
    "no_params": {
        "type": "function",
        "function": {
            "name": "get_time",
            "description": "Get the current time.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    "int_param": {
        "type": "function",
        "function": {
            "name": "get_directions_to_room",
            "description": "Get directions to a room by number.",
            "parameters": {
                "type": "object",
                "properties": {"room_number": {"type": "integer"}},
                "required": ["room_number"],
            },
        },
    },
    "str_param_short_name": {
        "type": "function",
        "function": {
            "name": "get_room",
            "description": "Get directions to a room.",
            "parameters": {
                "type": "object",
                "properties": {"room": {"type": "string"}},
                "required": ["room"],
            },
        },
    },
    "str_param_same_name_as_query": {
        "type": "function",
        "function": {
            "name": "get_room_info",
            "description": "Get directions to a room.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    "enum_param": {
        "type": "function",
        "function": {
            "name": "get_directions_to_room",
            "description": "Get directions to a room by number.",
            "parameters": {
                "type": "object",
                "properties": {
                    "room_number": {
                        "type": "string",
                        "enum": ["101", "102", "201", "230", "301", "302"],
                    }
                },
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
    "get_time": lambda a: json.dumps({"time": "14:30"}),
    "get_directions_to_room": lambda a: json.dumps({"room": str(a.get("room_number", "?")), "directions": "Stairs to 2nd floor, turn right."}),
    "get_room": lambda a: json.dumps({"room": a.get("room", "?"), "directions": "Stairs to 2nd floor, turn right."}),
    "get_room_info": lambda a: json.dumps({"room": a.get("query", "?"), "directions": "Stairs to 2nd floor, turn right."}),
}

# Simple 3-step test (no directions step — just test if 3rd tool breaks animation/query)
SCENARIO_BASIC = [
    {"user": "Hello!", "expect": ["play_animation"]},
    {"user": "What is the dean's phone number?", "expect": ["query_search"]},
    {"user": "Thank you!", "expect": ["play_animation"]},
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


def test_config(client, label, tools):
    passes = 0
    for run_i in range(RUNS):
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
        all_ok = True
        leaked = False
        details = []
        for step in SCENARIO_BASIC:
            tc, text = run_turn(client, msgs, step["user"], tools)
            ok = all(t in tc for t in step["expect"])
            if not ok:
                all_ok = False
            leak_kws = ["tool_call", "<tool_call>", "<|im_start"]
            if any(kw in text for kw in leak_kws):
                leaked = True
            details.append(f"{'->'.join(tc) or 'NONE'}")
        if all_ok:
            passes += 1
        mark = "OK" if all_ok else "FAIL"
        leak_s = " LEAK" if leaked else ""
        print(f"    run{run_i+1}: [{mark}]{leak_s}  {' | '.join(details)}")
    return passes


def main():
    client = OpenAI(base_url=VLLM_BASE, api_key="not-needed")
    client.models.list()

    results = {}
    for name, tool3 in THIRD_TOOLS.items():
        tools = [TOOL_QUERY, TOOL_ANIM, tool3]
        print(f"\n  {name}: {tool3['function']['name']}({list(tool3['function']['parameters'].get('properties', {}).keys())})")
        p = test_config(client, name, tools)
        results[name] = p

    print(f"\n{'=' * 50}")
    print("RESULTS")
    print(f"{'=' * 50}")
    for name, p in results.items():
        print(f"  {name:30s}  pass={p}/{RUNS} ({p/RUNS*100:.0f}%)")


if __name__ == "__main__":
    main()
