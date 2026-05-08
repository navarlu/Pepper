"""Animation-name resolution + dispatch helpers used by Path A (the
inline-tag parser in animation_tag_parser.py) and Path B (the
`gesture` parameter on every tool).

Single source of truth for benchmark animation work:
  - `_normalize_animation_name`: alias / group / direct-key resolver
  - `trigger_animation`: enqueue a gesture; a single async worker
    drains the queue and dispatches one at a time, sleeping
    `_ANIM_DURATION_SEC` between calls so consecutive gestures don't
    overlap (overlapping bridge mutes can silence live TTS mid-reply).

Kept benchmark-local on purpose. Production has its own
`trigger_animation` in `voice-agent/src/tools.py`; we don't share state
with it so this prototype can move without touching production.
"""

from __future__ import annotations

import asyncio
import random

from tools.utils._common import _VOICE_AGENT_DIR  # noqa: F401  (path side-effect)

from src.live.bridge_client import post_animation  # noqa: E402
from src.live.config import (  # noqa: E402
    ANIMATION_GROUPS,
    ANIMATION_TOOL_ALIASES,
    ANIMATION_TOOL_ALLOWED,
)

# Reserved for benchmark-only overrides on top of the production alias
# map in `voice-agent/src/live/config.py`. Empty by default — the
# canonical group keys (greet, think, explain, …) match the prompt
# vocabulary directly, so no local rewriting is needed.
_LOCAL_GESTURE_ALIASES: dict[str, str] = {}


# One-shot startup log so we can confirm in the agent stdout that the
# new vocabulary actually loaded on woska after the file copy.
print(
    "[anim] config loaded groups={} aliases={} variants={}".format(
        len(ANIMATION_GROUPS),
        len(ANIMATION_TOOL_ALIASES),
        len(ANIMATION_TOOL_ALLOWED),
    )
)
print("[anim] groups: {}".format(sorted(ANIMATION_GROUPS.keys())))


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


# Estimated typical Pepper gesture runtime. The bridge HTTP POST returns
# 200 immediately (animation runs in a background thread on the robot),
# so we can't observe true completion from here — instead the worker
# sleeps this long before pulling the next gesture, keeping consecutive
# animations from overlapping.
_ANIM_DURATION_SEC = 2.5

# Bounded queue. If the LLM fires more gestures than the worker can
# drain (chained tool calls in one turn), we drop the newest rather than
# letting the queue grow unbounded — a gesture that plays 10 s after
# the relevant speech is over is worse than no gesture at all.
_QUEUE_MAX = 2

_anim_queue: asyncio.Queue[str] | None = None
_worker_task: asyncio.Task[None] | None = None


async def _animation_worker() -> None:
    """Single consumer that drains `_anim_queue` sequentially.

    For each item: POST to the bridge, log the result, sleep one
    estimated gesture duration before pulling the next item. Runs
    forever once started; survives individual dispatch failures.
    """
    assert _anim_queue is not None
    print("[anim] worker started")
    while True:
        resolved = await _anim_queue.get()
        try:
            status, body = await asyncio.to_thread(post_animation, resolved)
            print(
                f"[anim] dispatched variant={resolved} status={status} "
                f"qsize={_anim_queue.qsize()}/{_QUEUE_MAX}"
            )
        except Exception as exc:
            print(f"[anim] dispatch failed variant={resolved} err={exc!r}")
        finally:
            _anim_queue.task_done()
        # Hold the slot for the estimated runtime so the next gesture
        # starts after the current one is roughly done on the robot.
        await asyncio.sleep(_ANIM_DURATION_SEC)


def _ensure_worker() -> None:
    """Lazily create the queue and start the worker task. Must be
    called from inside a running event loop.
    """
    global _anim_queue, _worker_task
    if _anim_queue is None:
        _anim_queue = asyncio.Queue(maxsize=_QUEUE_MAX)
        print(f"[anim] queue created maxsize={_QUEUE_MAX} duration={_ANIM_DURATION_SEC}s")
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.create_task(_animation_worker(), name="anim-worker")


async def trigger_animation(name: str) -> bool:
    """Resolve `name` through the alias map and enqueue it for the
    sequential dispatch worker.

    Returns True if the gesture was queued, False if the name didn't
    resolve or the queue was full. Body language is cosmetic — never
    gate a reply on the return value.
    """
    print(f"[anim] trigger called name={name!r}")
    resolved = _normalize_animation_name(name)
    if not resolved:
        print(f"[anim] reject name={name!r} reason=unknown_or_unmapped")
        return False
    print(f"[anim] resolved name={name!r} -> variant={resolved}")

    _ensure_worker()
    assert _anim_queue is not None

    try:
        _anim_queue.put_nowait(resolved)
        print(
            f"[anim] enqueued variant={resolved} "
            f"qsize={_anim_queue.qsize()}/{_QUEUE_MAX}"
        )
        return True
    except asyncio.QueueFull:
        print(
            f"[anim] dropped variant={resolved} reason=queue_full(max={_QUEUE_MAX})"
        )
        return False
