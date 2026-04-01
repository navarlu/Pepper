"""
Minimal test: call Qwen 2.5 7B via vLLM's OpenAI-compatible API.

Prerequisites:
  - SSH tunnel open: ssh -J navarlu2@ptak.felk.cvut.cz -L 8000:127.0.0.1:8000 -N navarlu2@lie &
  - vLLM serving on lie

Usage:
  uv run python tests/local_llm/test_qwen.py
"""

import json
import time
import urllib.request

VLLM_BASE = "http://localhost:8000/v1"
MODEL = "Qwen/Qwen2.5-7B-Instruct"


def test_models():
    """List available models."""
    print("=== /v1/models ===")
    req = urllib.request.Request(f"{VLLM_BASE}/models")
    with urllib.request.urlopen(req, timeout=5) as resp:
        data = json.loads(resp.read())
    for m in data.get("data", []):
        print(f"  - {m['id']}")
    return data


def test_chat(message="Hello, who are you?"):
    """Simple chat completion."""
    print(f"\n=== Chat: {message!r} ===")
    payload = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "You are Pepper, a helpful robot receptionist."},
            {"role": "user", "content": message},
        ],
        "max_tokens": 200,
        "temperature": 0.7,
    }).encode()

    req = urllib.request.Request(
        f"{VLLM_BASE}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    elapsed = time.perf_counter() - t0

    reply = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    print(f"  Reply: {reply}")
    print(f"  Tokens: prompt={usage.get('prompt_tokens')}, completion={usage.get('completion_tokens')}")
    print(f"  Latency: {elapsed:.2f}s")
    return data


def test_tool_call():
    """Test that vLLM returns tool calls (hermes parser)."""
    print("\n=== Tool call test ===")
    payload = json.dumps({
        "model": MODEL,
        "messages": [
            {"role": "system", "content": "You are a helpful assistant. Use tools when appropriate."},
            {"role": "user", "content": "Search for information about the Faculty of Electrical Engineering."},
        ],
        "tools": [
            {
                "type": "function",
                "function": {
                    "name": "query_search",
                    "description": "Search the knowledge base for information.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {"type": "string", "description": "The search query."},
                        },
                        "required": ["query"],
                    },
                },
            }
        ],
        "tool_choice": "auto",
        "max_tokens": 200,
    }).encode()

    req = urllib.request.Request(
        f"{VLLM_BASE}/chat/completions",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    elapsed = time.perf_counter() - t0

    choice = data["choices"][0]
    msg = choice["message"]
    if msg.get("tool_calls"):
        for tc in msg["tool_calls"]:
            fn = tc["function"]
            print(f"  Tool call: {fn['name']}({fn['arguments']})")
    else:
        print(f"  No tool call. Reply: {msg.get('content', '')[:200]}")
    print(f"  Latency: {elapsed:.2f}s")
    return data


if __name__ == "__main__":
    try:
        test_models()
    except Exception as e:
        print(f"ERROR: Cannot reach vLLM at {VLLM_BASE}: {e}")
        print("Is the SSH tunnel open? Run:")
        print("  ssh -J navarlu2@ptak.felk.cvut.cz -L 8000:127.0.0.1:8000 -N navarlu2@lie &")
        raise SystemExit(1)

    test_chat()
    test_tool_call()
    print("\nAll tests passed!")
