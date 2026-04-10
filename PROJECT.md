# Project Overview

**Thesis:** Design of an LLM-Driven Receptionist Robot for Social Interaction
**Author:** Bc. Lukas Navara
**Supervisor:** doc. Mgr. Matej Hoffmann, Ph.D.
**Department:** Katedra kybernetiky, FEL CVUT
**Assigned:** 2026-02-04

## Goal

Investigate how large language models can support natural spoken interaction with the humanoid robot **Pepper** in a receptionist scenario at CTU FEE (Karlovo namesti). The system must:

1. Conduct fluent spoken dialogue (speech recognition + synthesis).
2. Answer questions about FEE using retrieval-augmented generation (RAG) over internal documents.
3. Accompany speech with non-verbal behaviour — gestures, facial expressions (eye LEDs), and tablet content.
4. Work with both **cloud-based** (OpenAI) and **locally deployed** LLM backends, comparing latency and interaction quality.
5. Be evaluated via established HRI questionnaires and user feedback.

## Current Stack

### Voice Agent (`voice-agent/`)

| Component | Technology | Notes |
|---|---|---|
| Runtime | **LiveKit Agents SDK** (Python 3) | Manages room, audio tracks, agent session |
| LLM (cloud) | **OpenAI Realtime API** (`gpt-realtime-mini`) | Runs on RPi as `pepper-openai` — lightweight, co-located with LiveKit for minimal latency |
| LLM (local) | **Faster Whisper** STT + **Qwen 2.5 7B** (vLLM) + **Piper** TTS | Runs on GPU server as `pepper-local`; `agent.py` (local mode) + `local_speech.py` |
| RAG | **Weaviate** vector DB + `text-embedding-3-large` | Hybrid search (alpha 0.7), seeded from `voice-agent/data/FEL/` PDFs |
| Tools | `query_search` — RAG lookup + room directions; `play_animation` — trigger Pepper gestures via bridge HTTP | Unified 2-tool set for both modes, defined in `tools.py` |

Entry point: `voice-agent/src/agent.py` — connects to LiveKit room, waits for a real user participant (skips the bridge listener), starts an `AgentSession`, greets, then keeps the session alive as a persistent warm agent across conversations. The session manager dispatches to `pepper-openai` or `pepper-local` by agent name based on the selected mode.

### Robot Bridge (`robot/`)

| Component | File | Role |
|---|---|---|
| **Bridge** | `robot/src/bridge.py` | Python 3 HTTP server on host network. Receives PCM audio via TCP from the audio-bridge and plays it on Pepper via `qi`. Exposes `/animation/<name>`, `/tablet/...` HTTP endpoints. |
| **Audio Bridge** | `services/src/audio_bridge.py` | Joins LiveKit room, captures agent audio, forwards PCM over TCP to the bridge. |
| **Room Monitor** | `services/src/room_monitor.py` | Monitors LiveKit room state, forwards transcripts. |
| **Session Manager** | `services/src/session_manager/` | Orchestrates session lifecycle: creates LiveKit rooms/tokens, dispatches agents by mode, tracks component health, serves the **Operator Panel** on `:8787`. |
| **Safe Startup** | `robot/utils/safe_startup_watchdog.py` | Watchdog that ensures Pepper is reachable before accepting interactions. |
| **User Client** | `services/src/user_client.py` | Optional: joins LiveKit with a local microphone (ALSA on RPi) as the user audio source. |

### Infrastructure (`docker/`)

All services run via `docker compose -f docker/docker-compose.yml`. Key containers:

| Service | Image / Build | Port |
|---|---|---|
| `livekit` | `livekit/livekit-server:v1.9.11` | 7880 |
| `redis` | `redis:7.4.2-alpine` | 6379 |
| `weaviate` | `weaviate:1.35.1` | 8080, 50051 |
| `voice-agent` | Custom (`Dockerfile.runtime`) — OpenAI mode on RPi (`pepper-openai`); local mode on GPU server (`pepper-local`) | — |
| `session-manager` | Custom | 8787 |
| `bridge` | Custom, `network_mode: host` | 5000 (HTTP), 55555 (TCP) |
| `audio-bridge` | Custom | — |
| `room-monitor` | Custom | — |
| `safe-startup` | Custom, `network_mode: host` | — |
| `user-client` | Custom, ALSA passthrough | — (profile: `audio`) |

### Pepper Connection

Communication with the physical robot uses **libqi** (NAOqi SDK) via the `qi` Python module. On RPi this is self-built from source for ARM64 (see [rpi-dev.md](docs/notes/rpi-dev.md)). The bridge connects to Pepper at `tcp://<PEPPER_IP>:9559`.

### Knowledge Base

RAG documents in `voice-agent/data/FEL/`: CTU/FEE statutes, scholarship code, accommodation code, career code, disciplinary code, doctoral study code. Chunked and embedded into Weaviate on agent startup.

## Architecture Diagram

```
  Browser / Playground          RPi / Host machine           Pepper
  +----------------+         +----------------------+     +----------+
  |  Next.js UI    |<--ws-->|     LiveKit Server    |     |          |
  |  (mic+spkr)    |         |                      |     |  qi API  |
  +----------------+         +---+----------+-------+     |  :9559   |
                                 |          |              +----^-----+
                      +----------v--+  +----v-----------+       |
                      | Voice Agent |  | Audio Bridge   |  TCP  | HTTP
                      | (LLM+RAG)  |  | (audio track)  +--pcm->|
                      +------+------+  +----------------+  +----+-----+
                             |                             |  Bridge  |
                        tool call                          | (qi+HTTP)|
                      +------v------+                      +----------+
                      |  Weaviate   |                      animations
                      |  (vectors)  |                      tablet UI
                      +-------------+                      audio play
```

## Detailed Documentation

| Doc | Description |
|-----|-------------|
| **Operations** | |
| [running.md](docs/notes/running.md) | How to start, stop, deploy, and switch modes |
| [gpu-setup.md](docs/notes/gpu-setup.md) | GPU server (woska) setup — TURN relay, SSH tunnels, deployment |
| [local-llm-setup.md](docs/notes/local-llm-setup.md) | Local LLM backend (Qwen via vLLM) setup and SSH tunnel |
| [rpi-dev.md](docs/notes/rpi-dev.md) | RPi vs Ubuntu development differences (qi, Docker, audio) |
| **Debugging** | |
| [tools-issue.md](docs/notes/tools-issue.md) | Qwen 7B tool-calling investigation — 10 attempts, what worked and what didn't |
| [tool-history-stripping.md](docs/notes/tool-history-stripping.md) | Tool history stripping experiment (resolved — stripping was unnecessary) |
| [hello-problem.md](docs/notes/hello-problem.md) | First-interaction greeting bugs (resolved — unified tools, grace window) |
| [connection-issue.md](docs/notes/connection-issue.md) | WebRTC instability over SSH tunnel (mitigated by RPi agent split) |
| [zombies.md](docs/notes/zombies.md) | Zombie LiveKit agent participants (resolved — room rotation + cleanup) |
| [vllm-debugging.md](docs/notes/vllm-debugging.md) | How to read vLLM and agent logs on woska |
| **Other** | |
| [knowledge-sources.md](docs/notes/knowledge-sources.md) | Planned knowledge base sources |
| [scratchpad.md](docs/notes/scratchpad.md) | Miscellaneous notes, AI policy, git/overleaf workflow |

## Thesis Task Checklist

| # | Task | Status |
|---|---|---|
| 1 | Literature review (LLMs + social HRI) | In progress |
| 2 | LLM-based conversational framework on Pepper | **Done** (core pipeline working) |
| 3 | Speech recognition + synthesis integration | **Done** (OpenAI Realtime + local cascade) |
| 4 | RAG over internal documents | **Done** (Weaviate + FEE docs); needs more content |
| 5 | Non-verbal communication (gestures, expressions, tablet) | Partial — animations working; LEDs + tablet TODO |
| 6 | Receptionist demo scenario | Partially working end-to-end |
| 7 | Cloud vs. local LLM comparison | TODO |
| 8 | HRI evaluation (questionnaires + user study) | TODO |
