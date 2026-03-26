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

# LiveKit connection settings.
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
    os.path.join(REPO_ROOT, "data", "token-latest.json"),
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

# Local external microphone publisher (services/src/user_client.py).
USER_MIC_SAMPLE_RATE = _env_int("USER_MIC_SAMPLE_RATE", 48000)
USER_MIC_CHANNELS = _env_int("USER_MIC_CHANNELS", 1)
USER_MIC_BLOCKSIZE = _env_int("USER_MIC_BLOCKSIZE", 4800)
USER_MIC_RMS_THRESHOLD = _env_float("USER_MIC_RMS_THRESHOLD", 0.012)
USER_MIC_DEVICE = os.getenv("USER_MIC_DEVICE")
USER_CLIENT_TEST_MODE = _env_str("USER_CLIENT_TEST_MODE", "publish")

# PCM forwarding from listener -> Pepper audio server.
ALLOWED_STREAM_RATES = {16000, 22050, 44100, 48000}
PEPPER_STREAM_RATE = _env_int("PEPPER_STREAM_RATE", 16000)
PEPPER_STREAM_ATTENUATION = _env_float("PEPPER_STREAM_ATTENUATION", 0.4)
TCP_HOST = _env_str("TCP_HOST", "127.0.0.1")
TCP_PORT = _env_int("TCP_PORT", 55555)

# Bridge / tablet overlay references used by listener.
BRIDGE_URL = _env_str("BRIDGE_URL", "http://127.0.0.1:5000")
TABLET_DEBUG_LISTENER_ENABLED = _env_bool("TABLET_DEBUG_LISTENER_ENABLED", True)
TABLET_STATUS_ENABLED = _env_bool("TABLET_STATUS_ENABLED", False)
TABLET_TRANSCRIPT_MAX_LINES = _env_int("TABLET_TRANSCRIPT_MAX_LINES", 6)
TABLET_DEBUG_MIN_INTERVAL_LISTENER = _env_float("TABLET_DEBUG_MIN_INTERVAL_LISTENER", 0.8)
LISTENER_LOG_TABLET_POST = _env_bool("LISTENER_LOG_TABLET_POST", False)
LISTENER_LOG_PARTIAL_TRANSCRIPTS = _env_bool("LISTENER_LOG_PARTIAL_TRANSCRIPTS", False)
