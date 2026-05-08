#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Print Pepper autonomous-life state without changing anything.

Run inside the bridge container:

    docker compose -f docker/docker-compose.yml exec bridge \
        python robot/scripts/pepper_life_state.py
"""

import os
import sys

import qi

ABILITIES = (
    "AutonomousBlinking",
    "BackgroundMovement",
    "BasicAwareness",
    "ListeningMovement",
    "SpeakingMovement",
)


def connect():
    url = os.environ.get("PEPPER_QI_URL", "tcp://192.168.210.113:9559")
    session = qi.Session()
    try:
        session.connect(url)
    except Exception as exc:
        print("[life-state] cannot connect to {}: {}".format(url, exc), file=sys.stderr)
        sys.exit(2)
    return session, url


def safe_call(label, fn):
    try:
        return fn()
    except Exception as exc:
        return "ERR {}".format(exc)


def main():
    session, url = connect()
    life = session.service("ALAutonomousLife")

    print("[life-state] connected to {}".format(url))
    print("[life-state] state    = {}".format(safe_call("state", life.getState)))
    print("[life-state] focus    = {}".format(safe_call("focus", life.getFocus)))
    print("[life-state] activity = {}".format(safe_call("activity", life.getActivity)))
    for ability in ABILITIES:
        value = safe_call(
            ability,
            lambda ability=ability: life.getAutonomousAbilityEnabled(ability),
        )
        print("[life-state]   {:<22} {}".format(ability, value))
    return 0


if __name__ == "__main__":
    sys.exit(main())
