"""Weaviate RAG client for the FEL knowledge base.

The `query_search` tool calls `search_vectors()` to do a hybrid
(vector + BM25) lookup over the `WEAVIATE_COLLECTION` collection.
On first run (empty collection) `seed_collection()` ingests `.txt`
and `.pdf` files under `SEED_DATA_PATHS`, splitting them into
overlapping character chunks (`CHUNK_MAX_CHARS` / `CHUNK_OVERLAP_CHARS`).
Vectors are produced server-side by Weaviate's `text2vec-openai`
module using the embedding model configured in `voice-agent/src/config.py`.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pypdf import PdfReader
import weaviate
from weaviate.classes.config import Configure, DataType, Property
from weaviate.classes.query import MetadataQuery

from .config import (
    CHUNK_MAX_CHARS,
    CHUNK_OVERLAP_CHARS,
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
    """Open a Weaviate client using the host/ports from config.

    Caller is responsible for closing it — use as `with connect_weaviate() as client`.
    """
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
    """Create `WEAVIATE_COLLECTION` with the doc schema if missing.

    Returns True if the collection was just created (caller should
    seed it), False if it already existed.
    """
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


def _extract_pdf_text(pdf_path: Path) -> str:
    reader = PdfReader(str(pdf_path))
    pages: list[str] = []
    for page in reader.pages:
        page_text = page.extract_text() or ""
        if page_text.strip():
            pages.append(page_text.strip())
    return "\n\n".join(pages).strip()


def _split_long_text(text: str, max_chars: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    chunks: list[str] = []
    current_words: list[str] = []
    for word in words:
        candidate = " ".join(current_words + [word]).strip()
        if current_words and len(candidate) > max_chars:
            chunks.append(" ".join(current_words).strip())
            current_words = [word]
        else:
            current_words.append(word)
    if current_words:
        chunks.append(" ".join(current_words).strip())
    return [chunk for chunk in chunks if chunk]


def _tail_for_overlap(text: str, overlap_chars: int) -> str:
    if overlap_chars <= 0:
        return ""
    if len(text) <= overlap_chars:
        return text.strip()
    return text[-overlap_chars:].strip()


def _split_text_into_chunks(
    text: str,
    max_chars: int = CHUNK_MAX_CHARS,
    overlap_chars: int = CHUNK_OVERLAP_CHARS,
) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    if overlap_chars >= max_chars:
        overlap_chars = max(0, max_chars // 10)

    pieces: list[str] = []
    for paragraph in (part.strip() for part in text.split("\n\n")):
        if not paragraph:
            continue
        if len(paragraph) <= max_chars:
            pieces.append(paragraph)
        else:
            pieces.extend(_split_long_text(paragraph, max_chars=max_chars))
    if not pieces:
        return []

    chunks: list[str] = []
    current = ""
    for piece in pieces:
        candidate = f"{current}\n\n{piece}".strip() if current else piece
        if current and len(candidate) > max_chars:
            chunks.append(current.strip())
            overlap = _tail_for_overlap(current, overlap_chars)
            current = f"{overlap}\n\n{piece}".strip() if overlap else piece
            while len(current) > max_chars:
                split_parts = _split_long_text(current, max_chars=max_chars)
                if len(split_parts) <= 1:
                    break
                chunks.append(split_parts[0].strip())
                overlap = _tail_for_overlap(split_parts[0], overlap_chars)
                current = (
                    f"{overlap}\n\n{split_parts[1]}".strip()
                    if overlap
                    else split_parts[1]
                )
        else:
            current = candidate
    if current.strip():
        chunks.append(current.strip())
    return chunks


def _iter_seed_texts(paths: list[Path]) -> list[dict[str, str]]:
    """Walk seed paths, returning one record per chunk for `.txt` and `.pdf` files."""
    items: list[dict[str, str]] = []
    supported_suffixes = {".txt", ".pdf"}

    candidates: list[Path] = []
    for base in paths:
        base_path = Path(base)
        if base_path.is_dir():
            candidates.extend(sorted(base_path.rglob("*")))
        elif base_path.is_file():
            candidates.append(base_path)

    for file_path in candidates:
        suffix = file_path.suffix.lower()
        if suffix not in supported_suffixes:
            continue
        try:
            if suffix == ".pdf":
                text = _extract_pdf_text(file_path)
            else:
                text = file_path.read_text(encoding="utf-8", errors="ignore").strip()
        except OSError:
            continue
        if not text:
            continue
        chunks = _split_text_into_chunks(text)
        if not chunks:
            continue
        total = len(chunks)
        for index, chunk in enumerate(chunks, start=1):
            items.append(
                {
                    "title": f"{file_path.stem} (chunk {index}/{total})",
                    "content": chunk,
                    "source": f"{file_path}#chunk={index}",
                }
            )
    return items


def seed_collection(client) -> None:
    """Create the collection + ingest every `.txt` under `SEED_DATA_PATHS`.

    No-op if the collection already exists — seeding only runs on
    first boot (or after wiping the Weaviate volume).
    """
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


def search_vectors(query: str, limit: int = 5, alpha: float | None = None) -> list[dict[str, Any]]:
    """Run a hybrid search against the knowledge base.

    `alpha` is the vector-vs-keyword balance (0 = pure BM25, 1 = pure
    vector). Defaults to `WEAVIATE_HYBRID_ALPHA` from config. Returns
    a list of result dicts with id/title/content/source/created_at/
    distance/score.
    """
    effective_alpha = alpha if alpha is not None else WEAVIATE_HYBRID_ALPHA
    with connect_weaviate() as client:
        ensure_collection(client)
        collection = client.collections.use(WEAVIATE_COLLECTION)
        response = collection.query.hybrid(
            query=query,
            query_properties=[DOC_TITLE_FIELD, DOC_CONTENT_FIELD],
            alpha=effective_alpha,
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
