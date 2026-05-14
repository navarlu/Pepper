#!/usr/bin/env python3
"""Loop wrapper around launcher.py.

Runs one launcher.py session, watches its stdout for `user_turn` events,
and if no user_turn arrives for `--idle-seconds` (default 60s) the wrapper
sends `/done` to the launcher's stdin to end the session cleanly. Then it
auto-increments --student and starts the next session. Loops forever; stop
with Ctrl+C.

Usage:
    uv run python voice-agent/src/experiment/loop_launcher.py \
        --student 1 --variant A
    # ...converse, walk away, wrapper ends session after 60s idle and
    # starts the next one with --student 2...
"""
from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import time
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
LAUNCHER = THIS_DIR / "launcher.py"
PROJECT_ROOT = THIS_DIR.parent.parent.parent  # .../Pepper

# `experiment_active` in services/data/state.json is the single source
# of truth for "loop_launcher is running" — the bridge polls it to
# decide sleep/wake posture and the tablet polls it to decide chat
# vs zzz UI. Heartbeat ages out in HEARTBEAT_STALE_SEC so a hard kill
# of this wrapper auto-heals (Pepper falls asleep on her own).
sys.path.insert(0, str(THIS_DIR))
from _runtime_state import write_runtime_state  # noqa: E402

HEARTBEAT_INTERVAL_SEC = 2.0

VARIANTS = ("A", "B", "C")


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--student", type=int, required=True,
                   help="Starting student id (integer). Incremented per session.")
    p.add_argument("--variant", required=True, choices=VARIANTS)
    p.add_argument("--idle-seconds", type=float, default=30.0,
                   help="Seconds with no user_turn before ending the session (default 30).")
    p.add_argument("--warmup-grace", type=float, default=300.0,
                   help="After warmup banner, allow this many seconds for the FIRST "
                        "user_turn before counting as idle (default 300).")
    return p.parse_args()


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

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(PROJECT_ROOT),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )

    warm = False
    warm_at: float | None = None        # monotonic time of warmup banner
    last_user_turn: float | None = None  # monotonic time of last real user_turn
    done_sent = False

    async def reader() -> None:
        nonlocal warm, warm_at, last_user_turn
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

    async def idle_watch() -> None:
        nonlocal done_sent
        # Wait until warm.
        while not warm and proc.returncode is None:
            await asyncio.sleep(0.5)
        while proc.returncode is None:
            await asyncio.sleep(1.0)
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
        for t in (reader_task, watch_task, stdin_task):
            t.cancel()
        for t in (reader_task, watch_task, stdin_task):
            try:
                await t
            except (asyncio.CancelledError, Exception):
                pass

    print(f"[loop] <<< session student={student_id} variant={variant} "
          f"exited rc={rc}", flush=True)
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
    student = args.student
    try:
        while True:
            try:
                await _run_one(student, args.variant, args.idle_seconds,
                               args.warmup_grace)
            except KeyboardInterrupt:
                print("\n[loop] Ctrl+C — exiting wrapper.", flush=True)
                return 130
            student += 1
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
