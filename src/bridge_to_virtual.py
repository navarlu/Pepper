# -*- coding: utf-8 -*-
from __future__ import print_function
import os
import time
import qi
from flask import Flask, request, jsonify
from dotenv import load_dotenv
# You can also set NAOQI_URL env var like: NAOQI_URL=tcp://192.168.1.10:9559
load_dotenv()  # load from .env if present
DEFAULT_QI_URL = os.environ.get("NAOQI_URL", "tcp://127.0.0.1:41095")

app = Flask(__name__)

# Map friendly emotions to your actual installed behavior paths
EMOTION_BEHAVIORS = {
    # normalize keys to lowercase
    "bored":    ".lastUploadedChoregrapheBehavior/Bored",
    "chill":    ".lastUploadedChoregrapheBehavior/Chill",
    "confused": ".lastUploadedChoregrapheBehavior/Confused",
    "curious":  ".lastUploadedChoregrapheBehavior/Curious",
    "excited":  ".lastUploadedChoregrapheBehavior/Excited",
    "fear":     ".lastUploadedChoregrapheBehavior/Fear",
    "happy":    ".lastUploadedChoregrapheBehavior/Happy",
    "kisses":   ".lastUploadedChoregrapheBehavior/Kisses",
    "thinking": ".lastUploadedChoregrapheBehavior/Thinking",
}

def connect_session(qi_url):
    s = qi.Session()
    s.connect(qi_url)
    return s

def get_services(qi_url):
    s = connect_session(qi_url)
    return {
        "session": s,
        "motion": s.service("ALMotion"),
        "posture": s.service("ALRobotPosture"),
        "bm": s.service("ALBehaviorManager"),
        # "leds": s.service("ALLeds"),  # uncomment if you want LED endpoints
    }

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
        return jsonify({"ok": False, "error": str(e), "url": qi_url}), 500

@app.route("/rest", methods=["POST"])
def rest():
    qi_url = request.args.get("url", DEFAULT_QI_URL)
    try:
        svcs = get_services(qi_url)
        svcs["motion"].rest()
        return jsonify({"ok": True, "action": "rest", "url": qi_url}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "url": qi_url}), 500

@app.route("/behaviors", methods=["GET"])
def behaviors():
    qi_url = request.args.get("url", DEFAULT_QI_URL)
    try:
        svcs = get_services(qi_url)
        bm = svcs["bm"]
        installed = sorted(bm.getInstalledBehaviors())
        running = sorted(bm.getRunningBehaviors())
        tags = sorted(bm.getTagList()) if hasattr(bm, "getTagList") else []
        by_tag = {}
        for t in tags:
            try:
                lst = bm.getBehaviorsByTag(t)
                if lst: by_tag[t] = lst
            except Exception:
                pass
        return jsonify({
            "ok": True,
            "url": qi_url,
            "installed": installed,
            "running": running,
            "tags": tags,
            "by_tag": by_tag
        }), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "url": qi_url}), 500

@app.route("/behavior/start", methods=["POST"])
def behavior_start():
    """
    Body JSON:
      { "name": "<full behavior path>", "blocking": false }
    """
    qi_url = request.args.get("url", DEFAULT_QI_URL)
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    blocking = bool(data.get("blocking", False))
    if not name:
        return jsonify({"ok": False, "error": "Missing 'name' in JSON body"}), 400
    try:
        svcs = get_services(qi_url)
        bm = svcs["bm"]
        if not bm.isBehaviorInstalled(name):
            return jsonify({"ok": False, "error": "Behavior not installed", "name": name}), 404

        if blocking:
            bm.runBehavior(name)       # blocks until finished
        else:
            bm.startBehavior(name)     # non-blocking

        return jsonify({"ok": True, "started": name, "blocking": blocking, "url": qi_url}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "name": name, "url": qi_url}), 500

@app.route("/behavior/stop", methods=["POST"])
def behavior_stop():
    """
    Body JSON:
      { "name": "<full behavior path>" }
    """
    qi_url = request.args.get("url", DEFAULT_QI_URL)
    data = request.get_json(silent=True) or {}
    name = data.get("name")
    if not name:
        return jsonify({"ok": False, "error": "Missing 'name' in JSON body"}), 400
    try:
        svcs = get_services(qi_url)
        bm = svcs["bm"]
        if bm.isBehaviorRunning(name):
            bm.stopBehavior(name)
            return jsonify({"ok": True, "stopped": name, "url": qi_url}), 200
        else:
            return jsonify({"ok": True, "stopped": False, "reason": "not running", "name": name, "url": qi_url}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "name": name, "url": qi_url}), 500

@app.route("/emotion/<emotion>", methods=["POST"])
def emotion(emotion):
    """
    Start a behavior by friendly emotion name.
    Example: POST /emotion/happy  or /emotion/excited
    Optional JSON: { "blocking": false }
    """
    qi_url = request.args.get("url", DEFAULT_QI_URL)
    payload = request.get_json(silent=True) or {}
    blocking = bool(payload.get("blocking", False))
    key = (emotion or "").strip().lower()
    if key not in EMOTION_BEHAVIORS:
        return jsonify({
            "ok": False,
            "error": "Unknown emotion",
            "emotion": key,
            "allowed": sorted(EMOTION_BEHAVIORS.keys())
        }), 400
    name = EMOTION_BEHAVIORS[key]
    try:
        svcs = get_services(qi_url)
        bm = svcs["bm"]
        if not bm.isBehaviorInstalled(name):
            return jsonify({"ok": False, "error": "Mapped behavior is not installed", "name": name}), 404

        if blocking:
            bm.runBehavior(name)
        else:
            bm.startBehavior(name)

        return jsonify({"ok": True, "emotion": key, "started": name, "blocking": blocking, "url": qi_url}), 200
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "emotion": key, "name": name, "url": qi_url}), 500

if __name__ == "__main__":
    # 0.0.0.0 to be reachable from LAN; change to 127.0.0.1 if local only
    app.run(host="0.0.0.0", port=5000, debug=True)