"""Search a stable short name for the new UDB lookup tool against Qwen 7B.

Background: test_agent ships the tool as `lookup_fel_person`. That name is
solid in solo runs (0/4 leaks) but in the production 3-tool combo
(lookup + query_search + play_pose) Qwen's hermes parser leaks
<tool_call>/<|im_start|> tokens into the spoken text.

This test compares 5 candidate names under one fixed prompt and tool set
(Qwen sampling: temp=0.01, top_p=0.8, repetition_penalty=1.05). We hit
the real UDB endpoint so the LLM sees real result shapes.

Findings (as of the run that produced this file):
  - lookup_fel_person  -> 5/5 leaks (broken)
  - find_staff         -> unstable (first run 0, repeat 5/5)
  - find_person        -> 4/5 leaks
  - staff_lookup       -> 4/5 leaks
  - lookup_person      -> 0/5 std, 0/7 rich, 0/5 ambig (winner)

Run from project root:
    uv run --with openai --with requests --with beautifulsoup4 python \\
      voice-agent/tests/tool_lookup_name_search.py
"""
import json, sys
from pathlib import Path
sys.path.insert(0, str((Path(__file__).resolve().parent.parent / "src").resolve()))
from openai import OpenAI
from udb import lookup_person

VLLM, MODEL = "http://localhost:8000/v1", "Qwen/Qwen2.5-7B-Instruct"
LEAK = ["<tool_call>", "</tool_call>", "<|im_start|>", "<|im_end|>"]

def schema(tool_name):
    return {"type":"function","function":{"name":tool_name,"description":"Look up FEL staff contact info by surname or full name.","parameters":{"type":"object","properties":{"name":{"type":"string","description":"Person's surname or full name."}},"required":["name"]}}}

T_QS = {"type":"function","function":{"name":"query_search","description":"Look up factual information (people, rooms, schedules). Use whenever the user asks about facts — do not guess.","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}}}
T_POSE = {"type":"function","function":{"name":"play_pose","description":"Set the robot body posture before speaking. pose must be one of: greeting, bow, explain, happy, thinking, dont_know","parameters":{"type":"object","properties":{"pose":{"type":"string","description":"Body pose name"}},"required":["pose"]}}}

# Conversations to stress more aspects
CONV_STD = ["Hello!", "Where is room 230?", "I'm here to see Hoffmann.", "Matej.", "Thanks, goodbye!"]
CONV_RICH = [
    "Hello!",
    "Where is room 230?",
    "I'm here to see Hoffmann.",
    "Matej, the one in vision robotics.",
    "What is his email?",
    "Where is his office?",
    "Thanks, goodbye!",
]
CONV_AMBIG = [
    "Hello!",
    "I'm here to see Novak.",
    "First name Lukas.",
    "OK never mind, can you give me the dean's office instead?",
    "Thanks!",
]

def fake(name, args):
    if name == "play_pose": return None
    if name == "query_search":
        q=(args.get("query") or "").lower()
        if "230" in q or "room" in q: return {"room":"E-230","directions":"Second floor, turn right."}
        if "dean" in q: return {"name":"Prof. Doe","room":"E-301"}
        return {"result":"no info"}
    # any lookup-tool name we registered → real UDB call
    if "look" in name or "find" in name or "staff" in name:
        try: return lookup_person(args.get("name",""))
        except Exception as e: return {"error":str(e)}
    return {"error":"unknown"}

def turn(client,hist,txt,tools):
    hist.append({"role":"user","content":txt}); spoke=[]; tcalls=[]; leaks=[]
    for _ in range(4):
        r=client.chat.completions.create(model=MODEL,messages=hist,tools=tools,parallel_tool_calls=False,max_tokens=300,temperature=0.01,top_p=0.8,extra_body={"repetition_penalty":1.05})
        m=r.choices[0].message; t=(m.content or "").strip()
        if any(p in t for p in LEAK): leaks.append(t)
        if t: spoke.append(t)
        e={"role":"assistant","content":m.content}
        if m.tool_calls: e["tool_calls"]=[{"id":tc.id,"type":"function","function":{"name":tc.function.name,"arguments":tc.function.arguments}} for tc in m.tool_calls]
        hist.append(e)
        if not m.tool_calls: break
        all_none=True
        for tc in m.tool_calls:
            try: a=json.loads(tc.function.arguments or "{}")
            except: a={}
            res=fake(tc.function.name,a); tcalls.append((tc.function.name,a))
            if res is not None: all_none=False
            hist.append({"role":"tool","tool_call_id":tc.id,"name":tc.function.name,"content":"" if res is None else json.dumps(res)})
        if all_none: break
    return " ".join(spoke), tcalls, leaks

def run(label, prompt, tools, conv):
    print(f"\n── {label} ── (n={len(tools)}, prompt={len(prompt)}c)")
    print(f"   tools: {[t['function']['name'] for t in tools]}")
    c=OpenAI(base_url=VLLM,api_key="x"); h=[{"role":"system","content":prompt}]
    L=0; OK=0
    for u in conv:
        s,tc,lk=turn(c,h,u,tools); L+=int(bool(lk))
        if not lk and tc: OK+=1
        mk="🟥" if lk else ("✅" if tc else "  ")
        names=",".join(t[0] for t in tc)
        print(f"   {mk} u>{u!r:<46s} tools=[{names:<32s}] spoke={s[:90]!r}")
    print(f"   leaks={L}/{len(conv)}  tool-pass={OK}/{len(conv)}")
    return L,OK

def make_prompt(lookup_name):
    return (
        "You are Pepper, a brief and polite robot receptionist at FEL. "
        f"When a visitor names a staff member, call {lookup_name} with that name. "
        "If you need other facts (rooms, schedules), call query_search. "
        "Call play_pose right before your spoken reply. "
        "Never say tool names aloud."
    )

NAMES = ["lookup_fel_person", "find_staff", "find_person", "lookup_person", "staff_lookup"]

print(f"=== Comparing tool names on standard conv ===")
results = {}
for nm in NAMES:
    tools = [schema(nm), T_QS, T_POSE]
    L,OK = run(f"NAME={nm}", make_prompt(nm), tools, CONV_STD)
    results[nm] = {"std": (L, OK)}

print(f"\n\n=== Best name on RICH conv (more turns) ===")
# Take winners (any with 0 leaks)
winners = [nm for nm, r in results.items() if r["std"][0] == 0]
print(f"Winners on standard: {winners}")
for nm in winners:
    tools = [schema(nm), T_QS, T_POSE]
    L,OK = run(f"RICH NAME={nm}", make_prompt(nm), tools, CONV_RICH)
    results[nm]["rich"] = (L, OK)

print(f"\n\n=== Best name on AMBIGUOUS conv ===")
for nm in winners:
    tools = [schema(nm), T_QS, T_POSE]
    L,OK = run(f"AMBIG NAME={nm}", make_prompt(nm), tools, CONV_AMBIG)
    results[nm]["ambig"] = (L, OK)

print("\n" + "═" * 80)
print("FINAL — leak/tool-pass per name × scenario")
print("═" * 80)
print(f"{'name':<20s} | {'std (5)':<10s} | {'rich (7)':<10s} | {'ambig (5)':<10s}")
print("-" * 80)
for nm in NAMES:
    r = results.get(nm, {})
    std = r.get("std", (None, None))
    rich = r.get("rich", (None, None))
    ambig = r.get("ambig", (None, None))
    def fmt(t):
        if t == (None, None): return "-"
        return f"L{t[0]}/T{t[1]}"
    print(f"{nm:<20s} | {fmt(std):<10s} | {fmt(rich):<10s} | {fmt(ambig):<10s}")
