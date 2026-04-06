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

## Potential Next Steps

1. **Integrate into agent.py** — Replace the stripping logic with vanilla tool-calling flow, update system prompt and play_animation description.
2. **Larger model**: Qwen 2.5 14B or 32B handles tool calling much more reliably. If woska has enough VRAM, upgrading would eliminate most of these issues.
3. **Fine-tuning**: Fine-tune Qwen 7B on examples of single-tool-call-then-speech to reinforce the pattern.
