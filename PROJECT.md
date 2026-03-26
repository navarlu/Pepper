# Project Overview

**Thesis:** Design of an LLM-Driven Receptionist Robot for Social Interaction
**Author:** Bc. Lukáš Navara
**Supervisor:** doc. Mgr. Matěj Hoffmann, Ph.D.
**Department:** Katedra kybernetiky, FEL ČVUT
**Assigned:** 2026-02-04

## Goal

Investigate how large language models can support natural spoken interaction with the humanoid robot **Pepper** in a receptionist scenario at CTU FEE (Karlovo náměstí). The system must:

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
| LLM (cloud) | **OpenAI Realtime API** (`gpt-realtime-mini`) | Streaming speech-to-speech via `livekit-plugins-openai` |
| LLM (local, WIP) | Cascade pipeline: **Faster Whisper** STT → GPT-4.1-mini → **Piper** TTS | `cascade_agent.py` + `local_speech.py` |
| RAG | **Weaviate** vector DB + `text-embedding-3-large` | Hybrid search (alpha 0.7), seeded from `voice-agent/data/FEL/` PDFs |
| Tools | `query_search` — RAG lookup; `play_animation` — trigger Pepper gestures via bridge HTTP | Defined in `tools.py`, registered on agent |

Entry point: `voice-agent/src/agent.py` — connects to LiveKit room, waits for a real user participant (skips the bridge listener), starts an `AgentSession` with the realtime model, greets, then keeps the session alive until disconnect.

### Robot Bridge (`robot/`)

| Component | File | Role |
|---|---|---|
| **Bridge** | `robot/src/bridge.py` | Python 3 HTTP server running on the host. Receives PCM audio via TCP from the listener and plays it on Pepper via `qi`. Exposes `/animation/<name>`, `/tablet/...` HTTP endpoints. Manages Pepper's Autonomous Life settings. |
| **Listener** | `services/src/listener.py` | Joins the LiveKit room as a passive participant, captures the agent's audio track, and forwards raw PCM frames over TCP to the bridge. |
| **Session Manager** | `services/src/session_manager.py` | Orchestrates session lifecycle: creates LiveKit rooms/tokens, tracks component health via heartbeats, serves the **Operator Panel** (debug HTML UI on `:8787`), and manages session state (idle → active → cooldown). |
| **Safe Startup** | `robot/utils/safe_startup_watchdog.py` | Watchdog that ensures Pepper is reachable and services are healthy before the bridge starts accepting audio. |
| **User Client** | `services/src/user_client.py` | Optional: joins LiveKit room with a local microphone (ALSA on RPi) as the "user" audio source. |

### Infrastructure (`docker/`)

All services run via `docker compose`. Key containers:

| Service | Image / Build | Port |
|---|---|---|
| `livekit` | `livekit/livekit-server:v1.9.11` | 7880 |
| `redis` | `redis:7.4.2-alpine` | — |
| `weaviate` | `weaviate:1.35.1` | 8080, 50051 |
| `voice-agent` | Custom (`Dockerfile.runtime`) | — |
| `session-manager` | Custom | 8787 |
| `bridge` | Custom, `network_mode: host` | 5000 (HTTP), 55555 (TCP) |
| `listener` | Custom | — |
| `safe-startup` | Custom, `network_mode: host` | — |
| `user-client` | Custom, ALSA passthrough | — (profile: `audio`) |

### Pepper Connection

Communication with the physical robot uses **libqi** (NAOqi SDK) via the `qi` Python module. On RPi this is self-built from source for ARM64 (see `RPI_DEV.md`). The bridge connects to Pepper at `tcp://<PEPPER_IP>:9559`.

### Knowledge Base

RAG documents in `voice-agent/data/FEL/`: CTU/FEE statutes, scholarship code, accommodation code, career code, disciplinary code, doctoral study code. Chunked and embedded into Weaviate on agent startup.

## Architecture Diagram (data flow)

```
  Browser / Playground          RPi / Host machine           Pepper
  ┌──────────────┐         ┌──────────────────────┐     ┌──────────┐
  │  Next.js UI  │◄──ws──►│     LiveKit Server    │     │          │
  │  (mic+spkr)  │         │                      │     │  qi API  │
  └──────────────┘         └───┬──────────┬───────┘     │  :9559   │
                               │          │              └────▲─────┘
                    ┌──────────▼──┐  ┌────▼─────────┐        │
                    │ Voice Agent │  │   Listener    │   TCP  │ HTTP
                    │ (LLM+RAG)  │  │ (audio track) ├──pcm──►│
                    └──────┬──────┘  └──────────────┘   ┌────┴─────┐
                           │                            │  Bridge  │
                      tool call                         │ (qi+HTTP)│
                    ┌──────▼──────┐                     └──────────┘
                    │  Weaviate   │                      animations
                    │  (vectors)  │                      tablet UI
                    └─────────────┘                      audio play
```

## Today's Focus (2026-03-25)

### 1. Enrich FEE Knowledge Base
- Add more internal documents (study plans, department info, building maps, opening hours, contacts, FAQ).
- Goal: Pepper can actually guide visitors about FEE — programmes, offices, people, procedures.

### 2. Gesture & Animation System
- Expand beyond the current 6 animations (`Hey_1`, `BowShort_1`, `Explain_1`, `Happy_1`, `Thinking_1`, `IDontKnow_1`).
- Make the LLM call `play_animation` more naturally and frequently to feel human-like.
- Investigate Pepper's full animation library via `qi` and expose more gestures.

### 3. Eye LEDs / Facial Expressions
- Use Pepper's eye LEDs (`ALLeds` service) to convey emotion — e.g., blue when listening, green when happy, pulsing when thinking.
- Expose as a tool or automatic behaviour tied to agent state.

### 4. Tablet Integration
- The bridge already has tablet endpoints (`/tablet/...`). Make real use of them:
  - Show a welcome screen / FEE logo when idle.
  - Display key information during conversation (room numbers, maps, links, contact cards).
  - Show the conversation transcript live.
- Design a clean tablet UI that complements the spoken interaction.

### 5. Local LLM Testing
- `cascade_agent.py` already scaffolds Whisper STT → LLM → Piper TTS pipeline.
- Get it running end-to-end and benchmark latency vs. the OpenAI Realtime path.
- Consider vLLM or Ollama for the local LLM backend.

## Thesis Task Checklist

| # | Task | Status |
|---|---|---|
| 1 | Literature review (LLMs + social HRI) | In progress |
| 2 | LLM-based conversational framework on Pepper | **Done** (core pipeline working) |
| 3 | Speech recognition + synthesis integration | **Done** (OpenAI Realtime); local cascade WIP |
| 4 | RAG over internal documents | **Done** (Weaviate + FEE docs); needs more content |
| 5 | Non-verbal communication (gestures, expressions, tablet) | Partial — 6 animations working; LEDs + tablet TODO |
| 6 | Receptionist demo scenario | Partially working end-to-end |
| 7 | Cloud vs. local LLM comparison | TODO |
| 8 | HRI evaluation (questionnaires + user study) | TODO |
