"""Dev Console — Weaviate data viewer, query tester, and history log."""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI, File, Form, Query, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

from .config import DEV_CONSOLE_HOST, DEV_CONSOLE_PORT, UPLOAD_DIR, WEAVIATE_COLLECTION
from .file_parser import parse_file
from .history import VALID_SOURCES, clear_history, get_history, get_log_entry, log_query
from .weaviate_client import (
    collection_stats,
    delete_collection,
    delete_document,
    delete_documents_bulk,
    ensure_collection,
    get_document,
    insert_document,
    insert_documents_bulk,
    list_collections,
    list_documents,
    search_documents,
    update_document,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("dev-console")

app = FastAPI(title="Pepper Dev Console", version="0.1.0")

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# API: Collections
# ---------------------------------------------------------------------------

@app.get("/api/collections")
async def api_list_collections():
    return await asyncio.to_thread(list_collections)


@app.post("/api/collections")
async def api_create_collection(body: dict[str, Any]):
    name = str(body.get("name", "")).strip()
    if not name:
        return JSONResponse({"error": "name_required"}, status_code=400)
    created = await asyncio.to_thread(ensure_collection, name)
    return {"ok": True, "created": created, "name": name}


@app.delete("/api/collections/{name}")
async def api_delete_collection(name: str):
    ok = await asyncio.to_thread(delete_collection, name)
    if not ok:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return {"ok": True}


@app.get("/api/stats")
async def api_stats(collection: str | None = Query(default=None)):
    return await asyncio.to_thread(collection_stats, collection)


# ---------------------------------------------------------------------------
# API: Documents CRUD
# ---------------------------------------------------------------------------

@app.get("/api/documents")
async def api_list_documents(limit: int = 100, offset: int = 0, collection: str | None = Query(default=None)):
    return await asyncio.to_thread(list_documents, limit, offset, collection)


@app.get("/api/documents/{doc_id}")
async def api_get_document(doc_id: str, collection: str | None = Query(default=None)):
    doc = await asyncio.to_thread(get_document, doc_id, collection)
    if doc is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return doc


@app.put("/api/documents/{doc_id}")
async def api_update_document(doc_id: str, body: dict[str, Any]):
    ok = await asyncio.to_thread(
        update_document,
        doc_id,
        title=body.get("title"),
        content=body.get("content"),
        source=body.get("source"),
        collection=body.get("collection"),
    )
    if not ok:
        return JSONResponse({"error": "update_failed"}, status_code=400)
    return {"ok": True}


@app.delete("/api/documents/{doc_id}")
async def api_delete_document(doc_id: str, collection: str | None = Query(default=None)):
    ok = await asyncio.to_thread(delete_document, doc_id, collection)
    if not ok:
        return JSONResponse({"error": "delete_failed"}, status_code=400)
    return {"ok": True}


@app.post("/api/documents/delete-bulk")
async def api_delete_bulk(body: dict[str, Any]):
    ids = body.get("ids", [])
    if not ids:
        return JSONResponse({"error": "no_ids"}, status_code=400)
    deleted = await asyncio.to_thread(delete_documents_bulk, ids, body.get("collection"))
    return {"deleted": deleted}


@app.post("/api/documents")
async def api_insert_document(body: dict[str, Any]):
    title = body.get("title", "").strip()
    content = body.get("content", "").strip()
    source = body.get("source", "").strip()
    if not content:
        return JSONResponse({"error": "content_required"}, status_code=400)
    if not title:
        title = "Untitled"
    doc_id = await asyncio.to_thread(insert_document, title, content, source, body.get("collection"))
    return {"ok": True, "id": doc_id}


@app.post("/api/documents/bulk")
async def api_insert_bulk(body: dict[str, Any]):
    items = body.get("items", [])
    collection = body.get("collection")
    if not items:
        return JSONResponse({"error": "no_items"}, status_code=400)
    ids = await asyncio.to_thread(insert_documents_bulk, items, collection)
    return {"ok": True, "ids": ids, "count": len(ids)}


# ---------------------------------------------------------------------------
# API: File upload (PDF / TXT)
# ---------------------------------------------------------------------------

@app.post("/api/upload")
async def api_upload(
    files: list[UploadFile] = File(...),
    collection: str | None = Form(default=None),
):
    """Upload one or more .pdf/.txt files, parse and insert into Weaviate."""
    all_ids = []
    errors = []
    for upload in files:
        filename = upload.filename or "unknown"
        suffix = Path(filename).suffix.lower()
        if suffix not in (".pdf", ".txt", ".md", ".csv"):
            errors.append({"file": filename, "error": f"unsupported type: {suffix}"})
            continue

        dest = UPLOAD_DIR / filename
        content_bytes = await upload.read()
        dest.write_bytes(content_bytes)

        try:
            chunks = parse_file(dest)
        except Exception as exc:
            errors.append({"file": filename, "error": str(exc)})
            continue

        if not chunks:
            errors.append({"file": filename, "error": "no content extracted"})
            continue

        ids = await asyncio.to_thread(insert_documents_bulk, chunks, collection)
        all_ids.extend(ids)
        logger.info("upload_processed file=%s chunks=%d collection=%s", filename, len(ids), collection)

    return {"inserted": len(all_ids), "ids": all_ids, "errors": errors}


# ---------------------------------------------------------------------------
# API: Search / query (with logging)
# ---------------------------------------------------------------------------

@app.post("/api/query")
async def api_query(body: dict[str, Any]):
    """Run a search query and log it. Used by editor, Claude, benchmarks."""
    query = str(body.get("query", "")).strip()
    if not query:
        return JSONResponse({"error": "query_required"}, status_code=400)

    source = body.get("source", "editor")
    mode = body.get("mode", "hybrid")
    alpha = float(body.get("alpha", 0.7))
    limit = int(body.get("limit", 5))
    collection = body.get("collection") or None

    if mode not in ("hybrid", "vector", "keyword"):
        mode = "hybrid"

    t0 = time.monotonic()
    results = await asyncio.to_thread(search_documents, query, limit, alpha, mode, collection)
    duration_ms = (time.monotonic() - t0) * 1000

    log_id = log_query(
        query=query,
        source=source,
        mode=mode,
        alpha=alpha,
        limit=limit,
        result_count=len(results),
        results=results,
        duration_ms=duration_ms,
        collection=collection or WEAVIATE_COLLECTION,
    )

    return {
        "log_id": log_id,
        "query": query,
        "source": source,
        "mode": mode,
        "alpha": alpha,
        "limit": limit,
        "collection": collection or WEAVIATE_COLLECTION,
        "count": len(results),
        "duration_ms": round(duration_ms, 1),
        "results": results,
    }


# ---------------------------------------------------------------------------
# API: Log incoming query from voice-agent (live source)
# ---------------------------------------------------------------------------

@app.post("/api/log-query")
async def api_log_query(body: dict[str, Any]):
    """Called by voice-agent to log a live query without re-running it."""
    log_id = log_query(
        query=body.get("query", ""),
        source=body.get("source", "live"),
        mode=body.get("mode", "hybrid"),
        alpha=float(body.get("alpha", 0.7)),
        limit=int(body.get("limit", 5)),
        result_count=int(body.get("result_count", 0)),
        results=body.get("results"),
        duration_ms=float(body.get("duration_ms", 0)),
        collection=body.get("collection", WEAVIATE_COLLECTION),
    )
    return {"ok": True, "log_id": log_id}


# ---------------------------------------------------------------------------
# API: Query history
# ---------------------------------------------------------------------------

@app.get("/api/history")
async def api_history(
    limit: int = 50,
    offset: int = 0,
    source: str | None = Query(default=None),
    collection: str | None = Query(default=None),
):
    return get_history(limit=limit, offset=offset, source=source, collection=collection)


@app.get("/api/history/{log_id}")
async def api_history_entry(log_id: int):
    entry = get_log_entry(log_id)
    if entry is None:
        return JSONResponse({"error": "not_found"}, status_code=404)
    return entry


@app.delete("/api/history")
async def api_clear_history(source: str | None = Query(default=None)):
    deleted = clear_history(source=source)
    return {"deleted": deleted}


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index():
    return FRONTEND_HTML


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    logger.info("starting dev-console on %s:%s", DEV_CONSOLE_HOST, DEV_CONSOLE_PORT)
    uvicorn.run(app, host=DEV_CONSOLE_HOST, port=DEV_CONSOLE_PORT, log_level="info")


FRONTEND_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Pepper Dev Console</title>
<style>
:root {
  --bg: #f0f2f5; --panel: #fff; --text: #1a2332; --muted: #6b7a8d;
  --accent: #2968d8; --accent-soft: #edf4ff; --accent-deep: #17479f;
  --good: #2a9d5c; --warn: #d4880f; --hot: #c8403e;
  --line: #dce3eb; --shadow: 0 2px 12px rgba(0,0,0,0.08);
  --radius: 8px;
  --src-live: #2a9d5c; --src-editor: #2968d8; --src-claude: #8b5cf6;
  --src-benchmark: #d4880f; --src-unknown: #6b7a8d;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; background: var(--bg); color: var(--text); font-size: 14px; line-height: 1.5; }
.app { max-width: 1200px; margin: 0 auto; padding: 16px; }
.header { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; flex-wrap: wrap; gap: 8px; }
.header h1 { font-size: 22px; font-weight: 700; }
.header-right { display: flex; align-items: center; gap: 10px; }
.header .stats { font-size: 13px; color: var(--muted); }

/* Collection selector */
.col-selector { display: flex; align-items: center; gap: 6px; background: var(--panel); border: 1px solid var(--line); border-radius: var(--radius); padding: 4px 10px; }
.col-selector label { font-size: 11px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; margin: 0; }
.col-selector select { border: none; background: transparent; font-size: 13px; font-weight: 600; color: var(--accent-deep); cursor: pointer; outline: none; padding: 2px 4px; }
.col-selector .col-count { font-size: 11px; color: var(--muted); }
.col-actions { display: flex; gap: 4px; }

/* Tabs */
.tabs { display: flex; gap: 2px; background: var(--line); border-radius: var(--radius); padding: 2px; margin-bottom: 16px; }
.tab { flex: 1; padding: 8px 12px; text-align: center; cursor: pointer; border-radius: 6px; font-weight: 500; font-size: 13px; transition: all 0.15s; background: transparent; border: none; color: var(--muted); }
.tab:hover { color: var(--text); background: rgba(255,255,255,0.5); }
.tab.active { background: var(--panel); color: var(--text); box-shadow: 0 1px 3px rgba(0,0,0,0.08); }

/* Panels */
.panel { display: none; }
.panel.active { display: block; }
.card { background: var(--panel); border-radius: var(--radius); box-shadow: var(--shadow); padding: 16px; margin-bottom: 12px; }
.card h2 { font-size: 16px; margin-bottom: 12px; }

/* Forms */
input, textarea, select { font-family: inherit; font-size: 13px; border: 1px solid var(--line); border-radius: 6px; padding: 8px 10px; width: 100%; outline: none; transition: border 0.15s; }
input:focus, textarea:focus, select:focus { border-color: var(--accent); }
textarea { resize: vertical; min-height: 80px; }
label { font-size: 12px; font-weight: 600; color: var(--muted); text-transform: uppercase; letter-spacing: 0.5px; display: block; margin-bottom: 4px; }
.form-row { display: flex; gap: 8px; margin-bottom: 8px; }
.form-row > * { flex: 1; }
.form-group { margin-bottom: 10px; }

/* Buttons */
button, .btn { font-family: inherit; font-size: 13px; font-weight: 500; border: 1px solid var(--line); border-radius: 6px; padding: 7px 14px; cursor: pointer; transition: all 0.15s; background: var(--panel); color: var(--text); }
button:hover { background: var(--bg); }
.btn-primary { background: var(--accent); color: #fff; border-color: var(--accent); }
.btn-primary:hover { background: var(--accent-deep); }
.btn-danger { background: var(--hot); color: #fff; border-color: var(--hot); }
.btn-danger:hover { opacity: 0.85; }
.btn-sm { padding: 4px 10px; font-size: 12px; }
.actions-cell { display: flex; gap: 4px; align-items: center; }
.btn-icon { display: inline-flex; align-items: center; justify-content: center; width: 30px; height: 30px; padding: 0; border-radius: 6px; font-size: 15px; border: 1px solid var(--line); background: var(--panel); color: var(--muted); cursor: pointer; transition: all 0.15s; }
.btn-icon:hover { background: var(--bg); color: var(--text); }
.btn-icon.danger:hover { background: #fff0f0; color: var(--hot); border-color: var(--hot); }

/* Source badges */
.badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.3px; }
.badge-live { background: #e6f7ed; color: var(--src-live); }
.badge-editor { background: var(--accent-soft); color: var(--src-editor); }
.badge-claude { background: #f0e6ff; color: var(--src-claude); }
.badge-benchmark { background: #fff4e0; color: var(--src-benchmark); }
.badge-unknown { background: #eef0f3; color: var(--src-unknown); }

/* Table */
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--line); }
th { font-size: 11px; text-transform: uppercase; letter-spacing: 0.5px; color: var(--muted); font-weight: 600; }
tr:hover td { background: #f8fafc; }
.mono { font-family: 'SF Mono', Consolas, monospace; font-size: 12px; }
.truncate { max-width: 300px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.clickable { cursor: pointer; color: var(--accent); }
.clickable:hover { text-decoration: underline; }

/* Detail modal */
.modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.4); z-index: 100; justify-content: center; align-items: start; padding-top: 40px; }
.modal-overlay.open { display: flex; }
.modal { background: var(--panel); border-radius: var(--radius); box-shadow: 0 12px 40px rgba(0,0,0,0.15); width: 90%; max-width: 800px; max-height: 85vh; overflow-y: auto; padding: 20px; }
.modal-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.modal-head h2 { font-size: 18px; }
.modal-close { background: none; border: none; font-size: 22px; cursor: pointer; color: var(--muted); padding: 4px 8px; }
.modal-close:hover { color: var(--text); }
.meta-grid { display: grid; grid-template-columns: auto 1fr; gap: 4px 12px; font-size: 13px; margin-bottom: 12px; }
.meta-grid dt { font-weight: 600; color: var(--muted); }
.meta-grid dd { word-break: break-all; }
.content-box { background: #f8fafc; border: 1px solid var(--line); border-radius: 6px; padding: 12px; white-space: pre-wrap; word-break: break-word; font-size: 13px; max-height: 400px; overflow-y: auto; margin-bottom: 12px; }

/* Upload area */
.upload-zone { border: 2px dashed var(--line); border-radius: var(--radius); padding: 24px; text-align: center; cursor: pointer; transition: border-color 0.15s; margin-bottom: 12px; }
.upload-zone:hover, .upload-zone.dragover { border-color: var(--accent); background: var(--accent-soft); }
.upload-zone input { display: none; }

/* Filter bar */
.filter-bar { display: flex; gap: 6px; margin-bottom: 12px; flex-wrap: wrap; align-items: center; }
.filter-bar .badge { cursor: pointer; opacity: 0.5; transition: opacity 0.15s; }
.filter-bar .badge.active { opacity: 1; }

/* Results list */
.result-item { background: #f8fafc; border: 1px solid var(--line); border-radius: 6px; padding: 10px; margin-bottom: 6px; }
.result-item .result-head { display: flex; justify-content: space-between; font-size: 13px; font-weight: 600; margin-bottom: 4px; }
.result-item .result-score { font-size: 12px; color: var(--muted); font-family: monospace; }
.result-item .result-content { font-size: 12px; color: var(--muted); max-height: 60px; overflow: hidden; }
.result-item .result-content.expanded { max-height: none; }

/* Search bar for data viewer */
.search-bar { display: flex; gap: 8px; margin-bottom: 12px; }
.search-bar input { flex: 1; }

/* Pagination */
.pagination { display: flex; gap: 6px; justify-content: center; align-items: center; margin-top: 10px; font-size: 13px; }

/* Toast */
.toast { position: fixed; bottom: 20px; right: 20px; background: var(--text); color: #fff; padding: 10px 16px; border-radius: 6px; font-size: 13px; z-index: 200; opacity: 0; transition: opacity 0.3s; pointer-events: none; }
.toast.show { opacity: 1; }

.toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }
.toolbar .spacer { flex: 1; }

input[type="checkbox"] { width: auto; }

/* New collection modal input */
.inline-form { display: flex; gap: 6px; align-items: center; }
.inline-form input { width: 200px; }
</style>
</head>
<body>
<div class="app">
  <div class="header">
    <h1>Pepper Dev Console</h1>
    <div class="header-right">
      <div class="col-selector">
        <label>Collection</label>
        <select id="collectionSelect" onchange="onCollectionChange()"></select>
        <span class="col-count" id="colCount"></span>
        <div class="col-actions">
          <button class="btn-icon" onclick="showNewCollectionModal()" title="Create collection">&#43;</button>
          <button class="btn-icon danger" onclick="deleteCurrentCollection()" title="Delete collection">&#128465;</button>
        </div>
      </div>
      <div class="stats" id="statsBar"></div>
    </div>
  </div>

  <div class="tabs">
    <button class="tab active" data-tab="data">Data Viewer</button>
    <button class="tab" data-tab="query">Query Tester</button>
    <button class="tab" data-tab="history">Query Log</button>
  </div>

  <!-- ====================== DATA VIEWER ====================== -->
  <div class="panel active" id="panel-data">
    <div class="card">
      <div class="toolbar">
        <h2>Documents</h2>
        <div class="spacer"></div>
        <button class="btn-sm" onclick="refreshDocs()">Refresh</button>
        <button class="btn-sm btn-primary" onclick="showAddDocModal()">+ Add Document</button>
        <button class="btn-sm btn-danger" id="bulkDeleteBtn" style="display:none" onclick="bulkDelete()">Delete Selected</button>
      </div>
      <div class="search-bar">
        <input type="text" id="docSearchInput" placeholder="Filter documents by title, source, or content..." oninput="filterDocs()">
      </div>
      <div id="docTableWrap">
        <table>
          <thead>
            <tr>
              <th style="width:30px"><input type="checkbox" id="selectAll" onchange="toggleSelectAll()"></th>
              <th>Title</th>
              <th>Source</th>
              <th style="width:180px">Content Preview</th>
              <th style="width:80px">Actions</th>
            </tr>
          </thead>
          <tbody id="docTableBody"></tbody>
        </table>
        <div class="pagination" id="docPagination"></div>
      </div>
    </div>

    <!-- Upload -->
    <div class="card">
      <h2>Upload Files</h2>
      <div class="upload-zone" id="uploadZone" onclick="document.getElementById('fileInput').click()">
        <input type="file" id="fileInput" multiple accept=".pdf,.txt,.md,.csv" onchange="handleUpload(event)">
        <p>Drop .pdf or .txt files here, or click to browse</p>
        <p style="font-size:12px;color:var(--muted);margin-top:4px">Uploads to: <strong id="uploadCollectionLabel"></strong></p>
      </div>
      <div id="uploadStatus"></div>
    </div>
  </div>

  <!-- ====================== QUERY TESTER ====================== -->
  <div class="panel" id="panel-query">
    <div class="card">
      <h2>Query Tester</h2>
      <div class="form-group">
        <label>Query</label>
        <textarea id="queryInput" rows="2" placeholder="Enter your search query..."></textarea>
      </div>
      <div class="form-row">
        <div>
          <label>Mode</label>
          <select id="queryMode">
            <option value="hybrid" selected>Hybrid</option>
            <option value="vector">Vector (semantic)</option>
            <option value="keyword">Keyword (BM25)</option>
          </select>
        </div>
        <div>
          <label>Limit</label>
          <input type="number" id="queryLimit" value="5" min="1" max="20">
        </div>
        <div>
          <label>Alpha (hybrid ratio)</label>
          <input type="number" id="queryAlpha" value="0.7" min="0" max="1" step="0.05">
        </div>
        <div>
          <label>Source tag</label>
          <select id="querySource">
            <option value="editor" selected>Editor</option>
            <option value="claude">Claude</option>
            <option value="benchmark">Benchmark</option>
          </select>
        </div>
      </div>
      <div style="margin-bottom:8px;font-size:12px;color:var(--muted)">Querying collection: <strong id="queryCollectionLabel"></strong></div>
      <button class="btn-primary" onclick="runQuery()">Run Query</button>
    </div>
    <div id="queryResultsWrap"></div>
  </div>

  <!-- ====================== QUERY LOG ====================== -->
  <div class="panel" id="panel-history">
    <div class="card">
      <div class="toolbar">
        <h2>Query Log</h2>
        <div class="spacer"></div>
        <button class="btn-sm" onclick="refreshHistory()">Refresh</button>
        <button class="btn-sm btn-danger" onclick="clearHistoryAll()">Clear All</button>
      </div>
      <div class="filter-bar" id="historyFilters">
        <span style="font-size:12px;color:var(--muted);margin-right:4px;">Filter:</span>
        <span class="badge badge-live active" data-source="" onclick="toggleHistoryFilter(this,'')">All</span>
        <span class="badge badge-live" data-source="live" onclick="toggleHistoryFilter(this,'live')">Live</span>
        <span class="badge badge-editor" data-source="editor" onclick="toggleHistoryFilter(this,'editor')">Editor</span>
        <span class="badge badge-claude" data-source="claude" onclick="toggleHistoryFilter(this,'claude')">Claude</span>
        <span class="badge badge-benchmark" data-source="benchmark" onclick="toggleHistoryFilter(this,'benchmark')">Benchmark</span>
      </div>
      <div id="historyTableWrap">
        <table>
          <thead>
            <tr>
              <th style="width:60px">Source</th>
              <th>Query</th>
              <th style="width:90px">Collection</th>
              <th style="width:70px">Mode</th>
              <th style="width:55px">Alpha</th>
              <th style="width:55px">Limit</th>
              <th style="width:60px">Results</th>
              <th style="width:65px">Time</th>
              <th style="width:130px">Timestamp</th>
            </tr>
          </thead>
          <tbody id="historyTableBody"></tbody>
        </table>
        <div class="pagination" id="historyPagination"></div>
      </div>
    </div>
  </div>
</div>

<!-- Document detail / edit modal -->
<div class="modal-overlay" id="docModal">
  <div class="modal">
    <div class="modal-head">
      <h2 id="modalTitle">Document</h2>
      <button class="modal-close" onclick="closeModal()">&times;</button>
    </div>
    <div id="modalBody"></div>
  </div>
</div>

<!-- Toast -->
<div class="toast" id="toast"></div>

<script>
const API = '';
let currentCollection = '';
let allCollections = [];
let allDocs = [];
let docPage = 0;
const DOC_PAGE_SIZE = 50;
let historySource = '';
let historyPage = 0;
const HIST_PAGE_SIZE = 50;

// ---- Tabs ----
document.querySelectorAll('.tab').forEach(t => t.addEventListener('click', () => {
  document.querySelectorAll('.tab').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(x => x.classList.remove('active'));
  t.classList.add('active');
  document.getElementById('panel-' + t.dataset.tab).classList.add('active');
}));

// ---- Toast ----
function toast(msg) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  setTimeout(() => el.classList.remove('show'), 2500);
}

// ---- Helpers ----
function esc(s) { return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }
function truncate(s, n) { s = String(s||''); return s.length > n ? s.slice(0, n) + '...' : s; }
function fmtTime(ts) {
  if (!ts) return '-';
  const d = new Date(ts * 1000);
  return d.toLocaleString();
}
function badgeHtml(source) {
  return `<span class="badge badge-${source||'unknown'}">${esc(source||'unknown')}</span>`;
}
async function api(method, url, body) {
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body !== undefined) opts.body = JSON.stringify(body);
  const res = await fetch(API + url, opts);
  return await res.json();
}
function colParam() {
  return currentCollection ? `collection=${encodeURIComponent(currentCollection)}` : '';
}
function updateCollectionLabels() {
  const name = currentCollection || '(default)';
  document.getElementById('uploadCollectionLabel').textContent = name;
  document.getElementById('queryCollectionLabel').textContent = name;
}

// ====================== COLLECTIONS ======================
async function refreshCollections() {
  allCollections = await api('GET', '/api/collections');
  const select = document.getElementById('collectionSelect');
  const prev = currentCollection;
  select.innerHTML = allCollections.map(c =>
    `<option value="${esc(c.name)}" ${c.name === prev ? 'selected' : ''}>${esc(c.name)} (${c.total})</option>`
  ).join('');
  if (allCollections.length && !allCollections.find(c => c.name === prev)) {
    currentCollection = allCollections[0].name;
    select.value = currentCollection;
  }
  updateCollectionLabels();
}

function onCollectionChange() {
  currentCollection = document.getElementById('collectionSelect').value;
  updateCollectionLabels();
  refreshDocs();
  refreshStats();
  refreshHistory();
}

async function showNewCollectionModal() {
  const name = prompt('New collection name:');
  if (!name || !name.trim()) return;
  await api('POST', '/api/collections', { name: name.trim() });
  toast(`Collection "${name.trim()}" created`);
  currentCollection = name.trim();
  await refreshCollections();
  onCollectionChange();
}

async function deleteCurrentCollection() {
  if (!currentCollection) return;
  if (!confirm(`Delete entire collection "${currentCollection}" and all its documents?`)) return;
  await api('DELETE', `/api/collections/${encodeURIComponent(currentCollection)}`);
  toast(`Collection "${currentCollection}" deleted`);
  currentCollection = '';
  await refreshCollections();
  onCollectionChange();
}

// ====================== STATS ======================
async function refreshStats() {
  const data = await api('GET', `/api/stats?${colParam()}`);
  document.getElementById('statsBar').textContent =
    data.exists ? `${data.total} documents` : 'Collection not found';
}

// ====================== DATA VIEWER ======================
async function refreshDocs() {
  const data = await api('GET', `/api/documents?limit=500&offset=0&${colParam()}`);
  allDocs = data.documents || [];
  docPage = 0;
  filterDocs();
  refreshStats();
}

function filterDocs() {
  const q = (document.getElementById('docSearchInput').value || '').toLowerCase();
  const filtered = q
    ? allDocs.filter(d => (d.title||'').toLowerCase().includes(q) || (d.source||'').toLowerCase().includes(q) || (d.content||'').toLowerCase().includes(q))
    : allDocs;
  renderDocTable(filtered);
}

function renderDocTable(docs) {
  const start = docPage * DOC_PAGE_SIZE;
  const page = docs.slice(start, start + DOC_PAGE_SIZE);
  const tbody = document.getElementById('docTableBody');
  tbody.innerHTML = page.map(d => `
    <tr>
      <td><input type="checkbox" class="doc-check" data-id="${esc(d.id)}"></td>
      <td class="clickable" onclick="viewDoc('${esc(d.id)}')">${esc(d.title)}</td>
      <td class="mono" style="font-size:11px">${esc(truncate(d.source, 40))}</td>
      <td class="truncate" style="font-size:12px;color:var(--muted)">${esc(truncate(d.content, 80))}</td>
      <td class="actions-cell">
        <button class="btn-icon" onclick="viewDoc('${esc(d.id)}')" title="View / Edit">&#9998;</button>
        <button class="btn-icon danger" onclick="deleteDoc('${esc(d.id)}')" title="Delete">&#128465;</button>
      </td>
    </tr>
  `).join('');

  const totalPages = Math.ceil(docs.length / DOC_PAGE_SIZE);
  document.getElementById('docPagination').innerHTML = totalPages > 1
    ? `<button class="btn-sm" ${docPage===0?'disabled':''} onclick="docPage--;filterDocs()">Prev</button>
       <span>${docPage+1} / ${totalPages}</span>
       <button class="btn-sm" ${docPage>=totalPages-1?'disabled':''} onclick="docPage++;filterDocs()">Next</button>`
    : `<span style="color:var(--muted)">${docs.length} documents</span>`;

  updateBulkBtn();
}

function toggleSelectAll() {
  const checked = document.getElementById('selectAll').checked;
  document.querySelectorAll('.doc-check').forEach(cb => cb.checked = checked);
  updateBulkBtn();
}

document.addEventListener('change', e => { if (e.target.classList.contains('doc-check')) updateBulkBtn(); });

function updateBulkBtn() {
  const count = document.querySelectorAll('.doc-check:checked').length;
  const btn = document.getElementById('bulkDeleteBtn');
  btn.style.display = count > 0 ? '' : 'none';
  btn.textContent = `Delete Selected (${count})`;
}

async function deleteDoc(id) {
  if (!confirm('Delete this document?')) return;
  await api('DELETE', `/api/documents/${id}?${colParam()}`);
  toast('Document deleted');
  refreshDocs();
}

async function bulkDelete() {
  const ids = [...document.querySelectorAll('.doc-check:checked')].map(cb => cb.dataset.id);
  if (!ids.length) return;
  if (!confirm(`Delete ${ids.length} documents?`)) return;
  await api('POST', '/api/documents/delete-bulk', { ids, collection: currentCollection });
  toast(`Deleted ${ids.length} documents`);
  refreshDocs();
}

async function viewDoc(id) {
  const doc = await api('GET', `/api/documents/${id}?${colParam()}`);
  if (doc.error) { toast('Document not found'); return; }
  document.getElementById('modalTitle').textContent = doc.title || 'Document';
  document.getElementById('modalBody').innerHTML = `
    <dl class="meta-grid">
      <dt>ID</dt><dd class="mono">${esc(doc.id)}</dd>
      <dt>Collection</dt><dd class="mono">${esc(currentCollection)}</dd>
      <dt>Source</dt><dd>${esc(doc.source)}</dd>
      <dt>Created</dt><dd>${esc(doc.created_at)}</dd>
      <dt>Score</dt><dd>${doc.score != null ? doc.score : '-'}</dd>
      <dt>Distance</dt><dd>${doc.distance != null ? doc.distance : '-'}</dd>
    </dl>
    <label>Title</label>
    <input type="text" id="editTitle" value="${esc(doc.title)}">
    <div style="margin-top:8px">
      <label>Source</label>
      <input type="text" id="editSource" value="${esc(doc.source)}">
    </div>
    <div style="margin-top:8px">
      <label>Content</label>
      <textarea id="editContent" rows="12">${esc(doc.content)}</textarea>
    </div>
    <div style="margin-top:10px;display:flex;gap:8px;">
      <button class="btn-primary" onclick="saveDoc('${esc(doc.id)}')">Save Changes</button>
      <button class="btn-danger" onclick="deleteDoc('${esc(doc.id)}');closeModal()">Delete</button>
      <button onclick="closeModal()">Cancel</button>
    </div>
  `;
  document.getElementById('docModal').classList.add('open');
}

async function saveDoc(id) {
  const title = document.getElementById('editTitle').value;
  const content = document.getElementById('editContent').value;
  const source = document.getElementById('editSource').value;
  await api('PUT', `/api/documents/${id}`, { title, content, source, collection: currentCollection });
  toast('Document updated (re-embedding triggered)');
  closeModal();
  refreshDocs();
}

function showAddDocModal() {
  document.getElementById('modalTitle').textContent = 'Add Document';
  document.getElementById('modalBody').innerHTML = `
    <p style="font-size:12px;color:var(--muted);margin-bottom:10px;">Adding to collection: <strong>${esc(currentCollection)}</strong></p>
    <div class="form-group">
      <label>Title</label>
      <input type="text" id="newDocTitle" placeholder="Document title">
    </div>
    <div class="form-group">
      <label>Source</label>
      <input type="text" id="newDocSource" placeholder="e.g. manual-entry">
    </div>
    <div class="form-group">
      <label>Content</label>
      <textarea id="newDocContent" rows="10" placeholder="Paste document content here..."></textarea>
    </div>
    <div style="display:flex;gap:8px;">
      <button class="btn-primary" onclick="addDoc()">Insert</button>
      <button onclick="closeModal()">Cancel</button>
    </div>
  `;
  document.getElementById('docModal').classList.add('open');
}

async function addDoc() {
  const title = document.getElementById('newDocTitle').value.trim();
  const content = document.getElementById('newDocContent').value.trim();
  const source = document.getElementById('newDocSource').value.trim();
  if (!content) { toast('Content is required'); return; }
  await api('POST', '/api/documents', { title: title || 'Untitled', content, source: source || 'manual-entry', collection: currentCollection });
  toast('Document inserted');
  closeModal();
  refreshDocs();
}

function closeModal() { document.getElementById('docModal').classList.remove('open'); }
document.getElementById('docModal').addEventListener('click', e => { if (e.target === e.currentTarget) closeModal(); });

// ---- Upload ----
const uploadZone = document.getElementById('uploadZone');
uploadZone.addEventListener('dragover', e => { e.preventDefault(); uploadZone.classList.add('dragover'); });
uploadZone.addEventListener('dragleave', () => uploadZone.classList.remove('dragover'));
uploadZone.addEventListener('drop', e => {
  e.preventDefault();
  uploadZone.classList.remove('dragover');
  if (e.dataTransfer.files.length) doUpload(e.dataTransfer.files);
});

function handleUpload(e) { if (e.target.files.length) doUpload(e.target.files); }

async function doUpload(files) {
  const status = document.getElementById('uploadStatus');
  status.innerHTML = `<p style="color:var(--muted)">Uploading ${files.length} file(s) to ${esc(currentCollection)}...</p>`;
  const form = new FormData();
  for (const f of files) form.append('files', f);
  form.append('collection', currentCollection);
  try {
    const res = await fetch(API + '/api/upload', { method: 'POST', body: form });
    const data = await res.json();
    let html = `<p style="color:var(--good)">Inserted ${data.inserted} chunk(s) into ${esc(currentCollection)}</p>`;
    if (data.errors && data.errors.length) {
      html += data.errors.map(e => `<p style="color:var(--hot)">${esc(e.file)}: ${esc(e.error)}</p>`).join('');
    }
    status.innerHTML = html;
    refreshDocs();
    refreshCollections();
  } catch (err) {
    status.innerHTML = `<p style="color:var(--hot)">Upload failed: ${esc(err)}</p>`;
  }
}

// ====================== QUERY TESTER ======================
async function runQuery() {
  const query = document.getElementById('queryInput').value.trim();
  if (!query) { toast('Enter a query'); return; }
  const mode = document.getElementById('queryMode').value;
  const limit = parseInt(document.getElementById('queryLimit').value) || 5;
  const alpha = parseFloat(document.getElementById('queryAlpha').value) || 0.7;
  const source = document.getElementById('querySource').value;

  document.getElementById('queryResultsWrap').innerHTML = '<div class="card"><p style="color:var(--muted)">Running query...</p></div>';

  const data = await api('POST', '/api/query', { query, mode, limit, alpha, source, collection: currentCollection });

  let html = `<div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;flex-wrap:wrap;gap:6px;">
      <h2>${data.count} result(s)</h2>
      <span class="mono" style="font-size:12px;color:var(--muted)">${data.duration_ms}ms | ${badgeHtml(data.source)} | ${esc(data.collection)} | log #${data.log_id}</span>
    </div>`;

  if (data.results && data.results.length) {
    data.results.forEach((r, i) => {
      html += `<div class="result-item">
        <div class="result-head">
          <span class="clickable" onclick="viewDoc('${esc(r.id)}')">${esc(r.title)}</span>
          <span class="result-score">score: ${r.score != null ? r.score.toFixed(4) : '-'} | dist: ${r.distance != null ? r.distance.toFixed(4) : '-'}</span>
        </div>
        <div class="result-content" id="rc-${i}" onclick="this.classList.toggle('expanded')">${esc(r.content)}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:4px">source: ${esc(r.source)} | id: ${esc(r.id)}</div>
      </div>`;
    });
  } else {
    html += '<p style="color:var(--muted)">No results found.</p>';
  }
  html += '</div>';
  document.getElementById('queryResultsWrap').innerHTML = html;
}

// ====================== QUERY LOG ======================
async function refreshHistory() {
  const colFilter = currentCollection ? `&collection=${encodeURIComponent(currentCollection)}` : '';
  const data = await api('GET', `/api/history?limit=${HIST_PAGE_SIZE}&offset=${historyPage * HIST_PAGE_SIZE}${historySource ? '&source=' + historySource : ''}${colFilter}`);
  const entries = data.entries || [];
  const tbody = document.getElementById('historyTableBody');
  tbody.innerHTML = entries.map(e => `
    <tr class="clickable" onclick="viewHistoryEntry(${e.id})">
      <td>${badgeHtml(e.source)}</td>
      <td>${esc(truncate(e.query, 70))}</td>
      <td class="mono" style="font-size:11px">${esc(e.collection || '-')}</td>
      <td class="mono">${esc(e.mode)}</td>
      <td class="mono">${e.alpha}</td>
      <td class="mono">${e.limit_n}</td>
      <td class="mono">${e.result_count}</td>
      <td class="mono">${e.duration_ms ? e.duration_ms.toFixed(0) + 'ms' : '-'}</td>
      <td style="font-size:12px">${fmtTime(e.timestamp)}</td>
    </tr>
  `).join('') || '<tr><td colspan="9" style="color:var(--muted);text-align:center">No entries</td></tr>';

  const totalPages = Math.ceil((data.total || 0) / HIST_PAGE_SIZE);
  document.getElementById('historyPagination').innerHTML = totalPages > 1
    ? `<button class="btn-sm" ${historyPage===0?'disabled':''} onclick="historyPage--;refreshHistory()">Prev</button>
       <span>${historyPage+1} / ${totalPages}</span>
       <button class="btn-sm" ${historyPage>=totalPages-1?'disabled':''} onclick="historyPage++;refreshHistory()">Next</button>`
    : `<span style="color:var(--muted)">${data.total || 0} entries</span>`;
}

function toggleHistoryFilter(el, source) {
  historySource = source;
  historyPage = 0;
  document.querySelectorAll('#historyFilters .badge').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
  refreshHistory();
}

async function viewHistoryEntry(id) {
  const e = await api('GET', `/api/history/${id}`);
  if (e.error) { toast('Entry not found'); return; }
  document.getElementById('modalTitle').textContent = `Query Log #${e.id}`;
  let html = `
    <dl class="meta-grid">
      <dt>Source</dt><dd>${badgeHtml(e.source)}</dd>
      <dt>Collection</dt><dd class="mono">${esc(e.collection || '-')}</dd>
      <dt>Query</dt><dd>${esc(e.query)}</dd>
      <dt>Mode</dt><dd>${esc(e.mode)}</dd>
      <dt>Alpha</dt><dd>${e.alpha}</dd>
      <dt>Limit</dt><dd>${e.limit_n}</dd>
      <dt>Results</dt><dd>${e.result_count}</dd>
      <dt>Duration</dt><dd>${e.duration_ms ? e.duration_ms.toFixed(1) + 'ms' : '-'}</dd>
      <dt>Timestamp</dt><dd>${fmtTime(e.timestamp)}</dd>
    </dl>
    <h3 style="margin:12px 0 8px;font-size:14px;">Results</h3>`;
  if (e.results && e.results.length) {
    e.results.forEach(r => {
      html += `<div class="result-item">
        <div class="result-head">
          <span class="clickable" onclick="viewDoc('${esc(r.id)}')">${esc(r.title)}</span>
          <span class="result-score">score: ${r.score != null ? Number(r.score).toFixed(4) : '-'}</span>
        </div>
        <div class="result-content" onclick="this.classList.toggle('expanded')">${esc(r.content)}</div>
        <div style="font-size:11px;color:var(--muted);margin-top:4px">source: ${esc(r.source)}</div>
      </div>`;
    });
  } else {
    html += '<p style="color:var(--muted)">No results.</p>';
  }
  document.getElementById('modalBody').innerHTML = html;
  document.getElementById('docModal').classList.add('open');
}

async function clearHistoryAll() {
  if (!confirm('Clear all query history?')) return;
  await api('DELETE', '/api/history');
  toast('History cleared');
  refreshHistory();
}

// ---- Init ----
async function init() {
  await refreshCollections();
  if (allCollections.length) {
    currentCollection = allCollections[0].name;
    document.getElementById('collectionSelect').value = currentCollection;
  }
  updateCollectionLabels();
  refreshStats();
  refreshDocs();
  refreshHistory();
}
init();
</script>
</body>
</html>"""


if __name__ == "__main__":
    main()
