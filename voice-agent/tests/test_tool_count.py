"""
Test: Does the NUMBER of tools affect Qwen 7B's reliability?

Hypothesis: Qwen handles 2 tools well but degrades with 3+.

Usage:
  uv run python voice-agent/tests/test_tool_count.py
"""

import json
import time
from openai import OpenAI

VLLM_BASE = "http://localhost:8000/v1"
MODEL = "Qwen/Qwen2.5-7B-Instruct"
TEMPERATURE = 0.3
MAX_TOKENS = 300
RUNS = 5

SYSTEM_PROMPT = (
    "You are Pepper, a robot receptionist at CTU FEE Prague. "
    "Speak briefly and politely in English. "
    "Use query_search to find information. "
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

TOOL_DIRECTIONS = {
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
}

FAKE_RESULTS = {
    "play_animation": lambda a: json.dumps({"body_state": "ready", "posture": a.get("animation", "?")}),
    "query_search": lambda a: json.dumps({
        "query": a.get("query", ""), "count": 1,
        "results": [{"title": "Dean", "content": "Dean phone: +420 224 352 850.", "source": "web", "score": 0.9}],
    }),
    "get_directions_to_room": lambda a: json.dumps({"room": a.get("room_number", "?"), "directions": "Turn left."}),
}

SCENARIO = [
    {"user": "Hello!", "expect": ["play_animation"]},
    {"user": "What is the phone number of the dean?", "expect": ["query_search"]},
    {"user": "Thank you, goodbye!", "expect": ["play_animation"]},
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
                args = json.loads(tc.function.arguments)
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
    leaks = 0
    for run_i in range(RUNS):
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
        all_ok = True
        leaked = False
        details = []
        for step in SCENARIO:
            tc, text = run_turn(client, msgs, step["user"], tools)
            ok = all(t in tc for t in step["expect"])
            if not ok:
                all_ok = False
            leak_kws = ["tool_call", "<tool_call>", "play_animation", "query_search", "function", '{"animation', "body_state"]
            if any(kw in text.lower() for kw in leak_kws):
                leaked = True
            details.append(f"{'->'.join(tc) or 'NONE'}")
        if all_ok:
            passes += 1
        if leaked:
            leaks += 1
        mark = "OK" if all_ok else "FAIL"
        leak_s = " LEAK" if leaked else ""
        print(f"  {label} run{run_i+1}: [{mark}]{leak_s}  {' | '.join(details)}")
    return passes, leaks


def main():
    client = OpenAI(base_url=VLLM_BASE, api_key="not-needed")
    client.models.list()

    configs = [
        ("2_tools", [TOOL_QUERY, TOOL_ANIM]),
        ("3_tools", [TOOL_QUERY, TOOL_ANIM, TOOL_DIRECTIONS]),
    ]

    print(f"Running {RUNS} iterations each, temp={TEMPERATURE}\n")
    results = {}
    for label, tools in configs:
        print(f"\n{'=' * 50}")
        print(f"Config: {label} ({len(tools)} tools)")
        print(f"{'=' * 50}")
        p, l = test_config(client, label, tools)
        results[label] = (p, l)

    print(f"\n{'=' * 50}")
    print("SUMMARY")
    print(f"{'=' * 50}")
    for label, (p, l) in results.items():
        print(f"  {label:12s}  pass={p}/{RUNS} ({p/RUNS*100:.0f}%)  leakage={l}")


if __name__ == "__main__":
    main()
