"""Slim launcher for Pepper demos.

Edit the globals below to choose whether you control the real robot or the
virtual simulator and whether you want the CLI chat or voice experience.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# --- Toggle these values -----------------------------------------------------
REAL = False         # True = physical Pepper, False = virtual robot
RUN_MODE = "cli_chat"  # "cli_chat" or "voice"
# -----------------------------------------------------------------------------

ROOT_DIR = Path(__file__).resolve().parent
SRC_DIR = ROOT_DIR / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))


def _configure_environment():
    target = "real" if REAL else "virtual"
    os.environ["ROBOT_TARGET"] = target
    os.environ.setdefault("BRIDGE_URL", "http://127.0.0.1:5000")
    print(f"[main] ROBOT_TARGET set to {target}")
    print(f"[main] RUN_MODE set to {RUN_MODE}")


def _run_cli_chat():
    from cli_chat import main as chat_main

    chat_main()


def _run_voice():
    from cli_voice import cli, WorkerOptions, entrypoint

    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint))


def main():
    _configure_environment()
    if RUN_MODE == "cli_chat":
        _run_cli_chat()
    elif RUN_MODE == "voice":
        _run_voice()
    else:
        raise SystemExit("RUN_MODE must be 'cli_chat' or 'voice'")


if __name__ == "__main__":
    main()
