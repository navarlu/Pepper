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
from livekit.plugins import openai

from .config import (
    AGENT_VERSION,
    MODEL_NAME,
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


_load_root_env()


async def entrypoint(ctx: JobContext) -> None:
    logger.info("agent version=%s model=%s", AGENT_VERSION, MODEL_NAME)
    openai_api_key = _get_required_env("OPENAI_API_KEY")

    await ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_ALL)
    participant = await ctx.wait_for_participant()

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
        llm=openai.realtime.RealtimeModel(
            model=MODEL_NAME,
            voice=TTS_VOICE,
            api_key=openai_api_key,
        )
    )

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
        room_options=room_io.RoomOptions(close_on_disconnect=True),
    )

    greeting = await session.generate_reply(
        instructions=VOICE_AGENT_GREETING_INSTRUCTIONS,
    )
    await greeting.wait_for_playout()

    await session_closed.wait()


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
