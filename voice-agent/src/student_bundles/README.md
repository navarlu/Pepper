# Student Bundles

One folder per team. Each folder holds two files in the
teaching/tool-playground format:

```
student_bundles/
├── example/
│   ├── system_prompt.py      # SYSTEM_PROMPT = "..."
│   └── tools.py              # TOOLS = [{"schema": ..., "function": ...}]
├── team1/
│   ├── system_prompt.py
│   └── tools.py
└── team2/
    ├── system_prompt.py
    └── tools.py
```

## Adding a new team

1. Create a new folder, e.g. `team3/`.
2. Paste the student's `system_prompt.py` and `tools.py` in — unchanged.
3. In `voice-agent/src/student_bundle.py`, set `ACTIVE_TEAM = "team3"`.
4. Restart the voice-agent container.

## Notes

- Empty `ACTIVE_TEAM` disables the student-lab mode entirely (agent
  uses its normal system prompt and tool set).
- A missing folder or file is logged as a warning — the agent still
  starts, just without the missing piece.
