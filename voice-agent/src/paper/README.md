# Paper (Track B) — RPi-only realtime receptionist MVP

The on-robot source for the Track B study (`docs/paper/experiment_realtime.md`):
LiveKit + an OpenAI **realtime** model (speech-to-speech) + **2 tools**, talking
to Pepper in Czech. Runs entirely on the Raspberry Pi — no GPU server, no
reverse tunnel, no RAG. Design doc: `docs/paper/paper_code_plan.md`.

## Layout

```
paper/
  agent_realtime.py   # LiveKit Agents worker: RealtimeModel + 2 tools
  prompt.py           # Czech SYSTEM_PROMPT
  dispatcher.py       # keeps one agent dispatched into pepper-experiment
  tools/
    find_room.py      # Building E directions (reuses experiment helpers)
    lookup_person.py  # UDB staff lookup (reuses experiment helpers)
```

The tools import the existing logic from `src/experiment/tools/utils/` and
`src/live/udb.py` directly — nothing is copied. They are stripped of the
cascade-only extras (emotion/gesture args, filler TTS, heartbeat shim).

## Run on the RPi

```bash
# from the project root, .env filled in (OPENAI_API_KEY, LIVEKIT_KEYS, …):
docker compose -f docker/docker-compose.paper.yml up -d --build
```

That is the whole startup: the orchestrator creates the fixed
`pepper-experiment` room and writes tokens, the worker registers under
`pepper-paper-realtime`, and `paper-dispatcher` dispatches it into the room
(and re-dispatches whenever the agent's job ends, so the stack self-heals).
Pepper stays silent until the first Czech utterance, then greets + answers
in one breath.

## The two study conditions

Model choice is an env var — no code change, no A/B loop:

```bash
PAPER_REALTIME_MODEL=gpt-realtime-2.1        # big condition (default)
PAPER_REALTIME_MODEL=gpt-realtime-2.1-mini   # mini condition
```

Set it in `.env` (or inline) and restart the agent:

```bash
docker compose -f docker/docker-compose.paper.yml up -d --force-recreate realtime-agent
```

Other knobs: `PAPER_REALTIME_VOICE` (default `marin`), `AGENT_LANG`
(default `cs`, used for the input-transcription side channel),
`PAPER_TRANSCRIPTION_MODEL` (default `gpt-4o-mini-transcribe`).

## Quick local test (laptop, no robot, no compose)

```bash
uv run python voice-agent/src/paper/agent_realtime.py console
```

Talks over the laptop mic/speakers directly — the fastest way to probe Czech
quality and tool-calling (build-order milestone 1) before touching the RPi.

## Observability

The worker prints every VAD transition, transcript, tool call/result, and
agent-state change to stdout (`docker compose … logs -f realtime-agent`) and
publishes the same cascade-compatible event stream (`tool_call`,
`asr_result`, `agent_speech`, …) on the `pepper.experiment` data topic for
future study recording. Usage totals are logged at session end.
