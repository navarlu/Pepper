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
| LLM (cloud) | **OpenAI Realtime API** (`gpt-realtime-mini`) | Runs on RPi as `pepper-openai` — co-located with LiveKit for minimal latency |
| LLM (local) | **Faster Whisper** STT + **Qwen 2.5 7B** (vLLM) + **Piper** TTS | Runs on GPU server as `pepper-local`; `agent.py` (local mode) + `local_speech.py` |
| RAG | **Weaviate** vector DB + `text-embedding-3-large` | Hybrid search (alpha 0.7), seeded from `voice-agent/data/FEL/` PDFs |
| Tools | `query_search` — RAG lookup + room directions; `play_animation` — trigger Pepper gestures via bridge HTTP | Unified 2-tool set for both modes, defined in `tools.py` |

Entry point: [voice-agent/src/agent.py](voice-agent/src/agent.py) — connects to the LiveKit room, waits for the user participant (skips the audio-bridge listener), starts an `AgentSession`, greets, then keeps the session alive as a persistent warm agent across conversations. The `orchestrator` dispatches to `pepper-openai` or `pepper-local` by agent name based on the selected mode.

### Robot Bridge (`robot/`)

| Component | File | Role |
|---|---|---|
| **Bridge** | [robot/src/bridge.py](robot/src/bridge.py) | Python 3 HTTP server on host network. Receives PCM audio via TCP from the audio-bridge and plays it on Pepper via `qi`. Exposes `/animation/<name>`, `/tablet/...` HTTP endpoints. |
| **Safe Startup** | [robot/utils/safe_startup_watchdog.py](robot/utils/safe_startup_watchdog.py) | Watchdog that ensures Pepper is reachable (NAOqi up + ALMotion ready) before accepting interactions. |

### Services (`services/src/`)

| Component | File | Role |
|---|---|---|
| **Orchestrator** | [services/src/orchestrator.py](services/src/orchestrator.py) | Lightweight session lifecycle: creates the LiveKit room, writes participant tokens to [services/data/token-latest.json](services/data/token-latest.json), dispatches the warm agent, and watches `orchestrator_config.json` for mode changes. No HTTP server, no dashboard. |
| **Audio Bridge** | [services/src/audio_bridge.py](services/src/audio_bridge.py) | Joins LiveKit as `listener-python`, captures agent audio, forwards PCM over TCP to the bridge. |
| **User Client** | [services/src/user_client.py](services/src/user_client.py) | Joins LiveKit as `user`, captures the RPi mic (ALSA), publishes audio. Listens on `pepper.control` for soft mute/unmute. |
| **Text Chat CLI** | [services/src/text_chat.py](services/src/text_chat.py) | Terminal client that joins as `debug-cli` for typing instead of speaking. Streams tool-call events. See [text-chat-cli.md](docs/notes/text-chat-cli.md). |

### Infrastructure (`docker/`)

All services run via `docker compose -f docker/docker-compose.yml` from the project root. Key containers:

| Service | Image / Build | Port |
|---|---|---|
| `livekit` | `livekit/livekit-server:v1.10.1` | 7880, 7881 (loopback only) |
| `redis` | `redis:7.4.2-alpine` | 6379 |
| `weaviate` | `cr.weaviate.io/semitechnologies/weaviate:1.35.1` | 8080, 50051 |
| `voice-agent` | Custom (`Dockerfile.runtime`) — runs `pepper-openai` on RPi; `pepper-local` is started manually in tmux on woska | — |
| `orchestrator` | Custom — replaces the old session-manager | — |
| `bridge` | Custom, `network_mode: host` | 5000 (HTTP), 55555 (TCP) |
| `audio-bridge` | Custom | — |
| `user-client` | Custom, ALSA passthrough | — |
| `safe-startup` | Custom, `network_mode: host` | — |
| `reverse-tunnel` | `alpine` + autossh — RPi → woska (forwards 7880, 7881, 5000, 8080, 50051) | — |
| `ssh-tunnel` | `alpine` + autossh — RPi:8000 → woska:8000 (vLLM access for local mode) | — |

### Pepper Connection

Communication with the physical robot uses **libqi** (NAOqi SDK) via the `qi` Python module. On RPi this is self-built from source for ARM64 (see [rpi-dev.md](docs/notes/rpi-dev.md)). The bridge connects to Pepper at `tcp://<PEPPER_IP>:9559`.

### Knowledge Base

RAG documents in `voice-agent/data/FEL/`: CTU/FEE statutes, scholarship code, accommodation code, career code, disciplinary code, doctoral study code. Chunked and embedded into Weaviate on agent startup.

## Architecture Diagram

See [README.md](README.md#architecture) for the high-level diagram showing the RPi/GPU server split and how both agents connect to LiveKit.

## Detailed Documentation

| Doc | Description |
|-----|-------------|
| **Operations** | |
| [docs/notes/running.md](docs/notes/running.md) | How to start, stop, deploy, switch modes, check logs |
| [docs/notes/gpu-setup.md](docs/notes/gpu-setup.md) | woska (GPU server) setup — single SSH tunnel ICE/TCP topology, deployment |
| [docs/notes/local-llm-setup.md](docs/notes/local-llm-setup.md) | Local LLM backend (Qwen via vLLM) setup and SSH tunnel |
| [docs/notes/rpi-dev.md](docs/notes/rpi-dev.md) | RPi vs Ubuntu development differences (qi, Docker, audio) |
| [docs/notes/text-chat-cli.md](docs/notes/text-chat-cli.md) | The `services/src/text_chat.py` debug CLI — slash commands, topics, architecture |
| [docs/notes/cmd.md](docs/notes/cmd.md) | Frequently used commands (scp to woska, restart, mode switch) |
| **Debugging notes** (chronological investigation logs) | |
| [docs/notes/tools-issue.md](docs/notes/tools-issue.md) | Qwen 7B tool-calling investigation — 10 attempts, what worked and what didn't |
| [docs/notes/tool-history-stripping.md](docs/notes/tool-history-stripping.md) | Tool history stripping experiment (resolved — stripping was unnecessary) |
| [docs/notes/hello-problem.md](docs/notes/hello-problem.md) | First-interaction greeting bugs (resolved — unified tools + grace window) |
| [docs/notes/connection-issue.md](docs/notes/connection-issue.md) | WebRTC instability diagnosis (resolved 2026-04-15 — see logs/connection-test-journal.md) |
| [docs/notes/zombies.md](docs/notes/zombies.md) | Zombie LiveKit agent participants (mostly resolved — room rotation + cleanup) |
| [docs/notes/vllm-debugging.md](docs/notes/vllm-debugging.md) | How to read vLLM and agent logs on woska |
| **Working logs** | |
| [docs/logs/connection-test-journal.md](docs/logs/connection-test-journal.md) | Standalone investigation that produced the working SSH/ICE-TCP setup (the resolution referenced from `connection-issue.md`) |
| **Thesis** | |
| [docs/thesis/EXPERIMENT.md](docs/thesis/EXPERIMENT.md) | Experiment design — within-subjects study, Godspeed + Almere questionnaires, procedure, metrics |
| **Other** | |
| [docs/notes/knowledge-sources.md](docs/notes/knowledge-sources.md) | Planned knowledge base sources |
| [docs/notes/scratchpad.md](docs/notes/scratchpad.md) | Miscellaneous notes, AI policy, git/overleaf workflow |

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
