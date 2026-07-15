# Muting Pepper's Animation Sounds (while keeping ssh+paplay TTS alive)

*Findings from 2026-07-15. Everything here was verified by ear on the real robot.*

## Goal

Play NAOqi animations (gestures) **without their built-in sound effects**
(Sneeze "achoo", Angry_1 grunt, ...), while TTS audio — which we stream from
LiveKit into Pepper's PulseAudio via **ssh+paplay** (`audio_bridge.py`) —
keeps playing uninterrupted. Needed for:

- the animation-recording rig (`record_animations.py`) — 400 silent clips
- the live agent — silent gestures during speech

## How animation sound actually works (discovery result)

Animation sounds are played by **`ALSoundFilesPlayer`**, directly from sound
files shipped inside each behavior package on the robot:

```
/home/nao/.local/share/PackageManager/apps/animations/<pose>/<category>/<name>/*.ogg
e.g. .../Stand/Emotions/Neutral/Sneeze/sneeze6.ogg
```

At playback, ALSoundFilesPlayer opens its **own short-lived PulseAudio
sink-input** (`media.name = "ALSoundFilesPlayer Sink"`, app
`qi-AudioDeviceManagerPulse`), which exists only for the duration of the
sound (~0.3 s observed). This was captured with
`discover_animation_sound.py`, which samples `pactl list sink-inputs` on the
robot while the animation plays.

## What does NOT work (all tried, all failed by ear)

| Attempt | Why it fails |
|---|---|
| `ALAudioPlayer.setMasterVolume(0)` | Sound doesn't go through ALAudioPlayer; behaviors also set their own volumes when their boxes fire |
| `ALTextToSpeech.setVolume(0)` | The sound is not TTS-generated; it's a file |
| `ALAudioDevice.setOutputVolume(0)` | **Works** for the animation, but gates the whole PulseAudio sink — kills the ssh+paplay TTS stream too |
| `pactl set-sink-input-mute` on NAOqi's persistent stream (`alaudiodevice-legacy-sink`) | The sound uses a *separate, ephemeral* sink-input; the persistent one carries nothing relevant |
| Muting the ephemeral sink-input reactively | It lives ~0.3 s — the race is unwinnable |
| Copying behaviors to "silent" duplicates with a new name | Behaviors must be registered in the package `manifest.xml`; a copied folder is not callable |

## What WORKS: rename the sound files on disk

The motion timeline plays normally; the sound box simply finds nothing to
play; paplay is untouched by construction. Fully reversible. Two mechanisms,
using **different suffixes so they can never collide**:

### 1. Per-call override — bridge endpoint (suffix `.tmpmuted`)

```bash
# play whatever the current disk state is (no ssh, no extra latency):
curl -X POST "http://localhost:5000/animation/Angry_1?wait=1"

# guarantee SILENT for this call (mute=1 is a legacy alias):
curl -X POST "http://localhost:5000/animation/Angry_1?wait=1&sound=off"

# guarantee AUDIBLE for this call, even while session-wide muted:
curl -X POST "http://localhost:5000/animation/Angry_1?wait=1&sound=on"
```

The override is **state-aware and independent of the current disk state**
(`_force_behavior_sounds` in `robot/src/bridge.py`):

- `sound=off` ssh-renames active `*.ogg/*.wav/*.mp3` to `*.tmpmuted`;
  if the files are already muted (either suffix), it's a no-op.
- `sound=on` temporarily restores both `*.muted` (session tool) and
  `*.tmpmuted` files to their playable names.
- After the gesture, the **previous state is put back exactly** — a
  session-wide mute stays a session-wide mute after a `sound=on` call.
- The rename doubles as the state check: zero files renamed means the
  behavior was already in the requested state, and there is nothing to undo.

Safe during live TTS. Costs up to ~0.4 s ssh round-trip on each side of the
gesture (measured: +0.76 s total on Angry_1). SSH config comes from
`robot/src/config.py` (`PEPPER_SSH_HOST/USER/PASSWORD`, host defaults to
the NAOqi host from `PEPPER_QI_URL`; container ships ssh+sshpass). The sync
response includes `"sound": "default" | "on" | "off"`.

### 2. Session-wide mute — manual tool (suffix `.muted`)

```bash
python3 experiments/animation_metadata/mute_animation_sounds.py
```

Globals at the top: `MODE = "list" | "disable" | "restore"`,
`ONLY_ANIMATION = "<name>" | None` (None = every animation package).
Use `disable` + `ONLY_ANIMATION = None` before a long recording session
(one ssh call instead of 2×400), `restore` afterwards.

## Verification procedure

```bash
python3 experiments/animation_metadata/test_audio_mute.py
```

Streams `robot/data/hello.wav` on loop through the **identical** ssh+paplay
pipe the real TTS uses (24 kHz wav resampled to the pipe's 16 kHz), and
triggers `ANIMATION_NAME` with `mute=1` mid-playback. Pass criteria:
audio plays uninterrupted the whole run, gesture is silent.
Verified 2026-07-15 with Angry_1: PASS.

Diagnostic tool if a sound ever slips through again:

```bash
python3 experiments/animation_metadata/discover_animation_sound.py
```

Locates the behavior package + its sound files, and reports every
PulseAudio sink-input that appears while the animation plays.

## Related bridge facts (same investigation)

- The bridge *also* mutes `ALAudioPlayer` + `ALTextToSpeech` around every
  animation (soft layers; harmless, kept).
- Recording-rig stillness: `POST /motion/recording` applies the
  all-abilities-off life profile; `POST /motion/posture` does a blocking
  `goToPosture` neutral reset (both added for `record_animations.py`).
- If NAOqi restarts mid-mute with `?mute=1` in flight, sound files may be
  left renamed (`*.tmpmuted`). Recovery: rerun the animation with `mute=1`
  (restore runs in `finally`), or ssh and rename manually, or use
  `mute_animation_sounds.py` logic with the `.tmpmuted` suffix.
