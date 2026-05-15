"""Mode C worker (chained OpenAI 4o stack) — slim entrypoint.

agent_name = "pepper-experiment-4o". Audio path:

    silero VAD  →  gpt-4o-transcribe (STT)
                →  gpt-4o-mini (LLM)
                →  gpt-4o-mini-tts (TTS)

VAD is REQUIRED for the non-streaming `openai.STT` plugin (it chunks
mic audio into batch transcription calls). Interruptions are disabled
at the session level in `_pipeline.run_pipeline` — Pepper cannot be
cut off by VAD-detected user speech. Typed input on `pepper.text` is
still a deliberate barge-in.

Mode C overrides `SPLIT_SENTENCES=0` so `send_message_to_user` sends
the whole utterance as a single `session.say()` and lets `openai.TTS`
stream chunks within one HTTP call — lower first-byte latency than
splitting into sentences and paying RTT per sentence.

Run:
    uv run python voice-agent/src/experiment/agent_4o.py dev
"""

from __future__ import annotations

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import asyncio  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

from dotenv import load_dotenv  # noqa: E402
from livekit.agents import JobContext, WorkerOptions, cli  # noqa: E402
from livekit.plugins import openai, silero  # noqa: E402

THIS_DIR = Path(__file__).resolve().parent
VOICE_AGENT_DIR = THIS_DIR.parent.parent
for p in (str(THIS_DIR), str(VOICE_AGENT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

ROOT_ENV_PATH = VOICE_AGENT_DIR.parent / ".env"
if ROOT_ENV_PATH.exists():
    load_dotenv(dotenv_path=ROOT_ENV_PATH, override=False)

os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

# Mode C: openai.TTS supports streaming chunks within a single
# session.say() call, so DON'T split into sentences — that would force
# one HTTP RTT per sentence. Worker-level default; can be overridden in
# the dispatch env if desired.
os.environ.setdefault("SPLIT_SENTENCES", "0")

from src.live.bridge_client import post_tablet_clear  # noqa: E402

import importlib  # noqa: E402

from _pipeline import run_pipeline  # noqa: E402

_PROMPT_MODULE = os.environ.get("EXPERIMENT_PROMPT_MODULE", "prompt")
_TOOLS_MODULE = os.environ.get("EXPERIMENT_TOOLS_MODULE", "tools")
_prompt_mod = importlib.import_module(_PROMPT_MODULE)
SYSTEM_PROMPT = _prompt_mod.SYSTEM_PROMPT
GREETING_INSTRUCTIONS = _prompt_mod.GREETING_INSTRUCTIONS
tools = importlib.import_module(_TOOLS_MODULE)

logger = logging.getLogger("experiment-4o-worker")
logging.getLogger("livekit.agents").setLevel(logging.INFO)
logging.getLogger("livekit.plugins.openai").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

AGENT_NAME = os.environ.get("PEPPER_EXPERIMENT_4O_AGENT_NAME", "pepper-experiment-4o")
LANG = os.environ.get("AGENT_LANG", "en").strip().lower() or "en"

STT_MODEL = os.environ.get("OPENAI_STT_MODEL", "gpt-4o-transcribe")
LLM_MODEL = os.environ.get("OPENAI_LLM_MODEL", "gpt-4o-mini")
TTS_MODEL = os.environ.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.environ.get("OPENAI_TTS_VOICE", "nova")
TTS_INSTRUCTIONS = os.environ.get(
    "OPENAI_TTS_INSTRUCTIONS",
    "Speak in a friendly, warm, conversational tone — like a receptionist.",
)
TTS_RESPONSE_FORMAT = os.environ.get("OPENAI_TTS_RESPONSE_FORMAT", "pcm").strip() or "pcm"


_PREWARMED_VAD = None


def _get_vad():
    global _PREWARMED_VAD
    if _PREWARMED_VAD is None:
        t0 = time.monotonic()
        threshold = float(os.environ.get("LOCAL_VAD_THRESHOLD", "0.6"))
        min_speech = float(os.environ.get("LOCAL_VAD_MIN_SPEECH", "0.15"))
        _PREWARMED_VAD = silero.VAD.load(
            activation_threshold=threshold,
            min_speech_duration=min_speech,
        )
        logger.info(
            "vad_loaded threshold=%.2f min_speech=%.2fs elapsed=%.2fs",
            threshold, min_speech, time.monotonic() - t0,
        )
    return _PREWARMED_VAD


print(
    f"[experiment-4o-agent] booted, waiting for dispatch "
    f"(livekit_url={os.environ.get('LIVEKIT_URL', 'unset')} "
    f"stt={STT_MODEL} llm={LLM_MODEL} tts={TTS_MODEL} voice={TTS_VOICE})",
    flush=True,
)


def _build_stack():
    cloud_stt = openai.STT(model=STT_MODEL, language=LANG)
    cloud_llm = openai.LLM(model=LLM_MODEL, temperature=0.2, parallel_tool_calls=False)
    # Keep the response format configurable. Local diagnostics showed
    # large variance between mp3/opus/pcm depending on request shape;
    # for the current Pepper path, pcm has been the most stable default
    # because the LiveKit plugin emits decoded frames directly.
    cloud_tts = openai.TTS(
        model=TTS_MODEL,
        voice=TTS_VOICE,
        instructions=TTS_INSTRUCTIONS,
        response_format=TTS_RESPONSE_FORMAT,
    )
    return {
        "vad": _get_vad(),
        "stt": cloud_stt,
        "llm": cloud_llm,
        "tts": cloud_tts,
        "labels": {
            "stt_model": STT_MODEL,
            "llm_model": LLM_MODEL,
            "tts_model": TTS_MODEL,
            "tts_voice": TTS_VOICE,
            "tts_response_format": TTS_RESPONSE_FORMAT,
        },
    }


async def entrypoint(ctx: JobContext) -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        print(
            "[experiment-4o-worker] ERROR: OPENAI_API_KEY not set "
            "(check .env at project root).",
            file=sys.stderr, flush=True,
        )
        ctx.shutdown(reason="missing_openai_api_key")
        return

    async def _clear_tablet() -> None:
        try:
            await asyncio.to_thread(post_tablet_clear)
        except Exception as exc:
            logger.debug("tablet_clear_failed err=%s", exc)

    await run_pipeline(
        ctx,
        mode_label="4o-chained",
        stack_builder=_build_stack,
        system_prompt=SYSTEM_PROMPT,
        greeting_instructions=GREETING_INSTRUCTIONS,
        tools_module=tools,
        on_clear_tablet=_clear_tablet,
    )


def _prewarm(_) -> None:
    t0 = time.monotonic()
    _get_vad()
    logger.info("prewarm_done total=%.2fs", time.monotonic() - t0)


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=_prewarm,
            initialize_process_timeout=60.0,
            num_idle_processes=0,
            agent_name=AGENT_NAME,
            job_memory_warn_mb=1500,
            max_retry=2**31 - 1,
        )
    )
