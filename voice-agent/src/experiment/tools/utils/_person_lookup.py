"""Helpers for the lookup_person tool — title scoring, honorific
detection, English→Czech surname variants, and Levenshtein-based
best-variant picking.

Whisper transcribes Czech speech in English orthography ("Šebek" →
"Shebek" / "Shebeck"). UDB search is diacritic-insensitive but takes
the surname literally, so the English approximation gets 0 hits.
We undo the common digraph approximations and try every candidate in
parallel — first hit wins.
"""

from __future__ import annotations

import asyncio
import unicodedata


# Title weights for ranking the "most senior" candidate in a multi-hit
# UDB result.
_TITLE_WEIGHTS = {
    "prof.": 4.0, "doc.": 3.0, "DrSc.": 3.0, "Ph.D.": 2.0, "CSc.": 2.0,
    "RNDr.": 1.5, "MUDr.": 1.5, "JUDr.": 1.5, "PhDr.": 1.5,
    "Ing.": 1.0, "Mgr.": 1.0, "Bc.": 0.5,
}


def _name_tokens(name: str) -> list[str]:
    return (name or "").replace(",", " ").split()


def _title_score(name: str) -> float:
    return sum(_TITLE_WEIGHTS.get(tok, 0.0) for tok in _name_tokens(name))


def _first_name_token(name: str) -> str:
    for tok in _name_tokens(name):
        if tok not in _TITLE_WEIGHTS:
            return tok.lower()
    return ""


# Honorifics the LLM sometimes packs into `first_name` instead of asking
# the user for the real one ("Mr. Shebek" → first_name="Mr."). Folded,
# lowercase, no trailing punctuation. Czech: pan / paní / slečna.
_HONORIFIC_TOKENS = frozenset({
    "mr", "mrs", "ms", "miss", "sir", "madam", "madame",
    "pan", "pani", "slecna",
})


def _fold(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


def _is_honorific(token: str) -> bool:
    return _fold(token).strip().rstrip(".,;:") in _HONORIFIC_TOKENS


# ── English → Czech surname normalisation ─────────────────────────────
_ENG_TO_CZ_RULES = (
    ("sh", "s"),    # Šebek → Shebek
    ("zh", "z"),    # Železný → Zhelezny
    ("cz", "c"),    # Polish-style spelling
    ("w", "v"),     # Wagnerová → Vagnerova
    ("ck", "k"),    # Šebek written "Shebeck"
    ("ch", "c"),    # Čermák → Chermak (ch is also a real Czech digraph,
                    # so the identity candidate covers genuine ch names)
)


def _levenshtein(a: str, b: str) -> int:
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        curr = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            curr[j] = min(prev[j] + 1, curr[j - 1] + 1, prev[j - 1] + cost)
        prev = curr
    return prev[-1]


def _english_to_czech_variants(surname: str) -> list[str]:
    """Generate up to ~6 deduped Czech-spelling candidates from a folded
    English approximation. Always includes the identity (so a user who
    already said the Czech form gets a direct hit). Two passes so rules
    can stack: 'shebeck' → 'sebeck' (sh→s) + 'shebek' (ck→k) → 'sebek'."""
    base = _fold(surname).strip()
    if not base:
        return []
    variants: list[str] = [base]
    for old, new in _ENG_TO_CZ_RULES:
        if old in base:
            variants.append(base.replace(old, new))
    stacked = list(variants)
    for v in variants:
        for old, new in _ENG_TO_CZ_RULES:
            if old in v:
                stacked.append(v.replace(old, new))
    seen: set[str] = set()
    unique: list[str] = []
    for v in stacked:
        if v and v not in seen:
            seen.add(v)
            unique.append(v)
    return unique[:6]


async def _udb_lookup_variants(variants: list[str], udb_lookup_person, NotOnCzvutNetworkError):
    """Run UDB queries for every variant in parallel. Returns a list of
    (variant, result_or_None, error_or_None) preserving input order so
    we can deterministically pick the best match below.

    udb_lookup_person and NotOnCzvutNetworkError are passed in to keep
    this helper free of `src.*` imports — the tool file injects them.
    """
    async def _one(v: str):
        try:
            r = await asyncio.to_thread(udb_lookup_person, v)
            return (v, r, None)
        except NotOnCzvutNetworkError as exc:
            return (v, None, str(exc))
        except Exception as exc:  # noqa: BLE001
            return (v, None, str(exc))
    return await asyncio.gather(*[_one(v) for v in variants])


def _pick_best_variant(original: str, runs):
    """Pick the (variant, result) with non-empty matches, preferring
    the candidate closest to the original input by Levenshtein."""
    hits = [(v, r) for v, r, _ in runs if r and (r.get("matches") or [])]
    if not hits:
        return None
    return min(hits, key=lambda vr: _levenshtein(original, vr[0]))
