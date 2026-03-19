#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Python 3.12 safe startup for Pepper with auto-discovery.

Discovery order:
  1. CLI argument:  python safe_startup3.py tcp://1.2.3.4:9559
  2. mDNS:          pepper.local (requires avahi-daemon on Linux)
  3. ARP scan:      link-local neighbors (169.254.x.x) with port 9559 open
  4. Subnet probe:  quick scan of 169.254.x.0/24 ranges on active link-local interfaces
"""

import socket
import subprocess
import sys
import time

import qi

NAOQI_PORT = 9559
CONNECT_RETRY_SEC = 1.0
DISCOVERY_RETRY_SEC = 3.0
SERVICE_RETRY_SEC = 0.5
SERVICE_WAIT_TIMEOUT_SEC = 90.0
SCAN_TIMEOUT_SEC = 0.3  # per-host TCP connect timeout during discovery
PORT_PROBE_TIMEOUT_SEC = 1.0  # longer timeout for known candidates (Pepper may be slow to open port)


# ---------------------------------------------------------------------------
# Discovery helpers
# ---------------------------------------------------------------------------

def try_mdns(hostname: str = "pepper.local") -> str | None:
    """Resolve mDNS hostname (needs avahi-daemon / systemd-resolved)."""
    try:
        info = socket.getaddrinfo(hostname, NAOQI_PORT, socket.AF_INET, socket.SOCK_STREAM)
        if info:
            ip = info[0][4][0]
            return ip
    except socket.gaierror:
        pass
    return None


def try_arp_neighbors() -> list[str]:
    """Return link-local IPs from the kernel ARP/neighbor table."""
    candidates = []
    try:
        out = subprocess.check_output(["ip", "neigh", "show"], text=True, timeout=5)
        for line in out.splitlines():
            parts = line.split()
            if not parts:
                continue
            ip = parts[0]
            if ip.startswith("169.254.") and (
                "REACHABLE" in line or "STALE" in line or "DELAY" in line
            ):
                candidates.append(ip)
    except Exception as e:
        print(f"[discover] ip neigh failed: {e}")
    return candidates


def get_link_local_interfaces() -> list[str]:
    """Return local 169.254.x.x addresses from active interfaces."""
    addrs = []
    try:
        out = subprocess.check_output(
            ["ip", "-4", "addr", "show", "scope", "link"], text=True, timeout=5
        )
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("inet 169.254."):
                addr = line.split()[1].split("/")[0]
                addrs.append(addr)
    except Exception as e:
        print(f"[discover] ip addr failed: {e}")
    return addrs


def probe_port(ip: str, port: int = NAOQI_PORT, timeout: float = SCAN_TIMEOUT_SEC) -> bool:
    """Quick TCP connect probe."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def scan_subnet(base_ip: str) -> str | None:
    """Scan 169.254.X.0/24 around a base IP for NAOqi port."""
    parts = base_ip.split(".")
    prefix = f"{parts[0]}.{parts[1]}.{parts[2]}."
    print(f"[discover] scanning {prefix}0/24 for port {NAOQI_PORT}...")
    for host in range(1, 255):
        ip = f"{prefix}{host}"
        if ip == base_ip:
            continue
        if probe_port(ip):
            print(f"[discover] found NAOqi at {ip}:{NAOQI_PORT}")
            return ip
    return None


def discover_pepper_once() -> str | None:
    """Single discovery attempt. Returns IP or None."""
    # 1. mDNS (most reliable — Pepper advertises pepper.local)
    for hostname in ("pepper.local", "nao.local"):
        ip = try_mdns(hostname)
        if ip:
            print(f"[discover] mDNS resolved {hostname} -> {ip}")
            if probe_port(ip, timeout=PORT_PROBE_TIMEOUT_SEC):
                return ip
            print(f"[discover] {ip} found via mDNS but port {NAOQI_PORT} not open yet")

    # 2. ARP neighbors
    for ip in try_arp_neighbors():
        if probe_port(ip, timeout=PORT_PROBE_TIMEOUT_SEC):
            print(f"[discover] found NAOqi at {ip}:{NAOQI_PORT} (ARP)")
            return ip

    # 3. Subnet scan around our own link-local address
    for local_ip in get_link_local_interfaces():
        ip = scan_subnet(local_ip)
        if ip:
            return ip

    return None


def discover_pepper_loop() -> str:
    """Retry discovery until Pepper is found. Blocks until success."""
    attempt = 0
    while True:
        attempt += 1
        if attempt == 1:
            print("[discover] searching for Pepper on the network...")
        else:
            print(f"[discover] attempt {attempt}, retrying...")
        ip = discover_pepper_once()
        if ip:
            return ip
        time.sleep(DISCOVERY_RETRY_SEC)


# ---------------------------------------------------------------------------
# Startup logic (same as safe_startup.py)
# ---------------------------------------------------------------------------

def wait_connect(session: qi.Session, url: str) -> qi.Session:
    attempt = 0
    while True:
        attempt += 1
        print(f"[wait] connect attempt {attempt} to {url}...")
        try:
            fut = session.connect(url, _async=True)
            print("[wait] waiting for connect future (timeout 10s)...")
            fut.value(10 * 1000)  # timeout in ms
            print("[wait] connected!")
            return session
        except Exception as e:
            print(f"[wait] attempt {attempt} failed ({type(e).__name__}): {e}")
            try:
                session.close()
            except Exception:
                pass
            session = qi.Session()
            time.sleep(CONNECT_RETRY_SEC)


def safe(label: str, fn):
    try:
        res = fn()
        print(f"[ok] {label} -> {res}")
        return True, res
    except Exception as e:
        print(f"[warn] {label} failed: {e}")
        return False, None


def wait_service(session, name: str, timeout_sec: float = SERVICE_WAIT_TIMEOUT_SEC):
    t0 = time.time()
    while True:
        try:
            svc = session.service(name)
            print(f"[info] service ready: {name}")
            return svc
        except Exception as e:
            if time.time() - t0 > timeout_sec:
                raise RuntimeError(f"Timeout waiting for service {name} (last error: {e})")
            time.sleep(SERVICE_RETRY_SEC)


def main():
    # Resolve target URL
    if len(sys.argv) > 1:
        url = sys.argv[1]
        if not url.startswith("tcp://"):
            url = f"tcp://{url}:{NAOQI_PORT}"
        print(f"[info] using provided URL: {url}")
    else:
        print("[info] no URL provided, waiting for Pepper to appear on the network...")
        print("[info] (you can power on Pepper now — this will keep retrying)")
        ip = discover_pepper_loop()
        url = f"tcp://{ip}:{NAOQI_PORT}"
        print(f"[info] discovered Pepper at {url}")

    session = qi.Session()
    print(f"[info] waiting for NAOqi at {url}")
    session = wait_connect(session, url)
    print("[info] connected; waiting for core services...")

    motion = wait_service(session, "ALMotion")
    life = wait_service(session, "ALAutonomousLife")
    posture = wait_service(session, "ALRobotPosture")
    diag = wait_service(session, "ALDiagnosis")

    # 1) Disable diagnosis-effect reflex
    safe("ALMotion.setDiagnosisEffectEnabled(False)",
         lambda: motion.setDiagnosisEffectEnabled(False))

    # 2) Disable Autonomous Life
    safe("ALAutonomousLife.setState('disabled')",
         lambda: life.setState("disabled"))

    for a in ("AutonomousBlinking", "BackgroundMovement", "BasicAwareness",
              "ListeningMovement", "SpeakingMovement"):
        safe(f"setAutonomousAbilityEnabled({a}, False)",
             lambda aa=a: life.setAutonomousAbilityEnabled(aa, False))

    # 3) Wake and stand
    safe("ALMotion.wakeUp()", lambda: motion.wakeUp())
    safe("ALRobotPosture.goToPosture('StandInit', 0.6)",
         lambda: posture.goToPosture("StandInit", 0.6))

    # 4) Print diagnosis summary
    safe("ALDiagnosis.getPassiveDiagnosis()", lambda: diag.getPassiveDiagnosis())
    safe("ALDiagnosis.getActiveDiagnosis()", lambda: diag.getActiveDiagnosis())

    print(f"[done] stabilization complete. Pepper at {url}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
