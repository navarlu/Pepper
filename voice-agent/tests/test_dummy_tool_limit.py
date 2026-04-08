"""
Standalone A/B test: Does Qwen 2.5 7B break with 3 tools?

Uses completely generic dummy tools (weather, time, math) — no Pepper,
no project context. This isolates the question: is the 2-tool limit
a fundamental model constraint or something specific to our tool definitions?

Tests:
  A) 2 dummy tools  → expect reliable tool calling
  B) 3 dummy tools  → expect degradation (based on prior findings)
  C) 4 dummy tools  → expect worse degradation

Usage:
  uv run python voice-agent/tests/test_dummy_tool_limit.py
"""

import json
import time
from openai import OpenAI

VLLM_BASE = "http://localhost:8000/v1"
MODEL = "Qwen/Qwen2.5-7B-Instruct"
TEMPERATURE = 0.3
MAX_TOKENS = 300
RUNS = 5

# Minimal system prompt — no project specifics
SYSTEM_PROMPT = (
    "You are a helpful assistant. "
    "Use the provided tools whenever relevant to answer the user's question. "
    "Never mention tool names or JSON in your spoken reply."
)

# ── Tool definitions (all simple, all with one string param) ──────────────

TOOL_WEATHER = {
    "type": "function",
    "function": {
        "name": "get_weather",
        "description": (
            "Get the current weather for a city. "
            "Returns temperature, conditions, and humidity."
        ),
        "parameters": {
            "type": "object",
            "properties": {"city": {"type": "string", "description": "City name"}},
            "required": ["city"],
        },
    },
}

TOOL_TIME = {
    "type": "function",
    "function": {
        "name": "get_time",
        "description": (
            "Get the current local time for a timezone. "
            "Returns the time in HH:MM format."
        ),
        "parameters": {
            "type": "object",
            "properties": {"timezone": {"type": "string", "description": "Timezone like Europe/Prague"}},
            "required": ["timezone"],
        },
    },
}

TOOL_TRANSLATE = {
    "type": "function",
    "function": {
        "name": "translate_text",
        "description": (
            "Translate text to a target language. "
            "Returns the translated string."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "text": {"type": "string", "description": "Text to translate"},
                "target_language": {"type": "string", "description": "Target language code like 'cs', 'de', 'fr'"},
            },
            "required": ["text", "target_language"],
        },
    },
}

TOOL_CALCULATE = {
    "type": "function",
    "function": {
        "name": "calculate",
        "description": (
            "Evaluate a math expression. "
            "Returns the numeric result."
        ),
        "parameters": {
            "type": "object",
            "properties": {"expression": {"type": "string", "description": "Math expression like '2+2' or 'sqrt(16)'"}},
            "required": ["expression"],
        },
    },
}

# ── Fake tool results ────────────────────────────────────────────────────

FAKE_RESULTS = {
    "get_weather": lambda a: json.dumps({
        "city": a.get("city", "?"),
        "temperature": "18C",
        "conditions": "partly cloudy",
        "humidity": "62%",
    }),
    "get_time": lambda a: json.dumps({
        "timezone": a.get("timezone", "UTC"),
        "time": "14:35",
    }),
    "translate_text": lambda a: json.dumps({
        "original": a.get("text", ""),
        "translated": "Ahoj, jak se mas?",
        "language": a.get("target_language", "?"),
    }),
    "calculate": lambda a: json.dumps({
        "expression": a.get("expression", ""),
        "result": 42,
    }),
}

# ── Scenarios ────────────────────────────────────────────────────────────

# Each scenario is designed so the user's question clearly maps to one tool.
SCENARIO_2TOOLS = [
    {"user": "What's the weather like in Prague?", "expect": ["get_weather"]},
    {"user": "What time is it in Tokyo?", "expect": ["get_time"]},
    {"user": "And the weather in Berlin?", "expect": ["get_weather"]},
]

SCENARIO_3TOOLS = [
    {"user": "What's the weather like in Prague?", "expect": ["get_weather"]},
    {"user": "What time is it in Tokyo?", "expect": ["get_time"]},
    {"user": "Translate 'hello how are you' to Czech.", "expect": ["translate_text"]},
]

SCENARIO_4TOOLS = [
    {"user": "What's the weather like in Prague?", "expect": ["get_weather"]},
    {"user": "What time is it in Tokyo?", "expect": ["get_time"]},
    {"user": "Translate 'hello' to German.", "expect": ["translate_text"]},
    {"user": "What is 15 times 23?", "expect": ["calculate"]},
]


# ── Test runner ──────────────────────────────────────────────────────────

def safe_parse_args(raw_args: str) -> dict:
    """Parse tool call arguments, handling Qwen's trailing-brace bug."""
    try:
        return json.loads(raw_args)
    except json.JSONDecodeError:
        # Try to extract first balanced JSON object
        depth = 0
        start = raw_args.find("{")
        if start == -1:
            return {}
        for i, ch in enumerate(raw_args[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(raw_args[start : i + 1])
                    except json.JSONDecodeError:
                        return {}
        return {}


def run_turn(client, messages, user_text, tools):
    """Run one user turn, handling the tool-call loop."""
    messages.append({"role": "user", "content": user_text})
    tools_called = []
    final_text = ""

    for _ in range(6):  # max tool-call rounds
        resp = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            tools=tools,
            temperature=TEMPERATURE,
            max_tokens=MAX_TOKENS,
            parallel_tool_calls=False,
        )
        msg = resp.choices[0].message

        if msg.tool_calls:
            messages.append(msg)
            for tc in msg.tool_calls:
                name = tc.function.name
                args = safe_parse_args(tc.function.arguments)
                tools_called.append(name)
                result = FAKE_RESULTS.get(name, lambda a: '{"ok":true}')(args)
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": result})
            continue

        final_text = (msg.content or "").strip()
        messages.append({"role": "assistant", "content": final_text})
        break

    return tools_called, final_text


def test_config(client, label, tools, scenario):
    """Run a full scenario RUNS times and report pass/leak rates."""
    passes = 0
    leaks = 0

    for run_i in range(RUNS):
        msgs = [{"role": "system", "content": SYSTEM_PROMPT}]
        all_ok = True
        leaked = False
        details = []

        for step in scenario:
            tc, text = run_turn(client, msgs, step["user"], tools)
            ok = all(t in tc for t in step["expect"])
            if not ok:
                all_ok = False

            # Check for tool syntax leaking into spoken text
            leak_kws = [
                "tool_call", "<tool_call>", "function", "get_weather",
                "get_time", "translate_text", "calculate", '{"',
                "<|im_start|>", "arguments",
            ]
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

    # Verify vLLM is reachable
    try:
        models = client.models.list()
        print(f"Connected to vLLM. Model: {models.data[0].id}")
    except Exception as e:
        print(f"ERROR: Cannot connect to vLLM at {VLLM_BASE}: {e}")
        print("Make sure vLLM is running with: --enable-auto-tool-choice --tool-call-parser hermes")
        return

    configs = [
        ("2_tools", [TOOL_WEATHER, TOOL_TIME], SCENARIO_2TOOLS),
        ("3_tools", [TOOL_WEATHER, TOOL_TIME, TOOL_TRANSLATE], SCENARIO_3TOOLS),
        ("4_tools", [TOOL_WEATHER, TOOL_TIME, TOOL_TRANSLATE, TOOL_CALCULATE], SCENARIO_4TOOLS),
    ]

    print(f"\nQwen 2.5 7B Tool Count A/B Test (dummy tools)")
    print(f"Runs per config: {RUNS}, temperature: {TEMPERATURE}")
    print(f"All tools are simple, single-param, completely generic.\n")

    results = {}
    for label, tools, scenario in configs:
        print(f"\n{'=' * 60}")
        print(f"Config: {label} ({len(tools)} tools, {len(scenario)} steps)")
        print(f"Tools: {', '.join(t['function']['name'] for t in tools)}")
        print(f"{'=' * 60}")
        p, l = test_config(client, label, tools, scenario)
        results[label] = (p, l)
        time.sleep(1)  # brief pause between configs

    print(f"\n{'=' * 60}")
    print("SUMMARY")
    print(f"{'=' * 60}")
    for label, (p, l) in results.items():
        pct = p / RUNS * 100
        print(f"  {label:12s}  pass={p}/{RUNS} ({pct:3.0f}%)  leakage={l}/{RUNS}")

    print(f"\n{'=' * 60}")
    print("INTERPRETATION")
    print(f"{'=' * 60}")
    p2 = results["2_tools"][0]
    p3 = results["3_tools"][0]
    p4 = results["4_tools"][0]

    if p2 == RUNS and p3 < RUNS:
        print("  CONFIRMED: Qwen 2.5 7B degrades with 3+ tools.")
        print("  This is a model-level limitation, not specific to tool definitions.")
    elif p2 == RUNS and p3 == RUNS and p4 < RUNS:
        print("  Partial: 3 tools OK, but 4 tools causes degradation.")
        print("  The limit may be higher than 2 with simpler tool schemas.")
    elif p2 == RUNS and p3 == RUNS and p4 == RUNS:
        print("  DISPROVED: All configs pass. The issue may be specific to")
        print("  our project's tool definitions, not a general model limit.")
    else:
        print(f"  INCONCLUSIVE: Even 2 tools only passed {p2}/{RUNS}.")
        print("  Check vLLM config, temperature, or system prompt.")


if __name__ == "__main__":
    main()
