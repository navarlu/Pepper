"""
Systematic A/B test for Qwen 2.5 7B + vLLM tool calling.

Tests 1, 2, and 3-tool configurations across multiple scenarios with
multiple trials each, to determine empirically whether tool count or
tool definition is the primary cause of parser failures.

Also sweeps key sampling parameters (temperature, top_p, repetition_penalty)
to find the most stable configuration.

Failure detection per response:
  - "leak": text contains residual tool-call markers (<tool_call>, </tool_call>,
    <|im_start|>, etc.)
  - "no_tool_when_needed": scenario expected a tool call but none was made
  - "wrong_tool": scenario expected tool X but Y was called
  - "json_error": vLLM returned an HTTP error (parser exception)
  - "ok": tool extracted cleanly, no leakage, expected behavior

Scenarios are designed so EACH ONE has a deterministic expected tool:
  - "Hello!" → play_pose (only tool that makes sense)
  - "Where is room 230?" → search_kb (info needed)
  - "What time is it?" → get_time (when get_time tool is available)
  - "Tell me a joke" → no tool (chat-only)

The test evaluates ONE LLM call per scenario (no full multi-turn loop) so
we cleanly isolate "did Qwen produce a clean structured tool call given
this prompt + tools, or did it leak?"

Usage:
  python voice-agent/tests/tool_systematic_test.py
"""
import json
import re
import time
import traceback
from collections import Counter, defaultdict
from typing import Any
from openai import OpenAI

VLLM_BASE = "http://localhost:8000/v1"
MODEL = "Qwen/Qwen2.5-7B-Instruct"

# ── Three completely independent dummy tools (mirroring play_pose / search_kb / time)
TOOL_POSE = {
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

ALL_TOOLS = {"pose": TOOL_POSE, "search": TOOL_SEARCH, "time": TOOL_TIME}

# ── Tool combinations to test
TOOL_COMBOS = [
    ("solo_pose",   ["pose"]),
    ("solo_search", ["search"]),
    ("solo_time",   ["time"]),
    ("pair_pose+search", ["pose", "search"]),
    ("pair_pose+time",   ["pose", "time"]),
    ("pair_search+time", ["search", "time"]),
    ("trio_pose+search+time", ["pose", "search", "time"]),
]

# ── Scenarios with expected tool (or None = no-tool reply expected)
SCENARIOS = [
    ("greeting",     "Hello!",                     "pose"),    # only pose makes sense for greetings
    ("room_query",   "Where is room 230?",         "search"),  # info question → search
    ("dean_query",   "What is the dean phone number?", "search"),
    ("time_query",   "What time is it now?",       "time"),    # only valid if time tool present
    ("chat_only",    "Tell me a joke",             None),      # no tool needed
    ("goodbye",      "Thanks goodbye!",            "pose"),    # bow gesture
]

# ── Prompts: built DYNAMICALLY from the active tool subset, so we never
# mention a tool name that isn't actually registered (Qwen hallucinates
# <tool_call> for prompt-mentioned-but-unregistered tools).
def make_prompt(prompt_style: str, active_keys: list[str]) -> str:
    """Build a system prompt that ONLY mentions tools in `active_keys`."""
    base = "You are Pepper, a brief and polite robot receptionist."
    instructions: list[str] = []
    for key in active_keys:
        if key == "search":
            instructions.append("Call search_kb whenever the user asks about facts (people, rooms, schedules) — do not guess.")
        elif key == "pose":
            instructions.append("Call play_pose right before your spoken reply to animate the body.")
        elif key == "time":
            instructions.append("Call get_time when the user asks about the time.")

    if prompt_style == "minimal":
        # Just the base + one-liner per tool
        return base + " " + " ".join(instructions) + " Never say tool names aloud."

    if prompt_style == "ordered":
        # Same content but enforce ordering for chains
        ordered = []
        if "search_kb" in [TOOL_SEARCH["function"]["name"]] and "search" in active_keys:
            ordered.append("If you need information, call search_kb first.")
        if "pose" in active_keys:
            ordered.append("Call play_pose right before your spoken reply.")
        if "time" in active_keys:
            ordered.append("Use get_time when asked about the time.")
        return base + " " + " ".join(ordered) + " Never say tool names aloud."

    return base


PROMPT_VARIANTS = ["minimal", "ordered"]

# ── Sampling configurations to A/B (Qwen-recommended vs low-temp baseline)
SAMPLING_VARIANTS = {
    "qwen_official":  {"temperature": 0.7, "top_p": 0.8, "repetition_penalty": 1.05},
    "low_temp":       {"temperature": 0.3, "top_p": None, "repetition_penalty": None},
}

TRIALS_PER = 4                # number of repetitions per (combo × scenario × prompt × sampling)
LEAK_PATTERNS = ["<tool_call>", "</tool_call>", "<|im_start|>", "<|im_end|>", '"name":']


def detect_leak(text: str | None) -> bool:
    if not text:
        return False
    return any(p in text for p in LEAK_PATTERNS)


def evaluate(client, *, tools, system_prompt, sampling, scenario_text):
    """Single shot: send (system + user), return one of:
       'ok'           — tool call extracted, no leak in text
       'ok_no_tool'   — no tool call (might be valid or not depending on scenario)
       'leak'         — tool syntax leaked in text content
       'http_error'   — vLLM returned HTTP error
    Plus the actual tool name called (or None) and text preview.
    """
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": scenario_text},
    ]
    kwargs: dict[str, Any] = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": 256,
        "temperature": sampling["temperature"],
    }
    if sampling.get("top_p") is not None:
        kwargs["top_p"] = sampling["top_p"]
    extra_body = {}
    if sampling.get("repetition_penalty") is not None:
        extra_body["repetition_penalty"] = sampling["repetition_penalty"]
    if extra_body:
        kwargs["extra_body"] = extra_body
    if tools:
        kwargs["tools"] = tools
        kwargs["parallel_tool_calls"] = False

    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception as e:
        return "http_error", None, str(e)[:120]

    msg = resp.choices[0].message
    text = (msg.content or "").strip()
    tool_called = None
    if msg.tool_calls:
        tool_called = msg.tool_calls[0].function.name

    if detect_leak(text):
        return "leak", tool_called, text[:160]
    if tool_called:
        return "ok", tool_called, text[:160]
    return "ok_no_tool", None, text[:160]


def run_grid():
    client = OpenAI(base_url=VLLM_BASE, api_key="dummy")
    results = []  # list of dict rows
    total_calls = 0

    print(f"systematic test against {VLLM_BASE} model={MODEL}")
    print(f"trials per cell = {TRIALS_PER}")
    print(f"combos = {len(TOOL_COMBOS)}, scenarios = {len(SCENARIOS)}, "
          f"prompts = {len(PROMPT_VARIANTS)}, sampling = {len(SAMPLING_VARIANTS)}")
    print(f"total LLM calls = {len(TOOL_COMBOS) * len(SCENARIOS) * len(PROMPT_VARIANTS) * len(SAMPLING_VARIANTS) * TRIALS_PER}")
    print()

    t0 = time.monotonic()
    for combo_name, combo in TOOL_COMBOS:
        tools = [ALL_TOOLS[k] for k in combo]
        active_tool_keys = set(combo)

        for scenario_name, scenario_text, expected_tool in SCENARIOS:
            # Skip scenarios whose expected tool isn't even available in this combo
            if expected_tool and expected_tool not in active_tool_keys:
                continue

            for prompt_name in PROMPT_VARIANTS:
                prompt = make_prompt(prompt_name, combo)
                for sampling_name, sampling in SAMPLING_VARIANTS.items():
                    for trial in range(TRIALS_PER):
                        total_calls += 1
                        outcome, called, preview = evaluate(
                            client,
                            tools=tools,
                            system_prompt=prompt,
                            sampling=sampling,
                            scenario_text=scenario_text,
                        )
                        # Verdict logic
                        if outcome == "leak":
                            verdict = "LEAK"
                        elif outcome == "http_error":
                            verdict = "ERROR"
                        elif expected_tool is None:
                            verdict = "OK" if called is None else f"WRONG({called})"
                        elif called == expected_tool:
                            verdict = "OK"
                        elif called is None:
                            verdict = "NO_TOOL"
                        else:
                            verdict = f"WRONG({called})"

                        results.append({
                            "combo": combo_name,
                            "scenario": scenario_name,
                            "prompt": prompt_name,
                            "sampling": sampling_name,
                            "trial": trial,
                            "verdict": verdict,
                            "called": called,
                            "preview": preview,
                        })
        elapsed = time.monotonic() - t0
        print(f"  [{combo_name:30s}] elapsed={elapsed:5.0f}s  cumulative_calls={total_calls}")

    return results


def report(results):
    print("\n" + "═" * 80)
    print("SUMMARY — pass rate by (tool_combo × sampling), all prompts/scenarios pooled")
    print("═" * 80)

    # Aggregate by combo+sampling
    agg = defaultdict(Counter)
    for r in results:
        agg[(r["combo"], r["sampling"])][r["verdict"]] += 1

    samplings = list(SAMPLING_VARIANTS)
    print(f"{'combo':<30s} | " + " | ".join(f"{s:^16s}" for s in samplings))
    print("-" * (30 + 3 + (16 + 3) * len(samplings)))
    for combo_name, _ in TOOL_COMBOS:
        cells = []
        for s in samplings:
            counts = agg[(combo_name, s)]
            total = sum(counts.values())
            ok = counts.get("OK", 0)
            leaks = counts.get("LEAK", 0)
            no_tool = counts.get("NO_TOOL", 0)
            errs = counts.get("ERROR", 0)
            wrongs = sum(v for k, v in counts.items() if k.startswith("WRONG"))
            if total == 0:
                cells.append(f"{'(empty)':^16s}")
            else:
                pct = 100 * ok / total
                bad = total - ok
                cells.append(f"{pct:5.1f}% ({ok}/{total}, L{leaks} N{no_tool} W{wrongs} E{errs})".center(16))
        print(f"{combo_name:<30s} | " + " | ".join(cells))

    print("\n  Legend: L=leak, N=no_tool_when_needed, W=wrong_tool, E=http_error")

    print("\n" + "═" * 80)
    print("BREAKDOWN — per scenario, best-sampling × best-prompt config")
    print("═" * 80)
    # Find configurations that achieve highest pass rate per scenario
    by_combo_scen = defaultdict(lambda: defaultdict(Counter))
    for r in results:
        by_combo_scen[(r["combo"], r["scenario"])][(r["prompt"], r["sampling"])][r["verdict"]] += 1

    for combo_name, _ in TOOL_COMBOS:
        for scen_name, _, _ in SCENARIOS:
            cell = by_combo_scen[(combo_name, scen_name)]
            if not cell:
                continue
            # Best (prompt, sampling) by OK count
            best_key, best_counts = max(
                cell.items(),
                key=lambda kv: (kv[1].get("OK", 0), -kv[1].get("LEAK", 0)),
            )
            ok = best_counts.get("OK", 0)
            total = sum(best_counts.values())
            print(f"  {combo_name:<30s} | {scen_name:<14s} | best={best_key[0]:<11s}+{best_key[1]:<14s} | {ok}/{total}")

    print("\n" + "═" * 80)
    print("LEAK SAMPLES (first 5)")
    print("═" * 80)
    leaks = [r for r in results if r["verdict"] == "LEAK"][:5]
    for r in leaks:
        print(f"  combo={r['combo']:<25s} scen={r['scenario']:<10s} prompt={r['prompt']:<11s} samp={r['sampling']:<14s}")
        print(f"    text: {r['preview']!r}")


def main():
    results = run_grid()
    report(results)
    # Dump raw JSON next to the script for offline inspection
    out_path = "/tmp/tool_systematic_results.json"
    with open(out_path, "w") as f:
        json.dump(results, f, indent=2)
    print(f"\nraw results saved to {out_path}  (total rows: {len(results)})")


if __name__ == "__main__":
    main()
