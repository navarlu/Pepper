# look_around — vision tool design log

Session snapshot so we can pick this back up later.

**Goal:** give Pepper spatial awareness — she calls a `look_around` tool, gets a description of what her top camera sees, uses it in her spoken reply.

**Current state (2026-04-16):** infrastructure is all built and verified end-to-end. The tool is **gated off** (`ENABLE_LOOK_AROUND_TOOL = False` in [config.py](../voice-agent/src/live/config.py:89)) because of a regression with the local Qwen-7B-Instruct model when 3 tools are exposed. The 2-tool baseline (`query_search` + `play_animation`) is restored to the known-good state from commit `08d8ddd`. Flip the flag to re-enable; everything else still wired and running.

---

## Architecture we landed on

After two false starts (see "What we tried" below), this is the design that actually works:

```
[voice-agent on woska, pepper-local]
    ├── main LLM calls (tools, text) → vLLM :8000 → Qwen2.5-7B-Instruct  (GPU 0, text-only)
    └── look_around() tool          → vLLM :8001 → Qwen2.5-VL-3B-Instruct (GPU 1, describer-only)
                                             ↑
                                    image + describe prompt
                                             ↓
                                    plain-text description returned as tool result
```

Main LLM stays text-only — tool calling works natively with `hermes` parser, no chat-template issues. The VL model is a dumb captioner: no tools, no hermes, no auto-tool-choice. It only knows "look at this image, describe it in one sentence".

### Why this pattern

- **Avoids** the documented Qwen2.5-VL chat-template-strips-tools problem (see "Phase 1: direct VL LLM" below).
- **Works for both modes** — openai (gpt-4o-realtime) and local (Qwen2.5-7B) both just see a tool that returns text.
- **Decoupled failure modes** — if the VL describer crashes, `look_around` gracefully returns `{"error": "describer_failed"}`. Main LLM keeps working.

---

## What we tried (chronological)

### Phase 1 — swap main LLM to Qwen2.5-VL-7B ❌

**Hypothesis:** use a vision-language model as the main LLM so it sees images natively via `ImageContent` injected into chat_ctx.

**Results:**
- Single GPU (one 20 GB RTX 4000 Ada): `ValueError: No available memory for the cache blocks`. Model weights 15.63 GiB + vision encoder cache ~1.3 GiB + overhead ~1 GiB = no room for KV cache.
- Tried aggressive tuning (`--enforce-eager`, `--max-num-seqs 1`, `--gpu-memory-utilization 0.95`, `--limit-mm-per-prompt '{"image":1}'`, smaller `max_pixels`): gains were not enough on one 20 GB card.
- Tensor-parallel across both GPUs (`--tensor-parallel-size 2`): ✅ **boots cleanly**, weights split to ~7.9 GB per card, Application startup complete.
- **But:** direct test with `tool_choice: "auto"` → model returned plain `"Hello!"` with `tool_calls: []`. The chat template for VL variants does not expose tool instructions to the model.
- With `tool_choice: "required"` → model DID emit a tool call. Proves the model *can* tool-call but the default VL chat template strips the tool-use markup.

This is a known issue:
- [HF: Qwen2.5-VL-32B-AWQ discussion #10](https://huggingface.co/Qwen/Qwen2.5-VL-32B-Instruct-AWQ/discussions/10) — commit `66c370b` removed tool support from the chat template; workaround `--tokenizer-revision 05440b7` stopped working in newer vLLM.
- [QwenLM/Qwen3-VL#1093](https://github.com/QwenLM/Qwen3-VL/issues/1093) — affects 7B and 72B VL variants.
- Not strictly an AWQ problem — the full-precision template has the same structure.

### Phase 2 — Qwen2.5-VL-3B single-GPU ❌

**Hypothesis:** smaller model fits on one card, same visual capability class.

**Results:**
- Boots cleanly on one 20 GB card.
- Same chat-template tool-calling issue as 7B.
- **Also:** hit a dumb name mismatch — `LOCAL_LLM_MODEL` in [config.py](../voice-agent/src/live/config.py) was pointing at `Qwen/Qwen2.5-VL-7B-Instruct` while vLLM was serving the 3B → every request returned `404 NotFoundError`. Fix: keep the model name in config in sync with what vLLM actually serves.

### Phase 3 — AWQ research ❌

Researched whether `Qwen/Qwen2.5-VL-7B-Instruct-AWQ` would let us fit on one GPU with working tools. Conclusion: **no, same class of chat-template issue** affects AWQ variants, and the model card says nothing about tools. Skip.

### Phase 4 — side-VL captioner ✅

Current design. See "Architecture we landed on" above. End-to-end verified:
- Bridge `POST /camera/snapshot` → 640×480 JPEG, ~55 KB, ~400 ms round-trip from woska.
- VL describer → accurate descriptions like *"A man is sitting at a desk with two computer monitors, a laptop..."* in ~1.5 s per call (after warm-up).
- Full pipeline (bridge fetch → VL describe) ~1.6 s.

### Phase 5 — 3-tool regression ❌

Adding `look_around` as the 3rd tool broke `play_animation`'s "always speak after tool" invariant on local Qwen 7B. Commit `08d8ddd` from earlier history is titled literally **"2 tools work, 3 is too much"** — Lucas had documented this exact pattern before.

Observed failure mode:
- User says "hello" → model calls `play_animation({animation:"greeting"})` → no spoken transcript follows.
- Sometimes works with `look_around` but ignores the returned description ("where are you?" → model falls back to generic "CTU FEE reception" answer instead of describing the desk it actually saw).

**Attempted mitigations that didn't fully fix it:**
- Added `STRICT RULE: every tool call is only the first step of a turn...` to `LOCAL_SYSTEM_PROMPT`.
- Added `"next_step": "Now speak a reply..."` field to both `look_around` and `play_animation` tool result payloads.
- Changed `look_around` return from JSON to plain text prefixed with `CAMERA VIEW:` (helps model use the content but doesn't fix the silent-after-tool issue).
- Tightened look_around docstring to mirror the working `play_animation_local` trick (*"Returns ... which you need before speaking"*).

**These same mitigations also broke the 2-tool baseline** — the `next_step` JSON field in `play_animation`'s result and the `STRICT RULE` prompt addition changed token shapes Qwen was handling correctly before. Reverted both, prompt is now byte-identical to commit `08d8ddd`.

### Phase 6 — current pause state ✅ (2-tool baseline restored)

- `ENABLE_LOOK_AROUND_TOOL = False` in config.
- `LOCAL_SYSTEM_PROMPT` matches commit `08d8ddd` exactly.
- `play_animation` result payload = `{"ok": true, "status": "queued", "animation": "Hey_X"}` (no extras).
- Tool list = `[query_search, play_animation_local]`.
- All look_around code preserved; prompt blocks + tool registration gated by the single flag.

---

## What's built and running (infrastructure that survives a reboot)

- **Bridge** ([robot/src/bridge.py](../robot/src/bridge.py)): `POST /camera/snapshot` endpoint, `capture_camera_snapshot()` helper (pauseAwareness → subscribe top cam VGA RGB → getImageRemote → releaseImage → unsubscribe → resumeAwareness), PIL-based thumbnail ≤768 px, JPEG q82. ALVideoDevice + ALBasicAwareness acquired as optional services. Serialized via `camera_lock` → 429 if concurrent. Verified 400 ms round-trip.
- **Woska vLLM instances** (launcher scripts at `/tmp/vllm_main_7b.sh` and `/tmp/vllm_vision_3b.sh`):
  - `LLM` tmux → `Qwen/Qwen2.5-7B-Instruct` on GPU 0, port 8000, hermes parser, auto tool choice.
  - `VL` tmux → `Qwen/Qwen2.5-VL-3B-Instruct` on GPU 1, port 8001, describer-only (no tool parser).
- **Voice-agent** code on woska at `/mnt/data_personal/navarlu2/work/Pepper/voice-agent/src/`:
  - `_fetch_camera_snapshot()` — blocking POST to bridge, returns JPEG bytes.
  - `_describe_image_with_vl()` — POSTs image+prompt to VL:8001, returns plain text.
  - `look_around` tool — fetches, describes, returns `CAMERA VIEW: <text> + nudge`. Gated off.
  - `LOOK_AROUND_*` config vars (URL, model, timeout, max_tokens, temperature, prompt).
  - `_OPENAI_LOOK_AROUND_BLOCK` and `_LOCAL_LOOK_AROUND_BLOCK` — prompt snippets, only appended when flag is True.
- **LED effect manager** (separate feature from this log but worth noting it's live): `/leds/state {mode:idle|search_pulse|off}` works on the bridge.

---

## The regression to solve before re-enabling look_around

Local Qwen2.5-7B-Instruct under vLLM + `hermes` parser + `tool_choice: "auto"` reliably fails to produce spoken text after a tool call when **three** tools are exposed. With **two** tools it works. We don't know exactly why; hypotheses:

1. **Context budget** — more tool descriptions + more conversation + tool result pushes something over a threshold that trips the model into `finish_reason: tool_calls` and it never generates the follow-up text. Could test by trimming tool descriptions to be terse.
2. **Model preference drift** — with 3 tools the model spends more tokens deliberating and sometimes stops after the first tool. Could test by explicitly requiring text output in the response format.
3. **Chat template inflation** — hermes parser template embeds each tool schema; 3 schemas may push the `<|im_start|>assistant` marker past some attention horizon. Could test with a simpler tool parser.

### Mitigations to try next session

Ranked by effort:

1. **openai mode (easy — no code)** — swap PEPPER_AGENT_MODE to `openai`, flip `ENABLE_LOOK_AROUND_TOOL=True`, test. gpt-4o-realtime handles multi-tool scenarios much better and has native vision — might just work out of the box. Good for validating the design before fighting Qwen.
2. **Make play_animation a side-effect, not a tool (medium)** — parse an inline marker from the model's spoken reply (e.g. `[[greeting]]`) and fire the animation after TTS begins. Drops tool count back to 2 (query_search + look_around) — returns to the known-working 2-tool baseline.
3. **Combined tool (medium)** — merge look_around + play_animation into a single `act(animation, look_around)` tool where the look_around portion is optional. One tool call per turn, fewer decision points.
4. **Tool-description minification (low)** — shrink all three tool docstrings to the bare minimum. Might reclaim the context budget.
5. **Different text LLM (medium)** — Llama 3.1 8B Instruct, Hermes 3 8B, or the newer Qwen3 when it's stable. Some are better at tool chaining.
6. **Ditch hermes, use xml tool parser (low risk)** — try `--tool-call-parser qwen3_xml` or a custom parser. Might have different behaviour with 3+ tools.

### How to re-enable when you're ready

```bash
# 1. Flip the flag
# edit voice-agent/src/live/config.py line 89:
#   ENABLE_LOOK_AROUND_TOOL = True

# 2. Deploy to woska (don't forget — auto-sync is NOT set up)
scp -J navarlu2@halmos.felk.cvut.cz \
  voice-agent/src/live/tools.py voice-agent/src/live/config.py \
  navarlu2@woska:/mnt/data_personal/navarlu2/work/Pepper/voice-agent/src/

# 3. Confirm reload
ssh -J navarlu2@halmos.felk.cvut.cz navarlu2@woska \
  'tmux capture-pane -t pepper-agent2 -p -S -50 | grep "registered worker" | tail -2'
```

If the vLLM instances aren't running (woska rebooted etc.), restart them:

```bash
ssh -J navarlu2@halmos.felk.cvut.cz navarlu2@woska \
  'tmux send-keys -t LLM "bash /tmp/vllm_main_7b.sh" Enter'
# wait ~3 min for boot
ssh -J navarlu2@halmos.felk.cvut.cz navarlu2@woska \
  'tmux new-session -d -s VL -x 220 -y 50 2>/dev/null; tmux send-keys -t VL "bash /tmp/vllm_vision_3b.sh" Enter'
```

---

## Quick reference — files touched for this feature

- [robot/src/bridge.py](../robot/src/bridge.py) — `capture_camera_snapshot()`, `POST /camera/snapshot`, ALVideoDevice + ALBasicAwareness wiring.
- [voice-agent/src/live/tools.py](../voice-agent/src/live/tools.py) — `_fetch_camera_snapshot()`, `_describe_image_with_vl()`, `look_around` tool (inside `build_tools()`), gated by `ENABLE_LOOK_AROUND_TOOL`.
- [voice-agent/src/live/config.py](../voice-agent/src/live/config.py) — `ENABLE_LOOK_AROUND_TOOL` flag, `LOOK_AROUND_*` knobs, conditional system-prompt blocks.
- [robot/tests/camera_snapshot.py](../robot/tests/camera_snapshot.py) — standalone smoke test for the bridge endpoint's logic (predates this feature; still useful).

## Quick reference — tmux sessions on woska

| Session | Purpose | Port |
|---|---|---|
| `LLM` | main text-only vLLM (`Qwen2.5-7B-Instruct`) | 8000 |
| `VL` | side VL describer (`Qwen2.5-VL-3B-Instruct`) | 8001 |
| `pepper-agent2` | voice-agent in local mode (auto-reloads on scp via watchfiles) | — |
| `pepper-agent` | (unused legacy) | — |
