from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

LANG = "en"
AGENT_VERSION = "0.1.0"
MODEL_NAME = "gpt-realtime-mini"
TTS_VOICE = "marin"
CASCADE_STT_MODEL = "small"
CASCADE_STT_DEVICE = "cpu"
CASCADE_STT_COMPUTE_TYPE = "int8"
CASCADE_STT_CPU_THREADS = 0
CASCADE_LLM_MODEL = "gpt-4.1-mini"
CASCADE_TTS_MODEL_PATH = BASE_DIR / "models" / "piper" / "en_US-lessac-medium.onnx"
CASCADE_TTS_USE_CUDA = False
CASCADE_TTS_SPEAKER_ID = None
CASCADE_TTS_LENGTH_SCALE = 1.0
CASCADE_TTS_NOISE_SCALE = 0.667
CASCADE_TTS_NOISE_W_SCALE = 0.8
LISTENER_IDENTITY = "listener-python"
LIVEKIT_URL = "ws://127.0.0.1:7880"

AGENT_NAME = "Pepper"
ORGANIZATION = "CTU Faculty of Electrical Engineering"
PLACE = "Charles Square"

# Weaviate vector search configuration.
WEAVIATE_HOST = "localhost"
WEAVIATE_HTTP_PORT = 8080
WEAVIATE_GRPC_PORT = 50051
WEAVIATE_COLLECTION = "fel_v003"
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
QUERY_SEARCH_MAX_CONTENT_CHARS = 900

# Pepper animation tool (voice-agent -> robot bridge).
ENABLE_ANIMATION_TOOL = True
ANIMATION_BRIDGE_URL = "http://127.0.0.1:5000"
ANIMATION_TOOL_HTTP_TIMEOUT_SEC = 2.5
ANIMATION_TOOL_MAX_NAME_CHARS = 120
ANIMATION_TOOL_ALLOWED = (
    "Hey_1",         # welcome / greeting
    "BowShort_1",    # polite acknowledgement
    "Explain_1",     # giving information
    "Happy_1",       # positive response
    "Thinking_1",    # considering / searching
    "IDontKnow_1",   # uncertainty
)
ANIMATION_TOOL_ALIASES = {
    "hello": "Hey_1",
    "hi": "Hey_1",
    "greet": "Hey_1",
    "welcome": "Hey_1",
    "bow": "BowShort_1",
    "thanks": "BowShort_1",
    "explain": "Explain_1",
    "info": "Explain_1",
    "information": "Explain_1",
    "happy": "Happy_1",
    "positive": "Happy_1",
    "thinking": "Thinking_1",
    "searching": "Thinking_1",
    "uncertain": "IDontKnow_1",
    "dontknow": "IDontKnow_1",
    "i_dont_know": "IDontKnow_1",
}

VOICE_AGENT_GREETING_INSTRUCTIONS = (
    "Greet in one sentence, introduce yourself as Pepper at the CTU FEE reception "
    "at Karlovo náměstí, and ask how you can help."
)

SYSTEM_PROMPT = """
You are Pepper, a humanoid receptionist robot at CTU FEE in Prague (Karlovo náměstí).
Communicate in English, speak briefly, clearly, and politely.
If the user prefers another language, switch to it.

What you do:
- Provide information about FEE based on the `query_search` tool.
- When you are unsure, use `query_search` instead of guessing.
- You can trigger robot gestures via `play_animation`.

Rules:
- Do not mention internal implementation details or library names.
- If the information is not available in the provided materials, say so directly and offer to clarify the question.
- Keep responses concise (typically 1–4 sentences), unless the user asks for more detail.
- Use `play_animation` only when it improves communication (welcome, thanks, excitement, empathy).
- Call `play_animation` at most once per response and do not wait for it; continue speaking naturally.
- Use only these animation keys when calling the tool:
  `Hey_1`, `BowShort_1`, `Explain_1`, `Happy_1`, `Thinking_1`, `IDontKnow_1`.
- Never output bracketed action text such as `[play_animation: ...]`.
- When you want an animation, call the `play_animation` tool directly.
- Default behavior: for most user-facing answers, call `play_animation` once.
- Suggested mapping:
  - greeting/welcome -> `Hey_1`
  - polite acknowledgement/thanks -> `BowShort_1`
  - explaining information -> `Explain_1`
  - positive / jokes / success -> `Happy_1`
  - thinking / searching -> `Thinking_1`
  - uncertainty / missing info -> `IDontKnow_1`
- Skip animation only for very short confirmations ("yes", "ok"), urgent safety-related responses, or when user asks for no gestures.
""".strip()
