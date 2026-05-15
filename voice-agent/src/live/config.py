import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_str(name: str, default: str) -> str:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or not str(value).strip():
        return int(default)
    return int(value)


def _env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or not str(value).strip():
        return float(default)
    return float(value)


def _env_optional_int(name: str, default: int | None = None) -> int | None:
    value = os.getenv(name)
    if value is None or not str(value).strip():
        return default
    return int(value)

LANG = "en"
AGENT_VERSION = "0.6.4-llama-greet"
MODEL_NAME = "gpt-realtime-mini"
TTS_VOICE = "marin"
LOCAL_STT_MODEL = _env_str("LOCAL_STT_MODEL", "tiny")
LOCAL_STT_DEVICE = _env_str("LOCAL_STT_DEVICE", "cpu")
LOCAL_STT_COMPUTE_TYPE = _env_str("LOCAL_STT_COMPUTE_TYPE", "int8")
LOCAL_STT_CPU_THREADS = _env_int("LOCAL_STT_CPU_THREADS", 0)
LOCAL_LLM_BASE_URL = _env_str("LOCAL_LLM_BASE_URL", "http://localhost:8000/v1")
LOCAL_TTS_MODEL_PATH = Path(
    _env_str(
        "LOCAL_TTS_MODEL_PATH",
        str(BASE_DIR / "models" / "piper" / "en_US-hfc_female-medium.onnx"),
    )
)
LOCAL_TTS_USE_CUDA = False
LOCAL_TTS_SPEAKER_ID = _env_optional_int("LOCAL_TTS_SPEAKER_ID")
LOCAL_TTS_LENGTH_SCALE = _env_float("LOCAL_TTS_LENGTH_SCALE", 1.0)
LOCAL_TTS_NOISE_SCALE = _env_float("LOCAL_TTS_NOISE_SCALE", 0.667)
LOCAL_TTS_NOISE_W_SCALE = _env_float("LOCAL_TTS_NOISE_W_SCALE", 0.8)
LISTENER_IDENTITY = _env_str("LISTENER_IDENTITY", "listener-python")
MONITOR_IDENTITY = _env_str("MONITOR_IDENTITY", "monitor-python")
USER_IDENTITY = _env_str("USER_IDENTITY", "user")
LIVEKIT_URL = _env_str("LIVEKIT_URL", "ws://127.0.0.1:7880")
SESSION_IDLE_TIMEOUT_SEC = _env_float("SESSION_IDLE_TIMEOUT_SEC", 60.0)

AGENT_NAME = _env_str("PEPPER_AGENT_NAME", "pepper-local")
ORGANIZATION = "CTU Faculty of Electrical Engineering"
PLACE = "Charles Square"

# Weaviate vector search configuration.
WEAVIATE_HOST = _env_str("WEAVIATE_HOST", "localhost")
WEAVIATE_HTTP_PORT = _env_int("WEAVIATE_HTTP_PORT", 8080)
WEAVIATE_GRPC_PORT = _env_int("WEAVIATE_GRPC_PORT", 50051)
WEAVIATE_COLLECTION = "fel_v007"
WEAVIATE_OPENAI_MODEL = "text-embedding-3-large"
WEAVIATE_HYBRID_ALPHA = 0.7

DOC_TITLE_FIELD = "title"
DOC_CONTENT_FIELD = "content"
DOC_SOURCE_FIELD = "source"
DOC_CREATED_AT_FIELD = "created_at"
SEED_DATA_PATHS = [BASE_DIR / "data" / "FEL"]
SEED_LOG_PREFIX = "[weaviate-seed]"

ENABLE_QUERY_SEARCH = False  # ported off in favour of curated tools (lookup_person, find_path_to_room, mensa_menu, subject_schedule). Re-enable only after rewriting the trigger description with strict greeting carve-outs.
QUERY_SEARCH_DEFAULT_LIMIT = 5
QUERY_SEARCH_MAX_LIMIT = 8

# UDB staff-lookup tool — hits https://udb.fel.cvut.cz live for every call.
# Registered name MUST stay `lookup_person` in local mode (Qwen 2.5 7B):
# longer names like `lookup_fel_person` reproducibly leak <tool_call>
# tokens through the hermes parser when combined with query_search and
# play_pose. Validated by voice-agent/tests/tool_lookup_name_search.py.
ENABLE_LOOKUP_PERSON_TOOL = True

# Look-around vision tool — grabs a camera snapshot via the bridge and has a
# dedicated small VL model describe it, returning plain text to the main LLM.
# This avoids the chat-template tool-calling issues seen on Qwen2.5-VL main
# models and works uniformly for local + openai modes.
ENABLE_LOOK_AROUND_TOOL = False
LOOK_AROUND_HTTP_TIMEOUT_SEC = _env_float("LOOK_AROUND_HTTP_TIMEOUT_SEC", 3.0)
LOOK_AROUND_VISION_BASE_URL = _env_str("LOOK_AROUND_VISION_BASE_URL", "http://localhost:8001/v1")
LOOK_AROUND_VISION_MODEL = _env_str("LOOK_AROUND_VISION_MODEL", "Qwen/Qwen2.5-VL-3B-Instruct")
LOOK_AROUND_VISION_TIMEOUT_SEC = _env_float("LOOK_AROUND_VISION_TIMEOUT_SEC", 12.0)
LOOK_AROUND_VISION_MAX_TOKENS = _env_int("LOOK_AROUND_VISION_MAX_TOKENS", 150)
LOOK_AROUND_VISION_TEMPERATURE = _env_float("LOOK_AROUND_VISION_TEMPERATURE", 0.2)
LOOK_AROUND_VISION_PROMPT = _env_str(
    "LOOK_AROUND_VISION_PROMPT",
    "You are Pepper's visual describer. Describe what is in the camera frame in "
    "one or two short sentences. Focus on people (if any), objects, text/signs, "
    "and the general environment. Be concrete, do not speculate.",
)

# Pepper animation tool (voice-agent -> robot bridge).
# In local mode the AnimationDirector is the only path to animations —
# the LLM does not see `play_animation` as a callable tool. The tool
# implementation in tools.py is still used by the director (via
# `trigger_animation`) and by manual smoke tests.
ENABLE_ANIMATION_TOOL = False
# How much `adjust_volume` steps Pepper's speaker per call. The tool
# POSTs `/audio/volume {"delta": +VOLUME_STEP|-VOLUME_STEP}` to the
# bridge — the bridge owns the actual ALAudioDevice + state.json on
# the rpi side; the agent never touches state.json directly.
VOLUME_STEP = _env_int("VOLUME_STEP", 20)

ANIMATION_BRIDGE_URL = _env_str("ANIMATION_BRIDGE_URL", "http://127.0.0.1:5000")
ANIMATION_TOOL_HTTP_TIMEOUT_SEC = _env_float("ANIMATION_TOOL_HTTP_TIMEOUT_SEC", 2.5)
ANIMATION_TOOL_MAX_NAME_CHARS = 120

# Base form URL for the post-interaction feedback questionnaire. The
# `end_conversation` tool appends `?usp=pp_url&<ID_ENTRY>=T01` to
# pre-fill the conversation-ID field on the form, then encodes the
# resulting URL into a QR shown on the tablet.
EXPERIMENT_FEEDBACK_URL = _env_str(
    "EXPERIMENT_FEEDBACK_URL",
    "https://docs.google.com/forms/d/e/"
    "1FAIpQLSfCO5Z9cbQsEfodz6oBXe2umM6A0uzTgPntF3kPQXpjQf_MSg/viewform",
)
# Google-Forms `entry.<field-id>` parameter name for the Conversation-ID
# field. Pulled from the form's prefilled-URL helper — if the form is
# re-created the field ID changes, so swap this via env to update.
EXPERIMENT_FEEDBACK_ID_ENTRY = _env_str(
    "EXPERIMENT_FEEDBACK_ID_ENTRY",
    "entry.1544722281",
)
# Seconds to display the farewell QR before the session is ended.
EXPERIMENT_FAREWELL_DISPLAY_SEC = _env_int("EXPERIMENT_FAREWELL_DISPLAY_SEC", 30)

# Each group maps a semantic name (what the agent sees) to a list of actual
# Pepper animation keys.  The tool picks a random variant from the group so
# Pepper's movements feel natural and non-repetitive.
# Silent receptionist gesture set — every variant is from the
# `animations/Stand/Gestures/*` or `animations/Stand/BodyTalk/*`
# subtree (pure motion timelines, no embedded audio). Emotions/* and
# Reactions/* paths were dropped because their .qianim files include
# vocalisation tracks that bypass ALAudioPlayer's master volume.
ANIMATION_GROUPS: dict[str, list[str]] = {
    "greet":         ["Hey_1", "Hey_2", "Hey_3", "Hey_4", "Hey_6", "Hey_7", "Hey_8", "Hey_9", "Hey_10"],
    "bow":           ["BowShort_1", "BowShort_2", "BowShort_3"],
    "goodbye":       ["Kisses_1", "BowShort_1", "BowShort_2", "BowShort_3"],
    "affirm":        ["Yes_1", "Yes_2", "Yes_3", "Great_1"],
    "deny":          ["No_1", "No_2", "No_3", "No_4", "No_5", "No_6", "No_7", "No_8", "No_9"],
    "think":         ["Thinking_1", "Thinking_2", "Thinking_3", "Thinking_4", "Thinking_5",
                      "Thinking_6", "Thinking_7", "Thinking_8",
                      "Remember_1", "Remember_2", "Remember_3"],
    "explain":       ["Explain_1", "Explain_2", "Explain_3", "Explain_4", "Explain_5",
                      "Explain_6", "Explain_7", "Explain_8", "Explain_10", "Explain_11"],
    "emphasis":      ["Everything_1", "Everything_2", "Everything_3", "Everything_4",
                      "Everything_6", "Stretch_1", "Stretch_2"],
    "whisper":       ["Whisper_1"],
    "question":      [f"WhatSThis_{i}" for i in range(1, 17)],
    "calm":          ["CalmDown_1", "CalmDown_2", "CalmDown_3", "CalmDown_4", "CalmDown_5", "CalmDown_6"],
    "offer":         ["Give_1", "Give_2", "Give_3", "Give_4", "Give_5", "Give_6", "Take_1"],
    "address_user":  ["You_1", "You_2", "You_3", "You_4", "You_5",
                      "YouKnowWhat_1", "YouKnowWhat_2", "YouKnowWhat_3",
                      "YouKnowWhat_4", "YouKnowWhat_5", "YouKnowWhat_6"],
    "dont_know":     ["IDontKnow_1", "IDontKnow_2", "IDontKnow_3", "IDontKnow_4",
                      "IDontKnow_5", "IDontKnow_6", "DontUnderstand_1", "Confused_2"],
    "speak_neutral": [f"BodyTalk_{i}" for i in range(1, 17)],
}

# Flat set of all valid animation keys across all groups (for bridge validation).
ANIMATION_TOOL_ALLOWED: set[str] = {
    key for variants in ANIMATION_GROUPS.values() for key in variants
}

# Aliases let the agent use natural words that map to group names.
ANIMATION_TOOL_ALIASES: dict[str, str] = {
    "hello":         "greet",
    "hi":            "greet",
    "welcome":       "greet",
    "hey":           "greet",
    "greeting":      "greet",
    "thanks":        "bow",
    "thank_you":     "bow",
    "bye":           "goodbye",
    "farewell":      "goodbye",
    "yes":           "affirm",
    "agree":         "affirm",
    "confirm":       "affirm",
    "no":            "deny",
    "refuse":        "deny",
    "thinking":      "think",
    "consider":      "think",
    "searching":     "think",
    "info":          "explain",
    "information":   "explain",
    "describe":      "explain",
    "stress":        "emphasis",
    "important":     "emphasis",
    "secret":        "whisper",
    "quiet":         "whisper",
    "ask":           "question",
    "what":          "question",
    "reassure":      "calm",
    "calm_down":     "calm",
    "give":          "offer",
    "present":       "offer",
    "hand":          "offer",
    "you":           "address_user",
    "user":          "address_user",
    "uncertain":     "dont_know",
    "dontknow":      "dont_know",
    "i_dont_know":   "dont_know",
    "idk":           "dont_know",
    "shrug":         "dont_know",
    "neutral":       "speak_neutral",
    "default":       "speak_neutral",
}

BASE_SYSTEM_PROMPT = """
You are Pepper, a humanoid receptionist robot at CTU FEE in Prague (Karlovo náměstí).
Communicate in English, speak briefly, clearly, and politely.
If the user prefers another language, switch to it.

What you do:
- Provide information about FEE using the `query_search` tool.
- Answer CTU mensa or lunch menu questions using the `mensa_menu` tool. Mention one or two available meals, preferably from `suggested_meals`, unless the user asks for the full menu.
- Answer subject timetable questions using the `subject_schedule` tool when the user asks when or where a lecture, lab, exercise, or tutorial starts. If Czech and English subject codes share the same timetable, mention both codes and answer from the shared events.
- When you are unsure, use `query_search` instead of guessing.
- If the information is not available in the provided materials, say so directly and offer to clarify the question.
- Keep responses concise (typically 1–4 sentences), unless the user asks for more detail.
- Do not mention internal implementation details or library names.
""".strip()

_OPENAI_LOOK_AROUND_BLOCK = """

## Vision (look_around)

You have a top camera. Call the `look_around` tool whenever the user asks
about visible surroundings, objects, people, colours, signs, the room you
are in, or anything that needs visual context. The tool returns a short
text description of what is currently visible — use that description to
answer the user naturally. Do NOT announce that you are taking a photo,
and do NOT mention the describer or the tool itself."""

_LOCAL_LOOK_AROUND_BLOCK = (
    "When the user asks about what you see, who is in front of you, your "
    "surroundings, or any visual detail, call look_around first — it will "
    "return a short text description of the camera view (prefixed with "
    "'CAMERA VIEW:'). When you receive a CAMERA VIEW description you MUST "
    "restate concrete details from it in your spoken reply (people, "
    "objects, colours, text on signs, etc.). Do NOT ignore the description "
    "and fall back to generic answers about Pepper's location or job. "
    "If the description does not answer the user's question, say what you "
    "actually see and then offer to help further. "
)

_OPENAI_LOOKUP_BLOCK = """

## Staff lookup (lookup_person)

When the user names a member of FEL staff they want to see, call the
`lookup_person` tool with that name. Pass a full surname — prefixes like
"Hoff" return nothing, "Hoffmann" works. Diacritics do not matter.

Rules:
- If the tool returns count > 1, ask a disambiguating question (first
  name, department, role) before reading any contact info. Never silently
  pick the first match.
- If a field (phone, email, room) is null, say so honestly instead of
  inventing a fallback.
- If status == "not_found", try once more with a plausible alternate
  spelling (the user was transcribed by STT — common slips: missing
  doubled letters, missing diacritics, "-ová" feminine suffix). If still
  not found, ask the user to spell the surname.
- If status == "error" with off_network, apologise that the staff
  directory is unreachable and offer to take a message instead."""

OPENAI_SYSTEM_PROMPT = """
{base}

## Animations

You can trigger Pepper gestures with the `play_animation` tool.

Rules:
- For normal user-facing spoken replies, call `play_animation` exactly once.
- Use it at most once per reply.
- Skip it only for very short acknowledgements such as "yes" or "ok", urgent safety replies, or when the user asks for no gestures.
- When you use it, call the tool directly with one of these semantic animation names:
  `greeting`, `bow`, `explain`, `happy`, `thinking`, `dont_know`
- Suggested mapping:
  greeting or welcome -> `greeting`
  thanks or polite acknowledgement -> `bow`
  explaining information -> `explain`
  positive or cheerful answer -> `happy`
  searching or considering -> `thinking`
  uncertainty or missing information -> `dont_know`
- NEVER say tool names, tool arguments, bracketed action text, or stage directions aloud.{lookup}{vision}
""".strip().format(
    base=BASE_SYSTEM_PROMPT,
    lookup=_OPENAI_LOOKUP_BLOCK if ENABLE_LOOKUP_PERSON_TOOL else "",
    vision=_OPENAI_LOOK_AROUND_BLOCK if ENABLE_LOOK_AROUND_TOOL else "",
)

LOCAL_SYSTEM_PROMPT = (
    # Tuned for Llama 3.1 8B Instruct AWQ + vLLM llama3_json parser.
    # Ported from voice-agent/tests/local_llm_benchmark/prompt.py.
    #
    # Identity and persona FIRST, rules afterwards — the reverse
    # ordering causes Llama 3.1 to loop play_animation on the first
    # message of a fresh chat. No few-shot example replies in literal
    # quotes — the model treats them as patterns to match exactly,
    # which also triggers the loop.
    """\
You are Pepper, a humanoid receptionist robot at FEE (Faculty of
Electrical Engineering, CTU), Building E, Karlovo náměstí, Prague.

You greet users and help them with practical questions about FEE.

Personality: warm, brief, a little bit playful. You enjoy meeting
people. Speak like a friendly human receptionist, not a search engine.

What you ALREADY KNOW — never search for these:
  - You are Pepper, the friendly humanoid FEE receptionist.
  - You are in Building E on Karlovo náměstí, Prague.
  - FEE offers bachelor and master programmes in electrical
    engineering, informatics, and cybernetics.
  - The faculty staff directory (names, work phones, work emails,
    office rooms, departments) is PUBLIC information published by
    CTU. Sharing it with visitors is your normal job — never refuse
    a contact request on privacy grounds. If a tool returns staff
    contact details, read them out plainly.
  - Building E facilities — answer directly, do NOT call the
    room-directions tool for these:
      • Gym: in the basement, right behind the main staircase.
      • Café: right above the main staircase (first floor).
      • Toilets: right above the main staircase, next to the café.
      • Lockers: right next to the main staircase, on the right side
        (ground floor where you stand with the user).

Reply rules:
  - Replies are 1 to 3 short sentences. Stay in character as Pepper.
  - You speak through a TTS system. Reply in plain conversational
    prose only. Never use numbered lists, bullets, markdown, code
    blocks, line breaks for structure, or any formatting symbols.
    When listing several things, blend them into one sentence with
    commas and "and".
  - For greetings like "hi", "hello", "good morning", reply with a
    brief warm greeting in one sentence, no tool needed.
  - For smalltalk, opinions, or vague questions ("how are you?",
    "what can you do?"), reply directly as Pepper in 1-2 sentences,
    no tool needed.
  - Call a tool only when the user has named the concrete inputs
    the tool needs (a first name AND surname for a person lookup,
    a date for events, a course code for a timetable). If any
    required input is missing, ask the user for it in plain
    conversation instead of calling the tool with a guess.
  - When a tool result has `error: "missing_first_name"`, your next
    reply is plain text asking "What is their first name?" — do
    not call the tool again until the user has answered.
  - When a tool result has `error: "first_name_not_found"`, follow
    its `instruction`: tell the user that name was not found and
    ask them to confirm or correct the first name in plain text.
    Do not call the tool again until the user replies.
  - Before calling any tool, CHECK the earlier tool results in this
    conversation. If a previous tool result already contains the
    answer (e.g. you listed candidates and the user just picked
    one of them), reply directly from that prior result.
  - When a tool returns content, USE that content in your reply —
    quote names, numbers, rooms, phones, dates from the result.
  - When a tool returns multiple candidates and you need the user
    to pick one, briefly describe each candidate using its most
    distinguishing fields (first name plus role/title or
    department) and then ask which one. Never refer to candidates
    by index, ID, email, phone, or room code. Once the user picks,
    answer from that candidate's fields already in this
    conversation — do not call the tool again.
  - When mentioning a room, say ONLY the room code (for example
    "E-107" or "C-100"). NEVER read the street name, building
    address, or city — even if the tool result contains them.
  - Never speak tool names, argument names, JSON, or bracketed stage
    directions. Sound like Pepper, not a debugger.
  - Answer the user's actual question. Do not ask the user an
    unrelated question of your own.
  - For requests outside your role — booking, scheduling, personal
    opinions, jokes — explain briefly what you can help with instead.
    Do not refuse with a bare apology; redirect."""
    + (_LOCAL_LOOK_AROUND_BLOCK if ENABLE_LOOK_AROUND_TOOL else "")
)


# Extra system message appended on the user's first turn in local mode,
# via the GreetingAgent subclass in agent.py. Llama 3.1's chat template
# attaches the tools list to the FIRST user message, so steering the
# very first reply must happen AFTER the user message exists — which
# is what `llm_node` allows.
GREETING_INSTRUCTIONS = """\
Say: "Hello, how can I help you today?"
"""
