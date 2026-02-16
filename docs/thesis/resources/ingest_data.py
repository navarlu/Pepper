from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import shutil

from pypdf import PdfReader
import weaviate
from weaviate.classes.config import Configure, DataType, Property

BASE_DIR = Path(__file__).resolve().parent
TO_INGEST_DIR = BASE_DIR / "data" / "to_ingest"
INGESTED_DIR = BASE_DIR / "data" / "ingested"

WEAVIATE_HOST = "localhost"
WEAVIATE_HTTP_PORT = 8080
WEAVIATE_GRPC_PORT = 50051
WEAVIATE_COLLECTION = "resources_v001"
WEAVIATE_OPENAI_MODEL = "text-embedding-3-large"
CHUNK_MAX_CHARS = 3000
CHUNK_OVERLAP_CHARS = 300

TITLE_FIELD = "title"
CONTENT_FIELD = "content"
SOURCE_FIELD = "source"
INGESTED_AT_FIELD = "ingested_at"
PAGE_COUNT_FIELD = "page_count"


def connect_weaviate():
    return weaviate.connect_to_local(
        host=WEAVIATE_HOST,
        port=WEAVIATE_HTTP_PORT,
        grpc_port=WEAVIATE_GRPC_PORT,
    )


def ensure_collection(client) -> None:
    if client.collections.exists(WEAVIATE_COLLECTION):
        return

    client.collections.create(
        name=WEAVIATE_COLLECTION,
        properties=[
            Property(name=TITLE_FIELD, data_type=DataType.TEXT),
            Property(name=CONTENT_FIELD, data_type=DataType.TEXT),
            Property(name=SOURCE_FIELD, data_type=DataType.TEXT),
            Property(name=INGESTED_AT_FIELD, data_type=DataType.DATE),
            Property(name=PAGE_COUNT_FIELD, data_type=DataType.INT),
        ],
        vector_config=Configure.Vectors.text2vec_openai(
            model=WEAVIATE_OPENAI_MODEL,
            source_properties=[TITLE_FIELD, CONTENT_FIELD],
            vectorize_collection_name=False,
        ),
    )
    print(f"Created collection: {WEAVIATE_COLLECTION}")


def extract_pdf_text(pdf_path: Path) -> tuple[str, int]:
    reader = PdfReader(str(pdf_path))
    pages: list[str] = []

    for page in reader.pages:
        page_text = page.extract_text() or ""
        if page_text.strip():
            pages.append(page_text.strip())

    return "\n\n".join(pages).strip(), len(reader.pages)


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


def split_text_into_chunks(
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
                current = f"{overlap}\n\n{split_parts[1]}".strip() if overlap else split_parts[1]
        else:
            current = candidate

    if current.strip():
        chunks.append(current.strip())

    return chunks


def resolve_target_path(src_path: Path) -> Path:
    target = INGESTED_DIR / src_path.name
    if not target.exists():
        return target

    stem = src_path.stem
    suffix = src_path.suffix
    index = 1
    while True:
        candidate = INGESTED_DIR / f"{stem}_{index}{suffix}"
        if not candidate.exists():
            return candidate
        index += 1


def ingest_all_pdfs() -> None:
    TO_INGEST_DIR.mkdir(parents=True, exist_ok=True)
    INGESTED_DIR.mkdir(parents=True, exist_ok=True)

    pdf_files = sorted(path for path in TO_INGEST_DIR.iterdir() if path.suffix.lower() == ".pdf")
    if not pdf_files:
        print(f"No PDFs found in {TO_INGEST_DIR}")
        return

    with connect_weaviate() as client:
        ensure_collection(client)
        collection = client.collections.use(WEAVIATE_COLLECTION)

        success_count = 0
        failure_count = 0

        for pdf_path in pdf_files:
            try:
                text, page_count = extract_pdf_text(pdf_path)
                if not text:
                    raise ValueError("extracted text is empty")
                chunks = split_text_into_chunks(text)
                if not chunks:
                    raise ValueError("no valid chunks produced")

                ingested_at = datetime.now(timezone.utc).isoformat()
                chunk_count = len(chunks)
                for chunk_index, chunk_text in enumerate(chunks, start=1):
                    collection.data.insert(
                        {
                            TITLE_FIELD: f"{pdf_path.stem} (chunk {chunk_index}/{chunk_count})",
                            CONTENT_FIELD: chunk_text,
                            SOURCE_FIELD: f"{pdf_path}#chunk={chunk_index}",
                            INGESTED_AT_FIELD: ingested_at,
                            PAGE_COUNT_FIELD: page_count,
                        }
                    )

                target_path = resolve_target_path(pdf_path)
                shutil.move(str(pdf_path), str(target_path))
                success_count += 1
                print(
                    f"Ingested: {pdf_path.name} ({chunk_count} chunks) -> {target_path.name}"
                )
            except Exception as exc:
                failure_count += 1
                print(f"Failed: {pdf_path.name} ({exc})")

    print(f"Done. success={success_count} failed={failure_count}")


if __name__ == "__main__":
    ingest_all_pdfs()
