# Voice Agent — Operational Notes

## How to get vLLM logs from woska

vLLM runs in a tmux session on woska. To check the logs:

```bash
# From the RPi (via jump host):
ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska "tmux capture-pane -t LLM:0.0 -p -S -100"
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
ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska "tmux list-sessions"

# List panes in LLM session:
ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska "tmux list-panes -t LLM -F '#{pane_index} #{pane_current_command}'"

# Get more scrollback (last 500 lines):
ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska "tmux capture-pane -t LLM:0.0 -p -S -500"
```

### Pepper agent logs

The LiveKit voice agent also runs in a tmux session on woska:

```bash
ssh -J navarlu2@ptak.felk.cvut.cz navarlu2@woska "tmux capture-pane -t pepper-agent -p -S -100"
```
