# Deployment Notes

## Architecture

- `bridge.py` requires x86_64 NAOqi / Python 2 runtime → **not portable to ARM/Raspberry Pi**
- Docker services are portable in principle
- Host-side: `safe_startup.py`, `bridge.py`, `user_client.py`

## What runs where

| Component | Where |
|-----------|-------|
| livekit, redis, weaviate | Docker |
| voice-agent, session-manager, listener | Docker |
| safe_startup.py, bridge.py | Host (Python 2.7) |
| user_client.py | Host (Python 3 / uv) |

## Prerequisites

```bash
sudo apt install -y curl git build-essential ffmpeg libasound2-dev portaudio19-dev
curl -LsSf https://astral.sh/uv/install.sh | sh
uv sync
```

## Python 2.7 for NAOqi (pyenv)

```bash
pyenv shell naoqi27
unset LD_LIBRARY_PATH
export NAOQI_ROOT="$HOME/Projects/FEL/Pepper/robot/choregraphe"
export PYTHONPATH="$NAOQI_ROOT/lib/python2.7/site-packages"
```

## What to copy to a new machine

- The repository
- `.env`
- pyenv `naoqi27` environment (or recreate it)

## Raspberry Pi limitation

- `bridge.py` depends on x86_64 `qi` binaries
- Cannot run the current bridge on ARM without replacing the NAOqi runtime
