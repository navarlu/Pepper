# Pepper — LiveKit + LLM Voice Agent for Pepper

This project turns a Pepper robot into a conversational assistant by connecting:
1) a **Python 2.7 NAOqi bridge** (Pepper control/TTS/tablet/camera),
2) a **Python 3 LiveKit voice pipeline** (STT → LLM → TTS),
3) optional **Letta** (agent memory) and **Weaviate** (RAG),
4) and a simple **CLI** for testing.

The result is a full loop: a user speaks → LiveKit transcribes → the agent responds → audio is played back in the LiveKit room and optionally mirrored to Pepper’s speaker and tablet.

## High-Level Architecture

**Main pieces**
1. **NAOqi Bridge (Python 2.7, Flask)**  
   File: `src/bridge.py`  
   Exposes REST endpoints to:
   - Speak with Pepper (`/say`)
   - Trigger behaviors/animations (`/animation/...`)
   - Show text on tablet (`/tablet/text_inline`)
   - Capture a photo (`/camera/photo`)

2. **LiveKit Voice Pipeline (Python 3)**  
   File: `src/cli_voice.py`  
   Runs a LiveKit Agent session (STT + LLM + TTS).  
   When the assistant speaks, the text can be mirrored to Pepper via the bridge.

3. **Motion/Animation Controller (Python 3)**  
   File: `src/id_worker.py` (+ `src/motion.py`)  
   A lightweight “ID” agent that reads recent conversation and triggers a **single animation**
   (e.g., listening, gestures) via the bridge. This gives Pepper physical reactions that match
   the dialogue.

4. **LiveKit → Pepper Audio Bridge (optional, mixed Py2/Py3)**  
   - `src/listener_pepper_bridge.py` (Python 3): subscribes to LiveKit room audio, resamples to 48 kHz mono, sends PCM via TCP  
   - `src/pepper_audio_server.py` (Python 2.7): receives PCM and streams into `ALAudioDevice`  
   This lets Pepper speak **exactly the same audio** that is produced in the LiveKit room.

5. **Voice Agent Service (Python 3, optional)**  
   Folder: `voice-agent/`  
   A dedicated LiveKit agent with OpenAI realtime and optional Weaviate search.

6. **Infra via Docker**  
   File: `docker/docker-compose.yml`  
   Starts LiveKit, Redis, Letta, and a web playground.

## Key Flow (Voice)

1. User speaks in LiveKit room.
2. `cli_voice.py` transcribes (STT), sends to LLM, then generates TTS.
3. TTS audio is broadcast in the LiveKit room.
4. Optional: the same audio is forwarded to Pepper over TCP and played using NAOqi.
5. Optional: the assistant’s text response is also sent to Pepper `/say` for mirrored TTS.

## Modes

**Real vs Virtual robot**
- The `main.py` launcher sets `ROBOT_TARGET` using `REAL = True/False`.
- `REAL=True` targets physical Pepper (requires NAOqi).
- `REAL=False` targets a virtual robot (animations mapped in `virtual_animations.py`).

**Chat vs Voice**
- `main.py` uses `RUN_MODE = "cli_chat"` or `RUN_MODE = "cli_voice"`.


## Quick Start (Typical Demo)

### 1) Start infrastructure
```bash
docker compose -f docker/docker-compose.yml --env-file .env up -d
```

### 2) Start the Pepper bridge (Python 2.7)
```bash
pyenv shell naoqi27
unset LD_LIBRARY_PATH
export NAOQI_ROOT="$HOME/Projects/FEL/Pepper/choregraphe"
export PYTHONPATH="$NAOQI_ROOT/lib/python2.7/site-packages"
python2 src/bridge.py
```

### 3) Start the LiveKit voice CLI (Python 3)
```bash
python3 main.py
```
`main.py` uses `RUN_MODE = "cli_voice"` by default.  
To switch to text chat, set `RUN_MODE = "cli_chat"`.

### 4) (Optional) Route LiveKit audio to Pepper speakers
On the Pepper/NAOqi side (Python 2.7):
```bash
python2 src/pepper_audio_server.py
```
On your machine (Python 3):
```bash
python3 src/listener_pepper_bridge.py
```
`listener_pepper_bridge.py` watches a token snapshot file (default: `web/agents-playground/token-latest.json`) to reconnect when a new LiveKit session is created.

