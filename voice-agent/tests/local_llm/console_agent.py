"""
Console chat agent using local Qwen LLM via vLLM (OpenAI-compatible API).
Real tools: Weaviate RAG search, building map directions.
Animations are logged but not dispatched (no bridge needed).

Prerequisites:
  - SSH tunnel open: ssh -J navarlu2@ptak.felk.cvut.cz -L 8000:127.0.0.1:8000 -N navarlu2@lie &
  - vLLM serving Qwen on lie
  - Weaviate running (docker compose up -d weaviate)

Usage:
  uv run python tests/local_llm/console_agent.py
"""

import importlib.util
import json
import logging
import re
import sys
import time
import urllib.request
from pathlib import Path

# Add project root to path so we can import voice-agent modules
PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

VLLM_BASE = "http://localhost:8000/v1"
MODEL = "Qwen/Qwen2.5-7B-Instruct"

SYSTEM_PROMPT = """
You are Pepper, a humanoid receptionist robot at CTU FEE in Prague (Karlovo náměstí).
Communicate in English, speak briefly, clearly, and politely.
If the user prefers another language, switch to it.

What you do:
- Provide information about FEE using the `query_search` tool.
- When you are unsure, use `query_search` instead of guessing.
- If the information is not available in the provided materials, say so directly and offer to clarify the question.
- Keep responses concise (typically 1–4 sentences), unless the user asks for more detail.
- Do not mention internal implementation details or library names.

You also have these tools available:
- play_animation: trigger a body gesture (greeting, bow, explain, happy, thinking, dont_know, excited, interested, surprised)
- get_directions_to_room: get walking directions to a room in Building E
""".strip()

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_search",
            "description": "Search the FEE knowledge base for information.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_animation",
            "description": "Trigger a Pepper body gesture. Use one of: greeting, bow, explain, happy, thinking, dont_know, excited, interested, surprised.",
            "parameters": {
                "type": "object",
                "properties": {
                    "animation": {"type": "string", "description": "Animation group name."},
                },
                "required": ["animation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_directions_to_room",
            "description": "Get walking directions to a specific room in Building E.",
            "parameters": {
                "type": "object",
                "properties": {
                    "room_number": {"type": "string", "description": "The room number, e.g. 'E-301'."},
                },
                "required": ["room_number"],
            },
        },
    },
]


def chat_completion(messages, use_tools=True):
    """Call the vLLM OpenAI-compatible API."""
    payload = {
        "model": MODEL,
        "messages": messages,
        "max_tokens": 512,
        "temperature": 0.1,
    }
    if use_tools:
        payload["tools"] = TOOLS
        payload["tool_choice"] = "auto"

    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f"{VLLM_BASE}/chat/completions",
        data=data,
        headers={"Content-Type": "application/json"},
    )
    t0 = time.perf_counter()
    with urllib.request.urlopen(req, timeout=60) as resp:
        result = json.loads(resp.read())
    elapsed = time.perf_counter() - t0

    choice = result["choices"][0]
    usage = result.get("usage", {})
    return choice["message"], elapsed, usage


_TOOL_CALL_RE = re.compile(
    r"<tool_call>.*?(\{.*?\})\s*</tool_call>",
    re.DOTALL,
)

# Catch stray/incomplete <tool_call> tags (no closing tag or no JSON inside)
_STRAY_TOOL_TAG_RE = re.compile(r"</?tool_call[^>]*>", re.DOTALL)


def extract_xml_tool_calls(text):
    """Extract tool calls from raw <tool_call> XML in text content.

    Qwen sometimes emits tool calls as XML in the text instead of
    structured tool_calls in the API response. It may also inject
    garbage characters between the tag and the JSON object.
    """
    calls = []
    for match in _TOOL_CALL_RE.finditer(text):
        try:
            parsed = json.loads(match.group(1))
            calls.append(parsed)
        except json.JSONDecodeError:
            continue
    # Strip matched tool calls from text
    cleaned = _TOOL_CALL_RE.sub("", text)
    # Also strip any leftover stray <tool_call> tags
    cleaned = _STRAY_TOOL_TAG_RE.sub("", cleaned).strip()
    return calls, cleaned


logger = logging.getLogger("console-agent")


def _init_weaviate():
    """Connect to Weaviate and ensure collection is seeded. Returns True if available."""
    try:
        import weaviate
        from weaviate.classes.config import Configure, DataType, Property
        client = weaviate.connect_to_local(host="localhost", port=8080, grpc_port=50051)
        logger.info("weaviate connected")
        client.close()
        return True
    except Exception as exc:
        logger.warning("weaviate init failed: %s", exc)
        return False


def _search_weaviate(query, limit=5):
    """Real Weaviate hybrid search."""
    import weaviate
    from weaviate.classes.query import MetadataQuery

    COLLECTION = "fel_v007"
    with weaviate.connect_to_local(host="localhost", port=8080, grpc_port=50051) as client:
        collection = client.collections.use(COLLECTION)
        response = collection.query.hybrid(
            query=query,
            query_properties=["title", "content"],
            alpha=0.7,
            limit=limit,
            return_metadata=MetadataQuery(score=True, distance=True),
            return_properties=["title", "content", "source", "created_at"],
        )
        results = []
        for obj in response.objects:
            props = obj.properties or {}
            results.append({
                "title": props.get("title", ""),
                "content": props.get("content", ""),
                "source": props.get("source", ""),
                "score": getattr(obj.metadata, "score", None),
            })
        return results


def _load_room_data():
    """Load building map from map.py."""
    map_path = PROJECT_ROOT / "services" / "src" / "dev_console" / "data" / "map" / "map.py"
    spec = importlib.util.spec_from_file_location("building_map", map_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.FLOORS


def handle_tool_call(name, arguments):
    """Execute real tools where possible."""
    print(f"  [TOOL] {name}({arguments})")

    if name == "query_search":
        query = arguments.get("query", "")
        try:
            results = _search_weaviate(query)
            payload = {"query": query, "count": len(results), "results": results}
            # Show a preview of what was found
            for r in results[:3]:
                title = (r.get("title") or "")[:60]
                score = r.get("score")
                print(f"    -> {title} (score={score})")
            return json.dumps(payload, ensure_ascii=False)
        except Exception as exc:
            print(f"    [ERROR] Weaviate search failed: {exc}")
            return json.dumps({"error": "query_search_failed", "message": str(exc)})

    elif name == "play_animation":
        anim = arguments.get("animation", "unknown")
        print(f"    -> [animation: {anim}] (logged, not dispatched)")
        return json.dumps({"ok": True, "status": "logged", "animation": anim})

    elif name == "get_directions_to_room":
        room = arguments.get("room_number", "?")
        try:
            floors = _load_room_data()
            for floor_id, rooms_on_floor in floors.items():
                if room in rooms_on_floor:
                    room_data = rooms_on_floor[room]
                    directions = (room_data.get("directions") or "").strip()
                    room_name = (room_data.get("name") or "").strip()
                    if not directions:
                        return json.dumps({"error": "no_directions",
                                           "message": f"Room {room} is known but directions not filled in."})
                    result = {"room": room, "floor": floor_id, "directions": directions}
                    if room_name:
                        result["name"] = room_name
                    print(f"    -> {room} on {floor_id}: {directions[:80]}...")
                    return json.dumps(result, ensure_ascii=False)
            return json.dumps({"error": "room_not_found",
                               "message": f"Room {room} is not in my map."})
        except Exception as exc:
            print(f"    [ERROR] Map load failed: {exc}")
            return json.dumps({"error": "map_unavailable", "message": str(exc)})

    else:
        return json.dumps({"error": "unknown_tool", "name": name})


def main():
    logging.basicConfig(level=logging.WARNING, format="%(name)s: %(message)s")

    # Check vLLM connectivity
    try:
        req = urllib.request.Request(f"{VLLM_BASE}/models")
        with urllib.request.urlopen(req, timeout=5) as resp:
            models = json.loads(resp.read())
        model_ids = [m["id"] for m in models.get("data", [])]
        print(f"Connected to vLLM. Models: {model_ids}")
    except Exception as e:
        print(f"ERROR: Cannot reach vLLM at {VLLM_BASE}: {e}")
        print("Is the SSH tunnel open?")
        print("  ssh -J navarlu2@ptak.felk.cvut.cz -L 8000:127.0.0.1:8000 -N navarlu2@lie &")
        sys.exit(1)

    # Init Weaviate (real RAG search)
    weaviate_ok = _init_weaviate()
    if weaviate_ok:
        print("Weaviate connected (real RAG search enabled)")
    else:
        print("WARNING: Weaviate unavailable — query_search will fail")

    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    print(f"\n--- Pepper Console Agent (model: {MODEL}) ---")
    print("Tools: query_search (real), get_directions_to_room (real), play_animation (logged)")
    print("Type your message and press Enter. Ctrl+C to quit.\n")

    while True:
        try:
            user_input = input("You: ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nBye!")
            break

        if not user_input:
            continue

        messages.append({"role": "user", "content": user_input})

        # Loop to handle tool calls (model may call tools then respond)
        while True:
            try:
                reply, elapsed, usage = chat_completion(messages)
            except Exception as e:
                print(f"  [ERROR] API call failed: {e}")
                break

            tool_calls = reply.get("tool_calls")
            text = reply.get("content", "") or ""

            # Check for structured API tool calls
            if tool_calls:
                messages.append(reply)
                for tc in tool_calls:
                    fn = tc["function"]
                    args = json.loads(fn["arguments"]) if isinstance(fn["arguments"], str) else fn["arguments"]
                    result = handle_tool_call(fn["name"], args)
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result,
                    })
                continue

            # Check for raw <tool_call> XML in text (Qwen sometimes does this)
            xml_calls, cleaned_text = extract_xml_tool_calls(text)
            if xml_calls:
                # Build a proper assistant + tool message sequence
                messages.append({"role": "assistant", "content": text})
                for call in xml_calls:
                    name = call.get("name", "")
                    args = call.get("arguments", {})
                    if isinstance(args, str):
                        args = json.loads(args)
                    result = handle_tool_call(name, args)
                    # Inject tool result as a user message (no tool_call_id available)
                    messages.append({
                        "role": "user",
                        "content": f"[Tool result for {name}]: {result}",
                    })
                if cleaned_text:
                    print(f"Pepper: {cleaned_text}")
                continue

            # No tool calls — final text response
            prompt_tok = usage.get("prompt_tokens", "?")
            comp_tok = usage.get("completion_tokens", "?")
            print(f"Pepper: {text.strip()}")
            print(f"  [{elapsed:.2f}s | prompt={prompt_tok} completion={comp_tok}]")
            messages.append({"role": "assistant", "content": text.strip()})
            break


if __name__ == "__main__":
    main()
