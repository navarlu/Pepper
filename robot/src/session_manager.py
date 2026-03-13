import asyncio
import contextlib
import json
import os
import socket
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

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
MAX_TRANSCRIPT_ITEMS = 40
COMPONENT_STALE_AFTER_SEC = 12.0
COMPONENT_PROBE_INTERVAL_SEC = 3.0
STATUS_HTML = """<!doctype html>
<meta charset="utf-8">
<title>Pepper Operator</title>
<style>
:root {
  --bg: #f4f7fb;
  --panel: rgba(255,255,255,0.92);
  --line: #d6e1ee;
  --text: #16324a;
  --muted: #5f7a92;
  --good: #2e9f6b;
  --warn: #d89a2b;
  --hot: #d85b5b;
  --accent: #2d6cdf;
}
html,body { margin:0; padding:0; background:
  radial-gradient(circle at top left, rgba(45,108,223,0.10), transparent 28%),
  radial-gradient(circle at top right, rgba(46,159,107,0.10), transparent 24%),
  linear-gradient(180deg, #f9fbfe, #eef4fa);
  color:var(--text); font-family: "Segoe UI", Arial, sans-serif; }
.page { max-width: 1280px; margin: 0 auto; padding: 28px 20px 40px; }
.hero { display:flex; justify-content:space-between; gap:16px; align-items:flex-end; margin-bottom:18px; }
.hero h1 { margin:0; font-size:32px; }
.hero p { margin:6px 0 0; color:var(--muted); }
.grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(260px, 1fr)); gap:14px; margin-bottom:14px; }
.card { background:var(--panel); border:1px solid var(--line); border-radius:16px; padding:16px; box-shadow:0 14px 32px rgba(62,91,121,0.10); backdrop-filter: blur(10px); }
.label { color:var(--muted); font-size:13px; text-transform:uppercase; letter-spacing:0.08em; margin-bottom:8px; }
.value { font-size:24px; font-weight:600; }
.pill { display:inline-block; padding:4px 10px; border-radius:999px; background:#e8f6ef; color:var(--good); font-size:13px; }
table { width:100%; border-collapse:collapse; }
th, td { text-align:left; padding:10px 8px; border-bottom:1px solid rgba(39,65,95,0.7); }
th { color:var(--muted); font-size:13px; font-weight:600; }
.mono { font-family: "SFMono-Regular", Consolas, monospace; font-size: 13px; }
.footer { color:var(--muted); font-size:13px; margin-top:12px; }
.stack { display:grid; grid-template-columns: 1.2fr 0.8fr; gap:14px; }
.meter { height:14px; background:#edf2f8; border:1px solid var(--line); border-radius:999px; overflow:hidden; }
.meter > div { height:100%; width:0%; background:linear-gradient(90deg, var(--good), var(--warn), var(--hot)); transition:width 120ms linear; }
.bubble-list { display:flex; flex-direction:column; gap:10px; max-height:420px; overflow:auto; }
.bubble { padding:12px 14px; border-radius:14px; line-height:1.35; }
.bubble.user { background:#edf4ff; }
.bubble.pepper { background:#eaf8f0; }
.bubble .speaker { font-size:12px; color:var(--muted); text-transform:uppercase; letter-spacing:0.08em; margin-bottom:6px; }
.controls { display:flex; gap:10px; align-items:center; flex-wrap:wrap; }
textarea { width:100%; min-height:92px; resize:vertical; background:white; color:var(--text); border:1px solid var(--line); border-radius:12px; padding:12px; font:inherit; }
button { background:var(--accent); color:white; border:none; border-radius:10px; padding:10px 14px; font:inherit; cursor:pointer; }
button.secondary { background:#6b7f94; }
button.warn { background:#c64e4e; }
.countdown { font-size:32px; font-weight:700; }
</style>
<div class="page">
  <div class="hero">
    <div>
      <h1>Pepper Operator</h1>
      <p>Permanent room orchestration, agent dispatch state, and joined participants.</p>
    </div>
    <div class="pill" id="pollState">Polling...</div>
  </div>
  <div class="grid">
    <div class="card"><div class="label">Room</div><div class="value" id="roomName">-</div></div>
    <div class="card"><div class="label">Session State</div><div class="value" id="sessionState">-</div></div>
    <div class="card"><div class="label">Agent Deployed</div><div class="value" id="agentDeployed">-</div></div>
    <div class="card"><div class="label">Conversation ID</div><div class="value mono" id="conversationId">-</div></div>
  </div>
  <div class="grid">
    <div class="card">
      <div class="label">Mic Level</div>
      <div class="meter"><div id="micLevelBar"></div></div>
      <div class="footer mono" id="micLevelText">-</div>
    </div>
    <div class="card">
      <div class="label">Pepper Level</div>
      <div class="meter"><div id="pepperLevelBar"></div></div>
      <div class="footer mono" id="pepperLevelText">-</div>
    </div>
    <div class="card">
      <div class="label">Pepper Speaking</div>
      <div class="value" id="pepperSpeaking">-</div>
    </div>
    <div class="card">
      <div class="label">Idle Countdown</div>
      <div class="countdown mono" id="idleCountdown">-</div>
    </div>
  </div>
  <div class="stack">
    <div class="card">
      <div class="label">Conversation</div>
      <div class="bubble-list" id="transcriptList"><div class="footer">No transcript yet.</div></div>
    </div>
    <div class="card">
      <div class="label">User Text Input</div>
      <textarea id="userText" placeholder="Send a text turn as the user"></textarea>
      <div class="controls" style="margin-top:10px;">
        <button id="sendBtn">Send</button>
        <button class="secondary" id="clearBtn">Clear</button>
        <button class="warn" id="muteBtn">Toggle Mic Mute</button>
        <button class="warn" id="resetBtn">Restart Session</button>
      </div>
      <div class="footer" id="muteState">Mic state: -</div>
    </div>
  </div>
  <div class="card">
    <div class="label">Participants</div>
    <table>
      <thead><tr><th>Identity</th><th>Name</th><th>Kind</th><th>State</th><th>Metadata</th></tr></thead>
      <tbody id="participantsBody"><tr><td colspan="5">Loading...</td></tr></tbody>
    </table>
  </div>
  <div class="card" style="margin-top:14px;">
    <div class="label">Components</div>
    <table>
      <thead><tr><th>Name</th><th>State</th><th>Healthy</th><th>Source</th><th>Detail</th><th>Updated</th></tr></thead>
      <tbody id="componentsBody"><tr><td colspan="6">Loading...</td></tr></tbody>
    </table>
  </div>
  <div class="grid" style="margin-top:14px;">
    <div class="card"><div class="label">Last User Activity</div><div class="value mono" id="userActivity">-</div></div>
    <div class="card"><div class="label">Last Agent Activity</div><div class="value mono" id="agentActivity">-</div></div>
    <div class="card"><div class="label">Updated</div><div class="value mono" id="updatedAt">-</div></div>
  </div>
  <div class="footer">This page is read-only. Session start/end is driven by mic activity and idle timeout.</div>
</div>
<script>
    function fmtTs(value) {
  if (!value) return "-";
  return new Date(value).toLocaleTimeString();
}
function text(el, value) {
  document.getElementById(el).textContent = value || "-";
}
function meter(el, value) {
  const pct = Math.max(0, Math.min(100, Math.round((value || 0) * 100)));
  document.getElementById(el).style.width = pct + "%";
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
    text("roomName", data.room_name);
    text("sessionState", data.session_state);
    text("agentDeployed", data.agent_deployed ? "yes" : "no");
    text("conversationId", data.conversation_id);
    text("userActivity", fmtTs(data.last_user_activity_at));
    text("agentActivity", fmtTs(data.last_agent_activity_at));
    text("updatedAt", fmtTs(data.updated_at));
    text("pepperSpeaking", data.agent_speaking ? "yes" : "no");
    text("idleCountdown", data.idle_countdown_sec != null ? `${data.idle_countdown_sec.toFixed(1)}s` : "waiting");
    text("micLevelText", `level=${(data.mic_level || 0).toFixed(3)}`);
    text("pepperLevelText", `level=${(data.agent_audio_level || 0).toFixed(3)}`);
    text("muteState", `Mic state: ${data.mic_muted ? "muted" : "live"}`);
    meter("micLevelBar", data.mic_level || 0);
    meter("pepperLevelBar", data.agent_audio_level || 0);
    const tbody = document.getElementById("participantsBody");
    const rows = (data.participants || []).map((item) => `
      <tr>
        <td class="mono">${item.identity || ""}</td>
        <td>${item.name || ""}</td>
        <td>${item.kind || ""}</td>
        <td>${item.state || ""}</td>
        <td class="mono">${item.metadata || ""}</td>
      </tr>
    `).join("");
    tbody.innerHTML = rows || '<tr><td colspan="5">No participants.</td></tr>';
    const componentsBody = document.getElementById("componentsBody");
    const componentRows = (data.components || []).map((item) => `
      <tr>
        <td class="mono">${item.name || ""}</td>
        <td>${item.state || ""}</td>
        <td>${item.healthy ? "yes" : "no"}</td>
        <td>${item.source || ""}</td>
        <td class="mono">${item.detail || ""}</td>
        <td class="mono">${fmtTs(item.updated_at)}</td>
      </tr>
    `).join("");
    componentsBody.innerHTML = componentRows || '<tr><td colspan="6">No component state.</td></tr>';
    const transcriptEl = document.getElementById("transcriptList");
    const transcriptRows = (data.transcript_items || []).map((item) => `
      <div class="bubble ${item.speaker === 'Pepper' ? 'pepper' : 'user'}">
        <div class="speaker">${item.speaker} · ${fmtTs(item.at)}</div>
        <div>${item.text || ""}</div>
      </div>
    `).join("");
    transcriptEl.innerHTML = transcriptRows || '<div class="footer">No transcript yet.</div>';
  } catch (err) {
    pill.textContent = "Disconnected";
  }
}
document.getElementById("sendBtn").addEventListener("click", async () => {
  const el = document.getElementById("userText");
  const textValue = el.value.trim();
  if (!textValue) return;
  await postJson("/api/control/text", { text: textValue });
  el.value = "";
  refresh();
});
document.getElementById("clearBtn").addEventListener("click", () => {
  document.getElementById("userText").value = "";
});
document.getElementById("muteBtn").addEventListener("click", async () => {
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
        self.session_file = Path(LIVEKIT_SESSION_FILE)
        self.api_key = _get_required_env("LIVEKIT_API_KEY")
        self.api_secret = _get_required_env("LIVEKIT_API_SECRET")
        self.agent_name = (os.getenv("PEPPER_AGENT_NAME") or AGENT_NAME_DEFAULT).strip() or AGENT_NAME_DEFAULT
        self.session_state = "idle"
        self.agent_deployed = False
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
                await self.write_session_snapshot()
                self._bootstrap_complete = True
                self._set_component_state(
                    "session-manager",
                    state="ready",
                    detail="dashboard and orchestration online",
                    healthy=True,
                    source="internal",
                )
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
                try:
                    await lkapi.room.remove_participant(
                        api.RoomParticipantIdentity(room=self.room_name, identity=identity)
                    )
                    print(f"[session_manager] removed agent participant identity={identity}")
                except Exception as exc:
                    print(f"[session_manager] remove agent failed identity={identity} err={exc}")
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

    async def dispatch_agent(self) -> None:
        async with self._lock:
            if self.agent_deployed or not self._bootstrap_complete:
                return
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
                self.session_state = "active"
                self.updated_at = _utc_now_iso()
                print(
                    f"[session_manager] dispatched agent name={self.agent_name} "
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
                self.agent_deployed = False
                self.active_dispatch_id = ""
                self.dispatch_started_monotonic = 0.0
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
            await self._remove_agent_participants()
            self.agent_deployed = False
            self.conversation_id = ""
            self.last_user_activity_monotonic = 0.0
            self.last_agent_activity_monotonic = 0.0
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
            if not self.agent_deployed and self.session_state == "idle":
                await self.dispatch_agent()
        elif source == SESSION_SOURCE_AGENT:
            if now - self.last_agent_activity_monotonic < SESSION_ACTIVITY_DEBOUNCE_SEC:
                return
            self.last_agent_activity_monotonic = now
            self.last_agent_activity_at = activity_at
            if level is not None:
                self.agent_audio_level = max(0.0, min(1.0, level))

    def _append_transcript(self, speaker: str, text: str) -> None:
        clean = " ".join(str(text).strip().split())
        if not clean:
            return
        item = {"speaker": speaker, "text": clean, "at": _utc_now_iso()}
        self.transcript_items.append(item)
        if speaker == "Pepper":
            self.last_pepper_text = clean
        else:
            self.last_user_text = clean

    def _idle_countdown_sec(self) -> float | None:
        if not self.agent_deployed:
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
            if self.agent_deployed:
                if self.last_user_activity_monotonic > 0:
                    idle_for = now - self.last_user_activity_monotonic
                    if idle_for >= SESSION_IDLE_TIMEOUT_SEC:
                        await self.end_session(reason=f"no_user_activity_{idle_for:.1f}s")
                elif self.last_user_activity_monotonic == 0 and self.last_agent_activity_monotonic == 0:
                    if self.session_state == "active":
                        if (
                            self.dispatch_started_monotonic > 0
                            and (now - self.dispatch_started_monotonic) >= SESSION_PREROLL_ACTIVITY_SEC
                        ):
                            await self.end_session(reason="no_activity_after_dispatch")
            await asyncio.sleep(LIVEKIT_STATUS_POLL_INTERVAL_SEC)

    async def handle_status(self, request: web.Request) -> web.Response:
        del request
        payload = {
            "room_name": self.room_name,
            "session_state": self.session_state,
            "agent_deployed": self.agent_deployed,
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
            self._append_transcript(speaker, text)
        elif event_type == "mic_level" and level_value is not None:
            self.mic_level = max(0.0, min(1.0, level_value))
        elif event_type == "agent_level" and level_value is not None:
            self.agent_audio_level = max(0.0, min(1.0, level_value))
        elif event_type == "agent_speaking":
            self.agent_speaking = active

        self.updated_at = _utc_now_iso()
        return web.json_response({"ok": True})

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
        self.updated_at = _utc_now_iso()
        return web.json_response({"ok": True, "mic_muted": self.mic_muted})

    async def handle_text_send(self, request: web.Request) -> web.Response:
        data = await request.json()
        text = " ".join(str(data.get("text") or "").strip().split())
        if not text:
            return web.json_response({"ok": False, "error": "text required"}, status=400)
        item = {"id": uuid.uuid4().hex[:10], "text": text}
        self.pending_user_texts.append(item)
        self._append_transcript("User", text)
        self.updated_at = _utc_now_iso()
        return web.json_response({"ok": True, "queued": item})

    async def handle_user_client_state(self, request: web.Request) -> web.Response:
        del request
        return web.json_response(
            {
                "mic_muted": self.mic_muted,
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

    async def handle_root(self, request: web.Request) -> web.Response:
        del request
        return web.Response(text=STATUS_HTML, content_type="text/html")

    async def start(self) -> None:
        app = web.Application()
        app.add_routes(
            [
                web.get("/", self.handle_root),
                web.get("/api/status", self.handle_status),
                web.post("/api/activity", self.handle_activity),
                web.post("/api/debug-event", self.handle_debug_event),
                web.post("/api/component-status", self.handle_component_status),
                web.post("/api/control/mic", self.handle_mic_toggle),
                web.post("/api/control/text", self.handle_text_send),
                web.post("/api/control/reset", self.handle_reset),
                web.get("/api/user-client/state", self.handle_user_client_state),
                web.post("/api/user-client/ack", self.handle_user_client_ack),
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
