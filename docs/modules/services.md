# `services/src/` — The LiveKit mesh

Five Python processes that together form the runtime around the
voice-agent. They don't talk to each other directly — they
coordinate through two files and a handful of LiveKit data topics:

- **`services/data/token-latest.json`** — written by the orchestrator,
  consumed by everyone else. One JWT per role (`user`, `listener`,
  `monitor`, `debugCli`, `tablet`) plus room/ws metadata.
- **`services/data/state.json`** — runtime state the orchestrator
  actuates: `{agent_mode, mic_muted, dispatch_nonce}`. External
  writers (text_chat CLI, manual edits) change it; the orchestrator
  polls it every `STATE_POLL_SEC` and reacts.

On top of that, four LiveKit data topics:

| Topic | Writer | Readers |
|-------|--------|---------|
| `pepper.state` | orchestrator | tablet_server, user_client, text_chat |
| `pepper.control` | text_chat (`/reset`) | voice-agent |
| `pepper.text` | text_chat (text input) | voice-agent, tablet_server |
| `pepper.debug` | voice-agent (tool events, session resets) | tablet_server, text_chat |
| `session-control` | orchestrator (`shutdown`) | voice-agent |

All services are launched as scripts from
[docker-compose.yml](../../docker/docker-compose.yml):
`uv run python services/src/<name>.py`. The `services/src/` directory
is automatically on `sys.path[0]`, so modules use flat
`from config import …` / `from session import …` — no package form.

---

## Code layout

| File | Role |
|------|------|
| [config.py](../../services/src/config.py) | Every tunable (env-backed). Single source of truth for sample rates, identities, file paths, agent names, timing constants. |
| [session.py](../../services/src/session.py) | `SessionWatcher` — polls the token file, waits/notifies on rotation. `post_debug_event` — log helper. Shared by audio_bridge, tablet_server, user_client. |
| [orchestrator.py](../../services/src/orchestrator.py) | Room lifecycle + token minting + agent dispatch + state watcher + state broadcaster. The keystone. |
| [audio_bridge.py](../../services/src/audio_bridge.py) | Joins LiveKit as `listener`, forwards agent audio over TCP to the robot bridge. |
| [user_client.py](../../services/src/user_client.py) | Joins LiveKit as `user`, publishes the RPi microphone. Listens to `pepper.state` for soft mute. |
| [tablet_server.py](../../services/src/tablet_server.py) | Joins LiveKit as `tablet` (subscribe-only), renders chat HTML, POSTs `data:text/html` URLs to the bridge's `/tablet/url`. |
| [text_chat.py](../../services/src/text_chat.py) | Debug CLI. Joins as `debug-cli`, sends text over `pepper.text`, writes state.json for mode/mic/dispatch commands. |

---

## Orchestrator — [`orchestrator.py`](../../services/src/orchestrator.py)

The only long-lived writer of state. No HTTP server, no dashboard.

### Startup sequence (`Orchestrator.run`)

1. **Bootstrap** — retry `_ensure_room()` every 3 s until LiveKit is
   reachable. This also garbage-collects any `pepper-*` rooms left
   over from a previous run and clears stale agent dispatches.
2. **Write tokens** — mint one JWT per role (`user`, `listener`,
   `monitor`, `debugCli`, `tablet`, and a name-only `agent` record)
   and atomically write `token-latest.json`.
3. **Dispatch + confirm** — `create_dispatch` for the agent matching
   `state["agent_mode"]`, then poll `list_participants` until an
   agent-kind participant appears. On `JOIN_TIMEOUT` (45 s),
   force-remove anyone stale and retry exactly once. This is what
   prevents zombie agents from stacking up across rapid restarts.
4. **Broadcast** current state on `pepper.state` so late-joiners get
   a snapshot without polling.
5. **Run three forever-loops** in parallel:
   - `_token_refresh_loop` — rewrites `token-latest.json` every
     `TOKEN_REFRESH_SEC` (4 h). Tokens are minted with 30-day TTL so
     this is belt-and-suspenders.
   - `_state_watcher` — polls `state.json` every `STATE_POLL_SEC`
     (3 s) and actuates changes.
   - `_state_heartbeat` — re-broadcasts state every 10 s.

### State actuation

Three distinct state transitions, each with its own fixup path:

**Mode switch (`agent_mode` changed):** `_switch_mode(new_mode)` →
`_send_shutdown_signal` on `session-control` → poll until the agent
participant is gone (or `EXIT_TIMEOUT_SEC = 15 s`, then
force-remove) → update `self.agent_mode` + `self.agent_name` →
`_dispatch_and_confirm`. The *wait for gone* step is the whole
reason mode switching is reliable now.

**Mic toggle (`mic_muted` changed):** just update internal state and
broadcast on `pepper.state`. `user_client` picks it up and starts
zeroing outgoing audio frames.

**Manual re-dispatch (`dispatch_nonce` changed):** `_force_redispatch`
same flow as mode switch, but keeps the current mode. Used when the
voice-agent crashed or hot-reloaded and the orchestrator's view of
the room is stale. Triggered by `text_chat /dispatch`.

### Why the `pepper.state` broadcast

Anyone that joins the room mid-session doesn't know what mode
we're in or whether the mic is muted. Rather than requiring a
request/response dance, the orchestrator re-announces on every state
change plus a 10 s heartbeat. Every consumer (tablet_server,
user_client, text_chat) just listens.

---

## Session watcher — [`session.py`](../../services/src/session.py)

Dead-simple poll-file-for-changes abstraction around the token file.
One `SessionWatcher(role)` per consumer.

```
┌─────────────────┐        token-latest.json        ┌─────────────────┐
│  orchestrator   │ ──(write via tmp+rename)──────► │  SessionWatcher │
└─────────────────┘                                  │  (role=X)       │
                                                    └─────────────────┘
                                                       │
                                                       ├── wait_for_initial_token()  (startup)
                                                       ├── latest_token_info()       (one-shot)
                                                       └── watch(on_change)          (rotation)
```

`ws_url` resolution prefers `hostWsUrl` over `internalWsUrl` over
`wsUrl` — that's how services running *on the host* (user-client) get
a public URL while services inside the Docker network get the
internal one.

---

## Audio bridge — [`audio_bridge.py`](../../services/src/audio_bridge.py)

Bridges LiveKit audio into the robot bridge's TCP port. Two
independent concerns, both non-blocking:

**LiveKit side.** Connects as `listener-python`, subscribes to all
tracks, but only forwards audio from participants that look like the
agent. The filter (`_should_forward_audio`) checks:

1. Never forward from our own `LISTENER_IDENTITY`.
2. If `AGENT_TRACK_IDENTITY` is set → exact match required.
3. Otherwise: identity starting with `agent-` OR `kind` containing
   `AGENT` → forward.

When a new agent stream starts, `_cancel_existing_streams` kills any
earlier forwarding task to prevent two streams mixing into the same
TCP socket.

**TCP side.** On startup, try once to connect to the bridge. If
that fails, log once and keep running; every `BRIDGE_RETRY_SEC` (5 s)
try again. Every successful cycle sends a keepalive ping
(`CONTROL_FRAME_PING = 0xFFFFFFFF`) to detect dead sockets early.
When a stream ends we send a flush (`CONTROL_FRAME_FLUSH = 0`) so the
bridge drops any queued audio instead of playing it late.

Audio is attenuated (`PEPPER_STREAM_ATTENUATION`, default 0.4) via
`audioop.mul` before sending — Pepper's speakers are hot.

---

## User client — [`user_client.py`](../../services/src/user_client.py)

Captures the host microphone via `sounddevice` and publishes it as
the `user` participant. Runs **on the host**, not in Docker, because
it needs ALSA access.

The lifecycle is built around `_run_once`:

1. `SessionWatcher("user").wait_for_initial_token()` — block until
   the orchestrator has minted tokens.
2. `rtc.Room.connect()` with `auto_subscribe=False` (we don't want
   the agent's audio echoing back).
3. Create `rtc.AudioSource` → wrap in `LocalAudioTrack` → publish
   with `SOURCE_MICROPHONE`.
4. Run four coroutines in parallel:
   - `_audio_sender_loop` — drains the async queue and calls
     `source.capture_frame`.
   - `_control_loop` — no-op wait loop (kept so the task structure
     stays stable for future handlers).
   - `_room_monitor_loop` — checks for token rotation, disconnection,
     and stuck-in-reconnecting timeouts.
   - The main loop — just sleeps until `_reconnect_requested` is set.
5. Any reconnect trigger → teardown everything → top of `_run_once`.

### Soft mute

`pepper.state` broadcasts include `mic_muted`. When True,
`_audio_sender_loop` zeroes outgoing frame bytes and reports RMS 0.
The room participant stays, the track stays published, the agent's
`participant_identity` binding stays valid — only the audio content
is empty. This is what makes `/mic off` / `/mic on` instant.

### sounddevice ↔ asyncio glue

`InputStream` invokes its callback on a separate PortAudio thread.
The callback converts float32 → int16 PCM and pushes `(bytes,
samples, rms)` onto an `asyncio.Queue` via
`loop.call_soon_threadsafe`. Overflow policy: drop the oldest item,
never block the audio callback.

---

## Tablet server — [`tablet_server.py`](../../services/src/tablet_server.py)

Owns Pepper's tablet screen. Pepper's tablet browser can't reach the
RPi's LAN (it sits on an internal USB network), so we can't serve a
URL — every update is a fresh `data:text/html` data URL pushed
through the bridge's `/tablet/url` endpoint, which proxies to
`ALTabletService.showWebview`.

Inputs (all via LiveKit):

- `transcription_received` → user/Pepper speech → chat bubbles.
- `pepper.text` → text input from the debug CLI → user bubble.
- `pepper.debug` `kind=tool_call` → tool chip row (checkmark ✓ or ✗).
- `pepper.debug` `kind=session_reset` → clear history + divider.
- `pepper.state` → mode and mic pills in the header.

Output loop:

1. Any of the above calls `_dirty.set()` on the `asyncio.Event`.
2. `_render_loop` waits on `_dirty`, then sleeps
   `RENDER_DEBOUNCE_SEC` (300 ms) to coalesce bursts of partial
   transcripts into one render.
3. `_render_html()` rebuilds the whole DOM from
   `PAGE_TEMPLATE.format(...)`.
4. `_post_to_bridge` hashes the data URL; if it's unchanged from
   last post, skip — `showWebview` is not cheap on Pepper.

Chat history is bounded to `MAX_CHAT_ENTRIES = 40` — data URLs grow
linearly and the tablet browser slows down past ~30 KB of HTML.

---

## Text chat CLI — [`text_chat.py`](../../services/src/text_chat.py)

A terminal debugger that sits in the same LiveKit room as the agent.
Joins as `debug-cli` (subscribe-only), coexists with user-client
(which stays as `user`).

Two output streams into the room:

- **Text input** → `pepper.text` topic. The voice-agent feeds this
  into the session as if it came from the user, so the LLM never
  sees "debug-cli".
- **Control commands** → either `pepper.control` (`/reset`) or
  `state.json` (everything else: `/mode`, `/mic`, `/dispatch`). The
  orchestrator observes state.json and actuates.

| Command | What it does | Path |
|---------|--------------|------|
| `/help` | List commands | local |
| `/status` | Room snapshot: mode, participants, session age | local |
| `/mode <openai\|local>` | Switch agent mode | writes state.json |
| `/mic <on\|off>` | Soft mute user-client's mic | writes state.json |
| `/reset` | Clear agent's chat history | `pepper.control` |
| `/dispatch` | Force re-dispatch the current mode | writes state.json (dispatch_nonce) |
| `/quit` | Exit (also Ctrl-D) | local |

Room events (transcriptions, chat, tool calls from `pepper.debug`)
are printed inline as they arrive. The input loop uses
`run_in_executor(None, sys.stdin.readline)` so input isn't blocking
the event loop — it's a pragmatic choice, no readline integration,
async prints will appear on fresh lines above the prompt.

Full command doc: [text-chat-cli.md](../notes/text-chat-cli.md).

---

## Config knobs — [`config.py`](../../services/src/config.py)

Everything is env-backed; defaults work for a dev run on the RPi.
Most-touched:

| Name | Effect |
|------|--------|
| `LIVEKIT_URL` / `LIVEKIT_HOST_WS_URL` / `LIVEKIT_HTTP_URL` | WS for SDK, HTTP for server API. `HOST_WS_URL` is what clients running on the host see (via the SSH tunnel). |
| `LIVEKIT_SESSION_FILE` / `STATE_FILE` | Token file + runtime state file. Both under `services/data/` by default. |
| `PEPPER_AGENT_MODE` | Default mode at first boot. `state.json` takes over after that. |
| `PEPPER_AGENT_NAME_OPENAI` / `_LOCAL` | LiveKit agent names to dispatch per mode. |
| `STATE_POLL_SEC` | How often the orchestrator checks state.json. 3 s = snappy enough. |
| `TOKEN_REFRESH_SEC` | How often tokens are rewritten. 4 h = safe, tokens themselves live 30 d. |
| `PEPPER_STREAM_RATE` | PCM sample rate over the TCP link to the robot bridge. Validated against `ALLOWED_STREAM_RATES` in `config.py`. |
| `PEPPER_STREAM_ATTENUATION` | Volume multiplier applied to every outbound audio frame (Pepper's speakers are hot). |
| `AGENT_TRACK_IDENTITY` | Pin the audio-bridge to one specific identity. Empty = auto-detect agent-kind participants. |
| `USER_MIC_*` | Capture rate, block size, channels, RMS threshold, device. |
| `USER_CLIENT_TEST_MODE` | `publish` (default) / `connect-only` to skip mic publishing for debugging. |
| `BRIDGE_URL` | Where tablet_server POSTs the data URLs and audio-bridge resolves the TCP host:port from. |
| `TOKEN_POLL_INTERVAL` / `SESSION_ACTIVITY_DEBOUNCE_SEC` / `SESSION_IDLE_TIMEOUT_SEC` | Polling/debounce intervals shared by SessionWatcher + voice-agent. |

---

## Data flow summary

```
                       ┌───────────────────┐
                       │  orchestrator     │  writes:
                       │                   │    - token-latest.json
                       │                   │    - pepper.state (broadcast)
                       │                   │    - session-control (shutdown)
                       └─────────┬─────────┘
                                 │
       ┌─────────────────────────┼─────────────────────────┐
       │                         │                         │
       ▼                         ▼                         ▼
┌──────────────┐         ┌──────────────┐          ┌──────────────┐
│ audio_bridge │         │ user_client  │          │ tablet_server│
│  listener    │ ◀──────┤    user      │─────────▶│    tablet    │
│              │ tracks  │              │ pepper.  │              │
│              │         │              │ state    │              │
└──────┬───────┘         └──────┬───────┘          └──────┬───────┘
       │ TCP                    │ mic                     │ HTTP
       ▼                        ▼                         ▼
   robot bridge             LiveKit                   robot bridge
   (55555)                  (7880)                    (/tablet/url)
                                 ▲
                                 │ pepper.text / .control
                                 │ pepper.debug (reads)
                         ┌───────┴──────┐
                         │  text_chat   │
                         │  (debug-cli) │
                         └──────────────┘
                                 ▲
                                 │ tool events, transcripts
                         ┌───────┴──────┐
                         │  voice-agent │
                         │  (agent-*)   │
                         └──────────────┘
```
