"""Query history storage using SQLite."""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path
from typing import Any

from .config import HISTORY_DB_PATH, WEAVIATE_COLLECTION

logger = logging.getLogger("dev-console")

# Valid source tags for query origin tracking.
VALID_SOURCES = ("live", "editor", "claude", "benchmark", "unknown")


def _ensure_db() -> None:
    HISTORY_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(HISTORY_DB_PATH))
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS query_log (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp    REAL    NOT NULL,
                source       TEXT    NOT NULL DEFAULT 'unknown',
                collection   TEXT    NOT NULL DEFAULT '',
                query        TEXT    NOT NULL,
                mode         TEXT    NOT NULL DEFAULT 'hybrid',
                alpha        REAL    NOT NULL DEFAULT 0.7,
                limit_n      INTEGER NOT NULL DEFAULT 5,
                result_count INTEGER NOT NULL DEFAULT 0,
                results_json TEXT    NOT NULL DEFAULT '[]',
                duration_ms  REAL    NOT NULL DEFAULT 0
            )
        """)
        # Migration: add collection column if missing (existing DBs).
        columns = [row[1] for row in conn.execute("PRAGMA table_info(query_log)").fetchall()]
        if "collection" not in columns:
            conn.execute("ALTER TABLE query_log ADD COLUMN collection TEXT NOT NULL DEFAULT ''")
        conn.commit()
    finally:
        conn.close()


_ensure_db()


def log_query(
    query: str,
    source: str = "unknown",
    mode: str = "hybrid",
    alpha: float = 0.7,
    limit: int = 5,
    result_count: int = 0,
    results: list[dict[str, Any]] | None = None,
    duration_ms: float = 0,
    collection: str = "",
) -> int:
    """Log a query and its results. Returns the log entry ID."""
    if source not in VALID_SOURCES:
        source = "unknown"
    collection = collection or WEAVIATE_COLLECTION
    results_json = json.dumps(results or [], ensure_ascii=False)
    conn = sqlite3.connect(str(HISTORY_DB_PATH))
    try:
        cur = conn.execute(
            """INSERT INTO query_log
               (timestamp, source, collection, query, mode, alpha, limit_n, result_count, results_json, duration_ms)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (time.time(), source, collection, query, mode, alpha, limit, result_count, results_json, duration_ms),
        )
        conn.commit()
        log_id = cur.lastrowid
        logger.info("query_logged id=%d source=%s collection=%s query=%s results=%d", log_id, source, collection, query[:80], result_count)
        return log_id
    finally:
        conn.close()


def get_history(
    limit: int = 50,
    offset: int = 0,
    source: str | None = None,
    collection: str | None = None,
) -> dict[str, Any]:
    """Retrieve query history with optional source and collection filter."""
    conn = sqlite3.connect(str(HISTORY_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        conditions = []
        params: list[Any] = []
        if source and source in VALID_SOURCES:
            conditions.append("source = ?")
            params.append(source)
        if collection:
            conditions.append("LOWER(collection) = LOWER(?)")
            params.append(collection)

        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

        count_row = conn.execute(f"SELECT COUNT(*) as cnt FROM query_log {where}", params).fetchone()
        total = count_row["cnt"] if count_row else 0

        params.extend([limit, offset])
        rows = conn.execute(
            f"SELECT * FROM query_log {where} ORDER BY id DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()

        entries = []
        for row in rows:
            entry = dict(row)
            entry["results"] = json.loads(entry.pop("results_json", "[]"))
            entries.append(entry)

        return {"entries": entries, "total": total, "limit": limit, "offset": offset}
    finally:
        conn.close()


def get_log_entry(log_id: int) -> dict[str, Any] | None:
    """Get a single log entry by ID."""
    conn = sqlite3.connect(str(HISTORY_DB_PATH))
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM query_log WHERE id = ?", (log_id,)).fetchone()
        if not row:
            return None
        entry = dict(row)
        entry["results"] = json.loads(entry.pop("results_json", "[]"))
        return entry
    finally:
        conn.close()


def clear_history(source: str | None = None) -> int:
    """Clear history, optionally filtered by source. Returns deleted count."""
    conn = sqlite3.connect(str(HISTORY_DB_PATH))
    try:
        if source and source in VALID_SOURCES:
            cur = conn.execute("DELETE FROM query_log WHERE source = ?", (source,))
        else:
            cur = conn.execute("DELETE FROM query_log")
        conn.commit()
        deleted = cur.rowcount
        logger.info("history_cleared source=%s deleted=%d", source or "all", deleted)
        return deleted
    finally:
        conn.close()
