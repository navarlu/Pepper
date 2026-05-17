"""Console-mode smoke harness for the streaming agent.

Runs the streaming variant of the experiment agent inside LiveKit's
built-in `console --text` mode so you can type prompts in the terminal
and watch what the model does — without spinning up the launcher,
user-client, audio-bridge, recorder, tablet-server, or LiveKit room
machinery.

Mirrors [agent_streaming.py](../agent_streaming.py) wiring:

  * Same `StreamingAgent` class (first-turn greeting splice via
    `update_instructions` + `tools=[]` on turn 1 to suppress Llama's
    greeting-as-tool-call bias).
  * Same `prompt_streaming.SYSTEM_PROMPT` / `GREETING_INSTRUCTIONS`.
  * Same vLLM `openai.LLM` setup pointed at LOCAL_LLM_BASE_URL.
  * Same per-session `=== LLM CONTEXT DUMP ===` so you can verify
    every tool description landed without phantom-tool references.
  * Same per-turn `[LLM] turn user_msgs=N tools_passed=M ...` line
    so you can see the tools=[] trick switch back on for turn 2+.

Differences:

  * No STT / TTS / audio plumbing — text-only.
  * `end_conversation_streaming` is excluded because it depends on
    `_streaming_runtime` (set_room / set_end_session_callback) which
    only the real worker entrypoint wires up. The point of this
    harness is verifying info-tool firing + prompt visibility, not
    shutdown.
  * No `pepper.experiment` event publishing, no recorder, no
    metrics collector — those are inert without the launcher.

Prereqs: vLLM Llama 3.1 8B AWQ reachable at LOCAL_LLM_BASE_URL
(default http://localhost:8000/v1). On the RPi that means the SSH
tunnel to woska must be open. The harness will exit cleanly with a
helpful error if vLLM is not reachable.

Run directly (interactive):
    uv run python voice-agent/src/experiment/tests/streaming_console.py console --text

Or via the tmux wrapper:
    bash voice-agent/src/experiment/tests/run_streaming_smoke.sh
"""

from __future__ import annotations

# Quiet startup noise BEFORE any livekit-agents import.
import os as _os
_os.environ.setdefault("PYTHONWARNINGS", "ignore::DeprecationWarning")
import warnings  # noqa: E402
warnings.filterwarnings("ignore", category=DeprecationWarning)

import json  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import urllib.request  # noqa: E402
from pathlib import Path  # noqa: E402

# Console mode does not actually dial out to LiveKit Cloud, but the
# worker bootstrap still validates these env vars at startup.
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("LIVEKIT_API_KEY", "console-fake")
os.environ.setdefault("LIVEKIT_API_SECRET", "console-fake")

# Path glue: this file is one level deeper than agent_streaming.py, so
# we need both the experiment dir (for `from prompt_streaming import …`
# and `from tools.* import …`) and the voice-agent root (for the
# `src.live.*` reaches inside tool modules) on sys.path.
THIS_DIR = Path(__file__).resolve().parent
EXPERIMENT_DIR = THIS_DIR.parent
VOICE_AGENT_DIR = EXPERIMENT_DIR.parent.parent
for p in (str(EXPERIMENT_DIR), str(VOICE_AGENT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Trim the chattier loggers — text console renders its own clean
# transcript, no need to drown it in framework debug.
logging.getLogger("livekit.agents").setLevel(logging.WARNING)
logging.getLogger("livekit.plugins.openai").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

# Silence the `[tool→LLM] {...}` dump from tools/_common — the LiveKit
# console UI prints its own truncated tool-result line under `➜ name ✓`.
import tools.utils._common as _tc  # noqa: E402
_tc.DEBUG_TOOL_RESULTS = False

from typing import AsyncIterable  # noqa: E402

from livekit.agents import (  # noqa: E402
    Agent,
    AgentSession,
    JobContext,
    ModelSettings,
    WorkerOptions,
    cli,
)
from livekit.agents.voice.generation import update_instructions  # noqa: E402
from livekit.plugins import openai  # noqa: E402

# Local plugins — Qwen compatibility patch is still needed for
# vLLM-style function-args parsing on Llama tool calls.
from src.live.qwen_compat import install_function_args_patch  # noqa: E402

install_function_args_patch()

from prompt_streaming import SYSTEM_PROMPT, GREETING_INSTRUCTIONS  # noqa: E402

from tools.find_path_to_room import find_path_to_room  # noqa: E402
from tools.lookup_person import lookup_person  # noqa: E402
from tools.mensa_menu import mensa_menu  # noqa: E402
from tools.subject_schedule import subject_schedule  # noqa: E402
from tools.get_time import get_time  # noqa: E402
from tools.query_search import query_search  # noqa: E402

# end_conversation_streaming is INTENTIONALLY omitted — depends on
# _streaming_runtime callbacks that only the real worker wires up.
STREAMING_TOOLS = [
    find_path_to_room,
    lookup_person,
    mensa_menu,
    subject_schedule,
    get_time,
    query_search,
]


LOCAL_LLM_BASE_URL = os.environ.get(
    "LOCAL_LLM_BASE_URL", "http://localhost:8000/v1"
)


def _resolve_local_model_id() -> str:
    url = f"{LOCAL_LLM_BASE_URL.rstrip('/')}/models"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"vLLM unreachable at {url}: {exc!r}. "
            f"Open the SSH tunnel to woska first."
        ) from exc
    data = payload.get("data") or []
    if not data:
        raise RuntimeError(f"vLLM /models returned no entries: {payload!r}")
    return str(data[0]["id"])


def _format_tool_summary(tools_list) -> str:
    """Mirror of the helper in agent_streaming.py — one line per
    tool with the first line of its description, so the LLM context
    dump shows exactly what schemas the model sees."""
    lines: list[str] = []
    for t in tools_list:
        info = getattr(t, "info", None)
        name = getattr(info, "name", None) or getattr(t, "__name__", "?")
        desc = (getattr(info, "description", None)
                or (getattr(t, "__doc__", "") or "").strip())
        first = desc.splitlines()[0].strip() if desc else "(no doc)"
        lines.append(f"  - {name}: {first}")
    return "\n".join(lines)


class StreamingAgent(Agent):
    """Verbatim port of the worker's StreamingAgent — keeps the
    greeting splice + tools=[] first-turn workaround so console
    behaviour matches the deployed agent."""

    def __init__(self) -> None:
        super().__init__(instructions=SYSTEM_PROMPT, tools=STREAMING_TOOLS)
        self._greeting_done = False

    async def llm_node(self, chat_ctx, tools, model_settings):
        user_msgs = sum(
            1
            for item in chat_ctx.items
            if getattr(item, "type", None) == "message"
            and getattr(item, "role", None) == "user"
        )
        greeting_turn = user_msgs == 1 and not self._greeting_done
        if greeting_turn:
            merged = f"{SYSTEM_PROMPT}\n\n{GREETING_INSTRUCTIONS}"
            update_instructions(chat_ctx, instructions=merged, add_if_missing=True)
            self._greeting_done = True
            tools_passed: list = []
        else:
            tools_passed = tools
        print(
            f"  [LLM] turn user_msgs={user_msgs} "
            f"tools_passed={len(tools_passed)} "
            f"greeting_spliced={greeting_turn}",
            flush=True,
        )
        return Agent.default.llm_node(self, chat_ctx, tools_passed, model_settings)


async def entrypoint(ctx: JobContext) -> None:
    try:
        model_id = _resolve_local_model_id()
    except Exception as exc:
        print(f"[streaming-console] ERROR: {exc}", file=sys.stderr, flush=True)
        ctx.shutdown(reason="vllm_unreachable")
        return

    print(
        f"[streaming-console] === LLM CONTEXT DUMP ===\n"
        f"--- model={model_id} base_url={LOCAL_LLM_BASE_URL} ---\n"
        f"--- SYSTEM_PROMPT ({len(SYSTEM_PROMPT)} chars) ---\n"
        f"{SYSTEM_PROMPT}\n"
        f"--- GREETING_INSTRUCTIONS (spliced on turn 1) ---\n"
        f"{GREETING_INSTRUCTIONS}\n"
        f"--- TOOLS ({len(STREAMING_TOOLS)}) ---\n"
        f"{_format_tool_summary(STREAMING_TOOLS)}\n"
        f"=== END DUMP ===\n"
        f"Type prompts at the > prompt below. Ctrl+B then T to toggle "
        f"text/audio, Ctrl+C to quit.",
        flush=True,
    )

    session = AgentSession(
        llm=openai.LLM(
            model=model_id,
            base_url=LOCAL_LLM_BASE_URL,
            api_key="not-needed",
            temperature=0.2,
            parallel_tool_calls=False,
            _strict_tool_schema=False,
        ),
        # Same per-turn cap as the existing livekit_console — keeps a
        # runaway tool loop from spinning forever.
        max_tool_steps=4,
    )

    await session.start(agent=StreamingAgent(), room=ctx.room)


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
