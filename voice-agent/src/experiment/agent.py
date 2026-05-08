"""Real LiveKit worker for the Pepper student-study experiment.

agent_name = "pepper-experiment". This is a SEPARATE worker from the
production `pepper-local` so it can be dispatched to dedicated study
rooms without disturbing the production stack.

Audio path mirrors production local mode:
  - VAD: silero
  - STT: FasterWhisper (local, CPU)
  - LLM: vLLM Llama 3.1 8B AWQ via OpenAI-compat
  - TTS: Piper (local, CPU)

What's different from production agent.py:
  - Tools come from `tools/` (Letta-style: send_message_to_user
    is the terminal speech tool; everything else has emotion +
    request_heartbeat).
  - The agent publishes a structured event stream to `pepper.experiment`
    on the LiveKit data channel:
      * {"kind":"user_turn",      "text":"...", "ts":...}
      * {"kind":"tool_call",      "name":"...", "args":{...}, "ts":...}
      * {"kind":"agent_speech",   "text":"...", "ts":...}
    The experiment recorder (experiment.py) subscribes to this topic
    and writes JSONL turn records. No transcription loss vs. STT.
  - Reads dispatch metadata `experiment_variant` and
    `experiment_student_id` (the orchestrator passes them through as
    JSON in the agent dispatch metadata). They appear in the boot
    banner so the run is unambiguously identified in the log.

Run:
    uv run python voice-agent/src/experiment/agent.py dev
    # or, with explicit agent name (must match dispatch agent_name):
    uv run python voice-agent/src/experiment/agent.py dev --agent-name pepper-experiment

Prereqs:
  * LiveKit server is up (production docker compose is fine)
  * vLLM tunnel to woska is open on localhost:8000
  * .env at the project root carries LIVEKIT_URL/API_KEY/API_SECRET
"""

from __future__ import annotations

# Silence aiohttp/livekit DeprecationWarnings BEFORE livekit imports.
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import asyncio
import json
import logging
import os
import sys
import time
import urllib.request
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

# Path glue so we can import from this folder AND voice-agent/src
THIS_DIR = Path(__file__).resolve().parent
VOICE_AGENT_DIR = THIS_DIR.parent.parent
for p in (str(THIS_DIR), str(VOICE_AGENT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

# Load root .env so LIVEKIT_URL/API_KEY/SECRET are available.
ROOT_ENV_PATH = VOICE_AGENT_DIR.parent / ".env"
if ROOT_ENV_PATH.exists():
    load_dotenv(dotenv_path=ROOT_ENV_PATH, override=False)

# Default to the LiveKit endpoint that's reachable on both RPi and
# woska (via the reverse-tunnel container that publishes RPi's 7880
# on woska's 127.0.0.1:7880). Mirrors production agent.py — keeps
# the default in code so .env on woska doesn't need to carry it.
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

from src.live.local_speech import FasterWhisperSTT, PiperTTS  # noqa: E402
from src.live.qwen_compat import install_function_args_patch  # noqa: E402

import tools  # noqa: E402  (kept for set_tool_event_listener access from worker)
from src.live.bridge_client import post_tablet_clear  # noqa: E402
from tools import LIVEKIT_TOOLS_TOOLONLY  # noqa: E402
from prompt import GREETING_INSTRUCTIONS, SYSTEM_PROMPT  # noqa: E402

logger = logging.getLogger("experiment-worker")

# Show livekit.agents at INFO so worker registration / job-dispatch
# events appear in the woska tmux. The really chatty plugins stay at
# WARNING.
logging.getLogger("livekit.agents").setLevel(logging.INFO)
logging.getLogger("livekit.plugins.openai").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

# One-shot heartbeat so the tmux pane confirms the agent has reached
# its main module — useful when nothing else is logged yet (idle worker
# waiting for a job dispatch). flush=True bypasses any pipe buffering.
print(
    f"[experiment-agent] booted, waiting for dispatch "
    f"(livekit_url={os.environ.get('LIVEKIT_URL', 'unset')})",
    flush=True,
)

install_function_args_patch()


# ── Config (env-driven, with defaults matching production local mode) ──
AGENT_NAME = os.environ.get("PEPPER_EXPERIMENT_AGENT_NAME", "pepper-experiment")
USER_IDENTITY = os.environ.get("USER_IDENTITY", "user")
LISTENER_IDENTITY = os.environ.get("LISTENER_IDENTITY", "listener-python")
MONITOR_IDENTITY = os.environ.get("MONITOR_IDENTITY", "monitor-python")

LANG = "en"
LOCAL_LLM_BASE_URL = os.environ.get("LOCAL_LLM_BASE_URL", "http://localhost:8000/v1")
LOCAL_STT_MODEL = os.environ.get("LOCAL_STT_MODEL", "tiny")
LOCAL_STT_DEVICE = os.environ.get("LOCAL_STT_DEVICE", "cpu")
LOCAL_STT_COMPUTE_TYPE = os.environ.get("LOCAL_STT_COMPUTE_TYPE", "int8")
LOCAL_STT_CPU_THREADS = int(os.environ.get("LOCAL_STT_CPU_THREADS", "0"))
LOCAL_TTS_MODEL_PATH = os.environ.get(
    "LOCAL_TTS_MODEL_PATH",
    str(VOICE_AGENT_DIR / "models" / "piper" / "en_US-hfc_female-medium.onnx"),
)
# Truthy parse so any of "1", "true", "yes", "on" enables CUDA TTS.
LOCAL_TTS_USE_CUDA = (
    os.environ.get("LOCAL_TTS_USE_CUDA", "0").strip().lower()
    in ("1", "true", "yes", "on")
)

# Topic the recorder subscribes to.
TOPIC_EXPERIMENT = "pepper.experiment"


# ── Prewarmed singletons (per worker process) ─────────────────────────
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
        # activation_threshold=0.25 (default 0.5) so silero trips on
        # the DJI mic's quiet output. Production OpenAI Realtime is
        # tolerant of low levels, but local silero+Whisper is not, and
        # the DJI Mic Mini's ALSA capture sits around 0.002 RMS — well
        # under the default 0.5 probability gate. Drop to 0.25 so the
        # model picks up real speech without false-triggering on
        # background noise (which still typically scores < 0.1).
        _PREWARMED_VAD = silero.VAD.load(
            activation_threshold=float(os.environ.get("LOCAL_VAD_THRESHOLD", "0.25")),
        )
        logger.info(
            "vad_loaded threshold=%.2f elapsed=%.2fs",
            float(os.environ.get("LOCAL_VAD_THRESHOLD", "0.25")),
            time.monotonic() - t0,
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


# ── Agent ────────────────────────────────────────────────────────────


def _count_user_messages(chat_ctx: llm.ChatContext) -> int:
    return sum(
        1
        for item in chat_ctx.items
        if getattr(item, "type", None) == "message"
        and getattr(item, "role", None) == "user"
    )


class _ExperimentAgent(Agent):
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


# ── Helpers ──────────────────────────────────────────────────────────


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


def _is_user(participant) -> bool:
    return str(getattr(participant, "identity", "") or "") == USER_IDENTITY


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


# ── Entrypoint ───────────────────────────────────────────────────────


async def entrypoint(ctx: JobContext) -> None:
    t_entry = time.monotonic()
    meta = _parse_dispatch_metadata(ctx)
    variant = str(meta.get("experiment_variant") or "").strip()
    student_id = str(meta.get("experiment_student_id") or "").strip()
    model_id = _resolve_local_model_id()

    print(
        f"[experiment-worker] BOOT room={ctx.room.name} "
        f"variant={variant!r} student_id={student_id!r} model={model_id} "
        f"stt_device={LOCAL_STT_DEVICE} stt_compute={LOCAL_STT_COMPUTE_TYPE} "
        f"tts_cuda={LOCAL_TTS_USE_CUDA}",
        flush=True,
    )

    await ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_ALL)
    participant = await _wait_for_user_participant(ctx)
    logger.info("user_joined identity=%s", getattr(participant, "identity", ""))

    # ── STAGE A: track-subscription visibility ──────────────────────
    # Log the moment we subscribe to (or unsubscribe from) any audio
    # track on the user participant. If you NEVER see this line, the
    # agent isn't receiving audio — fix the room/dispatch wiring.
    @ctx.room.on("track_subscribed")
    def _on_track_subscribed(track, publication, p):
        kind = getattr(track, "kind", "?")
        identity = getattr(p, "identity", "?")
        sid = getattr(publication, "sid", "?")
        print(
            f"  [STAGE A] track_subscribed identity={identity} "
            f"kind={kind} sid={sid}",
            flush=True,
        )

    @ctx.room.on("track_unsubscribed")
    def _on_track_unsubscribed(_track, publication, p):
        identity = getattr(p, "identity", "?")
        sid = getattr(publication, "sid", "?")
        print(
            f"  [STAGE A] track_unsubscribed identity={identity} sid={sid}",
            flush=True,
        )

    main_loop = asyncio.get_running_loop()

    # Latency-debug baseline. Every worker-side trace line that wants a
    # timestamp prefix calls `_ts()` to get a "+s.sss" string relative
    # to this point. Pair with the existing absolute timestamps on
    # [STAGE B] / [STAGE D] / piper.voice / tts_done log lines to
    # measure inter-LLM-pass and TTS-to-audio latency.
    t_base = time.monotonic()

    def _ts() -> str:
        return f"+{time.monotonic() - t_base:6.3f}s"

    # Publish helper — fire-and-forget, schedules onto the main loop so it's
    # safe from worker threads (tool callbacks).
    async def _publish(payload: dict) -> None:
        try:
            data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            await ctx.room.local_participant.publish_data(data, topic=TOPIC_EXPERIMENT)
        except Exception as exc:
            logger.debug("publish_failed err=%s", exc)

    def _publish_sync(payload: dict) -> None:
        try:
            asyncio.run_coroutine_threadsafe(_publish(payload), main_loop)
        except Exception as exc:
            logger.debug("publish_schedule_failed err=%s", exc)

    # Wipe whatever display_info last drew on Pepper's tablet. Fired
    # whenever a new user turn begins (VAD start or typed input) so the
    # previous turn's phone/email/room doesn't linger on the screen.
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

    # Wire tool events from tools/ into the publish channel.
    def _on_tool_event(name: str, args: dict) -> None:
        try:
            args_preview = json.dumps(args, ensure_ascii=False, default=str)
        except Exception:
            args_preview = repr(args)
        print(f"  {_ts()} [TOOL] {name}({args_preview})", flush=True)
        _publish_sync({
            "kind": "tool_call",
            "name": name,
            "args": args,
            "ts": time.time(),
        })

    def _on_tool_result(name: str, result: Any) -> None:
        # Trim huge payloads in the tmux echo (RAG/search results can be
        # multi-KB) but preserve the full structure in the recorder.
        try:
            preview = json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            preview = repr(result)
        if len(preview) > 300:
            preview = preview[:300] + "…"
        print(f"  {_ts()} [TOOL-RESULT] {name} -> {preview}", flush=True)
        _publish_sync({
            "kind": "tool_result",
            "name": name,
            "result": result,
            "ts": time.time(),
        })

    tools.set_tool_event_listener(_on_tool_event)
    tools.set_tool_result_listener(_on_tool_result)

    # Build session.
    local_llm = openai.LLM(
        model=model_id,
        base_url=LOCAL_LLM_BASE_URL,
        api_key="not-needed",
        temperature=0.2,
        parallel_tool_calls=False,
        _strict_tool_schema=False,
    )
    session = AgentSession(
        vad=_get_vad(),
        stt=_get_stt(),
        llm=local_llm,
        tts=_get_tts(),
        max_tool_steps=4,
    )

    # Tracks user texts that arrived via the pepper.text topic so the
    # conversation_item_added handler can tag the resulting user_turn
    # as input="typed" vs "speech". Discarded as soon as it's matched.
    _typed_inputs: set[str] = set()

    # Boot frame for the recorder so the JSONL has a header turn.
    _publish_sync({
        "kind": "session_start",
        "room": ctx.room.name,
        "variant": variant,
        "student_id": student_id,
        "model": model_id,
        "ts": time.time(),
    })

    # ── DEBUG: visibility into the audio pipeline ──────────────────
    # Tmux pane on woska prints these so we can see where speech dies:
    #   STAGE B: VAD started  → silero detected speech
    #   STAGE B: VAD stopped  → silero detected end-of-speech
    #   STAGE C: STT (interim)→ Whisper produced a partial transcript
    #   STAGE C: STT (final)  → Whisper finalised it
    #   STAGE D: agent_state  → listening / thinking / speaking
    # If you see (B) but never (C), Whisper is silent — bump the model
    # size or the input level. If you don't see (B), VAD isn't tripping
    # — input level is too low or the worker didn't subscribe. If you
    # see (C interim) but never (C final), the endpointer isn't closing
    # the turn — adjust min_endpointing_delay / max_endpointing_delay.
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

    # Streaming STT debug — only echo non-empty transcripts so we don't
    # spam the tmux with empty interims/finals. The canonical user_turn
    # publish lives in conversation_item_added (more reliable: STT-final
    # events sometimes arrive empty when Whisper emits multiple short
    # interim segments).
    @session.on("user_input_transcribed")
    def _on_user_speech(event) -> None:
        text = (getattr(event, "text", "") or "").strip()
        if not text:
            return
        is_final = bool(getattr(event, "is_final", True))
        tag = "STT" if is_final else "STT-partial"
        print(f"  [{tag}] {text!r}", flush=True)

    # Mirror agent speech (the `text` arg of send_message_to_user is the
    # canonical truth, but conversation_item_added fires on every assistant
    # message including any meta-text the model may emit. We capture both
    # for cross-checking.)
    @session.on("conversation_item_added")
    def _on_item_added(event) -> None:
        item = event.item
        role = str(getattr(item, "role", "") or "")
        text = (getattr(item, "text_content", "") or "").strip()
        if not text:
            return
        if role == "user":
            # Canonical finalised user transcript (more reliable than the
            # streaming user_input_transcribed events, which sometimes
            # finalise with an empty string when FasterWhisper produces
            # multiple short interim segments).
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
        # Assistant items are skipped: Llama 3.1 in OpenAI-compat tool
        # mode leaks the same text into assistant.content AND into the
        # send_message_to_user tool arg. The [TOOL] send_message_to_user
        # event is the canonical record of what Pepper actually said.

    # Listen for /done from the experiment recorder, OR for the recorder
    # itself disconnecting (Ctrl+C, crash, etc.). Either way the worker
    # job exits — we never want stale agent jobs accumulating in the room
    # across launcher runs (each one would re-subscribe to user audio
    # and double-publish every event).
    RECORDER_IDENTITY = "experimenter-recorder"
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
            # New user turn — clear any info card from the previous one.
            _clear_tablet_sync()
            # Mark this text so the conversation_item_added handler tags
            # the resulting user_turn as input="typed" rather than "speech".
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

    agent = _ExperimentAgent()
    # Bind the session input pipeline to the user participant explicitly.
    # Without this, RoomIO defaults to whichever remote participant joined
    # first (often listener-python / experimenter-recorder) — a track with
    # no audio — and silero never sees the mic frames.
    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            participant_identity=str(getattr(participant, "identity", "") or USER_IDENTITY),
        ),
    )
    logger.info("session_started total_setup=%.2fs", time.monotonic() - t_entry)

    # Run until /done. session-close also halts us in case the room dies.
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

    _publish_sync({
        "kind": "session_end",
        "ts": time.time(),
    })

    try:
        await session.aclose()
    except Exception as exc:
        logger.debug("session.aclose failed: %s", exc)
    ctx.shutdown(reason="experiment_done")
    logger.info("experiment session ended")


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
            # 0 = no warm replacement process. Each dispatch spawns its
            # own. For the experiment we only do one conversation at
            # a time, and a single prewarmed process holds the STT/TTS
            # models in VRAM — having a second one loaded "just in
            # case" doubles GPU memory needlessly and OOMs alongside
            # vLLM. Trade-off: first dispatch after worker boot is
            # slightly slower (no pre-pool).
            num_idle_processes=0,
            agent_name=AGENT_NAME,
            job_memory_warn_mb=2000,
        )
    )
