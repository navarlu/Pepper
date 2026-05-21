"""Compute OpenAI cost for Variant B sessions.

For each B session:
  - read start ts from events.jsonl 'header' event (epoch UTC)
  - read duration_seconds from metrics.json
  - find every CSV minute-bin overlapping [start, end] and attribute tokens
    by overlap fraction (handles same-minute overlap between sessions)
  - compute cost per model (LLM / STT / TTS)

Outputs a per-session table + grand totals + hourly rate.
"""

import csv
import json
from pathlib import Path
from datetime import datetime, timezone

RESULTS_DIR = Path(
    "/home/lucas/Projects/FEL/Pepper/voice-agent/src/experiment/results/"
    "experiments_cleaned_manualy/2026-05-18"
)
CSV_PATH = Path(
    "/home/lucas/Projects/FEL/Pepper/docs/thesis/experiment/cost/"
    "completions_usage_2026-05-18_2026-05-18.csv"
)

USD_TO_CZK = 21.0

# OpenAI pricing per 1M tokens (USD)
PRICING = {
    "gpt-4o-mini-2024-07-18": {  # LLM
        "input_text": 0.15,
        "cached_input": 0.075,
        "output_text": 0.60,
    },
    "gpt-4o-mini-transcribe": {  # STT
        "input_text": 1.25,
        "input_audio": 3.00,
        "output_text": 5.00,
    },
    "gpt-4o-mini-tts": {  # TTS
        "input_text": 0.60,
        "output_audio": 12.00,
    },
}

MODEL_GROUP = {
    "gpt-4o-mini-2024-07-18": "LLM",
    "gpt-4o-mini-transcribe": "STT",
    "gpt-4o-mini-tts": "TTS",
}


def load_sessions():
    sessions = []
    for d in sorted(RESULTS_DIR.iterdir()):
        if "streamingB" not in d.name:
            continue
        events = d / "events.jsonl"
        metrics = d / "metrics.json"
        if not events.exists():
            continue
        start_ts = None
        last_ts = None
        conv_id = None
        with events.open() as f:
            for line in f:
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = float(ev.get("ts", 0))
                if start_ts is None and ev.get("event") == "header":
                    start_ts = ts
                    conv_id = ev.get("conv_id")
                if ts:
                    last_ts = ts
        if start_ts is None:
            continue
        if metrics.exists():
            m = json.loads(metrics.read_text())
            duration = float(m["duration_seconds"])
            conv_id = m.get("conv_id", conv_id)
        else:
            duration = (last_ts or start_ts) - start_ts
        sessions.append({
            "name": d.name,
            "conv_id": conv_id or "?",
            "start_ts": start_ts,
            "end_ts": start_ts + duration,
            "duration_s": duration,
        })
    return sessions


def cost_for_row(model, tokens):
    """tokens is the per-row dict; returns USD cost for that row's tokens.
    For LLM splits input into cached (cheaper) vs uncached."""
    p = PRICING[model]
    if model == "gpt-4o-mini-2024-07-18":
        cached = tokens["input_cached_tokens"]
        uncached = tokens["input_tokens"] - cached
        return (
            uncached * p["input_text"] / 1e6
            + cached * p["cached_input"] / 1e6
            + tokens["output_tokens"] * p["output_text"] / 1e6
        )
    if model == "gpt-4o-mini-transcribe":
        return (
            tokens["input_text_tokens"] * p["input_text"] / 1e6
            + tokens["input_audio_tokens"] * p["input_audio"] / 1e6
            + tokens["output_text_tokens"] * p["output_text"] / 1e6
        )
    if model == "gpt-4o-mini-tts":
        return (
            tokens["input_text_tokens"] * p["input_text"] / 1e6
            + tokens["output_audio_tokens"] * p["output_audio"] / 1e6
        )
    raise ValueError(model)


def load_csv_rows():
    rows = []
    with CSV_PATH.open() as f:
        for r in csv.DictReader(f):
            if not r.get("model"):
                continue
            try:
                row = {
                    "start": int(r["start_time"]),
                    "end": int(r["end_time"]),
                    "model": r["model"],
                    "input_tokens": float(r["input_tokens"] or 0),
                    "output_tokens": float(r["output_tokens"] or 0),
                    "input_cached_tokens": float(r["input_cached_tokens"] or 0),
                    "input_text_tokens": float(r["input_text_tokens"] or 0),
                    "output_text_tokens": float(r["output_text_tokens"] or 0),
                    "input_audio_tokens": float(r["input_audio_tokens"] or 0),
                    "output_audio_tokens": float(r["output_audio_tokens"] or 0),
                }
            except (KeyError, ValueError):
                continue
            rows.append(row)
    return rows


def scaled_tokens(row, frac):
    return {
        "input_tokens": row["input_tokens"] * frac,
        "output_tokens": row["output_tokens"] * frac,
        "input_cached_tokens": row["input_cached_tokens"] * frac,
        "input_text_tokens": row["input_text_tokens"] * frac,
        "output_text_tokens": row["output_text_tokens"] * frac,
        "input_audio_tokens": row["input_audio_tokens"] * frac,
        "output_audio_tokens": row["output_audio_tokens"] * frac,
    }


def fmt_ts(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M:%S")


def main():
    sessions = load_sessions()
    rows = load_csv_rows()

    print(f"Found {len(sessions)} Variant B sessions")
    print(f"CSV rows with model: {len(rows)}\n")

    per_session = []
    for s in sessions:
        costs = {"LLM": 0.0, "STT": 0.0, "TTS": 0.0}
        for row in rows:
            overlap = max(0, min(row["end"], s["end_ts"]) - max(row["start"], s["start_ts"]))
            if overlap <= 0:
                continue
            frac = overlap / (row["end"] - row["start"])  # bin is 60s
            grp = MODEL_GROUP[row["model"]]
            costs[grp] += cost_for_row(row["model"], scaled_tokens(row, frac))
        s["costs"] = costs
        s["total"] = sum(costs.values())
        per_session.append(s)

    # ---- per-session table ----
    hdr = (
        f"{'session':<38} {'conv':<5} {'start UTC':<10} "
        f"{'dur(s)':>7} "
        f"{'STT $':>8} {'LLM $':>8} {'TTS $':>8} {'total $':>9}  "
        f"{'STT Kč':>8} {'LLM Kč':>8} {'TTS Kč':>8} {'total Kč':>10}"
    )
    print(hdr)
    print("-" * len(hdr))
    tot = {"LLM": 0.0, "STT": 0.0, "TTS": 0.0}
    tot_dur = 0.0
    for s in per_session:
        c = s["costs"]
        tot["LLM"] += c["LLM"]
        tot["STT"] += c["STT"]
        tot["TTS"] += c["TTS"]
        tot_dur += s["duration_s"]
        r = USD_TO_CZK
        print(
            f"{s['name']:<38} {s['conv_id']:<5} {fmt_ts(s['start_ts']):<10} "
            f"{s['duration_s']:>7.1f} "
            f"{c['STT']:>8.4f} {c['LLM']:>8.4f} {c['TTS']:>8.4f} {s['total']:>9.4f}  "
            f"{c['STT']*r:>8.3f} {c['LLM']*r:>8.3f} {c['TTS']*r:>8.3f} {s['total']*r:>10.3f}"
        )

    total_cost = sum(tot.values())
    r = USD_TO_CZK
    print("-" * len(hdr))
    print(
        f"{'TOTAL':<38} {'':<5} {'':<10} "
        f"{tot_dur:>7.1f} "
        f"{tot['STT']:>8.4f} {tot['LLM']:>8.4f} {tot['TTS']:>8.4f} {total_cost:>9.4f}  "
        f"{tot['STT']*r:>8.3f} {tot['LLM']*r:>8.3f} {tot['TTS']*r:>8.3f} {total_cost*r:>10.3f}"
    )

    # ---- summary ----
    print(f"\n=== Summary (USD / CZK @ {USD_TO_CZK:.2f}) ===")
    print(f"Sessions:           {len(per_session)}")
    print(f"Total talk time:    {tot_dur:.1f} s  ({tot_dur/60:.2f} min, {tot_dur/3600:.3f} h)")
    print(f"Total STT cost:     ${tot['STT']:.4f}   ({tot['STT']*r:.3f} Kč)")
    print(f"Total LLM cost:     ${tot['LLM']:.4f}   ({tot['LLM']*r:.3f} Kč)")
    print(f"Total TTS cost:     ${tot['TTS']:.4f}   ({tot['TTS']*r:.3f} Kč)")
    print(f"Total cost:         ${total_cost:.4f}   ({total_cost*r:.3f} Kč)")
    print(
        f"Share of total:     STT {tot['STT']/total_cost*100:5.1f}%   "
        f"LLM {tot['LLM']/total_cost*100:5.1f}%   "
        f"TTS {tot['TTS']/total_cost*100:5.1f}%"
    )
    print(f"Per-session avg:    ${total_cost/len(per_session):.4f}   ({total_cost/len(per_session)*r:.3f} Kč)")
    print(f"Per-minute rate:    ${total_cost / (tot_dur/60):.4f} / min   ({total_cost / (tot_dur/60)*r:.3f} Kč / min)")
    print(f"Per-hour rate:      ${total_cost / (tot_dur/3600):.4f} / h   ({total_cost / (tot_dur/3600)*r:.3f} Kč / h)")


if __name__ == "__main__":
    main()
