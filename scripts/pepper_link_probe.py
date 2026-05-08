"""Pepper link latency probe.

Measures end-to-end libqi RPC latency from this host to Pepper, the same
TCP/libqi path the bridge audio uses — but isolated from the LLM/STT/TTS
pipeline. Run twice (no keepalive vs. with keepalive) and compare.

Modes
-----
rpc     calls `ALAudioDevice.getOutputVolume()` repeatedly. Cheap, has
        no audio side effect — safe to run while the bridge is active.
        Measures pure libqi-over-TCP RTT.

audio   calls `ALAudioDevice.sendRemoteBufferToOutput(BATCH_FRAMES,
        zeros)` repeatedly. Identical to the bridge's hot path —
        includes Pepper's audio-stack overhead per call. Only run when
        no conversation is active; the silent zero buffer would
        otherwise mix into a live TTS stream.

Run (from project root)
-----------------------
    # Inside the bridge container so libqi + PEPPER_QI_URL are already wired:
    docker compose -f docker/docker-compose.experiment.yml exec bridge \\
        python /workspace/scripts/pepper_link_probe.py rpc 200

Keepalive A/B
-------------
    # 1. Baseline (radio sleeping between idle bursts):
    docker compose -f docker/docker-compose.experiment.yml exec bridge \\
        python /workspace/scripts/pepper_link_probe.py rpc 200

    # 2. Start a continuous keepalive ping (separate terminal, on the host):
    nohup ping -i 0.1 -q 10.0.0.149 > /dev/null 2>&1 & disown

    # 3. Re-run the probe — radio should now stay awake throughout:
    docker compose -f docker/docker-compose.experiment.yml exec bridge \\
        python /workspace/scripts/pepper_link_probe.py rpc 200

    # 4. Stop the keepalive when done:
    pkill -f "ping -i 0.1 -q 10.0.0.149"

If keepalive helps, you'll see the mean drop, stdev shrink, and the
>50 ms / >100 ms slow-call counts collapse.
"""

from __future__ import annotations

import os
import statistics
import sys
import time

PEPPER_QI_URL = os.environ.get("PEPPER_QI_URL", "tcp://10.0.0.149:9559")
BATCH_FRAMES = int(os.environ.get("BATCH_FRAMES", "800"))
INTER_CALL_MS = float(os.environ.get("INTER_CALL_MS", "20"))  # ~50 Hz

mode = sys.argv[1] if len(sys.argv) > 1 else "rpc"
count = int(sys.argv[2]) if len(sys.argv) > 2 else 200

if mode not in ("rpc", "audio"):
    print(f"unknown mode: {mode!r} (expected rpc|audio)", file=sys.stderr)
    sys.exit(2)

import qi  # noqa: E402  (qi only available inside the bridge container)


def _connect():
    sess = qi.Session()
    sess.connect(PEPPER_QI_URL)
    return sess.service("ALAudioDevice"), sess


def _make_call(audio, mode: str):
    if mode == "rpc":
        return audio.getOutputVolume
    payload = b"\x00" * (BATCH_FRAMES * 4)  # int16 stereo, silent

    def _send():
        audio.sendRemoteBufferToOutput(BATCH_FRAMES, payload)

    return _send


def _percentile(sorted_xs, p: float) -> float:
    if not sorted_xs:
        return 0.0
    idx = min(len(sorted_xs) - 1, int(len(sorted_xs) * p))
    return sorted_xs[idx]


def main() -> int:
    print(f"[probe] connecting to {PEPPER_QI_URL} ...", flush=True)
    audio, _sess = _connect()
    print(
        f"[probe] connected. mode={mode} count={count} "
        f"batch_frames={BATCH_FRAMES} inter_call_ms={INTER_CALL_MS}",
        flush=True,
    )

    call = _make_call(audio, mode)

    # Drop first 5 to skip cold-path / TCP slow-start effects.
    for _ in range(5):
        call()

    times_ms: list[float] = []
    slow_50 = 0
    slow_100 = 0
    slow_300 = 0
    t_wall_start = time.monotonic()
    for _ in range(count):
        t0 = time.monotonic()
        call()
        dt_ms = (time.monotonic() - t0) * 1000.0
        times_ms.append(dt_ms)
        if dt_ms > 50.0:
            slow_50 += 1
        if dt_ms > 100.0:
            slow_100 += 1
        if dt_ms > 300.0:
            slow_300 += 1
        if INTER_CALL_MS > 0:
            time.sleep(INTER_CALL_MS / 1000.0)
    t_wall = time.monotonic() - t_wall_start

    times_ms.sort()
    n = len(times_ms)
    print()
    print(f"=== pepper_link_probe ({mode}) — N={n} over {t_wall:.1f}s ===")
    print(f"  min     {min(times_ms):7.2f} ms")
    print(f"  mean    {statistics.fmean(times_ms):7.2f} ms")
    print(f"  stdev   {statistics.pstdev(times_ms):7.2f} ms   (jitter)")
    print(f"  p50     {_percentile(times_ms, 0.50):7.2f} ms")
    print(f"  p90     {_percentile(times_ms, 0.90):7.2f} ms")
    print(f"  p95     {_percentile(times_ms, 0.95):7.2f} ms")
    print(f"  p99     {_percentile(times_ms, 0.99):7.2f} ms")
    print(f"  max     {max(times_ms):7.2f} ms")
    print(f"  >50ms   {slow_50:4d}  ({100.0 * slow_50 / n:5.1f}%)")
    print(f"  >100ms  {slow_100:4d}  ({100.0 * slow_100 / n:5.1f}%)")
    print(f"  >300ms  {slow_300:4d}  ({100.0 * slow_300 / n:5.1f}%)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
