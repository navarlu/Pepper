"""
Hold-everything-constant A/B test: which `play_pose` schema gets all 3 tools
working reliably?

Holds constant:
  - 3 tools active (search_kb + play_pose variant + get_time)
  - System prompt: ordered ("search first, pose right before reply, time when asked")
  - Sampling: Qwen-recommended (temp=0.7, top_p=0.8, repetition_penalty=1.05)

Varies the play_pose tool definition across 5 variants. Each variant runs
6 scenarios × 5 trials = 30 calls.

Verdicts:
  OK         — expected tool extracted cleanly, text has no leak markers
  OK_DIRTY   — expected tool extracted, but text has leaked tool-call markers
  LEAK_NO_TOOL — text has leak markers AND no tool extracted (real failure)
  NO_TOOL    — should have called a tool, didn't
  WRONG      — called a different tool than expected
"""
import json
import time
from collections import Counter, defaultdict
from openai import OpenAI

VLLM_BASE = "http://localhost:8000/v1"
MODEL = "Qwen/Qwen2.5-7B-Instruct"

# ── Constant: search and time tools ──
TOOL_SEARCH = {
    "type": "function",
    "function": {
        "name": "search_kb",
        "description": (
            "Look up factual information (people, rooms, schedules). "
            "Use whenever the user asks about facts — do not guess."
        ),
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string", "description": "Search query"}},
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

POSE_VALUES = ["greeting", "bow", "explain", "happy", "thinking", "dont_know"]

# ── 5 play_pose variants — only this changes between runs ──

# V0: BASELINE — current production schema
POSE_V0_BASELINE = {
    "type": "function",
    "function": {
        "name": "play_pose",
        "description": (
            "Set the robot body posture before speaking. "
            "Returns the current body state. "
            "pose must be one of: greeting, bow, explain, happy, thinking, dont_know"
        ),
        "parameters": {
            "type": "object",
            "properties": {"pose": {"type": "string", "description": "Body pose name"}},
            "required": ["pose"],
        },
    },
}

# V1: Drop misleading "Returns body state"
POSE_V1_NO_RETURN = {
    "type": "function",
    "function": {
        "name": "play_pose",
        "description": (
            "Set the robot body posture before speaking. "
            "pose must be one of: greeting, bow, explain, happy, thinking, dont_know"
        ),
        "parameters": {
            "type": "object",
            "properties": {"pose": {"type": "string", "description": "Body pose name"}},
            "required": ["pose"],
        },
    },
}

# V2: V1 + proper enum constraint on pose param
POSE_V2_ENUM = {
    "type": "function",
    "function": {
        "name": "play_pose",
        "description": "Set the robot body posture before speaking.",
        "parameters": {
            "type": "object",
            "properties": {
                "pose": {
                    "type": "string",
                    "enum": POSE_VALUES,
                    "description": "Body pose to play",
                },
            },
            "required": ["pose"],
        },
    },
}

# V3: V2 + factual "animate" framing instead of "set posture"
POSE_V3_ANIMATE = {
    "type": "function",
    "function": {
        "name": "play_pose",
        "description": "Animate Pepper with a short body gesture matching the speech tone.",
        "parameters": {
            "type": "object",
            "properties": {
                "pose": {
                    "type": "string",
                    "enum": POSE_VALUES,
                    "description": "Gesture name",
                },
            },
            "required": ["pose"],
        },
    },
}

# V4: V3 + renamed to play_gesture (clearer semantic, no "pose" overload)
POSE_V4_RENAMED = {
    "type": "function",
    "function": {
        "name": "play_gesture",
        "description": "Play a short body gesture animation matching the speech tone.",
        "parameters": {
            "type": "object",
            "properties": {
                "gesture": {
                    "type": "string",
                    "enum": POSE_VALUES,
                    "description": "Gesture name",
                },
            },
            "required": ["gesture"],
        },
    },
}

VARIANTS = {
    "V0_baseline":        POSE_V0_BASELINE,
    "V1_no_return":       POSE_V1_NO_RETURN,
    "V2_enum":            POSE_V2_ENUM,
    "V3_animate_enum":    POSE_V3_ANIMATE,
    "V4_renamed_gesture": POSE_V4_RENAMED,
}


def make_system_prompt(pose_tool_name: str) -> str:
    return (
        "You are Pepper, a brief and polite robot receptionist. "
        "If you need information, call search_kb first. "
        f"Call {pose_tool_name} right before your spoken reply. "
        "Use get_time when asked about the time. "
        "Never say tool names aloud."
    )


SCENARIOS = [
    ("greeting",   "Hello!",                          "POSE"),     # POSE = pose tool, name varies
    ("room_query", "Where is room 230?",              "search_kb"),
    ("dean_query", "What is the dean phone number?",  "search_kb"),
    ("time_query", "What time is it now?",            "get_time"),
    ("chat_only",  "Tell me a joke",                  None),
    ("goodbye",    "Thanks goodbye!",                 "POSE"),
]

LEAK_PATTERNS = ["<tool_call>", "</tool_call>", "<|im_start|>", "<|im_end|>"]
TRIALS = 5

SAMPLING = {"temperature": 0.7, "top_p": 0.8}
EXTRA_BODY = {"repetition_penalty": 1.05}


def run_variant(client, variant_name: str, pose_tool: dict):
    pose_name = pose_tool["function"]["name"]
    expected_map = {"POSE": pose_name, "search_kb": "search_kb", "get_time": "get_time", None: None}
    tools = [TOOL_SEARCH, pose_tool, TOOL_TIME]
    prompt = make_system_prompt(pose_name)
    counters: Counter = Counter()
    leak_examples: list[str] = []

    for scen_name, user_text, expected_key in SCENARIOS:
        expected = expected_map[expected_key]
        for trial in range(TRIALS):
            try:
                resp = client.chat.completions.create(
                    model=MODEL,
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": user_text},
                    ],
                    tools=tools,
                    parallel_tool_calls=False,
                    temperature=SAMPLING["temperature"],
                    top_p=SAMPLING["top_p"],
                    extra_body=EXTRA_BODY,
                    max_tokens=256,
                )
            except Exception as e:
                counters["ERROR"] += 1
                continue
            msg = resp.choices[0].message
            text = (msg.content or "").strip()
            called = msg.tool_calls[0].function.name if msg.tool_calls else None
            leak = any(p in text for p in LEAK_PATTERNS)

            if expected is None:
                verdict = ("OK" if not leak else "OK_DIRTY") if called is None else "WRONG"
            elif called == expected:
                verdict = "OK_DIRTY" if leak else "OK"
            elif called is None:
                verdict = "LEAK_NO_TOOL" if leak else "NO_TOOL"
            else:
                verdict = "WRONG"
            counters[verdict] += 1
            if verdict == "LEAK_NO_TOOL" and len(leak_examples) < 3:
                leak_examples.append(f"{scen_name}: {text[:130]!r}")

    return counters, leak_examples


def main():
    client = OpenAI(base_url=VLLM_BASE, api_key="dummy")

    print(f"Pose-design A/B test against {VLLM_BASE} model={MODEL}")
    print(f"  3 tools active (search_kb + play_pose-variant + get_time)")
    print(f"  6 scenarios × {TRIALS} trials = {6*TRIALS} calls per variant")
    print(f"  sampling: temp={SAMPLING['temperature']} top_p={SAMPLING['top_p']} rep_pen={EXTRA_BODY['repetition_penalty']}")
    print()

    results = {}
    t0 = time.monotonic()
    for name, tool in VARIANTS.items():
        print(f"  running {name}…", flush=True)
        counters, leaks = run_variant(client, name, tool)
        results[name] = (counters, leaks)
        elapsed = time.monotonic() - t0
        print(f"    {dict(counters)} ({elapsed:.0f}s elapsed)")

    print("\n" + "═" * 100)
    print("SUMMARY — pass rates per pose-variant (all 3 tools active, ordered prompt, qwen sampling)")
    print("═" * 100)
    print(f"{'variant':<22s} | {'OK':>4s} | {'DIRTY':>5s} | {'LEAK_NT':>7s} | {'NO_TOOL':>7s} | {'WRONG':>5s} | {'total':>5s} | {'OK%':>6s} | {'OK+DIRTY%':>9s}")
    print("-" * 100)
    rows = []
    for name in VARIANTS:
        c, _ = results[name]
        total = sum(c.values())
        ok = c.get("OK", 0); dirty = c.get("OK_DIRTY", 0)
        leak = c.get("LEAK_NO_TOOL", 0); nt = c.get("NO_TOOL", 0); wr = c.get("WRONG", 0)
        ok_pct = 100*ok/total if total else 0
        works_pct = 100*(ok+dirty)/total if total else 0
        rows.append((name, ok, dirty, leak, nt, wr, total, ok_pct, works_pct))
        print(f"{name:<22s} | {ok:4d} | {dirty:5d} | {leak:7d} | {nt:7d} | {wr:5d} | {total:5d} | {ok_pct:5.1f}% | {works_pct:8.1f}%")

    print("\n" + "═" * 100)
    print("LEAK SAMPLES per variant")
    print("═" * 100)
    for name in VARIANTS:
        _, leaks = results[name]
        print(f"\n  [{name}]")
        if not leaks:
            print("    (no leaks)")
        for ex in leaks:
            print(f"    {ex}")

    # Save raw
    with open("/tmp/tool_pose_design_results.json", "w") as f:
        json.dump({k: dict(v[0]) for k, v in results.items()}, f, indent=2)
    print("\nraw results saved to /tmp/tool_pose_design_results.json")

    # Pick winner
    rows.sort(key=lambda r: -r[7])  # by OK%
    winner = rows[0]
    print(f"\n🏆 WINNER: {winner[0]}  ({winner[7]:.1f}% OK, {winner[8]:.1f}% OK+DIRTY)")


if __name__ == "__main__":
    main()
