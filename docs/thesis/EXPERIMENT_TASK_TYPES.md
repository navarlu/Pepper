# Experiment Design — Pepper Receptionist, Task-Type Evaluation

> **Status:** draft — supersedes (or complements) [EXPERIMENT.md](EXPERIMENT.md). The previous design compared cloud vs. local LLM backends. This design fixes the backend and instead evaluates the **same Pepper system across three task types of increasing complexity**, measuring task success and subjective perception.

---

## 1. Goal

Evaluate whether the Pepper-based receptionist deployed at FEE Karlovo náměstí can usefully help students with realistic reception-desk requests, and how it is perceived along standard HRI dimensions (likeability, perceived intelligence, animacy, etc.).

### Research questions

1. **RQ1 — Task success.** How often does Pepper produce a correct/helpful answer for each of the three task types (no-tool / single-tool / multi-step)?
2. **RQ2 — Perception.** How is Pepper perceived overall (Godspeed subscales, plus an open question on what stood out)?
3. **RQ3 — Per-task helpfulness.** Does perceived helpfulness differ across task types, and does it correlate with objective correctness?
4. **RQ4 — Prior-attitude effect.** Do students with more prior robot/AI exposure or more positive prior attitudes (NARS) rate Pepper differently?

With *n* = 20 main participants, the study is framed as **descriptive / exploratory**, not confirmatory.

---

## 2. Design

**Within-subjects, single condition (system fixed), three task types, order counterbalanced.**

Each participant completes **three short conversations** with Pepper, one per task type. The order is randomized using a balanced **6-permutation Latin square** over the three tasks (A, B, C):

| Slot | Order 1 | Order 2 | Order 3 | Order 4 | Order 5 | Order 6 |
|---|---|---|---|---|---|---|
| 1st | A | A | B | B | C | C |
| 2nd | B | C | A | C | A | B |
| 3rd | C | B | C | A | B | A |

Pilot (*n* = 5) covers 5 of the 6 orders; main study (*n* ≈ 18–24) cycles the full square 3–4 times.

### The three tasks

| Task | Complexity | Example | What Pepper needs |
|---|---|---|---|
| **A — Static fact** | Trivial — answer is in the system prompt | "When does the school open?" / "Where is the main entrance?" / "What time does the building close?" | Plain LLM response, **no tool call** |
| **B — Single tool call** | Needs structured lookup | "What's professor X's phone / email / office?" | One tool call (`lookup_person`) |
| **C — Multi-step scenario** | Open-ended, requires composing tools | "I forgot when my *Linear Algebra* lecture starts today and where it is — can you check?" | At least two steps: `subject_schedule` (or similar) → optionally `find_path_to_room` |

For each task the experimenter hands the participant a **task card** (CZ + EN) with a one-line scenario but **not** the exact phrasing — students ask in their own words. To control for comparability, each card lists **one specific target** (e.g., "ask about *the subject you have next today*") so we can verify ground truth.

> **Important:** the cards must use **generic instructions, not real codes/names** that the system has been pre-tuned on (per project rule — see `feedback_no_domain_leak_in_tools.md`). The student supplies their own subject / teacher / room from their study plan.

---

## 3. Participants

- **Recruitment:** opportunistic, in-person, at FEE Karlovo náměstí reception during high-traffic windows between lecture blocks. Experimenter approaches students and explains the study in <1 min.
- **Inclusion:** adult FEE students (BSc, MSc, PhD).
- **Exclusion:** visiting school groups; non-students; participants who have already piloted the system.
- **Sample size:**
  - **Pilot:** 5 students. Goal = catch UX bugs, measure timing, fix wording. Pilot data is **discarded** from the final analysis.
  - **Main:** 20 students.
- **Compensation:** small thank-you (chocolate / FEE merch), if budget allows.

---

## 4. Materials

### 4.1 Consent form (one page, CZ + EN)
- Purpose of the study
- What is recorded: text logs (always), audio (opt-in), questionnaire answers
- Data flow: where audio/text is sent (declare any cloud component honestly)
- Retention: raw audio deleted after thesis defence; anonymised metrics kept as research data
- Right to withdraw within 30 days
- Contact (Lucas, supervisor)

### 4.2 Pre-questionnaire (~2 min)
- **Demographics:** age bracket, gender (optional), study programme + year, native language, English self-rating (CEFR A1–C2)
- **Prior exposure:** ChatGPT/voice-assistant use frequency (never / occasional / daily); prior interaction with a humanoid robot (yes/no — if yes, where).
- **Prior attitude — short NARS (Nomura et al. 2006), 14 items, 5-pt Likert**: standard validated scale for negative attitudes toward robots. Use as a covariate in analysis.

### 4.3 Per-task post-questionnaire (~30 s, after each conversation)
- *"Did Pepper give you a useful answer?"* (1 = not at all, 5 = perfectly)
- *"Was the answer correct, as far as you know?"* (correct / partially / wrong / I can't tell)
- *"How natural did this conversation feel?"* (1–5)
- One open line: *"Anything that surprised you (positively or negatively)?"*

### 4.4 Final post-questionnaire (~3 min, after all three conversations)
- **Godspeed** (Bartneck et al. 2009), 24 items, 5-pt semantic differential
  - Anthropomorphism, Animacy, Likeability, Perceived Intelligence, Perceived Safety
- *"Which of the three conversations did you find most helpful? Least helpful? Why?"*
- *"Would you use this kind of receptionist again?"* (1–5) + free text
- Debrief: explain that Pepper used different mechanisms (system prompt vs. tool calls) for the three tasks.

### 4.5 Experimenter live sheet (per participant)
- Anonymous ID (e.g. `P017`)
- Order assignment (one of the 6 permutations)
- For each of the 3 tasks, the **canonical ground truth** the experimenter looks up live (timetable / staff directory / room map), so each answer can be labelled *correct / partial / wrong / refused* post-hoc.

---

## 5. Procedure (~15 min per participant)

| Step | Time | Location |
|---|---|---|
| Greet, explain purpose, hand consent + info sheet | 1 min | reception |
| Sign consent + audio opt-in | 1 min | reception |
| Pre-questionnaire (demographics + NARS) on tablet/paper | 2 min | reception |
| Brief on the 3 tasks; show task cards | 1 min | reception |
| **Conversation 1** (per assigned order) | ~2 min | with Pepper |
| Post-task 1 questionnaire | 30 s | tablet/paper |
| **Conversation 2** | ~2 min | with Pepper |
| Post-task 2 questionnaire | 30 s | tablet/paper |
| **Conversation 3** | ~2 min | with Pepper |
| Post-task 3 questionnaire | 30 s | tablet/paper |
| Final questionnaire (Godspeed + comparative) | 3 min | tablet/paper |
| Debrief, thanks, optional Q&A | 1 min | reception |

Total target ≤ 15 min so participants don't bail mid-study between lectures.

---

## 6. Measures

### 6.1 Objective (from voice-agent logs — already emitted by `[PIPE]`)

The voice-agent already logs STT/LLM/TTS timings and structured tool-call events. Per session we capture:

- **End-to-end latency per turn** (user-stops → robot-starts speaking)
- **Tool calls:** name, arguments, duration, success/error
- **Number of turns** and total interaction duration
- **ASR transcript** of every user turn

**One small code addition:** dump per-session JSON-Lines at `voice-agent/logs/sessions/<session_id>.jsonl` plus `<session_id>.meta` (participant ID, order, demographics) and `<session_id>.notes` (experimenter ground truth + correctness label). Same scheme as in [EXPERIMENT.md](EXPERIMENT.md) so any tooling carries over.

### 6.2 Subjective (from questionnaires)

- Per-task: usefulness (1–5), correctness self-report, naturalness (1–5)
- Final: 5 Godspeed subscale means
- Open text: thematic coding by experimenter

### 6.3 Derived

- **Per-task success rate** (experimenter ground-truth label, not self-report)
- **Tool-call success rate** for tasks B and C
- **Correlation** between objective correctness and self-reported usefulness

---

## 7. Pilot (n = 5) — what to learn

Pilot data is **not** included in main analysis. Goals:

1. Validate that all three tasks are completable in <2 min with current system.
2. Catch ASR / TTS / tool-call failure modes before the main run.
3. Measure real per-step timings — adjust if total > 15 min.
4. Refine task-card wording (CZ + EN) so students understand without coaching.
5. Confirm questionnaires are unambiguous (especially translated NARS items).

Stop criteria for the pilot: rerun if any of the three tasks fails for >2 of 5 pilots due to a system issue (not the student's fault).

---

## 8. Analysis plan

- **Task success (RQ1):** report success rate ± Wilson 95 % CI per task type. Compare task types with Cochran's Q (paired binary across 3 conditions); pairwise McNemar with Holm correction.
- **Perception (RQ2):** report Godspeed subscale means ± SD. Compare to published baselines (Pepper in Chen et al., R-001) qualitatively.
- **Per-task helpfulness (RQ3):** Friedman test across task types on the 1–5 usefulness item; Spearman ρ between self-reported usefulness and ground-truth correctness.
- **Prior-attitude effect (RQ4):** Spearman ρ between NARS subscale scores and (a) overall Godspeed-Likeability, (b) per-task usefulness.
- **Latency:** report medians + IQR per task type.
- **Open feedback:** thematic coding, list 3 most common themes per task type.

With *n* = 20, all comparisons are reported with effect sizes; *p*-values are descriptive, not decisive.

---

## 9. Ethics

- One-page bilingual (CZ + EN) consent.
- Audio recording **opt-in**, default off — text logs are enough for the analyses above.
- Anonymous IDs (`P###`) on all stored data; the ID ↔ name mapping is **not** stored.
- Adults only; exclude minors / school groups.
- Confirm with supervisor whether **FEL ethics committee** review is required. Low-risk observational HRI usually doesn't, but a written "no review needed" note from the committee chair protects the thesis.

---

## 10. Open decisions (resolve before pilot)

- [ ] **Interaction language** — Czech, English, or participant-choice? Local LLM is English-tuned; Czech ASR via Whisper degrades. Capture CEFR as a covariate in either case.
- [ ] **Tablet vs. paper** for questionnaires — tablet is faster but adds a tech-failure mode at the reception.
- [ ] **Pepper face tracking** — track participant or fix gaze? Must be identical across all participants.
- [ ] **Task C target** — fixed list of subjects ("pick one of: …") vs. fully open ("any subject from your timetable"). Open is more realistic; fixed gives cleaner ground truth.
- [ ] **Compensation** — confirm budget for small thank-you items.
- [ ] **Pre-registration** — light-touch OSF entry before main run? Cheap and strengthens the thesis.

---

## 11. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Pepper / Wi-Fi / vLLM crash mid-session | Restart-fast playbook on the experimenter laptop; if a session is lost, re-invite the participant |
| Reception too noisy → ASR fails | Schedule sessions in calmer windows; have a fallback quiet corner |
| Participant runs out of time | Hard cap each conversation at 2 min; allow them to skip the final questionnaire and email it later |
| Task cards leak the answer | Phrase cards as scenarios, not as the question; pilot specifically tests this |
| Order effects despite Latin square | Report results both with and without first-task data |

---

## 12. Key references

| ID | Paper | Use |
|---|---|---|
| R-001 | Chen et al. — *Does ChatGPT and Whisper Make Humanoid Robots More Relatable?* | Closest prior work; ChatGPT + Whisper on Pepper |
| R-006 | Bartneck et al. (2009) — *Godspeed Questionnaire Series* | Subjective measurement instrument |
| R-012 | Bartneck et al. — *Human-Robot Interaction: An Introduction*, Ch. 10 | Methodology reference |
| — | Nomura et al. (2006) — *Negative Attitudes toward Robots Scale (NARS)* | Pre-questionnaire prior-attitude covariate |
