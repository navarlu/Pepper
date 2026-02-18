#!/usr/bin/env python2
# -*- coding: utf-8 -*-

from __future__ import print_function

import io
import os
import time
import qi


PEPPER_QI_URL = "tcp://10.0.0.149:9559"
LOG_DIR = os.path.join(os.path.dirname(__file__), "diagnostics")
LOGGER = None


class TeeLogger(object):
    def __init__(self, path):
        self.path = path
        self._fh = io.open(path, "w", encoding="utf-8")

    def log(self, msg):
        text = u"{}".format(msg)
        print(text)
        self._fh.write(text + u"\n")
        self._fh.flush()

    def close(self):
        try:
            self._fh.close()
        except Exception:
            pass


def log(msg):
    if LOGGER is not None:
        LOGGER.log(msg)
    else:
        print(msg)


def safe_call(label, fn, default=None):
    try:
        value = fn()
        log("[ok] {}: {}".format(label, value))
        return value
    except Exception as exc:
        log("[warn] {}: {}".format(label, exc))
        return default


def dump_memory_keys(memory, keys):
    log("\n=== ALMemory (selected keys) ===")
    for key in keys:
        try:
            value = memory.getData(key)
            log("{} = {}".format(key, value))
        except Exception as exc:
            log("{} = <unavailable: {}>".format(key, exc))


def dump_error_like_keys(memory):
    log("\n=== ALMemory (error-like keys) ===")
    names = safe_call("memory.getDataListName", lambda: memory.getDataListName(), default=[])
    if not names:
        return

    patterns = (
        "Diagnosis",
        "Error",
        "Alarm",
        "Battery",
        "RobotConfig",
        "Device/SubDeviceList",
    )
    matched = [n for n in names if any(p in n for p in patterns)]
    # keep output readable
    matched = matched[:120]

    for name in matched:
        try:
            value = memory.getData(name)
        except Exception:
            continue
        text = str(value)
        if "error" in name.lower() or "alarm" in name.lower() or value not in (None, "", 0, False):
            log("{} = {}".format(name, text))


def dump_life_state(life):
    log("\n=== ALAutonomousLife ===")
    safe_call("life.getState", lambda: life.getState())
    safe_call("life.getFocus", lambda: life.getFocus())
    safe_call("life.getAutonomousAbilitiesStatus", lambda: life.getAutonomousAbilitiesStatus())
    for ability in (
        "AutonomousBlinking",
        "BackgroundMovement",
        "BasicAwareness",
        "ListeningMovement",
        "SpeakingMovement",
    ):
        safe_call(
            "life.getAutonomousAbilityEnabled({})".format(ability),
            lambda a=ability: life.getAutonomousAbilityEnabled(a),
        )


def dump_motion_state(motion, posture):
    log("\n=== Motion/Posture ===")
    safe_call("motion.robotIsWakeUp", lambda: motion.robotIsWakeUp())
    safe_call("motion.getStiffnesses('Body')", lambda: motion.getStiffnesses("Body"))
    safe_call("posture.getPostureFamily", lambda: posture.getPostureFamily())
    safe_call("posture.getPosture", lambda: posture.getPosture())


def dump_battery(memory):
    log("\n=== Battery ===")
    safe_call(
        "Battery charge (%)",
        lambda: memory.getData("Device/SubDeviceList/Battery/Charge/Sensor/Value"),
    )
    safe_call(
        "Battery current (A)",
        lambda: memory.getData("Device/SubDeviceList/Battery/Current/Sensor/Value"),
    )
    safe_call(
        "Battery temperature (C)",
        lambda: memory.getData("Device/SubDeviceList/Battery/Temperature/Sensor/Value"),
    )


def dump_diagnosis(diag):
    log("\n=== ALDiagnosis ===")
    # Different NAOqi versions expose different methods, so probe safely.
    candidate_calls = [
        ("diag.getDiagnosis", lambda: diag.getDiagnosis()),
        ("diag.getActiveDiagnosis", lambda: diag.getActiveDiagnosis()),
        ("diag.getPassiveDiagnosis", lambda: diag.getPassiveDiagnosis()),
        ("diag.isNotificationEnabled", lambda: diag.isNotificationEnabled()),
    ]
    for label, fn in candidate_calls:
        safe_call(label, fn)


def main():
    global LOGGER
    if not os.path.isdir(LOG_DIR):
        os.makedirs(LOG_DIR)
    ts = time.strftime("%Y%m%d_%H%M%S")
    log_path = os.path.join(LOG_DIR, "life_diag_{}.log".format(ts))
    LOGGER = TeeLogger(log_path)

    session = qi.Session()
    session.connect(PEPPER_QI_URL)
    log("Connected to {}".format(PEPPER_QI_URL))
    log("Diagnostics log file: {}".format(log_path))

    life = safe_call("session.service(ALAutonomousLife)", lambda: session.service("ALAutonomousLife"))
    motion = safe_call("session.service(ALMotion)", lambda: session.service("ALMotion"))
    posture = safe_call("session.service(ALRobotPosture)", lambda: session.service("ALRobotPosture"))
    memory = safe_call("session.service(ALMemory)", lambda: session.service("ALMemory"))
    diag = safe_call("session.service(ALDiagnosis)", lambda: session.service("ALDiagnosis"))

    if life:
        dump_life_state(life)
    if motion and posture:
        dump_motion_state(motion, posture)
    if memory:
        dump_battery(memory)
        dump_memory_keys(
            memory,
            [
                "Diagnostics/Active",
                "Diagnostics/Passives",
                "ALMotion/Diagnosis",
                "RobotConfig/Body/BaseVersion",
                "RobotConfig/Body/Type",
            ],
        )
        dump_error_like_keys(memory)
    if diag:
        dump_diagnosis(diag)

    log("\nDiagnostics finished.")
    LOGGER.close()


if __name__ == "__main__":
    main()
