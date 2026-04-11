"""Parse uploaded files (.txt, .pdf) into document chunks."""

from __future__ import annotations

import logging
from pathlib import Path

from .config import CHUNK_MAX_CHARS, CHUNK_OVERLAP_CHARS

logger = logging.getLogger("dev-console")


# ---------------------------------------------------------------------------
# Smart chunking helpers (paragraph-aware splitting with overlap)
# ---------------------------------------------------------------------------

def _split_long_text(text: str, max_chars: int) -> list[str]:
    """Split text at word boundaries into pieces of at most *max_chars*."""
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
    """Return the last *overlap_chars* characters of *text*."""
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
    """Split *text* into chunks respecting paragraph boundaries with overlap."""
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


# ---------------------------------------------------------------------------
# File parsers
# ---------------------------------------------------------------------------

def parse_txt(file_path: Path) -> list[dict[str, str]]:
    """Read a .txt file as a single document chunk."""
    text = file_path.read_text(encoding="utf-8", errors="ignore").strip()
    if not text:
        return []
    return [{
        "title": file_path.stem,
        "content": text,
        "source": file_path.name,
    }]


def parse_pdf(file_path: Path) -> list[dict[str, str]]:
    """Extract text from a .pdf, one chunk per page (non-empty pages only)."""
    from pypdf import PdfReader

    reader = PdfReader(str(file_path))
    chunks = []
    for i, page in enumerate(reader.pages):
        text = (page.extract_text() or "").strip()
        if not text:
            continue
        chunks.append({
            "title": f"{file_path.stem} (p.{i + 1})",
            "content": text,
            "source": f"{file_path.name}#page={i + 1}",
        })
    if not chunks:
        logger.warning("pdf_empty file=%s", file_path.name)
    return chunks


def parse_pdf_chunked(file_path: Path) -> list[dict[str, str]]:
    """Extract full PDF text, then smart-chunk with overlap."""
    from pypdf import PdfReader

    reader = PdfReader(str(file_path))
    pages: list[str] = []
    for page in reader.pages:
        page_text = (page.extract_text() or "").strip()
        if page_text:
            pages.append(page_text)

    full_text = "\n\n".join(pages).strip()
    if not full_text:
        logger.warning("pdf_empty file=%s", file_path.name)
        return []

    text_chunks = split_text_into_chunks(full_text)
    total = len(text_chunks)
    return [
        {
            "title": f"{file_path.stem} (chunk {i}/{total})",
            "content": chunk,
            "source": f"{file_path.name}#chunk={i}",
        }
        for i, chunk in enumerate(text_chunks, start=1)
    ]


def parse_file(file_path: Path, chunking: str = "page") -> list[dict[str, str]]:
    """Auto-detect file type and parse into chunks."""
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        if chunking == "smart":
            return parse_pdf_chunked(file_path)
        return parse_pdf(file_path)
    elif suffix in (".txt", ".md", ".csv"):
        return parse_txt(file_path)
    else:
        logger.warning("unsupported_file_type ext=%s file=%s", suffix, file_path.name)
        return []
