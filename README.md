


# Pepper — LLM-Driven Receptionist Robot

![Pepper](docs/assets/pepper.jpeg)

## Demo
https://github.com/user-attachments/assets/2492acbb-9636-472e-82ef-f5f8f3a825a6

**Design of an LLM-Driven Receptionist Robot for Social Interaction**

Master's thesis project at CTU FEE Prague. Pepper greets students in front
of the faculty, holds a spoken conversation, looks up faculty information
(rooms, schedules, mensa menu, staff contacts) with retrieval-augmented
generation, and accompanies its speech with gestures and a tablet UI.

| | |
|---|---|
| **Author** | Bc. Lukas Navara |
| **Supervisor** | doc. Mgr. Matej Hoffmann, Ph.D. |
| **Department** | Dept. of Cybernetics, FEL CVUT |
| **Platform** | Raspberry Pi 5 + SoftBank Pepper |

---

## What it does

A student walks up to Pepper. Pepper:

1. **Listens** — captures audio over the RPi mic, streams it into a
   LiveKit room.
2. **Understands** — speech-to-text → LLM → speech-to-text-out, where each
   stage runs on either the local GPU server (woska) or on OpenAI's cloud
   depending on the experiment variant.
3. **Looks things up** — when the student asks a faculty question (a
   room number, a teacher's office hours, the mensa menu, the timetable
   for a course code, etc.) the agent calls one of the tools in
   [voice-agent/src/experiment/tools/](voice-agent/src/experiment/tools/).
   `query_search` hits a Weaviate vector DB seeded with FEE documents;
   `find_path_to_room`, `subject_schedule`, `lookup_person`,
   `mensa_menu`, `get_time` answer their respective queries directly.
4. **Responds** — generates a reply and plays it back through Pepper's
   speakers, with the live transcript and contextual content (QR codes,
   the farewell screen) shown on Pepper's tablet.
5. **Wraps up** — once the conversation reaches a natural close, the
   agent calls `end_conversation_streaming`, which displays a farewell
   QR for the feedback form and ends the session.

The whole thing is driven from the experimenter's laptop as a study
session: each conversation is one "participant" of the user study, logged
to a JSONL file, and the experiment loops through participants
automatically.

---

## Experiment variants

The same room, the same tools, the same prompt — only the speech stack
changes. Both variants run as **streaming** workers on the woska GPU
server:

| Variant | Where it runs | STT | LLM | TTS | Worker file |
|---|---|---|---|---|---|
| **A** (local streaming) | woska (GPU server) | Faster-Whisper | vLLM **Llama 3.1 8B Instruct (AWQ)** | Piper | [agent_streaming.py](voice-agent/src/experiment/agent_streaming.py) |
| **B** (cloud streaming) | woska (GPU server) | **gpt-4o-mini-transcribe** | **gpt-4o-mini** | **gpt-4o-mini-tts** | [agent_4o_streaming.py](voice-agent/src/experiment/agent_4o_streaming.py) |

Both workers register a distinct `agent_name` with LiveKit
(`pepper-experiment-local-streaming`, `pepper-experiment-streaming`) and
the launcher dispatches the one that matches the requested variant.
Tool definitions, the system prompt, and the JSONL recorder are shared
across variants so transcripts are directly comparable.

The auto-loop alternates **A ↔ B** after every session (see
[loop_launcher_streaming.py](voice-agent/src/experiment/loop_launcher_streaming.py)).

---

## Architecture

```
+---------------- RPi (Raspberry Pi 5) -------------------+      +-- woska (GPU server) -------+
|                                                         |      |                             |
|  Student / mic                                          |      |  Variant A worker           |
|       |                                                 |      |  (agent_streaming.py, tmux) |
|       v                                                 |      |    Faster-Whisper           |
|  +------------+                                         |      |    vLLM Llama 3.1 8B AWQ    |
|  |  LiveKit   |<--------------------------------+       |      |    Piper TTS                |
|  |  server    |                                 | SSH   |      |                             |
|  +-----+------+                                 | tun.  |      |  Variant B worker           |
|        |                                        <=====> |      |  (agent_4o_streaming.py,    |
|        |                                                |      |   tmux)                     |
|  +-----+------+   +-------------------+   +-----------+ |      |    gpt-4o-mini-transcribe   |
|  |  weaviate  |   |  experiment-      |   |  bridge   | |      |    gpt-4o-mini              |
|  |  (RAG over |   |  orchestrator     |   | (qi -> Pepper) |      |    gpt-4o-mini-tts          |
|  |   FEE      |   | (room+tokens+     |   +-----+-----+ |      |                             |
|  |   docs)    |   |  dispatch)        |         |       |      |  Both workers join the      |
|  +------------+   +-------------------+         |       |      |  LiveKit room               |
|                                                 |       |      |  `pepper-experiment` when   |
|  +------------+   +-----------------+   +------------+  |      |  dispatched by              |
|  | tablet     |   | audio_bridge    |   | user_client|  |      |  launcher_streaming.py      |
|  | server     |   | (agent → speaker|   | (mic → LK) |  |      |                             |
|  | (QR / UI)  |   |  via TCP)       |   |            |  |      +-----------------------------+
|  +------------+   +-----------------+   +------------+  |
|                                                         |               +----------+
+---------------------------------------------------------+  +----------->|  Pepper  |
                                                             |            |  robot   |
                                                             |            |  :9559   |
                                                             |            +----------+

                +----------------- experimenter laptop -------------------+
                |                                                         |
                |  loop_launcher_streaming.py  →  launcher_streaming.py   |
                |   (rotates A/B,                  (one session)          |
                |    persists state,               writes JSONL log,      |
                |    armed idle timer)             dispatches variant     |
                |                                  worker, watches stdin) |
                +---------------------------------------------------------+
```

The **experiment-orchestrator** creates the fixed `pepper-experiment`
room and hands tokens to all stationary participants (bridge,
audio-bridge, user-client, tablet-server). Per session,
`launcher_streaming.py` dispatches the variant-specific streaming agent
into that room and joins itself as `experimenter-recorder` to capture
every event on the `pepper.experiment` data topic.

---

## Quick start — a study session

### Prerequisites

- Raspberry Pi 5 (8 GB) with Docker + Docker Compose
- `.env` at project root with `OPENAI_API_KEY`, `LIVEKIT_API_KEY`,
  `LIVEKIT_API_SECRET`, `LIVEKIT_KEYS`
- Pepper robot reachable on the LAN
- For variant A: SSH access to `woska` via `halmos.felk.cvut.cz`, vLLM
  running there with Llama 3.1 8B Instruct AWQ

### 1. Bring up the stationary services (one-time)

```bash
docker compose -f docker/docker-compose.experiment.yml up -d
```

That starts LiveKit, Redis, Weaviate, the experiment-orchestrator,
the Pepper bridge, the audio bridge, the user-client (mic), the tablet
server, and the SSH tunnels.

### 2. Start the variant A streaming worker on woska

```bash
ssh -J navarlu2@halmos.felk.cvut.cz navarlu2@woska
tmux new-session -s pepper-experiment
cd /mnt/.../Pepper && source .venv3/bin/activate
python voice-agent/src/experiment/agent_streaming.py dev
```

Variant B runs in a separate woska tmux session (`pepper-experiment-4o`):

```bash
ssh -J navarlu2@halmos.felk.cvut.cz navarlu2@woska
tmux new-session -s pepper-experiment-4o
cd /mnt/.../Pepper && source .venv3/bin/activate
python voice-agent/src/experiment/agent_4o_streaming.py dev
```

### 3. Run the experiment loop

From the experimenter's laptop:

```bash
# First-time start (or to reset the counter):
uv run python voice-agent/src/experiment/loop_launcher_streaming.py \
    --student 1 --variant A

# Subsequent runs — no args needed, resumes from saved state:
uv run python voice-agent/src/experiment/loop_launcher_streaming.py
```

The loop runner:

- Dispatches `launcher_streaming.py` with the next `student_id` /
  `variant`.
- Streams the recorder's log to stdout so you can see every
  `user_turn`, `tool_call`, `agent_speech`.
- After **30 s with no user turn**, sends `/done` to end the session
  cleanly (override with `--idle-seconds`).
- Increments the student id and flips the variant (**A ↔ B**) for the
  next session.
- Persists the next-up `student_id` + `variant` to
  `voice-agent/src/experiment/results/streaming_loop_state.json` after
  every session, so re-running the loop with no args resumes where you
  left off (e.g. stopped at T05/A → next run picks up T06/B).
- Keeps a heartbeat in `services/data/state.json` so the bridge and
  tablet know the experiment is running and keep Pepper awake. If you
  hard-kill the loop, the heartbeat ages out and Pepper falls asleep on
  her own.

Stop with `Ctrl+C` — the wrapper writes a clean JSONL footer for the
in-progress session and exits.

### 4. Logs

```
voice-agent/src/experiment/results/experiments/<YYYY-MM-DD>/
    student<id>_variant<X>_<HHMMSS>.jsonl
```

Each line is one structured event (`header`, `session_start`,
`user_turn`, `tool_call`, `tool_result`, `agent_speech`,
`session_end`, `footer`).

---

## Manual single-session use

Bypass the loop and run one session by hand:

```bash
uv run python voice-agent/src/experiment/launcher_streaming.py \
    --student 1 --variant A
# ...talk to Pepper, or type plain text + Enter to inject a typed turn...
# Type /done + Enter to end.
```

Slash commands inside `launcher_streaming.py`'s stdin: `/help`, `/done`
(or EOF / Ctrl-D). Anything else is published on `pepper.text` as a
typed user turn so you can drive prompts without a mic.

---

## Project structure

```
voice-agent/
  src/
    experiment/                     # streaming study-mode workers + launchers
      launcher_streaming.py         # dispatch one session, record JSONL
      loop_launcher_streaming.py    # rotate students/variants, persist state
      agent_streaming.py            # variant A (local stack on woska)
      agent_4o_streaming.py         # variant B (OpenAI 4o cascade on woska)
      audio_capture.py              # per-turn WAV capture for analysis
      _streaming_runtime.py         # shared per-session state (room, callbacks)
      _runtime_state.py             # writes experiment_active heartbeat
      prompt_streaming.py           # system prompt (shared across A/B)
      tools/                        # query_search, find_path_to_room,
                                    # subject_schedule, lookup_person,
                                    # mensa_menu, get_time,
                                    # end_conversation_streaming
      results/                      # JSONL logs + streaming_loop_state.json
      tests/                        # streaming smoke test + tool tests
    live/                           # shared infra used by experiment workers
      bridge_client.py              #   HTTP clients for Pepper bridge
      config.py                     #   shared constants
      local_speech.py               #   FasterWhisper STT + Piper TTS plugins
      qwen_compat.py                #   LLM JSON-arg sanitization
      rag.py                        #   Weaviate RAG client
      mensa.py, timetable.py, udb.py, _person_helpers.py, _room_directions.py
  data/FEL/                         # RAG source documents
  models/piper/                     # Piper TTS ONNX model

services/
  src/
    experiment/orchestrator.py      # creates pepper-experiment room + tokens
    live/                           # shared services run by experiment compose
      audio_bridge.py, user_client.py, tablet_server.py,
      session.py, config.py

robot/
  src/
    bridge.py                       # Pepper HTTP bridge (gestures, tablet, audio)
    config.py, utils.py
  scripts/
    safe_startup.py, safe_startup_watchdog.py,
    capabilities.py, generate_animations_config.py

docker/
  docker-compose.experiment.yml     # study stack (sole compose file)
  Dockerfile.runtime
  livekit/livekit.yaml
```

---

## Documentation

See [PROJECT.md](PROJECT.md) for the full project overview and thesis
checklist.

| Topic | Doc |
|-------|-----|
| GPU server (woska) setup | [docs/notes/gpu-setup.md](docs/notes/gpu-setup.md) |
| vLLM debugging notes | [docs/notes/vllm-debugging.md](docs/notes/vllm-debugging.md) |
| RPi vs Ubuntu dev differences | [docs/notes/rpi-dev.md](docs/notes/rpi-dev.md) |
| All debugging/investigation notes | [docs/notes/](docs/notes/) |

---

## Tech stack

| Layer | Technology |
|-------|-----------|
| WebRTC / Rooms | [LiveKit](https://livekit.io) + LiveKit Agents SDK |
| LLM — variant A (local streaming) | [vLLM](https://github.com/vllm-project/vllm) + **Llama 3.1 8B Instruct (AWQ)** |
| LLM — variant B (cloud streaming) | OpenAI — **gpt-4o-mini** |
| STT — variant A | [Faster Whisper](https://github.com/SYSTRAN/faster-whisper) |
| STT — variant B | OpenAI **gpt-4o-mini-transcribe** |
| TTS — variant A | [Piper](https://github.com/rhasspy/piper) |
| TTS — variant B | OpenAI **gpt-4o-mini-tts** |
| VAD (A & B) | Silero VAD |
| RAG | [Weaviate](https://weaviate.io) + OpenAI embeddings |
| Robot SDK | libqi / NAOqi (SoftBank Pepper) |
| Deployment | Docker Compose on Raspberry Pi 5 |
| Language | Python 3.12 |
