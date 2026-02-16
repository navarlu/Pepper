# -*- coding: utf-8 -*-
'''This is bridge server for Pepper robot.
It exposes a REST API to control Pepper's behaviors, TTS, and tablet.
It uses python-qi to connect to Pepper's NAOqi services.
Starting the server:
pyenv activate naoqi27
export PYTHONPATH=/Projects/FEL/Pepper/choregraphe/lib/python2.7/site-packages:$PYTHONPATH
python /home/lucas/Projects/FEL/Pepper/src/bridge.py

the code is written to be compatible with both Python 2.7
'''
from __future__ import print_function, unicode_literals

import os
import time
import io
import json

import urllib

import requests
import qi
from flask import Flask, request, jsonify

from threading import Lock

from virtual_animations import VIRTUAL_EMOTIONS

# .env support (optional, safe if not installed)
try:
    from dotenv import load_dotenv  # type: ignore
except Exception:
    load_dotenv = None

# near the other imports
import io
import os
BASE_DIR = os.path.dirname(os.path.abspath(__file__))          
ROOT_DIR = os.path.abspath(os.path.join(BASE_DIR, os.pardir, os.pardir))
ENV_PATH = os.path.join(ROOT_DIR, ".env")

print("Loading .env from:", ENV_PATH)
def _load_env_file_fallback(path):
    """
    Minimal .env loader for environments where python-dotenv is not available.
    Supports simple KEY=VALUE lines and ignores comments/empty lines.
    """
    if not os.path.exists(path):
        return
    try:
        with io.open(path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except Exception as e:
        print("Warning: failed to load .env fallback:", str(e))

if load_dotenv is not None:
    load_dotenv(dotenv_path=ENV_PATH)
else:
    _load_env_file_fallback(ENV_PATH)

DEFAULT_QI_URL = os.environ.get("PEPPER_URL")
DEFAULT_VIRTUAL_QI_URL = (
    os.environ.get("VIRTUAL_PEPPER_URL")
    or os.environ.get("NAOQI_URL")
    or "tcp://127.0.0.1:41095"
)
ANIMATIONS_FILE = os.environ.get("ANIMATIONS_FILE")
print("Using DEFAULT_QI_URL =", DEFAULT_QI_URL)
print("Using DEFAULT_VIRTUAL_QI_URL =", DEFAULT_VIRTUAL_QI_URL)
print("Using ANIMATIONS_FILE =", ANIMATIONS_FILE)

AUTO_TABLET_ANNOUNCE = True
AUTO_TABLET_DURATION_MS = 2500   # how long to show the label (ms), 0 = keep until hidden
AUTO_TABLET_SIZE = 72            # font size (px)
AUTO_TABLET_BG = u"#000000"
AUTO_TABLET_FG = u"#FFFFFF"

EMOTION_BEHAVIORS = VIRTUAL_EMOTIONS

def _resolve_config_path(p):
    """
    Resolve a possibly-relative path from .env. Tries, in order:
    1) absolute path (as-is)
    2) relative to current working directory (where you run `python ...`)
    3) relative to this file's folder (Pepper/)
    4) relative to repo root (parent of Pepper/)
    Falls back to #4 if not found.
    """
    if not p:
        return os.path.join(BASE_DIR, "animations.json")
    if os.path.isabs(p):
        return p
    candidates = [
        os.path.abspath(os.path.join(os.getcwd(), p)),
        os.path.abspath(os.path.join(BASE_DIR, p)),
        os.path.abspath(os.path.join(ROOT_DIR, p)),
    ]
    for c in candidates:
        if os.path.exists(c):
            return c
    # default fallback (repo-root relative)
    return os.path.abspath(os.path.join(ROOT_DIR, p))
# key -> full behavior path (loaded from animations.json)
_animations = {}
_anim_lock = Lock()

app = Flask(__name__)

# ---- Helpers ----------------------------------------------------------------

try:
    text_type = unicode  # noqa: F821  (exists in py2)
except NameError:
    text_type = str

def to_text(x):
    """Return unicode text in py2 and str in py3."""
    try:
        if isinstance(x, bytes):
            return x.decode("utf-8", "ignore")
    except NameError:
        # In py3 'bytes' always exists; in py2 we might hit here for non-bytes
        pass
    try:
        return text_type(x)
    except Exception:
        return u"" + x  # last resort

def connect_session(qi_url):
    s = qi.Session()
    s.connect(qi_url)
    return s

def get_services(qi_url, include_tablet=True):
    s = connect_session(qi_url)
    services = {
        "session": s,
        "motion": s.service("ALMotion"),
        "posture": s.service("ALRobotPosture"),
        "bm": s.service("ALBehaviorManager"),
        # "leds": s.service("ALLeds"),
    }
    if include_tablet:
        try:
            services["tablet"] = s.service("ALTabletService")
        except Exception:
            services["tablet"] = None
    return services

def load_animations():
    """(Re)load key->behavior path mapping from animations.json."""
    global _animations
    try:
        resolved = _resolve_config_path(ANIMATIONS_FILE)
        with io.open(resolved, "r", encoding="utf-8") as f:
            data = json.load(f) or {}
        normalized = {}
        for k, v in data.items():
            key = (u"" + unicode(k)).strip() if not isinstance(k, unicode) else k.strip()
            if key:
                val = (u"" + unicode(v)).strip() if not isinstance(v, unicode) else v.strip()
                normalized[key] = val
        with _anim_lock:
            _animations = normalized
        print("[INFO] Loaded animations from: {}".format(resolved))
        return True, None
    except Exception as e:
        return False, unicode(e)
    
def resolve_animation_key(name, installed=None):
    """
    Resolve user-supplied name to a full behavior path:
    1) Exact key in animations.json
    2) If 'name' looks like a full path (contains '/'), use as-is
    3) Best-effort suffix match against installed behaviors
    """
    key = to_text(name).strip()
    if not key:
        return None, "Empty animation name"

    # 1) exact key in JSON
    with _anim_lock:
        if key in _animations:
            return _animations[key], None

    # 2) treat as full path if it contains '/'
    if "/" in key:
        return key, None

    # 3) suffix match against installed behaviors
    if installed:
        suffix = "/" + key
        candidates = [b for b in installed if b.endswith(suffix)]
        if len(candidates) == 1:
            return candidates[0], None
        elif len(candidates) > 1:
            # prefer official 'animations/' namespace
            prio = [c for c in candidates if c.startswith("animations/")]
            chosen = prio[0] if prio else candidates[0]
            return chosen, None

    return None, "Unknown animation key '{}' (not in animations.json and no suffix match found)".format(key)

# ---- Routes -----------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    return jsonify({"ok": True}), 200

@app.route("/wake", methods=["POST", "GET"])
def wake():
    qi_url = request.args.get("url", DEFAULT_QI_URL)
    try:
        svcs = get_services(qi_url)
        svcs["motion"].wakeUp()
        time.sleep(0.3)
        return jsonify({"ok": True, "action": "wakeUp", "url": qi_url}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": to_text(e), "url": qi_url}), 500

@app.route("/rest", methods=["POST"])
def rest():
    qi_url = request.args.get("url", DEFAULT_QI_URL)
    try:
        svcs = get_services(qi_url)
        svcs["motion"].rest()
        return jsonify({"ok": True, "action": "rest", "url": qi_url}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": to_text(e), "url": qi_url}), 500

# ---- Virtual Robot Routes ---------------------------------------------------

@app.route("/virtual_health", methods=["GET"])
def virtual_health():
    return jsonify({"ok": True, "virtual": True}), 200


@app.route("/virtual_wake", methods=["POST", "GET"])
def virtual_wake():
    qi_url = request.args.get("url", DEFAULT_VIRTUAL_QI_URL)
    try:
        svcs = get_services(qi_url, include_tablet=False)
        svcs["motion"].wakeUp()
        time.sleep(0.3)
        return jsonify({"ok": True, "action": "wakeUp", "url": qi_url, "virtual": True}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": to_text(e), "url": qi_url, "virtual": True}), 500


@app.route("/virtual_rest", methods=["POST"])
def virtual_rest():
    qi_url = request.args.get("url", DEFAULT_VIRTUAL_QI_URL)
    try:
        svcs = get_services(qi_url, include_tablet=False)
        svcs["motion"].rest()
        return jsonify({"ok": True, "action": "rest", "url": qi_url, "virtual": True}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": to_text(e), "url": qi_url, "virtual": True}), 500


@app.route("/virtual_behaviors", methods=["GET"])
def virtual_behaviors():
    qi_url = request.args.get("url", DEFAULT_VIRTUAL_QI_URL)
    try:
        svcs = get_services(qi_url, include_tablet=False)
        bm = svcs["bm"]
        installed = sorted(bm.getInstalledBehaviors())
        running = sorted(bm.getRunningBehaviors())
        tags = sorted(bm.getTagList()) if hasattr(bm, "getTagList") else []
        by_tag = {}
        for tag in tags:
            try:
                tagged = bm.getBehaviorsByTag(tag)
                if tagged:
                    by_tag[tag] = tagged
            except Exception:
                pass
        return jsonify({
            "ok": True,
            "url": qi_url,
            "virtual": True,
            "installed": installed,
            "running": running,
            "tags": tags,
            "by_tag": by_tag,
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "error": to_text(e), "url": qi_url, "virtual": True}), 500


@app.route("/virtual_behavior/start", methods=["POST"])
def virtual_behavior_start():
    qi_url = request.args.get("url", DEFAULT_VIRTUAL_QI_URL)
    payload = request.get_json(silent=True) or {}
    name = payload.get("name")
    blocking = bool(payload.get("blocking", False))
    if not name:
        return jsonify({"ok": False, "error": "Missing 'name' in JSON body", "virtual": True}), 400
    try:
        svcs = get_services(qi_url, include_tablet=False)
        bm = svcs["bm"]
        if not bm.isBehaviorInstalled(name):
            return jsonify({"ok": False, "error": "Behavior not installed", "name": name, "virtual": True}), 404

        if blocking:
            bm.runBehavior(name)
        else:
            bm.startBehavior(name)

        return jsonify({
            "ok": True,
            "started": name,
            "blocking": blocking,
            "url": qi_url,
            "virtual": True,
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "error": to_text(e), "name": name, "url": qi_url, "virtual": True}), 500


@app.route("/virtual_behavior/stop", methods=["POST"])
def virtual_behavior_stop():
    qi_url = request.args.get("url", DEFAULT_VIRTUAL_QI_URL)
    payload = request.get_json(silent=True) or {}
    name = payload.get("name")
    if not name:
        return jsonify({"ok": False, "error": "Missing 'name' in JSON body", "virtual": True}), 400
    try:
        svcs = get_services(qi_url, include_tablet=False)
        bm = svcs["bm"]
        if bm.isBehaviorRunning(name):
            bm.stopBehavior(name)
            stopped = True
        else:
            stopped = False
        return jsonify({
            "ok": True,
            "stopped": stopped,
            "name": name,
            "reason": None if stopped else "not running",
            "url": qi_url,
            "virtual": True,
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "error": to_text(e), "name": name, "url": qi_url, "virtual": True}), 500


@app.route("/virtual_emotion/<emotion>", methods=["POST"])
def virtual_emotion(emotion):
    qi_url = request.args.get("url", DEFAULT_VIRTUAL_QI_URL)
    payload = request.get_json(silent=True) or {}
    blocking = bool(payload.get("blocking", False))
    key = (emotion or "").strip().lower()
    if key not in EMOTION_BEHAVIORS:
        return jsonify({
            "ok": False,
            "error": "Unknown emotion",
            "emotion": key,
            "allowed": sorted(EMOTION_BEHAVIORS.keys()),
            "virtual": True,
        }), 400
    name = EMOTION_BEHAVIORS[key]
    try:
        svcs = get_services(qi_url, include_tablet=False)
        bm = svcs["bm"]
        if not bm.isBehaviorInstalled(name):
            return jsonify({"ok": False, "error": "Mapped behavior is not installed", "name": name, "virtual": True}), 404

        if blocking:
            bm.runBehavior(name)
        else:
            bm.startBehavior(name)

        return jsonify({
            "ok": True,
            "emotion": key,
            "started": name,
            "blocking": blocking,
            "url": qi_url,
            "virtual": True,
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "error": to_text(e), "emotion": key, "name": name, "url": qi_url, "virtual": True}), 500

@app.route("/animations", methods=["GET"])
def list_animations():
    """
    GET /animations
    Query params:
      - validate=true to check keys against installed behaviors on the robot
      - url=qi_url (optional; defaults to DEFAULT_QI_URL)
    """
    qi_url = request.args.get("url", DEFAULT_QI_URL)
    validate = (request.args.get("validate", "false") or "").lower() == "true"

    try:
        with _anim_lock:
            keys = sorted(_animations.keys())
        resp = {"ok": True, "count": len(keys), "keys": keys, "url": qi_url}

        if validate:
            svcs = get_services(qi_url)
            bm = svcs["bm"]
            installed = set(bm.getInstalledBehaviors())
            unknown = [k for k in keys if _animations.get(k) not in installed]
            resp["installed_count"] = len(installed)
            resp["unknown_keys"] = sorted(unknown)

        return jsonify(resp), 200
    except Exception as e:
        return jsonify({"ok": False, "error": to_text(e), "url": qi_url}), 500

@app.route("/animations/reload", methods=["POST"])
def reload_animations():
    ok, err = load_animations()
    if ok:
        with _anim_lock:
            return jsonify({"ok": True, "reloaded": True, "count": len(_animations)}), 200
    return jsonify({"ok": False, "error": to_text(err)}), 500

def _run_animation_request(name, qi_url, virtual=False):
    try:
        payload = request.get_json(silent=True) or {}
    except Exception:
        payload = {}

    blocking = bool(payload.get("blocking", False))
    blocking = True  # keep legacy behavior: always run blocking

    try:
        print("Connecting to robot at:", qi_url)
        svcs = get_services(qi_url, include_tablet=not virtual)
        bm = svcs["bm"]

        if AUTO_TABLET_ANNOUNCE and not virtual:
            try:
                print("Announcing animation on tablet:", name)
                requests.post(
                    "http://localhost:5000/tablet/text_inline",
                    json={"text": "Animation: " + name},
                    timeout=1.5,
                )
            except Exception as announce_err:
                print("[WARN] Tablet announce failed:", announce_err)

        print("Resolving animation key/path:", name)
        installed = bm.getInstalledBehaviors()
        path, err = resolve_animation_key(name, installed=installed)
        if err:
            return jsonify({
                "ok": False,
                "error": err,
                "name": to_text(name),
                "virtual": bool(virtual),
            }), 400

        if not bm.isBehaviorInstalled(path):
            return jsonify({
                "ok": False,
                "error": "Behavior not installed",
                "path": path,
                "virtual": bool(virtual),
            }), 404

        if blocking:
            print("Playing blocking behavior:", path)
            bm.runBehavior(path)
        else:
            print("Playing non-blocking behavior:", path)
            bm.startBehavior(path)

        return jsonify({
            "ok": True,
            "name": to_text(name),
            "path": path,
            "blocking": blocking,
            "url": qi_url,
            "virtual": bool(virtual),
        }), 200
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": to_text(e),
            "name": to_text(name),
            "url": qi_url,
            "virtual": bool(virtual),
        }), 500


@app.route("/animation/<name>", methods=["POST"])
def animation(name):
    """Trigger a behavior on the real robot via animations.json mapping."""
    print("Received request to play animation:", name)
    qi_url = request.args.get("url", DEFAULT_QI_URL)
    return _run_animation_request(name, qi_url, virtual=False)


@app.route("/virtual_animation/<name>", methods=["POST"])
def virtual_animation(name):
    """Same as /animation but targets the virtual robot session."""
    print("Received request to play VIRTUAL animation:", name)
    qi_url = request.args.get("url", DEFAULT_VIRTUAL_QI_URL)
    return _run_animation_request(name, qi_url, virtual=True)

# ---- Main -------------------------------------------------------------------
@app.route("/tablet/text_inline", methods=["POST"])
def tablet_text_inline():
    qi_url = request.args.get("url", DEFAULT_QI_URL)
    payload = request.get_json(silent=True) or {}

    text  = to_text(payload.get("text", u"Hello!"))
    fg    = to_text(payload.get("fg", u"#FFFFFF"))
    bg    = to_text(payload.get("bg", u"#000000"))
    align = to_text(payload.get("align", u"center"))
    size  = int(payload.get("size", 72))

    def _esc(u):
        return (u.replace(u"&", u"&amp;").replace(u"<", u"&lt;").replace(u">", u"&gt;"))

    html = u"""<!doctype html><meta charset="utf-8">
<style>html,body{{margin:0;height:100%;background:{bg};color:{fg};}}
.wrap{{display:flex;align-items:center;justify-content:center;height:100%;padding:4vw;}}
.txt{{font-family:Arial, sans-serif;font-size:{size}px;line-height:1.2;text-align:{align};word-wrap:break-word;}}
</style><div class="wrap"><div class="txt">{txt}</div></div>""".format(
        bg=bg, fg=fg, size=size, align=align, txt=_esc(text))

    try:
        import urllib
        # URL-encode as data URL so tablet doesn’t need the network
        data_url = "data:text/html;charset=utf-8," + urllib.quote(html.encode("utf-8"))
        svcs = get_services(qi_url)
        tablet = svcs.get("tablet")
        if tablet is None:
            raise RuntimeError("ALTabletService unavailable")
        tablet.showWebview(data_url)
        return jsonify({"ok": True, "mode": "inline"}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": to_text(e)}), 500

@app.route("/say", methods=["POST"])
def say_text():
    """
    POST /say
    JSON body:
      {
        "text": "Hello, I am Pepper!",
        "animated": true,           # optional (default true)
        "language": "English",      # optional
        "speed": 100,               # optional (0..400)
        "pitchShift": 1.0,          # optional (0.5..2.0)
        "volume": 1.0               # optional (0.0..1.0)
      }
    Query params:
      - url=qi_url (optional; defaults to PEPPER_URL)
    """
    qi_url = request.args.get("url", DEFAULT_QI_URL)

    try:
        payload = request.get_json(silent=True) or {}
    except Exception:
        payload = {}

    text       = to_text(payload.get("text", u"")).strip()
    animated   = bool(payload.get("animated", True))
    language   = to_text(payload.get("language")) if payload.get("language") else None
    speed      = payload.get("speed", None)
    pitch      = payload.get("pitchShift", None)
    volume     = payload.get("volume", None)

    if not text:
        return jsonify({"ok": False, "error": "Missing 'text'"}), 400

    try:
        svcs = get_services(qi_url)
        sess = svcs["session"]

        # Prefer AnimatedSpeech when requested/available
        tts = sess.service("ALTextToSpeech")
        animated_svc = None
        if animated:
            try:
                animated_svc = sess.service("ALAnimatedSpeech")
            except Exception:
                animated_svc = None  # fallback handled below

        # Configure TTS parameters if provided
        if language:
            try:
                tts.setLanguage(language)
            except Exception:
                # keep going even if the language isn't available
                pass

        if speed is not None:
            try:
                tts.setParameter("speed", float(speed))
            except Exception:
                pass

        if pitch is not None:
            try:
                tts.setParameter("pitchShift", float(pitch))
            except Exception:
                pass

        if volume is not None:
            try:
                tts.setVolume(float(volume))
            except Exception:
                pass

        # Optional: announce on tablet
        if AUTO_TABLET_ANNOUNCE:
            try:
                requests.post("http://localhost:5000/tablet/text_inline",
                              json={"text": text, "size": AUTO_TABLET_SIZE,
                                    "bg": AUTO_TABLET_BG, "fg": AUTO_TABLET_FG})
            except Exception:
                pass  # non-fatal

        # Speak!
        if animated_svc is not None:
            # bodyLanguageMode: "disabled" | "random" | "contextual"
            cfg = {"bodyLanguageMode": "contextual"}
            try:
                animated_svc.say(text, cfg)
            except Exception:
                # Fallback to plain TTS if animated fails
                tts.say(text)
        else:
            tts.say(text)

        return jsonify({
            "ok": True,
            "spoken": text,
            "animated": bool(animated_svc),
            "url": qi_url
        }), 200

    except Exception as e:
        return jsonify({"ok": False, "error": to_text(e), "url": qi_url}), 500

#vision
# ===== /camera/photo: use subscribe() + setParam(kCameraSelectID) =====
from flask import Response, send_file
from PIL import Image
import io
import time

K_CAMERA_SELECT_ID = 18  # NAOqi param for selecting active camera: 0=top, 1=bottom

def _camera_qi_url():
    try:
        url = DEFAULT_QI_URL
    except NameError:
        url = None
    if not url:
        url = "tcp://127.0.0.1:9559"
    print("[camera] Using qi url:", url)
    return url

def _get_video_service():
    print("[camera] Connecting to ALVideoDevice ...")
    sess = connect_session(_camera_qi_url())
    try:
        video = sess.service("ALVideoDevice")
        print("[camera] ALVideoDevice ready")
        return video
    except Exception as e:
        try:
            msg = to_text(e)
        except Exception:
            msg = str(e)
        print("[camera] ERROR getting ALVideoDevice:", msg)
        raise RuntimeError("ALVideoDevice unavailable: %s" % msg)

def _raw_bytes_py27(raw):
    # normalize to bytes-like for Py2.7 Pillow
    if isinstance(raw, str):
        return raw
    try:
        return buffer(raw)  # Py2 'buffer'
    except Exception:
        pass
    try:
        return "".join(chr(b & 0xFF) for b in raw)
    except Exception:
        return str(raw)

def _image_to_jpeg_bytes(img_tuple):
    if not img_tuple:
        raise RuntimeError("getImageRemote returned None/empty tuple")
    width = int(img_tuple[0]); height = int(img_tuple[1])
    colorspace = img_tuple[3] if len(img_tuple) > 3 else "?"
    raw = img_tuple[6] if len(img_tuple) > 6 else None
    print("[camera] Frame meta w=%d h=%d colorspace=%s type(raw)=%s" %
          (width, height, str(colorspace), type(raw).__name__))
    if raw is None:
        raise RuntimeError("Image buffer is None")
    raw_bytes = _raw_bytes_py27(raw)
    try:
        img = Image.frombytes("RGB", (width, height), raw_bytes, "raw", "RGB")
    except Exception as e1:
        print("[camera] RGB decode failed, trying BGR. Error:", e1)
        img = Image.frombytes("RGB", (width, height), raw_bytes, "raw", "BGR")
    bio = io.BytesIO()
    img.save(bio, "JPEG"); bio.seek(0)
    return bio

@app.route("/camera/photo", methods=["POST"])
def camera_photo():
    """
    Capture one frame from top camera (id 0) and download as pepper_photo_<ts>.jpg
    curl -X POST "http://localhost:5000/camera/photo" -O
    """
    try:
        video = _get_video_service()
        client_name = "flask_camera_photo_%d" % int(time.time())

        cam_id = 0        # 0=top, 1=bottom
        resolution = 2    # 2 = kVGA (640x480)
        color_space = 11  # 11 = kRGBColorSpace
        fps = 5

        print("[camera] Subscribing via subscribe() res=%d cs=%d fps=%d" % (resolution, color_space, fps))
        sub = video.subscribe(client_name, resolution, color_space, fps)
        print("[camera] Subscribed handle:", sub)

        try:
            # pick camera AFTER subscribe()
            print("[camera] Setting camera param %d = %d" % (K_CAMERA_SELECT_ID, cam_id))
            video.setParam(K_CAMERA_SELECT_ID, cam_id)

            # small warm-up delay helps on some builds
            time.sleep(0.1)

            img = video.getImageRemote(sub)
            if img is None:
                print("[camera] getImageRemote returned None; retrying ...")
                time.sleep(0.1)
                img = video.getImageRemote(sub)
            if img is None:
                raise RuntimeError("getImageRemote returned None (camera idle?)")
            print("[camera] Got image tuple of len:", len(img))
        finally:
            try:
                video.unsubscribe(sub)
                print("[camera] Unsubscribed:", sub)
            except Exception as ue:
                print("[camera] Unsubscribe error (ignored):", ue)

        bio = _image_to_jpeg_bytes(img)
        filename = "pepper_photo_%d.jpg" % int(time.time())
        return send_file(bio, mimetype="image/jpeg", as_attachment=True,
                         attachment_filename=filename)
    except Exception as e:
        try:
            msg = to_text(e)
        except Exception:
            msg = str(e)
        print("[camera] ERROR in /camera/photo:", msg)
        return jsonify({"ok": False, "error": msg}), 500
# ===== end patch =====

_animations_ok, _animations_err = load_animations()
if not _animations_ok:
    print("[WARN] animations.json not loaded (ANIMATIONS_FILE='{}'): {}".format(ANIMATIONS_FILE, _animations_err))

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
