"""Dispatcher — picks which student team's bundle is active.

Layout
------
Each team lives in its own folder under `student_bundles/`:

    student_bundles/
        team1/
            system_prompt.py     # defines SYSTEM_PROMPT = "..."
            tools.py             # defines TOOLS = [{"schema": ..., "function": ...}]
        team2/
            ...

Files inside a team folder use the exact same format as the
teaching/tool-playground CLI, so you can paste a student's submission
verbatim.

How to switch teams
-------------------
Change the `ACTIVE_TEAM` string below to the folder name (e.g. "team1"),
then restart the voice-agent container:

    docker compose -f docker/docker-compose.yml restart voice-agent

Leaving `ACTIVE_TEAM = ""` loads nothing — the agent falls back to its
default prompt and tool set.
"""

from __future__ import annotations

import importlib.util
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("voice-agent.student_bundle")


# ─── Change this to switch teams ─────────────────────────────────────
ACTIVE_TEAM = "example"    # e.g. "team1", "example", ""  (empty = disabled)
# ─────────────────────────────────────────────────────────────────────


SYSTEM_PROMPT: str | None = None
TOOLS: list[dict] = []


def _load_module(path: Path, fq_name: str) -> dict[str, Any] | None:
    """Load `path` as a standalone module, return its namespace dict."""
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(fq_name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return vars(module)


def _load_active_team() -> None:
    """Populate `SYSTEM_PROMPT` and `TOOLS` globals from the active team folder."""
    global SYSTEM_PROMPT, TOOLS

    if not ACTIVE_TEAM:
        logger.info("student_bundle_no_active_team")
        return

    team_dir = Path(__file__).parent / "student_bundles" / ACTIVE_TEAM
    if not team_dir.is_dir():
        logger.warning("student_bundle_team_missing team=%s path=%s", ACTIVE_TEAM, team_dir)
        return

    try:
        ns = _load_module(team_dir / "system_prompt.py", f"student_bundles.{ACTIVE_TEAM}.system_prompt")
        if ns is not None:
            prompt = ns.get("SYSTEM_PROMPT")
            if isinstance(prompt, str) and prompt.strip():
                SYSTEM_PROMPT = prompt
    except Exception as exc:
        logger.exception("student_bundle_prompt_load_failed team=%s error=%s", ACTIVE_TEAM, exc)

    try:
        ns = _load_module(team_dir / "tools.py", f"student_bundles.{ACTIVE_TEAM}.tools")
        if ns is not None:
            raw = ns.get("TOOLS") or []
            if isinstance(raw, list):
                TOOLS = raw
    except Exception as exc:
        logger.exception("student_bundle_tools_load_failed team=%s error=%s", ACTIVE_TEAM, exc)

    logger.info(
        "student_bundle_loaded team=%s has_prompt=%s tool_count=%d",
        ACTIVE_TEAM, SYSTEM_PROMPT is not None, len(TOOLS),
    )


_load_active_team()
