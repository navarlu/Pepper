"""
Test: merge directions into query_search so we stay at 2 tools.

The LLM just calls query_search("how to get to room 230") and the
tool implementation detects room queries and routes to the map data.

Usage:
  uv run python voice-agent/tests/test_merged_tools.py
"""

import json
import re
from openai import OpenAI

VLLM_BASE = "http://localhost:8000/v1"
MODEL = "Qwen/Qwen2.5-7B-Instruct"
TEMPERATURE = 0.3
MAX_TOKENS = 300
RUNS = 5

SYSTEM_PROMPT = (
    "You are Pepper, a robot receptionist at CTU FEE Prague. "
    "Speak briefly and politely in English. "
    "Use query_search to find information — do not guess. "
    "query_search also knows room locations and walking directions. "
    "Call play_animation to check your body state before every reply. "
    "Never say tool names aloud."
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_search",
            "description": (
                "Search the FEL knowledge base. Also returns room locations and directions."
            ),
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
]


# ── Smart routing in the tool implementation ──────────────────────────────────

ROOM_PATTERN = re.compile(r'\b(\d{3,4}[a-zA-Z]?)\b')

ROOM_DATA = {
    "230": {"floor": "2nd", "name": "Lecture Hall", "directions": "Enter main door, stairs to 2nd floor, turn right, third door on the left."},
    "101": {"floor": "1st", "name": "Lab A", "directions": "Enter main door, ground floor corridor, second door on the right."},
    "301": {"floor": "3rd", "name": "Seminar Room", "directions": "Enter main door, elevator to 3rd floor, turn left, at the end of the corridor."},
}


def fake_query_search(args):
    query = args.get("query", "").strip()

    # Check if this is a room/directions query
    room_match = ROOM_PATTERN.search(query)
    room_keywords = ["room", "direction", "where", "how to get", "find", "navigate", "located", "location", "místnost", "kam"]
    is_room_query = room_match and any(kw in query.lower() for kw in room_keywords)

    if is_room_query:
        room_num = room_match.group(1)
        if room_num in ROOM_DATA:
            room = ROOM_DATA[room_num]
            return json.dumps({
                "query": query,
                "type": "directions",
                "room": room_num,
                "floor": room["floor"],
                "name": room["name"],
                "directions": room["directions"],
            }, ensure_ascii=False)
        else:
            return json.dumps({
                "query": query,
                "type": "directions",
                "error": f"Room {room_num} not found in the building map.",
            }, ensure_ascii=False)

    # Normal knowledge base search
    return json.dumps({
        "query": query,
        "count": 1,
        "results": [{
            "title": "Dean of FEE",
            "content": "The current dean is prof. Mgr. Petr Páta, Ph.D. Phone: +420 224 352 850. Office: Building A, room A-123.",
            "source": "fee-website",
            "score": 0.92,
        }],
    }, ensure_ascii=False)


FAKE_RESULTS = {
    "play_animation": lambda a: json.dumps({"body_state": "ready", "posture": a.get("animation", "?")}),
    "query_search": fake_query_search,
}

SCENARIO = [
    {"user": "Hello!", "expect_tools": ["play_animation"], "expect_text": None, "label": "greeting"},
    {"user": "What is the phone number of the dean?", "expect_tools": ["query_search"], "expect_text": "+420 224 352 850", "label": "query"},
    {"user": "How do I get to room 230?", "expect_tools": ["query_search"], "expect_text": None, "label": "directions"},
    {"user": "Thank you, goodbye!", "expect_tools": ["play_animation"], "expect_text": None, "label": "goodbye"},
]


def run_turn(client, messages, user_text):
    messages.append({"role": "user", "content": user_text})
    tools_called = []
    final_text = ""
    for _ in range(6):
        resp = client.chat.completions.create(
            model=MODEL, messages=messages, tools=TOOLS,
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
                print(f"      TOOL: {name}({json.dumps(args)}) -> {result[:120]}")
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            continue
        final_text = (msg.content or "").strip()
        messages.append({"role": "assistant", "content": final_text})
        break
    return tools_called, final_text


def main():
    client = OpenAI(base_url=VLLM_BASE, api_key="not-needed")
    client.models.list()
    print(f"Model: {MODEL}  temp={TEMPERATURE}  runs={RUNS}")
    print(f"Tools: {[t['function']['name'] for t in TOOLS]} (2 tools only)\n")

    total_pass = 0
    total_leak = 0

    for run_i in range(RUNS):
        print(f"{'=' * 60}")
        print(f"RUN {run_i + 1}/{RUNS}")
        print(f"{'=' * 60}")

        msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
        all_ok = True
        any_leak = False

        for step in SCENARIO:
            print(f"\n  {step['label']}: \"{step['user']}\"")
            tc, text = run_turn(client, msgs, step["user"])

            # Check tool calls
            tool_ok = all(t in tc for t in step["expect_tools"])
            if not tool_ok:
                all_ok = False

            # Check expected text
            text_ok = True
            if step["expect_text"] and step["expect_text"] not in text:
                text_ok = False
                all_ok = False

            # Check leakage
            leak_kws = ["tool_call", "<tool_call>", "play_animation", "query_search", "<|im_start"]
            leaked = any(kw in text for kw in leak_kws)
            if leaked:
                any_leak = True

            status = "OK" if (tool_ok and text_ok and not leaked) else "FAIL"
            print(f"    [{status}] tools={tc} leak={leaked}")
            if text:
                print(f"    text: {text[:120]}")

        if all_ok and not any_leak:
            total_pass += 1
        if any_leak:
            total_leak += 1

    print(f"\n{'=' * 60}")
    print(f"SUMMARY: pass={total_pass}/{RUNS} ({total_pass/RUNS*100:.0f}%)  leakage={total_leak}")
    print(f"{'=' * 60}")


if __name__ == "__main__":
    main()
