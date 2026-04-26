#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Watchdog that monitors Pepper's availability and runs safe_startup
whenever she comes online. Stays idle while she's connected,
resumes polling when she goes offline.
"""

import os
import sys
import socket
import time
import logging
import json
from urllib.request import Request, urlopen

# ── qi library paths (Docker volumes) ──────────────────────────────
QI_LIB_PATH = os.environ.get("PYTHONPATH", "/opt/qi")
BOOST_LIB_PATH = os.environ.get("LD_LIBRARY_PATH", "/opt/boost-lib:/opt/qi-native-lib")

if QI_LIB_PATH not in sys.path:
    sys.path.insert(0, QI_LIB_PATH)

_ld = os.environ.get("LD_LIBRARY_PATH", "")
for p in BOOST_LIB_PATH.split(":"):
    if p and p not in _ld:
        os.environ["LD_LIBRARY_PATH"] = p + (":" + _ld if _ld else "")
        _ld = os.environ["LD_LIBRARY_PATH"]

import qi  # noqa: E402

# ── Config ─────────────────────────────────────────────────────────
NAOQI_PORT = 9559
POLL_INTERVAL_OFFLINE = 5.0      # seconds between checks when Pepper is offline
POLL_INTERVAL_ONLINE = 10.0      # seconds between heartbeat checks when online
CONNECT_TIMEOUT_SEC = 5.0        # TCP probe timeout
SESSION_CONNECT_TIMEOUT_MS = 10_000
SERVICE_WAIT_TIMEOUT_SEC = 90.0
SERVICE_RETRY_SEC = 0.5
SESSION_MANAGER_URL = os.environ.get("SESSION_MANAGER_URL", "").strip()
SAFE_STARTUP_VOLUME = int(os.environ.get("PEPPER_SAFE_STARTUP_VOLUME", "30"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [watchdog] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("watchdog")


# ── Helpers ────────────────────────────────────────────────────────

def parse_pepper_url() -> tuple[str, int]:
    """Get Pepper IP/port from PEPPER_QI_URL env var."""
    url = os.environ.get("PEPPER_QI_URL", "tcp://192.168.210.113:9559")
    # strip tcp://
    addr = url.replace("tcp://", "")
    if ":" in addr:
        host, port = addr.rsplit(":", 1)
        return host, int(port)
    return addr, NAOQI_PORT


def is_pepper_reachable(host: str, port: int) -> bool:
    """Quick TCP probe to check if NAOqi port is open."""
    try:
        with socket.create_connection((host, port), timeout=CONNECT_TIMEOUT_SEC):
            return True
    except (OSError, socket.timeout):
        return False


def safe(label: str, fn):
    """Run fn, log result or warning on failure."""
    try:
        res = fn()
        log.info("[ok] %s -> %s", label, res)
        return True, res
    except Exception as e:
        log.warning("[warn] %s failed: %s", label, e)
        return False, None


def clamp_volume(value: int) -> int:
    return max(0, min(100, int(value)))


def report_watchdog(
    summary: str,
    *,
    pepper_reachable: bool,
    safe_startup_running: bool,
    last_result: str = "",
    healthy: bool = True,
) -> None:
    if not SESSION_MANAGER_URL:
        return
    payload = {
        "summary": summary,
        "pepper_reachable": pepper_reachable,
        "safe_startup_running": safe_startup_running,
        "last_result": last_result,
        "healthy": healthy,
    }
    req = Request(
        f"{SESSION_MANAGER_URL.rstrip('/')}/api/watchdog-status",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        urlopen(req, timeout=1.0).read()
    except Exception:
        pass


def wait_service(session, name: str, timeout_sec: float = SERVICE_WAIT_TIMEOUT_SEC):
    """Block until a NAOqi service is available."""
    t0 = time.time()
    while True:
        try:
            svc = session.service(name)
            log.info("service ready: %s", name)
            return svc
        except Exception as e:
            if time.time() - t0 > timeout_sec:
                raise RuntimeError(f"Timeout waiting for service {name} (last error: {e})")
            time.sleep(SERVICE_RETRY_SEC)


def run_safe_startup(host: str, port: int) -> bool:
    """
    Connect to Pepper and run the safe startup sequence.
    Returns True on success, False on failure.
    """
    url = f"tcp://{host}:{port}"
    log.info("running safe startup on %s", url)

    session = qi.Session()
    try:
        fut = session.connect(url, _async=True)
        fut.value(SESSION_CONNECT_TIMEOUT_MS)
    except Exception as e:
        log.error("failed to connect to %s: %s", url, e)
        try:
            session.close()
        except Exception:
            pass
        return False

    log.info("connected, waiting for core services...")

    try:
        audio = wait_service(session, "ALAudioDevice")
        motion = wait_service(session, "ALMotion")
        life = wait_service(session, "ALAutonomousLife")
        posture = wait_service(session, "ALRobotPosture")

        # 1) Set speaker volume early so boot/recovery never comes up at 100.
        safe(f"ALAudioDevice.setOutputVolume({clamp_volume(SAFE_STARTUP_VOLUME)})",
             lambda: audio.setOutputVolume(clamp_volume(SAFE_STARTUP_VOLUME)))
        safe("ALAudioDevice.getOutputVolume()", lambda: audio.getOutputVolume())

        # 2) Disable diagnosis-effect reflex
        safe("ALMotion.setDiagnosisEffectEnabled(False)",
             lambda: motion.setDiagnosisEffectEnabled(False))

        # 3) Disable Autonomous Life
        safe("ALAutonomousLife.setState('disabled')",
             lambda: life.setState("disabled"))

        for a in ("AutonomousBlinking", "BackgroundMovement", "BasicAwareness",
                   "ListeningMovement", "SpeakingMovement"):
            safe(f"setAutonomousAbilityEnabled({a}, False)",
                 lambda aa=a: life.setAutonomousAbilityEnabled(aa, False))

        # 4) Wake and stand
        safe("ALMotion.wakeUp()", lambda: motion.wakeUp())
        safe("ALRobotPosture.goToPosture('StandInit', 0.6)",
             lambda: posture.goToPosture("StandInit", 0.6))

        # 5) Print diagnosis summary, best-effort only. Diagnosis is useful
        # for logs, but must not block the startup commands above when the
        # robot is already unstable.
        try:
            diag = wait_service(session, "ALDiagnosis", timeout_sec=5.0)
            safe("ALDiagnosis.getPassiveDiagnosis()", lambda: diag.getPassiveDiagnosis())
            safe("ALDiagnosis.getActiveDiagnosis()", lambda: diag.getActiveDiagnosis())
        except Exception as exc:
            log.warning("[warn] ALDiagnosis unavailable after safe startup: %s", exc)

        log.info("safe startup complete for %s", url)
        session.close()
        return True

    except Exception as e:
        log.error("safe startup failed: %s", e)
        try:
            session.close()
        except Exception:
            pass
        return False


# ── Main watchdog loop ─────────────────────────────────────────────

def main():
    host, port = parse_pepper_url()
    log.info("watchdog started — monitoring %s:%d", host, port)
    report_watchdog(
        "watchdog started",
        pepper_reachable=False,
        safe_startup_running=False,
        last_result="startup",
        healthy=True,
    )

    # If Pepper is already online when we start, skip safe_startup —
    # she's already running and we must not disrupt her.
    was_online = is_pepper_reachable(host, port)
    if was_online:
        log.info("Pepper already online at startup — skipping safe_startup, entering idle monitoring")
        report_watchdog(
            "Pepper already online",
            pepper_reachable=True,
            safe_startup_running=False,
            last_result="startup skipped",
            healthy=True,
        )
    else:
        log.info("Pepper offline at startup — waiting for her to come online")
        report_watchdog(
            "Waiting for Pepper",
            pepper_reachable=False,
            safe_startup_running=False,
            last_result="waiting for robot",
            healthy=False,
        )

    while True:
        online = is_pepper_reachable(host, port)

        if online and not was_online:
            # Pepper just came online — run safe startup
            log.info("Pepper is ONLINE at %s:%d — running safe startup", host, port)
            report_watchdog(
                "Running safe startup",
                pepper_reachable=True,
                safe_startup_running=True,
                last_result="startup in progress",
                healthy=True,
            )
            success = run_safe_startup(host, port)
            if success:
                was_online = True
                log.info("safe startup succeeded, entering idle monitoring")
                report_watchdog(
                    "Pepper online",
                    pepper_reachable=True,
                    safe_startup_running=False,
                    last_result="safe startup succeeded",
                    healthy=True,
                )
            else:
                log.warning("safe startup failed, will retry next cycle")
                # keep was_online = False so we retry
                report_watchdog(
                    "Safe startup failed",
                    pepper_reachable=True,
                    safe_startup_running=False,
                    last_result="safe startup failed",
                    healthy=False,
                )

        elif online and was_online:
            # Still online — idle heartbeat
            log.debug("heartbeat: Pepper still online")
            report_watchdog(
                "Pepper online",
                pepper_reachable=True,
                safe_startup_running=False,
                last_result="idle heartbeat",
                healthy=True,
            )

        elif not online and was_online:
            # Pepper went offline
            log.info("Pepper went OFFLINE — resuming polling every %.0fs", POLL_INTERVAL_OFFLINE)
            was_online = False
            report_watchdog(
                "Pepper offline",
                pepper_reachable=False,
                safe_startup_running=False,
                last_result="connection lost",
                healthy=False,
            )

        else:
            # Still offline
            log.debug("Pepper still offline, polling...")
            report_watchdog(
                "Waiting for Pepper",
                pepper_reachable=False,
                safe_startup_running=False,
                last_result="still offline",
                healthy=False,
            )

        interval = POLL_INTERVAL_ONLINE if was_online else POLL_INTERVAL_OFFLINE
        time.sleep(interval)


if __name__ == "__main__":
    main()
