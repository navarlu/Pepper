"""Record every Pepper animation on video for the gesture-metadata study.

For each entry in robot/data/animations.json: start recording, trigger the
animation via the bridge's POST /animation/<name> endpoint, keep recording
for a fixed window, save the clip as <name>.mp4. Skips clips that already
exist, so the session can be interrupted and resumed. A manifest json logs
per-clip stats and the bridge response.

Run:  uv run python experiments/animation_metadata/record_animations.py
"""

import json
import os
import threading
import time

import requests

from realsense_camera import RealSenseRecorder

# --- Configuration ---------------------------------------------------------
BRIDGE_URL = os.environ.get("PEPPER_BRIDGE_URL", "http://localhost:5000")
RECORD_SEC = 12.0        # fixed capture window per animation (trimmed later)
TRIGGER_DELAY_SEC = 0.5  # pre-roll before the animation is triggered
SETTLE_SEC = 2.0         # pause between animations (robot returns to rest)
LIMIT = None             # int -> record only the first N animations (for testing)
ONLY = None              # list of names -> record only these (for testing)
SKIP_PREFIXES = ("animations/LED/",)  # LED-only entries, nothing to film

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


def trigger_animation(name, result_holder):
    url = "%s/animation/%s" % (BRIDGE_URL, name)
    try:
        response = requests.post(url, timeout=10)
        result_holder["status"] = response.status_code
        result_holder["body"] = response.text[:200]
        print("[trigger] %s -> HTTP %d" % (name, response.status_code))
    except Exception as exc:
        result_holder["status"] = None
        result_holder["error"] = str(exc)
        print("[trigger] %s FAILED: %s" % (name, exc))


def main():
    with open(ANIMATIONS_JSON, "r", encoding="utf-8") as f:
        animations = json.load(f)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    manifest = load_manifest()

    names = list(animations.keys())
    if ONLY is not None:
        names = [n for n in names if n in ONLY]
    names = [n for n in names if not animations[n].startswith(SKIP_PREFIXES)]
    if LIMIT is not None:
        names = names[:LIMIT]

    todo = [n for n in names if not os.path.exists(os.path.join(OUTPUT_DIR, n + ".mp4"))]
    print("[rig] %d animations total, %d already recorded, %d to do"
          % (len(names), len(names) - len(todo), len(todo)))
    if not todo:
        print("[rig] nothing to do")
        return

    recorder = RealSenseRecorder()
    recorder.start()
    try:
        for index, name in enumerate(todo, 1):
            clip_path = os.path.join(OUTPUT_DIR, name + ".mp4")
            print("[rig] (%d/%d) %s -> %s" % (index, len(todo), name, animations[name]))

            trigger_result = {}
            trigger_thread = threading.Timer(
                TRIGGER_DELAY_SEC, trigger_animation, args=(name, trigger_result)
            )
            trigger_thread.start()
            stats = recorder.record_clip(clip_path, RECORD_SEC)
            trigger_thread.join()

            if trigger_result.get("status") != 200:
                # Keep no clip for a failed trigger so a re-run retries it.
                os.remove(clip_path)
                print("[rig] trigger failed, clip discarded — will retry on next run")

            manifest[name] = {
                "behavior": animations[name],
                "file": os.path.relpath(clip_path, SCRIPT_DIR),
                "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "trigger_delay_sec": TRIGGER_DELAY_SEC,
                "trigger": trigger_result,
                "capture": stats,
            }
            save_manifest(manifest)
            time.sleep(SETTLE_SEC)
    finally:
        recorder.stop()

    print("[rig] session done, manifest: %s" % MANIFEST_PATH)


if __name__ == "__main__":
    main()
