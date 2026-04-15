# Running the Pepper System

## Architecture overview

```
RPi (192.168.210.78)                      woska (GPU server via ptak)
├── livekit          (WebRTC, :7880)      ├── voice-agent "pepper-local" (tmux: pepper-agent2)
├── redis            (LiveKit state)      │   └── STT + LLM + TTS pipeline (Whisper + vLLM + Piper)
├── orchestrator     (room + tokens)      └── vLLM (Qwen 2.5 7B, :8000)
├── voice-agent      "pepper-openai"
├── bridge           (:5000, Pepper QI)   Reverse SSH tunnel (RPi → woska):
├── audio-bridge     (LiveKit → TCP)        7880 → LiveKit WS
├── safe-startup     (Pepper watchdog)      7881 → LiveKit RTC TCP
├── weaviate         (:8080, RAG)           5000 → bridge (animations)
├── user-client      (RPi mic + HW out)     8080 → weaviate HTTP
├── reverse-tunnel   (SSH to woska)         50051 → weaviate gRPC
└── ssh-tunnel       (SSH to woska:8000 for vLLM)
```

The **OpenAI mode** agent runs directly on the RPi (Docker, co-located with LiveKit
for minimal latency). The **local mode** agent runs on woska (GPU server) and
connects via SSH reverse tunnel. The `orchestrator` service creates the LiveKit
room, writes participant tokens, and dispatches the warm voice-agent. Mode
switching is driven by a JSON config file: [services/src/orchestrator_config.json](../../services/src/orchestrator_config.json).

For the connection topology details and the journey to a stable WebRTC link,
see [docs/logs/connection-test-journal.md](../logs/connection-test-journal.md).

---

## 1. Start RPi services

From the project root:

```bash
# Start everything (every service runs by default — no profiles):
docker compose -f docker/docker-compose.yml up -d

# Rebuild and recreate all services:
docker compose -f docker/docker-compose.yml up -d --force-recreate --build
```

## 2. Start local voice agent on woska (only for `local` mode)

SSH into woska and start in tmux (the session is named `pepper-agent2`):

```bash
ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska
tmux attach -t pepper-agent2 2>/dev/null || tmux new-session -s pepper-agent2

# Inside tmux:
cd /mnt/data_personal/navarlu2/work/Pepper
source .venv3/bin/activate
export PEPPER_AGENT_NAME=pepper-local
export PEPPER_AGENT_MODE=local
python -m voice-agent.src.agent dev
```

Detach without stopping: `Ctrl+B` then `D`.

## 3. Start vLLM on woska (only for `local` mode)

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
# Quick path — just the agent source files:
scp -J navarlu2@ptak.felk.cvut.cz \
  voice-agent/src/{agent.py,tools.py,config.py} \
  navarlu2@woska:/mnt/data_personal/navarlu2/work/Pepper/voice-agent/src/

# Full bundle (includes Piper voices, .env, requirements):
tar czf /tmp/pepper-agent.tar.gz voice-agent/src/ voice-agent/models/piper/ requirements.txt .env
scp -o ProxyJump=navarlu2@ptak.felk.cvut.cz /tmp/pepper-agent.tar.gz navarlu2@woska:/tmp/pepper-agent.tar.gz
ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska 'cd /mnt/data_personal/navarlu2/work/Pepper && tar xzf /tmp/pepper-agent.tar.gz && rm /tmp/pepper-agent.tar.gz'
```

Then restart the agent on woska (Ctrl+C in the `pepper-agent2` tmux, then re-run).
The RPi voice-agent (Docker bind-mount) auto-reloads via watchfiles — no manual restart needed.

## 5. Restart a single RPi service

```bash
docker compose -f docker/docker-compose.yml restart bridge
docker compose -f docker/docker-compose.yml logs -f bridge
```

If you changed env vars in `docker-compose.yml`, use `up -d` (which recreates
containers when env changes are detected) instead of `restart`:

```bash
docker compose -f docker/docker-compose.yml up -d
```

## 6. Check logs

```bash
# RPi services:
docker compose -f docker/docker-compose.yml logs --tail=30 orchestrator
docker compose -f docker/docker-compose.yml logs --tail=30 voice-agent
docker compose -f docker/docker-compose.yml logs --tail=30 bridge

# Voice agent on woska (local mode):
ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska -t 'tmux attach -t pepper-agent2'
# Or just capture without attaching:
ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska 'tmux capture-pane -t pepper-agent2 -p -S -100'
```

## 7. Switch agent mode

Via the chat CLI (recommended — see [text-chat-cli.md](text-chat-cli.md)):

```
/mode openai
/mode local
```

Or directly edit the orchestrator config file:

```bash
echo '{"agent_mode": "openai"}' > services/src/orchestrator_config.json
```

The orchestrator polls this file every 3 seconds. On a mode change it:
1. Sends a shutdown signal to the current agent
2. Deletes the LiveKit room (room name changes)
3. Creates a new room and dispatches a warm agent of the new mode
4. Writes fresh tokens to [services/data/token-latest.json](../../services/data/token-latest.json)

## 8. Debug via text instead of voice

There's a CLI that joins the room as a `debug-cli` participant — you type, the
agent replies, tool calls are streamed inline. Full docs in
[text-chat-cli.md](text-chat-cli.md). Quick start:

```bash
uv run python services/src/text_chat.py
# /help, /status, /mic on|off, /mode openai|local, /reset, /quit
```

---

## Environment files

| File | Location | Purpose |
|------|----------|---------|
| `.env` | RPi project root | API keys (OPENAI, LIVEKIT) |
| `.env` | `woska:/mnt/data_personal/navarlu2/work/Pepper/.env` | Same keys + woska-specific URLs |
| `docker-compose.yml` | `docker/` | RPi service definitions (always run from project root with `-f docker/docker-compose.yml`) |
| `orchestrator_config.json` | `services/src/` | Current agent mode (`openai` or `local`) |

## Key ports

| Port | Service | Where |
|------|---------|-------|
| 7880 | LiveKit WebRTC signaling (WS) | RPi (loopback only, tunneled to woska) |
| 7881 | LiveKit RTC TCP | RPi (loopback only, tunneled to woska) |
| 5000 | Bridge (Pepper HTTP / animations) | RPi |
| 8080 | Weaviate HTTP | RPi |
| 8000 | vLLM (Qwen 2.5 7B) | woska |
