"""
Orchestrator — lightweight replacement for session-manager.

Creates a LiveKit room, writes access tokens for all services,
dispatches the warm voice-agent, and refreshes tokens periodically.

Single source of truth for runtime state: `services/data/state.json`.
Schema: {"agent_mode": "openai|local", "mic_muted": bool, "updatedAt": "..."}

External writers (text_chat CLI, manual edits) change the file; the
orchestrator polls it every few seconds and actuates changes:
- Mode change → shutdown current agent, dispatch new warm agent.
- Mic change → broadcast state on `pepper.state` so user-client toggles.

On any state change the orchestrator also publishes the full state to
the LiveKit `pepper.state` topic so observers (text_chat, operator UI,
user-client) see live updates without polling the file.

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

from config import (
    AGENT_NAMES,
    DEBUG_CLI_IDENTITY,
    LIVEKIT_HOST_WS_URL,
    LIVEKIT_HTTP_URL,
    LIVEKIT_SESSION_FILE,
    LIVEKIT_URL,
    LISTENER_IDENTITY,
    MONITOR_IDENTITY,
    PEPPER_AGENT_MODE_DEFAULT,
    STATE_FILE,
    STATE_POLL_SEC,
    TABLET_IDENTITY,
    TOKEN_REFRESH_SEC,
    USER_IDENTITY,
)

ROOT_ENV_PATH = Path(__file__).resolve().parents[2] / ".env"
SESSION_FILE = Path(LIVEKIT_SESSION_FILE)
STATE_FILE = Path(STATE_FILE)
LEGACY_CONFIG_FILE = Path(__file__).resolve().parent / "orchestrator_config.json"
TOPIC_STATE = "pepper.state"


def _load_root_env() -> None:
    if ROOT_ENV_PATH.exists():
        load_dotenv(dotenv_path=ROOT_ENV_PATH, override=True)


_load_root_env()


def _required_env(name: str) -> str:
    value = (os.getenv(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required env: {name}")
    return value


def _default_state() -> dict:
    return {
        "agent_mode": PEPPER_AGENT_MODE_DEFAULT,
        "mic_muted": False,
    }


def _read_state() -> dict:
    """Read state.json, creating defaults if missing. Migrates from the old
    `orchestrator_config.json` if that still exists."""
    if not STATE_FILE.exists():
        state = _default_state()
        # One-shot migration from the old file if present
        if LEGACY_CONFIG_FILE.exists():
            try:
                legacy = json.loads(LEGACY_CONFIG_FILE.read_text(encoding="utf-8"))
                if legacy.get("agent_mode") in ("openai", "local"):
                    state["agent_mode"] = legacy["agent_mode"]
                    print(f"[orchestrator] migrated agent_mode from {LEGACY_CONFIG_FILE}")
            except Exception as exc:
                print(f"[orchestrator] legacy config read failed err={exc}")
        _write_state(state)
        print(f"[orchestrator] created state file {STATE_FILE}")
        return state
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[orchestrator] state read failed err={exc}, using defaults")
        return _default_state()


def _write_state(state: dict) -> None:
    """Write state.json atomically, stamping updatedAt.

    Chmods to 0o666 so the host-side text_chat CLI (running as the user,
    not root) can also write it. The orchestrator is the only writer from
    inside the container; shared writability is only needed because the
    Docker container runs as root while the host edits are from the user.
    """
    payload = dict(state)
    payload["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp.replace(STATE_FILE)
    try:
        os.chmod(STATE_FILE, 0o666)
    except OSError:
        pass


# ── Orchestrator ────────────────────────────────────────────────────────────

class Orchestrator:
    """Owns the LiveKit room lifecycle and the voice-agent dispatch.

    Responsibilities:
      - Create one fresh room per process lifetime, with a unique
        `pepper-<ts>` name, and GC any older `pepper-*` rooms that
        survived a previous run.
      - Mint per-role JWTs (`user`, `listener`, `monitor`, `debugCli`,
        `tablet`, `agent`) and write them to the token file.
      - Dispatch the warm voice-agent worker and confirm it joined
        the room (with force-cleanup retries to prevent zombie
        stacking — see `_dispatch_and_confirm`).
      - Watch `state.json` for runtime changes (mode switch, mic
        mute, manual re-dispatch) and actuate them.
      - Broadcast current state on `pepper.state` so late-joining
        observers (text_chat, tablet_server, user_client) see it
        without request/response.
    """

    def __init__(self) -> None:
        self.api_key = _required_env("LIVEKIT_API_KEY")
        self.api_secret = _required_env("LIVEKIT_API_SECRET")
        self.room_name = f"pepper-{int(time.time())}"
        state = _read_state()
        self.agent_mode = state.get("agent_mode", "openai")
        if self.agent_mode not in ("openai", "local"):
            self.agent_mode = "openai"
        self.mic_muted = bool(state.get("mic_muted", False))
        self.agent_name = AGENT_NAMES.get(self.agent_mode, AGENT_NAMES["openai"])
        # Initialize nonce to the current file value so orchestrator startup
        # doesn't self-trigger a re-dispatch (the file's nonce predates this run).
        self._last_dispatch_nonce = state.get("dispatch_nonce")
        print(
            f"[orchestrator] mode={self.agent_mode} agent={self.agent_name} "
            f"mic_muted={self.mic_muted} room={self.room_name}"
        )

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
            "tablet": {
                "identity": TABLET_IDENTITY,
                "token": self._build_token(TABLET_IDENTITY, can_publish=True, can_subscribe=True),
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

    # Timing constants. JOIN_TIMEOUT is generous because prewarm on first boot
    # can take 30-60s (VAD load + weaviate seed + ICE negotiation). EXIT_TIMEOUT
    # is tight — a warm agent that receives the shutdown signal should leave
    # within a few seconds; if it doesn't, we fall through to forced cleanup.
    JOIN_TIMEOUT_SEC = 45.0
    EXIT_TIMEOUT_SEC = 15.0
    POLL_INTERVAL_SEC = 1.0

    async def _list_agent_participants(self, lkapi: api.LiveKitAPI) -> list[str]:
        """Return identities of all agent-kind participants currently in the room."""
        try:
            resp = await lkapi.room.list_participants(
                api.ListParticipantsRequest(room=self.room_name)
            )
        except Exception:
            return []
        out: list[str] = []
        for p in getattr(resp, "participants", []) or []:
            identity = str(getattr(p, "identity", "") or "")
            kind = str(getattr(p, "kind", "") or "").upper()
            if identity.startswith("agent-") or "AGENT" in kind:
                out.append(identity)
        return out

    async def _dispatch_agent(self, lkapi: api.LiveKitAPI) -> str:
        """Create an agent dispatch; return dispatch_id or '' on failure."""
        metadata = json.dumps({"warm": True, "agent_mode": self.agent_mode})
        try:
            dispatch = await lkapi.agent_dispatch.create_dispatch(
                api.CreateAgentDispatchRequest(
                    agent_name=self.agent_name,
                    room=self.room_name,
                    metadata=metadata,
                )
            )
            dispatch_id = str(getattr(dispatch, "id", "") or "")
            print(f"[orchestrator] dispatched agent={self.agent_name} mode={self.agent_mode} dispatch_id={dispatch_id}")
            return dispatch_id
        except Exception as exc:
            print(f"[orchestrator] dispatch failed err={exc}")
            return ""

    async def _wait_for_agent_join(self, lkapi: api.LiveKitAPI, timeout: float) -> bool:
        """Poll until at least one agent-kind participant is in the room."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if await self._list_agent_participants(lkapi):
                return True
            await asyncio.sleep(self.POLL_INTERVAL_SEC)
        return False

    async def _wait_for_agents_gone(self, lkapi: api.LiveKitAPI, timeout: float) -> bool:
        """Poll until no agent-kind participants remain in the room."""
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            if not await self._list_agent_participants(lkapi):
                return True
            await asyncio.sleep(self.POLL_INTERVAL_SEC)
        return False

    async def _force_remove_agents(self, lkapi: api.LiveKitAPI) -> None:
        """Fallback: force-remove all agent participants + delete all dispatches.

        RemoveParticipant signals the participant via their own connection and
        may fail for true zombies (their signaling path is dead). We log and
        continue — `empty_timeout` on the room will eventually GC them, or
        deleting the room is the nuclear option.
        """
        identities = await self._list_agent_participants(lkapi)
        for identity in identities:
            try:
                await lkapi.room.remove_participant(
                    api.RoomParticipantIdentity(room=self.room_name, identity=identity)
                )
                print(f"[orchestrator] force-removed agent={identity}")
            except Exception as exc:
                print(f"[orchestrator] force-remove failed agent={identity} err={exc}")
        await self._cleanup_stale_dispatches(lkapi)

    async def _send_shutdown_signal(self, lkapi: api.LiveKitAPI) -> None:
        """Tell the current agent to exit cleanly via LiveKit data channel."""
        payload = json.dumps({"action": "shutdown"}).encode("utf-8")
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

    async def _dispatch_and_confirm(self) -> None:
        """Dispatch a single agent and wait for it to join.

        If it doesn't join within JOIN_TIMEOUT, force-clean the room (remove
        any stale participants + dispatches) and retry exactly once. This
        prevents the old multi-dispatch behavior that caused zombie stacking.
        """
        lkapi = self._lkapi()
        try:
            for attempt in (1, 2):
                await self._dispatch_agent(lkapi)
                if await self._wait_for_agent_join(lkapi, self.JOIN_TIMEOUT_SEC):
                    print("[orchestrator] agent confirmed in room")
                    return
                print(
                    f"[orchestrator] agent did not join within {self.JOIN_TIMEOUT_SEC:.0f}s "
                    f"(attempt {attempt}/2) — forcing cleanup"
                )
                await self._force_remove_agents(lkapi)
            print("[orchestrator] WARNING: agent failed to join after 2 attempts — giving up")
        finally:
            await lkapi.aclose()

    async def _switch_mode(self, new_mode: str) -> None:
        """Switch the agent mode: shutdown current, wait for it to leave, dispatch new.

        The key improvement over the old design: we poll `list_participants`
        to confirm the old agent actually left before dispatching the new one.
        If it doesn't leave within EXIT_TIMEOUT, we force-remove it. This is
        what prevents zombie stacking across rapid mode toggles.
        """
        print(f"[orchestrator] mode switch {self.agent_mode} -> {new_mode}")

        lkapi = self._lkapi()
        try:
            await self._send_shutdown_signal(lkapi)
            if not await self._wait_for_agents_gone(lkapi, self.EXIT_TIMEOUT_SEC):
                print(
                    f"[orchestrator] old agent still present after {self.EXIT_TIMEOUT_SEC:.0f}s "
                    "— forcing removal"
                )
                await self._force_remove_agents(lkapi)
            else:
                # Graceful exit succeeded; still clean up any lingering dispatch records.
                await self._cleanup_stale_dispatches(lkapi)
        finally:
            await lkapi.aclose()

        self.agent_mode = new_mode
        self.agent_name = AGENT_NAMES.get(new_mode, AGENT_NAMES["openai"])

        await self._dispatch_and_confirm()
        print(f"[orchestrator] mode switch complete, now running {new_mode}")

    async def _force_redispatch(self) -> None:
        """Manual re-dispatch of the current mode. Useful when voice-agent
        crashed / hot-reloaded and the orchestrator's view of the room is stale
        (agent left but we never noticed). Keeps the same room."""
        print(f"[orchestrator] force re-dispatch mode={self.agent_mode}")
        lkapi = self._lkapi()
        try:
            await self._send_shutdown_signal(lkapi)
            if not await self._wait_for_agents_gone(lkapi, self.EXIT_TIMEOUT_SEC):
                print(
                    f"[orchestrator] stale agent still present after {self.EXIT_TIMEOUT_SEC:.0f}s "
                    "— forcing removal"
                )
                await self._force_remove_agents(lkapi)
            else:
                await self._cleanup_stale_dispatches(lkapi)
        finally:
            await lkapi.aclose()
        await self._dispatch_and_confirm()
        print(f"[orchestrator] force re-dispatch complete mode={self.agent_mode}")

    async def _broadcast_state(self) -> None:
        """Publish the current runtime state on the pepper.state LiveKit topic.

        Called after any state change AND periodically so late-joining
        subscribers (text_chat, user-client) always see fresh state without
        a request/response dance.
        """
        payload = json.dumps({
            "agent_mode": self.agent_mode,
            "agent_name": self.agent_name,
            "mic_muted": self.mic_muted,
            "roomName": self.room_name,
            "updatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }).encode("utf-8")
        lkapi = self._lkapi()
        try:
            await lkapi.room.send_data(
                api.SendDataRequest(
                    room=self.room_name,
                    data=payload,
                    topic=TOPIC_STATE,
                )
            )
        except Exception as exc:
            print(f"[orchestrator] state broadcast failed err={exc}")
        finally:
            await lkapi.aclose()

    async def _state_watcher(self) -> None:
        """Poll state.json for external changes and actuate them.

        Mode changes trigger a full agent shutdown + re-dispatch.
        Mic changes are just broadcast via pepper.state (user-client subscribes).
        """
        while True:
            await asyncio.sleep(STATE_POLL_SEC)
            try:
                state = _read_state()
                new_mode = state.get("agent_mode", "openai")
                if new_mode not in ("openai", "local"):
                    new_mode = "openai"
                new_mic = bool(state.get("mic_muted", False))

                if new_mode != self.agent_mode:
                    await self._switch_mode(new_mode)
                    await self._broadcast_state()

                new_nonce = state.get("dispatch_nonce")
                if new_nonce is not None and new_nonce != self._last_dispatch_nonce:
                    self._last_dispatch_nonce = new_nonce
                    await self._force_redispatch()
                    await self._broadcast_state()

                if new_mic != self.mic_muted:
                    self.mic_muted = new_mic
                    print(f"[orchestrator] mic_muted change -> {self.mic_muted}")
                    await self._broadcast_state()
            except Exception as exc:
                print(f"[orchestrator] state watcher error err={exc}")

    async def _state_heartbeat(self) -> None:
        """Re-broadcast current state every 10s so late subscribers catch up."""
        while True:
            await asyncio.sleep(10)
            await self._broadcast_state()

    # ── Startup helpers ──

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
        await self._dispatch_and_confirm()
        await self._broadcast_state()

        print(f"[orchestrator] running mode={self.agent_mode} mic_muted={self.mic_muted} (watching {STATE_FILE})")
        await asyncio.gather(
            self._token_refresh_loop(),
            self._state_watcher(),
            self._state_heartbeat(),
        )


if __name__ == "__main__":
    asyncio.run(Orchestrator().run())
