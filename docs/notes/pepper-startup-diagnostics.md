# Pepper startup investigation

The `safe-startup` service in both Docker Compose files launches
`robot/scripts/safe_startup_watchdog.py`. Inspection of this checkout shows:

- It probes TCP port 9559 every 5 seconds offline and 10 seconds online,
  with up to 5 seconds per probe. Initial reachability skips the sequence.
- After an offline-to-online transition, it connects to qi (10-second timeout)
  and waits for four core services (up to 90 seconds **each**).
- It sets speaker volume, disables the diagnosis-effect reflex, disables
  Autonomous Life and five autonomous abilities, wakes the motors, and
  requests StandInit. The script's volume default is 30; Compose overrides
  it to 100 unless configured otherwise.
- It reads diagnosis only after issuing the motion commands. Individual
  command exceptions are logged and swallowed: reported success does not
  establish that the sequence succeeded or that hardware is healthy.
- A network dropout can rearm the sequence even without a robot reboot.
  An open TCP port is not a readiness or health check.

This is diagnosis-reflex suppression, not a laser repair. The existing
`autostart_plan.md` proposes migrating that suppression onto the robot;
it is not a verified deployment. No robot was contacted in this investigation.

## Read-only diagnostic snapshot

`robot/scripts/onboard_diagnostics.py` queries local NAOqi without changing
robot state. It retries startup connection/service availability and exits
within a 120-second process deadline. It runs once per invocation, with no
watchdog, boot installation, or motion commands. It is an alternative for
investigating the fault, **not a replacement for the Docker wake sequence**.

From the repository root, replace the example address with Pepper's current IP:

```bash
PEPPER_HOST=192.168.210.113
scp robot/scripts/onboard_diagnostics.py "nao@${PEPPER_HOST}:/home/nao/onboard_diagnostics.py"
ssh "nao@${PEPPER_HOST}" '/usr/bin/python /home/nao/onboard_diagnostics.py' > pepper-diagnostics.json
```

Use Pepper's interpreter with the installed qi SDK. The script uses Python
2.7-compatible syntax, but the on-robot interpreter/SDK still needs verification.
Exit 0 means all queries returned, 1 means an incomplete snapshot, and 2 means
missing qi or an overall timeout. A timeout can leave the output file empty;
the error appears on stderr. No exit code certifies hardware health.

Capture a snapshot while the fault is present and give its diagnosis output
to the robot maintainer for sensor/connection troubleshooting. Prior reflex
suppression by the existing stack can affect interpretation of the snapshot.
The snapshot also reports whether the diagnosis reflex is enabled.

Manufacturer reference: [ALMotion reflexes](https://doc.aldebaran.com/2-8/naoqi/motion/almotion.html).
The documentation describes diagnosis-triggered responses to hardware errors;
the exact fault on this Pepper has not been verified.
