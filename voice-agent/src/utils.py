from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import weaviate
from weaviate.classes.config import Configure, DataType, Property
from weaviate.classes.query import MetadataQuery

from .config import (
    DOC_CONTENT_FIELD,
    DOC_CREATED_AT_FIELD,
    DOC_SOURCE_FIELD,
    DOC_TITLE_FIELD,
    SEED_DATA_PATHS,
    SEED_LOG_PREFIX,
    WEAVIATE_COLLECTION,
    WEAVIATE_GRPC_PORT,
    WEAVIATE_HOST,
    WEAVIATE_HTTP_PORT,
    WEAVIATE_HYBRID_ALPHA,
    WEAVIATE_OPENAI_MODEL,
)


def connect_weaviate():
    return weaviate.connect_to_local(
        host=WEAVIATE_HOST,
        port=WEAVIATE_HTTP_PORT,
        grpc_port=WEAVIATE_GRPC_PORT,
    )


def _get_vector_config():
    return Configure.Vectors.text2vec_openai(
        model=WEAVIATE_OPENAI_MODEL,
        source_properties=[DOC_TITLE_FIELD, DOC_CONTENT_FIELD],
        vectorize_collection_name=False,
    )


def ensure_collection(client) -> bool:
    if client.collections.exists(WEAVIATE_COLLECTION):
        return False

    client.collections.create(
        name=WEAVIATE_COLLECTION,
        properties=[
            Property(name=DOC_TITLE_FIELD, data_type=DataType.TEXT),
            Property(name=DOC_CONTENT_FIELD, data_type=DataType.TEXT),
            Property(name=DOC_SOURCE_FIELD, data_type=DataType.TEXT),
            Property(name=DOC_CREATED_AT_FIELD, data_type=DataType.DATE),
        ],
        vector_config=_get_vector_config(),
    )
    return True


def _iter_seed_texts(paths: list[Path]) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for base in paths:
        path = Path(base)
        candidates = sorted(path.rglob("*.txt")) if path.is_dir() else [path]
        for file_path in candidates:
            if file_path.suffix.lower() != ".txt":
                continue
            try:
                text = file_path.read_text(encoding="utf-8", errors="ignore").strip()
            except OSError:
                continue
            if not text:
                continue
            items.append(
                {
                    "title": file_path.stem,
                    "content": text,
                    "source": str(file_path),
                }
            )
    return items


def seed_collection(client) -> None:
    created = ensure_collection(client)
    if not created:
        return

    print(f"{SEED_LOG_PREFIX} collection created: {WEAVIATE_COLLECTION}")

    items = _iter_seed_texts(SEED_DATA_PATHS)
    if not items:
        print(f"{SEED_LOG_PREFIX} no seed data found")
        return

    collection = client.collections.use(WEAVIATE_COLLECTION)
    created_at = datetime.now(timezone.utc).isoformat()

    for item in items:
        collection.data.insert(
            {
                DOC_TITLE_FIELD: item["title"],
                DOC_CONTENT_FIELD: item["content"],
                DOC_SOURCE_FIELD: item["source"],
                DOC_CREATED_AT_FIELD: created_at,
            }
        )

    print(f"{SEED_LOG_PREFIX} seeded {len(items)} items")


def _format_results(response) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for obj in response.objects:
        props = obj.properties or {}
        created_at = props.get(DOC_CREATED_AT_FIELD, "")
        if isinstance(created_at, datetime):
            created_at = created_at.isoformat()
        results.append(
            {
                "id": str(getattr(obj, "uuid", "")),
                "title": props.get(DOC_TITLE_FIELD, ""),
                "content": props.get(DOC_CONTENT_FIELD, ""),
                "source": props.get(DOC_SOURCE_FIELD, ""),
                "created_at": created_at,
                "distance": getattr(obj.metadata, "distance", None),
                "score": getattr(obj.metadata, "score", None),
            }
        )
    return results


def search_vectors(query: str, limit: int = 5) -> list[dict[str, Any]]:
    with connect_weaviate() as client:
        ensure_collection(client)
        collection = client.collections.use(WEAVIATE_COLLECTION)
        response = collection.query.hybrid(
            query=query,
            query_properties=[DOC_TITLE_FIELD, DOC_CONTENT_FIELD],
            alpha=WEAVIATE_HYBRID_ALPHA,
            limit=limit,
            return_metadata=MetadataQuery(score=True, distance=True),
            return_properties=[
                DOC_TITLE_FIELD,
                DOC_CONTENT_FIELD,
                DOC_SOURCE_FIELD,
                DOC_CREATED_AT_FIELD,
            ],
        )
        return _format_results(response)
