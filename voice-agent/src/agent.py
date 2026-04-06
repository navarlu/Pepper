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
    llm,
    room_io,
)
from livekit.plugins import openai, silero

from livekit.agents.llm.chat_context import FunctionCall
from livekit.agents.llm import utils as _llm_utils

from .config import (
    AGENT_NAME,
    AGENT_VERSION,
    LANG,
    LISTENER_IDENTITY,
    MONITOR_IDENTITY,
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
    LOCAL_SYSTEM_PROMPT,
    MODEL_NAME,
    OPENAI_SYSTEM_PROMPT,
    SESSION_MANAGER_URL,
    TTS_VOICE,
)
from .local_speech import FasterWhisperSTT, PiperTTS
from .tools import build_tools
from .utils import connect_weaviate, seed_collection

logger = logging.getLogger("voice-agent")

# --- Monkey-patch: fix Qwen's malformed tool call JSON ---
# Qwen 2.5 7B sometimes emits extra trailing braces, e.g. {"animation": "greeting"}}
# which causes `from_json` to fail with "trailing characters".
# We intercept prepare_function_arguments to sanitize the JSON before parsing.
_original_prepare_fn_args = _llm_utils.prepare_function_arguments


def _sanitized_prepare_fn_args(*, fnc, json_arguments, call_ctx=None):
    try:
        return _original_prepare_fn_args(fnc=fnc, json_arguments=json_arguments, call_ctx=call_ctx)
    except ValueError as exc:
        if "trailing" not in str(exc).lower():
            raise
        # Find the matching closing brace for the first '{' and discard the rest
        cleaned = json_arguments.strip()
        depth = 0
        for i, ch in enumerate(cleaned):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    cleaned = cleaned[: i + 1]
                    break
        logger.warning(
            "sanitized_tool_args original=%r cleaned=%r", json_arguments, cleaned
        )
        return _original_prepare_fn_args(fnc=fnc, json_arguments=cleaned, call_ctx=call_ctx)


_llm_utils.prepare_function_arguments = _sanitized_prepare_fn_args
# --- End monkey-patch ---

ROOT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
_PREWARMED_VAD = None
_PREWARMED_STT = None
_PREWARMED_TTS = None
_WORKER_READY = threading.Event()


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


def _post_pipeline_metric(metric: dict) -> None:
    """Fire-and-forget pipeline timing metric (STT/LLM/TTS) to the session manager."""
    threading.Thread(
        target=_post_debug_event,
        args=({"event": "pipeline_metric", **metric},),
        daemon=True,
    ).start()


def _is_bridge_listener(participant) -> bool:
    identity = str(getattr(participant, "identity", "") or "")
    return identity in (LISTENER_IDENTITY, MONITOR_IDENTITY)


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


class _SessionControl:
    """Handles session-control signals (activate / reset) from the session manager."""

    def __init__(self) -> None:
        self.activate_event = asyncio.Event()
        self.reset_event = asyncio.Event()
        self.payload: dict = {}

    def register(self, ctx: JobContext) -> None:
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
            action = data.get("action")
            logger.info("session_control_received action=%s payload=%s", action, data)
            if action == "activate":
                self.payload.update(data)
                self.reset_event.clear()
                self.activate_event.set()
            elif action == "reset":
                self.activate_event.clear()
                self.reset_event.set()

    def clear_activate(self) -> None:
        self.activate_event.clear()
        self.payload.clear()


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
            on_metrics=_post_pipeline_metric,
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
            on_metrics=_post_pipeline_metric,
        )
        logger.info("timing load_tts=%.3fs model=%s (first call)", time.monotonic() - t0, LOCAL_TTS_MODEL_PATH)
    return _PREWARMED_TTS


def _on_llm_metrics_collected(metrics) -> None:
    """Handle LLM metrics_collected event from the openai.LLM plugin."""
    try:
        _post_pipeline_metric({
            "stage": "llm",
            "duration_ms": round(metrics.duration * 1000, 1),
            "ttft_ms": round(metrics.ttft * 1000, 1),
            "completion_tokens": metrics.completion_tokens,
            "prompt_tokens": metrics.prompt_tokens,
            "tokens_per_second": round(metrics.tokens_per_second, 1),
        })
    except Exception:
        pass


def _sanitize_json(raw: str) -> str:
    """Extract the first balanced JSON object from a string.

    Qwen 2.5 7B sometimes appends extra braces or text after the JSON,
    e.g. '{"animation": "greeting"}}\nHello!'. vLLM 0.19 crashes on
    json.loads() of such strings when they appear in chat history.
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


def _sanitize_chat_ctx(chat_ctx: llm.ChatContext) -> None:
    """In-place sanitize FunctionCall arguments in chat history.

    This prevents vLLM 0.19 from crashing with 400 Bad Request when
    it re-parses malformed tool call arguments from conversation history.
    """
    for item in chat_ctx.items:
        if isinstance(item, FunctionCall):
            item.arguments = _sanitize_json(item.arguments)


def _build_local_session() -> AgentSession:
    """Build an AgentSession using local STT (Whisper) + local LLM (vLLM) + local TTS (Piper)."""
    local_llm = openai.LLM(
        model=LOCAL_LLM_MODEL,
        base_url=LOCAL_LLM_BASE_URL,
        api_key="not-needed",
        temperature=0.01,
        parallel_tool_calls=False,
        _strict_tool_schema=False,
    )
    local_llm.on("metrics_collected", _on_llm_metrics_collected)

    # Wrap chat to sanitize malformed tool call arguments before sending to vLLM.
    _original_chat = local_llm.chat

    def _chat_with_sanitized_history(*, chat_ctx, **kwargs):
        _sanitize_chat_ctx(chat_ctx)
        return _original_chat(chat_ctx=chat_ctx, **kwargs)

    local_llm.chat = _chat_with_sanitized_history  # type: ignore[method-assign]

    return AgentSession(
        vad=_get_local_vad(),
        stt=_get_local_stt(),
        llm=local_llm,
        tts=_get_local_tts(),
    )


async def _init_weaviate() -> None:
    """Seed Weaviate collection (best-effort, runs in background)."""
    try:
        with connect_weaviate() as client:
            seed_collection(client)
    except Exception as exc:
        logger.warning("weaviate_init_failed error=%s", str(exc))


def _get_agent_instructions(agent_mode: str) -> str:
    if agent_mode == "local":
        return LOCAL_SYSTEM_PROMPT
    return OPENAI_SYSTEM_PROMPT



async def entrypoint(ctx: JobContext) -> None:
    t_entry = time.monotonic()
    dispatch_meta = _parse_dispatch_metadata(ctx)
    agent_mode = str(dispatch_meta.get("agent_mode", "openai")).strip().lower()
    if agent_mode not in ("openai", "local"):
        agent_mode = "openai"
    is_warm = bool(dispatch_meta.get("warm"))
    is_persistent = is_warm and agent_mode == "local"

    logger.info(
        "agent version=%s agent_mode=%s model=%s warm=%s persistent=%s",
        AGENT_VERSION,
        agent_mode,
        LOCAL_LLM_MODEL if agent_mode == "local" else MODEL_NAME,
        is_warm,
        is_persistent,
    )

    async def _on_shutdown(reason: str):
        logger.info("shutdown_callback reason=%s (room already disconnected by framework)", reason)

    ctx.add_shutdown_callback(_on_shutdown)

    t0 = time.monotonic()
    await ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_ALL)
    logger.info("timing ctx.connect=%.3fs", time.monotonic() - t0)

    # Session control listener for warm/persistent agents
    sc = _SessionControl()
    if is_warm:
        sc.register(ctx)

    t0 = time.monotonic()
    participant = await _wait_for_user_participant(ctx)
    logger.info(
        "timing wait_for_participant=%.3fs participant_identity=%s warm=%s agent_mode=%s",
        time.monotonic() - t0,
        getattr(participant, "identity", ""),
        is_warm,
        agent_mode,
    )

    weaviate_task = asyncio.create_task(_init_weaviate())

    t0 = time.monotonic()
    if agent_mode == "local":
        session = _build_local_session()
    else:
        openai_api_key = _get_required_env("OPENAI_API_KEY")
        session = _build_openai_session(openai_api_key)
    logger.info("timing build_session=%.3fs agent_mode=%s", time.monotonic() - t0, agent_mode)

    agent_tools = build_tools(agent_mode)
    logger.info(
        "agent_tools_registered count=%d names=%s",
        len(agent_tools),
        [getattr(t, 'name', str(t)) for t in agent_tools],
    )
    agent = Agent(
        instructions=_get_agent_instructions(agent_mode),
        tools=agent_tools,
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
            text_input=room_io.TextInputOptions(),
        ),
    )
    logger.info("timing session.start=%.3fs agent_mode=%s", time.monotonic() - t0, agent_mode)

    await weaviate_task

    if is_warm:
        logger.info(
            "warm_agent_ready agent_mode=%s persistent=%s total_warm_setup=%.3fs",
            agent_mode,
            is_persistent,
            time.monotonic() - t_entry,
        )
        _post_debug_event({"event": "warm_ready", "active": True})

    logger.info(
        "agent_listening agent_mode=%s total_setup=%.3fs",
        agent_mode,
        time.monotonic() - t_entry,
    )

    if not is_persistent:
        # OpenAI / non-persistent: single session, exit when done.
        await session_closed.wait()
        return

    # --- Persistent local agent loop ---
    # Agent responds to user speech immediately.
    # On reset signal from session manager, clear history for a fresh conversation.
    session_num = 0
    while True:
        reset_task = asyncio.ensure_future(sc.reset_event.wait())
        close_task = asyncio.ensure_future(session_closed.wait())
        done, pending = await asyncio.wait(
            [reset_task, close_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in pending:
            t.cancel()

        if session_closed.is_set():
            logger.info("persistent_agent_exiting sessions_served=%d", session_num)
            return

        session_num += 1
        logger.info("persistent_agent_reset clearing_history session_num=%d", session_num)
        try:
            await session.interrupt()
            session.clear_user_turn()
            await agent.update_chat_ctx(llm.ChatContext.empty())
            logger.info("persistent_agent_history_cleared session_num=%d", session_num)
        except Exception as exc:
            logger.error("persistent_agent_reset_failed error=%s", exc)

        sc.reset_event.clear()
        _post_debug_event({"event": "warm_ready", "active": True})
        logger.info("persistent_agent_ready session_num=%d", session_num)


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

    _WORKER_READY.set()
    logger.info("prewarm_done total=%.3fs", time.monotonic() - t_start)


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            prewarm_fnc=_prewarm_process,
            initialize_process_timeout=120.0,
            num_idle_processes=1,
            agent_name=AGENT_NAME,
            job_memory_warn_mb=2000,
        )
    )
