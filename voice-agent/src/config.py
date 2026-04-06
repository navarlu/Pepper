import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


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


def _env_optional_int(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name)
    if value is None or not str(value).strip():
        return default
    return int(value)

LANG = "en"
AGENT_VERSION = "0.4.1"
MODEL_NAME = "gpt-realtime-mini"
TTS_VOICE = "marin"
LOCAL_STT_MODEL = _env_str("LOCAL_STT_MODEL", "tiny")
LOCAL_STT_DEVICE = _env_str("LOCAL_STT_DEVICE", "cpu")
LOCAL_STT_COMPUTE_TYPE = _env_str("LOCAL_STT_COMPUTE_TYPE", "int8")
LOCAL_STT_CPU_THREADS = _env_int("LOCAL_STT_CPU_THREADS", 0)
LOCAL_LLM_BASE_URL = _env_str("LOCAL_LLM_BASE_URL", "http://localhost:8000/v1")
LOCAL_LLM_MODEL = _env_str("LOCAL_LLM_MODEL", "Qwen/Qwen2.5-7B-Instruct")
LOCAL_TTS_MODEL_PATH = Path(
    _env_str(
        "LOCAL_TTS_MODEL_PATH",
        str(BASE_DIR / "models" / "piper" / "en_US-hfc_female-medium.onnx"),
    )
)
LOCAL_TTS_USE_CUDA = False
LOCAL_TTS_SPEAKER_ID = _env_optional_int("LOCAL_TTS_SPEAKER_ID")
LOCAL_TTS_LENGTH_SCALE = _env_float("LOCAL_TTS_LENGTH_SCALE", 1.0)
LOCAL_TTS_NOISE_SCALE = _env_float("LOCAL_TTS_NOISE_SCALE", 0.667)
LOCAL_TTS_NOISE_W_SCALE = _env_float("LOCAL_TTS_NOISE_W_SCALE", 0.8)
LISTENER_IDENTITY = _env_str("LISTENER_IDENTITY", "listener-python")
LIVEKIT_URL = _env_str("LIVEKIT_URL", "ws://127.0.0.1:7880")
SESSION_MANAGER_URL = _env_str("SESSION_MANAGER_URL", "http://127.0.0.1:8787")
DEV_CONSOLE_URL = _env_str("DEV_CONSOLE_URL", "http://127.0.0.1:8788")

AGENT_NAME = "Pepper"
ORGANIZATION = "CTU Faculty of Electrical Engineering"
PLACE = "Charles Square"

# Weaviate vector search configuration.
WEAVIATE_HOST = _env_str("WEAVIATE_HOST", "localhost")
WEAVIATE_HTTP_PORT = _env_int("WEAVIATE_HTTP_PORT", 8080)
WEAVIATE_GRPC_PORT = _env_int("WEAVIATE_GRPC_PORT", 50051)
WEAVIATE_COLLECTION = "fel_v007"
WEAVIATE_OPENAI_MODEL = "text-embedding-3-large"
WEAVIATE_HYBRID_ALPHA = 0.7

DOC_TITLE_FIELD = "title"
DOC_CONTENT_FIELD = "content"
DOC_SOURCE_FIELD = "source"
DOC_CREATED_AT_FIELD = "created_at"
SEED_DATA_PATHS = [BASE_DIR / "data" / "FEL"]
SEED_LOG_PREFIX = "[weaviate-seed]"

ENABLE_QUERY_SEARCH = True
QUERY_SEARCH_DEFAULT_LIMIT = 5
QUERY_SEARCH_MAX_LIMIT = 8

# Pepper animation tool (voice-agent -> robot bridge).
ENABLE_ANIMATION_TOOL = True
ANIMATION_BRIDGE_URL = _env_str("ANIMATION_BRIDGE_URL", "http://127.0.0.1:5000")
ANIMATION_TOOL_HTTP_TIMEOUT_SEC = _env_float("ANIMATION_TOOL_HTTP_TIMEOUT_SEC", 2.5)
ANIMATION_TOOL_MAX_NAME_CHARS = 120

# Each group maps a semantic name (what the agent sees) to a list of actual
# Pepper animation keys.  The tool picks a random variant from the group so
# Pepper's movements feel natural and non-repetitive.
ANIMATION_GROUPS: dict[str, list[str]] = {
    "greeting":   ["Hey_1", "Hey_2", "Hey_3", "Hey_4", "Hey_6", "Hey_7", "Hey_8", "Hey_9", "Hey_10"],
    "bow":        ["BowShort_1", "BowShort_2", "BowShort_3"],
    "explain":    ["Explain_1", "Explain_2", "Explain_3", "Explain_4", "Explain_5", "Explain_6", "Explain_7", "Explain_8"],
    "happy":      ["Happy_1", "Happy_2", "Happy_3", "Happy_4"],
    "thinking":   ["Thinking_1", "Thinking_2", "Thinking_3", "Thinking_4", "Thinking_5", "Thinking_6", "Thinking_7", "Thinking_8"],
    "dont_know":  ["IDontKnow_1", "IDontKnow_2", "IDontKnow_3", "IDontKnow_4", "IDontKnow_5", "IDontKnow_6"],
    "excited":    ["Excited_1", "Excited_2", "Excited_3"],
    "interested": ["Interested_1", "Interested_2"],
    "surprised":  ["Surprised_1", "Surprise_1", "Surprise_2", "Surprise_3"],
}

# Flat set of all valid animation keys across all groups (for bridge validation).
ANIMATION_TOOL_ALLOWED: set[str] = {
    key for variants in ANIMATION_GROUPS.values() for key in variants
}

# Aliases let the agent use natural words that map to group names.
ANIMATION_TOOL_ALIASES: dict[str, str] = {
    "hello":       "greeting",
    "hi":          "greeting",
    "greet":       "greeting",
    "welcome":     "greeting",
    "hey":         "greeting",
    "bow":         "bow",
    "thanks":      "bow",
    "thank_you":   "bow",
    "goodbye":     "bow",
    "explain":     "explain",
    "info":        "explain",
    "information": "explain",
    "happy":       "happy",
    "positive":    "happy",
    "joy":         "happy",
    "thinking":    "thinking",
    "searching":   "thinking",
    "consider":    "thinking",
    "uncertain":   "dont_know",
    "dontknow":    "dont_know",
    "i_dont_know": "dont_know",
    "shrug":       "dont_know",
    "excited":     "excited",
    "enthusiasm":  "excited",
    "interested":  "interested",
    "listening":   "interested",
    "curious":     "interested",
    "surprised":   "surprised",
    "wow":         "surprised",
    "shock":       "surprised",
}

BASE_SYSTEM_PROMPT = """
You are Pepper, a humanoid receptionist robot at CTU FEE in Prague (Karlovo náměstí).
Communicate in English, speak briefly, clearly, and politely.
If the user prefers another language, switch to it.

What you do:
- Provide information about FEE using the `query_search` tool.
- When you are unsure, use `query_search` instead of guessing.
- If the information is not available in the provided materials, say so directly and offer to clarify the question.
- Keep responses concise (typically 1–4 sentences), unless the user asks for more detail.
- Do not mention internal implementation details or library names.
""".strip()

OPENAI_SYSTEM_PROMPT = """
{base}

## Animations

You can trigger Pepper gestures with the `play_animation` tool.

Rules:
- For normal user-facing spoken replies, call `play_animation` exactly once.
- Use it at most once per reply.
- Skip it only for very short acknowledgements such as "yes" or "ok", urgent safety replies, or when the user asks for no gestures.
- When you use it, call the tool directly with one of these semantic animation names:
  `greeting`, `bow`, `explain`, `happy`, `thinking`, `dont_know`
- Suggested mapping:
  greeting or welcome -> `greeting`
  thanks or polite acknowledgement -> `bow`
  explaining information -> `explain`
  positive or cheerful answer -> `happy`
  searching or considering -> `thinking`
  uncertainty or missing information -> `dont_know`
- NEVER say tool names, tool arguments, bracketed action text, or stage directions aloud.
""".strip().format(base=BASE_SYSTEM_PROMPT)

LOCAL_SYSTEM_PROMPT = (
    "You are Pepper, a robot receptionist at CTU FEE Prague. "
    "Speak briefly and politely in English. If the user prefers another language, switch to it. "
    "Use query_search to find information — do not guess. "
    "Call play_animation to check your body state before every reply. "
    "Never say tool names aloud."
)

