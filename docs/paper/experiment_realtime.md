# Experiment design — Track B: realtime speech model (gpt-realtime-2.1 vs mini)

> **Sibling of Track A** (`experiment.md`). Track A is the controlled text
> benchmark + robot study on cascaded gpt-5.4/nano. **Track B (this file) has
> no offline benchmark** — we wire the existing tools into a speech-native
> realtime model and evaluate it live on the robot, in **Czech**.

## Idea in one sentence

Give a **speech-to-speech realtime** model the same receptionist tools and
prompt, and test whether the **small** realtime model (`gpt-realtime-2.1-mini`)
does the job as well as the **large** one (`gpt-realtime-2.1`) in a live,
Czech-speaking interaction on Pepper — at ~3× lower cost.

## Why this track

- **Czech.** Realtime models are multilingual, so the receptionist can speak
  Czech — fixing the thesis's biggest limitation (English-only) and making
  recruitment easy at a Czech university.
- **Speech-native.** End-to-end audio (no STT→LLM→TTS cascade) → lower latency
  and more natural turn-taking; a strong Humanoids fit.
- **Deploy-and-try, not benchmark.** We add the tools and evaluate the live
  system directly. No text benchmark.

## What this track deliberately gives up (be honest in the paper)

Without an offline benchmark there is **no controlled, per-category accuracy
comparison** and no large-N significance on tool-selection/hallucination. So:
- This track is closer to a **demonstrator + field study** than a controlled
  experiment. Frame the contribution as "a working Czech voice receptionist and
  what a live small-vs-large comparison shows," not "a benchmarked accuracy
  claim."
- The **on-robot study must carry the objective evidence** — log-scored task
  success against ground truth becomes the primary result, since there's no
  benchmark behind it.
- **Two must-verify items before committing** (the whole track depends on them):
  1. **Czech quality end-to-end**, especially Czech surnames/room codes through
     recognition (this is where the staff-lookup tool will make or break).
  2. **The mini reliably calls tools** — tool use is newly added to the mini
     tier; verify it selects `find_room`/`lookup_person` and fills args from
     Czech speech.

## Variables

- **Independent variable:** the realtime model — **A = `gpt-realtime-2.1`**
  (large) vs **B = `gpt-realtime-2.1-mini`** (small).
- **Held identical:** system prompt (Czech), tool surface, voice, session config
  (VAD/turn detection), gestures/tablet.
- **Cost:** audio tokens ≈ $10/$20 (mini) vs $32/$64 (flagship) per 1M →
  **~3× cheaper**. Report cost per successful task / per conversation minute.

## Integration (low effort — reuses the Pepper/LiveKit stack)

The thesis stack already runs on LiveKit Agents, which has a first-class OpenAI
**Realtime** model plugin. So the change is: **replace the STT + LLM + TTS
cascade with a single realtime model** and register the same tools in the
realtime session config (`session.update` with the tool schemas). The tool
*implementations* (room dict, UDB/timetable/mensa scrapers) are unchanged — they
already return Czech institutional data.

No standalone benchmark harness is built for this track.

## On-robot study

Same shape as Track A's robot study (within-subject, counterbalanced,
single-blind, N≈28), but in Czech and comparing the two realtime models.

- **Warm-up model:** ideally a neutral third — the previous-generation
  `gpt-realtime-2` — so the practice interaction exposes participants to neither
  A nor B. If that's not workable, a short scripted/canned warm-up is the
  fallback (the practice effect is about learning the interaction, which is
  model-independent; counterbalancing still removes any bias).
- **Tasks (Czech scenario cards, 2 matched sets):** lockers (no tool), staff
  lookup (`lookup_person`, Czech surname), room directions (`find_path_to_room`).
- **Counterbalance** order (A→B / B→A) and task-set assignment (Latin square).
- **Measures:** objective task success vs frozen ground truth (**primary**),
  self-rated correctness after reveal (secondary), Godspeed — use a validated
  **Czech** translation if available, else translate and note it (tertiary).
  System logs: time-to-first-audio, audio tokens, cost, tool calls.
- **Analysis:** mixed-effects logistic `success ~ model + order + taskset +
  (1|participant)`; **TOST equivalence** for the "as good" claim; report
  order-effect size; cost-per-successful-task ratio.

## The cost angle

Even if the mini is a little worse, ~3× cheaper audio at lower latency is a real
deployment argument — report accuracy vs cost-per-successful-task so "slightly
worse but 3× cheaper and faster" is quantified.

## Outcomes read the same way as Track A

| Outcome | Reading |
|---|---|
| mini ≈ flagship | Small realtime model is enough for a Czech voice receptionist; ~3× cheaper. |
| mini slightly worse | Near-parity at ~1/3 the cost → cost-effectiveness argument. |
| mini worse on specific cards (e.g. Czech-name lookup) | Boundary located: flagship needed for [X], mini fine for the rest. |

## Open questions to resolve before starting

- Czech ASR quality on Czech surnames (verify — probe).
- Mini tool-calling reliability from Czech speech (verify — probe).
- Realtime latency on the robot (re-measure; the text-model TTFT numbers don't
  transfer).
- Godspeed Czech translation availability.
- Warm-up: `gpt-realtime-2` vs scripted.
