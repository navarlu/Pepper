import asyncio
import contextlib
import datetime
import json
import math
import os
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

import aiohttp
from dotenv import load_dotenv
from livekit import api

from ..config import (
    BRIDGE_URL,
    LISTENER_IDENTITY,
    MONITOR_IDENTITY,
    LIVEKIT_HOST_WS_URL,
    LIVEKIT_HTTP_URL,
    LIVEKIT_SESSION_FILE,
    LIVEKIT_STATUS_POLL_INTERVAL_SEC,
    LIVEKIT_URL,
    SESSION_ACTIVITY_DEBOUNCE_SEC,
    SESSION_COOLDOWN_SEC,
    SESSION_IDLE_TIMEOUT_SEC,
    SESSION_MANAGER_HOST,
    SESSION_MANAGER_PORT,
    SESSION_PREROLL_ACTIVITY_SEC,
    USER_IDENTITY,
)
from .infra import (
    docker_get_json,
    docker_get_text,
    host_port_from_url,
    list_docker_containers,
    probe_http_health,
    probe_local_llm,
    probe_tcp,
)
from .web import create_app

ROOT_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"
AGENT_NAME_DEFAULT = "Pepper"
SESSION_SOURCE_USER = "user"
SESSION_SOURCE_AGENT = "agent"
SESSION_MANAGER_STATE_FILE = "session-manager-state.json"
MAX_TRANSCRIPT_ITEMS = 40
COMPONENT_PROBE_INTERVAL_SEC = 60.0
WARM_AGENT_JOIN_TIMEOUT_SEC = float(os.getenv("WARM_AGENT_JOIN_TIMEOUT_SEC", "90"))
DEFAULT_LOCAL_AUDIO_VOLUME = int(os.getenv("PEPPER_OUTPUT_VOLUME", "55"))
WARM_ACTIVATION_MIN_LEVEL = float(os.getenv("WARM_ACTIVATION_MIN_LEVEL", "0.05"))
DOCKER_SOCKET_PATH = os.getenv("DOCKER_SOCKET_PATH", "/var/run/docker.sock")
DOCKER_LOG_TAIL_LINES = int(os.getenv("DOCKER_LOG_TAIL_LINES", "160"))
KNOWN_DOCKER_SERVICES = (
    "bridge",
    "audio-bridge",
    "room-monitor",
    "livekit",
    "redis",
    "safe-startup",
    "session-manager",
    "user-client",
    "voice-agent",
    "weaviate",
)


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
    TOKEN_REFRESH_INTERVAL_SEC = 4 * 3600

    def __init__(self) -> None:
        _load_root_env()
        self.room_name = f"pepper-{int(time.time())}"
        self.livekit_ws_url = LIVEKIT_URL
        self.livekit_host_ws_url = LIVEKIT_HOST_WS_URL
        self.livekit_http_url = LIVEKIT_HTTP_URL
        self.bridge_url = BRIDGE_URL
        self.dev_console_url = os.getenv("DEV_CONSOLE_URL", "http://localhost:8788").rstrip("/")
        self.session_file = Path(LIVEKIT_SESSION_FILE)
        self.state_file = self.session_file.with_name(SESSION_MANAGER_STATE_FILE)
        self.api_key = _get_required_env("LIVEKIT_API_KEY")
        self.api_secret = _get_required_env("LIVEKIT_API_SECRET")
        self.agent_name = (os.getenv("PEPPER_AGENT_NAME") or AGENT_NAME_DEFAULT).strip() or AGENT_NAME_DEFAULT
        self.agent_mode = "openai"
        self.local_llm_base_url = os.getenv("LOCAL_LLM_BASE_URL", "http://localhost:8000/v1").rstrip("/")
        self.local_llm_healthy = False
        self.session_state = "idle"
        self.agent_deployed = False
        # Warm agent lifecycle: "none" → "deploying" → "ready"
        self._warm_phase = "none"
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
        self.local_audio_volume = max(0, min(100, DEFAULT_LOCAL_AUDIO_VOLUME))
        self._bridge_audio_volume_synced: int | None = None
        self.pending_user_texts: list[dict[str, str]] = []
        self._session_log: list[dict[str, Any]] = []
        self._session_log_started_at: str = ""
        self.components: dict[str, dict[str, Any]] = {}
        self.docker_socket_path = DOCKER_SOCKET_PATH
        self.docker_log_tail_lines = DOCKER_LOG_TAIL_LINES
        self._load_persisted_state()
        self.watchdog_status: dict[str, Any] = {
            "summary": "waiting for watchdog",
            "pepper_reachable": False,
            "safe_startup_running": False,
            "last_result": "",
            "updated_at": "",
        }
        self._lock = asyncio.Lock()
        self._bg_tasks: list[asyncio.Task[Any]] = []
        self._bootstrap_complete = False
        self._register_component("session-manager", state="starting", detail="initializing", healthy=True, source="internal")
        self._register_component("safe-startup", state="unknown", detail="waiting for watchdog", healthy=False, source="service")
        # Probe-based components (checked periodically in probe_components_loop)
        for name in ("bridge", "livekit", "redis", "weaviate", "local-llm"):
            self._register_component(name, state="unknown", detail="waiting for probe", healthy=False, source="probe")

    def _clear_agent_runtime_state(self) -> None:
        self.agent_deployed = False
        self._warm_phase = "none"
        self.active_dispatch_id = ""
        self.dispatch_started_monotonic = 0.0

    def _load_persisted_state(self) -> None:
        try:
            payload = json.loads(self.state_file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return
        except Exception as exc:
            print(f"[session_manager] load state failed path={self.state_file} err={exc}")
            return
        self.mic_muted = bool(payload.get("mic_muted", self.mic_muted))
        loaded_mode = str(payload.get("agent_mode", "")).strip().lower()
        if loaded_mode in ("openai", "local"):
            self.agent_mode = loaded_mode
        loaded_volume = payload.get("local_audio_volume")
        try:
            self.local_audio_volume = max(0, min(100, int(loaded_volume)))
        except Exception:
            pass

    def _persist_state(self) -> None:
        try:
            self.state_file.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "mic_muted": self.mic_muted,
                "agent_mode": self.agent_mode,
                "local_audio_volume": self.local_audio_volume,
                "updated_at": _utc_now_iso(),
            }
            tmp_path = self.state_file.with_suffix(".tmp")
            tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            tmp_path.replace(self.state_file)
        except Exception as exc:
            print(f"[session_manager] persist state failed path={self.state_file} err={exc}")

    async def _apply_bridge_audio_volume(self, *, force: bool = False) -> tuple[bool, str]:
        target_volume = max(0, min(100, int(self.local_audio_volume)))
        if not force and self._bridge_audio_volume_synced == target_volume:
            return True, "already synced"
        url = self.bridge_url.rstrip("/") + "/audio/volume"
        timeout = aiohttp.ClientTimeout(total=1.5)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json={"volume": target_volume}) as response:
                    data = await response.json(content_type=None)
                    if response.status >= 400 or not bool(data.get("ok")):
                        error = str(data.get("error") or "bridge rejected volume update")
                        return False, error
                    applied = int(data.get("volume", target_volume))
                    self._bridge_audio_volume_synced = applied
                    return True, ""
        except Exception as exc:
            return False, str(exc)

    async def _push_tablet(self, payload: dict) -> None:
        """Push a payload to Pepper's tablet via the bridge (fire-and-forget)."""
        url = self.bridge_url.rstrip("/") + "/tablet/text_inline"
        timeout = aiohttp.ClientTimeout(total=1.0)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload) as resp:
                    await resp.read()
        except Exception:
            pass

    async def _notify_tablet_status(self, status: str) -> None:
        """Push a simple status message to Pepper's tablet."""
        await self._push_tablet({
            "text": status,
            "size": 52,
            "bg": "#101820",
            "fg": "#D6F0FF",
            "align": "center",
        })

    async def _notify_tablet_transcript(self) -> None:
        """Push current transcript to Pepper's tablet (chat history view)."""
        items = list(self.transcript_items)
        # Drop everything before the last "Session ended" marker
        last_ended = -1
        for i, item in enumerate(items):
            if item.get("kind") == "session" and "Session ended" in str(item.get("text", "")):
                last_ended = i
        if last_ended >= 0:
            items = items[last_ended + 1:]
        # Skip "New session" marker
        if items and items[0].get("kind") == "session" and "New session" in str(items[0].get("text", "")):
            items = items[1:]
        # Keep last 10 messages
        items = items[-10:]
        await self._push_tablet({
            "ui": "chat_history",
            "transcript_items": items,
            "session_state": self.session_state,
        })

    def _register_component(self, name: str, *, state: str, detail: str, healthy: bool, source: str) -> None:
        self.components[name] = {"name": name, "state": state, "detail": detail, "healthy": healthy, "source": source}

    def _set_component_state(self, name: str, *, state: str, detail: str = "", healthy: bool = True, source: str | None = None) -> None:
        item = self.components.get(name) or {"name": name}
        item.update({"name": name, "state": state, "detail": detail, "healthy": bool(healthy)})
        if source is not None:
            item["source"] = source
        self.components[name] = item
        self.updated_at = _utc_now_iso()

    async def _docker_get_json(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        timeout_sec: float = 1.5,
    ) -> Any:
        return await docker_get_json(
            self.docker_socket_path,
            path,
            params=params,
            timeout_sec=timeout_sec,
        )

    async def _docker_get_text(
        self,
        path: str,
        *,
        params: dict[str, str] | None = None,
        timeout_sec: float = 2.5,
    ) -> str:
        return await docker_get_text(
            self.docker_socket_path,
            path,
            params=params,
            timeout_sec=timeout_sec,
        )

    async def _list_docker_containers(self) -> list[dict[str, str]]:
        return await list_docker_containers(
            self.docker_socket_path,
            KNOWN_DOCKER_SERVICES,
        )

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
            .with_ttl(datetime.timedelta(days=30))
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
        return await probe_tcp(host, port, timeout=timeout)

    async def _probe_http_health(self, raw_url: str, timeout: float = 1.0) -> bool:
        return await probe_http_health(raw_url, timeout=timeout)

    async def _probe_local_llm(self, timeout: float = 2.0) -> bool:
        return await probe_local_llm(self.local_llm_base_url, timeout=timeout)

    def _host_port_from_url(self, raw_url: str, default_port: int) -> tuple[str, int]:
        return host_port_from_url(raw_url, default_port)

    async def ensure_room(self) -> None:
        lkapi = self._new_lkapi()
        try:
            # Delete any leftover rooms from previous runs.
            existing = await lkapi.room.list_rooms(api.ListRoomsRequest())
            for old_room in getattr(existing, "rooms", []) or []:
                old_name = str(getattr(old_room, "name", "") or "")
                if old_name.startswith("pepper-") and old_name != self.room_name:
                    try:
                        await lkapi.room.delete_room(api.DeleteRoomRequest(room=old_name))
                        print(f"[session_manager] deleted old room={old_name}")
                    except Exception as exc:
                        print(f"[session_manager] delete old room failed room={old_name} err={exc}")
            # Create fresh room with empty_timeout so it auto-deletes when abandoned.
            await lkapi.room.create_room(
                api.CreateRoomRequest(
                    name=self.room_name,
                    empty_timeout=300,
                )
            )
            print(f"[session_manager] created room={self.room_name} empty_timeout=300s")
        except Exception as exc:
            print(f"[session_manager] ensure_room failed room={self.room_name} err={exc}")
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
                await self._remove_agent_participants()
                await self.write_session_snapshot()
                self._bootstrap_complete = True
                self._set_component_state(
                    "session-manager",
                    state="ready",
                    detail="dashboard and orchestration online",
                    healthy=True,
                    source="internal",
                )
                if self.agent_mode == "local":
                    self.local_llm_healthy = await self._probe_local_llm()
                    print(f"[session_manager] bootstrap vLLM probe: healthy={self.local_llm_healthy}")
                await self._dispatch_warm_agent()
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
            if bridge_ok:
                ok, err = await self._apply_bridge_audio_volume()
                if not ok:
                    print(f"[session_manager] bridge volume sync failed err={err}")
            else:
                self._bridge_audio_volume_synced = None
            llm_ok = await self._probe_local_llm()
            llm_was_down = not self.local_llm_healthy
            self.local_llm_healthy = llm_ok
            self._set_component_state(
                "local-llm",
                state="ready" if llm_ok else "down",
                detail=self.local_llm_base_url,
                healthy=llm_ok,
                source="probe",
            )
            if llm_ok and llm_was_down and not self.agent_deployed:
                print("[session_manager] vLLM became reachable, triggering warm dispatch")
                await self._dispatch_warm_agent()
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
            "monitor": {
                "identity": MONITOR_IDENTITY,
                "token": self._build_token(
                    identity=MONITOR_IDENTITY,
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
        has_zombies = False
        try:
            response = await lkapi.room.list_participants(
                api.ListParticipantsRequest(room=self.room_name)
            )
            for participant in getattr(response, "participants", []) or []:
                identity = str(getattr(participant, "identity", "") or "")
                kind = str(getattr(participant, "kind", "") or "")
                if not _identity_is_agent(identity, kind):
                    continue
                removed = False
                for attempt in range(3):
                    try:
                        await lkapi.room.remove_participant(
                            api.RoomParticipantIdentity(room=self.room_name, identity=identity)
                        )
                        print(f"[session_manager] removed agent participant identity={identity}")
                        removed = True
                        break
                    except Exception as exc:
                        print(
                            f"[session_manager] remove agent failed identity={identity} "
                            f"attempt={attempt + 1}/3 err={exc}"
                        )
                        if attempt < 2:
                            await asyncio.sleep(1.5)
                if not removed:
                    has_zombies = True
                    print(
                        f"[session_manager] WARNING: zombie agent identity={identity} "
                        f"- will rotate room to clear"
                    )
            if self.active_dispatch_id:
                try:
                    await lkapi.agent_dispatch.delete_dispatch(self.active_dispatch_id, self.room_name)
                    print(f"[session_manager] deleted active dispatch id={self.active_dispatch_id}")
                except Exception as exc:
                    print(f"[session_manager] delete active dispatch failed id={self.active_dispatch_id} err={exc}")
            # If any zombies survived, rotate the room: delete + recreate.
            if has_zombies:
                await self._rotate_room(lkapi)
        finally:
            self.active_dispatch_id = ""
            self.dispatch_started_monotonic = 0.0
            await lkapi.aclose()

    async def _rotate_room(self, lkapi: api.LiveKitAPI) -> None:
        old_name = self.room_name
        try:
            await lkapi.room.delete_room(api.DeleteRoomRequest(room=old_name))
            print(f"[session_manager] deleted zombie room={old_name}")
        except Exception as exc:
            print(f"[session_manager] delete zombie room failed room={old_name} err={exc}")
        self.room_name = f"pepper-{int(time.time())}"
        try:
            await lkapi.room.create_room(
                api.CreateRoomRequest(
                    name=self.room_name,
                    empty_timeout=300,
                )
            )
            print(f"[session_manager] rotated to new room={self.room_name}")
        except Exception as exc:
            print(f"[session_manager] create rotated room failed room={self.room_name} err={exc}")
        # Write fresh snapshot so listener/user-client reconnect to the new room.
        await self.write_session_snapshot()

    async def _dispatch_warm_agent(self) -> None:
        """Deploy a warm agent that pre-loads models and waits for activation."""
        async with self._lock:
            if self.agent_deployed or not self._bootstrap_complete:
                return
            if self.agent_mode == "local" and not self.local_llm_healthy:
                print("[session_manager] skipping warm dispatch (local mode, vLLM not reachable)")
                self.session_state = "idle"
                self.updated_at = _utc_now_iso()
                return
            self._warm_phase = "deploying"
            metadata = json.dumps({"warm": True, "agent_mode": self.agent_mode})
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
                self.session_state = "warm"
                self.dispatch_started_monotonic = time.monotonic()
                self.updated_at = _utc_now_iso()
                print(
                    f"[session_manager] warm agent dispatched name={self.agent_name} "
                    f"room={self.room_name} dispatch_id={self.active_dispatch_id}"
                )
                await self._notify_tablet_status("Warming up...")
            except Exception as exc:
                self.session_state = "idle"
                self._clear_agent_runtime_state()
                print(f"[session_manager] warm dispatch failed err={exc}")
            finally:
                await lkapi.aclose()

    def _start_conversation(self) -> None:
        """Begin tracking a new conversation on first user activity after agent is ready."""
        if self.conversation_id:
            return
        self.conversation_id = uuid.uuid4().hex[:10]
        self._reset_session_log()
        self.session_state = "active"
        self._append_session_marker(f"New session · {self.conversation_id}")
        self.updated_at = _utc_now_iso()
        print(f"[session_manager] conversation started conversation_id={self.conversation_id}")

    async def dispatch_cold_agent(self) -> None:
        """Cold-dispatch an agent (no warm preloading). Used as fallback."""
        async with self._lock:
            if not self._bootstrap_complete or self.agent_deployed:
                return
            self.conversation_id = uuid.uuid4().hex[:10]
            self._reset_session_log()
            self.session_state = "starting"
            self.dispatch_started_monotonic = time.monotonic()
            metadata = json.dumps({"conversation_id": self.conversation_id, "agent_mode": self.agent_mode})
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
                self._warm_phase = "none"
                self.session_state = "active"
                self._append_session_marker(f"New session · {self.conversation_id}")
                self.updated_at = _utc_now_iso()
                print(
                    f"[session_manager] cold-dispatched agent name={self.agent_name} "
                    f"room={self.room_name} conversation_id={self.conversation_id}"
                )
            except Exception as exc:
                self.session_state = "idle"
                self.conversation_id = ""
                self._clear_agent_runtime_state()
                print(f"[session_manager] dispatch failed err={exc}")
            finally:
                await lkapi.aclose()

    @property
    def _is_persistent_agent(self) -> bool:
        """A persistent agent stays in the room across sessions (local mode only)."""
        return self.agent_deployed and self.agent_mode == "local"

    async def _send_reset_signal(self) -> None:
        """Tell the persistent agent to clear its history and wait for next activation."""
        payload = json.dumps({"action": "reset"}).encode("utf-8")
        lkapi = self._new_lkapi()
        try:
            await lkapi.room.send_data(
                api.SendDataRequest(
                    room=self.room_name,
                    data=payload,
                    topic="session-control",
                )
            )
            print("[session_manager] sent reset signal to persistent agent")
        except Exception as exc:
            print(f"[session_manager] reset signal failed err={exc}")
        finally:
            await lkapi.aclose()

    async def end_session(self, reason: str, *, force_teardown: bool = False) -> None:
        async with self._lock:
            if not self.agent_deployed and self.session_state == "idle":
                return
            self.session_state = "ending"
            print(f"[session_manager] ending session reason={reason} force_teardown={force_teardown}")
            ended_conversation_id = self.conversation_id
            if ended_conversation_id:
                self._write_session_log(ended_conversation_id, reason)

            use_persistent_reset = self._is_persistent_agent and not force_teardown

            if use_persistent_reset:
                # Persistent local agent: send reset, keep agent alive in room
                await self._send_reset_signal()
                # Agent stays deployed — just reset session-level state
                self._warm_phase = "deploying"  # will become "ready" on warm_ready
                self.dispatch_started_monotonic = time.monotonic()  # reset timeout window
            else:
                # OpenAI / non-persistent: tear down and re-dispatch as before
                await self._remove_agent_participants()
                self._clear_agent_runtime_state()

            self.conversation_id = ""
            self.last_user_activity_monotonic = 0.0
            self.last_agent_activity_monotonic = 0.0
            if ended_conversation_id:
                self._append_session_marker(
                    f"Session ended · {ended_conversation_id} · {reason}"
                )
            self.updated_at = _utc_now_iso()

        if use_persistent_reset:
            # No cooldown needed — agent resets instantly.
            # warm_ready event from agent will set _warm_phase = "standby"
            async with self._lock:
                self.session_state = "warm"
                self.updated_at = _utc_now_iso()
                self._set_component_state(
                    "session-manager",
                    state="ready",
                    detail="persistent agent resetting",
                    healthy=True,
                    source="internal",
                )
            print("[session_manager] persistent agent reset — waiting for warm_ready")
        else:
            self.session_state = "cooldown"
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
            await self._dispatch_warm_agent()

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
            strong_signal = level is not None and level >= WARM_ACTIVATION_MIN_LEVEL
            if strong_signal:
                if self._warm_phase == "ready" and not self.conversation_id:
                    self._start_conversation()
                elif not self.agent_deployed and self.session_state == "idle":
                    await self.dispatch_cold_agent()
        elif source == SESSION_SOURCE_AGENT:
            if now - self.last_agent_activity_monotonic < SESSION_ACTIVITY_DEBOUNCE_SEC:
                return
            self.last_agent_activity_monotonic = now
            self.last_agent_activity_at = activity_at
            if level is not None:
                self.agent_audio_level = max(0.0, min(1.0, level))

    def _append_transcript(self, speaker: str, text: str, *, kind: str = "message") -> None:
        clean = " ".join(str(text).strip().split())
        if not clean:
            return
        item = {"speaker": speaker, "text": clean, "at": _utc_now_iso(), "kind": kind}
        self.transcript_items.append(item)
        if speaker == "Pepper":
            self.last_pepper_text = clean
        elif speaker == "User":
            self.last_user_text = clean
        # Feed session log (skip tool kind — tools post their own structured events).
        if kind == "message":
            if speaker == "User":
                self._append_session_log_event({"type": "user_speech", "text": clean})
            elif speaker == "Pepper":
                self._append_session_log_event({"type": "agent_speech", "text": clean})
            # Push transcript to Pepper's tablet
            asyncio.create_task(self._notify_tablet_transcript())

    def _append_session_marker(self, text: str) -> None:
        self._append_transcript("System", text, kind="session")
        self._append_session_log_event({"type": "session_event", "detail": text})

    def _append_session_marker_once(self, text: str) -> None:
        clean = " ".join(str(text).strip().split())
        if not clean:
            return
        last_item = self.transcript_items[-1] if self.transcript_items else None
        if (
            last_item
            and last_item.get("kind") == "session"
            and str(last_item.get("text") or "").strip() == clean
        ):
            return
        self._append_session_marker(clean)

    # ── Session log (persistent) ─────────────────────────────────────

    def _append_session_log_event(self, event: dict[str, Any]) -> None:
        event["t"] = _utc_now_iso()
        self._session_log.append(event)

    def _reset_session_log(self) -> None:
        self._session_log = []
        self._session_log_started_at = _utc_now_iso()

    def _write_session_log(self, conversation_id: str, reason: str) -> None:
        if not self._session_log:
            return
        sessions_dir = self.session_file.parent / "sessions"
        sessions_dir.mkdir(parents=True, exist_ok=True)
        ended_at = _utc_now_iso()
        turns = sum(
            1 for e in self._session_log
            if e.get("type") in ("user_speech", "agent_speech")
        )
        tool_calls = sum(1 for e in self._session_log if e.get("type") == "tool_call")
        errors = sum(1 for e in self._session_log if e.get("type") == "error")
        # Calculate duration.
        duration_sec = 0.0
        if self._session_log_started_at and ended_at:
            try:
                fmt = "%Y-%m-%dT%H:%M:%SZ"
                t_start = datetime.datetime.strptime(self._session_log_started_at, fmt)
                t_end = datetime.datetime.strptime(ended_at, fmt)
                duration_sec = round((t_end - t_start).total_seconds(), 1)
            except Exception:
                pass
        log = {
            "version": 1,
            "conversation_id": conversation_id,
            "agent_mode": self.agent_mode,
            "started_at": self._session_log_started_at,
            "ended_at": ended_at,
            "end_reason": reason,
            "duration_sec": duration_sec,
            "summary": {
                "turns": turns,
                "tool_calls": tool_calls,
                "errors": errors,
            },
            "events": self._session_log,
        }
        filepath = sessions_dir / f"{conversation_id}.json"
        tmp = filepath.with_suffix(".tmp")
        tmp.write_text(json.dumps(log, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(filepath)
        print(
            f"[session_manager] session_log_written path={filepath} "
            f"events={len(self._session_log)} turns={turns} tool_calls={tool_calls}"
        )
        self._session_log = []

    def _idle_countdown_sec(self) -> float | None:
        if not self.agent_deployed or self.session_state in ("warm", "idle"):
            return None
        if self.last_user_activity_monotonic <= 0:
            return float(SESSION_IDLE_TIMEOUT_SEC)
        remaining = SESSION_IDLE_TIMEOUT_SEC - (
            time.monotonic() - self.last_user_activity_monotonic
        )
        if remaining <= 0.0:
            return 0.0
        return math.ceil(remaining * 10.0) / 10.0

    async def monitor_loop(self) -> None:
        while True:
            now = time.monotonic()
            if self._bootstrap_complete:
                await self._refresh_participants_once()
            if self.agent_deployed and self.session_state == "active":
                if self.last_user_activity_monotonic > 0:
                    idle_for = now - self.last_user_activity_monotonic
                    if idle_for >= SESSION_IDLE_TIMEOUT_SEC:
                        await self.end_session(reason=f"no_user_activity_{idle_for:.1f}s")
                elif self.last_user_activity_monotonic == 0 and self.last_agent_activity_monotonic == 0:
                    if (
                        self.dispatch_started_monotonic > 0
                        and (now - self.dispatch_started_monotonic) >= SESSION_PREROLL_ACTIVITY_SEC
                    ):
                        await self.end_session(reason="no_activity_after_dispatch")
            if self.session_state == "warm" and self.agent_deployed:
                if (
                    self._warm_phase == "deploying"
                    and self.dispatch_started_monotonic > 0
                    and (now - self.dispatch_started_monotonic) >= WARM_AGENT_JOIN_TIMEOUT_SEC
                ):
                    print("[session_manager] warm agent never became ready - re-dispatching")
                    await self.end_session(reason="warm_agent_timeout", force_teardown=True)
            await asyncio.sleep(LIVEKIT_STATUS_POLL_INTERVAL_SEC)

    async def token_refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(self.TOKEN_REFRESH_INTERVAL_SEC)
            if not self._bootstrap_complete:
                continue
            try:
                await self.write_session_snapshot()
                print("[session_manager] token refresh: wrote new session snapshot")
            except Exception as exc:
                print(f"[session_manager] token refresh failed err={exc}")

    async def start(self) -> None:
        app = create_app(self)
        runner = aiohttp.web.AppRunner(app)
        await runner.setup()
        site = aiohttp.web.TCPSite(runner, SESSION_MANAGER_HOST, SESSION_MANAGER_PORT)
        await site.start()
        print(
            f"[session_manager] dashboard=http://{SESSION_MANAGER_HOST}:{SESSION_MANAGER_PORT} "
            f"room={self.room_name} agent_name={self.agent_name}"
        )
        self._bg_tasks.append(asyncio.create_task(self.bootstrap_loop()))
        self._bg_tasks.append(asyncio.create_task(self.monitor_loop()))
        self._bg_tasks.append(asyncio.create_task(self.probe_components_loop()))
        self._bg_tasks.append(asyncio.create_task(self.token_refresh_loop()))
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
