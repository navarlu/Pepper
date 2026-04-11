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

---

## Update (2026-04-11): Deep network diagnosis

Spent a session chasing this. Turned out the situation is **much more nuanced** than "the tunnel is bad" — sometimes the system runs completely stable for 10+ minutes and completes full conversations, other times it flaps within 15s of dispatch. Below is everything we pinned down so we don't have to re-derive it next time.

### Things we ruled OUT as the cause

1. **Room management / session-manager dispatch code.** The session-manager correctly creates rooms, dispatches warm agents by name, watches for readiness, redispatches on zombie. All the "redispatch loop" behavior is downstream of the agent never completing `ctx.connect()`, not a session-manager bug. See [services/src/session_manager/app.py](../../services/src/session_manager/app.py).

2. **Agent name mismatch.** Was briefly a red herring — the woska tmux was restarting the agent without `export PEPPER_AGENT_NAME=pepper-local`, so it registered as `"Pepper"` (the default from [voice-agent/src/config.py:63](../../voice-agent/src/config.py#L63)) and session-manager's dispatches were ignored. **Fixed** by changing the config default from `"Pepper"` → `"pepper-local"` so a bare `python -m voice-agent.src.agent dev` on woska Just Works. The RPi Docker container still overrides via compose env (`PEPPER_AGENT_NAME: pepper-openai` at [docker/docker-compose.yml:76](../../docker/docker-compose.yml#L76)), so this has no effect on OpenAI mode.

3. **`token-latest.json` location mismatch.** There are two of them (`data/token-latest.json` at repo root, `services/src/session_manager/data/token-latest.json` — the new canonical path per [docker-compose.yml:38](../../docker/docker-compose.yml#L38)). The voice-agent **never reads either** — warm-dispatched agents get their room token via the LiveKit job assignment RPC. The file is only consumed by services/robot/tests. Latent cleanup item, not a cause of this issue.

4. **WebRTC media being fundamentally broken through the tunnel.** Proven wrong by a direct measurement (see below).

### The actual network topology

| Host | Primary IP | Tailscale IP | Public IPv6 |
|---|---|---|---|
| RPi | `192.168.210.78` (LAN) | `100.111.97.63` / `fd7a:115c:a1e0::4d32:613f` | — (none) |
| woska | `192.168.84.4/20` (private LAN, CTU-internal) | — (not on tailnet) | `2001:718:2:1634:e273:e7ff:fe16:527e/64` |

Reverse SSH tunnel from RPi → woska (via `ptak.felk.cvut.cz` jump) carries:
```
-R 7880 (LiveKit signaling WS)
-R 7443 (LiveKit TURN-TLS)
-R 8787 (session-manager HTTP)
-R 5000 (bridge HTTP)
-R 8080, 50051 (weaviate)
```
NOT tunneled: `7881` (RTC TCP), `7882` (RTC UDP — SSH is TCP-only anyway).

From woska, `ping 147.32.87.248` (RPi's srflx external IP) and `ping6 2001:718:2:1634::1` (CTU IPv6 gateway) **both fail** with 100% loss. ICMP is blocked or there's no route. So on paper there is no direct network path.

### What LiveKit actually advertises as ICE candidates

From `docker compose logs livekit` at startup:

```
rtcconfig/ip.go:195  found external IP via STUN  localAddr=172.17.0.1:7882  externalIP=147.32.87.248
rtcconfig/ip.go:195  found external IP via STUN  localAddr=192.168.210.78:7882  externalIP=147.32.87.248
rtcconfig/ip.go:195  found external IP via STUN  localAddr=172.18.0.1:7882  externalIP=147.32.87.248
webrtc_config.go:99  no external IPs found, using node IP for NAT1To1Ips  ip=147.32.87.248
server.go:264        starting LiveKit server  nodeIP=147.32.87.248  rtc.portTCP=7881  rtc.portUDP=7882
```

And the per-participant candidate list (from `rtc/room.go:1259 participant active`):

```
[local][selected:1] udp4 host 147.32.87.248:7882          ← CTU public IP (symmetric-NAT srflx)
[local]             udp6 host [fd7a:115c:a1e0:...]:7882   ← RPi Tailscale ULA (unreachable from woska)
[local]             tcp4 host 147.32.87.248:7881
[local]             tcp6 host [fd7a:115c:a1e0:...]:7881
```

LiveKit's `rtc.use_external_ip: true` + STUN detection picked **the CTU public IP 147.32.87.248** as the node IP. That IP is behind a symmetric NAT — the mapping only exists transiently when the RPi has an outbound UDP to a specific remote — so inbound packets from woska can only reach the RPi briefly and only if LiveKit just happened to send something out to woska first. Classic symmetric-NAT WebRTC fragility.

### What we measured (proves media IS flowing, most of the time)

Ran a subscribe-only test **from woska** using the `listener` token, connecting via `ws://127.0.0.1:7880` (tunnel), reading `rtc.AudioStream(track)` and counting received frames for both the `user` and `agent-*` tracks.

**Result: 3000 audio frames received from each participant in 30 seconds. Zero drops, no state changes.** That's 100 fps per participant = 10 ms Opus frames flowing cleanly for the full test duration. So the subscribe path is genuinely working, not just a dead subscription over signaling.

Script used (for reproducing later — add to `voice-agent/tests/` if this recurs):

```python
# On woska: LIVEKIT_URL=ws://127.0.0.1:7880 python -m voice-agent.tests.<script>
# Reads data/token-latest.json, uses listener token, counts AudioStream frames.
# Hold for N seconds, print frame counts every 5s.
```

### Why it's intermittent (best current hypothesis)

The agent has TWO WebRTC PeerConnections — subscriber (LiveKit → agent, for user audio) and publisher (agent → LiveKit, for agent audio). They share a single UDP socket but select ICE candidate pairs independently.

- **When it works**: both PCs pick a candidate pair that benefits from a fresh NAT binding (usually udp4 host 147.32.87.248:7882) and successfully keepalive that binding for the duration of the session. Full conversations complete, sessions end naturally on inactivity (`end_session reason=no_user_activity_60.2s`). We observed one such session today lasting 13+ minutes with 4 turns and 3 tool calls.
- **When it flaps**: the NAT binding for the publisher PC's selected pair expires or the path asymmetry kicks in, LiveKit server doesn't receive publisher RTP within `participant_inactive_timeout` (~15s), sends `server request to leave: ConnectionTimeout`, and the flap loop begins. Agent log shows `Publisher pc state failed` specifically — subscriber PC is usually still alive.

The intermittency tracks with how "cold" the NAT state is at connect time. Fresh tunnel, fresh process → sometimes the ICE checks punch through cleanly and the binding sticks; sometimes they don't.

### Latent bugs found while diagnosing (unrelated, worth fixing)

1. **`host.docker.internal:7880` fails in session-manager container** at bootstrap on Linux Docker:
   ```
   session-manager | ensure_room failed err=Cannot connect to host host.docker.internal:7880 [Connect call failed ('172.17.0.1', 7880)]
   session-manager | bootstrap failed err=Cannot connect to host host.docker.internal:7880
   ```
   Config at [docker-compose.yml:33](../../docker/docker-compose.yml#L33). Fix: add `extra_hosts: ["host.docker.internal:host-gateway"]` to the session-manager service (and any other service using that URL). Session-manager currently recovers through a retry loop, so it's cosmetic but noisy.

2. **Stale room delete spam on startup**:
   ```
   session-manager | delete old room failed room=pepper-1775652872 err=TwirpError(could not find object)
   session-manager | delete old room failed room=pepper-1775898464 err=TwirpError(could not find object)
   ```
   session-manager persists room IDs in `services/src/session_manager/data/session-manager-state.json` and tries to delete them on next boot. LiveKit GCs empty rooms, so the delete 500s. Fix: `ListRooms` first, intersect with persisted set, only delete the intersection.

3. **`data/token-latest.json` at repo root is orphaned** — nothing reads it anymore (the canonical one is under `services/src/session_manager/data/`). Can delete. The standalone test at [voice-agent/tests/test_livekit_connection.py:39](../../voice-agent/tests/test_livekit_connection.py#L39) still reads from the old path — update to the new one or delete the test.

### Fix options, ranked

**Option A — Tailscale on woska (recommended long-term).** Install tailscale on woska, join the RPi's tailnet. LiveKit **already advertises the RPi's Tailscale IPv6 as an ICE candidate** (`fd7a:115c:a1e0::4d32:613f:7882`), so no code or LiveKit config changes needed — just set `LIVEKIT_URL=ws://100.111.97.63:7880` on woska so signaling goes over the tailnet too, and WireGuard handles NAT traversal cleanly. Kills the whole problem class.

- Kernel-mode install (needs sudo): `curl -fsSL https://tailscale.com/install.sh | sh && sudo systemctl enable --now tailscaled && sudo tailscale up`
- Userspace fallback (no sudo): `tailscaled --tun=userspace-networking --socks5-server=localhost:1055 --socket=/tmp/tailscaled.sock` — clunkier because the LiveKit client needs to use the SOCKS5 proxy.

**Option B — Force-relay via LiveKit's own TURN-TLS.** TURN-TLS on 7443 is already tunneled. Configure the woska agent to force `iceTransportPolicy: relay` so it ignores direct candidates and goes through TURN. Blocker: self-signed cert on `domain: 127.0.0.1` in [docker/livekit/livekit.yaml:10](../../docker/livekit/livekit.yaml#L10) — agent would need to skip cert verification or trust the local CA.

**Option C — Configure LiveKit with explicit `node_ip` override + tunnel port 7881.** Set `rtc.use_external_ip: false`, `rtc.node_ip: 127.0.0.1`, add `-R 7881:127.0.0.1:7881` to autossh. Breaks LAN clients (Pepper, dev-console on RPi LAN) because they'd also get `127.0.0.1` as a candidate. Would need per-client candidate policies, which LiveKit can do but is fiddly.

**Option D — Public TURN server (Twilio, LiveKit Cloud).** Works always but introduces a 3rd-party dependency and per-frame relay latency. Not worth it when Option A exists.

### Practical tips when this recurs

1. **Check if it's actually broken this time or just slow**: tail the woska agent tmux and session-manager logs simultaneously. If you see `Publisher pc state failed` or `wait_pc_connection timed out`, it's flapping. If you see `received job request` followed by `early_transcript speaker=Pepper`, it's working.

2. **Quickest recovery without a code change**: restart the woska python agent (Ctrl+C in tmux, re-run `python -m voice-agent.src.agent dev`). In several runs today, this fixed the flap immediately — cold ICE state often reshuffles into a working candidate pair. Not a permanent fix, but a 30-second unblock.

3. **Run the frame-count test to distinguish "network broken" from "just the publisher PC unhappy"**: if the listener test on woska receives frames from the agent, the subscribe path is fine and the problem is specifically the publisher PC.

4. **Check LiveKit's advertised candidates**: `docker compose -f docker/docker-compose.yml logs livekit 2>&1 | head -60` shows the `nodeIP` and STUN-detected external IP. If it picked something weird (e.g. after a DHCP change on the RPi), that explains a new regression.

5. **Useful commands reference**:
   ```bash
   # Current state of the woska agent
   ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska 'tmux capture-pane -t pepper-agent -p -S -80'

   # LiveKit startup / node IP detection
   docker compose -f docker/docker-compose.yml logs livekit 2>&1 | head -60

   # Session-manager dispatch / watchdog activity
   docker compose -f docker/docker-compose.yml logs --tail=60 session-manager

   # Push fresh token to woska (if running the standalone test)
   scp -J navarlu2@ptak.felk.cvut.cz \
     services/src/session_manager/data/token-latest.json \
     navarlu2@woska:/mnt/data_personal/navarlu2/work/Pepper/data/token-latest.json
   ```
