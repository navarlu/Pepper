#!/usr/bin/env bash
# Tmux launcher for the streaming-agent console smoke test.
#
# Spawns a detached tmux session "streaming-smoke" that runs the
# streaming agent in LiveKit's `console --text` mode, then prints
# attach instructions + a list of suggested test prompts.
#
# Usage:
#     bash voice-agent/src/experiment/tests/run_streaming_smoke.sh
#     # then:
#     tmux attach -t streaming-smoke
#
# Inside the tmux session:
#     - Type a prompt at the > line and press Enter.
#     - Watch:
#         * `=== LLM CONTEXT DUMP ===`        printed once on start
#         * `[LLM] turn user_msgs=N ...`      per turn
#         * `[tool] <name>(...)`              when a tool fires
#         * `➜ <name> ✓ {...}`                tool result rendered by livekit
#         * The agent's plain-text reply
#     - Ctrl+C to quit the agent.
#     - Detach without killing: Ctrl+B then D.
#     - Kill the whole session: tmux kill-session -t streaming-smoke
#
# Prereqs:
#     1. tmux installed.
#     2. vLLM Llama 3.1 8B AWQ reachable at LOCAL_LLM_BASE_URL
#        (default http://localhost:8000/v1) — open the SSH tunnel
#        to woska first if running on the RPi.
#     3. uv on PATH (or run from a shell where `uv` resolves).

set -euo pipefail

SESSION="streaming-smoke"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../../../.." && pwd)"
CONSOLE_SCRIPT="$SCRIPT_DIR/streaming_console.py"

if ! command -v tmux >/dev/null 2>&1; then
    echo "tmux not found — install with: sudo apt install tmux" >&2
    exit 1
fi

if [[ ! -f "$CONSOLE_SCRIPT" ]]; then
    echo "Missing $CONSOLE_SCRIPT" >&2
    exit 1
fi

if tmux has-session -t "$SESSION" 2>/dev/null; then
    echo "Session '$SESSION' already exists. Attach with:"
    echo "    tmux attach -t $SESSION"
    echo "Or kill it first:"
    echo "    tmux kill-session -t $SESSION"
    exit 0
fi

# Cheap reachability check — fail fast with a clear message rather
# than letting the agent spin up and crash inside the tmux pane.
LLM_URL="${LOCAL_LLM_BASE_URL:-http://localhost:8000/v1}/models"
if ! curl -fsS --max-time 5 "$LLM_URL" >/dev/null 2>&1; then
    echo "WARNING: vLLM not reachable at $LLM_URL"
    echo "         The agent will exit on dispatch. Open the SSH"
    echo "         tunnel to woska and re-run if you want to proceed."
    echo
fi

# `uv run` from the repo root so .venv resolves the project way.
CMD="cd '$REPO_ROOT' && uv run python '$CONSOLE_SCRIPT' console --text"

tmux new-session -d -s "$SESSION" -n agent "$CMD"

cat <<EOF

Started tmux session: $SESSION
Repo root: $REPO_ROOT
Console script: $CONSOLE_SCRIPT

Attach with:
    tmux attach -t $SESSION

Detach (without killing): Ctrl+B then D
Kill: tmux kill-session -t $SESSION

──────────────────────────────────────────────────────────
SUGGESTED TEST PROMPTS (one per turn, watch the logs)
──────────────────────────────────────────────────────────
  Turn 1 — greeting (should print tools_passed=0 greeting_spliced=True
           and reply in plain text, NO tool call):
    Hi

  Turn 2+ — info tools (each should print tools_passed=6 and fire
            the named tool):
    Where can I find professor Novák?      → lookup_person
    What's for lunch in the canteen?       → mensa_menu
    How do I get to room 230?              → find_path_to_room
    When is the next B0B14SE2 lecture?     → subject_schedule
    What time is it?                       → get_time

  Internal-docs / RAG tool:
    What are the rules for the state exam? → query_search

  Sanity (should NOT fire any tool — the agent already knows these):
    Where is the gym?
    Tell me a joke.

EOF
