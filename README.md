# Pepper receptionist — paper stack

Code for the paper's experiment: Pepper, a humanoid robot, works as a
receptionist in Building E of CTU FEE. A LiveKit voice agent listens
through a USB microphone, answers with OpenAI cloud models, speaks
through Pepper's speakers and gestures through the robot bridge.
Everything runs on one Raspberry Pi 5 with Docker Compose.

## Services

All services are defined in [docker/docker-compose.paper.yml](docker/docker-compose.paper.yml).

| Service | Entrypoint | Role |
|---|---|---|
| `livekit` | `livekit/livekit-server` image | Media server |
| `orchestrator` | [services/src/experiment/orchestrator.py](services/src/experiment/orchestrator.py) | Creates the fixed `pepper-experiment` room and writes tokens to `services/data/token-latest.json` |
| `agent-inline` | [voice-agent/src/experiment/agent_4o_streaming.py](voice-agent/src/experiment/agent_4o_streaming.py) | Default agent: STT → LLM → TTS cascade with inline gesture tags |
| `realtime-agent` | [voice-agent/src/paper/agent_realtime.py](voice-agent/src/paper/agent_realtime.py) | Speech-to-speech variant (profile `realtime`) |
| `paper-dispatcher` | [voice-agent/src/paper/dispatcher.py](voice-agent/src/paper/dispatcher.py) | Keeps one agent dispatched into the room |
| `user-client` | [services/src/live/user_client.py](services/src/live/user_client.py) | USB mic + WebRTC AEC3 → LiveKit |
| `audio-bridge` | [services/src/live/audio_bridge.py](services/src/live/audio_bridge.py) | Agent audio → ssh + paplay → Pepper speakers |
| `tablet-server` | [services/src/live/tablet_server.py](services/src/live/tablet_server.py) | Tablet UI (transcript, state) |
| `bridge` | [robot/src/bridge.py](robot/src/bridge.py) | HTTP → NAOqi: gestures, head, LEDs, volume, tablet |
| `safe-startup` | [robot/scripts/safe_startup_watchdog.py](robot/scripts/safe_startup_watchdog.py) | Boot-time pose and volume watchdog |

The agent's tools live in [voice-agent/src/experiment/tools/](voice-agent/src/experiment/tools/).
The on-robot safe-startup package and its deploy script are in [robot/onboard/](robot/onboard/).

## Start

```bash
cp .env_example .env          # then fill in OPENAI_API_KEY and the LiveKit keys
docker compose -f docker/docker-compose.paper.yml up -d --build
docker compose -f docker/docker-compose.paper.yml logs -f agent-inline
docker compose -f docker/docker-compose.paper.yml down
```

To use the realtime agent instead, stop `agent-inline` and run:

```bash
PAPER_AGENT_NAME=pepper-paper-realtime \
  docker compose --profile realtime -f docker/docker-compose.paper.yml up -d
```

The `bridge` and `safe-startup` containers need the NAOqi Python SDK (`qi`).
It is not a pip package; compose bind-mounts a local build from the paths
set in the compose file.

## Environment

| Variable | Used by | Meaning |
|---|---|---|
| `OPENAI_API_KEY` | agents | OpenAI key |
| `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `LIVEKIT_KEYS` | all | LiveKit credentials (`LIVEKIT_KEYS="key: secret"`) |
| `OPENAI_LLM_MODEL` | `agent-inline` | LLM (default `gpt-5.4-mini`) |
| `AGENT_LANG` | `agent-inline` | Agent language (default `en`) |
| `ENABLE_INLINE_GESTURES` | `agent-inline` | `1` = gesture tags on, `0` = no gestures |
| `LOCAL_VAD_THRESHOLD`, `LOCAL_VAD_MIN_SPEECH` | `agent-inline` | Silero VAD tuning |
| `PAPER_AGENT_NAME` | agents, dispatcher | Worker name the dispatcher targets |
| `PAPER_REALTIME_MODEL`, `PAPER_REALTIME_VOICE` | `realtime-agent` | Realtime model and voice |
| `PEPPER_QI_URL` | `bridge`, `safe-startup` | NAOqi endpoint, e.g. `tcp://<pepper-ip>:9559` |
| `PEPPER_SSH_HOST`, `PEPPER_SSH_USER`, `PEPPER_SSH_PASSWORD` | `audio-bridge` | SSH target for audio playback |
| `PEPPER_OUTPUT_VOLUME`, `PEPPER_SAFE_STARTUP_VOLUME` | `bridge`, `safe-startup` | Speaker volume (0–100) |

## Animation catalog

The inline-gesture layer reads
[voice-agent/src/experiment/data/animation_catalog.json](voice-agent/src/experiment/data/animation_catalog.json).
[experiments/animation_metadata/build_agent_catalog.py](experiments/animation_metadata/build_agent_catalog.py)
regenerates it from the per-animation annotation files (kept outside git).
