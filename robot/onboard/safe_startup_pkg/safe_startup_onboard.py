#!/usr/bin/python
# -*- coding: utf-8 -*-
"""One-shot, on-robot safe startup for Pepper/NAOqi 2.5 (Python 2.7)."""
from __future__ import print_function

import logging
import os
import sys
import time


QI_PYTHON_PATH = "/opt/aldebaran/lib/python2.7/site-packages"
QI_LIBRARY_PATH = "/opt/aldebaran/lib"
LOG_PATH = "/home/nao/safe_startup_onboard.log"
MAX_LOG_BYTES = 1024 * 1024

NAOQI_URL = "tcp://127.0.0.1:9559"
CONNECT_BUDGET_SEC = 120.0
CONNECT_ATTEMPT_TIMEOUT_MS = 5000
SERVICE_WAIT_TIMEOUT_SEC = 90.0
SERVICE_ATTEMPT_TIMEOUT_MS = 3000
SERVICE_RETRY_SEC = 0.5
CALL_TIMEOUT_MS = 15000
# Keep this aligned with the currently deployed RPi fallback setting.
SAFE_STARTUP_VOLUME = 60
STARTUP_QUIET_VOLUME = 0
STARTUP_QUIET_SETTLE_SEC = 10.0

AUTONOMOUS_ABILITIES = (
    "AutonomousBlinking",
    "BackgroundMovement",
    "BasicAwareness",
    "ListeningMovement",
    "SpeakingMovement",
)


def prepare_qi_environment():
    """Re-exec once so the dynamic linker sees Pepper's qi libraries."""
    if os.environ.get("SAFE_STARTUP_QI_ENV_READY") == "1":
        if QI_PYTHON_PATH not in sys.path:
            sys.path.insert(0, QI_PYTHON_PATH)
        return

    env = os.environ.copy()
    python_path = env.get("PYTHONPATH", "")
    library_path = env.get("LD_LIBRARY_PATH", "")
    env["PYTHONPATH"] = QI_PYTHON_PATH + (":" + python_path if python_path else "")
    env["LD_LIBRARY_PATH"] = QI_LIBRARY_PATH + (":" + library_path if library_path else "")
    env["SAFE_STARTUP_QI_ENV_READY"] = "1"
    os.execve(sys.executable, [sys.executable] + sys.argv, env)


def configure_logging():
    if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > MAX_LOG_BYTES:
        open(LOG_PATH, "w").close()

    formatter = logging.Formatter(
        "%(asctime)s [safe-startup-onboard] %(levelname)s %(message)s",
        "%Y-%m-%d %H:%M:%S",
    )
    logger = logging.getLogger("safe-startup-onboard")
    logger.setLevel(logging.INFO)

    stream_handler = logging.StreamHandler()
    stream_handler.setFormatter(formatter)
    logger.addHandler(stream_handler)

    try:
        file_handler = logging.FileHandler(LOG_PATH)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except Exception as exc:
        logger.warning("[warn] cannot open %s: %s", LOG_PATH, exc)

    return logger


prepare_qi_environment()
import qi  # noqa: E402

log = configure_logging()


def async_value(fn, args=(), timeout_ms=CALL_TIMEOUT_MS):
    """Invoke a qi method asynchronously and impose a client-side timeout."""
    return fn(*args, **{"_async": True}).value(timeout_ms)


def safe(label, fn, args=(), timeout_ms=CALL_TIMEOUT_MS, validator=None):
    """Run one bounded qi operation and record whether its result is valid."""
    try:
        result = async_value(fn, args, timeout_ms)
        if validator is not None and not validator(result):
            log.warning("[warn] %s returned unexpected result: %r", label, result)
            return False, result
        log.info("[ok] %s -> %r", label, result)
        return True, result
    except Exception as exc:
        log.warning("[warn] %s failed: %s", label, exc)
        return False, None


def connect_session():
    deadline = time.time() + CONNECT_BUDGET_SEC
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        session = qi.Session()
        try:
            session.connect(NAOQI_URL, _async=True).value(CONNECT_ATTEMPT_TIMEOUT_MS)
            log.info("[ok] connected to %s on attempt %d", NAOQI_URL, attempt)
            return session
        except Exception as exc:
            log.warning("[wait] connection attempt %d failed: %s", attempt, exc)
            try:
                session.close()
            except Exception:
                pass
            time.sleep(1.0)
    raise RuntimeError("NAOqi did not become reachable within %.0f seconds" % CONNECT_BUDGET_SEC)


def wait_service(session, name, timeout_sec=SERVICE_WAIT_TIMEOUT_SEC):
    deadline = time.time() + timeout_sec
    last_error = None
    while time.time() < deadline:
        try:
            service = session.service(name, _async=True).value(SERVICE_ATTEMPT_TIMEOUT_MS)
            log.info("[ok] service ready: %s", name)
            return service
        except Exception as exc:
            last_error = exc
            time.sleep(SERVICE_RETRY_SEC)
    raise RuntimeError("timeout waiting for %s (last error: %s)" % (name, last_error))


def disable_autonomous_life(life):
    for attempt in range(1, 11):
        safe("ALAutonomousLife.setState('disabled') attempt %d" % attempt,
             life.setState, ("disabled",))
        ok, state = safe("ALAutonomousLife.getState()", life.getState)
        if ok and state == "disabled":
            return True
        time.sleep(1.0)
    log.warning("[warn] Autonomous Life was not disabled after 10 attempts")
    return False


def run_safe_startup():
    log.info("boot run started")
    session = connect_session()
    checks = []
    audio = None

    try:
        audio = wait_service(session, "ALAudioDevice")
        safe("startup quiet volume (%d)" % STARTUP_QUIET_VOLUME,
             audio.setOutputVolume, (STARTUP_QUIET_VOLUME,))
        ok, volume = safe("ALAudioDevice.getOutputVolume()", audio.getOutputVolume)
        checks.append(ok and volume == STARTUP_QUIET_VOLUME)

        motion = wait_service(session, "ALMotion")
        life = wait_service(session, "ALAutonomousLife")
        posture = wait_service(session, "ALRobotPosture")

        safe("ALMotion.setDiagnosisEffectEnabled(False)",
             motion.setDiagnosisEffectEnabled, (False,))
        ok, diagnosis_effect = safe("ALMotion.getDiagnosisEffectEnabled()",
                                    motion.getDiagnosisEffectEnabled)
        checks.append(ok and diagnosis_effect is False)

        checks.append(disable_autonomous_life(life))
        for ability in AUTONOMOUS_ABILITIES:
            safe("setAutonomousAbilityEnabled(%s, False)" % ability,
                 life.setAutonomousAbilityEnabled, (ability, False))
            ok, enabled = safe("getAutonomousAbilityEnabled(%s)" % ability,
                               life.getAutonomousAbilityEnabled, (ability,))
            checks.append(ok and enabled is False)

        # The action return values can be False for a no-op. Verify final state.
        safe("ALMotion.wakeUp()", motion.wakeUp)
        ok, awake = safe("ALMotion.robotIsWakeUp()", motion.robotIsWakeUp)
        checks.append(ok and awake is True)

        safe("ALRobotPosture.goToPosture('StandInit', 0.6)",
             posture.goToPosture, ("StandInit", 0.6))
        ok, final_posture = safe("ALRobotPosture.getPosture()", posture.getPosture)
        checks.append(ok and final_posture == "StandInit")

        try:
            diagnosis = wait_service(session, "ALDiagnosis", timeout_sec=5.0)
            safe("ALDiagnosis.getPassiveDiagnosis()", diagnosis.getPassiveDiagnosis)
            safe("ALDiagnosis.getActiveDiagnosis()", diagnosis.getActiveDiagnosis)
        except Exception as exc:
            log.warning("[warn] ALDiagnosis unavailable: %s", exc)

    finally:
        try:
            if audio is not None:
                # Also restore audio if service discovery or startup aborts.
                try:
                    log.info("holding startup quiet volume for %.1f seconds",
                             STARTUP_QUIET_SETTLE_SEC)
                    time.sleep(STARTUP_QUIET_SETTLE_SEC)
                finally:
                    safe("restore output volume (%d)" % SAFE_STARTUP_VOLUME,
                         audio.setOutputVolume, (SAFE_STARTUP_VOLUME,))
                    ok, volume = safe("ALAudioDevice.getOutputVolume()",
                                      audio.getOutputVolume)
                    checks.append(ok and volume == SAFE_STARTUP_VOLUME)
        finally:
            try:
                session.close()
            except Exception:
                pass

    if all(checks):
        log.info("safe startup complete: final state verified")
    else:
        log.warning("safe startup complete with warnings: one or more final-state checks failed")


def main():
    try:
        run_safe_startup()
    except Exception as exc:
        # This is an autorun one-shot. Preserve the failure in persistent logs
        # without asking ServiceManager to restart it in a tight loop.
        log.exception("safe startup aborted: %s", exc)
    return 0


if __name__ == "__main__":
    sys.exit(main())
