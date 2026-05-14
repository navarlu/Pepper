"""Mode A worker (local stack) — slim entrypoint over _pipeline.run_pipeline.

agent_name = "pepper-experiment". Audio path:

    silero VAD  →  FasterWhisper STT  →  vLLM Llama 3.1 8B AWQ  →  Piper TTS

VAD is used ONLY for end-of-user-turn detection (the non-streaming
FasterWhisper plugin needs chunking). Interruptions are disabled at the
session level in `_pipeline.run_pipeline` — Pepper cannot be cut off by
VAD-detected user speech. Typed input on `pepper.text` is still a
deliberate barge-in.

Everything common to the two chained workers (Mode A and Mode C) lives
in [_pipeline.py](_pipeline.py); this file is only what's stack-specific:
env reads, prewarmed singletons, the vLLM model discovery, and
`WorkerOptions`.

Run:
    uv run python voice-agent/src/experiment/agent.py dev
"""

from __future__ import annotations

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import asyncio  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
import urllib.request  # noqa: E402
from pathlib import Path  # noqa: E402

from dotenv import load_dotenv  # noqa: E402
from livekit.agents import JobContext, WorkerOptions, cli  # noqa: E402
from livekit.plugins import silero  # noqa: E402

THIS_DIR = Path(__file__).resolve().parent
VOICE_AGENT_DIR = THIS_DIR.parent.parent
for p in (str(THIS_DIR), str(VOICE_AGENT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

ROOT_ENV_PATH = VOICE_AGENT_DIR.parent / ".env"
if ROOT_ENV_PATH.exists():
    load_dotenv(dotenv_path=ROOT_ENV_PATH, override=False)

# Default LiveKit URL — reachable on both RPi and woska via the reverse
# tunnel. Mirrors production agent.py.
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

# Mode A: split into sentences so atomically-synthesized Piper still
# pipelines first-sentence audio in ~250 ms. Mode C overrides this to
# "0" in agent_4o.py to let openai.TTS stream a single utterance.
os.environ.setdefault("SPLIT_SENTENCES", "1")

from src.live.local_speech import FasterWhisperSTT, PiperTTS  # noqa: E402
from src.live.qwen_compat import install_function_args_patch  # noqa: E402
from src.live.bridge_client import post_tablet_clear  # noqa: E402
from livekit.plugins import openai  # noqa: E402

import importlib  # noqa: E402

from _pipeline import run_pipeline  # noqa: E402

# Prompt + tools modules are env-selectable so language variants can be
# swapped in without forking the worker.
_PROMPT_MODULE = os.environ.get("EXPERIMENT_PROMPT_MODULE", "prompt")
_TOOLS_MODULE = os.environ.get("EXPERIMENT_TOOLS_MODULE", "tools")
_prompt_mod = importlib.import_module(_PROMPT_MODULE)
SYSTEM_PROMPT = _prompt_mod.SYSTEM_PROMPT
GREETING_INSTRUCTIONS = _prompt_mod.GREETING_INSTRUCTIONS
tools = importlib.import_module(_TOOLS_MODULE)

logger = logging.getLogger("experiment-worker")
logging.getLogger("livekit.agents").setLevel(logging.INFO)
logging.getLogger("livekit.plugins.openai").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

print(
    f"[experiment-agent] booted, waiting for dispatch "
    f"(livekit_url={os.environ.get('LIVEKIT_URL', 'unset')})",
    flush=True,
)

install_function_args_patch()


AGENT_NAME = os.environ.get("PEPPER_EXPERIMENT_AGENT_NAME", "pepper-experiment")
LANG = os.environ.get("AGENT_LANG", "en").strip().lower() or "en"
LOCAL_LLM_BASE_URL = os.environ.get("LOCAL_LLM_BASE_URL", "http://localhost:8000/v1")
LOCAL_STT_MODEL = os.environ.get("LOCAL_STT_MODEL", "tiny")
LOCAL_STT_DEVICE = os.environ.get("LOCAL_STT_DEVICE", "cpu")
LOCAL_STT_COMPUTE_TYPE = os.environ.get("LOCAL_STT_COMPUTE_TYPE", "int8")
LOCAL_STT_CPU_THREADS = int(os.environ.get("LOCAL_STT_CPU_THREADS", "0"))
LOCAL_TTS_MODEL_PATH = os.environ.get(
    "LOCAL_TTS_MODEL_PATH",
    str(VOICE_AGENT_DIR / "models" / "piper" / "en_US-hfc_female-medium.onnx"),
)
LOCAL_TTS_USE_CUDA = (
    os.environ.get("LOCAL_TTS_USE_CUDA", "0").strip().lower()
    in ("1", "true", "yes", "on")
)


# ── Prewarmed singletons ──────────────────────────────────────────────
_PREWARMED_VAD = None
_PREWARMED_STT = None
_PREWARMED_TTS = None


def _resolve_local_model_id() -> str:
    url = f"{LOCAL_LLM_BASE_URL.rstrip('/')}/models"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(
            f"Could not discover model from vLLM at {url}: {exc!r}. "
            f"Is the SSH tunnel to woska open?"
        ) from exc
    data = payload.get("data") or []
    if not data:
        raise RuntimeError(f"vLLM /models returned no entries: {payload!r}")
    return str(data[0]["id"])


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


def _get_stt():
    global _PREWARMED_STT
    if _PREWARMED_STT is None:
        t0 = time.monotonic()
        _PREWARMED_STT = FasterWhisperSTT(
            model=LOCAL_STT_MODEL,
            language=LANG,
            device=LOCAL_STT_DEVICE,
            compute_type=LOCAL_STT_COMPUTE_TYPE,
            cpu_threads=LOCAL_STT_CPU_THREADS,
        )
        logger.info("stt_loaded model=%s elapsed=%.2fs", LOCAL_STT_MODEL, time.monotonic() - t0)
    return _PREWARMED_STT


def _get_tts():
    global _PREWARMED_TTS
    if _PREWARMED_TTS is None:
        t0 = time.monotonic()
        _PREWARMED_TTS = PiperTTS(
            model_path=Path(LOCAL_TTS_MODEL_PATH),
            use_cuda=LOCAL_TTS_USE_CUDA,
        )
        logger.info(
            "tts_loaded path=%s use_cuda=%s elapsed=%.2fs",
            LOCAL_TTS_MODEL_PATH, LOCAL_TTS_USE_CUDA, time.monotonic() - t0,
        )
    return _PREWARMED_TTS


def _build_stack():
    """Local-stack builder for `_pipeline.run_pipeline`.

    Resolves the vLLM model id at dispatch time (so a worker that
    boots before vLLM is reachable can still recover when the tunnel
    comes up later — vs. baking the id in at module import).
    """
    model_id = _resolve_local_model_id()
    local_llm = openai.LLM(
        model=model_id,
        base_url=LOCAL_LLM_BASE_URL,
        api_key="not-needed",
        temperature=0.2,
        parallel_tool_calls=False,
        _strict_tool_schema=False,
    )
    return {
        "vad": _get_vad(),
        "stt": _get_stt(),
        "llm": local_llm,
        "tts": _get_tts(),
        "labels": {
            "stt_model": LOCAL_STT_MODEL,
            "llm_model": model_id,
            "tts_model": "piper",
            "stt_device": LOCAL_STT_DEVICE,
            "stt_compute": LOCAL_STT_COMPUTE_TYPE,
            "tts_cuda": LOCAL_TTS_USE_CUDA,
        },
    }


async def entrypoint(ctx: JobContext) -> None:
    async def _clear_tablet() -> None:
        try:
            await asyncio.to_thread(post_tablet_clear)
        except Exception as exc:
            logger.debug("tablet_clear_failed err=%s", exc)

    await run_pipeline(
        ctx,
        mode_label="local",
        stack_builder=_build_stack,
        system_prompt=SYSTEM_PROMPT,
        greeting_instructions=GREETING_INSTRUCTIONS,
        tools_module=tools,
        on_clear_tablet=_clear_tablet,
        # Local stack tolerates a longer interruption window on the
        # generate_reply path, but interruptions are disabled globally
        # so this is moot — kept for parity with the previous file.
        extra_session_kwargs=None,
    )


def _prewarm(_) -> None:
    t0 = time.monotonic()
    _get_vad()
    _get_stt()
    _get_tts()
    logger.info("prewarm_done total=%.2fs", time.monotonic() - t0)


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=_prewarm,
            initialize_process_timeout=120.0,
            num_idle_processes=0,
            agent_name=AGENT_NAME,
            job_memory_warn_mb=2000,
            max_retry=2**31 - 1,
        )
    )
