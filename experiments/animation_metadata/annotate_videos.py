"""Annotate recorded animation clips with Gemini (native video understanding).

For every clip in data/recordings/ this script:
  1. measures when the motion starts/ends via frame differencing
     (-> duration_s, measured locally, not trusted to the VLM),
  2. sends the whole video inline to Gemini with a structured-output schema,
  3. merges the VLM annotation with our own fields (action_id from
     animations.json, source_label derived from the animation name).

The VLM is given the animation's designer name as context (intent), but is
instructed to describe what is actually visible and to also capture
alternative readings of the motion (a gesture designed as "angry" may also
read as "enthusiastic emphasis") — the goal is a rich, honest description a
downstream LLM can use to pick a movement matching the conversation context.

Output: data/metadata/<name>.json per clip.
Already-annotated clips are skipped, so the script is resumable.

Needs GEMINI_API_KEY in the project .env.

Run:  uv run python experiments/animation_metadata/annotate_videos.py
"""

import json
import os
import re
import time
from typing import List, Literal

import cv2
import numpy as np
from dotenv import load_dotenv
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

# --- Configuration ---------------------------------------------------------
ANNOTATION_MODEL = os.environ.get("GEMINI_ANNOTATION_MODEL", "gemini-3.5-flash")
LIMIT = None               # int -> annotate only the first N pending clips
                         # (output check before the full run); None -> all
MOTION_THRESHOLD = 0.7   # mean gray diff (160x90) above which a frame counts as
                         # motion — Pepper's moving limb is a small share of the
                         # frame, so the whole-frame mean stays low (idle ~0.35).
                         # Only a fallback: gesture duration comes from the
                         # recorder manifest (Pepper's own elapsed_s) when present.
MOTION_PAD_SEC = 0.3     # padding around detected motion bounds
INLINE_LIMIT_MB = 15     # inline video request limit safety margin (API cap ~20 MB)
API_MAX_RETRIES = 5      # retries for transient 503/429/500 from the VLM
API_BACKOFF_SEC = 4.0    # base backoff, doubled each retry (4/8/16/32 s)
THINKING_LEVEL = "minimal"  # gemini-3.x reasoning depth; minimal = lowest latency,
                            # enough for this bounded description task
MEDIA_RESOLUTION = "low"    # low|medium|high — low is 66 tokens/frame (vs 258),
                            # faster and cheaper; ample for gross-gesture description

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RECORDINGS_DIR = os.path.join(SCRIPT_DIR, "data", "recordings")
METADATA_DIR = os.path.join(SCRIPT_DIR, "data", "metadata")
MANIFEST_PATH = os.path.join(SCRIPT_DIR, "data", "recordings_manifest.json")
ANIMATIONS_JSON = os.path.join(SCRIPT_DIR, "..", "..", "robot", "data", "animations.json")
SOUND_LIST_PATH = os.path.join(SCRIPT_DIR, "data", "animations_with_sound.json")
# ---------------------------------------------------------------------------

PROMPT = """You are annotating the gesture repertoire of a Pepper humanoid robot \
that works as a receptionist at a university. The attached video shows ONE \
animation performed by the robot, starting and ending in its neutral rest pose.

The animation ships on the robot as "{name}" — the last path segment is the \
designer's name for it, the folder its category ("Waiting" animations are \
standalone idle/entertainment pieces, "BodyTalk" subtle conversational \
motions). Treat this as the INTENDED meaning, but describe what is actually \
visible in the video — robot gestures \
are often ambiguous, and the same motion can carry meanings beyond the \
designer's intent (a motion named "angry" may equally read as enthusiastic \
emphasis). Capture both: ground every judgment in the visible motion, use \
the name to resolve ambiguity about intent, and record additional plausible \
readings in alternative_interpretations.

Describe the gesture for a gesture-selection system used by a dialogue agent: \
a downstream LLM will read only your annotation (never the video) to pick a \
movement matching the current conversation context. Guidance for specific fields:
- alternative_interpretations: other plausible readings of the motion for a \
viewer who does not know the name, phrased like communicative_functions \
entries (e.g. a gesture named "angry" might also list "emphasis", \
"enthusiasm"). Empty list if the motion is unambiguous.
- movement_description: 2-4 sentences narrating the WHOLE movement in \
chronological order: which body parts move, how (trajectory, hand shape, \
head orientation), and how it resolves. Written so someone who cannot see \
the video could picture the motion.
- speed: overall tempo of the motion — "slow" (calm, gradual), "moderate" \
(conversational pace), or "fast" (brisk, snappy).
- starts_in_neutral_posture / ends_in_neutral_posture: quality-control flags \
used to find broken recordings. Neutral means: both arms hanging relaxed at \
the sides, head level facing forward, torso upright. Be STRICT — if an arm \
is still raised or mid-motion in the first or last moments of the video, \
the flag is false.
- full_body_visible: true only if the robot's entire body — top of the head \
down to the wheeled base — stays inside the frame for the whole video.
- conversational: true if the gesture could naturally accompany or punctuate \
an ordinary reception dialogue. False for performance/entertainment acts \
that only make sense as a standalone show piece (e.g. playing air guitar, \
acting like a zombie, dancing, mimicking driving a car) — a dialogue agent \
should never pick those mid-conversation.
- communicative_functions: what the gesture DOES in conversation, e.g. \
"greeting", "attention_getting", "farewell", "affirmation", "negation", \
"pointing", "explaining", "apologizing", "expressing_uncertainty", \
"emphasis", "calming", "counting", "self_reference", "listener_reference".
- social_tone: e.g. "friendly", "welcoming", "formal", "informal", \
"apologetic", "enthusiastic", "playful", "serious", "empathetic".
- body_parts: from ["left_arm", "right_arm", "both_arms", "head", "torso", \
"hands", "base"].
- suitable_dialogue_states: e.g. "session_start", "session_end", \
"visitor_approach", "answering", "explaining", "clarification_request", \
"apology", "waiting", "thinking", "handover_to_human".
- avoid_contexts: dialogue situations where this gesture would be wrong or \
awkward, e.g. "apology", "uncertainty", "serious_policy_answer", "farewell".
- safety_notes: physical-space concerns (arm sweep radius etc.), or "" if none.
- confidence: your overall confidence in this annotation, 0.0-1.0."""


_MEDIA_RES = {
    "low": types.MediaResolution.MEDIA_RESOLUTION_LOW,
    "medium": types.MediaResolution.MEDIA_RESOLUTION_MEDIUM,
    "high": types.MediaResolution.MEDIA_RESOLUTION_HIGH,
}


class GestureAnnotation(BaseModel):
    short_caption: str = Field(description="One sentence: what the robot does, visitor-facing perspective.")
    movement_description: str = Field(description="2-4 sentences narrating the whole movement chronologically.")
    communicative_functions: List[str]
    alternative_interpretations: List[str] = Field(description="Plausible readings of the motion beyond the designer's intent; empty if unambiguous.")
    social_tone: List[str]
    body_parts: List[str]
    speed: Literal["slow", "moderate", "fast"]
    motion_energy: Literal["low", "medium", "high"]
    amplitude: Literal["small", "medium", "large"]
    symmetry: Literal["symmetric", "asymmetric"]
    dominant_direction: str = Field(description="Main direction of the movement, e.g. 'upward_and_outward', 'toward_listener', 'none'.")
    speech_compatibility: Literal["during_speech", "before_or_after_speech", "either", "avoid_with_speech"]
    suitable_dialogue_states: List[str]
    avoid_contexts: List[str]
    interruptible: bool = Field(description="Could the robot abort this mid-motion without looking broken?")
    conversational: bool = Field(description="Fits ordinary dialogue (true) vs standalone show piece (false).")
    starts_in_neutral_posture: bool
    ends_in_neutral_posture: bool
    full_body_visible: bool
    safety_notes: str
    confidence: float = Field(ge=0.0, le=1.0)


def source_label(name):
    """Derive a coarse human-readable label from the animation key:
    'Hey_1' -> 'hey', 'ShowFloor_2' -> 'show_floor', 'DontUnderstand_1' -> 'dont_understand'."""
    root = re.sub(r"_\d+$", "", name)
    root = re.sub(r"^(Gestures|Waiting)_", "", root)
    return re.sub(r"(?<!^)(?=[A-Z])", "_", root).lower()


def motion_bounds(clip_path):
    """Return (clip_sec, start_sec, end_sec, duration_sec); duration may be None."""
    cap = cv2.VideoCapture(clip_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    previous = None
    diffs = []
    while True:
        ok, frame = cap.read()
        if not ok:
            break
        small = cv2.cvtColor(cv2.resize(frame, (160, 90)), cv2.COLOR_BGR2GRAY).astype(np.int16)
        if previous is not None:
            diffs.append(float(np.abs(small - previous).mean()))
        previous = small
    cap.release()

    clip_sec = round((len(diffs) + 1) / fps, 2)
    active = [i for i, d in enumerate(diffs) if d > MOTION_THRESHOLD]
    if not active:
        return clip_sec, None, None, None
    start = max(0.0, active[0] / fps - MOTION_PAD_SEC)
    end = min(clip_sec, (active[-1] + 1) / fps + MOTION_PAD_SEC)
    return clip_sec, round(start, 2), round(end, 2), round(end - start, 2)


def annotate_clip(client, clip_path, name):
    # Clips over the inline request limit go through the Files API instead
    # (upload, wait until ACTIVE, reference in the prompt, delete afterwards).
    size_mb = os.path.getsize(clip_path) / 1e6
    uploaded = None
    if size_mb > INLINE_LIMIT_MB:
        print("[annotate]   %.1f MB > %d MB inline limit — using Files API upload"
              % (size_mb, INLINE_LIMIT_MB))
        uploaded = client.files.upload(file=clip_path)
        while uploaded.state and uploaded.state.name == "PROCESSING":
            time.sleep(2.0)
            uploaded = client.files.get(name=uploaded.name)
        if uploaded.state and uploaded.state.name != "ACTIVE":
            raise RuntimeError("Files API upload ended in state %s" % uploaded.state.name)
        video_part = uploaded
    else:
        with open(clip_path, "rb") as f:
            video_part = types.Part.from_bytes(data=f.read(), mime_type="video/mp4")

    try:
        return _generate_annotation(client, video_part, PROMPT.format(name=name))
    finally:
        if uploaded is not None:
            try:
                client.files.delete(name=uploaded.name)
            except Exception as exc:
                print("[annotate]   uploaded-file cleanup failed (harmless): %s" % exc)


def _generate_annotation(client, video_part, prompt):
    last_exc = None
    for attempt in range(1, API_MAX_RETRIES + 1):
        try:
            response = client.models.generate_content(
                model=ANNOTATION_MODEL,
                contents=[video_part, prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=GestureAnnotation,
                    thinking_config=types.ThinkingConfig(thinking_level=THINKING_LEVEL),
                    media_resolution=_MEDIA_RES[MEDIA_RESOLUTION],
                ),
            )
            return response.parsed
        except Exception as exc:
            # Retry transient server-side conditions (503 overloaded, 429
            # rate limit, 500). Anything else (e.g. bad request) re-raises.
            msg = str(exc)
            transient = any(code in msg for code in ("503", "429", "500", "UNAVAILABLE",
                                                     "RESOURCE_EXHAUSTED"))
            if not transient or attempt == API_MAX_RETRIES:
                raise
            wait = API_BACKOFF_SEC * (2 ** (attempt - 1))
            print("[annotate]   transient error (attempt %d/%d), retrying in %.0fs: %s"
                  % (attempt, API_MAX_RETRIES, wait, msg[:80]))
            last_exc = exc
            time.sleep(wait)
    raise last_exc


def main():
    load_dotenv(os.path.join(SCRIPT_DIR, "..", "..", ".env"))
    if not os.environ.get("GEMINI_API_KEY") and not os.environ.get("GOOGLE_API_KEY"):
        raise RuntimeError("GEMINI_API_KEY missing — add it to the project .env")
    client = genai.Client()

    with open(ANIMATIONS_JSON, "r", encoding="utf-8") as f:
        animations = json.load(f)
    manifest = {}
    if os.path.exists(MANIFEST_PATH):
        with open(MANIFEST_PATH, "r", encoding="utf-8") as f:
            manifest = json.load(f)
    # Animations whose behavior package ships sound files (the ones the
    # recorder muted) — recorded clips are silent either way, so this comes
    # from the robot inventory, not from the VLM.
    with_sound = set()
    if os.path.exists(SOUND_LIST_PATH):
        with open(SOUND_LIST_PATH, "r", encoding="utf-8") as f:
            with_sound = set(json.load(f))
    else:
        print("[annotate] WARNING: %s missing — has_sound will be false everywhere"
              % SOUND_LIST_PATH)
    os.makedirs(METADATA_DIR, exist_ok=True)

    clips = sorted(
        f for f in os.listdir(RECORDINGS_DIR)
        if f.endswith(".mp4") and not f.startswith("camera_test_")
    )
    pending = [c for c in clips
               if not os.path.exists(os.path.join(METADATA_DIR, c[:-4] + ".json"))]
    todo = pending if LIMIT is None else pending[:LIMIT]
    print("[annotate] %d clips, %d already annotated, %d pending, %d to do now (model: %s)"
          % (len(clips), len(clips) - len(pending), len(pending), len(todo), ANNOTATION_MODEL))

    for index, clip in enumerate(todo, 1):
        name = clip[:-4]
        clip_path = os.path.join(RECORDINGS_DIR, clip)
        print("[annotate] (%d/%d) %s" % (index, len(todo), name))
        try:
            clip_sec, start_sec, end_sec, duration_sec = motion_bounds(clip_path)
            # Prefer Pepper's own reported gesture time from the recorder
            # manifest — it is ground truth, unlike whole-frame differencing.
            entry = manifest.get(name, {})
            elapsed = (entry.get("trigger") or {}).get("elapsed_s")
            pre_roll = entry.get("pre_roll_sec")
            duration_source = "motion_detection"
            if elapsed is not None:
                duration_sec = round(elapsed, 2)
                duration_source = "robot_elapsed"
                if pre_roll is not None:
                    start_sec = round(pre_roll, 2)
                    end_sec = round(pre_roll + elapsed, 2)
            elif duration_sec is None:
                print("[annotate] %s: WARNING no motion detected and no manifest "
                      "elapsed_s — duration unknown" % name)
            annotation = annotate_clip(client, clip_path, animations.get(name, name))
        except Exception as exc:
            print("[annotate] %s FAILED: %s" % (name, exc))
            continue

        metadata = {
            "action_id": animations.get(name, name),
            "name": name,
            "source_label": source_label(name),
            "duration_s": duration_sec,
            "duration_source": duration_source,
            "motion_start_s": start_sec,
            "motion_end_s": end_sec,
            "clip_s": clip_sec,
            "has_sound": name in with_sound,
            "annotation_model": ANNOTATION_MODEL,
            **annotation.model_dump(),
        }
        with open(os.path.join(METADATA_DIR, name + ".json"), "w", encoding="utf-8") as f:
            json.dump(metadata, f, indent=2)
        flags = []
        if not metadata["starts_in_neutral_posture"]:
            flags.append("BAD START")
        if not metadata["ends_in_neutral_posture"]:
            flags.append("BAD END")
        if not metadata["full_body_visible"]:
            flags.append("BODY CUT OFF")
        if not metadata["conversational"]:
            flags.append("non-conversational")
        print("[annotate] %s: %.1fs %s%s" % (
            name, duration_sec or -1,
            ("[%s] " % ", ".join(flags)) if flags else "",
            metadata["short_caption"]))

    done = len([f for f in os.listdir(METADATA_DIR) if f.endswith(".json")])
    print("[annotate] done — %d per-clip annotations in %s" % (done, METADATA_DIR))


if __name__ == "__main__":
    main()
