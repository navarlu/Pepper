# ReceptionistBench (offline)

Phase-1 scaffold for the Humanoids paper's offline benchmark: **does a small
LLM match a big one on a tool-grounded receptionist task, given the same
prompt and tools?** Text-in / text-out, no robot, no speech.

This first cut is deliberately minimal — **one tool** (`find_room`), a **simple
system prompt**, and a **runner** that executes every question through each
model in the ladder — so we can smoke-test the whole tool-calling → trace →
results loop before authoring the full item set and adding the other
categories.

## Layout

```
docs/paper/benchmark/
  config.py            # models, paths, decoding — edit globals here (no CLI)
  prompt.py            # SYSTEM_PROMPT (trimmed from production)
  tools/
    find_room.py       # the one tool: callable + JSON schema + registry
  data/
    questions.jsonl    # benchmark items + ground truth (one JSON object/line)
    snapshots/         # frozen endpoint responses (phase 2, network tools)
  runner.py            # driver: model x question -> tool-calling loop -> trace
  chat.py              # interactive REPL to talk to the agent + watch tool calls
  results/             # per-model output: <model>.jsonl (git-ignored content)
```

## Setup

This code has its **own isolated venv** at `docs/paper/benchmark/.venv`
(separate from the root project). Create it from the repo root:

```
uv venv docs/paper/benchmark/.venv
uv pip install --python docs/paper/benchmark/.venv -r docs/paper/benchmark/requirements.txt
```

Set your key (or put it in a `.env` file at the repo root):

```
$env:OPENAI_API_KEY = "sk-..."      # PowerShell
export OPENAI_API_KEY=sk-...        # bash
```

## Run

Use this venv's interpreter directly (there is no `pyproject.toml`, so
`uv run` would pick the root project — call the venv's `python.exe` instead):

```
docs\paper\benchmark\.venv\Scripts\python.exe docs\paper\benchmark\runner.py
```

This writes one `results/<model>.jsonl` per model in `config.MODELS`
(`gpt-5.4-nano`, `gpt-5.4-mini`, `gpt-5.4`). The loop is **streaming**
(`agent.py`), so each trace records:

| field | meaning |
|---|---|
| `tool_calls` | tools called + parsed args (in order) |
| `final_text` | the user-facing answer |
| `ttft_answer_ms` | **Time-To-First-Token of the answer**, end-to-end from the start of the turn — includes any tool round-trips. The text analog of the thesis's time-to-first-audio. First-token is the answer's first `content` token; tool-call tokens never count. |
| `gen_ttft_ms` | request-sent → first answer token within the answering step (pure generation latency, tool time excluded) |
| `total_ms` | whole turn wall-clock |
| `input_tokens` / `output_tokens` | prompt / completion tokens, summed over hops |
| `hops` | model round-trips in the turn |

## Reasoning effort

The models are gpt-5-series *reasoning* models. `config.REASONING_EFFORT`
controls how much they think before answering — the dominant latency knob:

- values (gpt-5.4): `"none"` (thinking **off**) · `"low"` · `"medium"` · `"high"` · `"xhigh"`
- default `"low"` (latency-sensitive receptionist); set `None` to omit and use the model default.

Applied in `agent.py`; `run_turn(..., effort=...)` overrides it per call.

## Latency sweep

Measure TTFT for the same question across every model × effort (including
thinking off) and get a Markdown comparison table:

```
docs\paper\benchmark\.venv\Scripts\python.exe docs\paper\benchmark\latency_sweep.py
```

Writes `results/latency_sweep.md` (the table) and `results/latency_sweep.jsonl`
(raw per-rep traces). Edit the sweep grid (`SWEEP_MODELS`, `SWEEP_EFFORTS`,
`REPS`, `TEST_QUESTIONS`) at the top of `latency_sweep.py`. Unsupported
model/effort combos are caught and shown as `err` rather than aborting the run.

## Chat with the agent

To talk to the agent interactively and watch every tool call:

```
docs\paper\benchmark\.venv\Scripts\python.exe docs\paper\benchmark\chat.py
```

Each turn prints the tool calls (`[tool] find_room(room='E-107')`), their
returned payloads, and the final reply plus token/latency. In-chat commands:
`/model <name>` (switch model, resets), `/reset`, `/tools`, `/help`,
`/exit`.

## Ground-truth item schema (`data/questions.jsonl`)

| field | meaning |
|---|---|
| `id` | stable item id (e.g. `room-001`) |
| `category` | `room_directions`, `out_of_scope`, … |
| `query` | the user utterance (text) |
| `expected_behavior` | `answer` \| `clarify` \| `refuse` |
| `expected_tools` | ordered tool names expected (`[]` for refuse) |
| `expected_args` | expected args per tool + (later) match rule |
| `gold_answer` | canonical answer / expected outcome |
| `answer_criteria` | required substrings/facts the answer must contain |
| `difficulty` | `easy` \| `medium` \| `hard` |

## Not here yet (next steps)

- **Scorer** — tool-selection F1, arg accuracy, behavior classification,
  answer-criteria match (programmatic) + a Claude LLM-judge for free-text
  correctness / faithfulness.
- **More tools + categories** — staff lookup, schedule, canteen, time,
  compositional, ambiguous→clarify, ASR-noise twins; then RAG (phase 2, needs
  the FEL corpus + Weaviate).
- **Frozen endpoint snapshots** for the network-backed tools, stored under
  `data/snapshots/` so gold answers stay reproducible.
- **Analysis** — per-category paired CIs + McNemar, `pass^k`, cost-per-
  successful-task Pareto.
