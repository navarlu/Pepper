"""Shared utilities for audio-bridge and user-client services."""

import asyncio
import json
import time
from pathlib import Path
from typing import Awaitable, Callable, Optional

try:
    from .config import (
        LIVEKIT_SESSION_FILE,
        SESSION_ACTIVITY_DEBOUNCE_SEC,
        TOKEN_POLL_INTERVAL,
    )
except ImportError:
    from config import (
        LIVEKIT_SESSION_FILE,
        SESSION_ACTIVITY_DEBOUNCE_SEC,
        TOKEN_POLL_INTERVAL,
    )

SESSION_FILE = Path(LIVEKIT_SESSION_FILE)


class SessionWatcher:
    """Poll the LiveKit session token file and notify on changes."""

    def __init__(self, role: str, poll_interval: float = TOKEN_POLL_INTERVAL):
        self.role = role
        self.poll_interval = poll_interval
        self._last_token: Optional[str] = None
        self._missing_logged = False

    def _read_latest_snapshot(self) -> Optional[dict]:
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

    def _extract_token_info(self) -> Optional[dict]:
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
        return {
            "token": token,
            "roomName": snapshot.get("roomName"),
            "identity": role_data.get("identity"),
            "wsUrl": snapshot.get("internalWsUrl") or snapshot.get("wsUrl"),
            "agentIdentity": (
                (snapshot.get("agent") or {}).get("identity")
                if isinstance(snapshot.get("agent"), dict)
                else None
            ),
            "generatedAt": snapshot.get("generatedAt"),
        }

    def latest_token_info(self) -> Optional[dict]:
        return self._extract_token_info()

    async def wait_for_initial_token(self) -> dict:
        while True:
            info = self._extract_token_info()
            if info:
                self._last_token = info["token"]
                return info
            await asyncio.sleep(self.poll_interval)

    async def watch(self, on_change: Callable[[dict], Awaitable[None]]) -> None:
        while True:
            info = self._extract_token_info()
            if info and info["token"] != self._last_token:
                self._last_token = info["token"]
                await on_change(info)
            await asyncio.sleep(self.poll_interval)


def post_debug_event(event: str, **payload) -> None:
    """Log a debug event to terminal (no longer POSTs to session-manager)."""
    details = " ".join(f"{k}={v}" for k, v in payload.items())
    print(f"[debug-event] {event} {details}"[:200])


class AgentActivityReporter:
    """Debounced activity logger (no longer POSTs to session-manager)."""

    def __init__(self):
        self._last_post_monotonic = 0.0

    def report(self) -> None:
        now = time.monotonic()
        if now - self._last_post_monotonic < SESSION_ACTIVITY_DEBOUNCE_SEC:
            return
        self._last_post_monotonic = now
