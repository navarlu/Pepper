# Connection Issue — Agent ↔ LiveKit WebRTC Instability

## Symptom

While running the voice agent on the GPU server (woska) and the LiveKit server +
bridge on the RPi, the WebRTC connection between the agent worker and the
LiveKit server periodically dies. The agent log shows a sequence like:

```
play_animation_failed animation=Hey_1 error=timed out
livekit::rtc_engine - received session close: "server request to leave" ConnectionTimeout Resume
livekit::rtc_engine - received session close: "signal client closed: \"stream closed\"" UnknownReason Resume
livekit::rtc_engine - resuming connection... attempt: 0
livekit::rtc_engine - resuming connection failed: connection error: wait_pc_connection timed out
[PERSIST] session.on(close) fired — treating as shutdown (room likely deleted)
```

The session-manager side then sees the agent participant disappear, the zombie
detector waits its 10s grace period, and redispatches a fresh agent into the
same room. Total user-visible gap from the first failed packet to a healthy
agent is **~30-60 seconds**.

## Root cause (best current understanding)

It is **not** a code regression. The pattern is:

1. A burst of network traffic between woska and the RPi (e.g. agent joining a
   room → ICE/DTLS negotiation, plus an animation HTTP call from the agent to
   the bridge) coincides with a moment of campus-link / SSH-tunnel
   instability.
2. The LiveKit server stops receiving keepalive packets from the agent within
   its `participant_inactive_timeout`, marks the participant gone, and emits
   `server request to leave / ConnectionTimeout`.
3. The livekit-rtc client tries `Resume` for ~30s, fails, the session closes.
4. Our `session.on("close")` handler treats this as shutdown, the persistent
   loop exits, the worker becomes free, and the zombie detector eventually
   redispatches a fresh agent into the same room.

The first concrete signal is almost always an animation HTTP call timing out
(`play_animation_failed ... error=timed out`), which is a symptom — the network
to the RPi is already strained when that happens.

## Mitigations to consider (none of these are bugs in our code)

- **Co-locate the LiveKit server with the agent.** Run LiveKit on woska so the
  agent ↔ LiveKit traffic stays on localhost. The RPi stays a participant
  (listener / monitor / user), which is much more tolerant of brief link
  hiccups than the agent's WebRTC publish path.
- **Tune LiveKit's keepalive / inactivity timeout** if the campus link has
  bursty latency above the default threshold (`participant_inactive_timeout`,
  RTC `keepalive_interval`).
- **Make the bridge animation endpoint return immediately** (see fix below) so
  a slow Pepper animation never blocks the agent's HTTP call long enough to
  starve the WebRTC heartbeat.

## Fix applied: bridge animation endpoint acks immediately

The bridge handler at `robot/src/bridge.py` `/animation/<name>` previously
called `fut.value()` to wait for the Pepper animation to complete before
responding. On Pepper's NAOqi runtime that can take seconds, during which the
agent's blocking `urlopen` against the bridge holds an asyncio worker thread
and burns the only HTTP socket the LiveKit signaling path shares.

Changed: the bridge now resolves and validates the animation, **dispatches the
mute → run → unmute sequence in a background thread**, and returns
`200 OK {"ok": True, "name": ..., "behavior": ..., "queued": true}`
immediately. The agent gets its tool-call result in milliseconds and the
WebRTC heartbeat is never starved by Pepper's animation latency.

What this does NOT change:
- Animations still play exactly as before — only the HTTP ack moves earlier.
- If the animation fails on Pepper (unknown behavior, NAOqi error), the agent
  will still see success because the ack is fire-and-forget. Failures show up
  in bridge stdout as `[animation] failed:` lines.
- Mute / unmute of `ALAudioPlayer` still wraps the playback inside the
  background thread, so TTS audio doesn't bleed into the animation sounds.

## How to verify the fix

After deploying the new bridge to Pepper:

1. Tail the agent tmux. Trigger a conversation with a tool call (e.g. "hi" to
   the local agent so it calls `play_animation(greeting)`).
2. You should see in the agent log: `play_animation_dispatched ... status=200`
   within ~50ms of `executing tool play_animation`. No more
   `play_animation_failed ... error=timed out` lines unless the bridge itself
   is unreachable.
3. The animation should still visibly play on Pepper.
4. Tail the bridge stdout. You should see `[animation] queued behavior=...`
   immediately and `[animation] running:` / `[animation] done` follow shortly
   after.

## Open follow-up

If the WebRTC drops continue happening even with the bridge fix, the next
thing to investigate is the woska ↔ ptak ↔ RPi link itself, or move LiveKit
onto woska as suggested above.

## Update (2026-04-10): Partially mitigated by agent split

The voice agent is now split into two workers:
- **`pepper-openai`** runs directly on the RPi (Docker) — co-located with LiveKit, no SSH tunnel needed for WebRTC media. This eliminates the connection instability for OpenAI mode entirely.
- **`pepper-local`** still runs on woska via SSH tunnel — the connection issues described above still apply to local mode.

The session manager dispatches to the correct agent by name based on the selected mode. See [gpu-setup.md](gpu-setup.md) for the full architecture.
