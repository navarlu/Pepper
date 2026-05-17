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
import sys
import time
from pathlib import Path

from dotenv import load_dotenv
from livekit import api, rtc


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


def _make_log_path(student_id: str, variant: str) -> Path:
    now = dt.datetime.now()
    date_dir = RESULTS_ROOT / now.strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)
    return date_dir / f"student{student_id}_streaming{variant}_{now:%H%M%S}.jsonl"


# ── Recorder ─────────────────────────────────────────────────────────
class Recorder:
    """Subscribes to `pepper.experiment` and writes JSONL.

    Each line is one event:
      {"kind": "session_start", ...}
      {"kind": "user_turn", "text": "...", "input": "speech"|"typed"}
      {"kind": "tool_call", "name": "...", "args": {...}}
      {"kind": "tool_result", "name": "...", "result": {...}}
      {"kind": "agent_speech", "text": "..."}
      {"kind": "session_end"}
    """

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self._fh = log_path.open("a", buffering=1)  # line-buffered
        self.event_counts: dict[str, int] = {}
        self.ready_event = asyncio.Event()
        self.session_end_event = asyncio.Event()

    def write(self, event: dict) -> None:
        event = {**event, "recorded_at": time.time()}
        line = json.dumps(event, ensure_ascii=False, default=str)
        self._fh.write(line + "\n")
        kind = str(event.get("kind", "?"))
        self.event_counts[kind] = self.event_counts.get(kind, 0) + 1

        if kind == "session_start" and not self.ready_event.is_set():
            self._print_ready_banner(event)
            self.ready_event.set()
            return

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
            "student_id": student_id,
            "variant": f"streaming{variant}",
            "room": room,
            "started": started.isoformat(timespec="seconds"),
            "host": os.uname().nodename,
        })

    def write_footer(self, *, ended: dt.datetime, started: dt.datetime, exit_reason: str) -> None:
        self.write({
            "kind": "footer",
            "ended": ended.isoformat(timespec="seconds"),
            "duration_seconds": round((ended - started).total_seconds(), 2),
            "exit_reason": exit_reason,
            "event_counts": self.event_counts,
        })

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
    log_path = _make_log_path(student_id, variant)
    started = dt.datetime.now()

    print(f"[launcher-streaming] student_id = {student_id}")
    print(f"[launcher-streaming] variant    = {variant}  (agent={agent_name})")
    print(f"[launcher-streaming] room       = {room_name}")
    print(f"[launcher-streaming] log file   = {log_path}")

    recorder = Recorder(log_path)
    recorder.write_header(
        student_id=student_id, variant=variant, room=room_name, started=started,
    )

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

        await room.connect(args.livekit_url, token)
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
        recorder.write_footer(ended=ended, started=started, exit_reason=exit_reason)
        recorder.close()
        print(f"[launcher-streaming] done exit_reason={exit_reason} "
              f"log={recorder.log_path}")

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
