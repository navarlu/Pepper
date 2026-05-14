"""Tiny atomic read/merge/write helpers for services/data/state.json.

The bridge owns the canonical implementation in `robot/src/bridge.py`
(`_read_runtime_state` / `_write_runtime_state`). That code runs on
Python 2 inside the bridge container, so we keep a Py3 copy here
rather than sharing a module across version boundaries. The contract
is the JSON file, not Python imports.

Writers always merge — they read the current state, update specific
keys, and atomically rename through a `.tmp`. Concurrent writers
(bridge, loop_launcher) are safe as long as everyone uses this
pattern.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

# Resolve the same path the bridge writes to. Override with STATE_FILE
# env var for tests / alt setups.
_DEFAULT_STATE_FILE = (
    Path(__file__).resolve().parents[3] / "services" / "data" / "state.json"
)
STATE_FILE = Path(os.environ.get("STATE_FILE") or _DEFAULT_STATE_FILE)


def read_runtime_state() -> dict:
    """Return the current state.json contents, or {} on any error.

    Never raises — a missing or malformed file is treated as empty
    state. Callers should `.get()` keys with sensible defaults.
    """
    try:
        with STATE_FILE.open("r") as fh:
            data = json.load(fh) or {}
            return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"[runtime_state] read failed path={STATE_FILE} err={exc}", flush=True)
        return {}


def write_runtime_state(patch: dict) -> None:
    """Merge `patch` into state.json atomically.

    Reads the existing file, updates the keys in `patch`, stamps
    `updatedAt`, writes a `.tmp`, then renames over the target. Same
    pattern as the bridge's `_write_runtime_state` so the two writers
    don't clobber each other's keys.
    """
    try:
        state = read_runtime_state()
        state.update(patch)
        state["updatedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        parent = STATE_FILE.parent
        if not parent.is_dir():
            parent.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(STATE_FILE.suffix + ".tmp")
        with tmp.open("w") as fh:
            json.dump(state, fh, indent=2)
        os.replace(tmp, STATE_FILE)
        try:
            os.chmod(STATE_FILE, 0o666)
        except OSError:
            pass
    except Exception as exc:
        print(f"[runtime_state] write failed path={STATE_FILE} err={exc}", flush=True)
