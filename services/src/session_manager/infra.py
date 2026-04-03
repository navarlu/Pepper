import asyncio
import json
import struct
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import aiohttp


async def docker_get_json(
    docker_socket_path: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    timeout_sec: float = 1.5,
) -> Any:
    if not docker_socket_path or not Path(docker_socket_path).exists():
        raise RuntimeError("docker socket unavailable")
    connector = aiohttp.UnixConnector(path=docker_socket_path)
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        async with session.get(f"http://docker{path}", params=params) as response:
            if response.status >= 400:
                raise RuntimeError(f"docker api {response.status}")
            return await response.json()


async def docker_get_text(
    docker_socket_path: str,
    path: str,
    *,
    params: dict[str, str] | None = None,
    timeout_sec: float = 2.5,
) -> str:
    if not docker_socket_path or not Path(docker_socket_path).exists():
        raise RuntimeError("docker socket unavailable")
    connector = aiohttp.UnixConnector(path=docker_socket_path)
    timeout = aiohttp.ClientTimeout(total=timeout_sec)
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        async with session.get(f"http://docker{path}", params=params) as response:
            if response.status >= 400:
                raise RuntimeError(f"docker api {response.status}")
            payload = await response.read()
            return decode_docker_log_payload(payload)


def decode_docker_log_payload(payload: bytes) -> str:
    if not payload:
        return ""
    chunks: list[bytes] = []
    idx = 0
    size = len(payload)
    while idx + 8 <= size:
        stream_type = payload[idx]
        if stream_type not in (0, 1, 2, 3):
            chunks = [payload]
            break
        frame_len = struct.unpack(">I", payload[idx + 4 : idx + 8])[0]
        frame_start = idx + 8
        frame_end = frame_start + frame_len
        if frame_end > size:
            chunks = [payload]
            break
        chunks.append(payload[frame_start:frame_end])
        idx = frame_end
    if not chunks:
        chunks = [payload]
    if idx < size and chunks != [payload]:
        chunks.append(payload[idx:])
    return b"".join(chunks).decode("utf-8", errors="replace")


async def list_docker_containers(
    docker_socket_path: str,
    known_services: tuple[str, ...],
) -> list[dict[str, str]]:
    raw_items = await docker_get_json(
        docker_socket_path,
        "/containers/json",
        params={"all": "1"},
    )
    items: list[dict[str, str]] = []
    for raw in raw_items or []:
        labels = raw.get("Labels") or {}
        service = str(labels.get("com.docker.compose.service") or "").strip()
        if service not in known_services:
            continue
        names = raw.get("Names") or []
        container_name = str(names[0] or "").lstrip("/") if names else service
        items.append(
            {
                "id": str(raw.get("Id") or ""),
                "service": service,
                "name": container_name,
                "state": str(raw.get("State") or ""),
                "status": str(raw.get("Status") or ""),
            }
        )
    return sorted(items, key=lambda item: (item["service"], item["name"]))


async def probe_tcp(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        conn = asyncio.open_connection(host, port)
        _reader, writer = await asyncio.wait_for(conn, timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


async def probe_http_health(raw_url: str, timeout: float = 1.0) -> bool:
    health_url = raw_url.rstrip("/") + "/health"
    req = Request(health_url, method="GET")
    try:
        await asyncio.to_thread(lambda: urlopen(req, timeout=timeout).read())
        return True
    except Exception:
        return False


async def probe_local_llm(local_llm_base_url: str, timeout: float = 2.0) -> bool:
    models_url = local_llm_base_url.rstrip("/") + "/models"
    req = Request(models_url, method="GET")
    try:
        body = await asyncio.to_thread(lambda: urlopen(req, timeout=timeout).read())
        data = json.loads(body)
        return bool(data.get("data"))
    except Exception:
        return False


def host_port_from_url(raw_url: str, default_port: int) -> tuple[str, int]:
    parsed = urlparse(raw_url)
    host = parsed.hostname or "127.0.0.1"
    port = int(parsed.port or default_port)
    return host, port
