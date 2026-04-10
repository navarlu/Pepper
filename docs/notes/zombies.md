# Zombie LiveKit Agents

## Problem

Agent participants (`kind=4`) remain listed in a LiveKit room even after their
backing process has crashed or been stopped.  Because LiveKit uses Redis for
room state, the stale entries survive LiveKit container restarts.

**Observed behaviour (2026-04-03):**

- Room `pepper-main` showed 3 zombie agents (`agent-AJ_*`) alongside the
  healthy `user` and `listener-python` participants.
- `RemoveParticipant` API returned **503 / "no response from servers"** because
  LiveKit sends the disconnect signal via the agent's own connection, which no
  longer exists.
- Restarting the LiveKit container alone did **not** help; Redis preserved the
  stale participant records.
- Restarting `voice-agent` + `livekit` together also did not help for the same
  reason.

## What worked (nuclear option)

```python
# Delete the entire room — forces all participants out
await client.room.delete_room(api.DeleteRoomRequest(room="pepper-main"))
```

Healthy clients (`user`, `listener-python`) reconnected on their own or after a
container restart.  The zombie agents did **not** reconnect because their
processes were already gone.

**Caveat:** `listener-python` exhausted its reconnect attempts during the
LiveKit downtime and had to be restarted manually:

```bash
docker compose -f docker/docker-compose.yml --env-file .env restart listener
```

## Root cause candidates

| # | Cause | Why it leads to zombies |
|---|-------|------------------------|
| 1 | **voice-agent crash / OOM without clean disconnect** | Agent process dies before sending a graceful leave. LiveKit waits for a disconnect that never comes; Redis keeps the participant. |
| 2 | **Multiple agent dispatches for the same room** | If the LiveKit Agents framework dispatches a new agent worker before the old one has fully left, both register. When the old one eventually dies it becomes a zombie. |
| 3 | **No heartbeat / participant timeout in dev mode** | LiveKit `--dev` flag may relax or disable participant timeouts, so stale entries linger indefinitely. |
| 4 | **Redis persistence across restarts** | Room state is stored in Redis with AOF. Restarting LiveKit re-reads the stale state. |

## How to fix (quick)

### Option A: Delete the room (recommended for dev)

```bash
uv run python -c "
from livekit import api
import asyncio

async def main():
    client = api.LiveKitAPI('http://localhost:7880', 'devkey', 'secret')
    await client.room.delete_room(api.DeleteRoomRequest(room='pepper-main'))
    print('Room deleted')
    await client.aclose()

asyncio.run(main())
"
```

Then restart any services that didn't auto-reconnect:

```bash
docker compose -f docker/docker-compose.yml --env-file .env restart listener
```

### Option B: Flush Redis and restart everything

```bash
docker compose -f docker/docker-compose.yml --env-file .env exec redis redis-cli FLUSHALL
docker compose -f docker/docker-compose.yml --env-file .env restart livekit voice-agent listener
```

## Prevention ideas

1. **Room rotation** — ✅ **Implemented.** Room names are now `pepper-<timestamp>`, created fresh on each session-manager startup. Old rooms are deleted. Zombies in old rooms expire via `empty_timeout`.

2. **`empty_timeout`** — ✅ **Implemented.** Rooms created with `empty_timeout=300s`.

3. **Graceful shutdown in voice-agent** — ✅ **Implemented.** Persistent warm agents handle `shutdown` signal via data channel, `stop_grace_period: 15s` in Docker.

4. **Agent dispatch guard** — ✅ **Implemented.** Session manager removes stale agent participants before dispatching, tracks `active_dispatch_id`.

5. **Periodic participant cleanup (zombie detector)** — ✅ **Implemented.** `monitor_loop()` runs every 2s, detects missing agent participants with 10s grace period, triggers `_redispatch_warm_agent()`.

6. **LiveKit participant timeout config** — Not investigated further; the above mitigations have resolved the zombie problem in practice.
