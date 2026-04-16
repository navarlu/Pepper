#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Eye LED smoke test — cycles FaceLeds and resets.

Why the naive version didn't work:
  When ALAutonomousLife is in "interactive" state, a focused activity
  (the default life activity or dialog) continuously re-paints the eye
  LEDs through the mood/engagement system. Manual fadeRGB calls land
  but get overwritten within milliseconds.

Fix: call life.stopAll() to drop out of interactive into solitary.
  - Face tracking (BasicAwareness) stays on — it's a solitary ability.
  - No stiffness drop (unlike setState("disabled")).
  - After the test you can re-engage Pepper to return to interactive.
"""

from __future__ import print_function

import os
import time

import qi

PEPPER_QI_URL = os.environ.get("PEPPER_QI_URL", "tcp://192.168.210.113:9559")

HOLD_SECONDS = float(os.environ.get("LEDS_HOLD", "2.0"))
FADE_SECONDS = 0.4

SEQUENCE = [
    ("off",     None),
    ("red",     0x00FF0000),
    ("green",   0x0000FF00),
    ("blue",    0x000000FF),
    ("yellow",  0x00FFFF00),
    ("magenta", 0x00FF00FF),
    ("cyan",    0x0000FFFF),
    ("white",   0x00FFFFFF),
]
GROUP = "FaceLeds"


def log(msg):
    print("[leds] " + msg)


def safe(label, fn):
    try:
        out = fn()
        log("{} -> {}".format(label, out))
        return out
    except Exception as e:
        log("{} FAILED: {}".format(label, e))
        return None


def main():
    log("connecting to {}".format(PEPPER_QI_URL))
    session = qi.Session()
    session.connect(PEPPER_QI_URL)
    log("connected")

    leds = session.service("ALLeds")

    life = None
    prior_state = None
    prior_blink = None
    try:
        life = session.service("ALAutonomousLife")
    except Exception as e:
        log("ALAutonomousLife unavailable: {}".format(e))

    if life is not None:
        prior_state = safe("life.getState() (before)", lambda: life.getState())
        prior_blink = safe(
            "life.getAutonomousAbilityEnabled('AutonomousBlinking')",
            lambda: life.getAutonomousAbilityEnabled("AutonomousBlinking"),
        )

        # Release LED control: stop focused activity if any
        if prior_state == "interactive":
            safe("life.stopAll() (exit interactive)", lambda: life.stopAll())
            time.sleep(0.5)
            safe("life.getState() (after stopAll)", lambda: life.getState())

        # Blinking still overwrites FaceLeds every ~3 s — silence it
        if prior_blink:
            safe(
                "disable AutonomousBlinking",
                lambda: life.setAutonomousAbilityEnabled("AutonomousBlinking", False),
            )

    try:
        for label, color in SEQUENCE:
            if color is None:
                log("--> {:<8}  leds.off('{}')  hold={}s".format(label, GROUP, HOLD_SECONDS))
                leds.off(GROUP)
            else:
                log("--> {:<8}  0x{:06X}  fade={}s  hold={}s".format(
                    label, color, FADE_SECONDS, HOLD_SECONDS))
                leds.fadeRGB(GROUP, color, FADE_SECONDS)
            time.sleep(HOLD_SECONDS)

    finally:
        safe("leds.reset('{}')".format(GROUP), lambda: leds.reset(GROUP))

        if life is not None:
            if prior_blink:
                safe(
                    "restore AutonomousBlinking={}".format(prior_blink),
                    lambda: life.setAutonomousAbilityEnabled("AutonomousBlinking", prior_blink),
                )
            # Leave Pepper in solitary — re-engaging a person will lift her back to interactive naturally
            final_state = safe("life.getState() (final)", lambda: life.getState())
            log("final life state: {}  (prior was {})".format(final_state, prior_state))

    log("DONE")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
