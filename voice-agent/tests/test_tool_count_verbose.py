"""
Verbose version: see exactly what Qwen generates with 3 tools.
Also test: is it the 3rd tool's schema size, or just having 3 tools?

Usage:
  uv run python voice-agent/tests/test_tool_count_verbose.py
"""

import json
from openai import OpenAI

VLLM_BASE = "http://localhost:8000/v1"
MODEL = "Qwen/Qwen2.5-7B-Instruct"
TEMPERATURE = 0.3
MAX_TOKENS = 300

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

# A minimal dummy 3rd tool to test if it's the count or the specific tool
TOOL_DUMMY = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": "Get the current time.",
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
}

FAKE_RESULTS = {
    "play_animation": lambda a: json.dumps({"body_state": "ready", "posture": a.get("animation", "?")}),
    "query_search": lambda a: json.dumps({
        "query": a.get("query", ""), "count": 1,
        "results": [{"title": "Dean", "content": "Dean phone: +420 224 352 850.", "source": "web", "score": 0.9}],
    }),
    "get_directions_to_room": lambda a: json.dumps({"room": a.get("room_number"), "directions": "Turn left."}),
    "get_time": lambda a: json.dumps({"time": "14:30"}),
}


def run_turn_verbose(client, messages, user_text, tools):
    messages.append({"role": "user", "content": user_text})
    tools_called = []
    for rnd in range(6):
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
                print(f"      TOOL: {name}({json.dumps(args)})")
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            continue
        text = (msg.content or "").strip()
        messages.append({"role": "assistant", "content": text})
        print(f"      TEXT: {text[:200]}")
        return tools_called, text
    return tools_called, ""


def run_test(client, label, tools):
    print(f"\n{'=' * 60}")
    print(f"  {label} ({len(tools)} tools: {[t['function']['name'] for t in tools]})")
    print(f"{'=' * 60}")

    msgs = [{"role": "system", "content": SYSTEM_PROMPT}]

    print("\n  Step 1: 'Hello!'")
    tc1, t1 = run_turn_verbose(client, msgs, "Hello!", tools)
    s1 = "play_animation" in tc1

    print("\n  Step 2: 'What is the dean phone number?'")
    tc2, t2 = run_turn_verbose(client, msgs, "What is the phone number of the dean?", tools)
    s2 = "query_search" in tc2

    print("\n  Step 3: 'Thank you, goodbye!'")
    tc3, t3 = run_turn_verbose(client, msgs, "Thank you, goodbye!", tools)
    s3 = "play_animation" in tc3

    anim_rate = sum([s1, s3])
    print(f"\n  Result: anim={anim_rate}/2  query={'OK' if s2 else 'FAIL'}")
    return s1, s2, s3


def main():
    client = OpenAI(base_url=VLLM_BASE, api_key="not-needed")
    client.models.list()

    print("Test 1: With get_directions_to_room (the real 3rd tool)")
    run_test(client, "3_real", [TOOL_QUERY, TOOL_ANIM, TOOL_DIRECTIONS])

    print("\n\nTest 2: With get_time (minimal dummy 3rd tool)")
    run_test(client, "3_dummy", [TOOL_QUERY, TOOL_ANIM, TOOL_DUMMY])

    print("\n\nTest 3: With get_directions_to_room but play_animation FIRST in list")
    run_test(client, "3_anim_first", [TOOL_ANIM, TOOL_QUERY, TOOL_DIRECTIONS])

    print("\n\nTest 4: Only play_animation + get_directions_to_room (no query_search)")
    run_test(client, "2_anim_dir", [TOOL_ANIM, TOOL_DIRECTIONS])


if __name__ == "__main__":
    main()
