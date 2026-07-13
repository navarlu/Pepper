"""Latency sweep: the same question(s) across models x reasoning effort.

For each (model, effort) cell it runs the question REPS times and reports the
median Time-To-First-Token of the answer, generation-only TTFT, total turn
time, output tokens, and billed reasoning tokens. Writes a Markdown comparison
table to results/latency_sweep.md (and the raw per-rep traces to
results/latency_sweep.jsonl).

Run:
  docs\paper\benchmark\.venv\Scripts\python.exe docs\paper\benchmark\latency_sweep.py
"""
import json
import logging
import statistics
from datetime import datetime, timezone

from openai import OpenAI

import config
from agent import new_state, run_turn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("sweep")

# --- Sweep configuration (edit here) -----------------------------------
SWEEP_MODELS = ["gpt-5.4-nano", "gpt-5.4"]  # small vs "normal"
SWEEP_EFFORTS = ["none"]  # thinking off
REPS = 15
WARMUP = True  # one discarded call per cell to remove cold-start bias
TEST_QUESTIONS = [
    {"id": "greet", "query": "Hi, what can you help me with?"},   # no tool call
    {"id": "room", "query": "Where is room E-107?"},              # one tool call
]

MD_PATH = config.RESULTS_DIR / "latency_sweep.md"
RAW_PATH = config.RESULTS_DIR / "latency_sweep.jsonl"


def median(values):
    vals = [v for v in values if v is not None]
    return round(statistics.median(vals), 1) if vals else None


def run_cell(client, model, effort, question):
    """Run one (model, effort, question) cell REPS times; return the traces."""
    if WARMUP:
        try:
            run_turn(client, model, question["query"], new_state(), effort=effort)
        except Exception as e:
            logger.warning("warmup %s/%s/%s failed: %s", model, effort, question["id"], e)

    traces = []
    for rep in range(REPS):
        trace = run_turn(client, model, question["query"], new_state(), effort=effort)
        trace.update({"model": model, "question_id": question["id"], "rep": rep})
        traces.append(trace)
        logger.info(
            "%s | effort=%s | %s rep %d: ttft=%sms total=%.0fms reason_tok=%s",
            model, effort, question["id"], rep,
            trace["ttft_answer_ms"], trace["total_ms"], trace["reasoning_tokens"],
        )
    return traces


def summarize(traces):
    return {
        "ttft_p50": median([t["ttft_answer_ms"] for t in traces]),
        "gen_p50": median([t["gen_ttft_ms"] for t in traces]),
        "total_p50": median([t["total_ms"] for t in traces]),
        "out_p50": median([t["output_tokens"] for t in traces]),
        "reason_p50": median([t["reasoning_tokens"] for t in traces]),
        "tool": bool(traces[0]["tool_calls"]) if traces else False,
    }


def write_md(rows):
    """rows: list of (question, model, effort, summary_or_None, error_or_None)."""
    out = []
    out.append("# Latency sweep — TTFT by model x reasoning effort\n")
    out.append(
        f"_Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · "
        f"{REPS} reps/cell (median) · warmup={'on' if WARMUP else 'off'} · "
        f"temperature={config.TEMPERATURE}_\n"
    )
    out.append(
        "**TTFT** = time to first *answer* token, end-to-end from turn start "
        "(includes tool round-trips) — the text analog of time-to-first-audio. "
        "**gen** = generation-only first-token latency (tool time excluded). "
        "**total** = whole turn. **reason tok** = billed reasoning tokens "
        "(0 when effort is `none`). Times in ms; all values are medians.\n"
    )

    by_q = {}
    for (q, model, effort, summ, err) in rows:
        by_q.setdefault(q["id"], (q, []))
        by_q[q["id"]][1].append((model, effort, summ, err))

    for qid, (q, cells) in by_q.items():
        tool_hint = "tool call expected" if qid == "room" else "no tool expected"
        out.append(f"\n## `{qid}` — \"{q['query']}\"  _({tool_hint})_\n")
        out.append("| Model | Effort | TTFT | gen | total | out tok | reason tok | tool |")
        out.append("|---|---|--:|--:|--:|--:|--:|:--:|")
        for (model, effort, summ, err) in cells:
            if err:
                out.append(f"| {model} | {effort} | — | — | — | — | — | err |")
            else:
                out.append(
                    f"| {model} | {effort} | {summ['ttft_p50']} | {summ['gen_p50']} | "
                    f"{summ['total_p50']} | {summ['out_p50']} | {summ['reason_p50']} | "
                    f"{'yes' if summ['tool'] else 'no'} |"
                )

    MD_PATH.write_text("\n".join(out) + "\n", encoding="utf-8")


def main():
    if not config.OPENAI_API_KEY:
        raise SystemExit("Set OPENAI_API_KEY in the environment (or a .env file).")

    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    client = OpenAI(api_key=config.OPENAI_API_KEY)

    rows = []
    with open(RAW_PATH, "w", encoding="utf-8") as raw:
        for q in TEST_QUESTIONS:
            for model in SWEEP_MODELS:
                for effort in SWEEP_EFFORTS:
                    try:
                        traces = run_cell(client, model, effort, q)
                        for t in traces:
                            raw.write(json.dumps(t, ensure_ascii=False) + "\n")
                        rows.append((q, model, effort, summarize(traces), None))
                    except Exception as e:  # unsupported combo / API error -> mark, continue
                        logger.exception("cell %s/%s/%s failed", q["id"], model, effort)
                        rows.append((q, model, effort, None, str(e)))

    write_md(rows)
    logger.info("Wrote %s and %s", RAW_PATH, MD_PATH)


if __name__ == "__main__":
    main()
