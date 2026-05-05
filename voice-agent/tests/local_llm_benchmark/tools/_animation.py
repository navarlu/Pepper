"""Animation-name resolution helpers shared by play_animation.

Kept separate from the tool file so tweaking the resolution rules
doesn't require reopening the tool body.
"""

from __future__ import annotations

import random

from tools._common import _VOICE_AGENT_DIR  # noqa: F401  (path side-effect)

from src.config import (  # noqa: E402
    ANIMATION_GROUPS,
    ANIMATION_TOOL_ALIASES,
    ANIMATION_TOOL_ALLOWED,
)

ALLOWED_ANIMATIONS = ("greeting", "bow", "explain", "happy", "thinking", "dont_know")


def _pick_from_group(group_name: str) -> str:
    variants = ANIMATION_GROUPS.get(group_name, [])
    return random.choice(variants) if variants else ""


def _normalize_animation_name(raw_name: str) -> str:
    clean = str(raw_name or "").strip()
    if not clean:
        return ""

    clean_lower = clean.lower().replace("-", "_").replace(" ", "_")
    clean_lower = "".join(ch for ch in clean_lower if ch.isalnum() or ch == "_")

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
