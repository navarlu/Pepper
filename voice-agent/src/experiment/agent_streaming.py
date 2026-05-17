"""Streaming-variant local-stack experiment worker.

Audio path:

    silero VAD  →  FasterWhisper STT  →  vLLM Llama 3.1 8B AWQ
                                      →  Piper TTS

Plain-text streaming sibling of `agent.py`. The agent emits assistant
text directly and LiveKit pipes it through Piper sentence-by-sentence —
no `send_message_to_user` wrapping. First-audio latency drops to
"first sentence emitted by the LLM + ~250 ms Piper synth" instead of
"full response emitted as a tool-call JSON argument + Piper synth".

Differences vs. `agent.py` (the tool-wrapped local-stack worker):

  * No `_pipeline.run_pipeline`. We manage our own session, lifecycle
    and event publishing — same as `agent_4o_streaming.py`. Notably
    this skips the pipeline's mic-mute state machine; self-echo is
    AEC's problem, not the pipeline's.
  * Tools: information-only surface (mensa, schedule, person lookup,
    paths, time). No `send_message_to_user` (the whole point), no
    `end_conversation` / `adjust_volume` (they depend on the runtime
    callbacks only `_pipeline.run_pipeline` wires up).
  * `allow_interruptions=True` (LiveKit default). VAD-detected user
    speech can cut the agent off mid-sentence — desired UX for the
    streaming variant. The tool-wrapped path disables this because
    mid-tool-call interrupts corrupt the JSON state machine; plain
    streaming text has no such hazard.
  * Same `StreamingAgent` class as `agent_4o_streaming.py` — first-
    turn greeting splice + TTS first-frame latency probes — defined
    inline rather than shared to keep each entrypoint self-contained.

Run (worker):
    uv run python voice-agent/src/experiment/agent_streaming.py dev

Launcher (per conversation — overrides the agent name to point at
this worker rather than the 4o-streaming one):

    PEPPER_EXPERIMENT_STREAMING_AGENT_NAME=pepper-experiment-local-streaming \\
        uv run python voice-agent/src/experiment/launcher_streaming.py --student 1
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
from typing import AsyncIterable  # noqa: E402

from dotenv import load_dotenv  # noqa: E402
from livekit.agents import (  # noqa: E402
    Agent,
    AgentSession,
    JobContext,
    ModelSettings,
    WorkerOptions,
    cli,
    metrics,
    room_io,
)
from livekit.agents.voice.generation import update_instructions  # noqa: E402
from livekit.plugins import openai, silero  # noqa: E402


# ── Path / env setup ─────────────────────────────────────────────────
THIS_DIR = Path(__file__).resolve().parent
VOICE_AGENT_DIR = THIS_DIR.parent.parent
for p in (str(THIS_DIR), str(VOICE_AGENT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

ROOT_ENV_PATH = VOICE_AGENT_DIR.parent / ".env"
if ROOT_ENV_PATH.exists():
    load_dotenv(dotenv_path=ROOT_ENV_PATH, override=False)

os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

# Mode A streaming: still split into sentences so Piper can synth one
# sentence while the LLM is still emitting the next. The non-streaming
# `agent.py` defaults to 1 for the same reason. With cloud TTS (4o),
# we leave this at 0 because openai.TTS can stream chunks inside a
# single call; Piper is sentence-atomic.
os.environ.setdefault("SPLIT_SENTENCES", "1")

# Local plugin imports + the Qwen-compatibility patch for vLLM-style
# function-args parsing. `agent.py` enables this for all local LLMs;
# we follow the same default.
from src.live.local_speech import FasterWhisperSTT, PiperTTS  # noqa: E402
from src.live.qwen_compat import install_function_args_patch  # noqa: E402

install_function_args_patch()


# ── Prompt + tools ───────────────────────────────────────────────────
from prompt_streaming import SYSTEM_PROMPT, GREETING_INSTRUCTIONS  # noqa: E402

# Same streaming-friendly tool surface as `agent_4o_streaming.py`.
# Pure information-gathering — every other production tool either
# depends on `send_message_to_user` to actually speak (gone here) or
# on runtime callbacks only the production `_pipeline.run_pipeline`
# wires up.
from tools.find_path_to_room import find_path_to_room  # noqa: E402
from tools.lookup_person import lookup_person  # noqa: E402
from tools.mensa_menu import mensa_menu  # noqa: E402
from tools.subject_schedule import subject_schedule  # noqa: E402
from tools.get_time import get_time  # noqa: E402
from tools.end_conversation_streaming import end_conversation_streaming  # noqa: E402
from tools.utils._events import (  # noqa: E402
    set_tool_event_listener,
    set_tool_result_listener,
)
import _streaming_runtime  # noqa: E402

STREAMING_TOOLS = [
    find_path_to_room,
    lookup_person,
    mensa_menu,
    subject_schedule,
    get_time,
    end_conversation_streaming,
]


# ── Logging ──────────────────────────────────────────────────────────
logger = logging.getLogger("experiment-streaming-local")
logging.getLogger("livekit.agents").setLevel(logging.INFO)
logging.getLogger("livekit.plugins.openai").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)


# ── Tunables ─────────────────────────────────────────────────────────
AGENT_NAME = os.environ.get(
    "PEPPER_EXPERIMENT_LOCAL_STREAMING_AGENT_NAME",
    "pepper-experiment-local-streaming",
)
LANG = os.environ.get("AGENT_LANG", "en").strip().lower() or "en"

# Local-stack endpoints + model paths — same env vars as `agent.py`.
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

# Identities — must match the launcher and audio-bridge.
USER_IDENTITY = os.environ.get("USER_IDENTITY", "user")
RECORDER_IDENTITY = os.environ.get("RECORDER_IDENTITY", "experimenter-recorder")
TOPIC_EXPERIMENT = "pepper.experiment"
TOPIC_TEXT = "pepper.text"
TOPIC_CONTROL = "pepper.control"


# ── Prewarmed singletons ─────────────────────────────────────────────
# Same pattern as `agent.py`: load once per worker process so the
# silero / FasterWhisper / Piper init time isn't on the first
# dispatch's critical path.
_PREWARMED_VAD = None
_PREWARMED_STT = None
_PREWARMED_TTS = None


def _resolve_local_model_id() -> str:
    """Discover the model id vLLM is serving at LOCAL_LLM_BASE_URL.

    Mirrors `agent.py`'s logic — done lazily so a worker that booted
    before vLLM was reachable can still recover when the tunnel comes
    up later.
    """
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
        logger.info("stt_loaded model=%s elapsed=%.2fs",
                    LOCAL_STT_MODEL, time.monotonic() - t0)
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


print(
    f"[experiment-streaming-local] booted, waiting for dispatch "
    f"(livekit_url={os.environ.get('LIVEKIT_URL', 'unset')} "
    f"agent_name={AGENT_NAME} "
    f"vllm={LOCAL_LLM_BASE_URL} stt={LOCAL_STT_MODEL} tts=piper)",
    flush=True,
)


# ── Agent class ──────────────────────────────────────────────────────
class StreamingAgent(Agent):
    """Plain-text streaming agent with first-turn greeting + TTS
    first-frame latency probes.

    First-turn behaviour: on the first user message we splice
    `GREETING_INSTRUCTIONS` into the system slot so the model's first
    reply is a greeting, then drop back to the plain system prompt
    for subsequent turns.
    """

    def __init__(self, system_prompt: str, greeting_instructions: str, tools_list) -> None:
        super().__init__(instructions=system_prompt, tools=tools_list)
        self._system_prompt = system_prompt
        self._greeting_instructions = greeting_instructions
        # llm_node splices `greeting_instructions` into the system slot on
        # the FIRST user turn — so Pepper greets + answers in one breath
        # whenever the user actually speaks. No auto-greet at session
        # start (vLLM/Llama rejects an LLM call with tools but no prior
        # user message, AND Lucas wants Pepper to wait for the user).
        self._greeting_done = False

    async def llm_node(self, chat_ctx, tools, model_settings):
        user_msgs = sum(
            1
            for item in chat_ctx.items
            if getattr(item, "type", None) == "message"
            and getattr(item, "role", None) == "user"
        )
        if user_msgs == 1 and not self._greeting_done:
            merged = f"{self._system_prompt}\n\n{self._greeting_instructions}"
            update_instructions(chat_ctx, instructions=merged, add_if_missing=True)
            self._greeting_done = True
        return Agent.default.llm_node(self, chat_ctx, tools, model_settings)

    async def tts_node(
        self,
        text: AsyncIterable[str],
        model_settings: ModelSettings,
    ):
        """Wrap Piper's TTS node with first-text / first-frame
        timestamps. Lets us see at-a-glance whether a slow utterance
        is the LLM stalling tokens or Piper's per-sentence synth.
        """
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
                        f"  [TTS] first_text dt_ms="
                        f"{(first_text_at - t0) * 1000.0:.1f} "
                        f"chunk={chunk!r}",
                        flush=True,
                    )
                text_chars += len(chunk)
                yield chunk

        try:
            async for frame in Agent.default.tts_node(self, _instrumented_text(), model_settings):
                if first_frame_at is None:
                    first_frame_at = time.monotonic()
                    text_to_audio_ms = (
                        (first_frame_at - first_text_at) * 1000.0
                        if first_text_at is not None
                        else (first_frame_at - t0) * 1000.0
                    )
                    print(
                        f"  [TTS] first_frame dt_ms="
                        f"{(first_frame_at - t0) * 1000.0:.1f} "
                        f"text_to_audio_ms={text_to_audio_ms:.1f}",
                        flush=True,
                    )
                frames += 1
                yield frame
        finally:
            total_ms = (time.monotonic() - t0) * 1000.0
            if first_frame_at is None:
                print(
                    f"  [TTS] done frames=0 chars={text_chars} "
                    f"total_ms={total_ms:.1f} NO_AUDIO_PRODUCED",
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
    t_entry = time.monotonic()

    main_loop = asyncio.get_running_loop()

    # ── Per-turn event publisher ─────────────────────────────────────
    def _publish_event(event: dict) -> None:
        try:
            payload = json.dumps(event, ensure_ascii=False, default=str).encode("utf-8")
        except Exception as exc:
            logger.debug("publish_event encode failed: %s", exc)
            return
        try:
            asyncio.run_coroutine_threadsafe(
                ctx.room.local_participant.publish_data(payload, topic=TOPIC_EXPERIMENT),
                main_loop,
            )
        except Exception as exc:
            logger.debug("publish_event submit failed: %s", exc)

    # ── Tool event listeners (forward to the launcher's recorder) ────
    def _on_tool_event(name: str, args: dict) -> None:
        try:
            args_preview = json.dumps(args, ensure_ascii=False, default=str)
        except Exception:
            args_preview = repr(args)
        print(f"  [TOOL] {name}({args_preview})", flush=True)
        _publish_event({"kind": "tool_call", "name": name, "args": args, "ts": time.time()})

    def _on_tool_result(name: str, result) -> None:
        try:
            preview = json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            preview = repr(result)
        if len(preview) > 300:
            preview = preview[:300] + "…"
        print(f"  [TOOL-RESULT] {name} -> {preview}", flush=True)
        _publish_event({"kind": "tool_result", "name": name, "result": result, "ts": time.time()})

    set_tool_event_listener(_on_tool_event)
    set_tool_result_listener(_on_tool_result)

    # ── Streaming runtime: shared with end_conversation_streaming ────
    # Reset on every dispatch (workers are reused across jobs) and
    # parse the dispatch metadata to populate student_id so the QR
    # has the right participant code.
    _streaming_runtime.reset()
    try:
        meta = json.loads(getattr(ctx.job, "metadata", "") or "{}")
        sid_raw = meta.get("experiment_student_id")
        if sid_raw not in (None, ""):
            _streaming_runtime.set_student_id(int(sid_raw))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        logger.debug("could not parse student_id from dispatch metadata: %s", exc)
    _streaming_runtime.set_room(ctx.room)

    # ── Build the local LLM (vLLM via OpenAI-compatible API) ─────────
    try:
        model_id = _resolve_local_model_id()
    except Exception as exc:
        print(
            f"[experiment-streaming-local] ERROR: vLLM unreachable at "
            f"{LOCAL_LLM_BASE_URL}: {exc!r}",
            file=sys.stderr, flush=True,
        )
        ctx.shutdown(reason="vllm_unreachable")
        return

    local_llm = openai.LLM(
        model=model_id,
        base_url=LOCAL_LLM_BASE_URL,
        api_key="not-needed",
        temperature=0.2,
        parallel_tool_calls=False,
        # `agent.py` disables strict tool schema for vLLM Llama because
        # the upstream JSON-schema validator rejects some of our
        # tool argument shapes. Match that for parity.
        _strict_tool_schema=False,
    )

    # ── AgentSession ─────────────────────────────────────────────────
    session = AgentSession(
        vad=_get_vad(),
        stt=_get_stt(),
        llm=local_llm,
        tts=_get_tts(),
        # Start the LLM as soon as VAD endpoints — saves ~200-500 ms
        # per turn when the user pauses cleanly.
        preemptive_generation=True,
        # Voice barge-in DISABLED. Without AEC, Pepper's own audio
        # leaks from the chest speaker back into the user-client mic
        # → VAD trips on the agent's own voice → TTS cancelled before
        # any frame plays. Symptom: `[TTS] done frames=0
        # NO_AUDIO_PRODUCED` repeating on every utterance. Mirrors the
        # same flag on `agent_4o_streaming.py`. Re-enable once AEC is
        # wired into user_client.py so the mic stops hearing Pepper.
        # Typed `/...` from the launcher still works as a deliberate
        # barge-in because it calls `session.interrupt()` explicitly.
        allow_interruptions=False,
    )

    # ── Track typed vs spoken user turns ─────────────────────────────
    _typed_inputs: set[str] = set()

    # ── Boot frame payload (published AFTER session.start so the
    #    local_participant is actually connected to the room — see below).
    session_start_payload = {
        "kind": "session_start",
        "room": ctx.room.name,
        "mode": "local-streaming",
        "ts": time.time(),
        "stt_model": LOCAL_STT_MODEL,
        "llm_model": model_id,
        "tts_model": "piper",
        "stt_device": LOCAL_STT_DEVICE,
        "stt_compute": LOCAL_STT_COMPUTE_TYPE,
        "tts_cuda": LOCAL_TTS_USE_CUDA,
        "lang": LANG,
    }

    # ── Per-turn observability ───────────────────────────────────────
    usage_collector = metrics.UsageCollector()

    @session.on("metrics_collected")
    def _on_metrics(ev):
        metrics.log_metrics(ev.metrics)
        usage_collector.collect(ev.metrics)

    async def _log_usage_summary():
        try:
            summary = usage_collector.get_summary()
            print(f"[experiment-streaming-local] usage_summary={summary}", flush=True)
        except Exception as exc:
            logger.debug("usage_summary_failed err=%s", exc)

    ctx.add_shutdown_callback(_log_usage_summary)

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
        if role == "user":
            input_kind = "typed" if text in _typed_inputs else "speech"
            if input_kind == "typed":
                _typed_inputs.discard(text)
            print(f"  [USER:{input_kind}] {text!r}", flush=True)
            _publish_event({
                "kind": "user_turn", "text": text, "input": input_kind, "ts": time.time(),
            })
        elif role == "assistant":
            print(f"  [ASSISTANT] {text!r}", flush=True)
            _publish_event({
                "kind": "agent_speech", "text": text, "ts": time.time(),
            })

    # ── Room data handler: typed input + cooperative shutdown ───────
    # Two distinct shutdown paths — track which fired so we can choose
    # between `session.shutdown(drain=True)` (graceful, finish current
    # sentence — `/done`) and `session.aclose()` (immediate, cut mid-
    # word — recorder gone / agent kicked / error).
    shutdown_event = asyncio.Event()
    shutdown_mode = {"mode": "abort"}  # default; flipped to "drain" on /done

    # Tools (specifically `end_conversation_streaming`) call this to
    # ask the worker to shut down gracefully after the farewell has
    # been spoken + the QR posted. Identical effect to receiving
    # `/done` over pepper.control.
    async def _end_session_from_tool(reason: str) -> None:
        logger.info("end_session requested from tool reason=%s", reason)
        shutdown_mode["mode"] = "drain"
        shutdown_event.set()

    _streaming_runtime.set_end_session_callback(_end_session_from_tool)

    @ctx.room.on("data_received")
    def _on_data(packet):
        topic = str(getattr(packet, "topic", "") or "")
        if topic == TOPIC_TEXT:
            try:
                msg = json.loads(getattr(packet, "data", b"") or b"")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return
            text = str(msg.get("text", "") or "").strip()
            if not text:
                return
            sender = str(getattr(getattr(packet, "participant", None), "identity", "") or "?")
            logger.info("[pepper.text] from=%s text=%s", sender, text[:120])
            _typed_inputs.add(text)
            try:
                session.interrupt()
                session.generate_reply(user_input=text)
            except Exception as exc:
                logger.warning("pepper.text dispatch failed err=%s", exc)
            return

        if topic == TOPIC_CONTROL:
            try:
                msg = json.loads(getattr(packet, "data", b"") or b"")
            except Exception:
                return
            if str(msg.get("cmd", "")).strip().lower() == "shutdown":
                logger.info("shutdown signal received via pepper.control")
                # Cooperative shutdown from the launcher — drain so the
                # last sentence finishes playing before we tear down.
                shutdown_mode["mode"] = "drain"
                shutdown_event.set()

    @ctx.room.on("participant_disconnected")
    def _on_participant_left(p):
        identity = str(getattr(p, "identity", "") or "")
        if identity == RECORDER_IDENTITY:
            logger.info("recorder disconnected — shutting down job")
            # Unexpected end — recorder vanished without sending the
            # cooperative shutdown. Abort instead of drain.
            shutdown_event.set()

    # Belt-and-braces: register a JobContext shutdown callback so even
    # if the wait loop below is skipped (worker process kill, network
    # error during cleanup), the session's TTS/LLM tasks get cancelled
    # before the process exits.
    async def _ensure_session_closed():
        try:
            await session.aclose()
        except Exception as exc:
            logger.debug("shutdown_cb: session.aclose failed: %s", exc)

    ctx.add_shutdown_callback(_ensure_session_closed)

    # ── Connect + wait for the user participant ─────────────────────
    # We block until user-client is in the room so we can pin
    # `RoomOptions.participant_identity` to "user" — this gives us:
    #   (a) STT subscribes only to user's audio (no recorder/tablet noise)
    #   (b) `close_on_disconnect=True` auto-closes the session if
    #       user-client leaves (e.g. launcher kicks it, container crashes)
    await ctx.connect()
    user_participant = await ctx.wait_for_participant(identity=USER_IDENTITY)
    logger.info(
        "user_joined identity=%s",
        getattr(user_participant, "identity", USER_IDENTITY),
    )

    # ── Start the session ────────────────────────────────────────────
    agent = StreamingAgent(
        system_prompt=SYSTEM_PROMPT,
        greeting_instructions=GREETING_INSTRUCTIONS,
        tools_list=STREAMING_TOOLS,
    )
    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            participant_identity=USER_IDENTITY,
            close_on_disconnect=True,
        ),
    )
    print(
        f"[experiment-streaming-local] session_started "
        f"total_setup={(time.monotonic() - t_entry) * 1000.0:.0f}ms "
        f"room={ctx.room.name} model={model_id}",
        flush=True,
    )
    # Publish session_start NOW that the room is fully connected — the
    # launcher's recorder uses this event to print its "AGENT WARM AND
    # READY" banner.
    _publish_event(session_start_payload)

    # No auto-greet: Pepper waits silently until the user first speaks.
    # The first user turn triggers `llm_node`'s greeting splice, so the
    # model's first reply will greet + answer in one breath.

    # ── Wait for shutdown ────────────────────────────────────────────
    session_closed = asyncio.Event()

    @session.on("close")
    def _on_close(_):
        # AgentSession fires this on (a) close_on_disconnect when "user"
        # leaves, (b) our explicit aclose/shutdown calls, (c) internal
        # errors. Either way, exit the wait loop.
        session_closed.set()

    await asyncio.wait(
        [
            asyncio.create_task(shutdown_event.wait()),
            asyncio.create_task(session_closed.wait()),
        ],
        return_when=asyncio.FIRST_COMPLETED,
    )

    _publish_event({"kind": "session_end", "ts": time.time()})

    # Discriminated tear-down. `session.shutdown(drain=True)` is sync
    # (just schedules the drain); the session's `close` event fires
    # once drain finishes. So: trigger drain, wait briefly for the
    # close event, then aclose as a safety net. The launcher's
    # cooperative `pepper.control shutdown` ping flips us into drain
    # mode; every other path (recorder gone, session auto-close,
    # exception) falls through as abort.
    try:
        if shutdown_mode["mode"] == "drain" and not session_closed.is_set():
            session.shutdown(drain=True)
            # Up to ~5 s for the last sentence to finish playing.
            # In streaming mode the longest reasonable utterance is a
            # few seconds; if it takes longer we cut it off.
            try:
                await asyncio.wait_for(session_closed.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.info("drain timed out — falling back to aclose")
        await session.aclose()
    except Exception as exc:
        logger.debug("session teardown failed: %s", exc)

    ctx.shutdown(reason="experiment_streaming_local_done")
    logger.info("experiment streaming-local session ended")


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
