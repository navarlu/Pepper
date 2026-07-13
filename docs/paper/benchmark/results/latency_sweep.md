# Latency sweep — TTFT by model x reasoning effort

_Generated 2026-07-09 11:06 UTC · 15 reps/cell (median) · warmup=on · temperature=0.2_

**TTFT** = time to first *answer* token, end-to-end from turn start (includes tool round-trips) — the text analog of time-to-first-audio. **gen** = generation-only first-token latency (tool time excluded). **total** = whole turn. **reason tok** = billed reasoning tokens (0 when effort is `none`). Times in ms; all values are medians.


## `greet` — "Hi, what can you help me with?"  _(no tool expected)_

| Model | Effort | TTFT | gen | total | out tok | reason tok | tool |
|---|---|--:|--:|--:|--:|--:|:--:|
| gpt-5.4-nano | none | 619.3 | 619.3 | 1211.9 | 44 | 0 | no |
| gpt-5.4 | none | 676.3 | 676.3 | 1119.5 | 32 | 0 | no |

## `room` — "Where is room E-107?"  _(tool call expected)_

| Model | Effort | TTFT | gen | total | out tok | reason tok | tool |
|---|---|--:|--:|--:|--:|--:|:--:|
| gpt-5.4-nano | none | 1606.3 | 641.8 | 2036.4 | 48 | 0 | yes |
| gpt-5.4 | none | 1683.7 | 662.9 | 2082.0 | 47 | 0 | yes |
