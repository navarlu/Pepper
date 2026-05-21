"""Per-session TTFA comparison for Conditions A and B.

Companion to ``latency.py``, which pools every turn from every session into
a single per-condition sample. That presentation treats every turn as an
independent observation and so suffers from pseudo-replication: turns are
nested within sessions and within visitors, and per-condition turn counts
are dominated by the few longest sessions.

This script uses the **session as the unit of analysis** instead:

  1. For each session, compute the median TTFA across its turns.
  2. Aggregate those per-session medians per condition (Median, Mean,
     Q1, Q3, IQR, Min, Max).

With n=10 sessions per condition, the per-condition descriptives are
genuinely comparable (one observation per visitor, not one per turn).

Run from the project root:
    uv run python docs/thesis/experiment/evaluation/latency/latency_per_session.py
"""
from __future__ import annotations

import collections
import json
import pathlib
import re
import statistics
import sys


# Resolve the project root by walking up until we hit the docker/ directory,
# rather than counting parents (which is brittle if this file is moved).
PROJECT_ROOT = pathlib.Path(__file__).resolve()
while not (PROJECT_ROOT / "docker").exists() and PROJECT_ROOT != PROJECT_ROOT.parent:
    PROJECT_ROOT = PROJECT_ROOT.parent

LOG_ROOT = PROJECT_ROOT / "voice-agent/src/experiment/results/experiments_cleaned_manualy"


def walk_events(jl_path: pathlib.Path):
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
    by_turn = collections.defaultdict(dict)
    for ev in events:
        et = ev.get("event")
        data = ev.get("data") or {}
        tid = data.get("turn_id")
        if tid is None:
            continue
        if et in ("vad_user_speech_end", "typed_input", "pepper_first_sound"):
            by_turn[tid][et] = ev["ts"]
    out = []
    for ts in by_turn.values():
        start = ts.get("vad_user_speech_end") or ts.get("typed_input")
        first = ts.get("pepper_first_sound")
        if start is not None and first is not None:
            out.append(first - start)
    return out


def quartiles(values):
    """Return (Q1, Q2, Q3). NaNs if fewer than 2 samples."""
    if len(values) < 2:
        nan = float("nan")
        return (nan, nan, nan)
    q1, q2, q3 = statistics.quantiles(values, n=4)
    return q1, q2, q3


def print_table(header, rows):
    widths = [max(len(r[i]) for r in [header] + rows) for i in range(len(header))]
    def fmt(row):
        return "  ".join(c.rjust(w) for c, w in zip(row, widths))
    print(fmt(header))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(fmt(r))


def main() -> int:
    if not LOG_ROOT.exists():
        print(f"ERROR: log root not found: {LOG_ROOT}", file=sys.stderr)
        return 1

    sessions = []  # list of dicts collected from every session directory
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
        ttfas = per_turn_ttfa(events)
        if not ttfas:
            continue
        sessions.append({
            "session": d.name,
            "variant": variant,
            "n_turns": len(ttfas),
            "median": statistics.median(ttfas),
            "mean": statistics.mean(ttfas),
            "min": min(ttfas),
            "max": max(ttfas),
        })

    # ---- Per-session table ----
    print("Per-session TTFA (seconds) -- one row per session, sorted by condition then session name")
    print()
    sessions_sorted = sorted(sessions, key=lambda s: (s["variant"], s["session"]))
    header = ["Session", "Cond", "Turns", "Median", "Mean", "Min", "Max"]
    rows = [[
        s["session"], s["variant"], str(s["n_turns"]),
        f"{s['median']:.2f}", f"{s['mean']:.2f}",
        f"{s['min']:.2f}", f"{s['max']:.2f}",
    ] for s in sessions_sorted]
    print_table(header, rows)

    print()
    print("=" * 70)
    print()

    # ---- Per-condition aggregate over per-session medians ----
    n_a = sum(1 for s in sessions if s["variant"] == "A")
    n_b = sum(1 for s in sessions if s["variant"] == "B")
    print(f"Per-condition aggregate of per-session medians (unit of analysis = session)")
    print(f"n_A = {n_a} sessions,  n_B = {n_b} sessions")
    print()
    header2 = ["Cond", "Sessions", "Median", "Mean", "Q1", "Q3", "IQR", "Min", "Max"]
    rows2 = []
    for variant in ("A", "B"):
        sess_medians = [s["median"] for s in sessions if s["variant"] == variant]
        if not sess_medians:
            rows2.append([variant, "0", "-", "-", "-", "-", "-", "-", "-"])
            continue
        q1, _, q3 = quartiles(sess_medians)
        rows2.append([
            variant,
            str(len(sess_medians)),
            f"{statistics.median(sess_medians):.2f}",
            f"{statistics.mean(sess_medians):.2f}",
            f"{q1:.2f}",
            f"{q3:.2f}",
            f"{(q3 - q1):.2f}",
            f"{min(sess_medians):.2f}",
            f"{max(sess_medians):.2f}",
        ])
    print_table(header2, rows2)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
