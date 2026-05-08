"""Tool-event listener hook + Letta-style heartbeat shim.

Every tool emits a `_emit_tool_event(name, args)` at entry and a
`_heartbeat_or_none(payload, request_heartbeat)` at exit. The
experiment recorder (launcher.py) registers listeners via
`set_tool_event_listener` / `set_tool_result_listener` to capture
both into the JSONL transcript.

The `_heartbeat_or_none` shim implements Letta's TerminalToolRule
on top of LiveKit: when `request_heartbeat=False` it returns None,
which makes `reply_required=False` in LiveKit's loop and halts the
turn without another LLM pass. With `request_heartbeat=True` (the
default) it returns the JSON payload so LiveKit re-invokes the LLM
with the result.

Production agent.py uses the same listener pattern in
voice-agent/src/tools.py (`set_tool_event_listener`).
"""

from __future__ import annotations

import contextvars
from typing import Any

from ._common import _json


_external_tool_listener: Any = None
_external_tool_result_listener: Any = None

# Per-task context: which tool is currently executing. Set by
# _emit_tool_event at entry, read by _heartbeat_or_none at exit. A
# contextvars.ContextVar isolates per asyncio.Task, so concurrent tool
# calls don't cross-contaminate. This avoids having to plumb the tool
# name through every _heartbeat_or_none call site.
_current_tool_name: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "current_tool_name", default=None,
)


def set_tool_event_listener(listener) -> None:
    """Register a callback fired on every tool CALL (entry).

    Signature: listener(name, args). Errors in the listener are
    swallowed — body language must never break the agent's reply path.
    """
    global _external_tool_listener
    _external_tool_listener = listener


def set_tool_result_listener(listener) -> None:
    """Register a callback fired on every tool RESULT (exit), with the
    payload that the tool returned to the LLM (or None if the tool was
    a terminal one like send_message_to_user). Signature:
    listener(name, result_dict_or_None)."""
    global _external_tool_result_listener
    _external_tool_result_listener = listener


def _emit_tool_event(name: str, args: dict[str, Any]) -> None:
    """Internal: forward the tool call to the registered listener AND
    remember the tool name in the contextvar so _heartbeat_or_none can
    emit a matching result event later, without per-tool plumbing."""
    _current_tool_name.set(name)
    if _external_tool_listener is None:
        return
    try:
        _external_tool_listener(name, args)
    except Exception as exc:  # noqa: BLE001
        # Cosmetic — never let a recorder crash a tool.
        print(f"  [tool-event] listener error: {exc!r}")


def _emit_tool_result(name: str, result: Any) -> None:
    """Internal: forward the tool's return payload to the result
    listener so the experiment recorder can capture what the LLM
    actually saw back from each tool."""
    if _external_tool_result_listener is None:
        return
    try:
        _external_tool_result_listener(name, result)
    except Exception as exc:  # noqa: BLE001
        print(f"  [tool-result] listener error: {exc!r}")


def _heartbeat_or_none(payload: dict[str, Any], request_heartbeat: bool) -> Any:
    """Letta-style termination shim.

    When `request_heartbeat=True` (the default), return the JSON
    payload so LiveKit re-invokes the LLM with the result.
    When False, return None so `reply_required` is False and the
    loop halts after this tool — same effect as Letta's
    TerminalToolRule but driven by the model's own kwarg.

    Side effect: emits a tool_result event with the payload (whether or
    not the loop continues), tagged with the tool name from the
    contextvar set by _emit_tool_event. This is how the experiment
    recorder captures what the LLM saw from each tool.
    """
    name = _current_tool_name.get()
    if name:
        _emit_tool_result(name, payload)
    if not request_heartbeat:
        print(f"  [heartbeat] False — halting loop (payload dropped: {list(payload.keys())})")
        return None
    return _json(payload)
