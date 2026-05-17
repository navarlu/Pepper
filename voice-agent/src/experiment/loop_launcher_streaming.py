#!/usr/bin/env python3
"""Loop wrapper around launcher_streaming.py.

Runs one launcher_streaming.py session, watches its stdout for
`user_turn` events, and if no user_turn arrives for `--idle-seconds`
(default 30 s) the wrapper sends `/done` to the launcher's stdin to
end the session cleanly. Then it auto-increments `--student`,
alternates `--variant` between `A` (local-streaming on woska) and
`B` (4o-streaming), and starts the next session. Loops forever; stop
with Ctrl+C.

State persistence: the next-up `student_id` + `variant` are written
to `results/streaming_loop_state.json` after every session, so re-
running with no args resumes from where the previous run left off.
Kept separate from production `loop_state.json` so the two never
fight over the counter.

Differences vs. the production [loop_launcher.py](loop_launcher.py):

  * Target: `launcher_streaming.py` (LiveKit-native dispatch lifecycle
    with `delete_dispatch` + `remove_participant` cleanup) rather than
    `launcher.py`.
  * No `end_conversation` / farewell detection — the streaming
    workers don't include that tool. Sessions end via `/done` (typed
    or idle timeout) or worker self-shutdown (worker_disconnected,
    session_end event). The post-`session_end` force-exit watchdog
    is kept though, in case the launcher hangs during cleanup.
  * Variant ↔ agent mapping is delegated to `launcher_streaming.py`'s
    `AGENT_NAME_BY_VARIANT` table — we just pass `--variant A|B`.
  * Separate state file so production and streaming loops do not step
    on each other's counter when both are in use.

Usage:

    # First time (or to reset the counter):
    uv run python voice-agent/src/experiment/loop_launcher_streaming.py \\
        --student 1 --variant A

    # Subsequent runs — no args needed, resumes from saved state:
    uv run python voice-agent/src/experiment/loop_launcher_streaming.py
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
LAUNCHER = THIS_DIR / "launcher_streaming.py"
PROJECT_ROOT = THIS_DIR.parent.parent.parent  # .../Pepper
LOOP_STATE_FILE = THIS_DIR / "results" / "streaming_loop_state.json"

# `experiment_active` in services/data/state.json is the single source
# of truth for "an experiment is in progress" — the bridge polls it
# to decide sleep/wake posture. Heartbeat ages out in
# HEARTBEAT_STALE_SEC so a hard kill of this wrapper auto-heals
# (Pepper falls asleep on her own).
sys.path.insert(0, str(THIS_DIR))
from _runtime_state import write_runtime_state  # noqa: E402

HEARTBEAT_INTERVAL_SEC = 2.0

VARIANTS = ("A", "B")
# Grace window after the launcher prints `session_end` (worker has
# torn itself down) before we SIGTERM the launcher. The clean
# launcher cleanup typically completes in well under this — anything
# longer is a hang.
POST_SESSION_END_GRACE_SEC = 5.0


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument(
        "--student", type=int, default=None,
        help="Starting student id (integer). Incremented per session. "
             "If omitted, resumes from results/streaming_loop_state.json "
             "(or 1 if no state file exists).",
    )
    p.add_argument(
        "--variant", default=None, choices=VARIANTS,
        help="Starting variant. If omitted, resumes from "
             "results/streaming_loop_state.json (or A if no state file).",
    )
    p.add_argument(
        "--idle-seconds", type=float, default=30.0,
        help="Seconds with no user_turn before ending the session (default 30).",
    )
    p.add_argument(
        "--warmup-grace", type=float, default=3600.0,
        help="After warmup banner, allow this many seconds for the FIRST "
             "user_turn before counting as idle (default 3600).",
    )
    p.add_argument(
        "--inter-session-pause", type=float, default=2.0,
        help="Pause between sessions to let LiveKit settle (default 2.0s). "
             "Lower than this risks the next launcher's drain step racing "
             "the previous worker's still-disconnecting agent participant.",
    )
    return p.parse_args()


# ── Loop state persistence ───────────────────────────────────────────


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
        print(f"[loop-streaming] WARN loop_state read failed: {exc!r}", flush=True)
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
        print(f"[loop-streaming] WARN loop_state write failed: {exc!r}", flush=True)


# ── One session ──────────────────────────────────────────────────────


async def _run_one(student_id: int, variant: str, idle_seconds: float,
                   warmup_grace: float) -> int:
    """Spawn one launcher_streaming.py and end it after idle_seconds of
    no user_turn. Returns the launcher's exit code."""
    cmd = [
        "uv", "run", "python", str(LAUNCHER),
        "--student", str(student_id),
        "--variant", variant,
    ]
    print(
        f"\n[loop-streaming] >>> starting session student={student_id} variant={variant}",
        flush=True,
    )
    print(f"[loop-streaming] cmd: {' '.join(cmd)}", flush=True)

    # PYTHONUNBUFFERED=1 so the child's print() lines hit our pipe
    # immediately instead of sitting in a block buffer until exit.
    # Without this, late shutdown logs appear only after the child
    # actually dies, making hangs invisible from the experimenter's
    # terminal.
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
    warm_at: float | None = None
    last_user_turn: float | None = None
    done_sent = False
    # Set True once `[record] session_end` is observed. The worker is
    # dead by then; the launcher is just running cleanup. The watchdog
    # uses this as the signal to start a short grace period before
    # SIGTERMing in case the launcher hangs during cleanup.
    session_end_seen = False
    force_exit_task: asyncio.Task | None = None

    async def _force_exit_when_dead():
        """Once the launcher has emitted session_end, its
        `_cooperative_shutdown` has POST_SESSION_END_GRACE_SEC to finish.
        Anything beyond that is a hang — escalate SIGTERM → SIGKILL so
        the loop can dispatch the next participant."""
        while not session_end_seen and proc.returncode is None:
            await asyncio.sleep(0.25)
        if proc.returncode is not None:
            return
        print(
            f"[loop-streaming] session_end seen — launcher has "
            f"{POST_SESSION_END_GRACE_SEC:.0f}s to finish cleanup.",
            flush=True,
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=POST_SESSION_END_GRACE_SEC)
            return
        except asyncio.TimeoutError:
            pass
        print(
            "[loop-streaming] launcher hung after session_end — sending SIGTERM.",
            flush=True,
        )
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=2.0)
            return
        except asyncio.TimeoutError:
            pass
        print(
            "[loop-streaming] launcher still alive after SIGTERM — SIGKILL.",
            flush=True,
        )
        proc.kill()
        await proc.wait()

    async def reader() -> None:
        """Mirror the child's stdout into our own + look for marker
        lines (warm banner, user turns, session_end)."""
        nonlocal warm, warm_at, last_user_turn, session_end_seen, force_exit_task
        assert proc.stdout is not None
        while True:
            line = await proc.stdout.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            print(text, flush=True)
            if not warm and "STREAMING AGENT WARM AND READY" in text:
                warm = True
                warm_at = time.monotonic()
                print(
                    f"[loop-streaming] warmup complete — idle timer armed "
                    f"(grace={warmup_grace:.0f}s for first turn, "
                    f"then {idle_seconds:.0f}s after each turn).",
                    flush=True,
                )
                continue
            if warm and "[record] user_turn" in text:
                last_user_turn = time.monotonic()
            if not session_end_seen and "[record] session_end" in text:
                session_end_seen = True
                if force_exit_task is None:
                    force_exit_task = asyncio.create_task(_force_exit_when_dead())

    async def idle_watch() -> None:
        """Send /done after `idle_seconds` of no user turns."""
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
                print(
                    f"[loop-streaming] {kind} for {elapsed:.0f}s — sending /done",
                    flush=True,
                )
                done_sent = True
                try:
                    assert proc.stdin is not None
                    proc.stdin.write(b"/done\n")
                    await proc.stdin.drain()
                    proc.stdin.close()
                except Exception as exc:
                    print(
                        f"[loop-streaming] failed to send /done: {exc!r}",
                        flush=True,
                    )
                return

    async def stdin_forwarder() -> None:
        """Forward our stdin lines to the child so typed turns / /done
        still work when invoked interactively."""
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
                done_sent = True
            try:
                proc.stdin.write(line.encode("utf-8"))
                await proc.stdin.drain()
            except Exception as exc:
                print(
                    f"[loop-streaming] stdin forward failed: {exc!r}",
                    flush=True,
                )
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

    print(
        f"[loop-streaming] <<< session student={student_id} variant={variant} "
        f"exited rc={rc}",
        flush=True,
    )
    return rc


# ── Heartbeat ────────────────────────────────────────────────────────


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


# ── Main ─────────────────────────────────────────────────────────────


async def run(args: argparse.Namespace) -> int:
    # Announce active state before the first dispatch so the bridge /
    # tablet flip to "awake" before Pepper is actually needed.
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

    source = (
        "cli" if args.student is not None or args.variant is not None
        else ("state-file" if saved else "default")
    )
    print(
        f"[loop-streaming] resume state: student={student} variant={variant} "
        f"(source={source})",
        flush=True,
    )
    # Persist immediately so the state file exists from the first
    # session — a crash before the first increment still leaves a
    # sensible resume point.
    _write_loop_state(student, variant)

    try:
        while True:
            try:
                await _run_one(
                    student, variant, args.idle_seconds, args.warmup_grace,
                )
            except KeyboardInterrupt:
                print("\n[loop-streaming] Ctrl+C — exiting wrapper.", flush=True)
                return 130
            student += 1
            variant = "B" if variant == "A" else "A"
            _write_loop_state(student, variant)
            print(
                f"[loop-streaming] next session will use student={student} "
                f"variant={variant} (after {args.inter_session_pause:.1f}s pause)",
                flush=True,
            )
            # Let LiveKit fully propagate participant_disconnected for
            # the previous worker before launcher_streaming's drain
            # runs — otherwise it might catch the old worker mid-leave.
            await asyncio.sleep(args.inter_session_pause)
    finally:
        heartbeat_task.cancel()
        try:
            await heartbeat_task
        except (asyncio.CancelledError, Exception):
            pass
        # Explicit "we're done" — the bridge picks this up on the next
        # mtime tick (≤0.5 s) and puts Pepper to sleep immediately
        # rather than waiting for the heartbeat to age out.
        write_runtime_state({"experiment_active": False})
        print(
            "[loop-streaming] experiment_active=false written to state.json",
            flush=True,
        )


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
        print("\n[loop-streaming] Ctrl+C — exiting wrapper.", flush=True)
        return 130


if __name__ == "__main__":
    sys.exit(main())
