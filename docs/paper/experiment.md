# Experiment design — Track A: cascaded gpt-5.4 vs gpt-5.4-nano

> **This is one of two tracks.** Track A (this file) is the controlled,
> text-model comparison with a full offline benchmark. Track B
> (`experiment_realtime.md`) drops the benchmark and instead wires the tools
> into a speech-native realtime model to try live in Czech. Pick one, or run
> both (the text benchmark establishes the accuracy boundary; the realtime
> study shows it holds in a deployed voice agent).

## Idea in one sentence

Give an LLM a good system prompt and a set of deterministic tools so it can act
as a university-building receptionist, then test whether a **small** model
(`gpt-5.4-nano`) does the job as well as a **large** one (`gpt-5.4`) — and if it
falls a little short, show that it does so at **~12× lower cost** and equal
latency, which for this task may be the better engineering choice.

The interesting result is symmetric:
- **If small ≈ large:** you don't need a frontier model to run a reliable
  receptionist — scaffolding (tools + prompt), not model size, is what matters.
- **If small is a little worse:** quantify the gap and weigh it against the cost
  and latency savings (a cost-per-successful-task / Pareto argument).

Either way it's a publishable, honest finding.

---

## Claims / contributions

1. A **tool-grounded receptionist design** (system prompt + tool surface) that
   works unchanged across model sizes.
2. **ReceptionistBench** — an offline, text-in/text-out benchmark that measures
   *where* a small model suffices and where it breaks (per query category).
3. **On-robot evidence**: in a live embodied interaction, objective task success
   of the small model is [equivalent to / within X of] the large model, and
   users [cannot distinguish / rate them similarly], at ~12× lower cost and
   comparable latency.
4. A **practical guideline** for choosing model size in embodied public-service
   agents, framed as accuracy vs. cost-per-successful-task.

---

## Variables

- **Independent variable:** the LLM only — **A = `gpt-5.4`** (large) vs
  **B = `gpt-5.4-nano`** (small).
- **Held identical across A and B** (this is the fix the thesis lacked — it
  changed the whole cascade): system prompt, tool surface, STT model, TTS
  voice/model, gestures/tablet behavior, and **reasoning effort** (fixed to the
  same value for both — see note below).
- **Warm-up model: `gpt-5.4-mini`** — deliberately the *third* model, so the
  practice interaction exposes participants to neither A nor B before they are
  measured.

> **Reasoning effort:** we measured latency parity between nano and gpt-5.4 with
> thinking **off** (`effort="none"`), so latency will not confound the study.
> Fix effort to a single value for *both* conditions (recommended: the
> accuracy/latency sweet spot chosen from the offline benchmark; `none` or `low`).
> Effort is explored in the benchmark, not manipulated on the robot.

---

## Two phases

### Phase 1 — Offline benchmark (backbone, run first)

Text-in/text-out over ~250–300 curated reception queries with ground truth
(rooms, staff, schedule, canteen, time, compositional, ambiguous→clarify,
out-of-scope→refuse, ASR-noise twins). Same prompt + tools; only the model
changes. Metrics: task success, tool-selection F1, argument accuracy,
hallucination rate, appropriate-refusal / over-refusal, clarification rate,
latency, and **cost-per-successful-task**. This produces the significant
quantitative numbers and locates the small-vs-large boundary. (Spec + harness:
`docs/paper/benchmark/`.)

### Phase 2 — On-robot user study (this document)

Confirms that the benchmark result survives real speech, real ASR noise, and
unscripted users, and measures whether people *perceive* a difference.

---

## On-robot design

**Within-subject, counterbalanced, single-blind.** Each participant interacts
with **both** models, on **two matched task sets**, in a counterbalanced order.

Why within-subject (despite the practice-effect worry): the thesis's null result
came from *between-person variance* at small n (some people like robots, some
don't). Making each participant their own control removes that noise and gives
usable power at ~24–32 people instead of needing 30–40 *per* condition.

**Handling the practice/order effect** (participants get smoother on their
second interaction):
- **Warm-up first** — one unscored interaction on **`gpt-5.4-mini`** so everyone
  climbs the learning curve *before* either measured condition.
- **Counterbalance order** — half do A→B, half B→A. Any residual practice
  benefit lands equally on both models, so it becomes noise in the A–B
  comparison, not bias. Order is included as a factor in the analysis to confirm
  it washed out (and to report its size).
- **Matched-but-different task sets** — participants never repeat the same task;
  Set 1 and Set 2 have the same structure with different rooms/people, and are
  balanced across conditions (Latin square: Set1+A / Set2+B, and the swap).

### Procedure (per participant, ~10–12 min)

1. **Recruit + consent** at the reception desk (active recruitment — passive
   recruitment produced zero questionnaires in the thesis). Ethics approval on
   file (ref. from thesis).
2. **Warm-up** (unscored): Pepper running `gpt-5.4-mini`. "Ask Pepper anything
   about the building." ~1–2 turns to learn turn-taking, the mic, the tablet.
3. **Condition 1** (A or B per counterbalance), 3 scenario cards:
   - **C1 — no tool:** "Find out where the lockers are." (tests the hardcoded
     known-facts path — no tool should be called)
   - **C2 — staff lookup:** "Find the office/contact of [staff member]."
     (`lookup_person`; note real ASR may mangle Czech names — same for both
     models, and the tablet transcript lets users correct)
   - **C3 — directions:** "Find out how to get to [room]." (`find_path_to_room`)
4. **Godspeed** (short) for Condition 1.
5. **Condition 2** (the other model), 3 matched-but-different cards (same
   structure, different room/person).
6. **Godspeed** for Condition 2.
7. **Reveal + self-rated correctness:** show the correct answers; participant
   marks, per task, whether Pepper's answer was right (secondary measure).
8. **Debrief**; note they interacted with two different systems (not told which
   during the tasks — single-blind).

Target **N ≈ 28** (balanced across the 2 orders).

---

## Measures

**Primary — objective task success** (experimenter-scored against ground truth,
from the logs, identical criteria for everyone):
- task success per card (binary: correct info conveyed within a time/turn cap),
- number of turns to success,
- out-of-scope / wrong-tool events,
- correct tool selection (from the event log).

**Secondary — participant-reported:**
- self-rated correctness per task (after the reveal),
- perceived speed / "I got what I needed."

**Tertiary — perception:** Godspeed subscales (anthropomorphism, animacy,
likeability, perceived intelligence) per condition; optionally RoSAS-SF
(warmth/competence/discomfort, shorter).

**System-logged (both phases):** TTFT/time-to-first-audio, tokens, tool calls,
and **USD cost per interaction** → cost-per-successful-task.

---

## Analysis plan

- **Task success:** paired, within-subject. McNemar's test on matched tasks, or
  a mixed-effects logistic regression `success ~ model + order + taskset +
  (1|participant)`. Report the model effect with a CI.
- **Equivalence, not just null:** to claim "the small model is as good," run a
  **TOST equivalence test** on the success-rate difference with a pre-registered
  margin (e.g. ±10–15 percentage points). A non-significant difference is *not*
  evidence of equivalence — this is the trap the thesis fell into.
- **Godspeed:** paired Wilcoxon (or the mixed model); TOST for equivalence.
- **Order effect:** report it explicitly (confirms counterbalancing worked).
- **Cost & latency:** cost-per-successful-task per condition + the ratio;
  latency (TTFA) distributions per condition.

---

## The cost angle (fallback win)

At July-2026 prices: `gpt-5.4` = $2.50/$15 per 1M in/out; `gpt-5.4-nano` =
$0.20/$1.25. For a typical reception turn (~460 in / ~50 out tokens) that is
roughly **$0.0019 vs $0.00015 — about 12× cheaper per interaction**, at
essentially identical latency (measured).

So the result reads on a spectrum, all of it useful:

| Outcome | Reading |
|---|---|
| nano ≈ 5.4 (equivalence holds) | You don't need a frontier model — scaffolding wins. Same latency, ~12× cheaper. |
| nano slightly worse (within margin) | Near-parity at ~1/12 the cost → cost-per-successful-task argument; "good enough" for the deployment. |
| nano clearly worse on specific cards | Boundary located: big model needed for [multi-tool / names], small fine for the rest → nuanced guideline. |

Report accuracy vs **cost-per-successful-task** as a Pareto plot so "a bit worse
but 12× cheaper" is quantified rather than hand-waved.

---

## Threats to validity & mitigations

- **Practice/order effect** → warm-up (mini) + counterbalancing + matched task
  sets + order-as-factor.
- **ASR mangling (esp. Czech names)** → held identical across A/B (so it's not a
  confound); part of the real test; tablet transcript lets users self-correct.
- **Blinding** → single-blind (participant not told which model); experimenter
  scores objective success from logs against fixed ground truth.
- **Self-selection** → active recruitment; report sample demographics; expect
  inflated absolute ratings (interpret A–B *difference*, not absolute scores).
- **Ceiling effect** on easy tasks → include at least one harder/ambiguous card
  so the models can actually separate.
- **English-only deployment** (thesis limitation) → keep English and note it, or
  scope multilingual as future work.

---

## Materials checklist

- Scenario cards ×2 sets (matched structure, different room/person), with a
  hidden per-card **objective success criterion**.
- Ground-truth answer sheet (frozen at study time).
- Godspeed (+ optional RoSAS-SF) form, auto-embedding the session ID (fix the
  thesis's 6/26 unmatched-questionnaire loss).
- Counterbalance schedule (order × task-set assignment).
- Logging: per-turn events (tool calls, TTFA, tokens, cost) already emitted by
  the agent.
