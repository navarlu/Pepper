#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Shared helpers for the Pepper bridge.

Pure functions and protocol constants used by `robot/src/bridge.py`.
Anything that holds state or runs in its own thread (the LED manager,
the tablet reporter, the HTTP server) stays in `bridge.py`.
"""

from __future__ import print_function

import audioop
import io
import socket
import time

import qi

from config import (
    BRIDGE_CONNECT_POLL_INTERVAL_SEC,
    CAMERA_SNAPSHOT_CAMERA_INDEX,
    CAMERA_SNAPSHOT_COLOR_SPACE,
    CAMERA_SNAPSHOT_FPS,
    CAMERA_SNAPSHOT_MAX_SIDE,
    CAMERA_SNAPSHOT_NAME,
    CAMERA_SNAPSHOT_QUALITY,
    CAMERA_SNAPSHOT_RESOLUTION,
)


try:
    text_type = unicode  # noqa: F821 (py2)
except NameError:
    text_type = str


# -- Protocol constants (PCM TCP stream between audio-bridge and bridge) ------

CONTROL_FRAME_FLUSH = 0
CONTROL_FRAME_PING = 4294967295   # 0xFFFFFFFF
# Drain handshake: audio-bridge sends DRAIN_REQ when send_message_to_user
# finishes its last wait_for_playout(); the bridge replies with DRAIN_ACK
# once ALAudioDevice's queue is empty (plus one batch of tail to cover
# NAOqi's internal buffer). Lets the experiment worker return at the
# real end-of-speech instead of the LiveKit emitter drain.
CONTROL_FRAME_DRAIN_REQ = 4294967294  # 0xFFFFFFFE  service → robot
CONTROL_FRAME_DRAIN_ACK = 4294967293  # 0xFFFFFFFD  robot → service


# -- Text / type coercion -----------------------------------------------------

def to_text(x):
    """Best-effort coercion of any value to unicode text (Py2/Py3 safe)."""
    try:
        if isinstance(x, bytes):
            return x.decode("utf-8", "ignore")
    except Exception:
        pass
    try:
        return text_type(x)
    except Exception:
        return str(x)


# -- NAOqi connection ---------------------------------------------------------

def pepper_reachable(host, port, timeout=3.0):
    """Quick TCP probe — True if NAOqi's port is accepting connections."""
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except (OSError, socket.timeout):
        return False


def parse_qi_url(qi_url):
    """Extract (host, port) from a `tcp://host:port` string."""
    addr = qi_url.replace("tcp://", "")
    if ":" in addr:
        host, port = addr.rsplit(":", 1)
        return host, int(port)
    return addr, 9559


# region: connect_session
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
# endregion


def wait_for_service(session, service_name, timeout_sec=90.0, retry_sec=1.0):
    """Fetch a NAOqi service proxy, retrying until it's registered.

    NAOqi services come up in a loose order after boot — asking for
    `ALAnimationPlayer` or `ALTabletService` immediately after
    `qi.Session.connect` often fails. Polls `session.service(...)`
    until it succeeds or the deadline expires. Raises `RuntimeError`
    on timeout.
    """
    deadline = time.time() + float(timeout_sec)
    last_err = None
    while time.time() < deadline:
        try:
            svc = session.service(service_name)
            print("[bridge] service ready:", service_name)
            return svc
        except Exception as exc:
            last_err = exc
            print("[bridge] waiting for service '{}'... {}".format(
                service_name, to_text(exc)))
            time.sleep(retry_sec)
    raise RuntimeError(
        "Timed out waiting for service '{}': {}".format(service_name, to_text(last_err))
    )


# -- Animation name resolution ------------------------------------------------

def load_animations_map(path):
    """Read the JSON alias -> behavior-path map used by `/animation/<name>`.

    Maps friendly names (e.g. `"hello"`) to full NAOqi behavior paths
    (e.g. `"animations/Stand/Gestures/Hey_1"`). Returns `{}` on any
    error so the bridge still runs with direct behavior paths only.
    """
    import json

    try:
        with open(path, "r") as f:
            data = json.load(f) or {}
        normalized = {}
        for k, v in data.items():
            key = to_text(k).strip()
            val = to_text(v).strip()
            if key and val:
                normalized[key] = val
        print("[bridge] loaded animations:", len(normalized), "from", path)
        return normalized
    except Exception as exc:
        print("[bridge] failed to load animations map:", to_text(exc))
        return {}


# region: resolve_animation_name
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
# endregion


# -- TCP / audio helpers ------------------------------------------------------

def recv_all(conn, size):
    """Receive exactly `size` bytes from `conn`, or `None` on EOF."""
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = conn.recv(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def mono16_to_stereo16(raw_mono):
    """Duplicate a mono int16 PCM buffer into stereo (L,R,L,R,...).

    Uses `audioop.tostereo` (C) — a Python loop here is too jittery
    and breaks realtime playback.
    """
    return audioop.tostereo(raw_mono, 2, 1, 1)


def normalize_output_volume(raw_value):
    """Clamp an arbitrary input into a valid `ALAudioDevice` volume (0..100)."""
    volume = int(raw_value)
    if volume < 0:
        return 0
    if volume > 100:
        return 100
    return volume


# -- Camera snapshot ----------------------------------------------------------

# region: capture_camera_snapshot
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
# endregion


