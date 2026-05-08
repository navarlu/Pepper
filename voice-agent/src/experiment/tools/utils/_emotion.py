"""Shared `Emotion` Literal used by every tool's `emotion` argument.

Despite the legacy name, these are gesture categories (silent body
language), not emotional valence. Each value maps to an
`ANIMATION_GROUPS` key in `voice-agent/src/live/config.py`; the
dispatcher picks a random variant from the group's animation list.
The vocabulary is curated to the `Stand/Gestures/*` and
`Stand/BodyTalk/*` subtrees so no embedded audio leaks during
Pepper's live TTS.

Centralising the alias keeps the per-tool files in sync — when the
allowed gesture set changes, only this file needs to be touched.
"""

from __future__ import annotations

from typing import Literal


Emotion = Literal[
    "greet", "bow", "goodbye", "affirm", "deny", "think", "explain",
    "emphasis", "whisper", "question", "calm", "offer", "address_user",
    "dont_know", "speak_neutral",
]
