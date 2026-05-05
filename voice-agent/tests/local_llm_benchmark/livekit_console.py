"""Minimal LiveKit agent for console-mode debugging.

Same Llama-tuned prompt and 5 dummy tools as the benchmark, but driven
through a real `livekit.agents.AgentSession` so you can verify how the
framework actually behaves end-to-end. Reuses the prompt from
`prompt.py` so when you tweak it, both the benchmark and this console
agent see the change.

Run text mode (no STT/TTS, just type at the prompt):
    uv run python voice-agent/tests/local_llm_benchmark/livekit_console.py console --text

Run audio mode (uncomment STT/TTS lines below first):
    uv run python voice-agent/tests/local_llm_benchmark/livekit_console.py console

Prereqs: SSH tunnel from the RPi to woska's vLLM open on localhost:8000.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import urllib.request
from pathlib import Path

# Console mode does not actually connect to LiveKit Cloud, but the worker
# still validates LIVEKIT_URL/api creds at init. Provide harmless defaults
# so `console --text` can run without any .env on the RPi.
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("LIVEKIT_API_KEY", "console-fake")
os.environ.setdefault("LIVEKIT_API_SECRET", "console-fake")

from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    WorkerOptions,
    cli,
    llm,
)
from livekit.agents.voice.generation import update_instructions
from livekit.plugins import openai, silero

sys.path.insert(0, str(Path(__file__).resolve().parent))
from animation_director import AnimationDirector  # noqa: E402
from prompt import GREETING_INSTRUCTIONS, SYSTEM_PROMPT  # noqa: E402
from tools import LIVEKIT_TOOLS  # noqa: E402

logger = logging.getLogger("livekit-console")

VLLM_BASE_URL = "http://localhost:8000/v1"

# Flip to True to print the chat_ctx dumps and `>>>` traces that helped
# us debug the greeting-injection wiring. Off by default — keeps the
# Rich console panel clean during normal demo use.
DEBUG = False

if DEBUG:
    logging.basicConfig(level=logging.DEBUG, force=True)
    logging.getLogger("livekit.agents").setLevel(logging.DEBUG)
    logging.getLogger("livekit.plugins.openai").setLevel(logging.DEBUG)
    logging.getLogger("openai").setLevel(logging.DEBUG)
    logging.getLogger("httpx").setLevel(logging.DEBUG)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def _resolve_model_id() -> str:
    with urllib.request.urlopen(f"{VLLM_BASE_URL}/models", timeout=5) as resp:
        payload = json.loads(resp.read())
    return payload["data"][0]["id"]


# ----- Agent subclass ---------------------------------------------------


def _dprint(*args, **kwargs) -> None:
    """print() that only fires when DEBUG is True."""
    if DEBUG:
        print(*args, **kwargs)


def _dump_chat_ctx(label: str, chat_ctx: llm.ChatContext) -> None:
    """Loud print of every item in a chat context so we can see exactly
    what the LLM is about to receive. Uses print() because the LiveKit
    Rich console hides stderr (where logger.info goes) once the input
    panel takes over. Gated on DEBUG."""
    if not DEBUG:
        return
    print(f"\n========== {label} ({len(chat_ctx.items)} items) ==========")
    for i, item in enumerate(chat_ctx.items):
        item_type = getattr(item, "type", "?")
        role = getattr(item, "role", "-")
        item_id = getattr(item, "id", "-")
        text = (getattr(item, "text_content", None) or "") or ""
        text = text.replace("\n", " ⏎ ")
        print(f"  [{i}] type={item_type} role={role} id={item_id}")
        print(f"      content={text!r}")
        # Tool calls and tool outputs aren't always carried in
        # text_content — surface their raw fields so we can see exactly
        # what arguments the LLM emitted and what JSON went back.
        for attr in ("name", "arguments", "tool_call_id", "output", "content"):
            value = getattr(item, attr, None)
            if value not in (None, "", text):
                rendered = repr(value)
                if len(rendered) > 4000:
                    rendered = rendered[:4000] + "…"
                print(f"      {attr}={rendered}")
    print("=" * (24 + len(label)))


def _count_user_messages(chat_ctx: llm.ChatContext) -> int:
    return sum(
        1
        for item in chat_ctx.items
        if getattr(item, "type", None) == "message"
        and getattr(item, "role", None) == "user"
    )


class GreetingAgent(Agent):
    """Agent that, on the first user turn only, merges
    GREETING_INSTRUCTIONS into the existing system instructions message
    via livekit.agents.voice.generation.update_instructions — the same
    mechanism `generate_reply(instructions=...)` uses internally.

    Why merge instead of append: Llama 3.1's chat template renders the
    system slot at the very start. A second system message inserted
    mid-conversation gets emitted but weighed less, which is why our
    earlier append-at-end approach was ignored. Replacing the content of
    the existing system message at id `lk.agent_task.instructions`
    guarantees the LLM sees a single, well-positioned system prompt.

    Why llm_node and not on_user_turn_completed: the latter only fires
    for STT-driven audio turns (it's invoked from _user_turn_completed_
    task, wired to audio end-of-turn detection). Text input via
    `console --text` skips that codepath, so we override llm_node — the
    universal chokepoint that fires for every LLM call regardless of
    input modality.

    Why a `_greeting_done` flag: a single user turn may trigger several
    llm_node calls (initial call, then post-tool-result follow-ups). We
    only want to merge the greeting once, on the very first call. After
    that, the merge is preserved in this turn's chat_ctx (LiveKit reuses
    the same chat_ctx object for tool follow-ups via
    chat_ctx.items.extend at agent_activity.py:2161). On turn 2 the
    framework starts from a fresh agent.chat_ctx copy without our merge,
    user_msgs will be > 1, and we leave it alone.
    """

    def __init__(self) -> None:
        super().__init__(instructions=SYSTEM_PROMPT, tools=LIVEKIT_TOOLS)
        self._greeting_done = False
        # Captured reference to the most recent chat_ctx so the
        # AnimationDirector can read the latest user/assistant text
        # from outside the llm_node call stack.
        self._latest_chat_ctx: llm.ChatContext | None = None
        _dprint(">>> GreetingAgent constructed")

    def latest_user_and_reply_text(self) -> tuple[str, str]:
        """Walk the captured chat_ctx backward and return
        (last user message text, last assistant message text)."""
        user_text = ""
        reply_text = ""
        ctx = self._latest_chat_ctx
        if ctx is None:
            return user_text, reply_text
        for item in reversed(ctx.items):
            if getattr(item, "type", None) != "message":
                continue
            role = getattr(item, "role", None)
            text = (getattr(item, "text_content", None) or "").strip()
            if not text:
                continue
            if role == "assistant" and not reply_text:
                reply_text = text
            elif role == "user" and not user_text:
                user_text = text
            if user_text and reply_text:
                break
        return user_text, reply_text

    async def llm_node(self, chat_ctx, tools, model_settings):
        self._latest_chat_ctx = chat_ctx
        user_msgs = _count_user_messages(chat_ctx)
        tool_choice = getattr(model_settings, "tool_choice", None)
        _dprint(
            f"\n>>> llm_node fired user_msgs={user_msgs} "
            f"tool_choice={tool_choice!r} greeting_done={self._greeting_done}"
        )

        first_turn = user_msgs == 1 and not self._greeting_done
        if first_turn:
            merged = f"{SYSTEM_PROMPT}\n\n{GREETING_INSTRUCTIONS}"
            update_instructions(chat_ctx, instructions=merged, add_if_missing=True)
            self._greeting_done = True
            _dprint(
                f">>> MERGED GREETING + stripping tools on turn 1 "
                f"(+{len(GREETING_INSTRUCTIONS)} chars)"
            )
        else:
            _dprint(">>> no injection")

        _dump_chat_ctx("LLM_NODE INPUT (final, sent to LLM)", chat_ctx)
        return Agent.default.llm_node(self, chat_ctx, tools, model_settings)


# ----- Entrypoint -------------------------------------------------------


async def entrypoint(ctx: JobContext) -> None:
    model_id = _resolve_model_id()
    logger.info("connected to vllm model=%s", model_id)

    session = AgentSession(
        vad=silero.VAD.load(),
        llm=openai.LLM(
            model=model_id,
            base_url=VLLM_BASE_URL,
            api_key="not-needed",
            temperature=0.2,
            parallel_tool_calls=False,
        ),
        # --- Audio mode: uncomment one STT and one TTS below ---
        # stt=openai.STT(base_url=VLLM_BASE_URL, api_key="not-needed", model="whisper-1"),  # cloud
        # tts=openai.TTS(base_url=VLLM_BASE_URL, api_key="not-needed", model="tts-1", voice="alloy"),
        # Or pull the project's local Whisper + Piper:
        # from voice-agent.src.local_speech import FasterWhisperSTT, PiperTTS
        # stt=FasterWhisperSTT(model="tiny", device="cpu", compute_type="int8"),
        # tts=PiperTTS(model_path=Path("voice-agent/models/piper/en_US-hfc_female-medium.onnx")),
    )

    agent = GreetingAgent()
    director = AnimationDirector(llm_instance=session.llm)

    @session.on("conversation_item_added")
    def _on_item_added(ev):  # type: ignore[no-untyped-def]
        item = getattr(ev, "item", None)
        if item is None or getattr(item, "role", None) != "assistant":
            return
        if not (getattr(item, "text_content", "") or "").strip():
            return
        ctx = agent._latest_chat_ctx
        if ctx is None:
            return
        director.schedule(ctx)

    await session.start(agent=agent, room=ctx.room)


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
