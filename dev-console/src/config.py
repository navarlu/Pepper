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


# Weaviate
WEAVIATE_HOST = _env_str("WEAVIATE_HOST", "localhost")
WEAVIATE_HTTP_PORT = _env_int("WEAVIATE_HTTP_PORT", 8080)
WEAVIATE_GRPC_PORT = _env_int("WEAVIATE_GRPC_PORT", 50051)
WEAVIATE_COLLECTION = "fel_v007"
WEAVIATE_OPENAI_MODEL = "text-embedding-3-large"

DOC_TITLE_FIELD = "title"
DOC_CONTENT_FIELD = "content"
DOC_SOURCE_FIELD = "source"
DOC_CREATED_AT_FIELD = "created_at"

# Dev console
DEV_CONSOLE_HOST = _env_str("DEV_CONSOLE_HOST", "0.0.0.0")
DEV_CONSOLE_PORT = _env_int("DEV_CONSOLE_PORT", 8788)

# Query history DB
HISTORY_DB_PATH = BASE_DIR / "data" / "query_history.db"

# Upload temp dir
UPLOAD_DIR = BASE_DIR / "data" / "uploads"
