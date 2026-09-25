# Safe startup — onboard primary and RPi fallback

Safe-startup puts Pepper into a known baseline after NAOqi starts:

1. Run the wake sequence locally on Pepper — disable the diagnosis reflex and
   Autonomous Life, `wakeUp()`, stand to `StandInit`, and dump diagnosis.
2. Verify volume, reflex state, Autonomous Life, wake state, and posture.
3. Exit. NAOqi ServiceManager launches it again on the next NAOqi start.

The onboard package is the primary mechanism. The old RPi Docker watchdog is
retained behind the Compose `fallback` profile for manual recovery only. This
prevents an RPi/container restart or transient network interruption from
re-running motion commands on an already-running robot.

---

## Big picture

```
 Pepper / NAOqi start
          │
          ▼
 ServiceManager autorun
          │
          ▼
 safe_startup_onboard.py → local NAOqi at 127.0.0.1:9559:
   disable diagnosis-effect reflex
   ALAutonomousLife.setState("disabled")
   turn off all 5 autonomous abilities
   ALMotion.wakeUp()
   ALRobotPosture.goToPosture("StandInit", 0.6)
   dump passive + active diagnosis
```

The onboard program retries local connection and service discovery because
ServiceManager can launch it before all NAOqi services are ready. Individual
robot calls have client-side deadlines, and final state is queried explicitly.
Failures are preserved in the log rather than being reported as success.

---

## Code layout

Primary onboard files:

| File | Role |
|------|------|
| [robot/onboard/safe_startup_pkg/safe_startup_onboard.py](../../robot/onboard/safe_startup_pkg/safe_startup_onboard.py) | Python 2.7 one-shot service. Uses Pepper's `/opt/aldebaran` qi runtime and connects locally. |
| [robot/onboard/safe_startup_pkg/manifest.xml](../../robot/onboard/safe_startup_pkg/manifest.xml) | NAOqi package manifest; registers the autorun service `safe-startup-onboard.safestartup`. |
| [robot/onboard/deploy_onboard.sh](../../robot/onboard/deploy_onboard.sh) | Builds the `.pkg`, installs it over SSH, starts a smoke run, and prints its persistent log. |

RPi fallback and diagnostic tools:

| File                                                                              | Role |
|-----------------------------------------------------------------------------------|------|
| [robot/scripts/safe_startup_watchdog.py](../../robot/scripts/safe_startup_watchdog.py) | Manual Docker fallback (`safe-startup`, profile `fallback`). Polls over the network and contains its own wake sequence. |
| [robot/scripts/safe_startup.py](../../robot/scripts/safe_startup.py)                    | **The host-side CLI tool.** Same wake sequence + Pepper auto-discovery (mDNS / ARP / subnet scan). Hardcoded qi paths for the host. Useful when you want to manually wake Pepper from the RPi shell, or when you don't know her IP yet. |

> ⚠️ The two files duplicate the NAOqi wake sequence — the watchdog
> does **not** shell out to `safe_startup.py`. They share intent, not
> code. If you change the wake sequence, change it in both. A cleaner
> split would be to extract the sequence into a shared module and call
> it from both; not done yet.

---

## Install or update the onboard package

Run from the repository root on the RPi. Override the address because it can
change between Pepper's hotspot and another network:

```bash
PEPPER_HOST=10.42.0.205 robot/onboard/deploy_onboard.sh
```

The deployment performs a read-only runtime preflight before copying anything,
then replaces the package idempotently and starts one smoke run. Its two log
locations are:

- `/home/nao/safe_startup_onboard.log` — persistent, capped by truncating it
  before a run when it exceeds 1 MiB.
- `/var/log/naoqi/servicemanager` — ServiceManager's captured process output.

`SAFE_STARTUP_VOLUME` is a constant in the onboard script; Compose environment
variables do not reach an onboard package. A robot system update or factory
reset can remove locally installed packages; rerun the deployment script.

After a successful smoke run, stop the currently running RPi fallback before
testing a reboot. Merely adding the Compose profile does not stop a container
that was already running:

```bash
docker compose -f docker/docker-compose.experiment.yml stop safe-startup
```

Installation and a smoke run do not prove boot behavior. Complete a supervised
reboot and then a cold boot, checking for a fresh `boot run started` timestamp
and `safe startup complete: final state verified` in the persistent log.

## The fallback watchdog loop — [`safe_startup_watchdog.py`](../../robot/scripts/safe_startup_watchdog.py)

### State machine

Two flags drive everything: `was_online` (what we last saw) and
`online` (the current probe result).

| Transition                | What happens                                         |
|---------------------------|------------------------------------------------------|
| `offline → offline`       | Log + sleep `POLL_INTERVAL_OFFLINE` (5s).            |
| `offline → online`        | **Run the wake sequence.** On success, `was_online = True`. On failure, keep `was_online = False` so we retry next tick. |
| `online → online`         | Idle heartbeat, sleep `POLL_INTERVAL_ONLINE` (10s).  |
| `online → offline`        | Flip `was_online = False`, resume fast polling.      |

**Boot edge case.** If Pepper is **already online** when the watchdog
starts, it deliberately **skips** `run_safe_startup()` and enters idle
monitoring immediately. This matters — if a session is already in
progress and the container restarts, we must not kick the robot back
into StandInit mid-conversation.

### The wake sequence — `run_safe_startup(host, port)`

Connects a fresh `qi.Session` with a 10s timeout, then performs the
following steps, each wrapped in a `safe(...)` helper that logs
success/failure but never raises (partial-success is better than
crashing the whole watchdog):

1. **`ALMotion.setDiagnosisEffectEnabled(False)`** — disable the
   "robot reacts to joint-diagnosis issues" reflex. Without this,
   Pepper sometimes freezes mid-animation if a joint reports a
   transient warning.
2. **`ALAutonomousLife.setState("disabled")`** + disable all five
   autonomous abilities (`AutonomousBlinking`, `BackgroundMovement`,
   `BasicAwareness`, `ListeningMovement`, `SpeakingMovement`). This
   hands control fully to the bridge. The bridge's own
   `TOUCH_AUTONOMOUS_LIFE` flag can later re-enable abilities on a
   per-ability basis — see [bridge.md](bridge.md).
3. **`ALMotion.wakeUp()`** — stiffen joints, power the motors.
4. **`ALRobotPosture.goToPosture("StandInit", 0.6)`** — safe standing
   pose, speed 0.6 (deliberately slow to avoid alarming anyone
   nearby).
5. **`ALDiagnosis.getPassiveDiagnosis()` / `getActiveDiagnosis()`** —
   log whatever the robot reports. Purely observational; doesn't gate
   anything.

### Optional reporting — `SESSION_MANAGER_URL`

If the env var is set, the watchdog POSTs a JSON status to
`<url>/api/watchdog-status` on every tick:

```json
{
  "summary": "Pepper online",
  "pepper_reachable": true,
  "safe_startup_running": false,
  "last_result": "idle heartbeat",
  "healthy": true
}
```

> ⚠️ **This is a legacy hook.** The system used to have a
> session-manager HTTP service; it's since been replaced by the
> lighter [services/src/live/orchestrator.py](../../services/src/live/orchestrator.py),
> which does **not** expose `/api/watchdog-status`. Today `SESSION_MANAGER_URL`
> is unset in compose, so `report_watchdog()` is a no-op. Keep or
> delete the hook — both are fine.

---

## The standalone CLI — [`safe_startup.py`](../../robot/scripts/safe_startup.py)

Same wake sequence as the watchdog, with two extras that matter when
running from the host shell rather than Docker:

### Auto-discovery

If no URL is passed on the command line, the script blocks and
searches for Pepper in this order:

1. **mDNS** — resolve `pepper.local` / `nao.local` via the system
   resolver (avahi or systemd-resolved). Most reliable when it works.
2. **ARP neighbors** — parse `ip neigh show`, pick any `169.254.x.x`
   in state REACHABLE/STALE/DELAY, TCP-probe port 9559.
3. **Subnet scan** — for each active link-local interface, scan the
   full `169.254.X.0/24` around it with a 0.3s per-host TCP timeout.

The loop retries every `DISCOVERY_RETRY_SEC` (3s), so it's safe to
start this script *before* powering Pepper on — it'll pick her up
when she appears.

Usage:

```bash
# explicit URL
uv run python robot/scripts/safe_startup.py tcp://192.168.210.113:9559

# bare IP (port defaults to 9559)
uv run python robot/scripts/safe_startup.py 192.168.210.113

# no argument → auto-discovery loop
uv run python robot/scripts/safe_startup.py
```

### qi library paths

Unlike the watchdog (which reads `PYTHONPATH` / `LD_LIBRARY_PATH`
from env, populated by the Docker volume mounts), this script has
those paths **hardcoded at the top of the file**:

```python
QI_LIB_PATH  = "/home/lucas/Projects/FEL/QI_test/libqi-python/build/build/linux-armv8-gcc-release"
BOOST_LIB_PATH = "/home/lucas/.conan2/p/b/boost00dddf9f5dc9e/p/lib"
```

It also `os.execv`'s itself after setting `LD_LIBRARY_PATH`, because
`qi_python.so` resolves its boost dependency at import time and the
dynamic linker won't pick up a mid-process env change. If you clone
this repo to a different machine, those paths need updating.

See [rpi-dev.md](../notes/rpi-dev.md) for the qi build story.

---

## Docker fallback wiring

Defined in
[docker/docker-compose.experiment.yml](../../docker/docker-compose.experiment.yml)
as the `safe-startup` service. It is not part of a normal `up -d`:

```bash
docker compose -f docker/docker-compose.experiment.yml --profile fallback up -d safe-startup
```

The service includes:

```yaml
safe-startup:
  profiles: ["fallback"]
  build:
    context: ..
    dockerfile: docker/Dockerfile.runtime
  working_dir: /workspace
  command: ["uv", "run", "python", "robot/scripts/safe_startup_watchdog.py"]
  volumes:
    - ..:/workspace
    - /home/lucas/Projects/FEL/QI_test/libqi-python/build/build/linux-armv8-gcc-release:/opt/qi
    - /home/lucas/.conan2/p/b/boost00dddf9f5dc9e/p/lib:/opt/boost-lib
    - /home/lucas/Projects/FEL/QI_test/local_qi/lib:/opt/qi-native-lib
  environment:
    PYTHONPATH: /opt/qi
    LD_LIBRARY_PATH: /opt/boost-lib:/opt/qi-native-lib
    PEPPER_QI_URL: ${PEPPER_QI_URL:-tcp://192.168.210.113:9559}
  network_mode: host
  restart: unless-stopped
```

Key points:

- **`network_mode: host`** — needed because the container reaches
  Pepper on a link-local / LAN IP. Without host networking, the TCP
  probe to 9559 would go through Docker's bridge NAT.
- **qi volumes** — the self-built ARM64 qi and its boost dependency
  are mounted read-only into `/opt/...`. The env vars `PYTHONPATH` /
  `LD_LIBRARY_PATH` point the Python interpreter at them.
- **`PEPPER_QI_URL`** — the only runtime knob. Same value as the
  bridge container so both point at the same robot.
- **`restart: unless-stopped`** — once manually enabled, the fallback watchdog
  runs forever;
  if it crashes, compose restarts it. If Pepper is already online at
  restart, the "skip safe_startup at boot" branch kicks in.

The same runtime image (`docker/Dockerfile.runtime`) is used by the
bridge container — the two services differ only in `command` and
volumes.

---

## Interaction with the rest of the stack

- **Bridge** — independently connects to Pepper via `qi` through
  [robot/src/utils.py#connect_session](../../robot/src/utils.py). The
  bridge tolerates Pepper being offline (retries forever), so the
  ordering between bridge-up and safe-startup-completed doesn't
  matter strictly; what matters is that **by the time the agent
  connects, Pepper is awake and standing**, which safe-startup
  guarantees.
- **Orchestrator / voice-agent** — never call safe-startup directly.
  They assume the robot is ready.
- **`TOUCH_AUTONOMOUS_LIFE`** — safe-startup always **disables** all
  autonomous abilities. The bridge can later re-enable specific ones
  (blinking, background movement, speaking movement) via its
  `LIFE_*` config flags, but only if `TOUCH_AUTONOMOUS_LIFE=True`.
  The intent: safe-startup puts Pepper in a known-quiet state;
  the bridge selectively reintroduces motion.

---

## Config reference

Tunables in [safe_startup_watchdog.py](../../robot/scripts/safe_startup_watchdog.py)
(module-level constants, not env-driven except `PEPPER_QI_URL` and
`SESSION_MANAGER_URL`):

| Name                          | Default | Purpose                                 |
|-------------------------------|---------|-----------------------------------------|
| `NAOQI_PORT`                  | 9559    | TCP port of Pepper's NAOqi daemon.      |
| `POLL_INTERVAL_OFFLINE`       | 5s      | Probe interval while Pepper is offline. |
| `POLL_INTERVAL_ONLINE`        | 10s     | Heartbeat interval while she's up.      |
| `CONNECT_TIMEOUT_SEC`         | 5s      | Per-probe TCP timeout.                  |
| `SESSION_CONNECT_TIMEOUT_MS`  | 10000   | Timeout for `qi.Session.connect`.       |
| `SERVICE_WAIT_TIMEOUT_SEC`    | 90s     | How long to wait for each NAOqi service. |
| `SERVICE_RETRY_SEC`           | 0.5s    | Polling interval for service readiness. |

Env vars:

| Name                  | Purpose                                                      |
|-----------------------|--------------------------------------------------------------|
| `PEPPER_QI_URL`       | `tcp://<host>:<port>`. Same default as the bridge.           |
| `PYTHONPATH`          | Path to self-built `qi_python.so` (set by compose).          |
| `LD_LIBRARY_PATH`     | Paths to boost + qi native libs (set by compose).            |
| `SESSION_MANAGER_URL` | Legacy watchdog-status hook. Unused in current deployment.   |
