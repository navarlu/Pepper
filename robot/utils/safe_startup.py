#!/usr/bin/env python2
# -*- coding: utf-8 -*-
from __future__ import print_function
import sys
import time
import qi

DEFAULT_URL = "tcp://10.0.0.149:9559"
#DEFAULT_URL = "tcp://169.254.97.167:9559"

CONNECT_RETRY_SEC = 1.0
SERVICE_RETRY_SEC = 0.5
SERVICE_WAIT_TIMEOUT_SEC = 90.0  # how long to wait for services after connect


def wait_connect(session, url):
    attempt = 0
    while True:
        attempt += 1
        print("[wait] connect attempt {} to {}...".format(attempt, url))
        try:
            fut = session.connect(url, _async=True)
            print("[wait] waiting for connect future (timeout 10s)...")
            fut.value(10 * 1000)  # timeout in ms
            print("[wait] connected!")
            return session
        except Exception as e:
            print("[wait] attempt {} failed ({}): {}".format(attempt, type(e).__name__, e))
            try:
                session.close()
            except Exception:
                pass
            session = qi.Session()
            time.sleep(CONNECT_RETRY_SEC)


def safe(label, fn):
    try:
        res = fn()
        print("[ok] {} -> {}".format(label, res))
        return True, res
    except Exception as e:
        print("[warn] {} failed: {}".format(label, e))
        return False, None


def wait_service(session, name, timeout_sec=SERVICE_WAIT_TIMEOUT_SEC):
    t0 = time.time()
    while True:
        try:
            svc = session.service(name)
            print("[info] service ready:", name)
            return svc
        except Exception as e:
            if time.time() - t0 > timeout_sec:
                raise RuntimeError("Timeout waiting for service {} (last error: {})".format(name, e))
            time.sleep(SERVICE_RETRY_SEC)


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_URL

    session = qi.Session()
    print("[info] waiting for NAOqi at {}".format(url))
    session = wait_connect(session, url)
    print("[info] connected; waiting for core services...")

    # Wait for services to actually be registered
    motion = wait_service(session, "ALMotion")
    # These can appear slightly later; wait but don't hard-fail if you prefer
    life = wait_service(session, "ALAutonomousLife")
    posture = wait_service(session, "ALRobotPosture")
    diag = wait_service(session, "ALDiagnosis")

    # 1) Disable diagnosis-effect reflex (key)
    safe("ALMotion.setDiagnosisEffectEnabled(False)",
         lambda: motion.setDiagnosisEffectEnabled(False))

    # 2) Disable Autonomous Life (recommended)
    safe("ALAutonomousLife.setState('disabled')",
         lambda: life.setState("disabled"))

    for a in ("AutonomousBlinking", "BackgroundMovement", "BasicAwareness",
              "ListeningMovement", "SpeakingMovement"):
        safe("setAutonomousAbilityEnabled({}, False)".format(a),
             lambda aa=a: life.setAutonomousAbilityEnabled(aa, False))

    # 3) Wake and stand
    safe("ALMotion.wakeUp()", lambda: motion.wakeUp())
    safe("ALRobotPosture.goToPosture('StandInit', 0.6)",
         lambda: posture.goToPosture("StandInit", 0.6))

    # 4) Print diagnosis summary
    safe("ALDiagnosis.getPassiveDiagnosis()", lambda: diag.getPassiveDiagnosis())
    safe("ALDiagnosis.getActiveDiagnosis()", lambda: diag.getActiveDiagnosis())

    print("[done] stabilization complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
