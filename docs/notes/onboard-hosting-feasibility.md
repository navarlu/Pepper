# Can the app run on Pepper herself? (RPi removal study)

**Date:** 2026-09-25 · **Verdict:** No — the RPi (or some host machine) stays in the stack.
**Robot probed live:** `nao@10.42.0.205`, NAOqi up, during a normal session.

Motivation: the next study is a within-subjects comparison of two tool sets over a
STT → LLM → TTS loop. The desired workflow was "ssh to Pepper, start the voice loop,
record, change one parameter, download logs" — which would remove the RPi and let her
use her own microphones. This note records what was measured, so the question does not
get re-opened from scratch.

Two variants were examined: **local models on the robot** and **cloud APIs on the
robot**. Both fail, for different reasons. The microphone part, however, works today.

---

## 1. What Pepper actually is

```
Intel Atom E3845, 4 × 1.91 GHz   flags stop at sse4_2  → NO AVX / AVX2
i686 userland (32-bit)           kernel 4.0.4-rt1-aldebaran (2016)
Gentoo Base System 2.2 (NAOqi OS 2.5)  — not Ubuntu, not Debian
RAM 3.9 GB | rootfs 1.5 GB, mounted READ-ONLY | /data 25 GB (23 GB free)
Python 2.7.6 · pip 1.4.1 · numpy 1.8 · OpenSSL 1.0.1h (June 2014)
```

Idle load with NAOqi running:

```
naoqi-service  132 % CPU     hal  25 % CPU     → ~57 % of 4 cores free
```

Privileges and toolchain:

```
sudo -l   → halt, reboot, poweroff, shutdown, firewall lock scripts. Nothing else.
no gcc / cc / g++ / ld
/usr/include/stdio.h   does not exist          ← no libc headers at all
no emerge, no portage tree, no apt / opkg
/usr/local, /opt        read-only
/home/nao               writable, not noexec, shares the 23 GB /data partition
```

**Nothing can be compiled on the robot.** Any software added must be a prebuilt
i686 binary dropped into `/home/nao`.

---

## 2. Local models on the robot — no

Blocked at the platform level, before performance even matters:

- `faster-whisper`, `onnxruntime` (silero VAD) and Piper all require Python ≥ 3.8.
  The robot has Python 2.7.6 only.
- No 32-bit Linux wheels exist for any of them. The i686 wheel ecosystem is dead:
  modern numpy, onnxruntime and ctranslate2 publish nothing for it.
- `ctranslate2` effectively wants AVX2. This CPU is Silvermont — no AVX at all.
- No compiler on board to build any of it from source.

Even with a working interpreter, ~2.3 free cores of a 2014 Atom would not carry
Whisper at conversational latency.

## 3. Cloud APIs on the robot — no (but closer than expected)

Using an API key removes every local-compute objection. The blocker becomes TLS.

```
python2 ssl:  ssl.HAS_SNI → AttributeError           (module predates SNI entirely)
python2 urllib2 → https://api.openai.com
    URLError(SSLError: SSL23_GET_SERVER_HELLO: sslv3 alert handshake failure)
```

The robot's Python cannot open a TLS connection to any modern endpoint. The system
`curl` can (libcurl 7.45 does SNI properly), and so can `pycurl`:

```
python2 + pycurl → https://api.openai.com/v1/models
    HTTP_CODE = 401   {"error": {"message": "Missing bearer authentication ..."}}
```

So **request/response cloud calls do work from Python 2.7 on the robot** —
Whisper transcription POST, chat completions POST, TTS POST.

**Streaming does not:**

- libcurl 7.45 predates WebSocket support (added in curl 7.86) → no Realtime API,
  no streaming STT/TTS.
- No `stunnel`, `socat` or `websocat` on the robot to terminate TLS for a
  hand-rolled WS client. Only `nc` and `openssl`.

An on-robot cloud pipeline would therefore be a Python 2.7 turn-based loop:
record to silence → POST utterance → POST chat → POST TTS → play the file.
That costs roughly **+1.5–3 s per turn** and **loses barge-in entirely**, and it
discards [agent_streaming.py](../../voice-agent/src/experiment/agent_streaming.py)
(1107 lines) along with silero VAD, turn detection, interruption handling and the
tool-call plumbing — all Python 3.

For a study whose dependent variable is *which tool set people prefer*, degrading
latency and removing barge-in adds noise on exactly the axis being measured, and
breaks comparability with the existing `2026-05-18` session data.

## 4. Could a newer Python be installed? — only Python 3.7, and it does not help

Building one is impossible (§1). Downloading one nearly is:

```
python-build-standalone, last 100 releases → 0  i686-unknown-linux-gnu assets
conda / Anaconda                           → dropped 32-bit Linux in 2018
```

One survivor is still hosted:

```
https://repo.anaconda.com/miniconda/Miniconda3-latest-Linux-x86.sh  → HTTP 200
  VER 4.5.12 · PLAT linux-32 · 65 MB · last-modified Feb 2025  → Python 3.7
```

This would install into `/home/nao` without root, glibc 2.21 is well above what it
needs, and conda bundles its own OpenSSL — giving **real TLS with SNI**, hence
HTTPS and WebSockets, hence Realtime API. *Not verified by installing it;* the
verdict below does not depend on it.

It still does not unlock the stack:

```
livekit-agents   Requires-Python: >=3.9,<3.14
livekit (rtc)    ships a prebuilt Rust native lib, liblivekit_ffi.so
                 → no i686 wheel on PyPI, ever
```

`livekit-agents` hard-depends on `livekit.rtc` for `AudioFrame`, and no interpreter
version fixes a missing native build. Anything hosted on Pepper is hand-written pure
Python against raw HTTP/WebSocket, on a frozen 2018 platform where no compiled
dependency will ever install again.

**It is not the Python version that blocks this. It is the 32-bit architecture.**

## 5. Docker / LiveKit server on Pepper — no

Three independent blockers, any one fatal:

```
sudo -l                 → no root
CONFIG_VETH is not set  → Docker bridge networking impossible on this kernel
i686 userland           → Docker dropped i386; LiveKit publishes no 386 image
rootfs                  → read-only, 364 MB free
```

---

## 6. What *does* work: her microphones

`hw:0,0` is held by `hal`, but PulseAudio exposes the array and mixes clients:

```
alsa_input.PCH.input-microphones          4ch 48 kHz   RUNNING
alsa_output.PCH.output-speakers.monitor   available    (speaker signal at the DAC)
pulseaudio 6.0  +  module-echo-cancel.so  +  libwebrtc-util.so   present
```

Verified capture, nothing installed on the robot:

```
parec -d alsa_input.PCH.input-microphones --rate=16000 --channels=1 \
      --format=s16le --raw
→ 3.97 s captured, rms 228, peak 4482          (~32 kB/s, trivial over WiFi)
```

So the external USB mic and the mic-side reason for the RPi can both go, whenever
the host machine is decided. The mic path is literally
`ssh nao@pepper parec … | pipeline`.

### Echo cancellation note

The speaker monitor source is tapped **at the DAC**, so it already includes NAOqi's
~1 s playback delay and shares a clock with the mic — no drift, no envelope
prealignment. That removes the alignment problem from the earlier AEC work. Against
it, her mics sit ~40 cm from her own speaker, so raw echo is far louder than on the
external mic, and PA 6.0's bundled webrtc AEC is AECM-era. Worth one measurement
with [aec_offline_probe.py](../../voice-agent/src/experiment/tests/echo/aec_offline_probe.py)
pointed at `parec` instead of `sounddevice`; do not assume a big ERLE gain.

---

## 7. Conclusion and what to revisit

Pepper stays a **peripheral** — 4 mics, speaker, body, tablet. The pipeline needs a
host running Python 3 on a 64-bit machine. **The RPi remains in the stack** and
planning continues on that basis.

Open when the topic is re-opened:

- **Host choice.** With cloud APIs there is no GPU requirement, so a laptop is
  sufficient and woska is optional. The RPi is the status quo and still the only
  machine that provides Pepper's WiFi access point (`10.42.0.205` is the RPi
  hotspot) — removing it means she must join lab WiFi.
- **Dropping the LiveKit *server* while keeping the *library*.** Verified feasible:
  `AgentSession.start()` only creates a `RoomIO` "if the input or output audio is
  not already set" — assign `session.input.audio` / `session.output.audio` and no
  room, server or WebRTC is needed. This keeps silero VAD, turn detection,
  interruptions and tool-calling, and would remove `livekit`, `redis`,
  `audio-bridge`, `user-client` and `reverse-tunnel` from compose. Work needed: a
  `PepperAudioInput` wrapping the `parec` stream and a `PepperAudioOutput` whose
  `capture_frame` / `flush` / `clear_buffer` drive
  [bridge.py](../../robot/src/bridge.py).
- **`qi` on the host.** On x86_64 it is `pip install qi==3.1.5`; the self-built ARM
  qi ([rpi-dev.md](rpi-dev.md)) is RPi-specific pain that a host move would delete.
- **The Miniconda 3.7 route**, only if this ever becomes a permanent unattended
  installation with no host machine in the room. Cost is a full rewrite against raw
  HTTP/WS on an unsupported platform; it buys nothing a laptop does not.

## Reproducing the probes

```bash
sshpass -p Argus ssh -o StrictHostKeyChecking=no nao@10.42.0.205

# platform
uname -a; cat /etc/gentoo-release; free -m; df -h; mount | grep " / "
which gcc emerge python3; ls /usr/include/stdio.h; sudo -l
zcat /proc/config.gz | grep -E "VETH|NAMESPACES"

# TLS from the robot's Python
export PYTHONPATH=/opt/aldebaran/lib/python2.7/site-packages
python -c "import ssl; print(ssl.OPENSSL_VERSION, ssl.HAS_SNI)"     # AttributeError
python -c "import urllib2; urllib2.urlopen('https://api.openai.com/v1/models')"
python -c "import pycurl,StringIO; b=StringIO.StringIO(); c=pycurl.Curl(); \
  c.setopt(pycurl.URL,'https://api.openai.com/v1/models'); \
  c.setopt(pycurl.WRITEFUNCTION,b.write); c.perform(); \
  print(c.getinfo(pycurl.HTTP_CODE))"                               # 401 = TLS ok

# microphones
pactl list short sources
parec -d alsa_input.PCH.input-microphones --rate=16000 --channels=1 \
      --format=s16le --raw > /tmp/mic.raw
```
