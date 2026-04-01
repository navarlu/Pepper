# Local LLM Setup (Qwen via vLLM on GPU server)

Guide for running the cascade voice agent with a local LLM backend instead of OpenAI.

## Architecture

```
RPi (or Windows) ──SSH tunnel──> ptak.felk.cvut.cz ──> lie (GPU server)
     :8000          ProxyJump         jump host           vLLM :8000
```

The cascade agent pipeline: **Faster Whisper STT** (local, CPU) → **Qwen 2.5 7B** (vLLM on GPU) → **Piper TTS** (local, CPU)

## Prerequisites

- SSH key on RPi: `~/.ssh/id_ed25519` (already set up)
- Passwordless SSH to ptak: done via `ssh-copy-id navarlu2@ptak.felk.cvut.cz` (2026-03-31)
- ptak → lie is passwordless by default (CMP cluster policy)
- vLLM running on lie in tmux session `lie`

## 1. Start vLLM on the GPU server (if not running)

```bash
ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@lie
tmux new -s lie   # or: tmux attach -t lie

vllm serve Qwen/Qwen2.5-7B-Instruct \
  --host 127.0.0.1 --port 8000 \
  --enable-auto-tool-choice \
  --tool-call-parser hermes
```

Check if already running:
```bash
ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@lie "tmux ls"
```

## 2. Open the SSH tunnel (from RPi)

```bash
ssh -J navarlu2@ptak.felk.cvut.cz -L 8000:127.0.0.1:8000 -N navarlu2@lie &
```

This forwards `localhost:8000` on the RPi to vLLM on lie.

Verify the tunnel works:
```bash
curl -s http://localhost:8000/v1/models | python3 -m json.tool
```

## 3. Test the LLM (standalone)

```bash
cd /home/lucas/Projects/FEL/Pepper
uv run python tests/local_llm/test_qwen.py
```

See `tests/local_llm/test_qwen.py` for a minimal OpenAI-compatible client test.

## 4. Run the agent in local mode

Start infra first:
```bash
cd docker && docker compose up -d livekit redis weaviate
```

The unified agent (`voice-agent/src/agent.py`) handles both modes. The mode is
controlled by the **session manager** — toggle via the Operator UI button or:
```bash
curl -X POST http://localhost:8787/api/control/agent-mode -H 'Content-Type: application/json' -d '{"mode":"local"}'
```

The agent reads `LOCAL_LLM_BASE_URL` (default `http://localhost:8000/v1`) and
`LOCAL_LLM_MODEL` (default `Qwen/Qwen2.5-7B-Instruct`) from environment / config.
The local Piper voice is configured with `LOCAL_TTS_MODEL_PATH` and can be tuned
with `LOCAL_TTS_SPEAKER_ID`, `LOCAL_TTS_LENGTH_SCALE`, `LOCAL_TTS_NOISE_SCALE`,
and `LOCAL_TTS_NOISE_W_SCALE`.

## 5. Docker integration

In `docker/docker-compose.yml` under `voice-agent`, set these env vars:
```yaml
environment:
  LOCAL_LLM_BASE_URL: http://host.docker.internal:8000/v1
  LOCAL_LLM_MODEL: Qwen/Qwen2.5-7B-Instruct
  LOCAL_TTS_MODEL_PATH: /workspace/voice-agent/models/piper/en_US-hfc_female-medium.onnx
  LOCAL_TTS_SPEAKER_ID: ""
  LOCAL_TTS_LENGTH_SCALE: 1.0
  LOCAL_TTS_NOISE_SCALE: 0.667
  LOCAL_TTS_NOISE_W_SCALE: 0.8
  PEPPER_AGENT_MODE: local   # set to `local` to prewarm Whisper/Piper; default is `openai`
```

Then: `docker compose up -d voice-agent`

## Troubleshooting

- **Tunnel died?** Re-run the `ssh -L ...` command from step 2
- **vLLM not responding?** SSH into lie and check `tmux attach -t lie`
- **Connection refused on :8000?** Check `ss -tlnp | grep 8000` on RPi to verify tunnel is up
- **Tool calls not working?** vLLM must be started with `--enable-auto-tool-choice --tool-call-parser hermes`

## SSH setup history (2026-03-31)

1. RPi already had `~/.ssh/id_ed25519` key
2. `ssh-copy-id navarlu2@ptak.felk.cvut.cz` — installed key on ptak
3. Accepted lie host key via ProxyJump: `ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@lie`
4. Verified: `ssh -J ... navarlu2@lie echo "it works"` — passwordless end-to-end
