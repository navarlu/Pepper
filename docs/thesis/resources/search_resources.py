from __future__ import annotations

import sys
from datetime import datetime

import weaviate
from weaviate.classes.query import MetadataQuery

WEAVIATE_HOST = "localhost"
WEAVIATE_HTTP_PORT = 8080
WEAVIATE_GRPC_PORT = 50051
WEAVIATE_COLLECTION = "resources_v001"

TITLE_FIELD = "title"
CONTENT_FIELD = "content"
SOURCE_FIELD = "source"
INGESTED_AT_FIELD = "ingested_at"

HYBRID_ALPHA = 0.7
RESULT_LIMIT = 5
CONTENT_PREVIEW_CHARS = 400


def connect_weaviate():
    return weaviate.connect_to_local(
        host=WEAVIATE_HOST,
        port=WEAVIATE_HTTP_PORT,
        grpc_port=WEAVIATE_GRPC_PORT,
    )


def format_created_at(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    if value is None:
        return ""
    return str(value)


def trim_content(text: str, max_chars: int = CONTENT_PREVIEW_CHARS) -> str:
    clean = " ".join(text.split())
    if len(clean) <= max_chars:
        return clean
    return f"{clean[:max_chars].rstrip()}..."


def run_search(query: str) -> None:
    with connect_weaviate() as client:
        if not client.collections.exists(WEAVIATE_COLLECTION):
            print(f"Collection does not exist: {WEAVIATE_COLLECTION}")
            return

        collection = client.collections.use(WEAVIATE_COLLECTION)
        response = collection.query.hybrid(
            query=query,
            query_properties=[TITLE_FIELD, CONTENT_FIELD],
            alpha=HYBRID_ALPHA,
            limit=RESULT_LIMIT,
            return_metadata=MetadataQuery(score=True, distance=True),
            return_properties=[TITLE_FIELD, CONTENT_FIELD, SOURCE_FIELD, INGESTED_AT_FIELD],
        )

    if not response.objects:
        print("No results.")
        return

    print(f"Results for: {query}")
    print("")
    for index, obj in enumerate(response.objects, start=1):
        props = obj.properties or {}
        title = str(props.get(TITLE_FIELD, ""))
        source = str(props.get(SOURCE_FIELD, ""))
        content = str(props.get(CONTENT_FIELD, ""))
        created_at = format_created_at(props.get(INGESTED_AT_FIELD))
        score = getattr(obj.metadata, "score", None)
        distance = getattr(obj.metadata, "distance", None)

        print(f"{index}. {title}")
        print(f"   source: {source}")
        print(f"   score: {score} | distance: {distance} | ingested_at: {created_at}")
        print(f"   content: {trim_content(content)}")
        print("")


def main() -> None:
    if len(sys.argv) < 2:
        print('Usage: uv run python ../docs/thesis/resources/search_resources.py "your search query"')
        raise SystemExit(1)

    query = " ".join(sys.argv[1:]).strip()
    if not query:
        print("Search query cannot be empty.")
        raise SystemExit(1)

    run_search(query)


if __name__ == "__main__":
    main()
