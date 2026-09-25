"""Tool surface for the realtime agent (`agent_realtime.py`) — 2 tools.

  - find_room.py     : Building E directions (curated table + FelSight
                       floor fallback) — slim wrapper over the same
                       helpers the experiment tool uses.
  - lookup_person.py : staff directory via the live UDB scrape, with
                       the EN→CZ surname-variant fanout.

Both are stripped of the cascade-only extras (`emotion` /
`request_heartbeat` args, gesture + filler side effects) — the
realtime model speaks for itself and has no embodiment hooks
in the tool layer. Tool-call observability still flows through
`tools.utils._events` so the worker can publish
`tool_call` / `tool_result` events exactly like the cascade agent.
"""
