"""Pure-function checks for the lookup_person input validation
backstop. No LiveKit, no network — just exercises the
`_is_generic_non_name` predicate that prevents the tool from
calling the staff directory with generic words like 'user',
'human', 'someone'.

Plain script (mirrors the style of the other files in this dir),
so no pytest dependency is needed. Run from the project root:

    uv run python voice-agent/src/experiment/tests/test_lookup_person_validation.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Project convention: experiment code expects `voice-agent/src/` on sys.path
# so the `experiment.*` package resolves. Tests live at
# voice-agent/src/experiment/tests/, so add voice-agent/src/ explicitly.
_VA_SRC = Path(__file__).resolve().parents[2]
if str(_VA_SRC) not in sys.path:
    sys.path.insert(0, str(_VA_SRC))

from experiment.tools.utils._person_lookup import _is_generic_non_name  # noqa: E402


SHOULD_REJECT = [
    "user", "Human", "  someone  ", "Člověk", "clovek",
    "lidé", "lidi", "Nekdo", "anyone", "somebody",
    "person", "people", "PAN", "paní", "MAN.", "woman?",
]

SHOULD_PASS = [
    "Novák", "Novak", "Dvořák", "Smith", "Šebek", "Shebek",
    "Svoboda", "", "   ", "Petr", "Wagner",
]


def _check(value: str, expected: bool) -> bool:
    actual = _is_generic_non_name(value)
    ok = actual is expected
    mark = "ok " if ok else "FAIL"
    print(f"  [{mark}] _is_generic_non_name({value!r}) = {actual} "
          f"(expected {expected})")
    return ok


def main() -> int:
    failures = 0
    print("== should reject (generic words) ==")
    for v in SHOULD_REJECT:
        if not _check(v, True):
            failures += 1
    print("== should pass through (real surnames + empty) ==")
    for v in SHOULD_PASS:
        if not _check(v, False):
            failures += 1
    total = len(SHOULD_REJECT) + len(SHOULD_PASS)
    print(f"\nresult: {total - failures}/{total} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
