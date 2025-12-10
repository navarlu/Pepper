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
from livekit.plugins import openai as lk_openai
from livekit.plugins import silero

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


LETTA_VOICE_BASE = "http://127.0.0.1:8283/v1"


VOICE_AGENT_ID = "agent-705ed358-88fb-44de-a22c-ef1fcad56824"

LETTA_KEY = os.getenv("LETTA_API_KEY")
VOICE_OUTPUT_MODE = (os.getenv("VOICE_OUTPUT_MODE") or "livekit").strip().lower()
PEPPER_SPEAK = True
print("LETTA_VOICE_BASE:", LETTA_VOICE_BASE)
print("VOICE_AGENT_ID:", VOICE_AGENT_ID)


# --- tools ----------------------------------------------------------------
@function_tool
async def lookup_weather(context: Any, location: str) -> dict:
    return {"weather": "sunny", "temperature": 70}


# --- LLM bound to Letta ----------------------------------------------------
LLM = lk_openai.LLM.with_letta(
    agent_id=VOICE_AGENT_ID,
    api_key=LETTA_KEY,
    base_url="http://localhost:8283/v1",
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
    print(f"[VOICE CLI] Pepper is trying to speak ({len(text)} chars).")
    if ROBOT_TARGET != "real":
        return
    #TODO comented out just to test pepper lietsenr
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
    print(f"[HOOK] sending {len(cleaned)} chars into TTS pipeline")
    tasks = [
        _log_conversation("EGO", cleaned),
        _trigger_id_ego_start(cleaned),
    ]
    if PEPPER_SPEAK:
        print("[HOOK] PEPPER_SPEAK enabled -> mirroring text to Pepper")
        tasks.append(_pepper_say_async(cleaned))
    await asyncio.gather(*tasks, return_exceptions=True)

async def entrypoint(ctx: JobContext):
    await ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_ALL)

    agent = Agent(
        instructions="You are a friendly assistant named Pepper. Be concise and helpful.",
        tools=[lookup_weather],
    )

    output_targets = ["LiveKit room TTS"]
    if PEPPER_SPEAK:
        output_targets.append("Pepper mirroring")
    print(f"[cli_voice] Voice output mode env: {VOICE_OUTPUT_MODE}")
    print(f"[cli_voice] Voice output targets: {', '.join(output_targets)}")

    tts_voice = os.getenv("TTS_VOICE", "alloy")
    print(f"[cli_voice] Configured TTS voice: {tts_voice}")
    print(f"[cli_voice] PEPPER_SPEAK flag: {PEPPER_SPEAK}")
    
    session = AgentSession(
        vad=silero.VAD.load(),
        stt=lk_openai.STT(model="gpt-4o-transcribe", language=os.getenv("STT_LANG", "cs")),
        llm=LLM,
        tts=lk_openai.TTS(model="gpt-4o-mini-tts", voice="alloy"),
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
        print(f"[cli_voice] on conversation item added: {text}")
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
