#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import print_function

from collections import deque
import os
import socket
import struct
import sys
import time
import threading
import json

from urllib.parse import quote, unquote, urlparse
try:
    from BaseHTTPServer import BaseHTTPRequestHandler, HTTPServer
except Exception:
    from http.server import BaseHTTPRequestHandler, HTTPServer
try:
    from Queue import Queue, Empty, Full
except Exception:
    from queue import Queue, Empty, Full

from config import (
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
    STATE_FILE,
    TABLET_DEFAULT_ALIGN,
    TABLET_DEFAULT_BG,
    TABLET_DEFAULT_FG,
    TABLET_DEFAULT_SIZE,
    TABLET_DEBUG_MAX_LINES,
    TABLET_DEBUG_MIN_INTERVAL_AUDIO,
    TABLET_CHAT_HISTORY_HTML_TEMPLATE,
    TABLET_INLINE_HTML_TEMPLATE,
    TABLET_REPORTER_QUEUE_SIZE,
    TABLET_SPLIT_CHAT_HTML_TEMPLATE,
    TOUCH_AUTONOMOUS_LIFE,
    TCP_PORT,
)
from utils import (
    CONTROL_FRAME_FLUSH,
    CONTROL_FRAME_PING,
    capture_camera_snapshot,
    connect_session,
    load_animations_map,
    mono16_to_stereo16,
    normalize_output_volume,
    recv_all,
    resolve_animation_name,
    to_text,
    wait_for_service,
)

AUTONOMOUS_LIFE_ABILITIES = (
    "AutonomousBlinking",
    "BackgroundMovement",
    "BasicAwareness",
    "ListeningMovement",
    "SpeakingMovement",
)


class TabletDebugReporter(object):
    """Background publisher that renders HTML payloads on Pepper's tablet.

    Producers call `publish(...)` or `publish_payload(...)`; a worker
    thread drains a bounded queue and calls `ALTabletService.showWebview`
    with a `data:text/html` URL built from one of the templates in
    `config.py` (inline text, split-chat debug view, chat history).
    Rate-limited via `TABLET_DEBUG_MIN_INTERVAL_AUDIO` to avoid spamming the
    tablet during audio streaming. `enabled=False` disables publishing
    entirely — used in `main()` so `tablet_server.py` owns the screen.
    """

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
        """Enqueue a simple centered-text screen (title + optional body).

        Drops old entries to make room if the queue is full. Respects
        `TABLET_DEBUG_MIN_INTERVAL_AUDIO` unless `force=True`.
        """
        if not self.enabled:
            return
        now = time.time()
        if (not force) and (now - self._last_sent) < TABLET_DEBUG_MIN_INTERVAL_AUDIO:
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
        """Enqueue a raw payload dict — used by the `/tablet/text_inline`
        HTTP route so callers can drive the split-chat and chat-history
        views directly. The payload `ui` key selects the template
        rendered by `_post`.
        """
        if not self.enabled or self._tablet is None:
            return
        now = time.time()
        if (not force) and (now - self._last_sent) < TABLET_DEBUG_MIN_INTERVAL_AUDIO:
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
            session_state = to_text(payload.get("session_state", u""))
            idle_countdown = to_text(payload.get("idle_countdown", u""))
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
            if session_state:
                status_line += u" | Session: {}".format(_esc(session_state))
            if idle_countdown:
                status_line += u" | Idle: {}".format(_esc(idle_countdown))

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
            data_url = "data:text/html;charset=utf-8," + quote(
                html.encode("utf-8")
            )
            self._tablet.showWebview(data_url)
            return

        if ui_mode == "chat_history":
            items = payload.get("transcript_items") or []
            if not isinstance(items, list):
                items = []
            session_state = to_text(payload.get("session_state", u"idle"))
            pill_cls = "active" if session_state == "active" else (
                "warm" if session_state == "warm" else "idle"
            )
            session_pill = u'<div class="pill {cls}">{state}</div>'.format(
                cls=pill_cls, state=_esc(session_state.capitalize()),
            )
            if not items:
                bubbles_html = u'<div class="empty">No conversation yet.</div>'
            else:
                parts = []
                for item in items:
                    kind = to_text(item.get("kind", u"message") if isinstance(item, dict) else u"message")
                    if kind == "session":
                        text_val = to_text(item.get("text", u"Session update") if isinstance(item, dict) else u"")
                        parts.append(
                            u'<div class="bubble system"><div class="session-divider">'
                            u'<div class="session-chip">{t}</div>'
                            u'</div></div>'.format(t=_esc(text_val))
                        )
                        continue
                    speaker = to_text(item.get("speaker", u"") if isinstance(item, dict) else u"")
                    text_val = to_text(item.get("text", u"") if isinstance(item, dict) else u"")
                    is_pepper = speaker == "Pepper" or kind == "tool"
                    bubble_cls = "pepper" if is_pepper else "user"
                    tool_cls = " tool-bubble" if kind == "tool" else ""
                    parts.append(
                        u'<div class="bubble {bc}{tc}">'
                        u'<div class="speaker">{sp}</div>'
                        u'<div class="body-text">{tx}</div>'
                        u'</div>'.format(
                            bc=bubble_cls, tc=tool_cls,
                            sp=_esc(speaker), tx=_esc(text_val),
                        )
                    )
                bubbles_html = u"".join(parts)
            html = TABLET_CHAT_HISTORY_HTML_TEMPLATE.format(
                session_pill=session_pill,
                bubbles_html=bubbles_html,
            )
            data_url = "data:text/html;charset=utf-8," + quote(
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
        data_url = "data:text/html;charset=utf-8," + quote(
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


class LedEffectManager(object):
    """Background thread that continuously asserts the preferred eye-LED
    state so the NAOqi mood painter (active during interactive life state)
    can't hold the eyes pink.

    Modes:
      - "idle"         solid white, refreshed at ~3 Hz
      - "search_pulse" dim-blue <-> bright-blue loop (~0.5 s each)
      - "off"          LEDs off, refreshed at ~1 Hz
    """

    GROUP = "FaceLeds"
    IDLE_COLOR = 0x00FFFFFF
    PULSE_DIM = 0x00001133
    PULSE_BRIGHT = 0x0040A0FF
    IDLE_FADE = 0.2
    IDLE_TICK = 0.3
    PULSE_FADE = 0.5
    OFF_TICK = 1.0
    VALID_MODES = ("idle", "search_pulse", "off")

    def __init__(self, leds):
        self._leds = leds
        self._mode = "idle"
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._worker = None

    def start(self):
        if self._leds is None or self._worker is not None:
            return
        self._worker = threading.Thread(target=self._run, name="led-effects")
        self._worker.daemon = True
        self._worker.start()
        print("[leds] effect manager started mode={}".format(self._mode))

    def stop(self):
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=2.0)
            self._worker = None
        if self._leds is not None:
            try:
                self._leds.reset(self.GROUP)
            except Exception as exc:
                print("[leds] reset on stop warning:", to_text(exc))

    def set_mode(self, mode):
        mode = to_text(mode).strip().lower()
        if mode not in self.VALID_MODES:
            raise ValueError("unknown mode: {} (valid: {})".format(mode, self.VALID_MODES))
        with self._lock:
            prev = self._mode
            self._mode = mode
        if prev != mode:
            print("[leds] mode {} -> {}".format(prev, mode))

    def get_mode(self):
        with self._lock:
            return self._mode

    # region: led_worker_loop
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
    # endregion


class TabletOverlayHttpServer(threading.Thread):
    """HTTP front door for every non-audio action the bridge exposes.

    Runs in its own thread so the TCP audio loop in `main()` never
    blocks on HTTP. Routes:
      - GET  /health                 — liveness probe
      - POST /animation/<name>       — resolve + run a NAOqi behavior
                                       asynchronously (acks 200 immediately)
      - POST /tablet/text_inline     — render a tablet screen via
                                       `TabletDebugReporter`
      - POST /tablet/url             — navigate the tablet to an arbitrary
                                       URL (used by `tablet_server.py`)
      - POST /leds/state             — set the eye-LED mode on the
                                       `LedEffectManager`
      - GET  /life/state             — read autonomous-life state and
                                       ability flags
      - POST /audio/volume           — adjust `ALAudioDevice` output volume
      - POST /camera/snapshot        — capture one JPEG from the top
                                       camera (optionally pausing awareness)

    All service proxies are optional — the server degrades cleanly
    (503 / 500) if a service was not available at startup.
    """

    def __init__(
        self,
        bridge_url,
        tablet_reporter,
        behavior_manager,
        animation_player,
        life_service,
        animations_map,
        audio_player=None,
        tts=None,
        audio_device=None,
        tablet_service=None,
        led_manager=None,
        video_device=None,
        awareness=None,
    ):
        super(TabletOverlayHttpServer, self).__init__()
        self.daemon = True
        self._tablet = tablet_reporter
        self._bm = behavior_manager
        self._anim = animation_player
        self._life = life_service
        self._animations_map = animations_map
        self._audio_player = audio_player
        self._tts = tts
        self._audio_device = audio_device
        self._tablet_service = tablet_service
        self._led_manager = led_manager
        self._video_device = video_device
        self._awareness = awareness
        self._server = None
        parsed = urlparse(bridge_url or "")
        host = parsed.hostname or BRIDGE_BIND_HOST or "127.0.0.1"
        if host in ("127.0.0.1", "localhost") and BRIDGE_BIND_HOST == "0.0.0.0":
            host = BRIDGE_BIND_HOST
        port = parsed.port or 5000
        self._bind = (host, port)

    def run(self):
        server_self = self
        camera_lock = threading.Lock()

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def _is_disconnect_error(self, exc):
                err = getattr(exc, "errno", None)
                return err in (32, 104)  # EPIPE, ECONNRESET

            def _write_json(self, status_code, payload):
                try:
                    body = json.dumps(payload).encode("utf-8")
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

            def _write_bytes(self, status_code, content_type, body):
                try:
                    self.send_response(status_code)
                    self.send_header("Content-Type", content_type)
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                    return True
                except socket.error as exc:
                    if self._is_disconnect_error(exc):
                        return False
                    return False
                except Exception:
                    return False

            def do_POST(self):
                path_only = self.path.split("?", 1)[0]
                tablet = server_self._tablet
                bm = server_self._bm
                anim = server_self._anim
                life = server_self._life
                animations_map = server_self._animations_map
                audio_player = server_self._audio_player
                tts = server_self._tts
                audio_device = server_self._audio_device
                tablet_service = server_self._tablet_service
                led_manager = server_self._led_manager
                video_device = server_self._video_device
                awareness = server_self._awareness
                if path_only == "/camera/snapshot":
                    if video_device is None:
                        self._write_json(503, {"ok": False, "error": "video device unavailable"})
                        return
                    length = int(self.headers.get("Content-Length", "0") or "0")
                    raw = self.rfile.read(length) if length > 0 else "{}"
                    try:
                        payload = json.loads(raw) if raw else {}
                        if not isinstance(payload, dict):
                            payload = {}
                    except Exception:
                        payload = {}
                    pause_awareness = bool(payload.get("pause_awareness", True))
                    t0 = time.time()
                    # Serialize camera access — ALVideoDevice ring buffer gets
                    # unhappy with concurrent subscribeCamera calls from the
                    # same client name.
                    acquired = camera_lock.acquire(False)
                    if not acquired:
                        self._write_json(429, {"ok": False, "error": "camera busy"})
                        return
                    try:
                        jpeg = capture_camera_snapshot(
                            video_device,
                            awareness=awareness if pause_awareness else None,
                            pause_awareness=pause_awareness,
                        )
                        took_ms = int((time.time() - t0) * 1000)
                        print("[camera] snapshot ok bytes=%d took=%dms pause=%s"
                              % (len(jpeg), took_ms, pause_awareness))
                        self._write_bytes(200, "image/jpeg", jpeg)
                    except Exception as exc:
                        print("[camera] snapshot failed:", to_text(exc))
                        self._write_json(500, {"ok": False, "error": to_text(exc)})
                    finally:
                        camera_lock.release()
                    return

                if path_only == "/leds/state":
                    if led_manager is None:
                        self._write_json(503, {"ok": False, "error": "led manager unavailable"})
                        return
                    length = int(self.headers.get("Content-Length", "0") or "0")
                    raw = self.rfile.read(length) if length > 0 else "{}"
                    try:
                        payload = json.loads(raw)
                        mode = to_text(payload.get("mode", u"")).strip()
                    except Exception:
                        self._write_json(400, {"ok": False, "error": "invalid json"})
                        return
                    if not mode:
                        self._write_json(400, {"ok": False, "error": "missing mode"})
                        return
                    try:
                        led_manager.set_mode(mode)
                        self._write_json(200, {"ok": True, "mode": led_manager.get_mode()})
                    except ValueError as exc:
                        self._write_json(400, {"ok": False, "error": to_text(exc)})
                    except Exception as exc:
                        self._write_json(500, {"ok": False, "error": to_text(exc)})
                    return

                if path_only == "/audio/volume":
                    if audio_device is None:
                        self._write_json(500, {"ok": False, "error": "audio device unavailable"})
                        return
                    length = int(self.headers.get("Content-Length", "0") or "0")
                    raw = self.rfile.read(length) if length > 0 else "{}"
                    try:
                        payload = json.loads(raw)
                    except Exception:
                        self._write_json(400, {"ok": False, "error": "invalid json"})
                        return
                    # Accept either absolute `volume` (0..100) or relative
                    # `delta` (e.g. +20 / -20). Delta lets remote callers step
                    # the volume without first reading the current value —
                    # avoids the "agent on woska / state.json on rpi" split
                    # that bit us with the file-write tool design.
                    try:
                        previous_volume = int(audio_device.getOutputVolume())
                    except Exception as exc:
                        self._write_json(500, {"ok": False, "error": to_text(exc)})
                        return
                    if payload.get("delta") is not None:
                        try:
                            delta = int(payload.get("delta"))
                        except Exception:
                            self._write_json(400, {"ok": False, "error": "delta must be an integer"})
                            return
                        requested_volume = normalize_output_volume(previous_volume + delta)
                    elif "volume" in payload:
                        try:
                            requested_volume = normalize_output_volume(payload.get("volume"))
                        except Exception:
                            self._write_json(400, {"ok": False, "error": "volume must be an integer 0-100"})
                            return
                    else:
                        self._write_json(400, {"ok": False, "error": "missing volume or delta"})
                        return
                    try:
                        audio_device.setOutputVolume(requested_volume)
                        current_volume = int(audio_device.getOutputVolume())
                        clamped = current_volume != requested_volume or (
                            payload.get("delta") is not None
                            and current_volume != previous_volume + int(payload.get("delta"))
                        )
                        print("[bridge] output volume {} -> {}".format(previous_volume, current_volume))
                        _write_runtime_state({"pepper_output_volume": current_volume})
                        self._write_json(
                            200,
                            {
                                "ok": True,
                                "previous": previous_volume,
                                "volume": current_volume,
                                "clamped": clamped,
                                "persisted": True,
                            },
                        )
                    except Exception as exc:
                        self._write_json(500, {"ok": False, "error": to_text(exc)})
                    return
                if path_only.startswith("/animation/"):
                    # Validate quickly, then dispatch the actual playback in a
                    # background thread and ack 200 immediately. This keeps the
                    # agent's HTTP call latency in the millisecond range so the
                    # WebRTC heartbeat between woska and the LiveKit server is
                    # never starved by Pepper's animation runtime.
                    # See CONNECTION_ISSUE.md for the full story.
                    if bm is None and anim is None:
                        self._write_json(
                            202,
                            {
                                "ok": True,
                                "queued": False,
                                "status": "pepper_unavailable",
                                "message": "Animation service unavailable; Pepper is probably offline.",
                            },
                        )
                        return
                    raw_name = path_only[len("/animation/"):]
                    name = unquote(raw_name)
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

                    # region: animation_background
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
                    # endregion

                    worker = threading.Thread(
                        target=_run_animation_bg,
                        args=(behavior, name),
                    )
                    worker.daemon = True
                    worker.start()
                    print("[animation] queued behavior=%s name=%s" % (behavior, name))
                    self._write_json(
                        200,
                        {"ok": True, "name": name, "behavior": behavior, "queued": True},
                    )
                    return

                if path_only == "/tablet/url":
                    if tablet_service is None:
                        self._write_json(503, {"ok": False, "error": "tablet service unavailable"})
                        return
                    length = int(self.headers.get("Content-Length", "0") or "0")
                    raw = self.rfile.read(length) if length > 0 else "{}"
                    try:
                        payload = json.loads(raw)
                        url = to_text(payload.get("url", u"")).strip()
                    except Exception:
                        self._write_json(400, {"ok": False, "error": "invalid json"})
                        return
                    if not url:
                        self._write_json(400, {"ok": False, "error": "missing url"})
                        return
                    try:
                        tablet_service.showWebview(url)
                        print("[bridge] tablet navigated via /tablet/url:", url)
                        self._write_json(200, {"ok": True, "url": url})
                    except Exception as exc:
                        self._write_json(500, {"ok": False, "error": to_text(exc)})
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
                                _collect_life_state(life).get("abilities", {}),
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

            def do_GET(self):
                path_only = self.path.split("?", 1)[0]
                if path_only == "/life/state":
                    life = server_self._life
                    if life is None:
                        self._write_json(503, {"ok": False, "error": "life service unavailable"})
                        return
                    self._write_json(200, _collect_life_state(life))
                    return
                if path_only != "/health":
                    self.send_response(404)
                    self.end_headers()
                    return
                pepper_connected = server_self._audio_device is not None
                self._write_json(
                    200,
                    {
                        "ok": True,
                        "service": "bridge",
                        "pepper_connected": pepper_connected,
                        "animation_available": (
                            server_self._bm is not None or server_self._anim is not None
                        ),
                        "audio_bind_host": BRIDGE_BIND_HOST,
                        "audio_port": TCP_PORT,
                    },
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

    def update_services(
        self,
        behavior_manager=None,
        animation_player=None,
        life_service=None,
        audio_player=None,
        tts=None,
        audio_device=None,
        tablet_service=None,
        led_manager=None,
        video_device=None,
        awareness=None,
    ):
        self._bm = behavior_manager
        self._anim = animation_player
        self._life = life_service
        self._audio_player = audio_player
        self._tts = tts
        self._audio_device = audio_device
        self._tablet_service = tablet_service
        self._led_manager = led_manager
        self._video_device = video_device
        self._awareness = awareness


def _read_runtime_state():
    try:
        with open(STATE_FILE, "r") as fh:
            state = json.load(fh) or {}
            return state if isinstance(state, dict) else {}
    except Exception as exc:
        print("[bridge] state read failed path={} err={}".format(STATE_FILE, to_text(exc)))
        return {}


def _safe_life_call(life, method_name):
    try:
        return getattr(life, method_name)()
    except Exception as exc:
        return {"error": to_text(exc)}


def _collect_life_state(life):
    abilities = {}
    errors = {}
    for ability in AUTONOMOUS_LIFE_ABILITIES:
        try:
            abilities[ability] = bool(life.getAutonomousAbilityEnabled(ability))
        except Exception as exc:
            abilities[ability] = None
            errors[ability] = to_text(exc)

    payload = {
        "ok": True,
        "state": _safe_life_call(life, "getState"),
        "focus": _safe_life_call(life, "getFocus"),
        "activity": _safe_life_call(life, "getActivity"),
        "abilities": abilities,
    }
    if errors:
        payload["errors"] = errors
    return payload


def _write_runtime_state(patch):
    try:
        state = _read_runtime_state()
        state.update(patch)
        state["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        parent = os.path.dirname(STATE_FILE)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(state, fh, indent=2)
        os.rename(tmp, STATE_FILE)
        try:
            os.chmod(STATE_FILE, 0o666)
        except OSError:
            pass
    except Exception as exc:
        print("[bridge] state write failed path={} err={}".format(STATE_FILE, to_text(exc)))


def _state_output_volume(default_volume):
    state = _read_runtime_state()
    if "pepper_output_volume" not in state:
        return normalize_output_volume(default_volume)
    try:
        return normalize_output_volume(state.get("pepper_output_volume"))
    except Exception as exc:
        print("[bridge] invalid pepper_output_volume in state.json err={}".format(to_text(exc)))
        return normalize_output_volume(default_volume)


class RuntimeVolumeWatcher(threading.Thread):
    """Applies services/data/state.json pepper_output_volume changes live."""

    def __init__(self, audio_device, initial_volume, poll_sec=1.0):
        super(RuntimeVolumeWatcher, self).__init__()
        self.daemon = True
        self._audio = audio_device
        self._volume = int(initial_volume)
        self._poll_sec = float(poll_sec)
        self._stop_event = threading.Event()
        self._last_mtime = None

    def stop(self):
        self._stop_event.set()

    def run(self):
        while not self._stop_event.wait(self._poll_sec):
            try:
                mtime = os.path.getmtime(STATE_FILE)
            except OSError:
                continue
            if self._last_mtime == mtime:
                continue
            self._last_mtime = mtime
            state = _read_runtime_state()
            if "pepper_output_volume" not in state:
                continue
            try:
                requested_volume = normalize_output_volume(state.get("pepper_output_volume"))
            except Exception as exc:
                print("[bridge] ignoring invalid pepper_output_volume err={}".format(to_text(exc)))
                continue
            if requested_volume == self._volume:
                continue
            try:
                self._audio.setOutputVolume(requested_volume)
                current_volume = int(self._audio.getOutputVolume())
                self._volume = current_volume
                print("[bridge] output volume applied from state.json:", current_volume)
            except Exception as exc:
                print("[bridge] failed applying state volume err={}".format(to_text(exc)))


def main():
    """Bridge entry point.

    Startup sequence:
      1. Connect to Pepper via `qi.Session` (blocks until reachable).
      2. Resolve NAOqi services — `ALAudioDevice` is required; the
         rest (`ALBehaviorManager`, `ALAnimationPlayer`,
         `ALAutonomousLife`, `ALAudioPlayer`, `ALTextToSpeech`,
         `ALLeds`, `ALVideoDevice`, `ALBasicAwareness`,
         `ALTabletService`) are best-effort.
      3. Apply the autonomous-life ability profile from config.
      4. Configure audio: open outputs, set output sample rate,
         set default volume, compute playback batch + buffer sizes.
      5. Start auxiliary threads: `LedEffectManager`,
         `TabletDebugReporter`, `TabletOverlayHttpServer` (HTTP).
      6. Bind the TCP audio socket on `BRIDGE_BIND_HOST:TCP_PORT` and
         loop forever, accepting one `audio-bridge` client at a time
         and streaming its mono PCM into Pepper's speakers.

    The outer loop never exits on client disconnect — it just waits
    for the next client.
    """
    qi_url = PEPPER_QI_URL
    print("[pepper_audio] Python version:", sys.version)
    print("[pepper_audio] Connecting to Pepper:", qi_url)

    animations_map = load_animations_map(ANIMATIONS_FILE)
    tablet = TabletDebugReporter(False, None)
    tablet_http = TabletOverlayHttpServer(
        BRIDGE_URL,
        tablet,
        None,
        None,
        None,
        animations_map,
    )
    tablet_http.start()
    print("[bridge] HTTP control plane started; waiting for Pepper services")

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

    audio_player_service = None
    try:
        audio_player_service = wait_for_service(sess, "ALAudioPlayer", timeout_sec=BRIDGE_OPTIONAL_SERVICE_TIMEOUT_SEC)
        print("[bridge] ALAudioPlayer available — animation sounds will be muted")
    except Exception:
        print("[bridge] ALAudioPlayer not available — animation sounds cannot be muted")
        audio_player_service = None

    tts_service = None
    try:
        tts_service = wait_for_service(sess, "ALTextToSpeech", timeout_sec=BRIDGE_OPTIONAL_SERVICE_TIMEOUT_SEC)
        print("[bridge] ALTextToSpeech available — will be muted during animations")
    except Exception:
        print("[bridge] ALTextToSpeech not available")
        tts_service = None

    leds_service = None
    try:
        leds_service = wait_for_service(sess, "ALLeds", timeout_sec=BRIDGE_OPTIONAL_SERVICE_TIMEOUT_SEC)
        print("[bridge] ALLeds available — LED effect manager will run")
    except Exception:
        print("[bridge] ALLeds not available — LED effects disabled")
        leds_service = None

    led_manager = LedEffectManager(leds_service) if leds_service is not None else None

    video_device = None
    try:
        video_device = wait_for_service(sess, "ALVideoDevice", timeout_sec=BRIDGE_OPTIONAL_SERVICE_TIMEOUT_SEC)
        print("[bridge] ALVideoDevice available — /camera/snapshot enabled")
    except Exception:
        print("[bridge] ALVideoDevice not available — /camera/snapshot disabled")
        video_device = None

    awareness_service = None
    try:
        awareness_service = wait_for_service(sess, "ALBasicAwareness", timeout_sec=BRIDGE_OPTIONAL_SERVICE_TIMEOUT_SEC)
        print("[bridge] ALBasicAwareness available — snapshots will pause tracking")
    except Exception:
        print("[bridge] ALBasicAwareness not available — snapshots will skip pauseAwareness")
        awareness_service = None

    tablet_service = None
    try:
        tablet_service = sess.service("ALTabletService")
    except Exception:
        tablet_service = None

    # The tablet-display service (services/src/tablet_server.py) owns the
    # tablet screen via POST /tablet/url. We silence TabletDebugReporter so
    # this bridge's legacy data-URL publishes don't fight over showWebview().
    tablet_http.update_services(
        behavior_manager=behavior_manager,
        animation_player=animation_player,
        life_service=life_service,
        audio_player=audio_player_service,
        tts=tts_service,
        audio_device=audio,
        tablet_service=tablet_service,
        led_manager=led_manager,
        video_device=video_device,
        awareness=awareness_service,
    )

    if led_manager is not None:
        led_manager.start()
    tablet.publish(
        "Pepper audio server starting",
        "qi={}\nrate={}".format(qi_url, PEPPER_STREAM_RATE),
        force=True,
    )

    try:
        audio.openAudioOutputs()
    except Exception as e:
        print("[pepper_audio] openAudioOutputs warning:", to_text(e))

    try:
        audio.setParameter("outputSampleRate", PEPPER_STREAM_RATE)
        print("[pepper_audio] set outputSampleRate to", PEPPER_STREAM_RATE)
    except Exception as e:
        print("[pepper_audio] setParameter warning:", to_text(e))

    runtime_volume_watcher = None
    try:
        startup_volume = _state_output_volume(PEPPER_OUTPUT_VOLUME)
        audio.setOutputVolume(startup_volume)
        current_volume = audio.getOutputVolume()
        print("[pepper_audio] output volume set to", current_volume)
        _write_runtime_state({"pepper_output_volume": int(current_volume)})
        runtime_volume_watcher = RuntimeVolumeWatcher(audio, int(current_volume))
        runtime_volume_watcher.start()
    except Exception as e:
        print("[pepper_audio] setOutputVolume warning:", to_text(e))

    if PEPPER_PLAYBACK_BATCH_FRAMES > PEPPER_CHUNK_LIMIT_FRAMES:
        print(
            "[pepper_audio] PEPPER_PLAYBACK_BATCH_FRAMES too high, clamping to",
            PEPPER_CHUNK_LIMIT_FRAMES,
        )
    batch_frames = min(PEPPER_PLAYBACK_BATCH_FRAMES, PEPPER_CHUNK_LIMIT_FRAMES)
    batch_bytes = batch_frames * 4  # int16 stereo => 4 bytes per frame
    max_buffer_frames = max(PEPPER_MAX_BUFFER_FRAMES, batch_frames)
    max_buffer_bytes = max_buffer_frames * 4
    send_warn_threshold_ms = (float(batch_frames) / float(PEPPER_STREAM_RATE)) * 2000.0
    print(
        "[pepper_audio] buffering:",
        "batch_frames=", batch_frames,
        "max_buffer_frames=", max_buffer_frames,
    )

    # TCP server: receive mono 48kHz PCM from Python 3 process
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind((BRIDGE_BIND_HOST, TCP_PORT))
    server.listen(1)
    print("[pepper_audio] Waiting for Python3 client on %s:%d..." % (BRIDGE_BIND_HOST, TCP_PORT))
    tablet.publish(
        "Pepper audio ready",
        "waiting bridge on {}:{}".format(BRIDGE_BIND_HOST, TCP_PORT),
        force=True,
    )

    try:
        while True:
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
                    # region: tcp_wire_decode
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
                    # endregion

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
                    if nb_frames > PEPPER_CHUNK_LIMIT_FRAMES:
                        stereo = stereo[:PEPPER_CHUNK_LIMIT_FRAMES * 4]
                        nb_frames = PEPPER_CHUNK_LIMIT_FRAMES

                    stereo_queue.append(stereo)
                    queued_bytes += len(stereo)
                    recv_chunks_total += 1
                    recv_intervals_ms_sum += recv_interval_ms
                    if recv_interval_ms > max_recv_interval_ms:
                        max_recv_interval_ms = recv_interval_ms

                    # region: tcp_playback_drain
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
                    # endregion
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
                try:
                    conn.close()
                except Exception:
                    pass
                try:
                    audio.flushAudioOutputs()
                except Exception:
                    pass
                print("[pepper_audio] waiting for next Python3 client...")
                tablet.publish(
                    "Pepper audio ready",
                    "waiting bridge on {}:{}".format(BRIDGE_BIND_HOST, TCP_PORT),
                    force=True,
                )
    finally:
        server.close()
        if runtime_volume_watcher is not None:
            runtime_volume_watcher.stop()
        if led_manager is not None:
            led_manager.stop()
        tablet_http.stop()
        tablet.publish("Pepper audio server stopped", force=True)
        tablet.stop()
        print("[pepper_audio] server shut down")


if __name__ == "__main__":
    main()
