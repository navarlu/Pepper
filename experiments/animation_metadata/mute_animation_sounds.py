"""Silence Pepper's animation sounds by renaming their sound files on disk.

Discovery (discover_animation_sound.py) showed animation sounds are played
by ALSoundFilesPlayer from files shipped inside each behavior package
(e.g. Sneeze/sneeze6.ogg) through a short-lived PulseAudio stream that no
volume/mute API we tried covers. Renaming the files is deterministic:
the motion plays normally, the sound box finds nothing to play, and the
ssh+paplay TTS path is untouched by construction. Fully reversible.

Modes (set MODE below):
  "list"    — show every sound file (active and muted) without changing anything
  "disable" — rename  X.ogg -> X.ogg.muted
  "restore" — rename  X.ogg.muted -> X.ogg

Scope: ONLY_ANIMATION = "Sneeze" limits changes to that one behavior dir
(first test). Set it to None to process ALL animation sound files.

The script also dumps the ALSoundFilesPlayer API (qicli) at the end —
if it exposes a volume/mute method, that would be an alternative
bridge-side mute for the future.

Run (on the RPi, host-side):
    python3 experiments/animation_metadata/mute_animation_sounds.py
"""

import subprocess

# --- Configuration ---------------------------------------------------------
MODE = "disable"            # "list" | "disable" | "restore"
ONLY_ANIMATION = "Angry_1"  # None -> all animations under APPS_DIR
PEPPER_SSH_HOST = "10.42.0.205"
PEPPER_SSH_USER = "nao"
PEPPER_SSH_PASSWORD = "Argus"
APPS_DIR = "/home/nao/.local/share/PackageManager/apps/animations"
MUTED_SUFFIX = ".muted"
# ---------------------------------------------------------------------------


def ssh_run(remote_cmd, timeout=60.0):
    cmd = [
        "sshpass", "-p", PEPPER_SSH_PASSWORD,
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=5",
        "%s@%s" % (PEPPER_SSH_USER, PEPPER_SSH_HOST),
        remote_cmd,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return result.returncode, result.stdout, result.stderr


def scope_dir():
    if ONLY_ANIMATION:
        code, out, err = ssh_run(
            'find %s -type d -iname "*%s*" 2>/dev/null | head -1'
            % (APPS_DIR, ONLY_ANIMATION)
        )
        target = out.strip()
        if not target:
            raise RuntimeError("no behavior dir matching %r found" % ONLY_ANIMATION)
        return target
    return APPS_DIR


FIND_ACTIVE = (
    'find "%s" -type f \\( -iname "*.ogg" -o -iname "*.wav" -o -iname "*.mp3" \\)'
)
FIND_MUTED = 'find "%s" -type f -iname "*%s"'


def list_files(target):
    code, out, err = ssh_run(FIND_ACTIVE % target)
    active = sorted(line for line in out.splitlines() if line.strip())
    code, out, err = ssh_run(FIND_MUTED % (target, MUTED_SUFFIX))
    muted = sorted(line for line in out.splitlines() if line.strip())
    print("scope: %s" % target)
    print("active sound files (%d):" % len(active))
    for path in active:
        print("    %s" % path)
    print("muted sound files (%d):" % len(muted))
    for path in muted:
        print("    %s" % path)
    return active, muted


def disable(target):
    code, out, err = ssh_run(
        FIND_ACTIVE % target
        + ' -exec sh -c \'mv "$1" "$1%s" && echo "muted: $1"\' _ {} \\;' % MUTED_SUFFIX
    )
    print(out.strip() or "nothing to mute (already muted?)")
    if err.strip():
        print("stderr: %s" % err.strip())


def restore(target):
    code, out, err = ssh_run(
        FIND_MUTED % (target, MUTED_SUFFIX)
        + ' -exec sh -c \'mv "$1" "${1%%%s}" && echo "restored: ${1%%%s}"\' _ {} \\;'
        % (MUTED_SUFFIX, MUTED_SUFFIX)
    )
    print(out.strip() or "nothing to restore")
    if err.strip():
        print("stderr: %s" % err.strip())


def dump_soundfilesplayer_api():
    print("\n=== ALSoundFilesPlayer API (for a possible service-level mute) ===")
    code, out, err = ssh_run("qicli info ALSoundFilesPlayer 2>&1 | head -60")
    print(out.strip() or err.strip() or "qicli returned nothing")


def main():
    print("mode=%s scope=%s" % (MODE, ONLY_ANIMATION or "ALL animations"))
    target = scope_dir()
    if MODE == "list":
        list_files(target)
    elif MODE == "disable":
        disable(target)
        print("\nstate after change:")
        list_files(target)
    elif MODE == "restore":
        restore(target)
        print("\nstate after change:")
        list_files(target)
    else:
        raise ValueError("unknown MODE: %r" % MODE)
    dump_soundfilesplayer_api()
    print("\nDone. Now trigger the animation yourself and check sound + motion:")
    print('  curl -X POST "http://localhost:5000/animation/%s?wait=1"'
          % (ONLY_ANIMATION or "Angry_1"))
    print("Paste the output back to Kampion.")


if __name__ == "__main__":
    main()
