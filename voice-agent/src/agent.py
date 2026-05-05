import asyncio
import logging
import os
import threading
import time
import json
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

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

from .animation_director import AnimationDirector
from .config import (
    AGENT_NAME,
    AGENT_VERSION,
    GREETING_INSTRUCTIONS,
    LANG,
    LISTENER_IDENTITY,
    MONITOR_IDENTITY,
    USER_IDENTITY,
    LIVEKIT_URL,
    LOCAL_LLM_BASE_URL,
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
from .qwen_compat import install_function_args_patch
from .rag import connect_weaviate, seed_collection
from .tools import build_tools

logger = logging.getLogger("voice-agent")

# Install the Qwen 2.5 tool-args sanitizer as early as possible — before any
# LLM call builds its request pipeline. See `qwen_compat.py` for the why.
install_function_args_patch()

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


def _print_boot_banner() -> None:
    """Loud, unmissable boot banner so the tmux pane shows what build is live.

    Bumping AGENT_VERSION + restarting the agent should change the banner.
    The prompt fingerprint (sha256[:8]) confirms the *content* of the system
    prompt loaded into this Python process — useful when bind-mounted source
    is edited but the running process holds the previous version in memory.
    """
    import hashlib
    prompt_hash = hashlib.sha256(LOCAL_SYSTEM_PROMPT.encode("utf-8")).hexdigest()[:8]
    prompt_preview = LOCAL_SYSTEM_PROMPT.split("\n")[0][:80]
    banner = [
        "================================================================",
        f"[BOOT] voice-agent v{AGENT_VERSION}",
        f"[BOOT] agent_name={AGENT_NAME}",
        f"[BOOT] prompt_sha8={prompt_hash}  len={len(LOCAL_SYSTEM_PROMPT)}",
        f"[BOOT] prompt[0]: {prompt_preview}",
        "================================================================",
    ]
    for line in banner:
        print(line, flush=True)
        logger.info(line)


_print_boot_banner()


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


# region: wait_for_user_participant
async def _wait_for_user_participant(ctx: JobContext):
    # Must bind specifically to USER_IDENTITY — binding to any non-listener
    # (e.g. tablet, debug-cli) leaves AgentSession subscribed to a silent
    # track and the LLM never hears the human.
    last_logged_identity = None
    while True:
        for participant in _iter_remote_participants(ctx):
            identity = str(getattr(participant, "identity", "") or "")
            if identity == USER_IDENTITY:
                return participant
            if identity and identity != last_logged_identity:
                logger.info(
                    "waiting_for_user_participant skipping_identity=%s",
                    identity,
                )
                last_logged_identity = identity
        await asyncio.sleep(0.2)
# endregion


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
        # Who triggered the most recent reset: "idle" (timeout) or "manual"
        # (external /reset via pepper.control). Used for observability so the
        # chat CLI can show WHY the history was wiped.
        self.reset_reason: str = ""

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
                self.reset_reason = "manual"
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


_RESOLVED_LOCAL_MODEL: str | None = None
_MODEL_DISCOVERY_TIMEOUT_SEC = 3.0


def _resolve_local_model_name() -> str:
    """Ask the vLLM server which model it is currently serving.

    Hits GET {LOCAL_LLM_BASE_URL}/models, returns data[0].id, caches the
    result for the lifetime of the process. Raises RuntimeError on any
    failure so the agent refuses to start with a wrong/stale model name
    instead of silently falling back to the config default.
    """
    global _RESOLVED_LOCAL_MODEL
    if _RESOLVED_LOCAL_MODEL is not None:
        return _RESOLVED_LOCAL_MODEL

    url = f"{LOCAL_LLM_BASE_URL.rstrip('/')}/models"
    try:
        with urlopen(url, timeout=_MODEL_DISCOVERY_TIMEOUT_SEC) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        raise RuntimeError(
            f"Could not discover model from vLLM at {url}: {exc!r}. "
            f"Is the server running on woska?"
        ) from exc

    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list) or not data or not data[0].get("id"):
        raise RuntimeError(
            f"vLLM /models returned no model entries: {payload!r}"
        )

    model_id = str(data[0]["id"])
    logger.info(
        "local_model_discovered base_url=%s model=%s",
        LOCAL_LLM_BASE_URL, model_id,
    )
    _RESOLVED_LOCAL_MODEL = model_id
    return model_id


def _build_local_session() -> AgentSession:
    """Build an `AgentSession` using local STT (Whisper) + local LLM
    (vLLM) + local TTS (Piper).
    """
    # Sampling tuned for Llama 3.1 8B Instruct AWQ + vLLM llama3_json
    # parser. Validated end-to-end in voice-agent/tests/local_llm_benchmark/.
    # parallel_tool_calls=False is required: Llama 3.1's chat template
    # rejects assistant messages carrying more than one tool_call.
    local_llm = openai.LLM(
        model=_resolve_local_model_name(),
        base_url=LOCAL_LLM_BASE_URL,
        api_key="not-needed",
        # 0.2 — empirically the floor for Llama 3.1 8B AWQ on this prompt.
        # At 0.1 the model fixates on the highest-probability token
        # (play_animation greeting) and loops without producing text on
        # the very first turn of a fresh chat. 0.2 keeps tool-policy
        # discipline while leaving enough variance to escape that attractor.
        temperature=0.2,
        parallel_tool_calls=False,
        _strict_tool_schema=False,
    )
    local_llm.on("metrics_collected", _on_llm_metrics_collected)

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


def _count_user_messages(chat_ctx: llm.ChatContext) -> int:
    return sum(
        1
        for item in chat_ctx.items
        if getattr(item, "type", None) == "message"
        and getattr(item, "role", None) == "user"
    )


class GreetingAgent(Agent):
    """Agent subclass used in local mode that:
      1. Captures the latest chat_ctx on every llm_node call so the
         AnimationDirector can read it from outside the call stack.
      2. On the very first user turn, merges GREETING_INSTRUCTIONS into
         the existing system prompt via `update_instructions`. This
         steers Llama 3.1's first-turn reply into a brief verbal
         greeting; the chat template attaches the tool list to the
         FIRST user message so we cannot inject earlier than this.

    Ported from the local-LLM benchmark
    (`voice-agent/tests/local_llm_benchmark/livekit_console.py`)."""

    def __init__(self, instructions: str, tools: list, *, merge_greeting: bool) -> None:
        super().__init__(instructions=instructions, tools=tools)
        self._merge_greeting = merge_greeting
        self._greeting_done = False
        self._latest_chat_ctx: llm.ChatContext | None = None

    async def llm_node(self, chat_ctx, tools, model_settings):
        self._latest_chat_ctx = chat_ctx
        if self._merge_greeting and not self._greeting_done:
            user_msgs = _count_user_messages(chat_ctx)
            if user_msgs == 1:
                merged = f"{LOCAL_SYSTEM_PROMPT}\n\n{GREETING_INSTRUCTIONS}"
                update_instructions(chat_ctx, instructions=merged, add_if_missing=True)
                self._greeting_done = True
                logger.debug(
                    "greeting_merged user_msgs=%d chars=%d",
                    user_msgs, len(GREETING_INSTRUCTIONS),
                )
        return Agent.default.llm_node(self, chat_ctx, tools, model_settings)



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
        _resolve_local_model_name() if agent_mode == "local" else MODEL_NAME,
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
    # GreetingAgent subclass captures `_latest_chat_ctx` for the
    # AnimationDirector and (in local mode) merges
    # GREETING_INSTRUCTIONS on the first user turn. In OpenAI Realtime
    # mode the merge is skipped — the OpenAI prompt + Realtime model
    # don't have Llama's chat-template tool-eagerness.
    agent = GreetingAgent(
        instructions=_get_agent_instructions(agent_mode),
        tools=agent_tools,
        merge_greeting=(agent_mode == "local"),
    )

    # AnimationDirector — forced one-tool LLM pass picks an animation
    # for every assistant reply. Active in local mode only; OpenAI
    # Realtime keeps using the LLM-visible play_animation tool.
    animation_director: AnimationDirector | None = None
    if agent_mode == "local":
        animation_director = AnimationDirector(llm_instance=session.llm)

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

    main_loop = asyncio.get_running_loop()

    async def _publish_debug_async(payload: dict) -> None:
        try:
            data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            await ctx.room.local_participant.publish_data(data, topic="pepper.debug")
        except Exception as exc:
            logger.debug("debug_publish_failed err=%s", exc)

    def _on_tool_event(name, args, result, duration_ms, error):
        payload = {
            "kind": "tool_call",
            "name": name,
            "args": args,
            "result": result,
            "duration_ms": duration_ms,
            "error": error,
        }
        # Listener may be called from a worker thread (tool execution runs via to_thread),
        # so we cannot use asyncio.create_task — schedule onto the main loop instead.
        try:
            asyncio.run_coroutine_threadsafe(_publish_debug_async(payload), main_loop)
        except Exception as exc:
            logger.debug("debug_schedule_failed err=%s", exc)

    from . import tools as _tools_mod
    _tools_mod.set_tool_event_listener(_on_tool_event)

    @ctx.room.on("data_received")
    def _on_control_packet(packet):
        if str(getattr(packet, "topic", "") or "") != "pepper.control":
            return
        try:
            msg = json.loads(getattr(packet, "data", b"") or b"")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        cmd = str(msg.get("cmd", "")).strip().lower()
        if cmd == "reset":
            logger.info("[control] external reset requested via pepper.control")
            sc.reset_event.set()

    @ctx.room.on("data_received")
    def _on_text_packet(packet):
        """Accept text input from any participant (e.g. debug-cli) and feed
        it to the session as if it were the bound user. The agent's chat
        context records it as user input — the LLM never sees the LiveKit
        identity of the sender."""
        if str(getattr(packet, "topic", "") or "") != "pepper.text":
            return
        try:
            msg = json.loads(getattr(packet, "data", b"") or b"")
        except (json.JSONDecodeError, UnicodeDecodeError):
            return
        text = str(msg.get("text", "") or "").strip()
        if not text:
            return
        sender = str(getattr(getattr(packet, "participant", None), "identity", "") or "?")
        logger.info("[pepper.text] from=%s text=%s", sender, text[:120])
        # Text input counts as conversation activity — without this the idle
        # timer keeps firing while the user is chatting via text only.
        nonlocal last_user_activity, had_activity_since_reset
        last_user_activity = time.monotonic()
        had_activity_since_reset = True
        try:
            session.interrupt()
            session.generate_reply(user_input=text)
        except Exception as exc:
            logger.warning("pepper.text dispatch failed err=%s", exc)

    @session.on("conversation_item_added")
    def _on_item_added(event) -> None:
        """Push agent text to session-manager immediately (before TTS
        finishes), and trigger an animation pick via AnimationDirector
        in local mode."""
        item = event.item
        if not hasattr(item, "role") or str(item.role) != "assistant":
            return
        text = (getattr(item, "text_content", None) or "").strip()
        if not text:
            return
        logger.info("early_transcript speaker=Pepper text=%s", text[:120])
        if animation_director is not None and agent._latest_chat_ctx is not None:
            animation_director.schedule(agent._latest_chat_ctx)

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
    # The agent clears chat context after SESSION_IDLE_TIMEOUT_SEC of silence,
    # but ONLY if a real conversation has happened since the last reset. The
    # `had_activity_since_reset` flag prevents the noisy "clear empty history
    # every 60s" loop. Activity = user speech (transcribed) or text input via
    # pepper.text — anything that would put something in the chat context.
    last_user_activity = time.monotonic()
    had_activity_since_reset = False

    @session.on("user_input_transcribed")
    def _on_user_speech(event) -> None:
        nonlocal last_user_activity, had_activity_since_reset
        last_user_activity = time.monotonic()
        had_activity_since_reset = True
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

    # region: idle_monitor
    async def _idle_monitor() -> None:
        """Check for idle timeout every 2s. Only fires reset when there is
        something to reset — i.e. real conversation activity has happened
        since the last reset. Otherwise we'd clear an already-empty history
        every 60s forever."""
        nonlocal last_user_activity
        while not sc.shutdown_event.is_set():
            await asyncio.sleep(2)
            if not had_activity_since_reset:
                continue
            idle_sec = time.monotonic() - last_user_activity
            if idle_sec >= SESSION_IDLE_TIMEOUT_SEC:
                logger.info(
                    "[idle] %.0fs silence — clearing conversation history",
                    idle_sec,
                )
                sc.reset_reason = "idle"
                sc.reset_event.set()
                # Wait until the reset is processed before resuming monitoring
                while sc.reset_event.is_set() and not sc.shutdown_event.is_set():
                    await asyncio.sleep(0.5)
    # endregion

    idle_task = asyncio.create_task(_idle_monitor())

    session_num = 0
    try:
        # region: persistent_loop
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
                # Per LiveKit docs: returning from entrypoint is NOT enough —
                # the job stays alive as long as other non-agent participants
                # (user-client, debug-cli) are in the room. Must explicitly
                # call ctx.shutdown() + session.aclose() so the framework
                # disconnects us from the room and the participant record is
                # gone. Without this, mode-switch leaves a zombie.
                try:
                    await session.aclose()
                except Exception as exc:
                    logger.debug("session.aclose failed err=%s", exc)
                ctx.shutdown(reason="mode_switch_requested")
                return

            session_num += 1
            reason = sc.reset_reason or "unknown"
            logger.info(
                "[PERSIST] resetting chat history session_num=%d reason=%s",
                session_num,
                reason,
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

            # Broadcast the event so observers (chat CLI) can show it inline.
            asyncio.create_task(_publish_debug_async({
                "kind": "session_reset",
                "reason": reason,
                "session_num": session_num,
            }))

            # Reset the activity timer + dirty flag so the next idle window
            # starts now and won't fire again until there's real activity.
            last_user_activity = time.monotonic()
            had_activity_since_reset = False
            sc.reset_reason = ""
            sc.reset_event.clear()
            logger.info("[PERSIST] persistent_agent_ready session_num=%d", session_num)
        # endregion
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
