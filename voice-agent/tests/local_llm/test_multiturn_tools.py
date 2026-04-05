"""
Test multi-turn tool conversations with Qwen via vLLM.

Simulates the agent flow: user speaks → model calls tool → tool returns →
model responds (possibly with another tool call).

Tests stripping old tool calls from history to avoid vLLM JSON parse errors.

Usage (from project root, with SSH tunnel to woska active):
  uv run python voice-agent/tests/local_llm/test_multiturn_tools.py
"""

import json
import time
from urllib.request import Request, urlopen

VLLM_BASE = "http://localhost:8000/v1"
MODEL = "Qwen/Qwen2.5-7B-Instruct"

SYSTEM_PROMPT = """\
You are Pepper, a humanoid receptionist robot at CTU FEE in Prague (Karlovo náměstí).
Communicate in English, speak briefly, clearly, and politely.
If the user prefers another language, switch to it.

What you do:
- Provide information about FEE using the `query_search` tool.
- When you are unsure, use `query_search` instead of guessing.
- If the information is not available in the provided materials, say so directly and offer to clarify the question.
- Keep responses concise (typically 1–4 sentences), unless the user asks for more detail.
- Do not mention internal implementation details or library names.

You have a physical robot body. On every reply you MUST call the play_animation tool to move your body.
Never say tool names or animation names aloud — only call the tool silently and speak your reply naturally."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_search",
            "description": "Search the internal FEL knowledge base.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query"}
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_animation",
            "description": "Move Pepper's robot body. animation must be one of: greeting, bow, explain, happy, thinking, dont_know",
            "parameters": {
                "type": "object",
                "properties": {
                    "animation": {"type": "string", "description": "Animation name"}
                },
                "required": ["animation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_directions_to_room",
            "description": "Get directions to a room in Building E.",
            "parameters": {
                "type": "object",
                "properties": {
                    "room_number": {"type": "string", "description": "Room number"}
                },
                "required": ["room_number"],
            },
        },
    },
]

# Fake tool results for simulation
FAKE_TOOL_RESULTS = {
    "get_directions_to_room": '{"room": "E-301", "floor": "3", "directions": "Take elevator to 3rd floor, turn left, second door on the right."}',
    "query_search": '{"query": "FEL", "count": 1, "results": [{"title": "About FEL", "content": "Faculty of Electrical Engineering at CTU Prague.", "source": "wiki", "score": 0.95}]}',
    "play_animation": '{"ok": true, "status": "queued", "animation": "greeting"}',
}


def chat(messages: list[dict], stream: bool = False) -> dict:
    payload = json.dumps({
        "model": MODEL,
        "messages": messages,
        "tools": TOOLS,
        "stream": stream,
        "max_tokens": 300,
        "temperature": 0.7,
    }).encode()
    req = Request(
        f"{VLLM_BASE}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    with urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    elapsed = time.perf_counter() - t0
    data["_elapsed"] = elapsed
    return data


def strip_tool_history(messages: list[dict]) -> list[dict]:
    """Convert tool_call + tool_result pairs into plain assistant summaries.

    Instead of removing tool context (which causes re-calling), we collapse
    each tool_call → tool_result pair into an assistant message like:
      "[Used get_directions_to_room: {result}]"

    This gives the model context about what already happened without sending
    raw tool_call JSON that vLLM might choke on.
    """
    cleaned = []
    # Map tool_call_id → tool call info for pairing
    pending_tool_calls: dict[str, dict] = {}

    for msg in messages:
        role = msg.get("role", "")

        if role == "assistant" and msg.get("tool_calls"):
            # Store tool calls for pairing with results
            text = (msg.get("content") or "").strip()
            if text:
                cleaned.append({"role": "assistant", "content": text})
            for tc in msg["tool_calls"]:
                tc_id = tc.get("id", "")
                fn = tc.get("function", {})
                pending_tool_calls[tc_id] = {
                    "name": fn.get("name", "unknown"),
                    "arguments": fn.get("arguments", "{}"),
                }
            continue

        if role == "tool":
            tc_id = msg.get("tool_call_id", "")
            tc_info = pending_tool_calls.pop(tc_id, None)
            tool_name = tc_info["name"] if tc_info else "unknown_tool"
            result_text = (msg.get("content") or "").strip()
            # Collapse into a plain assistant message
            summary = f"[Used {tool_name} → {result_text[:200]}]"
            cleaned.append({"role": "assistant", "content": summary})
            continue

        cleaned.append(msg)
    return cleaned


def print_msg(role: str, content: str | None, tool_calls: list | None = None):
    if content:
        print(f"  [{role}] {content[:200]}")
    if tool_calls:
        for tc in tool_calls:
            fn = tc["function"]
            print(f"  [{role}] TOOL_CALL: {fn['name']}({fn['arguments']})")


def run_conversation(user_messages: list[str], use_stripping: bool):
    label = "WITH stripping" if use_stripping else "WITHOUT stripping"
    print(f"\n{'='*60}")
    print(f"  Multi-turn conversation ({label})")
    print(f"{'='*60}")

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    for i, user_text in enumerate(user_messages):
        print(f"\n--- Turn {i+1}: User says: {user_text!r} ---")
        messages.append({"role": "user", "content": user_text})

        # Optionally strip tool history before sending
        send_messages = strip_tool_history(messages) if use_stripping else messages

        print(f"  Sending {len(send_messages)} messages (original: {len(messages)})")

        try:
            result = chat(send_messages)
        except Exception as exc:
            print(f"  ERROR: {exc}")
            return False

        choice = result["choices"][0]
        msg = choice["message"]
        elapsed = result["_elapsed"]

        print_msg("assistant", msg.get("content"), msg.get("tool_calls"))
        print(f"  (latency: {elapsed:.2f}s, finish: {choice.get('finish_reason')})")

        # Add assistant message to history (raw, as-is)
        history_msg = {"role": "assistant", "content": msg.get("content")}
        if msg.get("tool_calls"):
            history_msg["tool_calls"] = msg["tool_calls"]
        messages.append(history_msg)

        # Process tool calls
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                fn = tc["function"]
                tool_name = fn["name"]
                fake_result = FAKE_TOOL_RESULTS.get(tool_name, '{"ok": true}')
                print(f"  -> Tool result for {tool_name}: {fake_result[:100]}")
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": fake_result,
                })

            # After tool results, get the model's actual text response
            send_messages = strip_tool_history(messages) if use_stripping else messages
            print(f"  Sending {len(send_messages)} messages for post-tool response")

            try:
                result2 = chat(send_messages)
            except Exception as exc:
                print(f"  ERROR on post-tool response: {exc}")
                return False

            choice2 = result2["choices"][0]
            msg2 = choice2["message"]
            elapsed2 = result2["_elapsed"]
            print_msg("assistant", msg2.get("content"), msg2.get("tool_calls"))
            print(f"  (latency: {elapsed2:.2f}s, finish: {choice2.get('finish_reason')})")

            history_msg2 = {"role": "assistant", "content": msg2.get("content")}
            if msg2.get("tool_calls"):
                history_msg2["tool_calls"] = msg2["tool_calls"]
            messages.append(history_msg2)

            # Handle any additional tool calls from post-tool response
            if msg2.get("tool_calls"):
                for tc in msg2["tool_calls"]:
                    fn = tc["function"]
                    fake_result = FAKE_TOOL_RESULTS.get(fn["name"], '{"ok": true}')
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": fake_result,
                    })

    print(f"\n  Final message count: {len(messages)}")
    return True


if __name__ == "__main__":
    # Check connectivity
    try:
        req = Request(f"{VLLM_BASE}/models")
        urlopen(req, timeout=5)
    except Exception as e:
        print(f"ERROR: Cannot reach vLLM at {VLLM_BASE}: {e}")
        raise SystemExit(1)

    user_turns = [
        "Hello! Where is room E-301?",
        "Thanks! And what is FEL known for?",
        "Can you show me a happy animation?",
    ]

    # Test WITHOUT stripping (should eventually fail on multi-turn)
    ok1 = run_conversation(user_turns, use_stripping=False)

    # Test WITH stripping (should work cleanly)
    ok2 = run_conversation(user_turns, use_stripping=True)

    print(f"\n{'='*60}")
    print(f"  Results:")
    print(f"    Without stripping: {'PASS' if ok1 else 'FAIL'}")
    print(f"    With stripping:    {'PASS' if ok2 else 'FAIL'}")
    print(f"{'='*60}")
