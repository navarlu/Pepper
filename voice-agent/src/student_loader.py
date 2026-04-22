"""Turn playground-format student tools into LiveKit function_tools at runtime.

The teaching/tool-playground CLI lets students write tools in a simple shape:

    def get_weather(city: str) -> str:
        ...

    GET_WEATHER_SCHEMA = {"type": "function", "function": {...}}

    TOOLS = [{"schema": GET_WEATHER_SCHEMA, "function": get_weather}]

The real agent uses LiveKit's `@function_tool`, which inspects the
function signature (argument names, types, docstring) to build its tool
schema — so we can't just decorate the student's sync function. We
synthesize an async wrapper on the fly with the signature derived from
the student's JSON schema, then decorate that wrapper.

Blocking student code is safe: every call is dispatched via
`asyncio.to_thread`, so `time.sleep`, `requests.get`, etc. won't stall
the agent's event loop.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from livekit.agents import RunContext, function_tool

logger = logging.getLogger("voice-agent.student_loader")


_JSON_TO_PY = {
    "string": "str",
    "integer": "int",
    "number": "float",
    "boolean": "bool",
    "array": "list",
    "object": "dict",
}


def _build_wrapper_source(
    name: str,
    description: str,
    properties: dict,
    required: list[str],
) -> str:
    """Render the source code of an async wrapper matching `name`'s schema.

    The wrapper also fires `_post_tool_event` on every call so the debug
    CLI (text_chat) and any other `pepper.debug` subscriber can see the
    tool's name, arguments, result, duration, and error — same as the
    built-in tools (query_search, play_animation, look_around).
    """
    sig_parts = ["context: RunContext"]
    call_kwarg_parts: list[str] = []
    args_dict_parts: list[str] = []

    for pname, pinfo in properties.items():
        py_type = _JSON_TO_PY.get((pinfo or {}).get("type", "string"), "str")
        if pname in required:
            sig_parts.append(f"{pname}: {py_type}")
        else:
            # Optional params get `= None` — the student's function is
            # expected to handle the `None` case (most won't need to).
            sig_parts.append(f"{pname}: {py_type} | None = None")
        call_kwarg_parts.append(f"{pname}={pname}")
        args_dict_parts.append(f'"{pname}": {pname}')

    params_sig = ", ".join(sig_parts)
    call_part = (", " + ", ".join(call_kwarg_parts)) if call_kwarg_parts else ""
    args_dict_literal = "{" + ", ".join(args_dict_parts) + "}"
    safe_desc = description.replace('"""', '\\"\\"\\"')

    return (
        f"async def {name}({params_sig}) -> str:\n"
        f'    """{safe_desc}"""\n'
        f"    del context\n"
        f"    _args = {args_dict_literal}\n"
        f"    _t0 = _time.monotonic()\n"
        f"    _err = None\n"
        f"    try:\n"
        f"        _result = await _asyncio.to_thread(_student_fn{call_part})\n"
        f"    except Exception as _exc:\n"
        f"        _err = repr(_exc)\n"
        f"        _result = 'ERROR: ' + _err\n"
        f"    _duration_ms = (_time.monotonic() - _t0) * 1000\n"
        f"    _post_tool_event({name!r}, _args, _result, _duration_ms, _err)\n"
        f"    return str(_result)\n"
    )


def _wrap_entry(entry: dict) -> Any:
    """Build one LiveKit function_tool from one `{schema, function}` entry."""
    schema = entry["schema"]
    fn = entry["function"]

    spec = schema["function"]
    name = spec["name"]
    description = spec.get("description", "") or ""
    parameters = spec.get("parameters", {}) or {}
    properties = parameters.get("properties", {}) or {}
    required = parameters.get("required", []) or []

    # Lazy import to avoid a module-level circular dep with tools.py
    # (tools.py imports build_student_tools from this module at call time).
    from .tools import _post_tool_event

    source = _build_wrapper_source(name, description, properties, required)
    namespace: dict[str, Any] = {
        "_asyncio": asyncio,
        "_time": time,
        "RunContext": RunContext,
        "_student_fn": fn,
        "_post_tool_event": _post_tool_event,
    }
    # Compile with an identifiable filename so tracebacks point at the tool.
    code = compile(source, f"<student_tool:{name}>", "exec")
    exec(code, namespace)
    wrapper = namespace[name]
    return function_tool(wrapper)


def build_student_tools() -> list[Any]:
    """Return a list of LiveKit function_tools wrapped from student_bundle.TOOLS.

    Returns an empty list if student_bundle is missing or has no tools,
    or if any individual tool fails to wrap (that tool is skipped and
    logged; the others are still returned).
    """
    try:
        from . import student_bundle
    except Exception as exc:
        logger.warning("student_bundle_import_failed error=%s", exc)
        return []

    raw_tools = getattr(student_bundle, "TOOLS", None) or []
    if not raw_tools:
        logger.info("student_tools_empty")
        return []

    tools: list[Any] = []
    for entry in raw_tools:
        try:
            name = entry["schema"]["function"]["name"]
        except Exception as exc:
            logger.exception("student_tool_bad_entry error=%s entry=%r", exc, entry)
            continue
        try:
            tools.append(_wrap_entry(entry))
            logger.info("student_tool_loaded name=%s", name)
        except Exception as exc:
            logger.exception("student_tool_load_failed name=%s error=%s", name, exc)
    return tools


def get_student_system_prompt() -> str | None:
    """Return the student-supplied system prompt, or None to use the default."""
    try:
        from . import student_bundle
    except Exception as exc:
        logger.warning("student_bundle_import_failed error=%s", exc)
        return None

    prompt = getattr(student_bundle, "SYSTEM_PROMPT", None)
    if isinstance(prompt, str) and prompt.strip():
        return prompt
    return None
