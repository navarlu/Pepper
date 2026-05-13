"""4o-chained experiment worker (Mode C) — runs on the RPi.

agent_name = "pepper-experiment-4o". Same room (`pepper-experiment`),
same launcher (launcher.py picks the agent_name from --variant), same
recorder JSONL schema as Mode A — only the underlying audio + LLM
pipeline differs:

  Mode A (agent.py, on woska):
    silero VAD → FasterWhisper STT → vLLM Llama → Piper TTS
    speaks via `send_message_to_user` tool (terminal).

  Mode B (agent_realtime.py, on the RPi):
    OpenAI Realtime API (gpt-realtime-mini) — VAD/STT/LLM/TTS all in
    one socket, speaks natively. `play_gesture` for body language.

  Mode C (this file, on the RPi):
    silero VAD → OpenAI gpt-4o-transcribe STT → OpenAI gpt-4o-mini LLM
    → OpenAI gpt-4o-mini-tts TTS. CHAINED, not the all-in-one realtime
    API. Same prompt + tools as Mode A — speaks via
    `send_message_to_user` so the per-utterance `emotion` gesture is
    preserved. VAD setup mirrors Mode A (client-side silero), no
    server-side turn detection.

Run:
    uv run python voice-agent/src/experiment/agent_4o.py dev

Inside docker (the normal path on the RPi):
    docker compose -f docker/docker-compose.experiment.yml up -d voice-agent-4o
    docker compose -f docker/docker-compose.experiment.yml logs -f voice-agent-4o

Prereqs:
  * OPENAI_API_KEY in .env at the project root.
  * LiveKit server is up (docker-compose.experiment.yml).
"""

from __future__ import annotations

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentSession,
    AutoSubscribe,
    JobContext,
    WorkerOptions,
    cli,
    llm,
    room_io,
)
from livekit.agents.voice.generation import update_instructions
from livekit.plugins import openai, silero

THIS_DIR = Path(__file__).resolve().parent
VOICE_AGENT_DIR = THIS_DIR.parent.parent
for p in (str(THIS_DIR), str(VOICE_AGENT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

ROOT_ENV_PATH = VOICE_AGENT_DIR.parent / ".env"
if ROOT_ENV_PATH.exists():
    load_dotenv(dotenv_path=ROOT_ENV_PATH, override=False)

os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

from src.live.bridge_client import post_tablet_clear  # noqa: E402

import importlib  # noqa: E402

# Same English prompt/tools surface as Mode A — the per-utterance
# `emotion` gesture only works when send_message_to_user is the terminal
# speech tool, so we reuse prompt.py (NOT prompt_realtime.py).
_PROMPT_MODULE = os.environ.get("EXPERIMENT_PROMPT_MODULE", "prompt")
_TOOLS_MODULE = os.environ.get("EXPERIMENT_TOOLS_MODULE", "tools")
_prompt_mod = importlib.import_module(_PROMPT_MODULE)
SYSTEM_PROMPT = _prompt_mod.SYSTEM_PROMPT
GREETING_INSTRUCTIONS = _prompt_mod.GREETING_INSTRUCTIONS
tools = importlib.import_module(_TOOLS_MODULE)
LIVEKIT_TOOLS_TOOLONLY = tools.LIVEKIT_TOOLS_TOOLONLY

logger = logging.getLogger("experiment-4o-worker")
logging.getLogger("livekit.agents").setLevel(logging.INFO)
logging.getLogger("livekit.plugins.openai").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

AGENT_NAME = os.environ.get("PEPPER_EXPERIMENT_4O_AGENT_NAME", "pepper-experiment-4o")
USER_IDENTITY = os.environ.get("USER_IDENTITY", "user")
RECORDER_IDENTITY = "experimenter-recorder"
TOPIC_EXPERIMENT = "pepper.experiment"

# Hardcoded 4o stack — swap by editing this file.
STT_MODEL = os.environ.get("OPENAI_STT_MODEL", "gpt-4o-transcribe")
LLM_MODEL = os.environ.get("OPENAI_LLM_MODEL", "gpt-4o-mini")
TTS_MODEL = os.environ.get("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")
TTS_VOICE = os.environ.get("OPENAI_TTS_VOICE", "shimmer")
TTS_INSTRUCTIONS = os.environ.get(
    "OPENAI_TTS_INSTRUCTIONS",
    "Speak in a friendly, warm, conversational tone — like a receptionist.",
)
LANG = os.environ.get("AGENT_LANG", "en").strip().lower() or "en"

# VAD is REQUIRED by openai.STT: the plugin doesn't support streaming
# transcription, so AgentSession needs VAD to chunk speech into batch
# STT calls. silero runs slightly slower than realtime on the RPi CPU
# (~0.4s lag per window) but works — that lag adds end-of-turn delay
# but doesn't break anything. allow_interruptions=False still ensures
# Pepper finishes every utterance fully.
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


def _count_user_messages(chat_ctx: llm.ChatContext) -> int:
    return sum(
        1
        for item in chat_ctx.items
        if getattr(item, "type", None) == "message"
        and getattr(item, "role", None) == "user"
    )


class _Experiment4oAgent(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=SYSTEM_PROMPT, tools=LIVEKIT_TOOLS_TOOLONLY)
        self._greeting_done = False

    async def llm_node(self, chat_ctx, tools, model_settings):
        user_msgs = _count_user_messages(chat_ctx)
        if user_msgs == 1 and not self._greeting_done:
            merged = f"{SYSTEM_PROMPT}\n\n{GREETING_INSTRUCTIONS}"
            update_instructions(chat_ctx, instructions=merged, add_if_missing=True)
            self._greeting_done = True
        return Agent.default.llm_node(self, chat_ctx, tools, model_settings)


def _parse_dispatch_metadata(ctx: JobContext) -> dict:
    raw = ""
    for attr in ("metadata", "agent_metadata"):
        raw = str(getattr(ctx.job, attr, "") or "").strip()
        if raw:
            break
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        logger.warning("dispatch_metadata_parse_failed raw=%s", raw[:200])
        return {}


async def _wait_for_user_participant(ctx: JobContext):
    last_logged = None
    while True:
        rps = list((getattr(ctx.room, "remote_participants", {}) or {}).values())
        for p in rps:
            ident = str(getattr(p, "identity", "") or "")
            if ident == USER_IDENTITY:
                return p
            if ident and ident != last_logged:
                logger.info("waiting_for_user skipping=%s", ident)
                last_logged = ident
        await asyncio.sleep(0.2)


async def entrypoint(ctx: JobContext) -> None:
    t_entry = time.monotonic()
    meta = _parse_dispatch_metadata(ctx)
    variant = str(meta.get("experiment_variant") or "").strip()
    student_id = str(meta.get("experiment_student_id") or "").strip()

    print(
        f"[experiment-4o-worker] BOOT room={ctx.room.name} "
        f"variant={variant!r} student_id={student_id!r} "
        f"stt={STT_MODEL} llm={LLM_MODEL} tts={TTS_MODEL}",
        flush=True,
    )

    if not os.environ.get("OPENAI_API_KEY"):
        print(
            "[experiment-4o-worker] ERROR: OPENAI_API_KEY not set "
            "(check .env at project root).",
            file=sys.stderr, flush=True,
        )
        ctx.shutdown(reason="missing_openai_api_key")
        return

    await ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_ALL)
    participant = await _wait_for_user_participant(ctx)
    logger.info("user_joined identity=%s", getattr(participant, "identity", ""))

    main_loop = asyncio.get_running_loop()
    t_base = time.monotonic()

    def _ts() -> str:
        return f"+{time.monotonic() - t_base:6.3f}s"

    async def _publish_topic(topic: str, payload: dict) -> None:
        try:
            data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            await ctx.room.local_participant.publish_data(data, topic=topic)
        except Exception as exc:
            logger.debug("publish_failed topic=%s err=%s", topic, exc)

    async def _publish(payload: dict) -> None:
        await _publish_topic(TOPIC_EXPERIMENT, payload)

    def _publish_sync(payload: dict) -> None:
        try:
            asyncio.run_coroutine_threadsafe(_publish(payload), main_loop)
        except Exception as exc:
            logger.debug("publish_schedule_failed err=%s", exc)

    def _publish_sync_topic(topic: str, payload: dict) -> None:
        try:
            asyncio.run_coroutine_threadsafe(_publish_topic(topic, payload), main_loop)
        except Exception as exc:
            logger.debug("publish_schedule_failed topic=%s err=%s", topic, exc)

    async def _clear_tablet() -> None:
        try:
            await asyncio.to_thread(post_tablet_clear)
        except Exception as exc:
            logger.debug("tablet_clear_failed err=%s", exc)

    def _clear_tablet_sync() -> None:
        try:
            asyncio.run_coroutine_threadsafe(_clear_tablet(), main_loop)
        except Exception as exc:
            logger.debug("tablet_clear_schedule_failed err=%s", exc)

    def _on_tool_event(name: str, args: dict) -> None:
        try:
            args_preview = json.dumps(args, ensure_ascii=False, default=str)
        except Exception:
            args_preview = repr(args)
        print(f"  {_ts()} [TOOL] {name}({args_preview})", flush=True)
        _publish_sync({
            "kind": "tool_call", "name": name, "args": args, "ts": time.time(),
        })

    def _on_tool_result(name: str, result: Any) -> None:
        try:
            preview = json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            preview = repr(result)
        if len(preview) > 300:
            preview = preview[:300] + "…"
        print(f"  {_ts()} [TOOL-RESULT] {name} -> {preview}", flush=True)
        _publish_sync({
            "kind": "tool_result", "name": name, "result": result, "ts": time.time(),
        })

    tools.set_tool_event_listener(_on_tool_event)
    tools.set_tool_result_listener(_on_tool_result)

    # Build the chained pipeline. Same VAD shape as Mode A (silero,
    # client-side) — no server-side turn detection.
    cloud_stt = openai.STT(model=STT_MODEL, language=LANG)
    cloud_llm = openai.LLM(model=LLM_MODEL, temperature=0.2, parallel_tool_calls=False)
    cloud_tts = openai.TTS(
        model=TTS_MODEL,
        voice=TTS_VOICE,
        instructions=TTS_INSTRUCTIONS,
    )
    # silero VAD chunks audio for the non-streaming openai.STT.
    # allow_interruptions=False so Pepper finishes every utterance
    # fully; while she's speaking she is NOT listening and cannot be
    # interrupted by speech. To barge in, type via the pepper.text topic.
    session = AgentSession(
        vad=_get_vad(),
        stt=cloud_stt,
        llm=cloud_llm,
        tts=cloud_tts,
        max_tool_steps=4,
        allow_interruptions=False,
    )

    _typed_inputs: set[str] = set()

    _publish_sync({
        "kind": "session_start",
        "room": ctx.room.name,
        "variant": variant,
        "student_id": student_id,
        "model": LLM_MODEL,
        "stt_model": STT_MODEL,
        "tts_model": TTS_MODEL,
        "tts_voice": TTS_VOICE,
        "mode": "4o-chained",
        "ts": time.time(),
    })

    _publish_sync_topic("pepper.state", {
        "agent_mode": "C",
        "agent_language": LANG,
    })

    @session.on("user_started_speaking")
    def _on_user_started(_event) -> None:
        print(f"  {_ts()} [STAGE B] VAD started speaking", flush=True)
        _clear_tablet_sync()

    @session.on("user_stopped_speaking")
    def _on_user_stopped(_event) -> None:
        print(f"  {_ts()} [STAGE B] VAD stopped speaking", flush=True)

    @session.on("agent_state_changed")
    def _on_agent_state(event) -> None:
        new_state = getattr(event, "new_state", None) or getattr(event, "state", "?")
        print(f"  {_ts()} [STAGE D] agent_state={new_state}", flush=True)
        _publish_sync_topic("pepper.state", {"agent_state": str(new_state)})

    @session.on("user_input_transcribed")
    def _on_user_speech(event) -> None:
        text = (getattr(event, "text", "") or "").strip()
        if not text:
            return
        is_final = bool(getattr(event, "is_final", True))
        tag = "STT" if is_final else "STT-partial"
        print(f"  [{tag}] {text!r}", flush=True)

    @session.on("conversation_item_added")
    def _on_item_added(event) -> None:
        item = event.item
        role = str(getattr(item, "role", "") or "")
        text = (getattr(item, "text_content", "") or "").strip()
        if not text:
            return
        if role == "user":
            input_kind = "typed" if text in _typed_inputs else "speech"
            if input_kind == "typed":
                _typed_inputs.discard(text)
            print(f"  [USER:{input_kind}] {text!r}", flush=True)
            _publish_sync({
                "kind": "user_turn",
                "text": text,
                "input": input_kind,
                "ts": time.time(),
            })
            logger.info("user_turn input=%s text=%s", input_kind, text)
        # Assistant items skipped — send_message_to_user's [TOOL] event
        # is the canonical record of Pepper's speech (mirrors Mode A).

    shutdown_event = asyncio.Event()

    @ctx.room.on("data_received")
    def _on_data(packet):
        topic = str(getattr(packet, "topic", "") or "")
        if topic == "pepper.text":
            try:
                msg = json.loads(getattr(packet, "data", b"") or b"")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return
            text = str(msg.get("text", "") or "").strip()
            if not text:
                return
            sender = str(getattr(getattr(packet, "participant", None), "identity", "") or "?")
            logger.info("[pepper.text] from=%s text=%s", sender, text[:120])
            _clear_tablet_sync()
            _typed_inputs.add(text)
            try:
                session.interrupt()
                session.generate_reply(user_input=text)
            except Exception as exc:
                logger.warning("pepper.text dispatch failed err=%s", exc)
            return
        if topic != "pepper.control":
            return
        try:
            msg = json.loads(getattr(packet, "data", b"") or b"")
        except Exception:
            return
        if str(msg.get("cmd", "")).strip().lower() == "shutdown":
            logger.info("shutdown signal received via pepper.control")
            shutdown_event.set()

    @ctx.room.on("participant_disconnected")
    def _on_participant_left(p):
        identity = str(getattr(p, "identity", "") or "")
        if identity == RECORDER_IDENTITY:
            logger.info("recorder disconnected (%s) — shutting down job", identity)
            shutdown_event.set()

    agent = _Experiment4oAgent()
    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            participant_identity=str(getattr(participant, "identity", "") or USER_IDENTITY),
        ),
    )
    logger.info("session_started total_setup=%.2fs", time.monotonic() - t_entry)

    session_closed = asyncio.Event()

    @session.on("close")
    def _on_close(_):
        session_closed.set()

    await asyncio.wait(
        [
            asyncio.create_task(shutdown_event.wait()),
            asyncio.create_task(session_closed.wait()),
        ],
        return_when=asyncio.FIRST_COMPLETED,
    )

    _publish_sync({"kind": "session_end", "ts": time.time()})

    try:
        await session.aclose()
    except Exception as exc:
        logger.debug("session.aclose failed: %s", exc)
    ctx.shutdown(reason="experiment_4o_done")
    logger.info("experiment 4o session ended")


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
