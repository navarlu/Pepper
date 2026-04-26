"""Isolate which part of the production prompt regresses things."""
from openai import OpenAI
client = OpenAI(base_url="http://localhost:8000/v1", api_key="dummy")

# Two tool variants we'll cycle
def make_anim_tool(name="play_pose", param="pose"):
    return {"type":"function","function":{"name":name,"description":"Set the robot body posture before speaking. " + param + " must be one of: greeting, bow, explain, happy, thinking, dont_know","parameters":{"type":"object","properties":{param:{"type":"string","description":"Body pose name"}},"required":[param]}}}

TOOL_QS_ORIG = {"type":"function","function":{"name":"search_kb","description":"Look up factual information.","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}}}
TOOL_QS_PROD = {"type":"function","function":{"name":"query_search","description":"Look up factual information.","parameters":{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}}}
# Plus get_time which was in the original test
TOOL_TIME = {"type":"function","function":{"name":"get_time","description":"Return the current time of day.","parameters":{"type":"object","properties":{}}}}

# Setup A: ORIGINAL successful test setup (search_kb + play_pose + get_time)
SETUPS = {
    "A_original_winner": {
        "tools": [TOOL_QS_ORIG, make_anim_tool("play_pose", "pose"), TOOL_TIME],
        "prompt": (
            "You are Pepper, a brief and polite robot receptionist. "
            "If you need information, call search_kb first. "
            "Call play_pose right before your spoken reply. "
            "Use get_time when asked about the time. "
            "Never say tool names aloud."
        ),
    },
    "B_no_time_tool": {
        # Drop get_time, keep everything else
        "tools": [TOOL_QS_ORIG, make_anim_tool("play_pose", "pose")],
        "prompt": (
            "You are Pepper, a brief and polite robot receptionist. "
            "If you need information, call search_kb first. "
            "Call play_pose right before your spoken reply. "
            "Never say tool names aloud."
        ),
    },
    "C_query_search_name": {
        # Rename search_kb → query_search but keep play_pose
        "tools": [TOOL_QS_PROD, make_anim_tool("play_pose", "pose")],
        "prompt": (
            "You are Pepper, a brief and polite robot receptionist. "
            "If you need information, call query_search first. "
            "Call play_pose right before your spoken reply. "
            "Never say tool names aloud."
        ),
    },
    "D_play_animation_name": {
        # query_search + play_animation
        "tools": [TOOL_QS_PROD, make_anim_tool("play_animation", "animation")],
        "prompt": (
            "You are Pepper, a brief and polite robot receptionist. "
            "If you need information, call query_search first. "
            "Call play_animation right before your spoken reply. "
            "Never say tool names aloud."
        ),
    },
}

LEAK = ["<tool_call>", "</tool_call>", "<|im_start|>", "<|im_end|>"]
TRIALS = 5
SCENS = ["Hello!", "Where is room 230?", "Thank you, goodbye!"]

header = f"{'setup':<22s} | {'scen':<22s} | OK  | LEAK | total"
print(header)
print("-" * len(header))
for sname, s in SETUPS.items():
    for scen in SCENS:
        ok = leak = 0
        for _ in range(TRIALS):
            r = client.chat.completions.create(
                model="Qwen/Qwen2.5-7B-Instruct",
                messages=[{"role":"system","content":s["prompt"]},{"role":"user","content":scen}],
                tools=s["tools"],
                parallel_tool_calls=False,
                temperature=0.01, top_p=0.8,
                extra_body={"repetition_penalty":1.05},
                max_tokens=200,
            )
            m = r.choices[0].message
            text = m.content or ""
            has_leak = any(p in text for p in LEAK)
            if has_leak: leak += 1
            else: ok += 1
        print(f"{sname:<22s} | {scen[:22]:<22s} | {ok:2d}  | {leak:3d}  | {TRIALS}")
