# vLLM / Voice Agent — Operational Notes

> **Note (2026-04-15):** tmux session for the local mode agent on woska is
> `pepper-agent2` (was `pepper-agent` in older notes). The `LLM` session for
> vLLM is unchanged.



## How to get vLLM logs from woska

vLLM runs in a tmux session on woska. To check the logs:

```bash
# From the RPi (via jump host):
ssh -J navarlu2@halmos.felk.cvut.cz navarlu2@woska "tmux capture-pane -t LLM:0.0 -p -S -100"
```

- **Session:** `LLM`
- **Pane 0:** vLLM server (python3 process) — this is where errors appear
- **Pane 1:** bash shell (used for scp commands etc.)

### What to look for

The most common error is the hermes tool parser failing to extract tool calls:

```
ERROR hermes_tool_parser.py:136 - Error in extracting tool call from response.
json.decoder.JSONDecodeError: Expecting value: line 2 column 1 (char 1)
```

This means Qwen generated a `<tool_call>` block with broken/empty JSON inside. When this happens, vLLM falls back to returning the raw text (including `<tool_call>` tags) as content, which causes tool syntax leakage into speech.

### Other useful tmux commands

```bash
# List sessions:
ssh -J navarlu2@halmos.felk.cvut.cz navarlu2@woska "tmux list-sessions"

# List panes in LLM session:
ssh -J navarlu2@halmos.felk.cvut.cz navarlu2@woska "tmux list-panes -t LLM -F '#{pane_index} #{pane_current_command}'"

# Get more scrollback (last 500 lines):
ssh -J navarlu2@halmos.felk.cvut.cz navarlu2@woska "tmux capture-pane -t LLM:0.0 -p -S -500"
```

### Pepper local agent logs (woska)

The local mode voice agent (`pepper-local`) runs in a tmux session on woska:

```bash
ssh -J navarlu2@halmos.felk.cvut.cz navarlu2@woska "tmux capture-pane -t pepper-agent2 -p -S -100"
```

### Pepper OpenAI agent logs (RPi)

The OpenAI mode voice agent (`pepper-openai`) runs as a Docker container on the RPi:

```bash
docker compose -f docker/docker-compose.yml logs --tail=50 voice-agent
```
