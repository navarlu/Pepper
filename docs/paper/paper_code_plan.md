# Paper code plan — RPi-only realtime receptionist MVP (Track B)

Goal: a **minimal** stack that runs **entirely on the Raspberry Pi** — LiveKit +
an OpenAI **realtime** model (speech-to-speech) + **2 tools** — talking to Pepper
in Czech. No GPU server, no reverse tunnel, no RAG. This is the code for the
Track B study (`experiment_realtime.md`); the offline benchmark
(`docs/paper/benchmark/`) is Track A and unaffected.

**Status: built** — the MVP lives in `voice-agent/src/paper/` +
`docker/docker-compose.paper.yml` (see `voice-agent/src/paper/README.md`).
Dispatch is handled by a small `paper-dispatcher` service that keeps one
agent dispatched into `pepper-experiment` and re-dispatches when a job ends.
`redis` was verified unused (no code references) and dropped.

---

## Why it gets much simpler

The current stack is split across two machines because the *local* cascade
needed a GPU:

```
Current (docker-compose.experiment.yml):
  RPi:   livekit, redis, weaviate, orchestrator, bridge, safe-startup,
         audio-bridge, user-client, tablet-server, reverse-tunnel
  woska: voice-agent workers (local vLLM cascade + cloud cascade)  ← the GPU box
  reverse-tunnel exists ONLY so woska's workers can reach the RPi's LiveKit.
```

A cloud realtime model needs no GPU, so the agent can run **on the RPi itself**.
That collapses the whole remote half:

```
Target (docker-compose.paper.yml, RPi only):
  livekit  ──  orchestrator (room + token)
     │
  user-client (USB mic + AEC) ──► LiveKit room ◄── realtime-agent (NEW: gpt-realtime-2.1[-mini] + 2 tools)
                                        │                    │ HTTP tool calls
  audio-bridge (agent audio ─ssh+paplay─► Pepper speakers)   ▼
  tablet-server ──HTTP──► bridge (QI: gestures/tablet/LEDs) ◄── safe-startup
```

- **No reverse-tunnel** (agent is local — nothing remote to reach in).
- **No GPU / vLLM / woska**.
- **No weaviate / RAG** (Track B has no `query_search`).
- The realtime model replaces the STT+LLM+TTS cascade *inside* one worker; the
  audio plumbing around it is unchanged.

---

## Keep / Drop / New

### Keep (reuse as-is)

| Component | File | Role |
|---|---|---|
| LiveKit server | `docker/livekit/livekit.yaml` | media server (single-node; `auto_create: true`, no redis needed) |
| Orchestrator | `services/src/experiment/orchestrator.py` | creates the `pepper-experiment` room + writes `token-latest.json` |
| Robot bridge | `robot/src/bridge.py` | QI: animations, tablet push, LEDs, volume (needs the QI native-lib mounts) |
| Safe-startup | `robot/scripts/safe_startup_watchdog.py` | boot pose/volume watchdog |
| Audio out | `services/src/live/audio_bridge.py` | agent audio track → ssh+paplay → Pepper PulseAudio |
| Audio in | `services/src/live/user_client.py` | USB mic capture + **WebRTC AEC3** → LiveKit (keep — mic hears Pepper across the glass) |
| Tablet UI | `services/src/live/tablet_server.py` | live transcript / state pills |
| Runtime image | `docker/Dockerfile.runtime` | base image for all services |

### Drop (not in the paper stack)

| Component | Why |
|---|---|
| `reverse-tunnel` | no remote workers to expose the RPi to |
| `weaviate` + `voice-agent/src/live/rag.py` + `query_search` tool | no RAG in Track B |
| `redis` | `livekit.yaml` has no redis block → single-node in-memory (verify nothing else uses it, then drop) |
| woska side: vLLM, `agent_streaming.py` (local cascade) | no GPU / local model |
| `agent_4o_streaming.py` (cloud cascade) | replaced by the realtime agent |
| cascade internals: `audio_capture.py`, `local_speech.py`, `qwen_compat.py`, `_streaming_runtime.py`, VAD/STT/TTS plugins | realtime model does speech-to-speech itself |
| `loop_launcher_streaming.py` (A/B alternation) | MVP is one model; study-time model choice is an env var, not a loop |
| tools: `subject_schedule`, `mensa_menu`, `get_time`, `query_search`, `end_conversation` | keep only 2 (see below) |

### New (build under `voice-agent/src/paper/`)

| Component | Role |
|---|---|
| realtime agent worker | LiveKit Agents worker using `openai.realtime.RealtimeModel`, Czech prompt, 2 tools, joins `pepper-experiment` |
| Czech system prompt | trimmed, Czech, 2-tool version of `voice-agent/src/experiment/prompt_streaming.py` |
| 2 tools | reuse existing tool logic (see below) |
| `docker/docker-compose.paper.yml` | the RPi-only stack |

---

## Proposed source layout

The paper source lives **inside `voice-agent/src/`, next to `experiment/` and
`live/`** — same package, same imports, same Docker mounts. (`docs/paper/`
stays docs-only: design docs + the Track-A benchmark.)

```
voice-agent/src/
  experiment/             # existing cascade workers (untouched)
  live/                   # existing shared runtime (untouched)
  paper/                  # NEW — Track B realtime agent
    __init__.py
    agent_realtime.py     # LiveKit Agents worker: RealtimeModel + tools, joins the room
    prompt.py             # Czech SYSTEM_PROMPT (identity, reply style, when-to-call-tools)
    tools/
      __init__.py
      find_room.py        # reuses voice-agent room-directions logic
      lookup_person.py    # reuses voice-agent UDB lookup logic
    README.md
docker/docker-compose.paper.yml
```

Living inside `voice-agent/src/` means the tools can import the existing logic
directly (`..experiment.tools.utils.find_path_to_room`, `..live.udb`) instead
of copying it, and the compose service reuses the same bind-mount the cascade
workers already use.

---

## The 2 tools

Recommended pair (reuse the existing implementations rather than rewrite):

1. **`find_room`** — directions from a room code. Backed by the manual dict in
   `voice-agent/src/experiment/tools/utils/find_path_to_room.py` (offline,
   deterministic, zero risk). The clean "happy path" tool.
2. **`lookup_person`** — staff contact by surname, via the live UDB scrape in
   `voice-agent/src/live/udb.py`. This is the compelling **Czech** demo (Czech
   surnames through speech) — and the riskiest, which is exactly what Track B
   wants to probe.

If Czech-name recognition proves shaky in the verify step, swap `lookup_person`
for **`mensa_menu`** (no name-matching, still a live tool). Reuse strategy: import
the core logic; drop the cascade-only extras (the `emotion`/`request_heartbeat`
args and gesture side-effects can be kept if we want embodiment, or stripped for
a first MVP).

---

## docker-compose.paper.yml — service list

In: `livekit`, `orchestrator`, `bridge`, `safe-startup`, `audio-bridge`,
`user-client`, `tablet-server`, **`realtime-agent` (new)**.
Out: `reverse-tunnel`, `weaviate`, `redis` (pending verify).

The `realtime-agent` service: built on `Dockerfile.runtime`, entrypoint
`voice-agent/src/paper/agent_realtime.py` (same `voice-agent` bind-mount the
cascade workers use), `network_mode: host`, needs `LIVEKIT_URL`, LiveKit API key/secret, and `OPENAI_API_KEY` from
`.env`, plus `PAPER_REALTIME_MODEL` (`gpt-realtime-2.1` | `gpt-realtime-2.1-mini`).
It does **not** need the QI mounts (only `bridge`/`safe-startup` do), so it's a
lean service.

---

## The realtime agent (how it differs from the cascade workers)

- Build an `AgentSession` around `openai.realtime.RealtimeModel(model=…,
  voice=…)` — **no** separate VAD/STT/LLM/TTS plugins; the realtime model does
  speech-in/speech-out and server-side turn detection.
- Register the 2 tools as `@function_tool`s (same LiveKit Agents mechanism the
  cascade used).
- Czech `instructions` (system prompt).
- Join the fixed `pepper-experiment` room (confirm dispatch wiring — explicit
  dispatch by agent name, as the cascade workers did, vs. auto-dispatch).
- Model chosen by env var → flip `gpt-realtime-2.1` ↔ `-mini` for the study's two
  conditions (no code change, no A/B loop for the MVP).

---

## Dependencies to add

- `livekit-agents` + `livekit-plugins-openai` **with realtime support** (the
  cascade already uses the OpenAI plugin; confirm the installed version exposes
  `openai.realtime.RealtimeModel`, bump if needed).
- No new infra deps (drop weaviate/redis images).

---

## Build order (MVP milestones)

1. **Verify-first (gate):** Czech recognition of Czech surnames, and that the
   *mini* reliably calls tools from Czech speech. (Probes — the whole track
   depends on this.)
2. `voice-agent/src/paper/prompt.py` (Czech) +
   `voice-agent/src/paper/tools/{find_room,lookup_person}.py` (import the
   existing logic, don't copy).
3. `voice-agent/src/paper/agent_realtime.py` — realtime worker, 2 tools, joins the room.
   Test standalone against a laptop mic in the LiveKit room (before Pepper).
4. `docker/docker-compose.paper.yml` — bring up livekit + orchestrator +
   realtime-agent + user-client + audio-bridge; confirm end-to-end audio in/out.
5. Add `bridge` + `tablet-server` + `safe-startup`; confirm gestures + tablet.
6. On Pepper at the desk: end-to-end Czech interaction, both models via env var.
7. Logging for the study (task events, tokens/cost, time-to-first-audio).

---

## Open questions (resolve before/while building)

- **Czech quality** end-to-end, esp. Czech surnames → tool args (verify).
- **Mini tool-calling** reliability from Czech speech (verify).
- ~~**Dispatch**~~ RESOLVED: explicit agent-name dispatch, automated by
  `voice-agent/src/paper/dispatcher.py` (compose service `paper-dispatcher`)
  — no manual launcher step, self-heals after job end.
- ~~**redis**~~ RESOLVED: no code references it → dropped from the paper compose.
- **AEC:** keep `user-client`'s WebRTC AEC3 (mic still hears Pepper across the
  glass) — assume yes.
- **Realtime latency on the RPi** (re-measure; text TTFT numbers don't transfer).
- **QI mounts** are host-specific absolute paths in the compose — keep them only
  on `bridge`/`safe-startup`.

---

## Explicitly NOT building here

- No offline benchmark for Track B (that's Track A only).
- No A/B alternation loop (env var swaps the model).
- No cascade, no local model, no RAG, no reverse tunnel, no GPU server.
