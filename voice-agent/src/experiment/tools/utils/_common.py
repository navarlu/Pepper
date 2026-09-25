"""Shared helpers for the per-tool files in this folder.

Importing this module also fixes sys.path so both `tools.*` and
`src.*` resolve for the tool modules, whichever agent imports them.
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
