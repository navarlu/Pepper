# On-board safe-startup for Pepper (NAOqi 2.5)

Plan for moving the `safe-startup` hack off the RPi and onto Pepper herself, so she powers up correctly on her own.

## Context

Pepper's bottom laser-range sensor is broken, which makes her boot self-test fail — she halts unless the `safe-startup` Docker service on the RPi connects and runs the wake sequence (suppress diagnosis reflex, disable Autonomous Life, wake up, StandInit, set volume).

This can run on the robot instead: NAOqi 2.5 on-board has Python 2.7 with the `qi` module available, and its PackageManager/ServiceManager auto-starts services declared with `autorun="true"` in an installed application package (`.pkg` = flat zip with `manifest.xml` at root). This is the officially supported mechanism (verified against NAOqi 2.5 docs: `doc.aldebaran.com/2-5/dev/tutos/create_a_new_service.html`, `packagemanager-api.html`, `servicemanager-api.html`). cron/systemd are not options (`nao` user has no root; sudo only allows shutdown).

Decision: the RPi `safe-startup` compose service is **kept but disabled** (behind a compose profile), available as a manual fallback.

## New files

### 1. `robot/onboard/safe_startup_pkg/safe_startup_onboard.py` — Python 2.7 one-shot script

Port of `run_safe_startup()` from `robot/scripts/safe_startup_watchdog.py:131-202` (keep the `safe()` never-raise helper and `wait_service()` loop; drop the watchdog polling — on-board it runs once per NAOqi start, which is exactly when re-waking is needed).

Python 2.7 constraints: no f-strings (use `%` formatting), no type annotations, no `10_000` literals, sockets aren't context managers, add `from __future__ import print_function`. Header `#!/usr/bin/python`.

Logic:
1. Retry-connect `qi.Session()` to `tcp://127.0.0.1:9559` (1 s retry, ~120 s budget — ServiceManager launches autorun services early, possibly before the port is fully up).
2. `wait_service` for `ALAudioDevice`, `ALMotion`, `ALAutonomousLife`, `ALRobotPosture` (90 s / 0.5 s, as today).
3. `setOutputVolume(100)` — volume becomes a module constant (`SAFE_STARTUP_VOLUME = 100`, matching compose); no env injection on-board.
4. `ALMotion.setDiagnosisEffectEnabled(False)` — the laser suppression.
5. `ALAutonomousLife.setState("disabled")` with a **verify-and-retry loop** (`getState() == "disabled"`, ≤10 tries × 1 s) — at boot, Autonomous Life runs its own wake-up and races us; verify before proceeding. Then disable the five autonomous abilities as today.
6. `ALMotion.wakeUp()` + `goToPosture("StandInit", 0.6)`.
7. Best-effort `ALDiagnosis.getPassiveDiagnosis()/getActiveDiagnosis()` logging (5 s wait).
8. Log "safe startup complete", `sys.exit(0)` always (never leave a "crashed" service).

Logging (observability): `logging` with both `StreamHandler` (captured by NAOqi in `/var/log/naoqi/servicemanager`) and `FileHandler("/home/nao/safe_startup_onboard.log")` (append; truncate at start if > 1 MB). Log a timestamped "boot run started" first line and every step `[ok]`/`[warn]` as today.

### 2. `robot/onboard/safe_startup_pkg/manifest.xml`

```xml
<package uuid="safe-startup-onboard" version="0.1.0">
  <services>
    <service name="safestartup" autorun="true"
             execStart="/usr/bin/python safe_startup_onboard.py" />
    <executableFiles>
      <file path="safe_startup_onboard.py" />
    </executableFiles>
  </services>
</package>
```

`execStart` script path resolves relative to the installed package root (`/home/nao/.local/share/PackageManager/apps/safe-startup-onboard/`). `executableFiles` is required or exec fails with Permission denied. Service handle: `safe-startup-onboard.safestartup`.

### 3. `robot/onboard/deploy_onboard.sh` — build + install over SSH

Global vars at top (no argparse): `PEPPER_HOST` (default `192.168.210.113`, overridable via env). Steps:
1. **Preflight**: `ssh nao@$PEPPER_HOST "/usr/bin/python --version && /usr/bin/python -c 'import qi' && which qicli"` — empirically confirm the on-board runtime before anything else.
2. **Build**: `zip -j /tmp/safe-startup-onboard.pkg manifest.xml safe_startup_onboard.py` (flat zip = valid .pkg; no qipkg toolchain needed).
3. **Copy**: `scp` to `/home/nao/`.
4. **Install** (idempotent): `qicli call PackageManager.removePkg safe-startup-onboard` (ignore failure) then `qicli call PackageManager.install /home/nao/safe-startup-onboard.pkg`, then delete the pkg file.
5. **Verify + smoke run**: `qicli call PackageManager.hasPackage safe-startup-onboard`, then `qicli call ALServiceManager.startService safe-startup-onboard.safestartup`, sleep ~15 s, `tail -n 30 /home/nao/safe_startup_onboard.log`.

## Modified files

### 4. `docker/docker-compose.experiment.yml` — RPi service kept but disabled

Add `profiles: ["fallback"]` to the `safe-startup` service (line ~107). It stops starting with plain `up -d`; manual start when needed:

```bash
docker compose -f docker/docker-compose.experiment.yml --profile fallback up -d safe-startup
```

Update the service's comment block to say the on-board package is now primary. (Note: `docker-compose.experiment.yml` has uncommitted local changes — touch only the safe-startup block.)

### 5. `docs/modules/safe_startup.md` — document the new setup

On-board package is primary: install path on robot, log locations (`/home/nao/safe_startup_onboard.log`, `/var/log/naoqi/servicemanager`), redeploy command, the fact that volume is now a baked-in constant, the compose fallback profile, and that a robot system update/factory reset wipes packages (re-run `deploy_onboard.sh`).

## Verification (end-to-end)

1. Run `robot/onboard/deploy_onboard.sh` from the RPi — preflight, install, and smoke run must succeed; log shows the full sequence.
2. Ensure the RPi `safe-startup` container is stopped (it will be, once behind the profile).
3. Reboot Pepper (`ssh nao@$PEPPER_HOST "sudo shutdown -r now"` is permitted for the nao user) and observe: she boots, doesn't halt on the laser self-test, wakes, reaches StandInit, volume 100, Autonomous Life quiet.
4. Check state: `qicli call ALAutonomousLife.getState` → `disabled`; `qicli call ALMotion.robotIsWakeUp` → true; fresh timestamped run in `/home/nao/safe_startup_onboard.log`.
5. Full power-off cold boot to confirm the broken-laser self-test path specifically.

## Risks / notes

- Runs on **every NAOqi restart**, not only cold boot — desired on-board (a NAOqi restart needs re-waking anyway; the RPi watchdog's "skip if already online" logic protected against *container* restarts, which don't exist here).
- All calls are idempotent; the setState verify-loop bounds the only real race (Life's own boot wake-up).
- Package survives robot reboots but **not** system updates/factory reset — deploy script is one-command re-runnable, and the compose fallback profile + manual CLI (`robot/scripts/safe_startup.py`) remain as recovery paths.
