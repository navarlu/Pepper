"""Shared RealSense recording helper for the animation-metadata experiment.

Keeps one pipeline open across many clips (restarting the pipeline costs
seconds per clip) and holds a constant frame rate by disabling auto-exposure
priority, so clip durations stay truthful for gesture-duration measurement.
"""

import os
import time

import cv2
import numpy as np
import pyrealsense2 as rs

WIDTH = 1280
HEIGHT = 720
FPS = 30
WARMUP_FRAMES = 30  # let auto-exposure settle after pipeline start
FPS_DRIFT_WARN = 0.05  # warn if effective fps deviates more than 5% from nominal


class RealSenseRecorder(object):
    def __init__(self):
        self._pipeline = None

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

    def record_clip(self, output_path, duration_sec):
        """Record one clip; returns a dict with frame count and effective fps."""
        if self._pipeline is None:
            raise RuntimeError("camera not started — call start() first")

        writer = cv2.VideoWriter(
            output_path, cv2.VideoWriter_fourcc(*"mp4v"), FPS, (WIDTH, HEIGHT)
        )
        if not writer.isOpened():
            raise RuntimeError("failed to open VideoWriter for %s" % output_path)

        frames_written = 0
        start = time.time()
        try:
            while time.time() - start < duration_sec:
                frames = self._pipeline.wait_for_frames()
                color_frame = frames.get_color_frame()
                if not color_frame:
                    continue
                writer.write(np.asanyarray(color_frame.get_data()))
                frames_written += 1
        finally:
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
        if self._pipeline is not None:
            self._pipeline.stop()
            self._pipeline = None
            print("[camera] stopped")
