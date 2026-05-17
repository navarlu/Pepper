#!/usr/bin/env bash
# Sync the experiment-agent source files from the RPi to woska.
#
# Run this every time you edit voice-agent/src/experiment/agent.py,
# prompt.py, or anything under tools/. Mirrors the existing scp
# workflow used for production voice-agent (see docs/notes/cmd.md).
# Other src/ deps (config.py, local_speech.py, bridge_client.py, etc.)
# are already on woska from your production deploy and shared between
# agents, so we don't push them here.
#
# Usage:
#     ./services/scripts/experiment/sync_to_woska.sh
#
# Safe to run repeatedly — scp overwrites by default.

set -euo pipefail

JUMP="navarlu2@halmos.felk.cvut.cz"
REMOTE="navarlu2@woska"
REMOTE_BASE="/mnt/data_personal/navarlu2/work/Pepper"
# Script lives at services/scripts/experiment/sync_to_woska.sh — three
# dirs up to reach the repo root.
LOCAL_BASE="$(cd "$(dirname "$0")/../../.." && pwd)"
SUBDIR="voice-agent/src/experiment"

cd "$LOCAL_BASE"

echo "[sync] target: $REMOTE:$REMOTE_BASE/$SUBDIR"
ssh -J "$JUMP" "$REMOTE" "mkdir -p '$REMOTE_BASE/$SUBDIR'"

scp -r -J "$JUMP" \
  "$SUBDIR/agent.py" \
  "$SUBDIR/agent_4o.py" \
  "$SUBDIR/agent_streaming.py" \
  "$SUBDIR/agent_4o_streaming.py" \
  "$SUBDIR/_pipeline.py" \
  "$SUBDIR/_streaming_runtime.py" \
  "$SUBDIR/prompt.py" \
  "$SUBDIR/prompt_streaming.py" \
  "$SUBDIR/tools" \
  "$REMOTE:$REMOTE_BASE/$SUBDIR/"

echo
echo "[sync] done. To start the agent on woska:"
echo
echo "    ssh -J $JUMP $REMOTE"
echo "    tmux attach -t pepper-experiment 2>/dev/null \\"
echo "        || tmux new-session -s pepper-experiment"
echo "    # inside tmux:"
echo "    cd $REMOTE_BASE && source .venv3/bin/activate"
echo "    export PEPPER_EXPERIMENT_AGENT_NAME=pepper-experiment"
# PYTHONUNBUFFERED=1 propagates to LiveKit Agents' watchfiles-spawned
# worker subprocess (python -u alone doesn't reach it), so prewarm logs
# show immediately instead of sitting in a pipe buffer.
echo "    export PYTHONUNBUFFERED=1"
# faster-whisper/ctranslate2 dlopen libcublas+libcudnn at runtime; the
# wheels live under .venv3/.../nvidia/*/lib but the dynamic linker
# can't find them unless LD_LIBRARY_PATH includes those dirs.
echo "    export LD_LIBRARY_PATH=\"\$(python -c 'import glob, nvidia; print(\":\".join(glob.glob(nvidia.__path__[0] + \"/*/lib\")))')\${LD_LIBRARY_PATH:+:\$LD_LIBRARY_PATH}\""
echo "    python voice-agent/src/experiment/agent.py dev"
echo "    # detach with Ctrl+B then D — the agent keeps running."
