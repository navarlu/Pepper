"""Production / live voice-agent code.

Long-running Pepper deployment — the standard receptionist agent that
runs for days at a time. Sibling of `src/experiment/`, which holds the
parallel student-study setup. Both share `voice-agent/src/` as the
import root; experiment imports reusable pieces from here via
`from src.live.X import …`.

Entry point: `python -m voice-agent.src.live.agent dev`.
"""

from __future__ import annotations
