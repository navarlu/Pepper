"""Realtime experiment worker (Mode B) — runs on the RPi.

agent_name = "pepper-experiment-realtime". Same room (`pepper-experiment`),
same launcher (launcher.py picks the agent_name from --variant), same
recorder JSONL schema as the local-stack worker in agent.py — only the
audio + LLM pipeline differs:

  Mode A (agent.py, on woska):
    silero VAD → FasterWhisper STT → vLLM Llama → Piper TTS
    speaks via `send_message_to_user` tool (terminal).

  Mode B (this file, on the RPi):
    OpenAI Realtime API (`gpt-realtime-mini`) — handles VAD, STT,
    LLM, TTS server-side in one socket. Speaks NATIVELY via the
    audio modality. The `send_message_to_user` tool is REMOVED from
    the surface; everything else (lookup_person, find_path_to_room,
    mensa_menu, subject_schedule, get_time, adjust_volume) is reused
    unchanged so tool-call traces, animations, and tablet behaviour
    stay consistent across modes.

Run:
    uv run python voice-agent/src/experiment/agent_realtime.py dev

Inside docker (the normal path on the RPi):
    docker compose -f docker/docker-compose.experiment.yml up -d voice-agent-realtime
    docker compose -f docker/docker-compose.experiment.yml logs -f voice-agent-realtime

Prereqs:
  * OPENAI_API_KEY in .env at the project root.
  * LiveKit server is up (docker-compose.experiment.yml).
"""

from __future__ import annotations

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any

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

THIS_DIR = Path(__file__).resolve().parent
VOICE_AGENT_DIR = THIS_DIR.parent.parent
for p in (str(THIS_DIR), str(VOICE_AGENT_DIR)):
    if p not in sys.path:
        sys.path.insert(0, p)

ROOT_ENV_PATH = VOICE_AGENT_DIR.parent / ".env"
if ROOT_ENV_PATH.exists():
    load_dotenv(dotenv_path=ROOT_ENV_PATH, override=False)

os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

from src.live.bridge_client import post_tablet_clear  # noqa: E402

import importlib  # noqa: E402

_PROMPT_MODULE = os.environ.get("EXPERIMENT_PROMPT_MODULE", "prompt_realtime")
_TOOLS_MODULE = os.environ.get("EXPERIMENT_TOOLS_MODULE", "tools")
_prompt_mod = importlib.import_module(_PROMPT_MODULE)
SYSTEM_PROMPT = _prompt_mod.SYSTEM_PROMPT
tools = importlib.import_module(_TOOLS_MODULE)

# Mode B speaks natively — drop send_message_to_user (no emotion-on-speech
# wrapper) and add play_gesture so the realtime model can still trigger
# body language explicitly.
from tools.play_gesture import play_gesture  # noqa: E402

REALTIME_TOOLS = [
    t for t in tools.LIVEKIT_TOOLS_TOOLONLY
    if getattr(t, "name", None) != "send_message_to_user"
    and getattr(getattr(t, "info", None), "name", None) != "send_message_to_user"
] + [play_gesture]

logger = logging.getLogger("experiment-realtime-worker")
logging.getLogger("livekit.agents").setLevel(logging.INFO)
logging.getLogger("livekit.plugins.openai").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

AGENT_NAME = os.environ.get(
    "PEPPER_EXPERIMENT_REALTIME_AGENT_NAME", "pepper-experiment-realtime"
)
USER_IDENTITY = os.environ.get("USER_IDENTITY", "user")
RECORDER_IDENTITY = "experimenter-recorder"
TOPIC_EXPERIMENT = "pepper.experiment"

# Hardcoded per design — swap by editing this file.
REALTIME_MODEL = "gpt-realtime-mini"
REALTIME_VOICE = os.environ.get("REALTIME_VOICE", "marin")

def _tool_name(t) -> str:
    return (
        getattr(t, "name", None)
        or getattr(getattr(t, "info", None), "name", None)
        or repr(t)
    )


print(
    f"[experiment-realtime-agent] booted, waiting for dispatch "
    f"(livekit_url={os.environ.get('LIVEKIT_URL', 'unset')} "
    f"model={REALTIME_MODEL} voice={REALTIME_VOICE})",
    flush=True,
)
print(
    f"[experiment-realtime-agent] tools=[{', '.join(_tool_name(t) for t in REALTIME_TOOLS)}]",
    flush=True,
)


class _RealtimeExperimentAgent(Agent):
    def __init__(self) -> None:
        super().__init__(instructions=SYSTEM_PROMPT, tools=REALTIME_TOOLS)


def _parse_dispatch_metadata(ctx: JobContext) -> dict:
    raw = ""
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


async def _wait_for_user_participant(ctx: JobContext):
    last_logged = None
    while True:
        rps = list((getattr(ctx.room, "remote_participants", {}) or {}).values())
        for p in rps:
            ident = str(getattr(p, "identity", "") or "")
            if ident == USER_IDENTITY:
                return p
            if ident and ident != last_logged:
                logger.info("waiting_for_user skipping=%s", ident)
                last_logged = ident
        await asyncio.sleep(0.2)


async def entrypoint(ctx: JobContext) -> None:
    t_entry = time.monotonic()
    meta = _parse_dispatch_metadata(ctx)
    variant = str(meta.get("experiment_variant") or "").strip()
    student_id = str(meta.get("experiment_student_id") or "").strip()

    print(
        f"[experiment-realtime-worker] BOOT room={ctx.room.name} "
        f"variant={variant!r} student_id={student_id!r} model={REALTIME_MODEL}",
        flush=True,
    )

    await ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_ALL)
    participant = await _wait_for_user_participant(ctx)
    logger.info("user_joined identity=%s", getattr(participant, "identity", ""))

    main_loop = asyncio.get_running_loop()
    t_base = time.monotonic()

    def _ts() -> str:
        return f"+{time.monotonic() - t_base:6.3f}s"

    async def _publish_topic(topic: str, payload: dict) -> None:
        try:
            data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            await ctx.room.local_participant.publish_data(data, topic=topic)
        except Exception as exc:
            logger.debug("publish_failed topic=%s err=%s", topic, exc)

    async def _publish(payload: dict) -> None:
        await _publish_topic(TOPIC_EXPERIMENT, payload)

    def _publish_sync(payload: dict) -> None:
        try:
            asyncio.run_coroutine_threadsafe(_publish(payload), main_loop)
        except Exception as exc:
            logger.debug("publish_schedule_failed err=%s", exc)

    def _publish_sync_topic(topic: str, payload: dict) -> None:
        try:
            asyncio.run_coroutine_threadsafe(_publish_topic(topic, payload), main_loop)
        except Exception as exc:
            logger.debug("publish_schedule_failed topic=%s err=%s", topic, exc)

    async def _clear_tablet() -> None:
        try:
            await asyncio.to_thread(post_tablet_clear)
        except Exception as exc:
            logger.debug("tablet_clear_failed err=%s", exc)

    def _clear_tablet_sync() -> None:
        try:
            asyncio.run_coroutine_threadsafe(_clear_tablet(), main_loop)
        except Exception as exc:
            logger.debug("tablet_clear_schedule_failed err=%s", exc)

    def _on_tool_event(name: str, args: dict) -> None:
        try:
            args_preview = json.dumps(args, ensure_ascii=False, default=str)
        except Exception:
            args_preview = repr(args)
        print(f"  {_ts()} [TOOL] {name}({args_preview})", flush=True)
        _publish_sync({
            "kind": "tool_call",
            "name": name,
            "args": args,
            "ts": time.time(),
        })

    def _on_tool_result(name: str, result: Any) -> None:
        try:
            preview = json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            preview = repr(result)
        if len(preview) > 300:
            preview = preview[:300] + "…"
        print(f"  {_ts()} [TOOL-RESULT] {name} -> {preview}", flush=True)
        _publish_sync({
            "kind": "tool_result",
            "name": name,
            "result": result,
            "ts": time.time(),
        })

    tools.set_tool_event_listener(_on_tool_event)
    tools.set_tool_result_listener(_on_tool_result)

    if not os.environ.get("OPENAI_API_KEY"):
        print(
            "[experiment-realtime-worker] ERROR: OPENAI_API_KEY not set "
            "(check .env at project root).",
            file=sys.stderr,
            flush=True,
        )
        ctx.shutdown(reason="missing_openai_api_key")
        return

    # Disable self-interruption: Pepper's speaker bleeds into the mic,
    # so server-side VAD was cutting her off mid-utterance. Keep VAD on
    # (we still need turn detection so the model knows when the user
    # has finished speaking) but set interrupt_response=False so the
    # mic echo can't kill an in-progress reply. Plus bump
    # min_interruption_duration to 3.0s on the agent side, mirroring
    # Mode A (LOCAL_MIN_INTERRUPTION) — belt-and-suspenders.
    from openai.types.realtime.realtime_audio_input_turn_detection import (  # noqa: E402
        SemanticVad,
    )
    realtime_llm = openai.realtime.RealtimeModel(
        model=REALTIME_MODEL,
        voice=REALTIME_VOICE,
        turn_detection=SemanticVad(
            type="semantic_vad",
            create_response=True,
            eagerness="medium",
            interrupt_response=False,
        ),
    )
    session = AgentSession(
        llm=realtime_llm,
        min_interruption_duration=float(
            os.environ.get("REALTIME_MIN_INTERRUPTION", "3.0")
        ),
    )

    _typed_inputs: set[str] = set()

    _publish_sync({
        "kind": "session_start",
        "room": ctx.room.name,
        "variant": variant,
        "student_id": student_id,
        "model": REALTIME_MODEL,
        "mode": "realtime",
        "ts": time.time(),
    })

    _publish_sync_topic("pepper.state", {
        "agent_mode": "realtime",
        "agent_language": os.environ.get("AGENT_LANG", "en"),
    })

    @session.on("user_started_speaking")
    def _on_user_started(_event) -> None:
        print(f"  {_ts()} [STAGE B] user started speaking", flush=True)
        _clear_tablet_sync()

    @session.on("user_stopped_speaking")
    def _on_user_stopped(_event) -> None:
        print(f"  {_ts()} [STAGE B] user stopped speaking", flush=True)

    @session.on("agent_state_changed")
    def _on_agent_state(event) -> None:
        new_state = getattr(event, "new_state", None) or getattr(event, "state", "?")
        print(f"  {_ts()} [STAGE D] agent_state={new_state}", flush=True)
        _publish_sync_topic("pepper.state", {"agent_state": str(new_state)})

    @session.on("conversation_item_added")
    def _on_item_added(event) -> None:
        item = event.item
        role = str(getattr(item, "role", "") or "")
        text = (getattr(item, "text_content", "") or "").strip()
        if not text:
            return
        if role == "user":
            input_kind = "typed" if text in _typed_inputs else "speech"
            if input_kind == "typed":
                _typed_inputs.discard(text)
            print(f"  [USER:{input_kind}] {text!r}", flush=True)
            _publish_sync({
                "kind": "user_turn",
                "text": text,
                "input": input_kind,
                "ts": time.time(),
            })
            logger.info("user_turn input=%s text=%s", input_kind, text)
        elif role == "assistant":
            # Realtime emits the model's spoken transcript here — this
            # is the canonical record of what Pepper said in Mode B
            # (no send_message_to_user wrapper in this mode).
            print(f"  [AGENT-SPEECH] {text!r}", flush=True)
            _publish_sync({
                "kind": "agent_speech",
                "text": text,
                "ts": time.time(),
            })

    shutdown_event = asyncio.Event()

    @ctx.room.on("data_received")
    def _on_data(packet):
        topic = str(getattr(packet, "topic", "") or "")
        if topic == "pepper.text":
            try:
                msg = json.loads(getattr(packet, "data", b"") or b"")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return
            text = str(msg.get("text", "") or "").strip()
            if not text:
                return
            sender = str(getattr(getattr(packet, "participant", None), "identity", "") or "?")
            logger.info("[pepper.text] from=%s text=%s", sender, text[:120])
            _clear_tablet_sync()
            _typed_inputs.add(text)
            try:
                session.interrupt()
                session.generate_reply(user_input=text)
            except Exception as exc:
                logger.warning("pepper.text dispatch failed err=%s", exc)
            return
        if topic != "pepper.control":
            return
        try:
            msg = json.loads(getattr(packet, "data", b"") or b"")
        except Exception:
            return
        if str(msg.get("cmd", "")).strip().lower() == "shutdown":
            logger.info("shutdown signal received via pepper.control")
            shutdown_event.set()

    @ctx.room.on("participant_disconnected")
    def _on_participant_left(p):
        identity = str(getattr(p, "identity", "") or "")
        if identity == RECORDER_IDENTITY:
            logger.info("recorder disconnected (%s) — shutting down job", identity)
            shutdown_event.set()

    agent = _RealtimeExperimentAgent()
    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            participant_identity=str(getattr(participant, "identity", "") or USER_IDENTITY),
        ),
    )
    logger.info("session_started total_setup=%.2fs", time.monotonic() - t_entry)

    # No proactive greeting: wait for the user to speak first.

    session_closed = asyncio.Event()

    @session.on("close")
    def _on_close(_):
        session_closed.set()

    await asyncio.wait(
        [
            asyncio.create_task(shutdown_event.wait()),
            asyncio.create_task(session_closed.wait()),
        ],
        return_when=asyncio.FIRST_COMPLETED,
    )

    _publish_sync({
        "kind": "session_end",
        "ts": time.time(),
    })

    try:
        await session.aclose()
    except Exception as exc:
        logger.debug("session.aclose failed: %s", exc)
    ctx.shutdown(reason="experiment_realtime_done")
    logger.info("experiment realtime session ended")


if __name__ == "__main__":
    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=entrypoint,
            initialize_process_timeout=60.0,
            num_idle_processes=0,
            agent_name=AGENT_NAME,
            job_memory_warn_mb=1500,
            max_retry=2**31 - 1,
        )
    )
