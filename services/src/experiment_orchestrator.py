"""Experiment orchestrator — slim sibling of orchestrator.py for
the student-study setup.

Design goal: replace the production orchestrator when running the
experiment compose. Same token plumbing (writes to
`livekit_session.json`, identities unchanged) so bridge,
audio-bridge, and listener follow into the experiment room with
zero config change. Differences vs. the production orchestrator:

  * Room name is FIXED — `pepper-experiment` — instead of
    `pepper-<timestamp>`. This lets the experiment.py launcher
    dispatch to a known target without coordinating timestamps.
  * Does NOT dispatch any agent. The launcher (run from the RPi)
    creates the agent dispatch on demand, with experiment metadata.
  * No state-file polling, no mode switching, no token-refresh
    loop, no pepper.state broadcasts. Tokens are minted with
    a 30-day TTL so they cover any single study session, then
    the process just sleeps to keep the docker container alive.

If you ever need richer behaviour for the experiment, port pieces
from orchestrator.py — they share the same building blocks.
"""

from __future__ import annotations

import asyncio
import datetime
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from livekit import api

from config import (
    DEBUG_CLI_IDENTITY,
    LIVEKIT_HOST_WS_URL,
    LIVEKIT_HTTP_URL,
    LIVEKIT_SESSION_FILE,
    LIVEKIT_URL,
    LISTENER_IDENTITY,
    MONITOR_IDENTITY,
    TABLET_IDENTITY,
    USER_IDENTITY,
)

ROOT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
SESSION_FILE = Path(LIVEKIT_SESSION_FILE)
EXPERIMENT_ROOM_NAME = os.environ.get(
    "EXPERIMENT_ROOM_NAME", "pepper-experiment"
)
TOKEN_TTL_DAYS = 30


def _load_root_env() -> None:
    if ROOT_ENV_PATH.exists():
        load_dotenv(dotenv_path=ROOT_ENV_PATH, override=False)


def _required_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


class ExperimentOrchestrator:
    def __init__(self) -> None:
        self.api_key = _required_env("LIVEKIT_API_KEY")
        self.api_secret = _required_env("LIVEKIT_API_SECRET")
        self.room_name = EXPERIMENT_ROOM_NAME
        print(
            f"[experiment-orchestrator] room={self.room_name} "
            f"livekit={LIVEKIT_URL}"
        )

    def _lkapi(self) -> api.LiveKitAPI:
        return api.LiveKitAPI(LIVEKIT_HTTP_URL, self.api_key, self.api_secret)

    def _build_token(
        self, identity: str, *, can_publish: bool, can_subscribe: bool
    ) -> str:
        return (
            api.AccessToken(self.api_key, self.api_secret)
            .with_ttl(datetime.timedelta(days=TOKEN_TTL_DAYS))
            .with_identity(identity)
            .with_name(identity)
            .with_grants(api.VideoGrants(
                room_join=True,
                room=self.room_name,
                can_publish=can_publish,
                can_subscribe=can_subscribe,
                can_publish_data=True,
            ))
            .to_jwt()
        )

    async def _ensure_room(self) -> None:
        lkapi = self._lkapi()
        try:
            try:
                await lkapi.room.create_room(api.CreateRoomRequest(
                    name=self.room_name, empty_timeout=300,
                ))
                print(f"[experiment-orchestrator] created room={self.room_name}")
            except Exception as exc:
                # `already exists` is fine — we're idempotent.
                if "already exists" not in str(exc).lower():
                    raise
                print(f"[experiment-orchestrator] room exists name={self.room_name}")
        finally:
            await lkapi.aclose()

    def _write_tokens(self) -> None:
        payload = {
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "roomName": self.room_name,
            "wsUrl": LIVEKIT_URL,
            "internalWsUrl": LIVEKIT_URL,
            "hostWsUrl": LIVEKIT_HOST_WS_URL,
            "source": "experiment-orchestrator",
            "user": {
                "identity": USER_IDENTITY,
                "token": self._build_token(USER_IDENTITY, can_publish=True, can_subscribe=True),
            },
            "listener": {
                "identity": LISTENER_IDENTITY,
                "token": self._build_token(LISTENER_IDENTITY, can_publish=False, can_subscribe=True),
            },
            "monitor": {
                "identity": MONITOR_IDENTITY,
                "token": self._build_token(MONITOR_IDENTITY, can_publish=False, can_subscribe=True),
            },
            "debugCli": {
                "identity": DEBUG_CLI_IDENTITY,
                "token": self._build_token(DEBUG_CLI_IDENTITY, can_publish=False, can_subscribe=True),
            },
            "tablet": {
                "identity": TABLET_IDENTITY,
                "token": self._build_token(TABLET_IDENTITY, can_publish=True, can_subscribe=True),
            },
            # `agent.name` is documentation-only here — the experiment.py
            # launcher dispatches the agent itself, not this process.
            "agent": {"name": "pepper-experiment"},
        }
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SESSION_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(SESSION_FILE)
        print(f"[experiment-orchestrator] wrote tokens to {SESSION_FILE}")

    async def run(self) -> None:
        # Bootstrap: retry until LiveKit is reachable.
        while True:
            try:
                await self._ensure_room()
                break
            except Exception as exc:
                print(f"[experiment-orchestrator] bootstrap failed err={exc} — retrying in 3s")
                await asyncio.sleep(3)
        self._write_tokens()
        print(
            "[experiment-orchestrator] ready. token TTL "
            f"{TOKEN_TTL_DAYS}d — sleeping (no refresh loop)."
        )
        # Keep the container alive so docker doesn't restart-loop us.
        while True:
            await asyncio.sleep(3600)


def main() -> None:
    _load_root_env()
    asyncio.run(ExperimentOrchestrator().run())


if __name__ == "__main__":
    main()
