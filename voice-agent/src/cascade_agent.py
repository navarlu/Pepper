import asyncio
import logging
import os
from pathlib import Path

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
    AGENT_VERSION,
    CASCADE_LLM_MODEL,
    CASCADE_STT_COMPUTE_TYPE,
    CASCADE_STT_CPU_THREADS,
    CASCADE_STT_DEVICE,
    CASCADE_STT_MODEL,
    CASCADE_TTS_LENGTH_SCALE,
    CASCADE_TTS_MODEL_PATH,
    CASCADE_TTS_NOISE_SCALE,
    CASCADE_TTS_NOISE_W_SCALE,
    CASCADE_TTS_SPEAKER_ID,
    CASCADE_TTS_USE_CUDA,
    LANG,
    LISTENER_IDENTITY,
    LIVEKIT_URL,
    SYSTEM_PROMPT,
    VOICE_AGENT_GREETING_INSTRUCTIONS,
)
from .local_speech import FasterWhisperSTT, PiperTTS
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


def _set_runtime_defaults() -> None:
    # Keep non-secret runtime defaults in config instead of `.env`.
    os.environ.setdefault("LIVEKIT_URL", LIVEKIT_URL)


_load_root_env()
_set_runtime_defaults()


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


async def entrypoint(ctx: JobContext) -> None:
    logger.info(
        "agent version=%s stt_model=%s llm_model=%s tts_model=%s",
        AGENT_VERSION,
        CASCADE_STT_MODEL,
        CASCADE_LLM_MODEL,
        str(CASCADE_TTS_MODEL_PATH),
    )
    await ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_ALL)
    participant = await _wait_for_user_participant(ctx)

    logger.info(
        "session_start room=%s participant_name=%s participant_identity=%s",
        getattr(ctx.room, "name", ""),
        getattr(participant, "name", ""),
        getattr(participant, "identity", ""),
    )

    try:
        with connect_weaviate() as client:
            seed_collection(client)
    except Exception as exc:
        logger.warning("weaviate_init_failed error=%s", str(exc))

    session = AgentSession(
        vad=silero.VAD.load(),
        stt=FasterWhisperSTT(
            model=CASCADE_STT_MODEL,
            language=LANG,
            device=CASCADE_STT_DEVICE,
            compute_type=CASCADE_STT_COMPUTE_TYPE,
            cpu_threads=CASCADE_STT_CPU_THREADS,
        ),
        llm=openai.LLM(
            model=CASCADE_LLM_MODEL,
        ),
        tts=PiperTTS(
            model_path=CASCADE_TTS_MODEL_PATH,
            use_cuda=CASCADE_TTS_USE_CUDA,
            speaker_id=CASCADE_TTS_SPEAKER_ID,
            length_scale=CASCADE_TTS_LENGTH_SCALE,
            noise_scale=CASCADE_TTS_NOISE_SCALE,
            noise_w_scale=CASCADE_TTS_NOISE_W_SCALE,
        ),
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

    greeting = await session.generate_reply(
        instructions=VOICE_AGENT_GREETING_INSTRUCTIONS,
    )
    await greeting.wait_for_playout()

    await session_closed.wait()


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
