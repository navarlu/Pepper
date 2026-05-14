"""Env-backed configuration for every service under `services/src/`.

One module, all the tunables — there's no per-service config file.
Each service (`orchestrator`, `audio_bridge`, `user_client`,
`tablet_server`, `text_chat`) imports exactly what it needs.
"""

from __future__ import annotations

import os

REPO_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(__file__), os.pardir, os.pardir, os.pardir)
)


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not str(value).strip():
        return int(default)
    return int(value)


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not str(value).strip():
        return float(default)
    return float(value)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return bool(default)
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


# ── LiveKit connection ──────────────────────────────────────────────────────
LIVEKIT_URL = _env_str("LIVEKIT_URL", "ws://127.0.0.1:7880")
LIVEKIT_HOST_WS_URL = _env_str("LIVEKIT_HOST_WS_URL", LIVEKIT_URL)
LIVEKIT_HTTP_URL = _env_str("LIVEKIT_HTTP_URL", "http://127.0.0.1:7880")

# Per-role identities used in the room. Orchestrator mints tokens for each.
USER_IDENTITY = _env_str("USER_IDENTITY", "user")
LISTENER_IDENTITY = _env_str("LISTENER_IDENTITY", "listener-python")
MONITOR_IDENTITY = _env_str("MONITOR_IDENTITY", "monitor-python")
DEBUG_CLI_IDENTITY = _env_str("DEBUG_CLI_IDENTITY", "debug-cli")
TABLET_IDENTITY = _env_str("TABLET_IDENTITY", "tablet")

# Optional hard pin for the agent audio track (overrides auto-detect in audio_bridge).
AGENT_TRACK_IDENTITY = _env_str("AGENT_TRACK_IDENTITY", "")

# How often consumers poll the token file; how often each cycle checks.
TOKEN_POLL_INTERVAL = _env_float("TOKEN_POLL_INTERVAL", 0.5)
SESSION_ACTIVITY_DEBOUNCE_SEC = _env_float("SESSION_ACTIVITY_DEBOUNCE_SEC", 0.75)
SESSION_IDLE_TIMEOUT_SEC = _env_float("SESSION_IDLE_TIMEOUT_SEC", 30.0)

# Shared files. Both live under services/data/ and are the glue between
# orchestrator and the other services (token rotation, runtime state).
LIVEKIT_SESSION_FILE = _env_str(
    "LIVEKIT_SESSION_FILE",
    os.path.join(REPO_ROOT, "services", "data", "token-latest.json"),
)
STATE_FILE = _env_str(
    "STATE_FILE",
    os.path.join(REPO_ROOT, "services", "data", "state.json"),
)


# ── Orchestrator ────────────────────────────────────────────────────────────
# Agent names registered with LiveKit for dispatch. Must match
# `PEPPER_AGENT_NAME` in the voice-agent worker (see voice-agent/src/live/config.py).
PEPPER_AGENT_MODE_DEFAULT = _env_str("PEPPER_AGENT_MODE", "openai")
AGENT_NAMES: dict[str, str] = {
    "openai": _env_str("PEPPER_AGENT_NAME_OPENAI", "pepper-openai"),
    "local": _env_str("PEPPER_AGENT_NAME_LOCAL", "pepper-local"),
}
# How often the orchestrator re-reads state.json for external changes.
STATE_POLL_SEC = _env_float("STATE_POLL_SEC", 3.0)
# TTL-ish refresh for LiveKit JWTs (the tokens themselves last 30 days but we
# rewrite the file periodically so downstream services don't drift if clocks do).
TOKEN_REFRESH_SEC = _env_float("TOKEN_REFRESH_SEC", 4 * 3600.0)


# ── User-client (RPi microphone) ────────────────────────────────────────────
USER_MIC_SAMPLE_RATE = _env_int("USER_MIC_SAMPLE_RATE", 48000)
USER_MIC_CHANNELS = _env_int("USER_MIC_CHANNELS", 1)
USER_MIC_BLOCKSIZE = _env_int("USER_MIC_BLOCKSIZE", 4800)
USER_MIC_RMS_THRESHOLD = _env_float("USER_MIC_RMS_THRESHOLD", 0.012)
USER_MIC_DEVICE = os.getenv("USER_MIC_DEVICE")
USER_CLIENT_TEST_MODE = _env_str("USER_CLIENT_TEST_MODE", "publish")


# ── Audio-bridge (LiveKit → TCP → robot bridge) ─────────────────────────────
ALLOWED_STREAM_RATES = {16000, 22050, 44100, 48000}
_raw_stream_rate = _env_int("PEPPER_STREAM_RATE", 16000)
if _raw_stream_rate not in ALLOWED_STREAM_RATES:
    print(f"[config] Unsupported PEPPER_STREAM_RATE={_raw_stream_rate}, fallback to 16000")
    _raw_stream_rate = 16000
PEPPER_STREAM_RATE = _raw_stream_rate
PEPPER_STREAM_ATTENUATION = _env_float("PEPPER_STREAM_ATTENUATION", 0.4)

# Silence-gate: skip forwarding near-silent frames so NAOqi's
# ALAudioDevice queue doesn't fill up between utterances. LiveKit
# AgentSession publishes a continuous audio track (50-100 frames/sec)
# whether the agent is speaking or not; without this gate the robot
# receives audio at ~1.6× real-time and `sendRemoteBufferToOutput`
# backs up, leading to 10s of seconds of audible delay.
#
# Tuning:
#  * THRESHOLD: int16 RMS. Default 20 catches the silence the LiveKit
#    track emits between utterances (~rms 5-15) while staying well
#    below the quietest TTS vowels (~rms 60+). Bump up if the gate
#    misses (silence flows through, backlog grows); bump down if
#    speech gets clipped at the START of an utterance.
#  * HANGOVER_MS: how long after the last loud frame we keep
#    forwarding before gating again. Default 1500 ms is long enough
#    to bridge sentence boundaries inside a single TTS reply (Piper
#    can emit 500 ms of silence between sentences). Make it shorter
#    if you want the gate to close faster; longer if you hear it
#    chopping inside a single reply.
#  * SET SILENCE_GATE_RMS=0 to disable gating entirely.
#  * SET SILENCE_GATE_TRACE=1 to log per-frame RMS — useful for
#    tuning, very noisy in production.
SILENCE_GATE_RMS = _env_int("SILENCE_GATE_RMS", 20)
SILENCE_GATE_HANGOVER_MS = _env_int("SILENCE_GATE_HANGOVER_MS", 1500)
SILENCE_GATE_TRACE = _env_int("SILENCE_GATE_TRACE", 0) == 1

TCP_HOST = _env_str("TCP_HOST", "127.0.0.1")
TCP_PORT = _env_int("TCP_PORT", 55555)


# ── Tablet display ──────────────────────────────────────────────────────────
# The bridge endpoint that proxies `showWebview` to Pepper's tablet.
BRIDGE_URL = _env_str("BRIDGE_URL", "http://127.0.0.1:5000")
