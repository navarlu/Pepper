# Experiment Design — HRI Evaluation

## Goal

Find out whether students perceive the Pepper receptionist as helpful, intelligent, and natural — and whether the perception differs between a **cloud** LLM (OpenAI Realtime) and a **locally-run** LLM (Llama 3.1 8B via vLLM). Deployed at the FEE Karlovo náměstí reception during high-traffic windows between lecture blocks.

---

## Design

**Within-subjects, two conditions, order counterbalanced.** Each participant talks to Pepper twice — once with each backend — and fills a short questionnaire after each session. Order (cloud-first vs. local-first) alternates between consecutive participants.

**Independent variable:** LLM backend.

| Condition | STT | LLM | TTS |
|---|---|---|---|
| **Cloud** | OpenAI Realtime (speech-to-speech) | `gpt-realtime-mini` | OpenAI Realtime |
| **Local** | FasterWhisper | Llama 3.1 8B Instruct AWQ via vLLM | Piper |

> ⚠️ **Disclose in the thesis.** Cloud is end-to-end speech-to-speech; local is a cascade of three independent models. Differences in prosody, latency and turn-taking cannot be attributed to the LLM alone. Frame the comparison as **system-level**, not as an isolated LLM swap.

**Sample:** 20–30 participants. Pilot with 2–3 lab members first; pilot data is discarded.

---

## Interaction (no scripted card)

Instead of fixed scripted questions, give each participant a **topic prompt** and let them ask about their own life. This trades cross-participant comparability for the realism of how a receptionist would actually be used.

> "Imagine Pepper is a new receptionist at FEE. Please ask her about:
> 1. **One of your subjects** — when's the next class?
> 2. **One of your teachers** — what's their email or office?
> 3. **A room** — how do I get there?
> 4. **The canteen** — what's for lunch today?
> 5. **Anything else** you'd want a receptionist to know."

Same five categories in both conditions. Participants are encouraged to pick **different** subjects / teachers / rooms in the second round — that removes the "I already heard the answer" bias and is what would naturally happen anyway.

**Ground truth captured live by the experimenter.** While the student talks, look up the canonical answer on a laptop (timetable / UDB / room directory) and write it on a session sheet. Post-hoc, label each answer as *correct / partial / wrong / refused*.

---

## Measurement

### After each condition (~3 min)

| Instrument | What it measures | Items |
|---|---|---|
| **Godspeed** (Bartneck et al. 2009) | Anthropomorphism, Animacy, Likeability, Perceived Intelligence, Perceived Safety | 24, 5-pt semantic differential |
| **Per-task items** | For each of the 5 questions: answer correct? helpful? (1–5) | 2 × 5 = 10 |
| **Open box** | "What did you notice about this interaction?" | 1 |

### After both conditions (~1 min)

- "Which interaction did you prefer? Why?"
- Reveal the cloud-vs-local design and debrief.

### From the logs (no extra work)

The voice-agent already emits `[PIPE]` STT/LLM/TTS timings and structured tool-call events. Per session, we get for free:

- **End-to-end latency** per turn (user-stops → robot-starts)
- **Which tool was called**, with arguments, duration, success/error
- **Number of turns** and total interaction time

**One small code addition:** dump these to a JSON-Lines file per session at `voice-agent/logs/sessions/<session_id>.jsonl`, so each interaction is replayable post-hoc.

---

## Procedure (~15 min/participant)

| Step | Time |
|---|---|
| Greet, hand info sheet (CZ + EN), sign consent | 1 min |
| Mini-demographics: age, study programme + year, English self-rating (CEFR), prior ChatGPT/voice-assistant use, prior robot interaction | 1 min |
| Brief: "Please ask Pepper about these 5 things." | 1 min |
| **Condition A** — interaction | 4 min |
| Post-A questionnaire (Godspeed + per-task items + open box) | 3 min |
| **Condition B** — same brief, different choices | 4 min |
| Post-B questionnaire | 3 min |
| Comparative + debrief | 2 min |

---

## Logging

```
voice-agent/logs/sessions/<session_id>.jsonl   # turns, tool calls, timings
voice-agent/logs/sessions/<session_id>.meta    # participant_id, condition, demographics
voice-agent/logs/sessions/<session_id>.notes   # experimenter live ground truth + correctness labels
```

Anonymous IDs only (`P017`). No names. Audio recording opt-in; default off — text logs are enough.

---

## Ethics (light-touch)

- One-page bilingual (CZ + EN) consent form: purpose, what's recorded, data flow (in cloud condition, audio is sent to OpenAI in the US — call this out explicitly), retention (raw audio deleted after defence, anonymised metrics kept indefinitely as research data), right to withdraw within 30 days.
- Adults only; exclude visiting school groups.
- Confirm with Matěj whether the **FEL ethics committee** needs to review. Low-risk observational HRI usually doesn't require it, but a written "no review needed" protects against later objections.

---

## Analysis

- **Godspeed subscales:** Wilcoxon signed-rank (paired) per subscale. Report effect size (rank-biserial *r*).
- **Answer correctness:** McNemar's test on paired binary (cloud-correct / local-correct).
- **Latency:** compare medians (right-skewed distribution); report distribution.
- **Open feedback:** quick thematic coding by you alone — list the 3 most common themes per condition.

With *n* ≈ 25, frame the study as **descriptive / exploratory**, not confirmatory.

---

## Open Decisions (resolve before pilot)

- [ ] **Interaction language** — English only is safest (local Llama is English-tuned; Czech ASR via Whisper degrades). Capture CEFR as a covariate.
- [ ] **Audio recording** — opt-in, off by default. Text-only is enough for analysis.
- [ ] **Tablet content** — thesis task #5 mentions tablet use. If we wire any (e.g. show staff card after `lookup_person`), keep it identical across conditions; otherwise note as future work.
- [ ] **Pepper face tracking** — known R-001 pain point. Decide: track participant or fix gaze straight ahead (consistent across participants).

---

## Key References

| ID | Paper | Use |
|---|---|---|
| R-001 | Chen et al. — "Does ChatGPT and Whisper Make Humanoid Robots More Relatable?" | Closest prior work; ChatGPT + Whisper on Pepper, *n* ≈ 30 |
| R-006 | Bartneck et al. — Godspeed Questionnaire Series | Subjective measurement instrument |
| R-012 | Bartneck et al. — *Human-Robot Interaction: An Introduction*, Ch. 10 | Methodology reference |
