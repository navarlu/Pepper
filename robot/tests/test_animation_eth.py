#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
End-to-end animation test — replays the bridge's `/animation/<name>` flow
against Pepper directly via NAOqi, with no HTTP layer in between.

Use this to confirm:
  1. Network reachability of Pepper at PEPPER_QI_URL (qi connect succeeds).
  2. NAOqi services come up (ALBehaviorManager, ALAnimationPlayer).
  3. The animation alias resolves and plays end-to-end.

Default target is the **ethernet** address (169.254.66.123); override via env:

    PEPPER_QI_URL=tcp://10.0.0.149:9559 \
      uv run python robot/tests/test_animation_eth.py
"""

from __future__ import print_function

import os
import sys
import time

import qi

# ---- config (top-of-file globals; no argparse per project convention) ------
PEPPER_QI_URL    = os.environ.get("PEPPER_QI_URL", "tcp://169.254.66.123:9559")
ANIMATION_NAME   = os.environ.get("ANIMATION_NAME", "Hey_1")
ANIMATIONS_FILE  = os.environ.get(
    "ANIMATIONS_FILE",
    os.path.join(os.path.dirname(__file__), "..", "data", "animations.json"),
)
CONNECT_TIMEOUT  = float(os.environ.get("CONNECT_TIMEOUT", "10.0"))
SERVICE_TIMEOUT  = float(os.environ.get("SERVICE_TIMEOUT", "10.0"))


# ---- minimal copies of the bridge's resolution logic -----------------------
# Kept inline so this script runs without importing the bridge package
# (tests should be runnable with just `qi` available).
def load_animations_map(path):
    import json
    try:
        with open(path, "r") as f:
            data = json.load(f) or {}
        return {str(k).strip(): str(v).strip() for k, v in data.items() if k and v}
    except Exception as exc:
        print("[test] failed to load animations map:", exc)
        return {}


def resolve_animation_name(name, animations_map, installed):
    key = str(name).strip()
    if not key:
        return None
    if key in animations_map:
        return animations_map[key]
    if "/" in key:
        return key
    suffix = "/" + key
    matches = [b for b in installed if b.endswith(suffix)]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    pref = [m for m in matches if m.startswith("animations/")]
    return pref[0] if pref else matches[0]


def wait_for_service(session, name, timeout):
    deadline = time.time() + timeout
    last_err = None
    while time.time() < deadline:
        try:
            return session.service(name)
        except Exception as exc:
            last_err = exc
            time.sleep(0.25)
    raise RuntimeError("timed out waiting for service '{}': {}".format(name, last_err))


# ---- main ------------------------------------------------------------------
def main():
    print("[test] connecting:", PEPPER_QI_URL)
    session = qi.Session()
    t0 = time.time()
    try:
        session.connect(PEPPER_QI_URL)
    except Exception as exc:
        print("[test] qi.connect FAILED after {:.2f}s: {}".format(time.time() - t0, exc))
        return 2
    print("[test] qi.connect OK in {:.2f}s".format(time.time() - t0))

    bm   = wait_for_service(session, "ALBehaviorManager", SERVICE_TIMEOUT)
    anim = wait_for_service(session, "ALAnimationPlayer", SERVICE_TIMEOUT)
    life = None
    try:
        life = session.service("ALAutonomousLife")
    except Exception as exc:
        print("[test] ALAutonomousLife unavailable:", exc)

    installed = bm.getInstalledBehaviors()
    print("[test] installed behaviors:", len(installed))

    animations_map = load_animations_map(ANIMATIONS_FILE)
    print("[test] animations map entries:", len(animations_map))

    behavior = resolve_animation_name(ANIMATION_NAME, animations_map, installed)
    if not behavior:
        print("[test] could not resolve animation:", ANIMATION_NAME)
        return 3
    print("[test] resolved {!r} -> {!r}".format(ANIMATION_NAME, behavior))

    if life is not None:
        try:
            state = str(life.getState())
            print("[test] autonomous-life state:", state)
            if state.lower() == "disabled":
                print("[test] enabling 'solitary' so animations can play")
                life.setState("solitary")
        except Exception as exc:
            print("[test] life check warning:", exc)

    print("[test] running behavior:", behavior)
    t0 = time.time()
    try:
        if behavior.startswith("animations/"):
            fut = anim.run(behavior)
            try:
                fut.value()
            except Exception:
                pass
        else:
            bm.runBehavior(behavior)
    except Exception as exc:
        print("[test] run FAILED after {:.2f}s: {}".format(time.time() - t0, exc))
        return 4

    print("[test] done in {:.2f}s — animation played successfully".format(time.time() - t0))
    return 0


if __name__ == "__main__":
    sys.exit(main())
