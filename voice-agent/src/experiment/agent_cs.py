"""Czech variant of the experiment worker.

Thin wrapper: presets env vars so the shared `agent.py` boots with the
Czech prompt module, Czech Whisper language tag, and a Czech Piper
voice. Same `agent_name = "pepper-experiment"` as the English worker
— run ONE OR THE OTHER in the woska tmux, not both.

Usage on woska:
    cd /mnt/data_personal/navarlu2/work/Pepper
    source .venv3/bin/activate
    # GPU STT/TTS exports as usual:
    export LOCAL_STT_MODEL=small  # or medium for better Czech WER
    export LOCAL_STT_DEVICE=cuda
    export LOCAL_STT_COMPUTE_TYPE=float16
    export LOCAL_TTS_USE_CUDA=1
    export PYTHONUNBUFFERED=1
    export LD_LIBRARY_PATH="$(python -c 'import glob, nvidia; print(":".join(glob.glob(nvidia.__path__[0] + "/*/lib")))')${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    # Optional override if the voice file lives elsewhere:
    # export LOCAL_TTS_MODEL_PATH=/path/to/cs_CZ-yourvoice-medium.onnx
    python voice-agent/src/experiment/agent_cs.py dev

Prereqs:
  * Czech Piper voice (.onnx + matching .onnx.json) placed at the
    default path below, OR LOCAL_TTS_MODEL_PATH set to its location.
    Browse voices: https://huggingface.co/rhasspy/piper-voices/tree/main/cs/cs_CZ
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Resolve VOICE_AGENT_DIR the same way agent.py does so the default
# voice path is consistent.
_THIS_DIR = Path(__file__).resolve().parent
_VOICE_AGENT_DIR = _THIS_DIR.parent.parent

# Default Czech voice path. Override with LOCAL_TTS_MODEL_PATH if you
# downloaded a different voice (e.g. a female one). The .onnx.json
# sidecar must sit next to the .onnx file.
_DEFAULT_CS_VOICE = _VOICE_AGENT_DIR / "models" / "piper" / "cs_CZ-jirka-medium.onnx"

os.environ.setdefault("EXPERIMENT_PROMPT_MODULE", "prompt_cs")
os.environ.setdefault("EXPERIMENT_TOOLS_MODULE", "tools_cs")
os.environ.setdefault("AGENT_LANG", "cs")
os.environ.setdefault("LOCAL_TTS_MODEL_PATH", str(_DEFAULT_CS_VOICE))

# Delegate to the shared agent module. Importing it runs the module
# body (banner print + cli wiring) but cli.run_app only fires under
# __main__, so we need to invoke it explicitly with our own argv.
if __name__ == "__main__":
    # Make the experiment dir importable so `import prompt_cs` inside
    # agent.py's importlib resolves correctly. agent.py adds this to
    # sys.path itself, but doing it here too is harmless and protects
    # against import ordering surprises.
    if str(_THIS_DIR) not in sys.path:
        sys.path.insert(0, str(_THIS_DIR))

    from livekit.agents import WorkerOptions, cli  # noqa: E402

    import agent as _agent  # noqa: E402

    cli.run_app(
        WorkerOptions(
            entrypoint_fnc=_agent.entrypoint,
            prewarm_fnc=_agent._prewarm,
            initialize_process_timeout=120.0,
            num_idle_processes=0,
            agent_name=_agent.AGENT_NAME,
            job_memory_warn_mb=2000,
            max_retry=2**31 - 1,
        )
    )
