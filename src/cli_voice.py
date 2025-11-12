from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentSession,
    AutoSubscribe,
    ConversationItemAddedEvent,
    JobContext,
    UserInputTranscribedEvent,
    WorkerOptions,
    cli,
    function_tool,
)
from livekit.plugins import openai, silero

from cli_chat import (
    CONVERSATION_BLOCK_ID,
    ROBOT_TARGET,
    id_react_ego_start,
    id_react_user_text,
    pepper_say,
)
from letta_io import append_block_text, sanitize_letta_text


# --- env ------------------------------------------------------------------
ROOT_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT_DIR / ".env"
try:
    load_dotenv(ENV_PATH)
except Exception:
    load_dotenv()


LETTA_VOICE_BASE = os.getenv("LETTA_BASE", "http://localhost:8283/v1/voice-beta")
VOICE_AGENT_ID = (
    os.getenv("VOICE_AGENT_ID")
    or os.getenv("AGENT_ID")
    or os.getenv("EGO_AGENT_ID")
    or "agent-0a54da6e-93a1-4092-837c-5f0141809f8b"
)
LETTA_KEY = os.getenv("LETTA_API_KEY")
VOICE_OUTPUT_MODE = (os.getenv("VOICE_OUTPUT_MODE") or "livekit").strip().lower()
USE_LIVEKIT_TTS = VOICE_OUTPUT_MODE in {"livekit", "room", "speaker"}


# --- tools ----------------------------------------------------------------
@function_tool
async def lookup_weather(context: Any, location: str) -> dict:
    return {"weather": "sunny", "temperature": 70}


# --- LLM bound to Letta ----------------------------------------------------
LLM = openai.LLM.with_letta(
    agent_id=VOICE_AGENT_ID,
    api_key=LETTA_KEY,
    base_url=f"{LETTA_VOICE_BASE}",
)


# --- helpers ---------------------------------------------------------------
def _strip_trailing_unbalanced_quote(text: str) -> str:
    cnt = 0
    i = 0
    while i < len(text):
        if text[i] == '"':
            esc = False
            j = i - 1
            while j >= 0 and text[j] == "\\":
                esc = not esc
                j -= 1
            if not esc:
                cnt += 1
        i += 1
    if cnt % 2 == 1 and text.rstrip().endswith('"'):
        return text.rstrip()[:-1].rstrip()
    return text


def sanitize_response_text(text: Optional[str]) -> str:
    if not text:
        return ""
    cleaned = sanitize_letta_text(text, preserve_whitespace=True)
    try:
        if cleaned.startswith('"') and cleaned.endswith('"'):
            cleaned = json.loads(cleaned)
    except Exception:
        pass
    cleaned = cleaned.replace("\\\"", '"')
    return _strip_trailing_unbalanced_quote(cleaned)


async def _run_in_background(func, *args, **kwargs):
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, lambda: func(*args, **kwargs))


async def _log_conversation(role: str, text: str):
    if not CONVERSATION_BLOCK_ID:
        return
    await _run_in_background(append_block_text, CONVERSATION_BLOCK_ID, f"{role}: {text}")


async def _trigger_id_user(text: str):
    await _run_in_background(id_react_user_text, text)


async def _trigger_id_ego_start(text: str):
    await _run_in_background(id_react_ego_start, text)


async def _pepper_say_async(text: str):
    if ROBOT_TARGET != "real":
        return
    await _run_in_background(
        pepper_say,
        text,
        animated=True,
        language="English",
        speed=50,
        pitchShift=0.5,
        volume=0.2,
    )


# --- Hooks for external integration ---------------------------------------
async def on_user_turn_final_text(text: str):
    print(f"[HOOK] user finished speaking: {text}")
    await asyncio.gather(
        _log_conversation("USER", text),
        _trigger_id_user(text),
        return_exceptions=True,
    )


async def on_assistant_text_before_tts(text: str):
    cleaned = sanitize_response_text(text)
    if not cleaned:
        return
    print(f"[HOOK] assistant will say: {cleaned}")
    tasks = [
        _log_conversation("EGO", cleaned),
        _trigger_id_ego_start(cleaned),
    ]
    if not USE_LIVEKIT_TTS:
        tasks.append(_pepper_say_async(cleaned))
    await asyncio.gather(*tasks, return_exceptions=True)


async def entrypoint(ctx: JobContext):
    await ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_ALL)

    agent = Agent(
        instructions="You are a friendly assistant named Pepper. Be concise and helpful.",
        tools=[lookup_weather],
    )

    output_desc = "LiveKit room TTS" if USE_LIVEKIT_TTS else "Pepper on-device speech"
    print(f"[cli_voice] Voice output mode: {output_desc}")

    tts_model = (
        openai.TTS(model="gpt-4o-mini-tts", voice=os.getenv("TTS_VOICE", "alloy"))
        if USE_LIVEKIT_TTS
        else None
    )

    session = AgentSession(
        vad=silero.VAD.load(),
        stt=openai.STT(model="gpt-4o-transcribe", language=os.getenv("STT_LANG", "cs")),
        llm=LLM,
        tts=tts_model,
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
            asyncio.create_task(on_assistant_text_before_tts(text))
        elif role == "user":
            print(f"User: {text}")

    await session.start(agent=agent, room=ctx.room)

    greeting = await session.generate_reply(
        user_input="Hello!",
        instructions="Greet the user and ask how their day is going.",
    )

    await greeting.wait_for_playout()


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
