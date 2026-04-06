"""
Compare the tool schemas: hand-crafted (test script) vs LiveKit-generated (Pydantic).

This reveals what extra fields LiveKit adds that vLLM's hermes parser sees.

Usage:
  uv run python voice-agent/tests/test_schema_compare.py
"""

import json
from livekit.agents import RunContext, function_tool


# ── LiveKit-generated schemas ────────────────────────────────────────────────

@function_tool(name="play_animation")
async def play_animation_lk(context: RunContext, animation: str) -> str:
    """Check and set the robot body posture. Returns the current body state which you need before speaking. animation must be one of: greeting, bow, explain, happy, thinking, dont_know"""
    del context
    return ""

@function_tool
async def query_search_lk(context: RunContext, query: str) -> str:
    """Vyhledej informace z interni znalostni baze FEL."""
    del context
    return ""

@function_tool
async def get_directions_to_room_lk(context: RunContext, room_number: str) -> str:
    """Get directions on how to walk to a specific room in Building E. Call this whenever a visitor asks where a room is or how to get there. Returns step-by-step walking directions from the main entrance."""
    del context
    return ""


# ── Hand-crafted schemas (from working test_agent_scenario.py) ───────────────

HAND_CRAFTED = [
    {
        "type": "function",
        "function": {
            "name": "query_search",
            "description": "Search the internal FEL knowledge base.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_animation",
            "description": (
                "Check and set the robot body posture. "
                "Returns the current body state which you need before speaking. "
                "animation must be one of: greeting, bow, explain, happy, thinking, dont_know"
            ),
            "parameters": {
                "type": "object",
                "properties": {"animation": {"type": "string"}},
                "required": ["animation"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_directions_to_room",
            "description": (
                "Get directions on how to walk to a specific room in Building E. "
                "Call this whenever a visitor asks where a room is or how to get there. "
                "Returns step-by-step walking directions from the main entrance."
            ),
            "parameters": {
                "type": "object",
                "properties": {"room_number": {"type": "string"}},
                "required": ["room_number"],
            },
        },
    },
]


def get_livekit_schema(tool) -> dict:
    """Extract the OpenAI-compatible schema that LiveKit would send to vLLM."""
    from livekit.agents.llm.utils import build_legacy_openai_schema
    return build_legacy_openai_schema(tool)


def main():
    lk_tools = [play_animation_lk, query_search_lk, get_directions_to_room_lk]

    print("=" * 70)
    print("SCHEMA COMPARISON: LiveKit-generated vs Hand-crafted")
    print("=" * 70)

    for lk_tool, hand in zip(lk_tools, HAND_CRAFTED):
        lk_schema = get_livekit_schema(lk_tool)
        name = lk_schema["function"]["name"]

        print(f"\n{'─' * 70}")
        print(f"Tool: {name}")
        print(f"{'─' * 70}")

        print("\n  LiveKit schema:")
        print(json.dumps(lk_schema, indent=4))

        print("\n  Hand-crafted schema:")
        print(json.dumps(hand, indent=4))

        # Show differences
        lk_params = lk_schema["function"].get("parameters", {})
        hc_params = hand["function"].get("parameters", {})

        lk_keys = set(lk_params.keys())
        hc_keys = set(hc_params.keys())
        extra = lk_keys - hc_keys
        if extra:
            print(f"\n  EXTRA top-level keys in LiveKit: {extra}")

        # Check property-level differences
        lk_props = lk_params.get("properties", {})
        hc_props = hc_params.get("properties", {})
        for prop_name in lk_props:
            lk_prop = lk_props[prop_name]
            hc_prop = hc_props.get(prop_name, {})
            lk_pk = set(lk_prop.keys())
            hc_pk = set(hc_prop.keys())
            extra_prop = lk_pk - hc_pk
            if extra_prop:
                print(f"  EXTRA keys in property '{prop_name}': {extra_prop}")
                for k in extra_prop:
                    print(f"    {k} = {lk_prop[k]!r}")

    print(f"\n{'=' * 70}")
    print("If LiveKit adds 'title' fields, these go into the tool definition")
    print("that vLLM's hermes parser sees in the chat template.")
    print("=" * 70)


if __name__ == "__main__":
    main()
