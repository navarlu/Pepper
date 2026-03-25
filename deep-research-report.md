# LLM-Driven Receptionist Robot on Pepper: CTU FEE Knowledge Corpus and Implementation Notes

## Context and scope constraints

Your thesis targets a receptionist-style social robot deployment at entity["organization","CTU Faculty of Electrical Engineering","faculty, prague"] under entity["organization","Czech Technical University in Prague","public university, prague"], specifically on the entity["point_of_interest","Karlovo náměstí","prague, czechia"] campus. The faculty’s public-facing web presence already contains many of the “reception desk” facts visitors repeatedly ask for: where departments are located, how to enter the campus, room-numbering conventions, maps of classrooms, key contacts, and office hours. citeturn28search3turn17view0turn5search1turn7search4

Within the Karlovo náměstí site cluster, the faculty explicitly lists four departments/workplaces there (Cybernetics, Control Engineering, Computers, Computer Graphics & Interaction). citeturn5search1turn17view0 This makes the web corpus scoping very practical: you can build a strong receptionist knowledge base without crawling the entire university.

Supervisor noted in your project brief: entity["people","Matěj Hoffmann","ctu fel researcher"].

## Current codebase reality check from the rpi branch

Your rpi branch already contains most of the structural “hooks” needed to scale the receptionist experience; the main blockers are (a) what content gets into Weaviate, (b) how updates happen over time, and (c) how much of Pepper’s expressive bandwidth is exposed to the LLM.

**RAG data model and seeding behavior (voice-agent).**  
The voice agent is configured to use a Weaviate collection named `fel_v003`, with OpenAI embeddings (`text-embedding-3-large`) and hybrid search `alpha=0.7`. fileciteturn31file0 fileciteturn32file0  
Critically, the built-in seeding flow only reads `*.txt` files from `SEED_DATA_PATHS = [voice-agent/data/FEL]` (not PDFs), and it only seeds when the collection does **not** already exist. That means “adding more documents” won’t automatically take effect in a long-lived deployment unless you (1) delete/recreate the collection, or (2) add an incremental ingestion path. fileciteturn31file0 fileciteturn32file0 fileciteturn34file0

**Tooling constraints that currently limit “human-likeness.”**  
The `play_animation` tool is intentionally hard-gated: only six animation keys are allowed (`Hey_1`, `BowShort_1`, `Explain_1`, `Happy_1`, `Thinking_1`, `IDontKnow_1`) and the system prompt explicitly instructs the model to use only those. fileciteturn31file0 fileciteturn33file0  
On the robot side, you already have a large animation mapping in `robot/data/animations.json`, meaning your system is technically ready to expose far more gestures—your constraint is primarily the **LLM-side allowlist**, not Pepper capability discovery. fileciteturn23file0

**Two parallel Weaviate ingestion tracks exist today.**  
In addition to the voice-agent’s `fel_v003` corpus, your repo includes a separate thesis utility that ingests PDFs into another collection, `resources_v001`, chunked at ~3000 characters with 300-character overlap. fileciteturn35file0 This is useful, but it will fragment your knowledge base unless you deliberately converge on a single collection strategy (or build cross-collection querying).

**You already started curating receptionist-friendly “facts” as text.**  
For example, `voice-agent/data/Contacts/study_office.txt` contains a structured, LLM-friendly summary (address, responsibilities, staff, office hours). fileciteturn36file0  
However, that folder is not included in `SEED_DATA_PATHS` right now (only `/data/FEL` is), so these curated contact files will not be searchable unless you move them under `/data/FEL` or extend `SEED_DATA_PATHS`. fileciteturn31file0 fileciteturn32file0

## High-value cvut.cz web corpus to ingest into Weaviate

Below is a prioritized list of **cvut.cz** pages and PDFs that are directly relevant to a Pepper receptionist at Karlovo náměstí. I’m separating (A) “must-have receptionist facts” from (B) “nice-to-have depth,” because RAG quality depends more on **coverage of frequent intents** than on raw document volume.

All URLs are provided in code blocks for copy/paste. The citations immediately after each block point to the corresponding cvut.cz sources.

### Core receptionist facts: where to go, who to contact, when it’s open

```text
https://fel.cvut.cz/en/faculty/contacts
https://fel.cvut.cz/cs/fakulta/kontakty

https://fel.cvut.cz/en/faculty/faculty-structure/administrative-offices
https://fel.cvut.cz/en/faculty/faculty-structure/administrative-offices/study-office
https://fel.cvut.cz/cs/fakulta/struktura-fakulty/dekanat/studijni-oddeleni
https://fel.cvut.cz/en/admissions/admission-procedures/contact
```
citeturn28search3turn24search5turn28search2turn28search0turn28search1turn28search6

Why these matter: the Contacts pages enumerate campus addresses (including Karlovo náměstí) and link to navigation assets (maps of rooms/classrooms, interactive navigation). citeturn28search3 The Study Office pages include concrete office hours and role-based contacts—classic receptionist questions. citeturn28search1turn28search0

### Karlovo campus navigation: room codes, classroom/floor maps, and interactive navigation entrypoint

```text
https://fel.cvut.cz/cs/fakulta/mapa-mistnosti-a-uceben/mapa-mistnosti
https://fel.cvut.cz/cs/fakulta/mapa-mistnosti-a-uceben/interaktivni-navigace
https://fel.cvut.cz/cs/fakulta/mapa-mistnosti-a-uceben/mapy-uceben/ucebny-karlovo-namesti

https://fel.cvut.cz/en/faculty/map-of-rooms-and-classrooms/maps-of-classrooms/classrooms-karlovo-namesti
```
citeturn19view1turn19view0turn23search1turn33view0

Why these matter: `mapa-mistnosti` explains the **room-numbering scheme** and explicitly defines “KN” as Karlovo náměstí 13, which is essential for reliable wayfinding answers (“KN:E-301 means Karlovo campus, building E, 3rd floor…”). citeturn19view1 The classroom map pages link to floorplan images for building E/G. citeturn23search1turn33view0

### Karlovo campus “meta page”: what’s in the campus, with map and IT notes

```text
https://www.felk.cvut.cz/
```
citeturn5search1

Why this matters: the FELK page is a compact “campus landing page” naming the departments on Karlovo náměstí and providing an embedded map and local IT references. citeturn5search1

### Department-specific “How to find us” guidance for real visitors

These pages are valuable because they contain exactly the sort of instructions a human receptionist repeats: **how to enter**, where the department physically is (building E/G), and how to travel there.

```text
https://control.fel.cvut.cz/en/management-and-contacts
https://control.fel.cvut.cz/vedeni-kontakty

https://cyber.felk.cvut.cz/department/contacts/
https://cyber.felk.cvut.cz/cs/department/contacts/

https://dcgi.fel.cvut.cz/en/contacts/
https://cmp.felk.cvut.cz/new_pages/contacts/
```
citeturn7search4turn24search3turn7search1turn7search2turn13view0turn6search0

One especially receptionist-relevant detail: the Control Engineering page explains that the Karlovo premises are entered through turnstiles at the main reception in building A (Faculty of Mechanical Engineering) and then via the glazed atrium to the courtyard; it also states the department is in buildings E and G. citeturn7search4 This is exactly the kind of instruction Pepper should answer consistently.

### IT “first day” questions: passwords, eduroam, ServiceDesk, and “how do I log in?”

For a university receptionist robot, IT access questions are frequent—especially from new students (Wi‑Fi, passwords, “First password,” ServiceDesk).

```text
https://svti.fel.cvut.cz/en/
https://svti.fel.cvut.cz/en/start/student.html
https://svti.fel.cvut.cz/cz/services/passwords.html
https://ist.cvut.cz/podpora/navody/servicedesk-zakladni-informace/
https://servicedesk.cvut.cz/
https://ist.cvut.cz/nase-sluzby/heslo-cvut/prvni-heslo/
https://usermap.cvut.cz/password
```
citeturn26search4turn27search4turn25search5turn25search0turn25search1turn25search3turn26search2

Why these matter: these pages define the institutional “canonical workflow” for credentials (CTU password, first password, where to reset), and they describe ServiceDesk as the central support tool. citeturn25search0turn25search1turn25search3

### Student orientation content that also helps visitors: “Life at FEL” and freshman survival guide

These sources are useful for “soft” receptionist conversations (“where do classes happen,” “where to eat,” “what does KN mean,” “what are main lecture halls”).

```text
https://fel.cvut.cz/cs/uchazeci/zivot-na-fel
https://fel.cvut.cz/cs/uchazeci/zivot-na-fel/pruvodce-prvakem
https://fel.cvut.cz/en/admissions/life-at-fee-ctu
https://fel.cvut.cz/en/admissions/life-at-fee-ctu/arrival-guide
```
citeturn24search7turn23search0turn36search3turn36search0

Example of why it’s valuable: the Life-at-FEL page explicitly describes the Karlovo campus marking “KN,” lists the two named lecture halls (Zengerova and Šrámkova) and ties them to room identifiers (e.g., KN:E‑107 and KN:E‑301). citeturn24search7

### PDFs you can ingest as-is for “arrival week” conversations

These are ideal for Weaviate because they are already structured and short.

```text
https://intranet.fel.cvut.cz/en/admissions/before-and-after-arrival-guide.pdf
```
citeturn38view0

This PDF includes “first steps after arrival,” references to where to obtain the university ID card, and other onboarding logistics. citeturn38view0

### Study plans and academic calendar pages (optional but high-impact for student-heavy interactions)

If your receptionist scenario includes prospective and current students asking about schedules and curriculum structure, these sources are high-value. Note: these pages can be deep/large; you’ll want to scope them to the most relevant programs for Karlovo (e.g., Open Informatics, Cybernetics/Robotics-related tracks) rather than bulk-ingesting everything.

```text
https://intranet.fel.cvut.cz/cz/education/harmonogram.html
https://intranet.fel.cvut.cz/cz/education/bk/plany/pl30007918.html
https://intranet.fel.cvut.cz/cz/education/bk/plany/pl30021567
```
citeturn30search0turn29search8turn29search4

### Policies and “what we are allowed to do with AI” (recommended for safety and governance)

Even if not directly asked by visitors, this content is extremely useful for your system’s **guardrails**: it supports refusal behaviors and privacy/safety messaging when users ask for disallowed actions or sensitive processing.

```text
https://intranet.fel.cvut.cz/cz/rozvoj/MP-pouzivani-ui.pdf
https://intranet.fel.cvut.cz/cz/rozvoj/MP-eticke-principy.pdf
https://intranet.fel.cvut.cz/cz/rozvoj/predpisy
```
citeturn32search39turn32search40turn32search3

## Practical ingestion strategy tailored to your current implementation

Because your current voice-agent seeding pipeline reads `*.txt` from `voice-agent/data/FEL` and only seeds on first collection creation, the fastest “works tomorrow” approach is:

1) **Keep using curated `.txt` sources for receptionist facts**, rather than ingesting raw HTML dumps. Your existing `study_office.txt` is a good model: it’s short, role-structured, and directly answerable. fileciteturn36file0  
2) **Expand `SEED_DATA_PATHS` to include your curated folders**, or move curated files under `voice-agent/data/FEL`. As-is, only `/data/FEL` is seeded. fileciteturn31file0 fileciteturn32file0  
3) **Add an incremental ingestion pathway** so updates take effect without deleting the collection. Right now, `seed_collection()` returns immediately if the collection exists. fileciteturn32file0  
4) Decide whether you want one Weaviate corpus or two. Today you have:
   - `fel_v003` for runtime QA in the voice agent, fileciteturn31file0
   - `resources_v001` for a thesis PDF ingestion workflow. fileciteturn35file0  
   If the receptionist must answer from the same knowledge base you evaluate, converging on **one** collection typically makes both engineering and thesis methodology cleaner.

If you do want to ingest PDFs directly (instead of converting to curated `.txt`), you already have a working reference implementation: `docs/thesis/resources/ingest_data.py` (PDF → text extraction → chunking → insert). fileciteturn35file0 The missing link is wiring that same approach into the voice-agent’s collection (or having the voice agent query `resources_v001` too).

## How the proposed corpus maps to receptionist behaviors and robot UI

Once ingested, the above corpus supports a highly practical intent set:

- **Wayfinding & campus entry**: “How do I get to Department X?”, “How do I enter the Karlovo campus?”, “What does KN:E‑301 mean?” (Control “turnstiles + atrium” guidance, room-code scheme pages, classroom maps). citeturn7search4turn19view1turn23search1  
- **Office hours & who handles what**: Study Office opening hours and agenda distribution (includes differentiating admissions vs Erasmus vs degree studies). citeturn28search1turn28search5  
- **New student IT onboarding**: “How do I set my first password?”, “How do I connect to eduroam?”, “Where do I report an IT issue?” (SVTI + IST + ServiceDesk). citeturn25search3turn26search4turn25search1  
- **Visitor-friendly “Life at FEL” facts**: Karlovo campus naming, lecture halls, food options, and how to travel between campuses. citeturn24search7turn36search3  
- **Policy-aware guardrails**: When asked about questionable uses of AI or sensitive requests, you can ground refusals and guidance in institutional rules. citeturn32search39turn32search40

On the non-verbal side, your current system prompt explicitly encourages calling `play_animation` once in most answers. fileciteturn31file0 If you expand the gestures, the web-derived intents above map cleanly to “gesture semantics” (e.g., greeting, pointing/explaining, thinking/searching, apologizing/uncertainty). The main engineering step is to expand `ANIMATION_TOOL_ALLOWED` and keep strong validation in the tool layer (which you already have via normalization + allowlist enforcement). fileciteturn31file0 fileciteturn33file0

## Risks, quality controls, and what to log for later thesis evaluation

Because your system prompt already forbids implementation details in user-facing speech and encourages tool use for uncertainty, your next big quality lever is **knowledge freshness and provenance**. fileciteturn31file0

For a receptionist robot on a live campus, the main failure modes to plan for are:

- **Stale operational facts** (office hours, contacts, rooms repurposed): mitigate via periodic re-ingestion or a “last updated” metadata field surfaced in answers. Office hours are explicitly published in faculty pages and can change seasonally (e.g., “summer office hours”). citeturn28search0turn28search1  
- **Over-ingesting dynamic content** (news feeds, event pages) which dilutes retrieval: prefer stable navigation/contacts pages and curated summaries. The Contacts and map pages are designed as stable references compared to news/event streams. citeturn28search3turn19view1  
- **Privacy and acceptable-use expectations**: even if pages are public, your robot’s interaction log may create new privacy considerations. It is valuable to align your “what I can/can’t do” messaging with institutional AI-use guidance and ethics guidance documents. citeturn32search39turn32search40  
- **Corpus fragmentation across collections and formats**: right now, your runtime agent queries a specific collection (`fel_v003`) with specific fields; the PDF ingestion script targets a different collection (`resources_v001`). fileciteturn31file0 fileciteturn35file0

A final codebase note: your repo already contains a literature/research drafting artifact (`docs/thesis/resources/deep-research-report.md`) that can serve as your “related work” staging area and methodology checklist—even if you keep the **web corpus** strictly on cvut.cz for retrieval. fileciteturn26file0