"""Animation-name resolution + dispatch helpers used by Path A (the
inline-tag parser in animation_tag_parser.py) and Path B (the
`gesture` parameter on every tool).

Single source of truth for benchmark animation work:
  - `_normalize_animation_name`: alias / group / direct-key resolver
  - `trigger_animation`: rate-limited fire-and-forget POST to the bridge

Kept benchmark-local on purpose. Production has its own
`trigger_animation` in `voice-agent/src/tools.py`; we don't share state
with it so this prototype can move without touching production.
"""

from __future__ import annotations

import asyncio
import random
import time

from tools.utils._common import _VOICE_AGENT_DIR  # noqa: F401  (path side-effect)

from src.live.bridge_client import post_animation  # noqa: E402
from src.live.config import (  # noqa: E402
    ANIMATION_GROUPS,
    ANIMATION_TOOL_ALIASES,
    ANIMATION_TOOL_ALLOWED,
)

ALLOWED_ANIMATIONS = ("greeting", "bow", "explain", "happy", "thinking", "dont_know")

# Benchmark-local short forms that mirror the system-prompt vocabulary
# (greet/think/explain/bow/happy/dont_know). The production alias map
# already covers `greet` and the rest, but not `think` — so we add it
# here without touching `voice-agent/src/config.py`.
_LOCAL_GESTURE_ALIASES: dict[str, str] = {
    "think": "thinking",
}


def _pick_from_group(group_name: str) -> str:
    variants = ANIMATION_GROUPS.get(group_name, [])
    return random.choice(variants) if variants else ""


def _normalize_animation_name(raw_name: str) -> str:
    clean = str(raw_name or "").strip()
    if not clean:
        return ""

    clean_lower = clean.lower().replace("-", "_").replace(" ", "_")
    clean_lower = "".join(ch for ch in clean_lower if ch.isalnum() or ch == "_")

    # Benchmark-local short-form first (think → thinking) so the prompt
    # vocabulary resolves uniformly.
    clean_lower = _LOCAL_GESTURE_ALIASES.get(clean_lower, clean_lower)

    if clean_lower in ANIMATION_GROUPS:
        return _pick_from_group(clean_lower)

    mapped_group = ANIMATION_TOOL_ALIASES.get(clean_lower)
    if mapped_group and mapped_group in ANIMATION_GROUPS:
        return _pick_from_group(mapped_group)

    if clean in ANIMATION_TOOL_ALLOWED:
        return clean
    for key in ANIMATION_TOOL_ALLOWED:
        if key.lower() == clean.lower():
            return key
    return ""


# Min gap between animations. Set just below the typical Pepper gesture
# duration so think→explain transitions land cleanly while still
# protecting the bridge audio-mute lifecycle (each animation mutes
# ALAudioPlayer at start and restores in `finally` — overlapping
# animations can silence Pepper's live TTS mid-reply).
_ANIMATION_MIN_GAP_SEC = 1.5
_last_animation_at = 0.0


async def trigger_animation(name: str) -> bool:
    """Resolve `name` through the alias map and POST to the bridge.

    Fire-and-forget: errors are swallowed (logged to stdout), body
    language is cosmetic and must never gate a reply. Returns True
    when an animation was dispatched, False on resolve failure or
    rate-limit.
    """
    global _last_animation_at

    resolved = _normalize_animation_name(name)
    if not resolved:
        print(f"  [anim] unknown name={name!r} — no-op")
        return False

    now = time.monotonic()
    gap = now - _last_animation_at
    if gap < _ANIMATION_MIN_GAP_SEC:
        print(
            f"  [anim] rate-limited name={resolved} gap_ms={gap * 1000:.0f}"
        )
        return False
    _last_animation_at = now

    try:
        status, _body = await asyncio.to_thread(post_animation, resolved)
        print(f"  [anim] dispatched name={resolved} status={status}")
    except Exception as exc:
        print(f"  [anim] dispatch failed name={resolved} err={exc}")
        return False
    return True
