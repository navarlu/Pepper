"""Parse uploaded files (.txt, .pdf) into document chunks."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger("dev-console")


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


def parse_file(file_path: Path) -> list[dict[str, str]]:
    """Auto-detect file type and parse into chunks."""
    suffix = file_path.suffix.lower()
    if suffix == ".pdf":
        return parse_pdf(file_path)
    elif suffix in (".txt", ".md", ".csv"):
        return parse_txt(file_path)
    else:
        logger.warning("unsupported_file_type ext=%s file=%s", suffix, file_path.name)
        return []
