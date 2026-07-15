"""Shared RealSense recording helper for the animation-metadata experiment.

Keeps one pipeline open across many clips (restarting the pipeline costs
seconds per clip) and holds a constant frame rate by disabling auto-exposure
priority, so clip durations stay truthful for gesture-duration measurement.
"""

import os
import threading
import time

import cv2
import numpy as np
import pyrealsense2 as rs

WIDTH = 1280
HEIGHT = 720
FPS = 30
WARMUP_FRAMES = 30  # let auto-exposure settle after pipeline start
FPS_DRIFT_WARN = 0.05  # warn if effective fps deviates more than 5% from nominal

# The camera is physically mounted rotated 90° (portrait) so Pepper's full
# body fits the frame. Frames are rotated in software so clips play upright:
#   "cw"  — camera was turned counterclockwise, rotate frames clockwise
#   "ccw" — camera was turned clockwise, rotate frames counterclockwise
#   None  — camera is level, no rotation
# If the preview shows Pepper upside down, switch cw <-> ccw.
ROTATE = "ccw"

# Live-preview snapshot settings (recorder keeps the newest frame as JPEG so
# an MJPEG server can stream it without touching the camera).
PREVIEW_JPEG_QUALITY = 75
PREVIEW_MAX_FPS = 10.0


def apply_rotation(frame):
    """Rotate one BGR frame according to ROTATE."""
    if ROTATE == "cw":
        return cv2.rotate(frame, cv2.ROTATE_90_CLOCKWISE)
    if ROTATE == "ccw":
        return cv2.rotate(frame, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return frame


def output_size():
    """(width, height) of frames after rotation — VideoWriter needs this."""
    if ROTATE in ("cw", "ccw"):
        return (HEIGHT, WIDTH)
    return (WIDTH, HEIGHT)


class RealSenseRecorder(object):
    def __init__(self):
        self._pipeline = None
        # Newest rotated frame as JPEG for the live preview. Updated by the
        # record loops while recording, and by a background "idle pump"
        # thread between clips so the preview never freezes.
        self._latest_jpeg = None
        self._latest_ts = 0.0
        self._latest_lock = threading.Lock()
        self._recording = threading.Event()
        self._pump_stop = threading.Event()
        self._pump = None

    def latest_jpeg(self):
        """Newest preview frame as JPEG bytes (None before first frame)."""
        with self._latest_lock:
            return self._latest_jpeg

    def _update_latest(self, rotated_frame):
        now = time.time()
        if now - self._latest_ts < 1.0 / PREVIEW_MAX_FPS:
            return
        ok, jpeg = cv2.imencode(
            ".jpg", rotated_frame,
            [int(cv2.IMWRITE_JPEG_QUALITY), PREVIEW_JPEG_QUALITY],
        )
        if ok:
            with self._latest_lock:
                self._latest_jpeg = jpeg.tobytes()
                self._latest_ts = now

    def _pump_loop(self):
        # Keeps preview frames flowing while no clip is being recorded.
        # Steps aside (sleeps) the moment a record loop takes over so the
        # two never compete for frames.
        while not self._pump_stop.is_set():
            if self._recording.is_set():
                time.sleep(0.05)
                continue
            try:
                frames = self._pipeline.wait_for_frames()
            except Exception:
                if self._pump_stop.is_set():
                    break
                time.sleep(0.1)
                continue
            color_frame = frames.get_color_frame()
            if color_frame:
                self._update_latest(
                    apply_rotation(np.asanyarray(color_frame.get_data()))
                )

    def start(self):
        self._pipeline = rs.pipeline()
        config = rs.config()
        config.enable_stream(rs.stream.color, WIDTH, HEIGHT, rs.format.bgr8, FPS)
        profile = self._pipeline.start(config)

        device = profile.get_device()
        print("[camera] device: %s (S/N %s, USB %s)" % (
            device.get_info(rs.camera_info.name),
            device.get_info(rs.camera_info.serial_number),
            device.get_info(rs.camera_info.usb_type_descriptor),
        ))

        # Constant frame rate: adjust gain instead of exposure time in dim
        # light, otherwise the sensor silently drops below the nominal FPS
        # and every clip plays back too fast.
        color_sensor = None
        for sensor in device.query_sensors():
            if sensor.is_color_sensor():
                color_sensor = sensor
                break
        if color_sensor is not None and color_sensor.supports(rs.option.auto_exposure_priority):
            color_sensor.set_option(rs.option.auto_exposure_priority, 0.0)
            print("[camera] auto_exposure_priority disabled (constant fps)")
        else:
            print("[camera] warning: auto_exposure_priority not available")

        print("[camera] warming up (%d frames)..." % WARMUP_FRAMES)
        for _ in range(WARMUP_FRAMES):
            self._pipeline.wait_for_frames()
        print("[camera] ready (%dx%d @ %d fps)" % (WIDTH, HEIGHT, FPS))

        self._pump_stop.clear()
        self._pump = threading.Thread(target=self._pump_loop, name="preview-pump")
        self._pump.daemon = True
        self._pump.start()

    def record_clip(self, output_path, duration_sec):
        """Record one clip; returns a dict with frame count and effective fps."""
        if self._pipeline is None:
            raise RuntimeError("camera not started — call start() first")

        writer = cv2.VideoWriter(
            output_path, cv2.VideoWriter_fourcc(*"mp4v"), FPS, output_size()
        )
        if not writer.isOpened():
            raise RuntimeError("failed to open VideoWriter for %s" % output_path)

        frames_written = 0
        self._recording.set()
        start = time.time()
        try:
            while time.time() - start < duration_sec:
                frames = self._pipeline.wait_for_frames()
                color_frame = frames.get_color_frame()
                if not color_frame:
                    continue
                rotated = apply_rotation(np.asanyarray(color_frame.get_data()))
                writer.write(rotated)
                frames_written += 1
                self._update_latest(rotated)
        finally:
            self._recording.clear()
            writer.release()
        elapsed = time.time() - start

        effective_fps = frames_written / elapsed if elapsed > 0 else 0.0
        if abs(effective_fps - FPS) / FPS > FPS_DRIFT_WARN:
            print("[camera] WARNING: effective fps %.1f deviates from nominal %d "
                  "— clip timing unreliable (improve lighting)" % (effective_fps, FPS))
        return {
            "frames": frames_written,
            "elapsed_sec": round(elapsed, 3),
            "effective_fps": round(effective_fps, 2),
            "nominal_fps": FPS,
            "size_mb": round(os.path.getsize(output_path) / 1e6, 2),
        }

    def record_until(self, output_path, stop_event, max_sec, min_sec=0.0):
        """Record until `stop_event` is set (or `max_sec` elapses as a safety
        cap), but never shorter than `min_sec`. Used to trim clips to the real
        gesture length: the caller sets the event once Pepper reports the
        animation is done. Returns the same stats dict as record_clip()."""
        if self._pipeline is None:
            raise RuntimeError("camera not started — call start() first")

        writer = cv2.VideoWriter(
            output_path, cv2.VideoWriter_fourcc(*"mp4v"), FPS, output_size()
        )
        if not writer.isOpened():
            raise RuntimeError("failed to open VideoWriter for %s" % output_path)

        frames_written = 0
        self._recording.set()
        start = time.time()
        try:
            while True:
                elapsed = time.time() - start
                if elapsed >= max_sec:
                    break
                if stop_event.is_set() and elapsed >= min_sec:
                    break
                try:
                    frames = self._pipeline.wait_for_frames()
                except RuntimeError:
                    # Pipeline was stopped under us (Ctrl+C teardown while a
                    # clip was open) — end the clip instead of crashing the
                    # worker thread.
                    print("[camera] pipeline stopped mid-clip — closing clip early")
                    break
                color_frame = frames.get_color_frame()
                if not color_frame:
                    continue
                rotated = apply_rotation(np.asanyarray(color_frame.get_data()))
                writer.write(rotated)
                frames_written += 1
                self._update_latest(rotated)
        finally:
            self._recording.clear()
            writer.release()
        elapsed = time.time() - start

        effective_fps = frames_written / elapsed if elapsed > 0 else 0.0
        if abs(effective_fps - FPS) / FPS > FPS_DRIFT_WARN:
            print("[camera] WARNING: effective fps %.1f deviates from nominal %d "
                  "— clip timing unreliable (improve lighting)" % (effective_fps, FPS))
        return {
            "frames": frames_written,
            "elapsed_sec": round(elapsed, 3),
            "effective_fps": round(effective_fps, 2),
            "nominal_fps": FPS,
            "size_mb": round(os.path.getsize(output_path) / 1e6, 2),
        }

    def stop(self):
        self._pump_stop.set()
        if self._pipeline is not None:
            self._pipeline.stop()
            self._pipeline = None
        if self._pump is not None:
            self._pump.join(2.0)
            self._pump = None
        print("[camera] stopped")
