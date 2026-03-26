import asyncio
import contextlib
import json
import os
import struct
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import aiohttp
from aiohttp import web
from dotenv import load_dotenv
from livekit import api

try:
    from .config import (
        LISTENER_IDENTITY,
        LIVEKIT_HOST_WS_URL,
        LIVEKIT_HTTP_URL,
        LIVEKIT_ROOM_NAME,
        LIVEKIT_SESSION_FILE,
        LIVEKIT_STATUS_POLL_INTERVAL_SEC,
        LIVEKIT_URL,
        BRIDGE_URL,
        SESSION_ACTIVITY_DEBOUNCE_SEC,
        SESSION_COOLDOWN_SEC,
        SESSION_IDLE_TIMEOUT_SEC,
        SESSION_MANAGER_HOST,
        SESSION_MANAGER_PORT,
        SESSION_PREROLL_ACTIVITY_SEC,
        USER_IDENTITY,
    )
except ImportError:
    from config import (
        LISTENER_IDENTITY,
        LIVEKIT_HOST_WS_URL,
        LIVEKIT_HTTP_URL,
        LIVEKIT_ROOM_NAME,
        LIVEKIT_SESSION_FILE,
        LIVEKIT_STATUS_POLL_INTERVAL_SEC,
        LIVEKIT_URL,
        BRIDGE_URL,
        SESSION_ACTIVITY_DEBOUNCE_SEC,
        SESSION_COOLDOWN_SEC,
        SESSION_IDLE_TIMEOUT_SEC,
        SESSION_MANAGER_HOST,
        SESSION_MANAGER_PORT,
        SESSION_PREROLL_ACTIVITY_SEC,
        USER_IDENTITY,
    )

ROOT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
AGENT_NAME_DEFAULT = "Pepper"
SESSION_SOURCE_USER = "user"
SESSION_SOURCE_AGENT = "agent"
SESSION_MANAGER_STATE_FILE = "session-manager-state.json"
MAX_TRANSCRIPT_ITEMS = 40
COMPONENT_STALE_AFTER_SEC = 12.0
COMPONENT_PROBE_INTERVAL_SEC = 3.0
WARM_AGENT_JOIN_TIMEOUT_SEC = 8.0
DOCKER_SOCKET_PATH = os.getenv("DOCKER_SOCKET_PATH", "/var/run/docker.sock")
DOCKER_LOG_TAIL_LINES = int(os.getenv("DOCKER_LOG_TAIL_LINES", "160"))
KNOWN_DOCKER_SERVICES = (
    "bridge",
    "listener",
    "livekit",
    "redis",
    "safe-startup",
    "session-manager",
    "user-client",
    "voice-agent",
    "weaviate",
)
STATUS_HTML = """<!doctype html>
<meta charset="utf-8">
<title>Pepper Operator Debug</title>
<style>
:root {
  --bg: #eef4fb;
  --bg-soft: #f8fbff;
  --panel: rgba(255,255,255,0.92);
  --panel-strong: rgba(255,255,255,0.98);
  --line: #d8e4f1;
  --line-strong: #c4d6ea;
  --text: #16324a;
  --muted: #667f97;
  --accent: #2968d8;
  --accent-soft: #edf4ff;
  --accent-deep: #17479f;
  --good: #5aa878;
  --good-soft: #ebf8ef;
  --warn: #cb8c2c;
  --hot: #c85757;
  --shadow: 0 18px 40px rgba(40, 74, 111, 0.10);
}
* { box-sizing:border-box; }
html,body {
  margin:0;
  padding:0;
  min-height:100%;
  background:
    radial-gradient(circle at top left, rgba(41,104,216,0.14), transparent 28%),
    radial-gradient(circle at top right, rgba(90,168,120,0.12), transparent 22%),
    linear-gradient(180deg, #f9fbff, #eef4fb 42%, #e9f0f8 100%);
  color:var(--text);
  font-family: "Segoe UI", Arial, sans-serif;
}
body { min-height:100vh; }
.page { max-width: 1540px; margin: 0 auto; padding: 16px 16px 24px; }
.hero {
  display:flex;
  justify-content:space-between;
  align-items:center;
  gap:16px;
  margin-bottom:12px;
}
.hero h1 { margin:0; font-size:32px; line-height:1; letter-spacing:-0.03em; }
.hero p { margin:6px 0 0; color:var(--muted); font-size:14px; }
.hero-meta { display:flex; align-items:center; gap:10px; flex-wrap:wrap; justify-content:flex-end; }
.nav-link {
  display:inline-flex;
  align-items:center;
  padding:8px 12px;
  border-radius:999px;
  text-decoration:none;
  color:var(--accent-deep);
  background:rgba(255,255,255,0.72);
  border:1px solid var(--line);
  font-size:13px;
  font-weight:600;
}
.nav-link.active {
  background:var(--accent-soft);
  border-color:rgba(41,104,216,0.18);
}
.shell {
  display:grid;
  gap:12px;
}
.card {
  background:var(--panel);
  border:1px solid var(--line);
  border-radius:22px;
  padding:16px;
  box-shadow:var(--shadow);
  backdrop-filter: blur(12px);
}
.card.featured {
  background:linear-gradient(180deg, rgba(255,255,255,0.98), rgba(245,250,255,0.95));
}
.label {
  color:var(--muted);
  font-size:12px;
  text-transform:uppercase;
  letter-spacing:0.12em;
  margin-bottom:10px;
}
.value { font-size:32px; font-weight:700; line-height:1.05; }
.value.compact { font-size:24px; }
.subvalue { margin-top:8px; color:var(--muted); font-size:14px; }
.pill {
  display:inline-flex;
  align-items:center;
  gap:8px;
  padding:8px 12px;
  border-radius:999px;
  background:var(--accent-soft);
  color:var(--accent-deep);
  font-size:13px;
  font-weight:600;
  border:1px solid rgba(41,104,216,0.14);
}
.pill::before {
  content:"";
  width:8px;
  height:8px;
  border-radius:999px;
  background:currentColor;
  opacity:0.9;
}
.pill.good {
  background:var(--good-soft);
  color:var(--good);
  border-color: rgba(90,168,120,0.18);
}
.pill.warn {
  background:#fff4e6;
  color:var(--warn);
  border-color: rgba(203,140,44,0.2);
}
.pill.hot {
  background:#fff0f0;
  color:var(--hot);
  border-color: rgba(200,87,87,0.2);
}
.top-strip {
  border:1px solid var(--line);
  border-radius:18px;
  background:rgba(255,255,255,0.84);
  box-shadow: 0 10px 22px rgba(40, 74, 111, 0.06);
  overflow:hidden;
}
.mini-table {
  width:100%;
  min-width:0;
}
.mini-table th,
.mini-table td {
  padding:10px 12px;
  border-bottom:1px solid var(--line);
  font-size:13px;
}
.mini-table th {
  width:18%;
  background:rgba(244,248,253,0.92);
}
.mini-table tr:last-child th,
.mini-table tr:last-child td {
  border-bottom:none;
}
.mini-cell-main {
  font-weight:700;
  font-size:15px;
}
.mini-cell-sub {
  margin-top:2px;
  color:var(--muted);
  font-size:12px;
}
.main-grid {
  display:grid;
  grid-template-columns: minmax(380px, 0.98fr) minmax(0, 1.62fr);
  gap:12px;
  align-items:start;
}
.rail,
.content-stack {
  display:grid;
  gap:12px;
  align-content:start;
}
.chat-card { padding:0; overflow:hidden; }
.chat-header {
  padding:18px 20px 12px;
  border-bottom:1px solid var(--line);
  display:flex;
  justify-content:space-between;
  gap:14px;
  align-items:flex-end;
}
.chat-header-main h2,
.panel-title,
.table-card h3,
.log-title {
  margin:0;
  font-size:20px;
  letter-spacing:-0.02em;
}
.chat-header-main p,
.panel-note,
.table-note {
  margin:6px 0 0;
  color:var(--muted);
  font-size:14px;
}
.chat-feed {
  display:flex;
  flex-direction:column;
  gap:12px;
  min-height:620px;
  max-height:620px;
  overflow:auto;
  padding:18px 20px 20px;
  background:
    linear-gradient(180deg, rgba(244,248,253,0.88), rgba(255,255,255,0.92));
}
.controls-card {
  background:linear-gradient(180deg, rgba(255,255,255,0.98), rgba(245,250,255,0.95));
}
.controls-grid {
  display:grid;
  gap:10px;
}
.controls-header {
  display:flex;
  justify-content:space-between;
  align-items:flex-end;
  gap:12px;
}
.controls-header .panel-note { margin:4px 0 0; }
.bubble {
  max-width:92%;
  border-radius:20px;
  padding:16px 18px;
  line-height:1.48;
  box-shadow: 0 8px 22px rgba(40, 74, 111, 0.06);
}
.bubble.user {
  align-self:flex-end;
  background:#edf4ff;
  border:1px solid rgba(41,104,216,0.10);
}
.bubble.pepper {
  align-self:flex-start;
  background:#ffffff;
  border:1px solid #dbe7f3;
}
.bubble.system {
  align-self:center;
  width:100%;
  max-width:100%;
  background:transparent;
  box-shadow:none;
  border:none;
  padding:4px 0;
}
.bubble .speaker {
  font-size:11px;
  font-weight:700;
  color:var(--muted);
  text-transform:uppercase;
  letter-spacing:0.12em;
  margin-bottom:8px;
}
.bubble .body {
  font-size:18px;
  color:var(--text);
  word-break:break-word;
}
.bubble.user .body { font-size:17px; }
.session-divider {
  display:flex;
  align-items:center;
  gap:12px;
  color:var(--accent-deep);
}
.session-divider::before,
.session-divider::after {
  content:"";
  height:1px;
  flex:1;
  background:linear-gradient(90deg, transparent, var(--line-strong), transparent);
}
.session-chip {
  padding:10px 14px;
  border-radius:999px;
  background:rgba(41,104,216,0.08);
  border:1px solid rgba(41,104,216,0.14);
  font-size:12px;
  font-weight:700;
  letter-spacing:0.10em;
  text-transform:uppercase;
}
.metrics-grid {
  display:grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap:12px;
}
.metric-tile {
  padding:14px;
  border-radius:18px;
  background:var(--bg-soft);
  border:1px solid var(--line);
}
.countdown { font-size:30px; font-weight:700; line-height:1; }
.meter {
  height:14px;
  background:#e8eff7;
  border:1px solid var(--line);
  border-radius:999px;
  overflow:hidden;
}
.meter > div {
  height:100%;
  width:0%;
  background:linear-gradient(90deg, #7fc39b, #4d88e6, #17479f);
  transition:width 120ms linear;
}
.controls {
  display:flex;
  gap:10px;
  align-items:center;
  flex-wrap:wrap;
  margin-top:12px;
}
textarea,
select {
  width:100%;
  background:white;
  color:var(--text);
  border:1px solid var(--line);
  border-radius:14px;
  padding:14px;
  font:inherit;
}
textarea {
  min-height:92px;
  resize:vertical;
}
select { padding-right:40px; }
button {
  background:#f6faff;
  color:var(--accent-deep);
  border:1px solid rgba(41,104,216,0.14);
  border-radius:14px;
  padding:12px 16px;
  font:inherit;
  font-weight:600;
  cursor:pointer;
  box-shadow:none;
}
button.primary {
  background:rgba(41,104,216,0.10);
  color:var(--accent-deep);
  border-color:rgba(41,104,216,0.22);
}
button.secondary {
  background:#f3f7fb;
  color:#617892;
  border-color:rgba(98,122,148,0.18);
}
button.warn {
  background:#fff6f6;
  color:#a44a4a;
  border-color:rgba(187,86,86,0.22);
}
button.ghost {
  background:#fbfdff;
  color:var(--accent-deep);
  border:1px solid var(--line-strong);
  box-shadow:none;
}
.btn-icon {
  display:inline-flex;
  width:18px;
  justify-content:center;
  margin-right:6px;
  opacity:0.82;
}
.panel-section { display:grid; gap:14px; }
.utility-grid { display:grid; gap:12px; }
.kv-list { display:grid; gap:12px; }
.kv-row {
  display:flex;
  justify-content:space-between;
  gap:12px;
  padding-bottom:10px;
  border-bottom:1px solid var(--line);
}
.kv-row:last-child { padding-bottom:0; border-bottom:none; }
.kv-key { color:var(--muted); font-size:13px; text-transform:uppercase; letter-spacing:0.08em; }
.kv-value { font-weight:600; text-align:right; }
.mono { font-family: "SFMono-Regular", Consolas, monospace; font-size:13px; }
.table-wrap {
  overflow:auto;
  border:1px solid var(--line);
  border-radius:16px;
  background:rgba(255,255,255,0.72);
}
table { width:100%; border-collapse:collapse; min-width:760px; }
.compact-table table { min-width:0; }
th, td {
  text-align:left;
  padding:9px 12px;
  border-bottom:1px solid var(--line);
  vertical-align:top;
}
th {
  color:var(--muted);
  font-size:11px;
  font-weight:700;
  text-transform:uppercase;
  letter-spacing:0.08em;
  background:rgba(244,248,253,0.92);
}
tr:last-child td { border-bottom:none; }
.table-wrap td { font-size:13px; line-height:1.35; }
.table-wrap .mono { font-size:12px; }
.table-card { padding:18px; }
.table-head {
  display:flex;
  justify-content:space-between;
  gap:14px;
  align-items:flex-end;
  margin-bottom:14px;
}
.fold-card {
  background:var(--panel);
  border:1px solid var(--line);
  border-radius:22px;
  box-shadow:var(--shadow);
  overflow:hidden;
}
.fold-summary {
  list-style:none;
  cursor:pointer;
  padding:15px 18px;
  display:flex;
  justify-content:space-between;
  align-items:center;
  gap:14px;
}
.fold-summary::-webkit-details-marker { display:none; }
.fold-summary::after {
  content:"+";
  font-size:24px;
  color:var(--accent-deep);
  line-height:1;
}
.fold-card[open] .fold-summary::after { content:"−"; }
.fold-body { padding:0 16px 16px; }
.state-badge {
  display:inline-flex;
  align-items:center;
  gap:8px;
  padding:5px 9px;
  border-radius:999px;
  background:#eef4ff;
  color:var(--accent-deep);
  font-size:11px;
  font-weight:700;
  text-transform:uppercase;
  letter-spacing:0.06em;
}
.state-badge::before {
  content:"";
  width:8px;
  height:8px;
  border-radius:999px;
  background:currentColor;
}
.state-badge.good {
  background:var(--good-soft);
  color:var(--good);
}
.state-badge.warn {
  background:#fff4e6;
  color:var(--warn);
}
.state-badge.hot {
  background:#fff0f0;
  color:var(--hot);
}
.log-block {
  min-height:220px;
  max-height:220px;
  overflow:auto;
  padding:12px 14px;
  border-radius:16px;
  border:1px solid var(--line);
  background:#f7fbff;
  color:#29425f;
  white-space:pre-wrap;
  line-height:1.45;
}
.mini-note { color:var(--muted); font-size:12px; }
.footer {
  color:var(--muted);
  font-size:13px;
  margin-top:14px;
}
@media (max-width: 1120px) {
  .main-grid { grid-template-columns: 1fr; }
}
@media (max-width: 760px) {
  .page { padding: 18px 14px 28px; }
  .hero { flex-direction:column; align-items:flex-start; }
  .hero h1 { font-size:36px; }
  .metrics-grid { grid-template-columns: 1fr; }
  .chat-feed { min-height:380px; max-height:380px; padding:16px; }
  .chat-header { padding:16px; }
  .bubble .body { font-size:17px; }
  .bubble.user .body { font-size:16px; }
}
</style>
<div class="page">
  <div class="hero">
    <div>
      <h1>Pepper Operator Debug</h1>
      <p>Debug view for logs, participants, service health, and supporting diagnostics.</p>
    </div>
    <div class="hero-meta">
      <a class="nav-link" href="/">Chat</a>
      <a class="nav-link active" href="/debug">Debug</a>
      <a class="nav-link" href="/console">Dev Console</a>
      <div class="pill" id="pollState">Polling</div>
      <div class="pill good" id="watchdogPill">Watchdog waiting</div>
    </div>
  </div>
  <div class="shell">
    <div class="main-grid">
      <div class="rail">
        <div class="card">
          <div class="utility-grid">
            <div>
              <div class="log-title">Container Logs</div>
              <div class="panel-note">Recent logs sit here as a side utility, not as a main panel.</div>
            </div>
            <div>
              <select id="logContainerSelect"><option value="">Loading containers...</option></select>
              <div class="controls">
                <button class="ghost" id="refreshLogsBtn">Refresh Logs</button>
                <div class="mini-note">Recent tail only.</div>
              </div>
              <div class="log-block mono" id="logOutput">Select a container to inspect recent logs.</div>
            </div>
          </div>
        </div>
        <div class="card">
          <div class="utility-grid">
            <div>
              <div class="panel-title">Room Snapshot</div>
              <div class="panel-note">Compact room and watchdog facts in table form.</div>
            </div>
            <div class="table-wrap compact-table">
              <table class="mini-table">
                <tbody>
                  <tr><th>Last user activity</th><td class="mono" id="userActivity">-</td></tr>
                  <tr><th>Last agent activity</th><td class="mono" id="agentActivity">-</td></tr>
                  <tr><th>Watchdog</th><td id="watchdogSummary">-</td></tr>
                  <tr><th>Pepper reachable</th><td id="watchdogReachable">-</td></tr>
                  <tr><th>Safe startup</th><td id="watchdogStartup">-</td></tr>
                  <tr><th>Last result</th><td id="watchdogResult">-</td></tr>
                  <tr><th>Updated</th><td class="mono" id="watchdogUpdated">-</td></tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
      <div class="content-stack">
        <details class="fold-card" open>
          <summary class="fold-summary">
            <div>
              <h3 style="margin:0;">Components</h3>
              <div class="table-note">Heartbeat state, probes, and supporting services.</div>
            </div>
          </summary>
          <div class="fold-body">
            <div class="table-wrap">
              <table>
                <thead><tr><th>Name</th><th>State</th><th>Healthy</th><th>Source</th><th>Detail</th><th>Updated</th></tr></thead>
                <tbody id="componentsBody"><tr><td colspan="6">Loading...</td></tr></tbody>
              </table>
            </div>
          </div>
        </details>
        <details class="fold-card" open>
          <summary class="fold-summary">
            <div>
              <h3 style="margin:0;">Participants</h3>
              <div class="table-note">Joined room participants, available on demand.</div>
            </div>
          </summary>
          <div class="fold-body">
            <div class="table-wrap">
              <table>
                <thead><tr><th>Identity</th><th>Name</th><th>Kind</th><th>State</th><th>Metadata</th></tr></thead>
                <tbody id="participantsBody"><tr><td colspan="5">Loading...</td></tr></tbody>
              </table>
            </div>
          </div>
        </details>
      </div>
    </div>
  </div>
  <div class="footer">Session lifecycle is still driven by mic activity and idle timeout. This dashboard remains an operator surface layered on top.</div>
</div>
<script>
let selectedContainerId = "";
function fmtTs(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}
function text(el, value) {
  document.getElementById(el).textContent = value || "-";
}
function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}
function meter(el, value) {
  const pct = Math.max(0, Math.min(100, Math.round((value || 0) * 100)));
  document.getElementById(el).style.width = pct + "%";
}
function toneClass(value) {
  const clean = String(value || "").toLowerCase();
  if (["ready", "active", "live", "connected", "healthy", "online", "success", "running", "reachable", "yes"].includes(clean)) return "good";
  if (["starting", "bootstrapping", "cooldown", "waiting", "unknown", "idle", "muted"].includes(clean)) return "warn";
  if (["down", "failed", "degraded", "disconnected", "stale", "offline", "error", "unreachable", "no"].includes(clean)) return "hot";
  return "";
}
function badge(value) {
  const tone = toneClass(value);
  return `<span class="state-badge ${tone}">${escapeHtml(value || "-")}</span>`;
}
async function postJson(url, body) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return await res.json();
}
async function refreshContainers() {
  const select = document.getElementById("logContainerSelect");
  try {
    const res = await fetch("/api/docker/containers");
    const data = await res.json();
    const items = data.containers || [];
    if (!items.length) {
      select.innerHTML = '<option value="">Docker access unavailable</option>';
      document.getElementById("logOutput").textContent = data.error || "No managed containers detected.";
      return;
    }
    select.innerHTML = items.map((item) => {
      const label = `${item.service} · ${item.name} · ${item.status}`;
      const selected = selectedContainerId === item.id ? "selected" : "";
      return `<option value="${escapeHtml(item.id)}" ${selected}>${escapeHtml(label)}</option>`;
    }).join("");
    if (!selectedContainerId) {
      selectedContainerId = items[0].id;
      select.value = selectedContainerId;
    }
  } catch (err) {
    select.innerHTML = '<option value="">Docker access unavailable</option>';
    document.getElementById("logOutput").textContent = "Failed to load container list.";
  }
}
async function refreshLogs() {
  const output = document.getElementById("logOutput");
  const select = document.getElementById("logContainerSelect");
  const containerId = select.value;
  selectedContainerId = containerId;
  if (!containerId) {
    output.textContent = "Select a container to inspect recent logs.";
    return;
  }
  output.textContent = "Loading logs...";
  try {
    const res = await fetch(`/api/docker/logs?container=${encodeURIComponent(containerId)}`);
    const data = await res.json();
    if (!data.ok) {
      output.textContent = data.error || "Failed to read logs.";
      return;
    }
    output.textContent = data.logs || "No logs available for this container.";
  } catch (err) {
    output.textContent = "Failed to read logs.";
  }
}
async function refresh() {
  const pill = document.getElementById("pollState");
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    pill.textContent = "Live";
    pill.className = "pill good";
    text("userActivity", fmtTs(data.last_user_activity_at));
    text("agentActivity", fmtTs(data.last_agent_activity_at));
    const tbody = document.getElementById("participantsBody");
    const rows = (data.participants || []).map((item) => `
      <tr>
        <td class="mono">${escapeHtml(item.identity || "")}</td>
        <td>${escapeHtml(item.name || "")}</td>
        <td>${escapeHtml(item.kind || "")}</td>
        <td>${badge(item.state || "")}</td>
        <td class="mono">${escapeHtml(item.metadata || "")}</td>
      </tr>
    `).join("");
    tbody.innerHTML = rows || '<tr><td colspan="5">No participants.</td></tr>';
    const componentsBody = document.getElementById("componentsBody");
    const componentRows = (data.components || []).map((item) => `
      <tr>
        <td class="mono">${escapeHtml(item.name || "")}</td>
        <td>${badge(item.state || "")}</td>
        <td>${badge(item.healthy ? "yes" : "no")}</td>
        <td>${escapeHtml(item.source || "")}</td>
        <td class="mono">${escapeHtml(item.detail || "")}</td>
        <td class="mono">${escapeHtml(fmtTs(item.updated_at))}</td>
      </tr>
    `).join("");
    componentsBody.innerHTML = componentRows || '<tr><td colspan="6">No component state.</td></tr>';
    const watchdog = data.watchdog || {};
    text("watchdogSummary", watchdog.summary || "waiting for updates");
    text("watchdogReachable", watchdog.pepper_reachable ? "reachable" : "offline");
    text("watchdogStartup", watchdog.safe_startup_running ? "running" : "idle");
    text("watchdogResult", watchdog.last_result || "none");
    text("watchdogUpdated", fmtTs(watchdog.updated_at));
    const watchdogPill = document.getElementById("watchdogPill");
    const watchdogTone = watchdog.safe_startup_running ? "warn" : (watchdog.pepper_reachable ? "good" : "hot");
    watchdogPill.className = `pill ${watchdogTone}`;
    watchdogPill.textContent = watchdog.summary || "Watchdog waiting";
  } catch (err) {
    pill.textContent = "Disconnected";
    pill.className = "pill hot";
  }
}
document.getElementById("logContainerSelect").addEventListener("change", refreshLogs);
document.getElementById("refreshLogsBtn").addEventListener("click", refreshLogs);
refresh();
refreshContainers().then(refreshLogs);
setInterval(refresh, 1500);
setInterval(refreshContainers, 8000);
</script>
"""

CHAT_HTML = """<!doctype html>
<meta charset="utf-8">
<title>Pepper Operator Chat</title>
<style>
:root {
  --bg: #eef4fb;
  --panel: rgba(255,255,255,0.94);
  --line: #d8e4f1;
  --text: #16324a;
  --muted: #667f97;
  --accent: #2968d8;
  --accent-soft: #edf4ff;
  --accent-deep: #17479f;
  --good: #5aa878;
  --good-soft: #ebf8ef;
  --hot: #c85757;
  --shadow: 0 18px 40px rgba(40, 74, 111, 0.10);
}
* { box-sizing:border-box; }
html,body {
  margin:0;
  padding:0;
  min-height:100%;
  background:
    radial-gradient(circle at top left, rgba(41,104,216,0.14), transparent 28%),
    radial-gradient(circle at top right, rgba(90,168,120,0.12), transparent 22%),
    linear-gradient(180deg, #f9fbff, #eef4fb 42%, #e9f0f8 100%);
  color:var(--text);
  font-family: "Segoe UI", Arial, sans-serif;
}
.page { max-width: 1220px; margin: 0 auto; padding: 18px 16px 24px; }
.hero {
  display:grid;
  grid-template-columns: 1fr auto 1fr;
  align-items:center;
  gap:12px;
  margin-bottom:12px;
}
.hero h1 { margin:0; font-size:32px; line-height:1; letter-spacing:-0.03em; }
.hero p { margin:6px 0 0; color:var(--muted); font-size:14px; }
.hero-meta {
  grid-column:2;
  justify-self:center;
  display:flex;
  align-items:center;
  gap:10px;
  flex-wrap:wrap;
}
.layout {
  display:grid;
  grid-template-columns: minmax(0, 1fr) minmax(210px, 240px);
  gap:12px;
  align-items:start;
}
.main-column,
.side-column {
  display:grid;
  gap:12px;
}
.nav-link {
  display:inline-flex;
  align-items:center;
  padding:8px 12px;
  border-radius:999px;
  text-decoration:none;
  color:var(--accent-deep);
  background:rgba(255,255,255,0.72);
  border:1px solid var(--line);
  font-size:13px;
  font-weight:600;
}
.nav-link.active {
  background:var(--accent-soft);
  border-color:rgba(41,104,216,0.18);
}
.pill {
  display:inline-flex;
  align-items:center;
  gap:8px;
  padding:8px 12px;
  border-radius:999px;
  background:var(--accent-soft);
  color:var(--accent-deep);
  font-size:13px;
  font-weight:600;
  border:1px solid rgba(41,104,216,0.14);
}
.pill::before {
  content:"";
  width:8px;
  height:8px;
  border-radius:999px;
  background:currentColor;
}
.pill.good {
  background:var(--good-soft);
  color:var(--good);
}
.pill.hot {
  background:#fff0f0;
  color:var(--hot);
}
.top-strip {
  border:1px solid var(--line);
  border-radius:18px;
  background:rgba(255,255,255,0.84);
  box-shadow: 0 10px 22px rgba(40, 74, 111, 0.06);
  overflow:hidden;
  margin-bottom:12px;
}
.mini-table { width:100%; border-collapse:collapse; }
.mini-table th,
.mini-table td {
  padding:10px 12px;
  border-bottom:1px solid var(--line);
  font-size:13px;
  text-align:left;
}
.mini-table th { width:18%; color:var(--muted); text-transform:uppercase; letter-spacing:0.08em; background:rgba(244,248,253,0.92); }
.mini-table tr:last-child th,
.mini-table tr:last-child td { border-bottom:none; }
.mini-cell-main { font-size:15px; font-weight:700; }
.mini-cell-sub { margin-top:2px; color:var(--muted); font-size:12px; }
.card {
  background:var(--panel);
  border:1px solid var(--line);
  border-radius:22px;
  box-shadow:var(--shadow);
  overflow:hidden;
}
.chat-header {
  padding:18px 20px 12px;
  border-bottom:1px solid var(--line);
  display:flex;
  justify-content:space-between;
  align-items:flex-end;
  gap:12px;
}
.chat-header-actions {
  display:flex;
  align-items:center;
  gap:10px;
}
.chat-header-actions #resetBtn {
  min-height:50px;
  padding:0 20px;
  border-radius:18px;
  background:rgba(255,255,255,0.9);
  color:#8c6f6f;
  border:1px solid rgba(196,176,176,0.32);
  box-shadow:none;
}
.chat-header-actions #resetBtn:hover {
  background:rgba(250,246,246,0.98);
  border-color:rgba(196,176,176,0.45);
}
.chat-header h2,
.controls-title {
  margin:0;
  font-size:20px;
  letter-spacing:-0.02em;
}
.chat-header p,
.controls-note {
  margin:6px 0 0;
  color:var(--muted);
  font-size:14px;
}
.chat-feed {
  display:flex;
  flex-direction:column;
  gap:12px;
  min-height:560px;
  max-height:560px;
  overflow:auto;
  padding:18px 20px 20px;
  background:linear-gradient(180deg, rgba(244,248,253,0.88), rgba(255,255,255,0.92));
}
.bubble {
  max-width:90%;
  border-radius:18px;
  padding:14px 16px;
  line-height:1.45;
  box-shadow: 0 8px 22px rgba(40, 74, 111, 0.06);
}
.bubble.user { align-self:flex-end; background:#edf4ff; border:1px solid rgba(41,104,216,0.10); }
.bubble.pepper { align-self:flex-start; background:#ffffff; border:1px solid #dbe7f3; }
.bubble.pepper.tool-bubble { opacity:0.7; padding:4px 10px; box-shadow:none; border-style:dashed; }
.bubble.pepper.tool-bubble .speaker { font-size:10px; }
.bubble.pepper.tool-bubble .body-text { font-size:12px; font-family:monospace; }
.bubble.system {
  align-self:center;
  width:100%;
  max-width:100%;
  background:transparent;
  box-shadow:none;
  border:none;
  padding:0;
}
.speaker {
  font-size:11px;
  font-weight:700;
  color:var(--muted);
  text-transform:uppercase;
  letter-spacing:0.12em;
  margin-bottom:8px;
}
.body-text { font-size:17px; color:var(--text); word-break:break-word; }
.session-divider {
  display:flex;
  align-items:center;
  gap:12px;
  color:#93a1b2;
}
.session-divider::before,
.session-divider::after {
  content:"";
  height:1px;
  flex:1;
  background:linear-gradient(90deg, transparent, #e1e7ee, transparent);
}
.session-chip {
  padding:0;
  border:none;
  background:transparent;
  font-size:12px;
  font-weight:500;
  letter-spacing:0.01em;
  text-transform:none;
  color:#93a1b2;
  line-height:1.2;
  white-space:nowrap;
}
.controls-card {
  margin-top:12px;
  padding:0;
  background:transparent;
  border:none;
  box-shadow:none;
  overflow:visible;
}
.composer {
  position:relative;
}
.tiny-card {
  padding:14px 14px 12px;
}
.signal-table {
  width:100%;
  border-collapse:collapse;
}
.signal-table th,
.signal-table td {
  padding:10px 0;
  text-align:left;
  vertical-align:top;
  border-bottom:1px solid var(--line);
}
.signal-table th {
  width:44%;
  color:var(--muted);
  font-size:11px;
  text-transform:uppercase;
  letter-spacing:0.08em;
}
.signal-table tr:last-child th,
.signal-table tr:last-child td {
  border-bottom:none;
}
.signal-value {
  font-size:13px;
  font-weight:600;
}
.tiny-meter {
  height:8px;
  border-radius:999px;
  overflow:hidden;
  background:#e8eff7;
  border:1px solid var(--line);
  margin-bottom:6px;
}
.tiny-meter > div {
  width:0%;
  height:100%;
  background:linear-gradient(90deg, #7fc39b, #4d88e6, #17479f);
}
.controls-head {
  margin-bottom:10px;
}
.mini-note { color:var(--muted); font-size:12px; }
.mono { font-family: "SFMono-Regular", Consolas, monospace; font-size:12px; }
textarea {
  display:block;
  width:100%;
  min-height:24px;
  max-height:136px;
  resize:none;
  overflow-y:auto;
  background:rgba(255,255,255,0.96);
  color:var(--text);
  border:1px solid #cfd9e6;
  border-radius:22px;
  padding:14px 118px 14px 24px;
  font:inherit;
  font-size:18px;
  line-height:1.25;
  box-shadow: 0 8px 18px rgba(40, 74, 111, 0.08);
}
.controls {
  position:absolute;
  right:14px;
  top:50%;
  transform:translateY(-50%);
  display:inline-flex;
  justify-content:flex-end;
  align-items:center;
  gap:8px;
  margin-top:0;
  min-height:auto;
  padding:0;
  pointer-events:none;
}
.toggle {
  display:inline-flex;
  align-items:center;
  padding:0;
  border:none;
  background:transparent;
  pointer-events:auto;
}
.toggle input {
  appearance:none;
  width:36px;
  height:36px;
  border-radius:18px;
  background-color:rgba(255,255,255,0.98);
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%238d99a7' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Crect x='9' y='3.5' width='6' height='10.5' rx='3'/%3E%3Cpath d='M6 10.5a6 6 0 0 0 12 0'/%3E%3Cpath d='M12 16.5v4.5'/%3E%3Cpath d='M8.5 21h7'/%3E%3C/svg%3E");
  background-repeat:no-repeat;
  background-position:center;
  background-size:18px 18px;
  outline:none;
  cursor:pointer;
  border:1px solid rgba(207,217,230,0.95);
  box-shadow: 0 6px 16px rgba(40, 74, 111, 0.08);
  transition:background-color 140ms ease, border-color 140ms ease, box-shadow 140ms ease;
}
.toggle span {
  display:none;
}
.toggle input:checked {
  background-color:#f4f7fb;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%238d99a7' stroke-width='2.2' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M4 4l16 16'/%3E%3Cpath d='M9.2 9.2V6.5a2.8 2.8 0 0 1 5.6 0v6a2.7 2.7 0 0 1-.4 1.4'/%3E%3Cpath d='M6 10.5a6 6 0 0 0 10.4 4.1'/%3E%3Cpath d='M12 16.5v4.5'/%3E%3Cpath d='M8.5 21h7'/%3E%3C/svg%3E");
  border-color:rgba(180,191,205,0.95);
}
button {
  background:#f6faff;
  color:var(--accent-deep);
  border:1px solid rgba(41,104,216,0.14);
  border-radius:14px;
  padding:12px 16px;
  font:inherit;
  font-weight:600;
  cursor:pointer;
}
button.primary { background:rgba(41,104,216,0.10); border-color:rgba(41,104,216,0.22); }
button.secondary { background:#f3f7fb; color:#617892; border-color:rgba(98,122,148,0.18); }
button.warn { background:#fff6f6; color:#a44a4a; border-color:rgba(187,86,86,0.22); }
.btn-icon { display:inline-flex; width:18px; justify-content:center; margin-right:6px; opacity:0.82; }
.controls #sendBtn {
  width:36px;
  height:36px;
  padding:0;
  border-radius:18px;
  background:#a8a8a8;
  border-color:#a8a8a8;
  color:#ffffff;
  display:inline-flex;
  align-items:center;
  justify-content:center;
  pointer-events:auto;
}
.controls #sendBtn .btn-icon {
  width:16px;
  height:16px;
  margin-right:0;
  font-size:0;
  opacity:1;
  background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 24 24' fill='none' stroke='%23ffffff' stroke-width='2.8' stroke-linecap='round' stroke-linejoin='round'%3E%3Cpath d='M12 17V7'/%3E%3Cpath d='M7.5 11.5 12 7l4.5 4.5'/%3E%3C/svg%3E");
  background-repeat:no-repeat;
  background-position:center;
  background-size:16px 16px;
}
.controls #sendBtn .btn-label {
  display:none;
}
@media (max-width: 760px) {
  .layout { grid-template-columns: 1fr; }
  .hero { grid-template-columns: 1fr; }
  .hero-meta { grid-column:auto; justify-self:start; }
  .hero h1 { font-size:28px; }
  .chat-feed { min-height:420px; max-height:420px; }
  .body-text { font-size:16px; }
  textarea {
    min-height:22px;
    padding:13px 108px 13px 20px;
    font-size:16px;
  }
  .controls {
    right:10px;
    margin-top:0;
    min-height:auto;
    padding:0;
  }
  .toggle input {
    width:34px;
    height:34px;
    border-radius:17px;
  }
  .controls #sendBtn {
    width:34px;
    height:34px;
    border-radius:17px;
  }
}
</style>
<div class="page">
  <div class="hero">
    <div>
      <h1>Pepper Operator</h1>
      <p>Focused chat surface for the live interaction.</p>
    </div>
    <div class="hero-meta">
      <a class="nav-link active" href="/">Chat</a>
      <a class="nav-link" href="/debug">Debug</a>
      <a class="nav-link" href="/console">Dev Console</a>
      <div class="pill" id="pollState">Polling</div>
      <div class="pill good" id="chatLivePill">Waiting for session</div>
    </div>
  </div>
  <div class="layout">
    <div class="main-column">
      <div class="card">
        <div class="chat-header">
          <div>
            <h2>Conversation History</h2>
          </div>
          <div class="chat-header-actions">
            <button class="warn" id="resetBtn">Restart Session</button>
          </div>
        </div>
        <div class="chat-feed" id="transcriptList"><div class="mini-note">No transcript yet.</div></div>
      </div>
      <div class="card controls-card">
        <div class="composer">
          <textarea id="userText" rows="1" placeholder="Ask anything"></textarea>
          <div class="controls">
            <label class="toggle" title="Toggle mic mute"><input type="checkbox" id="muteToggle"><span></span></label>
            <button class="primary" id="sendBtn" title="Send"><span class="btn-icon">↑</span><span class="btn-label">Send</span></button>
          </div>
        </div>
      </div>
    </div>
    <div class="side-column">
      <div class="card tiny-card">
        <div class="controls-title" style="font-size:16px;">Live Signal</div>
        <table class="signal-table">
          <tbody>
            <tr><th>Mic state</th><td class="signal-value mono" id="chatMuteState">-</td></tr>
            <tr><th>Mic</th><td class="signal-value mono" id="chatMicLevelText">-</td></tr>
            <tr><th>Pepper</th><td class="signal-value mono" id="chatPepperLevelText">-</td></tr>
            <tr><th>Mode</th><td class="signal-value mono" id="chatSessionState">-</td></tr>
            <tr><th>Standby</th><td class="signal-value mono" id="chatWarmState">-</td></tr>
            <tr><th>Speaking</th><td class="signal-value" id="chatPepperSpeaking">-</td></tr>
            <tr><th>Idle</th><td class="signal-value mono" id="chatIdleCountdown">-</td></tr>
          </tbody>
        </table>
      </div>
    </div>
  </div>
</div>
<script>
function fmtTs(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}
function text(el, value) {
  const node = document.getElementById(el);
  if (node) node.textContent = value || "-";
}
function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}
function warmStateLabel(data) {
  if (data.session_state === "active") return "activated into live session";
  if (!data.agent_deployed) return "not deployed";
  if (data.warm_agent_ready) return "ready";
  if (data.warm_activation_pending) return "warming, user waiting";
  return "warming";
}
function speakingLabel(data) {
  const level = Number(data.agent_audio_level || 0);
  return level >= 0.015 ? "yes" : "no";
}
function sessionStateLabel(data) {
  if (data.session_state === "active") return "active conversation";
  if (data.session_state === "warm") {
    if (data.warm_agent_ready) return "idle, warm standby ready";
    if (data.warm_activation_pending) return "starting session, waiting for standby";
    return "warming standby";
  }
  if (data.session_state === "cooldown") return "cooldown";
  if (data.session_state === "ending") return "ending session";
  if (data.session_state === "starting") return "starting session";
  if (data.session_state === "idle") return "idle";
  return data.session_state || "-";
}
async function postJson(url, body) {
  const res = await fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  return await res.json();
}
async function refresh() {
  const pill = document.getElementById("pollState");
  try {
    const res = await fetch("/api/status");
    const data = await res.json();
    pill.textContent = "Live";
    pill.className = "pill good";
    text("chatMuteState", data.mic_muted ? "muted" : "live");
    document.getElementById("muteToggle").checked = !!data.mic_muted;
    text("chatMicLevelText", (data.mic_level || 0).toFixed(3));
    text("chatPepperLevelText", (data.agent_audio_level || 0).toFixed(3));
    text("chatSessionState", sessionStateLabel(data));
    text("chatWarmState", warmStateLabel(data));
    text("chatPepperSpeaking", speakingLabel(data));
    text("chatIdleCountdown", data.idle_countdown_sec != null ? `${data.idle_countdown_sec.toFixed(1)}s` : "waiting");
    const chatLivePill = document.getElementById("chatLivePill");
    let pillClass = "pill";
    let pillText = "Waiting for session";
    if (data.session_state === "active") {
      pillClass = "pill good";
      pillText = "Live session";
    } else if (data.agent_deployed && data.warm_agent_ready) {
      pillClass = "pill good";
      pillText = "Agent";
    } else if (data.agent_deployed) {
      pillClass = "pill";
      pillText = data.warm_activation_pending ? "Starting session" : "Warming agent";
    }
    chatLivePill.className = pillClass;
    chatLivePill.textContent = pillText;
    const transcriptEl = document.getElementById("transcriptList");
    const transcriptRows = (data.transcript_items || []).map((item) => {
      if (item.kind === "session") {
        return `
          <div class="bubble system">
            <div class="session-divider">
              <div class="session-chip">${escapeHtml(item.text || "Session update")} · ${escapeHtml(fmtTs(item.at))}</div>
            </div>
          </div>
        `;
      }
      const isPepper = item.speaker === "Pepper" || item.kind === "tool";
      const bubbleClass = isPepper ? "pepper" : "user";
      const toolClass = item.kind === "tool" ? " tool-bubble" : "";
      return `
        <div class="bubble ${bubbleClass}${toolClass}">
          <div class="speaker">${escapeHtml(item.speaker || "")} · ${escapeHtml(fmtTs(item.at))}</div>
          <div class="body-text">${escapeHtml(item.text || "")}</div>
        </div>
      `;
    }).join("");
    const shouldStickToBottom =
      transcriptEl.scrollTop === 0 ||
      transcriptEl.scrollHeight - transcriptEl.scrollTop - transcriptEl.clientHeight < 96;
    transcriptEl.innerHTML = transcriptRows || '<div class="mini-note">No transcript yet.</div>';
    if (shouldStickToBottom) {
      transcriptEl.scrollTop = transcriptEl.scrollHeight;
    }
  } catch (err) {
    pill.textContent = "Disconnected";
    pill.className = "pill hot";
  }
}
async function sendUserText() {
  const el = document.getElementById("userText");
  const textValue = el.value.trim();
  if (!textValue) return;
  await postJson("/api/control/text", { text: textValue });
  el.value = "";
  refresh();
}
document.getElementById("sendBtn").addEventListener("click", sendUserText);
document.getElementById("userText").addEventListener("keydown", async (event) => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    await sendUserText();
  }
});
document.getElementById("muteToggle").addEventListener("change", async () => {
  await postJson("/api/control/mic", {});
  refresh();
});
document.getElementById("resetBtn").addEventListener("click", async () => {
  await postJson("/api/control/reset", {});
  refresh();
});
refresh();
setInterval(refresh, 1500);
</script>
"""


def _load_root_env() -> None:
    if ROOT_ENV_PATH.exists():
        load_dotenv(dotenv_path=ROOT_ENV_PATH, override=True)


def _get_required_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _identity_is_agent(identity: str, kind: str) -> bool:
    if identity.startswith("agent-"):
        return True
    return "AGENT" in kind.upper()


class SessionManager:
    def __init__(self) -> None:
        _load_root_env()
        self.room_name = LIVEKIT_ROOM_NAME
        self.livekit_ws_url = LIVEKIT_URL
        self.livekit_host_ws_url = LIVEKIT_HOST_WS_URL
        self.livekit_http_url = LIVEKIT_HTTP_URL
        self.bridge_url = BRIDGE_URL
        self.dev_console_url = os.getenv("DEV_CONSOLE_URL", "http://localhost:8788").rstrip("/")
        self.session_file = Path(LIVEKIT_SESSION_FILE)
        self.state_file = self.session_file.with_name(SESSION_MANAGER_STATE_FILE)
        self.api_key = _get_required_env("LIVEKIT_API_KEY")
        self.api_secret = _get_required_env("LIVEKIT_API_SECRET")
        self.agent_name = (os.getenv("PEPPER_AGENT_NAME") or AGENT_NAME_DEFAULT).strip() or AGENT_NAME_DEFAULT
        self.session_state = "idle"
        self.agent_deployed = False
        self.warm_agent_ready = False
        self.warm_activation_pending = False
        self.conversation_id = ""
        self.active_dispatch_id = ""
        self.last_user_activity_monotonic = 0.0
        self.last_agent_activity_monotonic = 0.0
        self.dispatch_started_monotonic = 0.0
        self.last_user_activity_at = ""
        self.last_agent_activity_at = ""
        self.updated_at = ""
        self.participants: list[dict[str, str]] = []
        self.transcript_items: deque[dict[str, str]] = deque(maxlen=MAX_TRANSCRIPT_ITEMS)
        self.last_user_text = ""
        self.last_pepper_text = ""
        self.mic_level = 0.0
        self.mic_muted = False
        self.agent_speaking = False
        self.agent_audio_level = 0.0
        self.pending_user_texts: list[dict[str, str]] = []
        self.components: dict[str, dict[str, Any]] = {}
        self.docker_socket_path = DOCKER_SOCKET_PATH
        self._load_persisted_state()
        self.watchdog_status: dict[str, Any] = {
            "summary": "waiting for watchdog",
            "pepper_reachable": False,
            "safe_startup_running": False,
            "last_result": "",
            "updated_at": "",
        }
        self._lock = asyncio.Lock()
        self._bg_tasks: list[asyncio.Task[Any]] = []
        self._bootstrap_complete = False
        self._register_component(
            "session-manager",
            state="starting",
            detail="initializing",
            healthy=True,
            source="internal",
        )
        self._register_component(
            "listener",
            state="unknown",
            detail="waiting for heartbeat",
            healthy=False,
            source="service",
        )
        self._register_component(
            "user-client",
            state="unknown",
            detail="waiting for heartbeat",
            healthy=False,
            source="service",
        )
        self._register_component(
            "voice-agent",
            state="unknown",
            detail="waiting for heartbeat",
            healthy=False,
            source="service",
        )
        self._register_component(
            "safe-startup",
            state="unknown",
            detail="waiting for watchdog",
            healthy=False,
            source="service",
        )
        self._register_component(
            "bridge",
            state="unknown",
            detail="waiting for probe",
            healthy=False,
            source="probe",
        )
        self._register_component(
            "livekit",
            state="unknown",
            detail="waiting for probe",
            healthy=False,
            source="probe",
        )
        self._register_component(
            "redis",
            state="unknown",
            detail="waiting for probe",
            healthy=False,
            source="probe",
        )
        self._register_component(
            "weaviate",
            state="unknown",
            detail="waiting for probe",
            healthy=False,
            source="probe",
        )

    def _clear_agent_runtime_state(self) -> None:
        self.agent_deployed = False
        self.warm_agent_ready = False
        self.warm_activation_pending = False
        self.active_dispatch_id = ""
        self.dispatch_started_monotonic = 0.0

    def _load_persisted_state(self) -> None:
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:
            print(f"[session_manager] load state failed path={self.state_file} err={exc}")
            return
        self.mic_muted = bool(payload.get("mic_muted", self.mic_muted))

    def _persist_state(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {"mic_muted": self.mic_muted, "updated_at": _utc_now_iso()}
            tmp_path = self.state_file.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp_path.replace(self.state_file)
        except Exception as exc:
            print(f"[session_manager] persist state failed path={self.state_file} err={exc}")

    def _register_component(
        self,
        name: str,
        *,
        state: str,
        detail: str,
        healthy: bool,
        source: str,
    ) -> None:
        self.components[name] = {
            "name": name,
            "state": state,
            "detail": detail,
            "healthy": healthy,
            "source": source,
            "updated_at": _utc_now_iso(),
            "updated_monotonic": time.monotonic(),
        }

    def _set_component_state(
        self,
        name: str,
        *,
        state: str,
        detail: str = "",
        healthy: bool = True,
        source: str | None = None,
    ) -> None:
        item = self.components.get(name) or {"name": name}
        item["name"] = name
        item["state"] = state
        item["detail"] = detail
        item["healthy"] = bool(healthy)
        if source is not None:
            item["source"] = source
        else:
            item["source"] = item.get("source", "service")
        item["updated_at"] = _utc_now_iso()
        item["updated_monotonic"] = time.monotonic()
        self.components[name] = item
        self.updated_at = item["updated_at"]

    async def _docker_get_json(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        timeout_sec: float = 1.5,
    ) -> Any:
        if not self.docker_socket_path or not Path(self.docker_socket_path).exists():
            raise RuntimeError("docker socket unavailable")
        connector = aiohttp.UnixConnector(path=self.docker_socket_path)
        timeout = aiohttp.ClientTimeout(total=timeout_sec)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.get(f"http://docker{path}", params=params) as response:
                if response.status >= 400:
                    raise RuntimeError(f"docker api {response.status}")
                return await response.json()

    async def _docker_get_text(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        timeout_sec: float = 2.5,
    ) -> str:
        if not self.docker_socket_path or not Path(self.docker_socket_path).exists():
            raise RuntimeError("docker socket unavailable")
        connector = aiohttp.UnixConnector(path=self.docker_socket_path)
        timeout = aiohttp.ClientTimeout(total=timeout_sec)
        async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
            async with session.get(f"http://docker{path}", params=params) as response:
                if response.status >= 400:
                    raise RuntimeError(f"docker api {response.status}")
                payload = await response.read()
                return self._decode_docker_log_payload(payload)

    def _decode_docker_log_payload(self, payload: bytes) -> str:
        if not payload:
            return ""
        chunks: list[bytes] = []
        idx = 0
        size = len(payload)
        while idx + 8 <= size:
            stream_type = payload[idx]
            if stream_type not in (0, 1, 2, 3):
                chunks = [payload]
                break
            frame_len = struct.unpack(">I", payload[idx + 4 : idx + 8])[0]
            frame_start = idx + 8
            frame_end = frame_start + frame_len
            if frame_end > size:
                chunks = [payload]
                break
            chunks.append(payload[frame_start:frame_end])
            idx = frame_end
        if not chunks:
            chunks = [payload]
        if idx < size and chunks != [payload]:
            chunks.append(payload[idx:])
        return b"".join(chunks).decode("utf-8", errors="replace")

    async def _list_docker_containers(self) -> list[dict[str, str]]:
        raw_items = await self._docker_get_json("/containers/json", params={"all": "1"})
        items: list[dict[str, str]] = []
        for raw in raw_items or []:
            labels = raw.get("Labels") or {}
            service = str(labels.get("com.docker.compose.service") or "").strip()
            if service not in KNOWN_DOCKER_SERVICES:
                continue
            names = raw.get("Names") or []
            container_name = str(names[0] or "").lstrip("/") if names else service
            items.append(
                {
                    "id": str(raw.get("Id") or ""),
                    "service": service,
                    "name": container_name,
                    "state": str(raw.get("State") or ""),
                    "status": str(raw.get("Status") or ""),
                }
            )
        return sorted(items, key=lambda item: (item["service"], item["name"]))

    def _new_lkapi(self) -> api.LiveKitAPI:
        return api.LiveKitAPI(self.livekit_http_url, self.api_key, self.api_secret)

    def _build_token(
        self,
        *,
        identity: str,
        can_publish: bool,
        can_subscribe: bool,
    ) -> str:
        return (
            api.AccessToken(self.api_key, self.api_secret)
            .with_identity(identity)
            .with_name(identity)
            .with_grants(
                api.VideoGrants(
                    room_join=True,
                    room=self.room_name,
                    can_publish=can_publish,
                    can_subscribe=can_subscribe,
                    can_publish_data=True,
                )
            )
            .to_jwt()
        )

    async def _probe_tcp(self, host: str, port: int, timeout: float = 1.0) -> bool:
        try:
            conn = asyncio.open_connection(host, port)
            reader, writer = await asyncio.wait_for(conn, timeout=timeout)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return True
        except Exception:
            return False

    async def _probe_http_health(self, raw_url: str, timeout: float = 1.0) -> bool:
        health_url = raw_url.rstrip("/") + "/health"
        req = Request(health_url, method="GET")
        try:
            await asyncio.to_thread(lambda: urlopen(req, timeout=timeout).read())
            return True
        except Exception:
            return False

    def _host_port_from_url(self, raw_url: str, default_port: int) -> tuple[str, int]:
        parsed = urlparse(raw_url)
        host = parsed.hostname or "127.0.0.1"
        port = int(parsed.port or default_port)
        return host, port

    async def ensure_room(self) -> None:
        lkapi = self._new_lkapi()
        try:
            try:
                await lkapi.room.create_room(api.CreateRoomRequest(name=self.room_name))
                print(f"[session_manager] created room={self.room_name}")
            except Exception as exc:
                print(f"[session_manager] create_room skipped room={self.room_name} err={exc}")
        finally:
            await lkapi.aclose()

    async def bootstrap_loop(self) -> None:
        while True:
            try:
                self._set_component_state(
                    "session-manager",
                    state="bootstrapping",
                    detail="ensuring room and session snapshot",
                    healthy=True,
                    source="internal",
                )
                await self.ensure_room()
                await self.cleanup_stale_dispatches()
                await self._remove_agent_participants()
                await self.write_session_snapshot()
                self._bootstrap_complete = True
                self._set_component_state(
                    "session-manager",
                    state="ready",
                    detail="dashboard and orchestration online",
                    healthy=True,
                    source="internal",
                )
                await self._dispatch_warm_agent()
                return
            except Exception as exc:
                self._set_component_state(
                    "session-manager",
                    state="degraded",
                    detail=f"bootstrap failed: {exc}",
                    healthy=False,
                    source="internal",
                )
                print(f"[session_manager] bootstrap failed err={exc}")
                await asyncio.sleep(3)

    async def probe_components_loop(self) -> None:
        livekit_host, livekit_port = self._host_port_from_url(self.livekit_http_url, 7880)
        redis_host = os.getenv("REDIS_HOST", "127.0.0.1")
        redis_port = int(os.getenv("REDIS_PORT", "6379"))
        weaviate_host = os.getenv("WEAVIATE_HOST", "127.0.0.1")
        weaviate_port = int(os.getenv("WEAVIATE_HTTP_PORT", "8080"))

        while True:
            checks = [
                ("livekit", livekit_host, livekit_port),
                ("redis", redis_host, redis_port),
                ("weaviate", weaviate_host, weaviate_port),
            ]
            for name, host, port in checks:
                ok = await self._probe_tcp(host, port)
                self._set_component_state(
                    name,
                    state="ready" if ok else "down",
                    detail=f"{host}:{port}",
                    healthy=ok,
                    source="probe",
                )
            bridge_ok = await self._probe_http_health(self.bridge_url)
            self._set_component_state(
                "bridge",
                state="ready" if bridge_ok else "down",
                detail=self.bridge_url,
                healthy=bridge_ok,
                source="probe",
            )
            now = time.monotonic()
            for name, item in list(self.components.items()):
                if item.get("source") != "service":
                    continue
                age = now - float(item.get("updated_monotonic") or 0.0)
                if age > COMPONENT_STALE_AFTER_SEC:
                    self._set_component_state(
                        name,
                        state="stale",
                        detail="no heartbeat received recently",
                        healthy=False,
                        source="service",
                    )
            await asyncio.sleep(COMPONENT_PROBE_INTERVAL_SEC)

    async def cleanup_stale_dispatches(self) -> None:
        lkapi = self._new_lkapi()
        try:
            for dispatch in await lkapi.agent_dispatch.list_dispatch(self.room_name):
                dispatch_id = str(getattr(dispatch, "id", "") or "")
                if not dispatch_id:
                    continue
                try:
                    await lkapi.agent_dispatch.delete_dispatch(dispatch_id, self.room_name)
                    print(f"[session_manager] deleted stale dispatch id={dispatch_id}")
                except Exception as exc:
                    print(f"[session_manager] delete stale dispatch failed id={dispatch_id} err={exc}")
        finally:
            await lkapi.aclose()

    async def write_session_snapshot(self) -> None:
        payload = {
            "generatedAt": _utc_now_iso(),
            "roomName": self.room_name,
            "wsUrl": self.livekit_ws_url,
            "internalWsUrl": self.livekit_ws_url,
            "hostWsUrl": self.livekit_host_ws_url,
            "source": "session-manager",
            "user": {
                "identity": USER_IDENTITY,
                "token": self._build_token(
                    identity=USER_IDENTITY,
                    can_publish=True,
                    # Keep subscribe permission enabled even for publisher-first clients.
                    # Delivery behavior is controlled by auto_subscribe=False on connect.
                    can_subscribe=True,
                ),
            },
            "listener": {
                "identity": LISTENER_IDENTITY,
                "token": self._build_token(
                    identity=LISTENER_IDENTITY,
                    can_publish=False,
                    can_subscribe=True,
                ),
            },
            "agent": {
                "name": self.agent_name,
            },
        }
        self.session_file.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.session_file.with_suffix(".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp_path.replace(self.session_file)
        print(f"[session_manager] wrote session snapshot {self.session_file}")

    async def _refresh_participants_once(self) -> None:
        lkapi = self._new_lkapi()
        try:
            response = await lkapi.room.list_participants(
                api.ListParticipantsRequest(room=self.room_name)
            )
            items = []
            for participant in getattr(response, "participants", []) or []:
                items.append(
                    {
                        "identity": str(getattr(participant, "identity", "") or ""),
                        "name": str(getattr(participant, "name", "") or ""),
                        "kind": str(getattr(participant, "kind", "") or ""),
                        "state": str(getattr(participant, "state", "") or ""),
                        "metadata": str(getattr(participant, "metadata", "") or ""),
                    }
                )
            self.participants = items
            self.updated_at = _utc_now_iso()
        except Exception as exc:
            print(f"[session_manager] list_participants failed err={exc}")
        finally:
            await lkapi.aclose()

    async def _remove_agent_participants(self) -> None:
        lkapi = self._new_lkapi()
        try:
            response = await lkapi.room.list_participants(
                api.ListParticipantsRequest(room=self.room_name)
            )
            for participant in getattr(response, "participants", []) or []:
                identity = str(getattr(participant, "identity", "") or "")
                kind = str(getattr(participant, "kind", "") or "")
                if not _identity_is_agent(identity, kind):
                    continue
                removed = False
                for attempt in range(3):
                    try:
                        await lkapi.room.remove_participant(
                            api.RoomParticipantIdentity(room=self.room_name, identity=identity)
                        )
                        print(f"[session_manager] removed agent participant identity={identity}")
                        removed = True
                        break
                    except Exception as exc:
                        print(
                            f"[session_manager] remove agent failed identity={identity} "
                            f"attempt={attempt + 1}/3 err={exc}"
                        )
                        if attempt < 2:
                            await asyncio.sleep(1.5)
                if not removed:
                    print(
                        f"[session_manager] WARNING: could not remove zombie agent "
                        f"identity={identity} after 3 attempts — LiveKit restart may be needed"
                    )
            if self.active_dispatch_id:
                try:
                    await lkapi.agent_dispatch.delete_dispatch(self.active_dispatch_id, self.room_name)
                    print(f"[session_manager] deleted active dispatch id={self.active_dispatch_id}")
                except Exception as exc:
                    print(f"[session_manager] delete active dispatch failed id={self.active_dispatch_id} err={exc}")
        finally:
            self.active_dispatch_id = ""
            self.dispatch_started_monotonic = 0.0
            await lkapi.aclose()

    async def _dispatch_warm_agent(self) -> None:
        """Dispatch agent into the room in warm standby mode.

        The agent connects, sets up its OpenAI Realtime session, and waits for
        an activation signal before greeting the user.
        """
        async with self._lock:
            if self.agent_deployed or not self._bootstrap_complete:
                return
            metadata = json.dumps({"warm": True})
            lkapi = self._new_lkapi()
            try:
                dispatch = await lkapi.agent_dispatch.create_dispatch(
                    api.CreateAgentDispatchRequest(
                        agent_name=self.agent_name,
                        room=self.room_name,
                        metadata=metadata,
                    )
                )
                self.active_dispatch_id = str(getattr(dispatch, "id", "") or "")
                self.agent_deployed = True
                self.warm_agent_ready = False
                self.warm_activation_pending = False
                self.session_state = "warm"
                self.dispatch_started_monotonic = time.monotonic()
                self.updated_at = _utc_now_iso()
                print(
                    f"[session_manager] warm agent dispatched name={self.agent_name} "
                    f"room={self.room_name} dispatch_id={self.active_dispatch_id}"
                )
                self._set_component_state(
                    "session-manager",
                    state="ready",
                    detail="warm agent standing by",
                    healthy=True,
                    source="internal",
                )
            except Exception as exc:
                self.session_state = "idle"
                self.agent_deployed = False
                self.active_dispatch_id = ""
                self._set_component_state(
                    "session-manager",
                    state="degraded",
                    detail=f"warm dispatch failed: {exc}",
                    healthy=False,
                    source="internal",
                )
                print(f"[session_manager] warm dispatch failed err={exc}")
            finally:
                await lkapi.aclose()

    async def _activate_warm_agent(self) -> None:
        """Send activation signal to the warm agent via LiveKit data channel."""
        self.conversation_id = uuid.uuid4().hex[:10]
        self.session_state = "active"
        self.warm_activation_pending = False
        self.dispatch_started_monotonic = time.monotonic()
        self._append_session_marker(f"New session · {self.conversation_id}")
        self.updated_at = _utc_now_iso()

        payload = json.dumps({
            "action": "activate",
            "conversation_id": self.conversation_id,
        }).encode("utf-8")
        lkapi = self._new_lkapi()
        try:
            await lkapi.room.send_data(
                api.SendDataRequest(
                    room=self.room_name,
                    data=payload,
                    topic="session-control",
                )
            )
            print(
                f"[session_manager] activated warm agent "
                f"conversation_id={self.conversation_id}"
            )
        except Exception as exc:
            print(f"[session_manager] activate signal failed err={exc}")
        finally:
            await lkapi.aclose()

    async def dispatch_agent(self) -> None:
        """Activate a warm agent, or cold-dispatch if none is warm."""
        async with self._lock:
            if not self._bootstrap_complete:
                return
            if self.session_state == "warm" and self.agent_deployed:
                if self.warm_agent_ready:
                    await self._activate_warm_agent()
                    return
                print(
                    "[session_manager] warm standby still connecting "
                    "— queueing activation request"
                )
                self.warm_activation_pending = True
                self.updated_at = _utc_now_iso()
                return
            if self.agent_deployed:
                return
            # Fallback: cold dispatch (no warm agent available)
            self.conversation_id = uuid.uuid4().hex[:10]
            self.session_state = "starting"
            self.dispatch_started_monotonic = time.monotonic()
            metadata = json.dumps({"conversation_id": self.conversation_id})
            lkapi = self._new_lkapi()
            try:
                dispatch = await lkapi.agent_dispatch.create_dispatch(
                    api.CreateAgentDispatchRequest(
                        agent_name=self.agent_name,
                        room=self.room_name,
                        metadata=metadata,
                    )
                )
                self.active_dispatch_id = str(getattr(dispatch, "id", "") or "")
                self.agent_deployed = True
                self.warm_agent_ready = False
                self.warm_activation_pending = False
                self.session_state = "active"
                self._append_session_marker(f"New session · {self.conversation_id}")
                self.updated_at = _utc_now_iso()
                print(
                    f"[session_manager] cold-dispatched agent name={self.agent_name} "
                    f"room={self.room_name} conversation_id={self.conversation_id} dispatch_id={self.active_dispatch_id}"
                )
                self._set_component_state(
                    "session-manager",
                    state="ready",
                    detail="agent dispatched",
                    healthy=True,
                    source="internal",
                )
            except Exception as exc:
                self.session_state = "idle"
                self.conversation_id = ""
                self._clear_agent_runtime_state()
                self._set_component_state(
                    "session-manager",
                    state="degraded",
                    detail=f"dispatch failed: {exc}",
                    healthy=False,
                    source="internal",
                )
                print(f"[session_manager] dispatch failed err={exc}")
            finally:
                await lkapi.aclose()

    async def end_session(self, reason: str) -> None:
        async with self._lock:
            if not self.agent_deployed and self.session_state == "idle":
                return
            self.session_state = "ending"
            print(f"[session_manager] ending session reason={reason}")
            ended_conversation_id = self.conversation_id
            await self._remove_agent_participants()
            self._clear_agent_runtime_state()
            self.conversation_id = ""
            self.last_user_activity_monotonic = 0.0
            self.last_agent_activity_monotonic = 0.0
            if ended_conversation_id:
                self._append_session_marker(
                    f"Session ended · {ended_conversation_id} · {reason}"
                )
            self.session_state = "cooldown"
            self.updated_at = _utc_now_iso()
        await asyncio.sleep(SESSION_COOLDOWN_SEC)
        async with self._lock:
            self.session_state = "idle"
            self.updated_at = _utc_now_iso()
            self._set_component_state(
                "session-manager",
                state="ready",
                detail="idle",
                healthy=True,
                source="internal",
            )
        # Pre-dispatch next warm agent so it's ready for the next user
        await self._dispatch_warm_agent()

    async def record_activity(self, source: str, level: float | None = None) -> None:
        now = time.monotonic()
        activity_at = _utc_now_iso()
        if source == SESSION_SOURCE_USER:
            if now - self.last_user_activity_monotonic < SESSION_ACTIVITY_DEBOUNCE_SEC:
                return
            self.last_user_activity_monotonic = now
            self.last_user_activity_at = activity_at
            if level is not None:
                self.mic_level = max(0.0, min(1.0, level))
            if self.session_state == "warm":
                if self.warm_agent_ready:
                    await self.dispatch_agent()
                else:
                    self.warm_activation_pending = True
            elif not self.agent_deployed and self.session_state == "idle":
                await self.dispatch_agent()
        elif source == SESSION_SOURCE_AGENT:
            if now - self.last_agent_activity_monotonic < SESSION_ACTIVITY_DEBOUNCE_SEC:
                return
            self.last_agent_activity_monotonic = now
            self.last_agent_activity_at = activity_at
            if level is not None:
                self.agent_audio_level = max(0.0, min(1.0, level))

    def _append_transcript(self, speaker: str, text: str, *, kind: str = "message") -> None:
        clean = " ".join(str(text).strip().split())
        if not clean:
            return
        item = {"speaker": speaker, "text": clean, "at": _utc_now_iso(), "kind": kind}
        self.transcript_items.append(item)
        if speaker == "Pepper":
            self.last_pepper_text = clean
        elif speaker == "User":
            self.last_user_text = clean

    def _append_session_marker(self, text: str) -> None:
        self._append_transcript("System", text, kind="session")

    def _append_session_marker_once(self, text: str) -> None:
        clean = " ".join(str(text).strip().split())
        if not clean:
            return
        last_item = self.transcript_items[-1] if self.transcript_items else None
        if (
            last_item
            and last_item.get("kind") == "session"
            and str(last_item.get("text") or "").strip() == clean
        ):
            return
        self._append_session_marker(clean)

    def _idle_countdown_sec(self) -> float | None:
        if not self.agent_deployed or self.session_state == "warm":
            return None
        if self.last_user_activity_monotonic <= 0:
            return float(SESSION_IDLE_TIMEOUT_SEC)
        remaining = SESSION_IDLE_TIMEOUT_SEC - (
            time.monotonic() - self.last_user_activity_monotonic
        )
        return max(0.0, remaining)

    async def monitor_loop(self) -> None:
        while True:
            now = time.monotonic()
            if self._bootstrap_complete:
                await self._refresh_participants_once()
            if self.agent_deployed and self.session_state == "active":
                if self.last_user_activity_monotonic > 0:
                    idle_for = now - self.last_user_activity_monotonic
                    if idle_for >= SESSION_IDLE_TIMEOUT_SEC:
                        await self.end_session(reason=f"no_user_activity_{idle_for:.1f}s")
                elif self.last_user_activity_monotonic == 0 and self.last_agent_activity_monotonic == 0:
                    if (
                        self.dispatch_started_monotonic > 0
                        and (now - self.dispatch_started_monotonic) >= SESSION_PREROLL_ACTIVITY_SEC
                    ):
                        await self.end_session(reason="no_activity_after_dispatch")
            # Re-dispatch warm agent if it never becomes ready
            if self.session_state == "warm" and self.agent_deployed:
                if (
                    not self.warm_agent_ready
                    and self.dispatch_started_monotonic > 0
                    and (now - self.dispatch_started_monotonic) >= WARM_AGENT_JOIN_TIMEOUT_SEC
                ):
                    print("[session_manager] warm agent never became ready — re-dispatching")
                    await self.end_session(reason="warm_agent_timeout")
            await asyncio.sleep(LIVEKIT_STATUS_POLL_INTERVAL_SEC)

    async def handle_status(self, request: web.Request) -> web.Response:
        del request
        payload = {
            "room_name": self.room_name,
            "session_state": self.session_state,
            "agent_deployed": self.agent_deployed,
            "warm_agent_ready": self.warm_agent_ready,
            "warm_activation_pending": self.warm_activation_pending,
            "conversation_id": self.conversation_id,
            "last_user_activity_at": self.last_user_activity_at,
            "last_agent_activity_at": self.last_agent_activity_at,
            "updated_at": self.updated_at,
            "participants": self.participants,
            "agent_name": self.agent_name,
            "transcript_items": list(self.transcript_items),
            "last_user_text": self.last_user_text,
            "last_pepper_text": self.last_pepper_text,
            "mic_level": self.mic_level,
            "mic_muted": self.mic_muted,
            "agent_speaking": self.agent_speaking,
            "agent_audio_level": self.agent_audio_level,
            "idle_countdown_sec": self._idle_countdown_sec(),
            "watchdog": dict(self.watchdog_status),
            "components": sorted(
                (
                    {
                        "name": item.get("name", ""),
                        "state": item.get("state", ""),
                        "detail": item.get("detail", ""),
                        "healthy": bool(item.get("healthy", False)),
                        "source": item.get("source", ""),
                        "updated_at": item.get("updated_at", ""),
                    }
                    for item in self.components.values()
                ),
                key=lambda item: item["name"],
            ),
        }
        return web.json_response(payload)

    async def handle_activity(self, request: web.Request) -> web.Response:
        data = await request.json()
        source = str(data.get("source") or "").strip().lower()
        if source not in {SESSION_SOURCE_USER, SESSION_SOURCE_AGENT}:
            return web.json_response({"ok": False, "error": "invalid source"}, status=400)
        level = data.get("level")
        try:
            level_value = float(level) if level is not None else None
        except Exception:
            level_value = None
        await self.record_activity(source, level=level_value)
        return web.json_response({"ok": True, "source": source})

    async def handle_debug_event(self, request: web.Request) -> web.Response:
        data = await request.json()
        event_type = str(data.get("event") or "").strip().lower()
        speaker = str(data.get("speaker") or "").strip()
        text = str(data.get("text") or "").strip()
        level = data.get("level")
        active = bool(data.get("active"))
        try:
            level_value = float(level) if level is not None else None
        except Exception:
            level_value = None

        if event_type == "transcript" and speaker and text:
            kind = str(data.get("kind") or "message").strip()
            self._append_transcript(speaker, text, kind=kind)
        elif event_type == "mic_level" and level_value is not None:
            self.mic_level = max(0.0, min(1.0, level_value))
        elif event_type == "agent_level" and level_value is not None:
            self.agent_audio_level = max(0.0, min(1.0, level_value))
        elif event_type == "agent_speaking":
            self.agent_speaking = active
        elif event_type == "warm_ready":
            self.warm_agent_ready = True
            self._append_session_marker_once("Agent ready")
            if (
                self.session_state == "warm"
                and self.agent_deployed
                and (self.warm_activation_pending or self.pending_user_texts)
            ):
                asyncio.create_task(self.dispatch_agent())

        self.updated_at = _utc_now_iso()
        return web.json_response({"ok": True})

    async def handle_watchdog_status(self, request: web.Request) -> web.Response:
        data = await request.json()
        summary = " ".join(str(data.get("summary") or "").strip().split()) or "watchdog update"
        pepper_reachable = bool(data.get("pepper_reachable", False))
        safe_startup_running = bool(data.get("safe_startup_running", False))
        last_result = " ".join(str(data.get("last_result") or "").strip().split())
        healthy = bool(data.get("healthy", pepper_reachable or safe_startup_running))
        updated_at = _utc_now_iso()
        self.watchdog_status = {
            "summary": summary,
            "pepper_reachable": pepper_reachable,
            "safe_startup_running": safe_startup_running,
            "last_result": last_result,
            "updated_at": updated_at,
        }
        detail_parts = [
            "Pepper reachable" if pepper_reachable else "Pepper offline",
            "safe startup running" if safe_startup_running else "safe startup idle",
        ]
        if last_result:
            detail_parts.append(last_result)
        self._set_component_state(
            "safe-startup",
            state=summary,
            detail=" | ".join(detail_parts),
            healthy=healthy,
            source="service",
        )
        return web.json_response({"ok": True, "updated_at": updated_at})

    async def handle_component_status(self, request: web.Request) -> web.Response:
        data = await request.json()
        name = " ".join(str(data.get("name") or "").strip().split())
        state = " ".join(str(data.get("state") or "").strip().split())
        detail = " ".join(str(data.get("detail") or "").strip().split())
        healthy = bool(data.get("healthy", True))
        if not name or not state:
            return web.json_response({"ok": False, "error": "name and state required"}, status=400)
        if name == "bridge":
            return web.json_response({"ok": True, "name": name, "ignored": True})
        self._set_component_state(
            name,
            state=state,
            detail=detail,
            healthy=healthy,
            source="service",
        )
        return web.json_response({"ok": True, "name": name, "state": state})

    async def handle_mic_toggle(self, request: web.Request) -> web.Response:
        del request
        self.mic_muted = not self.mic_muted
        self._persist_state()
        self.updated_at = _utc_now_iso()
        return web.json_response({"ok": True, "mic_muted": self.mic_muted})

    async def handle_text_send(self, request: web.Request) -> web.Response:
        data = await request.json()
        text = " ".join(str(data.get("text") or "").strip().split())
        if not text:
            return web.json_response({"ok": False, "error": "text required"}, status=400)
        now = time.monotonic()
        self.last_user_activity_monotonic = now
        self.last_user_activity_at = _utc_now_iso()
        if self.session_state in ("warm", "idle"):
            await self.dispatch_agent()
        item = {"id": uuid.uuid4().hex[:10], "text": text}
        self.pending_user_texts.append(item)
        self.updated_at = _utc_now_iso()
        return web.json_response({"ok": True, "queued": item})

    async def handle_user_client_state(self, request: web.Request) -> web.Response:
        del request
        return web.json_response(
            {
                "mic_muted": self.mic_muted,
                "agent_deployed": self.agent_deployed,
                "session_state": self.session_state,
                "pending_texts": list(self.pending_user_texts),
            }
        )

    async def handle_user_client_ack(self, request: web.Request) -> web.Response:
        data = await request.json()
        ack_id = str(data.get("id") or "").strip()
        if ack_id:
            self.pending_user_texts = [item for item in self.pending_user_texts if item.get("id") != ack_id]
        return web.json_response({"ok": True})

    async def handle_reset(self, request: web.Request) -> web.Response:
        del request
        await self.end_session(reason="manual_reset")
        return web.json_response({"ok": True, "session_state": self.session_state})

    async def handle_docker_containers(self, request: web.Request) -> web.Response:
        del request
        try:
            containers = await self._list_docker_containers()
            return web.json_response({"ok": True, "containers": containers})
        except Exception as exc:
            return web.json_response(
                {"ok": False, "containers": [], "error": f"docker unavailable: {exc}"},
                status=503,
            )

    async def handle_docker_logs(self, request: web.Request) -> web.Response:
        container_id = str(request.query.get("container") or "").strip()
        if not container_id:
            return web.json_response(
                {"ok": False, "error": "container query param required"},
                status=400,
            )
        try:
            logs = await self._docker_get_text(
                f"/containers/{container_id}/logs",
                params={
                    "stdout": "1",
                    "stderr": "1",
                    "tail": str(DOCKER_LOG_TAIL_LINES),
                    "timestamps": "1",
                },
            )
            return web.json_response({"ok": True, "logs": logs})
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"failed to fetch logs: {exc}"},
                status=503,
            )

    async def handle_root(self, request: web.Request) -> web.Response:
        del request
        return web.Response(text=CHAT_HTML, content_type="text/html")

    async def handle_debug_root(self, request: web.Request) -> web.Response:
        del request
        return web.Response(text=STATUS_HTML, content_type="text/html")

    async def handle_console_proxy(self, request: web.Request) -> web.Response:
        """Reverse-proxy requests to the dev-console service."""
        subpath = request.match_info.get("path", "")
        target = f"{self.dev_console_url}/{subpath}"
        qs = request.query_string
        if qs:
            target = f"{target}?{qs}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method=request.method,
                    url=target,
                    headers={k: v for k, v in request.headers.items()
                             if k.lower() not in ("host", "transfer-encoding")},
                    data=await request.read() if request.can_read_body else None,
                ) as resp:
                    body = await resp.read()
                    headers = {k: v for k, v in resp.headers.items()
                               if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")}
                    # Rewrite API base so the SPA fetches hit /console/api/...
                    if not subpath and resp.content_type and "html" in resp.content_type:
                        text = body.decode("utf-8", errors="replace")
                        text = text.replace("const API = '';", "const API = '/console';", 1)
                        body = text.encode("utf-8")
                    return web.Response(body=body, status=resp.status, headers=headers)
        except Exception as exc:
            print(f"[session_manager] console proxy error: {exc}")
            return web.Response(text=f"Dev console unavailable: {exc}", status=502)

    async def start(self) -> None:
        app = web.Application()
        app.add_routes(
            [
                web.get("/", self.handle_root),
                web.get("/debug", self.handle_debug_root),
                web.get("/api/status", self.handle_status),
                web.post("/api/activity", self.handle_activity),
                web.post("/api/debug-event", self.handle_debug_event),
                web.post("/api/watchdog-status", self.handle_watchdog_status),
                web.post("/api/component-status", self.handle_component_status),
                web.post("/api/control/mic", self.handle_mic_toggle),
                web.post("/api/control/text", self.handle_text_send),
                web.post("/api/control/reset", self.handle_reset),
                web.get("/api/docker/containers", self.handle_docker_containers),
                web.get("/api/docker/logs", self.handle_docker_logs),
                web.get("/api/user-client/state", self.handle_user_client_state),
                web.post("/api/user-client/ack", self.handle_user_client_ack),
                web.route("*", "/console", self.handle_console_proxy),
                web.route("*", "/console/{path:.*}", self.handle_console_proxy),
            ]
        )
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, SESSION_MANAGER_HOST, SESSION_MANAGER_PORT)
        await site.start()
        print(
            f"[session_manager] dashboard=http://{SESSION_MANAGER_HOST}:{SESSION_MANAGER_PORT} "
            f"room={self.room_name} agent_name={self.agent_name}"
        )
        self._bg_tasks.append(asyncio.create_task(self.bootstrap_loop()))
        self._bg_tasks.append(asyncio.create_task(self.monitor_loop()))
        self._bg_tasks.append(asyncio.create_task(self.probe_components_loop()))
        try:
            while True:
                await asyncio.sleep(3600)
        finally:
            for task in self._bg_tasks:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
            await runner.cleanup()


async def main() -> None:
    manager = SessionManager()
    await manager.start()


if __name__ == "__main__":
    asyncio.run(main())
