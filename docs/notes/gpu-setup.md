# Running Voice Agent on GPU Server (woska)

The **local mode** voice agent (STT/LLM/TTS) runs on woska (GPU server), while the **OpenAI mode** agent runs directly on the RPi (co-located with LiveKit for minimal latency). The session manager dispatches to the correct agent based on the selected mode.

## Architecture

```
RPi (192.168.210.78)                    woska (GPU server)
├── livekit (host network, :7880)       ├── voice-agent "pepper-local" (tmux)
│   └── TURN server (:7443 TLS)        │   └── connects via ws://127.0.0.1:7880
├── redis (:6379)                       └── vLLM (Qwen 2.5 7B, :8000)
├── session-manager (:8787)
│   └── dispatches to pepper-openai     Reverse SSH tunnel (RPi → woska):
│       or pepper-local by mode           7880  → LiveKit WS + signaling
├── voice-agent "pepper-openai" (Docker)  7443  → LiveKit TURN (TLS relay)
├── bridge (:5000, Pepper control)        8787  → session-manager API
├── listener (audio forwarding)           5000  → bridge (animations)
├── weaviate (:8080, RAG)                 8080  → weaviate HTTP
├── user-client (RPi mic)                 50051 → weaviate gRPC
├── safe-startup (Pepper watchdog)
└── reverse-tunnel container
```

## Agent naming

Each agent registers with LiveKit under a unique name so the session manager can dispatch to the right one:

| Mode | Agent name | Runs on | Env vars |
|------|-----------|---------|----------|
| OpenAI | `pepper-openai` | RPi (Docker) | `PEPPER_AGENT_NAME=pepper-openai PEPPER_AGENT_MODE=openai` |
| Local | `pepper-local` | woska (tmux) | `PEPPER_AGENT_NAME=pepper-local PEPPER_AGENT_MODE=local` |

## Why TURN is needed

The SSH tunnel only forwards TCP. WebRTC needs UDP for media, which can't go through SSH. LiveKit's built-in TURN server relays media over TLS/TCP on port 7443, which IS forwarded through the tunnel. The TURN domain is set to `127.0.0.1` so the agent on woska reaches it via the tunnel.

## Key config files

| File | Purpose |
|------|---------|
| `docker/livekit/livekit.yaml` | LiveKit server config (TURN enabled, domain=127.0.0.1, tls_port=7443) |
| `docker/livekit/turn.crt` / `turn.key` | Self-signed TLS cert for TURN (CN=127.0.0.1) |
| `docker/docker-compose.yml` | All RPi services, LiveKit on host network |
| `.env` | API keys (OPENAI, LIVEKIT_KEYS) |

## Setup steps

### 1. Start RPi services

```bash
# From project root:
docker compose -f docker/docker-compose.yml --profile remote-agent --profile audio up -d

# The RPi voice-agent (pepper-openai) runs alongside the woska agent (pepper-local).
# No need to stop it — they have different agent names.
```

### 2. Verify tunnel is up

The `reverse-tunnel` container (profile: remote-agent) auto-establishes the SSH tunnel. Check on woska:

```bash
ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska 'ss -tlnp | grep -E "7880|7443|8787|5000|8080"'
```

Expected: all five ports listening on 127.0.0.1.

### 3. Start voice agent on woska

```bash
ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska

# Attach or create tmux session
tmux attach -t pepper-agent 2>/dev/null || tmux new-session -s pepper-agent

# Inside tmux:
cd /mnt/data_personal/navarlu2/work/Pepper
source .venv3/bin/activate
export PEPPER_AGENT_NAME=pepper-local
export PEPPER_AGENT_MODE=local
python -m voice-agent.src.agent dev
```

Detach without stopping: `Ctrl+B` then `D`

### 4. Start vLLM on woska (if not running)

```bash
tmux attach -t LLM 2>/dev/null || tmux new-session -s LLM

cd /mnt/data_personal/navarlu2/work/Pepper
source .venv2/bin/activate
vllm serve Qwen/Qwen2.5-7B-Instruct \
  --host 127.0.0.1 --port 8000 \
  --enable-auto-tool-choice --tool-call-parser hermes \
  --max-model-len 8192
```

### 5. Restart session-manager AFTER agent is registered

The agent must register before the session-manager dispatches. If the agent wasn't running when the session-manager started, restart it:

```bash
docker compose -f docker/docker-compose.yml restart session-manager
```

## Deploy code changes to woska

```bash
# From project root on RPi:
tar czf /tmp/pepper-agent.tar.gz voice-agent/src/ voice-agent/models/piper/ dev-console/data/map/ requirements.txt .env

scp -o ProxyJump=navarlu2@ptak.felk.cvut.cz /tmp/pepper-agent.tar.gz navarlu2@woska:/tmp/pepper-agent.tar.gz

ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska \
  'cd /mnt/data_personal/navarlu2/work/Pepper && tar xzf /tmp/pepper-agent.tar.gz && rm /tmp/pepper-agent.tar.gz'
```

Then Ctrl+C the agent in tmux and re-run `python -m voice-agent.src.agent dev`.

## Connection test

Run from woska to verify TURN relay works:

```bash
cd /mnt/data_personal/navarlu2/work/Pepper
.venv3/bin/python -m voice-agent.tests.test_livekit_connection
```

## Regenerating the TURN certificate

The self-signed cert expires after 10 years. To regenerate:

```bash
cd docker/livekit
openssl req -x509 -newkey rsa:2048 -keyout turn.key -out turn.crt \
  -days 3650 -nodes -subj '/CN=127.0.0.1' -addext 'subjectAltName=IP:127.0.0.1'
```

Then restart LiveKit: `docker compose -f docker/docker-compose.yml restart livekit`

## Troubleshooting

**Agent shows `wait_pc_connection timed out`:**
- Check tunnel is up: `ss -tlnp | grep 7443` on woska
- Check TURN cert exists: `docker/livekit/turn.crt`
- Verify LiveKit is on host network: `docker inspect docker-livekit-1 | grep NetworkMode`
- Restart tunnel: `docker compose -f docker/docker-compose.yml --profile remote-agent restart reverse-tunnel`

**Session-manager says `no worker is available`:**
- Agent wasn't registered when dispatch was sent
- Start the agent on woska first, then restart session-manager

**Both agents can run simultaneously:**
- `pepper-openai` (RPi Docker) and `pepper-local` (woska tmux) use different agent names
- The session manager dispatches to the correct one based on the current mode
- No need to stop one when the other is running
