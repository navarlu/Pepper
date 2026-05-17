"""
Tablet Display — read-only chat view rendered on Pepper's built-in tablet.

Pepper's tablet browser can't reach the RPi's LAN (it lives on an internal
USB network), so we can't serve the UI over HTTP. Instead this process:

  1. Joins the LiveKit room as identity ``tablet`` (subscribe-only).
  2. Maintains in-memory session state — mode, mic, chat history, tool calls.
  3. On every event, renders a compact HTML page and POSTs it as a
     ``data:text/html`` URL to the bridge's ``/tablet/url`` endpoint. The
     bridge forwards it to ``ALTabletService.showWebview()``.

Chat history clears automatically on ``pepper.debug`` ``session_reset``
(fires on agent idle timeout or explicit ``/reset``).

No server, no bidirectional comms — the tablet is a pure display.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import html as html_module
import json
import os
import time
from pathlib import Path
from urllib.parse import quote

import aiohttp
from livekit import rtc

from config import BRIDGE_URL as _BRIDGE_URL
from config import LIVEKIT_URL as _LIVEKIT_URL
from config import STATE_FILE as _STATE_FILE
from session import SessionWatcher

# Constants local to this module (tabled-display specific).
BRIDGE_URL = _BRIDGE_URL.rstrip("/")
LIVEKIT_URL_DEFAULT = _LIVEKIT_URL
STATE_FILE = Path(_STATE_FILE)

# Keep the chat window tight — data URLs grow linearly and Pepper's tablet
# browser gets sluggish past ~30 KB of HTML.
MAX_CHAT_ENTRIES = 40
# Debounce so a burst of partial transcripts doesn't trigger a showWebview per
# segment. 300 ms feels instant but coalesces bursts nicely.
RENDER_DEBOUNCE_SEC = 0.30
# How long the farewell QR stays on the tablet after a worker
# publishes `farewell_active=True`. tablet-server owns this window
# locally so we can dispatch the next session immediately without it
# overwriting the QR with fresh chat HTML.
FAREWELL_HOLD_SEC = float(os.environ.get("EXPERIMENT_FAREWELL_DISPLAY_SEC", "30"))
POST_TIMEOUT_SEC = 3.0
# Slow heartbeat that forces a re-post even when nothing in our state has
# changed. Guards against the tablet drifting to a foreign page (NAOqi
# default app, bridge restart seed, etc.) — our content reclaims the
# screen on the next tick.
REFRESH_TICK_SEC = 15.0

TOPIC_CHAT = "lk.chat"
TOPIC_DEBUG = "pepper.debug"
TOPIC_STATE = "pepper.state"
TOPIC_TEXT = "pepper.text"


def _log(msg: str) -> None:
    print(f"[tablet-display] {msg}", flush=True)


def _read_state_file() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8")) or {}
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


# ── HTML template ───────────────────────────────────────────────────────────

# Single-file, single-purpose layout. Light theme, large readable text,
# minimal tool rows. Full DOM rebuilt server-side on each update; a tiny
# inline script scrolls the feed to the latest message on load.
PAGE_TEMPLATE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{{box-sizing:border-box;margin:0;padding:0;-webkit-tap-highlight-color:transparent;}}
html,body{{height:100%;background:#f7f8fa;color:#1b2430;
  font-family:-apple-system,"Segoe UI",Roboto,sans-serif;}}
body{{display:flex;flex-direction:column;overflow:hidden;}}
header{{flex:0 0 auto;padding:18px 28px;background:#ffffff;
  border-bottom:1px solid #e3e6eb;display:flex;gap:14px;align-items:center;
  box-shadow:0 1px 2px rgba(15,23,42,.04);}}
.title{{font-size:44px;font-weight:800;letter-spacing:-.01em;color:#1b2430;
  margin-right:auto;display:flex;align-items:baseline;gap:14px;}}
.title .role{{font-size:44px;font-weight:800;color:#1b2430;
  letter-spacing:-.01em;}}
.pill{{padding:10px 20px;border-radius:999px;background:#eef1f5;
  color:#1b2430;font-size:14px;font-weight:700;border:1px solid #dfe4ea;
  line-height:1.1;white-space:nowrap;text-transform:uppercase;
  letter-spacing:.08em;}}
.pill .k{{color:#6b7280;font-size:14px;text-transform:uppercase;
  letter-spacing:.08em;margin-right:8px;font-weight:700;}}
.pill.mode-openai{{background:#e7f1ff;border-color:#b6d4fe;color:#0b5ed7;}}
.pill.mode-local{{background:#e6f7ec;border-color:#b7e2c7;color:#0a7a2f;}}
.pill.mic-live{{background:#e6f7ec;border-color:#b7e2c7;color:#0a7a2f;}}
.pill.mic-muted{{background:#fdecea;border-color:#f5c3bf;color:#b42318;}}
.pill.agent-ready{{background:#e6f7ec;border-color:#b7e2c7;color:#0a7a2f;}}
.pill.agent-absent{{background:#fdecea;border-color:#f5c3bf;color:#b42318;}}
.pill.lang{{background:#f3eaff;border-color:#d6c2f0;color:#5e2bb0;}}
.statebar{{flex:0 0 auto;display:flex;justify-content:center;align-items:center;
  padding:12px 28px;font-size:32px;font-weight:700;letter-spacing:.02em;
  text-align:center;border-bottom:1px solid #e3e6eb;}}
.statebar.listening{{background:#e6f7ec;color:#0a7a2f;}}
.statebar.thinking{{background:#fff4e0;color:#9a5b00;}}
.statebar.speaking{{background:#e7f1ff;color:#0b5ed7;}}
.statebar.initializing,.statebar.idle{{background:#eef1f5;color:#6b7280;}}
.sleeping{{flex:1 1 auto;display:flex;flex-direction:column;
  justify-content:center;align-items:center;text-align:center;
  background:#f0eef9;color:#4b3f7a;padding:40px 28px;}}
.sleeping .zzz{{font-size:120px;font-weight:800;letter-spacing:.05em;
  line-height:1;margin-bottom:18px;color:#7a6cb8;}}
.sleeping .msg{{font-size:38px;font-weight:700;margin-bottom:8px;}}
.sleeping .sub{{font-size:22px;color:#7d75a3;font-style:italic;}}
main{{flex:1 1 auto;overflow-y:auto;overflow-x:hidden;padding:22px 28px 28px;
  -webkit-overflow-scrolling:touch;scroll-behavior:smooth;}}
.feed{{display:flex;flex-direction:column;gap:18px;max-width:980px;margin:0 auto;
  padding-bottom:20px;}}
.empty{{color:#8a94a6;font-size:28px;text-align:center;margin-top:80px;
  font-style:italic;}}
.row{{display:flex;flex-direction:column;max-width:82%;gap:6px;}}
.row.user{{align-self:flex-end;align-items:flex-end;}}
.row.pepper{{align-self:flex-start;align-items:flex-start;}}
.who{{font-size:15px;font-weight:700;text-transform:uppercase;
  letter-spacing:.12em;color:#8a94a6;}}
.row.user .who{{color:#0b5ed7;}}
.row.pepper .who{{color:#0a7a2f;}}
.bubble{{padding:14px 18px;border-radius:20px;line-height:1.35;
  font-size:22px;word-wrap:break-word;word-break:break-word;white-space:pre-wrap;
  background:#ffffff;border:1px solid #e3e6eb;
  box-shadow:0 2px 6px rgba(15,23,42,.04);}}
.row.user .bubble{{background:#e7f1ff;border-color:#b6d4fe;color:#08326f;
  border-bottom-right-radius:8px;}}
.row.pepper .bubble{{background:#ffffff;border-color:#e3e6eb;color:#1b2430;
  border-bottom-left-radius:8px;}}
.row.tool{{align-self:flex-start;align-items:flex-start;max-width:100%;gap:0;}}
.row.tool .chip{{display:inline-flex;align-items:center;gap:8px;
  padding:6px 14px;border-radius:999px;background:#eef1f5;
  color:#6b7280;font-size:16px;font-style:italic;
  border:1px solid #e3e6eb;}}
.row.tool .chip .mk{{font-style:normal;color:#0a7a2f;font-weight:700;}}
.row.tool.err .chip{{background:#fdecea;border-color:#f5c3bf;color:#b42318;}}
.row.tool.err .chip .mk{{color:#b42318;}}
.row.session{{align-self:center;color:#8a94a6;font-size:16px;
  font-style:italic;letter-spacing:.08em;padding:6px 0;}}
#anchor{{height:1px;}}
</style></head>
<body>
{body}
<script>
// Pin to the bottom on every render so new messages are visible. User can
// still touch-scroll up to read history briefly; the next render snaps back.
(function(){{
  var a = document.getElementById("anchor");
  if (a && a.scrollIntoView) a.scrollIntoView(false);
  else document.getElementById("main").scrollTop = 1e9;
}})();
</script>
</body></html>"""


def _esc(text) -> str:
    return html_module.escape(str(text) if text is not None else "", quote=True)


def _truncate(text: str, n: int) -> str:
    if len(text) <= n:
        return text
    return text[: n - 1] + "…"


# ── Display ─────────────────────────────────────────────────────────────────


class TabletDisplay:
    def __init__(self) -> None:
        self._watcher = SessionWatcher("tablet")
        self._room: rtc.Room | None = None
        self._room_lock = asyncio.Lock()
        self._livekit_url = LIVEKIT_URL_DEFAULT
        self._chat: collections.deque = collections.deque(maxlen=MAX_CHAT_ENTRIES)
        self._state = {
            "agent_mode": None,
            "agent_name": None,
            "mic_muted": False,
            "room_name": None,
            "agent_state": None,  # listening / thinking / speaking / initializing
            "agent_present": False,  # any participant with identity "agent-*" in the room
            "agent_language": "en",  # spoken language; updated via pepper.state
            # Participant code (e.g. "T01") published once per session by
            # the experiment worker on pepper.state. Rendered as the ID
            # pill in the header so users can note it for the
            # post-interaction questionnaire.
            "student_id": None,
            # Set True by the experiment worker (via `pepper.state`) for
            # the duration of the `end_conversation` farewell QR. While
            # active, the render loop below stops POSTing to the bridge
            # so the QR — owned exclusively by the voice-agent during
            # this window — does not flicker under chat re-renders.
            "farewell_active": False,
        }
        # Owns the 30 s farewell QR window locally. When the worker
        # publishes `farewell_active=True`, this captures the moment;
        # the render loop and the data handler both consult it. The
        # subsequent session's `session_start` payload still contains
        # `farewell_active=False`, but we IGNORE that field until our
        # own timer expires so the new session's chat does not race in
        # over a QR the user is still trying to scan.
        self._farewell_until_monotonic: float | None = None
        self._dirty = asyncio.Event()
        self._http: aiohttp.ClientSession | None = None
        self._last_posted_hash: int | None = None
        # `experiment_active` is the single source of truth for whether
        # to render the chat UI or the zzz sleeping UI. Written by
        # loop_launcher.py into services/data/state.json. Refreshed by
        # the state.json watcher below.
        self._state["experiment_active"] = False
        # Tracks last state-file mtime so the watcher only re-derives
        # state on actual file changes (cheap polling).
        self._last_state_mtime: float | None = None

        # Prime state from disk so the first render has pills ready.
        file_state = _read_state_file()
        self._state["agent_mode"] = file_state.get("agent_mode")
        self._state["mic_muted"] = bool(file_state.get("mic_muted", False))

    # -- LiveKit event handlers ----------------------------------------------

    def _append_chat(self, entry: dict) -> None:
        self._chat.append(entry)
        self._dirty.set()

    def _install_room_handlers(self, room: rtc.Room) -> None:
        @room.on("transcription_received")
        def _on_transcription(segments, participant, _publication):
            identity = str(getattr(participant, "identity", "") or "")
            for seg in segments:
                text = str(getattr(seg, "text", "") or "").strip()
                final = bool(getattr(seg, "final", True))
                if not text or not final:
                    continue
                self._append_chat({
                    "kind": "msg",
                    "speaker": "User" if identity == "user" else "Pepper",
                    "text": text,
                })

        @room.on("data_received")
        def _on_data(packet):
            topic = str(getattr(packet, "topic", "") or "")
            raw = getattr(packet, "data", b"") or b""

            if topic == TOPIC_STATE:
                try:
                    payload = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return
                if payload.get("agent_mode") is not None:
                    self._state["agent_mode"] = payload["agent_mode"]
                if payload.get("agent_name") is not None:
                    self._state["agent_name"] = payload["agent_name"]
                if "mic_muted" in payload:
                    self._state["mic_muted"] = bool(payload["mic_muted"])
                if payload.get("roomName") is not None:
                    self._state["room_name"] = payload["roomName"]
                if payload.get("agent_state") is not None:
                    self._state["agent_state"] = str(payload["agent_state"])
                if payload.get("agent_language") is not None:
                    self._state["agent_language"] = str(payload["agent_language"]).lower()
                if "student_id" in payload:
                    sid = payload.get("student_id")
                    self._state["student_id"] = (
                        str(sid).strip() if sid not in (None, "") else None
                    )
                if "farewell_active" in payload:
                    incoming = bool(payload["farewell_active"])
                    if incoming:
                        # Worker entered end_conversation. Start (or
                        # extend) the local QR-hold window — the
                        # tablet-side render loop will refuse to post
                        # chat HTML until this expires regardless of
                        # what the next session publishes.
                        self._state["farewell_active"] = True
                        self._farewell_until_monotonic = (
                            time.monotonic() + FAREWELL_HOLD_SEC
                        )
                        _log(
                            "farewell_active=True received, holding "
                            f"tablet for {FAREWELL_HOLD_SEC}s"
                        )
                    else:
                        # Ignore incoming False while our timer is still
                        # ticking — the next session re-publishes False
                        # at startup, and without this guard the QR
                        # would be replaced by the new chat in <1s.
                        if (
                            self._farewell_until_monotonic is not None
                            and time.monotonic() < self._farewell_until_monotonic
                        ):
                            _log("farewell_active=False ignored (hold still active)")
                        else:
                            was_active = bool(self._state.get("farewell_active"))
                            self._state["farewell_active"] = False
                            if was_active:
                                self._last_posted_hash = None
                self._dirty.set()
                return

            if topic == TOPIC_CHAT:
                try:
                    msg = json.loads(raw)
                    text = (msg.get("message") or msg.get("text") or "").strip()
                except (json.JSONDecodeError, UnicodeDecodeError):
                    text = raw.decode("utf-8", "ignore").strip()
                if text:
                    self._append_chat({"kind": "msg", "speaker": "Pepper", "text": text})
                return

            if topic == TOPIC_TEXT:
                # Text typed into the experiment launcher. The agent receives
                # it as a user utterance, so mirror that on the tablet.
                try:
                    payload = json.loads(raw)
                    text = str(payload.get("text") or "").strip()
                except (json.JSONDecodeError, UnicodeDecodeError):
                    text = raw.decode("utf-8", "ignore").strip()
                if text:
                    self._append_chat({"kind": "msg", "speaker": "User", "text": text})
                return

            if topic == TOPIC_DEBUG:
                try:
                    payload = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    return
                kind = payload.get("kind")
                if kind == "session_reset":
                    self._chat.clear()
                    self._append_chat({
                        "kind": "session",
                        "text": "— session reset —",
                    })
                    return
                if kind == "tool_call":
                    args = payload.get("args", "")
                    if not isinstance(args, str):
                        args = json.dumps(args, ensure_ascii=False, default=str)
                    result = payload.get("result", "")
                    if not isinstance(result, str):
                        result = json.dumps(result, ensure_ascii=False, default=str)
                    self._append_chat({
                        "kind": "tool",
                        "name": str(payload.get("name", "?")),
                        "args": args,
                        "result": result,
                        "duration_ms": float(payload.get("duration_ms") or 0),
                        "error": payload.get("error"),
                    })
                    return

        @room.on("disconnected")
        def _on_disconnect(*_args):
            _log("room disconnected")
            self._state["agent_present"] = False
            self._state["agent_state"] = None
            self._dirty.set()

        # Track agent presence by identity prefix "agent-". When a fresh
        # agent joins (new loop iteration), wipe the chat so the next
        # session starts clean. When the agent leaves, flip the pill so
        # the user sees Pepper is not ready.
        def _is_agent(p) -> bool:
            return str(getattr(p, "identity", "") or "").startswith("agent-")

        def _refresh_agent_presence() -> None:
            present = any(
                _is_agent(p)
                for p in (getattr(room, "remote_participants", {}) or {}).values()
            )
            self._state["agent_present"] = present
            self._dirty.set()

        @room.on("participant_connected")
        def _on_participant_connected(p):
            if _is_agent(p):
                _log(f"agent connected: {getattr(p, 'identity', '?')}")
                self._chat.clear()
                self._state["agent_state"] = None
                _refresh_agent_presence()

        @room.on("participant_disconnected")
        def _on_participant_disconnected(p):
            if _is_agent(p):
                _log(f"agent disconnected: {getattr(p, 'identity', '?')}")
                self._state["agent_state"] = None
                _refresh_agent_presence()

    # -- Rendering -----------------------------------------------------------

    _STATE_LABELS = {
        "listening": "Listening",
        "thinking": "Thinking…",
        "speaking": "Speaking",
        "initializing": "Starting up…",
    }

    # The 4o-chained experiment variant shares its tablet appearance
    # with the production OpenAI variant — same label "B", same blue
    # pill — so the participant can't tell them apart.
    _MODE_LABELS = {"local": "A", "openai": "B", "4o-chained": "B"}

    def _render_html(self) -> str:
        mode = self._state.get("agent_mode") or "?"
        if mode == "4o-chained":
            mode_cls = "mode-openai"
        elif mode in ("openai", "local"):
            mode_cls = "mode-" + mode
        else:
            mode_cls = ""
        mode_label = self._MODE_LABELS.get(mode, mode)
        mic_muted = bool(self._state.get("mic_muted"))
        mic_cls = "mic-muted" if mic_muted else "mic-live"
        mic_text = "MUTED" if mic_muted else "live"
        agent_present = bool(self._state.get("agent_present"))
        agent_cls = "agent-ready" if agent_present else "agent-absent"
        agent_text = "ready" if agent_present else "starting…"
        pid_text = self._state.get("student_id") or ""

        agent_state = (self._state.get("agent_state") or "").lower()
        state_cls = agent_state if agent_state in self._STATE_LABELS else "idle"
        state_text = self._STATE_LABELS.get(agent_state, "—")

        if not self._chat:
            rows_html = '<div class="empty">Ready for your question.</div>'
        else:
            parts = []
            for entry in self._chat:
                kind = entry.get("kind")
                if kind == "session":
                    parts.append(
                        '<div class="row session">{t}</div>'.format(
                            t=_esc(entry.get("text", "")),
                        )
                    )
                    continue
                if kind == "tool":
                    err = entry.get("error")
                    err_cls = " err" if err else ""
                    icon = "\u2717" if err else "\u2713"
                    name = _esc(entry.get("name", "?"))
                    parts.append(
                        '<div class="row tool{ec}">'
                        '<div class="chip"><span class="mk">{i}</span>{n}</div>'
                        '</div>'.format(ec=err_cls, i=icon, n=name)
                    )
                    continue
                # normal message
                speaker = entry.get("speaker", "")
                cls = "user" if speaker == "User" else "pepper"
                parts.append(
                    '<div class="row {cls}">'
                    '<div class="who">{who}</div>'
                    '<div class="bubble">{text}</div>'
                    '</div>'.format(
                        cls=cls,
                        who=_esc(speaker),
                        text=_esc(entry.get("text", "")),
                    )
                )
            rows_html = "".join(parts)

        experiment_active = bool(self._state.get("experiment_active"))
        if not experiment_active:
            body = (
                '<div class="sleeping">'
                '<div class="zzz">z z z</div>'
                '<div class="msg">Pepper is sleeping</div>'
                '<div class="sub">Please do not disturb — '
                "I'll wake up soon.</div>"
                '</div>'
            )
        else:
            id_pill_html = (
                f'<div class="pill lang"><span class="k">ID</span>{_esc(pid_text)}</div>'
                if pid_text else ""
            )
            header = (
                '<header>'
                '<div class="title">Pepper<span class="role"> — Receptionist</span></div>'
                f'{id_pill_html}'
                f'<div class="pill {mode_cls}"><span class="k">Mode</span>{_esc(mode_label)}</div>'
                f'<div class="pill {agent_cls}"><span class="k">Agent</span>{agent_text}</div>'
                # f'<div class="pill {mic_cls}"><span class="k">Mic</span>{mic_text}</div>'
                '</header>'
            )
            body = (
                f'{header}'
                f'<div class="statebar {state_cls}">{_esc(state_text)}</div>'
                f'<main id="main"><div class="feed">{rows_html}'
                '<div id="anchor"></div></div></main>'
            )

        return PAGE_TEMPLATE.format(body=body)

    async def _post_to_bridge(self, html: str) -> bool:
        """POST a rendered page to bridge `/tablet/url`.

        Returns True on 2xx, False otherwise. The render loop uses
        this to decide whether to retry — early failures during
        bridge qi-warmup (ALTabletService not yet resolved) come
        back as 503 and must be re-attempted, otherwise Pepper's
        tablet stays on whatever it was last showing.
        """
        data_url = "data:text/html;charset=utf-8," + quote(html.encode("utf-8"))
        # Dedup: if the rendered page is identical to the last one we pushed,
        # skip the showWebview — Pepper's tablet re-layouts on every call.
        h = hash(data_url)
        if h == self._last_posted_hash:
            return True
        assert self._http is not None
        try:
            async with self._http.post(
                BRIDGE_URL + "/tablet/url",
                json={"url": data_url},
                timeout=aiohttp.ClientTimeout(total=POST_TIMEOUT_SEC),
            ) as resp:
                if 200 <= resp.status < 300:
                    self._last_posted_hash = h
                    _log(f"rendered chat={len(self._chat)} bytes={len(data_url)}")
                    return True
                body = await resp.text()
                _log(f"bridge POST non-2xx status={resp.status} body={body[:120]}")
                return False
        except Exception as exc:
            _log(f"bridge POST failed err={exc}")
            return False

    async def _presence_watchdog(self) -> None:
        """Re-derive `agent_present` from the live participant list every
        few seconds. Belt-and-braces: if a participant_disconnected event
        is dropped (network blip, server hiccup) the event-driven state
        would stay stale forever. This poll fixes that without flooding.
        """
        while True:
            await asyncio.sleep(3.0)
            room = self._room
            if room is None:
                truth = False
            else:
                # rtc.Room exposes connection_state on newer versions; fall
                # back to remote_participants enumeration which works on all.
                try:
                    truth = any(
                        str(getattr(p, "identity", "") or "").startswith("agent-")
                        for p in (getattr(room, "remote_participants", {}) or {}).values()
                    )
                except Exception:
                    truth = False
            if bool(self._state.get("agent_present")) != truth:
                _log(f"presence watchdog: agent_present {self._state.get('agent_present')} → {truth}")
                self._state["agent_present"] = truth
                if not truth:
                    self._state["agent_state"] = None
                self._dirty.set()

    async def _state_file_watcher(self) -> None:
        """Mirror `experiment_active` from services/data/state.json
        into `self._state` so the renderer can switch between the chat
        UI and the zzz sleeping UI.

        loop_launcher.py is the only writer; it also stamps an
        `experiment_heartbeat_ts`. If the heartbeat is stale (process
        died without a clean exit), treat the experiment as inactive.

        Polls every 0.5 s. Re-derives the truth on every tick (not
        just on mtime change) so heartbeat staleness alone is enough
        to flip to sleep.
        """
        HEARTBEAT_STALE_SEC = 10.0
        POLL_SEC = 0.5
        while True:
            try:
                state = _read_state_file()
            except Exception as exc:
                _log(f"state.json read error err={exc}")
                state = {}
            active = bool(state.get("experiment_active", False))
            if active:
                try:
                    hb = float(state.get("experiment_heartbeat_ts", 0))
                except Exception:
                    hb = 0.0
                if hb > 0 and (time.time() - hb) >= HEARTBEAT_STALE_SEC:
                    active = False
            if active != bool(self._state.get("experiment_active")):
                _log(f"experiment_active {self._state.get('experiment_active')} → {active}")
                self._state["experiment_active"] = active
                self._dirty.set()
            await asyncio.sleep(POLL_SEC)

    async def _render_loop(self) -> None:
        while True:
            await self._dirty.wait()
            # Debounce — gather bursts of partial events into one render.
            await asyncio.sleep(RENDER_DEBOUNCE_SEC)
            self._dirty.clear()
            # Auto-expire the farewell hold once our local timer is up.
            # The next session's `session_start` already tried to set
            # `farewell_active=False` (ignored above while we were
            # within the hold window); flip it now so chat resumes.
            if (
                self._state.get("farewell_active")
                and self._farewell_until_monotonic is not None
                and time.monotonic() >= self._farewell_until_monotonic
            ):
                _log("farewell_active hold expired — resuming chat renders")
                self._state["farewell_active"] = False
                self._farewell_until_monotonic = None
                self._last_posted_hash = None
            # During the farewell window the voice-agent owns the
            # tablet (it has posted a QR + JS countdown). Skip our
            # chat-render POST so we do not flicker on top of it.
            # State changes still update self._state in the background
            # so the next render after the farewell ends reflects them.
            if self._state.get("farewell_active"):
                # Schedule a wake-up at expiry so we resume even if no
                # other event comes in to set self._dirty.
                if self._farewell_until_monotonic is not None:
                    delay = max(
                        0.5,
                        self._farewell_until_monotonic - time.monotonic(),
                    )
                    asyncio.get_running_loop().call_later(
                        delay, self._dirty.set,
                    )
                continue
            try:
                html = self._render_html()
                ok = await self._post_to_bridge(html)
            except Exception as exc:
                _log(f"render error err={exc}")
                ok = False
            if not ok:
                # Bridge wasn't ready (qi-warmup race) or some transient
                # error. Re-trigger ourselves so the next loop iteration
                # retries — otherwise tablet would stay on the previous
                # render until the next state change.
                await asyncio.sleep(2.0)
                self._dirty.set()

    async def _refresh_tick(self) -> None:
        """Force a re-post every REFRESH_TICK_SEC so the tablet can't drift
        to a foreign page. We clear the dedup hash before flagging dirty,
        otherwise _post_to_bridge would short-circuit the identical render.
        """
        while True:
            await asyncio.sleep(REFRESH_TICK_SEC)
            self._last_posted_hash = None
            self._dirty.set()

    # -- LiveKit connection lifecycle ----------------------------------------

    async def _connect_room(self, info: dict) -> None:
        async with self._room_lock:
            if self._room is not None:
                with contextlib.suppress(Exception):
                    await self._room.disconnect()
                self._room = None

            ws_url = str(info.get("wsUrl") or self._livekit_url).strip() or self._livekit_url
            self._livekit_url = ws_url
            room = rtc.Room()
            self._install_room_handlers(room)
            await room.connect(ws_url, info["token"])
            self._room = room
            self._state["room_name"] = getattr(room, "name", self._state.get("room_name"))
            identity = getattr(room.local_participant, "identity", "?")
            # Seed agent presence in case the agent joined before us.
            self._state["agent_present"] = any(
                str(getattr(p, "identity", "") or "").startswith("agent-")
                for p in (getattr(room, "remote_participants", {}) or {}).values()
            )
            _log(f"connected room='{self._state['room_name']}' identity='{identity}'")
            # Trigger an initial render so the tablet shows the current state
            # even before any event arrives.
            self._dirty.set()

    async def _on_token_change(self, info: dict) -> None:
        _log(f"token change — reconnecting to room='{info.get('roomName')}'")
        # Fresh room means a fresh session — clear history and drop any
        # stale agent-presence state so the tablet shows "sleeping" until
        # the new room is actually up.
        self._chat.clear()
        self._state["agent_present"] = False
        self._state["agent_state"] = None
        self._dirty.set()
        try:
            await self._connect_room(info)
        except Exception as exc:
            _log(f"reconnect failed err={exc}")

    async def _lk_loop(self) -> None:
        info = await self._watcher.wait_for_initial_token()
        _log(f"initial token identity='{info.get('identity')}' room='{info.get('roomName')}'")
        while True:
            try:
                await self._connect_room(info)
                break
            except Exception as exc:
                _log(f"connect failed err={exc}, retrying")
                await asyncio.sleep(3)
                latest = self._watcher.latest_token_info()
                if latest:
                    info = latest
        await self._watcher.watch(self._on_token_change)

    # -- Lifecycle -----------------------------------------------------------

    async def run(self) -> None:
        self._http = aiohttp.ClientSession()
        try:
            lk_task = asyncio.create_task(self._lk_loop())
            render_task = asyncio.create_task(self._render_loop())
            presence_task = asyncio.create_task(self._presence_watchdog())
            state_task = asyncio.create_task(self._state_file_watcher())
            refresh_task = asyncio.create_task(self._refresh_tick())
            # First render as soon as we start, so any cached state shows up
            # before LK connects.
            self._dirty.set()
            try:
                await asyncio.Event().wait()
            finally:
                lk_task.cancel()
                render_task.cancel()
                presence_task.cancel()
                state_task.cancel()
                refresh_task.cancel()
                for t in (lk_task, render_task, presence_task, state_task, refresh_task):
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await t
                if self._room is not None:
                    with contextlib.suppress(Exception):
                        await self._room.disconnect()
        finally:
            await self._http.close()


async def main() -> None:
    display = TabletDisplay()
    await display.run()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
