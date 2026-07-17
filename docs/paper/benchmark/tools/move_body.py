"""The move_body gesture tool: real catalog + name resolution, stubbed execution.

Loads the annotated animation-metadata JSONs (Gemini annotations from
experiments/animation_metadata/data/metadata) at import time and builds:

  - CATALOG_TEXT: one plain-text line per animation
    (``Name: movement | communicative functions | tone | energy``)
  - ANIMATION_PROMPT_SECTION: instructions + catalog, appended to the
    system prompt by agent.py when config.ENABLE_MOVE_BODY is on
  - a lowercase name index used to validate and fuzzy-resolve whatever
    name the model passes

Execution is a stub: it logs the resolved animation and returns an
"ok, playing" payload instead of contacting the robot. For the live
integration, replace `_execute` with a call to
voice-agent/src/live/bridge_client.post_animation (mind `has_sound`
entries — route those through the bridge's `?sound=off` mute path).
"""
import difflib
import json
import logging
import re

import config

logger = logging.getLogger(__name__)


# --- Catalog build (import time, deterministic so the prompt is cache-stable) ---

def _tidy_caption(caption: str) -> str:
    """'The robot raises its left hand.' -> 'raises its left hand'."""
    c = (caption or "").strip()
    if c.startswith("The robot "):
        c = c[len("The robot "):]
    return c.rstrip(".")


def _load_entries() -> dict:
    files = sorted(config.ANIMATION_METADATA_DIR.glob("*.json"))
    entries = {}
    for path in files:
        d = json.loads(path.read_text(encoding="utf-8"))
        entries[d["name"]] = {
            "caption": _tidy_caption(d.get("short_caption", "")),
            "functions": d.get("communicative_functions", []),
            "tone": d.get("social_tone", []),
            "energy": d.get("motion_energy", ""),
            "has_sound": d.get("has_sound", False),
        }
    if not entries:
        raise RuntimeError(
            f"move_body: no animation metadata found in {config.ANIMATION_METADATA_DIR}"
        )
    return entries


_ENTRIES = _load_entries()
_NAME_BY_LOWER = {name.lower(): name for name in _ENTRIES}

CATALOG_TEXT = "\n".join(
    f"{name}: {e['caption']} | {', '.join(e['functions'])} | "
    f"{', '.join(e['tone'])} | {e['energy']}"
    for name, e in sorted(_ENTRIES.items())
)

# Speaking style for the embodied-chat experiments (kept out of prompt.py so
# MOVE_BODY_MODE="off" remains the exact original benchmark prompt). Short
# single-clause sentences also give the inline mode clean per-sentence tag
# anchors.
_SPEAKING_STYLE_SECTION = """
## Speaking style
Speak in short, simple sentences. Put one idea in each sentence and end it \
with a period — where you would chain clauses with a comma, start a new \
sentence instead. Use at most three sentences per answer.
"""

ANIMATION_PROMPT_SECTION = f"""{_SPEAKING_STYLE_SECTION}
## Body movement
You have a physical robot body. Call move_body to play one animation that \
supports what you are about to say — when you greet, say goodbye, apologize, \
agree, decline, explain, point, wait, or react emotionally. Call it before \
writing your reply and pick the animation whose movement, communicative \
function and tone best match the meaning of that reply. Call it at most once \
per reply, and pass the animation name exactly as it appears in the list \
below. For plain factual follow-ups where movement would be distracting, \
answer without it.

Available animations (Name: movement | communicative functions | tone | energy):
{CATALOG_TEXT}
"""


INLINE_PROMPT_SECTION = f"""{_SPEAKING_STYLE_SECTION}
## Body movement
You have a physical robot body and you always move while you speak. Start \
every sentence with an animation tag — an animation name in square brackets. \
Pick the animation whose movement, communicative functions and tone best \
match what that sentence says: greeting, saying goodbye, apologizing, \
agreeing, declining, explaining, pointing, waiting, or reacting emotionally. \
Use only names exactly as they appear in the list below.

Reply format example (every sentence starts with its own tag):
[Hey_1] Hi there. [Explain_3] The canteen is on the ground floor. [BowShort_1] Enjoy your visit.

Available animations (Name: movement | communicative functions | tone | energy):
{CATALOG_TEXT}
"""


# --- Name resolution ---

def _resolve(raw: str) -> tuple[str | None, list[str]]:
    """Resolve the model-provided name to a canonical catalog name.

    Returns (canonical_name, suggestions). canonical_name is None when the
    name didn't resolve; suggestions then holds the closest catalog names.
    """
    key = (raw or "").strip().strip("'\"").lower()
    if key in _NAME_BY_LOWER:
        return _NAME_BY_LOWER[key], []
    close = difflib.get_close_matches(key, _NAME_BY_LOWER.keys(), n=3, cutoff=0.6)
    return None, [_NAME_BY_LOWER[c] for c in close]


# --- Tool ---

MOVE_BODY_SCHEMA = {
    "type": "function",
    "function": {
        "name": "move_body",
        "description": (
            "Play one animation on your robot body to accompany your reply. "
            "Call this when a body gesture supports what you are about to "
            "say (greeting, farewell, apology, agreement, explaining, "
            "pointing, emotional reaction). Pass an animation name from the "
            "'Available animations' list in your instructions."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "animation_name": {
                    "type": "string",
                    "description": (
                        "Exact animation name from the Available animations "
                        "list, e.g. 'Hey_1'."
                    ),
                }
            },
            "required": ["animation_name"],
            "additionalProperties": False,
        },
    },
}


def _execute(name: str) -> None:
    """Stub for the real robot call (bridge_client.post_animation)."""
    logger.info("move_body: would play %r on the robot", name)


def move_body(animation_name: str) -> dict:
    name, suggestions = _resolve(animation_name)
    if name is None:
        logger.info(
            "move_body: unresolved name %r (suggestions: %s)",
            animation_name, suggestions,
        )
        return {
            "error": "unknown_animation",
            "animation_name": animation_name,
            "did_you_mean": suggestions,
            "instruction": (
                "Retry move_body with one name from did_you_mean or from "
                "the Available animations list, or answer without moving."
            ),
        }
    _execute(name)
    entry = _ENTRIES[name]
    return {"status": "playing", "animation": name, "movement": entry["caption"]}


# Registry: tool name -> (callable, schema). Same shape as find_room.TOOLS.
TOOLS = {"move_body": (move_body, MOVE_BODY_SCHEMA)}


# --- Inline-tag mode ---

def play_animation(name: str) -> str:
    """Trigger one already-resolved animation; returns its caption.

    This is the plain-Python entry point the inline parser fires — the
    live integration swaps `_execute` for the robot bridge call.
    """
    _execute(name)
    return _ENTRIES[name]["caption"]


# A tag candidate is name-like text between brackets: letters/digits/
# underscores with at least one letter. Anything longer than the longest
# catalog name (+ slack), containing other characters, or letter-free
# (e.g. a "[1]" citation) is ordinary prose, not a gesture attempt.
_TAG_MAX_LEN = 40
_TAG_LIKE = re.compile(r"^(?=.*[A-Za-z])[A-Za-z0-9_ ]+$")


class InlineGestureParser:
    """Incremental filter for streamed LLM text.

    Feed it text deltas; it returns the text with gesture tags removed,
    firing `on_gesture(name)` at the moment a `[AnimationName]` tag
    completes — tags may arrive split across any number of deltas.
    Name-like tags that don't resolve are stripped and recorded in
    `misses`; non-name brackets (citations, asides) pass through as text.
    Call `flush()` after the stream ends to release a dangling '['.
    """

    def __init__(self, on_gesture):
        self._on_gesture = on_gesture
        self._buf = None  # None = passthrough; str = inside a '[...' candidate
        self.fired = []
        self.misses = []

    def feed(self, delta: str) -> str:
        out = []
        for ch in delta:
            if self._buf is None:
                if ch == "[":
                    self._buf = ""
                else:
                    out.append(ch)
            elif ch == "]":
                out.append(self._close_tag())
            elif ch == "[":
                # New '[' inside a candidate: what we buffered was prose.
                out.append("[" + self._buf)
                self._buf = ""
            else:
                self._buf += ch
                if len(self._buf) > _TAG_MAX_LEN or ch == "\n":
                    out.append("[" + self._buf)
                    self._buf = None
        return "".join(out)

    def _close_tag(self) -> str:
        candidate, self._buf = self._buf, None
        name, suggestions = _resolve(candidate)
        if name is not None:
            self.fired.append(name)
            self._on_gesture(name)
            return ""
        if _TAG_LIKE.match(candidate or ""):
            logger.info(
                "inline gesture: unresolved tag %r (suggestions: %s)",
                candidate, suggestions,
            )
            self.misses.append((candidate, suggestions))
            return ""
        return f"[{candidate}]"

    def flush(self) -> str:
        """End of stream: return any buffered text that never closed."""
        if self._buf is None:
            return ""
        out = "[" + self._buf
        self._buf = None
        return out


if __name__ == "__main__":
    # Quick preview: catalog size + a resolution smoke test.
    n_lines = len(_ENTRIES)
    n_chars = len(CATALOG_TEXT)
    print(f"catalog: {n_lines} animations, {n_chars} chars (~{n_chars // 4} tokens)")
    print(f"prompt section total: ~{len(ANIMATION_PROMPT_SECTION) // 4} tokens")
    print(f"with sound: {sum(1 for e in _ENTRIES.values() if e['has_sound'])}")
    print("\nfirst 3 lines:")
    for line in CATALOG_TEXT.splitlines()[:3]:
        print(f"  {line}")
    print("\nresolution smoke test:")
    for probe in ("Angry_3", "angry_3", " 'Hey_1' ", "Anrgy_3", "TotallyMadeUp_9"):
        resolved, sugg = _resolve(probe)
        print(f"  {probe!r} -> {resolved!r}  suggestions={sugg}")

    print("\ninline parser smoke test:")
    stream = ["Hel", "lo the", "re! [He", "y_1] Welcome to buil",
              "ding E [1] (see [Anrgy_3] docs)", ", enjoy [Bo"]
    parser = InlineGestureParser(lambda name: print(f"    fired: {name}"))
    text = "".join(parser.feed(d) for d in stream) + parser.flush()
    print(f"  clean text: {text!r}")
    print(f"  fired={parser.fired}  misses={parser.misses}")
