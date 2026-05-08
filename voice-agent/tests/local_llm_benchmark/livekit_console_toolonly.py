"""Tool-only console agent — Letta / MemGPT-style flow ported to
LiveKit Agents.

Differences from `livekit_console.py`:
  * Plain assistant text from the LLM is never spoken to the user.
    The only way Pepper talks is through `send_message_to_user`,
    a tool that pushes its `text` argument through TTS via
    `session.say` and returns None.
  * Returning None makes LiveKit's loop terminate
    (generation.py:814 sets `reply_required = fnc_out is not None`,
    and agent_activity.py:2160 only re-invokes the LLM when
    `_reply_required` is True). So `send_message_to_user` is the
    natural terminal — exactly the role of Letta's `send_message`
    plus `TerminalToolRule`.
  * Every non-terminal tool gets a `request_heartbeat: bool = True`
    parameter and an `emotion: Literal[...]` parameter. heartbeat
    True (default) keeps the loop alive; False halts the loop
    without speaking by returning None.
  * `tts_node` is NOT overridden — `session.say` inside
    `send_message_to_user` uses the default TTS path; if the LLM
    accidentally emits plain text it WILL get spoken (graceful
    fallback rather than silent failure).

Run:
    uv run python voice-agent/tests/local_llm_benchmark/livekit_console_toolonly.py console --text

Prereqs: SSH tunnel from the RPi to woska's vLLM open on
localhost:8000. Same as `livekit_console.py`.
"""

from __future__ import annotations

# Quiet the framework's startup chatter and aiohttp deprecation warnings
# BEFORE any livekit-agents import — the warnings fire during their
# own imports, so this filter must land first. The env var also covers
# warnings that the cli.run_app codepath would emit after a fresh
# warnings filter context is created.
import os as _os
_os.environ.setdefault("PYTHONWARNINGS", "ignore::DeprecationWarning")
import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import json
import logging
import os
import sys
import urllib.request
from pathlib import Path

# Console mode does not actually connect to LiveKit Cloud, but the worker
# still validates LIVEKIT_URL/api creds at init.
os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")
os.environ.setdefault("LIVEKIT_API_KEY", "console-fake")
os.environ.setdefault("LIVEKIT_API_SECRET", "console-fake")

# Tune these back up to DEBUG when debugging the framework rather than
# the agent's behaviour. WARNING is the right floor for normal use —
# it kills "using audio io" / "using transcript io" / asyncio selector
# noise but still surfaces real problems.
logging.getLogger("livekit.agents").setLevel(logging.WARNING)
logging.getLogger("livekit.plugins.openai").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("voice-agent.udb").setLevel(logging.WARNING)

# Silence the verbose `[tool→LLM] {...}` JSON dump from tools/_common —
# the LiveKit console already renders a truncated version under
# `➜ tool_name ✓ ...`, so dumping it again is double-noise.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import tools._common as _tc
_tc.DEBUG_TOOL_RESULTS = False

from livekit.agents import (
    Agent,
    AgentSession,
    JobContext,
    WorkerOptions,
    cli,
    llm,
)
from livekit.agents.voice.generation import update_instructions
from livekit.plugins import openai, silero

from tools_toolonly import LIVEKIT_TOOLS_TOOLONLY  # noqa: E402

logger = logging.getLogger("livekit-console-toolonly")


# ── Experiment metadata pass-through ──────────────────────────────
# Set by experiment.py via env vars. Empty string when running the
# console standalone. The agent itself doesn't vary by variant — the
# variant labels which conversation scenario the experimenter ran
# (different topics A/B/C) so the JSONL log can be split downstream.
EXPERIMENT_VARIANT = os.environ.get("EXPERIMENT_VARIANT", "")
EXPERIMENT_STUDENT_ID = os.environ.get("EXPERIMENT_STUDENT_ID", "")

VLLM_BASE_URL = "http://localhost:8000/v1"
DEBUG = False

if DEBUG:
    logging.basicConfig(level=logging.DEBUG, force=True)
    logging.getLogger("livekit.agents").setLevel(logging.DEBUG)
    logging.getLogger("livekit.plugins.openai").setLevel(logging.DEBUG)
    logging.getLogger("openai").setLevel(logging.DEBUG)
    logging.getLogger("httpx").setLevel(logging.DEBUG)


# ── System prompt ────────────────────────────────────────────────────


SYSTEM_PROMPT = """\
You are Pepper, a humanoid receptionist robot at FEE (Faculty of
Electrical Engineering, CTU), Building E, Karlovo náměstí, Prague.

Personality: warm, brief, a little bit playful. You enjoy meeting
people. Speak like a friendly human receptionist, not a search engine.

What you ALREADY KNOW — never search for these:
  - You are Pepper, the friendly humanoid FEE receptionist.
  - You are in Building E on Karlovo náměstí, Prague.
  - FEE offers bachelor and master programmes in electrical
    engineering, informatics, and cybernetics.
  - The faculty staff directory is PUBLIC information published by
    CTU. Sharing it with visitors is your normal job.
  - Building E facilities — answer directly without find_path_to_room:
      • Gym: in the basement, right behind the main staircase.
      • Café: right above the main staircase (first floor).
      • Toilets: right above the main staircase, next to the café.
      • Lockers: right next to the main staircase, on the right
        side (ground floor where you stand with the user).

# How communication works (CRITICAL)

You communicate ONLY through tool calls. You have no other channel
to the user.

  - Plain assistant text is NEVER heard by the user. The TTS only
    speaks the `text` argument of `send_message_to_user`. If you
    write a normal reply, the user hears nothing.
  - To speak, call `send_message_to_user(text="...", emotion="...")`.
    This tool ENDS the turn — you cannot call any other tool after
    it. Do all your information gathering first, then speak last.
  - Every other tool returns its result to YOU (not the user). After
    you read the result, decide whether to call another tool or
    speak via `send_message_to_user`.
  - Each non-terminal tool has a `request_heartbeat` argument,
    default True. Leave it True so the loop continues and you get
    to speak. Only set False if you really want to halt the turn
    without speaking (rare).

# Body language

Every tool takes an `emotion` argument from this fixed set. You
choose freely per call — pick what matches the moment:

  greet     hello / welcome
  bow       thanks / goodbye / sign-off
  think     looking something up, considering, "let me check"
  explain   delivering a factual / informational answer
  happy     enthusiastic, playful, or warm affirmation
  dont_know apology, "I couldn't find", "I don't know"

Default heuristic (override freely when the moment calls for it):
  - On a non-terminal tool that fetches information
    (lookup_person, find_path_to_room, mensa_menu,
    subject_schedule, get_time, query_search): default to `think` —
    Pepper looks thoughtful while she works. Override with `happy`
    if the user's request was excited or playful, or with `dont_know`
    if you already suspect the result will be empty.
  - On send_message_to_user: pick the emotion that fits the WORDS
    you are about to speak. Most factual replies are `explain`;
    warm or playful replies are `happy`; apologies are `dont_know`;
    greetings are `greet`; goodbyes/thanks are `bow`. Match the
    feeling of the sentence, not a rigid mapping.

Rule of thumb: while you're FETCHING → usually `think`. While
you're SPEAKING → whatever fits the words.

# Reply style for `text` inside send_message_to_user

  - 1 to 3 short sentences. Plain conversational prose.
  - No markdown, bullets, line breaks for structure, JSON, tool
    names, argument names, or stage directions.
  - When mentioning a room, say only the room code (e.g. "E-107").
    Never read the street name or building address.
  - For greetings, smalltalk, opinions: speak directly via
    send_message_to_user, no other tool needed.
  - For factual lookups: call the relevant tool first
    (lookup_person, find_path_to_room, mensa_menu,
    subject_schedule, get_time), THEN call send_message_to_user
    with a short reply that uses the tool's result.
  - When `lookup_person` returns multiple candidates, ask the user
    via send_message_to_user which one they meant.
  - When a tool result has `error: "missing_first_name"`, your next
    send_message_to_user is "What is their first name?" — do not
    call lookup_person again until the user has answered.
  - Never call a tool with values you don't have. If the user
    didn't name the inputs, ask via send_message_to_user first.

# Examples of correct turn shapes

  - User: "Hi" → call send_message_to_user(text="Hello! How can I
    help?", emotion="greet"). Done.
  - User: "Where is room 230?" → call find_path_to_room(room="230",
    emotion="explain"), read result, then send_message_to_user with
    the directions, emotion="explain". Done.
  - User: "What's for lunch?" → call mensa_menu(emotion="think"),
    read result, then send_message_to_user with 1-2 dishes,
    emotion="explain". Done.

Do not chain more tools than necessary. Maximum a few steps per
user turn before you must speak.
"""


GREETING_INSTRUCTIONS = """\
This is the user's first turn. Call send_message_to_user with
text="Hello, how can I help you today?" and emotion="greet".
"""


# ── Agent subclass ──────────────────────────────────────────────────


def _resolve_model_id() -> str:
    with urllib.request.urlopen(f"{VLLM_BASE_URL}/models", timeout=5) as resp:
        payload = json.loads(resp.read())
    return payload["data"][0]["id"]


def _count_user_messages(chat_ctx: llm.ChatContext) -> int:
    return sum(
        1
        for item in chat_ctx.items
        if getattr(item, "type", None) == "message"
        and getattr(item, "role", None) == "user"
    )


class ToolOnlyAgent(Agent):
    """Same first-turn greeting-merge trick as livekit_console.py's
    GreetingAgent — Llama 3.1's chat template attaches the tool list
    to the first user message, so we update the instructions inside
    `llm_node` rather than via `generate_reply(instructions=...)` at
    job pickup.

    No tts_node override here — speech goes through `session.say`
    inside the `send_message_to_user` tool body, which uses the
    default TTS path. Plain LLM text would also reach TTS, but the
    prompt + tool design steer the model away from emitting any.
    """

    def __init__(self) -> None:
        super().__init__(instructions=SYSTEM_PROMPT, tools=LIVEKIT_TOOLS_TOOLONLY)
        self._greeting_done = False

    async def llm_node(self, chat_ctx, tools, model_settings):
        user_msgs = _count_user_messages(chat_ctx)
        if user_msgs == 1 and not self._greeting_done:
            merged = f"{SYSTEM_PROMPT}\n\n{GREETING_INSTRUCTIONS}"
            update_instructions(chat_ctx, instructions=merged, add_if_missing=True)
            self._greeting_done = True
        return Agent.default.llm_node(self, chat_ctx, tools, model_settings)


# ── Entrypoint ──────────────────────────────────────────────────────


async def entrypoint(ctx: JobContext) -> None:
    model_id = _resolve_model_id()
    logger.info("connected to vllm model=%s", model_id)
    if EXPERIMENT_VARIANT or EXPERIMENT_STUDENT_ID:
        # Banner — visible in the captured log so the variant is
        # unambiguously recoverable from the file.
        print(
            f"[experiment] student_id={EXPERIMENT_STUDENT_ID!r} "
            f"variant={EXPERIMENT_VARIANT!r} model={model_id}",
            flush=True,
        )

    session = AgentSession(
        vad=silero.VAD.load(),
        llm=openai.LLM(
            model=model_id,
            base_url=VLLM_BASE_URL,
            api_key="not-needed",
            temperature=0.2,
            parallel_tool_calls=False,
        ),
        # Cap the per-turn loop. send_message_to_user normally
        # terminates by returning None, so this is a safety net for
        # turns where the model loops on lookups without ever
        # calling send_message_to_user. After max_tool_steps, the
        # framework forces tool_choice="none" and the next pass
        # produces text — that text WILL get spoken (graceful
        # fallback) instead of leaving the user hanging.
        max_tool_steps=4,
        # --- Audio mode: uncomment STT/TTS as in livekit_console.py ---
    )

    agent = ToolOnlyAgent()

    await session.start(agent=agent, room=ctx.room)


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))
