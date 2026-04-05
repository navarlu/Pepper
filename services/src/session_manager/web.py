import asyncio
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp import web

from .views import CHAT_HTML, SESSIONS_HTML, STATUS_HTML


def _utc_now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class SessionManagerHttp:
    def __init__(self, manager: Any) -> None:
        self.manager = manager

    async def handle_status(self, request: web.Request) -> web.Response:
        del request
        payload = {
            "room_name": self.manager.room_name,
            "session_state": self.manager.session_state,
            "agent_deployed": self.manager.agent_deployed,
            "warm_phase": self.manager._warm_phase,
            "conversation_id": self.manager.conversation_id,
            "last_user_activity_at": self.manager.last_user_activity_at,
            "last_agent_activity_at": self.manager.last_agent_activity_at,
            "updated_at": self.manager.updated_at,
            "participants": self.manager.participants,
            "agent_name": self.manager.agent_name,
            "agent_mode": self.manager.agent_mode,
            "local_llm_healthy": self.manager.local_llm_healthy,
            "transcript_items": list(self.manager.transcript_items),
            "last_user_text": self.manager.last_user_text,
            "last_pepper_text": self.manager.last_pepper_text,
            "mic_level": self.manager.mic_level,
            "mic_muted": self.manager.mic_muted,
            "agent_speaking": self.manager.agent_speaking,
            "agent_audio_level": self.manager.agent_audio_level,
            "local_audio_volume": self.manager.local_audio_volume,
            "idle_countdown_sec": self.manager._idle_countdown_sec(),
            "watchdog": dict(self.manager.watchdog_status),
            "components": sorted(
                self.manager.components.values(),
                key=lambda item: item.get("name", ""),
            ),
        }
        return web.json_response(payload)

    async def handle_activity(self, request: web.Request) -> web.Response:
        data = await request.json()
        source = str(data.get("source") or "").strip().lower()
        if source not in {"user", "agent"}:
            return web.json_response({"ok": False, "error": "invalid source"}, status=400)
        level = data.get("level")
        try:
            level_value = float(level) if level is not None else None
        except Exception:
            level_value = None
        await self.manager.record_activity(source, level=level_value)
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
            kind = str(data.get("kind") or "message").strip()
            self.manager._append_transcript(speaker, text, kind=kind)
        elif event_type == "mic_level" and level_value is not None:
            self.manager.mic_level = max(0.0, min(1.0, level_value))
        elif event_type == "agent_level" and level_value is not None:
            self.manager.agent_audio_level = max(0.0, min(1.0, level_value))
        elif event_type == "agent_speaking":
            self.manager.agent_speaking = active
        elif event_type == "warm_ready":
            was_pending = self.manager._warm_phase == "pending"
            self.manager._warm_phase = "standby"
            self.manager._append_session_marker_once("Agent ready")
            asyncio.create_task(self.manager._notify_tablet_status("Ready"))
            # If activation was queued while deploying, or text is waiting, activate now
            if was_pending or self.manager.pending_user_texts:
                asyncio.create_task(self.manager._try_activate_warm())
        elif event_type == "tool_call":
            self.manager._append_session_log_event({
                "type": "tool_call",
                "tool": str(data.get("tool") or ""),
                "args": data.get("args"),
                "result": data.get("result"),
                "duration_ms": data.get("duration_ms"),
                "error": data.get("error"),
            })
        elif event_type == "pipeline_metric":
            self.manager._append_session_log_event({
                "type": "pipeline_metric",
                "stage": str(data.get("stage") or ""),
                "duration_ms": data.get("duration_ms"),
                "ttft_ms": data.get("ttft_ms"),
                "audio_duration_ms": data.get("audio_duration_ms"),
                "completion_tokens": data.get("completion_tokens"),
                "prompt_tokens": data.get("prompt_tokens"),
                "tokens_per_second": data.get("tokens_per_second"),
                "characters": data.get("characters"),
                "text": data.get("text"),
            })

        self.manager.updated_at = _utc_now_iso()
        return web.json_response({"ok": True})

    async def handle_watchdog_status(self, request: web.Request) -> web.Response:
        data = await request.json()
        summary = (
            " ".join(str(data.get("summary") or "").strip().split())
            or "watchdog update"
        )
        pepper_reachable = bool(data.get("pepper_reachable", False))
        safe_startup_running = bool(data.get("safe_startup_running", False))
        last_result = " ".join(str(data.get("last_result") or "").strip().split())
        healthy = bool(data.get("healthy", pepper_reachable or safe_startup_running))
        updated_at = _utc_now_iso()
        self.manager.watchdog_status = {
            "summary": summary,
            "pepper_reachable": pepper_reachable,
            "safe_startup_running": safe_startup_running,
            "last_result": last_result,
            "updated_at": updated_at,
        }
        detail_parts = [
            "Pepper reachable" if pepper_reachable else "Pepper offline",
            "safe startup running" if safe_startup_running else "safe startup idle",
        ]
        if last_result:
            detail_parts.append(last_result)
        self.manager._set_component_state(
            "safe-startup",
            state=summary,
            detail=" | ".join(detail_parts),
            healthy=healthy,
            source="service",
        )
        return web.json_response({"ok": True, "updated_at": updated_at})

    async def handle_component_status(self, request: web.Request) -> web.Response:
        data = await request.json()
        name = " ".join(str(data.get("name") or "").strip().split())
        state = " ".join(str(data.get("state") or "").strip().split())
        detail = " ".join(str(data.get("detail") or "").strip().split())
        healthy = bool(data.get("healthy", True))
        if not name or not state:
            return web.json_response(
                {"ok": False, "error": "name and state required"},
                status=400,
            )
        if name == "bridge":
            return web.json_response({"ok": True, "name": name, "ignored": True})
        self.manager._set_component_state(
            name,
            state=state,
            detail=detail,
            healthy=healthy,
            source="service",
        )
        return web.json_response({"ok": True, "name": name, "state": state})

    async def handle_mic_toggle(self, request: web.Request) -> web.Response:
        del request
        self.manager.mic_muted = not self.manager.mic_muted
        self.manager._persist_state()
        self.manager.updated_at = _utc_now_iso()
        return web.json_response({"ok": True, "mic_muted": self.manager.mic_muted})

    async def handle_text_send(self, request: web.Request) -> web.Response:
        data = await request.json()
        text = " ".join(str(data.get("text") or "").strip().split())
        if not text:
            return web.json_response(
                {"ok": False, "error": "text required"},
                status=400,
            )
        now = time.monotonic()
        self.manager.last_user_activity_monotonic = now
        self.manager.last_user_activity_at = _utc_now_iso()
        if self.manager.session_state == "warm":
            await self.manager._try_activate_warm()
        elif self.manager.session_state == "idle":
            await self.manager.dispatch_cold_agent()
        item = {"id": uuid.uuid4().hex[:10], "text": text}
        self.manager.pending_user_texts.append(item)
        self.manager.updated_at = _utc_now_iso()
        return web.json_response({"ok": True, "queued": item})

    async def handle_user_client_state(self, request: web.Request) -> web.Response:
        del request
        return web.json_response(
            {
                "mic_muted": self.manager.mic_muted,
                "agent_deployed": self.manager.agent_deployed,
                "session_state": self.manager.session_state,
                "pending_texts": list(self.manager.pending_user_texts),
            }
        )

    async def handle_user_client_ack(self, request: web.Request) -> web.Response:
        data = await request.json()
        ack_id = str(data.get("id") or "").strip()
        if ack_id:
            self.manager.pending_user_texts = [
                item
                for item in self.manager.pending_user_texts
                if item.get("id") != ack_id
            ]
        return web.json_response({"ok": True})

    async def handle_reset(self, request: web.Request) -> web.Response:
        del request
        await self.manager.end_session(reason="manual_reset")
        return web.json_response(
            {"ok": True, "session_state": self.manager.session_state}
        )

    async def handle_agent_mode(self, request: web.Request) -> web.Response:
        data = await request.json()
        new_mode = str(data.get("mode") or "").strip().lower()
        if new_mode not in ("openai", "local"):
            new_mode = "local" if self.manager.agent_mode == "openai" else "openai"
        old_mode = self.manager.agent_mode
        self.manager.agent_mode = new_mode
        self.manager._persist_state()
        if old_mode != new_mode:
            print(f"[session_manager] agent_mode changed {old_mode} -> {new_mode}")
            self.manager._append_session_marker(f"Agent mode: {new_mode}")
            await self.manager.end_session(reason=f"agent_mode_changed_to_{new_mode}")
        return web.json_response({"ok": True, "agent_mode": self.manager.agent_mode})

    async def handle_audio_volume(self, request: web.Request) -> web.Response:
        data = await request.json()
        try:
            volume = int(data.get("volume"))
        except Exception:
            return web.json_response(
                {"ok": False, "error": "volume must be an integer 0-100"},
                status=400,
            )
        if volume < 0 or volume > 100:
            return web.json_response(
                {"ok": False, "error": "volume must be between 0 and 100"},
                status=400,
            )
        self.manager.local_audio_volume = volume
        self.manager._persist_state()
        self.manager.updated_at = _utc_now_iso()
        ok, err = await self.manager._apply_bridge_audio_volume(force=True)
        if not ok:
            self.manager._bridge_audio_volume_synced = None
            return web.json_response(
                {
                    "ok": False,
                    "volume": self.manager.local_audio_volume,
                    "error": f"bridge update failed: {err}",
                },
                status=502,
            )
        return web.json_response({"ok": True, "volume": self.manager.local_audio_volume})

    async def handle_docker_containers(self, request: web.Request) -> web.Response:
        del request
        try:
            containers = await self.manager._list_docker_containers()
            return web.json_response({"ok": True, "containers": containers})
        except Exception as exc:
            return web.json_response(
                {
                    "ok": False,
                    "containers": [],
                    "error": f"docker unavailable: {exc}",
                },
                status=503,
            )

    async def handle_docker_logs(self, request: web.Request) -> web.Response:
        container_id = str(request.query.get("container") or "").strip()
        if not container_id:
            return web.json_response(
                {"ok": False, "error": "container query param required"},
                status=400,
            )
        try:
            logs = await self.manager._docker_get_text(
                f"/containers/{container_id}/logs",
                params={
                    "stdout": "1",
                    "stderr": "1",
                    "tail": str(self.manager.docker_log_tail_lines),
                    "timestamps": "1",
                },
            )
            return web.json_response({"ok": True, "logs": logs})
        except Exception as exc:
            return web.json_response(
                {"ok": False, "error": f"failed to fetch logs: {exc}"},
                status=503,
            )

    async def handle_root(self, request: web.Request) -> web.Response:
        del request
        return web.Response(text=CHAT_HTML, content_type="text/html")

    async def handle_debug_root(self, request: web.Request) -> web.Response:
        del request
        return web.Response(text=STATUS_HTML, content_type="text/html")

    async def handle_sessions_page(self, request: web.Request) -> web.Response:
        del request
        return web.Response(text=SESSIONS_HTML, content_type="text/html")

    async def handle_sessions_list(self, request: web.Request) -> web.Response:
        del request
        sessions_dir: Path = self.manager.session_file.parent / "sessions"
        if not sessions_dir.exists():
            return web.json_response({"sessions": []})
        files = sorted(sessions_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
        sessions = []
        for f in files[:100]:
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                sessions.append({
                    "conversation_id": data.get("conversation_id", ""),
                    "agent_mode": data.get("agent_mode", ""),
                    "started_at": data.get("started_at", ""),
                    "ended_at": data.get("ended_at", ""),
                    "end_reason": data.get("end_reason", ""),
                    "duration_sec": data.get("duration_sec", 0),
                    "summary": data.get("summary", {}),
                    "filename": f.name,
                })
            except Exception:
                continue
        return web.json_response({"sessions": sessions})

    async def handle_session_detail(self, request: web.Request) -> web.Response:
        filename = request.match_info["filename"]
        if not re.match(r"^[\w\-.]+$", filename):
            return web.json_response({"error": "invalid filename"}, status=400)
        filepath: Path = self.manager.session_file.parent / "sessions" / filename
        if not filepath.exists():
            return web.json_response({"error": "not found"}, status=404)
        data = json.loads(filepath.read_text(encoding="utf-8"))
        return web.json_response(data)

    async def handle_console_proxy(self, request: web.Request) -> web.Response:
        subpath = request.match_info.get("path", "")
        target = f"{self.manager.dev_console_url}/{subpath}"
        qs = request.query_string
        if qs:
            target = f"{target}?{qs}"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.request(
                    method=request.method,
                    url=target,
                    headers={
                        k: v
                        for k, v in request.headers.items()
                        if k.lower() not in ("host", "transfer-encoding")
                    },
                    data=await request.read() if request.can_read_body else None,
                ) as resp:
                    body = await resp.read()
                    headers = {
                        k: v
                        for k, v in resp.headers.items()
                        if k.lower()
                        not in (
                            "transfer-encoding",
                            "content-encoding",
                            "content-length",
                        )
                    }
                    if not subpath and resp.content_type and "html" in resp.content_type:
                        text = body.decode("utf-8", errors="replace")
                        text = text.replace(
                            "const API = '';",
                            "const API = '/console';",
                            1,
                        )
                        body = text.encode("utf-8")
                    return web.Response(body=body, status=resp.status, headers=headers)
        except Exception as exc:
            print(f"[session_manager] console proxy error: {exc}")
            return web.Response(text=f"Dev console unavailable: {exc}", status=502)


def create_app(manager: Any) -> web.Application:
    http = SessionManagerHttp(manager)
    app = web.Application()
    app.add_routes(
        [
            web.get("/", http.handle_root),
            web.get("/debug", http.handle_debug_root),
            web.get("/sessions", http.handle_sessions_page),
            web.get("/api/sessions", http.handle_sessions_list),
            web.get("/api/sessions/{filename}", http.handle_session_detail),
            web.get("/api/status", http.handle_status),
            web.post("/api/activity", http.handle_activity),
            web.post("/api/debug-event", http.handle_debug_event),
            web.post("/api/watchdog-status", http.handle_watchdog_status),
            web.post("/api/component-status", http.handle_component_status),
            web.post("/api/control/mic", http.handle_mic_toggle),
            web.post("/api/control/text", http.handle_text_send),
            web.post("/api/control/reset", http.handle_reset),
            web.post("/api/control/agent-mode", http.handle_agent_mode),
            web.post("/api/control/audio-volume", http.handle_audio_volume),
            web.get("/api/docker/containers", http.handle_docker_containers),
            web.get("/api/docker/logs", http.handle_docker_logs),
            web.get("/api/user-client/state", http.handle_user_client_state),
            web.post("/api/user-client/ack", http.handle_user_client_ack),
            web.route("*", "/console", http.handle_console_proxy),
            web.route("*", "/console/{path:.*}", http.handle_console_proxy),
        ]
    )
    return app
