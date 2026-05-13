## Common commands

Copy agent files to woska (after editing voice-agent code in local mode):
```bash
scp -J navarlu2@halmos.felk.cvut.cz \
  voice-agent/src/live/{agent.py,tools.py,config.py} \
  navarlu2@woska:/mnt/data_personal/navarlu2/work/Pepper/voice-agent/src/live/
```

SSH to halmos (jump host):
```bash
ssh navarlu2@halmos.felk.cvut.cz
```

Restart a service after compose env changes (formerly session-manager — now orchestrator):
```bash
docker compose -f docker/docker-compose.yml restart orchestrator
```
## Starting the CLI

```bash
tmux attach -t pepper-chat 2>/dev/null || tmux new-session -s pepper-chat
uv run python services/src/live/text_chat.py
```

```bash
ssh -J navarlu2@halmos.felk.cvut.cz navarlu2@woska
tmux attach -t pepper-agent2 2>/dev/null || tmux new-session -s pepper-agent2

# Inside tmux:
cd /mnt/data_personal/navarlu2/work/Pepper
source .venv3/bin/activate
export PEPPER_AGENT_NAME=pepper-local
export PEPPER_AGENT_MODE=local
python -m voice-agent.src.live.agent dev
```

Detach without stopping: `Ctrl+B` then `D`.

## Run the experiment (student study)

The experiment uses its own clean stack — **two compose files**, you
toggle between them. There's no surgical service swap; production is
fully off during experiments.

```
Production stack            ←→            Experiment stack
docker-compose.yml                        docker-compose.experiment.yml
  • orchestrator                            • experiment-orchestrator
  • voice-agent (production)                • (no agent — runs on woska)
  • bridge, audio-bridge                    • bridge, audio-bridge
  • user-client, tablet-server              • (omitted)
  • livekit, redis, weaviate                • livekit, redis, weaviate
  • reverse-tunnel                          • reverse-tunnel
  Room: pepper-<timestamp>                  Room: pepper-experiment (fixed)
```

Both stacks share the same `services/data/token-latest.json` — the
two orchestrators are mutually exclusive, you only run one at a time.

### One-time per study session

```bash
# 1. Stop production stack:
cd /home/lucas/Projects/FEL/Pepper
docker compose -f docker/docker-compose.yml down

# 2. Start experiment stack:
docker compose -f docker/docker-compose.experiment.yml up -d
docker compose -f docker/docker-compose.experiment.yml ps
# Expect: livekit, redis, weaviate, reverse-tunnel, bridge,
#         audio-bridge, experiment-orchestrator — all "running".

# 3. Sync the experiment-worker source files RPi → woska
#    (re-run any time you edit experiment/agent.py, prompt.py, or tools/):
./services/scripts/experiment/sync_to_woska.sh

# 4. Start the experiment worker on woska in tmux. vLLM must already be
#    up in its own tmux (see § 3 below). The pepper-experiment worker
#    coexists with the existing pepper-agent2 tmux — they have
#    different agent_names so they don't collide.
ssh -J navarlu2@halmos.felk.cvut.cz navarlu2@woska
tmux attach -t pepper-experiment 2>/dev/null || tmux new-session -s pepper-experiment

# Inside tmux on woska:
cd /mnt/data_personal/navarlu2/work/Pepper
source .venv3/bin/activate
# GPU STT (bigger model + fp16) — only if onnxruntime-gpu is installed
# in .venv3; otherwise leave the LOCAL_STT_* / LOCAL_TTS_USE_CUDA vars
# unset and STT/TTS run on CPU (slower but reliable).
export LOCAL_STT_MODEL=small
export LOCAL_STT_DEVICE=cuda
export LOCAL_STT_COMPUTE_TYPE=float16
export LOCAL_TTS_USE_CUDA=1
export PYTHONUNBUFFERED=1
# Make ctranslate2/faster-whisper find libcublas + libcudnn from the
# nvidia-* pip wheels. Required for LOCAL_STT_DEVICE=cuda; without it
# the worker dies on first STT call with `Library libcublas.so.12 is
# not found or cannot be loaded`.
export LD_LIBRARY_PATH="$(python -c 'import glob, nvidia; print(":".join(glob.glob(nvidia.__path__[0] + "/*/lib")))')${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
python voice-agent/src/experiment/agent.py dev
# PYTHONUNBUFFERED reaches the watchfiles-spawned worker subprocess
# (plain `python -u` does not). Without it, prewarm logs sit in a pipe
# buffer and the pane looks silent until the buffer fills (~4 KB).
# Wait for `prewarm_done`. Detach with Ctrl+B then D.
```

### Per conversation (only this runs in the foreground)

```bash
cd /home/lucas/Projects/FEL/Pepper
uv run python voice-agent/src/experiment/launcher.py \
    --student 1 --variant A
# Wait for the "AGENT WARM AND READY" banner.
# Have the student talk to Pepper.
# Type `/done` + Enter to end the conversation. JSONL log auto-saved.

# Same student, next variant:
uv run python voice-agent/src/experiment/launcher.py \
    --student 1 --variant B
uv run python voice-agent/src/experiment/launcher.py \
    --student 1 --variant C
# Next student:
uv run python voice-agent/src/experiment/launcher.py \
    --student 2 --variant A
```

Logs land in
`voice-agent/src/experiment/results/experiments/<YYYY-MM-DD>/`,
one JSONL file per conversation.

### End of study — restore production

```bash
docker compose -f docker/docker-compose.experiment.yml down
docker compose -f docker/docker-compose.yml up -d
# Optional: stop the woska tmux (Ctrl+C in pepper-experiment pane).
# Leaving it running is harmless — without the experiment compose,
# nothing dispatches to it.
```

## 3. Start vLLM on woska (only for `local` mode)

```bash
ssh -J navarlu2@halmos.felk.cvut.cz navarlu2@woska
tmux attach -t LLM 2>/dev/null || tmux new-session -s LLM

# Volume
curl -X POST http://127.0.0.1:5000/audio/volume \
  -H "Content-Type: application/json" \
  -d '{"volume":60}'
## Starting the CLI

```bash
tmux attach -t pepper-chat 2>/dev/null || tmux new-session -s pepper-chat
uv run python services/src/live/text_chat.py
```

 vllm serve hugging-quants/Meta-Llama-3.1-8B-Instruct-AWQ-INT4   --host 0.0.0.0 --port 8000   --quantization awq_marlin   --dtype half   --max-model-len 8192   --gpu-memory-utilization 0.85   --enable-auto-tool-choice   --tool-call-parser llama3_json   --chat-template ~/vllm-templates/tool_chat_template_llama3.1_json.jinja


 uv run python voice-agent/tests/local_llm_benchmark/livekit_console.py console --text

 uv run python voice-agent/src/experiment/launcher.py \
    --student 1 --variant A

./services/scripts/experiment/sync_to_woska.sh