I screened this one in the same style. Overall, this is a strong and practically impressive MSc thesis: the system is real, deployed, technically nontrivial, and the writing is clearer than in many engineering theses. The main issues are formal draft artifacts, overclaiming in the local-vs-cloud comparison, and missing privacy/ethics/reproducibility details. The thesis is already close, but several fixes are necessary before submission.
Highest-priority blockers
Remove all draft markers. The footer still says v6.2-draft | May 20, 2026 throughout the PDF, including front matter and body pages. This must be removed for the final submission.
Fix PDF metadata. The PDF metadata still has /Title: titletitle and /Author: author. This should be corrected to the thesis title and Lukáš Navara.
Appendix A is broken. The Visitor Instruction Board appendix currently prints a LaTeX/include placeholder-like line:
width=!,height=!,pages=-,pagecommand=,width=
and the actual instruction board is missing. This is a hard blocker because Appendix A is explicitly referenced as the board used in the study.
Check Project Specification placement and content. The official assignment is placed as Appendix B at the end. If FEL/CTU expects the assignment immediately after the title page, move it. Also check the blank consultant field in the assignment: the title page lists Ing. Lukáš Rustler as supervisor-specialist, but the assignment’s second-supervisor/consultant field appears blank.
Resolve count mismatch in visitor preferences. The results say 19 of 26 preferred Pepper and 4 preferred a human, which sums to 23. The remaining 3 responses must be reported: “no preference,” “depends,” “other,” missing data, etc. This affects the headline claim in the abstract and conclusion.
Tone down the abstract’s “cloud backend came out ahead” statement. Only 20 responses were condition-matched, with 10 per condition. The cloud condition had higher descriptive means, but the thesis does not show inferential support. Use “was rated higher descriptively” rather than “came out ahead.”
Main conceptual/methodological issue
The most important scientific issue is that the study does not isolate the LLM backend. Condition A and Condition B differ in STT, LLM, and TTS simultaneously: local faster-whisper + Llama 3.1 8B + Piper versus OpenAI transcription + GPT-4o-mini + OpenAI TTS. Therefore, the study compares two complete local/cloud voice cascades, not only “locally hosted vs cloud-hosted LLM backends.” This should be made explicit in the title/abstract/conclusion wherever needed.
A precise sentence to add:
“Because the two conditions differ in speech recognition, language model, and speech synthesis components, the comparison should be interpreted as a comparison of complete local and cloud voice cascades rather than as an isolated test of the language model alone.”
This is especially important because the thesis attributes subjective advantages partly to GPT-4o-mini, but the smoother TTS voice and better STT could plausibly explain much of the perceived quality difference.
Study design and statistics
The study is a valuable field deployment, but it should be framed as exploratory. The sample is small: 26 total responses, only 20 matched to logs, with 10 per backend. The thesis already notes self-selection and experimenter priming, which is good, but it should go further: the alternating assignment is not full randomization, and time-of-day or visitor-type effects could correlate with condition.
For the latency analysis, the unit of analysis should be handled carefully. Table 4.1 reports 59 local turns and 44 cloud turns, but turns are nested within sessions and visitors. A better presentation would report per-session median TTFA, then compare the 10 local session medians with the 10 cloud session medians. This avoids pseudo-replication from treating every turn as independent.
For the subjective ratings, means are fine for a compact thesis, but add median/IQR or at least confidence intervals. With n=10 per condition, the thesis should avoid language such as “Condition B was better” and use:
“Condition B received higher descriptive ratings on all subjective items, but the small sample does not support a strong inferential claim.”
The “latency paradox” discussion is good: local TTFA is objectively faster, but cloud is rated faster subjectively. Keep this, but explicitly link it to the confound that cloud TTS/STT quality may change perceived speed.
Missing or underdeveloped evaluation pieces
The thesis would be much stronger with one compact task-success analysis from logs. The questionnaire asks whether visitors got the information they wanted, but the system likely has logs for tool calls. Add a small table:
number of sessions per condition,
number of user turns,
number of tool calls,
successful tool calls,
failed tool calls,
apologies/fallbacks,
average retries per tool call,
number of sessions ending normally vs timeout.
This would directly support RQ1 and RQ2 better than subjective ratings alone.
The RAG component is implemented, but apparently rarely used in the field because most reception questions went to live tools. That is fine, but the assignment explicitly asks for retrieval-augmented question answering. Add a small offline evaluation or demo table with 5–10 example institutional-document questions, retrieved source chunks, and whether the answer was correct. Otherwise, the RAG task is technically present but not really evaluated.
The gesture evaluation is weak relative to the claims. The system includes many gesture labels, but because gestures fire mainly on tool calls, visitors mostly see “thinking/lookup” gestures. The custom gesture item is useful, but the thesis should be clear that this is not a full evaluation of semantic gesture-speech congruence.
Privacy, ethics, and data handling
This is the biggest missing section. The system records/transcribes speech in a public reception area, sends data to OpenAI in the cloud condition, logs sessions, links questionnaire rows to session IDs, scrapes staff directory data, and may pick up background chatter. The thesis needs a short but explicit “Ethics and data handling” subsection in Chapter 4 or Chapter 6.
It should answer:
Were participants informed that their speech/transcripts/logs would be processed?
Was consent obtained before questionnaire submission or interaction?
Were raw audio recordings stored, or only transcripts/events?
How were logs anonymized?
Were bystanders protected, given the background-chatter issue?
What exactly leaves the institution in Condition B?
Are public directory lookups sent to OpenAI as part of user prompts or tool results?
How long are logs retained?
There is a second privacy issue: the “local” pipeline may not be fully local if query_search uses OpenAI text-embedding-3-large at ingestion or query time. The thesis says chunks are stored with OpenAI embedding vectors. If query embeddings are also computed through OpenAI, then Condition A is not fully local whenever RAG is used. Clarify this. Either switch to a local embedding model for the local condition, or write:
“The local cascade keeps speech recognition, dialogue generation, and speech synthesis local; however, the current RAG seeding/query embedding implementation uses OpenAI embeddings, so RAG use is not fully local unless embeddings are replaced by a local model.”
That caveat matters because the Discussion claims the local cascade keeps visitor audio, transcripts, and generated text on controlled hardware.
Cost comparison
The cost section is useful, but it currently risks sounding too definitive. The statement that local inference is “free afterwards” should be changed to “has no per-token API fee.” Local deployment still has electricity, maintenance, hardware depreciation, admin time, GPU opportunity cost, and model-update costs.
Also, API pricing changes quickly. Since the thesis cites OpenAI pricing, it should state an access date and ideally snapshot the exact token/audio rates used in a table or appendix. The official OpenAI pricing page is explicitly current and model-dependent, so the calculation should be presented as “using prices accessed on [date]” rather than as a durable number. (OpenAI)
Suggested wording:
“At prices accessed on May X, 2026, the observed Condition B sessions corresponded to approximately USD 0.29 per active conversation hour. This estimate excludes non-API operating costs and should be interpreted as a snapshot rather than a stable long-term price.”
Related work
The related work is focused and appropriate. The receptionist/guide robot history, LLM-social-robotics examples, gesture literature, and Godspeed/RoSAS/Almere evaluation discussion are all relevant.
A few improvements:
Add a short paragraph on privacy and data governance in cloud-based conversational robots, since the local/cloud comparison is one of the thesis’s central themes.
The FurChat comparison is strong and directly relevant; it should be used later in the Discussion when interpreting Pepper’s tool/RAG design, not only in Related Work. FurChat is a good precedent for LLM-based receptionist behavior with open- and closed-domain dialogue (Cherakara et al., 2023). (arxiv.org)
The discussion of OpenAI function/tool calling should cite a stable documentation snapshot or avoid depending too heavily on current API behavior, because OpenAI’s API docs and model names change.
The thesis cites “Moshi” and the OpenAI Realtime API as alternatives. This is useful, but the local/cloud comparison intentionally avoids end-to-end audio models. Make explicit that this was a design-control decision, not a claim that cascades are generally superior.
Figures and presentation
The system architecture figure is good and genuinely helps the reader understand the deployment. The deployment photo is also useful, because the glass partition and microphone placement are central to the field-study limitations. However, check privacy/consent for all photos that include identifiable people, even partially.
Figure 3.2’s caption is too informal and names a specific colleague:
“The transcript also doubles as a small spell-check: the French colleague MSc. Jason Khoury asked Pepper to find himself…”
Replace with something neutral:
“The live transcript helps users detect recognition errors; in this example, the speech recognizer mistranscribed a surname.”
Figures 4.2 and 4.3 are useful, but the bars with standard deviations are not enough for n=10 groups. Consider showing individual points overlaid on the bars, or use box/strip plots.
Language and consistency edits
High-priority edits:
“iteraction with receptionist” → “interaction with a receptionist.”
“Manualy prepared dictionary” → “Manually prepared dictionary.”
“This however did not work” → “However, this did not work.”
“came out ahead” → “received higher descriptive ratings.”
“the more pleasant one to talk to” → “was perceived as more pleasant to interact with.”
“free at inference time once the GPU is paid for” → “does not incur per-token inference fees once the hardware is available.”
“human judgement”/“behaviour”/“organised”/“synthesised”/“amortise” → standardize to American English if that is the target style: “judgment,” “behavior,” “organized,” “synthesized,” “amortize.”
“realtime” → “real-time.”
“local versus cloud deployment” → use consistently; avoid switching between “backend,” “cascade,” and “deployment” unless the distinction is intentional.
“Godspeed subscales, three custom Likert items” → “Godspeed subscales and three custom Likert items.”
“Pepper was placed at the standard reception desk” → “Pepper was placed behind the standard reception desk.”
“who they would prefer at the reception desk in general” → “whom they would prefer at the reception desk in general.”
“the author had prior familiarity with the instrument from earlier coursework” → this sounds weak as a scientific rationale. Keep “Godspeed is widely used”; move personal familiarity to an implementation note or delete.
Suggested abstract revision
A tightened version of the final abstract paragraph:
“The system was deployed for one day at a faculty reception desk. Twenty-six visitors completed a questionnaire combining four Godspeed subscales, three deployment-specific Likert items, and preference questions; 20 responses could be matched to logged sessions. Descriptively, the cloud cascade received higher subjective ratings, whereas the local cascade achieved lower median time-to-first-audio. Because the two conditions differed in speech recognition, language model, and speech synthesis, the results should be interpreted as an exploratory comparison of complete local and cloud voice cascades rather than as an isolated comparison of language models.”
Suggested limitations paragraph
Add this to Chapter 6:
“The A/B comparison should be interpreted cautiously. The two conditions differed not only in the language model but also in speech recognition and speech synthesis, so subjective differences cannot be attributed uniquely to the LLM. The study was also small, with only 10 matched sessions per condition, and the alternating assignment was vulnerable to time-of-day and visitor-population effects. Finally, passive recruitment failed and was replaced by experimenter-guided recruitment, which likely improved task discoverability but also changed participant expectations.”
References
Cherakara, N., Varghese, F., Shabana, S., Nelson, N., Karukayil, A., Kulothungan, R., Farhan, M. A., Nesset, B., Moujahid, M., Dinkar, T., Rieser, V., & Lemon, O. (2023). FurChat: An embodied conversational agent using LLMs, combining open and closed-domain dialogue with facial expressions. arXiv. https://arxiv.org/abs/2308.15214
OpenAI. (2026). API pricing. https://openai.com/api/pricing/
