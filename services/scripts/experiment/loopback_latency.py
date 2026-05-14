#!/usr/bin/env python3
"""Speaker→mic loopback latency probe.

Measures the end-to-end delay from "PCM byte handed to the robot bridge
TCP socket" to "the same audio detected by the USB mic". This isolates
the audio output path (audio-bridge → bridge → NAOqi `sendRemoteBufferToOutput`
→ ALAudioDevice → speaker → air → USB mic → sounddevice) from STT,
LLM, VAD, and the LiveKit pipeline.

Why we care: the experiment-pipeline echo problem could plausibly be
either NAOqi holding ~1 s of audio in its `ALAudioDevice` internal
buffer OR FasterWhisper just taking ~1 s to finalize a transcript.
This probe answers the first question directly without any
interpretation: how long after we push audio does it actually leave
the speaker?

How it works:
  1. Open USB mic via sounddevice InputStream with 10 ms callbacks.
  2. Send a tone burst (default 150 ms @ 1 kHz) over the bridge TCP socket.
  3. Watch the mic callback for the first frame with RMS above threshold.
  4. delta = mic_rms_spike_time − tcp_send_time
  5. Repeat N times and report median / mean / min / max.

The measured delta = NAOqi internal buffer + speaker→mic acoustic
propagation (negligible, <5 ms at typical distances) + USB-mic capture
latency (~30-50 ms, constant across runs). So if delta is ~50 ms, NAOqi
is essentially zero-latency. If delta is ~1500 ms, NAOqi is holding
~1.5 s of audio internally.

Run order:
    # Stop the services that compete for the same resources:
    docker compose -f docker/docker-compose.experiment.yml stop audio-bridge user-client

    # Run the probe:
    uv run python services/scripts/experiment/loopback_latency.py --n 10

    # Restore:
    docker compose -f docker/docker-compose.experiment.yml start audio-bridge user-client

If you need to pick a specific mic device:
    uv run python services/scripts/experiment/loopback_latency.py --list-devices
    uv run python services/scripts/experiment/loopback_latency.py --mic-device 3
"""

from __future__ import annotations

import argparse
import socket
import sys
import time
import threading

import numpy as np
import sounddevice as sd


# ── Defaults ────────────────────────────────────────────────────────
# Mirror the values in services/src/live/config.py + robot/src/config.py
# so we exercise the exact production path.
DEFAULT_PEPPER_HOST = "127.0.0.1"
DEFAULT_PEPPER_PORT = 55555
SEND_SAMPLE_RATE = 16000      # PEPPER_STREAM_RATE — bridge protocol
# USB mics rarely support 16 kHz natively (e.g. DJI MIC MINI is
# 48 kHz only). The capture side is independent of the playback side
# — the tone is 1 kHz, well within Nyquist at any sane rate. We just
# need RMS over short windows, so use the mic's native rate.
MIC_SAMPLE_RATE = 48000
TONE_HZ = 1000
# 1 s is long enough to be plainly audible even with NAOqi's
# initial-buffer fill time, and short enough to fit in NAOqi's
# accept buffer without backpressure. Earlier 150 ms bursts may have
# been too short for NAOqi to actually flush out to the DAC.
TONE_DURATION_S = 1.0
TONE_AMPLITUDE = 0.8          # fraction of int16 full-scale
# Pre-roll silence frames sent right before each tone. NAOqi's
# `sendRemoteBufferToOutput` needs the playback pipeline to be in
# "streaming" mode; if we drop a single isolated burst into an
# otherwise idle device, the very first batch can be eaten on the
# way to the DAC. 500 ms of silence first warms up the stream.
PREROLL_SILENCE_S = 0.5

# 10 ms blocks on the mic side → tight timing granularity on detection.
# The script's measurement floor ≈ MIC_BLOCK_FRAMES / MIC_SAMPLE_RATE +
# sounddevice/OS scheduling jitter (~5-15 ms on Linux).
MIC_BLOCK_FRAMES = 480        # 10 ms at 48 kHz

# RMS above this = "tone detected". The 1 kHz tone at 0.8 amplitude
# arriving at the mic is loud; a quiet room baseline is typically <50.
# Bump down if the room is loud / mic is far; bump up if false positives.
RMS_THRESHOLD = 800.0

# How long we wait for the tone after sending before declaring timeout.
# Sized to comfortably exceed any plausible NAOqi buffer.
DETECTION_TIMEOUT_S = 4.0

# Inter-trial pause so the room goes quiet between bursts.
INTER_TRIAL_PAUSE_S = 2.0


def generate_tone_pcm(rate: int, freq: float, duration_s: float, amplitude: float) -> bytes:
    """Build a mono 16-bit PCM sine burst."""
    n = int(rate * duration_s)
    t = np.arange(n) / rate
    # Soft envelope (5 ms ramps) to avoid click artefacts at burst edges.
    envelope = np.ones(n)
    ramp = max(1, int(rate * 0.005))
    envelope[:ramp] = np.linspace(0, 1, ramp)
    envelope[-ramp:] = np.linspace(1, 0, ramp)
    samples = amplitude * envelope * np.sin(2 * np.pi * freq * t)
    int16 = (samples * 32767).astype(np.int16)
    return int16.tobytes()


def connect_bridge(host: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.connect((host, port))
    return sock


def run_one_trial(sock: socket.socket, mic_device, tone_pcm: bytes,
                  rms_threshold: float, verbose: bool) -> dict:
    """One send-and-detect cycle. Returns timing dict (or None values on timeout).

    The mic stream is opened fresh per trial so the InputStream's internal
    queue starts empty — otherwise stale frames from before send_ts could
    fool the detector.
    """
    captured_rms: list[tuple[float, float]] = []   # (monotonic_ts, rms)
    detection_ts = [None]                          # mutable holder
    pre_send_baseline = [0.0]                      # noise floor at trial start

    detection_evt = threading.Event()

    def callback(indata, frames, time_info, status):
        if status:
            # InputOverflow / etc. — just log, keep going.
            print(f"[probe] sd status: {status}", file=sys.stderr)
        t_now = time.monotonic()
        flat = indata.reshape(-1).astype(np.float64)
        rms = float(np.sqrt(np.mean(flat * flat))) if flat.size else 0.0
        captured_rms.append((t_now, rms))
        if rms > rms_threshold and detection_ts[0] is None:
            detection_ts[0] = t_now
            detection_evt.set()

    stream = sd.InputStream(
        device=mic_device,
        samplerate=MIC_SAMPLE_RATE,
        channels=1,
        dtype="int16",
        blocksize=MIC_BLOCK_FRAMES,
        latency="low",
        callback=callback,
    )

    with stream:
        # Let the stream stabilise and measure the noise floor for a half-second.
        time.sleep(0.5)
        if captured_rms:
            noise = max(r for _, r in captured_rms)
            pre_send_baseline[0] = noise
            if noise > rms_threshold * 0.5:
                print(
                    f"[probe] WARNING noise floor RMS={noise:.0f} is close to "
                    f"threshold {rms_threshold:.0f} — false positives likely. "
                    f"Quiet the room or lower TONE_AMPLITUDE in source.",
                    file=sys.stderr,
                )

        # Reset detection state so the noise-floor sample doesn't trip it.
        captured_rms.clear()
        detection_ts[0] = None
        detection_evt.clear()

        # Build one combined chunk: [pre-roll silence] [tone] [post-roll silence].
        # The pre-roll warms up NAOqi's playback pipeline so the tone
        # doesn't get eaten on a cold buffer; the post-roll keeps the
        # stream busy past the tone so NAOqi doesn't underrun before
        # the tone finishes draining out the DAC.
        # We measure `detection_time - t_send` then subtract the known
        # pre-roll duration to isolate the NAOqi buffer + acoustic +
        # mic-capture latency.
        preroll_frames = int(SEND_SAMPLE_RATE * PREROLL_SILENCE_S)
        postroll_frames = int(SEND_SAMPLE_RATE * PREROLL_SILENCE_S)
        silence_frame = b"\x00\x00"
        payload = (
            silence_frame * preroll_frames
            + tone_pcm
            + silence_frame * postroll_frames
        )
        size_bytes = len(payload).to_bytes(4, "big")
        t_send = time.monotonic()
        sock.sendall(size_bytes + payload)

        detected = detection_evt.wait(timeout=DETECTION_TIMEOUT_S)

    delta_ms = None
    rms_at_detect = None
    if detected:
        raw_delta_ms = (detection_ts[0] - t_send) * 1000.0
        # Subtract the known pre-roll playback time so the reported
        # delta is "byte → audible", not "byte → audible-via-preroll".
        delta_ms = raw_delta_ms - PREROLL_SILENCE_S * 1000.0
        # Find the RMS of the detecting frame.
        for ts, rms in captured_rms:
            if ts == detection_ts[0]:
                rms_at_detect = rms
                break

    result = {
        "delta_ms": delta_ms,
        "rms_at_detect": rms_at_detect,
        "noise_floor": pre_send_baseline[0],
        "frames_captured": len(captured_rms),
    }

    if verbose and detected:
        # Dump the RMS curve around detection for sanity check.
        send_relative = [(ts - t_send, rms) for ts, rms in captured_rms]
        print(f"[probe]   RMS profile (first 60 frames, every 10 ms):")
        for t_rel, rms in send_relative[:60]:
            bar = "#" * min(40, int(rms / 100))
            marker = " <-- detect" if abs(t_rel * 1000 - delta_ms) < 11 else ""
            print(f"          t={t_rel * 1000:+7.1f}ms  rms={rms:6.0f}  {bar}{marker}")

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--host", default=DEFAULT_PEPPER_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PEPPER_PORT)
    parser.add_argument("--mic-device",
                        help="sounddevice input device (index or name substring). "
                             "Use --list-devices to see what's available.")
    parser.add_argument("--n", type=int, default=10,
                        help="Number of trials (default 10).")
    parser.add_argument("--rms-threshold", type=float, default=RMS_THRESHOLD,
                        help=f"RMS above which we declare 'tone detected' "
                             f"(default {RMS_THRESHOLD}).")
    parser.add_argument("--list-devices", action="store_true",
                        help="Print sounddevice device table and exit.")
    parser.add_argument("--verbose", action="store_true",
                        help="Print per-trial RMS curve for inspection.")
    args = parser.parse_args()

    if args.list_devices:
        print(sd.query_devices())
        return 0

    # Resolve mic device (int index or name substring → index).
    mic_device = None
    if args.mic_device:
        try:
            mic_device = int(args.mic_device)
        except ValueError:
            mic_device = args.mic_device

    tone_pcm = generate_tone_pcm(SEND_SAMPLE_RATE, TONE_HZ, TONE_DURATION_S, TONE_AMPLITUDE)
    print(f"[probe] tone: {TONE_DURATION_S * 1000:.0f} ms @ {TONE_HZ} Hz "
          f"({len(tone_pcm)} bytes, {len(tone_pcm) // 2} samples)")
    print(f"[probe] rms_threshold={args.rms_threshold}")

    print(f"[probe] connecting to bridge {args.host}:{args.port}...")
    try:
        sock = connect_bridge(args.host, args.port)
    except OSError as exc:
        print(f"[probe] ERROR: could not connect to bridge: {exc}", file=sys.stderr)
        print("[probe] Is the bridge container up? Is audio-bridge still "
              "holding the TCP socket?", file=sys.stderr)
        return 2
    print("[probe] connected.")

    deltas_ms: list[float] = []
    timeouts = 0
    try:
        for i in range(args.n):
            print(f"[probe] trial {i + 1}/{args.n}...")
            result = run_one_trial(
                sock=sock,
                mic_device=mic_device,
                tone_pcm=tone_pcm,
                rms_threshold=args.rms_threshold,
                verbose=args.verbose,
            )
            if result["delta_ms"] is None:
                timeouts += 1
                print(f"  TIMEOUT after {DETECTION_TIMEOUT_S:.1f}s "
                      f"(noise_floor={result['noise_floor']:.0f}, "
                      f"frames_captured={result['frames_captured']})")
            else:
                print(f"  delta={result['delta_ms']:.1f}ms "
                      f"rms_at_detect={result['rms_at_detect']:.0f} "
                      f"noise_floor={result['noise_floor']:.0f}")
                deltas_ms.append(result["delta_ms"])
            time.sleep(INTER_TRIAL_PAUSE_S)
    finally:
        sock.close()

    print()
    if deltas_ms:
        arr = np.array(deltas_ms)
        print(f"[probe] === SUMMARY (n={len(deltas_ms)} successful, "
              f"{timeouts} timed out) ===")
        print(f"          median = {np.median(arr):7.1f} ms")
        print(f"          mean   = {np.mean(arr):7.1f} ms")
        print(f"          stdev  = {np.std(arr):7.1f} ms")
        print(f"          min    = {np.min(arr):7.1f} ms")
        print(f"          max    = {np.max(arr):7.1f} ms")
        print()
        print("[probe] Interpretation:")
        print("  ≤  150 ms : NAOqi internal buffer is basically zero — echo gap")
        print("              we observed is mostly STT finalization. Look elsewhere.")
        print("  150-500 ms: Small NAOqi buffer. Worth shrinking via setParameter")
        print("              if we can find the knob; otherwise post-STT filter.")
        print("  ≥ 1000 ms : NAOqi is holding ≥1 s of audio internally. This is")
        print("              the buffer floor — confirms the speaker_drained gap")
        print("              theory. Hunt for ALAudioDevice's buffer knob.")
        return 0
    else:
        print(f"[probe] ALL TRIALS TIMED OUT — diagnose:")
        print("  - Is Pepper powered on and connected to the bridge?")
        print("  - Is the USB mic close enough to Pepper's speaker?")
        print("  - Try --verbose to see the RMS curve and adjust --rms-threshold")
        print("  - Run --list-devices and pick the right --mic-device")
        return 1


if __name__ == "__main__":
    sys.exit(main())
