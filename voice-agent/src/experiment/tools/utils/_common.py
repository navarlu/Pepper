"""Shared helpers for the per-tool files in this folder.

Importing this module also fixes sys.path so each tool file works in
two run modes:
  1. As part of the benchmark package: `from tools.query_search import ...`
  2. As a standalone script: `uv run python tools/query_search.py`

In mode 2 Python runs the file directly and never executes
`tools/__init__.py`, so each tool file does `from tools._common import ...`
and relies on the path setup below to make both `tools.*` and `src.*`
resolve.
"""

from __future__ import annotations

import json
import sys
import unicodedata
from pathlib import Path
from typing import Any

# Make the experiment folder importable so `from tools.X import ...`
# works whether the file is run directly or imported as a module.
# Layout: voice-agent/src/experiment/tools/utils/_common.py
#   parents[2] = experiment/, parents[4] = voice-agent/
_BENCHMARK_DIR = Path(__file__).resolve().parents[2]
if str(_BENCHMARK_DIR) not in sys.path:
    sys.path.insert(0, str(_BENCHMARK_DIR))

# Make the voice-agent root importable so `from src.* import ...` works.
_VOICE_AGENT_DIR = Path(__file__).resolve().parents[4]
if str(_VOICE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(_VOICE_AGENT_DIR))


DEBUG_TOOL_RESULTS = True


def _strip_diacritics(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def _fold_value(value: Any) -> Any:
    if isinstance(value, str):
        return _strip_diacritics(value)
    if isinstance(value, dict):
        return {k: _fold_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_fold_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_fold_value(v) for v in value)
    return value


def _json(data: Any) -> str:
    payload = json.dumps(_fold_value(data), ensure_ascii=False)
    if DEBUG_TOOL_RESULTS:
        rendered = payload if len(payload) <= 4000 else payload[:4000] + "…"
        print(f"  [tool→LLM] {rendered}")
    return payload


def _agent_result(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": item.get("title"),
        "content": item.get("content"),
        "source": item.get("source"),
        "score": item.get("score"),
    }


_WEAVIATE_SEED_CHECKED = False


def _ensure_weaviate_seeded_once() -> None:
    """Seed the Weaviate collection on the first call of this process."""
    global _WEAVIATE_SEED_CHECKED
    if _WEAVIATE_SEED_CHECKED:
        return
    from src.rag import connect_weaviate, seed_collection
    with connect_weaviate() as client:
        seed_collection(client)
    _WEAVIATE_SEED_CHECKED = True


def _run_main(coro: Any) -> None:
    """Run a tool from its `if __name__ == "__main__":` block and print
    the result exactly once.

    Disables the [tool→LLM] auto-print in `_json` so we don't see the
    payload twice (once from `_json`, once from the bare print).
    """
    import asyncio

    global DEBUG_TOOL_RESULTS
    DEBUG_TOOL_RESULTS = False
    print(asyncio.run(coro))
