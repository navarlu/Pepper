"""
Full scenario test for Pepper agent via Qwen 2.5 7B + vLLM.

Standard OpenAI tool-calling flow with multi-round loop.
play_animation described as returning body state (model needs the result).

Usage:
  uv run python voice-agent/tests/test_agent_scenario.py
"""

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

TOOLS = [
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
]

DEAN_PHONE = "+420 224 352 850"

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
                f"The current dean of the Faculty of Electrical Engineering (FEE/FEL) "
                f"at CTU Prague is prof. Mgr. Petr Páta, Ph.D. "
                f"His office phone number is {DEAN_PHONE}. "
                f"The dean's office is located in Building A, room A-123."
            ),
            "source": "fee-website",
            "score": 0.92,
        }],
    }, ensure_ascii=False),
}

SCENARIO = [
    {
        "user": "Hello!",
        "expect_tools": ["play_animation"],
        "expect_in_text": None,
    },
    {
        "user": "What is the phone number of the dean?",
        "expect_tools": ["query_search"],
        "expect_in_text": DEAN_PHONE,
    },
    {
        "user": "Thank you, goodbye!",
        "expect_tools": ["play_animation"],
        "expect_in_text": None,
    },
]


def run_turn(client, messages, user_text):
    messages.append({"role": "user", "content": user_text})
    tools_called = []
    final_text = ""

    for rnd in range(6):
        print(f"    round {rnd}  msgs={len(messages)}")

        t0 = time.perf_counter()
        response = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            parallel_tool_calls=False,
        )
        elapsed = time.perf_counter() - t0
        msg = response.choices[0].message

        if msg.tool_calls:
            messages.append(msg)
            for tc in msg.tool_calls:
                name = tc.function.name
                args = json.loads(tc.function.arguments)
                tools_called.append(name)
                result = FAKE_TOOL_RESULTS.get(name, lambda a: '{"ok":true}')(args)
                print(f"    TOOL {name}({json.dumps(args)}) -> {result[:120]}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })
            continue

        final_text = (msg.content or "").strip()
        messages.append({"role": "assistant", "content": final_text})
        print(f"    TEXT ({elapsed:.2f}s): {final_text[:200]}")
        break

    return tools_called, final_text


def main():
    client = OpenAI(base_url=VLLM_BASE, api_key="not-needed")
    try:
        client.models.list()
        print("Connected to vLLM")
    except Exception as e:
        print(f"ERROR: {e}")
        raise SystemExit(1)

    print(f"Temperature: {TEMPERATURE}\n")

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    results = []

    for i, step in enumerate(SCENARIO):
        user_text = step["user"]
        expect_tools = step.get("expect_tools") or []
        expect_in_text = step.get("expect_in_text")

        print(f"═══ Step {i+1}: \"{user_text}\" ═══")
        tools_called, final_text = run_turn(client, messages, user_text)

        tool_ok = all(t in tools_called for t in expect_tools)
        if expect_tools:
            status = "PASS" if tool_ok else "FAIL"
            print(f"  tools expected={expect_tools} got={tools_called} [{status}]")

        text_ok = True
        if expect_in_text:
            text_ok = expect_in_text in final_text
            status = "PASS" if text_ok else "FAIL"
            print(f"  text contains \"{expect_in_text}\": [{status}]")

        leakage_keywords = [
            "tool_call", "<tool_call>", "play_animation", "query_search",
            "function", '{"animation', "body_state",
        ]
        leakage = any(kw in final_text.lower() for kw in leakage_keywords)
        if leakage:
            print(f"  WARNING: tool syntax leaked into spoken text!")

        results.append({
            "step": i + 1,
            "tools_ok": tool_ok,
            "text_ok": text_ok,
            "leakage": leakage,
            "tools_called": tools_called,
            "response": final_text[:200],
        })
        print()

    print("═══ SUMMARY ═══")
    all_pass = True
    for r in results:
        passed = r["tools_ok"] and r["text_ok"] and not r["leakage"]
        all_pass = all_pass and passed
        mark = "PASS" if passed else "FAIL"
        print(f"  Step {r['step']}: [{mark}] tools={r['tools_called']} leakage={r['leakage']}")
        print(f"    response: {r['response']}")

    print(f"\nOverall: {'ALL PASS' if all_pass else 'SOME FAILED'}")
    print(f"Settings: temp={TEMPERATURE} model={MODEL}")
    return 0 if all_pass else 1


if __name__ == "__main__":
    raise SystemExit(main())
