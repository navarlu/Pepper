"""get_time: current local time in Europe/Prague."""

from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from livekit.agents import RunContext, function_tool

from tools._common import _run_main


@function_tool
async def get_time(context: RunContext) -> str:
    """Return the current local time. Use only when the user explicitly asks
    what time it is.
    """
    del context
    print("  [tool] get_time()")
    now = datetime.now(ZoneInfo("Europe/Prague"))
    return now.strftime("%Y-%m-%d %H:%M %Z")


if __name__ == "__main__":
    _run_main(get_time(None))
