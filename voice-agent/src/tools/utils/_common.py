"""Shared helpers for the per-tool files in this folder."""

from __future__ import annotations

import json
import unicodedata
from typing import Any

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
