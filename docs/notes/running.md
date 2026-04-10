# Running the Pepper System

## Architecture Overview

```
RPi (192.168.210.78)                    woska (GPU server via ptak)
├── livekit          (WebRTC, :7880)    ├── voice-agent "pepper-local" (tmux)
├── redis            (for livekit)      │   └── STT + LLM + TTS pipeline
├── session-manager  (:8787, operator)  └── vLLM (Qwen 2.5 7B, :8000)
├── voice-agent "pepper-openai" (Docker)
├── bridge           (:5000, Pepper)    Reverse SSH tunnel (RPi → woska):
├── audio-bridge     (LiveKit → TCP)      7880  → LiveKit WS
├── room-monitor     (room state)         7443  → LiveKit TURN (TLS)
├── safe-startup     (Pepper watchdog)    8787  → session-manager API
├── weaviate         (:8080, RAG)         5000  → bridge (animations)
├── user-client      (RPi mic, optional)  8080  → weaviate HTTP
└── reverse-tunnel   (SSH to woska)       50051 → weaviate gRPC
```

The **OpenAI mode** agent runs directly on the RPi (co-located with LiveKit for minimal latency). The **local mode** agent runs on woska (GPU server) and connects via SSH reverse tunnel. The session manager dispatches to the correct agent (`pepper-openai` or `pepper-local`) based on the selected mode.

---

## 1. Start RPi services

From the project root:

```bash
# Base services (includes pepper-openai voice agent):
docker compose -f docker/docker-compose.yml up -d

# With RPi microphone:
docker compose -f docker/docker-compose.yml --profile audio up -d

# With reverse tunnel to woska (required for local mode):
docker compose -f docker/docker-compose.yml --profile remote-agent up -d

# All profiles:
docker compose -f docker/docker-compose.yml --profile audio --profile remote-agent up -d
```

## 2. Start local voice agent on woska (optional, for local mode)

SSH into woska and start in tmux:

```bash
ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska

tmux attach -t pepper-agent 2>/dev/null || tmux new-session -s pepper-agent

# Inside tmux:
cd /mnt/data_personal/navarlu2/work/Pepper
source .venv3/bin/activate
export PEPPER_AGENT_NAME=pepper-local
export PEPPER_AGENT_MODE=local
python -m voice-agent.src.agent dev
```

To detach from tmux without stopping: `Ctrl+B` then `D`

## 3. Start vLLM on woska (if using local mode)

```bash
tmux attach -t LLM 2>/dev/null || tmux new-session -s LLM

cd /mnt/data_personal/navarlu2/work/Pepper
source .venv2/bin/activate
vllm serve Qwen/Qwen2.5-7B-Instruct \
  --host 127.0.0.1 --port 8000 \
  --enable-auto-tool-choice --tool-call-parser hermes \
  --max-model-len 8192
```

## 4. Deploy code changes to woska

```bash
# From project root on RPi:
tar czf /tmp/pepper-agent.tar.gz voice-agent/src/ voice-agent/models/piper/ dev-console/data/map/ requirements.txt .env

scp -o ProxyJump=navarlu2@ptak.felk.cvut.cz /tmp/pepper-agent.tar.gz navarlu2@woska:/tmp/pepper-agent.tar.gz

ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska 'cd /mnt/data_personal/navarlu2/work/Pepper && tar xzf /tmp/pepper-agent.tar.gz && rm /tmp/pepper-agent.tar.gz'
```

Then restart the agent on woska (Ctrl+C in tmux, then re-run the python command).

## 5. Restart a single RPi service

```bash
docker compose -f docker/docker-compose.yml restart bridge
docker compose -f docker/docker-compose.yml logs -f bridge
```

## 6. Check logs

```bash
# RPi services:
docker compose -f docker/docker-compose.yml logs --tail=30 session-manager
docker compose -f docker/docker-compose.yml logs --tail=30 voice-agent
docker compose -f docker/docker-compose.yml logs --tail=30 bridge

# Voice agent on woska:
ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska -t 'tmux attach -t pepper-agent'
```

## 7. Switch agent mode

Via operator panel at `http://192.168.210.78:8787`, or:

```bash
curl -X POST http://localhost:8787/api/control/agent-mode -H 'Content-Type: application/json' -d '{"mode":"openai"}'
curl -X POST http://localhost:8787/api/control/agent-mode -H 'Content-Type: application/json' -d '{"mode":"local"}'
```

The session manager will shut down the current agent and dispatch a warm agent of the new mode.

---

## Environment files

| File | Location | Purpose |
|------|----------|---------|
| `.env` | RPi project root | API keys (OPENAI, LIVEKIT) |
| `.env` | `woska:/mnt/data_personal/navarlu2/work/Pepper/.env` | Same keys + woska-specific URLs |
| `docker-compose.yml` | `docker/` | RPi service definitions |

## Key ports

| Port | Service | Where |
|------|---------|-------|
| 7880 | LiveKit WebRTC | RPi |
| 8787 | Session Manager / Operator UI | RPi |
| 5000 | Bridge (Pepper HTTP) | RPi |
| 8080 | Weaviate HTTP | RPi |
| 8000 | vLLM (Qwen 2.5 7B) | woska |
