## Common commands

Copy agent files to woska (after editing voice-agent code in local mode):
```bash
scp -J navarlu2@ptak.felk.cvut.cz \
  voice-agent/src/{agent.py,tools.py,config.py} \
  navarlu2@woska:/mnt/data_personal/navarlu2/work/Pepper/voice-agent/src/
```

SSH to ptak (jump host):
```bash
ssh navarlu2@ptak.felk.cvut.cz
```

Restart a service after compose env changes (formerly session-manager — now orchestrator):
```bash
docker compose -f docker/docker-compose.yml restart orchestrator
```
## Starting the CLI

```bash
tmux attach -t pepper-chat 2>/dev/null || tmux new-session -s pepper-chat
uv run python services/src/text_chat.py
```

```bash
ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska
tmux attach -t pepper-agent2 2>/dev/null || tmux new-session -s pepper-agent2

# Inside tmux:
cd /mnt/data_personal/navarlu2/work/Pepper
source .venv3/bin/activate
export PEPPER_AGENT_NAME=pepper-local
export PEPPER_AGENT_MODE=local
python -m voice-agent.src.agent dev
```

Detach without stopping: `Ctrl+B` then `D`.

## 3. Start vLLM on woska (only for `local` mode)

```bash
ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska
tmux attach -t LLM 2>/dev/null || tmux new-session -s LLM


## Starting the CLI

```bash
tmux attach -t pepper-chat 2>/dev/null || tmux new-session -s pepper-chat
uv run python services/src/text_chat.py
```