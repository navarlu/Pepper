"""Minimal streaming OpenAI 4o agent — for latency diagnosis.

This is NOT the production experiment worker. It is a clean,
self-contained version of the 4o-chained pipeline that uses the
LIVEKIT-STANDARD streaming path:

    silero VAD  →  gpt-4o-transcribe (STT, streaming)
                →  gpt-4o-mini (LLM, streaming)
                →  gpt-4o-mini-tts (TTS, streaming)

Differences vs. the production [agent_4o.py](../agent_4o.py):

  * NO `send_message_to_user` tool. The production agent uses a
    tool-only pattern where the model has to emit a full JSON
    tool-call (with the entire `text` argument) BEFORE any audio
    can be synthesized — that blocks LLM-to-TTS streaming because
    the TTS only starts after the LLM has finished. Here the model
    just emits plain assistant text and LiveKit pipes the streamed
    tokens straight into the streaming TTS, so first-audio arrives
    as soon as the first sentence fragment is emitted.

  * ONE tool: `get_time`. Inline, no tool-result-listener wiring,
    no body-language gestures, no heartbeat plumbing — just enough
    to exercise the tool-call path so we can measure its latency.

  * NO bridge / tablet / audio-bridge integration. No
    `_pipeline.run_pipeline`, no `SessionRuntime`, no `pepper.state`
    publishing. The session runs against whatever room the
    `console` subcommand wires up (local mic+speaker by default).

  * Latency observability via LiveKit's built-in
    `metrics_collected` event — STT TTFB, LLM TTFT, TTS TTFB are
    all logged to stdout per-turn.

Run modes:

    # Local mic/speaker, no LiveKit server required:
    uv run python voice-agent/src/experiment/tests/agent_4o_streaming.py console

    # Same but typed input (paste sentences instead of speaking):
    uv run python voice-agent/src/experiment/tests/agent_4o_streaming.py console --text

    # Connect to a real LiveKit room (needs LIVEKIT_URL + creds):
    uv run python voice-agent/src/experiment/tests/agent_4o_streaming.py dev

Prereqs:
  * OPENAI_API_KEY in .env at the project root.

"""

from __future__ import annotations

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import asyncio  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from datetime import datetime  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import AsyncIterable  # noqa: E402

from dotenv import load_dotenv  # noqa: E402
from livekit.agents import (  # noqa: E402
    Agent,
    AgentSession,
    JobContext,
    ModelSettings,
    RunContext,
    WorkerOptions,
    cli,
    function_tool,
    metrics,
)
from livekit.plugins import openai, silero  # noqa: E402


# ── Path / env setup ─────────────────────────────────────────────────
THIS_DIR = Path(__file__).resolve().parent
EXPERIMENT_DIR = THIS_DIR.parent
VOICE_AGENT_DIR = EXPERIMENT_DIR.parent.parent
PROJECT_ROOT = VOICE_AGENT_DIR.parent

ROOT_ENV_PATH = PROJECT_ROOT / ".env"
if ROOT_ENV_PATH.exists():
    load_dotenv(dotenv_path=ROOT_ENV_PATH, override=False)

# console mode does not actually use these but the worker boots through
# `cli.run_app` which validates LIVEKIT_URL exists.
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("LIVEKIT_API_KEY", "console-fake")
os.environ.setdefault("LIVEKIT_API_SECRET", "console-fake")


# ── Tunables (global vars, no CLI — per project convention) ─────────
STT_MODEL = os.environ.get("OPENAI_STT_MODEL", "gpt-4o-mini-transcribe")
LLM_MODEL = os.environ.get("OPENAI_LLM_MODEL", "gpt-4o-mini")
TTS_MODEL = os.environ.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.environ.get("OPENAI_TTS_VOICE", "nova")
TTS_INSTRUCTIONS = os.environ.get(
    "OPENAI_TTS_INSTRUCTIONS",
    "Speak in a friendly, warm, conversational tone — like a receptionist.",
)
LANG = os.environ.get("AGENT_LANG", "en").strip().lower() or "en"


# ── Logging ──────────────────────────────────────────────────────────
logging.getLogger("livekit.agents").setLevel(logging.INFO)
logging.getLogger("livekit.plugins.openai").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

logger = logging.getLogger("agent-4o-streaming")


# ── System prompt ────────────────────────────────────────────────────
# Minimal — the entire point of this file is to let the model emit
# plain assistant text so LiveKit can stream tokens to TTS. No
# "you can only speak via tools" gymnastics.
SYSTEM_PROMPT = """\
You are Pepper, a humanoid receptionist robot at a university front desk.
Personality: warm, brief, a little playful — like a friendly human
receptionist, not a search engine.

Reply style:
  * 1 to 2 short sentences. Plain conversational prose.
  * No markdown, no bullets, no emojis, no stage directions.
  * Speak directly — your text is read aloud verbatim by TTS.

Tools:
  * Call `get_time` ONLY when the user explicitly asks what time it is.
    Otherwise just reply directly.
"""


# ── The single tool ──────────────────────────────────────────────────
@function_tool
async def get_time(context: RunContext) -> str:
    """Return the current local time.

    Use only when the user explicitly asks what time it is.
    """
    # Filler / hold message — masks the post-tool LLM+TTS hop so the
    # user hears audio immediately instead of waiting in silence while
    # the second LLM call assembles the final reply.
    #
    # CRITICAL — do NOT await: the handle plays in parallel with the
    # tool return + LLM-2 + TTS-2, and LiveKit's reply scheduler queues
    # the real answer's audio behind this filler automatically.
    #
    # add_to_chat_ctx=False keeps the filler out of the LLM history so
    # the model does not see "Let me check…" as a prior assistant turn
    # and either skip the real answer or echo the phrase.
    print(f"  [tool] get_time start (filler)", flush=True)
    context.session.say(
        "Let me check the time for you.",
        add_to_chat_ctx=False,
    )
    # `astimezone()` with no arg reads the system local timezone — works
    # on Windows without the `tzdata` package. The production tool can
    # pin Europe/Prague because it runs in Linux Docker; this diagnostic
    # script just trusts the host clock.
    now = datetime.now().astimezone()
    formatted = now.strftime("%H:%M")
    print(f"  [tool] get_time -> {formatted}", flush=True)
    return formatted


# ── Agent with first-frame latency probe in tts_node ────────────────
class StreamingAgent(Agent):
    """Same default LLM/TTS pipeline as Agent.default, with an extra
    print() in tts_node so we can see TTS first-byte latency without
    digging through the metrics dump."""

    def __init__(self) -> None:
        super().__init__(instructions=SYSTEM_PROMPT, tools=[get_time])

    async def on_enter(self) -> None:
        # Trigger an initial greeting so console mode has something to
        # listen to right away.
        self.session.generate_reply(
            instructions="Greet the user briefly and offer to help.",
        )

    async def tts_node(
        self,
        text: AsyncIterable[str],
        model_settings: ModelSettings,
    ):
        t0 = time.monotonic()
        first_text_at: float | None = None
        first_frame_at: float | None = None
        text_chars = 0
        frames = 0

        async def _instrumented_text():
            nonlocal first_text_at, text_chars
            async for chunk in text:
                if first_text_at is None:
                    first_text_at = time.monotonic()
                    print(
                        f"  [TTS] first_text_chunk dt_ms="
                        f"{(first_text_at - t0) * 1000.0:.1f} "
                        f"chunk={chunk!r}",
                        flush=True,
                    )
                text_chars += len(chunk)
                yield chunk

        try:
            async for frame in Agent.default.tts_node(
                self, _instrumented_text(), model_settings,
            ):
                if first_frame_at is None:
                    first_frame_at = time.monotonic()
                    gap_ms = (
                        (first_frame_at - first_text_at) * 1000.0
                        if first_text_at is not None
                        else (first_frame_at - t0) * 1000.0
                    )
                    print(
                        f"  [TTS] first_frame dt_ms="
                        f"{(first_frame_at - t0) * 1000.0:.1f} "
                        f"text_to_audio_ms={gap_ms:.1f}",
                        flush=True,
                    )
                frames += 1
                yield frame
        finally:
            total_ms = (time.monotonic() - t0) * 1000.0
            if first_frame_at is None:
                print(
                    f"  [TTS] done frames=0 total_ms={total_ms:.1f} "
                    f"NO_AUDIO_PRODUCED chars={text_chars}",
                    flush=True,
                )
            else:
                stream_ms = (time.monotonic() - first_frame_at) * 1000.0
                print(
                    f"  [TTS] done frames={frames} chars={text_chars} "
                    f"total_ms={total_ms:.1f} stream_ms={stream_ms:.1f}",
                    flush=True,
                )


# ── Entrypoint ───────────────────────────────────────────────────────
async def entrypoint(ctx: JobContext) -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        print(
            "[agent-4o-streaming] ERROR: OPENAI_API_KEY not set "
            "(check .env at project root).",
            file=sys.stderr,
            flush=True,
        )
        ctx.shutdown(reason="missing_openai_api_key")
        return

    t_boot = time.monotonic()

    session = AgentSession(
        vad=silero.VAD.load(),
        stt=openai.STT(model=STT_MODEL, language=LANG),
        llm=openai.LLM(
            model=LLM_MODEL,
            temperature=0.2,
        ),
        tts=openai.TTS(
            model=TTS_MODEL,
            voice=TTS_VOICE,
            instructions=TTS_INSTRUCTIONS,
            response_format="pcm",
        ),
        # Start generating the LLM response while we're still waiting
        # for the endpointing delay to expire — saves ~200-500 ms on
        # every turn when the user pauses cleanly.
        preemptive_generation=True,
    )

    # ── Per-turn latency observability ──────────────────────────────
    # LiveKit emits metrics for each pipeline stage: STT (audio-in →
    # final transcript), LLM (TTFT, total tokens, total time), TTS
    # (TTFB, audio duration). `metrics.log_metrics` prints a nice
    # human-readable line per stage; the usage collector aggregates
    # them so we can print a session-end summary.
    usage_collector = metrics.UsageCollector()

    @session.on("metrics_collected")
    def _on_metrics(ev):
        metrics.log_metrics(ev.metrics)
        usage_collector.collect(ev.metrics)

    async def _log_usage_summary():
        try:
            summary = usage_collector.get_summary()
            print(f"[agent-4o-streaming] usage_summary={summary}", flush=True)
        except Exception as exc:
            logger.debug("usage_summary_failed err=%s", exc)

    ctx.add_shutdown_callback(_log_usage_summary)

    # ── Visibility for the basic event flow ─────────────────────────
    @session.on("user_started_speaking")
    def _on_user_started(_ev) -> None:
        print(f"  [VAD] user_started_speaking", flush=True)

    @session.on("user_stopped_speaking")
    def _on_user_stopped(_ev) -> None:
        print(f"  [VAD] user_stopped_speaking", flush=True)

    @session.on("user_input_transcribed")
    def _on_user_speech(event) -> None:
        text = (getattr(event, "text", "") or "").strip()
        if not text:
            return
        is_final = bool(getattr(event, "is_final", True))
        tag = "STT" if is_final else "STT-partial"
        print(f"  [{tag}] {text!r}", flush=True)

    @session.on("agent_state_changed")
    def _on_agent_state(event) -> None:
        new_state = getattr(event, "new_state", None) or getattr(event, "state", "?")
        print(f"  [STATE] agent_state={new_state}", flush=True)

    @session.on("conversation_item_added")
    def _on_item_added(event) -> None:
        item = event.item
        role = str(getattr(item, "role", "") or "")
        text = (getattr(item, "text_content", "") or "").strip()
        if not text:
            return
        print(f"  [{role.upper()}] {text!r}", flush=True)

    print(
        f"[agent-4o-streaming] BOOT room={ctx.room.name} "
        f"stt={STT_MODEL} llm={LLM_MODEL} tts={TTS_MODEL} voice={TTS_VOICE} "
        f"setup_ms={(time.monotonic() - t_boot) * 1000.0:.0f}",
        flush=True,
    )

    await session.start(agent=StreamingAgent(), room=ctx.room)


# ── Worker boot ──────────────────────────────────────────────────────
if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            initialize_process_timeout=60.0,
        )
    )
