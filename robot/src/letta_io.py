import os
import time
from typing import List, Dict, Any, Optional
import requests
import re

from dotenv import load_dotenv
from pathlib import Path

DEBUG_ENV = False
# Resolve project root (two levels up from this file)
ROOT_DIR = Path(__file__).resolve().parent.parent.parent
ENV_PATH = ROOT_DIR / ".env"

# Load .env from root folder
load_dotenv(dotenv_path=ENV_PATH)
if DEBUG_ENV: print(f"[ENV] Loaded .env from {ENV_PATH}")
if DEBUG_ENV: print(f"[ENV] OPENAI_API_KEY present: {'OPENAI_API_KEY' in os.environ}")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
CONVERSATION_BLOCK_ID = os.getenv("CONVERSATION_BLOCK_ID")
EGO_AGENT_ID = os.getenv("EGO_AGENT_ID")
if DEBUG_ENV: print(f"[ENV] Using CONVERSATION_BLOCK_ID={CONVERSATION_BLOCK_ID}")
if DEBUG_ENV: print(f"[ENV] Using EGO_AGENT_ID={EGO_AGENT_ID}")
MAX_RECENT_MESSAGES = int(os.getenv("MAX_RECENT_MESSAGES"))
if DEBUG_ENV: print(f"[ENV] Using MAX_RECENT_MESSAGES={MAX_RECENT_MESSAGES}")
GOAL_BLOCK_ID = os.getenv("GOAL_BLOCK_ID")
if DEBUG_ENV: print(f"[ENV] Using GOAL_BLOCK_ID={GOAL_BLOCK_ID}")
EMOTION_STATE_BLOCK_ID = os.getenv("EMOTION_STATE_BLOCK_ID")
if DEBUG_ENV: print(f"[ENV] Using EMOTION_STATE_BLOCK_ID={EMOTION_STATE_BLOCK_ID}")
LETTA_API_KEY = os.getenv("LETTA_API_KEY")
LETTA_BASE_URL = os.getenv("LETTA_BASE_URL")
if DEBUG_ENV: print(f"[ENV] Using LETTA_BASE_URL={LETTA_BASE_URL}")
if DEBUG_ENV: print(f"[ENV] Using LETTA_API_KEY={LETTA_API_KEY}") 
# ---- HTTP session ----
def _session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "Authorization": f"Bearer {LETTA_API_KEY}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    })
    return s

def _json_or_raise(r: requests.Response) -> Any:
    ctype = r.headers.get("content-type", "")
    if "application/json" not in ctype.lower():
        raise RuntimeError(f"Non-JSON response ({ctype}) {r.status_code}: {r.text[:300]}")
    return r.json()

def _normalize_role(msg: dict) -> str:
    """Map Letta message_type -> friendly role ('user'/'assistant'/'system'/'tool')."""
    mt = (msg.get("message_type") or "").lower()
    if mt == "assistant_message":
        return "assistant"
    if mt == "user_message":
        return "user"
    if mt == "system_message":
        return "system"
    # group tool-related under 'tool' (optional)
    if mt in {"tool_call_message", "tool_return_message"}:
        return "tool"
    # fall back to provided role if the server includes it
    return (msg.get("role") or "").lower() or mt or "unknown"

def _msg_text(msg: dict) -> str:
    """
    Safely extract text. Some servers return `content` as a string;
    others as a list of content parts ({"type":"text","text":"..."}).
    """
    content = msg.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text", ""))
        return "\n".join([p for p in parts if p])
    return ""

def send_to_ego(agent_id: str, user_text: str) -> str:
    """
    Send user text -> Ego, return the assistant reply string.
    Uses the Letta payload shape: messages: [{role, content: [{type:'text', text:...}]}]
    """
    sess = _session()

    payload = {
        "messages": [{
            "role": "user",
            "content": [{"type": "text", "text": user_text}]
        }],
        "max_output_tokens": 80  # or lower, like 60 for 2–3 sentences
    }
    resp = sess.post(f"{LETTA_BASE_URL}/agents/{agent_id}/messages", json=payload, timeout=60)
    if resp.status_code >= 400:
        raise RuntimeError(f"send_to_ego POST failed: {resp.status_code} {resp.text[:400]}")
    post_json = _json_or_raise(resp)

    # Try to read assistant directly from the POST response
    msgs = post_json.get("messages") if isinstance(post_json, dict) else None
    if isinstance(msgs, list):
        for m in reversed(msgs):
            if _normalize_role(m) == "assistant":
                return _msg_text(m)

    # Fallback: short poll the thread
    last_assistant: Optional[str] = None
    for _ in range(20):
        thread = sess.get(f"{LETTA_BASE_URL}/agents/{agent_id}/messages", params={"limit": 50}, timeout=30)
        if thread.status_code >= 400:
            raise RuntimeError(f"send_to_ego GET failed: {thread.status_code} {thread.text[:400]}")
        items = _json_or_raise(thread)
        # docs show this endpoint returns a JSON array
        if isinstance(items, list):
            for m in reversed(items):
                if _normalize_role(m) == "assistant":
                    last_assistant = _msg_text(m)
                    break
        if last_assistant:
            break
        time.sleep(0.2)

    return last_assistant or "(no response)"

import json
_HEARTBEAT_PATTERNS = [
    re.compile(r'(?:\\?")?,\s*(?:\\?")?request_heartbeat(?:\\?")?\s*:\s*false\}', re.IGNORECASE),
    re.compile(r'(?:\\?")?,\s*(?:\\?")?request_heartbeat(?:\\?")?\s*:\s*false', re.IGNORECASE),
    re.compile(r'(?:\\?")?request_heartbeat(?:\\?")?\s*:\s*false', re.IGNORECASE),
]


def sanitize_letta_text(text: str, *, preserve_whitespace: bool = False) -> str:
    """
    Remove Letta heartbeat metadata while optionally keeping leading spaces in streamed deltas.
    """
    if not isinstance(text, str):
        return ""

    cleaned = text
    for pat in _HEARTBEAT_PATTERNS:
        cleaned = pat.sub("", cleaned)

    if preserve_whitespace:
        # Keep tokens readable while trimming control characters that Letta adds to boundaries.
        return cleaned.strip("\r\n")
    return cleaned.strip()


def send_to_ego_sse_stream(
    agent_id: str,
    user_text: str,
    stream_tokens: bool = True,
    include_reasoning: bool = False,
    request_timeout: float = 60.0,
):
    """
    True streaming via Letta SSE endpoint. Yields assistant text pieces as they arrive.
    Works with both token streaming and step streaming.
    """
    sess = _session()

    url = f"{LETTA_BASE_URL}/agents/{agent_id}/messages/stream"
    payload = {
        "messages": [
            {
                "role": "user",
                "content": [{"type": "text", "text": user_text}],
            }
        ],
        "max_output_tokens": 80  # or lower, like 60 for 2–3 sentences
    }
    if stream_tokens:
        payload["stream_tokens"] = True
    if include_reasoning:
        payload["reasoning"] = True

    headers = dict(sess.headers)
    headers["Accept"] = "text/event-stream"
    headers["Cache-Control"] = "no-cache"

    def _extract_text_from_content(val):
        # Handles both string and list-of-parts content shapes
        if isinstance(val, str):
            return val
        if isinstance(val, list):
            parts = []
            for p in val:
                if isinstance(p, dict) and p.get("type") == "text":
                    parts.append(p.get("text", ""))
            return "".join(parts)
        return ""

    last_by_id = {}  # msg_id -> last_text_seen

    with sess.post(url, json=payload, headers=headers, stream=True, timeout=request_timeout) as r:
        r.raise_for_status()
        for raw in r.iter_lines(decode_unicode=True):
            if not raw or not raw.startswith("data:"):
                continue
            data = raw[5:].strip()
            if data == "[DONE]":
                break

            try:
                evt = json.loads(data)
            except Exception:
                continue

            msg_type = evt.get("message_type")
            if msg_type not in ("assistant_message",):
                # ignore reasoning/tool/usage/stop events for chat text
                continue

            msg_id = evt.get("id") or evt.get("message_id")
            # Prefer fine-grained deltas if present
            piece = evt.get("delta") or evt.get("text_delta")

            # Fallback: some providers send full 'content' each time
            if not piece and "content" in evt:
                # normalize content to string
                c = evt["content"]
                if isinstance(c, list):
                    c = "".join([p.get("text", "") for p in c if isinstance(p, dict) and p.get("type") == "text"])
                elif not isinstance(c, str):
                    c = ""
                # compute suffix vs last seen for this message id
                prev = last_by_id.get(msg_id, "")
                # emit only the new part
                piece = c[len(prev):] if c.startswith(prev) else c
                last_by_id[msg_id] = c  # update accumulator
            elif isinstance(piece, str):
                # if deltas are true granular deltas, accumulate too
                prev = last_by_id.get(msg_id, "")
                last_by_id[msg_id] = prev + piece

            if isinstance(piece, str):
                piece = sanitize_letta_text(piece, preserve_whitespace=True)
                if piece:
                    yield piece


def fetch_recent_conversation(agent_id: str, limit: int = 12) -> List[Dict[str, Any]]:
    """
    Returns [{role, content, raw}], newest last, normalized for your workers.
    """
    sess = _session()
    r = sess.get(f"{LETTA_BASE_URL}/agents/{agent_id}/messages", params={"limit": max(1, limit)}, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"fetch_recent_conversation failed: {r.status_code} {r.text[:400]}")
    data = _json_or_raise(r)
    result: List[Dict[str, Any]] = []
    if isinstance(data, list):
        for m in data[-limit:]:
            result.append({
                "role": _normalize_role(m),
                "content": _msg_text(m),
                "raw": m,
            })
    return result

# ---- Blocks (memory) ----
def read_block_text(block_id: str) -> str:
    sess = _session()
    r = sess.get(f"{LETTA_BASE_URL}/blocks/{block_id}", timeout=20)
    if r.status_code >= 400:
        raise RuntimeError(f"read_block_text failed: {r.status_code} {r.text[:400]}")
    j = _json_or_raise(r)
    return j.get("value", "") or ""

def overwrite_block_text(block_id: str, text: str) -> None:
    sess = _session()
    r = sess.patch(
        f"{LETTA_BASE_URL}/blocks/{block_id}",
        json={"value": text},
        timeout=30,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"overwrite_block_text failed: {r.status_code} {r.text[:400]}")

def append_block_text(block_id: str, text: str, max_chars: int = 20000) -> None:
    """
    Safe append implemented as read->concat->trim->patch, in case your server
    doesn't expose a dedicated 'append' endpoint.
    """
    current = ""
    try:
        current = read_block_text(block_id)
    except Exception:
        current = ""
    sep = "\n" if current and not current.endswith("\n") else ""
    new_val = f"{current}{sep}{text}"

    # Soft trim (keep last max_chars characters)
    if len(new_val) > max_chars:
        new_val = new_val[-max_chars:]

    overwrite_block_text(block_id, new_val)
