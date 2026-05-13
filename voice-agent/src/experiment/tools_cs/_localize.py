"""Helper: re-wrap an English `FunctionTool` with a Czech docstring
without duplicating any logic.

`function_tool` re-parses the docstring on decoration, so swapping the
underlying function's `__doc__` to a Czech string yields a tool whose
top-level description AND per-parameter descriptions are both Czech.
The original async implementation is shared by reference — only the
schema metadata diverges.
"""

from __future__ import annotations

import types
from typing import Any

from livekit.agents import function_tool


def localize(en_tool: Any, docstring: str, name: str | None = None) -> Any:
    orig = en_tool._func
    new_fn = types.FunctionType(
        orig.__code__,
        orig.__globals__,
        orig.__name__,
        orig.__defaults__,
        orig.__closure__,
    )
    new_fn.__doc__ = docstring
    new_fn.__annotations__ = dict(orig.__annotations__)
    new_fn.__kwdefaults__ = orig.__kwdefaults__
    return function_tool(new_fn, name=name or en_tool._info.name)
