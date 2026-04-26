"""
Pre-production test for the new `lookup_fel_person` tool (UDB scraper)
running on the LOCAL Qwen 2.5 7B agent.

What we're checking, copying the pattern from tool_multiturn_test.py:
  1. Does Qwen call lookup_fel_person reliably when a visitor names a person?
  2. Does it survive alongside the existing production tools (query_search,
     play_pose)?
  3. Does the verbose Field description / docstring trigger <tool_call>
     leakage on the hermes parser?
  4. Does the verbose test_agent SYSTEM_PROMPT leak (we already know long
     multi-line prompts do — testing the minimal variant too)?
  5. Multi-turn end-to-end: greet -> visitor names someone -> lookup ->
     disambiguate -> answer.

We hit UDB live (this network has been verified) so the LLM sees the
actual tool output shape, not a mock.

Run from project root:
    uv run python voice-agent/tests/tool_lookup_fel_test.py
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Make voice-agent/src importable so we can call the real udb.py
HERE = Path(__file__).resolve().parent
SRC_DIR = HERE.parent / "src"
sys.path.insert(0, str(SRC_DIR))

from openai import OpenAI

from udb import lookup_person, NotOnCzvutNetworkError  # noqa: E402

VLLM_BASE = "http://localhost:8000/v1"
MODEL = "Qwen/Qwen2.5-7B-Instruct"

LEAK_PATTERNS = ["<tool_call>", "</tool_call>", "<|im_start|>", "<|im_end|>"]
SAMPLING = {"temperature": 0.01, "top_p": 0.8}
EXTRA_BODY = {"repetition_penalty": 1.05}

# ───────────────────────────────────────────────────────────────────────
# Tool schemas (what the LLM sees)
# ───────────────────────────────────────────────────────────────────────

# A: Verbose schema as written in test_agent/tools.py — the full Field
#    description copy-pasted.
TOOL_LOOKUP_VERBOSE = {
    "type": "function",
    "function": {
        "name": "lookup_fel_person",
        "description": (
            "Look up contact info for a person at FEL ČVUT by name. "
            "Queries the FEL university directory (UDB) and returns every "
            "profile that matches the given surname, each including name, "
            "email, phone, room, and department. If multiple people share a "
            "surname, all of them are returned — ask the visitor for a "
            "first name or department to pick the right one."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": (
                        "Name of the FEL staff member to look up. Surname "
                        "alone works (e.g. 'Hoffmann'); full name is better "
                        "for disambiguation (e.g. 'Matej Hoffmann'). Must "
                        "include a full surname word — prefixes like 'Hoff' "
                        "return nothing."
                    ),
                },
            },
            "required": ["name"],
        },
    },
}

# B: Minimal schema — same name & param, much shorter strings. We've
#    learned long descriptions correlate with Qwen leakage, so test both.
TOOL_LOOKUP_MIN = {
    "type": "function",
    "function": {
        "name": "lookup_fel_person",
        "description": "Look up FEL staff contact info by surname or full name.",
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Person's surname or full name.",
                },
            },
            "required": ["name"],
        },
    },
}

# Production-style sibling tools so we can also test mixed-tool scenarios.
TOOL_SEARCH = {
    "type": "function",
    "function": {
        "name": "query_search",
        "description": "Look up factual information (rooms, schedules, FEE info).",
        "parameters": {
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
        },
    },
}
TOOL_POSE = {
    "type": "function",
    "function": {
        "name": "play_pose",
        "description": (
            "Set the robot body posture before speaking. pose must be one "
            "of: greeting, bow, explain, happy, thinking, dont_know"
        ),
        "parameters": {
            "type": "object",
            "properties": {"pose": {"type": "string", "description": "Body pose name"}},
            "required": ["pose"],
        },
    },
}

# ───────────────────────────────────────────────────────────────────────
# System prompts (what we put in role=system)
# ───────────────────────────────────────────────────────────────────────

# A: The exact prod-style minimal prompt (4 imperative sentences, no LF).
PROMPT_MIN_LOOKUP_ONLY = (
    "You are Pepper, a robot receptionist at FEL ČVUT. "
    "When a visitor names someone they want to see, call lookup_fel_person with that name. "
    "If the tool returns multiple matches, ask which one. "
    "Be brief and polite."
)

PROMPT_MIN_THREE_TOOLS = (
    "You are Pepper, a robot receptionist at FEL ČVUT. "
    "When a visitor names a staff member, call lookup_fel_person with that name. "
    "For other facts, call query_search. "
    "Always call play_pose before speaking. "
    "Be brief and polite."
)

# B: The full verbose multi-line prompt that test_agent shipped before
# being merged into production. Kept here verbatim so this test still
# exercises the "long multi-line prompt" leak risk.
PROMPT_VERBOSE = """You are Pepper, the reception robot at the Faculty of Electrical Engineering (FEE), Czech Technical University in Prague (CTU / ČVUT). You greet visitors and help them find the person they are here to see.

Tone:
- Warm, brief, professional. One or two sentences per reply.
- Mirror the visitor's language (English or Czech). Transliterate names if needed.

Tool use — lookup_fel_person:
- Whenever the visitor names a staff member they want to see, CALL lookup_fel_person with that name. Do not guess contact details from memory.
- Pass a FULL surname. Prefixes like "Hoff" return nothing; use the whole word ("Hoffmann").
- Diacritics do not matter — "Novak" and "Novák" behave the same.
- If the tool returns count > 1, ASK the visitor a disambiguating question (first name, department, role) before reading out any contact info. Never silently pick the first match.
- If count == 1, confirm the person and offer the most useful info first (room / office, then email or phone if asked).
- If any field is null (phone, email, room), say so honestly instead of inventing a fallback — e.g. "I have no phone on file for them, but you can email them at …".
- If status == "not_found", DO NOT give up immediately. Try lookup_fel_person AGAIN with 1 or 2 plausible alternative spellings (STT slip patterns: doubled letters, missing diacritics, "-ová" suffix, palatalized endings). After up to 2 alternate attempts, if all still return not_found, apologise briefly and ask the visitor to spell the surname or give the department.

General rules:
- Never echo raw JSON from the tool — speak the result in natural language.
- If the visitor just chats (greeting, small talk, asking what FEE is), answer normally without calling any tool.
- Keep the conversation moving toward helping them find or contact the right person.
"""


# ───────────────────────────────────────────────────────────────────────
# Tool execution
# ───────────────────────────────────────────────────────────────────────

def execute_tool(name: str, args: dict):
    """Execute a tool call and return the JSON-serializable result."""
    if name == "lookup_fel_person":
        try:
            return lookup_person(args.get("name", ""))
        except NotOnCzvutNetworkError as e:
            return {"status": "error", "error": "off_network", "message": str(e)}
        except Exception as e:
            return {"status": "error", "error": "fetch_failed", "message": str(e)}
    if name == "play_pose":
        return None  # production behavior: no re-call
    if name == "query_search":
        q = (args.get("query") or "").lower()
        if "230" in q or "room" in q:
            return {"room": "E-230", "directions": "Second floor, turn right."}
        if "wifi" in q:
            return {"ssid": "FEL-guest"}
        return {"result": "no info"}
    return {"error": "unknown_tool"}


def trim_tool_result(result):
    """UDB results are huge — trim before sending back to LLM (the agent
    code passes them whole; we mirror that, but log only summaries)."""
    return result


# ───────────────────────────────────────────────────────────────────────
# Loop runner
# ───────────────────────────────────────────────────────────────────────

def run_turn(client, history, user_text, tools, *, max_calls=4):
    """One user turn through the SDK-like tool loop. Returns spoken text,
    list of (tool_name, args), and any leaked text chunks."""
    history.append({"role": "user", "content": user_text})
    spoken_segments, tools_called, leaks = [], [], []

    for _ in range(max_calls):
        resp = client.chat.completions.create(
            model=MODEL,
            messages=history,
            tools=tools,
            parallel_tool_calls=False,
            max_tokens=400,
            temperature=SAMPLING["temperature"],
            top_p=SAMPLING["top_p"],
            extra_body=EXTRA_BODY,
        )
        msg = resp.choices[0].message
        text = (msg.content or "").strip()
        if any(p in text for p in LEAK_PATTERNS):
            leaks.append(text)
        if text:
            spoken_segments.append(text)

        entry = {"role": "assistant", "content": msg.content}
        if msg.tool_calls:
            entry["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]
        history.append(entry)

        if not msg.tool_calls:
            break

        all_none = True
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = execute_tool(tc.function.name, args)
            tools_called.append((tc.function.name, args, result))
            if result is not None:
                all_none = False
            content = "" if result is None else json.dumps(trim_tool_result(result))
            history.append(
                {
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "name": tc.function.name,
                    "content": content,
                }
            )
        if all_none:
            break

    spoken = " ".join(s for s in spoken_segments if s)
    return spoken, tools_called, leaks


def fmt_tool_summary(tools_called):
    out = []
    for name, args, result in tools_called:
        if name == "lookup_fel_person":
            count = result.get("count") if isinstance(result, dict) else "?"
            status = result.get("status") if isinstance(result, dict) else "?"
            out.append(f"{name}({args.get('name')!r})→{status}/{count}")
        else:
            out.append(f"{name}({args!r})")
    return ", ".join(out)


# ───────────────────────────────────────────────────────────────────────
# Scenarios
# ───────────────────────────────────────────────────────────────────────

def run_scenario(label, prompt, tools, conversation, *, expectations=None):
    """A single labelled scenario. Returns dict with stats."""
    expectations = expectations or {}
    print(f"\n{'═'*88}\n{label}\n{'═'*88}")
    print(f"  prompt: len={len(prompt)} newlines={prompt.count(chr(10))}")
    print(f"  tools : {[t['function']['name'] for t in tools]}")

    client = OpenAI(base_url=VLLM_BASE, api_key="dummy")
    history = [{"role": "system", "content": prompt}]
    leaks_total = 0
    tool_pass = 0
    keyword_pass = 0
    n = len(conversation)

    for i, user_text in enumerate(conversation):
        exp = expectations.get(i, {})
        spoken, tools_called, leaks = run_turn(client, history, user_text, tools)
        leak_mark = "🟥" if leaks else "  "
        leaks_total += int(bool(leaks))

        # tool expectation
        expect_tool = exp.get("expect_tool")
        if expect_tool is None:
            tool_ok = True  # don't care
        else:
            tool_ok = any(t[0] == expect_tool for t in tools_called)
        tool_mark = "✅" if tool_ok else "❌"
        if tool_ok:
            tool_pass += 1

        # keyword expectation
        expect_kw = exp.get("expect_kw") or []
        kw_missing = [kw for kw in expect_kw if kw.lower() not in spoken.lower()]
        kw_ok = not kw_missing
        kw_mark = "🔤" if expect_kw and kw_ok else ("🛑" if expect_kw else "  ")
        if kw_ok:
            keyword_pass += 1

        print(
            f"  {leak_mark}{tool_mark}{kw_mark} user>{user_text!r:<48s}"
            f"\n        spoken={spoken[:120]!r}"
            f"\n        tools=[{fmt_tool_summary(tools_called)}]"
        )
        if kw_missing:
            print(f"        MISSING_KW={kw_missing}")
        if leaks:
            print(f"        LEAK_CHUNK={leaks[0][:200]!r}")

    print(f"  → leaks: {leaks_total}/{n}   tool-pass: {tool_pass}/{n}   kw-pass: {keyword_pass}/{n}")
    return {"leaks": leaks_total, "tool_pass": tool_pass, "kw_pass": keyword_pass, "n": n}


# ───────────────────────────────────────────────────────────────────────
# Main
# ───────────────────────────────────────────────────────────────────────

def main():
    print(f"Endpoint: {VLLM_BASE} model={MODEL}")
    print(f"Sampling: temp={SAMPLING['temperature']} top_p={SAMPLING['top_p']} rep_pen={EXTRA_BODY['repetition_penalty']}")
    print(f"Verbose prompt size: {len(PROMPT_VERBOSE)} chars, {PROMPT_VERBOSE.count(chr(10))} newlines")

    summary = {}

    # ─ Scenario 1: solo tool, minimal prompt ────────────────────────
    conv = [
        "Hi.",
        "I'm here to see Hoffmann.",
        "Matej, the one in vision robotics.",
        "Thanks, goodbye.",
    ]
    exp = {
        1: {"expect_tool": "lookup_fel_person", "expect_kw": []},
        # turn 2: model may call lookup again with full name OR just answer
        # turn 3: should NOT call lookup
    }
    summary["S1_solo_min_prompt_min_schema"] = run_scenario(
        "S1: solo lookup_fel_person, minimal prompt, minimal schema",
        PROMPT_MIN_LOOKUP_ONLY,
        [TOOL_LOOKUP_MIN],
        conv,
        expectations=exp,
    )

    # ─ Scenario 2: solo tool, minimal prompt, VERBOSE schema ────────
    summary["S2_solo_min_prompt_verbose_schema"] = run_scenario(
        "S2: solo lookup_fel_person, minimal prompt, VERBOSE schema (full Field text)",
        PROMPT_MIN_LOOKUP_ONLY,
        [TOOL_LOOKUP_VERBOSE],
        conv,
        expectations=exp,
    )

    # ─ Scenario 3: solo tool, VERBOSE prompt (the test_agent one) ───
    summary["S3_solo_verbose_prompt_min_schema"] = run_scenario(
        "S3: solo lookup_fel_person, VERBOSE multi-line prompt (test_agent's), minimal schema",
        PROMPT_VERBOSE,
        [TOOL_LOOKUP_MIN],
        conv,
        expectations=exp,
    )

    # ─ Scenario 4: 3 tools, minimal prompt (production combo) ──────
    conv2 = [
        "Hello!",
        "Where is room 230?",
        "I'm here to see Hoffmann.",
        "Matej.",
        "Thanks, goodbye!",
    ]
    exp2 = {
        0: {"expect_tool": "play_pose"},
        1: {"expect_tool": "query_search", "expect_kw": ["second floor"]},
        2: {"expect_tool": "lookup_fel_person"},
        4: {"expect_tool": "play_pose"},
    }
    summary["S4_three_tools_min"] = run_scenario(
        "S4: 3 tools (lookup+search+pose), minimal prompt",
        PROMPT_MIN_THREE_TOOLS,
        [TOOL_LOOKUP_MIN, TOOL_SEARCH, TOOL_POSE],
        conv2,
        expectations=exp2,
    )

    # ─ Scenario 5: 3 tools, verbose schema for lookup ───────────────
    summary["S5_three_tools_verbose_schema"] = run_scenario(
        "S5: 3 tools, minimal prompt, lookup uses VERBOSE schema",
        PROMPT_MIN_THREE_TOOLS,
        [TOOL_LOOKUP_VERBOSE, TOOL_SEARCH, TOOL_POSE],
        conv2,
        expectations=exp2,
    )

    # ─ Scenario 6: ambiguous-only — does it ask back? ──────────────
    conv3 = [
        "Hello.",
        "I'm here to see Novak.",  # very common surname → should disambiguate
        "First name Lukas.",
    ]
    exp3 = {
        1: {"expect_tool": "lookup_fel_person"},
    }
    summary["S6_ambiguous"] = run_scenario(
        "S6: ambiguous surname (Novak), check disambiguation",
        PROMPT_MIN_LOOKUP_ONLY,
        [TOOL_LOOKUP_MIN],
        conv3,
        expectations=exp3,
    )

    # ─ Final summary ────────────────────────────────────────────────
    print("\n" + "═" * 88)
    print("FINAL SUMMARY")
    print("═" * 88)
    print(f"{'scenario':<40s} | {'leaks':<6s} | {'tool-pass':<10s} | {'kw-pass':<8s}")
    print("-" * 88)
    for k, v in summary.items():
        print(f"{k:<40s} | {v['leaks']}/{v['n']:<3d} | {v['tool_pass']}/{v['n']:<7d} | {v['kw_pass']}/{v['n']}")

    print("\nLegend: 🟥 leak, ✅/❌ expected tool fired, 🔤 keyword present, 🛑 keyword missing")


if __name__ == "__main__":
    main()
