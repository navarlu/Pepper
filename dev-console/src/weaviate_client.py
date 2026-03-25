"""Weaviate connection and CRUD operations."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any

import weaviate
from weaviate.classes.config import Configure, DataType, Property
from weaviate.classes.query import MetadataQuery

from .config import (
    DOC_CONTENT_FIELD,
    DOC_CREATED_AT_FIELD,
    DOC_SOURCE_FIELD,
    DOC_TITLE_FIELD,
    WEAVIATE_COLLECTION,
    WEAVIATE_GRPC_PORT,
    WEAVIATE_HOST,
    WEAVIATE_HTTP_PORT,
    WEAVIATE_OPENAI_MODEL,
)

logger = logging.getLogger("dev-console")

ALL_PROPERTIES = [DOC_TITLE_FIELD, DOC_CONTENT_FIELD, DOC_SOURCE_FIELD, DOC_CREATED_AT_FIELD]


def _col(collection: str | None) -> str:
    """Resolve collection name, falling back to default."""
    return (collection or "").strip() or WEAVIATE_COLLECTION


@contextmanager
def _client():
    c = weaviate.connect_to_local(
        host=WEAVIATE_HOST,
        port=WEAVIATE_HTTP_PORT,
        grpc_port=WEAVIATE_GRPC_PORT,
    )
    try:
        yield c
    finally:
        c.close()


def _get_vector_config():
    return Configure.Vectors.text2vec_openai(
        model=WEAVIATE_OPENAI_MODEL,
        source_properties=[DOC_TITLE_FIELD, DOC_CONTENT_FIELD],
        vectorize_collection_name=False,
    )


def list_collections() -> list[dict[str, Any]]:
    """List all collections in Weaviate with doc counts."""
    with _client() as client:
        result = []
        all_cols = client.collections.list_all()
        for name in sorted(all_cols.keys()):
            try:
                col = client.collections.use(name)
                total = col.aggregate.over_all(total_count=True).total_count
            except Exception:
                total = 0
            result.append({"name": name, "total": total})
        return result


def ensure_collection(collection: str | None = None) -> bool:
    name = _col(collection)
    with _client() as client:
        if client.collections.exists(name):
            return False
        client.collections.create(
            name=name,
            properties=[
                Property(name=DOC_TITLE_FIELD, data_type=DataType.TEXT),
                Property(name=DOC_CONTENT_FIELD, data_type=DataType.TEXT),
                Property(name=DOC_SOURCE_FIELD, data_type=DataType.TEXT),
                Property(name=DOC_CREATED_AT_FIELD, data_type=DataType.DATE),
            ],
            vector_config=_get_vector_config(),
        )
        logger.info("created collection %s", name)
        return True


def delete_collection(collection: str) -> bool:
    """Delete an entire collection."""
    name = _col(collection)
    with _client() as client:
        if not client.collections.exists(name):
            return False
        client.collections.delete(name)
        logger.info("deleted collection %s", name)
        return True


def _serialize_obj(obj: Any) -> dict[str, Any]:
    props = obj.properties or {}
    created_at = props.get(DOC_CREATED_AT_FIELD, "")
    if isinstance(created_at, datetime):
        created_at = created_at.isoformat()
    return {
        "id": str(getattr(obj, "uuid", "")),
        "title": props.get(DOC_TITLE_FIELD, ""),
        "content": props.get(DOC_CONTENT_FIELD, ""),
        "source": props.get(DOC_SOURCE_FIELD, ""),
        "created_at": created_at,
        "distance": getattr(obj.metadata, "distance", None) if obj.metadata else None,
        "score": getattr(obj.metadata, "score", None) if obj.metadata else None,
    }


def list_documents(limit: int = 100, offset: int = 0, collection: str | None = None) -> dict[str, Any]:
    """List all documents with pagination."""
    name = _col(collection)
    with _client() as client:
        if not client.collections.exists(name):
            return {"documents": [], "total": 0, "limit": limit, "offset": offset, "collection": name}
        col = client.collections.use(name)
        response = col.query.fetch_objects(
            limit=limit,
            offset=offset,
            return_properties=ALL_PROPERTIES,
            return_metadata=MetadataQuery(creation_time=True),
        )
        total = col.aggregate.over_all(total_count=True).total_count
        return {
            "documents": [_serialize_obj(obj) for obj in response.objects],
            "total": total,
            "limit": limit,
            "offset": offset,
            "collection": name,
        }


def get_document(doc_id: str, collection: str | None = None) -> dict[str, Any] | None:
    """Get a single document by UUID with full content and metadata."""
    name = _col(collection)
    with _client() as client:
        if not client.collections.exists(name):
            return None
        col = client.collections.use(name)
        try:
            obj = col.query.fetch_object_by_id(
                doc_id,
                return_properties=ALL_PROPERTIES,
            )
            if obj is None:
                return None
            return _serialize_obj(obj)
        except Exception:
            logger.exception("get_document failed id=%s collection=%s", doc_id, name)
            return None


def search_documents(
    query: str,
    limit: int = 10,
    alpha: float = 0.7,
    mode: str = "hybrid",
    collection: str | None = None,
) -> list[dict[str, Any]]:
    """Search documents with configurable mode and params."""
    name = _col(collection)
    with _client() as client:
        if not client.collections.exists(name):
            return []
        col = client.collections.use(name)
        metadata = MetadataQuery(score=True, distance=True)

        if mode == "vector":
            response = col.query.near_text(
                query=query,
                limit=limit,
                return_metadata=metadata,
                return_properties=ALL_PROPERTIES,
            )
        elif mode == "keyword":
            response = col.query.bm25(
                query=query,
                query_properties=[DOC_TITLE_FIELD, DOC_CONTENT_FIELD],
                limit=limit,
                return_metadata=metadata,
                return_properties=ALL_PROPERTIES,
            )
        else:
            response = col.query.hybrid(
                query=query,
                query_properties=[DOC_TITLE_FIELD, DOC_CONTENT_FIELD],
                alpha=alpha,
                limit=limit,
                return_metadata=metadata,
                return_properties=ALL_PROPERTIES,
            )

        return [_serialize_obj(obj) for obj in response.objects]


def delete_document(doc_id: str, collection: str | None = None) -> bool:
    """Delete a document by UUID."""
    name = _col(collection)
    with _client() as client:
        if not client.collections.exists(name):
            return False
        col = client.collections.use(name)
        try:
            col.data.delete_by_id(doc_id)
            logger.info("deleted document id=%s collection=%s", doc_id, name)
            return True
        except Exception:
            logger.exception("delete_document failed id=%s collection=%s", doc_id, name)
            return False


def delete_documents_bulk(doc_ids: list[str], collection: str | None = None) -> int:
    """Delete multiple documents. Returns count of successfully deleted."""
    name = _col(collection)
    deleted = 0
    with _client() as client:
        if not client.collections.exists(name):
            return 0
        col = client.collections.use(name)
        for doc_id in doc_ids:
            try:
                col.data.delete_by_id(doc_id)
                deleted += 1
            except Exception:
                logger.warning("bulk delete failed for id=%s", doc_id)
    logger.info("bulk_delete collection=%s requested=%d deleted=%d", name, len(doc_ids), deleted)
    return deleted


def update_document(
    doc_id: str,
    title: str | None = None,
    content: str | None = None,
    source: str | None = None,
    collection: str | None = None,
) -> bool:
    """Update document fields. Only non-None fields are updated. Triggers re-embedding."""
    name = _col(collection)
    with _client() as client:
        if not client.collections.exists(name):
            return False
        col = client.collections.use(name)
        try:
            updates: dict[str, Any] = {}
            if title is not None:
                updates[DOC_TITLE_FIELD] = title
            if content is not None:
                updates[DOC_CONTENT_FIELD] = content
            if source is not None:
                updates[DOC_SOURCE_FIELD] = source
            if not updates:
                return False
            col.data.update(uuid=doc_id, properties=updates)
            logger.info("updated document id=%s collection=%s fields=%s", doc_id, name, list(updates.keys()))
            return True
        except Exception:
            logger.exception("update_document failed id=%s collection=%s", doc_id, name)
            return False


def insert_document(title: str, content: str, source: str = "", collection: str | None = None) -> str:
    """Insert a new document. Returns the new UUID."""
    name = _col(collection)
    with _client() as client:
        ensure_collection(name)
        col = client.collections.use(name)
        created_at = datetime.now(timezone.utc).isoformat()
        uuid = col.data.insert({
            DOC_TITLE_FIELD: title,
            DOC_CONTENT_FIELD: content,
            DOC_SOURCE_FIELD: source,
            DOC_CREATED_AT_FIELD: created_at,
        })
        logger.info("inserted document id=%s collection=%s title=%s", uuid, name, title)
        return str(uuid)


def insert_documents_bulk(items: list[dict[str, str]], collection: str | None = None) -> list[str]:
    """Insert multiple documents. Returns list of UUIDs."""
    name = _col(collection)
    ids = []
    with _client() as client:
        ensure_collection(name)
        col = client.collections.use(name)
        created_at = datetime.now(timezone.utc).isoformat()
        for item in items:
            uuid = col.data.insert({
                DOC_TITLE_FIELD: item.get("title", ""),
                DOC_CONTENT_FIELD: item.get("content", ""),
                DOC_SOURCE_FIELD: item.get("source", ""),
                DOC_CREATED_AT_FIELD: created_at,
            })
            ids.append(str(uuid))
    logger.info("bulk_insert collection=%s count=%d", name, len(ids))
    return ids


def collection_stats(collection: str | None = None) -> dict[str, Any]:
    """Get collection statistics."""
    name = _col(collection)
    with _client() as client:
        if not client.collections.exists(name):
            return {"exists": False, "collection": name, "total": 0}
        col = client.collections.use(name)
        total = col.aggregate.over_all(total_count=True).total_count
        return {
            "exists": True,
            "collection": name,
            "total": total,
        }
