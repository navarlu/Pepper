"""Shared `Emotion` Literal used by every tool's `emotion` argument.

Centralising the alias keeps the per-tool files in sync — when the
allowed emotion set changes, only this file needs to be touched.
"""

from __future__ import annotations

from typing import Literal


Emotion = Literal[
    "greet", "think", "explain", "bow", "happy", "dont_know",
]
