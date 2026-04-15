"""
Orchestrator — lightweight replacement for session-manager.

Creates a LiveKit room, writes access tokens for all services,
dispatches the warm voice-agent, and refreshes tokens periodically.

Mode switching: reads `agent_mode` from a JSON config file
(services/src/orchestrator_config.json). Change the file while running
and the orchestrator will shut down the current agent and dispatch
a new one in the new mode.

No HTTP server, no dashboard, no health probing.
"""

import asyncio
import datetime
import json
import os
import time
from pathlib import Path

from dotenv import load_dotenv
from livekit import api

# ── Config ──────────────────────────────────────────────────────────────────

ROOT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
CONFIG_FILE = Path(__file__).resolve().parent / "orchestrator_config.json"
CONFIG_POLL_SEC = 3  # how often to check for config changes

def _load_root_env() -> None:
    if ROOT_ENV_PATH.exists():
        load_dotenv(dotenv_path=ROOT_ENV_PATH, override=True)

_load_root_env()

def _env(name: str, default: str = "") -> str:
    return (os.getenv(name) or default).strip()

def _required_env(name: str) -> str:
    value = _env(name)
    if not value:
        raise RuntimeError(f"Missing required env: {name}")
    return value

LIVEKIT_URL = _env("LIVEKIT_URL", "ws://127.0.0.1:7880")
LIVEKIT_HOST_WS_URL = _env("LIVEKIT_HOST_WS_URL", LIVEKIT_URL)
LIVEKIT_HTTP_URL = _env("LIVEKIT_HTTP_URL", "http://127.0.0.1:7880")
SESSION_FILE = Path(_env(
    "LIVEKIT_SESSION_FILE",
    str(Path(__file__).resolve().parent / "session_manager" / "data" / "token-latest.json"),
))
AGENT_NAMES = {
    "openai": _env("PEPPER_AGENT_NAME_OPENAI", "pepper-openai"),
    "local": _env("PEPPER_AGENT_NAME_LOCAL", "pepper-local"),
}
USER_IDENTITY = "user"
LISTENER_IDENTITY = "listener-python"
MONITOR_IDENTITY = "monitor-python"
DEBUG_CLI_IDENTITY = "debug-cli"
TOKEN_REFRESH_SEC = 4 * 3600  # 4 hours


def _read_config() -> dict:
    """Read orchestrator config file. Creates default if missing."""
    if not CONFIG_FILE.exists():
        default = {"agent_mode": _env("PEPPER_AGENT_MODE", "openai")}
        CONFIG_FILE.write_text(json.dumps(default, indent=2), encoding="utf-8")
        print(f"[orchestrator] created config file {CONFIG_FILE}")
        return default
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[orchestrator] config read failed err={exc}, using defaults")
        return {"agent_mode": "openai"}


# ── Orchestrator ────────────────────────────────────────────────────────────

class Orchestrator:
    def __init__(self) -> None:
        self.api_key = _required_env("LIVEKIT_API_KEY")
        self.api_secret = _required_env("LIVEKIT_API_SECRET")
        self.room_name = f"pepper-{int(time.time())}"
        config = _read_config()
        self.agent_mode = config.get("agent_mode", "openai")
        if self.agent_mode not in ("openai", "local"):
            self.agent_mode = "openai"
        self.agent_name = AGENT_NAMES.get(self.agent_mode, AGENT_NAMES["openai"])
        print(f"[orchestrator] mode={self.agent_mode} agent={self.agent_name} room={self.room_name}")

    def _lkapi(self) -> api.LiveKitAPI:
        return api.LiveKitAPI(LIVEKIT_HTTP_URL, self.api_key, self.api_secret)

    def _build_token(self, identity: str, *, can_publish: bool, can_subscribe: bool) -> str:
        return (
            api.AccessToken(self.api_key, self.api_secret)
            .with_ttl(datetime.timedelta(days=30))
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

    # ── Room lifecycle ──

    async def _cleanup_old_rooms(self, lkapi: api.LiveKitAPI) -> None:
        try:
            existing = await lkapi.room.list_rooms(api.ListRoomsRequest())
            for room in getattr(existing, "rooms", []) or []:
                name = str(getattr(room, "name", "") or "")
                if name.startswith("pepper-") and name != self.room_name:
                    try:
                        await lkapi.room.delete_room(api.DeleteRoomRequest(room=name))
                        print(f"[orchestrator] deleted old room={name}")
                    except Exception as exc:
                        if "could not find object" not in str(exc):
                            print(f"[orchestrator] delete old room failed room={name} err={exc}")
        except Exception as exc:
            print(f"[orchestrator] list rooms failed err={exc}")

    async def _cleanup_stale_dispatches(self, lkapi: api.LiveKitAPI) -> None:
        try:
            for dispatch in await lkapi.agent_dispatch.list_dispatch(self.room_name):
                dispatch_id = str(getattr(dispatch, "id", "") or "")
                if dispatch_id:
                    try:
                        await lkapi.agent_dispatch.delete_dispatch(dispatch_id, self.room_name)
                        print(f"[orchestrator] deleted stale dispatch id={dispatch_id}")
                    except Exception as exc:
                        print(f"[orchestrator] delete dispatch failed id={dispatch_id} err={exc}")
        except Exception:
            pass  # room might not exist yet

    async def _ensure_room(self) -> None:
        lkapi = self._lkapi()
        try:
            await self._cleanup_old_rooms(lkapi)
            await lkapi.room.create_room(
                api.CreateRoomRequest(name=self.room_name, empty_timeout=300)
            )
            print(f"[orchestrator] created room={self.room_name}")
            await self._cleanup_stale_dispatches(lkapi)
        finally:
            await lkapi.aclose()

    # ── Tokens ──

    async def _write_tokens(self) -> None:
        payload = {
            "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "roomName": self.room_name,
            "wsUrl": LIVEKIT_URL,
            "internalWsUrl": LIVEKIT_URL,
            "hostWsUrl": LIVEKIT_HOST_WS_URL,
            "source": "orchestrator",
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
            "agent": {"name": self.agent_name},
        }
        SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = SESSION_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(SESSION_FILE)
        print(f"[orchestrator] wrote tokens to {SESSION_FILE}")

    async def _token_refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(TOKEN_REFRESH_SEC)
            try:
                await self._write_tokens()
                print("[orchestrator] tokens refreshed")
            except Exception as exc:
                print(f"[orchestrator] token refresh failed err={exc}")

    # ── Agent dispatch & mode switching ──

    async def _dispatch_warm_agent(self) -> None:
        metadata = json.dumps({"warm": True, "agent_mode": self.agent_mode})
        lkapi = self._lkapi()
        try:
            dispatch = await lkapi.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=self.agent_name,
                    room=self.room_name,
                    metadata=metadata,
                )
            )
            dispatch_id = str(getattr(dispatch, "id", "") or "")
            print(f"[orchestrator] warm agent dispatched name={self.agent_name} mode={self.agent_mode} dispatch_id={dispatch_id}")
        except Exception as exc:
            print(f"[orchestrator] dispatch failed err={exc}")
        finally:
            await lkapi.aclose()

    async def _send_shutdown_signal(self) -> None:
        """Tell the current agent to exit cleanly via LiveKit data channel."""
        payload = json.dumps({"action": "shutdown"}).encode("utf-8")
        lkapi = self._lkapi()
        try:
            await lkapi.room.send_data(
                api.SendDataRequest(
                    room=self.room_name,
                    data=payload,
                    topic="session-control",
                )
            )
            print(f"[orchestrator] sent shutdown signal to room={self.room_name}")
        except Exception as exc:
            print(f"[orchestrator] shutdown signal failed err={exc}")
        finally:
            await lkapi.aclose()

    async def _switch_mode(self, new_mode: str) -> None:
        """Switch the agent mode: shutdown current agent, dispatch new one."""
        print(f"[orchestrator] mode switch {self.agent_mode} -> {new_mode}")

        # Tell the current agent to exit
        await self._send_shutdown_signal()
        # Give it time to exit and free the worker slot
        await asyncio.sleep(5)

        # Update state
        self.agent_mode = new_mode
        self.agent_name = AGENT_NAMES.get(new_mode, AGENT_NAMES["openai"])

        # Dispatch new agent with retry
        await self._dispatch_with_retry()
        print(f"[orchestrator] mode switch complete, now running {new_mode}")

    async def _config_watcher(self) -> None:
        """Poll the config file for mode changes."""
        while True:
            await asyncio.sleep(CONFIG_POLL_SEC)
            try:
                config = _read_config()
                new_mode = config.get("agent_mode", "openai")
                if new_mode not in ("openai", "local"):
                    continue
                if new_mode != self.agent_mode:
                    await self._switch_mode(new_mode)
            except Exception as exc:
                print(f"[orchestrator] config watcher error err={exc}")

    # ── Startup helpers ──

    async def _is_agent_in_room(self) -> bool:
        """Check if an agent participant is present in the room."""
        lkapi = self._lkapi()
        try:
            resp = await lkapi.room.list_participants(
                api.ListParticipantsRequest(room=self.room_name)
            )
            for p in getattr(resp, "participants", []) or []:
                identity = str(getattr(p, "identity", "") or "")
                kind = str(getattr(p, "kind", "") or "").upper()
                if identity.startswith("agent-") or "AGENT" in kind:
                    return True
            return False
        except Exception:
            return False
        finally:
            await lkapi.aclose()

    async def _dispatch_with_retry(self) -> None:
        """Dispatch agent and verify it joined, retrying if needed."""
        for attempt in range(6):
            if attempt > 0:
                print(f"[orchestrator] re-dispatching (attempt {attempt + 1})...")
            await self._dispatch_warm_agent()
            await asyncio.sleep(15)
            if await self._is_agent_in_room():
                print("[orchestrator] agent confirmed in room")
                return
            print("[orchestrator] agent not in room yet — will retry")
        print("[orchestrator] WARNING: agent did not join after retries")

    # ── Main ──

    async def run(self) -> None:
        # Bootstrap: retry until LiveKit is ready
        while True:
            try:
                await self._ensure_room()
                break
            except Exception as exc:
                print(f"[orchestrator] bootstrap failed err={exc} — retrying in 3s")
                await asyncio.sleep(3)

        await self._write_tokens()
        await self._dispatch_with_retry()

        print(f"[orchestrator] running mode={self.agent_mode} (watching {CONFIG_FILE})")
        await asyncio.gather(
            self._token_refresh_loop(),
            self._config_watcher(),
        )


if __name__ == "__main__":
    asyncio.run(Orchestrator().run())
