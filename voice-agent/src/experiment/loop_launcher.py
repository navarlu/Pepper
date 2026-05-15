#!/usr/bin/env python3
"""Loop wrapper around launcher.py.

Runs one launcher.py session, watches its stdout for `user_turn` events,
and if no user_turn arrives for `--idle-seconds` (default 60s) the wrapper
sends `/done` to the launcher's stdin to end the session cleanly. Then it
auto-increments --student and starts the next session. Loops forever; stop
with Ctrl+C.

State persistence: the next-up `student_id` + `variant` are written to
`results/loop_state.json` after every session, so re-running the wrapper
with no args resumes where the previous run left off (e.g. stopped at
T05/A → next start picks up T06/C automatically). Pass `--student` /
`--variant` explicitly to override the saved state.

Usage:
    # First time (or to reset the counter):
    uv run python voice-agent/src/experiment/loop_launcher.py \
        --student 1 --variant A
    # Subsequent runs — no args needed, resumes from saved state:
    uv run python voice-agent/src/experiment/loop_launcher.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
import time
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
LAUNCHER = THIS_DIR / "launcher.py"
PROJECT_ROOT = THIS_DIR.parent.parent.parent  # .../Pepper
LOOP_STATE_FILE = THIS_DIR / "results" / "loop_state.json"

# `experiment_active` in services/data/state.json is the single source
# of truth for "loop_launcher is running" — the bridge polls it to
# decide sleep/wake posture and the tablet polls it to decide chat
# vs zzz UI. Heartbeat ages out in HEARTBEAT_STALE_SEC so a hard kill
# of this wrapper auto-heals (Pepper falls asleep on her own).
sys.path.insert(0, str(THIS_DIR))
from _runtime_state import write_runtime_state  # noqa: E402

HEARTBEAT_INTERVAL_SEC = 2.0

VARIANTS = ("A", "B", "C")

# Seconds to keep the farewell QR visible AFTER a session ends with
# `end_conversation`. Enforced here (post-session sleep) rather than
# inside the tool so the worker can fully shut down and no queued
# user_turn can fire during this window. Read at startup from the
# shared voice-agent config so the tool and the loop agree.
def _read_farewell_display_sec() -> int:
    import os
    raw = os.environ.get("EXPERIMENT_FAREWELL_DISPLAY_SEC")
    if raw and raw.strip():
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return 30


FAREWELL_DISPLAY_SEC = _read_farewell_display_sec()


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--student", type=int, default=None,
                   help="Starting student id (integer). Incremented per session. "
                        "If omitted, resumes from results/loop_state.json "
                        "(or 1 if no state file exists).")
    p.add_argument("--variant", default=None, choices=VARIANTS,
                   help="Starting variant. If omitted, resumes from "
                        "results/loop_state.json (or A if no state file).")
    p.add_argument("--idle-seconds", type=float, default=30.0,
                   help="Seconds with no user_turn before ending the session (default 30).")
    p.add_argument("--warmup-grace", type=float, default=3600.0,
                   help="After warmup banner, allow this many seconds for the FIRST "
                        "user_turn before counting as idle (default 3600).")
    return p.parse_args()


def _read_loop_state() -> dict:
    """Read the persisted next-up `student_id` + `variant`. Returns an
    empty dict if the file is missing or unreadable — caller falls back
    to defaults."""
    try:
        with LOOP_STATE_FILE.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {}
        return data
    except FileNotFoundError:
        return {}
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[loop] WARN loop_state read failed: {exc!r}", flush=True)
        return {}


def _write_loop_state(student: int, variant: str) -> None:
    """Persist the next-up `student_id` + `variant` so the next run
    resumes from here. Best-effort — a write failure logs but does not
    raise, since losing the counter is a UX paper cut, not a session
    failure."""
    try:
        LOOP_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "next_student_id": int(student),
            "next_variant": str(variant),
            "updated_at": time.time(),
        }
        tmp = LOOP_STATE_FILE.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        tmp.replace(LOOP_STATE_FILE)
    except OSError as exc:
        print(f"[loop] WARN loop_state write failed: {exc!r}", flush=True)


async def _run_one(student_id: int, variant: str, idle_seconds: float,
                   warmup_grace: float) -> int:
    """Spawn one launcher.py and end it after idle_seconds of no user_turn.

    Returns the launcher's exit code.
    """
    cmd = [
        "uv", "run", "python", str(LAUNCHER),
        "--student", str(student_id),
        "--variant", variant,
    ]
    print(f"\n[loop] >>> starting session student={student_id} variant={variant}",
          flush=True)
    print(f"[loop] cmd: {' '.join(cmd)}", flush=True)

    # PYTHONUNBUFFERED=1 forces line-buffered stdout in the child, so
    # every print() flushes immediately into our pipe instead of
    # sitting in a 4 KB block buffer until process exit. Without this,
    # late-stage logs like `[loop] launcher did not exit ...` only
    # surface AFTER the child finally dies, which makes hangs look
    # invisible from the experimenter's terminal.
    import os
    child_env = {**os.environ, "PYTHONUNBUFFERED": "1"}
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(PROJECT_ROOT),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=child_env,
    )

    warm = False
    warm_at: float | None = None        # monotonic time of warmup banner
    last_user_turn: float | None = None  # monotonic time of last real user_turn
    done_sent = False
    # Set True once the recorder feed shows `tool_call end_conversation`
    # — i.e. the agent decided to wrap up. From that point on the
    # session is ending naturally and the idle timer / /done path must
    # stay out of the way.
    farewell_in_progress = False
    # Set True once `[record] session_end` is observed. The worker is
    # dead by then; launcher.py is just shutting down clients. The
    # watchdog (spawned below when farewell starts) uses this as the
    # signal to start the short grace period before SIGTERM-ing.
    session_end_seen = False
    # Holds the watchdog task. Created lazily on farewell detection so
    # we never SIGTERM a healthy launcher.
    force_exit_task: asyncio.Task | None = None

    async def _force_exit_when_dead():
        """Once the worker has emitted session_end, launcher.py has at
        most a few seconds to finish writing the footer + close its
        HTTP/livekit clients. Anything beyond that is a hang we cannot
        afford — kill it so loop_launcher can dispatch the next
        participant."""
        # Phase 1: wait for session_end (or proc to exit on its own).
        while not session_end_seen and proc.returncode is None:
            await asyncio.sleep(0.25)
        if proc.returncode is not None:
            return
        # Phase 2: short grace for footer write + client teardown.
        print(
            "[loop] session_end seen — launcher has 3s to finish cleanup.",
            flush=True,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=3.0)
            return
        except asyncio.TimeoutError:
            pass
        print(
            "[loop] launcher hung after session_end — sending SIGTERM.",
            flush=True,
        )
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
            return
        except asyncio.TimeoutError:
            pass
        print(
            "[loop] launcher still alive after SIGTERM — sending SIGKILL.",
            flush=True,
        )
        proc.kill()
        await proc.wait()

    async def reader() -> None:
        nonlocal warm, warm_at, last_user_turn, farewell_in_progress
        nonlocal session_end_seen, force_exit_task
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            # Mirror to our stdout so the experimenter still sees everything.
            print(text, flush=True)
            if not warm and "AGENT WARM AND READY" in text:
                warm = True
                warm_at = time.monotonic()
                print(f"[loop] warmup complete — idle timer armed "
                      f"(grace={warmup_grace:.0f}s for first turn, "
                      f"then {idle_seconds:.0f}s after each turn).",
                      flush=True)
                continue
            if warm and "[record] user_turn" in text:
                last_user_turn = time.monotonic()
            # The recorder forwards every worker-emitted tool_call over
            # the data channel, so this marker appears here regardless
            # of whether the agent runs on woska or on the RPi.
            if not farewell_in_progress and \
                    "[record] tool_call end_conversation" in text:
                farewell_in_progress = True
                print(
                    "[loop] end_conversation detected — idle timer "
                    "disabled, force-exit watchdog armed.",
                    flush=True,
                )
                if force_exit_task is None:
                    force_exit_task = asyncio.create_task(_force_exit_when_dead())
            if not session_end_seen and "[record] session_end" in text:
                session_end_seen = True

    async def idle_watch() -> None:
        nonlocal done_sent
        # Wait until warm.
        while not warm and proc.returncode is None:
            await asyncio.sleep(0.5)
        while proc.returncode is None:
            await asyncio.sleep(1.0)
            # Once end_conversation has started running the worker
            # will shut itself down cleanly — do not race it with /done.
            if farewell_in_progress:
                continue
            now = time.monotonic()
            if last_user_turn is None:
                # No user turn yet — use warmup_grace from warmup banner.
                assert warm_at is not None
                elapsed = now - warm_at
                limit = warmup_grace
                kind = "no-first-turn"
            else:
                elapsed = now - last_user_turn
                limit = idle_seconds
                kind = "idle"
            if elapsed >= limit and not done_sent:
                print(f"[loop] {kind} for {elapsed:.0f}s — sending /done",
                      flush=True)
                done_sent = True
                try:
                    assert proc.stdin is not None
                    proc.stdin.write(b"/done\n")
                    await proc.stdin.drain()
                    proc.stdin.close()
                except Exception as exc:
                    print(f"[loop] failed to send /done: {exc!r}", flush=True)
                return

    async def stdin_forwarder() -> None:
        """Forward our stdin lines to the child so typed turns / /done still work."""
        nonlocal done_sent
        loop = asyncio.get_running_loop()
        assert proc.stdin is not None
        while proc.returncode is None:
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:  # EOF on wrapper stdin
                return
            if done_sent or proc.stdin.is_closing():
                continue
            if line.strip().lower() in ("/done", "done"):
                done_sent = True  # so idle_watch doesn't also try to send it
            try:
                proc.stdin.write(line.encode("utf-8"))
                await proc.stdin.drain()
            except Exception as exc:
                print(f"[loop] stdin forward failed: {exc!r}", flush=True)
                return

    reader_task = asyncio.create_task(reader())
    watch_task = asyncio.create_task(idle_watch())
    stdin_task = asyncio.create_task(stdin_forwarder())

    try:
        rc = await proc.wait()
    except asyncio.CancelledError:
        # Forward cancellation as SIGINT to the child.
        try:
            proc.send_signal(signal.SIGINT)
            rc = await asyncio.wait_for(proc.wait(), timeout=15.0)
        except Exception:
            proc.kill()
            rc = await proc.wait()
        raise
    finally:
        tasks = [reader_task, watch_task, stdin_task]
        if force_exit_task is not None:
            tasks.append(force_exit_task)
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    print(f"[loop] <<< session student={student_id} variant={variant} "
          f"exited rc={rc}", flush=True)

    # No post-session sleep — tablet-server owns the QR-hold window
    # itself via its `farewell_active` auto-clear timer (see
    # `services/src/live/tablet_server.py`). We can dispatch the next
    # session immediately so its warmup overlaps the QR display; the
    # tablet-server keeps the QR up regardless, and the new worker's
    # session_start payload no longer races to clear the farewell
    # banner (it sets `farewell_active=False`, but tablet-server
    # ignores that field until its own timer expires).

    return rc


async def _heartbeat_loop() -> None:
    """Refresh `experiment_active`/`experiment_heartbeat_ts` every 2 s.

    The bridge treats the experiment as active only if the heartbeat
    is recent — so as long as this task is running, Pepper stays
    awake; if this task dies for any reason, the heartbeat ages out
    and the bridge puts her to sleep.
    """
    while True:
        write_runtime_state({
            "experiment_active": True,
            "experiment_heartbeat_ts": time.time(),
        })
        await asyncio.sleep(HEARTBEAT_INTERVAL_SEC)


async def run(args: argparse.Namespace) -> int:
    # Announce we're active before the first session dispatch so the
    # bridge / tablet flip to "awake" before Pepper is actually needed.
    write_runtime_state({
        "experiment_active": True,
        "experiment_heartbeat_ts": time.time(),
    })
    heartbeat_task = asyncio.create_task(_heartbeat_loop())
    saved = _read_loop_state()
    if args.student is not None:
        student = args.student
    else:
        student = int(saved.get("next_student_id", 1))
    if args.variant is not None:
        variant = args.variant
    else:
        variant = str(saved.get("next_variant", "A"))
        if variant not in VARIANTS:
            variant = "A"
    print(f"[loop] resume state: student={student} variant={variant} "
          f"(source={'cli' if args.student is not None or args.variant is not None else 'state-file' if saved else 'default'})",
          flush=True)
    # Persist immediately so the file exists from the first session — a
    # crash before the first increment still leaves a sensible resume
    # point.
    _write_loop_state(student, variant)
    try:
        while True:
            try:
                await _run_one(student, variant, args.idle_seconds,
                               args.warmup_grace)
            except KeyboardInterrupt:
                print("\n[loop] Ctrl+C — exiting wrapper.", flush=True)
                return 130
            student += 1
            # Alternate between A and C after each session. If a different
            # variant (e.g. B) was passed in, switch into the A/C rotation
            # starting from A on the next session.
            variant = "C" if variant == "A" else "A"
            _write_loop_state(student, variant)
            print(f"[loop] next session will use student={student} "
                  f"variant={variant}", flush=True)
            # Brief breather between sessions so log files don't collide on HHMMSS.
            await asyncio.sleep(1.5)
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except (asyncio.CancelledError, Exception):
            pass
        # Explicit "we're done" — the bridge/tablet pick this up on the
        # next mtime tick (≤0.5s) and put Pepper to sleep immediately
        # rather than waiting for the heartbeat to age out.
        write_runtime_state({"experiment_active": False})
        print("[loop] experiment_active=false written to state.json", flush=True)


def main() -> int:
    args = _parse_args()
    # SIGTERM handler so `systemctl stop` / `docker stop` / `kill <pid>`
    # also reach the finally block above. SIGINT is already handled by
    # asyncio's KeyboardInterrupt path. kill -9 cannot be intercepted —
    # the bridge's heartbeat-staleness check handles that case.
    def _term_handler(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _term_handler)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("\n[loop] Ctrl+C — exiting wrapper.", flush=True)
        return 130


if __name__ == "__main__":
    sys.exit(main())
