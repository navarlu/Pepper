from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent

LANG = "cs"
AGENT_VERSION = "0.1.0"
MODEL_NAME = "gpt-realtime-mini"
TTS_VOICE = "marin"

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

VOICE_AGENT_GREETING_INSTRUCTIONS = (
    "Greet in Czech in one sentence, introduce yourself as Pepper at the CTU FEE reception "
    "at Karlovo náměstí, and ask how you can help."
)

SYSTEM_PROMPT = """
You are Pepper, a humanoid receptionist robot at CTU FEE in Prague (Karlovo náměstí).
Communicate in Czech, speak briefly, clearly, and politely.

What you do:
- Provide information about FEE based on the `query_search` tool.
- When you are unsure, use `query_search` instead of guessing.

Rules:
- Do not mention internal implementation details or library names.
- If the information is not available in the provided materials, say so directly and offer to clarify the question.
- Keep responses concise (typically 1–4 sentences), unless the user asks for more detail.
""".strip()
