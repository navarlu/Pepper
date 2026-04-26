"""Test: does the issue come from prompt LENGTH or from NEWLINES?

4 variants of system prompts with deterministic content.
Same 4-turn conversation, count leaks per variant.
"""
import json
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key="dummy")
TOOL_QS = {"type":"function","function":{"name":"query_search","description":"Look up factual information.","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}}}
TOOL_POSE = {"type":"function","function":{"name":"play_pose","description":"Set the robot body posture before speaking. pose must be one of: greeting, bow, explain, happy, thinking, dont_know","parameters":{"type":"object","properties":{"pose":{"type":"string"}},"required":["pose"]}}}

# Same words, different formatting
LONG_BODY_PARTS = [
    "You are Pepper, a brief and polite robot receptionist at CTU FEE Prague.",
    "If the user prefers another language, switch to it.",
    "If you need information, call query_search first — do not guess.",
    "query_search also knows room locations and walking directions.",
    "Call play_pose right before your spoken reply.",
    "Never say tool names aloud.",
]
SHORT_BODY_PARTS = [
    "You are Pepper, a robot receptionist.",
    "Always call query_search to look up facts before answering.",
    "Always call play_pose before speaking.",
    "Be brief and polite.",
]

PROMPTS = {
    "A_short_singleline":   " ".join(SHORT_BODY_PARTS),
    "B_long_singleline":    " ".join(LONG_BODY_PARTS),     # same words as prod, all on ONE line
    "C_long_LF_per_sent":   "\n".join(LONG_BODY_PARTS),    # newline between sentences
    "D_long_doubleLF":      "\n\n".join(LONG_BODY_PARTS),  # paragraph breaks
    "E_short_LF_per_sent":  "\n".join(SHORT_BODY_PARTS),   # short but with newlines
}

LEAK = ["<tool_call>","</tool_call>","<|im_start|>","<|im_end|>"]
def fake(name, args):
    if name == "play_pose": return {"ok":True,"pose":args.get("pose","")}
    if name == "query_search":
        q = (args.get("query") or "").lower()
        if "230" in q or "room" in q: return {"room":"E-230","floor":2,"directions":"Take stairs to second floor and turn right."}
        if "dean" in q: return {"name":"Prof. Doe","phone":"+420 224 35 1234"}
        return {"result":"no info"}

def turn(history, txt):
    history.append({"role":"user","content":txt})
    spoken=[]; tools=[]; leaks=[]
    for _ in range(4):
        r = client.chat.completions.create(model="Qwen/Qwen2.5-7B-Instruct",messages=history,tools=[TOOL_QS,TOOL_POSE],parallel_tool_calls=False,temperature=0.01,top_p=0.8,extra_body={"repetition_penalty":1.05},max_tokens=300)
        m = r.choices[0].message
        text=(m.content or "").strip()
        if any(p in text for p in LEAK): leaks.append(text)
        if text: spoken.append(text)
        e={"role":"assistant","content":m.content}
        if m.tool_calls: e["tool_calls"]=[{"id":tc.id,"type":"function","function":{"name":tc.function.name,"arguments":tc.function.arguments}} for tc in m.tool_calls]
        history.append(e)
        if not m.tool_calls: break
        all_none=True
        for tc in m.tool_calls:
            try: a=json.loads(tc.function.arguments or "{}")
            except: a={}
            res=fake(tc.function.name, a); tools.append((tc.function.name,a))
            if res is not None: all_none=False
            history.append({"role":"tool","tool_call_id":tc.id,"name":tc.function.name,"content":json.dumps(res)})
        if all_none: break
    return " ".join(spoken), tools, leaks

CONV = ["Hello!","Where is room 230?","What is the dean phone number?","Thank you, goodbye!"]

# Show prompt formatting plus per-prompt results
print("=" * 90)
print("PROMPT FORMATTING TEST  —  length × line-breaks")
print("=" * 90)
for pname, prompt in PROMPTS.items():
    print(f"\n[{pname}]  len={len(prompt)} chars  newlines={prompt.count(chr(10))}")
    print(f"  prompt: {prompt!r}")
    hist=[{"role":"system","content":prompt}]
    lk = ok = 0
    for u in CONV:
        s,t,l = turn(hist,u)
        if l: lk += 1
        else: ok += 1
        mk = "🟥" if l else "✅"
        ts = ",".join(x[0] for x in t)
        print(f"    {mk} {u:<32s} tools=[{ts:<13s}] bot={s[:80]!r}")
    print(f"    → leaks {lk}/{len(CONV)}")

print("\n" + "=" * 90)
print("SUMMARY")
print("=" * 90)
