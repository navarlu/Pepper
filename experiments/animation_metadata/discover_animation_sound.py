"""Discover which audio path a sound-carrying animation actually uses.

Muting NAOqi's persistent PulseAudio sink-input (qi-AudioDeviceManagerPulse)
did NOT silence Sneeze, so the sound travels some other way. This script
finds out which, in one run:

  1. Unmutes NAOqi's persistent sink-input (clean baseline — we WANT the
     sound to play so we can observe its route).
  2. Locates the installed behavior on Pepper's disk and lists any sound
     files it ships (wav/ogg/mp3 referenced by the .xar / present in the
     package). If the "achoo" is a file, renaming it silences the animation
     forever without touching any volume.
  3. Snapshots `pactl list sink-inputs` at ~5 Hz on the robot WHILE the
     animation plays, then reports every sink-input that appeared or
     un-corked during playback — that's the stream carrying the sound.

Run it, then paste the full output back to Kampion.

Run (on the RPi, host-side):
    python3 experiments/animation_metadata/discover_animation_sound.py
"""

import re
import subprocess
import threading
import time

import urllib.request

# --- Configuration ---------------------------------------------------------
BRIDGE_URL = "http://localhost:5000"
ANIMATION_NAME = "Sneeze"
PEPPER_SSH_HOST = "10.42.0.205"
PEPPER_SSH_USER = "nao"
PEPPER_SSH_PASSWORD = "Argus"
SAMPLE_SNAPSHOTS = 40       # pactl snapshots taken on the robot
SAMPLE_INTERVAL_SEC = 0.2   # ~8 s of coverage total
TRIGGER_DELAY_SEC = 1.5     # sampler runs this long before the gesture fires
APPS_DIR = "/home/nao/.local/share/PackageManager/apps"
# ---------------------------------------------------------------------------

SNAP_MARK = "---SNAP---"


def ssh_run(remote_cmd, timeout=30.0):
    cmd = [
        "sshpass", "-p", PEPPER_SSH_PASSWORD,
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "%s@%s" % (PEPPER_SSH_USER, PEPPER_SSH_HOST),
        remote_cmd,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.returncode, result.stdout, result.stderr


def step_unmute_naoqi_stream():
    print("=== 1. restore clean baseline (unmute NAOqi sink-input) ===")
    code, out, err = ssh_run(
        'idx=$(pactl list sink-inputs | awk "/^Sink Input #/{i=substr(\\$3,2)}'
        ' /qi-AudioDeviceManagerPulse/{print i}");'
        ' if [ -n "$idx" ]; then pactl set-sink-input-mute $idx 0 &&'
        ' echo "unmuted sink-input $idx"; else echo "naoqi sink-input not found"; fi'
    )
    print((out or err).strip())


def step_find_behavior_sounds():
    print("\n=== 2. behavior package on disk ===")
    code, out, err = ssh_run(
        'find %s -type d -iname "*%s*" 2>/dev/null' % (APPS_DIR, ANIMATION_NAME)
    )
    dirs = [line for line in out.splitlines() if line.strip()]
    if not dirs:
        print("no behavior directory matching '%s' found under %s" % (ANIMATION_NAME, APPS_DIR))
        return
    for behavior_dir in dirs:
        print("behavior dir: %s" % behavior_dir)
        code, out, err = ssh_run('find "%s" -type f | head -40' % behavior_dir)
        print("  files:")
        for line in out.splitlines():
            print("    %s" % line)
        # Sound references inside the behavior definition (played via a
        # Play Sound box) — plus any audio files shipped in the package.
        code, out, err = ssh_run(
            'grep -ohrE "[A-Za-z0-9_/.-]+\\.(wav|ogg|mp3)" "%s" 2>/dev/null | sort -u'
            % behavior_dir
        )
        refs = [line for line in out.splitlines() if line.strip()]
        print("  sound references in behavior files: %s" % (refs or "NONE"))


PACTL_SAMPLER = (
    'for i in $(seq 1 %d); do pactl list sink-inputs; echo "%s"; sleep %s; done'
    % (SAMPLE_SNAPSHOTS, SNAP_MARK, SAMPLE_INTERVAL_SEC)
)


def parse_snapshot(text):
    """One pactl dump -> {index: {corked, mute, app, media, binary}}."""
    inputs = {}
    for block in text.split("Sink Input #")[1:]:
        match = re.match(r"(\d+)", block)
        if not match:
            continue
        idx = int(match.group(1))

        def grab(pattern):
            found = re.search(pattern, block)
            return found.group(1).strip() if found else ""

        inputs[idx] = {
            "corked": grab(r"Corked: (\S+)"),
            "mute": grab(r"Mute: (\S+)"),
            "app": grab(r'application\.name = "([^"]*)"'),
            "media": grab(r'media\.name = "([^"]*)"'),
            "binary": grab(r'application\.process\.binary = "([^"]*)"'),
        }
    return inputs


def trigger_animation():
    url = "%s/animation/%s?wait=1" % (BRIDGE_URL, ANIMATION_NAME)
    print("[trigger] POST %s" % url)
    request = urllib.request.Request(url, data=b"", method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30.0) as response:
            print("[trigger] HTTP %d: %s"
                  % (response.status, response.read().decode("utf-8", "replace")[:200]))
    except Exception as exc:
        print("[trigger] FAILED: %s" % exc)


def step_watch_pulseaudio():
    print("\n=== 3. watch PulseAudio during playback ===")
    print("[sampler] starting %d snapshots at %.1fs intervals on the robot..."
          % (SAMPLE_SNAPSHOTS, SAMPLE_INTERVAL_SEC))
    holder = {}

    def _sample():
        holder["result"] = ssh_run(PACTL_SAMPLER, timeout=90.0)

    sampler = threading.Thread(target=_sample)
    sampler.start()

    time.sleep(TRIGGER_DELAY_SEC)
    trigger_animation()
    sampler.join()

    code, out, err = holder["result"]
    snapshots = [parse_snapshot(s) for s in out.split(SNAP_MARK) if s.strip()]
    if not snapshots:
        print("[sampler] no snapshots captured! stderr: %s" % err[:300])
        return

    baseline = snapshots[0]
    print("[sampler] %d snapshots captured, baseline sink-inputs:" % len(snapshots))
    for idx, props in sorted(baseline.items()):
        print("    #%d app=%r media=%r corked=%s mute=%s"
              % (idx, props["app"], props["media"], props["corked"], props["mute"]))

    events = []
    previous = baseline
    for snap_index, snap in enumerate(snapshots[1:], 1):
        stamp = snap_index * SAMPLE_INTERVAL_SEC
        for idx, props in sorted(snap.items()):
            if idx not in previous:
                events.append("t=%.1fs NEW sink-input #%d app=%r media=%r binary=%r corked=%s"
                              % (stamp, idx, props["app"], props["media"],
                                 props["binary"], props["corked"]))
            elif props["corked"] != previous[idx]["corked"]:
                events.append("t=%.1fs sink-input #%d corked %s -> %s (app=%r media=%r)"
                              % (stamp, idx, previous[idx]["corked"], props["corked"],
                                 props["app"], props["media"]))
        for idx in previous:
            if idx not in snap:
                events.append("t=%.1fs GONE sink-input #%d (app=%r media=%r)"
                              % (stamp, idx, previous[idx]["app"], previous[idx]["media"]))
        previous = snap

    print("\n[sampler] events during playback:")
    if events:
        for event in events:
            print("    %s" % event)
    else:
        print("    NONE — no PulseAudio stream changed while the sound played,")
        print("    meaning the sound bypasses PulseAudio entirely (direct ALSA).")


def main():
    print("Discovery run: animation=%s — the gesture WILL make its sound once."
          % ANIMATION_NAME)
    step_unmute_naoqi_stream()
    step_find_behavior_sounds()
    step_watch_pulseaudio()
    print("\nDone. Paste this full output back to Kampion.")


if __name__ == "__main__":
    main()
