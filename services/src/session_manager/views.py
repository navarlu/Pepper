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
  const dd = String(date.getDate()).padStart(2,"0");
  const mm = String(date.getMonth()+1).padStart(2,"0");
  const yyyy = date.getFullYear();
  const hh = String(date.getHours()).padStart(2,"0");
  const min = String(date.getMinutes()).padStart(2,"0");
  const ss = String(date.getSeconds()).padStart(2,"0");
  return `${dd}/${mm}/${yyyy}, ${hh}:${min}:${ss}`;
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
  align-items:center;
  gap:12px;
}
.chat-header-actions {
  display:flex;
  align-items:center;
  gap:10px;
}
.volume-control {
  display:inline-flex;
  align-items:center;
  gap:8px;
  min-height:50px;
  padding:0 14px;
  border-radius:18px;
  background:rgba(255,255,255,0.9);
  border:1px solid rgba(207,217,230,0.95);
  box-shadow:0 8px 18px rgba(40, 74, 111, 0.05);
}
.volume-control label {
  font-size:12px;
  font-weight:700;
  letter-spacing:0.08em;
  text-transform:uppercase;
  color:var(--muted);
}
.volume-control input[type="range"] {
  width:110px;
  accent-color:var(--accent);
  cursor:pointer;
}
.volume-value {
  min-width:44px;
  text-align:right;
  color:var(--text);
  font-size:13px;
  font-weight:600;
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
.chat-header h2 {
  margin:0;
  font-size:18px;
  font-weight:500;
  letter-spacing:-0.02em;
  color:#1a1a1a;
}
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
            <button class="primary" id="agentModeBtn" title="Toggle between OpenAI and Local LLM">OpenAI</button>
            <div class="volume-control" title="Pepper output volume used by the bridge">
              <label for="audioVolumeSelect">Volume</label>
              <input id="audioVolumeSelect" type="range" min="0" max="100" step="1" value="55">
              <span class="volume-value mono" id="audioVolumeValue">55%</span>
            </div>
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
            <tr><th>LLM</th><td class="signal-value mono" id="chatAgentMode">-</td></tr>
            <tr><th>Pipeline</th><td class="signal-value mono" id="chatWarmState">-</td></tr>
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
  const dd = String(date.getDate()).padStart(2,"0");
  const mm = String(date.getMonth()+1).padStart(2,"0");
  const yyyy = date.getFullYear();
  const hh = String(date.getHours()).padStart(2,"0");
  const min = String(date.getMinutes()).padStart(2,"0");
  const ss = String(date.getSeconds()).padStart(2,"0");
  return `${dd}/${mm}/${yyyy}, ${hh}:${min}:${ss}`;
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
  if (data.agent_mode === "local" && !data.local_llm_healthy) return "vLLM unreachable";
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
  if (data.session_state === "idle") {
    if (data.agent_mode === "local" && !data.local_llm_healthy) return "idle (vLLM down)";
    return "idle (waiting for user)";
  }
  if (data.session_state === "cooldown") return "cooldown";
  if (data.session_state === "ending") return "ending session";
  if (data.session_state === "starting") return "starting session";
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
    if (data.agent_mode === "local") {
      const llmStatus = data.local_llm_healthy ? "connected" : "offline";
      text("chatAgentMode", `Local (Qwen) · vLLM ${llmStatus}`);
    } else {
      text("chatAgentMode", "OpenAI Realtime");
    }
    const audioVolumeSelect = document.getElementById("audioVolumeSelect");
    const audioVolume = Number(data.local_audio_volume ?? 55);
    audioVolumeSelect.value = String(audioVolume);
    text("audioVolumeValue", `${audioVolume}%`);
    const modeBtn = document.getElementById("agentModeBtn");
    if (data.agent_mode === "local") {
      modeBtn.textContent = data.local_llm_healthy ? "Local" : "Local (LLM down)";
      modeBtn.className = "primary";
      if (data.local_llm_healthy) {
        modeBtn.style.background = "var(--good-soft)";
        modeBtn.style.color = "var(--good)";
        modeBtn.style.borderColor = "rgba(90,168,120,0.3)";
      } else {
        modeBtn.style.background = "#fff0f0";
        modeBtn.style.color = "var(--hot)";
        modeBtn.style.borderColor = "rgba(200,87,87,0.3)";
      }
    } else {
      modeBtn.textContent = "OpenAI";
      modeBtn.className = "primary";
      modeBtn.style.background = "";
      modeBtn.style.color = "";
      modeBtn.style.borderColor = "";
    }
    const chatLivePill = document.getElementById("chatLivePill");
    let pillClass = "pill";
    let pillText = "Waiting for session";
    if (data.session_state === "active") {
      pillClass = "pill good";
      pillText = data.agent_mode === "local" ? "Live (Local)" : "Live session";
    } else if (data.agent_mode === "local" && !data.local_llm_healthy) {
      pillClass = "pill hot";
      pillText = "vLLM offline";
    } else if (data.agent_deployed && data.warm_agent_ready) {
      pillClass = "pill good";
      pillText = "Agent ready";
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
document.getElementById("agentModeBtn").addEventListener("click", async () => {
  await postJson("/api/control/agent-mode", {});
  refresh();
});
document.getElementById("audioVolumeSelect").addEventListener("input", (event) => {
  const value = Number.parseInt(event.target.value, 10);
  text("audioVolumeValue", `${value}%`);
});
document.getElementById("audioVolumeSelect").addEventListener("change", async (event) => {
  const value = Number.parseInt(event.target.value, 10);
  await postJson("/api/control/audio-volume", { volume: value });
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


SESSIONS_HTML = """<!doctype html>
<meta charset="utf-8">
<title>Pepper Session Logs</title>
<style>
:root {
  --bg: #f4f6f9; --panel: #fff; --line: #dde3ea; --text: #1a2b3c;
  --muted: #6b7f94; --accent: #2968d8; --accent-soft: #edf4ff;
  --user: #2968d8; --agent: #3a9e5c; --tool: #c77d20; --error: #c85757;
  --event: #8b8fa3; --metric: #7c5cbf; --shadow: 0 2px 12px rgba(0,0,0,0.06);
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif;
       background: var(--bg); color: var(--text); padding: 20px; }
a { color: var(--accent); text-decoration: none; }
a:hover { text-decoration: underline; }

.header { display: flex; align-items: center; gap: 16px; margin-bottom: 24px; }
.header h1 { font-size: 22px; font-weight: 600; }
.header .back { font-size: 14px; color: var(--muted); }

/* Session list */
.sessions-table { width: 100%; border-collapse: collapse; background: var(--panel);
                  border-radius: 10px; overflow: hidden; box-shadow: var(--shadow); }
.sessions-table th { text-align: left; padding: 10px 14px; font-size: 12px;
                     font-weight: 600; color: var(--muted); text-transform: uppercase;
                     letter-spacing: 0.5px; border-bottom: 2px solid var(--line); }
.sessions-table td { padding: 10px 14px; font-size: 13px; border-bottom: 1px solid var(--line); }
.sessions-table tr:last-child td { border-bottom: none; }
.sessions-table tr:hover td { background: var(--accent-soft); cursor: pointer; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 4px;
         font-size: 11px; font-weight: 600; }
.badge-openai { background: #e8f5e9; color: #2e7d32; }
.badge-local { background: #fff3e0; color: #e65100; }
.empty { text-align: center; padding: 40px; color: var(--muted); }

/* Detail view */
.detail-header { background: var(--panel); border-radius: 10px; padding: 18px 22px;
                 box-shadow: var(--shadow); margin-bottom: 18px; }
.detail-header h2 { font-size: 18px; margin-bottom: 8px; }
.detail-meta { display: flex; flex-wrap: wrap; gap: 20px; font-size: 13px; color: var(--muted); }
.detail-meta strong { color: var(--text); }

.timeline { display: flex; flex-direction: column; gap: 6px; }
.ev { background: var(--panel); border-radius: 8px; padding: 10px 14px;
      box-shadow: var(--shadow); border-left: 4px solid var(--line); font-size: 13px; }
.ev-user_speech { border-left-color: var(--user); }
.ev-agent_speech { border-left-color: var(--agent); }
.ev-tool_call { border-left-color: var(--tool); }
.ev-error { border-left-color: var(--error); background: #fff5f5; }
.ev-session_event { border-left-color: var(--event); opacity: 0.7; }
.ev-pipeline_metric { border-left-color: var(--metric); background: #f8f6ff; }

.ev-time { font-size: 11px; color: var(--muted); margin-bottom: 2px; }
.ev-label { font-size: 11px; font-weight: 700; text-transform: uppercase; letter-spacing: 0.5px;
            margin-bottom: 4px; }
.ev-label-user_speech { color: var(--user); }
.ev-label-agent_speech { color: var(--agent); }
.ev-label-tool_call { color: var(--tool); }
.ev-label-error { color: var(--error); }
.ev-label-session_event { color: var(--event); }
.ev-label-pipeline_metric { color: var(--metric); }
.metric-bar { display: flex; gap: 16px; flex-wrap: wrap; font-size: 12px; }
.metric-bar .metric-val { font-weight: 700; }
.metric-bar .metric-lbl { color: var(--muted); }

.ev-text { line-height: 1.5; }
.ev-duration { font-size: 11px; color: var(--muted); margin-top: 4px; }
.ev-details { margin-top: 6px; }
.ev-details summary { font-size: 12px; color: var(--muted); cursor: pointer; }
.ev-details pre { margin-top: 4px; font-size: 11px; background: #f6f8fa;
                  padding: 8px 10px; border-radius: 6px; overflow-x: auto;
                  max-height: 300px; white-space: pre-wrap; word-break: break-word; }
</style>

<div class="header">
  <h1 id="pageTitle">Session Logs</h1>
  <a href="/" class="back">&larr; Dashboard</a>
  <a href="#" class="back" id="backToList" style="display:none">&larr; All Sessions</a>
</div>
<div id="content"><div class="empty">Loading...</div></div>

<script>
const $content = document.getElementById("content");
const $title = document.getElementById("pageTitle");
const $backToList = document.getElementById("backToList");

function fmtTime(iso) {
  if (!iso) return "";
  try { return new Date(iso).toLocaleString(); } catch { return iso; }
}
function fmtDuration(sec) {
  if (!sec && sec !== 0) return "";
  if (sec < 60) return sec.toFixed(1) + "s";
  return Math.floor(sec/60) + "m " + Math.round(sec%60) + "s";
}

async function loadList() {
  $backToList.style.display = "none";
  $title.textContent = "Session Logs";
  try {
    const resp = await fetch("/api/sessions");
    const data = await resp.json();
    if (!data.sessions || !data.sessions.length) {
      $content.innerHTML = '<div class="empty">No sessions recorded yet.</div>';
      return;
    }
    let html = '<table class="sessions-table"><thead><tr>' +
      '<th>ID</th><th>Mode</th><th>Started</th><th>Duration</th>' +
      '<th>Turns</th><th>Tools</th><th>Errors</th><th>End Reason</th></tr></thead><tbody>';
    for (const s of data.sessions) {
      const sm = s.summary || {};
      const modeClass = s.agent_mode === "openai" ? "badge-openai" : "badge-local";
      html += '<tr onclick="loadDetail(\\'' + s.filename + '\\')">' +
        '<td><code>' + (s.conversation_id||"").substring(0,8) + '</code></td>' +
        '<td><span class="badge ' + modeClass + '">' + (s.agent_mode||"?") + '</span></td>' +
        '<td>' + fmtTime(s.started_at) + '</td>' +
        '<td>' + fmtDuration(s.duration_sec) + '</td>' +
        '<td>' + (sm.turns||0) + '</td>' +
        '<td>' + (sm.tool_calls||0) + '</td>' +
        '<td>' + (sm.errors||0) + '</td>' +
        '<td style="font-size:12px;color:var(--muted)">' + (s.end_reason||"") + '</td></tr>';
    }
    html += '</tbody></table>';
    $content.innerHTML = html;
  } catch (e) {
    $content.innerHTML = '<div class="empty">Failed to load sessions: ' + e.message + '</div>';
  }
}

async function loadDetail(filename) {
  $backToList.style.display = "inline";
  $backToList.onclick = (e) => { e.preventDefault(); loadList(); };
  try {
    const resp = await fetch("/api/sessions/" + encodeURIComponent(filename));
    const data = await resp.json();
    $title.textContent = "Session " + (data.conversation_id || "");
    const sm = data.summary || {};
    const modeClass = data.agent_mode === "openai" ? "badge-openai" : "badge-local";
    let html = '<div class="detail-header">' +
      '<h2>' + (data.conversation_id||"") + '</h2>' +
      '<div class="detail-meta">' +
        '<span>Mode: <span class="badge ' + modeClass + '">' + (data.agent_mode||"") + '</span></span>' +
        '<span>Start: <strong>' + fmtTime(data.started_at) + '</strong></span>' +
        '<span>Duration: <strong>' + fmtDuration(data.duration_sec) + '</strong></span>' +
        '<span>Turns: <strong>' + (sm.turns||0) + '</strong></span>' +
        '<span>Tool calls: <strong>' + (sm.tool_calls||0) + '</strong></span>' +
        '<span>Errors: <strong>' + (sm.errors||0) + '</strong></span>' +
        '<span>End: <strong>' + (data.end_reason||"") + '</strong></span>' +
      '</div></div>';

    html += '<div class="timeline">';
    const labels = {
      user_speech: "User", agent_speech: "Pepper",
      tool_call: "Tool", error: "Error", session_event: "Event",
      pipeline_metric: "Metric"
    };
    for (const ev of (data.events || [])) {
      const tp = ev.type || "session_event";
      html += '<div class="ev ev-' + tp + '">';
      html += '<div class="ev-time">' + fmtTime(ev.t) + '</div>';
      html += '<div class="ev-label ev-label-' + tp + '">' + (labels[tp]||tp) + '</div>';

      if (tp === "user_speech" || tp === "agent_speech") {
        html += '<div class="ev-text">' + escHtml(ev.text||"") + '</div>';
      } else if (tp === "tool_call") {
        html += '<div class="ev-text"><strong>' + escHtml(ev.tool||"") + '</strong>';
        if (ev.duration_ms != null) html += ' <span class="ev-duration">' + ev.duration_ms.toFixed(1) + 'ms</span>';
        if (ev.error) html += ' <span style="color:var(--error)">(' + escHtml(ev.error) + ')</span>';
        html += '</div>';
        html += '<div class="ev-details"><details><summary>args &amp; result</summary>';
        html += '<pre>' + escHtml(JSON.stringify(ev.args, null, 2)) + '</pre>';
        html += '<pre>' + escHtml(JSON.stringify(ev.result, null, 2)) + '</pre>';
        html += '</details></div>';
      } else if (tp === "pipeline_metric") {
        const stage = (ev.stage||"").toUpperCase();
        html += '<div class="metric-bar"><span><span class="metric-lbl">' + stage + '</span> <span class="metric-val">' + (ev.duration_ms||0).toFixed(1) + 'ms</span></span>';
        if (ev.ttft_ms != null) html += '<span><span class="metric-lbl">TTFT</span> <span class="metric-val">' + ev.ttft_ms.toFixed(1) + 'ms</span></span>';
        if (ev.tokens_per_second) html += '<span><span class="metric-val">' + ev.tokens_per_second.toFixed(1) + '</span> <span class="metric-lbl">tok/s</span></span>';
        if (ev.completion_tokens) html += '<span><span class="metric-val">' + ev.completion_tokens + '</span> <span class="metric-lbl">tokens</span></span>';
        if (ev.audio_duration_ms) html += '<span><span class="metric-lbl">audio</span> <span class="metric-val">' + (ev.audio_duration_ms/1000).toFixed(1) + 's</span></span>';
        if (ev.characters) html += '<span><span class="metric-val">' + ev.characters + '</span> <span class="metric-lbl">chars</span></span>';
        html += '</div>';
        if (ev.text) html += '<div class="ev-text" style="margin-top:4px;font-size:12px;color:var(--muted)">' + escHtml(ev.text) + '</div>';
      } else if (tp === "error") {
        html += '<div class="ev-text"><strong>' + escHtml(ev.source||"") + ':</strong> ' + escHtml(ev.message||"") + '</div>';
      } else if (tp === "session_event") {
        html += '<div class="ev-text">' + escHtml(ev.detail||"") + '</div>';
      }
      html += '</div>';
    }
    html += '</div>';
    $content.innerHTML = html;
  } catch (e) {
    $content.innerHTML = '<div class="empty">Failed to load session: ' + e.message + '</div>';
  }
}

function escHtml(s) {
  const d = document.createElement("div");
  d.textContent = s;
  return d.innerHTML;
}

// Check URL hash for direct link
if (location.hash && location.hash.startsWith("#detail:")) {
  loadDetail(location.hash.substring(8));
} else {
  loadList();
}
</script>
"""
