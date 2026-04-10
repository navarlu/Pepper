https://docs.google.com/document/d/18wjGIsm8TbbgqFEvbgbX9QL37sdoOspIgIwiWLcWKaw/edit?pli=1&tab=t.0#heading=h.kmtf914entwk

## gitguide:

### 1) Update GitHub (source of truth)

```bash
cd /home/lucas/Projects/FEL/Pepper
git add docs/thesis/latex/main
git commit -m "thesis: update related work section"
git push origin main
```

### 2) Mirror same state to Overleaf (checkpoint)

```bash
cd /home/lucas/Projects/FEL/Pepper
git fetch overleaf
git worktree add /tmp/overleaf-sync overleaf/master
rsync -a --delete --exclude '.git' docs/thesis/latex/main/ /tmp/overleaf-sync/
cd /tmp/overleaf-sync
git add -A
git commit -m "Checkpoint from local thesis state"
git push overleaf HEAD:master
cd /home/lucas/Projects/FEL/Pepper
git worktree remove /tmp/overleaf-sync
```

## operator panel adress:

http://100.111.97.63:8787/

## run on GPU server

See [gpu-setup.md](gpu-setup.md) for the full setup with agent naming (`pepper-local`).

Quick start on woska:
```bash
ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska
tmux attach -t pepper-agent

cd /mnt/data_personal/navarlu2/work/Pepper
source .venv3/bin/activate
export PEPPER_AGENT_NAME=pepper-local
export PEPPER_AGENT_MODE=local
python -m voice-agent.src.agent dev
```

## safe_startup_example:

```text
lucas@lucas-rpi-5-8gb:~/Projects/FEL/Pepper$ uv run python robot/utils/safe_startup.py 
Safe startup 3 staring...
[info] no URL provided, waiting for Pepper to appear on the network...
[info] (you can power on Pepper now — this will keep retrying)
[discover] searching for Pepper on the network...
[discover] attempt 2, retrying...
[discover] attempt 3, retrying...
[discover] attempt 4, retrying...
[discover] mDNS resolved pepper.local -> 192.168.210.113
[discover] 192.168.210.113 found via mDNS but port 9559 not open yet
[discover] attempt 5, retrying...
[discover] mDNS resolved pepper.local -> 192.168.210.113
[info] discovered Pepper at tcp://192.168.210.113:9559
[W] 1774355076.247677 16476 qi.path.sdklayout: No Application was created, trying to deduce paths
[info] waiting for NAOqi at tcp://192.168.210.113:9559
[wait] connect attempt 1 to tcp://192.168.210.113:9559...
[wait] waiting for connect future (timeout 10s)...
[wait] connected!
[info] connected; waiting for core services...
[info] service ready: ALMotion
[info] service ready: ALAutonomousLife
[info] service ready: ALRobotPosture
[info] service ready: ALDiagnosis
[ok] ALMotion.setDiagnosisEffectEnabled(False) -> None
[warn] ALAutonomousLife.setState('disabled') failed:    AutonomousLife::setState
        Calls to the setState method are not currently allowed. Did you finish the getting started wizard?
[ok] setAutonomousAbilityEnabled(AutonomousBlinking, False) -> None
[ok] setAutonomousAbilityEnabled(BackgroundMovement, False) -> None
[ok] setAutonomousAbilityEnabled(BasicAwareness, False) -> None
[ok] setAutonomousAbilityEnabled(ListeningMovement, False) -> None
[ok] setAutonomousAbilityEnabled(SpeakingMovement, False) -> None
[ok] ALMotion.wakeUp() -> False
[ok] ALRobotPosture.goToPosture('StandInit', 0.6) -> False
[ok] ALDiagnosis.getPassiveDiagnosis() -> []
[ok] ALDiagnosis.getActiveDiagnosis() -> []
[done] stabilization complete. Pepper at tcp://192.168.210.113:9559
```
AI Policy:
AI Policy Notes — FEE (FEL) ČVUT Thesis
Applicable Documents

ČVUT Metodický pokyn č. 5/2023 — Rámcová pravidla používání umělé inteligence na ČVUT pro studijní a pedagogické účely v Bc a NM studiu

Full document (PDF)
Effective since: 25.09.2023


ČVUT Metodický pokyn č. 2/2024 — O dodržování etických principů při přípravě vysokoškolských závěrečných prací

ČVUT legislation page
Effective since: 19.02.2024


FEL CourseWare — AI Tools in Education (Draft Rules)

CZ version
EN version


ČVUT Library — How to Cite AI / ChatGPT

Citation guide




What Is Allowed in a Thesis
ActivityAllowed?NotesGrammar checking✅ YesNo need to declareText reformulation / editing✅ YesMust declare in SW listText structure proposals⚠️ PartiallyMust declare; critically evaluateLiterature search (rešerše)⚠️ PartiallyOnly as starting point; verify everythingFinding authors in your field✅ YesAlways verify resultsWriting results / conclusions❌ NoMust be your own workGenerating citations❌ NoAI fabricates referencesCode generation⚠️ PartiallyMust understand, explain, and be able to rewrite all code

My Situation

Code: Entire codebase vibecoded using Claude Code / Codex
Text: Thesis text drafted by providing structure and key points to AI, which handled formulation

What I Need to Do

Declare AI usage — Add a declaration section in the thesis (see template below)
List AI tools in software/bibliography — Follow ČVUT library citation style
Understand the code deeply — Be able to explain every module, design decision, and trade-off at the defense
Own the results chapters — Results and conclusions should be formulated by me, not AI
Consult with supervisor — Discuss AI usage openly; they may have additional requirements


Declaration Template
Include near the beginning of the thesis or in the methodology chapter:

Prohlášení o použití nástrojů umělé inteligence
Softwarová implementace byla vytvořena s asistencí nástroje Anthropic Claude (Claude Code / Codex) pro generování kódu. Autor veškerý vygenerovaný kód revidoval, testoval a upravoval.
Text této práce byl sestaven s pomocí Claude jako asistenta pro psaní — autor poskytl strukturu, klíčové body a technický obsah, zatímco nástroj AI asistoval s formulací a jazykovou úpravou. Veškerý obsah byl autorem zkontrolován a schválen.

English version:

Declaration on the Use of Artificial Intelligence Tools
The software implementation was developed with the assistance of Anthropic Claude (Claude Code / Codex) for code generation. The author reviewed, tested, and modified all generated code.
The text of this thesis was drafted using Claude as a writing assistant — the author provided the structure, key points, and technical content, while the AI tool assisted with formulation and language editing. All content was reviewed and approved by the author.


Defense Preparation Checklist

 Go through every module of the codebase and understand what it does and why
 Be able to explain architectural decisions (e.g., why Docker, why OPC UA → InfluxDB → Grafana)
 Identify any quirky AI-generated patterns and know how you'd refactor them
 Practice whiteboard-style explanations of key components without looking at code
 Make sure results/conclusions chapters reflect your own understanding
 Verify all citations are real and accessible
 Confirm declaration wording with thesis supervisor