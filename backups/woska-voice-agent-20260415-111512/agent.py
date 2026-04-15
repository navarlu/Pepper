import asyncio
import logging
import os
import threading
import time
import json
from pathlib import Path

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
    SESSION_IDLE_TIMEOUT_SEC,
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


def _log_event(payload: dict[str, object]) -> None:
    """Log a structured event to terminal (replaces HTTP POST to session-manager)."""
    event = payload.get("event", "unknown")
    details = {k: v for k, v in payload.items() if k != "event"}
    detail_str = " ".join(f"{k}={v}" for k, v in details.items())
    logger.info("[event] %s %s", event, detail_str[:200])


def _post_pipeline_metric(metric: dict) -> None:
    """Log pipeline timing metric (STT/LLM/TTS) to terminal."""
    stage = metric.get("stage", "?")
    parts = [f"{k}={v}" for k, v in metric.items() if k != "stage"]
    logger.info("[PIPE] stage=%s %s", stage, " ".join(parts))


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
    """Handles session-control signals (activate / reset / shutdown) from the session manager."""

    def __init__(self) -> None:
        self.activate_event = asyncio.Event()
        self.reset_event = asyncio.Event()
        self.shutdown_event = asyncio.Event()
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
                logger.warning("session_control_invalid_json raw=%r", raw[:120])
                return
            action = data.get("action")
            logger.info("[PERSIST] session_control_received action=%s payload=%s", action, data)
            if action == "activate":
                self.payload.update(data)
                self.reset_event.clear()
                self.activate_event.set()
            elif action == "reset":
                self.activate_event.clear()
                self.reset_event.set()
            elif action == "shutdown":
                logger.info("[PERSIST] shutdown_signal_received — agent will exit cleanly")
                self.activate_event.clear()
                self.reset_event.clear()
                self.shutdown_event.set()
            else:
                logger.warning("session_control_unknown_action action=%s", action)

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
    payload = {"stage": "llm"}
    for src, dst, scale in (
        ("duration", "duration_ms", 1000),
        ("ttft", "ttft_ms", 1000),
        ("completion_tokens", "completion_tokens", 1),
        ("prompt_tokens", "prompt_tokens", 1),
        ("tokens_per_second", "tokens_per_second", 1),
    ):
        try:
            v = getattr(metrics, src, None)
            if v is not None:
                payload[dst] = round(v * scale, 1) if scale != 1 else v
        except Exception as e:
            logger.debug("llm metric field %s failed: %s", src, e)
    _post_pipeline_metric(payload)


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
        temperature=0.1,
        parallel_tool_calls=False,
        _strict_tool_schema=False,
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
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
    # All warm agents are persistent now: they stay in the room across conversations
    # and only reset chat history on idle. Only mode switch (shutdown signal) tears
    # the worker down. Symmetric for local & openai modes.
    is_persistent = is_warm

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

    # Debug visibility into participant churn (so we can confirm the persistent
    # session survives user disconnects/reconnects without tearing down).
    @ctx.room.on("participant_connected")
    def _on_participant_connected(participant):
        identity = str(getattr(participant, "identity", "") or "")
        logger.info("[PERSIST] participant_connected identity=%s", identity)

    @ctx.room.on("participant_disconnected")
    def _on_participant_disconnected(participant):
        identity = str(getattr(participant, "identity", "") or "")
        logger.info(
            "[PERSIST] participant_disconnected identity=%s (session stays alive)",
            identity,
        )

    # --- WebRTC / ICE diagnostics --------------------------------------------------
    # These listeners surface exactly what the underlying RTC engine is doing so we
    # can tell whether failures are signaling vs media, and which path ICE picked.
    _rtc_t0 = time.monotonic()

    @ctx.room.on("connection_state_changed")
    def _on_conn_state(state):
        logger.info(
            "[RTC] connection_state=%s elapsed=%.3fs",
            state, time.monotonic() - _rtc_t0,
        )

    @ctx.room.on("connection_quality_changed")
    def _on_conn_quality(participant, quality):
        identity = str(getattr(participant, "identity", "") or "")
        logger.info("[RTC] quality identity=%s quality=%s", identity, quality)

    @ctx.room.on("reconnecting")
    def _on_reconnecting():
        logger.warning("[RTC] reconnecting elapsed=%.3fs", time.monotonic() - _rtc_t0)

    @ctx.room.on("reconnected")
    def _on_reconnected():
        logger.info("[RTC] reconnected elapsed=%.3fs", time.monotonic() - _rtc_t0)

    @ctx.room.on("disconnected")
    def _on_disconnected(reason=None):
        logger.warning(
            "[RTC] disconnected reason=%s elapsed=%.3fs",
            reason, time.monotonic() - _rtc_t0,
        )

    @ctx.room.on("track_published")
    def _on_track_published(publication, participant):
        identity = str(getattr(participant, "identity", "") or "")
        sid = getattr(publication, "sid", "?")
        kind = getattr(publication, "kind", "?")
        logger.info("[RTC] track_published identity=%s sid=%s kind=%s", identity, sid, kind)

    @ctx.room.on("track_subscribed")
    def _on_track_subscribed(track, publication, participant):
        identity = str(getattr(participant, "identity", "") or "")
        sid = getattr(publication, "sid", "?")
        kind = getattr(track, "kind", "?")
        logger.info(
            "[RTC] track_subscribed identity=%s sid=%s kind=%s elapsed=%.3fs",
            identity, sid, kind, time.monotonic() - _rtc_t0,
        )

    @ctx.room.on("track_unsubscribed")
    def _on_track_unsubscribed(track, publication, participant):
        identity = str(getattr(participant, "identity", "") or "")
        sid = getattr(publication, "sid", "?")
        logger.info("[RTC] track_unsubscribed identity=%s sid=%s", identity, sid)

    # Periodic RTT sampler — reads per-participant connection_quality_info, which
    # exposes the actual WebRTC RTT in seconds (loopback latency over the media
    # path). This number is the real network cost between the agent and each
    # remote peer, including the SSH-tunneled signaling and the direct/relayed
    # media path.
    async def _rtc_rtt_sampler():
        await asyncio.sleep(3.0)
        while not session_closed_for_sampler.is_set():
            try:
                room = ctx.room
                participants = list(getattr(room, "remote_participants", {}).values())
                for p in participants:
                    identity = str(getattr(p, "identity", "") or "")
                    # livekit-rtc exposes ConnectionQualityInfo on the participant
                    info = getattr(p, "connection_quality_info", None) or getattr(p, "_connection_quality_info", None)
                    if info is not None:
                        rtt = getattr(info, "rtt_ms", None) or getattr(info, "rtt", None)
                        score = getattr(info, "score", None)
                        loss = getattr(info, "packet_loss", None) or getattr(info, "packets_lost", None)
                        logger.info(
                            "[RTT] identity=%s rtt_ms=%s score=%s loss=%s",
                            identity, rtt, score, loss,
                        )
                    else:
                        # Fallback: just log that the participant is alive
                        logger.info("[RTT] identity=%s no_quality_info", identity)
            except Exception as e:
                logger.warning("[RTT] sampler error: %s", e)
            await asyncio.sleep(5.0)

    # Sentinel so the sampler stops cleanly when the session ends
    session_closed_for_sampler = asyncio.Event()

    asyncio.create_task(_rtc_rtt_sampler())
    # ------------------------------------------------------------------------------

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
        # With close_on_disconnect=False, this only fires when the room itself
        # dies (session-manager deleted/recreated it on restart) or the framework
        # is tearing the worker down. Treat as shutdown so the persistent loop
        # exits and the worker becomes free for the next dispatch.
        logger.info("[PERSIST] session.on(close) fired — treating as shutdown (room likely deleted)")
        session_closed.set()
        sc.shutdown_event.set()

    @session.on("conversation_item_added")
    def _on_item_added(event) -> None:
        """Push agent text to session-manager immediately (before TTS finishes)."""
        item = event.item
        if not hasattr(item, "role") or str(item.role) != "assistant":
            return
        text = (getattr(item, "text_content", None) or "").strip()
        if not text:
            return
        logger.info("early_transcript speaker=Pepper text=%s", text[:120])

    t0 = time.monotonic()
    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            # Persistent agent: do NOT tear down when the user briefly disconnects.
            # The session must keep its STT pipeline and lk.chat text handler alive
            # so the next reconnect (audio or UI text) is handled immediately.
            close_on_disconnect=False,
            participant_identity=str(getattr(participant, "identity", "") or ""),
            text_input=room_io.TextInputOptions(),
        ),
    )
    logger.info(
        "[PERSIST] session.start complete close_on_disconnect=False text_input=lk.chat",
    )
    logger.info("timing session.start=%.3fs agent_mode=%s", time.monotonic() - t0, agent_mode)

    await weaviate_task

    # --- Idle timeout tracking ---
    # The agent itself tracks speech activity and clears chat context after
    # SESSION_IDLE_TIMEOUT_SEC of silence (replaces session-manager idle logic).
    last_user_activity = time.monotonic()

    @session.on("user_input_transcribed")
    def _on_user_speech(event) -> None:
        nonlocal last_user_activity
        last_user_activity = time.monotonic()
        text = getattr(event, "text", "") or ""
        if text.strip():
            logger.info("user_speech text=%s", text.strip()[:120])

    if is_warm:
        logger.info(
            "warm_agent_ready agent_mode=%s persistent=%s total_warm_setup=%.3fs",
            agent_mode,
            is_persistent,
            time.monotonic() - t_entry,
        )

    logger.info(
        "agent_listening agent_mode=%s total_setup=%.3fs",
        agent_mode,
        time.monotonic() - t_entry,
    )

    if not is_persistent:
        # Non-warm dispatch (e.g. cold-only flow): single session, exit when done.
        logger.info("[PERSIST] non-persistent path — awaiting session_closed")
        await session_closed.wait()
        return

    # --- Persistent warm agent loop (both local & openai) ---
    # The agent stays in the room forever. When idle for SESSION_IDLE_TIMEOUT_SEC
    # it clears its chat history in-place. On `shutdown` (mode switch / external
    # signal) it exits cleanly so the worker can be redispatched with new config.
    logger.info(
        "[PERSIST] entering persistent loop agent_mode=%s idle_timeout=%ss",
        agent_mode,
        SESSION_IDLE_TIMEOUT_SEC,
    )

    async def _idle_monitor() -> None:
        """Check for idle timeout every 2s and set the reset event when triggered."""
        nonlocal last_user_activity
        while not sc.shutdown_event.is_set():
            await asyncio.sleep(2)
            idle_sec = time.monotonic() - last_user_activity
            if idle_sec >= SESSION_IDLE_TIMEOUT_SEC:
                logger.info(
                    "[idle] %.0fs silence — clearing conversation history",
                    idle_sec,
                )
                sc.reset_event.set()
                # Wait until the reset is processed before resuming monitoring
                while sc.reset_event.is_set() and not sc.shutdown_event.is_set():
                    await asyncio.sleep(0.5)

    idle_task = asyncio.create_task(_idle_monitor())

    session_num = 0
    try:
        while True:
            reset_task = asyncio.ensure_future(sc.reset_event.wait())
            shutdown_task = asyncio.ensure_future(sc.shutdown_event.wait())
            done, pending = await asyncio.wait(
                [reset_task, shutdown_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()

            if sc.shutdown_event.is_set():
                logger.info(
                    "[PERSIST] shutdown received — exiting loop sessions_served=%d agent_mode=%s",
                    session_num,
                    agent_mode,
                )
                return

            session_num += 1
            logger.info(
                "[PERSIST] resetting chat history session_num=%d",
                session_num,
            )
            try:
                await session.interrupt()
                session.clear_user_turn()
                await agent.update_chat_ctx(llm.ChatContext.empty())
                logger.info(
                    "[PERSIST] history cleared, agent ready for next user session_num=%d",
                    session_num,
                )
            except Exception as exc:
                logger.error("[PERSIST] reset failed error=%s", exc)

            # Reset the activity timer so the next idle window starts now
            last_user_activity = time.monotonic()
            sc.reset_event.clear()
            logger.info("[PERSIST] persistent_agent_ready session_num=%d", session_num)
    finally:
        idle_task.cancel()


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
