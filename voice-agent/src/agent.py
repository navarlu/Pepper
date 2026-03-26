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
from livekit.plugins import openai

from .config import (
    AGENT_NAME,
    AGENT_VERSION,
    LISTENER_IDENTITY,
    LIVEKIT_URL,
    MODEL_NAME,
    SESSION_MANAGER_URL,
    SYSTEM_PROMPT,
    TTS_VOICE,
    VOICE_AGENT_GREETING_INSTRUCTIONS,
)
from .tools import build_tools
from .utils import connect_weaviate, seed_collection

logger = logging.getLogger("voice-agent")

ROOT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


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


async def entrypoint(ctx: JobContext) -> None:
    logger.info("agent version=%s model=%s", AGENT_VERSION, MODEL_NAME)
    openai_api_key = _get_required_env("OPENAI_API_KEY")

    dispatch_meta = _parse_dispatch_metadata(ctx)
    is_warm = bool(dispatch_meta.get("warm"))

    await ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_ALL)

    # Register activation listener early — before session.start() takes over room events
    activation_event, activation_payload = (None, None)
    if is_warm:
        activation_event, activation_payload = _setup_activation_listener(ctx)

    participant = await _wait_for_user_participant(ctx)

    logger.info(
        "session_start room=%s participant_name=%s participant_identity=%s warm=%s",
        getattr(ctx.room, "name", ""),
        getattr(participant, "name", ""),
        getattr(participant, "identity", ""),
        is_warm,
    )

    try:
        with connect_weaviate() as client:
            seed_collection(client)
    except Exception as exc:
        logger.warning("weaviate_init_failed error=%s", str(exc))

    session = AgentSession(
        llm=openai.realtime.RealtimeModel(
            model=MODEL_NAME,
            voice=TTS_VOICE,
            api_key=openai_api_key,
        )
    )

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

    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            close_on_disconnect=True,
            participant_identity=str(getattr(participant, "identity", "") or ""),
            text_input=room_io.TextInputOptions(text_input_cb=_text_input_cb),
        ),
    )

    if is_warm and activation_event is not None:
        # Agent is fully connected with live OpenAI session — wait for activation
        _post_debug_event({"event": "warm_ready", "active": True})
        logger.info("warm_agent_ready openai_session_established waiting_for_activation")
        await activation_event.wait()
        logger.info(
            "warm_agent_activated conversation_id=%s",
            activation_payload.get("conversation_id", ""),
        )

    greeting = await session.generate_reply(
        instructions=VOICE_AGENT_GREETING_INSTRUCTIONS,
    )
    await greeting.wait_for_playout()

    await session_closed.wait()


if __name__ == "__main__":
    heartbeat_stop = _start_component_heartbeat()
    try:
        cli.run_app(
            WorkerOptions(
                entrypoint_fnc=entrypoint,
                agent_name=AGENT_NAME,
            )
        )
    finally:
        heartbeat_stop.set()
        _post_component_status("stopping", "worker stopped", healthy=False)
