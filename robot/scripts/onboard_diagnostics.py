#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Read-only local NAOqi snapshot; compatible with Python 2.7 and 3.

Waits for NAOqi/services, prints JSON, and exits. Never requests motion,
changes reflexes, clears notifications, or changes Autonomous Life.
Exit 0 means all queries returned, NOT that the robot is safe or healthy.
"""
from __future__ import print_function

import json
import signal
import sys
import time


def deadline(signum, frame):
    # SystemExit is deliberately not caught by the retry loops.
    sys.stderr.write("Diagnostic deadline exceeded (120 seconds).\n")
    raise SystemExit(2)


def main():
    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(120)
    try:
        import qi
    except ImportError:
        sys.stderr.write("Run with Pepper's Python interpreter containing qi.\n")
        return 2

    session = qi.Session()
    report = {"timestamp_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
              "values": {}, "errors": {}, "health_assessed": False}
    try:
        connected = False
        for attempt in range(30):
            try:
                session.connect("tcp://127.0.0.1:9559", _async=True).value(2000)
                connected = True
                break
            except Exception:
                session.close()
                session = qi.Session()
                time.sleep(1)
        if not connected:
            report["errors"]["connection"] = "Local NAOqi did not become available"
        else:
            queries = (
                ("ALDiagnosis", "getPassiveDiagnosis"),
                ("ALDiagnosis", "getActiveDiagnosis"),
                ("ALMotion", "getDiagnosisEffectEnabled"),
                ("ALMotion", "robotIsWakeUp"),
                ("ALAutonomousLife", "getState"),
                ("ALRobotPosture", "getPosture"),
            )
            for service_name, method in queries:
                key = service_name + "." + method
                for attempt in range(3):
                    try:
                        service = session.service(service_name, _async=True).value(2000)
                        report["values"][key] = getattr(service, method)(_async=True).value(3000)
                        break
                    except Exception as exc:
                        if attempt == 2:
                            report["errors"][key] = str(exc)
                        else:
                            time.sleep(1)
        print(json.dumps(report, indent=2, sort_keys=True, default=repr))
        return 1 if report["errors"] else 0
    finally:
        # The process exits immediately; avoid a potentially blocking close.
        signal.alarm(0)


if __name__ == "__main__":
    sys.exit(main())
