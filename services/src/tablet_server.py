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
POST_TIMEOUT_SEC = 3.0

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
.title{{font-size:28px;font-weight:700;letter-spacing:-.01em;color:#1b2430;
  margin-right:auto;}}
.pill{{padding:10px 20px;border-radius:999px;background:#eef1f5;
  color:#1b2430;font-size:20px;font-weight:600;border:1px solid #dfe4ea;
  line-height:1.1;white-space:nowrap;}}
.pill .k{{color:#6b7280;font-size:14px;text-transform:uppercase;
  letter-spacing:.08em;margin-right:8px;font-weight:700;}}
.pill.mode-openai{{background:#e7f1ff;border-color:#b6d4fe;color:#0b5ed7;}}
.pill.mode-local{{background:#e6f7ec;border-color:#b7e2c7;color:#0a7a2f;}}
.pill.mic-live{{background:#e6f7ec;border-color:#b7e2c7;color:#0a7a2f;}}
.pill.mic-muted{{background:#fdecea;border-color:#f5c3bf;color:#b42318;}}
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
.bubble{{padding:16px 22px;border-radius:22px;line-height:1.35;
  font-size:30px;word-wrap:break-word;word-break:break-word;white-space:pre-wrap;
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
<header>
  <div class="title">Pepper</div>
  <div class="pill {mode_cls}"><span class="k">Mode</span>{mode_text}</div>
  <div class="pill {mic_cls}"><span class="k">Mic</span>{mic_text}</div>
</header>
<main id="main">
  <div class="feed">{rows}<div id="anchor"></div></div>
</main>
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
        }
        self._dirty = asyncio.Event()
        self._http: aiohttp.ClientSession | None = None
        self._last_posted_hash: int | None = None

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
                # Text typed into text_chat.py / debug-cli. The agent receives
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

    # -- Rendering -----------------------------------------------------------

    def _render_html(self) -> str:
        mode = self._state.get("agent_mode") or "?"
        mode_cls = "mode-" + mode if mode in ("openai", "local") else ""
        mic_muted = bool(self._state.get("mic_muted"))
        mic_cls = "mic-muted" if mic_muted else "mic-live"
        mic_text = "MUTED" if mic_muted else "live"

        if not self._chat:
            rows_html = '<div class="empty">Waiting for conversation…</div>'
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

        return PAGE_TEMPLATE.format(
            mode_cls=mode_cls,
            mode_text=_esc(mode),
            mic_cls=mic_cls,
            mic_text=mic_text,
            rows=rows_html,
        )

    async def _post_to_bridge(self, html: str) -> None:
        data_url = "data:text/html;charset=utf-8," + quote(html.encode("utf-8"))
        # Dedup: if the rendered page is identical to the last one we pushed,
        # skip the showWebview — Pepper's tablet re-layouts on every call.
        h = hash(data_url)
        if h == self._last_posted_hash:
            return
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
                else:
                    body = await resp.text()
                    _log(f"bridge POST non-2xx status={resp.status} body={body[:120]}")
        except Exception as exc:
            _log(f"bridge POST failed err={exc}")

    async def _render_loop(self) -> None:
        while True:
            await self._dirty.wait()
            # Debounce — gather bursts of partial events into one render.
            await asyncio.sleep(RENDER_DEBOUNCE_SEC)
            self._dirty.clear()
            try:
                html = self._render_html()
                await self._post_to_bridge(html)
            except Exception as exc:
                _log(f"render error err={exc}")

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
            _log(f"connected room='{self._state['room_name']}' identity='{identity}'")
            # Trigger an initial render so the tablet shows the current state
            # even before any event arrives.
            self._dirty.set()

    async def _on_token_change(self, info: dict) -> None:
        _log(f"token change — reconnecting to room='{info.get('roomName')}'")
        # Fresh room means a fresh session — clear history.
        self._chat.clear()
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
            # First render as soon as we start, so any cached state shows up
            # before LK connects.
            self._dirty.set()
            try:
                await asyncio.Event().wait()
            finally:
                lk_task.cancel()
                render_task.cancel()
                for t in (lk_task, render_task):
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
