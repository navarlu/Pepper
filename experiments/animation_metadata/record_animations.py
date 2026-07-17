"""Record every Pepper animation on video for the gesture-metadata study.

For each entry in robot/data/animations.json: start recording, trigger the
animation via the bridge's blocking POST /animation/<name>?wait=1 endpoint
(which only returns once Pepper has finished the gesture), then stop the
clip a short tail after completion. This trims every clip to the real
gesture length — small files that upload cleanly to the VLM and carry a
truthful duration straight from the robot, instead of a fixed padded window.

Session hygiene (applied automatically):
  - POST /motion/recording once at start: autonomous-life stillness profile
    (BasicAwareness / BackgroundMovement / ListeningMovement /
    SpeakingMovement all off) so the head never moves on its own.
  - Session-wide animation-sound mute: every sound file in the animations
    package on the robot is renamed to *.muted at start (single ssh call,
    via mute_animation_sounds.py) and restored at the end. This is what
    keeps the gestures silent — see ANIMATION_SOUND_MUTING.md.
  - POST /audio/volume {SESSION_VOLUME} at start, restored on exit: low
    speaker volume so the per-clip start chime stays quiet. (Clips carry no
    audio track, so room sound never ends up in the videos.)
  - Every clip is neutral -> gesture -> neutral: a blocking goToPosture
    runs BEFORE recording starts (reset not in frame), and a second one
    runs at the end of the clip WHILE still recording, so the video shows
    the full return to the base pose.
  - A soft two-note chime plays on Pepper (ssh+paplay) as each clip starts,
    as an audible "still running" cue. Disable with CHIME_ENABLED = False.
  - Live preview: while the rig runs, the current camera image is served
    as MJPEG at http://<rpi>:PREVIEW_PORT — same frames the recorder sees.

Make sure loop_launcher / the live experiment is NOT running — an
experiment_active transition would make the bridge re-apply the wake
profile and re-enable BasicAwareness mid-session.

Skips clips that already exist, so the session can be interrupted and
resumed. A manifest json logs per-clip stats and the bridge response.

Run:  uv run python experiments/animation_metadata/record_animations.py
"""

import json
import math
import os
import struct
import subprocess
import threading
import time

import requests

from camera_preview import start_preview_server
from mute_animation_sounds import (
    APPS_DIR as PEPPER_ANIMATIONS_DIR,
    PEPPER_SSH_HOST,
    PEPPER_SSH_PASSWORD,
    PEPPER_SSH_USER,
    disable as disable_all_animation_sounds,
    restore as restore_all_animation_sounds,
    ssh_run,
)
from realsense_camera import RealSenseRecorder

# --- Configuration ---------------------------------------------------------
BRIDGE_URL = os.environ.get("PEPPER_BRIDGE_URL", "http://localhost:5000")
PRE_ROLL_SEC = 0.5          # record this long before triggering the animation
TAIL_SEC = 0.7              # keep recording after the final neutral reset
MAX_RECORD_SEC = 30.0       # safety cap: gesture + recorded neutral return
SETTLE_SEC = 1.5            # pause between animations (robot returns to rest)
TRIGGER_TIMEOUT_SEC = 30.0  # blocking trigger waits out the whole gesture
NEUTRAL_POSTURE = "Stand"   # ALRobotPosture base pose (before + end of clip)
POSTURE_SPEED = 0.5
POSTURE_TIMEOUT_SEC = 20.0  # goToPosture is blocking on the bridge side
HEAD_PITCH_RAD = 0.2     # applied after each posture reset; negative = up
                            # (Stand's own head pitch looks slightly down on
                            # camera; Pepper's HeadPitch range is -0.71..0.64)
HEAD_SPEED = 0.2
LIMIT = 400                 # int -> record only the first N animations (for testing)
ONLY = None                 # list of names -> record only these (for testing)
SKIP_PREFIXES = ("animations/LED/",)  # LED-only entries, nothing to film
# Behaviors that loop forever until explicitly stopped — the blocking
# ?wait=1 trigger never returns for them, so they must not be recorded.
SKIP_NAME_SUBSTRINGS = ("loop",)
PREVIEW_PORT = 8089         # live MJPEG preview while recording (None = off)
# Thermal guard: joints checked before every clip. Above PAUSE the session
# waits (head motor unloaded to cool faster) until all are below RESUME.
# Pepper motors start cutting torque ~75-80 C — a drooping head mid-session
# would silently ruin clips (commands still return ok!).
THERMAL_JOINTS = ("HeadPitch", "HeadYaw", "LShoulderPitch", "RShoulderPitch")
THERMAL_PAUSE_C = 72
THERMAL_RESUME_C = 62
THERMAL_POLL_SEC = 30.0
SESSION_VOLUME = 40         # low speaker volume: chime audible, not loud
CHIME_ENABLED = True        # soft two-note cue on Pepper at each clip start
CHIME_RATE = 16000

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ANIMATIONS_JSON = os.path.join(SCRIPT_DIR, "..", "..", "robot", "data", "animations.json")
OUTPUT_DIR = os.path.join(SCRIPT_DIR, "data", "recordings")
MANIFEST_PATH = os.path.join(SCRIPT_DIR, "data", "recordings_manifest.json")
# ---------------------------------------------------------------------------


def load_manifest():
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_manifest(manifest):
    with open(MANIFEST_PATH, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, sort_keys=True)


def build_chime_pcm():
    """Soft two-note (E5 -> A5) chime, mono s16le, ~0.45 s with gentle
    attack/release so it never clicks or startles."""
    notes = ((659.3, 0.18), (880.0, 0.24))
    frames = []
    for freq, duration in notes:
        count = int(duration * CHIME_RATE)
        attack = int(0.02 * CHIME_RATE)
        release = int(0.06 * CHIME_RATE)
        for i in range(count):
            envelope = min(1.0, i / float(attack), (count - i) / float(release))
            sample = 0.22 * envelope * math.sin(2.0 * math.pi * freq * i / CHIME_RATE)
            frames.append(struct.pack("<h", int(sample * 32767)))
    return b"".join(frames)


_CHIME_PCM = build_chime_pcm() if CHIME_ENABLED else b""


def play_chime():
    """Fire-and-forget the chime to Pepper's speaker via ssh+paplay (the
    same transport the TTS uses). Non-blocking: plays during the pre-roll;
    clips have no audio track, so it can never end up in a video."""
    if not CHIME_ENABLED:
        return
    cmd = [
        "sshpass", "-p", PEPPER_SSH_PASSWORD,
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=5",
        "%s@%s" % (PEPPER_SSH_USER, PEPPER_SSH_HOST),
        "paplay --raw --format=s16le --rate=%d --channels=1" % CHIME_RATE,
    ]

    def _feed():
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            proc.stdin.write(_CHIME_PCM)
            proc.stdin.close()
            proc.wait(timeout=10.0)
        except Exception as exc:
            print("[rig] chime failed (harmless): %s" % exc)

    threading.Thread(target=_feed, daemon=True).start()


def read_joint_temps():
    """Read the THERMAL_JOINTS motor temperatures (deg C) in one ssh call.
    Returns {joint: temp}; missing joints are simply absent on error."""
    remote = " ; ".join(
        'qicli call ALMemory.getData "Device/SubDeviceList/%s/Temperature/Sensor/Value"' % j
        for j in THERMAL_JOINTS
    )
    try:
        code, out, err = ssh_run(remote, timeout=20.0)
    except Exception as exc:
        print("[rig] temp read failed: %s" % exc)
        return {}
    temps = {}
    values = []
    for line in out.splitlines():
        line = line.strip()
        try:
            values.append(float(line))
        except ValueError:
            continue
    for joint, value in zip(THERMAL_JOINTS, values):
        temps[joint] = value
    return temps


def thermal_guard():
    """Pause the session while any monitored motor is too hot.

    While paused, the head motor is unstiffened so it cools instead of
    holding the head up the whole time; stiffness is restored on resume and
    the following go_neutral() re-poses everything.
    """
    temps = read_joint_temps()
    if not temps:
        return
    hottest_joint = max(temps, key=temps.get)
    if temps[hottest_joint] < THERMAL_PAUSE_C:
        return
    print("[rig] THERMAL PAUSE: %s at %.0f C (limit %d) — cooling, head unloaded"
          % (hottest_joint, temps[hottest_joint], THERMAL_PAUSE_C))
    ssh_run('qicli call ALMotion.setStiffnesses "Head" 0.0', timeout=15.0)
    try:
        while True:
            time.sleep(THERMAL_POLL_SEC)
            temps = read_joint_temps()
            if not temps:
                continue
            hottest_joint = max(temps, key=temps.get)
            print("[rig] cooling... hottest: %s %.0f C (resume below %d)"
                  % (hottest_joint, temps[hottest_joint], THERMAL_RESUME_C))
            if temps[hottest_joint] <= THERMAL_RESUME_C:
                break
    finally:
        ssh_run('qicli call ALMotion.setStiffnesses "Head" 1.0', timeout=15.0)
    print("[rig] thermal pause over — resuming")


def bridge_post(path, payload=None, timeout=10.0):
    """POST a JSON payload to the bridge; returns the parsed body (or a
    synthetic error dict) plus the HTTP status. Never raises."""
    url = BRIDGE_URL + path
    try:
        response = requests.post(url, json=payload or {}, timeout=timeout)
        try:
            body = response.json()
        except Exception:
            body = {"raw": response.text[:200]}
        return response.status_code, body
    except Exception as exc:
        return None, {"error": str(exc)}


def wait_for_pepper():
    """Block until the bridge reports a LIVE Pepper connection (/health does
    a real qi probe). Used at session start and after any trigger failure —
    on a network drop the bridge self-restarts (watchdog) and reconnects
    once the robot is reachable, and this waits that out."""
    announced = False
    while True:
        try:
            response = requests.get(BRIDGE_URL + "/health", timeout=5.0)
            health = response.json()
            if health.get("pepper_connected"):
                if announced:
                    print("[rig] Pepper is back — continuing")
                return
        except Exception:
            pass
        if not announced:
            print("[rig] waiting for Pepper/bridge connection...")
            announced = True
        time.sleep(5.0)


def setup_session():
    """Apply the stillness profile and hard-mute the speaker.

    Returns the pre-session output volume (so teardown can restore it),
    or None if the mute call failed. Aborts the run if the stillness
    profile cannot be applied — recordings with autonomous head motion
    would be useless for the VLM analysis.
    """
    wait_for_pepper()
    # Generous timeout: the recording profile runs motion.wakeUp(), which can
    # take tens of seconds when the robot starts from rest.
    status, body = bridge_post("/motion/recording", timeout=90.0)
    if status != 200:
        raise RuntimeError(
            "failed to apply recording stillness profile: HTTP %s %s" % (status, body)
        )
    print("[rig] stillness profile applied (autonomous movement off)")

    # Session-wide sound-file mute: rename every animation sound file on the
    # robot once (X.ogg -> X.ogg.muted) instead of paying 2 ssh round-trips
    # per clip via the bridge's ?sound=off. Restored in teardown_session().
    print("[rig] disabling all animation sound files on the robot...")
    disable_all_animation_sounds(PEPPER_ANIMATIONS_DIR)

    # Low (not zero) volume: gestures stay silent thanks to the sound-file
    # mute above, while the per-clip chime remains audible but quiet.
    status, body = bridge_post("/audio/volume", {"volume": SESSION_VOLUME})
    if status != 200:
        print("[rig] WARNING: could not set session volume (HTTP %s %s)" % (status, body))
        return None
    previous_volume = body.get("previous")
    print("[rig] speaker volume set to %s for the session (was %s)"
          % (SESSION_VOLUME, previous_volume))
    return previous_volume


def teardown_session(previous_volume):
    """Restore sound files + speaker volume and put Pepper back to sleep."""
    print("[rig] restoring animation sound files on the robot...")
    try:
        restore_all_animation_sounds(PEPPER_ANIMATIONS_DIR)
    except Exception as exc:
        print("[rig] WARNING: sound-file restore failed: %s — "
              "run mute_animation_sounds.py with MODE='restore' manually" % exc)
    if previous_volume is not None:
        status, body = bridge_post("/audio/volume", {"volume": previous_volume})
        if status == 200:
            print("[rig] speaker volume restored to %s" % previous_volume)
        else:
            print("[rig] WARNING: volume restore failed: HTTP %s %s" % (status, body))
    status, body = bridge_post("/motion/sleep")
    print("[rig] sleep profile re-applied (HTTP %s)" % status)


def go_neutral():
    """Blocking reset to the neutral base pose; returns the bridge response
    for the manifest. Logged but non-fatal — one bad reset should not kill
    a 400-clip session."""
    status, body = bridge_post(
        "/motion/posture",
        {"posture": NEUTRAL_POSTURE, "speed": POSTURE_SPEED},
        timeout=POSTURE_TIMEOUT_SEC,
    )
    if status != 200 or not body.get("reached", True):
        print("[rig] WARNING: neutral reset imperfect: HTTP %s %s" % (status, body))
    body["status"] = status
    return body


def trigger_animation(name, result_holder):
    """POST /animation/<name>?wait=1 — blocks until Pepper finishes the
    gesture, then records the bridge's status, body and measured elapsed."""
    url = "%s/animation/%s?wait=1" % (BRIDGE_URL, name)
    try:
        response = requests.post(url, timeout=TRIGGER_TIMEOUT_SEC)
        result_holder["status"] = response.status_code
        result_holder["body"] = response.text[:200]
        try:
            result_holder["elapsed_s"] = response.json().get("elapsed_s")
        except Exception:
            result_holder["elapsed_s"] = None
        print("[trigger] %s -> HTTP %d (gesture %.2fs)"
              % (name, response.status_code, result_holder.get("elapsed_s") or -1))
    except Exception as exc:
        result_holder["status"] = None
        result_holder["error"] = str(exc)
        print("[trigger] %s FAILED: %s" % (name, exc))


def _record_worker(recorder, clip_path, stop_event, stats_holder):
    stats_holder["stats"] = recorder.record_until(
        clip_path, stop_event, MAX_RECORD_SEC, min_sec=PRE_ROLL_SEC
    )


def main():
    with open(ANIMATIONS_JSON, "r", encoding="utf-8") as f:
        animations = json.load(f)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    manifest = load_manifest()

    names = list(animations.keys())
    if ONLY is not None:
        names = [n for n in names if n in ONLY]
    names = [n for n in names if not animations[n].startswith(SKIP_PREFIXES)]
    names = [
        n for n in names
        if not any(s in n.lower() or s in animations[n].lower()
                   for s in SKIP_NAME_SUBSTRINGS)
    ]
    if LIMIT is not None:
        names = names[:LIMIT]

    todo = [n for n in names if not os.path.exists(os.path.join(OUTPUT_DIR, n + ".mp4"))]
    print("[rig] %d animations total, %d already recorded, %d to do"
          % (len(names), len(names) - len(todo), len(todo)))
    if not todo:
        print("[rig] nothing to do")
        return

    previous_volume = setup_session()
    recorder = RealSenseRecorder()
    recorder.start()
    preview_server = None
    if PREVIEW_PORT:
        preview_server = start_preview_server(recorder.latest_jpeg, PREVIEW_PORT)
    try:
        for index, name in enumerate(todo, 1):
            clip_path = os.path.join(OUTPUT_DIR, name + ".mp4")
            print("[rig] (%d/%d) %s -> %s" % (index, len(todo), name, animations[name]))

            # Wait out any motor overheating before investing in a clip —
            # an overheated HeadPitch droops silently and ruins the video.
            thermal_guard()

            # Blocking reset to the neutral base pose *before* the clip
            # starts, so every recording opens from the identical posture
            # and the reset motion itself is never in frame.
            posture_pre = go_neutral()

            stop_event = threading.Event()
            stats_holder = {}
            rec_thread = threading.Thread(
                target=_record_worker,
                args=(recorder, clip_path, stop_event, stats_holder),
            )
            rec_thread.start()
            play_chime()                      # audible "clip starting" cue

            time.sleep(PRE_ROLL_SEC)          # pre-roll before the gesture starts
            trigger_result = {}
            trigger_animation(name, trigger_result)  # blocks until Pepper is done
            # Second blocking neutral reset WHILE still recording, so the
            # clip ends with Pepper visibly back at the base pose.
            posture_end = go_neutral()
            time.sleep(TAIL_SEC)
            stop_event.set()
            rec_thread.join()
            stats = stats_holder.get("stats")

            if trigger_result.get("status") != 200:
                # Keep no clip for a failed trigger so a re-run retries it.
                if os.path.exists(clip_path):
                    os.remove(clip_path)
                print("[rig] trigger failed, clip discarded — will retry on next run")
                # Wait for a live robot connection before touching the next
                # clip (a network drop means the bridge is restarting), then
                # stop any behavior the failed trigger may have left running
                # and re-apply the stillness profile the restart wiped out.
                wait_for_pepper()
                status, body = bridge_post("/behaviors/stop")
                print("[rig] emergency behavior stop: HTTP %s %s" % (status, body))
                status, body = bridge_post("/motion/recording", timeout=90.0)
                print("[rig] stillness profile re-applied: HTTP %s" % status)

            manifest[name] = {
                "behavior": animations[name],
                "file": os.path.relpath(clip_path, SCRIPT_DIR),
                "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "pre_roll_sec": PRE_ROLL_SEC,
                "tail_sec": TAIL_SEC,
                "neutral_reset_pre": posture_pre,
                "neutral_reset_end": posture_end,
                "trigger": trigger_result,
                "capture": stats,
            }
            save_manifest(manifest)
            time.sleep(SETTLE_SEC)
    finally:
        if preview_server is not None:
            preview_server.shutdown()
        recorder.stop()
        teardown_session(previous_volume)

    print("[rig] session done, manifest: %s" % MANIFEST_PATH)


if __name__ == "__main__":
    main()
