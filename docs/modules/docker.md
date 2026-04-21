# `docker/` — The compose topology

Everything the RPi runs lives in one compose file. All services share
the same runtime image ([Dockerfile.runtime](../../docker/Dockerfile.runtime))
and the project directory is bind-mounted in as `/workspace`, so code
changes are live on disk — restart the affected container, that's it.

> ⚠️ **The networking here is load-bearing.** `node_ip=127.0.0.1`,
> `use_ice_lite=true`, `network_mode: host` on most services, the full
> port list in the SSH tunnels — these were hard-won during the
> connection investigation ([connection-test-journal.md](../logs/connection-test-journal.md)).
> Do not change them without reading that file end-to-end first.

---

## Big picture

```
  docker compose up -d   (RPi, ARM64)
  ─────────────────────
       │
       ├── livekit          ws://127.0.0.1:7880, tcp://:7881  (bridge net, loopback-only ports)
       ├── redis            127.0.0.1:6379 (LiveKit state)
       ├── weaviate         :8080 HTTP, :50051 gRPC           (RAG — bridge net)
       │
       ├── orchestrator     host net — mints tokens, dispatches agent
       ├── voice-agent      host net — OpenAI mode worker ("pepper-openai")
       ├── audio-bridge     host net — LiveKit → TCP to Pepper
       ├── user-client      host net — RPi mic → LiveKit (ALSA passthrough)
       ├── tablet-server    host net — chat HTML → bridge /tablet/url
       │
       ├── bridge           host net — talks to Pepper via qi
       ├── safe-startup     host net — Pepper wake sidecar
       │
       ├── reverse-tunnel   autossh RPi → woska    (exposes RPi services to the GPU server)
       └── ssh-tunnel       autossh RPi :8000 ← woska :8000  (vLLM access)
```

Two processes run the LLM side of the system (only one at a time):

| Mode   | Agent name      | Runs on        | How it's launched |
|--------|-----------------|----------------|-------------------|
| OpenAI | `pepper-openai` | **RPi** (this compose, `voice-agent` service) | Automatic — `docker compose up -d` |
| Local  | `pepper-local`  | **woska** (GPU server) | Manual tmux — see [gpu-setup.md](../notes/gpu-setup.md) |

The orchestrator picks one via `state.json.agent_mode` and dispatches
by name — see [services.md](services.md) for the dispatch story.

---

## Files in `docker/`

| File | Role |
|------|------|
| [Dockerfile.runtime](../../docker/Dockerfile.runtime) | Single image used by every custom-build service: Python 3.12-slim + portaudio + libglib + `uv`. Installs from `requirements.txt`. |
| [docker-compose.yml](../../docker/docker-compose.yml) | The whole stack. One file, no profiles. |
| [livekit/livekit.yaml](../../docker/livekit/livekit.yaml) | LiveKit server config — the load-bearing networking bits. |
| [livekit/turn.crt](../../docker/livekit/turn.crt), [turn.key](../../docker/livekit/turn.key) | TURN certs. Not currently used (`turn.enabled: false`) but kept for future. |
| `.env` → `../.env` (symlink) | API keys, shared with the project root. |
| `weaviate_data/`, `redis-data/` | Bind-mounted state. |

`requirements.txt` is at the **project root**, not under `docker/`, but
it's what `Dockerfile.runtime` installs — so it effectively belongs to
the runtime image.

---

## Service-by-service

Every service below is a custom build of `Dockerfile.runtime` unless
noted. Every service has `restart: unless-stopped`.

### `livekit`

Image: `livekit/livekit-server:v1.10.1` (upstream, not custom-built).
**Bridge networking** (not host). Maps `127.0.0.1:7880` and
`127.0.0.1:7881` — so LiveKit is reachable only from loopback on the
RPi, and via the reverse tunnel from woska. Config comes from
[livekit.yaml](../../docker/livekit/livekit.yaml).

### `redis`

Image: `redis:7.4.2-alpine3.21`. Backs LiveKit state. Append-only
persistence in `./redis-data`.

### `orchestrator`

Runs `services/src/orchestrator.py`. Host networking so it can reach
`livekit` on `127.0.0.1:7880`. Bind-mounts the repo at `/workspace`.

**Env needed:** `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET` (currently
**not** loaded via `env_file` — see "Potential improvements" below).

### `voice-agent`

Runs `-m voice-agent.src.agent dev` — the LiveKit CLI with file
watching (`dev` enables `watchfiles` auto-reload). Host networking.
Loads `.env` via `env_file`.

This is the **OpenAI-mode** worker (`pepper-openai`). Local mode lives
on woska and is not in this compose file — orchestrator still
dispatches it by name, through the reverse tunnel.

`stop_grace_period: 15s` — gives the agent time to `ctx.shutdown()`
cleanly before compose sends SIGKILL.

### `bridge`

Runs `robot/src/bridge.py`. Host networking because the qi connection
to Pepper lives on a LAN IP. Three extra bind mounts provide the
self-built ARM64 qi library (see [bridge.md](bridge.md) and
[rpi-dev.md](../notes/rpi-dev.md)):

- `/opt/qi` ← host's built `libqi-python`
- `/opt/boost-lib` ← host's conan-built boost
- `/opt/qi-native-lib` ← host's libqi native libs

`PYTHONPATH` and `LD_LIBRARY_PATH` point the interpreter at them.

### `safe-startup`

Runs `robot/scripts/safe_startup_watchdog.py`. Same qi mounts as
`bridge`. Host networking (needs raw TCP to Pepper's 9559). See
[safe_startup.md](safe_startup.md).

### `audio-bridge`, `user-client`, `tablet-server`

All three run a Python file from `services/src/`. Host networking so
they reach `livekit` on loopback. `user-client` additionally needs
`/dev/snd:/dev/snd` and `group_add: [audio]` for ALSA. See
[services.md](services.md) for what each does.

### `weaviate`

Image: `cr.weaviate.io/semitechnologies/weaviate:1.35.1`. Bridge
networking, exposes `:8080` (HTTP) and `:50051` (gRPC). Uses the
`text2vec-openai` module so needs `OPENAI_APIKEY` from `.env`.

### `reverse-tunnel`

Alpine + autossh. Opens an SSH connection from RPi → `woska` (via
jump host `ptak.felk.cvut.cz`) and sets up **remote forwards** so
that RPi-side services appear on `localhost` at woska:

| Port | What it forwards |
|------|------------------|
| `7880` | LiveKit signaling (WS) |
| `7881` | LiveKit RTC (TCP) |
| `7443` | TURN TLS (reserved — TURN is currently disabled) |
| `5000` | Robot bridge HTTP |
| `8080` | Weaviate HTTP |
| `50051` | Weaviate gRPC |

With this tunnel up, the local-mode agent on woska can reach LiveKit,
the robot bridge, and Weaviate by just talking to `localhost` on
woska — exactly like RPi-side code does.

### `ssh-tunnel`

Same image, opposite direction: **local forward** `RPi:0.0.0.0:8000 →
woska:127.0.0.1:8000`. This is how the RPi can hit the remote vLLM
instance at woska:8000 as if it were local. Used by the
`voice-agent` local mode when you run it *on the RPi* instead of on
woska (not the primary path, but useful for debugging).

---

## The LiveKit config (the sacred bit)

[livekit.yaml](../../docker/livekit/livekit.yaml) is short on purpose
— every knob is load-bearing:

```yaml
port: 7880              # WS signaling
bind_addresses: [0.0.0.0]

rtc:
  tcp_port: 7881        # RTC over TCP — matches the SSH tunnel forward
  port_range_start: 0
  port_range_end: 0     # UDP disabled — no UDP over SSH, so TCP-only
  use_external_ip: false
  node_ip: "127.0.0.1"  # Only advertises the loopback candidate
  use_ice_lite: true    # Server never initiates connectivity checks

room:
  auto_create: true
  empty_timeout: 300

turn:
  enabled: false        # Not needed — loopback-only ICE works via the tunnel
```

Why each value matters:

- `node_ip: 127.0.0.1` — the only ICE candidate LiveKit advertises.
  Any client (RPi side, or woska via the reverse tunnel) sees
  "connect me at 127.0.0.1:7881" and that matches their local reality.
- `use_ice_lite: true` — server is passive in ICE, relies on the
  client to make the connection. Required when the only path is an
  already-established SSH tunnel.
- `tcp_port: 7881` + `port_range_start/end=0` — TCP-only, no UDP.
  UDP over SSH doesn't work well; TCP does.
- `bind_addresses: [0.0.0.0]` combined with the `127.0.0.1:7880`
  **port publish** in compose means LiveKit listens on all interfaces
  *inside* the container but only exposes loopback to the host. Hence
  the bridge networking for this service (not host).
- `turn.enabled: false` — TURN would add a UDP relay, which we don't
  want (no UDP path exists anyway).

The proof that this works end-to-end is in
[connection-test-journal.md](../logs/connection-test-journal.md).

---

## Secrets & env

All secrets live in `/home/lucas/Projects/FEL/Pepper/.env`
(symlinked from `docker/.env`). Compose services load them with
`env_file: ../.env`. Expected keys (see `.env.example`):

| Key | Used by |
|-----|---------|
| `OPENAI_API_KEY` | voice-agent (OpenAI Realtime), weaviate (embeddings) |
| `LIVEKIT_API_KEY` | orchestrator, voice-agent |
| `LIVEKIT_API_SECRET` | orchestrator, voice-agent |
| `LIVEKIT_KEYS` | livekit server (auth config) |

The compose file currently loads `.env` into **three** services:
`livekit`, `voice-agent`, `weaviate`. Others that need LiveKit
credentials (orchestrator) currently rely on a different mechanism —
see "Potential improvements" below.

---

## Running — the short story

All from the project root:

```bash
# Start everything
docker compose -f docker/docker-compose.yml up -d

# Rebuild the runtime image (after requirements.txt / Dockerfile changes)
docker compose -f docker/docker-compose.yml up -d --force-recreate --build

# Restart one service
docker compose -f docker/docker-compose.yml restart <service>

# Tail logs
docker compose -f docker/docker-compose.yml logs -f <service>
```

There are no compose profiles — every service starts by default.

---

## Potential improvements (not done — flagged for future)

These are things I noticed during the audit but **deliberately did
not touch** because the running setup is stable and the networking is
precious. Listed in order from "probably safe" to "needs judgment".

### 1. Stale doc references to `orchestrator_config.json`

[gpu-setup.md](../notes/gpu-setup.md) and
[local-llm-setup.md](../notes/local-llm-setup.md) still tell people
to edit `services/src/orchestrator_config.json` to change the agent
mode. That file was migrated to `services/data/state.json` during the
orchestrator refactor — the code still reads the old path for a
one-shot migration, but new writes must go to `state.json`.

**Fix:** text-only find/replace in those two docs. No runtime impact.

### 2. Server name drift: `woska` vs `lie`

[gpu-setup.md](../notes/gpu-setup.md) calls the GPU server `woska`.
[local-llm-setup.md](../notes/local-llm-setup.md) calls it `lie`.
Same box, different names in docs.

**Fix:** pick one and update the other doc. No code changes.

### 3. vLLM invocation drift

`gpu-setup.md` uses `--max-model-len 8192`. `local-llm-setup.md`
doesn't set it. Whichever is the actual working command on woska
should become the canonical one in both docs.

**Fix:** text-only, once you confirm which you're running.

### 4. `orchestrator` service is missing `env_file: ../.env`

The orchestrator calls `_required_env("LIVEKIT_API_KEY")` and
`_required_env("LIVEKIT_API_SECRET")` at startup, but its compose
block has no `env_file`. If the setup is working today, those vars
are arriving some other way (shell export before `docker compose up`,
a different wrapper, or — silently — it's crash-looping and you
haven't noticed because the other services are fine).

**Check:**
```bash
docker compose -f docker/docker-compose.yml logs orchestrator --tail=30
```

If you see normal `[orchestrator] ...` log lines, the env is getting
in somehow — leave it alone, but document how. If you see
`RuntimeError: Missing required env`, adding
```yaml
env_file: ../.env
```
under the `orchestrator:` block fixes it. The change is identical to
what `voice-agent` already does.

### 5. Inconsistent launch style

`voice-agent` runs via `-m voice-agent.src.agent dev` (module form,
with a hyphen in the package name that Python tolerates but is
unusual). Every other Python service runs as a plain script path
(`services/src/orchestrator.py`, etc.). The module form is required
for the LiveKit `dev` harness to work (file watching), so this is
cosmetic unless we want to unify the launch style — which would need
renaming `voice-agent/` to `voice_agent/` (breaking change across all
docs, the git history, and `scp` scripts).

**Recommendation:** leave as-is.

### 6. Dead port forward: `7443` in `reverse-tunnel`

`livekit.yaml` has `turn.enabled: false` and no other service listens
on 7443, yet the reverse tunnel forwards it. Purely cosmetic dead
weight — but removing it changes the SSH command, and the current one
is proven to work. Leaving it in costs nothing.

**Recommendation:** do not touch. Note the intent in a comment if
it's ever worth the risk.

### 7. woska-side has no scripts in the repo

Starting the local-mode agent and vLLM on woska is a manual tmux
dance. Could be formalized as `scripts/woska/start_vllm.sh` and
`scripts/woska/start_local_agent.sh` — small wrappers that encode the
exact commands from [gpu-setup.md](../notes/gpu-setup.md) so the
procedure is reproducible via `bash scripts/woska/start_vllm.sh`
rather than a paste-from-doc session.

**Value:** reproducibility + a single place to update when the vLLM
flags change. **Cost:** adds a new directory and convention. Worth
doing only when the procedure feels fragile in practice.

### 8. `requirements.txt` is flat and unverified

80+ pinned dependencies in a flat list. Some look potentially unused
(`beautifulsoup4`, `python-multipart`) but I didn't verify. An
occasional `uv pip compile --upgrade` + dead-code sweep would keep
the image lean. Not a correctness issue, just a hygiene one.

**Recommendation:** low priority; only worth doing if build time
starts hurting.
