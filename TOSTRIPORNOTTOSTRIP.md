# Tool History Stripping for Qwen 2.5 7B + vLLM

Investigation into multi-turn tool call issues with the local LLM pipeline.
Date: 2025-04-05

## The Problem

When Qwen 2.5 7B (via vLLM with `--tool-call-parser hermes`) handles multi-turn conversations with tools, two things go wrong:

1. **Model embeds tool calls as plain text** — after receiving a tool result and generating a follow-up response, Qwen writes things like `play_animation("happy")` or raw `<tool_call>` tags directly in its text output instead of using the structured tool_call mechanism.

2. **vLLM chokes on re-processing** — when that malformed output is sent back as conversation history on the next turn, vLLM's hermes parser (or 0.19.0's stricter `_postprocess_messages`) fails with `json.JSONDecodeError`.

The root cause is Qwen 2.5 7B (small model) confusing the tool-calling format with text generation, especially in multi-turn contexts where it sees its own prior tool calls in the history.

## What We Tried

### 1. Upgrading vLLM (0.16.0 -> 0.19.0)

- **Result:** Made things worse. 0.19.0 added stricter JSON validation of tool call arguments in `_postprocess_messages`, which crashes on the malformed history that 0.16.0 silently ignored.
- **New error:** `json.decoder.JSONDecodeError: Extra data` in `chat_utils.py:1576`

### 2. Stricter system prompt instructions

Added explicit rules like:
```
CRITICAL RULES FOR TOOL USAGE:
- Call tools ONLY through the tool_call mechanism. NEVER write tool names in text.
- WRONG: "Sure! play_animation("happy")"
- RIGHT: "Sure, let me help you with that!"
```

- **Result:** Made things much worse. The model started outputting raw `<tool_call>` tags and `<|im_start|>` chat template tokens directly in text content. Responses took 16+ seconds and hit max token length. The hermes parser couldn't parse the broken JSON inside those tags.

### 3. Simpler prompt (original style)

```
You have a physical robot body. On every reply you MUST call the play_animation tool.
Never say tool names or animation names aloud — only call the tool silently and speak naturally.
```

- **Result:** Works OK for the first turn. Model uses proper tool_call mechanism. But on subsequent turns when it sees its own prior tool calls in history, it starts embedding them as text again.

### 4. History stripping (collapsing tool calls into summaries) -- WINNER

Instead of sending raw `tool_call` + `tool` message pairs back to the LLM, we collapse them into plain assistant messages:

```
Assistant tool_call: get_directions_to_room({"room_number": "E-301"})
Tool result: {"room": "E-301", "floor": "3", "directions": "..."}
```
becomes:
```
Assistant: [Used get_directions_to_room -> {"room": "E-301", "floor": "3", ...}]
```

- **Result:** Clean behavior across all turns. Model uses proper structured tool calls, text responses are pure natural speech with no tool names or JSON. No vLLM parser errors.

### 5. Naive stripping (removing tool history entirely)

Just dropping all `tool_call` and `tool` messages from history.

- **Result:** Model lost context about what already happened and re-called the same tools in a loop. Unusable.

## What Works

**History stripping with summaries** is the solution. The `strip_tool_history()` function:

1. Finds `assistant` messages with `tool_calls` — keeps any text content, drops the `tool_calls` field
2. Finds `tool` result messages — replaces them with `[Used tool_name -> result_summary]` as a plain assistant message
3. Leaves all other messages untouched

This gives the model enough context to know what already happened, without exposing raw tool call JSON that confuses it on subsequent turns.

## Test Script

`voice-agent/tests/local_llm/test_multiturn_tools.py` — runs a 3-turn conversation both with and without stripping to compare behavior.

## Key Takeaway

Qwen 2.5 7B is too small to reliably maintain proper tool-calling format across multi-turn conversations when it sees its own prior tool call outputs. The fix is on the application side: never feed raw tool history back to the model.
