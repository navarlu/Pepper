#!/usr/bin/env python2
# -*- coding: utf-8 -*-

from __future__ import print_function

from collections import deque
import socket
import struct
import sys
import audioop
import time
import threading
import json
import qi
import urllib
import urlparse
try:
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
except Exception:
    from http.server import BaseHTTPRequestHandler, HTTPServer
try:
    from Queue import Queue, Empty, Full
except Exception:
    from queue import Queue, Empty, Full

from config import (
    ALLOWED_STREAM_RATES,
    ANIMATIONS_FILE,
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
PEPPER_CHUNK_LIMIT = int(PEPPER_CHUNK_LIMIT_FRAMES)  # max frames for sendRemoteBufferToOutput
DEFAULT_OUTPUT_VOLUME = int(PEPPER_OUTPUT_VOLUME)
PLAYBACK_BATCH_FRAMES = int(PEPPER_PLAYBACK_BATCH_FRAMES)
MAX_BUFFER_FRAMES = int(PEPPER_MAX_BUFFER_FRAMES)
TABLET_DEBUG_MIN_INTERVAL = float(TABLET_DEBUG_MIN_INTERVAL_AUDIO)

try:
    text_type = unicode  # noqa: F821 (py2)
except NameError:
    text_type = str


def to_text(x):
    try:
        if isinstance(x, bytes):
            return x.decode("utf-8", "ignore")
    except Exception:
        pass
    try:
        return text_type(x)
    except Exception:
        return str(x)


def connect_session(qi_url):
    s = qi.Session()
    s.connect(qi_url)
    return s


def wait_for_service(session, service_name, timeout_sec=90.0, retry_sec=1.0):
    deadline = time.time() + float(timeout_sec)
    last_err = None
    while time.time() < deadline:
        try:
            svc = session.service(service_name)
            print("[bridge] service ready:", service_name)
            return svc
        except Exception as exc:
            last_err = exc
            print(
                "[bridge] waiting for service '{}'... {}".format(
                    service_name, to_text(exc)
                )
            )
            time.sleep(retry_sec)
    raise RuntimeError(
        "Timed out waiting for service '{}': {}".format(service_name, to_text(last_err))
    )


def load_animations_map(path):
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


def resolve_animation_name(name, animations_map, installed):
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


class TabletDebugReporter(object):
    def __init__(self, enabled, tablet):
        self.enabled = enabled and (tablet is not None)
        self._tablet = tablet
        self._queue = Queue(maxsize=int(TABLET_REPORTER_QUEUE_SIZE))
        self._stop = threading.Event()
        self._worker = None
        self._last_sent = 0.0

    def start(self):
        if self._tablet is None or self._worker is not None:
            return
        self._worker = threading.Thread(target=self._run, name="tablet-debug-audio")
        self._worker.daemon = True
        self._worker.start()

    def stop(self):
        if not self.enabled:
            return
        self._stop.set()
        if self._worker is not None:
            self._worker.join(1.0)
            self._worker = None

    def publish(self, title, body="", force=False):
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
    
    def publish_payload(self, payload, force=False):
        if self._tablet is None:
            return
        now = time.time()
        if (not force) and (now - self._last_sent) < TABLET_DEBUG_MIN_INTERVAL:
            return
        self._last_sent = now
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

    def _post(self, payload):
        def _esc(u):
            return (
                u.replace(u"&", u"&amp;")
                .replace(u"<", u"&lt;")
                .replace(u">", u"&gt;")
            )

        ui_mode = to_text(payload.get("ui", u""))
        if ui_mode == "split_chat_debug":
            user_text = to_text(payload.get("user_text", u""))
            pepper_text = to_text(payload.get("pepper_text", u""))
            debug_lines = payload.get("debug_lines", []) or []
            if not isinstance(debug_lines, list):
                debug_lines = [to_text(debug_lines)]
            life_state = to_text(payload.get("life_state", u"unknown"))
            active_animation = to_text(payload.get("active_animation", u""))
            life_abilities = payload.get("life_abilities", {}) or {}
            if not isinstance(life_abilities, dict):
                life_abilities = {}
            abilities_line = u", ".join(
                u"{}={}".format(
                    to_text(k),
                    u"on" if bool(v) else u"off",
                )
                for k, v in life_abilities.items()
            )
            if active_animation:
                status_line = u"Life: {} | Anim: {}".format(_esc(life_state), _esc(active_animation))
            else:
                status_line = u"Life: {}".format(_esc(life_state))

            debug_html = u"".join(
                u"<div class='dbg-line'>{}</div>".format(_esc(to_text(line)))
                for line in debug_lines[-int(TABLET_DEBUG_MAX_LINES):]
            )
            html = TABLET_SPLIT_CHAT_HTML_TEMPLATE.format(
                status_line=status_line,
                abilities_line=_esc(abilities_line),
                debug_html=debug_html or u"<div class='dbg-line'>waiting for events...</div>",
                user_text=_esc(user_text or u"..."),
                pepper_text=_esc(pepper_text or u"..."),
            )
            data_url = "data:text/html;charset=utf-8," + urllib.quote(
                html.encode("utf-8")
            )
            self._tablet.showWebview(data_url)
            return

        text = to_text(payload.get("text", u""))
        fg = to_text(payload.get("fg", u"#FFFFFF"))
        bg = to_text(payload.get("bg", u"#000000"))
        align = to_text(payload.get("align", u"center"))
        size = int(payload.get("size", 56))

        html = TABLET_INLINE_HTML_TEMPLATE.format(
            bg=bg, fg=fg, size=size, align=align, txt=_esc(text)
        )
        data_url = "data:text/html;charset=utf-8," + urllib.quote(
            html.encode("utf-8")
        )
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
    def __init__(
        self,
        bridge_url,
        tablet_reporter,
        behavior_manager,
        animation_player,
        life_service,
        animations_map,
    ):
        super(TabletOverlayHttpServer, self).__init__()
        self.daemon = True
        self._tablet = tablet_reporter
        self._bm = behavior_manager
        self._anim = animation_player
        self._life = life_service
        self._animations_map = animations_map
        self._server = None
        parsed = urlparse.urlparse(bridge_url or "")
        host = parsed.hostname or "127.0.0.1"
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
                err = getattr(exc, "errno", None)
                return err in (32, 104)  # EPIPE, ECONNRESET

            def _write_json(self, status_code, payload):
                try:
                    body = json.dumps(payload)
                    self.send_response(status_code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return True
                except socket.error as exc:
                    if self._is_disconnect_error(exc):
                        return False
                    return False
                except Exception:
                    return False

            def handle_one_request(self):
                try:
                    BaseHTTPRequestHandler.handle_one_request(self)
                except socket.error as exc:
                    # Python2 can raise from internal flush() on client timeout/disconnect.
                    if self._is_disconnect_error(exc):
                        return
                    raise

            def finish(self):
                try:
                    BaseHTTPRequestHandler.finish(self)
                except socket.error as exc:
                    # Client disconnected before response flush; ignore noisy traceback.
                    if self._is_disconnect_error(exc):
                        return
                    raise

            def do_POST(self):
                path_only = self.path.split("?", 1)[0]
                if path_only.startswith("/animation/"):
                    if bm is None and anim is None:
                        self._write_json(
                            500,
                            {
                                "ok": False,
                                "error": "No animation service available (ALAnimationPlayer/ALBehaviorManager)",
                            },
                        )
                        return
                    raw_name = path_only[len("/animation/"):]
                    name = urllib.unquote(raw_name)
                    try:
                        installed = bm.getInstalledBehaviors()
                    except Exception as exc:
                        self._write_json(500, {"ok": False, "error": to_text(exc)})
                        return
                    behavior = resolve_animation_name(name, animations_map, installed)
                    if not behavior:
                        self._write_json(
                            404,
                            {"ok": False, "error": "unknown animation", "name": name},
                        )
                        return
                    try:
                        # Publish state transition hint for the tablet debug panel.
                        tablet.publish_payload(
                            {
                                "ui": "split_chat_debug",
                                "debug_lines": [
                                    "animation: starting {}".format(behavior),
                                ],
                                "life_state": to_text(life.getState()) if life is not None else "unknown",
                                "active_animation": behavior,
                            },
                            force=True,
                        )
                        if life is not None and TOUCH_AUTONOMOUS_LIFE:
                            try:
                                state = to_text(life.getState())
                                print("[life] state before animation:", state)
                                if state.lower() == "disabled":
                                    print("[life] state is disabled, switching to solitary")
                                    life.setState("solitary")
                            except Exception as life_exc:
                                print("[life] warning:", to_text(life_exc))

                        print("[animation] running:", behavior)
                        # Prefer ALAnimationPlayer to keep AutonomousLife behavior model intact.
                        if anim is not None and behavior.startswith("animations/"):
                            fut = anim.run(behavior)
                            # Wait for completion to keep API semantics of "play now".
                            try:
                                fut.value()
                            except Exception:
                                pass
                        else:
                            bm.runBehavior(behavior)
                        self._write_json(
                            200,
                            {"ok": True, "name": name, "behavior": behavior},
                        )
                        tablet.publish_payload(
                            {
                                "ui": "split_chat_debug",
                                "debug_lines": [
                                    "animation: finished {}".format(behavior),
                                ],
                                "life_state": to_text(life.getState()) if life is not None else "unknown",
                                "active_animation": "",
                            },
                            force=True,
                        )
                    except Exception as exc:
                        self._write_json(500, {"ok": False, "error": to_text(exc), "behavior": behavior})
                    return

                if self.path != "/tablet/text_inline":
                    self.send_response(404)
                    self.end_headers()
                    return
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length > 0 else "{}"
                if BRIDGE_LOG_TABLET_HTTP:
                    print("[tablet_http] POST /tablet/text_inline bytes=%s" % length)
                try:
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        payload = {"text": to_text(payload)}
                    preview = to_text(payload.get("text", u""))[:160]
                    if not preview:
                        preview = (
                            "ui=%s user=%s pepper=%s"
                            % (
                                to_text(payload.get("ui", u"")),
                                to_text(payload.get("user_text", u""))[:60],
                                to_text(payload.get("pepper_text", u""))[:60],
                            )
                        )
                    if BRIDGE_LOG_TABLET_HTTP:
                        print("[tablet_http] payload %s" % preview)
                    payload.setdefault("size", int(TABLET_DEFAULT_SIZE))
                    payload.setdefault("bg", TABLET_DEFAULT_BG)
                    payload.setdefault("fg", TABLET_DEFAULT_FG)
                    payload.setdefault("align", TABLET_DEFAULT_ALIGN)
                    if life is not None:
                        try:
                            payload.setdefault("life_state", to_text(life.getState()))
                            payload.setdefault(
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
                    tablet.publish_payload(payload, force=True)
                    self._write_json(200, {"ok": True})
                except Exception as exc:
                    print("[tablet_http] ERROR %s" % to_text(exc))
                    self._write_json(
                        500,
                        {"ok": False, "error": to_text(exc).replace('"', "'")},
                    )

        self._server = HTTPServer(self._bind, Handler)
        print("[tablet_http] listening on http://%s:%s" % self._bind)
        self._server.serve_forever()

    def stop(self):
        if self._server is not None:
            try:
                self._server.shutdown()
            except Exception:
                pass


def recv_all(conn, size):
    """Receive exactly size bytes from conn or None on EOF."""
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
    """
    raw_mono: bytes, int16 mono.
    Return bytes, int16 stereo interleaved (L,R,L,R,...).
    """
    # Use C-optimized conversion to avoid Python-loop jitter.
    return audioop.tostereo(raw_mono, 2, 1, 1)

def main():
    qi_url = PEPPER_QI_URL or DEFAULT_QI_URL
    print("[pepper_audio] Python version:", sys.version)
    print("[pepper_audio] Connecting to Pepper:", qi_url)

    sess = connect_session(qi_url)
    try:
        audio = wait_for_service(sess, "ALAudioDevice", timeout_sec=BRIDGE_AUDIO_SERVICE_TIMEOUT_SEC)
    except Exception as exc:
        print("[bridge] FATAL:", to_text(exc))
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
            current_state = to_text(life_service.getState())
            print("[life] current state:", current_state)
            if TOUCH_AUTONOMOUS_LIFE and current_state.lower() == "disabled":
                print("[life] enabling autonomous life -> solitary")
                life_service.setState("solitary")
        except Exception as life_exc:
            print("[life] warning:", to_text(life_exc))
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
                print("[life] ability enable warning", ability, to_text(ability_exc))
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
        BRIDGE_URL,
        tablet,
        behavior_manager,
        animation_player,
        life_service,
        animations_map,
    )
    tablet_http.start()
    tablet.publish(
        "Pepper audio server starting",
        "qi={}\nrate={}".format(qi_url, TARGET_RATE),
        force=True,
    )

    try:
        audio.openAudioOutputs()
    except Exception as e:
        print("[pepper_audio] openAudioOutputs warning:", to_text(e))

    try:
        audio.setParameter("outputSampleRate", TARGET_RATE)
        print("[pepper_audio] set outputSampleRate to", TARGET_RATE)
    except Exception as e:
        print("[pepper_audio] setParameter warning:", to_text(e))

    try:
        audio.setOutputVolume(DEFAULT_OUTPUT_VOLUME)
        current_volume = audio.getOutputVolume()
        print("[pepper_audio] output volume set to", current_volume)
    except Exception as e:
        print("[pepper_audio] setOutputVolume warning:", to_text(e))

    if PLAYBACK_BATCH_FRAMES > PEPPER_CHUNK_LIMIT:
        print(
            "[pepper_audio] PLAYBACK_BATCH_FRAMES too high, clamping to",
            PEPPER_CHUNK_LIMIT,
        )
    batch_frames = min(PLAYBACK_BATCH_FRAMES, PEPPER_CHUNK_LIMIT)
    batch_bytes = batch_frames * 4  # int16 stereo => 4 bytes per frame
    max_buffer_frames = max(MAX_BUFFER_FRAMES, batch_frames)
    max_buffer_bytes = max_buffer_frames * 4
    send_warn_threshold_ms = (float(batch_frames) / float(TARGET_RATE)) * 2000.0
    print(
        "[pepper_audio] buffering:",
        "batch_frames=", batch_frames,
        "max_buffer_frames=", max_buffer_frames,
    )

    # TCP server: receive mono 48kHz PCM from Python 3 process
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((TCP_HOST, TCP_PORT))
    server.listen(1)
    print("[pepper_audio] Waiting for Python3 client on %s:%d..." % (TCP_HOST, TCP_PORT))
    tablet.publish(
        "Pepper audio ready",
        "waiting bridge on {}:{}".format(TCP_HOST, TCP_PORT),
        force=True,
    )

    conn, addr = server.accept()
    print("[pepper_audio] Client connected:", addr)
    tablet.publish(
        "Bridge client connected",
        "from={}{}".format(addr[0], ":" + str(addr[1]) if len(addr) > 1 else ""),
        force=True,
    )
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
    stereo_queue = deque()

    try:
        while True:
            # Read 4-byte length header
            header = recv_all(conn, 4)
            if not header:
                print("[pepper_audio] client disconnected (no header)")
                break

            size = struct.unpack(">I", header)[0]

            # Sanity check
            if size <= 0 or size > 2 ** 20:
                print("[pepper_audio] invalid size:", size)
                break

            # Read 'size' bytes of mono PCM
            chunk = recv_all(conn, size)
            if not chunk:
                print("[pepper_audio] client disconnected (no chunk)")
                break
            now_ts = time.time()
            recv_interval_ms = (now_ts - last_chunk_ts) * 1000.0
            last_chunk_ts = now_ts

            # mono int16 -> stereo int16 interleaved
            stereo = mono16_to_stereo16(chunk)

            nb_frames = len(stereo) // 4  # 2 channels * 2 bytes
            if nb_frames > PEPPER_CHUNK_LIMIT:
                stereo = stereo[:PEPPER_CHUNK_LIMIT * 4]
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

                payload = "".join(parts)
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
                        "[pepper_audio] First playback batch sent:",
                        "batch_frames=", batch_frames,
                        "recv_interval_ms=", round(recv_interval_ms, 2),
                        "send_duration_ms=", round(send_duration_ms, 2),
                    )
                if send_duration_ms > send_warn_threshold_ms:
                    print(
                        "[pepper_audio] WARNING slow sendRemoteBufferToOutput:",
                        "send_duration_ms=", round(send_duration_ms, 2),
                        "batch_frames=", batch_frames,
                    )

            if recv_chunks_total == 1:
                print(
                    "[pepper_audio] First chunk received:",
                    "bytes=", len(chunk),
                    "frames=", nb_frames,
                    "recv_interval_ms=", round(recv_interval_ms, 2),
                )
                tablet.publish(
                    "Audio stream active",
                    "first_chunk bytes={}\nframes={}".format(len(chunk), nb_frames),
                    force=True,
                )
            elif recv_chunks_total % 200 == 0:
                avg_recv_interval_ms = recv_intervals_ms_sum / float(recv_chunks_total)
                avg_send_duration_ms = (
                    send_durations_ms_sum / float(send_calls_total)
                    if send_calls_total
                    else 0.0
                )
                print(
                    "[pepper_audio] stream heartbeat:",
                    "recv_chunks=", recv_chunks_total,
                    "send_calls=", send_calls_total,
                    "frames_total=", frames_sent_total,
                    "buffered_frames=", queued_bytes // 4,
                    "dropped_frames_total=", dropped_frames_total,
                    "avg_recv_interval_ms=", round(avg_recv_interval_ms, 2),
                    "avg_send_duration_ms=", round(avg_send_duration_ms, 2),
                    "max_recv_interval_ms=", round(max_recv_interval_ms, 2),
                    "max_send_duration_ms=", round(max_send_duration_ms, 2),
                )
                tablet.publish(
                    "Audio heartbeat",
                    "recv={}\nsent_frames={}\nbuffered={}".format(
                        recv_chunks_total,
                        frames_sent_total,
                        queued_bytes // 4,
                    ),
                )

            # No explicit sleep: recv_all() already blocks on incoming real-time chunks.
            # Additional sleeps can cause underruns/overruns and "freeze then catch-up".
    finally:
        conn.close()
        server.close()
        tablet_http.stop()
        tablet.publish("Pepper audio server stopped", force=True)
        tablet.stop()
        print("[pepper_audio] server shut down")


if __name__ == "__main__":
    main()
