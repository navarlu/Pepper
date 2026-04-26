#!/usr/bin/env python3
"""
Pepper IP discovery + safe_startup runner — host-side, no docker dependency.

Strategy (all in parallel):
  1. Probe well-known IPs (lab DHCP, last-known) — fastest if she's at her
     usual address.
  2. Sweep every IPv4 subnet on eth0 (so it works wherever the RPi lives).
  3. Sweep 169.254.0.0/16 — covers direct-cable APIPA fallback.
  4. Watch `ip neigh show dev eth0` for any entry whose MAC starts with the
     Aldebaran/SoftBank OUI prefix `00:13:95:` — that's Pepper.

Once an IP is confirmed (TCP 9559 reachable), run the disable-autonomy
sequence directly via host-side qi. Prints the final
`PEPPER_QI_URL=tcp://<ip>:9559` so the operator can plug it into docker.

Usage (from project root):
    uv run python robot/scripts/pepper_catch_fire.py             # full auto-discover
    uv run python robot/scripts/pepper_catch_fire.py 192.168.210.113   # hint a known IP
"""
import os, sys, time, socket, subprocess, threading, ipaddress

IFACE = "eth0"
NAOQI_PORT = 9559
SAFE_STARTUP_VOLUME = int(os.environ.get("PEPPER_SAFE_STARTUP_VOLUME", "30"))

# Pepper's MAC OUI (Aldebaran / SoftBank). Used to identify her in the ARP
# table without false-matching the gateway/switch/other devices.
PEPPER_OUI_PREFIX = "00:13:95"

# Well-known IPs to probe directly (instant if she's at her usual address)
KNOWN_IPS = [
    "192.168.210.113",   # original lab DHCP lease
]

# Our own addresses to ignore
OUR_IPS = {"10.0.0.200", "169.254.1.1"}

# Host-side qi library locations (override via env if your build lives elsewhere)
QI_PY_PATH = os.environ.get("PEPPER_QI_PY_PATH",
    "/home/lucas/Projects/FEL/QI_test/libqi-python/build/build/linux-armv8-gcc-release")
BOOST_LIB  = os.environ.get("PEPPER_BOOST_LIB",
    "/home/lucas/.conan2/p/b/boost00dddf9f5dc9e/p/lib")
QI_NATIVE  = os.environ.get("PEPPER_QI_NATIVE_LIB",
    "/home/lucas/Projects/FEL/QI_test/local_qi/lib")

# Re-exec ourselves with proper LD_LIBRARY_PATH if missing (qi native .so)
_needed_ld = f"{BOOST_LIB}:{QI_NATIVE}"
if _needed_ld not in os.environ.get("LD_LIBRARY_PATH", ""):
    env = os.environ.copy()
    env["LD_LIBRARY_PATH"] = _needed_ld + (":" + env["LD_LIBRARY_PATH"] if env.get("LD_LIBRARY_PATH") else "")
    env["PYTHONPATH"] = QI_PY_PATH + (":" + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")
    os.execvpe(sys.executable, [sys.executable, *sys.argv], env)

sys.path.insert(0, QI_PY_PATH)


def log(msg):
    print(f"{time.strftime('%H:%M:%S.')}{int((time.time()%1)*1000):03d} {msg}", flush=True)


def clamp_volume(value):
    return max(0, min(100, int(value)))


# ── Subnet detection on eth0 ─────────────────────────────────────────
def detect_eth0_subnets():
    """Return list of ipaddress.IPv4Network for every IPv4 addr on eth0."""
    subnets = []
    try:
        out = subprocess.run(
            ["ip", "-4", "-o", "addr", "show", "dev", IFACE],
            capture_output=True, text=True, timeout=2,
        ).stdout
    except Exception:
        return subnets
    for line in out.splitlines():
        for tok in line.split():
            if "/" in tok and tok.count(".") == 3:
                try:
                    net = ipaddress.IPv4Network(tok, strict=False)
                    if net.prefixlen >= 16:
                        subnets.append(net)
                except ValueError:
                    pass
    return subnets


def find_pepper_ip():
    """Look for any neighbor on eth0 whose MAC starts with Pepper's OUI."""
    try:
        out = subprocess.run(
            ["ip", "neigh", "show", "dev", IFACE],
            capture_output=True, text=True, timeout=1,
        ).stdout
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if not parts:
            continue
        ip, state = parts[0], parts[-1]
        if ip in OUR_IPS or state in ("FAILED", "INCOMPLETE"):
            continue
        for i, tok in enumerate(parts):
            if tok == "lladdr" and i + 1 < len(parts):
                mac = parts[i + 1].lower()
                if mac.startswith(PEPPER_OUI_PREFIX.lower()):
                    return ip
    return None


def port_open(ip, port, timeout=0.5):
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


# ── Background sweepers (active probe → kernel learns ARP passively) ─
_sweep_stop = threading.Event()


def sweep_subnet(net: ipaddress.IPv4Network, batch=128):
    hosts = [str(h) for h in net.hosts()]
    i = 0
    while i < len(hosts) and not _sweep_stop.is_set():
        end = min(i + batch, len(hosts))
        procs = []
        for ip in hosts[i:end]:
            if _sweep_stop.is_set():
                break
            procs.append(subprocess.Popen(
                ["ping", "-c", "1", "-W", "1", "-q", "-n", ip],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            ))
        for p in procs:
            try: p.wait(timeout=2)
            except Exception: p.kill()
        i = end


def sweep_loop(networks):
    while not _sweep_stop.is_set():
        for net in networks:
            if _sweep_stop.is_set():
                return
            sweep_subnet(net)


def known_ip_probe_loop():
    """Hammer KNOWN_IPS in parallel — fastest path if she's at her usual addr."""
    while not _sweep_stop.is_set():
        for ip in KNOWN_IPS:
            if _sweep_stop.is_set():
                return
            subprocess.Popen(
                ["ping", "-c", "1", "-W", "1", "-q", "-n", ip],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        time.sleep(0.5)


# ── Safe startup sequence via qi ─────────────────────────────────────
def run_safe_startup(ip):
    import qi
    url = f"tcp://{ip}:{NAOQI_PORT}"
    log(f"[safe-start] qi.Session().connect({url})")
    session = qi.Session()
    try:
        session.connect(url, _async=True).value(10000)
    except Exception as e:
        log(f"[safe-start] CONNECT FAILED: {e}")
        return False
    log("[safe-start] connected; grabbing services…")

    def wait_svc(name, tmax=30):
        t0 = time.time()
        while True:
            try:
                svc = session.service(name)
                log(f"[safe-start] service ready: {name} ({time.time()-t0:.1f}s)")
                return svc
            except Exception as e:
                if time.time() - t0 > tmax:
                    log(f"[safe-start] TIMEOUT waiting for {name}: {e}")
                    return None
                time.sleep(0.2)

    audio  = wait_svc("ALAudioDevice")
    life   = wait_svc("ALAutonomousLife", tmax=60)
    motion = wait_svc("ALMotion")
    post   = wait_svc("ALRobotPosture")

    def trycall(label, fn):
        try:
            r = fn()
            log(f"[safe-start] OK   {label} -> {r}")
        except Exception as e:
            log(f"[safe-start] WARN {label}: {e}")

    if audio:
        volume = clamp_volume(SAFE_STARTUP_VOLUME)
        trycall(f"audio.setOutputVolume({volume})",
                lambda: audio.setOutputVolume(volume))
        trycall("audio.getOutputVolume()",
                lambda: audio.getOutputVolume())
    if life:
        trycall("life.setState('disabled')",
                lambda: life.setState("disabled"))
        for a in ("AutonomousBlinking","BackgroundMovement","BasicAwareness",
                  "ListeningMovement","SpeakingMovement"):
            trycall(f"life.setAutonomousAbilityEnabled({a}, False)",
                    lambda aa=a: life.setAutonomousAbilityEnabled(aa, False))
    if motion:
        trycall("motion.setDiagnosisEffectEnabled(False)",
                lambda: motion.setDiagnosisEffectEnabled(False))
        trycall("motion.wakeUp()", lambda: motion.wakeUp())
    if post:
        trycall("post.goToPosture('StandInit', 0.6)",
                lambda: post.goToPosture("StandInit", 0.6))

    log("[safe-start] DONE ✓")
    log(f"[safe-start] >>> Pepper IP: {ip}  ->  PEPPER_QI_URL=tcp://{ip}:9559")
    try: session.close()
    except Exception: pass
    return True


def race_port_then_fire(ip, source):
    """Wait for NAOqi 9559 to open on `ip`, then fire safe_startup."""
    log(f"[catch] >>> candidate {ip} from {source} — racing NAOqi port {NAOQI_PORT}")
    _sweep_stop.set()
    t0 = time.time()
    while time.time() - t0 < 180:
        if port_open(ip, NAOQI_PORT, timeout=0.3):
            log(f"[catch] NAOqi OPEN on {ip} after {time.time()-t0:.2f}s — FIRING")
            return run_safe_startup(ip)
        time.sleep(0.1)
    log(f"[catch] TIMEOUT: port {NAOQI_PORT} never opened on {ip}")
    return False


def main():
    hint = sys.argv[1] if len(sys.argv) > 1 else None

    if hint:
        log(f"[catch] hint mode — probing {hint} directly")
        return 0 if race_port_then_fire(hint, "hint") else 1

    detected = detect_eth0_subnets()
    sweep_nets = [ipaddress.IPv4Network("169.254.0.0/16")]
    for net in detected:
        if net.network_address != ipaddress.IPv4Address("169.254.0.0"):
            sweep_nets.append(net)

    log(f"[catch] eth0 subnets to sweep: {[str(n) for n in sweep_nets]}")
    log(f"[catch] known IP probes: {KNOWN_IPS}")
    log(f"[catch] looking for MAC starting with {PEPPER_OUI_PREFIX}:*")

    threading.Thread(target=sweep_loop, args=(sweep_nets,), daemon=True).start()
    threading.Thread(target=known_ip_probe_loop, daemon=True).start()

    log("[catch] watching `ip neigh show dev eth0` — POWER ON PEPPER NOW")
    last_hb = time.time()
    while True:
        for kip in KNOWN_IPS:
            if port_open(kip, NAOQI_PORT, timeout=0.3):
                return 0 if race_port_then_fire(kip, "known_ip_match") else 2

        ip = find_pepper_ip()
        if ip:
            return 0 if race_port_then_fire(ip, "MAC OUI match") else 2

        if time.time() - last_hb > 5:
            last_hb = time.time()
            log("[catch] still watching eth0…")
        time.sleep(0.2)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("[catch] interrupted")
        sys.exit(130)
