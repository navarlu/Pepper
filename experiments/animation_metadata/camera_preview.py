"""Live RealSense preview in the browser.

Two ways to use it:

  1. Standalone (camera positioning, exclusive camera access):
         uv run python experiments/animation_metadata/camera_preview.py
     Stop with Ctrl+C before starting the recording rig.

  2. During recording: record_animations.py imports start_preview_server()
     and serves the RealSenseRecorder's own frames — one camera owner, so
     preview and recording run together.

Frames are rotated exactly like the recordings (ROTATE in
realsense_camera.py); the standalone mode draws an optional framing grid.
"""

import socket
import threading
import time

import cv2
import numpy as np
import pyrealsense2 as rs

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from realsense_camera import WIDTH, HEIGHT, FPS, ROTATE, apply_rotation

# --- Configuration ---------------------------------------------------------
PORT = 8089
JPEG_QUALITY = 80
DRAW_GRID = True            # standalone mode: rule-of-thirds + center cross
STREAM_FPS = 15             # browser stream rate (capture stays at FPS)
# ---------------------------------------------------------------------------

_INDEX_HTML = (
    b"<!doctype html><html><head><title>RealSense preview</title>"
    b"<style>body{margin:0;background:#111;display:flex;"
    b"justify-content:center;align-items:center;height:100vh}"
    b"img{max-width:100vw;max-height:100vh}</style></head>"
    b"<body><img src='/stream'></body></html>"
)


def _local_ip():
    try:
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        probe.connect(("8.8.8.8", 80))
        ip = probe.getsockname()[0]
        probe.close()
        return ip
    except Exception:
        return "<rpi-ip>"


def start_preview_server(get_jpeg, port=PORT, stream_fps=STREAM_FPS):
    """Start a daemon MJPEG server streaming whatever `get_jpeg()` returns
    (JPEG bytes or None). Returns the server object (call .shutdown() to
    stop it, or just let the process exit — threads are daemonic)."""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            return

        def do_GET(self):
            if self.path == "/":
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(_INDEX_HTML)))
                self.end_headers()
                self.wfile.write(_INDEX_HTML)
                return
            if self.path != "/stream":
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header(
                "Content-Type", "multipart/x-mixed-replace; boundary=frame"
            )
            self.end_headers()
            try:
                while True:
                    jpeg = get_jpeg()
                    if jpeg is not None:
                        self.wfile.write(b"--frame\r\n")
                        self.wfile.write(b"Content-Type: image/jpeg\r\n")
                        self.wfile.write(
                            b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                        )
                        self.wfile.write(jpeg)
                        self.wfile.write(b"\r\n")
                    time.sleep(1.0 / stream_fps)
            except (BrokenPipeError, ConnectionResetError):
                pass  # browser tab closed

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    worker = threading.Thread(target=server.serve_forever, name="preview-http")
    worker.daemon = True
    worker.start()
    print("[preview] live view: http://%s:%d" % (_local_ip(), port))
    return server


# --- Standalone mode ---------------------------------------------------------

_latest_jpeg = None
_latest_lock = threading.Lock()
_stop = threading.Event()


def _draw_grid(frame):
    h, w = frame.shape[:2]
    color = (0, 255, 0)
    for fx in (w // 3, 2 * w // 3):
        cv2.line(frame, (fx, 0), (fx, h), color, 1)
    for fy in (h // 3, 2 * h // 3):
        cv2.line(frame, (0, fy), (w, fy), color, 1)
    cv2.drawMarker(frame, (w // 2, h // 2), color, cv2.MARKER_CROSS, 40, 1)
    return frame


def _get_latest():
    with _latest_lock:
        return _latest_jpeg


def _capture_loop():
    global _latest_jpeg
    pipeline = rs.pipeline()
    config = rs.config()
    config.enable_stream(rs.stream.color, WIDTH, HEIGHT, rs.format.bgr8, FPS)
    pipeline.start(config)
    print("[preview] camera streaming at %dx%d @ %d fps (rotation: %s)"
          % (WIDTH, HEIGHT, FPS, ROTATE or "none"))
    try:
        while not _stop.is_set():
            frames = pipeline.wait_for_frames()
            color_frame = frames.get_color_frame()
            if not color_frame:
                continue
            frame = apply_rotation(np.asanyarray(color_frame.get_data()))
            if DRAW_GRID:
                frame = _draw_grid(frame)
            ok, jpeg = cv2.imencode(
                ".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), JPEG_QUALITY]
            )
            if ok:
                with _latest_lock:
                    _latest_jpeg = jpeg.tobytes()
    finally:
        pipeline.stop()
        print("[preview] camera stopped")


def main():
    capture = threading.Thread(target=_capture_loop, daemon=True)
    capture.start()
    server = start_preview_server(_get_latest)
    print("[preview] Ctrl+C to stop (required before recording!)")
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        pass
    finally:
        _stop.set()
        server.shutdown()
        capture.join(3.0)
        print("[preview] stopped")


if __name__ == "__main__":
    main()
