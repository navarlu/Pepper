"""
Text Chat CLI — debug Pepper from the terminal alongside the live mic path.

Connects as identity "debug-cli" (subscribe-only), so it coexists with
user-client (which stays connected as "user"). Text input is published on
the `pepper.text` topic; the agent feeds it into the session as if it
came from the user — the LLM sees only "user input", never knows about
debug-cli. Mic mute is a soft-mute via `pepper.control` — user-client
keeps its room presence and just sends silent frames.

Slash commands (type / followed by command):
    /help              show this list
    /status            snapshot of room, mode, participants, mic state
    /mode <m>          switch agent mode (openai | local)
    /mic <on|off>      mute/unmute user-client's mic (soft, no disconnect)
    /reset             clear agent's chat history
    /quit              exit (also: Ctrl-D)
"""

import asyncio
import json
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path

from livekit import rtc

# ── Paths & topics ──────────────────────────────────────────────────────────

ROOT_DIR = Path(__file__).resolve().parents[2]
TOKEN_FILE = ROOT_DIR / "services" / "data" / "token-latest.json"
CONFIG_FILE = ROOT_DIR / "services" / "src" / "orchestrator_config.json"

TOPIC_CHAT = "lk.chat"
TOPIC_DEBUG = "pepper.debug"
TOPIC_CONTROL = "pepper.control"
TOPIC_TEXT = "pepper.text"

TRUNC = 200
PROMPT = "You> "


# ── Helpers ──────────────────────────────────────────────────────────────────

def _truncate(text: str, limit: int = TRUNC) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _print(msg: str = "") -> None:
    """Print a line, clearing the current input line first.

    The next prompt is drawn by the main input loop (or by the next print).
    Async events that fire while the user is typing will appear on a fresh
    line — we don't try to redraw partial input (no readline integration).
    """
    sys.stdout.write(f"\r\033[K{msg}\n")
    sys.stdout.flush()


def _read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _read_token_snapshot() -> dict:
    snap = _read_json(TOKEN_FILE)
    if not snap:
        print(f"Token file missing or invalid: {TOKEN_FILE}")
        print("Make sure the orchestrator is running.")
        sys.exit(1)
    return snap


def _extract_debug_cli_credentials(snapshot: dict) -> tuple[str, str, str, str]:
    entry = snapshot.get("debugCli") or {}
    token = str(entry.get("token") or "").strip()
    identity = str(entry.get("identity") or "debug-cli").strip()
    ws_url = str(
        snapshot.get("hostWsUrl")
        or snapshot.get("wsUrl")
        or snapshot.get("internalWsUrl")
        or ""
    ).strip()
    room_name = str(snapshot.get("roomName") or "").strip()
    if not token:
        print("Token snapshot missing debugCli.token — restart the orchestrator so it provisions one.")
        sys.exit(1)
    if not (ws_url and room_name):
        print("Token snapshot missing wsUrl or roomName.")
        sys.exit(1)
    return token, ws_url, room_name, identity


def _read_mode() -> str:
    return str(_read_json(CONFIG_FILE).get("agent_mode", "?")).strip() or "?"


def _write_mode(mode: str) -> None:
    CONFIG_FILE.write_text(json.dumps({"agent_mode": mode}, indent=2), encoding="utf-8")


def _format_age(iso_ts: str) -> str:
    if not iso_ts:
        return "?"
    try:
        ts = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except ValueError:
        return iso_ts
    delta = datetime.now(timezone.utc) - ts.astimezone(timezone.utc)
    secs = int(delta.total_seconds())
    if secs < 60:
        return f"{secs}s ago"
    if secs < 3600:
        return f"{secs // 60}m{secs % 60}s ago"
    return f"{secs // 3600}h{(secs % 3600) // 60}m ago"


# ── Command handlers ────────────────────────────────────────────────────────

class ChatSession:
    def __init__(self, room: rtc.Room, identity: str) -> None:
        self.room = room
        self.identity = identity
        self.snapshot = _read_token_snapshot()
        self._stop = asyncio.Event()
        # Track last mic state we asked for; "?" until /mic is used.
        self.mic_state = "?"

    # --- commands -----------------------------------------------------------

    async def cmd_help(self, _args: list[str]) -> None:
        _print("Commands:")
        for name, (_fn, helptext) in COMMANDS.items():
            _print(f"  /{name:<8} {helptext}")

    async def cmd_status(self, _args: list[str]) -> None:
        snap = _read_token_snapshot()
        room = self.room
        remotes = list(getattr(room, "remote_participants", {}).values())
        participants = ", ".join(
            sorted([str(p.identity) for p in remotes] + [self.identity])
        ) or "<none>"
        user_present = any(str(p.identity) == "user" for p in remotes)
        _print("─── status ───")
        _print(f"  room         {room.name}")
        _print(f"  mode         {_read_mode()}")
        _print(f"  identity     {self.identity}")
        _print(f"  participants {participants}")
        _print(f"  user-client  {'connected' if user_present else 'absent'}")
        _print(f"  mic          {self.mic_state}")
        _print(f"  session      generated {_format_age(snap.get('generatedAt', ''))}")
        _print("──────────────")

    async def cmd_mode(self, args: list[str]) -> None:
        if not args or args[0] not in ("openai", "local"):
            _print("Usage: /mode <openai|local>")
            return
        new_mode = args[0]
        current = _read_mode()
        if current == new_mode:
            _print(f"Already in '{new_mode}' mode.")
            return
        _write_mode(new_mode)
        _print(f"Mode change requested: {current} -> {new_mode} (orchestrator picks up within ~3s)")

    async def cmd_mic(self, args: list[str]) -> None:
        if not args or args[0] not in ("on", "off"):
            _print("Usage: /mic <on|off>")
            return
        muted = args[0] == "off"
        payload = json.dumps({"cmd": "mic", "muted": muted}).encode("utf-8")
        try:
            await self.room.local_participant.publish_data(payload, topic=TOPIC_CONTROL)
            self.mic_state = "muted" if muted else "live"
            _print(f"  mic {self.mic_state} (signal sent to user-client)")
        except Exception as exc:
            _print(f"  failed to send: {exc}")

    async def cmd_reset(self, _args: list[str]) -> None:
        payload = json.dumps({"cmd": "reset"}).encode("utf-8")
        try:
            await self.room.local_participant.publish_data(payload, topic=TOPIC_CONTROL)
            _print("Reset signal sent — agent will clear chat history shortly.")
        except Exception as exc:
            _print(f"  failed to send reset: {exc}")

    async def cmd_quit(self, _args: list[str]) -> None:
        self._stop.set()

    # --- main loop ----------------------------------------------------------

    async def send_text(self, text: str) -> None:
        payload = json.dumps({"text": text}).encode("utf-8")
        try:
            await self.room.local_participant.publish_data(payload, topic=TOPIC_TEXT)
        except Exception as exc:
            _print(f"  [error] failed to send: {exc}")

    async def dispatch(self, line: str) -> None:
        if not line.startswith("/"):
            await self.send_text(line)
            return
        parts = shlex.split(line[1:])
        if not parts:
            return
        name, args = parts[0].lower(), parts[1:]
        entry = COMMANDS.get(name)
        if not entry:
            _print(f"Unknown command: /{name}. Type /help.")
            return
        await entry[0](self, args)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        # Background mode-change watcher
        watcher = asyncio.create_task(self._mode_watcher())
        try:
            while not self._stop.is_set():
                sys.stdout.write(PROMPT)
                sys.stdout.flush()
                line = await loop.run_in_executor(None, sys.stdin.readline)
                if not line:
                    break
                stripped = line.strip()
                if not stripped:
                    continue
                await self.dispatch(stripped)
        except (KeyboardInterrupt, EOFError):
            pass
        finally:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass

    async def _mode_watcher(self) -> None:
        last = _read_mode()
        while not self._stop.is_set():
            await asyncio.sleep(2.0)
            current = _read_mode()
            if current != last:
                _print(f"  [mode] {last} -> {current}")
                last = current


COMMANDS: dict[str, tuple] = {
    "help":   (ChatSession.cmd_help,   "show this list"),
    "status": (ChatSession.cmd_status, "snapshot: room, mode, participants, session age"),
    "mode":   (ChatSession.cmd_mode,   "<openai|local> — switch agent mode"),
    "mic":    (ChatSession.cmd_mic,    "<on|off> — start/stop user-client container"),
    "reset":  (ChatSession.cmd_reset,  "clear agent's chat history"),
    "quit":   (ChatSession.cmd_quit,   "exit"),
}


# ── Room event handlers ─────────────────────────────────────────────────────

def _install_room_handlers(room: rtc.Room, cli_identity: str) -> None:
    @room.on("transcription_received")
    def _on_transcription(segments, participant, _publication):
        identity = str(getattr(participant, "identity", "") or "")
        for seg in segments:
            text = str(getattr(seg, "text", "") or "").strip()
            if text and getattr(seg, "final", True):
                _print(f"  [{identity}]: {text}")

    @room.on("data_received")
    def _on_data(packet):
        topic = str(getattr(packet, "topic", "") or "")
        participant = getattr(packet, "participant", None)
        identity = str(getattr(participant, "identity", "") or "") if participant else ""
        if identity == cli_identity:
            return

        if topic == TOPIC_CHAT:
            raw = getattr(packet, "data", b"") or b""
            try:
                msg = json.loads(raw)
                text = msg.get("message", "") or msg.get("text", "") or ""
            except (json.JSONDecodeError, UnicodeDecodeError):
                text = raw.decode("utf-8", "ignore")
            if text.strip():
                _print(f"  [{identity}]: {text.strip()}")
            return

        if topic == TOPIC_DEBUG:
            raw = getattr(packet, "data", b"") or b""
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return
            if payload.get("kind") == "tool_call":
                name = payload.get("name", "?")
                args = payload.get("args", "")
                if not isinstance(args, str):
                    args = json.dumps(args, ensure_ascii=False, default=str)
                result = payload.get("result", "")
                if not isinstance(result, str):
                    result = json.dumps(result, ensure_ascii=False, default=str)
                dur = payload.get("duration_ms", 0) or 0
                err = payload.get("error")
                marker = "✗" if err else "✓"
                _print(f"  [tool {marker}] {name}({_truncate(args)}) -> {_truncate(result)} [{dur:.0f}ms]")
                if err:
                    _print(f"  [tool err] {_truncate(err)}")

    @room.on("participant_connected")
    def _on_join(participant):
        _print(f"  [room] + {participant.identity}")

    @room.on("participant_disconnected")
    def _on_leave(participant):
        _print(f"  [room] - {participant.identity}")


# ── Entrypoint ──────────────────────────────────────────────────────────────

async def main() -> None:
    snapshot = _read_token_snapshot()
    token, ws_url, room_name, cli_identity = _extract_debug_cli_credentials(snapshot)
    print(f"Connecting to room '{room_name}' at {ws_url} as '{cli_identity}'…")

    room = rtc.Room()
    _install_room_handlers(room, cli_identity)

    try:
        await room.connect(ws_url, token, rtc.RoomOptions(auto_subscribe=True))
    except Exception as exc:
        print(f"Failed to connect: {exc}")
        sys.exit(1)

    print(f"Connected as '{cli_identity}'. Type /help for commands.\n")

    session = ChatSession(room, cli_identity)
    try:
        await session.run()
    finally:
        print("\nDisconnecting…")
        await room.disconnect()
        print("Bye!")


if __name__ == "__main__":
    asyncio.run(main())
