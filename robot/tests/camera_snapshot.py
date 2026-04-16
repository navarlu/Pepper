#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Camera snapshot smoke test — grabs one frame from Pepper's top camera
without disrupting Autonomous Life / face tracking.

Recipe (from notes/control.md §4a):
    ALBasicAwareness.pauseAwareness()
    video.subscribeCamera -> getImageRemote -> releaseImage -> unsubscribe
    ALBasicAwareness.resumeAwareness()

Saves the image to robot/tests/captures/snap_<timestamp>.jpg.
Reads PEPPER_QI_URL from env; defaults to tcp://192.168.210.113:9559.
"""

from __future__ import print_function

import os
import time

import qi
from PIL import Image

PEPPER_QI_URL = os.environ.get("PEPPER_QI_URL", "tcp://192.168.210.113:9559")
CAPTURES_DIR = os.path.join(os.path.dirname(__file__), "captures")

# ALVideoDevice.subscribeCamera parameters
CAMERA_INDEX = 0    # 0=top, 1=bottom, 2=depth
RESOLUTION = 2      # kVGA (640x480)
COLOR_SPACE = 11    # kRGB (3 layers, RGB888)
FPS = 10


def log(msg):
    print("[camera] " + msg)


def main():
    log("connecting to {}".format(PEPPER_QI_URL))
    session = qi.Session()
    session.connect(PEPPER_QI_URL)
    log("connected")

    video = session.service("ALVideoDevice")
    try:
        awareness = session.service("ALBasicAwareness")
        have_awareness = True
    except Exception as e:
        log("ALBasicAwareness not available ({}); skipping pause/resume".format(e))
        awareness = None
        have_awareness = False

    # Report awareness state before/after for verification
    if have_awareness:
        try:
            log("awareness running before: {}".format(awareness.isAwarenessRunning()))
        except Exception as e:
            log("awareness isAwarenessRunning failed: {}".format(e))

        try:
            awareness.pauseAwareness()
            log("pauseAwareness() called — head frozen, tracked person preserved")
        except Exception as e:
            log("pauseAwareness failed: {} — continuing anyway".format(e))

    handle = None
    try:
        handle = video.subscribeCamera("kampion_snap", CAMERA_INDEX, RESOLUTION, COLOR_SPACE, FPS)
        log("subscribed handle={!r}".format(handle))

        img = video.getImageRemote(handle)
        if img is None:
            log("ERROR: getImageRemote returned None")
            return 1

        width, height, nb_layers, color_space = img[0], img[1], img[2], img[3]
        ts_sec, ts_usec = img[4], img[5]
        buf = img[6]
        cam_id = img[7]
        log("frame: {}x{}  layers={}  colorSpace={}  camID={}  ts={}.{:06d}".format(
            width, height, nb_layers, color_space, cam_id, ts_sec, ts_usec))
        log("buffer type={}  len={}".format(type(buf).__name__, len(buf)))

        video.releaseImage(handle)
        log("releaseImage() OK")

        if not os.path.isdir(CAPTURES_DIR):
            os.makedirs(CAPTURES_DIR)
        out_path = os.path.join(
            CAPTURES_DIR, "snap_{}.jpg".format(time.strftime("%Y%m%d_%H%M%S"))
        )

        pil = Image.frombytes("RGB", (width, height), bytes(buf))
        pil.save(out_path, "JPEG", quality=90)
        log("saved: {}".format(out_path))

    finally:
        if handle is not None:
            try:
                video.unsubscribe(handle)
                log("unsubscribed")
            except Exception as e:
                log("unsubscribe failed: {}".format(e))

        if have_awareness:
            try:
                awareness.resumeAwareness()
                log("resumeAwareness() called")
            except Exception as e:
                log("resumeAwareness failed: {}".format(e))

            try:
                log("awareness running after:  {}".format(awareness.isAwarenessRunning()))
            except Exception as e:
                log("awareness isAwarenessRunning failed: {}".format(e))

    log("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
