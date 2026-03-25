"""
Benchmark runner for RAG retrieval quality.

Runs a set of test queries against Weaviate via the dev-console API,
checks if expected keywords appear in the top-k results, and reports
per-query and aggregate metrics.

All queries are logged to dev-console history with source="benchmark".

Usage:
    uv run python dev-console/run_benchmark.py dev-console/benchmark_contacts.json

Options:
    --limit N       Number of results per query (default: 5)
    --alpha F       Hybrid search alpha (default: 0.7)
    --mode MODE     Search mode: hybrid, vector, keyword (default: hybrid)
    --top-k N       Check expected keywords in top-k results (default: 3)

Output:
    - Per-query pass/fail with details
    - Aggregate hit rate and MRR
    - Results saved to dev-console/data/benchmark_results/<name>_<timestamp>.json
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path
from urllib.request import Request, urlopen

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BASE_DIR / "data" / "benchmark_results"
DEV_CONSOLE_URL = "http://localhost:8788"


def query_weaviate(
    query: str,
    collection: str,
    limit: int = 5,
    alpha: float = 0.7,
    mode: str = "hybrid",
) -> dict:
    """Run a search query via dev-console API."""
    payload = json.dumps({
        "query": query,
        "collection": collection,
        "limit": limit,
        "alpha": alpha,
        "mode": mode,
        "source": "benchmark",
    }).encode()
    req = Request(
        f"{DEV_CONSOLE_URL}/api/query",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    resp = urlopen(req, timeout=15)
    return json.loads(resp.read())


def check_keywords_in_results(results: list[dict], expected_keywords: list[str], top_k: int) -> dict:
    """Check if expected keywords appear in the top-k results' content or title."""
    top_results = results[:top_k]
    combined_text = " ".join(
        (r.get("title", "") + " " + r.get("content", "")).lower()
        for r in top_results
    )

    found = []
    missing = []
    for kw in expected_keywords:
        if kw.lower() in combined_text:
            found.append(kw)
        else:
            missing.append(kw)

    return {
        "all_found": len(missing) == 0,
        "found": found,
        "missing": missing,
        "hit_rate": len(found) / len(expected_keywords) if expected_keywords else 1.0,
    }


def check_source_in_results(results: list[dict], expected_source: str, top_k: int) -> bool:
    """Check if at least one top-k result's source contains the expected string."""
    if not expected_source:
        return True
    for r in results[:top_k]:
        if expected_source.lower() in r.get("source", "").lower():
            return True
    return False


def find_first_relevant_rank(results: list[dict], expected_keywords: list[str]) -> int | None:
    """Find the rank (1-indexed) of the first result containing all keywords."""
    for i, r in enumerate(results):
        text = (r.get("title", "") + " " + r.get("content", "")).lower()
        if all(kw.lower() in text for kw in expected_keywords):
            return i + 1
    return None


def run_benchmark(
    benchmark_path: str,
    limit: int = 5,
    alpha: float = 0.7,
    mode: str = "hybrid",
    top_k: int = 3,
) -> dict:
    """Run all test cases and return results."""
    with open(benchmark_path) as f:
        benchmark = json.load(f)

    collection = benchmark["collection"]
    test_cases = benchmark["test_cases"]
    logger.info(
        "Running benchmark '%s': %d test cases, collection=%s, mode=%s, alpha=%.2f, limit=%d, top_k=%d",
        benchmark["name"], len(test_cases), collection, mode, alpha, limit, top_k,
    )

    results = []
    total_passed = 0
    total_source_ok = 0
    reciprocal_ranks = []

    for i, tc in enumerate(test_cases):
        tc_id = tc["id"]
        query = tc["query"]
        expected_kw = tc.get("expected_keywords", [])
        expected_src = tc.get("expected_source_contains", "")

        logger.info("[%d/%d] %s: %s", i + 1, len(test_cases), tc_id, query)

        try:
            resp = query_weaviate(query, collection, limit=limit, alpha=alpha, mode=mode)
            search_results = resp.get("results", [])
            duration_ms = resp.get("duration_ms", 0)
        except Exception as exc:
            logger.error("  Query failed: %s", exc)
            results.append({"id": tc_id, "query": query, "error": str(exc), "passed": False})
            continue

        kw_check = check_keywords_in_results(search_results, expected_kw, top_k)
        src_ok = check_source_in_results(search_results, expected_src, top_k)
        rank = find_first_relevant_rank(search_results, expected_kw)
        passed = kw_check["all_found"]

        if passed:
            total_passed += 1
        if src_ok:
            total_source_ok += 1
        if rank is not None:
            reciprocal_ranks.append(1.0 / rank)

        status = "PASS" if passed else "FAIL"
        logger.info("  %s (keywords: %d/%d, source_ok=%s, rank=%s, %.0fms)",
                     status, len(kw_check["found"]), len(expected_kw), src_ok, rank, duration_ms)
        if kw_check["missing"]:
            logger.info("  Missing: %s", kw_check["missing"])

        results.append({
            "id": tc_id,
            "query": query,
            "passed": passed,
            "keywords_found": kw_check["found"],
            "keywords_missing": kw_check["missing"],
            "keyword_hit_rate": kw_check["hit_rate"],
            "source_ok": src_ok,
            "first_relevant_rank": rank,
            "result_count": len(search_results),
            "duration_ms": duration_ms,
            "top_results": [
                {"title": r.get("title", ""), "source": r.get("source", ""), "distance": r.get("distance")}
                for r in search_results[:top_k]
            ],
        })

    # Aggregate metrics.
    n = len(test_cases)
    mrr = sum(reciprocal_ranks) / n if n else 0
    summary = {
        "benchmark_name": benchmark["name"],
        "collection": collection,
        "params": {"mode": mode, "alpha": alpha, "limit": limit, "top_k": top_k},
        "total_cases": n,
        "passed": total_passed,
        "failed": n - total_passed,
        "pass_rate": total_passed / n if n else 0,
        "source_accuracy": total_source_ok / n if n else 0,
        "mrr": mrr,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "results": results,
    }

    # Save results.
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    out_path = RESULTS_DIR / f"{benchmark['name']}_{ts}.json"
    with open(out_path, "w") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False)
    logger.info("Results saved to %s", out_path)

    # Print summary.
    print("\n" + "=" * 60)
    print(f"  Benchmark: {benchmark['name']}")
    print(f"  Collection: {collection}")
    print(f"  Mode: {mode}, Alpha: {alpha}, Limit: {limit}, Top-K: {top_k}")
    print(f"  Pass rate: {total_passed}/{n} ({summary['pass_rate']:.0%})")
    print(f"  Source accuracy: {total_source_ok}/{n} ({summary['source_accuracy']:.0%})")
    print(f"  MRR: {mrr:.3f}")
    print("=" * 60)

    if summary["failed"] > 0:
        print("\nFailed cases:")
        for r in results:
            if not r.get("passed"):
                print(f"  - {r['id']}: {r['query']}")
                if r.get("keywords_missing"):
                    print(f"    Missing keywords: {r['keywords_missing']}")

    return summary


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run RAG retrieval benchmark")
    parser.add_argument("benchmark_file", help="Path to benchmark JSON file")
    parser.add_argument("--limit", type=int, default=5, help="Results per query")
    parser.add_argument("--alpha", type=float, default=0.7, help="Hybrid search alpha")
    parser.add_argument("--mode", default="hybrid", choices=["hybrid", "vector", "keyword"])
    parser.add_argument("--top-k", type=int, default=3, help="Check keywords in top-k results")
    parser.add_argument("--console-url", default=DEV_CONSOLE_URL)
    args = parser.parse_args()

    DEV_CONSOLE_URL = args.console_url
    run_benchmark(args.benchmark_file, limit=args.limit, alpha=args.alpha, mode=args.mode, top_k=args.top_k)
