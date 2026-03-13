import os

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir)
)

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

# LiveKit listener bridge settings.
LIVEKIT_URL = "ws://127.0.0.1:7880"
LIVEKIT_HTTP_URL = "http://127.0.0.1:7880"
LIVEKIT_ROOM_NAME = "pepper-main"
LISTENER_IDENTITY = "listener-python"
USER_IDENTITY = "user"
AGENT_TRACK_IDENTITY = ""
TOKEN_POLL_INTERVAL = 0.5
LIVEKIT_SESSION_FILE = os.path.join(
    REPO_ROOT, "web", "agents-playground", "token-latest.json"
)
LIVEKIT_STATUS_POLL_INTERVAL_SEC = 2.0

# Session manager service.
SESSION_MANAGER_HOST = "127.0.0.1"
SESSION_MANAGER_PORT = 8787
SESSION_MANAGER_URL = "http://{}:{}".format(
    SESSION_MANAGER_HOST,
    SESSION_MANAGER_PORT,
)
SESSION_IDLE_TIMEOUT_SEC = 30.0
SESSION_COOLDOWN_SEC = 4.0
SESSION_PREROLL_ACTIVITY_SEC = 0.8
SESSION_ACTIVITY_DEBOUNCE_SEC = 0.75

# Local external microphone publisher (`robot/src/user_client.py`).
USER_MIC_SAMPLE_RATE = 48000
USER_MIC_CHANNELS = 1
USER_MIC_BLOCKSIZE = 4800
USER_MIC_RMS_THRESHOLD = 0.012
USER_MIC_DEVICE = None
USER_CLIENT_TEST_MODE = "publish"

# PCM forwarding from listener -> Pepper audio server.
ALLOWED_STREAM_RATES = {16000, 22050, 44100, 48000}
PEPPER_STREAM_RATE = 16000
PEPPER_STREAM_ATTENUATION = 0.4
TCP_HOST = "127.0.0.1"
TCP_PORT = 55555

# Pepper audio server playback tuning.
PEPPER_OUTPUT_VOLUME = 55
PEPPER_PLAYBACK_BATCH_FRAMES = 1600
PEPPER_MAX_BUFFER_FRAMES = 19200
PEPPER_CHUNK_LIMIT_FRAMES = 16384

# Local tablet overlay API served by `robot/src/bridge.py`.
BRIDGE_URL = "http://127.0.0.1:5000"
TABLET_DEBUG_AUDIO_ENABLED = False
TABLET_DEBUG_LISTENER_ENABLED = True
TABLET_STATUS_ENABLED = False
TABLET_TRANSCRIPT_MAX_LINES = 10
TABLET_DEBUG_MIN_INTERVAL_LISTENER = 0.8
TABLET_DEBUG_MIN_INTERVAL_AUDIO = 1.0
TABLET_DEFAULT_SIZE = 42
TABLET_DEFAULT_BG = "#0F1720"
TABLET_DEFAULT_FG = "#D7F2FF"
TABLET_DEFAULT_ALIGN = "left"
TABLET_REPORTER_QUEUE_SIZE = 8
TABLET_DEBUG_MAX_LINES = 12
BRIDGE_LOG_TABLET_HTTP = False
LISTENER_LOG_TABLET_POST = False
LISTENER_LOG_PARTIAL_TRANSCRIPTS = False

# Pepper NAOqi endpoint used by the audio receiver.
PEPPER_QI_URL = "tcp://10.0.0.149:9559"

# Bridge service lookup tuning.
BRIDGE_AUDIO_SERVICE_TIMEOUT_SEC = 120.0
BRIDGE_OPTIONAL_SERVICE_TIMEOUT_SEC = 15.0

# Animation key -> behavior path mapping JSON.
ANIMATIONS_FILE = os.path.join(REPO_ROOT, "robot", "data", "animations.json")

# Keep `False` while diagnosing hardware/safeguard issues.
# When False, bridge reads life state for diagnostics but does not modify it.
TOUCH_AUTONOMOUS_LIFE = False

# Autonomous life profile (safer for fixed reception setup).
LIFE_AUTONOMOUS_BLINKING = True
LIFE_BACKGROUND_MOVEMENT = True
LIFE_BASIC_AWARENESS = False
LIFE_LISTENING_MOVEMENT = False
LIFE_SPEAKING_MOVEMENT = True
