# `robot/src/bridge.py` — The Pepper Bridge

The bridge is the only process that talks to Pepper's NAOqi stack over
`qi`. Everything else in the system (the voice agent, the audio-bridge,
the orchestrator, the tablet server) reaches Pepper through the bridge.

It plays two roles at once:

1. **Audio sink** — a TCP server that accepts mono 16-bit PCM frames
   from [services/src/live/audio_bridge.py](../../services/src/live/audio_bridge.py)
   and streams them into Pepper's speakers via `ALAudioDevice`.
2. **HTTP control plane** — a small HTTP server that exposes animations,
   tablet rendering, eye LEDs, camera snapshots, and audio volume to
   the rest of the stack.

It runs inside the `bridge` Docker container on the RPi with
`network_mode: host`, listening on `:5000` (HTTP) and `:55555` (TCP).

---

## Big picture

```
                                   +------------------------------+
 audio-bridge (LiveKit listener) --+--> TCP 55555 ---+            |
                                   |                 |            |
 voice-agent (play_animation tool)-+--> HTTP 5000 ---+--- qi ---> Pepper
                                   |                 |  (NAOqi)
 tablet-server (showWebview proxy)-+--> HTTP 5000 ---+
                                   |                 |
 orchestrator / user-client  ------+--> HTTP 5000 ---+
                                   +------------------------------+
```

The Python process runs **Python 3** inside Docker, but the `qi`
library is self-built for ARM64 and is pinned via `PYTHONPATH` /
`LD_LIBRARY_PATH` (see [rpi-dev.md](../notes/rpi-dev.md)).

**Code layout.** The bridge is split across three files:

| File                                            | Role |
|-------------------------------------------------|------|
| [robot/src/bridge.py](../../robot/src/bridge.py) | Stateful classes (`TabletDebugReporter`, `LedEffectManager`, `TabletOverlayHttpServer`), `main()` wiring, TCP audio loop. |
| [robot/src/utils.py](../../robot/src/utils.py)  | Pure helpers + protocol constants: `connect_session`, `wait_for_service`, animation name resolution, camera capture, `mono16_to_stereo16`, `CONTROL_FRAME_*`. |
| [robot/src/config.py](../../robot/src/config.py) | All tunables (env-backed): stream rate, buffer sizes, camera capture parameters, tablet templates, autonomous-life profile, HTTP/TCP binds. |

---

## Startup flow — `main()`

1. **Connect to Pepper** — [`connect_session()`](../../robot/src/utils.py)
   polls Pepper's NAOqi TCP port until it answers, then creates a
   `qi.Session`. It never raises; it just retries. This is what lets the
   bridge container start before the robot is powered on.

   <!-- snippet: connect_session -->
<!-- generated from robot/src/utils.py:80 by docs/embed_snippets.py -->
```python
def connect_session(qi_url):
    """Block until a `qi.Session` is connected to Pepper at `qi_url`.

    Polls the NAOqi TCP port every `BRIDGE_CONNECT_POLL_INTERVAL_SEC`
    seconds and retries `qi.Session.connect` on failure. Returns the
    live session — never raises. This is what lets the bridge container
    start before Pepper is powered on.
    """
    host, port = parse_qi_url(qi_url)
    poll = float(BRIDGE_CONNECT_POLL_INTERVAL_SEC)

    while True:
        if not pepper_reachable(host, port):
            print("[bridge] Pepper unreachable at {}:{} — retrying in {}s".format(
                host, port, int(poll)))
            time.sleep(poll)
            continue

        try:
            s = qi.Session()
            s.connect(qi_url)
            print("[bridge] connected to Pepper at", qi_url)
            return s
        except Exception as exc:
            print("[bridge] qi.connect failed: {} — retrying in {}s".format(
                to_text(exc), int(poll)))
            time.sleep(poll)
```
<!-- /snippet -->
2. **Resolve services** via [`wait_for_service()`](../../robot/src/utils.py):
   - **Required:** `ALAudioDevice` (the bridge exits if this fails).
   - **Optional** (bridge keeps running with reduced capabilities):
     `ALBehaviorManager`, `ALAnimationPlayer`, `ALAutonomousLife`,
     `ALAudioPlayer`, `ALTextToSpeech`, `ALLeds`, `ALVideoDevice`,
     `ALBasicAwareness`, `ALTabletService`.
3. **Apply autonomous-life profile** — when `TOUCH_AUTONOMOUS_LIFE` is
   on, the bridge switches Pepper to `solitary` and sets the abilities
   (blinking, background movement, awareness, listening/speaking motion)
   from `config.py`. This is how we stop Pepper from freezing like a
   statue while the LLM is talking.
4. **Audio setup** — `openAudioOutputs()`, `setParameter("outputSampleRate", PEPPER_STREAM_RATE)`,
   `setOutputVolume(PEPPER_OUTPUT_VOLUME)`, and compute `batch_frames` /
   `max_buffer_frames` from config. These numbers control how much audio
   can queue up before the bridge starts dropping frames.
5. **Spawn auxiliary threads** — `LedEffectManager`,
   `TabletDebugReporter`, and `TabletOverlayHttpServer` (the HTTP
   server). The HTTP server lives on its own thread so that animations
   and tablet updates never block the audio socket.
6. **TCP audio loop** — bind `BRIDGE_BIND_HOST:TCP_PORT`, then accept
   one client at a time and stream its audio. On disconnect we flush
   Pepper's output buffers and wait for the next client.

---

## TCP audio pipeline — the hot path

This is the most performance-sensitive part of the bridge. It lives
inside the inner `while True:` of [`main()`](../../robot/src/bridge.py).

**Wire format.** Each message is a 4-byte big-endian length followed by
`length` bytes of mono int16 PCM at `PEPPER_STREAM_RATE`. Two control
values on the length field:

| Length value           | Meaning                                           |
|------------------------|---------------------------------------------------|
| `0` (`CONTROL_FRAME_FLUSH`) | Drop the local queue and call `flushAudioOutputs()`. |
| `0xFFFFFFFF` (`CONTROL_FRAME_PING`) | Keepalive — ignore.                           |
| `0xFFFFFFFE` (`CONTROL_FRAME_DRAIN_REQ`) | Service asks "is the speaker idle yet?" — bridge replies `DRAIN_ACK` (4-byte BE) once `stereo_queue` empties plus one batch of tail. Used by `send_message_to_user` to wait on real end-of-speech instead of LiveKit's emitter drain. |
| `0xFFFFFFFD` (`CONTROL_FRAME_DRAIN_ACK`) | Reply to DRAIN_REQ, sent **bridge → service**.  |
| Anything else          | PCM chunk of exactly that many bytes.            |

### Silence-gating (audio-bridge side)

LiveKit's `AgentSession` publishes a **continuous** audio track — even
between TTS utterances it emits silence frames at ~50-100 fps to keep
the WebRTC stream alive. Forwarding all of those to NAOqi fills the
internal `ALAudioDevice` queue (whose state isn't readable from
Python), and `sendRemoteBufferToOutput` ends up running slower than
real-time. The visible symptom is *Pepper's reply plays seconds — even
tens of seconds — after the text already appeared on the tablet*.

The audio-bridge side ([services/src/live/audio_bridge.py](../../services/src/live/audio_bridge.py))
gates by frame RMS:

  * Forward when `audioop.rms(frame, 2) ≥ SILENCE_GATE_RMS` (default 50).
  * After the last loud frame, keep forwarding for
    `SILENCE_GATE_HANGOVER_MS` (default 400 ms) so we don't clamp the
    gate shut between syllables.
  * On the falling edge (gate closes), send one `CONTROL_FRAME_FLUSH`
    so NAOqi drops whatever was still queued from the previous
    utterance. This is what stops a stale tail from playing late on
    the *next* turn.

Tune via env (`SILENCE_GATE_RMS=0` disables gating entirely). The
heartbeat log line now shows `voiced_frames` and `silence_dropped`
separately so you can see at a glance whether the gate is doing its
job.

<!-- snippet: tcp_wire_decode -->
<!-- generated from robot/src/bridge.py:1018 by docs/embed_snippets.py -->
```python
# Read 4-byte length header
header = recv_all(conn, 4)
if not header:
    print("[pepper_audio] client disconnected (no header)")
    break

size = struct.unpack(">I", header)[0]

# Control frame: flush any queued audio without dropping the TCP session.
if size == 0:
    stereo_queue = deque()
    queued_bytes = 0
    try:
        audio.flushAudioOutputs()
    except Exception:
        pass
    print("[pepper_audio] control flush: cleared buffered audio")
    continue

if size == CONTROL_FRAME_PING:
    continue

# Sanity check
if size > 2 ** 20:
    print("[pepper_audio] invalid size:", size)
    break
```
<!-- /snippet -->

**Per-chunk flow.**

1. `recv_all(conn, 4)` → read the header; EOF here ends the session.
2. `recv_all(conn, size)` → read the PCM payload.
3. [`mono16_to_stereo16()`](../../robot/src/utils.py) — uses
   `audioop.tostereo` (C) to duplicate the mono channel. Python-loop
   interleaving was too jittery.
4. Clamp to `PEPPER_CHUNK_LIMIT_FRAMES` (NAOqi's
   `sendRemoteBufferToOutput` has a hard cap).
5. Append to the `stereo_queue` deque.
6. **Overflow drop** — if the queue exceeds `max_buffer_bytes`, pop the
   oldest bytes and call `flushAudioOutputs()`. We log `dropped_frames`
   so late audio can be traced to a specific overflow event.
7. **Drain** — while we have ≥ `batch_bytes` queued, concatenate one
   `batch_frames` worth of stereo bytes and hand it to
   `audio.sendRemoteBufferToOutput(batch_frames, payload)`.

<!-- snippet: tcp_playback_drain -->
<!-- generated from robot/src/bridge.py:1071 by docs/embed_snippets.py -->
```python
if queued_bytes > max_buffer_bytes:
    overflow_bytes = queued_bytes - max_buffer_bytes
    dropped_bytes = 0
    while stereo_queue and dropped_bytes < overflow_bytes:
        head = stereo_queue[0]
        need = overflow_bytes - dropped_bytes
        if len(head) <= need:
            dropped_bytes += len(head)
            queued_bytes -= len(head)
            stereo_queue.popleft()
        else:
            stereo_queue[0] = head[need:]
            dropped_bytes += need
            queued_bytes -= need
            break

    dropped_frames = dropped_bytes // 4
    dropped_frames_total += dropped_frames
    try:
        audio.flushAudioOutputs()
    except Exception:
        pass
    print(
        "[pepper_audio] WARNING buffer overflow:",
        "dropped_frames=", dropped_frames,
        "dropped_frames_total=", dropped_frames_total,
        "buffered_frames=", queued_bytes // 4,
    )

while queued_bytes >= batch_bytes:
    need_bytes = batch_bytes
    parts = []
    while need_bytes > 0 and stereo_queue:
        head = stereo_queue[0]
        if len(head) <= need_bytes:
            parts.append(head)
            need_bytes -= len(head)
            queued_bytes -= len(head)
            stereo_queue.popleft()
        else:
            parts.append(head[:need_bytes])
            stereo_queue[0] = head[need_bytes:]
            queued_bytes -= need_bytes
            need_bytes = 0

    payload = b"".join(parts)
    send_start_ts = time.time()
    audio.sendRemoteBufferToOutput(batch_frames, payload)
```
<!-- /snippet -->

**Observability.** The loop tracks `frames_sent_total`,
`recv_intervals_ms_sum`, `send_durations_ms_sum`, and `max_*` and emits
a heartbeat line every 200 chunks (and a one-shot line on the first
chunk / first send). Slow sends (>2× realtime) fire a `WARNING` line.

**Why no sleep?** `recv_all()` already blocks until the next chunk
arrives. Adding a sleep creates the classic "freeze then catch-up"
stutter.

---

## HTTP control plane — [`TabletOverlayHttpServer`](../../robot/src/bridge.py)

Lives on a thread, binds to `BRIDGE_URL` (host = `BRIDGE_BIND_HOST`,
port from the URL). The `Handler` class is defined inline so it can
close over the NAOqi service proxies passed to `__init__`. All routes
return JSON (`{"ok": bool, ...}`) except `/camera/snapshot` and
`/health`.

| Method | Path                  | What it does |
|--------|-----------------------|--------------|
| `GET`  | `/health`             | Liveness + the audio bind info. |
| `POST` | `/animation/<name>`   | Resolve `<name>` via [`resolve_animation_name()`](../../robot/src/utils.py), then **ack 200 immediately** and run the behavior on a background thread. This is intentional — see note below. |
| `POST` | `/tablet/text_inline` | Forward a JSON payload (text, or `ui=split_chat_debug`, or `ui=chat_history`) to the `TabletDebugReporter`. The bridge augments the payload with current `ALAutonomousLife` state before enqueueing. |
| `POST` | `/tablet/url`         | `ALTabletService.showWebview(url)` — used by `tablet_server.py` to own the screen. |
| `POST` | `/leds/state`         | `{"mode": "idle"|"search_pulse"|"off"}` → `LedEffectManager.set_mode`. |
| `POST` | `/audio/volume`       | `{"volume": 0..100}` → `ALAudioDevice.setOutputVolume`. |
| `POST` | `/camera/snapshot`    | Grab one JPEG from the top camera (see `capture_camera_snapshot` below). Serialized by a `camera_lock` — NAOqi's video ring buffer misbehaves under concurrent subscribes. Returns `image/jpeg`. |
| `POST` | `/motion/head_lock`   | `{"lock": bool, "yaw"?: float, "pitch"?: float, "speed"?: float}` — park the head at `(yaw, pitch)` rad and `pauseAwareness()`, or `resumeAwareness()` when `lock=false`. Defaults from `HEAD_LOCK_YAW_RAD` / `HEAD_LOCK_PITCH_RAD` / `HEAD_LOCK_SPEED`. |
| `POST` | `/motion/sleep`       | Between-session sleep: disable all autonomous abilities, drop head to `SLEEP_HEAD_PITCH_RAD`, set eye LEDs to `off`. Driven by `tablet_server` when no `agent-*` participant is in the LiveKit room. |
| `POST` | `/motion/wake`        | Between-session wake: enable the awake ability profile (BasicAwareness + BackgroundMovement + AutonomousBlinking + SpeakingMovement), lift head to `WAKE_HEAD_PITCH_RAD`, set eye LEDs to `idle`. Driven by `tablet_server` when an `agent-*` participant joins. |

### `/motion/sleep` and `/motion/wake` — body matches the experiment state

`loop_launcher.py` is the single writer that says "the experiment is
running." It stamps `experiment_active: true` and refreshes
`experiment_heartbeat_ts` every 2 s in `services/data/state.json`.
On every exit path it writes `experiment_active: false`. A hard
crash heals automatically: if the heartbeat goes stale (>10 s old)
the bridge treats the experiment as inactive.

Two consumers poll the file every 0.5 s:

- **Bridge** runs `ExperimentStateWatcher` (modeled on the same
  pattern as `RuntimeVolumeWatcher`). On every transition it calls
  `apply_pepper_state("wake")` or `apply_pepper_state("sleep")` —
  the same function the HTTP endpoints below dispatch through, so
  the side-effects are defined exactly once.
- **`tablet_server`** runs `_state_file_watcher` and renders the
  chat UI when active, the zzz UI when inactive.

State machine:

| State        | Source                        | Abilities                                                                   | Head pitch (rad) | LEDs |
|--------------|-------------------------------|-----------------------------------------------------------------------------|------------------|------|
| Asleep       | `experiment_active=false`     | all off, life state="disabled"                                              | `SLEEP_HEAD_PITCH_RAD` (+0.445) | off  |
| Awake-idle   | `experiment_active=true`      | AutonomousBlinking + BackgroundMovement + BasicAwareness + SpeakingMovement, life state="solitary" | `WAKE_HEAD_PITCH_RAD` (0.00)   | idle |
| Awake-locked | voice-agent `session.start`   | BasicAwareness paused (via `/motion/head_lock`)                             | `HEAD_LOCK_PITCH_RAD` (-0.15)  | idle |

Asleep ↔ Awake-idle is owned by `ExperimentStateWatcher` (state.json).
Awake-idle ↔ Awake-locked is owned by the voice-agent session
([_pipeline.py](../../voice-agent/src/experiment/_pipeline.py)). The
two layers compose: head_lock during a conversation pauses awareness
without changing life state, and on session end it just resumes
awareness — the outer wake/sleep gate stays whatever the watcher set.

Flipping `life.setState("disabled")` for sleep is essential — NAOqi's
mood/awareness painter repaints `FaceLeds` and `EarLeds` faster than
our LED-off ticks while life state is `solitary`/`interactive`.
Disabling individual abilities is not enough. Wake always pairs with
sleep through the watcher, so Pepper never gets stuck in `disabled`.

The `/motion/sleep` and `/motion/wake` HTTP endpoints remain as
diagnostic primitives — they call exactly the same code path the
watcher uses. A subsequent state.json write (or a stale-heartbeat
tick) will overwrite any manual change. That's intentional: there
is one source of truth, and the endpoints are debugging conveniences.

```bash
# Manual wake / sleep for testing — watcher will reassert on next tick.
curl -X POST http://<bridge>:5000/motion/sleep
curl -X POST http://<bridge>:5000/motion/wake
```

### `/motion/head_lock` — pin the head during an interaction

Used by the loop-experiment voice agent: at session start the head is
parked centered + slightly up (`yaw=0`, `pitch=-0.15` rad ≈ -8.6°) and
`ALBasicAwareness` is paused so Pepper stops scanning faces/sound; at
session end the awareness is resumed and she goes back to looking
around on her own. Body sway, blinking, speaking gestures and
`/animation/*` calls are **not** affected — this only stops the
autonomous head scanning. The voice-agent wraps the lock/unlock in a
`try/finally`, so any normal session exit (graceful close, recorder
leaves, in-process crash) releases the head. A hard `kill -9` of the
worker is the only path that can leave the head parked until the next
session locks/unlocks it.

```bash
curl -X POST http://<bridge>:5000/motion/head_lock \
     -H 'Content-Type: application/json' -d '{"lock": true}'
curl -X POST http://<bridge>:5000/motion/head_lock \
     -H 'Content-Type: application/json' -d '{"lock": false}'
```

Requires `ALMotion` (for `setAngles`) and benefits from
`ALBasicAwareness` (for `pause/resumeAwareness`); either being absent
is logged but doesn't fail the other half.

### Why `/animation/<name>` acks before running

Behaviors take seconds. If the bridge held the HTTP response open until
the behavior finished, the agent's `play_animation` tool call would
block, which could starve the WebRTC heartbeat between woska and the
LiveKit server. See [connection-issue.md](../notes/connection-issue.md) for the
full story. The handler therefore:

1. Validates + resolves the name synchronously.
2. Spawns a daemon thread that mutes `ALAudioPlayer` (so animation
   sounds don't collide with streamed TTS), runs the behavior, and
   restores volume.
3. Returns `200 {"queued": true}` immediately.

### Disconnect handling

`_is_disconnect_error` recognises `EPIPE` / `ECONNRESET`, and the
overrides of `handle_one_request` / `finish` swallow these so the
Python 2-style traceback doesn't pollute the logs when a client times
out.

---

## Animations — the `play_animation` pipeline

Animations (Pepper's gestures, emotions, body-talk loops, LED
patterns) are the only non-audio way the voice agent can drive the
robot's body. They flow through the bridge end-to-end:

```
LLM tool call                HTTP                     qi / NAOqi
─────────────                ────                     ──────────
play_animation("Hey_1")  ──► POST /animation/Hey_1 ──► ALAnimationPlayer.run(
 (voice-agent/src/live/tools.py)   (bridge)                   "animations/Stand/Gestures/Hey_1")
                                │
                                └── or ALBehaviorManager.runBehavior(<path>)
                                    for user-installed behaviors
```

### The alias map — [robot/data/animations.json](../../robot/data/animations.json)

A flat JSON dict mapping a short, LLM-friendly name to the full NAOqi
behavior path. ~400 aliases covering:

- `BodyTalk_*`, `Listening_*`, `Remember_*`, `ThinkingLoop_*` — idle /
  speech-overlay body motion.
- `Emotions/Positive/*`, `Emotions/Negative/*` (Happy, Angry, Bored,
  Excited, …) — mood gestures.
- `Gestures/*` (`Hey_1`, `YouKnowWhat_1`, `CalmDown_1`, …) — concrete
  communicative gestures.
- `Waiting/*` — fillers (`Stretch_1`, `LookHand_1`, `ScratchHead_1`).
- `LED/*` (`CircleEyes`, `RainbowEyes`, …) — tablet-less LED-only
  animations.

Loaded once at startup by [`load_animations_map()`](../../robot/src/utils.py).
The map is what the voice agent's system prompt is allowed to use —
new gestures go in this file, not in the agent.

### Resolution order — [`resolve_animation_name()`](../../robot/src/utils.py)

When a request arrives at `/animation/<name>`, the bridge first calls
`behavior_manager.getInstalledBehaviors()` to discover every behavior
currently loaded on Pepper. Then `resolve_animation_name(name, map, installed)`
walks:

1. **Exact alias** — `map["Hey_1"]` → `"animations/Stand/Gestures/Hey_1"`. Done.
2. **Literal path** — if `name` already contains `/`, return it as-is.
   (Lets the agent address custom behaviors that aren't in the map.)
3. **Suffix match** — walk `installed` and pick any entry ending in
   `/<name>`. When more than one matches, prefer entries under
   `animations/` (the NAOqi stock library). This is the fallback for
   names that didn't make it into the alias map yet.
4. Nothing matched → bridge replies `404 {"ok": false, "error": "unknown animation"}`.

<!-- snippet: resolve_animation_name -->
<!-- generated from robot/src/utils.py:164 by docs/embed_snippets.py -->
```python
def resolve_animation_name(name, animations_map, installed):
    """Resolve a user-facing animation name to an installed behavior path.

    Resolution order:
      1. Exact alias hit in `animations_map`.
      2. If `name` already contains a '/', treat it as a literal path.
      3. Suffix match against `installed` behaviors — preferring
         entries under `animations/` when multiple match.
    Returns `None` if no candidate is found.
    """
    key = to_text(name).strip()
    if not key:
        return None
    mapped = animations_map.get(key)
    if mapped:
        return mapped
    if "/" in key:
        return key
    suffix = "/" + key
    matches = [b for b in installed if b.endswith(suffix)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        pref = [m for m in matches if m.startswith("animations/")]
        return pref[0] if pref else matches[0]
    return None
```
<!-- /snippet -->

### Execution — background thread

After resolution the handler spawns a daemon thread and returns
`200 {"queued": true}` immediately (see "Why `/animation/<name>` acks
before running" above). Inside the thread:

1. **Autonomous life** — if `ALAutonomousLife.getState()` is
   `"disabled"` and `TOUCH_AUTONOMOUS_LIFE` is on, switch to
   `"solitary"`. Animations won't play while life is disabled.
2. **Mute `ALAudioPlayer`** (`setMasterVolume(0.0)`). Many stock
   animations carry their own sound effects — without muting, those
   collide with the streamed TTS coming through the TCP path.
3. **Run the behavior:**
   - Paths starting with `animations/` → `ALAnimationPlayer.run(path)`.
     We take the returned future and call `.value()` so exceptions
     surface (but we swallow them — the HTTP response is long gone).
   - Everything else → `ALBehaviorManager.runBehavior(path)`. This
     covers user-installed packages that aren't under the stock
     animation library.
4. **Restore `ALAudioPlayer` volume** (`setMasterVolume(1.0)`) in the
   `finally` so a crash mid-animation doesn't leave the robot mute.

<!-- snippet: animation_background -->
<!-- generated from robot/src/bridge.py:636 by docs/embed_snippets.py -->
```python
def _run_animation_bg(behavior_local, name_local):
    try:
        if life is not None and TOUCH_AUTONOMOUS_LIFE:
            try:
                state = to_text(life.getState())
                print("[life] state before animation:", state)
                if state.lower() == "disabled":
                    print("[life] state is disabled, switching to solitary")
                    life.setState("solitary")
            except Exception as life_exc:
                print("[life] warning:", to_text(life_exc))

        # Mute ALAudioPlayer so animation sounds don't
        # overlap with the streamed TTS audio.
        if audio_player is not None:
            try:
                audio_player.setMasterVolume(0.0)
                print("[animation] muted ALAudioPlayer")
            except Exception as mute_exc:
                print("[animation] mute warning:", to_text(mute_exc))

        print("[animation] running:", behavior_local)
        try:
            if anim is not None and behavior_local.startswith("animations/"):
                fut = anim.run(behavior_local)
                try:
                    fut.value()
                except Exception:
                    pass
            else:
                bm.runBehavior(behavior_local)
            print("[animation] done:", behavior_local)
        finally:
            if audio_player is not None:
                try:
                    audio_player.setMasterVolume(1.0)
                    print("[animation] restored ALAudioPlayer volume")
                except Exception as unmute_exc:
                    print("[animation] unmute warning:", to_text(unmute_exc))
    except Exception as bg_exc:
        print("[animation] failed:", name_local, to_text(bg_exc))
```
<!-- /snippet -->

### Failure modes & logging

- Unknown alias → `404`, logged as `[animation] ...` from the handler.
- NAOqi exception inside the background thread → printed as
  `[animation] failed: <name> <exc>` but never propagated (the HTTP
  response was already sent).
- Autonomous-life state change failures only warn (`[life] warning: ...`);
  the bridge tries to play the animation anyway.

---

## Eye LEDs — [`LedEffectManager`](../../robot/src/bridge.py)

Background thread that continuously asserts the desired eye-LED state.
Needed because NAOqi's "mood painter" (active in `solitary` autonomous
life) constantly tries to paint the eyes pink; a one-shot
`fadeRGB` call gets overwritten within a second.

Modes:

| Mode            | Behavior                                       |
|-----------------|------------------------------------------------|
| `idle`          | Solid white, refreshed every 0.3 s.            |
| `search_pulse`  | Alternates dim-blue ↔ bright-blue every 0.5 s. |
| `off`           | LEDs off, refreshed every 1 s.                 |

`set_mode` is thread-safe and `start` / `stop` manage the worker
thread. On `stop` the LED group is reset so nothing stays stuck.

<!-- snippet: led_worker_loop -->
<!-- generated from robot/src/bridge.py:356 by docs/embed_snippets.py -->
```python
def _run(self):
    pulse_phase = 0
    while not self._stop.is_set():
        mode = self.get_mode()
        try:
            if mode == "idle":
                self._leds.fadeRGB(self.GROUP, self.IDLE_COLOR, self.IDLE_FADE)
                if self._stop.wait(self.IDLE_TICK):
                    break
            elif mode == "search_pulse":
                color = self.PULSE_BRIGHT if pulse_phase else self.PULSE_DIM
                pulse_phase ^= 1
                self._leds.fadeRGB(self.GROUP, color, self.PULSE_FADE)
            elif mode == "off":
                self._leds.off(self.GROUP)
                if self._stop.wait(self.OFF_TICK):
                    break
            else:
                if self._stop.wait(0.2):
                    break
        except Exception as exc:
            print("[leds] tick failed mode={} err={}".format(mode, to_text(exc)))
            if self._stop.wait(0.5):
                break
```
<!-- /snippet -->

---

## Tablet rendering — [`TabletDebugReporter`](../../robot/src/bridge.py)

Thin producer/consumer wrapper around `ALTabletService.showWebview`.

- **Queue** — a bounded `Queue` (`TABLET_REPORTER_QUEUE_SIZE`); when
  full, the oldest payload is dropped to make room. This prevents
  runaway backlog if the tablet is slow to render.
- **Rate limit** — `TABLET_DEBUG_MIN_INTERVAL_AUDIO` between posts,
  unless the caller passes `force=True` (used for state transitions).
- **Worker** — drains the queue every 0.2 s and calls `_post`.
- **`_post` selects a template based on `payload["ui"]`:**
  - `"split_chat_debug"` → live agent debug view (life state, animation,
    session state, idle countdown, ability flags, last N debug lines,
    user + Pepper bubbles). Rendered with `TABLET_SPLIT_CHAT_HTML_TEMPLATE`.
  - `"chat_history"` → transcript bubbles (`TABLET_CHAT_HISTORY_HTML_TEMPLATE`).
  - *anything else* → plain centered text (`TABLET_INLINE_HTML_TEMPLATE`).
  The HTML is base64'd into a `data:text/html;charset=utf-8,...` URL
  and handed to `showWebview`.

> ⚠️ In `main()` the reporter is constructed with `enabled=False` so
> the bridge does **not** drive the tablet anymore — the
> [`tablet_server.py`](../../services/src/live/tablet_server.py) service
> owns it via `/tablet/url`. The reporter remains to handle direct
> `/tablet/text_inline` HTTP calls.

---

## Camera — [`capture_camera_snapshot()`](../../robot/src/utils.py)

One-shot JPEG grab from the top camera (`kVGA`, RGB888) backing the
`/camera/snapshot` endpoint. All capture parameters
(`CAMERA_SNAPSHOT_CAMERA_INDEX`, `CAMERA_SNAPSHOT_RESOLUTION`,
`CAMERA_SNAPSHOT_QUALITY`, …) live in `config.py`.

Steps:

1. Optionally `ALBasicAwareness.pauseAwareness()` — Pepper's face
   tracker constantly pans the head; pausing prevents motion blur.
2. `ALVideoDevice.subscribeCamera(...)` → `getImageRemote()` → decode
   with Pillow → optional downscale to `CAMERA_SNAPSHOT_MAX_SIDE`.
3. Encode as JPEG (quality from `CAMERA_SNAPSHOT_QUALITY`, default 82).
4. `unsubscribe()` and `resumeAwareness()` in a `finally` so the
   camera is always released.

The endpoint handler additionally holds a `camera_lock` around the
capture because NAOqi's video ring buffer does not tolerate concurrent
subscribes from the same client name.

<!-- snippet: capture_camera_snapshot -->
<!-- generated from robot/src/utils.py:230 by docs/embed_snippets.py -->
```python
def capture_camera_snapshot(video, awareness=None, pause_awareness=True):
    """Grab one RGB frame from Pepper's top camera and return JPEG bytes.

    If `awareness` is provided and `pause_awareness` is True,
    `pauseAwareness()` is called around the capture and
    `resumeAwareness()` restores it afterwards. This prevents motion
    blur caused by Pepper's head drifting during face tracking.
    The `finally` block guarantees the camera subscription and
    awareness state are always released, even on errors.
    """
    from PIL import Image  # lazy import so bridge can boot without Pillow

    if video is None:
        raise RuntimeError("ALVideoDevice unavailable")

    paused = False
    if awareness is not None and pause_awareness:
        try:
            awareness.pauseAwareness()
            paused = True
        except Exception as exc:
            print("[camera] pauseAwareness warning:", to_text(exc))

    handle = None
    try:
        handle = video.subscribeCamera(
            CAMERA_SNAPSHOT_NAME,
            CAMERA_SNAPSHOT_CAMERA_INDEX,
            CAMERA_SNAPSHOT_RESOLUTION,
            CAMERA_SNAPSHOT_COLOR_SPACE,
            CAMERA_SNAPSHOT_FPS,
        )
        img = video.getImageRemote(handle)
        if img is None:
            raise RuntimeError("getImageRemote returned None")

        width, height = img[0], img[1]
        pil = Image.frombytes("RGB", (width, height), bytes(img[6]))
        video.releaseImage(handle)

        if CAMERA_SNAPSHOT_MAX_SIDE and max(width, height) > CAMERA_SNAPSHOT_MAX_SIDE:
            pil.thumbnail(
                (CAMERA_SNAPSHOT_MAX_SIDE, CAMERA_SNAPSHOT_MAX_SIDE),
                Image.LANCZOS,
            )

        buf = io.BytesIO()
        pil.save(buf, format="JPEG", quality=CAMERA_SNAPSHOT_QUALITY, optimize=True)
        return buf.getvalue()
    finally:
        if handle is not None:
            try:
                video.unsubscribe(handle)
            except Exception as exc:
                print("[camera] unsubscribe warning:", to_text(exc))
        if paused:
            try:
                awareness.resumeAwareness()
            except Exception as exc:
                print("[camera] resumeAwareness warning:", to_text(exc))
```
<!-- /snippet -->

---

## Regenerating embedded snippets

The `<!-- snippet: NAME -->` blocks above are driven by region markers
(`# region: NAME` / `# endregion`) in the source files. To refresh them
after editing the source:

```bash
uv run python docs/embed_snippets.py
# or, in CI, fail if anything is stale:
uv run python docs/embed_snippets.py --check
```

---

## Quick reference: config knobs

All read from [`robot/src/config.py`](../../robot/src/config.py). The
ones that most affect runtime behavior:

| Name                         | Effect                                               |
|------------------------------|------------------------------------------------------|
| `PEPPER_QI_URL`              | `tcp://<robot>:9559` — where to find Pepper.         |
| `PEPPER_STREAM_RATE`         | PCM sample rate over the TCP link.                   |
| `PEPPER_CHUNK_LIMIT_FRAMES`  | Max frames per `sendRemoteBufferToOutput` call.      |
| `PEPPER_PLAYBACK_BATCH_FRAMES` | Target batch size drained from the queue.          |
| `PEPPER_MAX_BUFFER_FRAMES`   | Overflow threshold — beyond this we drop + flush.    |
| `PEPPER_OUTPUT_VOLUME`       | Initial `ALAudioDevice` output volume.               |
| `LIFE_*` flags               | Autonomous-life ability profile applied at startup.  |
| `TOUCH_AUTONOMOUS_LIFE`      | If False, the bridge won't touch life state/abilities. |
| `HEAD_LOCK_YAW_RAD` / `HEAD_LOCK_PITCH_RAD` / `HEAD_LOCK_SPEED` | Default head pose for `POST /motion/head_lock`. |
| `SLEEP_HEAD_PITCH_RAD` / `WAKE_HEAD_PITCH_RAD` / `SLEEP_HEAD_SPEED` / `WAKE_HEAD_SPEED` | Head pose + servo speed for `POST /motion/{sleep,wake}`. |
| `BRIDGE_URL` / `BRIDGE_BIND_HOST` / `TCP_PORT` | HTTP/TCP binds.                 |
| `BRIDGE_CONNECT_POLL_INTERVAL_SEC` | Retry delay when Pepper is offline at startup. |
| `CAMERA_SNAPSHOT_*`          | Top-camera capture parameters (index, resolution, FPS, quality, downscale). |
| `TABLET_*` templates + limits | Tablet rendering behaviour.                         |
