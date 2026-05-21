"""Smoke-test the query_search RAG path end-to-end.

Seeds the runtime Weaviate collection from `SEED_DATA_PATHS` (PDFs + txt
chunked at `CHUNK_MAX_CHARS` / `CHUNK_OVERLAP_CHARS`) and runs three
sample queries against it, printing top-K hits with title + score.

Pre-requisites:
  - docker-weaviate-1 running with text2vec-openai enabled
    (OPENAI_APIKEY exported in its env)
  - voice-agent/src/data/FEL/ populated with the corpus PDFs

Run from the project root:
  .venv/bin/python voice-agent/scripts/smoke_query_search.py
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from live.rag import connect_weaviate, search_vectors, seed_collection


QUERIES = [
    "How do I apply for a scholarship?",
    "What are the rules for dormitory accommodation?",
    "How can I appeal a disciplinary decision?",
]
TOP_K = 3


def main() -> None:
    with connect_weaviate() as client:
        seed_collection(client)

    for query in QUERIES:
        print("\n" + "=" * 70)
        print(f"QUERY: {query}")
        print("=" * 70)
        hits = search_vectors(query, limit=TOP_K)
        if not hits:
            print("  (no hits)")
            continue
        for rank, hit in enumerate(hits, start=1):
            title = hit.get("title", "")
            score = hit.get("score")
            snippet = (hit.get("content") or "").strip().replace("\n", " ")
            if len(snippet) > 220:
                snippet = snippet[:220] + "..."
            print(f"\n  [{rank}] score={score} title={title}")
            print(f"      {snippet}")


if __name__ == "__main__":
    main()
