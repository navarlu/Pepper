"""Bundle the per-animation metadata JSONs into one catalog file for the
voice agent.

The streaming workers run on woska with their own copy of `voice-agent/`,
so the agent must not depend on the 390 loose metadata files in
`experiments/`. This script extracts the fields the inline-gesture layer
needs (catalog line + dispatch flags) into a single deterministic JSON
bundle inside `voice-agent/` that gets deployed with the code.

Run after (re-)annotating:  uv run python experiments/animation_metadata/build_agent_catalog.py
"""
import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
METADATA_DIR = PROJECT_ROOT / "experiments" / "animation_metadata" / "data" / "metadata"
OUTPUT_FILE = PROJECT_ROOT / "voice-agent" / "src" / "experiment" / "data" / "animation_catalog.json"


def main() -> None:
    entries = []
    for path in sorted(METADATA_DIR.glob("*.json")):
        d = json.loads(path.read_text(encoding="utf-8"))
        entries.append({
            "name": d["name"],
            "caption": d.get("short_caption", ""),
            "functions": d.get("communicative_functions", []),
            "tone": d.get("social_tone", []),
            "energy": d.get("motion_energy", ""),
            "has_sound": bool(d.get("has_sound", False)),
            "duration_s": d.get("duration_s"),
        })
    if not entries:
        raise SystemExit(f"no metadata found in {METADATA_DIR}")
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(
        json.dumps(entries, ensure_ascii=False, indent=1) + "\n",
        encoding="utf-8",
    )
    with_sound = sum(1 for e in entries if e["has_sound"])
    print(f"wrote {len(entries)} animations ({with_sound} with sound) -> {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
