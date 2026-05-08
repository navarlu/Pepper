#!/usr/bin/env bash
# Sync the experiment-worker source files from the RPi to woska.
#
# Run this every time you edit experiment_worker.py or tools_toolonly.py
# on the RPi — mirrors the existing scp workflow used for production
# voice-agent (see docs/notes/cmd.md). Other src/ deps (config.py,
# tools.py, etc.) are already on woska from your production deploy and
# shared between agents, so we don't push them here.
#
# Usage:
#     ./scripts/sync_experiment_to_woska.sh
#
# Safe to run repeatedly — scp overwrites by default.

set -euo pipefail

JUMP="navarlu2@ptak.felk.cvut.cz"
REMOTE="navarlu2@woska"
REMOTE_BASE="/mnt/data_personal/navarlu2/work/Pepper"
LOCAL_BASE="$(cd "$(dirname "$0")/.." && pwd)"
SUBDIR="voice-agent/tests/local_llm_benchmark"

cd "$LOCAL_BASE"

echo "[sync] target: $REMOTE:$REMOTE_BASE/$SUBDIR"
ssh -J "$JUMP" "$REMOTE" "mkdir -p '$REMOTE_BASE/$SUBDIR'"

scp -r -J "$JUMP" \
  "$SUBDIR/experiment_worker.py" \
  "$SUBDIR/tools_toolonly.py" \
  "$SUBDIR/tools" \
  "$REMOTE:$REMOTE_BASE/$SUBDIR/"

echo
echo "[sync] done. To start the worker on woska:"
echo
echo "    ssh -J $JUMP $REMOTE"
echo "    tmux attach -t pepper-experiment 2>/dev/null \\"
echo "        || tmux new-session -s pepper-experiment"
echo "    # inside tmux:"
echo "    cd $REMOTE_BASE && source .venv3/bin/activate"
echo "    export PEPPER_EXPERIMENT_AGENT_NAME=pepper-experiment"
echo "    python voice-agent/tests/local_llm_benchmark/experiment_worker.py dev"
echo "    # detach with Ctrl+B then D — the worker keeps running."
