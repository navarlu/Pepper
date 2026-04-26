"""
End-to-end multi-turn conversation test with the full production loop:
  - V1 play_pose schema (no "returns body state")
  - play_pose returns None  → skip LLM re-call (matches LiveKit SDK)
  - search_kb returns data  → triggers LLM re-call (so chain works)
  - get_time returns data
  - Ordered system prompt
  - Sampling: temp=0.01, top_p=0.8, repetition_penalty=1.05

Conversation:
  T1: "Hello!"                       → expect: pose(greeting) called, ONE greeting text
  T2: "Where is room 230?"           → expect: search_kb → answer text + pose(explain)
  T3: "What is the dean phone?"      → expect: search_kb → phone number + pose(explain)
  T4: "What time is it?"             → expect: get_time → time text + pose(explain)
  T5: "Tell me a joke"               → expect: text only OR text + pose(happy)
  T6: "Thank you, goodbye!"          → expect: pose(bow), goodbye text

Pass criteria:
  - No leaked tool-call markers in any spoken text
  - No duplicate greetings (greeting text appears at most once per turn)
  - Search-needing queries get answered with the search result content
  - Animation is called for greeting and goodbye
"""
import json
import time
from openai import OpenAI

VLLM_BASE = "http://localhost:8000/v1"
MODEL = "Qwen/Qwen2.5-7B-Instruct"

TOOL_SEARCH = {"type":"function","function":{"name":"search_kb","description":"Look up factual information (people, rooms, schedules). Use whenever the user asks about facts — do not guess.","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}}}
TOOL_TIME = {"type":"function","function":{"name":"get_time","description":"Return the current time of day.","parameters":{"type":"object","properties":{}}}}
# V1 winner — no "Returns the current body state"
TOOL_POSE = {
    "type": "function",
    "function": {
        "name": "play_pose",
        "description": "Set the robot body posture before speaking. pose must be one of: greeting, bow, explain, happy, thinking, dont_know",
        "parameters": {
            "type": "object",
            "properties": {"pose": {"type": "string", "description": "Body pose name"}},
            "required": ["pose"],
        },
    },
}

PROMPT = (
    "You are Pepper, a brief and polite robot receptionist. "
    "If you need information, call search_kb first. "
    "Call play_pose right before your spoken reply. "
    "Use get_time when asked about the time. "
    "Never say tool names aloud."
)

LEAK_PATTERNS = ["<tool_call>", "</tool_call>", "<|im_start|>", "<|im_end|>"]
SAMPLING = {"temperature": 0.01, "top_p": 0.8}
EXTRA_BODY = {"repetition_penalty": 1.05}


_pose_returns_data = False  # toggleable

def execute_tool(name: str, args: dict):
    """Mirror production: pose return is variable (test both), search/time return data."""
    if name == "play_pose":
        if _pose_returns_data:
            return {"ok": True, "pose": args.get("pose", "")}
        return None
    if name == "get_time":
        return {"time": "14:35"}
    if name == "search_kb":
        q = (args.get("query") or "").lower()
        if "room" in q or "230" in q:
            return {"room": "E-230", "floor": 2, "directions": "Take the stairs to the second floor and turn right."}
        if "dean" in q:
            return {"name": "Prof. Doe", "phone": "+420 224 35 1234"}
        if "wifi" in q:
            return {"ssid": "FEL-guest", "password": "ask reception"}
        return {"result": "no info found"}
    return {"error": "unknown_tool"}


def run_turn(client, history, user_text, *, log):
    """Send a user message, run the full SDK-like loop, return the spoken text."""
    history.append({"role": "user", "content": user_text})
    spoken_segments = []
    n_calls = 0
    tools_called = []
    text_with_leaks = []

    while True:
        n_calls += 1
        resp = client.chat.completions.create(
            model=MODEL,
            messages=history,
            tools=[TOOL_SEARCH, TOOL_POSE, TOOL_TIME],
            parallel_tool_calls=False,
            max_tokens=300,
            temperature=SAMPLING["temperature"],
            top_p=SAMPLING["top_p"],
            extra_body=EXTRA_BODY,
        )
        msg = resp.choices[0].message
        text = (msg.content or "").strip()
        leak = any(p in text for p in LEAK_PATTERNS)
        if leak:
            text_with_leaks.append(text)
        if text:
            spoken_segments.append(text)

        # Append assistant message (with tool_calls) to history
        entry = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            entry["tool_calls"] = [
                {"id": tc.id, "type": "function",
                 "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
                for tc in msg.tool_calls
            ]
        history.append(entry)

        if not msg.tool_calls:
            break

        # Execute tools
        all_none = True
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = execute_tool(tc.function.name, args)
            tools_called.append((tc.function.name, args))
            if result is not None:
                all_none = False
            content = "" if result is None else json.dumps(result)
            history.append({"role": "tool", "tool_call_id": tc.id, "name": tc.function.name, "content": content})

        if all_none:
            break  # SDK behavior

        if n_calls >= 4:
            break

    spoken = " ".join(s for s in spoken_segments if s)
    log.append({
        "user": user_text,
        "spoken": spoken,
        "tools_called": tools_called,
        "n_llm_calls": n_calls,
        "leak_in_text": bool(text_with_leaks),
        "leaked_chunks": text_with_leaks,
    })
    return spoken


def check_pass(log_entry, *, expect_tool=None, expect_no_dup=True, expect_keywords=None):
    """Validate a turn against expectations. Returns (ok, reasons)."""
    reasons = []
    if log_entry["leak_in_text"]:
        reasons.append("LEAK in spoken text")
    if expect_tool and not any(t[0] == expect_tool for t in log_entry["tools_called"]):
        reasons.append(f"expected {expect_tool} not called")
    if expect_no_dup:
        spoken = log_entry["spoken"].lower()
        # Detect repeated phrases — split on punctuation, look for repeating sentences
        import re
        sentences = [s.strip() for s in re.split(r"[.!?]\s*", spoken) if s.strip()]
        seen = set()
        for s in sentences:
            sig = re.sub(r"\W+", " ", s).strip()
            if len(sig) > 10 and sig in seen:
                reasons.append(f"DUPLICATE sentence: {s!r}")
                break
            seen.add(sig)
    if expect_keywords:
        for kw in expect_keywords:
            if kw.lower() not in log_entry["spoken"].lower():
                reasons.append(f"missing keyword {kw!r} in spoken text")
    return (not reasons, reasons)


def run_full_conversation(client, *, temperature, label):
    global SAMPLING
    SAMPLING = {"temperature": temperature, "top_p": 0.8}
    history = [{"role": "system", "content": PROMPT}]
    log = []

    print(f"\n{'═'*80}\n{label}  (temp={temperature})\n{'═'*80}")

    turns = [
        ("Hello!",                        {"expect_tool": "play_pose",  "expect_keywords": []}),
        ("Where is room 230?",            {"expect_tool": "search_kb",  "expect_keywords": ["second floor"]}),
        ("What is the dean phone number?",{"expect_tool": "search_kb",  "expect_keywords": ["+420"]}),
        ("What time is it?",              {"expect_tool": "get_time",   "expect_keywords": ["14:35"]}),
        ("Tell me a joke",                {"expect_tool": None,         "expect_keywords": []}),
        ("Thank you, goodbye!",           {"expect_tool": "play_pose",  "expect_keywords": []}),
    ]

    pass_count = 0
    spoke_text_count = 0
    for turn_text, expect in turns:
        spoken = run_turn(client, history, turn_text, log=log)
        entry = log[-1]
        ok, reasons = check_pass(entry, **expect)
        if ok: pass_count += 1
        if spoken: spoke_text_count += 1
        status = "✅" if ok else "❌"
        spoke = "🗣️" if spoken else "  "
        print(f"{status}{spoke} user>{turn_text!r:<35s}  spoken={spoken[:80]!r:<82s}  tools={[t[0] for t in entry['tools_called']]}")
        if reasons:
            for r in reasons:
                print(f"      REASON: {r}")

    print(f"  → tool-pass: {pass_count}/{len(turns)}   text-spoken: {spoke_text_count}/{len(turns)}")
    return pass_count, spoke_text_count


def main():
    global _pose_returns_data
    client = OpenAI(base_url=VLLM_BASE, api_key="dummy")
    print(f"Multi-turn 2-D sweep against {VLLM_BASE} model={MODEL}")
    print(f"  V1 schema, top_p=0.8, rep_pen=1.05")

    summary = []
    for temp in [0.01, 0.3, 0.7]:
        for pose_data in [False, True]:
            _pose_returns_data = pose_data
            label = f"  temp={temp}  pose_returns={'DATA' if pose_data else 'None'}"
            pc, sc = run_full_conversation(client, temperature=temp, label=label)
            summary.append((temp, "DATA" if pose_data else "None", pc, sc))

    print("\n" + "═"*80)
    print("FINAL 2-D SWEEP SUMMARY")
    print("═"*80)
    print(f"{'temp':<6s} | {'pose':<6s} | {'tool-pass':<9s} | {'text-spoken':<11s}")
    for t, pd, pc, sc in summary:
        print(f"{t:<6} | {pd:<6s} | {pc}/6      | {sc}/6")


if __name__ == "__main__":
    main()
