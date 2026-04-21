# Pepper Body Control — qi/NAOqi reference

A practical reference for embodying Pepper beyond audio+tablet+animations: cameras, microphone, eye LEDs, sensors, and how to use them **without** breaking Autonomous Life's face-tracking and idle sway.

- **Runtime target:** NAOqi 2.5.x on the original Pepper (Python 2.7 SDK, `import qi`)
- **Base doc URL pattern:** `http://doc.aldebaran.com/2-5/naoqi/<area>/<service>.html`
- **Session idiom (everything in this doc assumes this is already set up):**

```python
import qi
session = qi.Session()
session.connect("tcp://pepper.local:9559")   # or the IP

tts = session.service("ALTextToSpeech")
tts.say("hello")
```

---

## 1. What this project already uses

Grounding: these services are *already wired up* in the bridge and safe-startup. New work should extend this, not duplicate it.

| Service | Where | What it does |
|---|---|---|
| `ALAudioDevice` | [robot/src/bridge.py](../robot/src/bridge.py) TCP server on port 55555 | Streams PCM to the speakers via `sendRemoteBufferToOutput` |
| `ALAnimationPlayer` / `ALBehaviorManager` | [robot/src/bridge.py](../robot/src/bridge.py) `POST /animation/{name}` | Runs animations (with animations.json name mapping) |
| `ALTabletService` | [robot/src/bridge.py](../robot/src/bridge.py) `POST /tablet/url`, `POST /tablet/text_inline` | `showWebview` for URLs/HTML; HTML templates in [config.py](../robot/src/config.py) |
| `ALAutonomousLife` | [robot/src/bridge.py](../robot/src/bridge.py) | Read-only by default (`TOUCH_AUTONOMOUS_LIFE=False`); abilities profile only applied when opted in |
| `ALMotion` / `ALRobotPosture` / `ALDiagnosis` | [robot/scripts/safe_startup.py](../robot/scripts/safe_startup.py) | `wakeUp` → `StandInit` → diagnosis dump |

Not currently used, but documented here because they're the next step: **ALVideoDevice, ALPhotoCapture, ALAudioRecorder, ALLeds, ALBasicAwareness, ALFaceDetection, ALTracker, ALMemory, ALTouch, ALAnimatedSpeech**.

---

## 2. Coexistence rule (the core mental model)

Autonomous Life is **on** on this robot because face-tracking + idle sway feel alive. Most embodiment extensions do **not** need it off. Pick the narrowest switch:

| You want to… | Do this |
|---|---|
| Take a picture | Just `ALVideoDevice.subscribeCamera(...)` — shared service, no Life changes needed |
| Freeze head briefly for a sharp photo | `ALBasicAwareness.pauseAwareness()` → snapshot → `resumeAwareness()` (keeps tracked person) |
| Record audio | `ALAudioRecorder.startMicrophonesRecording(...)` — no Life changes |
| Change eye color | `ALLeds.fadeRGB(...)` — no Life changes |
| Run a scripted animation | `ALAnimationPlayer.run(...)` — BasicAwareness auto-pauses during head motion, then resumes |
| Walk to a waypoint | `ALMotion.moveTo(...)` — awareness auto-pauses |
| Long routine owning the body | Flip specific abilities: `life.setAutonomousAbilityEnabled("BackgroundMovement", False)`, re-enable after |
| Full manual control (calibration only) | `life.setState("disabled")` — but motors go soft; you must `motion.wakeUp()` after |

**Rule of thumb:** *don't* call `setState("disabled")`. Prefer `pauseAwareness()` for brief pauses, or toggle one ability at a time.

**Why it works:** ALVideoDevice is a multi-subscriber distributor — ALFaceDetection, ALPeoplePerception, ALBasicAwareness, and your own subscriber all coexist. Subscribing does not steal the camera. See the [ALVideoDevice API 2.5](http://doc.aldebaran.com/2-5/naoqi/vision/alvideodevice-api.html).

---

## 3. Service reference

### 3.1 ALVideoDevice — raw camera access

Subscribe / get / release / unsubscribe lifecycle. Works while Autonomous Life is running.

```python
video = session.service("ALVideoDevice")

handle = video.subscribeCamera(
    "kampion_top",   # name — if it collides it's auto-suffixed "_2", so always use the returned handle
    0,               # cameraIndex: 0=top, 1=bottom, 2=depth, 3=stereo
    2,               # resolution: 0=QQVGA 1=QVGA 2=VGA 3=4VGA 4=16VGA
    11,              # colorSpace: 0=Yuv 9=YUV422 10=YUV 11=RGB 13=BGR 17=Depth
    10               # fps: 1,5,10,15,20,30 (depth ≤ 20)
)

img = video.getImageRemote(handle)   # blocks up to ~1 s
# img is a 12-element ALValue:
#   [0] width, [1] height, [2] nbLayers, [3] colorSpace,
#   [4] timeStamp_s, [5] timeStamp_us,
#   [6] buffer (raw pixels),
#   [7] cameraID, [8] leftAngle, [9] topAngle, [10] rightAngle, [11] bottomAngle

video.releaseImage(handle)   # MUST call — otherwise the ring buffer deadlocks
video.unsubscribe(handle)    # when done permanently
```

Convert the buffer:

```python
import numpy as np
from PIL import Image

w, h = img[0], img[1]
buf  = img[6]

# RGB (colorSpace 11):
pil = Image.fromstring("RGB", (w, h), buf)     # Py2.7; use frombytes() on Py3
arr = np.frombuffer(buf, dtype=np.uint8).reshape((h, w, 3))

# Depth (colorSpace 17): uint16 millimetres
depth = np.frombuffer(buf, dtype=np.uint16).reshape((h, w))
```

**Gotchas**
- Forget `releaseImage` → the ring buffer locks up after a few frames.
- Always pass the **handle** returned from `subscribeCamera` to subsequent calls (not the original name — name may have been disambiguated).
- On NAOqi 2.5, the buffer may come back as `str` or `bytearray` depending on the libqi build; wrap in `bytes()` to normalize.
- Pepper cameras: top 2D (up to 2560×1920), bottom 2D (up to 2560×1920), 3D depth (QVGA @ 20 fps max). Call `video.getCameraIndexes()` to discover what's present.

Docs: [ALVideoDevice API 2.5](http://doc.aldebaran.com/2-5/naoqi/vision/alvideodevice-api.html), [Pepper video overview](http://doc.aldebaran.com/2-5/family/pepper_technical/video_overview.html), [Retrieving images example](http://doc.aldebaran.com/2-5/dev/python/examples/vision/get_image.html).

### 3.2 ALPhotoCapture — easy JPEG snapshots

Thin wrapper around ALVideoDevice. Slower (~0.5–1 s) because it re-subscribes and settles exposure each call.

```python
photo = session.service("ALPhotoCapture")
photo.setResolution(2)          # kVGA
photo.setPictureFormat("jpg")   # "jpg" | "png" | "bmp"
photo.setCameraID(0)            # 0=top, 1=bottom
paths = photo.takePictures(1, "/home/nao/recordings/cameras/", "snap")
# -> ["/home/nao/recordings/cameras/snap.jpg"]
```

Always writes to disk; no in-memory variant in 2.5. 2D only (no depth).

Docs: [ALPhotoCapture tutorial 2.5](http://doc.aldebaran.com/2-5/naoqi/vision/alphotocapture-tuto.html).

### 3.3 ALAudioDevice — microphone input

**Caveat:** raw mic subscription requires a **locally registered** NAOqi module implementing `processRemote(nbChannels, nbSamples, timestamp, buffer)`. From a plain remote Python script this is painful. Use **ALAudioRecorder** (§3.4) or ALMemory sound events instead.

Still useful methods: `getOutputVolume/setOutputVolume/muteAudioOut`, `playSine(freq, gain, pan, duration)`. Sample rates 16000 or 48000; channels = 4 (rear/left/front/right) or mono-front.

Docs: [ALAudioDevice 2.5](http://doc.aldebaran.com/2-5/naoqi/audio/alaudiodevice.html).

### 3.4 ALAudioRecorder — record to WAV/OGG

The practical path for microphone capture from a remote Python client.

```python
import time
rec = session.service("ALAudioRecorder")

# channels = [left, right, front, rear] — 1=include, 0=skip
rec.stopMicrophonesRecording()  # guard — only one recording at a time
rec.startMicrophonesRecording(
    "/home/nao/recordings/microphones/clip.wav",
    "wav",       # "wav" or "ogg"
    16000,       # Hz
    [0, 0, 1, 0] # front mic only
)
time.sleep(5)
rec.stopMicrophonesRecording()
```

Format constraints:
- **WAV** = 16 kHz mono **or** 48 kHz 4-channel
- **OGG** = 48 kHz 4-channel only

Pull the file afterwards: `scp nao@pepper.local:/home/nao/recordings/microphones/clip.wav .`. Not affected by Autonomous Life.

Docs: [ALAudioRecorder API 2.5](http://doc.aldebaran.com/2-5/naoqi/audio/alaudiorecorder-api.html).

### 3.5 ALLeds — LED control

Every LED or named group is addressed by string. Pepper's groups differ from NAO — some NAO groups don't exist on Pepper.

```python
leds = session.service("ALLeds")

leds.on("FaceLeds")                             # full white
leds.off("FaceLeds")
leds.fadeRGB("FaceLeds", 0x00FF0000, 0.5)       # red, 500 ms fade
leds.fadeRGB("FaceLeds", "blue", 0.3)           # named colors accepted
leds.fadeListRGB("FaceLeds",
                 [0x00FF0000, 0x0000FF00, 0x000000FF],
                 [0.5, 1.0, 1.5])                # color sequence at cumulative times
leds.randomEyes(3.0)                             # 3 s rainbow
leds.rotateEyes(0x00FF0000, 1.0, 3.0)            # color, period, total duration
leds.reset("FaceLeds")                           # back to default white
leds.post.fadeRGB("FaceLeds", 0x00FFFF00, 0.4)   # non-blocking variant
```

Colors accepted by `fadeRGB`: `0x00RRGGBB` int, hex string, or one of `"white" "red" "green" "blue" "yellow" "magenta" "cyan"`.

**Pepper LED groups (verified present)**

RGB (full colour):
- `FaceLeds` — all 16 eye LEDs (8 per eye)
- `FaceLedsLeft`, `FaceLedsRight`
- `FaceLedsLeftExternal`, `FaceLedsLeftInternal`, `FaceLedsRightExternal`, `FaceLedsRightInternal`
- `FaceLedsTop`, `FaceLedsBottom`
- `ChestLeds` (shoulder/chest badge)
- `ShoulderLeds`

Mono (blue-only on Pepper — address `/Blue/Actuator/Value` or use `fade`, not `fadeRGB`):
- `EarLeds`, `LeftEarLeds`, `RightEarLeds`
- `BrainLeds` (status indicator, limited control)

**Not present on Pepper** (NAO-only — raises `ALError`): `FeetLeds`, `LFootLeds`, `RFootLeds`.

Use `leds.listGroups()` and `leds.listGroup("FaceLeds")` to discover at runtime.

Docs: [ALLeds 2.5](http://doc.aldebaran.com/2-5/naoqi/sensors/alleds.html), [Pepper LEDs hardware](http://doc.aldebaran.com/2-5/family/pepper_technical/leds_pep.html).

### 3.6 ALAutonomousLife — the orchestrator

```python
life = session.service("ALAutonomousLife")

state = life.getState()              # "solitary" | "interactive" | "disabled" | "safeguard"
life.setAutonomousAbilityEnabled("BasicAwareness", True)
life.getAutonomousAbilityEnabled("BackgroundMovement")
```

States:
- `solitary` — default idle; BasicAwareness + BackgroundMovement + AutonomousBlinking active, no focused activity
- `interactive` — engaged with a human; a user activity has focus
- `safeguard` — danger detected (fall, thermal); `stopAll()` called
- `disabled` — everything Life-related off; your code owns the robot; stiffness drops

Autonomous abilities (what this project's env vars in [config.py](../robot/src/config.py) map onto):
- `BasicAwareness` — face/sound head tracking  *(this is the tracking Lucas wants to keep)*
- `BackgroundMovement` — slight idle body sway  *(the "alive" feeling)*
- `AutonomousBlinking` — eye LED blink
- `ListeningMovement` — head nods while listening
- `SpeakingMovement` — gestures while TTS plays

Priority: `SpeakingMovement`/`ListeningMovement` > `BackgroundMovement`; `BasicAwareness` + `AutonomousBlinking` run in parallel regardless.

**Gotcha:** manual transition `interactive → solitary` is rejected. If you need it, go via `disabled` first (and re-`wakeUp`).

Docs: [ALAutonomousLife API 2.5](http://doc.aldebaran.com/2-5/naoqi/interaction/autonomouslife-api.html), [Autonomous Abilities 2.5](http://doc.aldebaran.com/2-5/ref/life/autonomous_abilities_management.html).

### 3.7 ALBasicAwareness — the face-tracking service

Under the hood of the `BasicAwareness` ability, but also directly callable. This is the precise tool for "briefly stop tracking".

```python
awr = session.service("ALBasicAwareness")

awr.setEngagementMode("Unengaged")          # "Unengaged" | "SemiEngaged" | "FullyEngaged"
awr.setTrackingMode("Head")                 # "Head" | "BodyRotation" | "WholeBody" | "MoveContextually"
awr.setStimulusDetectionEnabled("Sound", True)
awr.setStimulusDetectionEnabled("Movement", True)
awr.setStimulusDetectionEnabled("People", True)
awr.setStimulusDetectionEnabled("Touch", True)

awr.pauseAwareness()     # freezes head BUT remembers the tracked person
awr.resumeAwareness()    # picks back up with the same person
awr.stopAwareness()      # forgets — only use when you really want to reset
awr.startAwareness()
```

**Key insight**: if your code moves the head via ALMotion, BasicAwareness auto-pauses until the motion finishes, then auto-resumes. `pauseAwareness()` is for when you want the head still for something *other* than a motion call (e.g. a photo).

Docs: [ALBasicAwareness API 2.5](http://doc.aldebaran.com/2-5/naoqi/interaction/autonomousabilities/albasicawareness-api.html).

### 3.8 ALFaceDetection / ALTracker — direct tracking

When BasicAwareness isn't granular enough, drive tracking yourself.

```python
face_det = session.service("ALFaceDetection")
face_det.subscribe("MyFaceDet", 200, 0.0)    # name, periodMs, confidenceThreshold

tracker = session.service("ALTracker")
tracker.registerTarget("Face", 0.15)          # 0.15 m expected face width
tracker.setMode("Head")                       # "Head" | "WholeBody" | "Move"
tracker.setEffector("None")
tracker.track("Face")
# ... later
tracker.stopTracker()
tracker.unregisterAllTargets()
face_det.unsubscribe("MyFaceDet")
```

Face-detection results land in ALMemory as `"FaceDetected"` (see §3.12).

Docs: [ALTracker samples 2.5](http://doc.aldebaran.com/2-5/naoqi/trackers/trackers-sample.html).

### 3.9 ALMotion — joints, walk, stiffness, breathing

```python
motion = session.service("ALMotion")

motion.wakeUp()                               # motors on, init posture
motion.rest()                                 # safe posture, motors off

motion.setStiffnesses("Body", 1.0)            # 0.0 limp .. 1.0 full
motion.setStiffnesses(["HeadYaw","HeadPitch"], [0.8, 0.8])

# Non-blocking:
motion.setAngles(["HeadYaw","HeadPitch"], [0.0, -0.2], 0.1)     # fractionMaxSpeed

# Blocking:
motion.angleInterpolation("HeadPitch", -0.3, 1.0, True)
motion.angleInterpolationWithSpeed("HeadPitch", 0.0, 0.2)

# Walk (Pepper has an omni base):
motion.moveTo(0.2, 0.0, 0.0)                  # x m fwd, y m left, theta rad
motion.moveToward(0.5, 0.0, 0.0)              # velocity command
motion.stopMove()

motion.setBreathEnabled("Body", True)         # "Body"|"Arms"|"Head"
motion.getAngles("HeadYaw", True)             # useSensors=True for actual, False for commanded
motion.robotIsWakeUp()
```

`moveTo` is blocking — use `motion.post.moveTo(...)` or pass `_async=True` for fire-and-forget. Moving the head auto-pauses BasicAwareness.

Docs: [ALMotion API 2.5](http://doc.aldebaran.com/2-5/naoqi/motion/almotion-api.html).

### 3.10 ALTextToSpeech / ALAnimatedSpeech

Plain TTS:

```python
tts = session.service("ALTextToSpeech")
tts.setLanguage("English")       # resets to default every reboot
tts.setParameter("speed", 90)    # 50..200
tts.setParameter("pitchShift", 1.1)
tts.setVolume(0.8)
tts.say("Hello Lucas.")
```

Animated TTS — body gestures synced with speech:

```python
asp = session.service("ALAnimatedSpeech")
asp.setBodyLanguageMode(2)       # 0=disabled, 1=random, 2=contextual
asp.say("^start(animations/Stand/Gestures/Hey_1) Hi there ^wait(animations/Stand/Gestures/Hey_1)")
asp.say("Watch this", {"bodyLanguageMode": "contextual"})
```

Tags: `^start(path)`, `^wait(path)`, `^run(path)`, `^stop(path)`, `^mode(disabled|random|contextual)`, `^pCall(...)`.

Docs: [ALTextToSpeech API 2.5](http://doc.aldebaran.com/2-5/naoqi/audio/altexttospeech-api.html), [ALAnimatedSpeech API 2.5](http://doc.aldebaran.com/2-5/naoqi/audio/alanimatedspeech-api.html).

### 3.11 ALAnimationPlayer / ALBehaviorManager

Already used by the bridge — short reference for when you extend it.

```python
anim = session.service("ALAnimationPlayer")
anim.run("animations/Stand/Gestures/Hey_1")                  # blocks
future = anim.run("animations/Stand/Gestures/Hey_1", _async=True)
future.cancel()
anim.runTag("hello")                                         # random tagged animation
anim.runTag("hello", "animations/Stand")
```

Pepper only has `animations/Stand/*` paths (no Sit/Crouch). Categories include `Stand/Gestures/*`, `Stand/Emotions/Positive/*`, `Stand/BodyTalk/*`. See the short-name mapping in [robot/data/animations.json](../robot/data/animations.json).

```python
bm = session.service("ALBehaviorManager")
bm.getInstalledBehaviors()
bm.isBehaviorInstalled("my-app/behavior_1")
bm.startBehavior("my-app/behavior_1")     # non-blocking
bm.runBehavior("my-app/behavior_1")       # blocking
bm.stopBehavior("my-app/behavior_1")
bm.stopAllBehaviors()
```

Docs: [ALAnimationPlayer 2.5](http://doc.aldebaran.com/2-5/naoqi/motion/alanimationplayer.html).

### 3.12 ALMemory — events and sensors (the pub/sub bus)

Modern qi-signal API works from plain remote Python — **no ALModule boilerplate needed**.

```python
memory = session.service("ALMemory")

# One-shot read:
val = memory.getData("FaceDetected")
head_front = memory.getData("Device/SubDeviceList/Head/Touch/Front/Sensor/Value")

# Subscribe (qi-style):
def on_face(value):
    if value and len(value) >= 2:
        print("face event:", value)

sub  = memory.subscriber("FaceDetected")
link = sub.signal.connect(on_face)
# ...
sub.signal.disconnect(link)
```

**Common events on Pepper**
- Faces: `FaceDetected`
- People: `PeoplePerception/PeopleList`, `PeoplePerception/JustArrived`, `PeoplePerception/JustLeft`
- Engagement: `ALBasicAwareness/HumanTracked`, `ALBasicAwareness/HumanLost`
- Head touch: `FrontTactilTouched`, `MiddleTactilTouched`, `RearTactilTouched`, `TouchChanged`
- Hand touch: `HandLeftBackTouched`, `HandLeftLeftTouched`, `HandLeftRightTouched`, `HandRightBackTouched`, `HandRightLeftTouched`, `HandRightRightTouched`
- Bumpers: `BumperLeft`, `BumperRight`, `BumperBack`
- Speech: `WordRecognized`, `SpeechDetected`, `ALTextToSpeech/Status`, `Dialog/LastInput`
- Power: `BatteryChargeChanged`, `BatteryPowerPluggedChanged`
- Motion: `ALMotion/MoveFailed`
- Tablet: `ALTabletService/error`

Touch events are bistable — `True` on press, `False` on release.

Docs: [ALMemory tutorial 2.5](http://doc.aldebaran.com/2-5/naoqi/core/almemory-tuto.html).

### 3.13 ALTouch — touch sensor state

```python
touch = session.service("ALTouch")
touch.getSensorList()
touch.getStatus()       # [[name, isActive, [[sensor, value], ...]], ...]
```

Sensor names on Pepper: head front/middle/rear, left/right hand back/left/right, bumpers front-left/front-right/back. Subscribe to events via ALMemory (§3.12).

Docs: [ALTouch API 2.5](http://doc.aldebaran.com/2-5/naoqi/sensors/altouch-api.html).

### 3.14 ALTabletService — chest tablet

Already used by the bridge — short reference.

```python
tablet = session.service("ALTabletService")

tablet.showImage("http://198.18.0.1/apps/my-app/pic.png")   # robot IP from tablet side = 198.18.0.1
tablet.hideImage()
tablet.loadUrl("https://example.com"); tablet.showWebview()
tablet.reloadPage(True)
tablet.hideWebview(); tablet.cleanWebview()
tablet.playVideo("http://198.18.0.1/apps/my-app/clip.mp4"); tablet.stopVideo()
tablet.loadApplication("my-app")
```

Tablet signals: `onTouchDown`, `onTouchUp`, `onJsEvent`, `onPageFinished`, `onInputText`.

**Gotchas**
- Tablet browser is Chromium ~30 — ES5 only (no `const`/`fetch`).
- Image cache is aggressive — append a `?v=<nonce>` query string to bust.
- Only present on Pepper 1.6/1.8 models.

Docs: [ALTabletService API 2.5](http://doc.aldebaran.com/2-5/naoqi/core/altabletservice-api.html).

---

## 4. Practical recipes

### (a) Grab a photo without disrupting face tracking

```python
import qi, time
from PIL import Image

session = qi.Session()
session.connect("tcp://pepper.local:9559")

video = session.service("ALVideoDevice")
awr   = session.service("ALBasicAwareness")

awr.pauseAwareness()                 # keeps tracked person; just freezes the head
try:
    handle = video.subscribeCamera("kampion_snap", 0, 2, 11, 10)   # top, VGA, RGB, 10 fps
    try:
        img = video.getImageRemote(handle)
        w, h, buf = img[0], img[1], img[6]
        Image.fromstring("RGB", (w, h), buf).save("/tmp/snap.jpg")
    finally:
        video.releaseImage(handle)
        video.unsubscribe(handle)
finally:
    awr.resumeAwareness()
```

`pauseAwareness()` is optional — you can skip it if motion blur from the sway doesn't bother you. Subscribing itself doesn't fight tracking.

### (b) Record N seconds of microphone audio

```python
import time
rec = session.service("ALAudioRecorder")

path = "/home/nao/recordings/microphones/clip.wav"
rec.stopMicrophonesRecording()       # guard — only one recording at a time
rec.startMicrophonesRecording(path, "wav", 16000, [0, 0, 1, 0])   # front mic mono
time.sleep(5)
rec.stopMicrophonesRecording()

# Pull it off the robot afterwards:
#   scp nao@pepper.local:/home/nao/recordings/microphones/clip.wav .
```

### (c) Eye color for 3 s, then restore

```python
import time
leds = session.service("ALLeds")

leds.fadeRGB("FaceLeds", 0x0000FF00, 0.3)    # green in 300 ms
time.sleep(3.0)
leds.reset("FaceLeds")                        # back to default white

# Non-blocking (fade runs on the robot, returns immediately):
leds.post.fadeRGB("FaceLeds", 0x00FF00FF, 0.5)
```

### (d) Subscribe to face / touch events via ALMemory

```python
memory = session.service("ALMemory")

def on_face(value):
    # value = [timestamp_info, FaceInfoArray, cameraPoseInTorsoFrame, cameraPoseInRobotFrame, cameraID]
    if value and len(value) >= 2 and value[1]:
        print("face visible")

def on_front_head(value):
    print("front head touch:", value)         # True on press, False on release

face_sub = memory.subscriber("FaceDetected")
head_sub = memory.subscriber("FrontTactilTouched")

face_link = face_sub.signal.connect(on_face)
head_link = head_sub.signal.connect(on_front_head)

# Later:
face_sub.signal.disconnect(face_link)
head_sub.signal.disconnect(head_link)
```

The subscriber must stay referenced — if it's garbage-collected, the connection silently dies.

### (e) Temporary "own the body" profile without killing face tracking

```python
life = session.service("ALAutonomousLife")

# Keep face tracking; stop only the idle sway (e.g. while running a precise gesture sequence):
prev_bg = life.getAutonomousAbilityEnabled("BackgroundMovement")
life.setAutonomousAbilityEnabled("BackgroundMovement", False)
try:
    # ... your scripted routine here ...
    pass
finally:
    life.setAutonomousAbilityEnabled("BackgroundMovement", prev_bg)
```

This is the correct pattern instead of `setState("disabled")` — you keep BasicAwareness on throughout.

---

## 5. Integration hints for this bridge

Breadcrumbs for when these get wired into [robot/src/bridge.py](../robot/src/bridge.py):

- **New HTTP endpoints** belong in `TabletOverlayHttpServer` alongside `/animation`, `/tablet/url`, `/audio/volume`. Suggested routes: `POST /camera/snapshot`, `POST /audio/record`, `POST /leds/eyes`.
- **Service acquisition** follows the existing optional pattern — `wait_service(...)` with a per-service timeout (`BRIDGE_OPTIONAL_SERVICE_TIMEOUT_SEC`). New services (`ALVideoDevice`, `ALLeds`, `ALAudioRecorder`, `ALBasicAwareness`) should be best-effort so the bridge still boots if a service is temporarily unavailable.
- **Config conventions** in [robot/src/config.py](../robot/src/config.py) use `PEPPER_*`, `LIFE_*`, `TABLET_*` prefixes. Suggest `CAMERA_*`, `LEDS_*`, `MIC_*` for new knobs.
- **Camera snapshots over HTTP**: return the image as bytes in the response body (e.g. `Content-Type: image/jpeg`) rather than writing to disk on the robot — simpler to consume from the voice-agent side.

---

## 6. Source URLs

**Vision**
- [ALVideoDevice API 2.5](http://doc.aldebaran.com/2-5/naoqi/vision/alvideodevice-api.html)
- [ALVideoDevice overview 2.5](http://doc.aldebaran.com/2-5/naoqi/vision/alvideodevice.html)
- [Pepper video & depth sensors](http://doc.aldebaran.com/2-5/family/pepper_technical/video_overview.html)
- [Retrieving images (Python)](http://doc.aldebaran.com/2-5/dev/python/examples/vision/get_image.html)
- [ALPhotoCapture tutorial](http://doc.aldebaran.com/2-5/naoqi/vision/alphotocapture-tuto.html)

**Audio**
- [ALAudioDevice](http://doc.aldebaran.com/2-5/naoqi/audio/alaudiodevice.html)
- [ALAudioRecorder API](http://doc.aldebaran.com/2-5/naoqi/audio/alaudiorecorder-api.html)
- [ALTextToSpeech API](http://doc.aldebaran.com/2-5/naoqi/audio/altexttospeech-api.html)
- [ALAnimatedSpeech API](http://doc.aldebaran.com/2-5/naoqi/audio/alanimatedspeech-api.html)

**LEDs & sensors**
- [ALLeds](http://doc.aldebaran.com/2-5/naoqi/sensors/alleds.html)
- [Pepper LEDs hardware](http://doc.aldebaran.com/2-5/family/pepper_technical/leds_pep.html)
- [ALTouch API](http://doc.aldebaran.com/2-5/naoqi/sensors/altouch-api.html)

**Autonomous Life & tracking**
- [ALAutonomousLife API](http://doc.aldebaran.com/2-5/naoqi/interaction/autonomouslife-api.html)
- [ALAutonomousLife advanced](http://doc.aldebaran.com/2-5/naoqi/interaction/autonomouslife_advanced.html)
- [Autonomous Abilities](http://doc.aldebaran.com/2-5/ref/life/autonomous_abilities_management.html)
- [ALBasicAwareness API](http://doc.aldebaran.com/2-5/naoqi/interaction/autonomousabilities/albasicawareness-api.html)
- [ALBasicAwareness Getting Started](http://doc.aldebaran.com/2-5/naoqi/interaction/autonomousabilities/albasicawareness-gettingstarted.html)
- [ALTracker samples](http://doc.aldebaran.com/2-5/naoqi/trackers/trackers-sample.html)

**Motion & behaviors**
- [ALMotion API](http://doc.aldebaran.com/2-5/naoqi/motion/almotion-api.html)
- [Stiffness control](http://doc.aldebaran.com/2-5/naoqi/motion/control-stiffness-api.html)
- [ALAnimationPlayer](http://doc.aldebaran.com/2-5/naoqi/motion/alanimationplayer.html)
- [ALAnimationPlayer tutorials](http://doc.aldebaran.com/2-5/naoqi/motion/alanimationplayer-tutorial.html)

**Core**
- [qi.Session Python API](http://doc.aldebaran.com/2-5/dev/libqi/api/python/session.html)
- [Python qimessaging client guide](http://doc.aldebaran.com/2-5/dev/libqi/guide/py-client.html)
- [ALMemory tutorial](http://doc.aldebaran.com/2-5/naoqi/core/almemory-tuto.html)
- [ALTabletService API](http://doc.aldebaran.com/2-5/naoqi/core/altabletservice-api.html)
- [Using Pepper's tablet](http://doc.aldebaran.com/2-5/getting_started/creating_applications/using_peppers_tablet.html)
