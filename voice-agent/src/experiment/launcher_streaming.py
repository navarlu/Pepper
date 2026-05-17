#!/usr/bin/env python3
"""Launcher for the streaming-variant experiment workers.

Dispatches one of the two streaming workers into a LiveKit room, joins
itself as the recorder, and writes one JSONL line per event published
by the worker on `pepper.experiment`.

Variants (mirrors the production launcher's --variant A / B convention):
  * A → local stack  (agent_streaming.py: FasterWhisper + vLLM Llama
                      3.1 + Piper). Runs on woska.
  * B → cloud stack  (agent_4o_streaming.py: gpt-4o-mini-* chain).
                      Runs on woska or RPi.

Differences vs. the production launcher (`launcher.py`):

  * NO subscription to `pepper.state` and NO mic-mute orchestration.
    `user-client` keeps its mic open continuously — self-echo is the
    AEC's problem, not the launcher's.
  * NO `audio_bridge_present` / drain-handshake plumbing — the
    streaming workers don't use those callbacks.
  * Typed input on `pepper.text` is still supported as a deliberate
    barge-in (same as the production launcher).

Run:
    uv run python voice-agent/src/experiment/launcher_streaming.py \\
        --student 1 --variant A          # local
    uv run python voice-agent/src/experiment/launcher_streaming.py \\
        --student 1 --variant B          # 4o cloud
"""

from __future__ import annotations

import warnings
warnings.filterwarnings("ignore", category=DeprecationWarning)

import argparse
import asyncio
import datetime as dt
import json
import logging
import os
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

from dotenv import load_dotenv
from livekit import api, rtc

# Audio capture lives in the same package; import after sys.path
# manipulation in case the launcher is run with cwd ≠ project root.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from audio_capture import AudioCapture  # noqa: E402


# ── Paths + env ──────────────────────────────────────────────────────
THIS_DIR = Path(__file__).resolve().parent
VOICE_AGENT_DIR = THIS_DIR.parent.parent
ROOT_ENV_PATH = VOICE_AGENT_DIR.parent / ".env"
if ROOT_ENV_PATH.exists():
    load_dotenv(dotenv_path=ROOT_ENV_PATH, override=False)


# ── Tunables ─────────────────────────────────────────────────────────
# Variant → worker agent_name. Mirrors the production launcher's
# --variant A / B convention.
#   A → local stack (FasterWhisper + vLLM Llama 3.1 + Piper)
#   B → cloud stack (gpt-4o-mini-transcribe + gpt-4o-mini + gpt-4o-mini-tts)
# Both workers register their respective agent_name with LiveKit; this
# launcher just dispatches into the room by name.
VARIANTS = ("A", "B")
AGENT_NAME_BY_VARIANT = {
    "A": os.environ.get(
        "PEPPER_EXPERIMENT_LOCAL_STREAMING_AGENT_NAME",
        "pepper-experiment-local-streaming",
    ),
    "B": os.environ.get(
        "PEPPER_EXPERIMENT_STREAMING_AGENT_NAME",
        "pepper-experiment-streaming",
    ),
}
DEFAULT_ROOM_NAME = "pepper-experiment"
RECORDER_IDENTITY = "experimenter-recorder"
TOPIC_EXPERIMENT = "pepper.experiment"
TOPIC_CONTROL = "pepper.control"
TOPIC_TEXT = "pepper.text"
RESULTS_ROOT = THIS_DIR / "results" / "experiments"
AGENT_WAIT_TIMEOUT_SEC = 60.0


logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("livekit").setLevel(logging.WARNING)
logger = logging.getLogger("launcher-streaming")
logger.setLevel(logging.INFO)


# ── Args ─────────────────────────────────────────────────────────────
def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run one streaming-variant Pepper conversation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--student", required=True,
                   help="Student id (e.g. 1, 2, s12345). Used in the log filename.")
    p.add_argument("--variant", required=True, choices=VARIANTS,
                   help="A = local stack (Llama 3.1 8B + FasterWhisper + Piper), "
                        "B = cloud stack (gpt-4o-mini chain).")
    p.add_argument("--room", default=DEFAULT_ROOM_NAME,
                   help=f"LiveKit room. Default: {DEFAULT_ROOM_NAME!r}.")
    p.add_argument("--livekit-url",
                   default=os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880"))
    p.add_argument("--api-key", default=os.environ.get("LIVEKIT_API_KEY"))
    p.add_argument("--api-secret", default=os.environ.get("LIVEKIT_API_SECRET"))
    return p.parse_args()


def _make_session_dir(student_id: str, variant: str) -> Path:
    """Per-session directory under ``results/experiments/<date>/``.

    Layout (one tree per conversation):

        results/experiments/2026-05-17/
          student22_streamingA_174650/
            events.jsonl
            metrics.json
            audio/
              T22_turn1_user.wav
              T22_turn1_agent.wav
              …

    Keeping each session in its own folder makes mixed-day exports
    trivial (just `cp -r` one directory), keeps audio next to its
    JSONL, and avoids any "did THIS sidecar belong to THAT jsonl?"
    ambiguity when scanning a day with dozens of sessions.
    """
    now = dt.datetime.now()
    date_dir = RESULTS_ROOT / now.strftime("%Y-%m-%d")
    session_dir = date_dir / f"student{student_id}_streaming{variant}_{now:%H%M%S}"
    session_dir.mkdir(parents=True, exist_ok=True)
    return session_dir


def _format_conv_id(student_id: str) -> str:
    """Map student id (1, 2, …, s12345) to the experiment's `T07`-form
    conversation id. Mirrors `tools/end_conversation_streaming.py`
    so the QR + JSONL agree on a single label."""
    try:
        return f"T{int(student_id):02d}"
    except (TypeError, ValueError):
        # Non-numeric student id (e.g. "s12345") — fall through to a
        # stripped variant. Still unique within a day's experiments.
        return f"T{str(student_id).strip() or '??'}"


# ── Recorder ─────────────────────────────────────────────────────────
class Recorder:
    """Subscribes to `pepper.experiment` and writes JSONL.

    Envelope (one line per event):
        {
          "ts":       1731768612.347531,   # wall clock, time.time()
          "ts_mono":  12.4567,             # seconds since session-start
                                           # monotonic anchor (wall-clock-
                                           # derived; see below)
          "conv_id":  "T07",
          "variant":  "A" or "B",
          "event":    "<kind>",
          "data":     { …payload… }
        }

    The worker publishes raw `{kind, ts, ...payload}`; this class
    rewrites each incoming dict into the target envelope, computes
    `ts_mono` from a one-shot `mono_anchor` event emitted by the
    worker just after `session_start`, and stamps every line with
    the shared `conv_id` / `variant`.

    `recorded_at` is no longer written into the envelope but kept
    available via `_last_recorded_at` for debug log lines.
    """

    def __init__(self, log_path: Path, *, conv_id: str, variant: str) -> None:
        self.log_path = log_path
        self._fh = log_path.open("a", buffering=1)  # line-buffered
        self.conv_id = conv_id
        self.variant = variant
        self.event_counts: dict[str, int] = {}
        self.ready_event = asyncio.Event()
        self.session_end_event = asyncio.Event()
        # `mono_anchor` arrives once, right after session_start. Its
        # `ts` defines ts_mono=0 for the session; every subsequent
        # event's ts_mono is `ts - anchor_wall`. If we receive events
        # BEFORE the anchor (header, session_start) they get
        # ts_mono=0 retroactively pinned to their own ts.
        self._anchor_wall: float | None = None

    def _envelope(self, event_kind: str, ts: float, data: dict) -> dict:
        if self._anchor_wall is None:
            ts_mono = 0.0
        else:
            ts_mono = round(ts - self._anchor_wall, 6)
        return {
            "ts": ts,
            "ts_mono": ts_mono,
            "conv_id": self.conv_id,
            "variant": self.variant,
            "event": event_kind,
            "data": data,
        }

    def write(self, event: dict) -> None:
        """Accept a worker-side `{kind, ts, ...payload}` dict (or a
        launcher-synthesised event with the same shape), rewrite it
        into the envelope, write one JSON line."""
        kind = str(event.get("kind", "?"))
        ts = float(event.get("ts") or time.time())

        # `mono_anchor` is a meta-event — capture the worker's wall
        # clock at "now" so subsequent ts_mono can be derived.
        if kind == "mono_anchor" and self._anchor_wall is None:
            anchor_wall = event.get("worker_wall")
            if isinstance(anchor_wall, (int, float)):
                self._anchor_wall = float(anchor_wall)
            else:
                self._anchor_wall = ts

        # Strip envelope-meta keys out of the payload.
        data = {
            k: v for k, v in event.items()
            if k not in {"kind", "ts"}
        }

        envelope = self._envelope(kind, ts, data)
        line = json.dumps(envelope, ensure_ascii=False, default=str)
        self._fh.write(line + "\n")
        self.event_counts[kind] = self.event_counts.get(kind, 0) + 1

        if kind == "session_start" and not self.ready_event.is_set():
            self._print_ready_banner(event)
            self.ready_event.set()

        # Operator-facing one-liner. `[record] {kind}{tail}` MUST stay
        # byte-identical for `loop_launcher_streaming.py`'s idle
        # watchdog (it greps stdout for `[record] user_turn` and
        # `[record] session_end`).
        if kind == "session_end":
            self.session_end_event.set()
            print("[record] session_end", flush=True)
            return

        tail = ""
        if kind == "tool_call":
            try:
                args_str = json.dumps(event.get("args") or {}, ensure_ascii=False, default=str)
            except Exception:
                args_str = repr(event.get("args"))
            tail = f" {event.get('name')}({args_str})"
        elif kind == "tool_result":
            try:
                result_str = json.dumps(event.get("result"), ensure_ascii=False, default=str)
            except Exception:
                result_str = repr(event.get("result"))
            if len(result_str) > 200:
                result_str = result_str[:200] + "…"
            tail = f" {event.get('name')} -> {result_str}"
        elif "text" in event:
            tail = f" text={str(event['text'])!r}"
        print(f"[record] {kind}{tail}", flush=True)

    @staticmethod
    def _print_ready_banner(event: dict) -> None:
        model = event.get("llm_model") or event.get("model") or "?"
        bar = "=" * 70
        print()
        print(bar, flush=True)
        print("  STREAMING AGENT WARM AND READY  —  begin the conversation.", flush=True)
        print(f"  model: {model}", flush=True)
        print("  type any text + Enter to send a typed user turn.", flush=True)
        print("  /help for commands, /done when finished.", flush=True)
        print(bar, flush=True)
        print(flush=True)

    def write_header(self, *, student_id: str, variant: str, room: str,
                     started: dt.datetime) -> None:
        self.write({
            "kind": "header",
            "ts": time.time(),
            "student_id": student_id,
            "variant": f"streaming{variant}",
            "room": room,
            "started": started.isoformat(timespec="seconds"),
            "host": os.uname().nodename,
        })

    def write_footer(self, *, ended: dt.datetime, started: dt.datetime,
                     exit_reason: str) -> None:
        """Post-pass: stream-read the JSONL we just wrote, compute
        per-turn latency aggregates + counts, write one final
        envelope line, and dump a `<basename>.metrics.json` sidecar
        with the same data so the analysis notebook can read N
        sessions with a single `pandas.read_json` glob."""
        metrics = self._compute_aggregates(
            ended=ended, started=started, exit_reason=exit_reason,
        )
        self.write({
            "kind": "footer",
            "ts": time.time(),
            **metrics,
        })
        # Sidecar: same payload as the footer's `data`, written to
        # `metrics.json` alongside the events.jsonl. Optional —
        # best-effort; analysis works without it (the footer line is
        # canonical).
        try:
            sidecar = self.log_path.parent / "metrics.json"
            with sidecar.open("w", encoding="utf-8") as fh:
                json.dump({
                    "conv_id": self.conv_id,
                    "variant": self.variant,
                    **metrics,
                }, fh, ensure_ascii=False, indent=2, default=str)
        except OSError as exc:
            print(
                f"[launcher-streaming] metrics sidecar write failed: {exc!r}",
                file=sys.stderr,
            )

    def _compute_aggregates(
        self, *, ended: dt.datetime, started: dt.datetime, exit_reason: str,
    ) -> dict:
        """Stream-read the JSONL we just wrote and compute per-turn
        latency pairs + counts. Stateless — re-runnable post-hoc on an
        unchanged file."""
        # Per-turn timestamps (ts_mono seconds) for every latency event.
        # Each key holds {turn_id: ts_mono}.
        per_turn: dict[str, dict[int, float]] = {
            "vad_user_speech_end": {},
            "llm_request_start": {},
            "llm_response_end": {},
            "tts_request_start": {},
            "tts_first_audio": {},
            "pepper_first_sound": {},
            "pepper_drain_ack": {},
        }
        tools_used: list[str] = []
        emotions_used: Counter[str] = Counter()
        n_user_turns = n_tool_calls = n_animation_requests = 0
        n_animation_dropped = n_errors = n_warnings = 0
        n_typed_input = 0

        try:
            with self.log_path.open("r", encoding="utf-8") as fh:
                for raw in fh:
                    raw = raw.strip()
                    if not raw:
                        continue
                    try:
                        line = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    event_kind = line.get("event")
                    data = line.get("data") or {}
                    ts_mono = float(line.get("ts_mono") or 0.0)
                    if event_kind in per_turn:
                        turn_id = data.get("turn_id")
                        if isinstance(turn_id, int):
                            # First occurrence wins — preemptive
                            # generation can fire llm_request_start
                            # twice for the same turn; the user-
                            # facing latency is the first one.
                            per_turn[event_kind].setdefault(turn_id, ts_mono)
                    if event_kind == "user_turn":
                        n_user_turns += 1
                    elif event_kind == "tool_call":
                        n_tool_calls += 1
                        name = data.get("name")
                        if name:
                            tools_used.append(name)
                    elif event_kind == "animation_requested":
                        n_animation_requests += 1
                        emo = data.get("emotion") or data.get("group")
                        if emo:
                            emotions_used[str(emo)] += 1
                    elif event_kind == "animation_dropped":
                        n_animation_dropped += 1
                    elif event_kind == "error":
                        n_errors += 1
                    elif event_kind == "warn":
                        n_warnings += 1
                    elif event_kind == "typed_input":
                        n_typed_input += 1
        except OSError as exc:
            print(
                f"[launcher-streaming] footer read-back failed: {exc!r}",
                file=sys.stderr,
            )

        def _pair_delta(key_start: str, key_end: str) -> list[float]:
            starts = per_turn.get(key_start, {})
            ends = per_turn.get(key_end, {})
            out: list[float] = []
            for tid, t_start in starts.items():
                t_end = ends.get(tid)
                if t_end is None or t_end < t_start:
                    continue
                out.append(t_end - t_start)
            return out

        def _summarise(samples: list[float]) -> dict:
            if not samples:
                return {}
            ordered = sorted(samples)

            def _pct(p: float) -> float:
                # Nearest-rank percentile — small-N robust, stdlib only.
                idx = max(0, min(len(ordered) - 1, int(round(p * (len(ordered) - 1)))))
                return ordered[idx]
            return {
                "n": len(ordered),
                "min": round(ordered[0], 4),
                "max": round(ordered[-1], 4),
                "median": round(statistics.median(ordered), 4),
                "p90": round(_pct(0.90), 4),
            }

        return {
            "conv_id": self.conv_id,
            "variant": self.variant,
            "ended": ended.isoformat(timespec="seconds"),
            "duration_seconds": round((ended - started).total_seconds(), 2),
            "exit_reason": exit_reason,
            "n_user_turns": n_user_turns,
            "n_typed_input": n_typed_input,
            "n_tool_calls": n_tool_calls,
            "n_animation_requests": n_animation_requests,
            "n_animation_dropped": n_animation_dropped,
            "n_errors": n_errors,
            "n_warnings": n_warnings,
            "tools_used": sorted(set(tools_used)),
            "emotions_used": dict(emotions_used),
            "event_counts": self.event_counts,
            "latency_first_audio_s": _summarise(
                _pair_delta("vad_user_speech_end", "pepper_first_sound"),
            ),
            "llm_latency_s": _summarise(
                _pair_delta("llm_request_start", "llm_response_end"),
            ),
            "tts_first_audio_s": _summarise(
                _pair_delta("tts_request_start", "tts_first_audio"),
            ),
            "playback_duration_s": _summarise(
                _pair_delta("pepper_first_sound", "pepper_drain_ack"),
            ),
            "total_turn_s": _summarise(
                _pair_delta("vad_user_speech_end", "pepper_drain_ack"),
            ),
        }

    def close(self) -> None:
        self._fh.close()


# ── Stdin watcher ────────────────────────────────────────────────────
async def _watch_stdin_for_done(room: rtc.Room | None) -> str:
    """Block on stdin. Plain lines get published on `pepper.text`;
    `/done` / EOF returns the exit reason."""
    loop = asyncio.get_running_loop()

    def _read_line() -> str | None:
        try:
            return sys.stdin.readline()
        except Exception:
            return None

    def _print_help() -> None:
        print("[launcher-streaming] commands:", flush=True)
        print("  /done        end the conversation (also: EOF / Ctrl+D)", flush=True)
        print("  /help        show this list", flush=True)
        print("  <any text>   send to the agent as a typed user turn", flush=True)

    while True:
        line = await loop.run_in_executor(None, _read_line)
        if line is None or line == "":
            return "eof"
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if lowered in ("/done", "done"):
            return "done"
        if lowered in ("/help", "help", "?", "/?"):
            _print_help()
            continue
        if stripped.startswith("/"):
            print(f"[launcher-streaming] unknown command: {stripped.split()[0]} — type /help",
                  flush=True)
            continue
        if room is None:
            print("[launcher-streaming] room not connected yet — text input ignored",
                  flush=True)
            continue
        try:
            await room.local_participant.publish_data(
                json.dumps({"text": stripped}).encode("utf-8"),
                topic=TOPIC_TEXT,
            )
            print(f"[typed] {stripped}", flush=True)
        except Exception as exc:
            print(f"[launcher-streaming] failed to send text: {exc}", flush=True)


async def _wait_session_end(recorder: Recorder) -> str:
    """Resolve when the worker publishes session_end (e.g. after the
    `end_conversation` tool would normally fire — though this variant
    doesn't include that tool, the event still fires on session.close)."""
    await recorder.session_end_event.wait()
    return "session_end"


async def _wait_for_agents(room: rtc.Room, *, timeout: float):
    """Wait until at least one `agent-*` participant joins."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        agents = [
            p for p in room.remote_participants.values()
            if str(getattr(p, "identity", "") or "").startswith("agent-")
        ]
        if agents:
            return agents
        await asyncio.sleep(0.2)
    return [
        p for p in room.remote_participants.values()
        if str(getattr(p, "identity", "") or "").startswith("agent-")
    ]


# ── Worker-lifecycle helpers (LiveKit-native) ────────────────────────


async def _drain_existing_workers(lkapi: api.LiveKitAPI, room: str) -> None:
    """Remove every active dispatch + agent participant in `room`.

    Two-step cleanup because the LiveKit primitives have separate
    responsibilities:

      * `delete_dispatch` removes the *dispatch record* — prevents
        LiveKit from re-dispatching the worker if a job ends — but
        does NOT terminate a currently-running job.
      * `remove_participant` kicks the agent's *participant* out of
        the room — which the worker's `RoomOptions(close_on_disconnect=
        True, participant_identity="user")` translates into a clean
        AgentSession close on the worker side (since "user" leaves
        from the worker's perspective when the room is dropped under
        it).

    Then we poll until no `agent-*` remains so the new dispatch races
    against nothing.
    """
    # 1. Delete every dispatch record for this room.
    try:
        dispatches = await lkapi.agent_dispatch.list_dispatch(room_name=room)
    except Exception as exc:
        print(f"[launcher-streaming] list_dispatch failed: {exc!r}", file=sys.stderr)
        dispatches = []
    for d in dispatches:
        did = getattr(d, "id", "") or ""
        dname = getattr(d, "agent_name", "?")
        if not did:
            continue
        try:
            await lkapi.agent_dispatch.delete_dispatch(dispatch_id=did, room_name=room)
            print(f"[launcher-streaming] deleted stale dispatch id={did} agent={dname}")
        except Exception as exc:
            print(
                f"[launcher-streaming] delete_dispatch({did}) failed: {exc!r}",
                file=sys.stderr,
            )

    # 2. Kick every agent-* participant currently in the room.
    try:
        existing = await lkapi.room.list_participants(
            api.ListParticipantsRequest(room=room)
        )
    except Exception as exc:
        print(
            f"[launcher-streaming] list_participants failed: {exc!r}",
            file=sys.stderr,
        )
        return
    for p in existing.participants:
        ident = str(getattr(p, "identity", "") or "")
        if not ident.startswith("agent-"):
            continue
        try:
            await lkapi.room.remove_participant(
                api.RoomParticipantIdentity(room=room, identity=ident)
            )
            print(f"[launcher-streaming] kicked stale agent={ident}")
        except Exception as exc:
            print(
                f"[launcher-streaming] failed to remove {ident}: {exc!r}",
                file=sys.stderr,
            )

    # 3. Poll until no agent-* remains. LiveKit propagates
    #    participant_disconnected asynchronously; without this poll
    #    `_wait_for_agents` below can pick up the just-kicked agent
    #    and treat it as our "fresh" one.
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        try:
            check = await lkapi.room.list_participants(
                api.ListParticipantsRequest(room=room)
            )
        except Exception:
            break
        still_there = [
            p for p in check.participants
            if str(getattr(p, "identity", "") or "").startswith("agent-")
        ]
        if not still_there:
            return
        await asyncio.sleep(0.15)


async def _cooperative_shutdown(
    room: rtc.Room | None,
    lkapi: api.LiveKitAPI,
    room_name: str,
    dispatch_id: str | None,
) -> None:
    """Tear down the active dispatch via every available signal.

    Run on every exit path (clean /done, session_end, SIGINT,
    exception). Order matters:

      1. Publish `pepper.control shutdown` so the worker's data
         handler can run its shutdown callback (drain TTS, publish
         session_end, etc.) BEFORE we kick it.
      2. Brief sleep so step 1 propagates.
      3. `delete_dispatch` so LiveKit forgets the dispatch.
      4. `remove_participant` on any straggler agent-*.

    Every step is best-effort and logged but never raised — we never
    want the cleanup itself to mask the real exit reason.
    """
    # 1. Cooperative ping.
    if room is not None:
        try:
            await room.local_participant.publish_data(
                json.dumps({"cmd": "shutdown"}).encode("utf-8"),
                topic=TOPIC_CONTROL,
            )
            print("[launcher-streaming] shutdown ping sent on pepper.control")
        except Exception as exc:
            print(
                f"[launcher-streaming] shutdown ping failed: {exc!r}",
                file=sys.stderr,
            )
    # 2. Give the worker ~300 ms to drain.
    await asyncio.sleep(0.3)
    # 3. Delete dispatch record (bookkeeping; does not stop in-flight job).
    if dispatch_id:
        try:
            await lkapi.agent_dispatch.delete_dispatch(
                dispatch_id=dispatch_id, room_name=room_name,
            )
            print(f"[launcher-streaming] deleted dispatch id={dispatch_id}")
        except Exception as exc:
            print(
                f"[launcher-streaming] delete_dispatch({dispatch_id}) failed: {exc!r}",
                file=sys.stderr,
            )
    # 4. Hard kick any leftover agent participants.
    try:
        existing = await lkapi.room.list_participants(
            api.ListParticipantsRequest(room=room_name)
        )
        for p in existing.participants:
            ident = str(getattr(p, "identity", "") or "")
            if not ident.startswith("agent-"):
                continue
            try:
                await lkapi.room.remove_participant(
                    api.RoomParticipantIdentity(room=room_name, identity=ident)
                )
                print(f"[launcher-streaming] cleaned up agent={ident}")
            except Exception as exc:
                print(
                    f"[launcher-streaming] cleanup remove {ident} failed: {exc!r}",
                    file=sys.stderr,
                )
    except Exception as exc:
        print(
            f"[launcher-streaming] cleanup list_participants failed: {exc!r}",
            file=sys.stderr,
        )


# ── Main ─────────────────────────────────────────────────────────────
async def run(args: argparse.Namespace) -> int:
    if not args.api_key or not args.api_secret:
        print(
            "[launcher-streaming] LIVEKIT_API_KEY / LIVEKIT_API_SECRET not set "
            "(check .env at project root).",
            file=sys.stderr,
        )
        return 2

    student_id = str(args.student).strip()
    variant = args.variant
    agent_name = AGENT_NAME_BY_VARIANT[variant]
    room_name = args.room
    session_dir = _make_session_dir(student_id, variant)
    log_path = session_dir / "events.jsonl"
    conv_id = _format_conv_id(student_id)
    started = dt.datetime.now()

    print(f"[launcher-streaming] student_id  = {student_id}")
    print(f"[launcher-streaming] conv_id     = {conv_id}")
    print(f"[launcher-streaming] variant     = {variant}  (agent={agent_name})")
    print(f"[launcher-streaming] room        = {room_name}")
    print(f"[launcher-streaming] session_dir = {session_dir}")

    recorder = Recorder(log_path, conv_id=conv_id, variant=variant)
    recorder.write_header(
        student_id=student_id, variant=variant, room=room_name, started=started,
    )

    # Per-session audio dir — sits inside `session_dir/audio/` so the
    # session is self-contained (events.jsonl + metrics.json + audio
    # under one tree).
    audio_dir = session_dir / "audio"
    audio_capture: AudioCapture | None = None

    exit_reason = "ok"
    room: rtc.Room | None = None
    dispatch_id: str | None = None

    lkapi = api.LiveKitAPI(args.livekit_url, args.api_key, args.api_secret)
    try:
        # ── 1. Drain stale dispatches + agent participants ───────────
        # See `_drain_existing_workers` for the full rationale. tl;dr:
        # any previous run's dispatch must be cancelled AND its agent
        # participant kicked before we create the fresh dispatch,
        # otherwise the two agents race on the user's audio track and
        # cancel each other's TTS streams.
        await _drain_existing_workers(lkapi, room_name)

        # ── 2. Dispatch the streaming worker. ───────────────────────
        metadata_blob = json.dumps({"experiment_student_id": student_id})
        try:
            dispatch = await lkapi.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=agent_name,
                    room=room_name,
                    metadata=metadata_blob,
                )
            )
            dispatch_id = str(getattr(dispatch, "id", "") or "") or None
            print(f"[launcher-streaming] dispatched agent={agent_name} "
                  f"dispatch_id={dispatch_id}")
        except Exception as exc:
            print(f"[launcher-streaming] dispatch failed: {exc!r}", file=sys.stderr)
            worker_file = (
                "agent_streaming.py" if variant == "A" else "agent_4o_streaming.py"
            )
            print(
                f"[launcher-streaming] is {worker_file} running?\n"
                f"    uv run python voice-agent/src/experiment/{worker_file} dev",
                file=sys.stderr,
            )
            exit_reason = f"dispatch_failed:{exc!r}"
            return 3

        # ── 3. Join the room as the recorder. ───────────────────────
        token = (
            api.AccessToken(args.api_key, args.api_secret)
            .with_identity(RECORDER_IDENTITY)
            .with_name("Experimenter Recorder")
            .with_grants(api.VideoGrants(
                room_join=True,
                room=room_name,
                can_publish=True,
                can_subscribe=True,
                can_publish_data=True,
            ))
        ).to_jwt()

        room = rtc.Room()

        @room.on("data_received")
        def _on_data(packet: rtc.DataPacket):
            topic = str(getattr(packet, "topic", "") or "")
            if topic != TOPIC_EXPERIMENT:
                return
            try:
                event = json.loads(packet.data)
            except (ValueError, TypeError):
                return
            recorder.write(event)
            # Route turn-boundary events into AudioCapture so per-turn
            # WAVs roll in lockstep with the JSONL. Agent rotation is
            # NOT per-TTS-utterance — multiple `session.say()` calls
            # within one turn (e.g. the end-of-conversation farewell)
            # must accumulate into ONE `T<id>_turn{N}_agent.wav`.
            # So we rotate the agent WAV only on (a) the NEXT user
            # turn boundary, or (b) session_end.
            if audio_capture is None:
                return
            kind = str(event.get("kind") or "")
            if kind == "vad_user_speech_end":
                turn_id = event.get("turn_id")
                if isinstance(turn_id, int):
                    audio_capture.close_user_turn(turn_id)
                    # Close the agent's response to the previous turn
                    # (its audio was captured between two user-end
                    # markers). Turn 1's agent file gets closed on
                    # session_end.
                    if turn_id > 1:
                        audio_capture.close_agent_turn(turn_id - 1)
            elif kind == "session_end":
                # Final agent file: the response to the LAST user
                # turn. The launcher doesn't track turn_id locally,
                # so peek at the recorder's last-seen turn (best
                # effort — recorder.event_counts doesn't carry it,
                # but the worker re-emits turn_id on every event so
                # the value below is set elsewhere). Fall back to
                # using the recorder's count of vad ends.
                final_turn = recorder.event_counts.get("vad_user_speech_end", 0)
                if final_turn > 0:
                    audio_capture.close_agent_turn(final_turn)

        await room.connect(args.livekit_url, token)
        # Start audio capture AFTER room.connect so the `track_subscribed`
        # handler is registered in time for the agent's audio publish.
        audio_capture = AudioCapture(
            room=room,
            audio_dir=audio_dir,
            conv_id=conv_id,
            user_identity=os.environ.get("USER_IDENTITY", "user"),
        )
        audio_capture.start()
        print(f"[launcher-streaming] recorder joined room={room_name} "
              f"identity={RECORDER_IDENTITY}")
        print("[launcher-streaming] waiting for agent to warm up...", flush=True)

        # Sanity check: exactly ONE agent in the room.
        agents = await _wait_for_agents(room, timeout=AGENT_WAIT_TIMEOUT_SEC)
        agent_idents = sorted(a.identity for a in agents)
        if not agents:
            print(
                f"[launcher-streaming] ERROR: no agent participant joined within "
                f"{AGENT_WAIT_TIMEOUT_SEC:.0f}s. Is the worker running?",
                file=sys.stderr,
            )
            exit_reason = "no_agent_joined"
            return 4
        if len(agents) > 1:
            print(
                f"[launcher-streaming] WARNING: {len(agents)} agent participants "
                f"in room: {agent_idents}. Stale workers from previous runs are "
                f"likely still alive. Restart the streaming worker.",
                file=sys.stderr,
            )
            # Don't bail — proceed and let the experimenter decide.
        else:
            print(f"[launcher-streaming] one agent in room: {agent_idents[0]}",
                  flush=True)

        # ── 4. Wait for /done, worker self-shutdown, OR worker death.
        # Three signals can end the loop:
        #   stdin_task         — experimenter typed /done or hit EOF
        #   session_end_task   — worker published session_end on
        #                        pepper.experiment (clean self-shutdown)
        #   worker_gone_task   — worker's agent-* participant left the
        #                        room mid-session (crash, SSH tunnel
        #                        drop, kicked elsewhere). Without this
        #                        the launcher hangs in readline() after
        #                        the worker died, which makes
        #                        loop_launcher's proc.wait() block.
        worker_gone_event = asyncio.Event()

        @room.on("participant_disconnected")
        def _on_participant_disconnected(p):
            ident = str(getattr(p, "identity", "") or "")
            if ident.startswith("agent-"):
                print(
                    f"[launcher-streaming] agent left room mid-session "
                    f"identity={ident}",
                    flush=True,
                )
                worker_gone_event.set()

        async def _wait_worker_gone() -> str:
            await worker_gone_event.wait()
            return "worker_disconnected"

        try:
            stdin_task = asyncio.create_task(_watch_stdin_for_done(room))
            session_end_task = asyncio.create_task(_wait_session_end(recorder))
            worker_gone_task = asyncio.create_task(_wait_worker_gone())
            done, pending = await asyncio.wait(
                [stdin_task, session_end_task, worker_gone_task],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in pending:
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass
            if session_end_task in done:
                exit_reason = "session_end"
                print(
                    "[launcher-streaming] session_end received — worker "
                    "shut down on its own; finishing cleanup..."
                )
            elif worker_gone_task in done:
                exit_reason = "worker_disconnected"
                print(
                    "[launcher-streaming] worker disconnected unexpectedly; "
                    "finishing cleanup..."
                )
            else:
                exit_reason = stdin_task.result()
        except KeyboardInterrupt:
            exit_reason = "interrupted"
            print("[launcher-streaming] interrupted — sending shutdown...")

    except Exception as exc:
        exit_reason = f"exception:{type(exc).__name__}:{exc}"
        print(f"[launcher-streaming] unexpected error: {exc!r}", file=sys.stderr)
    finally:
        # Cooperative shutdown: ping the worker via pepper.control so
        # its add_shutdown_callback runs (drains TTS, publishes
        # session_end, aclose's the session) BEFORE we kick it, then
        # delete the dispatch record + remove any straggler
        # agent-* participants. See `_cooperative_shutdown`.
        try:
            await _cooperative_shutdown(room, lkapi, room_name, dispatch_id)
        except Exception as exc:
            print(
                f"[launcher-streaming] cooperative_shutdown failed: {exc!r}",
                file=sys.stderr,
            )

        # Finalise any in-flight audio FIRST — once we disconnect the
        # room the track subscription drops and we lose tail frames.
        if audio_capture is not None:
            try:
                await audio_capture.shutdown()
            except Exception as exc:
                print(
                    f"[launcher-streaming] audio_capture shutdown failed: {exc!r}",
                    file=sys.stderr,
                )

        if room is not None:
            try:
                await room.disconnect()
            except Exception:
                pass

        try:
            await lkapi.aclose()
        except Exception:
            pass

        ended = dt.datetime.now()
        # Post-pass: streams back through the JSONL we just wrote,
        # computes per-turn latency aggregates + counts, appends one
        # `footer` event, and dumps a sidecar `.metrics.json`.
        recorder.write_footer(ended=ended, started=started, exit_reason=exit_reason)
        recorder.close()
        print(f"[launcher-streaming] done exit_reason={exit_reason} "
              f"session_dir={recorder.log_path.parent}")

    return 0


def main() -> int:
    args = _parse_args()
    # Install a SIGINT handler that calls into the event loop's signal
    # support so the `finally:` in `run()` reliably runs and the
    # cooperative shutdown ping is sent. Without this, a Ctrl+C while
    # `_watch_stdin_for_done` is blocked on stdin can unwind the
    # event loop in ways that skip the cleanup.
    #
    # asyncio.run() already raises KeyboardInterrupt to the run()
    # coroutine on SIGINT, so the existing `except KeyboardInterrupt`
    # path catches it. This handler is belt-and-braces: it logs that
    # the signal arrived so we can tell SIGINT-triggered cleanups
    # apart from natural EOFs in the trace.
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[launcher-streaming] Ctrl+C — exiting (log may be incomplete)")
        return 130


if __name__ == "__main__":
    sys.exit(main())
