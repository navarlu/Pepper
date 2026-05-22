"""Histogram of per-turn TTFA for Conditions A and B.

Companion to ``latency_per_session.py``. Whereas that script reduces every
session to a single descriptive statistic, this script keeps the **turn as
the unit of analysis** and plots the full distribution.

Two figures are produced next to this file:

  1. ``latency_histogram_AB.png`` -- per-turn TTFA for Condition A vs B
     (overlaid histograms).
  2. ``latency_histogram_tool.png`` -- per-turn TTFA split by whether the
     turn involved at least one ``tool_call`` event. Conditions A and B
     are shown as separate subplots, each with tool/no-tool overlaid.

A turn is classified as a *tool* turn iff at least one ``tool_call``
event in the same session shares its ``turn_id``.

TTFA semantics: time from user speech end to the **first** audible
response of any kind (filler "Let me check..." counts). On long tool
turns the audio bridge emits a second ``pepper_first_sound`` for the
real answer after the filler drains; we ignore that and keep the
earliest, since that is what the user actually heard first.

Run from the project root:
    uv run python docs/thesis/experiment/evaluation/latency/latency_histogram.py
"""
from __future__ import annotations

import collections
import json
import pathlib
import re
import sys

import matplotlib.pyplot as plt
import numpy as np
from matplotlib.ticker import MaxNLocator, MultipleLocator


PROJECT_ROOT = pathlib.Path(__file__).resolve()
while not (PROJECT_ROOT / "docker").exists() and PROJECT_ROOT != PROJECT_ROOT.parent:
    PROJECT_ROOT = PROJECT_ROOT.parent

LOG_ROOT = PROJECT_ROOT / "voice-agent/src/experiment/results/experiments_cleaned_manualy"
OUT_DIR = pathlib.Path(__file__).resolve().parent

# Cap displayed range so a couple of huge outliers do not flatten the
# histogram; turns above this are clipped into the last bin.
TTFA_CLIP_SECONDS = 10.0
N_BINS = 30

# Match the palette used in docs/thesis/experiment/results/analysis.ipynb.
COLORS = {"A": "#3b6db7", "B": "#d8702a", "unknown": "#9e9e9e"}
# Tool-vs-no-tool split: keep condition color for "no tool" and use a
# darker shade of the same hue for "tool" so the two subplots stay
# visually grouped with the A/B figure.
TOOL_COLORS = {
    "A": {"no_tool": "#3b6db7", "tool": "#1f3f73"},
    "B": {"no_tool": "#d8702a", "tool": "#8a4416"},
}


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


def per_turn_ttfa_with_tool_flag(events):
    """Return list of (ttfa_seconds, used_tool: bool) for one session.

    TTFA = time from user speech end to the **first** ``pepper_first_sound``
    event for the turn. On long tool turns the audio bridge can emit two
    first-sound events (one for the filler, one for the actual answer
    after the filler drains); we want what the user *first heard*, so we
    keep the earliest. ``vad_user_speech_end`` and ``typed_input`` are
    also kept as first-occurrence for symmetry, though they're emitted
    once per turn in practice.
    """
    by_turn: dict[int, dict] = collections.defaultdict(dict)
    tool_turns: set[int] = set()
    for ev in events:
        et = ev.get("event")
        data = ev.get("data") or {}
        tid = data.get("turn_id")
        if tid is None:
            continue
        if et in ("vad_user_speech_end", "typed_input", "pepper_first_sound"):
            # First occurrence wins -- don't overwrite.
            by_turn[tid].setdefault(et, ev["ts"])
        elif et == "tool_call":
            tool_turns.add(tid)

    out = []
    for tid, ts in by_turn.items():
        start = ts.get("vad_user_speech_end") or ts.get("typed_input")
        first = ts.get("pepper_first_sound")
        if start is not None and first is not None:
            out.append((first - start, tid in tool_turns))
    return out


def collect():
    """Walk all sessions and return per-condition lists of (ttfa, used_tool)."""
    per_cond: dict[str, list[tuple[float, bool]]] = {"A": [], "B": []}
    n_sessions: dict[str, int] = {"A": 0, "B": 0}

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
        turns = per_turn_ttfa_with_tool_flag(events)
        if not turns:
            continue
        per_cond[variant].extend(turns)
        n_sessions[variant] += 1
    return per_cond, n_sessions


def _clip(values):
    return np.clip(np.asarray(values, dtype=float), 0.0, TTFA_CLIP_SECONDS)


def _xlabel(*sample_arrays):
    """X-axis label. Mention the clip only if it actually fired."""
    clipped = any(
        (np.asarray(a, dtype=float) > TTFA_CLIP_SECONDS).any()
        for a in sample_arrays if len(a)
    )
    if clipped:
        return f"TTFA [s]  (clipped at {TTFA_CLIP_SECONDS:.0f}s)"
    return "TTFA [s]"


def plot_ab(per_cond, n_sessions, out_path: pathlib.Path):
    raw_a = [v for v, _ in per_cond["A"]]
    raw_b = [v for v, _ in per_cond["B"]]
    a = _clip(raw_a)
    b = _clip(raw_b)
    bins = np.linspace(0.0, TTFA_CLIP_SECONDS, N_BINS + 1)

    fig, ax = plt.subplots(figsize=(8, 5))
    ax.hist(a, bins=bins, alpha=0.6, label=f"A (n={len(a)} turns, {n_sessions['A']} sessions)", color=COLORS["A"])
    ax.hist(b, bins=bins, alpha=0.6, label=f"B (n={len(b)} turns, {n_sessions['B']} sessions)", color=COLORS["B"])
    ax.set_xlabel(_xlabel(raw_a, raw_b))
    ax.set_ylabel("Turns")
    ax.set_title("Per-turn TTFA distribution: Condition A vs B")
    ax.xaxis.set_major_locator(MultipleLocator(1))
    ax.yaxis.set_major_locator(MaxNLocator(integer=True))
    ax.legend()
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"wrote {out_path}")


def plot_tool_split(per_cond, out_path: pathlib.Path):
    bins = np.linspace(0.0, TTFA_CLIP_SECONDS, N_BINS + 1)
    fig, axes = plt.subplots(1, 2, figsize=(13, 5), sharey=True)

    for ax, variant in zip(axes, ("A", "B")):
        raw_tool = [v for v, used in per_cond[variant] if used]
        raw_notool = [v for v, used in per_cond[variant] if not used]
        tool_vals = _clip(raw_tool)
        notool_vals = _clip(raw_notool)
        ax.hist(
            notool_vals, bins=bins, alpha=0.6,
            color=TOOL_COLORS[variant]["no_tool"],
            label=f"no tool (n={len(notool_vals)})",
        )
        ax.hist(
            tool_vals, bins=bins, alpha=0.75,
            color=TOOL_COLORS[variant]["tool"],
            label=f"tool   (n={len(tool_vals)})",
        )
        title = f"Condition {variant}"
        if len(tool_vals) and len(notool_vals):
            title += (
                f"\nmedian: tool={np.median(tool_vals):.2f}s, "
                f"no-tool={np.median(notool_vals):.2f}s"
            )
        ax.set_title(title)
        ax.set_xlabel(_xlabel(raw_tool, raw_notool))
        ax.xaxis.set_major_locator(MultipleLocator(1))
        ax.yaxis.set_major_locator(MaxNLocator(integer=True))
        ax.legend()
        ax.grid(True, alpha=0.3)
    axes[0].set_ylabel("Turns")
    fig.suptitle("Per-turn TTFA: tool vs no-tool turns")
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)
    print(f"wrote {out_path}")


def print_summary(per_cond, n_sessions):
    print()
    print(f"{'Cond':<6}{'Subset':<10}{'n_turns':>10}{'median':>10}{'mean':>10}{'p90':>10}{'max':>10}")
    print("-" * 66)
    for variant in ("A", "B"):
        for label, subset in (
            ("all",     [v for v, _ in per_cond[variant]]),
            ("tool",    [v for v, used in per_cond[variant] if used]),
            ("no-tool", [v for v, used in per_cond[variant] if not used]),
        ):
            if not subset:
                print(f"{variant:<6}{label:<10}{'0':>10}{'-':>10}{'-':>10}{'-':>10}{'-':>10}")
                continue
            arr = np.asarray(subset, dtype=float)
            print(
                f"{variant:<6}{label:<10}{len(arr):>10d}"
                f"{np.median(arr):>10.2f}{arr.mean():>10.2f}"
                f"{np.quantile(arr, 0.9):>10.2f}{arr.max():>10.2f}"
            )
        print(f"  (sessions in condition {variant}: {n_sessions[variant]})")


def main() -> int:
    if not LOG_ROOT.exists():
        print(f"ERROR: log root not found: {LOG_ROOT}", file=sys.stderr)
        return 1

    per_cond, n_sessions = collect()
    if not any(per_cond.values()):
        print("ERROR: no TTFA samples found", file=sys.stderr)
        return 1

    print_summary(per_cond, n_sessions)
    plot_ab(per_cond, n_sessions, OUT_DIR / "latency_histogram_AB.png")
    plot_tool_split(per_cond, OUT_DIR / "latency_histogram_tool.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
