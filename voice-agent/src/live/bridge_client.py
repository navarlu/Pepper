"""Thin HTTP client for the robot bridge the voice-agent talks to.

All calls are synchronous and best-effort from the tools' point of
view (tools `asyncio.to_thread` them so the event loop is never
blocked). The bridge (`ANIMATION_BRIDGE_URL`, default :5000) is the
Python HTTP server on the RPi exposed by `robot/src/bridge.py`; it is
used for animations, head lock, LED modes, volume and tablet pages.
"""

from __future__ import annotations

import html as _html
import json
import logging
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

from .config import (
    ANIMATION_BRIDGE_URL,
    ANIMATION_TOOL_HTTP_TIMEOUT_SEC,
)

logger = logging.getLogger("voice-agent")


def _bridge_base() -> str:
    """Return the bridge base URL without a trailing slash, or raise."""
    base = str(ANIMATION_BRIDGE_URL or "").rstrip("/")
    if not base:
        raise RuntimeError("animation_bridge_url_missing")
    return base


def post_animation(animation_name: str, *, sound_off: bool = False) -> tuple[int, str]:
    """POST `/animation/<name>` and return `(status_code, response_body)`.

    The bridge replies 200 immediately and runs the behavior in a
    background thread, so this call should
    always be fast. Raises `RuntimeError` if the bridge is
    unreachable.

    `sound_off=True` appends `?sound=off` so the bridge mutes the
    behavior's embedded audio files before running it — required for
    Emotions/* and Reactions/* clips whose vocalisations bypass the
    master volume.
    """
    endpoint = f"{_bridge_base()}/animation/{quote(animation_name, safe='')}"
    if sound_off:
        endpoint += "?sound=off"
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


def post_volume_delta(delta: int) -> tuple[int, dict]:
    """POST `/audio/volume {"delta": <int>}` — step Pepper's speaker
    volume up or down. Returns `(status_code, response_json)`.

    The bridge clamps to 0..100, applies via `ALAudioDevice`, persists
    to the rpi-side `state.json`, and returns the previous + new
    volume. This is the single source of truth for volume changes —
    don't write the JSON file from here, since the agent and the
    bridge run on different machines.
    """
    endpoint = f"{_bridge_base()}/audio/volume"
    body = json.dumps({"delta": int(delta)}).encode("utf-8")
    req = Request(endpoint, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=float(ANIMATION_TOOL_HTTP_TIMEOUT_SEC)) as response:
            status = int(getattr(response, "status", response.getcode()))
            raw = response.read().decode("utf-8", "ignore")
    except HTTPError as exc:
        status = int(exc.code)
        raw = exc.read().decode("utf-8", "ignore")
    except URLError as exc:
        raise RuntimeError(f"audio_bridge_unreachable: {exc}") from exc
    try:
        data = json.loads(raw) if raw else {}
        if not isinstance(data, dict):
            data = {"raw": raw}
    except Exception:
        data = {"raw": raw}
    return status, data


def post_head_lock(
    lock: bool,
    yaw: float | None = None,
    pitch: float | None = None,
) -> None:
    """Best-effort POST `/motion/head_lock`.

    Pauses ALBasicAwareness + parks the head at (yaw, pitch) when
    `lock=True`; resumes awareness when `lock=False`. Used at
    experiment session start/end to stop Pepper's autonomous head
    scanning while a participant is interacting with her, then
    release it afterwards.

    Never raises — if the bridge is unreachable the head simply
    keeps doing whatever it was already doing, which is a safe
    fallback.
    """
    try:
        base = _bridge_base()
    except RuntimeError:
        return
    endpoint = f"{base}/motion/head_lock"
    body: dict[str, object] = {"lock": bool(lock)}
    if yaw is not None:
        body["yaw"] = float(yaw)
    if pitch is not None:
        body["pitch"] = float(pitch)
    req = Request(endpoint, data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=1.5) as response:
            _ = response.read()
    except Exception as exc:
        logger.debug("head_lock_post_failed lock=%s error=%s", lock, exc)


def post_led_state(mode: str) -> None:
    """Best-effort POST `/leds/state {"mode": ...}`.

    Never raises — logs at DEBUG on failure. LED state is purely
    cosmetic feedback (e.g. blue pulse while a tool is running) so a
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


_TABLET_INFO_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
html,body{{height:100%;background:#f7f8fa;color:#1b2430;
  font-family:-apple-system,"Segoe UI",Roboto,sans-serif;}}
body{{display:flex;align-items:center;justify-content:center;padding:32px;}}
.card{{
  max-width:92%;
  background:#ffffff;border:1px solid #e3e6eb;
  border-radius:24px;padding:48px 56px;
  box-shadow:0 8px 32px rgba(15,23,42,.08);
  text-align:center;
}}
.label{{font-size:18px;font-weight:700;letter-spacing:.18em;
  text-transform:uppercase;color:#0a7a2f;margin-bottom:18px;}}
.value{{font-size:60px;font-weight:600;color:#1b2430;
  line-height:1.25;word-break:break-word;white-space:pre-wrap;}}
</style></head>
<body><div class="card">
  <div class="label">Info</div>
  <div class="value">{value}</div>
</div></body></html>"""


_TABLET_BLANK_HTML = (
    '<!doctype html><html><head><meta charset="utf-8">'
    '<style>html,body{margin:0;height:100%;background:#f7f8fa;}</style>'
    '</head><body></body></html>'
)


def _post_tablet_url(html: str) -> tuple[int, str]:
    data_url = "data:text/html;charset=utf-8," + quote(html.encode("utf-8"))
    endpoint = f"{_bridge_base()}/tablet/url"
    body = json.dumps({"url": data_url}).encode("utf-8")
    req = Request(endpoint, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    try:
        with urlopen(req, timeout=float(ANIMATION_TOOL_HTTP_TIMEOUT_SEC)) as response:
            status = int(getattr(response, "status", response.getcode()))
            resp_body = response.read().decode("utf-8", "ignore")
            return status, resp_body
    except HTTPError as exc:
        resp_body = exc.read().decode("utf-8", "ignore")
        return int(exc.code), resp_body
    except URLError as exc:
        raise RuntimeError(f"tablet_bridge_unreachable: {exc}") from exc


def post_tablet_info(text: str) -> tuple[int, str]:
    """Render `text` as an info card on Pepper's tablet via `/tablet/url`.

    Used by the experiment-agent `display_info` tool to surface
    copy-worthy values (phone numbers, emails, room codes) in writing
    while Pepper speaks. A later call overwrites the page; the card
    is also cleared by `post_tablet_clear()` when the next user turn
    starts.

    Returns `(status_code, response_body)`. Raises `RuntimeError` if
    the bridge is unreachable.
    """
    safe = _html.escape(str(text or ""), quote=True)
    return _post_tablet_url(_TABLET_INFO_HTML.format(value=safe))


def post_tablet_clear() -> tuple[int, str]:
    """Blank the info card. Called by the experiment worker when a new
    user turn begins so a phone number / email shown for the previous
    turn doesn't linger on the screen indefinitely."""
    return _post_tablet_url(_TABLET_BLANK_HTML)


_TABLET_FAREWELL_HTML = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
*{{box-sizing:border-box;margin:0;padding:0;}}
html,body{{height:100%;background:#f7f8fa;color:#1b2430;
  font-family:-apple-system,"Segoe UI",Roboto,sans-serif;}}
body{{display:flex;align-items:center;justify-content:center;padding:18px;
  position:relative;}}
.card{{
  max-width:98%;
  background:#ffffff;border:1px solid #e3e6eb;
  border-radius:24px;padding:24px 32px 28px;
  box-shadow:0 8px 32px rgba(15,23,42,.08);
  text-align:center;display:flex;flex-direction:column;align-items:center;
  gap:12px;
}}
.label{{font-size:16px;font-weight:700;letter-spacing:.18em;
  text-transform:uppercase;color:#5e2bb0;}}
.qr{{width:560px;height:560px;display:flex;align-items:center;
  justify-content:center;background:#ffffff;}}
.qr img{{width:100%;height:100%;display:block;
  image-rendering:pixelated;image-rendering:-moz-crisp-edges;}}
.id{{font-size:52px;font-weight:800;color:#1b2430;letter-spacing:.04em;
  font-family:"SF Mono","Menlo",monospace;}}
.caption{{font-size:20px;color:#6b7280;font-weight:600;}}
.countdown{{position:absolute;top:18px;right:24px;
  padding:10px 20px;border-radius:999px;background:#eef1f5;
  color:#1b2430;font-size:20px;font-weight:800;border:1px solid #dfe4ea;
  letter-spacing:.05em;font-variant-numeric:tabular-nums;}}
</style></head>
<body>
<div class="countdown" id="cd">{remaining} s</div>
<div class="card">
  <div class="label">Thanks for chatting</div>
  <div class="qr"><img src="{qr_data_uri}" alt="QR"></div>
  <div class="id">{conv_id}</div>
  <div class="caption">Scan to give feedback</div>
</div>
<script>
(function(){{
  var r = {remaining};
  var el = document.getElementById('cd');
  var iv = setInterval(function(){{
    if (r > 0) {{
      r -= 1;
      el.textContent = r + ' s';
      if (r === 0) {{ el.style.display = 'none'; clearInterval(iv); }}
    }}
  }}, 1000);
}})();
</script>
</body></html>"""


def post_tablet_farewell(conv_id: str, qr_data_uri: str, remaining: int) -> tuple[int, str]:
    """Render the post-interaction farewell page (QR + participant ID
    + JS-driven countdown) on Pepper's tablet via `/tablet/url`.

    `qr_data_uri` should be a `data:image/png;base64,...` string
    produced by `segno.make(...).png_data_uri(...)`. PNG keeps the
    image rasterised on the NAOqi WebView, which renders inline SVG
    less reliably on the older WebKit it ships with.

    The page contains its own JS countdown timer — the caller posts
    once at the start with `remaining` set to the full duration and
    does not need to re-post each second.

    Returns `(status_code, response_body)`. Raises `RuntimeError` if
    the bridge is unreachable.
    """
    html = _TABLET_FAREWELL_HTML.format(
        qr_data_uri=qr_data_uri,
        conv_id=_html.escape(str(conv_id or ""), quote=True),
        remaining=int(remaining),
    )
    return _post_tablet_url(html)
