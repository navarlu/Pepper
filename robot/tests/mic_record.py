#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Microphone record smoke test — records N seconds of the front mic to a WAV
file on Pepper's filesystem via ALAudioRecorder.

Format constraint (NAOqi 2.5):
    WAV -> 16 kHz mono  OR  48 kHz 4-channel
    OGG -> 48 kHz 4-channel only

After the test, pull the file with:
    scp nao@<pepper-ip>:<REMOTE_PATH> .
"""

from __future__ import print_function

import os
import time

import qi

PEPPER_QI_URL = os.environ.get("PEPPER_QI_URL", "tcp://192.168.210.113:9559")
RECORD_SECONDS = float(os.environ.get("MIC_TEST_SECONDS", "3.0"))
REMOTE_PATH = "/home/nao/recordings/microphones/kampion_clip.wav"
SAMPLE_RATE = 16000
# [left, right, front, rear] — 1=include, 0=skip; WAV 16 kHz requires mono
CHANNELS = [0, 0, 1, 0]


def log(msg):
    print("[mic] " + msg)


def main():
    log("connecting to {}".format(PEPPER_QI_URL))
    session = qi.Session()
    session.connect(PEPPER_QI_URL)
    log("connected")

    rec = session.service("ALAudioRecorder")

    # Guard — only one recording at a time
    try:
        rec.stopMicrophonesRecording()
        log("stopped any pre-existing recording")
    except Exception as e:
        log("stopMicrophonesRecording (pre-guard) -> {} (ok, ignore)".format(e))

    log("starting WAV recording: {}  @ {} Hz, channels={}, {:.1f}s".format(
        REMOTE_PATH, SAMPLE_RATE, CHANNELS, RECORD_SECONDS))
    rec.startMicrophonesRecording(REMOTE_PATH, "wav", SAMPLE_RATE, CHANNELS)

    t0 = time.time()
    try:
        time.sleep(RECORD_SECONDS)
    finally:
        rec.stopMicrophonesRecording()
        elapsed = time.time() - t0

    log("stopped after {:.2f}s".format(elapsed))
    log("remote file (on Pepper): {}".format(REMOTE_PATH))

    # Try to show size via ALMemory if available — not all robots expose
    # filesystem info, so this is best-effort.
    try:
        memory = session.service("ALMemory")
        # No standard memory key for file size; just a hint for the user.
        _ = memory
    except Exception:
        pass

    host_hint = PEPPER_QI_URL.replace("tcp://", "").rsplit(":", 1)[0]
    log("to pull:  scp nao@{}:{} .".format(host_hint, REMOTE_PATH))
    log("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
