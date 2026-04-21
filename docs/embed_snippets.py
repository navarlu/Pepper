#!/usr/bin/env python3
"""Embed named source snippets into markdown files.

Source files declare regions:

    # region: NAME
    ... code ...
    # endregion

Markdown files declare placeholders whose body is rewritten in-place:

    <!-- snippet: NAME -->
    ```python
    (this body is replaced by the script)
    ```
    <!-- /snippet -->

Run from anywhere:

    uv run python docs/embed_snippets.py          # rewrite in place
    uv run python docs/embed_snippets.py --check  # exit 1 if anything is stale
"""

from __future__ import annotations

import re
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

SOURCE_FILES = [
    "robot/src/bridge.py",
    "robot/src/utils.py",
    "voice-agent/src/agent.py",
    "voice-agent/src/tools.py",
    "voice-agent/src/local_speech.py",
    "voice-agent/src/qwen_compat.py",
    "voice-agent/src/rooms.py",
]

MARKDOWN_FILES = [
    "docs/modules/bridge.md",
    "docs/modules/voice-agent.md",
]

REGION_START = re.compile(r"^\s*#\s*region:\s*(\S+)\s*$")
REGION_END = re.compile(r"^\s*#\s*endregion\b")
SNIPPET_BLOCK = re.compile(
    r"(<!--\s*snippet:\s*(\S+)\s*-->)(.*?)(<!--\s*/snippet\s*-->)",
    re.DOTALL,
)


def collect_regions(paths: list[str]) -> dict[str, tuple[str, str, int]]:
    regions: dict[str, tuple[str, str, int]] = {}
    for rel in paths:
        text = (ROOT / rel).read_text()
        lines = text.splitlines()
        i = 0
        while i < len(lines):
            m = REGION_START.match(lines[i])
            if not m:
                i += 1
                continue
            name = m.group(1)
            start = i + 1
            j = start
            while j < len(lines) and not REGION_END.match(lines[j]):
                j += 1
            if j >= len(lines):
                raise SystemExit(f"{rel}: region '{name}' has no '# endregion'")
            if name in regions:
                prev_file = regions[name][0]
                raise SystemExit(
                    f"duplicate region '{name}' (in {prev_file} and {rel})"
                )
            body = textwrap.dedent("\n".join(lines[start:j])).rstrip() + "\n"
            regions[name] = (rel, body, start + 1)
            i = j + 1
    return regions


def render_block(regions: dict[str, tuple[str, str, int]], name: str) -> str:
    rel, body, start_line = regions[name]
    header = (
        f"<!-- generated from {rel}:{start_line} by docs/embed_snippets.py -->"
    )
    return f"\n{header}\n```python\n{body}```\n"


def process_markdown(
    md_path: Path,
    regions: dict[str, tuple[str, str, int]],
    check: bool,
) -> tuple[bool, bool]:
    text = md_path.read_text()
    missing: list[str] = []

    def repl(m: re.Match[str]) -> str:
        open_tag = m.group(1)
        name = m.group(2)
        close_tag = m.group(4)
        if name not in regions:
            missing.append(name)
            return m.group(0)
        return f"{open_tag}{render_block(regions, name)}{close_tag}"

    new_text = SNIPPET_BLOCK.sub(repl, text)
    changed = new_text != text

    if missing:
        for name in missing:
            print(
                f"{md_path}: unknown snippet '{name}'",
                file=sys.stderr,
            )

    if changed and not check:
        md_path.write_text(new_text)
        print(f"updated {md_path.relative_to(ROOT)}")

    return (not missing, changed)


def main() -> int:
    check = "--check" in sys.argv
    regions = collect_regions(SOURCE_FILES)

    any_missing = False
    any_stale = False
    for rel in MARKDOWN_FILES:
        ok, changed = process_markdown(ROOT / rel, regions, check=check)
        if not ok:
            any_missing = True
        if check and changed:
            any_stale = True
            print(f"{rel}: out of date", file=sys.stderr)

    if any_missing:
        return 1
    if check and any_stale:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
