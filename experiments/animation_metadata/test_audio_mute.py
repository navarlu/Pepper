"""Audio-mute sanity test for the animation-recording setup.

Verifies the two audio paths stay independent:
  1. "TTS" — hello.wav looped into Pepper's PulseAudio through the exact
     same ssh+paplay pipe `services/src/live/audio_bridge.py` uses.
     This must stay audible the whole time.
  2. Animation sound — a sound-carrying behavior triggered via the bridge's
     POST /animation/<name>?wait=1. The bridge mutes ALAudioPlayer around
     every animation, so the gesture must be silent.

Expected result: you hear the wav looping for the whole run, the robot
performs the gesture, and the animation makes NO sound. If you hear the
animation's sound over the speech, the ALAudioPlayer mute does not cover
that behavior's audio path and we need a different mute strategy.

Run (on the RPi, host-side):  python3 experiments/animation_metadata/test_audio_mute.py
"""

import audioop
import os
import subprocess
import threading
import time
import wave

import urllib.request

# --- Configuration ---------------------------------------------------------
BRIDGE_URL = os.environ.get("PEPPER_BRIDGE_URL", "http://localhost:5000")
ANIMATION_NAME = "Angry_1"  # sound-carrying animation to trigger mid-playback
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WAV_PATH = os.path.join(SCRIPT_DIR, "..", "..", "robot", "data", "hello.wav")
PEPPER_SSH_HOST = os.environ.get("PEPPER_SSH_HOST", "10.42.0.205")
PEPPER_SSH_USER = os.environ.get("PEPPER_SSH_USER", "nao")
PEPPER_SSH_PASSWORD = os.environ.get("PEPPER_SSH_PASSWORD", "Argus")
STREAM_RATE = 16000         # matches PEPPER_STREAM_RATE default
LOOP_GAP_SEC = 0.5          # silence between wav repetitions
TONE_LEAD_SEC = 3.0         # audio plays this long before the animation fires
TONE_TAIL_SEC = 3.0         # audio keeps playing this long after it finishes
TONE_TOTAL_SEC = 30.0       # looped audio length (stopped early via stdin close)
TRIGGER_TIMEOUT_SEC = 30.0
# ---------------------------------------------------------------------------


def build_wav_pcm():
    """hello.wav as mono s16le at STREAM_RATE, looped with short gaps to
    TONE_TOTAL_SEC. Resampled with audioop.ratecv because the wav is 24 kHz
    while the real TTS pipe runs at 16 kHz."""
    with wave.open(WAV_PATH, "rb") as w:
        channels = w.getnchannels()
        width = w.getsampwidth()
        rate = w.getframerate()
        # The header's frame count is bogus (streamed wav) — readframes in
        # chunks until EOF instead of trusting getnframes().
        chunks = []
        while True:
            data = w.readframes(STREAM_RATE)
            if not data:
                break
            chunks.append(data)
    pcm = b"".join(chunks)
    if width != 2:
        pcm = audioop.lin2lin(pcm, width, 2)
    if channels == 2:
        pcm = audioop.tomono(pcm, 2, 0.5, 0.5)
    if rate != STREAM_RATE:
        pcm, _ = audioop.ratecv(pcm, 2, 1, rate, STREAM_RATE, None)
    clip_sec = len(pcm) / 2.0 / STREAM_RATE
    print("[audio] hello.wav: %.2fs, %d Hz -> %d Hz" % (clip_sec, rate, STREAM_RATE))
    gap = b"\x00" * int(LOOP_GAP_SEC * STREAM_RATE) * 2
    repeats = max(1, int(TONE_TOTAL_SEC / (clip_sec + LOOP_GAP_SEC)) + 1)
    return (pcm + gap) * repeats


def build_ssh_cmd():
    """Same command shape as audio_bridge._build_ssh_cmd()."""
    paplay_remote = (
        "paplay --raw --format=s16le --rate=%d --channels=1 --latency-msec=30"
        % STREAM_RATE
    )
    ssh_opts = [
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
    ]
    target = "%s@%s" % (PEPPER_SSH_USER, PEPPER_SSH_HOST)
    if PEPPER_SSH_PASSWORD:
        return ["sshpass", "-p", PEPPER_SSH_PASSWORD, "ssh"] + ssh_opts + [target, paplay_remote]
    return ["ssh"] + ssh_opts + [target, paplay_remote]


def feed_pcm(proc, pcm, stop_event):
    """Write PCM into paplay's stdin; the pipe applies real-time backpressure."""
    chunk = STREAM_RATE * 2 // 10  # 100 ms of s16le mono
    try:
        for offset in range(0, len(pcm), chunk):
            if stop_event.is_set():
                break
            proc.stdin.write(pcm[offset:offset + chunk])
            proc.stdin.flush()
    except (BrokenPipeError, OSError) as exc:
        print("[tone] paplay pipe closed early: %s" % exc)
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass


def trigger_animation(name):
    url = "%s/animation/%s?wait=1&mute=1" % (BRIDGE_URL, name)
    print("[test] triggering %s (blocking)..." % name)
    started = time.time()
    request = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(request, timeout=TRIGGER_TIMEOUT_SEC) as response:
        body = response.read().decode("utf-8", "replace")
        print("[test] bridge HTTP %d after %.2fs: %s"
              % (response.status, time.time() - started, body[:200]))


def main():
    print("[test] expected: hello.wav looping audibly, silent gesture (%s)" % ANIMATION_NAME)
    pcm = build_wav_pcm()

    cmd = build_ssh_cmd()
    print("[test] starting ssh+paplay: %s" % " ".join(
        c if c != PEPPER_SSH_PASSWORD else "***" for c in cmd))
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE)

    stop_event = threading.Event()
    feeder = threading.Thread(target=feed_pcm, args=(proc, pcm, stop_event))
    feeder.start()

    try:
        print("[test] lead-in %.1fs — you should hear the wav NOW" % TONE_LEAD_SEC)
        time.sleep(TONE_LEAD_SEC)
        trigger_animation(ANIMATION_NAME)
        print("[test] tail %.1fs — the wav should still be playing" % TONE_TAIL_SEC)
        time.sleep(TONE_TAIL_SEC)
    finally:
        stop_event.set()
        feeder.join(5.0)
        try:
            proc.wait(10.0)
        except subprocess.TimeoutExpired:
            proc.kill()
    print("[test] done. Heard the wav the whole time + silent gesture? Then mute works.")


if __name__ == "__main__":
    main()
