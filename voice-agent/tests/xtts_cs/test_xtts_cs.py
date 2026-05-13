"""Generate Czech TTS samples with Coqui XTTS-v2 for voice auditioning.

XTTS-v2 is the realistic local option for Czech female speech. Czech is a
first-class language in the model. Two modes:

  * Built-in speakers (default) — 12 female-leaning studio speakers. Quality
    is great but accent can drift toward English since the speakers were
    recorded in English.
  * Voice cloning (XTTS_REFERENCE_WAV) — point at any 6-10s clean Czech
    female reference clip. This is what locks the accent to native Czech and
    is the recommended path for production.

Setup:
    uv pip install "coqui-tts>=0.24.0"
    export COQUI_TOS_AGREED=1

Run (built-in speakers):
    uv run python voice-agent/tests/xtts_cs/test_xtts_cs.py

Run (voice cloning — recommended):
    export XTTS_REFERENCE_WAV=/abs/path/to/female_cs_ref.wav
    uv run python voice-agent/tests/xtts_cs/test_xtts_cs.py

Output:
    voice-agent/tests/xtts_cs/output/xtts/<speaker>/<idx>_<slug>.wav
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from pathlib import Path

os.environ.setdefault("COQUI_TOS_AGREED", "1")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("xtts-cs-test")

THIS_DIR = Path(__file__).resolve().parent
OUT_DIR = THIS_DIR / "output"

MODEL_NAME = "tts_models/multilingual/multi-dataset/xtts_v2"
LANGUAGE = "cs"

# XTTS-v2 ships with ~58 built-in studio speakers. The shortlist below is the
# subset commonly described as female / female-leaning in the model card.
# We'll generate every sentence with each so Lucas can A/B them.
FEMALE_SPEAKERS = [
    "Claribel Dervla",
    "Daisy Studious",
    "Gracie Wise",
    "Tammie Ema",
    "Alison Dietlinde",
    "Ana Florence",
    "Annmarie Nele",
    "Vjollca Johnnie",
    "Henriette Usha",
    "Sofia Hellen",
    "Tammy Grit",
    "Tanja Adelina",
]

# Mix: greeting, a tool-use-style sentence, a longer prosody test.
SENTENCES = [
    "Ahoj, jsem Pepper. Jak se dnes máš?",
    "Dobře, najdu ti nejkratší cestu do laboratoře. Pojď za mnou prosím.",
    "V mense je dnes na oběd kuřecí řízek s bramborovou kaší, vegetariánská lasagne a krémová polévka z dýně.",
]


def _slug(text: str, maxlen: int = 32) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "_", text.strip().lower())
    return s[:maxlen].strip("_") or "sample"


def _speaker_dirname(speaker: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", speaker).strip("_").lower()


def _run_xtts(engine_out: Path) -> int:
    try:
        import torch  # noqa: F401
        from TTS.api import TTS
    except ImportError as e:
        log.error("[xtts] missing dep: %s. Install: uv pip install 'coqui-tts>=0.24.0'", e)
        return 0

    use_cuda = bool(int(os.environ.get("XTTS_USE_CUDA", "0"))) or _torch_cuda_available()
    log.info("[xtts] loading %s (cuda=%s) — first load downloads ~1.8GB", MODEL_NAME, use_cuda)
    t0 = time.monotonic()
    tts = TTS(MODEL_NAME).to("cuda" if use_cuda else "cpu")
    log.info("[xtts] ready in %.1fs", time.monotonic() - t0)

    reference_wav = os.environ.get("XTTS_REFERENCE_WAV")
    if reference_wav:
        ref_path = Path(reference_wav).expanduser().resolve()
        if not ref_path.is_file():
            log.error("[xtts] XTTS_REFERENCE_WAV not found: %s", ref_path)
            return 0
        log.info("[xtts] voice-cloning from: %s", ref_path)
        speakers = [("clone_" + ref_path.stem, str(ref_path))]
        use_clone = True
    else:
        log.info("[xtts] built-in-speaker mode (%d female speakers)", len(FEMALE_SPEAKERS))
        speakers = [(s, None) for s in FEMALE_SPEAKERS]
        use_clone = False

    total = 0
    for speaker_name, speaker_wav in speakers:
        spk_dir = engine_out / _speaker_dirname(speaker_name)
        spk_dir.mkdir(parents=True, exist_ok=True)
        log.info("[xtts] --- speaker: %s ---", speaker_name)
        for idx, sentence in enumerate(SENTENCES, start=1):
            out_path = spk_dir / f"{idx:02d}_{_slug(sentence)}.wav"
            t = time.monotonic()
            kwargs = dict(text=sentence, language=LANGUAGE, file_path=str(out_path))
            if use_clone:
                kwargs["speaker_wav"] = speaker_wav
            else:
                kwargs["speaker"] = speaker_name
            tts.tts_to_file(**kwargs)
            log.info("[xtts]   [%d/%d] %.1fs -> %s", idx, len(SENTENCES), time.monotonic() - t, out_path)
            total += 1
    return total


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    total = _run_xtts(OUT_DIR / "xtts")
    log.info("Done. Generated %d files under %s", total, OUT_DIR)
    return 0


def _torch_cuda_available() -> bool:
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:
        return False


if __name__ == "__main__":
    sys.exit(main())
