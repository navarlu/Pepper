"""Paper dispatcher — keeps exactly one realtime agent dispatched into
the fixed experiment room.

Why this exists: the worker registers with an explicit `agent_name`
(same wiring as the cascade workers), which means LiveKit only sends
it a job when someone creates an *agent dispatch* for the room. The
cascade had a per-conversation launcher do that by hand; the paper MVP
wants `docker compose up` to be the only manual step, so this small
loop does it instead — and re-does it whenever the agent's job ends
(user-client restart, worker crash, session close), making the stack
self-healing.

Loop, every POLL_INTERVAL_SEC:
  1. List participants of ROOM_NAME. If an agent participant is
     present → nothing to do.
  2. No agent → delete any stale dispatches (a dispatch record can
     outlive its job and would otherwise block re-dispatch), create a
     fresh one, then wait POST_DISPATCH_GRACE_SEC so the worker has
     time to join before we re-check.

Run: uv run python voice-agent/src/paper/dispatcher.py
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from livekit import api
from livekit.protocol import models

# ── Path / env setup ─────────────────────────────────────────────────
ROOT_ENV_PATH = Path(__file__).resolve().parents[3] / ".env"
if ROOT_ENV_PATH.exists():
    load_dotenv(dotenv_path=ROOT_ENV_PATH, override=False)

LIVEKIT_HTTP_URL = os.environ.get("LIVEKIT_HTTP_URL", "http://127.0.0.1:7880")
ROOM_NAME = os.environ.get("EXPERIMENT_ROOM_NAME", "pepper-experiment")
AGENT_NAME = os.environ.get("PAPER_AGENT_NAME", "pepper-paper-realtime")
POLL_INTERVAL_SEC = 10.0
POST_DISPATCH_GRACE_SEC = 30.0

_KIND_AGENT = models.ParticipantInfo.Kind.Value("AGENT")


def _required_env(name: str) -> str:
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise RuntimeError(f"Missing required env var: {name}")
    return value


def _is_agent(p) -> bool:
    """Agent participants have kind=AGENT; the identity-prefix check is
    a fallback for older server/proto combinations."""
    if getattr(p, "kind", None) == _KIND_AGENT:
        return True
    return str(getattr(p, "identity", "")).startswith("agent-")


async def _ensure_dispatched(lkapi: api.LiveKitAPI) -> bool:
    """Returns True if a fresh dispatch was created this pass."""
    try:
        resp = await lkapi.room.list_participants(
            api.ListParticipantsRequest(room=ROOM_NAME)
        )
    except Exception as exc:
        # Room not created yet (orchestrator still booting) or LiveKit
        # unreachable — either way, just retry on the next tick.
        print(
            f"[paper-dispatcher] room {ROOM_NAME!r} not listable yet "
            f"({exc}) — waiting",
            flush=True,
        )
        return False

    agents = [p for p in resp.participants if _is_agent(p)]
    if agents:
        return False

    # No agent in the room: clear stale dispatch records, then create
    # a fresh dispatch for our worker.
    try:
        stale = await lkapi.agent_dispatch.list_dispatch(room_name=ROOM_NAME)
        for d in stale:
            print(
                f"[paper-dispatcher] deleting stale dispatch id={d.id} "
                f"agent={d.agent_name}",
                flush=True,
            )
            try:
                await lkapi.agent_dispatch.delete_dispatch(d.id, ROOM_NAME)
            except Exception as exc:
                print(
                    f"[paper-dispatcher] delete_dispatch id={d.id} "
                    f"failed err={exc}",
                    flush=True,
                )
    except Exception as exc:
        print(f"[paper-dispatcher] list_dispatch failed err={exc}", flush=True)

    try:
        dispatch = await lkapi.agent_dispatch.create_dispatch(
            api.CreateAgentDispatchRequest(
                agent_name=AGENT_NAME, room=ROOM_NAME,
            )
        )
        print(
            f"[paper-dispatcher] created dispatch id={dispatch.id} "
            f"agent={AGENT_NAME} room={ROOM_NAME}",
            flush=True,
        )
        return True
    except Exception as exc:
        # Typical transient: worker not registered with LiveKit yet.
        print(
            f"[paper-dispatcher] create_dispatch failed err={exc} — "
            "will retry",
            flush=True,
        )
        return False


async def main() -> None:
    api_key = _required_env("LIVEKIT_API_KEY")
    api_secret = _required_env("LIVEKIT_API_SECRET")
    print(
        f"[paper-dispatcher] started room={ROOM_NAME} agent={AGENT_NAME} "
        f"livekit={LIVEKIT_HTTP_URL} poll={POLL_INTERVAL_SEC}s",
        flush=True,
    )
    lkapi = api.LiveKitAPI(LIVEKIT_HTTP_URL, api_key, api_secret)
    try:
        while True:
            created = await _ensure_dispatched(lkapi)
            await asyncio.sleep(
                POST_DISPATCH_GRACE_SEC if created else POLL_INTERVAL_SEC
            )
    finally:
        await lkapi.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
