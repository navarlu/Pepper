"""lookup_person_czech: variant of lookup_person that undoes English
digraph approximations of Czech surnames before querying UDB.

Whisper transcribes Czech speech in English orthography, so "Šebek"
arrives as "Shebek" or "Shebeck". UDB search is diacritic-insensitive
but takes the surname literally — so "Shebeck" gets 0 hits.

Pipeline per call:
  1. Lowercase + strip diacritics.
  2. Apply each English→Czech digraph rule to generate candidates.
  3. Query UDB for every candidate in parallel via asyncio.gather.
  4. First candidate with non-empty matches wins.
  5. Tiebreak by Levenshtein distance to the original input.

The substitution table is intentionally tiny — covers the common
English approximations, leaves Czech-native digraphs (ch is a real
Czech consonant) intact via the identity candidate.
"""

from __future__ import annotations

import sys
import unicodedata
from pathlib import Path
from typing import Literal
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncio

from livekit.agents import RunContext, function_tool

from tools._animation import trigger_animation
from tools._common import _json, _run_main
from tools._person import _slim_match

from src.udb import NotOnCzvutNetworkError, lookup_person as udb_lookup_person  # noqa: E402

_Gesture = Literal["greet", "think", "explain", "bow", "happy", "dont_know"]


_TITLE_WEIGHTS = {
    "prof.": 4.0,
    "doc.": 3.0,
    "DrSc.": 3.0,
    "Ph.D.": 2.0,
    "CSc.": 2.0,
    "RNDr.": 1.5,
    "MUDr.": 1.5,
    "JUDr.": 1.5,
    "PhDr.": 1.5,
    "Ing.": 1.0,
    "Mgr.": 1.0,
    "Bc.": 0.5,
}


# Digraph rewrites: English approximation → Czech-folded form. Order
# matters only for readability; the variant generator emits one
# candidate per rule that actually matches the input.
_ENG_TO_CZ_RULES = (
    ("sh", "s"),    # Šebek → Shebek
    ("zh", "z"),    # Železný → Zhelezny
    ("cz", "c"),    # Polish-style → Czech
    ("w", "v"),     # Wagnerová → Vagnerova
    ("ck", "k"),    # Šebek written as Shebeck
    ("ch", "c"),    # Čermák → Chermak (ch is also a real Czech digraph,
                    # so we keep the identity candidate too)
)


def _name_tokens(name: str) -> list[str]:
    return (name or "").replace(",", " ").split()


def _title_score(name: str) -> float:
    return sum(_TITLE_WEIGHTS.get(tok, 0.0) for tok in _name_tokens(name))


def _first_name_token(name: str) -> str:
    for tok in _name_tokens(name):
        if tok not in _TITLE_WEIGHTS:
            return tok.lower()
    return ""


def _fold(text: str) -> str:
    nfkd = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


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
    """Generate at most ~6 deduped Czech-spelling candidates for the
    folded English input. Always includes the identity (so a user who
    already said the Czech form gets a direct hit)."""
    base = _fold(surname).strip()
    if not base:
        return []

    variants: list[str] = [base]
    # First pass: apply each rule independently to the base.
    for old, new in _ENG_TO_CZ_RULES:
        if old in base:
            variants.append(base.replace(old, new))
    # Second pass: stack rules — apply every rule to every previous
    # variant once. Catches things like "shebeck" → "sebeck" → "sebek".
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


async def _udb_lookup_variants(variants: list[str]) -> list[tuple[str, dict | None, str | None]]:
    """Run UDB queries for every variant in parallel. Returns list of
    (variant, result_dict_or_None, error_or_None) preserving order."""
    async def _one(v: str):
        try:
            r = await asyncio.to_thread(udb_lookup_person, v)
            return (v, r, None)
        except NotOnCzvutNetworkError as exc:
            return (v, None, str(exc))
        except Exception as exc:
            return (v, None, str(exc))

    return await asyncio.gather(*[_one(v) for v in variants])


def _pick_best(
    original: str,
    runs: list[tuple[str, dict | None, str | None]],
) -> tuple[str, dict] | None:
    """Pick the (variant, result) with non-empty matches, preferring
    the candidate closest to the original input by Levenshtein."""
    hits = [(v, r) for v, r, _ in runs if r and (r.get("matches") or [])]
    if not hits:
        return None
    return min(hits, key=lambda vr: _levenshtein(original, vr[0]))


@function_tool
async def lookup_person(
    context: RunContext,
    first_name: str,
    surname: str,
    gesture: _Gesture = "think",
) -> str:
    """Look up a person's contact info (phone, email, room) in the
    public staff directory. Tolerates English-spelled approximations of
    Czech surnames (e.g. "Shebek" → "Šebek").

    Both `first_name` and `surname` are required. Honorifics like Mr./
    Mrs./Pan must NOT be passed as first_name — ask the user for the
    real first name first.
    """
    del context
    if gesture:
        asyncio.create_task(trigger_animation(gesture))
    surname_q = str(surname or "").strip()
    first_q = str(first_name or "").strip()
    if (
        not surname_q
        or not first_q
        or first_q.lower() == surname_q.lower()
    ):
        return _json({
            "error": "missing_first_name",
            "instruction": (
                "A real first name AND surname are required, and "
                "they must be different. Ask the user — in plain "
                "conversation — what the person's first name is, "
                "and call this tool again only after the user "
                "answers."
            ),
        })

    variants = _english_to_czech_variants(surname_q)
    print(f"  [tool] lookup_person_czech variants={variants!r}")

    runs = await _udb_lookup_variants(variants)
    for v, r, err in runs:
        if err:
            print(f"  [tool] udb({v!r}) error: {err}")
        else:
            n = len(r.get("matches") or []) if r else 0
            print(f"  [tool] udb({v!r}) -> {n} match(es)")

    picked = _pick_best(surname_q, runs)
    if picked is None:
        # Surface the first network error if one occurred, otherwise empty.
        first_err = next((err for _, _, err in runs if err), None)
        if first_err:
            return _json({"error": first_err})
        return _json({"count": 0, "matches": [], "tried_variants": variants})

    chosen_variant, result = picked
    candidates = [_slim_match(m) for m in (result.get("matches") or [])]
    if not candidates:
        return _json({"count": 0, "matches": [], "tried_variants": variants})

    wanted = _fold(first_q).rstrip(".").strip()

    def _candidate_first(m: dict) -> str:
        return _fold(_first_name_token(m.get("name") or ""))

    exact = [m for m in candidates if _candidate_first(m) == wanted]
    if exact:
        candidates = exact
    elif len(wanted) == 1:
        initial = [m for m in candidates if _candidate_first(m).startswith(wanted)]
        if initial:
            candidates = initial
        else:
            return _json({
                "error": "first_name_not_found",
                "instruction": (
                    f"No {chosen_variant!r} matches first name {first_q!r}. "
                    "Tell the user and ask them to confirm or correct "
                    "the first name. Do not call this tool again "
                    "until they do."
                ),
            })
    else:
        return _json({
            "error": "first_name_not_found",
            "instruction": (
                f"No person named {first_q!r} {chosen_variant!r} "
                "exists in the directory. Tell the user that name was "
                "not found and ask them to confirm or correct the "
                "first name. Do not call this tool again until they do."
            ),
        })

    best = max(candidates, key=lambda m: _title_score(m.get("name") or ""))
    print(
        f"  [tool] lookup_person_czech picked {best.get('name')!r} "
        f"via variant={chosen_variant!r} "
        f"(score={_title_score(best.get('name') or ''):.1f}, "
        f"from {len(candidates)} candidate(s))"
    )
    return _json({
        "count": 1,
        "matches": [best],
        "instruction": (
            "Public staff directory entry. Reply with only the field "
            "the user asked for — if they asked for a phone, say just "
            "the phone; if they asked for an email, say just the email; "
            "if they asked where the person is, say just the room. "
            "Use the person's surname or 'professor X' / 'doctor X' "
            "form, never the full title chain."
        ),
    })


async def _probe(surname: str) -> None:
    """Standalone probe: show variants + which UDB query returns hits.
    Doesn't apply the first-name filter so we can see the raw
    diacritic-insensitive UDB behaviour."""
    print(f"INPUT surname={surname!r}")
    variants = _english_to_czech_variants(surname)
    print(f"VARIANTS ({len(variants)}): {variants}")
    print()
    runs = await _udb_lookup_variants(variants)
    for v, r, err in runs:
        if err:
            print(f"  udb({v!r}) ERROR: {err}")
            continue
        matches = r.get("matches") or [] if r else []
        names = [m.get("name") or "?" for m in matches[:5]]
        print(f"  udb({v!r}) -> {len(matches)} match(es): {names}")
    print()
    picked = _pick_best(surname, runs)
    if picked is None:
        print("RESULT: no variant produced any UDB hit")
    else:
        chosen, result = picked
        names = [m.get("name") or "?" for m in (result.get("matches") or [])[:5]]
        print(f"RESULT: variant {chosen!r} won — {len(result.get('matches') or [])} match(es): {names}")


if __name__ == "__main__":
    import asyncio as _asyncio
    SURNAME = "shebeck"
    _asyncio.run(_probe(SURNAME))
