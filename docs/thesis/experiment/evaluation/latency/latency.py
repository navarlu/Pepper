"""Print the TTFA-per-turn comparison table for Conditions A and B.

Reads every cleaned session under
  voice-agent/src/experiment/results/experiments_cleaned_manualy/
walks each events.jsonl, computes per-turn time-to-first-audio
(end of user utterance -> Pepper starts speaking), and prints
median / mean / min / max per condition. No external dependencies
beyond the Python standard library.

Run from the project root:
    uv run python docs/thesis/experiment/results/headline_table.py
"""
from __future__ import annotations

import collections
import json
import pathlib
import re
import statistics
import sys

# Path to the cleaned session corpus. The script file lives at
# docs/thesis/experiment/results/headline_table.py, so we go up four
# parents to reach the project root before descending into voice-agent.
LOG_ROOT = (
    pathlib.Path(__file__).resolve().parents[4]
    / "voice-agent/src/experiment/results/experiments_cleaned_manualy"
)


def walk_events(jl_path: pathlib.Path):
    """Read a session's events.jsonl into a list of dicts.

    The format is one JSON object per line. Malformed lines and blanks
    are skipped silently so a single corrupt event does not abort the
    whole session.
    """
    out = []
    for ln in jl_path.read_text(errors="replace").splitlines():
        if not ln.strip():
            continue
        try:
            out.append(json.loads(ln))
        except json.JSONDecodeError:
            continue
    return out


def per_turn_ttfa(events):
    """Compute per-turn time-to-first-audio (TTFA) for one session.

    TTFA is measured between:
      - turn start: either vad_user_speech_end (end of spoken input)
                    or typed_input (operator-typed input)
      - turn end:   pepper_first_sound (first PCM frame submitted to
                    NAOqi's audio device)

    Events are grouped by turn_id; turns missing either marker are
    skipped. Returns a list of float seconds, one per valid turn.
    """
    # Group every relevant event into a per-turn timestamp dict.
    by_turn = collections.defaultdict(dict)
    for ev in events:
        et = ev.get("event")
        data = ev.get("data") or {}
        tid = data.get("turn_id")
        if tid is None:
            continue
        if et in ("vad_user_speech_end", "typed_input", "pepper_first_sound"):
            by_turn[tid][et] = ev["ts"]

    # For each turn, take whichever start event fired (speech or typed)
    # and pair it with the moment Pepper first produced sound.
    out = []
    for ts in by_turn.values():
        start = ts.get("vad_user_speech_end") or ts.get("typed_input")
        first = ts.get("pepper_first_sound")
        if start is not None and first is not None:
            out.append(first - start)
    return out


def main() -> int:
    # Defensive guard: nothing else is meaningful if the corpus is missing.
    if not LOG_ROOT.exists():
        print(f"ERROR: log root not found: {LOG_ROOT}", file=sys.stderr)
        return 1

    # Aggregate per-turn TTFAs and a session count for each condition.
    per_cond_turns: dict[str, list[float]] = {"A": [], "B": []}
    per_cond_sessions: dict[str, int] = {"A": 0, "B": 0}

    # Walk every session directory. Names look like:
    #   student63_streamingB_172410
    # The (A|B) letter is the condition; the trailing HHMMSS is the
    # session start time.
    for d in sorted(LOG_ROOT.glob("*/student*_streaming*_*")):
        if not d.is_dir():
            continue
        m = re.match(r"student(\d+)_streaming([AB])_(\d{6})$", d.name)
        if not m:
            continue
        variant = m.group(2)
        jl = d / "events.jsonl"
        if not jl.exists():
            continue
        events = walk_events(jl)
        if not events:
            continue
        per_cond_sessions[variant] += 1
        per_cond_turns[variant].extend(per_turn_ttfa(events))

    # Build the table rows: one row per condition.
    rows = []
    header = ["Condition", "Sessions", "Turns", "Median (s)", "Mean (s)", "Min (s)", "Max (s)"]
    for variant in ("A", "B"):
        vals = per_cond_turns[variant]
        if not vals:
            # No valid TTFA samples in this condition (e.g. all sessions
            # somehow lacked a pepper_first_sound). Emit a placeholder.
            rows.append([f"{variant}", str(per_cond_sessions[variant]), "0",
                         "—", "—", "—", "—"])
            continue
        rows.append([
            variant,
            str(per_cond_sessions[variant]),
            str(len(vals)),
            f"{statistics.median(vals):.2f}",
            f"{statistics.mean(vals):.2f}",
            f"{min(vals):.2f}",
            f"{max(vals):.2f}",
        ])

    # Compute column widths so the printed table aligns regardless of value sizes.
    widths = [max(len(r[i]) for r in [header] + rows) for i in range(len(header))]

    def fmt(row):
        # Right-align each cell to its column width and join with two spaces.
        return "  ".join(c.rjust(w) for c, w in zip(row, widths))

    # Print the table.
    print("Per-turn TTFA (end of visitor utterance -> Pepper starts speaking)")
    print()
    print(fmt(header))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(fmt(r))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
