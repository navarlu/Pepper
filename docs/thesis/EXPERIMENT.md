# Experiment Design — HRI Evaluation

## Overview

Within-subjects user study comparing two system variants (cloud vs. local LLM) of the Pepper receptionist in a real FEE Karlovo namesti setting. Each participant experiences both conditions, order counterbalanced.

**Independent variable:** LLM backend — `pepper-openai` (OpenAI Realtime) vs. `pepper-local` (Whisper + Qwen 2.5 7B + Piper)

**Target sample:** 20–30 participants (within-subjects designs need fewer participants; R-012 ch.10, R-001 used ~30)

---

## Conditions

| Condition | STT | LLM | TTS |
|---|---|---|---|
| **Cloud** | OpenAI Realtime (built-in) | GPT Realtime Mini | OpenAI Realtime (built-in) |
| **Local** | Faster Whisper | Qwen 2.5 7B (vLLM) | Piper |

Both conditions use the same RAG pipeline (Weaviate + `text-embedding-3-large`), the same tool set (`query_search`, `play_animation`), and the same system prompt. The only difference is the LLM backend.

---

## Tasks per Condition

Each participant performs 2–3 structured tasks with each variant:

1. **Factual / RAG question** — "Where is room KN:E-301?" or "What are the office hours for the study department?" (tests retrieval accuracy + response correctness)
2. **Informational question** — "What scholarships are available for master's students?" (tests longer-form generation quality)
3. **Social / open-ended exchange** — "What's it like being a robot?" or free conversation (tests naturalness, personality, non-verbal behaviour)

Tasks should be comparable across conditions but not identical (to avoid memorization). Prepare two matched task sets and counterbalance assignment.

---

## Measurement Instruments

### Subjective (questionnaires, administered after each condition)

| Instrument | Constructs | Scale type | Reference |
|---|---|---|---|
| **Godspeed** (Bartneck et al., 2009) | Anthropomorphism, Animacy, Likeability, Perceived Intelligence, Perceived Safety | 5-point semantic differential | R-006 |
| **Almere Model** (Heerink et al., 2010) — selected subscales | Perceived Usefulness, Perceived Ease of Use, Perceived Enjoyment, Social Presence, Trust, Anxiety, Intention to Use | 5-point Likert | R-007 |
| **NASA-TLX** (optional) | Cognitive workload | 21-point scales + pairwise comparisons | Used by R-003 (ROS-LLM) |

**Tips from the literature:**
- When using multiple questionnaires in one session, mix the items to mask intention (Godspeed paper, chunk 25)
- Add dummy items if using a single scale in isolation
- Do a power analysis to confirm sample size is sufficient for expected effect size
- Consult with a psychologist on overall methodology if possible (Godspeed paper, chunk 3)

### Objective (system metrics, logged automatically)

| Metric | Source | How |
|---|---|---|
| **Response latency** | LiveKit agent logs | Time from end-of-user-speech to start-of-robot-speech |
| **Word Error Rate (WER)** | Compare ASR transcript vs. ground truth | Prepare a set of test utterances; R-001 benchmarked this across accents |
| **Task completion rate** | Manual annotation | Did the robot give a correct, relevant answer? |
| **RAG retrieval relevance** | Dev console query log | Was the retrieved context appropriate? |
| **Interaction duration** | Session manager logs | Total time per condition |
| **Repair attempts** | Manual annotation from transcripts | How often did the participant have to repeat / rephrase? |

---

## Procedure

1. **Welcome & consent** — Participant reads info sheet, signs consent form. Collect demographics: age, gender, native language, English proficiency, prior experience with ChatGPT / voice assistants / robots.
2. **Brief orientation** — Explain the receptionist scenario (no mention of two different backends yet).
3. **Condition A** — Participant interacts with Pepper (cloud or local, counterbalanced). Performs the 2–3 structured tasks, followed by a short free conversation.
4. **Post-condition A questionnaire** — Godspeed + Almere subscales.
5. **Short break** (~2 min).
6. **Condition B** — Same structure, other backend, matched task set.
7. **Post-condition B questionnaire** — Same instruments.
8. **Comparative questionnaire** — "Which interaction felt more natural / enjoyable?" + open-ended: "What differences did you notice?" + general feedback.
9. **Debriefing** — Reveal the two-condition design, explain the cloud vs. local distinction, answer questions.

**Estimated time per participant:** 20–30 minutes.

---

## Covariates to Capture

Based on findings from R-001 (ChatGPT + Whisper on Pepper):
- **Prior ChatGPT experience** — participants with heavy ChatGPT usage had higher expectations and reported more disappointment (R-001, chunk 18). Record this as a covariate.
- **English proficiency** — non-native speakers may struggle more with ASR, especially local STT.
- **Prior robot experience** — familiarity with robots affects expectations.

---

## Known Pitfalls

- **Don't over-decompose concepts** into ad-hoc sub-scales; use validated instruments as-is (Godspeed, chunk 3)
- **Pepper face tracking** was a major pain point in R-001 — participants had to repeat themselves and seek the robot's attention. Log these failures; consider it a limitation.
- **Wizard-of-Oz is not needed** — our system is fully autonomous, which is a strength to highlight vs. WoZ-based studies
- **Order effects** — always counterbalance condition order; analyse for order effects in the results
- **Expectation bias** — don't reveal the cloud/local distinction until debriefing

---

## Analysis Plan

- **Within-subjects comparisons**: paired t-tests or Wilcoxon signed-rank tests for each Godspeed / Almere subscale between conditions
- **Effect sizes**: report Cohen's d for each comparison
- **Objective metrics**: compare means (latency, WER, task completion) between conditions
- **Qualitative**: thematic analysis of open-ended responses and comparative feedback
- **Covariates**: check if ChatGPT experience, English proficiency, or robot experience moderate the effects (e.g., ANCOVA or regression)

---

## Key References (in Thesis KB)

| ID | Paper | Relevance |
|---|---|---|
| R-001 | Chen et al. — "Does ChatGPT and Whisper Make Humanoid Robots More Relatable?" | Closest prior work: ChatGPT + Whisper on Pepper, user study with ~30 participants |
| R-003 | Mower et al. — "ROS-LLM" | Used NASA-TLX for HRI evaluation, task completion timing |
| R-006 | Bartneck et al. — "Godspeed Questionnaire Series" | Primary subjective measurement instrument for robot perception |
| R-007 | Heerink et al. — "Almere Model" | Technology acceptance model for social robots, adapted from UTAUT |
| R-012 | Bartneck et al. — "Human-Robot Interaction: An Introduction" | Ch. 10: Research Methods — study design, WoZ, sample size, metrics |
| R-013 | Jurafsky & Martin — "Speech and Language Processing" | Background on ASR, WER, dialogue systems |
