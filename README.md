# Pepper — LLM-Driven Receptionist Robot

![Pepper](docs/assets/pepper.jpeg)

**Design of an LLM-Driven Receptionist Robot for Social Interaction**

Master's thesis project at CTU FEE Prague. Pepper greets visitors, holds
fluent spoken dialogue, answers questions about the faculty using
retrieval-augmented generation over internal documents, and accompanies its
speech with gestures and animations.

| | |
|---|---|
| **Author** | Bc. Lukas Navara |
| **Supervisor** | doc. Mgr. Matej Hoffmann, Ph.D. |
| **Department** | Dept. of Cybernetics, FEL CVUT |
| **Platform** | Raspberry Pi 5 + SoftBank Pepper |

---

## What it does

A visitor walks up to Pepper. Pepper:

1. **Listens** — captures audio over the RPi microphone (or any LiveKit
   client), streams it to a voice agent.
2. **Understands** — speech-to-text (Whisper for local mode, OpenAI Realtime
   for cloud mode) feeds an LLM that decides what to say and what to do.
3. **Looks things up** — when the visitor asks a faculty question (room,
   contact, deadline, etc.), the agent calls a `query_search` tool that hits
   a Weaviate vector DB seeded with FEE documents.
4. **Responds** — generates a reply, plays it back through Pepper's speakers,
   and triggers a matching gesture (`play_animation`) on Pepper's body.

Two interchangeable backends share the same pipeline:

- **OpenAI mode** — `gpt-realtime-mini` running directly on the RPi for
  minimal latency.
- **Local mode** — Whisper STT + Qwen 2.5 7B (vLLM on the GPU server) +
  Piper TTS, connected via a single SSH reverse tunnel.

You can switch between them at runtime — the orchestrator handles the
hand-off.

---

## Architecture

```
+---------------- RPi (Raspberry Pi 5) ------------------+      +-- woska (GPU server) --+
|                                                        |      |                        |
|  Visitor / mic                                         |      |  pepper-local agent    |
|       |                                                |      |  (Whisper + Piper)     |
|       v                                                |      |          |             |
|  +------------+        +-----------------+             |      |          |             |
|  |  LiveKit   |<-----> | voice-agent     |             |      |   +-----------+        |
|  |  server    |        | "pepper-openai" |             |      |   |   vLLM    |        |
|  +-----+------+        | (OpenAI         |  SSH tunnel |      |   | Qwen 2.5  |        |
|        |               |  Realtime API)  |  <======>   |      |   |   7B      |        |
|        |               +-----------------+             |      |   +-----------+        |
|        |                                               |      |                        |
|  +-----+------+   +--------------+   +-----------+     |      +------------------------+
|  | weaviate   |   | orchestrator |   |  bridge   |
|  | (RAG over  |   | (room +      |   | (qi -> Pepper)
|  |  FEE docs) |   |  tokens +    |   +-----+-----+     +----------+
|  +------------+   |  dispatch)   |         |           |  Pepper  |
|                   +--------------+         +---------->|  robot   |
|                                                        |  :9559   |
+--------------------------------------------------------+----------+
```

The **orchestrator** creates the LiveKit room, hands out tokens to all
participants (`user`, `agent`, `listener-python`, `debug-cli`), and
dispatches the warm voice-agent that matches the currently selected mode.
Mode switching is a simple file write — no HTTP API needed.

**Connection topology** for local mode: a single SSH reverse tunnel from
RPi → woska (via the `ptak.felk.cvut.cz` jump host) carries both LiveKit
signaling (port 7880) and WebRTC media (port 7881 TCP). No UDP, no TURN, no
VPN. The full investigation is at
[docs/logs/connection-test-journal.md](docs/logs/connection-test-journal.md).

---

## Quick Start

### Prerequisites

- Raspberry Pi 5 (8 GB) with Docker + Docker Compose
- `.env` file in the project root with `OPENAI_API_KEY`, `LIVEKIT_API_KEY`,
  `LIVEKIT_API_SECRET`, `LIVEKIT_KEYS`
- Pepper robot on the same network (or reachable via TCP)
- For local mode only: SSH access to `woska` via `ptak.felk.cvut.cz`

### Bring it up

```bash
# All RPi services (one command starts everything — no profiles, no flags):
docker compose -f docker/docker-compose.yml up -d
```

That brings up LiveKit, the orchestrator, the OpenAI voice-agent, the
robot bridge, the audio bridge, the user-client (RPi mic), the safe-startup
watchdog, Weaviate (RAG), and the SSH tunnels to woska.

### Talk to Pepper

- **Voice** — once user-client is up, just speak into the RPi mic. Pepper
  replies through her speakers.
- **Text (debug)** — open a CLI in the same room and type:
  ```bash
  uv run python services/src/text_chat.py
  ```
  Slash commands available: `/help`, `/status`, `/mode openai|local`,
  `/mic on|off`, `/reset`, `/quit`. Tool calls and agent transcripts stream
  inline. See [docs/notes/text-chat-cli.md](docs/notes/text-chat-cli.md).

### Switch backend

From inside the chat CLI:
```
/mode local       # use Qwen 2.5 7B on woska
/mode openai      # back to OpenAI Realtime
```
Or write the file directly:
```bash
echo '{"agent_mode": "local"}' > services/src/orchestrator_config.json
```

The orchestrator picks up the change within 3 seconds.

---

## Project Structure

```
voice-agent/
  src/
    agent.py            # LiveKit agent — warm dispatch, persistent session loop
    config.py           # Configuration, system prompts, animation groups
    tools.py            # query_search + play_animation tool definitions
    local_speech.py     # Faster Whisper STT + Piper TTS (local mode)
    utils.py            # Weaviate connection, hybrid search
  data/FEL/             # RAG source documents (FEE statutes, codes)
  models/piper/         # Piper TTS ONNX model

robot/
  src/
    bridge.py           # Pepper HTTP bridge (animations, tablet, audio)
    utils.py            # Pure helpers imported by bridge.py
    config.py           # Env-backed tunables for bridge + scripts
  scripts/              # Standalone entry points (not a library)
    safe_startup_watchdog.py  # Docker `safe-startup` service
    safe_startup.py           # wakeUp + StandInit bootstrap (Pepper discovery)
    capabilities.py           # One-shot NAOqi service/behavior dump
    generate_animations_config.py  # Rebuild animations.json from a dump

services/
  src/
    orchestrator.py     # Room + token + dispatch (replaces old session-manager)
    audio_bridge.py     # Agent audio (LiveKit) -> TCP -> Pepper speakers
    user_client.py      # RPi mic -> LiveKit
    text_chat.py        # Debug CLI (joins as debug-cli, prints tool calls)
  data/
    token-latest.json   # Current LiveKit tokens (written by orchestrator)

docker/
  docker-compose.yml    # All service definitions
  Dockerfile.runtime    # Shared Python 3.12 + uv base image
  livekit/livekit.yaml  # LiveKit server config (node_ip=127.0.0.1, ICE/TCP)
```

---

## Documentation

See [PROJECT.md](PROJECT.md) for the full project overview, components,
and thesis checklist.

| Topic | Doc |
|-------|-----|
| Running and deploying | [docs/notes/running.md](docs/notes/running.md) |
| GPU server (woska) setup | [docs/notes/gpu-setup.md](docs/notes/gpu-setup.md) |
| Local LLM (vLLM) | [docs/notes/local-llm-setup.md](docs/notes/local-llm-setup.md) |
| RPi vs Ubuntu dev differences | [docs/notes/rpi-dev.md](docs/notes/rpi-dev.md) |
| Debug chat CLI | [docs/notes/text-chat-cli.md](docs/notes/text-chat-cli.md) |
| All debugging/investigation notes | [docs/notes/](docs/notes/) |
| Connection test journal (the "how we made WebRTC stable" log) | [docs/logs/connection-test-journal.md](docs/logs/connection-test-journal.md) |

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| WebRTC / Rooms | [LiveKit](https://livekit.io) + LiveKit Agents SDK |
| LLM (cloud) | OpenAI Realtime API (`gpt-realtime-mini`) |
| LLM (local) | [vLLM](https://github.com/vllm-project/vllm) + Qwen 2.5 7B |
| STT (local) | [Faster Whisper](https://github.com/SYSTRAN/faster-whisper) |
| TTS (local) | [Piper](https://github.com/rhasspy/piper) |
| RAG | [Weaviate](https://weaviate.io) + OpenAI embeddings |
| Robot SDK | libqi / NAOqi (SoftBank Pepper) |
| Deployment | Docker Compose on Raspberry Pi 5 |
| Language | Python 3.12 |
