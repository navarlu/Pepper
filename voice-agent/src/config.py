import os


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


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None or not str(value).strip():
        return bool(default)
    return value.strip().lower() in ("1", "true", "yes", "on")


# Inline gesture tags (agent.py only): the LLM starts every
# sentence with an [AnimationName] tag from the annotated 390-clip
# catalog; llm_node strips the tags and dispatches them to the bridge.
# See tools/utils/_inline_gestures.py. Set 0 for a no-gesture baseline.
ENABLE_INLINE_GESTURES = _env_bool("ENABLE_INLINE_GESTURES", True)

# Robot bridge (voice-agent -> robot/src/bridge.py).
ANIMATION_BRIDGE_URL = _env_str("ANIMATION_BRIDGE_URL", "http://127.0.0.1:5000")
ANIMATION_TOOL_HTTP_TIMEOUT_SEC = _env_float("ANIMATION_TOOL_HTTP_TIMEOUT_SEC", 2.5)

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
