"""play_animation: trigger a Pepper body gesture via the bridge."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio

from livekit.agents import RunContext, function_tool

from tools._animation import ALLOWED_ANIMATIONS, _normalize_animation_name
from tools._common import _json, _run_main

from src.bridge_client import post_animation  # noqa: E402


_AnimationGroup = Literal[
    "greeting", "bow", "explain", "happy", "thinking", "dont_know",
]


@function_tool
async def play_animation(
    context: RunContext,
    name: _AnimationGroup,
) -> str:
    """Trigger a body gesture for the humanoid robot.

    Pick the group that matches the meaning of the assistant's reply:
      - greeting: hello/welcome
      - bow: thanks, goodbye
      - explain: factual/informational answer
      - happy: enthusiastic affirmation
      - thinking: clarifying question or expressing uncertainty
      - dont_know: apology or 'couldn't find'

    name: one of greeting, bow, explain, happy, thinking, dont_know.
    """
    del context
    animation_name = str(name or "").strip()
    print(f"  [tool] play_animation({animation_name!r})")
    resolved = _normalize_animation_name(animation_name)
    if not resolved:
        return _json({
            "error": "unknown_animation",
            "allowed": list(ALLOWED_ANIMATIONS),
        })

    try:
        status, body = await asyncio.to_thread(post_animation, resolved)
    except Exception as exc:
        print(f"  [tool] play_animation bridge unavailable: {exc}")
        return _json({"ok": True, "status": "queued", "animation": resolved})

    if 200 <= status < 300:
        return _json({"ok": True, "status": "queued", "animation": resolved})
    print(f"  [tool] play_animation bridge returned {status}: {body[:200]}")
    return _json({
        "ok": True,
        "status": "queued",
        "animation": resolved,
    })


if __name__ == "__main__":
    NAME = "greeting"
    _run_main(play_animation(None, name=NAME))
