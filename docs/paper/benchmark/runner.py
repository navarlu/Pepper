"""Offline benchmark runner.

Runs every question in data/questions.jsonl through each model in
config.MODELS using the shared streaming loop (agent.run_turn), recording a
full trace per item: tool calls + args, final answer, token counts, and
latency including Time-To-First-Token of the answer. Results are written to
results/<model>.jsonl. Scoring is a separate step (added next).

Run:  docs\paper\benchmark\.venv\Scripts\python.exe docs\paper\benchmark\runner.py
"""
import json
import logging
from datetime import datetime, timezone

from openai import OpenAI

import config
from agent import new_state, run_turn

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("runner")


def load_questions():
    items = []
    with open(config.QUESTIONS_FILE, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    logger.info("Loaded %d questions from %s", len(items), config.QUESTIONS_FILE)
    return items


def run_one(client, model, question):
    """Run a single question through the shared turn loop; return a trace."""
    trace = run_turn(client, model, question["query"], new_state())
    trace.update({
        "id": question["id"],
        "category": question.get("category"),
        "query": question["query"],
        "model": model,
        "expected_tools": question.get("expected_tools", []),
        "expected_behavior": question.get("expected_behavior"),
        "gold_answer": question.get("gold_answer"),
        "answer_criteria": question.get("answer_criteria", []),
    })
    logger.info(
        "[%s] %s ttft=%sms total=%.0fms tools=%s",
        model, question["id"], trace["ttft_answer_ms"], trace["total_ms"],
        [t["name"] for t in trace["tool_calls"]],
    )
    return trace


def main():
    if not config.OPENAI_API_KEY:
        raise SystemExit("Set OPENAI_API_KEY in the environment (or a .env file).")

    config.RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    client = OpenAI(api_key=config.OPENAI_API_KEY)
    questions = load_questions()

    for model in config.MODELS:
        out_path = config.RESULTS_DIR / f"{model}.jsonl"
        logger.info("=== Running model %s -> %s ===", model, out_path)
        with open(out_path, "w", encoding="utf-8") as out:
            for q in questions:
                try:
                    trace = run_one(client, model, q)
                except Exception as e:  # best-effort: log, never silently drop an item
                    logger.exception("[%s] %s FAILED: %s", model, q["id"], e)
                    trace = {"id": q["id"], "model": model, "error": str(e)}
                out.write(json.dumps(trace, ensure_ascii=False) + "\n")
        logger.info("Wrote %s", out_path)

    logger.info("Done at %s", datetime.now(timezone.utc).isoformat())


if __name__ == "__main__":
    main()
