#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Freeze Pepper: disable autonomous life and every autonomous ability.

After this runs, Pepper stays exactly where she is — no idle motion,
no head tracking, no listening nods, no speaking gestures. Only
explicit commands from the bridge (animations, posture changes,
streamed audio) will move her.

Designed to be run inside the bridge container (which already has qi
on the path and a route to PEPPER_QI_URL):

    docker compose -f docker/docker-compose.yml exec bridge \\
        python robot/scripts/freeze_pepper.py

Pass --rest to also crouch and remove stiffness so the motors go
fully passive (lowest power, safe to leave unattended). Pass
--unfreeze to restore the bridge's intended profile from config
(state=solitary + LIFE_* flags applied). Pass --all-on for the
fully-social profile (state=interactive + every ability enabled).
"""

import argparse
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


def _connect():
    url = os.environ.get("PEPPER_QI_URL", "tcp://192.168.210.113:9559")
    session = qi.Session()
    try:
        session.connect(url)
    except Exception as exc:
        print(f"[freeze] cannot connect to {url}: {exc}", file=sys.stderr)
        sys.exit(2)
    return session, url


def _print_state(life, prefix):
    state = life.getState()
    print(f"{prefix} state = {state}")
    for a in ABILITIES:
        try:
            print(f"{prefix}   {a:<22} {life.getAutonomousAbilityEnabled(a)}")
        except Exception as exc:
            print(f"{prefix}   {a:<22} ERR {exc}")


def freeze(session):
    life = session.service("ALAutonomousLife")
    _print_state(life, "[freeze] before:")
    # Disable abilities first; setting state changes them again on transition.
    for a in ABILITIES:
        try:
            life.setAutonomousAbilityEnabled(a, False)
        except Exception as exc:
            print(f"[freeze] disable {a} failed: {exc}", file=sys.stderr)
    try:
        life.setState("disabled")
    except Exception as exc:
        print(f"[freeze] setState('disabled') failed: {exc}", file=sys.stderr)
    # Re-apply ability flags after the state change — NAOqi sometimes resets
    # them on transition, so this guarantees the post-state matches intent.
    for a in ABILITIES:
        try:
            life.setAutonomousAbilityEnabled(a, False)
        except Exception:
            pass
    _print_state(life, "[freeze] after: ")


def rest(session):
    """Lowest-power passive: crouch + zero stiffness."""
    try:
        posture = session.service("ALRobotPosture")
        print("[freeze] going to Crouch posture (slow)...")
        posture.goToPosture("Crouch", 0.4)
    except Exception as exc:
        print(f"[freeze] Crouch failed: {exc}", file=sys.stderr)
    try:
        motion = session.service("ALMotion")
        motion.setStiffnesses("Body", 0.0)
        print("[freeze] body stiffness -> 0.0 (motors off)")
    except Exception as exc:
        print(f"[freeze] stiffness 0 failed: {exc}", file=sys.stderr)


def unfreeze(session):
    """Restore the bridge's intended profile (state=solitary + project LIFE_*)."""
    profile = {
        "AutonomousBlinking": True,
        "BackgroundMovement": True,
        "BasicAwareness": False,
        "ListeningMovement": False,
        "SpeakingMovement": True,
    }
    _apply_profile(session, "unfreeze", state="solitary", profile=profile)


def all_on(session):
    """Fully-social profile: interactive + every ability on."""
    profile = {a: True for a in ABILITIES}
    _apply_profile(session, "all-on", state="interactive", profile=profile)


def _apply_profile(session, tag: str, state: str, profile: dict) -> None:
    life = session.service("ALAutonomousLife")
    _print_state(life, f"[{tag}] before:")
    try:
        life.setState(state)
    except Exception as exc:
        print(f"[{tag}] setState({state!r}) failed: {exc}", file=sys.stderr)
    for a, on in profile.items():
        try:
            life.setAutonomousAbilityEnabled(a, on)
        except Exception as exc:
            print(f"[{tag}] {a}={on} failed: {exc}", file=sys.stderr)
    try:
        motion = session.service("ALMotion")
        motion.setStiffnesses("Body", 1.0)
        print(f"[{tag}] body stiffness -> 1.0")
    except Exception as exc:
        print(f"[{tag}] stiffness restore failed: {exc}", file=sys.stderr)
    _print_state(life, f"[{tag}] after: ")


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--rest", action="store_true",
                   help="also crouch + zero stiffness (motors off, fully passive)")
    p.add_argument("--unfreeze", action="store_true",
                   help="restore project profile (solitary + LIFE_* flags)")
    p.add_argument("--all-on", action="store_true", dest="all_on",
                   help="fully social: interactive + every ability on")
    args = p.parse_args()

    chosen = sum(int(x) for x in (args.rest, args.unfreeze, args.all_on))
    if chosen > 1:
        print("[freeze] --rest / --unfreeze / --all-on are mutually exclusive",
              file=sys.stderr)
        return 2

    session, url = _connect()
    print(f"[freeze] connected to {url}")
    if args.unfreeze:
        unfreeze(session)
    elif args.all_on:
        all_on(session)
    else:
        freeze(session)
        if args.rest:
            rest(session)
    return 0


if __name__ == "__main__":
    sys.exit(main())
