"""Thin HTTP clients for external services the voice-agent talks to.

Three destinations — all synchronous, all best-effort from the tools'
point of view (tools `asyncio.to_thread` them so the event loop is
never blocked):

  1. **Robot bridge** (`ANIMATION_BRIDGE_URL`, default :5000) — the
     Python HTTP server on the RPi exposed by `robot/src/bridge.py`.
     Used for animations, LED modes, and camera snapshots.
  2. **Vision describer** (`LOOK_AROUND_VISION_BASE_URL`, default :8001)
     — a secondary vLLM serving Qwen2.5-VL used only by the
     `look_around` tool to caption snapshots.

The bridge calls use `/animation/<name>`, `/leds/state`,
`/camera/snapshot`. The vision call is OpenAI-compatible
`/chat/completions` with an `image_url` content part.
"""

from __future__ import annotations

import base64
import json
import logging
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .config import (
    ANIMATION_BRIDGE_URL,
    ANIMATION_TOOL_HTTP_TIMEOUT_SEC,
    LOOK_AROUND_HTTP_TIMEOUT_SEC,
    LOOK_AROUND_VISION_BASE_URL,
    LOOK_AROUND_VISION_MAX_TOKENS,
    LOOK_AROUND_VISION_MODEL,
    LOOK_AROUND_VISION_PROMPT,
    LOOK_AROUND_VISION_TEMPERATURE,
    LOOK_AROUND_VISION_TIMEOUT_SEC,
)

logger = logging.getLogger("voice-agent")


def _bridge_base() -> str:
    """Return the bridge base URL without a trailing slash, or raise."""
    base = str(ANIMATION_BRIDGE_URL or "").rstrip("/")
    if not base:
        raise RuntimeError("animation_bridge_url_missing")
    return base


def post_animation(animation_name: str) -> tuple[int, str]:
    """POST `/animation/<name>` and return `(status_code, response_body)`.

    The bridge replies 200 immediately and runs the behavior in a
    background thread (see `docs/modules/bridge.md` — "Why
    /animation/<name> acks before running"). So this call should
    always be fast. Raises `RuntimeError` if the bridge is
    unreachable.
    """
    endpoint = f"{_bridge_base()}/animation/{quote(animation_name, safe='')}"
    req = Request(endpoint, data=b"", method="POST")
    try:
        with urlopen(req, timeout=float(ANIMATION_TOOL_HTTP_TIMEOUT_SEC)) as response:
            status = int(getattr(response, "status", response.getcode()))
            body = response.read().decode("utf-8", "ignore")
            return status, body
    except HTTPError as exc:
        body = exc.read().decode("utf-8", "ignore")
        return int(exc.code), body
    except URLError as exc:
        raise RuntimeError(f"animation_bridge_unreachable: {exc}") from exc


def post_led_state(mode: str) -> None:
    """Best-effort POST `/leds/state {"mode": ...}`.

    Never raises — logs at DEBUG on failure. LED state is purely
    cosmetic feedback (e.g. blue pulse while RAG is running) so a
    failure must never stop the tool call itself.
    """
    try:
        base = _bridge_base()
    except RuntimeError:
        return
    endpoint = f"{base}/leds/state"
    body = json.dumps({"mode": mode}).encode("utf-8")
    req = Request(endpoint, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=1.5) as response:
            _ = response.read()
    except Exception as exc:
        logger.debug("led_state_post_failed mode=%s error=%s", mode, exc)


def fetch_camera_snapshot() -> bytes:
    """POST `/camera/snapshot` and return the JPEG bytes.

    Pauses `ALBasicAwareness` on Pepper for the duration of the
    capture to avoid head-tracking motion blur. Raises on non-200 or
    non-`image/*` responses — callers treat that as "camera
    unavailable".
    """
    endpoint = f"{_bridge_base()}/camera/snapshot"
    body = b'{"pause_awareness": true}'
    req = Request(endpoint, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=float(LOOK_AROUND_HTTP_TIMEOUT_SEC)) as response:
        status = int(getattr(response, "status", response.getcode()))
        content_type = response.headers.get("Content-Type", "")
        data = response.read()
    if status != 200:
        raise RuntimeError(f"snapshot_http_{status}: {data[:200]!r}")
    if not content_type.startswith("image/"):
        raise RuntimeError(f"snapshot_bad_content_type: {content_type}")
    return data


def describe_image_with_vl(jpeg_bytes: bytes, extra_purpose: str = "") -> str:
    """Ask the side VL model to caption a JPEG and return plain text.

    Isolating vision reasoning from the main LLM lets the main chat
    model stay text-only — this avoids chat-template issues we hit
    with Qwen2.5-VL tool-calling. The call is OpenAI-compatible so
    any vLLM/Ollama/LM-Studio backend that speaks the right wire
    format can be swapped in via `LOOK_AROUND_VISION_BASE_URL`.

    `extra_purpose` is appended as a "Focus hint" so the LLM can
    steer the captioner (e.g. "read the sign", "count chairs").
    """
    base = str(LOOK_AROUND_VISION_BASE_URL or "").rstrip("/")
    if not base:
        raise RuntimeError("look_around_vision_base_url_missing")
    endpoint = f"{base}/chat/completions"

    data_url = "data:image/jpeg;base64," + base64.b64encode(jpeg_bytes).decode("ascii")

    user_text = LOOK_AROUND_VISION_PROMPT
    if extra_purpose:
        user_text = user_text + "\nFocus hint: " + extra_purpose

    payload = {
        "model": LOOK_AROUND_VISION_MODEL,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": data_url}},
                    {"type": "text", "text": user_text},
                ],
            }
        ],
        "max_tokens": int(LOOK_AROUND_VISION_MAX_TOKENS),
        "temperature": float(LOOK_AROUND_VISION_TEMPERATURE),
    }
    req = Request(endpoint, data=json.dumps(payload).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=float(LOOK_AROUND_VISION_TIMEOUT_SEC)) as response:
        raw = response.read()
    data = json.loads(raw)
    try:
        return str(data["choices"][0]["message"]["content"]).strip()
    except Exception as exc:
        raise RuntimeError(f"vl_unparsable_response: {str(data)[:300]}") from exc
