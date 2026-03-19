#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Python 3.12 port of bridge.py.
Connects to Pepper via qi (pip install qi==3.1.5) and serves a TCP audio
server + HTTP control API identical to the Python 2 bridge.
"""

from collections import deque
import json
import socket
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from queue import Empty, Full, Queue
from urllib.parse import urlparse, quote, unquote

import qi

from config import (
    ALLOWED_STREAM_RATES,
    ANIMATIONS_FILE,
    BRIDGE_BIND_HOST,
    BRIDGE_AUDIO_SERVICE_TIMEOUT_SEC,
    BRIDGE_LOG_TABLET_HTTP,
    BRIDGE_OPTIONAL_SERVICE_TIMEOUT_SEC,
    BRIDGE_URL,
    LIFE_AUTONOMOUS_BLINKING,
    LIFE_BACKGROUND_MOVEMENT,
    LIFE_BASIC_AWARENESS,
    LIFE_LISTENING_MOVEMENT,
    LIFE_SPEAKING_MOVEMENT,
    PEPPER_CHUNK_LIMIT_FRAMES,
    PEPPER_MAX_BUFFER_FRAMES,
    PEPPER_OUTPUT_VOLUME,
    PEPPER_PLAYBACK_BATCH_FRAMES,
    PEPPER_QI_URL,
    PEPPER_STREAM_RATE,
    TABLET_DEFAULT_ALIGN,
    TABLET_DEFAULT_BG,
    TABLET_DEFAULT_FG,
    TABLET_DEFAULT_SIZE,
    TABLET_DEBUG_AUDIO_ENABLED,
    TABLET_DEBUG_MAX_LINES,
    TABLET_DEBUG_MIN_INTERVAL_AUDIO,
    TABLET_INLINE_HTML_TEMPLATE,
    TABLET_REPORTER_QUEUE_SIZE,
    TABLET_SPLIT_CHAT_HTML_TEMPLATE,
    TOUCH_AUTONOMOUS_LIFE,
    TCP_HOST,
    TCP_PORT,
)


def _resolve_stream_rate():
    raw = int(PEPPER_STREAM_RATE)
    if raw not in ALLOWED_STREAM_RATES:
        print("[pepper_audio] Unsupported PEPPER_STREAM_RATE=", raw, "fallback to 16000")
        return 16000
    return raw


TARGET_RATE = _resolve_stream_rate()
DEFAULT_QI_URL = PEPPER_QI_URL
PEPPER_CHUNK_LIMIT = int(PEPPER_CHUNK_LIMIT_FRAMES)
DEFAULT_OUTPUT_VOLUME = int(PEPPER_OUTPUT_VOLUME)
PLAYBACK_BATCH_FRAMES = int(PEPPER_PLAYBACK_BATCH_FRAMES)
MAX_BUFFER_FRAMES = int(PEPPER_MAX_BUFFER_FRAMES)
TABLET_DEBUG_MIN_INTERVAL = float(TABLET_DEBUG_MIN_INTERVAL_AUDIO)


def mono16_to_stereo16(raw_mono: bytes) -> bytes:
    """Convert mono int16 PCM to stereo interleaved int16 PCM (L,R,L,R,...)."""
    n = len(raw_mono) // 2
    samples = struct.unpack_from(f"{n}h", raw_mono)
    return struct.pack(f"{n * 2}h", *(v for s in samples for v in (s, s)))


def connect_session(qi_url: str) -> qi.Session:
    s = qi.Session()
    s.connect(qi_url)
    return s


def wait_for_service(session, service_name, timeout_sec=90.0, retry_sec=1.0):
    deadline = time.time() + float(timeout_sec)
    last_err = None
    while time.time() < deadline:
        try:
            svc = session.service(service_name)
            print(f"[bridge] service ready: {service_name}")
            return svc
        except Exception as exc:
            last_err = exc
            print(f"[bridge] waiting for service '{service_name}'... {exc}")
            time.sleep(retry_sec)
    raise RuntimeError(f"Timed out waiting for service '{service_name}': {last_err}")


def load_animations_map(path: str) -> dict:
    try:
        with open(path, "r") as f:
            data = json.load(f) or {}
        normalized = {k.strip(): v.strip() for k, v in data.items() if k.strip() and v.strip()}
        print(f"[bridge] loaded animations: {len(normalized)} from {path}")
        return normalized
    except Exception as exc:
        print(f"[bridge] failed to load animations map: {exc}")
        return {}


def resolve_animation_name(name: str, animations_map: dict, installed: list):
    key = str(name).strip()
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


class TabletDebugReporter:
    def __init__(self, enabled: bool, tablet):
        self.enabled = enabled and (tablet is not None)
        self._tablet = tablet
        self._queue: Queue = Queue(maxsize=int(TABLET_REPORTER_QUEUE_SIZE))
        self._stop = threading.Event()
        self._worker = None
        self._last_sent = 0.0

    def start(self):
        if self._tablet is None or self._worker is not None:
            return
        self._worker = threading.Thread(target=self._run, name="tablet-debug-audio", daemon=True)
        self._worker.start()

    def stop(self):
        if not self.enabled:
            return
        self._stop.set()
        if self._worker is not None:
            self._worker.join(1.0)
            self._worker = None

    def publish(self, title: str, body: str = "", force: bool = False):
        if not self.enabled:
            return
        now = time.time()
        if (not force) and (now - self._last_sent) < TABLET_DEBUG_MIN_INTERVAL:
            return
        self._last_sent = now
        text = title.strip()
        if body.strip():
            text = text + "\n" + body.strip()
        payload = {
            "text": text,
            "size": int(TABLET_DEFAULT_SIZE),
            "bg": TABLET_DEFAULT_BG,
            "fg": TABLET_DEFAULT_FG,
            "align": TABLET_DEFAULT_ALIGN,
        }
        self._enqueue(payload)

    def publish_payload(self, payload: dict, force: bool = False):
        if self._tablet is None:
            return
        now = time.time()
        if (not force) and (now - self._last_sent) < TABLET_DEBUG_MIN_INTERVAL:
            return
        self._last_sent = now
        self._enqueue(payload)

    def _enqueue(self, payload: dict):
        try:
            self._queue.put_nowait(payload)
        except Full:
            try:
                self._queue.get_nowait()
            except Empty:
                pass
            try:
                self._queue.put_nowait(payload)
            except Full:
                pass

    def _post(self, payload: dict):
        def _esc(u: str) -> str:
            return u.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        ui_mode = str(payload.get("ui", ""))
        if ui_mode == "split_chat_debug":
            user_text = str(payload.get("user_text", ""))
            pepper_text = str(payload.get("pepper_text", ""))
            debug_lines = payload.get("debug_lines", []) or []
            if not isinstance(debug_lines, list):
                debug_lines = [str(debug_lines)]
            life_state = str(payload.get("life_state", "unknown"))
            active_animation = str(payload.get("active_animation", ""))
            session_state = str(payload.get("session_state", ""))
            idle_countdown = str(payload.get("idle_countdown", ""))
            life_abilities = payload.get("life_abilities", {}) or {}
            if not isinstance(life_abilities, dict):
                life_abilities = {}
            abilities_line = ", ".join(
                f"{k}={'on' if bool(v) else 'off'}" for k, v in life_abilities.items()
            )
            if active_animation:
                status_line = f"Life: {_esc(life_state)} | Anim: {_esc(active_animation)}"
            else:
                status_line = f"Life: {_esc(life_state)}"
            if session_state:
                status_line += f" | Session: {_esc(session_state)}"
            if idle_countdown:
                status_line += f" | Idle: {_esc(idle_countdown)}"

            debug_html = "".join(
                f"<div class='dbg-line'>{_esc(str(line))}</div>"
                for line in debug_lines[-int(TABLET_DEBUG_MAX_LINES):]
            )
            html = TABLET_SPLIT_CHAT_HTML_TEMPLATE.format(
                status_line=status_line,
                abilities_line=_esc(abilities_line),
                debug_html=debug_html or "<div class='dbg-line'>waiting for events...</div>",
                user_text=_esc(user_text or "..."),
                pepper_text=_esc(pepper_text or "..."),
            )
            data_url = "data:text/html;charset=utf-8," + quote(html.encode("utf-8"))
            self._tablet.showWebview(data_url)
            return

        text = str(payload.get("text", ""))
        fg = str(payload.get("fg", "#FFFFFF"))
        bg = str(payload.get("bg", "#000000"))
        align = str(payload.get("align", "center"))
        size = int(payload.get("size", 56))
        html = TABLET_INLINE_HTML_TEMPLATE.format(
            bg=bg, fg=fg, size=size, align=align, txt=_esc(text)
        )
        data_url = "data:text/html;charset=utf-8," + quote(html.encode("utf-8"))
        self._tablet.showWebview(data_url)

    def _run(self):
        while not self._stop.is_set():
            try:
                payload = self._queue.get(True, 0.2)
            except Empty:
                continue
            try:
                self._post(payload)
            except Exception:
                pass


class TabletOverlayHttpServer(threading.Thread):
    def __init__(self, bridge_url, tablet_reporter, behavior_manager, animation_player, life_service, animations_map):
        super().__init__(daemon=True)
        self._tablet = tablet_reporter
        self._bm = behavior_manager
        self._anim = animation_player
        self._life = life_service
        self._animations_map = animations_map
        self._server = None
        parsed = urlparse(bridge_url or "")
        host = parsed.hostname or BRIDGE_BIND_HOST or "127.0.0.1"
        if host in ("127.0.0.1", "localhost") and BRIDGE_BIND_HOST == "0.0.0.0":
            host = BRIDGE_BIND_HOST
        port = parsed.port or 5000
        self._bind = (host, port)

    def run(self):
        tablet = self._tablet
        bm = self._bm
        anim = self._anim
        life = self._life
        animations_map = self._animations_map

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def _is_disconnect_error(self, exc):
                return getattr(exc, "errno", None) in (32, 104)  # EPIPE, ECONNRESET

            def _write_json(self, status_code, payload):
                try:
                    body = json.dumps(payload).encode()
                    self.send_response(status_code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return True
                except (socket.error, Exception):
                    return False

            def handle_one_request(self):
                try:
                    BaseHTTPRequestHandler.handle_one_request(self)
                except socket.error as exc:
                    if not self._is_disconnect_error(exc):
                        raise

            def finish(self):
                try:
                    BaseHTTPRequestHandler.finish(self)
                except socket.error as exc:
                    if not self._is_disconnect_error(exc):
                        raise

            def do_POST(self):
                path_only = self.path.split("?", 1)[0]
                if path_only.startswith("/animation/"):
                    if bm is None and anim is None:
                        self._write_json(500, {"ok": False, "error": "No animation service available"})
                        return
                    raw_name = path_only[len("/animation/"):]
                    name = unquote(raw_name)
                    try:
                        installed = bm.getInstalledBehaviors()
                    except Exception as exc:
                        self._write_json(500, {"ok": False, "error": str(exc)})
                        return
                    behavior = resolve_animation_name(name, animations_map, installed)
                    if not behavior:
                        self._write_json(404, {"ok": False, "error": "unknown animation", "name": name})
                        return
                    try:
                        tablet.publish_payload(
                            {
                                "ui": "split_chat_debug",
                                "debug_lines": [f"animation: starting {behavior}"],
                                "life_state": str(life.getState()) if life is not None else "unknown",
                                "active_animation": behavior,
                            },
                            force=True,
                        )
                        if life is not None and TOUCH_AUTONOMOUS_LIFE:
                            try:
                                state = str(life.getState())
                                print(f"[life] state before animation: {state}")
                                if state.lower() == "disabled":
                                    print("[life] state is disabled, switching to solitary")
                                    life.setState("solitary")
                            except Exception as life_exc:
                                print(f"[life] warning: {life_exc}")

                        print(f"[animation] running: {behavior}")
                        if anim is not None and behavior.startswith("animations/"):
                            fut = anim.run(behavior)
                            try:
                                fut.value()
                            except Exception:
                                pass
                        else:
                            bm.runBehavior(behavior)
                        self._write_json(200, {"ok": True, "name": name, "behavior": behavior})
                        tablet.publish_payload(
                            {
                                "ui": "split_chat_debug",
                                "debug_lines": [f"animation: finished {behavior}"],
                                "life_state": str(life.getState()) if life is not None else "unknown",
                                "active_animation": "",
                            },
                            force=True,
                        )
                    except Exception as exc:
                        self._write_json(500, {"ok": False, "error": str(exc), "behavior": behavior})
                    return

                if self.path != "/tablet/text_inline":
                    self.send_response(404)
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length > 0 else b"{}"
                if BRIDGE_LOG_TABLET_HTTP:
                    print(f"[tablet_http] POST /tablet/text_inline bytes={length}")
                try:
                    payload_data = json.loads(raw)
                    if not isinstance(payload_data, dict):
                        payload_data = {"text": str(payload_data)}
                    preview = str(payload_data.get("text", ""))[:160]
                    if not preview:
                        preview = (
                            "ui={} user={} pepper={}".format(
                                payload_data.get("ui", ""),
                                str(payload_data.get("user_text", ""))[:60],
                                str(payload_data.get("pepper_text", ""))[:60],
                            )
                        )
                    if BRIDGE_LOG_TABLET_HTTP:
                        print(f"[tablet_http] payload {preview}")
                    payload_data.setdefault("size", int(TABLET_DEFAULT_SIZE))
                    payload_data.setdefault("bg", TABLET_DEFAULT_BG)
                    payload_data.setdefault("fg", TABLET_DEFAULT_FG)
                    payload_data.setdefault("align", TABLET_DEFAULT_ALIGN)
                    if life is not None:
                        try:
                            payload_data.setdefault("life_state", str(life.getState()))
                            payload_data.setdefault(
                                "life_abilities",
                                {
                                    "AutonomousBlinking": bool(life.getAutonomousAbilityEnabled("AutonomousBlinking")),
                                    "BackgroundMovement": bool(life.getAutonomousAbilityEnabled("BackgroundMovement")),
                                    "BasicAwareness": bool(life.getAutonomousAbilityEnabled("BasicAwareness")),
                                    "ListeningMovement": bool(life.getAutonomousAbilityEnabled("ListeningMovement")),
                                    "SpeakingMovement": bool(life.getAutonomousAbilityEnabled("SpeakingMovement")),
                                },
                            )
                        except Exception:
                            pass
                    tablet.publish_payload(payload_data, force=True)
                    self._write_json(200, {"ok": True})
                except Exception as exc:
                    print(f"[tablet_http] ERROR {exc}")
                    self._write_json(500, {"ok": False, "error": str(exc).replace('"', "'")})

            def do_GET(self):
                path_only = self.path.split("?", 1)[0]
                if path_only != "/health":
                    self.send_response(404)
                    self.end_headers()
                    return
                self._write_json(
                    200,
                    {
                        "ok": True,
                        "service": "bridge",
                        "audio_bind_host": BRIDGE_BIND_HOST,
                        "audio_port": TCP_PORT,
                    },
                )

        self._server = HTTPServer(self._bind, Handler)
        print(f"[tablet_http] listening on http://{self._bind[0]}:{self._bind[1]}")
        self._server.serve_forever()

    def stop(self):
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass


def recv_all(conn, size: int):
    """Receive exactly size bytes or return None on EOF."""
    chunks = []
    remaining = size
    while remaining > 0:
        chunk = conn.recv(remaining)
        if not chunk:
            return None
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


CONTROL_FRAME_FLUSH = 0
CONTROL_FRAME_PING = 4294967295


def main():
    qi_url = PEPPER_QI_URL or DEFAULT_QI_URL
    print(f"[pepper_audio] Python version: {sys.version}")
    print(f"[pepper_audio] Connecting to Pepper: {qi_url}")

    sess = connect_session(qi_url)
    try:
        audio = wait_for_service(sess, "ALAudioDevice", timeout_sec=BRIDGE_AUDIO_SERVICE_TIMEOUT_SEC)
    except Exception as exc:
        print(f"[bridge] FATAL: {exc}")
        return

    behavior_manager = None
    try:
        behavior_manager = wait_for_service(sess, "ALBehaviorManager", timeout_sec=BRIDGE_OPTIONAL_SERVICE_TIMEOUT_SEC)
    except Exception:
        behavior_manager = None

    animation_player = None
    try:
        animation_player = wait_for_service(sess, "ALAnimationPlayer", timeout_sec=BRIDGE_OPTIONAL_SERVICE_TIMEOUT_SEC)
    except Exception:
        animation_player = None

    life_service = None
    try:
        life_service = wait_for_service(sess, "ALAutonomousLife", timeout_sec=BRIDGE_OPTIONAL_SERVICE_TIMEOUT_SEC)
        try:
            current_state = str(life_service.getState())
            print(f"[life] current state: {current_state}")
            if TOUCH_AUTONOMOUS_LIFE and current_state.lower() == "disabled":
                print("[life] enabling autonomous life -> solitary")
                life_service.setState("solitary")
        except Exception as life_exc:
            print(f"[life] warning: {life_exc}")
    except Exception:
        life_service = None

    if life_service is not None and TOUCH_AUTONOMOUS_LIFE:
        ability_profile = {
            "AutonomousBlinking": LIFE_AUTONOMOUS_BLINKING,
            "BackgroundMovement": LIFE_BACKGROUND_MOVEMENT,
            "BasicAwareness": LIFE_BASIC_AWARENESS,
            "ListeningMovement": LIFE_LISTENING_MOVEMENT,
            "SpeakingMovement": LIFE_SPEAKING_MOVEMENT,
        }
        for ability, enabled in ability_profile.items():
            try:
                life_service.setAutonomousAbilityEnabled(ability, bool(enabled))
            except Exception as ability_exc:
                print(f"[life] ability enable warning {ability}: {ability_exc}")
    elif life_service is not None:
        print("[life] TOUCH_AUTONOMOUS_LIFE=False -> bridge will not modify life state/abilities")

    animations_map = load_animations_map(ANIMATIONS_FILE)
    tablet_service = None
    try:
        tablet_service = sess.service("ALTabletService")
    except Exception:
        tablet_service = None

    tablet = TabletDebugReporter(TABLET_DEBUG_AUDIO_ENABLED, tablet_service)
    tablet.start()
    tablet_http = TabletOverlayHttpServer(
        BRIDGE_URL, tablet, behavior_manager, animation_player, life_service, animations_map
    )
    tablet_http.start()
    tablet.publish("Pepper audio server starting", f"qi={qi_url}\nrate={TARGET_RATE}", force=True)

    try:
        audio.openAudioOutputs()
    except Exception as e:
        print(f"[pepper_audio] openAudioOutputs warning: {e}")

    try:
        audio.setParameter("outputSampleRate", TARGET_RATE)
        print(f"[pepper_audio] set outputSampleRate to {TARGET_RATE}")
    except Exception as e:
        print(f"[pepper_audio] setParameter warning: {e}")

    try:
        audio.setOutputVolume(DEFAULT_OUTPUT_VOLUME)
        current_volume = audio.getOutputVolume()
        print(f"[pepper_audio] output volume set to {current_volume}")
    except Exception as e:
        print(f"[pepper_audio] setOutputVolume warning: {e}")

    if PLAYBACK_BATCH_FRAMES > PEPPER_CHUNK_LIMIT:
        print(f"[pepper_audio] PLAYBACK_BATCH_FRAMES too high, clamping to {PEPPER_CHUNK_LIMIT}")
    batch_frames = min(PLAYBACK_BATCH_FRAMES, PEPPER_CHUNK_LIMIT)
    batch_bytes = batch_frames * 4  # int16 stereo => 4 bytes per frame
    max_buffer_frames = max(MAX_BUFFER_FRAMES, batch_frames)
    max_buffer_bytes = max_buffer_frames * 4
    send_warn_threshold_ms = (float(batch_frames) / float(TARGET_RATE)) * 2000.0
    print(
        f"[pepper_audio] buffering: batch_frames={batch_frames} max_buffer_frames={max_buffer_frames}"
    )

    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((BRIDGE_BIND_HOST, TCP_PORT))
    server.listen(1)
    print(f"[pepper_audio] Waiting for client on {BRIDGE_BIND_HOST}:{TCP_PORT}...")
    tablet.publish("Pepper audio ready", f"waiting bridge on {BRIDGE_BIND_HOST}:{TCP_PORT}", force=True)

    try:
        while True:
            conn, addr = server.accept()
            print(f"[pepper_audio] Client connected: {addr}")
            tablet.publish("Bridge client connected", f"from={addr[0]}:{addr[1]}", force=True)

            frames_sent_total = 0
            recv_chunks_total = 0
            send_calls_total = 0
            last_chunk_ts = time.time()
            recv_intervals_ms_sum = 0.0
            send_durations_ms_sum = 0.0
            max_recv_interval_ms = 0.0
            max_send_duration_ms = 0.0
            dropped_frames_total = 0
            queued_bytes = 0
            stereo_queue: deque = deque()

            try:
                while True:
                    header = recv_all(conn, 4)
                    if not header:
                        print("[pepper_audio] client disconnected (no header)")
                        break

                    size = struct.unpack(">I", header)[0]

                    if size == CONTROL_FRAME_FLUSH:
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

                    if size > 2**20:
                        print(f"[pepper_audio] invalid size: {size}")
                        break

                    chunk = recv_all(conn, size)
                    if not chunk:
                        print("[pepper_audio] client disconnected (no chunk)")
                        break

                    now_ts = time.time()
                    recv_interval_ms = (now_ts - last_chunk_ts) * 1000.0
                    last_chunk_ts = now_ts

                    stereo = mono16_to_stereo16(chunk)
                    nb_frames = len(stereo) // 4
                    if nb_frames > PEPPER_CHUNK_LIMIT:
                        stereo = stereo[: PEPPER_CHUNK_LIMIT * 4]
                        nb_frames = PEPPER_CHUNK_LIMIT

                    stereo_queue.append(stereo)
                    queued_bytes += len(stereo)
                    recv_chunks_total += 1
                    recv_intervals_ms_sum += recv_interval_ms
                    if recv_interval_ms > max_recv_interval_ms:
                        max_recv_interval_ms = recv_interval_ms

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
                            f"[pepper_audio] WARNING buffer overflow: "
                            f"dropped_frames={dropped_frames} "
                            f"dropped_frames_total={dropped_frames_total} "
                            f"buffered_frames={queued_bytes // 4}"
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
                        send_duration_ms = (time.time() - send_start_ts) * 1000.0
                        send_calls_total += 1
                        frames_sent_total += batch_frames
                        send_durations_ms_sum += send_duration_ms
                        if send_duration_ms > max_send_duration_ms:
                            max_send_duration_ms = send_duration_ms
                        if send_calls_total == 1:
                            print(
                                f"[pepper_audio] First playback batch sent: "
                                f"batch_frames={batch_frames} "
                                f"recv_interval_ms={recv_interval_ms:.2f} "
                                f"send_duration_ms={send_duration_ms:.2f}"
                            )
                        if send_duration_ms > send_warn_threshold_ms:
                            print(
                                f"[pepper_audio] WARNING slow sendRemoteBufferToOutput: "
                                f"send_duration_ms={send_duration_ms:.2f} "
                                f"batch_frames={batch_frames}"
                            )

                    if recv_chunks_total == 1:
                        print(
                            f"[pepper_audio] First chunk received: "
                            f"bytes={len(chunk)} frames={nb_frames} "
                            f"recv_interval_ms={recv_interval_ms:.2f}"
                        )
                        tablet.publish(
                            "Audio stream active",
                            f"first_chunk bytes={len(chunk)}\nframes={nb_frames}",
                            force=True,
                        )
                    elif recv_chunks_total % 200 == 0:
                        avg_recv_ms = recv_intervals_ms_sum / float(recv_chunks_total)
                        avg_send_ms = send_durations_ms_sum / float(send_calls_total) if send_calls_total else 0.0
                        print(
                            f"[pepper_audio] stream heartbeat: "
                            f"recv_chunks={recv_chunks_total} "
                            f"send_calls={send_calls_total} "
                            f"frames_total={frames_sent_total} "
                            f"buffered_frames={queued_bytes // 4} "
                            f"dropped_frames_total={dropped_frames_total} "
                            f"avg_recv_interval_ms={avg_recv_ms:.2f} "
                            f"avg_send_duration_ms={avg_send_ms:.2f} "
                            f"max_recv_interval_ms={max_recv_interval_ms:.2f} "
                            f"max_send_duration_ms={max_send_duration_ms:.2f}"
                        )
                        tablet.publish(
                            "Audio heartbeat",
                            f"recv={recv_chunks_total}\nsent_frames={frames_sent_total}\nbuffered={queued_bytes // 4}",
                        )
            finally:
                try:
                    conn.close()
                except Exception:
                    pass
                try:
                    audio.flushAudioOutputs()
                except Exception:
                    pass
                print("[pepper_audio] waiting for next client...")
                tablet.publish("Pepper audio ready", f"waiting bridge on {BRIDGE_BIND_HOST}:{TCP_PORT}", force=True)
    finally:
        server.close()
        tablet_http.stop()
        tablet.publish("Pepper audio server stopped", force=True)
        tablet.stop()
        print("[pepper_audio] server shut down")


if __name__ == "__main__":
    main()
