#!/usr/bin/env python3
"""Pepper student-study experiment launcher.

Per conversation, this script:
  1. Generates a unique room name `experiment-student<id>-variant<X>-<HHMMSS>`.
  2. Dispatches the `pepper-experiment` agent to that room via
     LiveKit's agent_dispatch API, with metadata
     {experiment_variant, experiment_student_id} so the worker
     specialises per condition.
  3. Joins the room itself as participant `experimenter-recorder`,
     subscribes to the `pepper.experiment` data topic the worker
     publishes to, and writes every event as a JSONL turn record.
  4. Watches stdin for `/done`. On /done, publishes a shutdown
     command on `pepper.control` (the worker subscribes), waits for
     it to leave, then disconnects and writes the JSONL footer.

RECOMMENDED: run the experiment worker on woska so STT (FasterWhisper)
and TTS (Piper) hit the GPU instead of the RPi's CPU.

Run order for a study session:

    cd /home/lucas/Projects/FEL/Pepper

    # 1. Production stack stays up (one-time):
    docker compose -f docker/docker-compose.yml up -d

    # 2. Sync experiment files to woska + start the worker on woska in
    #    a tmux session named `pepper-experiment`. See cmd.md for the
    #    full ssh+tmux block. tl;dr:
    ./services/scripts/experiment/sync_to_woska.sh
    # then on woska:
    #   tmux new-session -s pepper-experiment
    #   cd /mnt/.../Pepper && source .venv3/bin/activate
    #   python voice-agent/src/experiment/agent.py dev

    # 3. Per conversation — only this command runs in the foreground:
    uv run python voice-agent/tests/local_llm_benchmark/experiment.py \
        --student 1 --variant A
    # …converse, type `/done` + Enter to end…
    uv run python voice-agent/tests/local_llm_benchmark/experiment.py \
        --student 1 --variant B
    # …etc.

Local-only fallback (worker runs on the RPi inside Docker — slower,
but no woska needed):
    docker compose -f docker/docker-compose.yml stop voice-agent
    docker compose -f docker/docker-compose.yml --profile experiment \
        up -d voice-agent-experiment
    # then run the launcher as above; restore with:
    docker compose -f docker/docker-compose.yml stop voice-agent-experiment
    docker compose -f docker/docker-compose.yml start voice-agent

Logs are written to:
    voice-agent/tests/local_llm_benchmark/results/experiments/<YYYY-MM-DD>/
        student<id>_variant<X>_<HHMMSS>.jsonl
"""

from __future__ import annotations

# Silence aiohttp/livekit DeprecationWarnings before any livekit import.
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

# Path glue + .env for credentials.
THIS_DIR = Path(__file__).resolve().parent
VOICE_AGENT_DIR = THIS_DIR.parent.parent
ROOT_ENV_PATH = VOICE_AGENT_DIR.parent / ".env"
if ROOT_ENV_PATH.exists():
    load_dotenv(dotenv_path=ROOT_ENV_PATH, override=False)

VARIANTS = ("A", "B", "C")
EXPERIMENT_AGENT_NAME = "pepper-experiment"
EXPERIMENT_AGENT_NAME_REALTIME = "pepper-experiment-realtime"
EXPERIMENT_AGENT_NAME_4O = "pepper-experiment-4o"
# Variant → agent_name. A uses the local-stack worker on woska
# (agent.py). B uses the OpenAI Realtime worker on the RPi
# (agent_realtime.py). C uses the OpenAI 4o-chained worker on the
# RPi (agent_4o.py: silero VAD + gpt-4o-transcribe + gpt-4o-mini +
# gpt-4o-mini-tts). All three share the same room + recorder +
# JSONL schema — only the dispatch target differs.
AGENT_NAME_BY_VARIANT = {
    "A": EXPERIMENT_AGENT_NAME,
    "B": EXPERIMENT_AGENT_NAME_REALTIME,
    "C": EXPERIMENT_AGENT_NAME_4O,
}
EXPERIMENT_ROOM_NAME = "pepper-experiment"  # matches experiment-orchestrator
RECORDER_IDENTITY = "experimenter-recorder"
TOPIC_EXPERIMENT = "pepper.experiment"
TOPIC_CONTROL = "pepper.control"
TOPIC_TEXT = "pepper.text"
RESULTS_ROOT = THIS_DIR / "results" / "experiments"

logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s.%(msecs)03d %(levelname)s %(name)s %(message)s",
    datefmt="%H:%M:%S",
)
logging.getLogger("livekit").setLevel(logging.WARNING)
logger = logging.getLogger("experiment")
logger.setLevel(logging.INFO)


# ── Args ──────────────────────────────────────────────────────────────


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="Run one Pepper student-study conversation.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--student", required=True, help="Student id (e.g. 1, 2, s12345).")
    p.add_argument("--variant", required=True, choices=VARIANTS,
                   help="Experimental condition.")
    p.add_argument("--room", default=EXPERIMENT_ROOM_NAME,
                   help=f"LiveKit room to dispatch into. Default: "
                        f"{EXPERIMENT_ROOM_NAME!r} — matches the room "
                        f"created by docker-compose.experiment.yml's "
                        f"experiment-orchestrator.")
    p.add_argument("--livekit-url", default=os.environ.get("LIVEKIT_URL", "ws://127.0.0.1:7880"))
    p.add_argument("--api-key", default=os.environ.get("LIVEKIT_API_KEY"))
    p.add_argument("--api-secret", default=os.environ.get("LIVEKIT_API_SECRET"))
    return p.parse_args()


def _make_log_path(student_id: str, variant: str) -> Path:
    now = dt.datetime.now()
    date_dir = RESULTS_ROOT / now.strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)
    return date_dir / f"student{student_id}_variant{variant}_{now:%H%M%S}.jsonl"


# Room name is fixed (see EXPERIMENT_ROOM_NAME). The
# experiment-orchestrator creates it on docker-compose up, and
# bridge + audio-bridge join it via livekit_session.json. The
# launcher just dispatches the agent into the existing room and
# joins as the recorder participant.


# ── Recorder ──────────────────────────────────────────────────────────


class Recorder:
    """Subscribes to `pepper.experiment` data packets and writes JSONL.

    Each line in the file is one structured event:
      {"kind": "session_start", "ts": ..., "variant": ..., ...}
      {"kind": "user_turn",     "ts": ..., "text": "..."}
      {"kind": "tool_call",     "ts": ..., "name": "...", "args": {...}}
      {"kind": "agent_speech",  "ts": ..., "text": "..."}
      {"kind": "session_end",   "ts": ...}
    """

    def __init__(self, log_path: Path) -> None:
        self.log_path = log_path
        self._fh = log_path.open("a", buffering=1)  # line-buffered
        self.event_counts: dict[str, int] = {}
        self.ready_event = asyncio.Event()  # set on first session_start

    def write(self, event: dict) -> None:
        # Always tag with the wall-clock recording time so we can
        # reconstruct order even if the worker's clock drifts.
        event = {**event, "recorded_at": time.time()}
        line = json.dumps(event, ensure_ascii=False, default=str)
        self._fh.write(line + "\n")
        kind = str(event.get("kind", "?"))
        self.event_counts[kind] = self.event_counts.get(kind, 0) + 1
        if kind == "session_start" and not self.ready_event.is_set():
            self._print_ready_banner(event)
            self.ready_event.set()
        else:
            # Echo to stdout so the experimenter sees what's being
            # logged without tailing the file. Full text — no truncation,
            # since seeing the entire utterance / tool args is the whole
            # point of the live log.
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
        """Loud, can't-miss-it banner so the experimenter knows when
        to start the conversation."""
        model = event.get("model") or "?"
        bar = "=" * 70
        print()
        print(bar, flush=True)
        print("  AGENT WARM AND READY  —  begin the conversation now.", flush=True)
        print(f"  model: {model}", flush=True)
        print("  type any text + Enter to send a typed user turn.", flush=True)
        print("  /help for commands, /done when finished.", flush=True)
        print(bar, flush=True)
        print(flush=True)

    def write_header(
        self, *, student_id: str, variant: str, room: str, started: dt.datetime,
    ) -> None:
        self.write({
            "kind": "header",
            "student_id": student_id,
            "variant": variant,
            "room": room,
            "started": started.isoformat(timespec="seconds"),
            "host": os.uname().nodename,
        })

    def write_footer(
        self, *, ended: dt.datetime, started: dt.datetime, exit_reason: str,
    ) -> None:
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


async def _watch_stdin_for_done(room: rtc.Room | None = None) -> None:
    """Read stdin until the experimenter types `/done` (or EOF).

    Plain text lines are published on the `pepper.text` topic so the
    experiment worker injects them as user input (bypassing STT) — lets
    you drive the conversation by typing instead of speaking. Use this
    to test prompts/tools without firing up the mic.

    Slash commands:
      /done          end the conversation (also: bare `done` or EOF)
      /help          show this list
    """
    loop = asyncio.get_running_loop()

    def _read_line() -> str | None:
        try:
            return sys.stdin.readline()
        except Exception:
            return None

    def _print_help() -> None:
        print("[experiment] commands:", flush=True)
        print("  /done        end the conversation (also: EOF / Ctrl+D)", flush=True)
        print("  /help        show this list", flush=True)
        print("  <any text>   send to the agent as user input (typed turn)", flush=True)

    while True:
        line = await loop.run_in_executor(None, _read_line)
        if line is None or line == "":
            return  # EOF (Ctrl+D)
        stripped = line.strip()
        if not stripped:
            continue
        lowered = stripped.lower()
        if lowered in ("/done", "done"):
            return
        if lowered in ("/help", "help", "?", "/?"):
            _print_help()
            continue
        if stripped.startswith("/"):
            print(f"[experiment] unknown command: {stripped.split()[0]} — type /help",
                  flush=True)
            continue
        if room is None:
            print("[experiment] room not connected yet — text input ignored",
                  flush=True)
            continue
        try:
            await room.local_participant.publish_data(
                json.dumps({"text": stripped}).encode("utf-8"),
                topic=TOPIC_TEXT,
            )
            print(f"[typed] {stripped}", flush=True)
        except Exception as exc:
            print(f"[experiment] failed to send text: {exc}", flush=True)


# ── Main ─────────────────────────────────────────────────────────────


async def run(args: argparse.Namespace) -> int:
    if not args.api_key or not args.api_secret:
        print(
            "[experiment] LIVEKIT_API_KEY / LIVEKIT_API_SECRET not set "
            "(check .env at project root).",
            file=sys.stderr,
        )
        return 2

    student_id = str(args.student).strip()
    variant = args.variant
    room_name = args.room
    log_path = _make_log_path(student_id, variant)
    started = dt.datetime.now()

    print(f"[experiment] student_id = {student_id}")
    print(f"[experiment] variant    = {variant}")
    print(f"[experiment] room       = {room_name}")
    print(f"[experiment] log file   = {log_path}")

    recorder = Recorder(log_path)
    recorder.write_header(
        student_id=student_id, variant=variant, room=room_name, started=started,
    )
    # Single source of truth for the JSONL footer's exit_reason. Mutated
    # along the way (KeyboardInterrupt, abort paths, normal /done) and
    # consumed once in the outer `finally`.
    exit_reason = "ok"
    # `room` lives in run()'s scope so the outer finally can clean it up
    # regardless of which exit path we take (incl. Ctrl+C before /done).
    room: rtc.Room | None = None

    # ── 1. Dispatch the agent into the room. ────────────────────────
    metadata_blob = json.dumps({
        "experiment_variant": variant,
        "experiment_student_id": student_id,
    })
    agent_name = AGENT_NAME_BY_VARIANT.get(variant, EXPERIMENT_AGENT_NAME)
    lkapi = api.LiveKitAPI(args.livekit_url, args.api_key, args.api_secret)
    try:
        try:
            dispatch = await lkapi.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=agent_name,
                    room=room_name,
                    metadata=metadata_blob,
                )
            )
            dispatch_id = str(getattr(dispatch, "id", "") or "")
            print(f"[experiment] dispatched agent={agent_name} "
                  f"dispatch_id={dispatch_id}")
        except Exception as exc:
            print(f"[experiment] dispatch failed: {exc!r}", file=sys.stderr)
            if agent_name == EXPERIMENT_AGENT_NAME_REALTIME:
                print(
                    "[experiment] is the voice-agent-realtime container running?\n"
                    "    docker compose -f docker/docker-compose.experiment.yml "
                    "up -d voice-agent-realtime\n"
                    "    docker compose -f docker/docker-compose.experiment.yml "
                    "logs -f voice-agent-realtime",
                    file=sys.stderr,
                )
            elif agent_name == EXPERIMENT_AGENT_NAME_4O:
                print(
                    "[experiment] is agent_4o.py running on woska in tmux "
                    "'pepper-experiment-4o'?\n"
                    "    ssh -J navarlu2@halmos.felk.cvut.cz navarlu2@woska\n"
                    "    tmux attach -t pepper-experiment-4o",
                    file=sys.stderr,
                )
            else:
                print(
                    "[experiment] is the pepper-experiment worker running on "
                    "woska in tmux 'pepper-experiment'? See docs/notes/cmd.md.",
                    file=sys.stderr,
                )
            exit_reason = f"dispatch_failed:{exc!r}"
            return 3

        # ── 2. Mint a token + join the room as the recorder. ────────
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
        loop = asyncio.get_running_loop()

        # Subscribe to data packets on the experiment topic.
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
        print(f"[experiment] recorder joined room={room_name} "
              f"identity={RECORDER_IDENTITY}")
        print("[experiment] waiting for agent to warm up "
              "(STT/TTS/LLM load — first dispatch can take 30-60s)...",
              flush=True)

        # ── 2b. Sanity check: exactly ONE agent must be in the room.
        # If a previous launcher run was Ctrl+C'd before /done, its
        # agent job stays in the room forever (the worker's session is
        # bound to user-client which never disconnects). Each new
        # dispatch then piles a NEW agent on top of the old ones, and
        # every transcript/tool call gets published once per agent.
        # Bail loudly so the experimenter knows to clean up before
        # collecting bad data.
        agents = await _wait_for_agents(room, timeout=60.0)
        agent_idents = sorted(a.identity for a in agents)
        if not agents:
            if agent_name == EXPERIMENT_AGENT_NAME_REALTIME:
                hint = ("Is voice-agent-realtime running on the RPi? "
                        "`docker compose -f docker/docker-compose.experiment.yml "
                        "logs -f voice-agent-realtime`")
            elif agent_name == EXPERIMENT_AGENT_NAME_4O:
                hint = ("Is agent_4o.py running on woska in tmux "
                        "'pepper-experiment-4o'? "
                        "`ssh -J navarlu2@halmos.felk.cvut.cz navarlu2@woska` "
                        "then `tmux attach -t pepper-experiment-4o`")
            else:
                hint = ("Is agent.py running on woska in tmux 'pepper-experiment'?")
            print(f"[experiment] ERROR: no agent participant joined the "
                  f"room within 60s. {hint}", file=sys.stderr)
            exit_reason = "no_agent_joined"
            return 4
        if len(agents) > 1:
            if agent_name == EXPERIMENT_AGENT_NAME_REALTIME:
                cleanup = ("docker compose -f docker/docker-compose.experiment.yml "
                           "restart voice-agent-realtime")
            elif agent_name == EXPERIMENT_AGENT_NAME_4O:
                # agent_4o.py runs on woska in tmux 'pepper-experiment-4o'.
                # Restart the worker there to drop all stale jobs.
                cleanup = ("ssh -J navarlu2@halmos.felk.cvut.cz navarlu2@woska "
                           "'tmux send-keys -t pepper-experiment-4o C-c \"python voice-agent/src/experiment/agent_4o.py dev\" Enter'")
            else:
                cleanup = ("ssh -J navarlu2@halmos.felk.cvut.cz navarlu2@woska && "
                           "pkill -f experiment/agent.py")
            print(f"[experiment] ERROR: {len(agents)} agent participants "
                  f"in room {room_name!r}: {agent_idents}.\n"
                  f"[experiment] Stale workers from previous runs are still "
                  f"alive. Clean up before re-running:\n"
                  f"    {cleanup}",
                  file=sys.stderr)
            exit_reason = "duplicate_agents"
            return 4
        print(f"[experiment] one agent in room: {agent_idents[0]}", flush=True)

        # ── 3. Wait for /done from stdin. ─────────────────────────
        # The outer `finally` always sends the shutdown command on
        # pepper.control + disconnects the room, regardless of which
        # exit path we take (/done, EOF, Ctrl+C, exception, abort).
        # The worker on woska also self-shuts when it sees the recorder
        # leave the room — belt and suspenders.
        try:
            await _watch_stdin_for_done(room)
            print("[experiment] /done received — sending shutdown to worker...")
        except (KeyboardInterrupt, asyncio.CancelledError):
            exit_reason = "interrupted"
            print("[experiment] interrupted — sending shutdown to worker...")
            raise
    finally:
        if room is not None:
            try:
                await room.local_participant.publish_data(
                    json.dumps({"cmd": "shutdown"}).encode("utf-8"),
                    topic=TOPIC_CONTROL,
                )
            except Exception as exc:
                logger.warning("shutdown publish failed: %s", exc)

            # Give the worker ~5s to flush its session_end event before
            # we disconnect (otherwise we'd miss the closing record).
            try:
                await asyncio.wait_for(_wait_session_end(recorder), timeout=8.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
            except Exception as exc:
                logger.warning("wait_session_end failed: %s", exc)

            try:
                await room.disconnect()
            except Exception as exc:
                logger.warning("room disconnect failed: %s", exc)

        recorder.write_footer(
            ended=dt.datetime.now(), started=started, exit_reason=exit_reason,
        )
        recorder.close()
        try:
            await lkapi.aclose()
        except Exception:
            pass

    print(f"[experiment] log saved: {log_path}")
    return 0


async def _wait_session_end(recorder: Recorder) -> None:
    """Poll the recorder's event_counts for `session_end`."""
    while recorder.event_counts.get("session_end", 0) == 0:
        await asyncio.sleep(0.1)


async def _wait_for_agents(room: rtc.Room, timeout: float) -> list:
    """Wait until at least one `agent-*` participant appears, then give
    any stragglers a 2s window to also show up so we can detect
    duplicates from leaked previous-run jobs."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        agents = [
            p for p in (room.remote_participants or {}).values()
            if str(getattr(p, "identity", "") or "").startswith("agent-")
        ]
        if agents:
            await asyncio.sleep(2.0)
            return [
                p for p in (room.remote_participants or {}).values()
                if str(getattr(p, "identity", "") or "").startswith("agent-")
            ]
        await asyncio.sleep(0.5)
    return []


def main() -> int:
    args = _parse_args()
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[experiment] Ctrl+C — exiting (log may be incomplete)")
        return 130


if __name__ == "__main__":
    sys.exit(main())
