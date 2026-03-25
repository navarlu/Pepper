"""Web scraper for fetching and cleaning content from cvut.cz pages."""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

from .config import BASE_DIR

logger = logging.getLogger("dev-console")

SCRAPED_DIR = BASE_DIR / "data" / "scraped"

# Request settings.
REQUEST_TIMEOUT = 15
REQUEST_HEADERS = {
    "User-Agent": "PepperBot/1.0 (CTU FEL thesis project)",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en,cs;q=0.9",
}


def _url_to_filename(url: str) -> str:
    """Convert a URL into a safe, readable filename."""
    parsed = urlparse(url)
    # e.g. "fel.cvut.cz/en/faculty/contacts" -> "fel.cvut.cz__en__faculty__contacts"
    path = parsed.path.strip("/").replace("/", "__")
    host = parsed.hostname or "unknown"
    name = f"{host}__{path}" if path else host
    # Sanitize
    name = re.sub(r"[^\w\-.]", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name[:120] + ".txt"


def _extract_text(html: str, url: str) -> str:
    """Extract clean readable text from HTML, stripping nav/footer/boilerplate."""
    soup = BeautifulSoup(html, "html.parser")

    # Remove noisy elements.
    for tag in soup.find_all(["script", "style", "nav", "footer", "header",
                              "noscript", "aside", "iframe", "svg"]):
        tag.decompose()

    # Try to find main content area.
    main = (
        soup.find("main")
        or soup.find("article")
        or soup.find("div", class_=re.compile(r"content|main|body", re.I))
        or soup.find("div", id=re.compile(r"content|main|body", re.I))
    )
    target = main or soup.body or soup

    # Extract text with some structure preserved.
    lines: list[str] = []
    for el in target.descendants:
        if el.name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            text = el.get_text(strip=True)
            if text:
                lines.append(f"\n{'#' * int(el.name[1])} {text}\n")
        elif el.name == "li":
            text = el.get_text(strip=True)
            if text:
                lines.append(f"- {text}")
        elif el.name in ("p", "div", "td", "th", "dt", "dd"):
            text = el.get_text(" ", strip=True)
            if text and len(text) > 2:
                lines.append(text)

    # Deduplicate consecutive identical lines.
    deduped: list[str] = []
    for line in lines:
        if not deduped or line != deduped[-1]:
            deduped.append(line)

    raw = "\n".join(deduped)
    # Collapse excessive blank lines.
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


def fetch_page(url: str) -> dict[str, Any]:
    """Fetch a single URL and return extracted text + metadata."""
    logger.info("scraping url=%s", url)
    try:
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.error("scrape_failed url=%s error=%s", url, exc)
        return {"url": url, "ok": False, "error": str(exc), "text": ""}

    text = _extract_text(resp.text, url)
    logger.info("scrape_ok url=%s chars=%d", url, len(text))
    return {"url": url, "ok": True, "error": None, "text": text, "status_code": resp.status_code}


def scrape_urls(urls: list[str], category: str = "general", delay: float = 1.0) -> list[dict[str, Any]]:
    """
    Scrape a list of URLs and save extracted text as .txt files.

    Files are saved to: dev-console/data/scraped/<category>/<filename>.txt
    Each file has a header with source URL and scrape timestamp.

    Returns list of results with ok/error status per URL.
    """
    out_dir = SCRAPED_DIR / category
    out_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for i, url in enumerate(urls):
        result = fetch_page(url)

        if result["ok"] and result["text"]:
            filename = _url_to_filename(url)
            filepath = out_dir / filename
            content = (
                f"Source: {url}\n"
                f"Scraped: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"Category: {category}\n"
                f"---\n\n"
                f"{result['text']}\n"
            )
            filepath.write_text(content, encoding="utf-8")
            result["saved_to"] = str(filepath)
            logger.info("saved scraped content to %s (%d chars)", filepath.name, len(result["text"]))

        results.append(result)

        # Be polite — don't hammer the server.
        if i < len(urls) - 1:
            time.sleep(delay)

    return results


def load_scraped_files(category: str | None = None) -> list[dict[str, str]]:
    """
    Load previously scraped .txt files, ready for Weaviate ingestion.

    Returns list of dicts with: title, content, source (URL).
    """
    if category:
        search_dir = SCRAPED_DIR / category
    else:
        search_dir = SCRAPED_DIR

    if not search_dir.exists():
        return []

    items = []
    for txt_file in sorted(search_dir.rglob("*.txt")):
        raw = txt_file.read_text(encoding="utf-8")

        # Parse header.
        source_url = ""
        body = raw
        if "---" in raw:
            header, _, body = raw.partition("---")
            for line in header.splitlines():
                if line.startswith("Source:"):
                    source_url = line.split(":", 1)[1].strip()

        body = body.strip()
        if not body:
            continue

        # Use filename (without extension) as title, cleaned up.
        title = txt_file.stem.replace("__", " / ").replace("_", " ")

        items.append({
            "title": title,
            "content": body,
            "source": source_url,
        })

    return items
