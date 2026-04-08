# Qwen 2.5 7B Tool Call Issues

## Setup

- Model: `Qwen/Qwen2.5-7B-Instruct` served via vLLM
- vLLM flags: `--enable-auto-tool-choice --tool-call-parser hermes`
- Framework: LiveKit Agents SDK (AgentSession with pipeline: STT → LLM → TTS)
- Tools: `play_animation` (called every reply), `query_search` (RAG lookup)

The `hermes` parser is correct for this model — Qwen 2.5 Instruct uses the `<tool_call></tool_call>` format natively.

## The Core Problem

After the LLM calls a tool, the framework executes it and calls the LLM again with the result in context. Qwen 2.5 7B then either:

1. **Calls the same tool again** (infinite loop), or
2. **Outputs `<tool_call>` JSON as spoken text** (user hears tool syntax)

These are two sides of the same coin: the 7B model is too small to reliably separate structured tool-call syntax from natural language in multi-turn conversation.

## What We Tried

### Attempt 1: Keep tool history as-is
**Result**: Qwen sees raw `FunctionCall` + `FunctionCallOutput` items in chat history. On the next turn, it confuses the JSON with text and starts speaking tool call syntax aloud: *"Used play_animation arrow name explain arguments animation explain"*

### Attempt 2: Replace tool pairs with assistant-role summaries
```python
# FunctionCall + FunctionCallOutput → ChatMessage(role="assistant", content="[Used play_animation → result]")
```
**Result**: Qwen mimics the `[Used ...]` format in its spoken output because it sees it as something an assistant would say. TTS speaks: *"[Used play_animation → ...]"*

### Attempt 3: Drop tool history entirely
```python
# FunctionCall + FunctionCallOutput → removed completely
```
**Result**: Qwen doesn't know it already called the tool, so it calls `play_animation` 3-4 times in a row before producing speech.

### Attempt 4: Replace with system-role notes
```python
# FunctionCall + FunctionCallOutput → ChatMessage(role="system", content="(play_animation done)")
```
**Result**: Still repeated calls. System-role notes aren't strong enough for 7B to inhibit tool-calling behavior.

### Attempt 5: `tool_choice="none"` after tool execution
After a tool returns, pass `tool_choice="none"` to force text-only response.
**Result**: vLLM doesn't parse `<tool_call>` tags as actual calls, but Qwen still **generates them as text** because the tool definitions are still in the prompt. TTS speaks: *"oplay_animation thinking I'll check that for you tool_call name query_search..."*

### Attempt 6 (current, v0.3.4): Drop tool history + pass `tools=None` after tool execution
```python
def _chat_with_stripped_history(*, chat_ctx, tools=None, **kwargs):
    has_tool_output = any(isinstance(it, FunctionCallOutput) for it in chat_ctx.items)
    stripped = _strip_tool_history(chat_ctx)  # drops FunctionCall + FunctionCallOutput

    if has_tool_output:
        # No tool definitions → Qwen can't generate <tool_call> syntax
        return _original_chat(chat_ctx=stripped, tools=None, **kwargs)
    
    return _original_chat(chat_ctx=stripped, tools=tools, **kwargs)
```
**Status**: Testing. The idea is that without tool definitions in the prompt, Qwen has no template to generate `<tool_call>` tags from.

**Open risk**: On multi-tool turns (e.g. `play_animation` then `query_search`), the second tool call won't happen because tools are removed after the first. This may need a smarter approach — e.g. only remove `play_animation` from the tools list after it's called, but keep `query_search` available.

## Additional Issues

### Malformed JSON from Qwen
Qwen sometimes emits extra trailing braces: `{"animation": "greeting"}}`. Fixed with a monkey-patch on `prepare_function_arguments` that strips trailing characters after the first balanced `{}`.

### Memory usage
The agent process uses ~700MB on woska (Whisper tiny + Piper TTS + VAD models). LiveKit warns at 500MB but this is normal for the loaded models.

### Attempt 7 (2026-04-06): WORKING — Vanilla flow + description trick + single-line prompt

Root cause analysis via A/B testing (`voice-agent/tests/test_agent_scenario.py`) revealed three independent issues:

**Finding 1: Qwen only calls tools it believes return needed data.**
The model treats tool calls as "I need information", not "trigger a side effect". `query_search` always worked because the model needs the result to answer. `play_animation` was skipped or inlined as text because the model saw it as fire-and-forget.

**Fix:** Rewrite `play_animation` description to frame it as returning data the model needs:
```
"Check and set the robot body posture. Returns the current body state which you need before speaking.
animation must be one of: greeting, bow, explain, happy, thinking, dont_know"
```
The fake/real result should return something like `{"body_state": "ready", "posture": "greeting"}`.

**Finding 2: Multi-line system prompts cause hermes parser failures.**
With a multi-line Pepper prompt, the model sometimes generates text + `<tool_call>` interleaved, and vLLM's hermes parser can't extract the tool calls. They end up as text content with garbage `<|im_start|>` tokens. A single-line prompt avoids this.

**Fix:** Keep the system prompt on one line:
```
"You are Pepper, a robot receptionist at CTU FEE Prague. Speak briefly and politely in English. Use query_search to find information. Call play_animation to check your body state before every reply. Never say tool names aloud."
```

**Finding 3: No stripping needed.**
The standard OpenAI tool-calling flow works correctly: assistant message with `tool_calls` → tool result with `role: "tool"` → model generates next response. No history stripping, no tool hiding, no workarounds. The previous stripping logic (attempts 3-6) was causing more problems than it solved — it removed query_search results so the model hallucinated answers.

**Working config (ALL PASS on 3-step scenario):**
- Temperature: 0.3
- `parallel_tool_calls=False`
- Single-line system prompt with tool instructions
- `play_animation` description says it returns body state
- Standard OpenAI tool-calling loop (no stripping)
- Tool results kept in history as-is

**Test results:**
```
Step 1: "Hello!"              → play_animation(greeting) → PASS
Step 2: "Dean's phone number?" → query_search → correct phone number → PASS
Step 3: "Thank you, goodbye!"  → play_animation(bow) → PASS
No leakage in any step.
```

### Attempt 8 (2026-04-06): Integration into real agent — partial success

Integrated the Attempt 7 findings into the real LiveKit agent pipeline. Discovered several new problems that don't appear in the test script but do in the live system.

**What was done:**
- Removed all stripping logic from agent.py
- Created separate `play_animation_local` / `play_animation_openai` tool variants with `@function_tool(name="play_animation")` (the SDK uses `func.__name__` as tool name — without explicit name, it registered as `play_animation_local` which the model couldn't match)
- Local variant returns `{"body_state": "ready", "posture": ...}`, OpenAI variant unchanged
- Added `temperature=0.3` to `openai.LLM()` constructor
- Single-line `LOCAL_SYSTEM_PROMPT` in config.py
- Split `listener` service into `room-monitor` + `audio-bridge` (transcript forwarding was blocked by robot bridge TCP connection)

**New Problem 1: vLLM 0.19 crashes on malformed tool args in history (400 Bad Request)**
Qwen sometimes generates malformed tool call arguments like `{"animation": "greeting"}}\nHello!` (extra brace + text appended). The existing monkey-patch on `prepare_function_arguments` cleans this for local execution, but when the LiveKit SDK sends the conversation history back to vLLM on the next turn, vLLM 0.19's `_postprocess_messages` (chat_utils.py:1576) does `json.loads()` on the raw arguments string and crashes.

**Fix:** Added `_sanitize_chat_ctx()` — before every LLM call, scan the chat context and fix any `FunctionCall.arguments` that have trailing garbage. Extracts the first balanced JSON object. This is a lightweight chat wrapper, not history stripping.

**New Problem 2: Double responses (LiveKit SDK re-calls LLM after tool execution)**
After `play_animation` returns a result, the SDK calls the LLM again because `reply_required=True` (set when tool returns non-None). Since Qwen generates text + tool_call in the same streaming response, the text is already going to TTS — the second LLM call produces an unwanted duplicate response. This is a known SDK bug: livekit/agents#4554.

**Fix:** `play_animation` in local mode returns `None` instead of body state data. From LiveKit docs: "Return None to complete the tool silently without requiring a reply from the LLM." The tool description still says "returns body state" so Qwen still calls it, but the actual return is silent.

**New Problem 3: `play_animation` only called ~60% of the time (non-deterministic)**
Even with all fixes applied, streaming consistency tests show `play_animation` is called only 3-4 out of 5 times at temp=0.3. The model sometimes just generates text without calling the tool. This is the fundamental Qwen 7B limitation — it doesn't reliably call side-effect tools. `query_search` works reliably because the model needs the data to answer; `play_animation` is optional from the model's perspective.

**New Problem 4: Competing agents**
The RPi Docker setup had `voice-agent` running alongside the woska agent. Both registered with LiveKit and competed for dispatches. The RPi agent had old code without the tool fixes. Fixed by removing voice-agent from the main docker-compose.yml and creating a separate `docker-compose.rpi.yml`.

**Current state (v0.4.9):**
- `query_search` — works reliably, correct results, no leakage
- `play_animation` — works sometimes (~60%), non-deterministic at temp=0.3
- `get_directions_to_room` — not called by model (same "optional tool" problem as play_animation)
- No 400 errors (sanitization working)
- No double responses (None return working)
- Transcripts appear in UI via room-monitor service

### Attempt 9 (2026-04-06): SOLVED — Qwen 7B has a hard 2-tool limit

**Root cause: Qwen 2.5 7B cannot reliably handle 3+ tools.**

Systematic A/B testing (`voice-agent/tests/test_tool_count.py`) proved this definitively:

| Config | pass rate | leakage |
|--------|-----------|---------|
| 2 tools (query_search + play_animation) | **5/5 (100%)** | 0 |
| 3 tools (+ get_directions_to_room) | **0/5 (0%)** | 5/5 |
| 3 tools (+ get_time, no params) | **5/5 (100%)** initially, then inconsistent |

When a 3rd tool with parameters is present, Qwen generates `<tool_call>` blocks interleaved with raw `<|im_start|>` tokens:
```
Hello! How can I assist you today? <tool_call>
<|im_start|>assistant {"name": "play_animation", "arg...
```

vLLM's hermes parser (`hermes_tool_parser.py:110`) regex-matches `<tool_call>...</tool_call>` and runs `json.loads()` on the content. The `<|im_start|>` garbage causes `JSONDecodeError: Expecting value: line 2 column 1`. The parser catches the error and falls back to returning everything as text content — the user hears tool syntax aloud.

**Further testing showed:**
- The description length/content of the 3rd tool does not matter (5 variants tested, all 0%)
- Renaming the tool does not help
- Changing parameter type (string → int, enum) does not help
- Even a 3rd tool with zero parameters becomes inconsistent
- The issue is purely about the number of tool schemas in the hermes chat template overwhelming Qwen 7B's attention

**Fix: merge `get_directions_to_room` into `query_search`.**

The LLM naturally calls `query_search("location of room 230")` for room questions. The tool implementation detects room number patterns in the query string and routes to the building map data instead of Weaviate.

Test results with merged approach (`voice-agent/tests/test_merged_tools.py`):
```
4-step scenario (greeting → dean query → room directions → goodbye):
  pass=5/5 (100%)  leakage=0
```

All steps pass including directions, with only 2 tools registered.

**Changes applied:**
- `tools.py`: `query_search` now detects room queries via regex and routes to `_load_room_data()`
- `tools.py`: `get_directions_to_room` removed from the tools list (code kept for OpenAI mode)
- `config.py`: system prompts updated — mention that `query_search` handles room directions
- Agent stays at exactly 2 tools: `query_search` + `play_animation`

## Ideas for Next Steps (not yet validated)

> **Note:** The 2-tool limit is a fundamental Qwen 7B constraint. These ideas address remaining gaps.

1. **Parse animation from text** — If the model doesn't call `play_animation` as a tool, detect the intent from the response text (sentiment analysis or keyword matching) and trigger the animation in code. This is a fallback, not a fix, but would guarantee animation on every reply.
2. **Larger model** — Qwen 2.5 14B or 32B would handle 3+ tools reliably. Needs VRAM check on woska. This would remove the 2-tool constraint entirely.
3. **Fine-tuning** — Fine-tune Qwen 7B on examples of always-call-animation-then-speak. Time-consuming but would solve the root cause for this specific model.
4. **Lower temperature** — Try temp=0.0 or 0.1 for more deterministic tool calling. Trade-off: less natural speech variation.

### Attempt 10 (2026-04-06): 2-tool limit DISPROVED — it's the tool definitions, not the count

**Test:** `voice-agent/tests/test_dummy_tool_limit.py` — standalone A/B test with completely generic dummy tools (get_weather, get_time, translate_text, calculate). No Pepper context, no project-specific prompts. Plain "helpful assistant" system prompt.

**Results:**
```
2_tools   pass=5/5 (100%)  leakage=0/5
3_tools   pass=5/5 (100%)  leakage=0/5
4_tools   pass=5/5 (100%)  leakage=0/5
```

All configs pass perfectly — even 4 tools with parameters work fine.

**Conclusion:** The "2-tool hard limit" from Attempt 9 was wrong. Qwen 2.5 7B handles 3+ tools reliably when the tool schemas are clean and simple. The failure with `get_directions_to_room` was caused by something specific to our tool definitions (description length, parameter naming, interaction with the Pepper system prompt, or schema complexity) — not a fundamental model constraint.

**What this means:**
- The `get_directions_to_room` merge into `query_search` still works and is fine, but it was not strictly necessary
- Re-adding a 3rd tool is possible if the schema is kept simple and clean
- The "Next Steps" ideas about larger models or fine-tuning for tool count are no longer relevant — focus should be on tool schema quality instead
- The PA-Tool paper's recommendation still applies: adapt schemas to the model, don't blame the model for bad schemas
