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
    WEAVIATE_COLLECTION,
    WEAVIATE_OPENAI_MODEL,
)
from .tools import build_tools
from .utils import connect_weaviate, seed_collection


load_dotenv(dotenv_path=Path(__file__).parent.parent / ".env")
logger = logging.getLogger("voice-agent")
logger.propagate = True
if logger.handlers:
    logger.handlers.clear()
if not logging.getLogger().handlers:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))


async def entrypoint(ctx: JobContext) -> None:
    logger.info("agent version=%s", AGENT_VERSION)

    await ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_ALL)
    participant = await ctx.wait_for_participant()

    room_name = getattr(ctx.room, "name", "") or "room"
    participant_name = (getattr(participant, "name", "") or "").strip()
    participant_identity = (getattr(participant, "identity", "") or "").strip()
    participant_phone = (
        participant_identity
        or participant_name
        or getattr(participant, "sid", "")
        or "unknown"
    )
    logger.info(
        "session_start room=%s participant_name=%s participant_identity=%s participant_phone=%s "
        "agent_version=%s model_name=%s tts_voice=%s weaviate_collection=%s weaviate_embed_model=%s",
        room_name,
        participant_name,
        participant_identity,
        participant_phone,
        AGENT_VERSION,
        MODEL_NAME,
        TTS_VOICE,
        WEAVIATE_COLLECTION,
        WEAVIATE_OPENAI_MODEL,
    )

    with connect_weaviate() as client:
        seed_collection(client)

    session = AgentSession(
        llm=openai.realtime.RealtimeModel(
            model=MODEL_NAME,
            voice=TTS_VOICE,
            api_key=os.getenv("OPENAI_API_KEY"),
        ),
    )

    tools = build_tools()
    agent = Agent(
        instructions=SYSTEM_PROMPT,
        tools=tools,
    )

    session_closed = asyncio.Event()

    @session.on("close")
    def _on_close(_) -> None:
        logger.info(
            "session_close room=%s participant_name=%s participant_identity=%s participant_phone=%s",
            room_name,
            participant_name,
            participant_identity,
            participant_phone,
        )
        session_closed.set()

    @session.on("conversation_item_added")
    def _on_conversation_item(event) -> None:
        message = getattr(event, "item", None)
        if not message or getattr(message, "type", None) != "message":
            return
        role = getattr(message, "role", None)
        text = getattr(message, "text_content", None)
        if role and text:
            logger.info(
                "conversation_item room=%s participant_phone=%s role=%s created_at=%s text=%s",
                room_name,
                participant_phone,
                role,
                getattr(message, "created_at", None),
                text,
            )

    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(close_on_disconnect=True),
    )

    handle = await session.generate_reply(
        instructions=VOICE_AGENT_GREETING_INSTRUCTIONS,
    )
    await handle.wait_for_playout()

    await session_closed.wait()


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
