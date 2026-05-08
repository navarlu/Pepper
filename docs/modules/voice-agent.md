# `voice-agent/` — The LLM brain

The voice-agent is where the receptionist actually "thinks". It joins
the LiveKit room as a worker, listens to the user, talks back, and
decides when to trigger tools (RAG search, Pepper animations,
optional vision captioning). Two backends are wired in and selected
per-dispatch by a metadata flag:

| Mode      | LLM                                  | STT                | TTS              |
|-----------|--------------------------------------|--------------------|------------------|
| `openai`  | OpenAI Realtime (`gpt-realtime-mini`) | Realtime API        | Realtime API     |
| `local`   | Qwen 2.5 7B via vLLM                  | Faster-Whisper (CPU/ARM64) | Piper (ONNX)     |

Both modes use the same tool set. Both boot as *warm persistent*
agents: once loaded they stay in the room across conversations and
only wipe their own chat history on idle — see the persistent loop in
[agent.py](../../voice-agent/src/live/agent.py).

---

## Big picture

```
  user voice ──►  LiveKit room  ──►  agent.entrypoint(ctx)
                                       │
                                       ├── wait for user participant
                                       │   (skip the audio-bridge listener identity)
                                       │
                                       ├── build session by mode:
                                       │     openai → RealtimeModel
                                       │     local  → Whisper STT + vLLM LLM + Piper TTS
                                       │               + qwen_compat.wrap_llm_chat_with_history_sanitizer()
                                       │
                                       ├── register tools (query_search, play_animation, look_around?)
                                       │
                                       ├── session.start() with close_on_disconnect=False
                                       │
                                       └── persistent loop:
                                             ├── idle_monitor → reset_event when silent
                                             └── reset_event → clear chat history, loop
                                             └── shutdown_event → session.aclose + ctx.shutdown
```

On every turn:

```
user audio → STT → LLM (with tool schemas) ──► tool call? ──► yes: run tool, feed result back, LLM again
                                               │
                                               └── no: text → TTS → LiveKit → audio-bridge → Pepper speakers
```

---

## Code layout

| File | Role |
|------|------|
| [agent.py](../../voice-agent/src/live/agent.py) | Entry point. LiveKit `entrypoint()`, session lifecycle, mode selection, persistent loop, model prewarming, RTC diagnostics, session-control signals. |
| [config.py](../../voice-agent/src/live/config.py) | Every tunable. Env-backed. Includes the two system prompts, `ANIMATION_GROUPS`, `ANIMATION_TOOL_ALIASES`, Weaviate settings, Piper/Whisper settings, vision settings. |
| [tools.py](../../voice-agent/src/live/tools.py) | The three `@function_tool` definitions built in `build_tools(agent_mode)`. Tool logic only — HTTP plumbing lives in `bridge_client`. |
| [bridge_client.py](../../voice-agent/src/live/bridge_client.py) | HTTP clients for the robot bridge (animations, LEDs, camera) and the side VL describer (captioning). |
| [rag.py](../../voice-agent/src/live/rag.py) | Weaviate client: connect, create collection, seed from `.txt` files under `data/FEL/`, hybrid search. Formerly `utils.py` (renamed to match its actual role). |
| [rooms.py](../../voice-agent/src/live/rooms.py) | Hand-curated directions for Building E (Karlovo náměstí). Keyword + regex lookup so `query_search` can answer room questions deterministically. |
| [local_speech.py](../../voice-agent/src/live/local_speech.py) | `FasterWhisperSTT` and `PiperTTS` — LiveKit plugin classes that wrap the local STT/TTS models for the `local` mode. |
| [qwen_compat.py](../../voice-agent/src/live/qwen_compat.py) | Qwen 2.5 quirk patches (malformed tool-call JSON). Isolated so the workaround can be deleted wholesale once we move off Qwen 2.5. |

---

## `agent.py` — what happens on dispatch

### 1. Mode selection via dispatch metadata

The orchestrator dispatches this worker with a JSON metadata blob:

```json
{"agent_mode": "openai" | "local", "warm": true}
```

`_parse_dispatch_metadata(ctx)` reads it. `agent_mode` picks the
session builder; `warm=True` enables the persistent loop.

### 2. Wait for the user participant

A LiveKit room already has the audio-bridge sitting in it as the
"listener" identity (`LISTENER_IDENTITY`). The agent must bind to the
*actual* user, not the bridge, or STT would just loop the agent's own
output. `_wait_for_user_participant()` polls `remote_participants`,
skipping any identity in `{LISTENER_IDENTITY, MONITOR_IDENTITY}`.

<!-- snippet: wait_for_user_participant -->
<!-- generated from voice-agent/src/live/agent.py:117 by docs/embed_snippets.py -->
```python
async def _wait_for_user_participant(ctx: JobContext):
    last_logged_identity = None
    while True:
        for participant in _iter_remote_participants(ctx):
            if not _is_bridge_listener(participant):
                return participant
            identity = str(getattr(participant, "identity", "") or "")
            if identity and identity != last_logged_identity:
                logger.info(
                    "waiting_for_user_participant skipping_identity=%s",
                    identity,
                )
                last_logged_identity = identity
        await asyncio.sleep(0.2)
```
<!-- /snippet -->

### 3. Build the session

Two paths, sharing nothing except the resulting `AgentSession`:

- **`_build_openai_session(api_key)`** — `openai.realtime.RealtimeModel`
  with `MODEL_NAME` / `TTS_VOICE`. STT/LLM/TTS all live server-side
  at OpenAI.
- **`_build_local_session()`** — `openai.LLM(base_url=vLLM)` +
  pre-warmed VAD + `FasterWhisperSTT` + `PiperTTS`. Also wraps
  `local_llm.chat` via `qwen_compat.wrap_llm_chat_with_history_sanitizer`
  so malformed tool-call JSON in history doesn't crash vLLM 0.19.

Prewarming is handled by `_prewarm_process()`, called by the LiveKit
framework before the first dispatch. It eagerly loads VAD (always) and
Whisper + Piper (unless `PEPPER_AGENT_MODE=openai`). This is what
keeps cold-boot latency low.

### 4. Tools & listeners

`build_tools(agent_mode)` returns `[query_search, play_animation]`
(and `look_around` if `ENABLE_LOOK_AROUND_TOOL`). The agent also
registers a tool-event listener that forwards every tool call to the
debug CLI over the `pepper.debug` data topic.

Data-channel listeners:
- `session-control` — activate/reset/shutdown from the orchestrator.
- `pepper.control` — external `reset` command.
- `pepper.text` — text input from the debug CLI (stored as user turn,
  triggers a reply).

### 5. Start

```python
await session.start(
    agent=agent,
    room=ctx.room,
    room_options=room_io.RoomOptions(
        close_on_disconnect=False,         # persistent: keep session alive
        participant_identity=user.identity,
        text_input=room_io.TextInputOptions(),
    ),
)
```

`close_on_disconnect=False` is what lets the same session survive a
user reconnecting — the STT/LLM/TTS pipeline stays warm.

### 6. Persistent loop

A long-running `while True:` that waits for either `reset_event` or
`shutdown_event`.

- **Reset** (idle-timeout OR `/reset` signal): `session.interrupt()`,
  `session.clear_user_turn()`, `agent.update_chat_ctx(empty)`,
  broadcast a `session_reset` event on `pepper.debug`, loop.
- **Shutdown** (mode switch): `session.aclose()` + `ctx.shutdown()`.
  Critical — just returning from entrypoint isn't enough because
  other participants are still in the room, so LiveKit keeps the job
  alive. This is how a mode switch actually unblocks the worker.

The idle monitor is guarded by `had_activity_since_reset` — it
**only** resets if real conversation happened. Prevents the "clear
empty history every 60s" log spam on an idle room.

<!-- snippet: idle_monitor -->
<!-- generated from voice-agent/src/live/agent.py:646 by docs/embed_snippets.py -->
```python
async def _idle_monitor() -> None:
    """Check for idle timeout every 2s. Only fires reset when there is
    something to reset — i.e. real conversation activity has happened
    since the last reset. Otherwise we'd clear an already-empty history
    every 60s forever."""
    nonlocal last_user_activity
    while not sc.shutdown_event.is_set():
        await asyncio.sleep(2)
        if not had_activity_since_reset:
            continue
        idle_sec = time.monotonic() - last_user_activity
        if idle_sec >= SESSION_IDLE_TIMEOUT_SEC:
            logger.info(
                "[idle] %.0fs silence — clearing conversation history",
                idle_sec,
            )
            sc.reset_reason = "idle"
            sc.reset_event.set()
            # Wait until the reset is processed before resuming monitoring
            while sc.reset_event.is_set() and not sc.shutdown_event.is_set():
                await asyncio.sleep(0.5)
```
<!-- /snippet -->

<!-- snippet: persistent_loop -->
<!-- generated from voice-agent/src/live/agent.py:674 by docs/embed_snippets.py -->
```python
while True:
    reset_task = asyncio.ensure_future(sc.reset_event.wait())
    shutdown_task = asyncio.ensure_future(sc.shutdown_event.wait())
    done, pending = await asyncio.wait(
        [reset_task, shutdown_task],
        return_when=asyncio.FIRST_COMPLETED,
    )
    for t in pending:
        t.cancel()

    if sc.shutdown_event.is_set():
        logger.info(
            "[PERSIST] shutdown received — exiting loop sessions_served=%d agent_mode=%s",
            session_num,
            agent_mode,
        )
        # Per LiveKit docs: returning from entrypoint is NOT enough —
        # the job stays alive as long as other non-agent participants
        # (user-client, debug-cli) are in the room. Must explicitly
        # call ctx.shutdown() + session.aclose() so the framework
        # disconnects us from the room and the participant record is
        # gone. Without this, mode-switch leaves a zombie.
        try:
            await session.aclose()
        except Exception as exc:
            logger.debug("session.aclose failed err=%s", exc)
        ctx.shutdown(reason="mode_switch_requested")
        return

    session_num += 1
    reason = sc.reset_reason or "unknown"
    logger.info(
        "[PERSIST] resetting chat history session_num=%d reason=%s",
        session_num,
        reason,
    )
    try:
        await session.interrupt()
        session.clear_user_turn()
        await agent.update_chat_ctx(llm.ChatContext.empty())
        logger.info(
            "[PERSIST] history cleared, agent ready for next user session_num=%d",
            session_num,
        )
    except Exception as exc:
        logger.error("[PERSIST] reset failed error=%s", exc)

    # Broadcast the event so observers (chat CLI) can show it inline.
    asyncio.create_task(_publish_debug_async({
        "kind": "session_reset",
        "reason": reason,
        "session_num": session_num,
    }))

    # Reset the activity timer + dirty flag so the next idle window
    # starts now and won't fire again until there's real activity.
    last_user_activity = time.monotonic()
    had_activity_since_reset = False
    sc.reset_reason = ""
    sc.reset_event.clear()
    logger.info("[PERSIST] persistent_agent_ready session_num=%d", session_num)
```
<!-- /snippet -->

### 7. RTC diagnostics

A block of event handlers on `ctx.room` logs everything WebRTC-level
(connection_state, quality, reconnecting, track_subscribed, …) plus a
periodic `_rtc_rtt_sampler` that prints per-participant RTT every 5s.
Pure observability — none of it affects behavior. Lives inline in
`entrypoint()` because it closes over `_rtc_t0` and the session
sentinel.

---

## Tools — [`tools.py`](../../voice-agent/src/live/tools.py)

All three tools are built inside `build_tools(agent_mode)`. The
closure captures `agent_mode` so `play_animation` can return
differently in local vs openai mode (see the Qwen quirk below).

### `query_search(query)`

The RAG tool. Flow:

1. **Fast path — room lookup.** `rooms.try_room_lookup(query)` checks
   if the query looks like "where is room 107". Returns a hand-curated
   directions dict on match. Purely deterministic — no Weaviate call.
2. **Slow path — hybrid search.** `rag.search_vectors(query,
   limit=5, alpha=0.7)` runs a hybrid (vector + BM25) query against
   the Weaviate `fel_v007` collection. The 5 top results are trimmed
   to `{title, content, source, score}` and returned as JSON text.
3. While the search runs, LED mode is set to `search_pulse` via
   `bridge_client.post_led_state` so Pepper's eyes pulse blue.
   Restored to `idle` in `finally`.

### `play_animation(animation)`

Two tool functions with the same implementation but different
docstrings:

- **`play_animation_openai`** — docstring is a plain side-effect
  description. The Realtime model happily ignores the return and
  doesn't loop.
- **`play_animation_local`** — docstring is framed as *"returns the
  current body state which you need before speaking"*. This is a
  prompt-engineering hack: Qwen 7B keeps the tool in the active set
  when the docstring implies a required return value, so it actually
  uses the tool. See [tools-issue.md](../notes/tools-issue.md) for
  the full 10-attempt investigation.

Shared logic in `_play_animation_impl(animation)`:

1. Validate length/non-empty.
2. `_normalize_animation_name()` resolves the name through:
   - group name → random variant from `ANIMATION_GROUPS[group]`
   - alias → mapped group → random variant
   - direct key → pass-through
3. `asyncio.create_task(_dispatch_animation(resolved))` fires the
   HTTP POST without blocking the LLM turn.
4. Returns:
   - **local mode** — `None`. This is deliberate: returning a value
     would trigger a second LLM call (Qwen emits text + tool call in
     the same response, so the text is already heading to TTS — a
     second call would duplicate the reply). See the inline comment.
   - **openai mode** — `{"ok": true, "status": "queued", ...}` JSON.

<!-- snippet: normalize_animation_name -->
<!-- generated from voice-agent/src/live/tools.py:73 by docs/embed_snippets.py -->
```python
def _normalize_animation_name(raw_name: str) -> str:
    """Resolve an animation name to a concrete Pepper animation key.

    Accepts:
    - Group names (e.g. "greeting") → picks a random variant from the group.
    - Natural-language aliases (e.g. "hello") → mapped to a group, then randomized.
    - Direct animation keys (e.g. "Hey_1") → passed through as-is.
    """
    clean = str(raw_name or "").strip()
    if not clean:
        return ""

    # 1. Direct match against a group name (case-insensitive).
    clean_lower = clean.lower().replace("-", "_").replace(" ", "_")
    clean_lower = "".join(ch for ch in clean_lower if ch.isalnum() or ch == "_")

    if clean_lower in ANIMATION_GROUPS:
        return _pick_from_group(clean_lower)

    # 2. Alias lookup → resolves to a group name.
    mapped_group = ANIMATION_TOOL_ALIASES.get(clean_lower)
    if mapped_group and mapped_group in ANIMATION_GROUPS:
        return _pick_from_group(mapped_group)

    # 3. Direct animation key (exact or case-insensitive).
    if clean in ANIMATION_TOOL_ALLOWED:
        return clean
    for key in ANIMATION_TOOL_ALLOWED:
        if key.lower() == clean.lower():
            return key

    return ""
```
<!-- /snippet -->

<!-- snippet: play_animation_impl -->
<!-- generated from voice-agent/src/live/tools.py:262 by docs/embed_snippets.py -->
```python
async def _play_animation_impl(animation: str) -> str:
    """Shared implementation for play_animation (both modes)."""
    t0 = time.monotonic()

    if not ENABLE_ANIMATION_TOOL:
        return json.dumps(
            {"error": "play_animation_disabled"},
            ensure_ascii=False,
        )

    animation_name = str(animation or "").strip()
    if not animation_name:
        return json.dumps(
            {"error": "missing_animation", "message": "animation name cannot be empty"},
            ensure_ascii=False,
        )
    if len(animation_name) > ANIMATION_TOOL_MAX_NAME_CHARS:
        return json.dumps(
            {
                "error": "animation_name_too_long",
                "max_chars": int(ANIMATION_TOOL_MAX_NAME_CHARS),
            },
            ensure_ascii=False,
        )

    resolved = _normalize_animation_name(animation_name)
    allowed = list(ANIMATION_GROUPS.keys())
    error_message = "Use one of the allowed animation group names."

    if not resolved:
        result_payload = {
            "error": "unknown_animation",
            "message": error_message,
            "allowed": allowed,
        }
        duration_ms = (time.monotonic() - t0) * 1000
        await asyncio.to_thread(
            _post_tool_event, "play_animation",
            {"animation": animation_name}, result_payload, duration_ms,
            error="unknown_animation",
        )
        return json.dumps(result_payload, ensure_ascii=False)

    logger.info("play_animation_queued animation=%s resolved=%s", animation_name, resolved)
    asyncio.create_task(_dispatch_animation(resolved))

    duration_ms = (time.monotonic() - t0) * 1000

    if agent_mode == "local":
        # Local mode: return None so the LiveKit SDK does NOT re-call the LLM.
        # Qwen generates text + tool_call in the same response, so text is
        # already being sent to TTS. Returning data here would trigger a
        # second LLM call (livekit/agents#4554).
        result_payload = {"body_state": "ready", "posture": resolved}
        await asyncio.to_thread(
            _post_tool_event, "play_animation",
            {"animation": animation_name, "resolved": resolved},
            result_payload, duration_ms,
        )
        return None
    else:
        result_payload = {
            "ok": True,
            "status": "queued",
            "animation": resolved,
        }
        await asyncio.to_thread(
            _post_tool_event, "play_animation",
            {"animation": animation_name, "resolved": resolved},
            result_payload, duration_ms,
        )
        return json.dumps(result_payload, ensure_ascii=False)
```
<!-- /snippet -->

### `look_around(purpose)` — gated by `ENABLE_LOOK_AROUND_TOOL`

1. `bridge_client.fetch_camera_snapshot()` → JPEG bytes.
2. `bridge_client.describe_image_with_vl(jpeg, purpose)` → plain-text
   caption from a side vLLM serving Qwen2.5-VL.
3. Returns `"CAMERA VIEW: <description>\n\nReply to the user now ..."`
   — not JSON. Qwen 7B ignores structured JSON returns but reads flat
   text with instructions aggressively. Empirically this is what
   actually makes it talk about what it saw.

The VL model is separate from the main chat model on purpose:
Qwen2.5-VL has chat-template issues around tool-calling that bit us
early on, so we isolate vision reasoning behind a one-shot caption
call.

### The tool-event hook

Each tool reports `(name, args, result, duration_ms, error)` via
`_post_tool_event`, which also forwards to the external listener
registered by `agent.py`. That listener publishes on `pepper.debug`
so the [text_chat CLI](../../services/src/live/text_chat.py) can stream
tool activity live.

---

## Local speech — [`local_speech.py`](../../voice-agent/src/live/local_speech.py)

### `FasterWhisperSTT`

Non-streaming LiveKit STT plugin. Flow per utterance:

1. LiveKit's VAD hands us a list of `AudioFrame` representing one
   utterance. Combine → int16 PCM → downmix to mono → float32 →
   resample to 16 kHz via `np.interp`.
2. RMS check: if the buffer is quieter than `min_energy` (default
   0.01), discard the Whisper output. This kills the classic "mic
   bleed → hallucinated 'thank you'" failure mode.
3. `WhisperModel.transcribe(beam_size=1, best_of=1, vad_filter=True)`.
   Small-beam settings trade accuracy for latency; we're real-time.
4. Report `{stage: "stt", duration_ms, audio_duration_ms, text}` via
   `on_metrics`.

<!-- snippet: whisper_recognize -->
<!-- generated from voice-agent/src/live/local_speech.py:117 by docs/embed_snippets.py -->
```python
async def _recognize_impl(
    self,
    buffer: rtc.AudioFrame | list[rtc.AudioFrame],
    *,
    language: NotGivenOr[str] = NOT_GIVEN,
    conn_options: APIConnectOptions,
) -> stt.SpeechEvent:
    del conn_options
    frame = rtc.combine_audio_frames(buffer)
    pcm = np.frombuffer(frame.data, dtype=np.int16)
    if frame.num_channels > 1:
        pcm = pcm.reshape(-1, frame.num_channels).mean(axis=1).astype(np.int16)

    audio = (pcm.astype(np.float32) / 32768.0).clip(-1.0, 1.0)
    audio_16k = _resample_audio(audio, src_rate=frame.sample_rate, dst_rate=16000)
    audio_duration_ms = round(len(audio_16k) / 16000.0 * 1000, 1)

    requested_language: str | None = None
    if language is not NOT_GIVEN:
        requested_language = language
    elif self._language:
        requested_language = self._language

    t0 = time.monotonic()
    text, detected_language = await asyncio.to_thread(
        self._recognize_sync,
        audio_16k,
        requested_language,
    )
    duration_ms = round((time.monotonic() - t0) * 1000, 1)

    # Filter hallucinations from quiet audio or mic bleed.
    rms = float(np.sqrt(np.mean(audio_16k ** 2)))
    if rms < self._min_energy:
        logger.info("stt_filtered reason=low_energy rms=%.4f text=%s", rms, text[:60])
        text = ""
```
<!-- /snippet -->

### `PiperTTS`

Non-streaming LiveKit TTS plugin. `PiperChunkedStream._run` synthesizes
the full utterance (`PiperVoice.synthesize(text, syn_config=...)`) in
a worker thread, then pushes raw int16 PCM chunks to the framework's
`AudioEmitter`. Sample rate is whatever the ONNX model declares;
currently 22050 for `en_US-hfc_female-medium`.

---

## Qwen compatibility — [`qwen_compat.py`](../../voice-agent/src/live/qwen_compat.py)

Three exports, all addressing **one** class of problem: Qwen 2.5 7B
emits malformed tool-call JSON (usually an extra trailing brace) about
0.5–1% of the time.

- **`sanitize_json(raw)`** — find the first `{`, walk forward with a
  brace counter, drop everything after the matching `}`.
- **`install_function_args_patch()`** — monkey-patches
  `livekit.agents.llm.utils.prepare_function_arguments` to catch
  `ValueError("trailing characters")`, sanitize, and retry. Called
  once at import time in `agent.py`.
- **`wrap_llm_chat_with_history_sanitizer(local_llm)`** — wraps
  `local_llm.chat` so every `chat_ctx` gets its stored
  `FunctionCall.arguments` sanitized before hitting vLLM. This
  prevents vLLM 0.19 from crashing with 400 Bad Request when it
  re-parses chat history to build its prompt.

The whole file is a workaround. When we upgrade past Qwen 2.5 or vLLM
fixes the re-parse crash, delete it — nothing else depends on it.

<!-- snippet: sanitize_json -->
<!-- generated from voice-agent/src/live/qwen_compat.py:36 by docs/embed_snippets.py -->
```python
def sanitize_json(raw: str) -> str:
    """Extract the first balanced JSON object from a string.

    If `raw` doesn't start with `{` (e.g. an empty string or plain
    text), it's returned unchanged. Otherwise we find the matching
    `}` for the opening brace and drop anything after it. Logs when a
    sanitization actually happens, so the malformed inputs are
    traceable.
    """
    stripped = raw.strip()
    if not stripped.startswith("{"):
        return raw
    depth = 0
    for i, ch in enumerate(stripped):
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                cleaned = stripped[: i + 1]
                if cleaned != raw:
                    logger.info("sanitize_json original=%r cleaned=%r", raw[:120], cleaned)
                return cleaned
    return raw
```
<!-- /snippet -->

---

## Rooms — [`rooms.py`](../../voice-agent/src/live/rooms.py)

Not in Weaviate because the answer is fixed, short, and we want
deterministic behavior. `try_room_lookup(query)` gates on *both* a
room-number regex match AND at least one room-related keyword (English
or Czech) — otherwise "what is 2025" would hijack it. Returns a
directions dict or `None`; `query_search` falls through to Weaviate on
`None`.

Schema is deliberately sparse (`{"directions": str, ...optional
name}`) so updating means editing the dict — no migration, no reindex.

<!-- snippet: try_room_lookup -->
<!-- generated from voice-agent/src/live/rooms.py:65 by docs/embed_snippets.py -->
```python
def try_room_lookup(query: str) -> dict | None:
    """Try to answer a room/directions question from `BUILDING_ROOMS`.

    Two gates before we commit to a directions answer:
      1. The query must contain a room-number-like token (2-4 digits,
         optional letter suffix, matched by `_ROOM_NUMBER_RE`).
      2. It must also contain at least one room-related keyword
         (English or Czech). This prevents "what is 2025" or similar
         numeric questions from being hijacked.

    Returns:
      - `{"type": "directions", "room": ..., "floor": ..., "directions": ...}`
        on a full hit.
      - A `{"type": "directions", "error": ...}` dict if the number is
        a known room but directions are empty, or if the number isn't
        on our map at all.
      - `None` if the gates didn't match — caller should fall through
        to the Weaviate knowledge-base search.
    """
    match = _ROOM_NUMBER_RE.search(query)
    if not match:
        return None
    query_lower = query.lower()
    if not any(kw in query_lower for kw in _ROOM_KEYWORDS):
        return None

    room_number = match.group(1)

    for floor_id, rooms_on_floor in BUILDING_ROOMS.items():
        if room_number in rooms_on_floor:
            room = rooms_on_floor[room_number]
            directions = (room.get("directions") or "").strip()
            name = (room.get("name") or "").strip()
            if not directions:
                return {
                    "type": "directions",
                    "error": "no_directions",
                    "message": f"Room {room_number} is known but directions are not filled in yet.",
                }
            result = {
                "type": "directions",
                "room": room_number,
                "floor": floor_id,
                "directions": directions,
            }
            if name:
                result["name"] = name
            return result

    return {
        "type": "directions",
        "error": "room_not_found",
        "message": f"Room {room_number} is not in my map. I only know Building E rooms.",
    }
```
<!-- /snippet -->

---

## RAG — [`rag.py`](../../voice-agent/src/live/rag.py)

Three public entry points:

- **`connect_weaviate()`** — context-managed Weaviate client.
- **`seed_collection(client)`** — creates the collection schema and
  ingests every `.txt` under `SEED_DATA_PATHS` on first boot. No-op
  afterward. Run in background at entrypoint start so first request
  doesn't wait on ingestion.
- **`search_vectors(query, limit=5, alpha=0.7)`** — hybrid search,
  returns a list of dicts with `{id, title, content, source,
  created_at, distance, score}`. `alpha` controls the vector/BM25
  balance.

Vectors are produced server-side by Weaviate's `text2vec-openai`
module using `text-embedding-3-large`. That's a billed OpenAI
call per document ingested — mind it if you wipe the collection.

---

## System prompts & mode-specific quirks

Both prompts come from [config.py](../../voice-agent/src/live/config.py):
`OPENAI_SYSTEM_PROMPT` and `LOCAL_SYSTEM_PROMPT`. They share the same
core identity (`BASE_SYSTEM_PROMPT`) but diverge on tool framing:

- **OpenAI** — can handle explicit rules ("call `play_animation`
  exactly once per reply", "skip it only for …"). Realtime is
  obedient about this.
- **Local (Qwen 7B)** — needs tool use *motivated* ("call
  play_animation to check your body state before every reply"). The
  "checking body state" framing is what keeps Qwen 7B from ignoring
  the tool entirely. See [tools-issue.md](../notes/tools-issue.md).

If `ENABLE_LOOK_AROUND_TOOL` is set, a vision-specific paragraph is
appended to each prompt. The local version is more forceful ("you MUST
restate concrete details from it") because Qwen 7B otherwise returns
generic answers even after a successful caption.

---

## Config reference

Knobs most worth knowing — full list in
[config.py](../../voice-agent/src/live/config.py):

| Name                               | Effect |
|------------------------------------|--------|
| `PEPPER_AGENT_NAME`                | LiveKit agent identity for dispatch. |
| `PEPPER_AGENT_MODE` (prewarm hint) | `openai` skips loading Whisper/Piper at prewarm. |
| `LIVEKIT_URL`                      | LiveKit server to join. |
| `LOCAL_LLM_BASE_URL`               | vLLM endpoint (`http://localhost:8000/v1` via SSH tunnel to woska). |
| `LOCAL_LLM_MODEL`                  | Model name the vLLM server expects. |
| `LOCAL_STT_MODEL` / `DEVICE` / `COMPUTE_TYPE` | Whisper size + quantization. Default `tiny`+`int8` on CPU. |
| `LOCAL_TTS_MODEL_PATH`             | Piper ONNX file. Must exist at process start. |
| `SESSION_IDLE_TIMEOUT_SEC`         | How long of silence triggers history reset (default 60 s). |
| `ANIMATION_BRIDGE_URL`             | The robot bridge base URL (animations, LEDs, camera). |
| `ANIMATION_TOOL_HTTP_TIMEOUT_SEC`  | Fail fast if the bridge is slow — 2.5 s is plenty since the bridge acks 200 immediately. |
| `ENABLE_LOOK_AROUND_TOOL`          | Gate the vision tool. Off by default. |
| `LOOK_AROUND_VISION_BASE_URL`      | Side VL vLLM. Separate from `LOCAL_LLM_BASE_URL` on purpose. |
| `WEAVIATE_HOST` / `WEAVIATE_*`     | Where the knowledge-base lives. |
| `WEAVIATE_COLLECTION`              | `fel_v007` — bump when the chunking/schema changes. |
| `WEAVIATE_HYBRID_ALPHA`            | 0.7 = vector-leaning. Lower for more BM25. |
| `ANIMATION_GROUPS` / `ANIMATION_TOOL_ALIASES` | The contract between the agent's system prompt and the robot bridge's animation map. |

---

## Regenerating embedded snippets

The `<!-- snippet: NAME -->` blocks above are driven by region markers
(`# region: NAME` / `# endregion`) in the voice-agent source files. To
refresh them after editing source:

```bash
uv run python docs/embed_snippets.py
# or, in CI, fail if anything is stale:
uv run python docs/embed_snippets.py --check
```
