import asyncio
import logging
import os
import threading
import time
import json
from pathlib import Path
from urllib.request import Request, urlopen

from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentSession,
    AutoSubscribe,
    JobContext,
    WorkerOptions,
    cli,
    room_io,
)
from livekit.plugins import openai, silero

from .config import (
    AGENT_NAME,
    AGENT_VERSION,
    LANG,
    LISTENER_IDENTITY,
    LIVEKIT_URL,
    LOCAL_LLM_BASE_URL,
    LOCAL_LLM_MODEL,
    LOCAL_STT_COMPUTE_TYPE,
    LOCAL_STT_CPU_THREADS,
    LOCAL_STT_DEVICE,
    LOCAL_STT_MODEL,
    LOCAL_TTS_LENGTH_SCALE,
    LOCAL_TTS_MODEL_PATH,
    LOCAL_TTS_NOISE_SCALE,
    LOCAL_TTS_NOISE_W_SCALE,
    LOCAL_TTS_SPEAKER_ID,
    LOCAL_TTS_USE_CUDA,
    MODEL_NAME,
    SESSION_MANAGER_URL,
    SYSTEM_PROMPT,
    TTS_VOICE,
    VOICE_AGENT_GREETING_INSTRUCTIONS,
)
from .local_speech import FasterWhisperSTT, PiperTTS
from .tools import build_tools
from .utils import connect_weaviate, seed_collection

logger = logging.getLogger("voice-agent")

ROOT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
_PREWARMED_VAD = None
_PREWARMED_STT = None
_PREWARMED_TTS = None


def _load_root_env() -> None:
    if ROOT_ENV_PATH.exists():
        load_dotenv(dotenv_path=ROOT_ENV_PATH, override=True)
        logger.info("dotenv_loaded path=%s", str(ROOT_ENV_PATH))
        return
    logger.info("dotenv_loaded path=<missing:%s>", str(ROOT_ENV_PATH))


def _get_required_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _set_runtime_defaults() -> None:
    # Keep non-secret runtime defaults in config instead of `.env`.
    os.environ.setdefault("LIVEKIT_URL", LIVEKIT_URL)


_load_root_env()
_set_runtime_defaults()


def _post_component_status(state: str, detail: str, healthy: bool) -> None:
    if not SESSION_MANAGER_URL:
        return
    url = f"{SESSION_MANAGER_URL.rstrip('/')}/api/component-status"
    req = Request(
        url,
        data=json.dumps(
            {
                "name": "voice-agent",
                "state": state,
                "detail": detail,
                "healthy": healthy,
            }
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urlopen(req, timeout=0.5).read()
    except Exception:
        pass


def _post_debug_event(payload: dict[str, object]) -> None:
    if not SESSION_MANAGER_URL:
        return
    url = f"{SESSION_MANAGER_URL.rstrip('/')}/api/debug-event"
    req = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urlopen(req, timeout=0.5).read()
    except Exception:
        pass


def _start_component_heartbeat() -> threading.Event:
    stop_event = threading.Event()

    def _worker() -> None:
        _post_component_status("starting", "worker booting", healthy=False)
        while not stop_event.wait(5.0):
            _post_component_status(
                "ready",
                "worker registered and waiting for jobs",
                healthy=True,
            )

    thread = threading.Thread(target=_worker, name="voice-agent-heartbeat", daemon=True)
    thread.start()
    return stop_event


def _is_bridge_listener(participant) -> bool:
    identity = str(getattr(participant, "identity", "") or "")
    return identity == LISTENER_IDENTITY


def _iter_remote_participants(ctx: JobContext):
    participants = getattr(ctx.room, "remote_participants", {}) or {}
    if hasattr(participants, "values"):
        return list(participants.values())
    return list(participants)


async def _wait_for_user_participant(ctx: JobContext):
    last_logged_identity = None
    while True:
        for participant in _iter_remote_participants(ctx):
            if not _is_bridge_listener(participant):
                return participant
            identity = str(getattr(participant, "identity", "") or "")
            if identity and identity != last_logged_identity:
                logger.info(
                    "waiting_for_user_participant skipping_identity=%s",
                    identity,
                )
                last_logged_identity = identity
        await asyncio.sleep(0.2)


def _parse_dispatch_metadata(ctx: JobContext) -> dict:
    """Extract metadata dict from the agent dispatch job."""
    raw = ""
    # Try job-level metadata first, then room metadata
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


def _setup_activation_listener(ctx: JobContext) -> tuple[asyncio.Event, dict]:
    """Register data handler for activation signal. Must be called before session.start().

    Returns (event, payload_container) — await the event, then read the payload.
    """
    activation_event = asyncio.Event()
    activation_payload: dict = {}

    @ctx.room.on("data_received")
    def _on_data(packet):
        topic = str(getattr(packet, "topic", "") or "")
        if topic != "session-control":
            return
        raw = getattr(packet, "data", b"") or b""
        try:
            data = json.loads(raw)
        except Exception:
            return
        logger.info("data_received topic=%s action=%s", topic, data.get("action"))
        if data.get("action") == "activate":
            nonlocal activation_payload
            activation_payload.update(data)
            activation_event.set()

    return activation_event, activation_payload


def _build_openai_session(openai_api_key: str) -> AgentSession:
    """Build an AgentSession using the OpenAI Realtime API (speech-to-speech)."""
    return AgentSession(
        llm=openai.realtime.RealtimeModel(
            model=MODEL_NAME,
            voice=TTS_VOICE,
            api_key=openai_api_key,
        )
    )


def _get_local_vad():
    global _PREWARMED_VAD
    if _PREWARMED_VAD is None:
        t0 = time.monotonic()
        _PREWARMED_VAD = silero.VAD.load()
        logger.info("timing load_vad=%.3fs (first call)", time.monotonic() - t0)
    return _PREWARMED_VAD


def _get_local_stt():
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
        logger.info("timing load_stt=%.3fs model=%s (first call)", time.monotonic() - t0, LOCAL_STT_MODEL)
    return _PREWARMED_STT


def _get_local_tts():
    global _PREWARMED_TTS
    if _PREWARMED_TTS is None:
        t0 = time.monotonic()
        _PREWARMED_TTS = PiperTTS(
            model_path=LOCAL_TTS_MODEL_PATH,
            use_cuda=LOCAL_TTS_USE_CUDA,
            speaker_id=LOCAL_TTS_SPEAKER_ID,
            length_scale=LOCAL_TTS_LENGTH_SCALE,
            noise_scale=LOCAL_TTS_NOISE_SCALE,
            noise_w_scale=LOCAL_TTS_NOISE_W_SCALE,
        )
        logger.info("timing load_tts=%.3fs model=%s (first call)", time.monotonic() - t0, LOCAL_TTS_MODEL_PATH)
    return _PREWARMED_TTS


def _build_local_session() -> AgentSession:
    """Build an AgentSession using local STT (Whisper) + local LLM (vLLM) + local TTS (Piper)."""
    return AgentSession(
        vad=_get_local_vad(),
        stt=_get_local_stt(),
        llm=openai.LLM(
            model=LOCAL_LLM_MODEL,
            base_url=LOCAL_LLM_BASE_URL,
            api_key="not-needed",
            parallel_tool_calls=False,
            # vLLM/Qwen in local mode is more reliable with permissive tool schemas.
            _strict_tool_schema=False,
        ),
        tts=_get_local_tts(),
    )


async def _init_weaviate() -> None:
    """Seed Weaviate collection (best-effort, runs in background)."""
    try:
        with connect_weaviate() as client:
            seed_collection(client)
    except Exception as exc:
        logger.warning("weaviate_init_failed error=%s", str(exc))


async def entrypoint(ctx: JobContext) -> None:
    t_entry = time.monotonic()
    dispatch_meta = _parse_dispatch_metadata(ctx)
    agent_mode = str(dispatch_meta.get("agent_mode", "openai")).strip().lower()
    if agent_mode not in ("openai", "local"):
        agent_mode = "openai"
    is_warm = bool(dispatch_meta.get("warm"))

    logger.info(
        "agent version=%s agent_mode=%s model=%s warm=%s",
        AGENT_VERSION,
        agent_mode,
        LOCAL_LLM_MODEL if agent_mode == "local" else MODEL_NAME,
        is_warm,
    )

    t0 = time.monotonic()
    await ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_ALL)
    logger.info("timing ctx.connect=%.3fs", time.monotonic() - t0)

    # Register activation listener early — before session.start() takes over room events
    activation_event, activation_payload = (None, None)
    if is_warm:
        activation_event, activation_payload = _setup_activation_listener(ctx)

    # In warm mode, find whatever participant is already in the room (bridge/listener)
    # so we can start the session immediately. The real user arrives after activation.
    t0 = time.monotonic()
    participant = await _wait_for_user_participant(ctx)
    logger.info(
        "timing wait_for_participant=%.3fs participant_identity=%s warm=%s agent_mode=%s",
        time.monotonic() - t0,
        getattr(participant, "identity", ""),
        is_warm,
        agent_mode,
    )

    # Run Weaviate init in parallel with session build — they are independent
    weaviate_task = asyncio.create_task(_init_weaviate())

    t0 = time.monotonic()
    if agent_mode == "local":
        session = _build_local_session()
    else:
        openai_api_key = _get_required_env("OPENAI_API_KEY")
        session = _build_openai_session(openai_api_key)
    logger.info("timing build_session=%.3fs agent_mode=%s", time.monotonic() - t0, agent_mode)

    async def _text_input_cb(
        sess: AgentSession,
        event: room_io.TextInputEvent,
    ) -> None:
        message = str(event.text or "").strip()
        if not message:
            return
        logger.info(
            "text_input_received participant_identity=%s text=%s",
            getattr(event.participant, "identity", ""),
            message[:120],
        )
        await sess.interrupt()
        reply = sess.generate_reply(user_input=message)
        await reply.wait_for_playout()

    agent = Agent(
        instructions=SYSTEM_PROMPT,
        tools=build_tools(),
    )

    session_closed = asyncio.Event()

    @session.on("close")
    def _on_close(_) -> None:
        session_closed.set()

    t0 = time.monotonic()
    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            close_on_disconnect=True,
            participant_identity=str(getattr(participant, "identity", "") or ""),
            text_input=room_io.TextInputOptions(text_input_cb=_text_input_cb),
        ),
    )
    logger.info("timing session.start=%.3fs agent_mode=%s", time.monotonic() - t0, agent_mode)

    # Ensure Weaviate init finished (should be done by now, it ran in parallel)
    await weaviate_task

    if is_warm and activation_event is not None:
        t_warm_ready = time.monotonic()
        logger.info(
            "warm_agent_ready waiting_for_activation agent_mode=%s total_warm_setup=%.3fs",
            agent_mode,
            t_warm_ready - t_entry,
        )
        _post_debug_event({"event": "warm_ready", "active": True})
        await activation_event.wait()
        logger.info(
            "warm_agent_activated conversation_id=%s wait_duration=%.3fs",
            activation_payload.get("conversation_id", ""),
            time.monotonic() - t_warm_ready,
        )

    t0 = time.monotonic()
    logger.info("timing total_before_greeting=%.3fs agent_mode=%s warm=%s", t0 - t_entry, agent_mode, is_warm)
    greeting = await session.generate_reply(
        instructions=VOICE_AGENT_GREETING_INSTRUCTIONS,
    )
    await greeting.wait_for_playout()
    logger.info("timing greeting_playout=%.3fs agent_mode=%s", time.monotonic() - t0, agent_mode)

    await session_closed.wait()


def _prewarm_process(_) -> None:
    """Eagerly load models that the expected agent mode will need.

    PEPPER_AGENT_MODE env var is set by docker-compose / session-manager.
    - "local"  → load VAD + Whisper STT + Piper TTS (heavy, ARM64)
    - "openai" → load only VAD (Realtime API handles the rest)
    - unset    → load everything so both modes work without restart
    """
    mode_hint = (os.environ.get("PEPPER_AGENT_MODE") or "").strip().lower()
    t_start = time.monotonic()
    logger.info("prewarm_start mode_hint=%s", mode_hint or "<unset>")

    # VAD is used by local mode; cheap enough to always load.
    _get_local_vad()

    if mode_hint == "openai":
        logger.info("prewarm_skip_local_models mode_hint=openai")
    else:
        # mode_hint is "local" or unset → load STT + TTS
        _get_local_stt()
        _get_local_tts()

    logger.info("prewarm_done total=%.3fs", time.monotonic() - t_start)


if __name__ == "__main__":
    heartbeat_stop = _start_component_heartbeat()
    try:
        cli.run_app(
            WorkerOptions(
                entrypoint_fnc=entrypoint,
                prewarm_fnc=_prewarm_process,
                initialize_process_timeout=120.0,
                num_idle_processes=1,
                agent_name=AGENT_NAME,
            )
        )
    finally:
        heartbeat_stop.set()
        _post_component_status("stopping", "worker stopped", healthy=False)
