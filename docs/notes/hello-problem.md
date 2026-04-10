# Hello Interaction Problem

> **Resolved (2026-04-10):** The separate prompt/tool behavior per mode described below has been unified. Both OpenAI and local modes now use the same 2-tool set (`query_search` + `play_animation`) with the same tool definitions. The animation tool is enabled in both modes. The double-greeting issue was mitigated via grace-window suppression and persistent warm agents. Kept for historical reference of debugging process.

## Goal

Stabilize the very first interaction after session activation so Pepper:

- greets naturally
- optionally gestures appropriately
- does not speak tool text
- does not produce multiple nearly-identical greeting turns
- behaves consistently across `openai` and `local` agent modes

## What Was Going Wrong

We hit multiple different issues during "hello" testing:

1. Local mode tool loop

- After animation prompting changes, local Qwen/vLLM started calling `play_animation` repeatedly.
- This caused self-chaining replies from a single user turn.
- Logs showed repeated assistant outputs under the same turn and eventually:
  `maximum number of function calls steps reached`

2. OpenAI mode spoke tool text instead of calling the tool

- OpenAI Realtime sometimes produced spoken text such as:
  `[play_animation: greeting]`
- In those cases no structured `play_animation` tool call happened, and the bridge never ran an animation.

3. Warm greeting raced with first user utterance

- The warm agent sends a proactive greeting right after activation.
- If the user says "hello" at roughly the same time, Pepper can produce:
  - one proactive greeting
  - one response to the user's greeting
- This looks like a duplicate, but it is actually two separate turns close together.

## Current State

### Prompt / tool split

We now use separate prompt and tool behavior for the two backends:

- `openai`
  - animation tool is enabled
  - prompt uses semantic animation names such as `greeting`, `bow`, `explain`, `happy`, `thinking`, `dont_know`
  - OpenAI is instructed to use one animation on normal user-facing replies

- `local`
  - animation tool is disabled for normal turns
  - this avoids the local tool-call loop
  - local greeting animation is triggered from code, not from the model

### Greeting animation behavior

- OpenAI greeting now works again and can trigger animation correctly.
- Local greeting animation is code-driven via `trigger_animation("greeting")`.
- `greeting` resolves to a random `Hey_*` animation internally, so the motion stays varied.

### Double-greeting mitigation

A grace-window suppression was added:

- after warm activation, the agent waits briefly for immediate user speech
- if the user speaks within that window, the proactive greeting should be skipped

Config:

- `INITIAL_GREETING_GRACE_SEC` in `voice-agent/src/config.py`

## What I Think Happened In The Latest Interaction

Screenshot time: `01/04/2026 15:12`

The logs show this was `local` mode, not `openai`.

Sequence from logs:

- `15:12:39` session activated
- `15:12:40` local greeting path started and triggered `play_animation(Hey_1)`
- `15:12:45` proactive spoken greeting started:
  `Hello! I am Pepper, your friendly receptionist at CTU FEE. How can I assist you today?`
- user transcript `Hello, Pepper.` was only finalized at `15:12:53`
- after that, local model generated a second greeting-like response:
  `Hello! I'm here to help with any questions you have about FEE. How can I assist you?`

So the latest interaction was not caused by the old "immediate overlap" race.

It looks like this instead:

- local STT finalized the user greeting late
- by the time the transcript was delivered, the proactive greeting had already completed
- the agent then treated `Hello, Pepper.` as a fresh user turn and answered it separately

That means the current grace-window fix helps when the user speaks immediately after activation, but it does not solve delayed STT finalization in local mode.

## Current Best Hypothesis

The remaining first-turn problem is mostly a local-mode timing problem:

- local STT is slow to finalize short greetings
- proactive greeting starts before the final user transcript is available
- once the final transcript arrives, the agent still responds to it as a new turn

Contributing evidence from the latest logs:

- local mode startup and STT path are much slower than OpenAI
- user transcript delay was about `12.87s`
- animation dispatch also timed out once on that interaction

## Likely Next Fix

Most likely needed next:

1. For `local` mode, delay or suppress proactive greeting more aggressively.
2. Possibly do not proactively greet in local mode at all if incoming speech is already present.
3. Treat short first-turn greetings like `hello`, `hi`, `hello pepper` specially so Pepper does not answer them with a second full greeting if it has just greeted already.

## Files Touched So Far

- `voice-agent/src/config.py`
- `voice-agent/src/agent.py`
- `voice-agent/src/tools.py`
