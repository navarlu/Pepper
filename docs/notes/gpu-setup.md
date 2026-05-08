# Running Voice Agent on GPU Server (woska)

The **local mode** voice agent (Whisper STT + Qwen LLM + Piper TTS) runs on woska,
while the **OpenAI mode** agent runs directly on the RPi (Docker, co-located
with LiveKit for minimal latency). The `orchestrator` service dispatches to the
correct agent by name, based on the mode in [services/src/orchestrator_config.json](../../services/src/orchestrator_config.json).

## Architecture

```
RPi (192.168.210.78)                       woska (GPU server)
├── livekit (loopback :7880, :7881)        ├── voice-agent "pepper-local" (tmux: pepper-agent2)
│   └── node_ip=127.0.0.1, ICE/TCP only    │   └── connects via ws://localhost:7880
├── redis (LiveKit state)                  └── vLLM (Qwen 2.5 7B, :8000)
├── orchestrator (room + tokens + dispatch)
│   └── reads orchestrator_config.json     Reverse SSH tunnel (RPi → woska, via ptak):
│       and dispatches pepper-openai          7880 → LiveKit signaling (WS)
│       or pepper-local by mode               7881 → LiveKit RTC TCP
├── voice-agent "pepper-openai" (Docker)      5000 → bridge (animations)
├── bridge (:5000, Pepper QI)                 8080 → weaviate HTTP
├── audio-bridge (LiveKit → TCP to robot)     50051 → weaviate gRPC
├── weaviate (:8080, RAG)
├── user-client (RPi mic + speakers)
├── safe-startup (Pepper QI watchdog)
├── reverse-tunnel (autossh to woska)
└── ssh-tunnel (RPi:8000 → woska:8000 vLLM)
```

## Connection topology — TLDR

A single SSH tunnel through `ptak.felk.cvut.cz` carries both **signaling (7880)**
and **WebRTC media (7881 TCP)**. LiveKit is configured with `node_ip=127.0.0.1`
and `use_ice_lite=true` so it only advertises the loopback candidate — which
matches what woska sees on its end of the tunnel. **No UDP, no TURN, no
Tailscale.** This is the proven setup; see
[docs/logs/connection-test-journal.md](../logs/connection-test-journal.md) for
the full investigation.

## Agent naming

Each agent registers with LiveKit under a unique name so the orchestrator can
dispatch to the right one:

| Mode   | Agent name      | Runs on        | Env vars                                                                 |
|--------|-----------------|----------------|--------------------------------------------------------------------------|
| OpenAI | `pepper-openai` | RPi (Docker)   | `PEPPER_AGENT_NAME=pepper-openai PEPPER_AGENT_MODE=openai` (set in compose) |
| Local  | `pepper-local`  | woska (tmux)   | `PEPPER_AGENT_NAME=pepper-local PEPPER_AGENT_MODE=local` (set in tmux)   |

Both agents can be running simultaneously — the orchestrator only ever
dispatches one of them at a time, based on the configured mode.

## Key config files

| File | Purpose |
|------|---------|
| [docker/livekit/livekit.yaml](../../docker/livekit/livekit.yaml) | LiveKit server config: `node_ip=127.0.0.1`, `tcp_port=7881`, `use_ice_lite=true` |
| [docker/docker-compose.yml](../../docker/docker-compose.yml) | All RPi services, LiveKit on bridge network with loopback port maps |
| [services/src/orchestrator_config.json](../../services/src/orchestrator_config.json) | Current agent mode (`openai` or `local`) |
| `.env` | API keys (OPENAI_API_KEY, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, LIVEKIT_KEYS) |

## Setup steps

### 1. Start RPi services

```bash
docker compose -f docker/docker-compose.yml up -d
```

Both voice-agents (pepper-openai on RPi, and the spot for pepper-local on woska)
register with LiveKit. The orchestrator picks the right one based on mode.

### 2. Verify the reverse tunnel is up

The `reverse-tunnel` container auto-establishes the SSH tunnel. From woska:

```bash
ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska 'ss -tlnp | grep -E "7880|7881|5000|8080"'
```

Expected: all four ports listening on `127.0.0.1`.

### 3. Start voice agent on woska (local mode only)

```bash
ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska
tmux attach -t pepper-agent2 2>/dev/null || tmux new-session -s pepper-agent2

# Inside tmux:
cd /mnt/data_personal/navarlu2/work/Pepper
source .venv3/bin/activate
export PEPPER_AGENT_NAME=pepper-local
export PEPPER_AGENT_MODE=local
python -m voice-agent.src.live.agent dev
```

Detach without stopping: `Ctrl+B` then `D`.

### 4. Start vLLM on woska (local mode only)

```bash
tmux attach -t LLM 2>/dev/null || tmux new-session -s LLM

cd /mnt/data_personal/navarlu2/work/Pepper
source .venv2/bin/activate
vllm serve Qwen/Qwen2.5-7B-Instruct \
  --host 127.0.0.1 --port 8000 \
  --enable-auto-tool-choice --tool-call-parser hermes \
  --max-model-len 8192
```

### 5. Switch mode (no service restart needed)

The orchestrator polls [services/src/orchestrator_config.json](../../services/src/orchestrator_config.json)
every 3 seconds. Change the mode either from the chat CLI or directly:

```bash
# From the chat CLI:
uv run python services/src/live/text_chat.py
# Then: /mode openai  or  /mode local

# Or directly:
echo '{"agent_mode": "local"}' > services/src/orchestrator_config.json
```

The orchestrator handles the rest: shuts down the current agent, deletes the
room, creates a new room, dispatches a warm agent of the new mode, writes
fresh tokens to [services/data/token-latest.json](../../services/data/token-latest.json).

## Deploy code changes to woska

```bash
# Quick path (just the agent source files):
scp -J navarlu2@ptak.felk.cvut.cz \
  voice-agent/src/{agent.py,tools.py,config.py} \
  navarlu2@woska:/mnt/data_personal/navarlu2/work/Pepper/voice-agent/src/

# Then restart agent in tmux on woska:
ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska -t 'tmux attach -t pepper-agent2'
# Ctrl+C, then re-run: python -m voice-agent.src.live.agent dev
```

The RPi voice-agent (Docker) auto-reloads via watchfiles — no manual restart.

## Connection test

Run from the RPi to verify a fresh agent can join the room over the tunnel:

```bash
uv run python voice-agent/tests/test_livekit_connection.py
```

## Troubleshooting

**Agent shows `wait_pc_connection timed out` in the woska tmux:**
- Check tunnel is up: `ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska 'ss -tlnp | grep 7881'`
- Check LiveKit advertises `nodeIP=127.0.0.1`: `docker compose -f docker/docker-compose.yml logs livekit | head -30`
- Restart the tunnel: `docker compose -f docker/docker-compose.yml restart reverse-tunnel`

**Orchestrator says `agent not in room yet — will retry`:**
- The dispatched warm agent isn't joining. For openai mode, check
  `docker compose logs voice-agent`. For local mode, check the woska tmux
  agent is running and registered.

**Both agents seem to reply at once / multiple agent participants:**
- Stale dispatches accumulated across mode toggles. Restart the voice-agent:
  `docker compose -f docker/docker-compose.yml restart voice-agent`. See
  the "two agents" note in [text-chat-cli.md](text-chat-cli.md).
