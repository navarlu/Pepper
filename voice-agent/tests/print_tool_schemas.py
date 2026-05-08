"""Print the exact tool schemas that LiveKit ships to the LLM.

Run from the voice-agent dir so `src.*` resolves:

    cd voice-agent
    uv run python tests/print_tool_schemas.py

For each tool in `LIVEKIT_TOOLS_TOOLONLY` we print:

  * `info.name`               — tool name as registered
  * `info.description`        — exactly the string LiveKit puts in the
                                "description" field of the OpenAI/Anthropic
                                tool schema. This is parsed from the
                                docstring via `docstring_parser` — i.e.
                                everything BEFORE the first parameter
                                section. Anything in an `Args:` /
                                `Parameters:` block is NOT in here, it goes
                                into per-parameter descriptions.
  * legacy OpenAI schema      — `build_legacy_openai_schema(tool)`, the
                                literal dict the framework hands to the LLM
                                client.

If a description looks truncated in the printed output you can compare it
against the source docstring at the path printed in the header — so you
can tell whether the docstring itself is bad, or whether the
docstring_parser is dropping content (e.g. when an `Args:`/`Returns:`
block is missing a leading blank line).
"""

from __future__ import annotations

import inspect
import json
import sys
from pathlib import Path

# Make `src.*` importable when this file is run directly.
_VOICE_AGENT_DIR = Path(__file__).resolve().parents[1]
if str(_VOICE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_VOICE_AGENT_DIR))

from livekit.agents.llm.utils import build_legacy_openai_schema  # noqa: E402

from src.experiment.tools import LIVEKIT_TOOLS_TOOLONLY  # noqa: E402


SEP = "=" * 78
SUB = "-" * 78


def _source_path(tool) -> str:
    try:
        return inspect.getsourcefile(tool.__wrapped__) or "?"
    except Exception:
        try:
            return inspect.getsourcefile(tool) or "?"
        except Exception:
            return "?"


def main() -> None:
    print(SEP)
    print(f"LIVEKIT_TOOLS_TOOLONLY — {len(LIVEKIT_TOOLS_TOOLONLY)} tools")
    print(SEP)

    for tool in LIVEKIT_TOOLS_TOOLONLY:
        info = tool.info
        src_path = _source_path(tool)

        print()
        print(SEP)
        print(f"TOOL: {info.name}")
        print(f"  source: {src_path}")
        print(SEP)

        desc = info.description or ""
        print(f"description (chars: {len(desc)}, lines: {desc.count(chr(10)) + 1}):")
        print(SUB)
        print(desc if desc else "<EMPTY — LiveKit will send description=''>")
        print(SUB)

        schema = build_legacy_openai_schema(tool)
        print("legacy OpenAI schema (what LiveKit sends to the LLM):")
        print(SUB)
        print(json.dumps(schema, indent=2, ensure_ascii=False))
        print(SUB)

        params = schema.get("function", {}).get("parameters", {}).get("properties", {})
        if params:
            print("per-parameter descriptions:")
            for pname, pdef in params.items():
                pdesc = pdef.get("description", "")
                marker = "" if pdesc else "  <-- EMPTY"
                print(f"  - {pname}: ({len(pdesc)} chars){marker}")
                if pdesc:
                    for line in pdesc.splitlines() or [pdesc]:
                        print(f"      {line}")

    print()
    print(SEP)
    print("done.")
    print(SEP)


if __name__ == "__main__":
    main()
