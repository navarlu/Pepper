"""Qwen 2.5 JSON-sanitization workarounds.

Qwen 2.5 7B (and similar fine-tunes) occasionally emits malformed
tool-call JSON — the classic failure mode is an extra trailing brace,
e.g. `{"animation": "greeting"}}` or trailing text after the closing
brace. This breaks two places in the pipeline:

  1. LiveKit's `llm.utils.prepare_function_arguments` raises
     `ValueError("trailing characters")` when it tries to parse the
     malformed string, which aborts the tool call.
  2. vLLM 0.19 returns HTTP 400 on a follow-up `chat/completions` if
     the prior `FunctionCall.arguments` in chat history is
     syntactically wrong (vLLM re-parses history to build its prompt).

We fix both with a single strategy: find the first `{` and walk
forward until brace-depth returns to zero, then throw away the rest.

This whole file is a patch against a specific quirk of a specific
model version. When we upgrade away from Qwen 2.5 7B (or vLLM fixes
the history-parse crash), it can be deleted wholesale — nothing else
depends on it.
"""

from __future__ import annotations

import logging

from livekit.agents import llm
from livekit.agents.llm.chat_context import FunctionCall
from livekit.agents.llm import utils as _llm_utils

logger = logging.getLogger("voice-agent")


# region: sanitize_json
def sanitize_json(raw: str) -> str:
    """Extract the first balanced JSON object from a string.

    If `raw` doesn't start with `{` (e.g. an empty string or plain
    text), it's returned unchanged. Otherwise we find the matching
    `}` for the opening brace and drop anything after it. Logs when a
    sanitization actually happens, so the malformed inputs are
    traceable.
    """
    stripped = raw.strip()
    if not stripped.startswith("{"):
        return raw
    depth = 0
    for i, ch in enumerate(stripped):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                cleaned = stripped[: i + 1]
                if cleaned != raw:
                    logger.info("sanitize_json original=%r cleaned=%r", raw[:120], cleaned)
                return cleaned
    return raw
# endregion


def sanitize_chat_ctx(chat_ctx: llm.ChatContext) -> None:
    """In-place fix every `FunctionCall.arguments` in `chat_ctx`.

    Call this before sending chat history to vLLM — prevents the
    re-parse crash on stored malformed arguments.
    """
    for item in chat_ctx.items:
        if isinstance(item, FunctionCall):
            item.arguments = sanitize_json(item.arguments)


def install_function_args_patch() -> None:
    """Monkey-patch `prepare_function_arguments` to recover from
    "trailing characters" `ValueError`s by sanitizing and retrying.

    Idempotent — repeat calls are safe. Must run before the first
    tool call goes through LiveKit's chat pipeline; the agent
    entrypoint calls this at module import time.
    """
    original = _llm_utils.prepare_function_arguments
    # Already patched: detect the sentinel to stay idempotent.
    if getattr(original, "_pepper_sanitized", False):
        return

    def _sanitized(*, fnc, json_arguments, call_ctx=None):
        try:
            return original(fnc=fnc, json_arguments=json_arguments, call_ctx=call_ctx)
        except ValueError as exc:
            if "trailing" not in str(exc).lower():
                raise
            cleaned = sanitize_json(json_arguments)
            logger.warning(
                "sanitized_tool_args original=%r cleaned=%r",
                json_arguments, cleaned,
            )
            return original(fnc=fnc, json_arguments=cleaned, call_ctx=call_ctx)

    _sanitized._pepper_sanitized = True  # type: ignore[attr-defined]
    _llm_utils.prepare_function_arguments = _sanitized


def wrap_llm_chat_with_history_sanitizer(local_llm) -> None:
    """Wrap `local_llm.chat` so chat history is sanitized before every
    request to vLLM.

    Mutates the LLM instance in place. Mirror of `sanitize_chat_ctx`
    applied lazily at call time.
    """
    original_chat = local_llm.chat

    def _chat_with_sanitized_history(*, chat_ctx, **kwargs):
        sanitize_chat_ctx(chat_ctx)
        return original_chat(chat_ctx=chat_ctx, **kwargs)

    local_llm.chat = _chat_with_sanitized_history  # type: ignore[method-assign]
