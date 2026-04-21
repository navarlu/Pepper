import os

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
)


def _env_str(name, default):
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_int(name, default):
    value = os.getenv(name)
    if value is None or not str(value).strip():
        return int(default)
    return int(value)


def _env_float(name, default):
    value = os.getenv(name)
    if value is None or not str(value).strip():
        return float(default)
    return float(value)


def _env_bool(name, default):
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}

# Tablet UI HTML templates rendered by `robot/src/bridge.py`.
TABLET_SPLIT_CHAT_HTML_TEMPLATE = """<!doctype html><meta charset="utf-8">
<style>
html,body{{margin:0;height:100%;background:#0D1522;color:#D9F3FF;font-family:Arial,sans-serif;}}
.root{{display:flex;height:100%;box-sizing:border-box;padding:2.2vw;gap:1.4vw;}}
.debug{{flex:0.95;background:#111A2D;border:2px solid #22395A;border-radius:12px;padding:1.2vw;overflow:hidden;}}
.chat{{flex:1.25;background:#111A2D;border:2px solid #22395A;border-radius:12px;padding:1.2vw;display:flex;flex-direction:column;gap:1.2vw;}}
.title{{font-size:30px;font-weight:bold;color:#8EC7FF;margin-bottom:0.8vw;}}
.status{{font-size:18px;line-height:1.3;opacity:0.95;margin-bottom:0.8vw;white-space:pre-wrap;}}
.dbg-lines{{font-size:18px;line-height:1.25;white-space:pre-wrap;overflow:hidden;}}
.dbg-line{{margin-bottom:0.25em;}}
.bubble{{border-radius:12px;padding:0.9vw 1vw;font-size:31px;line-height:1.25;white-space:pre-wrap;word-wrap:break-word;}}
.user{{background:#1C2742;color:#DDF2FF;}}
.pepper{{background:#12303C;color:#D7FFF3;}}
.label{{font-size:31px;font-weight:bold;opacity:0.9;margin-bottom:0.3vw;}}
</style>
<div class="root">
  <div class="debug">
    <div class="title">Debug</div>
    <div class="status">{status_line}
{abilities_line}</div>
    <div class="dbg-lines">{debug_html}</div>
  </div>
  <div class="chat">
    <div class="bubble user"><div class="label">User</div>{user_text}</div>
    <div class="bubble pepper"><div class="label">Pepper</div>{pepper_text}</div>
  </div>
</div>"""

TABLET_INLINE_HTML_TEMPLATE = """<!doctype html><meta charset="utf-8">
<style>html,body{{margin:0;height:100%;background:{bg};color:{fg};}}
.wrap{{display:flex;align-items:flex-end;justify-content:flex-start;height:100%;padding:3vw;box-sizing:border-box;}}
.txt{{font-family:Arial, sans-serif;font-size:{size}px;line-height:1.25;text-align:{align};word-wrap:break-word;white-space:pre-wrap;max-height:100%;width:100%;overflow:hidden;}}
</style><div class="wrap"><div class="txt">{txt}</div></div>"""

# Tablet conversation-history template (mirrors the Pepper Operator chat style).
# Rendered by the bridge when ``ui == "chat_history"``.
# Placeholders: {{session_pill}}, {{bubbles_html}}
TABLET_CHAT_HISTORY_HTML_TEMPLATE = """<!doctype html><meta charset="utf-8">
<style>
html,body{{margin:0;height:100%;background:
  radial-gradient(circle at top left, rgba(41,104,216,0.14), transparent 28%),
  radial-gradient(circle at top right, rgba(90,168,120,0.12), transparent 22%),
  linear-gradient(180deg, #f9fbff, #eef4fb 42%, #e9f0f8 100%);
  color:#16324a;font-family:"Segoe UI",Arial,sans-serif;}}
*{{box-sizing:border-box;}}
.page{{height:100%;display:flex;flex-direction:column;padding:16px 18px;}}
.header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;}}
.header h1{{margin:0;font-size:31px;letter-spacing:-0.03em;}}
.pill{{display:inline-flex;align-items:center;gap:8px;padding:8px 13px;border-radius:999px;
  font-size:16px;font-weight:600;border:1px solid rgba(41,104,216,0.14);}}
.pill::before{{content:"";width:9px;height:9px;border-radius:999px;background:currentColor;}}
.pill.idle{{background:#edf4ff;color:#17479f;}}
.pill.warm{{background:#fff8e6;color:#b38600;}}
.pill.active{{background:#ebf8ef;color:#5aa878;}}
.feed{{flex:1;display:flex;flex-direction:column;gap:13px;overflow-y:auto;
  padding:16px;border-radius:22px;border:1px solid #d8e4f1;
  background:linear-gradient(180deg, rgba(244,248,253,0.88), rgba(255,255,255,0.92));}}
.bubble{{max-width:88%;border-radius:20px;padding:13px 18px;line-height:1.4;
  box-shadow:0 6px 16px rgba(40,74,111,0.06);}}
.bubble.user{{align-self:flex-end;background:#edf4ff;border:1px solid rgba(41,104,216,0.10);}}
.bubble.pepper{{align-self:flex-start;background:#ffffff;border:1px solid #dbe7f3;}}
.bubble.pepper.tool-bubble{{opacity:0.7;padding:5px 10px;box-shadow:none;border-style:dashed;}}
.bubble.pepper.tool-bubble .speaker{{font-size:12px;}}
.bubble.pepper.tool-bubble .body-text{{font-size:14px;font-family:monospace;}}
.bubble.system{{align-self:center;width:100%;max-width:100%;background:transparent;
  box-shadow:none;border:none;padding:5px 0;}}
.speaker{{font-size:13px;font-weight:700;color:#667f97;text-transform:uppercase;
  letter-spacing:0.12em;margin-bottom:6px;}}
.body-text{{font-size:29px;color:#16324a;word-break:break-word;}}
.session-divider{{display:flex;align-items:center;gap:13px;color:#17479f;}}
.session-divider::before,.session-divider::after{{content:"";height:1px;flex:1;
  background:linear-gradient(90deg, transparent, #c4d6ea, transparent);}}
.session-chip{{padding:8px 13px;border-radius:999px;background:rgba(41,104,216,0.08);
  border:1px solid rgba(41,104,216,0.14);font-size:14px;font-weight:700;
  letter-spacing:0.10em;text-transform:uppercase;}}
.empty{{color:#667f97;font-size:18px;text-align:center;margin-top:40px;}}
</style>
<div class="page">
  <div class="header">
    <h1>Pepper</h1>
    {session_pill}
  </div>
  <div class="feed">{bubbles_html}</div>
</div>"""

# LiveKit listener bridge settings.
LIVEKIT_URL = _env_str("LIVEKIT_URL", "ws://127.0.0.1:7880")
LIVEKIT_HOST_WS_URL = _env_str("LIVEKIT_HOST_WS_URL", "ws://127.0.0.1:7880")
LIVEKIT_HTTP_URL = _env_str("LIVEKIT_HTTP_URL", "http://127.0.0.1:7880")
LIVEKIT_ROOM_NAME = _env_str("LIVEKIT_ROOM_NAME", "pepper-main")
LISTENER_IDENTITY = _env_str("LISTENER_IDENTITY", "listener-python")
USER_IDENTITY = _env_str("USER_IDENTITY", "user")
AGENT_TRACK_IDENTITY = _env_str("AGENT_TRACK_IDENTITY", "")
TOKEN_POLL_INTERVAL = _env_float("TOKEN_POLL_INTERVAL", 0.5)
LIVEKIT_SESSION_FILE = _env_str(
    "LIVEKIT_SESSION_FILE",
    os.path.join(REPO_ROOT, "services", "data", "token-latest.json"),
)
LIVEKIT_STATUS_POLL_INTERVAL_SEC = _env_float("LIVEKIT_STATUS_POLL_INTERVAL_SEC", 2.0)

# Session manager service.
SESSION_MANAGER_HOST = _env_str("SESSION_MANAGER_HOST", "127.0.0.1")
SESSION_MANAGER_PORT = _env_int("SESSION_MANAGER_PORT", 8787)
SESSION_MANAGER_URL = _env_str(
    "SESSION_MANAGER_URL",
    "http://{}:{}".format(
    SESSION_MANAGER_HOST,
    SESSION_MANAGER_PORT,
    ),
)
SESSION_IDLE_TIMEOUT_SEC = _env_float("SESSION_IDLE_TIMEOUT_SEC", 30.0)
SESSION_COOLDOWN_SEC = _env_float("SESSION_COOLDOWN_SEC", 4.0)
SESSION_PREROLL_ACTIVITY_SEC = _env_float("SESSION_PREROLL_ACTIVITY_SEC", 0.8)
SESSION_ACTIVITY_DEBOUNCE_SEC = _env_float("SESSION_ACTIVITY_DEBOUNCE_SEC", 0.75)

# Local external microphone publisher (`services/src/user_client.py`).
USER_MIC_SAMPLE_RATE = _env_int("USER_MIC_SAMPLE_RATE", 48000)
USER_MIC_CHANNELS = _env_int("USER_MIC_CHANNELS", 1)
USER_MIC_BLOCKSIZE = _env_int("USER_MIC_BLOCKSIZE", 4800)
USER_MIC_RMS_THRESHOLD = _env_float("USER_MIC_RMS_THRESHOLD", 0.012)
USER_MIC_DEVICE = os.getenv("USER_MIC_DEVICE")
USER_CLIENT_TEST_MODE = _env_str("USER_CLIENT_TEST_MODE", "publish")

# PCM forwarding from listener -> Pepper audio server.
ALLOWED_STREAM_RATES = {16000, 22050, 44100, 48000}
_raw_stream_rate = _env_int("PEPPER_STREAM_RATE", 16000)
if _raw_stream_rate not in ALLOWED_STREAM_RATES:
    print("[config] Unsupported PEPPER_STREAM_RATE=%s, fallback to 16000" % _raw_stream_rate)
    _raw_stream_rate = 16000
PEPPER_STREAM_RATE = _raw_stream_rate
PEPPER_STREAM_ATTENUATION = _env_float("PEPPER_STREAM_ATTENUATION", 0.4)
BRIDGE_BIND_HOST = _env_str("BRIDGE_BIND_HOST", "0.0.0.0")
TCP_HOST = _env_str("TCP_HOST", "127.0.0.1")
TCP_PORT = _env_int("TCP_PORT", 55555)

# Pepper audio server playback tuning.
PEPPER_OUTPUT_VOLUME = _env_int("PEPPER_OUTPUT_VOLUME", 55)
PEPPER_PLAYBACK_BATCH_FRAMES = _env_int("PEPPER_PLAYBACK_BATCH_FRAMES", 1600)
PEPPER_MAX_BUFFER_FRAMES = _env_int("PEPPER_MAX_BUFFER_FRAMES", 19200)
PEPPER_CHUNK_LIMIT_FRAMES = _env_int("PEPPER_CHUNK_LIMIT_FRAMES", 16384)

# Local tablet overlay API served by `robot/src/bridge.py`.
# The tablet-display service (services/src/tablet_server.py) owns the tablet
# screen and POSTs rendered HTML to `BRIDGE_URL/tablet/url`.
BRIDGE_URL = _env_str("BRIDGE_URL", "http://127.0.0.1:5000")
TABLET_DEBUG_AUDIO_ENABLED = _env_bool("TABLET_DEBUG_AUDIO_ENABLED", False)
TABLET_DEBUG_LISTENER_ENABLED = _env_bool("TABLET_DEBUG_LISTENER_ENABLED", True)
TABLET_STATUS_ENABLED = _env_bool("TABLET_STATUS_ENABLED", False)
TABLET_TRANSCRIPT_MAX_LINES = _env_int("TABLET_TRANSCRIPT_MAX_LINES", 6)
TABLET_DEBUG_MIN_INTERVAL_LISTENER = _env_float("TABLET_DEBUG_MIN_INTERVAL_LISTENER", 0.8)
TABLET_DEBUG_MIN_INTERVAL_AUDIO = _env_float("TABLET_DEBUG_MIN_INTERVAL_AUDIO", 1.0)
TABLET_DEFAULT_SIZE = _env_int("TABLET_DEFAULT_SIZE", 42)
TABLET_DEFAULT_BG = _env_str("TABLET_DEFAULT_BG", "#0F1720")
TABLET_DEFAULT_FG = _env_str("TABLET_DEFAULT_FG", "#D7F2FF")
TABLET_DEFAULT_ALIGN = _env_str("TABLET_DEFAULT_ALIGN", "left")
TABLET_REPORTER_QUEUE_SIZE = _env_int("TABLET_REPORTER_QUEUE_SIZE", 8)
TABLET_DEBUG_MAX_LINES = _env_int("TABLET_DEBUG_MAX_LINES", 12)
BRIDGE_LOG_TABLET_HTTP = _env_bool("BRIDGE_LOG_TABLET_HTTP", False)
LISTENER_LOG_TABLET_POST = _env_bool("LISTENER_LOG_TABLET_POST", False)
LISTENER_LOG_PARTIAL_TRANSCRIPTS = _env_bool("LISTENER_LOG_PARTIAL_TRANSCRIPTS", False)

# Pepper NAOqi endpoint used by the audio receiver.
PEPPER_QI_URL = _env_str("PEPPER_QI_URL", "tcp://192.168.210.113:9559")

# Bridge service lookup tuning.
BRIDGE_AUDIO_SERVICE_TIMEOUT_SEC = _env_float("BRIDGE_AUDIO_SERVICE_TIMEOUT_SEC", 120.0)
BRIDGE_OPTIONAL_SERVICE_TIMEOUT_SEC = _env_float("BRIDGE_OPTIONAL_SERVICE_TIMEOUT_SEC", 15.0)
BRIDGE_CONNECT_POLL_INTERVAL_SEC = _env_float("BRIDGE_CONNECT_POLL_INTERVAL_SEC", 5.0)

# Camera snapshot capture parameters (used by utils.capture_camera_snapshot,
# exposed via POST /camera/snapshot on the bridge).
CAMERA_SNAPSHOT_NAME = _env_str("CAMERA_SNAPSHOT_NAME", "kampion_look_around")
CAMERA_SNAPSHOT_CAMERA_INDEX = _env_int("CAMERA_SNAPSHOT_CAMERA_INDEX", 0)   # 0=top, 1=bottom
CAMERA_SNAPSHOT_RESOLUTION = _env_int("CAMERA_SNAPSHOT_RESOLUTION", 2)       # kVGA = 640x480
CAMERA_SNAPSHOT_COLOR_SPACE = _env_int("CAMERA_SNAPSHOT_COLOR_SPACE", 11)    # kRGB (RGB888)
CAMERA_SNAPSHOT_FPS = _env_int("CAMERA_SNAPSHOT_FPS", 10)
CAMERA_SNAPSHOT_MAX_SIDE = _env_int("CAMERA_SNAPSHOT_MAX_SIDE", 768)
CAMERA_SNAPSHOT_QUALITY = _env_int("CAMERA_SNAPSHOT_QUALITY", 82)

# Animation key -> behavior path mapping JSON.
ANIMATIONS_FILE = os.path.join(REPO_ROOT, "robot", "data", "animations.json")

# Keep `False` while diagnosing hardware/safeguard issues.
# When False, bridge reads life state for diagnostics but does not modify it.
TOUCH_AUTONOMOUS_LIFE = _env_bool("TOUCH_AUTONOMOUS_LIFE", False)

# Autonomous life profile (safer for fixed reception setup).
LIFE_AUTONOMOUS_BLINKING = _env_bool("LIFE_AUTONOMOUS_BLINKING", True)
LIFE_BACKGROUND_MOVEMENT = _env_bool("LIFE_BACKGROUND_MOVEMENT", True)
LIFE_BASIC_AWARENESS = _env_bool("LIFE_BASIC_AWARENESS", False)
LIFE_LISTENING_MOVEMENT = _env_bool("LIFE_LISTENING_MOVEMENT", False)
LIFE_SPEAKING_MOVEMENT = _env_bool("LIFE_SPEAKING_MOVEMENT", True)
