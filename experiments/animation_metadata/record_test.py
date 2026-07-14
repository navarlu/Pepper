"""Smoke test: record a single short clip from the RealSense camera.

Run:  uv run python experiments/animation_metadata/record_test.py
"""

import os
from datetime import datetime

from realsense_camera import RealSenseRecorder

DURATION_SEC = 5.0
OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "recordings")


def main():
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    output_path = os.path.join(
        OUTPUT_DIR, "camera_test_%s.mp4" % datetime.now().strftime("%Y%m%d_%H%M%S")
    )

    recorder = RealSenseRecorder()
    recorder.start()
    try:
        print("[test] recording %.1f s..." % DURATION_SEC)
        stats = recorder.record_clip(output_path, DURATION_SEC)
    finally:
        recorder.stop()

    print("[test] stats: %s" % stats)
    print("[test] saved: %s" % output_path)


if __name__ == "__main__":
    main()
