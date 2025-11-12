#!/usr/bin/env python3
# generate_animations_json.py
# Autogenerates animations.json from a Pepper diagnostics text dump.

import json
import os
import re
from collections import OrderedDict

def extract_behaviors(txt: str):
    """Extract lines of installed behaviors from the '=== Behaviors ===' section."""
    behaviors = []
    for match in re.finditer(r"=== Behaviors ===", txt):
        start = match.end()
        end = txt.find("===", start + 1)
        section = txt[start: end if end != -1 else None]
        for raw in section.splitlines():
            line = raw.strip()
            if line.startswith("* "):
                path = line[2:].strip()
                if path:
                    behaviors.append(path)
    return behaviors

def filter_animation_namespace(paths, prefix="animations/"):
    """Keep only behaviors starting with the given prefix."""
    return [p for p in paths if p.startswith(prefix)]

def make_unique_keys(paths):
    """Generate short, unique keys for behavior paths."""
    used = set()
    mapping = OrderedDict()
    for p in paths:
        seg = p.split("/")
        candidates = []
        if seg:
            candidates.append(seg[-1])
            if len(seg) >= 2:
                candidates.append(f"{seg[-2]}_{seg[-1]}")
            if len(seg) >= 3:
                candidates.append(f"{seg[-3]}_{seg[-2]}_{seg[-1]}")
            if len(seg) >= 4:
                candidates.append(f"{seg[-4]}_{seg[-3]}_{seg[-2]}_{seg[-1]}")
        candidates.append("__".join(seg))
        chosen = None
        for c in candidates:
            if c not in used:
                chosen = c
                break
        if chosen is None:
            base = seg[-1] if seg else p
            i = 2
            while f"{base}_{i}" in used:
                i += 1
            chosen = f"{base}_{i}"
        used.add(chosen)
        mapping[chosen] = p
    return mapping

def main():
    # --- EDIT THESE PATHS ---
    input_txt = "Pepper/real_pepper.txt"       # Path to the Pepper dump
    output_json = "Pepper/animations.json"     # Output file path
    include_non_animations = False      # True = include all, False = only 'animations/'

    # --- LOGIC ---
    if not os.path.isfile(input_txt):
        raise SystemExit(f"Input file not found: {input_txt}")

    with open(input_txt, "r", encoding="utf-8", errors="ignore") as f:
        txt = f.read()

    all_behaviors = extract_behaviors(txt)
    if not all_behaviors:
        raise SystemExit("No behaviors found in the input file. "
                         "Make sure it contains a '=== Behaviors ===' section.")

    behaviors = (all_behaviors if include_non_animations
                 else filter_animation_namespace(all_behaviors, "animations/"))

    if not behaviors:
        raise SystemExit("No behaviors under 'animations/' were found. "
                         "Set include_non_animations = True if you want everything.")

    # Deduplicate
    seen = set()
    ordered = []
    for b in behaviors:
        if b not in seen:
            seen.add(b)
            ordered.append(b)

    mapping = make_unique_keys(ordered)

    os.makedirs(os.path.dirname(os.path.abspath(output_json)) or ".", exist_ok=True)
    with open(output_json, "w", encoding="utf-8") as f:
        json.dump(mapping, f, indent=2, ensure_ascii=False)

    print(f"Wrote {len(mapping)} animations to: {output_json}")

if __name__ == "__main__":
    main()
