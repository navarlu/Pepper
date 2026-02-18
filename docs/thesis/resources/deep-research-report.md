# LLM-Driven Receptionist Robot on Pepper: Reusable Research, Code, and Reading Map

## Project scope and research assistant operating brief

This thesis project targets the design and evaluation of an LLM-driven receptionist robot with **live spoken interaction**, **optional retrieval-augmented generation (RAG)** over internal documents, and **coordinated non-verbal behavior** (gestures/animations/tablet). The implementation stack you described strongly suggests three engineering “hard parts” that should drive both literature review and code reuse: (a) **real-time voice latency and turn-taking**, (b) **grounding and safety for robot actions** (preventing the LLM from triggering invalid or unsafe behaviors), and (c) **evaluation methodology** suitable for a public-facing receptionist context. citeturn24view0turn24view1turn17view3turn17view2

A practical instruction set for your research assistant (human or LLM) that matches the thesis constraints:

**Research assistant mission statement (copy/paste-ready):**  
You are supporting a master’s thesis on an LLM-driven receptionist robot. Your objective is to find peer-reviewed papers, reproducible code, and high-quality books that can be directly reused in (1) implementation and (2) the thesis methodology + related work. Prioritize sources that include deployment details, real-world HRI user studies, and open-source artifacts. For every candidate source, extract: system architecture, interface contracts (APIs), latency numbers and measurement method, evaluation instruments/questions, and pitfalls reported by the authors. Reject sources that are purely theoretical, manipulation-only with no spoken/social interaction, or that do not contain actionable implementation details.

**How to score sources (use consistently across papers/repos/books):**  
Evidence Quality (1–5): peer-reviewed + replicated + widely cited + clear method → higher.  
Implementation Readiness (1–5): open code + setup instructions + maintained + matches your toolchain → higher.

## Top papers with direct reuse value

### Does ChatGPT and Whisper Make Humanoid Robots More Relatable?

**Full citation + link:**  
Chen, X., Luo, K., Gee, T., & Nejati, M. (2024). *Does ChatGPT and Whisper Make Humanoid Robots More Relatable?* (Published in ACRA 2023; arXiv:2402.07095). Link: https://arxiv.org/abs/2402.07095 citeturn20view2

**Summary (3–5 sentences):**  
This paper describes an end-to-end integration of **Whisper ASR + ChatGPT** with the **Pepper** humanoid robot (“Pepper‑GPT”) and includes a small human evaluation. It explicitly compares ASR choices and reports that Whisper performed best among the tested options, reporting an average WER and processing time, and reports user satisfaction ratings (“excellent” vs “good”). The abstract also highlights remaining limitations (e.g., multilingual ability and facial tracking) that matter in public-facing deployments. citeturn20view2

**Exact relevance to your project (assignment task support):**  
Directly supports **Pepper + live voice pipeline integration**, plus **evaluation framing** for a receptionist/helpdesk robot. citeturn20view2turn15view1

**Reuse plan (copy/adapt vs avoid):**  
Reuse: the repo’s split architecture (“BlackBox” + “PepperController”), networking considerations, and lessons learned about Pepper + ASR/LLM integration. citeturn15view1turn20view2  
Avoid: mirroring the repo’s Python 2.7 dependency choices unless you are forced into legacy NAOqi constraints; instead, translate the concepts into your current pipeline (LiveKit + modern Python). citeturn15view1

**Evidence quality (1–5):** 3/5 (conference publication + concrete experiment, but limited sample size) citeturn20view2  
**Implementation readiness (1–5):** 4/5 (public code + setup notes, but legacy dependencies) citeturn15view1

**Risks/limits:**  
Small-scale evaluation and a Pepper-specific environment/network setup may not generalize. The repo describes a mixed OS/Python setup and explicit routing/VPN assumptions that you may not have. citeturn15view1turn20view2

---

### ChatGPT for Robotics: Design Principles and Model Abilities

**Full citation + link:**  
Vemprala, S., Bonatti, R., Bucker, A., & Kapoor, A. (2023). *ChatGPT for Robotics: Design Principles and Model Abilities* (arXiv:2306.17582). Link: https://arxiv.org/abs/2306.17582 citeturn20view0turn19view1

**Summary (3–5 sentences):**  
This paper proposes a practical pattern for using LLMs in robotics: build a **high-level function library**, explicitly constrain the model to those functions, and use “user-on-the-loop” feedback to correct behaviors. It emphasizes prompt structures, structured outputs (e.g., parsing tags), and iterative refinement, and introduces an open collaborative resource (PromptCraft) for robotics prompting examples. It is not receptionist-specific, but the design principles map cleanly to speech + gesture “skills” on Pepper. citeturn20view0turn19view1

**Exact relevance to your project (assignment task support):**  
Strongly supports **LLM-grounded robot behavior/planning**, especially how to constrain an LLM so it can only trigger valid robot capabilities (speech acts, animations, tablet actions). citeturn19view1turn20view0

**Reuse plan (copy/adapt vs avoid):**  
Reuse: their “define APIs → teach the model the allowed APIs → keep a human in the loop” structure as your core control architecture. citeturn19view1  
Avoid: directly porting manipulation/navigation examples; instead, rewrite the API set to receptionist primitives (greet, ask-clarifying-question, look-up-docs, point-to-location, show-tablet-map, escalate-to-human). citeturn19view1turn20view0

**Evidence quality (1–5):** 4/5 (well-known, widely used design patterns; strong documentation) citeturn20view0turn19view1  
**Implementation readiness (1–5):** 4/5 (paired with an open prompt/code resource via PromptCraft) citeturn15view4turn20view0

**Risks/limits:**  
Many exemplars target drones/manipulation, not social reception; you must carefully re-ground the function library and add safety checks for real deployments. citeturn19view1turn20view0

---

### ROS-LLM: A ROS framework for embodied AI with task feedback and structured reasoning

**Full citation + link:**  
Mower, C. E., Wan, Y., Yu, H., et al. (2024). *ROS‑LLM: A ROS framework for embodied AI with task feedback and structured reasoning* (arXiv:2406.19741). Link: https://arxiv.org/abs/2406.19741 citeturn20view1

**Summary (3–5 sentences):**  
ROS‑LLM proposes a framework for natural language robot programming that converts LLM outputs into executable robot behaviors, explicitly supporting **sequence**, **behavior tree**, and **state machine** modes. It also highlights “reflection” via human/environment feedback and discusses expanding the robot’s action library via imitation learning. Even if your Pepper stack is not ROS-centric, the architecture is valuable as a reference design for separating: dialogue → structured plan → execution. citeturn20view1

**Exact relevance to your project (assignment task support):**  
Supports **LLM-grounded planning**, **structured behaviors**, and **feedback loops**—all critical when your receptionist must reliably follow institutional policies and keep conversations coherent. citeturn20view1turn14view0

**Reuse plan (copy/adapt vs avoid):**  
Reuse: the idea of compiling LLM output into a constrained behavior representation (BT/SM) and treating the dialogue manager as producing structured actions. citeturn20view1  
Avoid: locking into ROS‑specific plumbing if your Pepper control path is QiSDK-based; instead, implement the same concept in your own “action router” layer. citeturn20view1turn25view0

**Evidence quality (1–5):** 4/5 (research framework with explicit design features and experiments) citeturn20view1  
**Implementation readiness (1–5):** 3/5 (open-source claim, but integration cost depends on your robot stack) citeturn20view1turn14view0

**Risks/limits:**  
ROS‑heavy frameworks can expand scope quickly; a receptionist robot may benefit from a smaller custom state machine rather than adopting an entire ROS meta-framework. citeturn20view1

---

### Do As I Can, Not As I Say: Grounding Language in Robotic Affordances

**Full citation + link:**  
Ahn, M., Brohan, A., Brown, N., et al. (2022). *Do As I Can, Not As I Say: Grounding Language in Robotic Affordances* (arXiv:2204.01691). Link: https://arxiv.org/abs/2204.01691 citeturn20view3

**Summary (3–5 sentences):**  
This paper is central to a key thesis problem: LLMs are knowledgeable but not embodied, so you must **ground** outputs in a robot’s feasible skills. The authors propose constraining language model plans using pre-trained skills/value functions so the resulting actions are feasible and context-appropriate. The robotic domain is not receptionist dialogue, but the design principle transfers: constrain the LLM to only receptionist actions that make sense (and can be validated). citeturn20view3

**Exact relevance to your project (assignment task support):**  
Supports **LLM grounding and safety**, and motivates designing a “capability set” for Pepper (speech acts, gestures, tablet UI actions, policies). citeturn20view3turn19view1

**Reuse plan (copy/adapt vs avoid):**  
Reuse: the conceptual separation “LLM proposes high-level intent; skill library enforces feasibility.” citeturn20view3  
Avoid: copying manipulation-specific skill/value-function machinery; instead, implement simpler validators (schema checks, policy checks, cooldowns, confirmation prompts). citeturn20view3

**Evidence quality (1–5):** 5/5 (high-impact, widely adopted grounding approach) citeturn20view3  
**Implementation readiness (1–5):** 2/5 (needs significant adaptation for receptionist behaviors) citeturn20view3

**Risks/limits:**  
The original approach assumes robots can execute physical skills with measurable success signals; receptionist tasks need different success detection (task completion, user satisfaction, correctness of info). citeturn20view3

---

### How do people talk with a robot?: an analysis of human-robot dialogues in the real world

**Full citation + link:**  
Lee, M. K., & Makatchev, M. (2009). *How do people talk with a robot?: an analysis of human-robot dialogues in the real world.* In *CHI ’09 Extended Abstracts on Human Factors in Computing Systems (CHI EA ’09)*, pp. 3769–3774. Link: https://www.ri.cmu.edu/publications/how-do-people-talk-with-a-robot-an-analysis-of-human-robot-dialogues-in-the-real-world/ citeturn21view0

**Summary (3–5 sentences):**  
This paper analyzes dialogue logs from **Roboceptionist**, a robot receptionist deployed in a high-traffic academic building. It reports that giving the robot an **occupation/background persona** helps people establish common ground, and that users vary significantly in how much they follow social norms of human-human dialogue when talking to a robot. The paper distills implications for designing the dialogue of social robots in real public contexts. citeturn21view0

**Exact relevance to your project (assignment task support):**  
Directly supports the **dialogue design** part of your receptionist: greeting, persona, handling off-topic questions, and anticipating real “in the wild” user behavior. citeturn21view0

**Reuse plan (copy/adapt vs avoid):**  
Reuse: their design implication that persona/background changes how people talk—use this to justify a receptionist “role script” in your system prompt + interaction design section. citeturn21view0  
Avoid: overfitting to one building’s FAQ; instead, use it to design categories of user intents and fallback strategies. citeturn21view0

**Evidence quality (1–5):** 4/5 (real deployment + log analysis) citeturn21view0  
**Implementation readiness (1–5):** 3/5 (design insights rather than code) citeturn21view0

**Risks/limits:**  
Older (pre-LLM) but still valuable. It does not address modern speech pipelines, hallucinations, or privacy expectations. citeturn21view0

---

### The Receptionist Robot

**Full citation + link:**  
Holthaus, P., & Wachsmuth, S. (2014). *The Receptionist Robot* (HRI ’14 demo; DOI in paper). Link: https://patrickholthaus.de/publications/Holthaus2014a.pdf citeturn22view0

**Summary (3–5 sentences):**  
This demo paper describes a humanoid receptionist that provides directions on a map using **speech plus deictic gestures**, explicitly designed to improve user experience by being aware of non-verbal social signals. It describes an interaction setup that includes a dialog module, perception/vision components, and a “spatial attention strategy” so the robot can indicate availability and initiate dialog. Even though it is not Pepper, it is one of the clearest concise references for coupling spoken guidance with gesture and attention behaviors. citeturn22view0

**Exact relevance to your project (assignment task support):**  
Supports **non-verbal robot behavior** design (gesture, attention cues) and ties them to a receptionist wayfinding scenario. citeturn22view0

**Reuse plan (copy/adapt vs avoid):**  
Reuse: the interaction pattern “detect approach → signal availability → answer with coordinated speech+gesture,” and map that into Pepper animations/tablet visuals + voice. citeturn22view0  
Avoid: relying on their specific perception stack; implement the high-level logic using Pepper’s built-in sensing and your own “engagement state machine.” citeturn22view0turn25view0

**Evidence quality (1–5):** 3/5 (demo paper, limited evaluation detail) citeturn22view0  
**Implementation readiness (1–5):** 3/5 (actionable design idea, but not a drop-in library) citeturn22view0

**Risks/limits:**  
Short demo format: not enough details on failure modes, timing constraints, and real acoustic challenges that Pepper will face. citeturn22view0

---

### Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks

**Full citation + link:**  
Lewis, P., Perez, E., Piktus, A., et al. (2020). *Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks* (NeurIPS 2020; arXiv:2005.11401). Link: https://arxiv.org/abs/2005.11401 citeturn18view0turn18view1

**Summary (3–5 sentences):**  
This paper formalizes **RAG** as combining a parametric generator (seq2seq model) with a non-parametric retrieved document store, motivated by limits of “knowledge stored in parameters” and the need for provenance and updateability. It describes RAG variants (fixed retrieved docs vs per-token retrieval) and reports strong results on knowledge-intensive QA tasks, emphasizing that retrieval helps outputs be more factual and specific. For your project, it provides the canonical academic grounding for why you should use RAG on internal university documents instead of relying purely on an LLM’s memory. citeturn18view0turn18view1

**Exact relevance to your project (assignment task support):**  
Directly supports **RAG over internal FEL documents**, plus thesis “why RAG” arguments around provenance and updateability. citeturn18view0turn24view3

**Reuse plan (copy/adapt vs avoid):**  
Reuse: the conceptual architecture and terminology for your related work + method section; treat Weaviate as the practical document index implementation. citeturn18view1turn24view3  
Avoid: copying evaluation metrics without adaptation; your setting is closed-domain institutional QA, so you need correctness/traceability over open-domain recall. citeturn18view1turn24view3

**Evidence quality (1–5):** 5/5 (canonical RAG reference; peer-reviewed) citeturn18view1  
**Implementation readiness (1–5):** 3/5 (conceptual; implementation relies on modern tooling) citeturn18view1turn24view3

**Risks/limits:**  
Original experiments are open-domain QA; closed-domain university “truth” requires strict document governance, chunking, and refusal behaviors when retrieval fails. citeturn18view1turn24view3

---

### Assessing Acceptance of Assistive Social Agent Technology by Older Adults: the Almere Model

**Full citation + link:**  
Heerink, M., Kröse, B., Evers, V., & Wielinga, B. (2010). *Assessing Acceptance of Assistive Social Agent Technology by Older Adults: the Almere Model.* *International Journal of Social Robotics*, 2, 361–375. Link: https://link.springer.com/article/10.1007/s12369-010-0068-5 citeturn17view2turn2search1

**Summary (3–5 sentences):**  
The Almere Model adapts and extends UTAUT specifically for **assistive social agents**, adding variables related to social interaction (not just usefulness/ease of use). The paper reports testing with controlled and longitudinal data and reports substantial explained variance in both usage intention and actual use across settings. While it is oriented toward older adults and assistive agents, it remains one of the clearest acceptance-model foundations for social robots. citeturn17view2

**Exact relevance to your project (assignment task support):**  
Supports the **evaluation methodology** chapter: acceptance/intent-to-use constructs, questionnaire design, and how to interpret “would you use this receptionist again?” beyond raw task success. citeturn17view2

**Reuse plan (copy/adapt vs avoid):**  
Reuse: the constructs and model framing for your thesis evaluation design; use it to justify measuring intent-to-use and social factors. citeturn17view2  
Avoid: copying the exact population assumptions—revalidate items for students/staff/visitors at a university reception. citeturn17view2

**Evidence quality (1–5):** 5/5 (highly cited, validated model in social robotics) citeturn17view2  
**Implementation readiness (1–5):** 4/5 (directly reusable constructs/items; minimal code needs) citeturn17view2

**Risks/limits:**  
Population mismatch (elderly care vs university visitors). You’ll need to justify adapted items and run reliability checks (Cronbach’s alpha) in your study. citeturn17view2

---

### Measurement Instruments for the Anthropomorphism, Animacy, Likeability, Perceived Intelligence, and Perceived Safety of Robots

**Full citation + link:**  
Bartneck, C., Kulić, D., Croft, E., & Zoghbi, S. (2009). *Measurement Instruments for the Anthropomorphism, Animacy, Likeability, Perceived Intelligence, and Perceived Safety of Robots.* *International Journal of Social Robotics*, 1(1), 71–81. Link: https://link.springer.com/article/10.1007/s12369-008-0001-3 citeturn17view3turn9search7

**Summary (3–5 sentences):**  
This paper introduces what is commonly referred to as the **Godspeed Questionnaire Series**, aiming to standardize measurement of key HRI perception concepts so results can be compared across studies. It distills five questionnaires using semantic differential scales and reports reliability/validity indicators across empirical studies. For a receptionist robot, these constructs map well to “does this robot feel safe, likable, intelligent, and human-like enough to approach?”—critical in a real reception hallway. citeturn17view3turn9search7

**Exact relevance to your project (assignment task support):**  
Directly supports the **HRI questionnaire** requirement, especially perception/safety assessment for a social receptionist. citeturn17view3

**Reuse plan (copy/adapt vs avoid):**  
Reuse: the questionnaire items (properly cited) and the justification that standardized measures enable comparison to prior HRI work. citeturn17view3turn9search7  
Avoid: treating Godspeed as sufficient by itself; for modern social robots, consider pairing it with task success + intent-to-use constructs (Almere) and open-ended qualitative feedback. citeturn17view3turn17view2

**Evidence quality (1–5):** 5/5 (canonical HRI instrument paper) citeturn17view3turn9search7  
**Implementation readiness (1–5):** 5/5 (instrument is directly reusable) citeturn17view3

**Risks/limits:**  
Recent HRI meta-method work has raised ongoing “fit” questions about legacy scales in some contexts; be prepared to justify why Godspeed matches your reception scenario and reporting goals. citeturn17view3turn23search12

## Top GitHub repositories with direct implementation reuse

### LiveKit Agents

**Link:** https://github.com/livekit/agents citeturn24view0turn15view0

**Summary (3–5 sentences):**  
The Agents framework is designed for building real-time, programmable participants that can “see, hear, and understand,” and explicitly supports mixing STT/LLM/TTS components. It also mentions features relevant to your latency goals, such as “semantic turn detection” and a flexible integration ecosystem. This repo is the highest-leverage codebase for your live receptionist voice pipeline. citeturn24view0turn24view1

**Exact relevance to your project (assignment task support):**  
Supports **live voice pipeline**, **latency measurement**, and **deployment/observability**. citeturn24view0turn24view1

**Reuse plan (what to copy/adapt vs avoid):**  
Reuse: pipeline abstractions + hooks for metrics (TTFT/TTFB) and turn boundaries; adopt their latency decomposition formula as your baseline measurement method. citeturn24view1turn24view2  
Avoid: treating default settings as “production optimal” for an embodied robot; you will need a Pepper-specific tuning pass for VAD/turn-taking and echo/noise. citeturn24view4turn24view0

**Evidence quality:** 4/5 (well-documented OSS framework) citeturn24view0turn24view1  
**Implementation readiness:** 5/5 (directly usable with examples + metrics support) citeturn24view2turn3search13

**Risks/limits:**  
Real-time conversational quality depends on configuration and hardware. You will need to validate end-to-end latency and turn-taking robustness in noisy public spaces. citeturn24view1turn24view4

---

### Pepper-GPT

**Link:** https://github.com/UoA-CARES/Pepper-GPT citeturn15view1turn20view2

**Summary (3–5 sentences):**  
Pepper‑GPT provides a concrete Pepper + Whisper + GPT integration with specific notes on networking and environment setup. The README explains a split architecture and describes practical requirements like GPU availability and NAOqi SDK installation checks. This is the closest “reference implementation” to your thesis topic among publicly available repos. citeturn15view1turn20view2

**Exact relevance:**  
Supports **Pepper integration**, **voice pipeline wiring**, and gives a ready comparison point when you justify architectural decisions in your thesis. citeturn15view1turn20view2

**Reuse plan:**  
Reuse: system decomposition, command routing ideas (LLM detects user commands and triggers robot actions), and experiment scaffolding. citeturn8search9turn15view1  
Avoid: copying legacy Python constraints blindly; treat this repo as a “pattern library,” not a dependency baseline. citeturn15view1

**Evidence quality:** 3/5 (project code aligned with a paper, but niche environment) citeturn20view2turn15view1  
**Implementation readiness:** 4/5 (code exists; porting effort likely) citeturn15view1

**Risks/limits:**  
If your Pepper runs NAOqi 2.9 + QiSDK, portions of older Python/NAOqi assumptions may not align with your lab robot configuration. citeturn25view0turn15view1

---

### naoqi_driver (ROS bridge)

**Link:** https://github.com/ros-naoqi/naoqi_driver citeturn15view3

**Summary (3–5 sentences):**  
This repo provides a ROS bridge to NAOqi, publishing sensors/robot position and enabling calls to parts of the NAOqi API; it explicitly notes testing with Pepper. It documents installation via apt for ROS Noetic and gives operational advice such as disabling autonomous life before launching. If your project involves ROS integration (common for research labs), this is your cleanest bridge layer. citeturn15view3

**Exact relevance:**  
Supports **robot control integration** (Pepper ↔ external compute), which is essential if LiveKit/LLM runs off-robot and Pepper is the embodiment endpoint. citeturn15view3turn24view0

**Reuse plan:**  
Reuse: the bridging pattern (robot runs NAOqi; desktop runs ROS and orchestrates). citeturn15view3  
Avoid: depending on ROS if your control path is purely QiSDK/Android; in that case, use it only as a reference for what should be exposed (pose, sensors, action calls). citeturn25view0turn15view3

**Evidence quality:** 4/5 (long-used bridge in the ecosystem) citeturn15view3  
**Implementation readiness:** 3/5 (depends on your ROS version and Pepper OS constraints) citeturn15view3turn4search5

**Risks/limits:**  
Network stability and version mismatches (ROS/NAOqi) can consume time. Build a minimal connectivity test early (publish one sensor + trigger one animation). citeturn15view3turn4search9

---

### Weaviate

**Link:** https://github.com/weaviate/weaviate citeturn15view2

**Summary (3–5 sentences):**  
Weaviate is an open-source vector database that stores objects and vectors and supports semantic retrieval + filtering; it explicitly positions itself for RAG use cases. For your project, it functions as the institutional document store behind “ask the faculty policies / office locations / procedures” questions. You can use it to return retrieved passages and then generate answers conditioned on them. citeturn15view2turn24view3

**Exact relevance:**  
Supports the **RAG over internal FEL documents** requirement. citeturn24view3turn15view2

**Reuse plan:**  
Reuse: hybrid search + filtering patterns (e.g., by document type/date/department) and Weaviate’s RAG query concept (retrieve first, then pass results into a generative model). citeturn24view3turn15view2  
Avoid: relying on “cloud-only” helpers if your constraints require on-prem; confirm which capabilities require managed services. citeturn24view3turn15view2

**Evidence quality:** 4/5 (widely used OSS infrastructure) citeturn15view2turn9search9  
**Implementation readiness:** 4/5 (good docs + clear query patterns) citeturn24view3turn3search15

**Risks/limits:**  
RAG quality depends on chunking, metadata, and retrieval strategy; the database alone won’t prevent hallucinations when retrieval misses. citeturn24view3turn18view1

---

### vLLM

**Link:** https://github.com/vllm-project/vllm citeturn16view2

**Summary (3–5 sentences):**  
vLLM is an inference/serving engine designed for fast LLM serving and explicitly advertises an **OpenAI-compatible API server**, streaming outputs, and continuous batching. It also links to the research paper introducing PagedAttention, explaining how it improves KV-cache memory efficiency and throughput. For your cloud-vs-local thesis requirement, vLLM is a high-quality “local baseline” when you want GPU-hosted, low-latency, repeatable serving. citeturn16view2turn10search2

**Exact relevance:**  
Supports **local LLM backend implementation** and formal benchmarking for the cloud vs local comparison. citeturn16view2turn10search2

**Reuse plan:**  
Reuse: OpenAI-compatible server mode to swap between cloud APIs and local inference without rewriting your agent code. citeturn16view2turn5search1  
Avoid: optimizing for throughput if you only serve one receptionist session; prioritize consistent latency and stability instead. citeturn24view1turn16view2

**Evidence quality:** 5/5 (paired with strong systems paper) citeturn10search2turn16view2  
**Implementation readiness:** 4/5 (excellent if you have a supported GPU; otherwise limited) citeturn16view2

**Risks/limits:**  
Requires careful GPU/driver setup and model selection; CPU-only setups should consider lighter servers (e.g., llama.cpp). citeturn16view2turn16view1

## Books that map cleanly to thesis chapters

### entity["book","Human-Robot Interaction: An Introduction","2nd ed 2024"]

**Bibliographic info + link:**  
Bartneck, C., Belpaeme, T., Eyssel, F., Kanda, T., Keijsers, M., & Šabanović, S. (2024). *Human-Robot Interaction: An Introduction* (2nd ed.). entity["organization","Cambridge University Press","publisher"]. Link: https://www.cambridge.org/ag/universitypress/subjects/computer-science/computer-graphics-image-processing-and-robotics/human-robot-interaction-introduction-2nd-edition?format=PB&isbn=9781009424233 citeturn7search4turn7search0

**Why it belongs in your core reading (3–5 sentences):**  
This is a direct thesis-writing accelerator for background chapters: HRI concepts, how people perceive robots, and evaluation framing are explicitly within its scope. It is also one of the cleanest references to justify why you chose specific measures (e.g., perception, acceptance) and how embodiment changes interaction compared to disembodied voice assistants. Use it as your “HRI spine” to avoid scattered citations. citeturn7search4turn7search0

**Risks/limits:**  
Not implementation-specific; pair it with code-centric sources (LiveKit, Pepper docs, Pepper-GPT). citeturn24view0turn25view0turn15view1

---

### entity["book","Speech and Language Processing","3rd ed draft 2026"]

**Bibliographic info + link:**  
entity["people","Dan Jurafsky","nlp researcher"] & entity["people","James H. Martin","nlp researcher"]. (2026, Jan 6 draft). *Speech and Language Processing* (3rd ed. draft). Link: https://web.stanford.edu/~jurafsky/slp3/ citeturn7search1turn7search5

**Why it belongs (3–5 sentences):**  
This is the best single reference to justify your choices in ASR, dialogue, and speech generation—particularly if you need to explain streaming ASR/TTS constraints, evaluation metrics, and failure cases. It is also useful for writing the “voice pipeline” methods section rigorously (not as a blog-style stack description). citeturn7search1turn7search5

**Risks/limits:**  
It is broad; for implementation decisions you still need system-specific docs (LiveKit metrics and plugins). citeturn24view1turn24view4

---

### entity["book","Designing Voice User Interfaces","cathy pearl 2017"]

**Bibliographic info + link:**  
entity["people","Cathy Pearl","voice ux designer"]. (2017). *Designing Voice User Interfaces: Principles of Conversational Experiences.* Link: https://www.cathypearl.com/book citeturn7search18turn7search10

**Why it belongs (3–5 sentences):**  
Receptionist robots are “voice-first” interaction systems with strict expectations for politeness, turn-taking, repair, and fallback behaviors. This book gives practical VUI design rules and measurement ideas that you can turn into concrete dialogue requirements (e.g., confirmation strategies, handling misunderstandings, concise prompts). It complements HRI theory by translating it into conversation UX decisions. citeturn7search18turn7search10

**Risks/limits:**  
Voice UI guidance is not robot-specific; you must map recommendations into embodied behaviors (gesture/tablet cues) and HRI evaluation. citeturn22view0turn17view3

## Evaluation instruments and study design shaping

A receptionist robot evaluation usually needs to answer four questions: (1) was the information correct and usable (task success), (2) was the interaction smooth (latency, turn-taking, repair), (3) how did people perceive the robot socially (likeability, safety, intelligence), and (4) would people want to use it again (acceptance/intention). The strongest “minimal set” aligned with your assignment is:

Godspeed for social perception (anthropomorphism/animacy/likeability/perceived intelligence/safety). citeturn17view3turn9search7  
Almere Model constructs for acceptance/intention-to-use framing (adapt items for your population). citeturn17view2  
Objective system metrics for latency and responsiveness using decomposed measures (end-of-utterance delay + LLM TTFT + TTS TTFB). citeturn24view1  
Qualitative feedback + error taxonomy (“what went wrong?”) to connect engineering changes to user experience.

Two additional resources that are especially useful if you want modern, scale-focused HRI justification:

RoSAS (Robot Social Attributes Scale), a social-perception scale with dimensions warmth/competence/discomfort—often used as a modern alternative/complement to Godspeed. citeturn2search2turn23search3  
A scale discovery resource: the “Finding the Perfect Scale” database, which can support your justification for choosing a specific instrument set. citeturn23search6

RAG-specific evaluation is often neglected in robot theses, but it is central when your receptionist answers institutional questions. Strong, implementation-friendly tooling exists for evaluating retrieval + generation jointly (e.g., Ragas). citeturn23search1turn23search8

## Engineering patterns to reuse in your stack

image_group{"layout":"carousel","aspect_ratio":"16:9","query":["SoftBank Robotics Pepper robot receptionist","Pepper robot tablet interface at reception","Pepper robot gesture interaction","Pepper robot in public space human interaction"],"num_per_query":1}

### Real-time voice pipeline and latency instrumentation

The key to a “robot feels present” user experience is not only raw model speed, but **turn boundary detection** and predictable response timing. LiveKit provides concrete observability primitives for decomposing latency: it defines total conversation latency as approximated by end-of-utterance delay plus LLM time-to-first-token plus TTS time-to-first-byte, which is directly usable as your thesis metric definition. citeturn24view1

Also, LiveKit’s agent examples demonstrate capturing TTS metrics (TTFB, durations) without blocking call flow, which is useful for your “cloud vs local” backend comparison: you can record latency distributions under both backends with identical instrumentation. citeturn24view2turn24view1

If you decide to experiment with “realtime speech-to-speech” models (to reduce pipeline complexity), LiveKit documents that such models can bypass separate STT/TTS components and may capture emotional context better, but the turn-taking/VAD implications must be handled carefully. citeturn24view4

### RAG implementation details that matter for a university receptionist

Weaviate’s RAG description is directly aligned with your needs: a RAG query is “search + prompt,” where retrieval happens first and retrieved results are passed into the generative model. citeturn24view3  
For a receptionist, the “must-have” design constraint is that responses should be grounded in retrieved sources (and ideally cite or quote snippets), and should refuse when retrieval confidence is low—because institutional QA is not creative writing. This is consistent with the original RAG motivation: provenance and updatability are open problems for parametric-only models. citeturn18view0turn18view1

To make this thesis-grade rather than “demo-grade,” you should plan for at least: (a) document governance + metadata filters, (b) chunking strategy, (c) prompt rules that force citing retrieved passages, and (d) an evaluation loop that measures retrieval failures vs generation failures. The Ragas toolkit exists specifically for turning RAG evaluation into systematic, repeatable workflows. citeturn23search8turn23search1

### Cloud vs local LLM backend comparison

For the local side, vLLM is a strong candidate when you have GPU access: it supports streaming, OpenAI-compatible APIs, and is backed by a systems paper (PagedAttention) describing how it improves serving throughput and efficiency. citeturn16view2turn10search2  
If you need a CPU-friendly baseline (or want to run locally on modest hardware), llama.cpp provides an “OpenAI-compatible API server” mode and is explicitly designed for minimal setup and local inference across diverse hardware. citeturn16view1turn5search1

A good thesis comparison design is to hold everything constant (same prompts, same conversation scripts, same turn detection settings, same TTS voice) and vary only the LLM backend, measuring both objective latency metrics and perceived responsiveness (Godspeed + acceptance items). citeturn24view1turn17view3turn17view2

### Pepper-specific implementation constraints

Pepper development paths vary significantly by software generation; high-quality documentation hubs aggregate the relevant references. A practical starting point is the Pepper developer documentation hub which points you to QiSDK resources and multiple “dialogue-focused” tutorials (QiChat, linking dialogue and code, prosody, multimodal presentation). citeturn25view0  
For Pepper NAOqi 2.9 (Android/QiSDK-era) specifics, the SoftBank Robotics Labs “additional documentation” repo includes navigation best practices and conversion notes for animations—directly relevant for your gesture/animation requirement. citeturn25view2

## What to implement next week

1. **Create a measurable end-to-end latency baseline in your voice stack**: instrument end-of-utterance delay, LLM TTFT, and TTS TTFB exactly as defined in LiveKit docs; log distributions and store them per session. citeturn24view1turn24view2

2. **Implement a “receptionist action library” + schema-constrained router**: mirror the “high-level function library” pattern from ChatGPT-for-Robotics, but define only receptionist-safe actions (speak, gesture, show-tablet, ask-clarifying-question, retrieve-docs, escalate). citeturn19view1turn20view0

3. **Build a minimal Weaviate RAG path with strict grounding rules**: implement the two-step “retrieve → generate” flow and force answers to include citations/snippets from retrieved passages; add refusal on low retrieval confidence. citeturn24view3turn18view1turn15view2

4. **Stand up one local LLM server behind an OpenAI-compatible interface**: use vLLM (GPU) or llama.cpp-based servers (CPU/GPU) so your LiveKit agent can switch between cloud and local by changing an endpoint, not rewriting code. citeturn16view2turn16view1turn5search1

5. **Draft the evaluation packet and run a 3–5 person pilot**: combine Godspeed (perception), Almere-inspired acceptance items (intent-to-use), and a short qualitative form focused on breakdowns (mishearing, long pauses, wrong info, awkward gestures). Use the pilot to identify the top 5 failure modes you must engineer away before the real study. citeturn17view3turn17view2turn24view1