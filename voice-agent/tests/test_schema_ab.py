"""
A/B test: LiveKit-generated schemas vs hand-crafted clean schemas.

Runs the same 3-step scenario N times with each schema set and compares
tool-calling reliability. This isolates whether the Pydantic 'title' fields
in LiveKit schemas hurt Qwen's tool-calling behavior.

Usage:
  uv run python voice-agent/tests/test_schema_ab.py [--runs 5]
"""

import argparse
import json
import time
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

# ── Schema Set A: Clean hand-crafted (known working) ─────────────────────────

TOOLS_CLEAN = [
    {
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
    },
    {
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
    },
    {
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
]

# ── Schema Set B: LiveKit-style with Pydantic titles ─────────────────────────

TOOLS_PYDANTIC = [
    {
        "type": "function",
        "function": {
            "name": "query_search",
            "description": "Search the internal FEL knowledge base.",
            "parameters": {
                "title": "QuerySearchArgs",
                "type": "object",
                "properties": {
                    "query": {"title": "Query", "type": "string"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_animation",
            "description": (
                "Check and set the robot body posture. "
                "Returns the current body state which you need before speaking. "
                "animation must be one of: greeting, bow, explain, happy, thinking, dont_know"
            ),
            "parameters": {
                "title": "PlayAnimationLocalArgs",
                "type": "object",
                "properties": {
                    "animation": {"title": "Animation", "type": "string"}
                },
                "required": ["animation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_directions_to_room",
            "description": (
                "Get directions on how to walk to a specific room in Building E. "
                "Call this whenever a visitor asks where a room is or how to get there. "
                "Returns step-by-step walking directions from the main entrance."
            ),
            "parameters": {
                "title": "GetDirectionsToRoomArgs",
                "type": "object",
                "properties": {
                    "room_number": {"title": "Room Number", "type": "string"}
                },
                "required": ["room_number"],
            },
        },
    },
]

# ── Schema Set C: Streaming mode test ────────────────────────────────────────
# Same as clean but we'll test with stream=True to see if streaming matters

FAKE_TOOL_RESULTS = {
    "play_animation": lambda args: json.dumps(
        {"body_state": "ready", "posture": args.get("animation", "?")}
    ),
    "query_search": lambda args: json.dumps({
        "query": args.get("query", ""),
        "count": 1,
        "results": [{
            "title": "Dean of FEE",
            "content": (
                "The current dean of the Faculty of Electrical Engineering (FEE/FEL) "
                "at CTU Prague is prof. Mgr. Petr Páta, Ph.D. "
                "His office phone number is +420 224 352 850."
            ),
            "source": "fee-website",
            "score": 0.92,
        }],
    }, ensure_ascii=False),
    "get_directions_to_room": lambda args: json.dumps({
        "room": args.get("room_number", "?"),
        "floor": "3rd",
        "directions": "Take the elevator to 3rd floor, turn left.",
    }),
}

SCENARIO = [
    {"user": "Hello!", "expect_tools": ["play_animation"]},
    {"user": "What is the phone number of the dean?", "expect_tools": ["query_search"]},
    {"user": "Thank you, goodbye!", "expect_tools": ["play_animation"]},
]


def run_turn(client, messages, user_text, tools, stream=False):
    messages.append({"role": "user", "content": user_text})
    tools_called = []
    final_text = ""

    for rnd in range(6):
        if stream:
            # Streaming mode — collect chunks
            chunks = list(client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=tools,
                temperature=TEMPERATURE,
                max_tokens=MAX_TOKENS,
                parallel_tool_calls=False,
                stream=True,
            ))
            # Reconstruct message from chunks
            content_parts = []
            tool_calls_map = {}
            for chunk in chunks:
                delta = chunk.choices[0].delta if chunk.choices else None
                if not delta:
                    continue
                if delta.content:
                    content_parts.append(delta.content)
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_map:
                            tool_calls_map[idx] = {
                                "id": tc.id or "",
                                "name": "",
                                "arguments": "",
                            }
                        if tc.id:
                            tool_calls_map[idx]["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                tool_calls_map[idx]["name"] = tc.function.name
                            if tc.function.arguments:
                                tool_calls_map[idx]["arguments"] += tc.function.arguments

            content = "".join(content_parts)

            if tool_calls_map:
                # Build assistant message with tool calls
                tc_list = []
                for idx in sorted(tool_calls_map):
                    tc_data = tool_calls_map[idx]
                    tc_list.append({
                        "id": tc_data["id"],
                        "type": "function",
                        "function": {
                            "name": tc_data["name"],
                            "arguments": tc_data["arguments"],
                        },
                    })
                messages.append({
                    "role": "assistant",
                    "content": content or None,
                    "tool_calls": tc_list,
                })
                for tc_data in [tool_calls_map[i] for i in sorted(tool_calls_map)]:
                    name = tc_data["name"]
                    try:
                        args = json.loads(tc_data["arguments"])
                    except json.JSONDecodeError:
                        args = {}
                    tools_called.append(name)
                    result = FAKE_TOOL_RESULTS.get(name, lambda a: '{"ok":true}')(args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc_data["id"],
                        "content": result,
                    })
                continue
            else:
                final_text = content.strip()
                messages.append({"role": "assistant", "content": final_text})
                break
        else:
            # Non-streaming mode
            response = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                tools=tools,
                temperature=TEMPERATURE,
                max_tokens=MAX_TOKENS,
                parallel_tool_calls=False,
            )
            msg = response.choices[0].message

            if msg.tool_calls:
                messages.append(msg)
                for tc in msg.tool_calls:
                    name = tc.function.name
                    args = json.loads(tc.function.arguments)
                    tools_called.append(name)
                    result = FAKE_TOOL_RESULTS.get(name, lambda a: '{"ok":true}')(args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result,
                    })
                continue

            final_text = (msg.content or "").strip()
            messages.append({"role": "assistant", "content": final_text})
            break

    return tools_called, final_text


def run_scenario(client, tools, label, stream=False):
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    step_results = []
    for step in SCENARIO:
        tools_called, text = run_turn(client, messages, step["user"], tools, stream=stream)
        expected = step["expect_tools"]
        ok = all(t in tools_called for t in expected)

        leakage_keywords = [
            "tool_call", "<tool_call>", "play_animation", "query_search",
            "function", '{"animation', "body_state",
        ]
        leakage = any(kw in text.lower() for kw in leakage_keywords)

        step_results.append({
            "ok": ok,
            "leakage": leakage,
            "tools_called": tools_called,
            "text": text[:100],
        })
    return step_results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    args = parser.parse_args()

    client = OpenAI(base_url=VLLM_BASE, api_key="not-needed")
    try:
        client.models.list()
    except Exception as e:
        print(f"Cannot connect to vLLM: {e}")
        raise SystemExit(1)

    configs = [
        ("A_clean",          TOOLS_CLEAN,    False),
        ("B_pydantic",       TOOLS_PYDANTIC, False),
        # Streaming tests disabled — vLLM 400 on malformed Qwen JSON in history
        # ("C_clean_stream",   TOOLS_CLEAN,    True),
        # ("D_pydantic_stream", TOOLS_PYDANTIC, True),
    ]

    totals = {label: {"pass": 0, "fail": 0, "leakage": 0} for label, _, _ in configs}

    for run_i in range(args.runs):
        print(f"\n{'=' * 60}")
        print(f"RUN {run_i + 1}/{args.runs}")
        print(f"{'=' * 60}")

        for label, tools, stream in configs:
            t0 = time.perf_counter()
            results = run_scenario(client, tools, label, stream=stream)
            elapsed = time.perf_counter() - t0

            all_ok = all(r["ok"] for r in results)
            any_leak = any(r["leakage"] for r in results)

            totals[label]["pass" if all_ok else "fail"] += 1
            if any_leak:
                totals[label]["leakage"] += 1

            status = "PASS" if all_ok else "FAIL"
            leak_str = " LEAKAGE!" if any_leak else ""
            tools_summary = " | ".join(
                f"step{i+1}:{','.join(r['tools_called']) or 'NONE'}"
                for i, r in enumerate(results)
            )
            print(f"  {label:20s} [{status}]{leak_str}  ({elapsed:.1f}s)  {tools_summary}")

    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")
    for label, _, _ in configs:
        t = totals[label]
        total = t["pass"] + t["fail"]
        rate = (t["pass"] / total * 100) if total else 0
        print(f"  {label:20s}  pass={t['pass']}/{total} ({rate:.0f}%)  leakage={t['leakage']}")


if __name__ == "__main__":
    main()
