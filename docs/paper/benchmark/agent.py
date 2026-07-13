"""Shared agent turn loop (streaming) for the benchmark.

Uses the OpenAI **Responses API** (`client.responses.create`), because gpt-5.4
rejects function tools together with a non-"none" `reasoning_effort` on
/v1/chat/completions — the Responses API is the supported path for tools +
reasoning. Reasoning context is carried across tool hops (and across chat
turns) via `previous_response_id`, so conversation state is a small dict
`{"prev": <last response id>}` rather than a message list.

Streaming lets us measure Time-To-First-Token of the *answer*: only
`response.output_text.delta` events count as the first token — reasoning
summaries and tool-call argument deltas are different event types, so TTFT is
the answer's first token, never the thinking's or the tool call's.

Per turn we record:
  - ttft_answer_ms : end-to-end, turn start -> first answer token (incl. tool
                     round-trips). Text analog of the thesis's time-to-first-audio.
  - gen_ttft_ms    : within the answering step, request-sent -> first token.
  - total_ms, input_tokens, output_tokens, reasoning_tokens (0 when off), hops.

Reasoning effort is tunable per call via `effort` (default config.REASONING_EFFORT).
"""
import json
import time

import config
from prompt import SYSTEM_PROMPT
from tools.find_room import TOOLS

# Responses API function-tool format (flat: name/description/parameters at top level).
RESPONSES_TOOLS = [
    {
        "type": "function",
        "name": schema["function"]["name"],
        "description": schema["function"]["description"],
        "parameters": schema["function"]["parameters"],
    }
    for (_fn, schema) in TOOLS.values()
]


def new_state():
    """Fresh conversation state for the Responses-API chaining model."""
    return {"prev": None}


def _execute_tool(name, args):
    fn = TOOLS.get(name, (None, None))[0]
    if fn is None:
        return {"error": "unknown_tool", "name": name}
    try:
        return fn(**args)
    except TypeError as e:
        return {"error": "bad_arguments", "detail": str(e)}


def _stream_step(client, model, input_items, previous_response_id, effort, on_text=None):
    """Stream one Responses-API step. Returns a dict with the response id, the
    streamed answer text, any function calls, first-token wall time + gen TTFT,
    and token usage. `on_text(delta)` fires for each answer-text token."""
    kwargs = dict(
        model=model,
        instructions=SYSTEM_PROMPT,
        input=input_items,
        tools=RESPONSES_TOOLS,
        stream=True,
    )
    if previous_response_id is not None:
        kwargs["previous_response_id"] = previous_response_id
    if effort is not None:
        kwargs["reasoning"] = {"effort": effort}
    # Note: reasoning models reject sampling params, so temperature is not sent.

    req_start = time.perf_counter()
    first_token_wall = None
    gen_ttft_ms = None
    text_parts = []
    response_obj = None

    for event in client.responses.create(**kwargs):
        etype = event.type
        if etype == "response.output_text.delta":
            if first_token_wall is None:
                first_token_wall = time.perf_counter()
                gen_ttft_ms = (first_token_wall - req_start) * 1000
            text_parts.append(event.delta)
            if on_text is not None:
                on_text(event.delta)
        elif etype == "response.completed":
            response_obj = event.response

    func_calls = []
    in_tok = out_tok = reasoning_tok = 0
    if response_obj is not None:
        for item in response_obj.output:
            if item.type == "function_call":
                func_calls.append(item)
        usage = response_obj.usage
        if usage is not None:
            in_tok = usage.input_tokens or 0
            out_tok = usage.output_tokens or 0
            details = getattr(usage, "output_tokens_details", None)
            if details is not None:
                reasoning_tok = getattr(details, "reasoning_tokens", 0) or 0

    return {
        "response_id": response_obj.id if response_obj is not None else previous_response_id,
        "text": "".join(text_parts),
        "func_calls": func_calls,
        "first_token_wall": first_token_wall,
        "gen_ttft_ms": gen_ttft_ms,
        "in_tok": in_tok,
        "out_tok": out_tok,
        "reasoning_tok": reasoning_tok,
    }


def run_turn(client, model, user_text, state, on_tool=None, on_text=None, effort="__default__"):
    """Run one user turn. `state` (from new_state()) is mutated to carry the
    Responses-API context to the next turn. `on_tool(name, args, result)` is
    called for each executed tool; `on_text(delta)` for each answer-text token.
    `effort` overrides config.REASONING_EFFORT (pass None to omit the reasoning
    parameter). Returns a trace dict.
    """
    if effort == "__default__":
        effort = config.REASONING_EFFORT

    turn_start = time.perf_counter()
    prev = state.get("prev")
    current_input = [{"role": "user", "content": user_text}]

    ttft_answer_ms = None
    gen_ttft_ms = None
    tool_calls_made = []
    in_tok = out_tok = reasoning_tok = 0
    final_text = None
    hops = 0

    for _hop in range(config.MAX_TOOL_HOPS):
        hops += 1
        step = _stream_step(client, model, current_input, prev, effort, on_text=on_text)
        in_tok += step["in_tok"]
        out_tok += step["out_tok"]
        reasoning_tok += step["reasoning_tok"]
        prev = step["response_id"]

        if step["first_token_wall"] is not None and ttft_answer_ms is None:
            ttft_answer_ms = (step["first_token_wall"] - turn_start) * 1000
            gen_ttft_ms = step["gen_ttft_ms"]

        if step["func_calls"]:
            outputs = []
            for fc in step["func_calls"]:
                try:
                    args = json.loads(fc.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                result = _execute_tool(fc.name, args)
                tool_calls_made.append({"name": fc.name, "args": args})
                if on_tool is not None:
                    on_tool(fc.name, args, result)
                outputs.append({
                    "type": "function_call_output",
                    "call_id": fc.call_id,
                    "output": json.dumps(result, ensure_ascii=False),
                })
            current_input = outputs
            continue

        final_text = step["text"]
        break

    state["prev"] = prev
    total_ms = (time.perf_counter() - turn_start) * 1000
    return {
        "final_text": final_text,
        "tool_calls": tool_calls_made,
        "effort": effort,
        "ttft_answer_ms": round(ttft_answer_ms, 1) if ttft_answer_ms is not None else None,
        "gen_ttft_ms": round(gen_ttft_ms, 1) if gen_ttft_ms is not None else None,
        "total_ms": round(total_ms, 1),
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "reasoning_tokens": reasoning_tok,
        "hops": hops,
    }
