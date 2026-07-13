"""Paper (Track B) realtime worker — RPi-only receptionist MVP.

Audio path:

    user-client (USB mic + AEC3) ─► LiveKit room ─► gpt-realtime[-mini]
                                                    (speech-to-speech,
                                                     server-side VAD,
                                                     2 tools)
    agent audio track ─► audio-bridge ─ssh+paplay─► Pepper speakers

One realtime model replaces the whole VAD+STT+LLM+TTS cascade of
`src/experiment/agent_4o_streaming.py`; the audio plumbing around it
is unchanged. Differences vs. the cascade worker:

  * `AgentSession(llm=RealtimeModel(...))` — no VAD/STT/TTS plugins,
    no `llm_node`/`tts_node` overrides (there is no text→speech seam
    to instrument; latency probes come from the agent-state events
    and the audio bridge's `speaker_first_sound`).
  * Czech prompt, 2 tools (`find_room`, `lookup_person`).
  * Model flips between the study's two conditions via the
    `PAPER_REALTIME_MODEL` env var — no A/B loop, no launcher.
  * Dispatch: the worker registers under `PAPER_AGENT_NAME`;
    `dispatcher.py` (its own compose service) keeps one dispatch
    alive in the fixed `pepper-experiment` room, so `docker compose
    up` is the only manual step.

Run (worker, inside the compose):
    uv run python voice-agent/src/paper/agent_realtime.py start

Quick local test with laptop mic+speakers (no LiveKit room needed):
    uv run python voice-agent/src/paper/agent_realtime.py console
"""

from __future__ import annotations

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import asyncio  # noqa: E402
import json  # noqa: E402
import logging  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

from dotenv import load_dotenv  # noqa: E402

# ── Path / env setup ─────────────────────────────────────────────────
THIS_DIR = Path(__file__).resolve().parent            # voice-agent/src/paper
VOICE_AGENT_DIR = THIS_DIR.parent.parent              # voice-agent/
if str(VOICE_AGENT_DIR) not in sys.path:
    sys.path.insert(0, str(VOICE_AGENT_DIR))

ROOT_ENV_PATH = VOICE_AGENT_DIR.parent / ".env"
if ROOT_ENV_PATH.exists():
    load_dotenv(dotenv_path=ROOT_ENV_PATH, override=False)

os.environ.setdefault("LIVEKIT_URL", "ws://127.0.0.1:7880")

from livekit.agents import (  # noqa: E402
    Agent,
    AgentSession,
    JobContext,
    WorkerOptions,
    cli,
    metrics,
    room_io,
)
from livekit.plugins import openai  # noqa: E402
from openai.types.realtime import AudioTranscription  # noqa: E402

from src.paper.prompt import SYSTEM_PROMPT  # noqa: E402
from src.paper.tools.find_room import find_room  # noqa: E402
from src.paper.tools.lookup_person import lookup_person  # noqa: E402
from src.experiment.tools.utils._events import (  # noqa: E402
    set_tool_event_listener,
    set_tool_result_listener,
)
from src.live.bridge_client import post_head_lock  # noqa: E402

PAPER_TOOLS = [find_room, lookup_person]


# ── Logging ──────────────────────────────────────────────────────────
logger = logging.getLogger("paper-realtime")
logging.getLogger("livekit.agents").setLevel(logging.INFO)
logging.getLogger("livekit.plugins.openai").setLevel(logging.WARNING)
logging.getLogger("openai").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)


# ── Tunables ─────────────────────────────────────────────────────────
AGENT_NAME = os.environ.get("PAPER_AGENT_NAME", "pepper-paper-realtime")
# The study's two conditions: gpt-realtime-2.1 vs gpt-realtime-2.1-mini.
REALTIME_MODEL = os.environ.get("PAPER_REALTIME_MODEL", "gpt-realtime-2.1")
REALTIME_VOICE = os.environ.get("PAPER_REALTIME_VOICE", "marin")
LANG = os.environ.get("AGENT_LANG", "cs").strip().lower() or "cs"
# Input transcription is for logs/tablet only — the realtime model
# understands the audio directly; this side-channel gives us readable
# Czech transcripts of what the user said.
TRANSCRIPTION_MODEL = os.environ.get(
    "PAPER_TRANSCRIPTION_MODEL", "gpt-4o-mini-transcribe",
)

# Identities / topics — must match orchestrator, audio-bridge, tablet.
USER_IDENTITY = os.environ.get("USER_IDENTITY", "user")
# `console` subcommand = local laptop mic/speaker test: there is no
# `user` participant to wait for and no room plumbing to pin to it.
IS_CONSOLE = "console" in sys.argv[1:]
TOPIC_EXPERIMENT = "pepper.experiment"
TOPIC_TEXT = "pepper.text"
TOPIC_CONTROL = "pepper.control"
TOPIC_STATE = "pepper.state"
TOPIC_SPEECH = "pepper.speech"


print(
    f"[paper-realtime] booted, waiting for dispatch "
    f"(livekit_url={os.environ.get('LIVEKIT_URL', 'unset')} "
    f"agent_name={AGENT_NAME} model={REALTIME_MODEL} "
    f"voice={REALTIME_VOICE} lang={LANG})",
    flush=True,
)


def _format_tool_summary(tools_list) -> str:
    """One line per tool: `- name: first-line of description`."""
    lines: list[str] = []
    for t in tools_list:
        info = getattr(t, "info", None)
        name = getattr(info, "name", None) or getattr(t, "__name__", "?")
        desc = (getattr(info, "description", None)
                or (getattr(t, "__doc__", "") or "").strip())
        first = desc.splitlines()[0].strip() if desc else "(no doc)"
        lines.append(f"  - {name}: {first}")
    return "\n".join(lines)


# ── Entrypoint ───────────────────────────────────────────────────────
async def entrypoint(ctx: JobContext) -> None:
    t_entry = time.monotonic()

    if not os.environ.get("OPENAI_API_KEY"):
        print(
            "[paper-realtime] ERROR: OPENAI_API_KEY not set "
            "(check .env at project root).",
            file=sys.stderr, flush=True,
        )
        ctx.shutdown(reason="missing_openai_api_key")
        return

    main_loop = asyncio.get_running_loop()

    # ── Event publisher (pepper.experiment JSONL-style stream) ───────
    # Same schema as the cascade workers, so any future recorder /
    # study-logging code works unchanged.
    def _publish_data(payload_dict: dict, topic: str) -> None:
        try:
            payload = json.dumps(
                payload_dict, ensure_ascii=False, default=str,
            ).encode("utf-8")
        except Exception as exc:
            logger.debug("publish encode failed topic=%s err=%s", topic, exc)
            return
        try:
            asyncio.run_coroutine_threadsafe(
                ctx.room.local_participant.publish_data(payload, topic=topic),
                main_loop,
            )
        except Exception as exc:
            logger.debug("publish submit failed topic=%s err=%s", topic, exc)

    def _publish_event(event: dict) -> None:
        _publish_data(event, TOPIC_EXPERIMENT)

    def _publish_agent_state(state: str) -> None:
        """`pepper.state` is what tablet_server reads for its pill."""
        _publish_data({"agent_state": state}, TOPIC_STATE)

    # ── Per-session turn counter ─────────────────────────────────────
    turn_state: dict = {"turn_id": 0}

    # ── Tool event listeners → experiment stream ─────────────────────
    def _on_tool_event(name: str, args: dict) -> None:
        try:
            args_preview = json.dumps(args, ensure_ascii=False, default=str)
        except Exception:
            args_preview = repr(args)
        print(f"  [TOOL] {name}({args_preview})", flush=True)
        _publish_event({
            "kind": "tool_call",
            "name": name,
            "args": args,
            "turn_id": turn_state["turn_id"],
            "ts": time.time(),
        })

    def _on_tool_result(name: str, result) -> None:
        try:
            preview = json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            preview = repr(result)
        if len(preview) > 300:
            preview = preview[:300] + "…"
        print(f"  [TOOL-RESULT] {name} -> {preview}", flush=True)
        _publish_event({
            "kind": "tool_result",
            "name": name,
            "result": result,
            "turn_id": turn_state["turn_id"],
            "ts": time.time(),
        })
        err = result.get("error") if isinstance(result, dict) else None
        if err:
            _publish_event({
                "kind": "tool_failed",
                "name": name,
                "error": err,
                "turn_id": turn_state["turn_id"],
                "ts": time.time(),
            })

    set_tool_event_listener(_on_tool_event)
    set_tool_result_listener(_on_tool_result)

    # ── AgentSession around the realtime model ───────────────────────
    # No VAD/STT/TTS plugins: the realtime model is speech-in/speech-out
    # with server-side turn detection. allow_interruptions stays at the
    # default (True) — AEC3 in user-client keeps Pepper's own voice out
    # of the mic, so barge-in only trips on real user speech.
    session = AgentSession(
        llm=openai.realtime.RealtimeModel(
            model=REALTIME_MODEL,
            voice=REALTIME_VOICE,
            input_audio_transcription=AudioTranscription(
                model=TRANSCRIPTION_MODEL,
                language=LANG,
            ),
        ),
    )

    # ── Track typed vs spoken user turns ─────────────────────────────
    _typed_inputs: set[str] = set()

    session_start_payload = {
        "kind": "session_start",
        "room": ctx.room.name,
        "mode": "paper-realtime",
        "ts": time.time(),
        "realtime_model": REALTIME_MODEL,
        "voice": REALTIME_VOICE,
        "transcription_model": TRANSCRIPTION_MODEL,
        "lang": LANG,
    }

    # ── Usage / cost observability ───────────────────────────────────
    usage_collector = metrics.UsageCollector()

    @session.on("metrics_collected")
    def _on_metrics(ev):
        metrics.log_metrics(ev.metrics)
        usage_collector.collect(ev.metrics)

    async def _log_usage_summary():
        try:
            summary = usage_collector.get_summary()
            print(f"[paper-realtime] usage_summary={summary}", flush=True)
        except Exception as exc:
            logger.debug("usage_summary_failed err=%s", exc)

    ctx.add_shutdown_callback(_log_usage_summary)

    # ── Head lock: freeze autonomous head scanning on first speech ───
    head_locked = {"v": False}

    @session.on("user_state_changed")
    def _on_user_state(event) -> None:
        old_state = str(getattr(event, "old_state", "?") or "?")
        new_state = str(getattr(event, "new_state", "?") or "?")
        print(f"  [VAD] user_state {old_state}->{new_state}", flush=True)
        if old_state != "speaking" and new_state == "speaking":
            if not head_locked["v"]:
                head_locked["v"] = True
                asyncio.create_task(asyncio.to_thread(post_head_lock, True))
        elif old_state == "speaking" and new_state != "speaking":
            turn_state["turn_id"] = int(turn_state.get("turn_id", 0)) + 1
            _publish_event({
                "kind": "vad_user_speech_end",
                "turn_id": turn_state["turn_id"],
                "prev_state": old_state,
                "new_state": new_state,
                "ts": time.time(),
            })

    @session.on("user_input_transcribed")
    def _on_user_speech(event) -> None:
        text = (getattr(event, "text", "") or "").strip()
        is_final = bool(getattr(event, "is_final", True))
        tag = "STT" if is_final else "STT-partial"
        if not text:
            return
        print(f"  [{tag}] {text!r}", flush=True)
        if is_final:
            _publish_event({
                "kind": "asr_result",
                "text": text,
                "is_final": True,
                "turn_id": turn_state["turn_id"],
                "ts": time.time(),
            })

    @session.on("agent_state_changed")
    def _on_agent_state(event) -> None:
        old_state = str(getattr(event, "old_state", "?") or "?")
        new_state = str(getattr(event, "new_state", None)
                        or getattr(event, "state", "?"))
        print(f"  [STATE] agent_state {old_state}->{new_state} "
              f"(tablet pinned to listening)", flush=True)
        # End of an utterance: ask the audio bridge to drain its paplay
        # buffer. The cascade fired this from its tts_node; the realtime
        # path has no TTS seam, so the speaking→* transition is the
        # utterance-end signal.
        if old_state == "speaking" and new_state != "speaking":
            _publish_data({"kind": "request_drain", "ts": time.time()},
                          TOPIC_SPEECH)
        # The mic is hot for the whole session, so the tablet pill is
        # pinned to "listening" (same rationale as the cascade workers).
        _publish_event({
            "kind": "tablet_state",
            "state": "listening",
            "turn_id": turn_state["turn_id"],
            "ts": time.time(),
        })
        _publish_agent_state("listening")

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
            _publish_event({
                "kind": "user_turn",
                "text": text,
                "input": input_kind,
                "turn_id": turn_state["turn_id"],
                "ts": time.time(),
            })
        elif role == "assistant":
            print(f"  [ASSISTANT] {text!r}", flush=True)
            _publish_event({
                "kind": "agent_speech",
                "text": text,
                "turn_id": turn_state["turn_id"],
                "ts": time.time(),
            })

    # ── Room data handler: typed input + speaker acks + shutdown ────
    shutdown_event = asyncio.Event()
    shutdown_mode = {"mode": "abort"}

    @ctx.room.on("data_received")
    def _on_data(packet):
        topic = str(getattr(packet, "topic", "") or "")
        if topic == TOPIC_TEXT:
            try:
                msg = json.loads(getattr(packet, "data", b"") or b"")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return
            text = str(msg.get("text", "") or "").strip()
            if not text:
                return
            sender = str(getattr(getattr(packet, "participant", None),
                                 "identity", "") or "?")
            logger.info("[pepper.text] from=%s text=%s", sender, text[:120])
            _typed_inputs.add(text)
            turn_state["turn_id"] = int(turn_state.get("turn_id", 0)) + 1
            _publish_event({
                "kind": "typed_input",
                "text": text,
                "by": sender or "operator",
                "turn_id": turn_state["turn_id"],
                "ts": time.time(),
            })
            try:
                session.interrupt()
                session.generate_reply(user_input=text)
            except Exception as exc:
                logger.warning("pepper.text dispatch failed err=%s", exc)
                _publish_event({
                    "kind": "error",
                    "component": "typed_input_dispatch",
                    "message": repr(exc),
                    "turn_id": turn_state["turn_id"],
                    "recovered": True,
                    "ts": time.time(),
                })
            return

        if topic == TOPIC_SPEECH:
            # Audio bridge acks: re-emit on TOPIC_EXPERIMENT so any
            # recorder sees one unified stream.
            try:
                msg = json.loads(getattr(packet, "data", b"") or b"")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return
            kind = str(msg.get("kind", "")).lower()
            if kind == "speaker_first_sound":
                _publish_event({
                    "kind": "pepper_first_sound",
                    "turn_id": turn_state["turn_id"],
                    "ts": time.time(),
                    "first_chunk_bytes": msg.get("first_chunk_bytes"),
                })
            elif kind == "speaker_drained":
                _publish_event({
                    "kind": "pepper_drain_ack",
                    "turn_id": turn_state["turn_id"],
                    "reason": msg.get("reason"),
                    "ts": time.time(),
                })
            return

        if topic == TOPIC_CONTROL:
            try:
                msg = json.loads(getattr(packet, "data", b"") or b"")
            except Exception:
                return
            if str(msg.get("cmd", "")).strip().lower() == "shutdown":
                logger.info("shutdown signal received via pepper.control")
                shutdown_mode["mode"] = "drain"
                shutdown_event.set()

    # Belt-and-braces session cleanup on any exit path.
    async def _ensure_session_closed():
        try:
            await session.aclose()
        except Exception as exc:
            logger.debug("shutdown_cb: session.aclose failed: %s", exc)

    ctx.add_shutdown_callback(_ensure_session_closed)

    # ── Connect + wait for the user participant ─────────────────────
    await ctx.connect()
    if not IS_CONSOLE:
        user_participant = await ctx.wait_for_participant(identity=USER_IDENTITY)
        logger.info(
            "user_joined identity=%s",
            getattr(user_participant, "identity", USER_IDENTITY),
        )

    # ── Start the session ────────────────────────────────────────────
    # No auto-greet: Pepper waits until the user first speaks; the
    # prompt tells the model to greet + answer in one breath on the
    # first turn. close_on_disconnect=True: if user-client drops, the
    # session (and job) end — dispatcher.py then re-dispatches a fresh
    # agent, so the stack self-heals.
    agent = Agent(instructions=SYSTEM_PROMPT, tools=PAPER_TOOLS)
    start_kwargs: dict = {}
    if not IS_CONSOLE:
        start_kwargs["room_options"] = room_io.RoomOptions(
            participant_identity=USER_IDENTITY,
            close_on_disconnect=True,
        )
    await session.start(agent=agent, room=ctx.room, **start_kwargs)
    print(
        f"[paper-realtime] session_started model={REALTIME_MODEL} "
        f"total_setup={(time.monotonic() - t_entry) * 1000.0:.0f}ms "
        f"room={ctx.room.name}",
        flush=True,
    )
    # One-shot context dump: confirm from stdout exactly which prompt +
    # tools this session runs with.
    print(
        f"[paper-realtime] === REALTIME CONTEXT DUMP ===\n"
        f"--- MODEL {REALTIME_MODEL} voice={REALTIME_VOICE} lang={LANG} ---\n"
        f"--- SYSTEM_PROMPT ({len(SYSTEM_PROMPT)} chars) ---\n"
        f"{SYSTEM_PROMPT}\n"
        f"--- TOOLS ({len(PAPER_TOOLS)}) ---\n"
        f"{_format_tool_summary(PAPER_TOOLS)}\n"
        f"=== END DUMP ===",
        flush=True,
    )
    _publish_event(session_start_payload)
    _publish_event({
        "kind": "tablet_state",
        "state": "listening",
        "turn_id": turn_state["turn_id"],
        "ts": time.time(),
    })
    _publish_agent_state("listening")
    # Monotonic anchor for any future recorder's ts_mono derivation.
    _publish_event({
        "kind": "mono_anchor",
        "ts": time.time(),
        "worker_mono": time.monotonic(),
        "worker_wall": time.time(),
    })

    # ── Wait for shutdown ────────────────────────────────────────────
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

    _publish_event({"kind": "session_end", "ts": time.time()})

    # Release the head BEFORE draining so awareness resumes during the
    # final-sentence playback. post_head_lock never raises.
    await asyncio.to_thread(post_head_lock, False)

    try:
        if shutdown_mode["mode"] == "drain" and not session_closed.is_set():
            session.shutdown(drain=True)
            try:
                await asyncio.wait_for(session_closed.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.info("drain timed out — falling back to aclose")
        await session.aclose()
    except Exception as exc:
        logger.debug("session teardown failed: %s", exc)

    ctx.shutdown(reason="paper_realtime_done")
    logger.info("paper realtime session ended")


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
