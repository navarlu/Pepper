# Local LLM LiveKit console

This folder is for tuning Llama 3.1 8B through the same LiveKit tool surface
the production agent uses. `tools.py` is the source of truth: LiveKit reads the
`@function_tool` names, type hints, and docstrings from there.

## Run

From the project root, with the SSH tunnel to woska up:

```bash
uv run python voice-agent/tests/local_llm_benchmark/probe.py
uv run python voice-agent/tests/local_llm_benchmark/runner.py
uv run python voice-agent/tests/local_llm_benchmark/livekit_console.py console --text
```

`probe.py` only checks that vLLM is reachable. `runner.py` is a lightweight
unit-style check that the prompt/tool surface imports cleanly and does not
drift back to FEL. Use `livekit_console.py console --text` for real behavioral
tuning.

## Files

- `prompt.py` — shared system prompt.
- `tools.py` — dummy LiveKit `@function_tool` implementations and docstrings.
- `livekit_console.py` — real LiveKit console agent using `tools.py`.
- `chat.py` — deprecated direct-vLLM REPL stub.

The old direct OpenAI-schema benchmark was intentionally removed because it
tested a different tool surface from production.
