from livekit.agents import (
    JobContext, cli, WorkerOptions,
    Agent, AgentSession, function_tool,
    UserInputTranscribedEvent, ConversationItemAddedEvent, AutoSubscribe
)
from livekit.plugins import openai, silero
from pathlib import Path
import os
from dotenv import load_dotenv
import asyncio
from typing import Any
# --- env ---
try:
    load_dotenv(Path(__file__).resolve().parents[1] / ".env")
except Exception:
    load_dotenv()

# --- Letta config ---
LETTA_BASE = os.getenv("LETTA_BASE", "http://localhost:8283/v1/voice-beta")
AGENT_ID = os.getenv("AGENT_ID", "agent-0a54da6e-93a1-4092-837c-5f0141809f8b")
LETTA_KEY = os.getenv("LETTA_API_KEY")

# --- tools ---
@function_tool
async def lookup_weather(context: Any, location: str) -> dict:
    return {"weather": "sunny", "temperature": 70}

# --- LLM bound to Letta ---
LLM = openai.LLM.with_letta(
    agent_id=AGENT_ID,
    api_key=LETTA_KEY,
    base_url=f"{LETTA_BASE}",
)

# --- Hooks for external integration ---
async def on_user_turn_final_text(text: str):
    print(f"[HOOK] user finished speaking: {text}")

async def on_assistant_text_before_tts(text: str):
    print(f"[HOOK] assistant will say: {text}")


async def entrypoint(ctx: JobContext):
    await ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_ALL)

    agent = Agent(
        instructions="You are a friendly assistant named Pepper. Be concise and helpful.",
        tools=[lookup_weather],
    )

    session = AgentSession(
        vad=silero.VAD.load(),
        stt=openai.STT(model="gpt-4o-transcribe", language=os.getenv("STT_LANG", "cs")),
        llm=LLM,
        tts=openai.TTS(model="gpt-4o-mini-tts", voice=os.getenv("TTS_VOICE", "alloy")),
    )

    @session.on("user_input_transcribed")
    def _on_user_input_transcribed(ev: UserInputTranscribedEvent):
        if ev.is_final:
            print(f"User [{ev.language}]: {ev.transcript}")
            asyncio.create_task(on_user_turn_final_text(ev.transcript))

    @session.on("conversation_item_added")
    def _on_conversation_item_added(ev: ConversationItemAddedEvent):
        role = ev.item.role
        text = ev.item.text_content
        if not text:
            return
        if role == "assistant":
            print(f"Pepper: {text}")
            asyncio.create_task(on_assistant_text_before_tts(text))
        elif role == "user":
            print(f"User: {text}")

    await session.start(agent=agent, room=ctx.room)

    # optional greeting
    greeting = await session.generate_reply(
        user_input="Hello!",
        instructions="Greet the user and ask how their day is going.",
    )
    await greeting.wait_for_playout()


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))