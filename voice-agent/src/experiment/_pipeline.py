"""Shared worker runner for the experiment chained-pipeline modes.

Both Mode A (local: silero VAD + FasterWhisper STT + vLLM Llama + Piper
TTS, [agent.py](agent.py)) and Mode C (cloud: silero VAD +
gpt-4o-transcribe + gpt-4o-mini + gpt-4o-mini-tts,
[agent_4o.py](agent_4o.py)) are chained STT->LLM->TTS pipelines that
share ~80 % of their wiring. Before this module they each carried a
near-identical copy of:

  * dispatch-metadata parsing (`experiment_variant`, `experiment_student_id`)
  * waiting for the `user` participant
  * publish helpers for `pepper.experiment` / `pepper.state` / `pepper.speech`
  * tablet-clear / tool-event listener wiring
  * `AgentSession` construction + greeting-instruction injection
  * `pepper.text` typed-input path + `pepper.control` shutdown path
  * `agent_state_changed` + `conversation_item_added` event handlers
  * `session.start` + shutdown lifecycle

That drifted: Mode A still allowed interruptions
(`min_interruption_duration=3.0`), Mode C didn't (`allow_interruptions=False`).
The drift is the root cause of the "VAD interrupts her too much" issue.

This module centralises all of that. Each mode now provides a tiny
`stack_builder(lang)` callable that returns the `{vad, stt, llm, tts}`
dict plus a `mode_label` string, and calls `run_pipeline(...)`. VAD
stays active for end-of-user-turn detection in both modes; interruptions
are disabled at the session level so Pepper cannot be cut off by user
speech detected by VAD. Typed input on `pepper.text` is still a
deliberate barge-in (it calls `session.interrupt()` explicitly).

The pipeline also wires the real speaker-EOS signal: it subscribes to
the `pepper.speech` topic published by `audio_bridge.py` once the
on-robot bridge has ACKed that ALAudioDevice's queue drained, and
exposes a `SessionRuntime` so `send_message_to_user` can wait on it.
When no audio-bridge participant is in the room (laptop-only dev), the
runtime's `audio_bridge_present=False` short-circuits the wait so
turns end at `wait_for_playout()` instead of timing out after 5 s.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Any, Awaitable, Callable

from livekit.agents import (
    Agent,
    AgentSession,
    AutoSubscribe,
    JobContext,
    llm,
    room_io,
)
from livekit.agents.voice.generation import update_instructions

from src.live.bridge_client import post_head_lock
from tools.utils._session_runtime import (
    SessionRuntime,
    clear_session_runtime,
    set_session_runtime,
)

logger = logging.getLogger("experiment-pipeline")

# Topics published by the chained pipeline. Recorder reads pepper.experiment,
# tablet reads pepper.state, audio_bridge publishes speaker_drained on
# pepper.speech (Change 4).
TOPIC_EXPERIMENT = "pepper.experiment"
TOPIC_STATE = "pepper.state"
TOPIC_SPEECH = "pepper.speech"
TOPIC_TEXT = "pepper.text"
TOPIC_CONTROL = "pepper.control"

USER_IDENTITY = os.environ.get("USER_IDENTITY", "user")
RECORDER_IDENTITY = os.environ.get("RECORDER_IDENTITY", "experimenter-recorder")
AUDIO_BRIDGE_IDENTITY = os.environ.get("AUDIO_BRIDGE_IDENTITY", "listener-python")


def _truthy(env_val: str | None, default: bool) -> bool:
    if env_val is None:
        return default
    return env_val.strip().lower() in ("1", "true", "yes", "on")


class _ExperimentAgent(Agent):
    """Letta-style chained agent.

    Identical for both modes: it carries the system prompt + tool set,
    and on the first user turn it merges `GREETING_INSTRUCTIONS` into
    the system slot once via `update_instructions` so the very first
    reply is a greeting. Subsequent turns drop back to the plain
    system prompt — keeps the LLM context tidy.
    """

    def __init__(self, system_prompt: str, greeting_instructions: str,
                 tools_list) -> None:
        super().__init__(instructions=system_prompt, tools=tools_list)
        self._system_prompt = system_prompt
        self._greeting_instructions = greeting_instructions
        self._greeting_done = False

    async def llm_node(self, chat_ctx, tools, model_settings):
        user_msgs = sum(
            1
            for item in chat_ctx.items
            if getattr(item, "type", None) == "message"
            and getattr(item, "role", None) == "user"
        )
        if user_msgs == 1 and not self._greeting_done:
            merged = f"{self._system_prompt}\n\n{self._greeting_instructions}"
            update_instructions(chat_ctx, instructions=merged, add_if_missing=True)
            self._greeting_done = True
        return Agent.default.llm_node(self, chat_ctx, tools, model_settings)


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
    """Block until the `user` identity joins the room.

    Logs every other participant we skip past so a misconfigured room
    (e.g. only the recorder joined) is visible in tmux.
    """
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


async def run_pipeline(
    ctx: JobContext,
    *,
    mode_label: str,
    stack_builder: Callable[..., Awaitable[dict] | dict],
    system_prompt: str,
    greeting_instructions: str,
    tools_module,
    extra_session_kwargs: dict | None = None,
    on_clear_tablet: Callable[[], Awaitable[None]] | None = None,
    boot_extras: dict | None = None,
) -> None:
    """Run one chained STT->LLM->TTS session.

    Args:
        mode_label: short tag published on pepper.state (`local`, `4o-chained`,
            etc.). The tablet uses this to render the mode pill.
        stack_builder: returns `{"vad", "stt", "llm", "tts", "labels"}`. May be
            sync or async; `labels` is a dict of human-readable model names
            for the boot banner.
        system_prompt, greeting_instructions: passed straight to
            `_ExperimentAgent`. Loaded by each mode's slim entrypoint so
            the same module can ship multiple language variants without
            forking this runner.
        tools_module: provides `LIVEKIT_TOOLS_TOOLONLY` + the two
            listener-setter functions. Both modes use the same `tools` /
            `tools_cs` module — only the language differs.
        extra_session_kwargs: passed through to `AgentSession` for
            stack-specific tuning (max_tool_steps overrides, etc.).
            `allow_interruptions=False` is enforced here regardless of
            what callers pass.
        on_clear_tablet: optional async callable invoked on user-turn
            start; used to wipe display_info cards (Mode A only — Mode
            C doesn't have the display_info tool wired).
        boot_extras: extra fields merged into the `session_start` event
            published to the recorder.
    """
    t_entry = time.monotonic()
    runtime = SessionRuntime(
        eos_mode=os.environ.get("EOS_MODE", "auto").strip().lower() or "auto",
        drain_timeout=float(os.environ.get("DRAIN_TIMEOUT", "5.0")),
        split_sentences=_truthy(os.environ.get("SPLIT_SENTENCES"), default=True),
    )
    set_session_runtime(runtime)

    meta = _parse_dispatch_metadata(ctx)
    variant = str(meta.get("experiment_variant") or "").strip()
    student_id = str(meta.get("experiment_student_id") or "").strip()

    # Resolve the stack now so the boot banner has the full model line
    # — useful when grepping logs for "which models did this session
    # actually run with?".
    stack_obj = stack_builder()
    if asyncio.iscoroutine(stack_obj):
        stack = await stack_obj
    else:
        stack = stack_obj
    labels = dict(stack.get("labels") or {})

    print(
        f"[PIPELINE] mode={mode_label} variant={variant!r} student_id={student_id!r} "
        f"room={ctx.room.name} "
        f"interruptions=disabled "
        f"eos_mode={runtime.eos_mode} drain_timeout={runtime.drain_timeout:.1f}s "
        f"split_sentences={runtime.split_sentences} "
        f"stack={labels}",
        flush=True,
    )

    await ctx.connect(auto_subscribe=AutoSubscribe.SUBSCRIBE_ALL)
    participant = await _wait_for_user_participant(ctx)
    logger.info("user_joined identity=%s", getattr(participant, "identity", ""))

    main_loop = asyncio.get_running_loop()
    runtime.ts0 = time.monotonic()

    def _ts() -> str:
        return runtime.ts()

    async def _publish_topic(topic: str, payload: dict) -> None:
        try:
            data = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
            await ctx.room.local_participant.publish_data(data, topic=topic)
        except Exception as exc:
            logger.debug("publish_failed topic=%s err=%s", topic, exc)

    async def _request_drain() -> None:
        """Ask audio_bridge to round-trip a drain ACK with the robot.

        Published once per `send_message_to_user` after the final
        `wait_for_playout()`. audio_bridge listens on `pepper.speech`,
        forwards a DRAIN_REQ control frame over TCP, and publishes
        speaker_drained once the robot replies.
        """
        print(f"  {_ts()} [SPEECH] drain_req_publish", flush=True)
        await _publish_topic(TOPIC_SPEECH, {"kind": "request_drain", "ts": time.time()})

    runtime.request_drain = _request_drain

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

    async def _clear_tablet_default() -> None:
        return None

    _clear_tablet = on_clear_tablet or _clear_tablet_default

    def _clear_tablet_sync() -> None:
        try:
            asyncio.run_coroutine_threadsafe(_clear_tablet(), main_loop)
        except Exception as exc:
            logger.debug("tablet_clear_schedule_failed err=%s", exc)

    # ── Tool event forwarding to the experiment recorder ────────────
    def _on_tool_event(name: str, args: dict) -> None:
        try:
            args_preview = json.dumps(args, ensure_ascii=False, default=str)
        except Exception:
            args_preview = repr(args)
        print(f"  {_ts()} [TOOL] {name}({args_preview})", flush=True)
        _publish_sync({"kind": "tool_call", "name": name, "args": args, "ts": time.time()})

    def _on_tool_result(name: str, result: Any) -> None:
        try:
            preview = json.dumps(result, ensure_ascii=False, default=str)
        except Exception:
            preview = repr(result)
        if len(preview) > 300:
            preview = preview[:300] + "…"
        print(f"  {_ts()} [TOOL-RESULT] {name} -> {preview}", flush=True)
        _publish_sync({"kind": "tool_result", "name": name, "result": result, "ts": time.time()})

    tools_module.set_tool_event_listener(_on_tool_event)
    tools_module.set_tool_result_listener(_on_tool_result)

    # ── AgentSession ────────────────────────────────────────────────
    # `allow_interruptions=False` is the entire VAD-interruption fix.
    # VAD remains active for endpointing (turn segmentation feeding the
    # non-streaming STT plugins), but VAD-detected user speech does NOT
    # cut off the agent's TTS playout. Mic audio while she's speaking
    # is silently discarded (`discard_audio_if_uninterruptible=True`,
    # the default in 1.3.12). Typed input on `pepper.text` is still a
    # deliberate barge-in because it calls `session.interrupt()`
    # explicitly below.
    session_kwargs = {
        "vad": stack["vad"],
        "stt": stack["stt"],
        "llm": stack["llm"],
        "tts": stack["tts"],
        "max_tool_steps": 4,
    }
    if extra_session_kwargs:
        session_kwargs.update(extra_session_kwargs)
    session_kwargs["allow_interruptions"] = False  # enforced
    session = AgentSession(**session_kwargs)

    # Track typed inputs so conversation_item_added can tag the resulting
    # user_turn as input="typed" vs input="speech".
    _typed_inputs: set[str] = set()

    # ── Boot frame for the recorder ─────────────────────────────────
    session_start_payload = {
        "kind": "session_start",
        "room": ctx.room.name,
        "variant": variant,
        "student_id": student_id,
        "mode": mode_label,
        "ts": time.time(),
    }
    session_start_payload.update(labels)
    if boot_extras:
        session_start_payload.update(boot_extras)
    _publish_sync(session_start_payload)

    _publish_sync_topic(TOPIC_STATE, {
        "agent_mode": mode_label,
        "agent_language": os.environ.get("AGENT_LANG", "en").strip().lower() or "en",
        # Ensure user-client starts each session with the mic live. A
        # crashed previous session could have left mic_muted=True
        # latched in user_client; this re-broadcasts the clean state.
        "mic_muted": False,
    })

    # ── audio-bridge presence tracking (Change 6) ───────────────────
    def _refresh_audio_bridge_presence() -> None:
        present = any(
            str(getattr(p, "identity", "") or "") == AUDIO_BRIDGE_IDENTITY
            for p in (getattr(ctx.room, "remote_participants", {}) or {}).values()
        )
        if present != runtime.audio_bridge_present:
            runtime.audio_bridge_present = present
            print(
                f"  {_ts()} [PIPELINE] audio_bridge_present={present}",
                flush=True,
            )

    _refresh_audio_bridge_presence()

    @ctx.room.on("participant_connected")
    def _on_participant_connected(_p):
        _refresh_audio_bridge_presence()

    # ── STAGE A: track-subscription visibility ──────────────────────
    @ctx.room.on("track_subscribed")
    def _on_track_subscribed(track, publication, p):
        kind = getattr(track, "kind", "?")
        identity = getattr(p, "identity", "?")
        sid = getattr(publication, "sid", "?")
        print(
            f"  {_ts()} [STAGE A] track_subscribed identity={identity} "
            f"kind={kind} sid={sid}",
            flush=True,
        )

    @ctx.room.on("track_unsubscribed")
    def _on_track_unsubscribed(_track, publication, p):
        identity = getattr(p, "identity", "?")
        sid = getattr(publication, "sid", "?")
        print(
            f"  {_ts()} [STAGE A] track_unsubscribed identity={identity} sid={sid}",
            flush=True,
        )

    # ── VAD / STT / agent_state visibility (STAGE B/C/D) ────────────
    @session.on("user_started_speaking")
    def _on_user_started(_event) -> None:
        print(f"  {_ts()} [STAGE B] VAD started speaking", flush=True)
        _clear_tablet_sync()

    @session.on("user_stopped_speaking")
    def _on_user_stopped(_event) -> None:
        print(f"  {_ts()} [STAGE B] VAD stopped speaking", flush=True)

    # Track mic-mute state so we only publish on changes (idempotency
    # protects user_client from log spam and serial publish_data churn).
    mic_state = {"muted": False}
    # Generation counter — when the worker schedules a delayed unmute
    # and a new "mute" event arrives before that timer fires, we bump
    # the generation so the old unmute task no-ops on wake. Prevents a
    # late grace-window unmute from clobbering a fresh "speaking" mute.
    mic_unmute_gen = {"value": 0}
    # Extra padding after `speaker_drained` (real EOS at the bridge)
    # so whatever audio NAOqi's internal ALAudioDevice buffer is still
    # holding finishes playing before the mic re-opens. With the
    # robot-bridge real-time pacing (sendRemoteBufferToOutput called
    # once per batch_duration), NAOqi never accumulates more than ~one
    # batch (~50 ms at 16 kHz / 800-frame batches), so 250 ms is plenty
    # for jitter. Bump higher if Pepper still picks up her own tail.
    MIC_UNMUTE_GRACE_MS = int(os.environ.get("MIC_UNMUTE_GRACE_MS", "250"))

    def _set_mic_muted(muted: bool, reason: str) -> None:
        if mic_state["muted"] == muted:
            return
        mic_state["muted"] = muted
        print(f"  {_ts()} [MIC] mic_muted={muted} reason={reason}", flush=True)
        _publish_sync_topic(TOPIC_STATE, {"mic_muted": muted})

    async def _unmute_after_grace(gen: int, reason: str) -> None:
        if MIC_UNMUTE_GRACE_MS > 0:
            await asyncio.sleep(MIC_UNMUTE_GRACE_MS / 1000.0)
        # If a newer mute event has fired since this task was scheduled
        # (e.g. Pepper started a back-to-back utterance), bail out.
        if gen != mic_unmute_gen["value"]:
            print(
                f"  {_ts()} [MIC] grace_unmute_superseded gen={gen} latest={mic_unmute_gen['value']}",
                flush=True,
            )
            return
        _set_mic_muted(False, reason=reason)

    def _schedule_unmute(reason: str) -> None:
        gen = mic_unmute_gen["value"]
        asyncio.run_coroutine_threadsafe(
            _unmute_after_grace(gen, f"{reason}+grace{MIC_UNMUTE_GRACE_MS}ms"),
            main_loop,
        )

    def _mark_mute(reason: str) -> None:
        # Invalidate any pending grace-window unmute, then mute.
        mic_unmute_gen["value"] += 1
        _set_mic_muted(True, reason=reason)

    @session.on("agent_state_changed")
    def _on_agent_state(event) -> None:
        new_state = getattr(event, "new_state", None) or getattr(event, "state", "?")
        new_state_str = str(new_state)
        print(f"  {_ts()} [STAGE D] agent_state={new_state_str}", flush=True)
        _publish_sync_topic(TOPIC_STATE, {"agent_state": new_state_str})
        # Mute the user mic while Pepper speaks so her own audio leaking
        # back through the mic doesn't get treated as a new user turn.
        # Unmute is driven by the real speaker-drained signal below;
        # the listening-state fallback covers the laptop-dev case where
        # there's no audio-bridge to send a drain ACK.
        if new_state_str == "speaking":
            _mark_mute("agent_state=speaking")
        elif new_state_str == "listening" and not runtime.audio_bridge_present:
            # Laptop dev: no audio-bridge means no speaker_drained ACK,
            # so we have to fall back to the agent_state transition.
            # Still pad with grace so the LiveKit-emitter drain isn't
            # mistaken for the actual speaker drain (the speaker is
            # whatever local headphones — playback ~immediate but TTS
            # might still be flushing the last chunk).
            _schedule_unmute("listening_no_audio_bridge")

    @session.on("user_input_transcribed")
    def _on_user_speech(event) -> None:
        text = (getattr(event, "text", "") or "").strip()
        if not text:
            return
        is_final = bool(getattr(event, "is_final", True))
        tag = "STT" if is_final else "STT-partial"
        print(f"  [{tag}] {text!r}", flush=True)

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
                "kind": "user_turn", "text": text, "input": input_kind, "ts": time.time(),
            })
            logger.info("user_turn input=%s text=%s", input_kind, text)
        # Assistant items skipped — send_message_to_user's [TOOL]
        # event is the canonical record of Pepper's speech.

    shutdown_event = asyncio.Event()

    @ctx.room.on("data_received")
    def _on_data(packet):
        topic = str(getattr(packet, "topic", "") or "")

        if topic == TOPIC_SPEECH:
            # Real speaker-drained ACK from audio_bridge.py (Change 4).
            try:
                msg = json.loads(getattr(packet, "data", b"") or b"")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return
            if str(msg.get("kind", "")).lower() == "speaker_drained":
                reason = msg.get("reason", "robot_ack")
                print(
                    f"  {_ts()} [SPEECH] speaker_drained received reason={reason}",
                    flush=True,
                )
                runtime.speaker_drained_evt.set()
                # Real EOS at the bridge — but NAOqi's internal
                # ALAudioDevice buffer still has hundreds of ms of audio
                # left to play. Schedule the unmute after MIC_UNMUTE_GRACE_MS
                # so the mic doesn't pick up the speaker's tail audio
                # (which is exactly the "she heard her own last words"
                # symptom). If Pepper starts a new utterance during the
                # grace window, the new "speaking" event bumps the
                # generation and this delayed unmute will no-op.
                _schedule_unmute(f"speaker_drained:{reason}")
            return

        if topic == TOPIC_TEXT:
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

        if topic == TOPIC_CONTROL:
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
        _refresh_audio_bridge_presence()

    agent = _ExperimentAgent(
        system_prompt=system_prompt,
        greeting_instructions=greeting_instructions,
        tools_list=tools_module.LIVEKIT_TOOLS_TOOLONLY,
    )
    await session.start(
        agent=agent,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            participant_identity=str(getattr(participant, "identity", "") or USER_IDENTITY),
        ),
    )
    logger.info("session_started total_setup=%.2fs", time.monotonic() - t_entry)

    # Park the head + pause autonomous head scanning while the
    # participant is interacting. The `finally` below guarantees we
    # release on every normal exit path (graceful close, recorder
    # disconnect, in-process crash) so between sessions Pepper goes
    # back to looking around on her own.
    await asyncio.to_thread(post_head_lock, True)

    try:
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
    finally:
        await asyncio.to_thread(post_head_lock, False)

    _publish_sync({"kind": "session_end", "ts": time.time()})

    try:
        await session.aclose()
    except Exception as exc:
        logger.debug("session.aclose failed: %s", exc)

    clear_session_runtime()
    ctx.shutdown(reason=f"experiment_{mode_label}_done")
    logger.info("experiment %s session ended", mode_label)
