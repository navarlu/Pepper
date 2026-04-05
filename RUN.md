# Running the Pepper System

## Architecture Overview

The system is split across two machines:

```
RPi (192.168.210.78)                    woska (GPU server via ptak)
├── livekit        (WebRTC, :7880)      ├── voice-agent  (STT + TTS + LiveKit agent)
├── redis          (for livekit)        └── vLLM         (Qwen 2.5 7B, :8000)
├── session-manager(:8787, operator UI)
├── bridge         (:5000, Pepper control)
├── listener       (audio forwarding)
├── safe-startup   (Pepper watchdog)
├── weaviate       (:8080, RAG vector DB)
├── user-client    (RPi mic, optional)
└── reverse-tunnel (connects woska → RPi)
```

The **reverse SSH tunnel** (RPi → woska) exposes RPi ports on woska's localhost,
so the voice agent on woska can reach LiveKit, session-manager, bridge, and weaviate.

---

## 1. Start RPi services

From the project root:

```bash
cd docker

# Base services (no voice-agent, it runs on woska):
docker compose up -d

# With RPi microphone:
docker compose --profile audio up -d

# With reverse tunnel to woska (required for remote voice-agent):
docker compose --profile remote-agent up -d

# All profiles:
docker compose --profile audio --profile remote-agent up -d
```

## 2. Start reverse SSH tunnel to woska

The voice agent on woska needs to reach RPi services (LiveKit, session-manager, bridge, weaviate).
Run this from the RPi (or use a dedicated tmux session):

```bash
ssh -J navarlu2@ptak.felk.cvut.cz -N \
  -R 7880:localhost:7880 \
  -R 8787:localhost:8787 \
  -R 5000:localhost:5000 \
  -R 8080:localhost:8080 \
  navarlu2@woska
```

## 3. Start voice agent on woska

SSH into woska and start in tmux:

```bash
ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska

# Attach or create tmux session
tmux attach -t pepper-agent 2>/dev/null || \
  tmux new-session -s pepper-agent

# Inside tmux:
cd /mnt/data_personal/navarlu2/work/Pepper
source .venv3/bin/activate
python -m voice-agent.src.agent dev
```

To detach from tmux without stopping: `Ctrl+B` then `D`

## 4. Start vLLM on woska (if not already running)

```bash
# In a separate tmux session on woska:
tmux attach -t LLM 2>/dev/null || tmux new-session -s LLM

cd /mnt/data_personal/navarlu2/work/Pepper
source .venv2/bin/activate
vllm serve Qwen/Qwen2.5-7B-Instruct \
  --host 127.0.0.1 --port 8000 \
  --enable-auto-tool-choice --tool-call-parser hermes \
  --max-model-len 8192
```

## 5. Deploy code changes to woska

After editing voice-agent code on the RPi:

```bash
# From project root on RPi:
tar czf /tmp/pepper-agent.tar.gz voice-agent/src/ voice-agent/models/piper/ dev-console/data/map/ requirements.txt .env

scp -o ProxyJump=navarlu2@ptak.felk.cvut.cz /tmp/pepper-agent.tar.gz navarlu2@woska:/tmp/pepper-agent.tar.gz

ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska 'cd /mnt/data_personal/navarlu2/work/Pepper && tar xzf /tmp/pepper-agent.tar.gz && rm /tmp/pepper-agent.tar.gz'
```

Then restart the agent on woska (Ctrl+C in tmux, then re-run the python command).

If requirements.txt changed, also run:

```bash
# On woska, in the pepper-agent tmux:
source .venv2/bin/activate
pip install -r requirements.txt
```

## 6. Restart a single RPi service

```bash
cd docker
docker compose restart bridge        # or: session-manager, listener, etc.
docker compose logs -f bridge        # follow logs
```

## 7. Check logs

```bash
# RPi services:
cd docker
docker compose logs --tail=30 session-manager
docker compose logs --tail=30 listener
docker compose logs --tail=30 bridge

# Voice agent on woska:
ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska 'tail -50 /tmp/pepper-agent.log'

# Or attach to tmux:
ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska -t 'tmux attach -t pepper-agent'
```

## 8. Fallback to local voice-agent (RPi only)

If woska is down or you want to run everything locally:

```bash
cd docker

# Start the local voice-agent + SSH tunnel for vLLM:
docker compose --profile local-llm up -d voice-agent ssh-tunnel

# Stop the reverse tunnel (not needed in local mode):
docker compose --profile remote-agent stop reverse-tunnel
```

## 9. Operator panel

Open in browser: `http://192.168.210.78:8787`

The panel is optional — the system works fully without it.
Pepper's tablet shows "Ready" / "Warming up..." status directly.

---

## Environment files

| File | Location | Purpose |
|------|----------|---------|
| `.env` | RPi project root | API keys (OPENAI, LIVEKIT) |
| `.env` | `woska:/mnt/data_personal/navarlu2/work/Pepper/.env` | Same keys + woska-specific URLs (tunnel localhost) |
| `docker-compose.yml` | `docker/` | RPi service definitions |

## Key ports

| Port | Service | Where |
|------|---------|-------|
| 7880 | LiveKit WebRTC | RPi |
| 8787 | Session Manager / Operator UI | RPi |
| 5000 | Bridge (Pepper HTTP) | RPi |
| 8080 | Weaviate HTTP | RPi |
| 8000 | vLLM (Qwen 2.5 7B) | woska |
