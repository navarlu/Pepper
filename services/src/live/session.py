"""LiveKit session-file watcher shared by the services.

The orchestrator writes `services/data/token-latest.json` with fresh
tokens every time it (re)provisions the room. Every other service
reads that file to discover the room name and its per-role token.

This module exposes two things:
  - `SessionWatcher` — polls the file, surfaces changes, can wait for
    the initial snapshot. Used by `audio_bridge`, `tablet_server`,
    `user_client` (via `SessionWatcher("user")`).
  - `post_debug_event` — minimal log helper kept for readability at
    call sites that used to POST to the retired session-manager.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path

from config import LIVEKIT_SESSION_FILE, TOKEN_POLL_INTERVAL

SESSION_FILE = Path(LIVEKIT_SESSION_FILE)


class SessionWatcher:
    """Poll the LiveKit session token file and notify on changes.

    One watcher per role (`"user"`, `"listener"`, `"monitor"`,
    `"debugCli"`, `"tablet"`). `role` indexes into the JSON payload —
    if the role is missing the watcher simply waits, logging once on
    first missing read so startup logs don't spin.
    """

    def __init__(self, role: str, poll_interval: float = TOKEN_POLL_INTERVAL):
        self.role = role
        self.poll_interval = poll_interval
        self._last_token: str | None = None
        self._missing_logged = False

    def _read_latest_snapshot(self) -> dict | None:
        try:
            text = SESSION_FILE.read_text(encoding="utf-8")
            return json.loads(text)
        except FileNotFoundError:
            if not self._missing_logged:
                print(f"[token-watcher] Waiting for session snapshot in {SESSION_FILE}")
                self._missing_logged = True
            return None
        except json.JSONDecodeError:
            print(f"[token-watcher] Invalid JSON in {SESSION_FILE}, waiting for next update")
            return None

    def _extract_token_info(self) -> dict | None:
        """Return `{token, roomName, identity, wsUrl, agentIdentity,
        generatedAt}` for `self.role`, or `None` if the snapshot isn't
        ready or doesn't contain the role yet.
        """
        snapshot = self._read_latest_snapshot()
        if not snapshot:
            return None
        if self._missing_logged:
            print(f"[token-watcher] Found session snapshot in {SESSION_FILE}")
            self._missing_logged = False
        role_data = snapshot.get(self.role)
        if not isinstance(role_data, dict):
            return None
        token = role_data.get("token")
        if not token:
            return None
        agent = snapshot.get("agent")
        return {
            "token": token,
            "roomName": snapshot.get("roomName"),
            "identity": role_data.get("identity"),
            # hostWsUrl wins for clients running on the host (user-client);
            # fall back to the internal URL otherwise.
            "wsUrl": (
                snapshot.get("hostWsUrl")
                or snapshot.get("internalWsUrl")
                or snapshot.get("wsUrl")
            ),
            "agentIdentity": agent.get("identity") if isinstance(agent, dict) else None,
            "generatedAt": snapshot.get("generatedAt"),
        }

    def latest_token_info(self) -> dict | None:
        """Non-blocking snapshot read. Useful for `on_disconnect` paths
        that want to reconnect with whatever token is current.
        """
        return self._extract_token_info()

    async def wait_for_initial_token(self) -> dict:
        """Block until a snapshot with a token for `self.role` exists."""
        while True:
            info = self._extract_token_info()
            if info:
                self._last_token = info["token"]
                return info
            await asyncio.sleep(self.poll_interval)

    async def watch(self, on_change: Callable[[dict], Awaitable[None]]) -> None:
        """Forever-loop: call `on_change(info)` every time the token rotates.

        Pairs with `wait_for_initial_token()` — call that once at
        startup, then spawn this as a background task.
        """
        while True:
            info = self._extract_token_info()
            if info and info["token"] != self._last_token:
                self._last_token = info["token"]
                await on_change(info)
            await asyncio.sleep(self.poll_interval)


def post_debug_event(event: str, **payload) -> None:
    """Structured-log helper for service-internal events.

    Emitted as `[debug-event] <event> k=v k=v ...` on stdout, clamped
    to 200 chars so a misbehaving caller can't flood the log.
    """
    details = " ".join(f"{k}={v}" for k, v in payload.items())
    print(f"[debug-event] {event} {details}"[:200])
