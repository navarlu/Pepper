"""
Scrape contact/people pages from cvut.cz and save as clean .txt files.

This is Phase 1 of data ingestion — contacts, offices, faculty management,
and department "how to find us" pages relevant to the Pepper receptionist
at Karlovo náměstí.

Usage:
    uv run python dev-console/scrape_contacts.py

Output:
    dev-console/data/scraped/contacts/*.txt
    Each file contains a metadata header (source URL, scrape date, category)
    followed by the extracted text content.

After scraping, review the files manually, then run:
    uv run python dev-console/ingest_scraped.py --category contacts --collection fel_v007
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
SCRAPED_DIR = BASE_DIR / "data" / "scraped"

REQUEST_TIMEOUT = 15
REQUEST_HEADERS = {
    "User-Agent": "PepperBot/1.0 (CTU FEL thesis project)",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en,cs;q=0.9",
}

# ---------------------------------------------------------------------------
# Phase 1: Contacts & People
# From deep-research-report.md sections:
#   - Core receptionist facts
#   - Department-specific "How to find us"
# ---------------------------------------------------------------------------
CONTACT_URLS = [
    # Faculty-wide contacts and offices
    "https://fel.cvut.cz/en/faculty/contacts",
    "https://fel.cvut.cz/en/faculty/faculty-structure/administrative-offices",
    "https://fel.cvut.cz/en/faculty/faculty-structure/administrative-offices/study-office",
    "https://fel.cvut.cz/en/admissions/admission-procedures/contact",
    # Department contacts at Karlovo náměstí
    "https://control.fel.cvut.cz/en/management-and-contacts",
    "https://cyber.felk.cvut.cz/department/contacts/",
    "https://dcgi.fel.cvut.cz/en/contacts/",
    "https://cmp.felk.cvut.cz/new_pages/contacts/",
]


def url_to_filename(url: str) -> str:
    """Convert a URL into a safe, readable filename."""
    parsed = urlparse(url)
    path = parsed.path.strip("/").replace("/", "__")
    host = parsed.hostname or "unknown"
    name = f"{host}__{path}" if path else host
    name = re.sub(r"[^\w\-.]", "_", name)
    name = re.sub(r"_+", "_", name).strip("_")
    return name[:120] + ".txt"


def extract_text(html: str) -> str:
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
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


def scrape_and_save(urls: list[str], category: str, delay: float = 1.5) -> None:
    """Scrape URLs and save extracted text as .txt files."""
    out_dir = SCRAPED_DIR / category
    out_dir.mkdir(parents=True, exist_ok=True)

    ok_count = 0
    fail_count = 0

    for i, url in enumerate(urls):
        logger.info("[%d/%d] Fetching %s", i + 1, len(urls), url)
        try:
            resp = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except requests.RequestException as exc:
            logger.error("  FAILED: %s", exc)
            fail_count += 1
            continue

        text = extract_text(resp.text)
        if not text:
            logger.warning("  Empty content after extraction, skipping")
            fail_count += 1
            continue

        filename = url_to_filename(url)
        filepath = out_dir / filename
        content = (
            f"Source: {url}\n"
            f"Scraped: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
            f"Category: {category}\n"
            f"---\n\n"
            f"{text}\n"
        )
        filepath.write_text(content, encoding="utf-8")
        logger.info("  Saved %s (%d chars)", filename, len(text))
        ok_count += 1

        if i < len(urls) - 1:
            time.sleep(delay)

    logger.info("Done: %d OK, %d failed, files in %s", ok_count, fail_count, out_dir)


if __name__ == "__main__":
    scrape_and_save(CONTACT_URLS, category="contacts")
