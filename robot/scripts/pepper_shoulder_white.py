#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Force Pepper's shoulder (ChestLeds) LEDs to a solid color.

Autonomous life keeps repainting the chest LEDs (e.g. red blinking when
something is wrong, blue while booting), so we have to keep asserting
the color in a loop. Stop with Ctrl-C — the script then resets the
group so NAOqi's mood painter regains control.

Usage (from bridge container, or any host with `qi` available):

    docker compose -f docker/docker-compose.experiment.yml exec bridge \
        python robot/scripts/pepper_shoulder_white.py

    # or pick a color:
    PEPPER_SHOULDER_COLOR=green \
        python robot/scripts/pepper_shoulder_white.py
    PEPPER_SHOULDER_COLOR=0x00FF00 \
        python robot/scripts/pepper_shoulder_white.py
"""

import os
import sys
import time

import qi

GROUP = "ChestLeds"
TICK_SEC = 0.3   # how often we re-assert (life-painter runs at a few Hz)
FADE_SEC = 0.2   # fadeRGB duration each tick

NAMED_COLORS = {
    "white": 0x00FFFFFF,
    "green": 0x0000FF00,
    "red":   0x00FF0000,
    "blue":  0x000000FF,
    "off":   0x00000000,
}


def parse_color(raw):
    raw = (raw or "white").strip().lower()
    if raw in NAMED_COLORS:
        return NAMED_COLORS[raw]
    s = raw.lstrip("#")
    if s.startswith("0x"):
        s = s[2:]
    return int(s, 16) & 0x00FFFFFF


def main():
    url = os.environ.get("PEPPER_QI_URL", "tcp://192.168.210.113:9559")
    color = parse_color(os.environ.get("PEPPER_SHOULDER_COLOR", "white"))
    print("[shoulder] connecting to", url)
    session = qi.Session()
    try:
        session.connect(url)
    except Exception as exc:
        print("[shoulder] FATAL connect failed:", exc)
        sys.exit(1)

    leds = session.service("ALLeds")
    print("[shoulder] forcing {} -> 0x{:06X} (Ctrl-C to release)".format(GROUP, color))
    try:
        while True:
            try:
                leds.fadeRGB(GROUP, color, FADE_SEC)
            except Exception as exc:
                print("[shoulder] fadeRGB failed:", exc)
            time.sleep(TICK_SEC)
    except KeyboardInterrupt:
        print("\n[shoulder] releasing group")
        try:
            leds.reset(GROUP)
        except Exception as exc:
            print("[shoulder] reset warning:", exc)


if __name__ == "__main__":
    main()
