"""
Ingest scraped .txt files into a Weaviate collection via the dev-console API.

Reads files from dev-console/data/scraped/<category>/ and inserts each as a
document into the specified Weaviate collection. Uses the dev-console HTTP API
so Weaviate connection details are handled by the running service.

Usage:
    uv run python dev-console/ingest_scraped.py --category contacts --collection fel_v007

Prerequisites:
    - dev-console service must be running (localhost:8788)
    - Target collection must exist (create via dev-console UI or API)
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
from urllib.request import Request, urlopen

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
SCRAPED_DIR = BASE_DIR / "data" / "scraped"
DEV_CONSOLE_URL = "http://localhost:8788"


def load_scraped_files(category: str) -> list[dict[str, str]]:
    """Load .txt files from scraped directory, parsing the metadata header."""
    search_dir = SCRAPED_DIR / category
    if not search_dir.exists():
        logger.error("Directory not found: %s", search_dir)
        return []

    items = []
    for txt_file in sorted(search_dir.glob("*.txt")):
        raw = txt_file.read_text(encoding="utf-8")

        source_url = ""
        body = raw
        if "---" in raw:
            header, _, body = raw.partition("---")
            for line in header.splitlines():
                if line.startswith("Source:"):
                    source_url = line.split(":", 1)[1].strip()

        body = body.strip()
        if not body:
            logger.warning("Skipping empty file: %s", txt_file.name)
            continue

        # Use filename (cleaned) as title.
        title = txt_file.stem.replace("__", " / ").replace("_", " ")

        items.append({
            "title": title,
            "content": body,
            "source": source_url,
        })
        logger.info("Loaded %s (%d chars, source=%s)", txt_file.name, len(body), source_url)

    return items


def ingest_to_weaviate(items: list[dict[str, str]], collection: str) -> None:
    """Insert documents into Weaviate via the dev-console bulk API."""
    url = f"{DEV_CONSOLE_URL}/api/documents/bulk?collection={collection}"
    payload = json.dumps({"items": items}).encode()
    req = Request(url, data=payload, method="POST",
                  headers={"Content-Type": "application/json"})

    logger.info("Inserting %d documents into collection '%s' ...", len(items), collection)
    try:
        resp = urlopen(req, timeout=30)
        result = json.loads(resp.read())
        ids = result.get("ids", [])
        logger.info("Inserted %d documents. IDs: %s", len(ids), ids)
    except Exception as exc:
        logger.error("Ingestion failed: %s", exc)
        raise


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest scraped files into Weaviate")
    parser.add_argument("--category", required=True, help="Scraped category folder name (e.g. 'contacts')")
    parser.add_argument("--collection", required=True, help="Weaviate collection name (e.g. 'fel_v007')")
    parser.add_argument("--console-url", default=DEV_CONSOLE_URL, help="Dev console base URL")
    args = parser.parse_args()

    DEV_CONSOLE_URL = args.console_url
    items = load_scraped_files(args.category)
    if not items:
        logger.error("No files to ingest. Run scrape_contacts.py first.")
        raise SystemExit(1)

    logger.info("Loaded %d documents from category '%s'", len(items), args.category)
    ingest_to_weaviate(items, args.collection)
