# Debug session — 2026-04-25 (lab-move + tool-calling)

This is a chat-derived report capturing what we thought, what was actually
true, and the working recipes. Goal: don't repeat these traces.

## TL;DR — the things future-you should know

1. **Pepper's IP changes by room/network. Always run [`robot/scripts/pepper_catch_fire.py`](../../robot/scripts/pepper_catch_fire.py)** to discover her, run safe_startup, and print the right `PEPPER_QI_URL` to feed back to docker.
2. **Direct cable to Pepper without a DHCP server → APIPA (`169.254.x.x`) on her side**, so the RPi `eth0` profile must have a 169.254/16 secondary address (already in `pepper-ethernet` NM profile). Verify with `nmcli con show pepper-ethernet | grep ipv4.addresses`.
3. **DJI mic invisible inside the user-client container** has TWO root causes that BOTH must be fixed:
   - `/proc/asound/cards` is masked by default → fix is `privileged: true` on user-client (already in compose).
   - PipeWire on the host grabs the USB audio device exclusively → must `systemctl --user mask pipewire pipewire-pulse wireplumber` (already done, persists across reboots).
   - Whenever the DJI USB connection changes (replug, RPi reboot with mic plugged in late), **restart user-client** to refresh ALSA enumeration.
4. **Qwen 2.5 7B + vLLM hermes parser tool calling has THREE specific landmines** — fix all three or the parser leaks `<tool_call>` / `<|im_start|>` into spoken text:
   - Tool name `play_animation` is poison; rename to `play_pose` for the LiveKit-registered name.
   - System prompt: avoid **newlines in long prompts**. Single-line is safe at any length; multi-line is fine if short. Long + multi-line = broken.
   - Sampling: `temperature=0.01`, `top_p=0.8`, `repetition_penalty=1.05`, and tools should return DATA (not None) so the SDK re-calls the LLM and produces clean text in call 2.

---

## Issue 1 — Pepper's IP / safe_startup race

### What we thought
- "Just update `PEPPER_QI_URL` in compose, restart safe-startup, done."
- "If she halts, we missed her — try a faster watchdog poll."

### What was actually true
- Pepper's DHCP IP is stable per-network: **lab WiFi gives her `192.168.210.113`** every time. In makeshift setups (no DHCP, direct cable) she falls back to **APIPA `169.254.x.x`**, which is RANDOM each boot.
- The OS-level "halt because safe_startup didn't run" fires **fast**. The dockerized `safe_startup_watchdog.py` path takes minutes worst-case (container recreate + 5s `POLL_INTERVAL_OFFLINE` + 5s `CONNECT_TIMEOUT_SEC` TCP probes + 10s qi handshake + up to ~25s waiting for `ALAutonomousLife` service to appear — that one's the killer, observed at 23s in the lab logs). Pepper often halts before all that completes. That's why the catcher in `robot/scripts/pepper_catch_fire.py` invokes safe_startup **directly via host-side qi** the moment NAOqi port 9559 opens, with no docker overhead.
- `safe_startup_watchdog.py` polls TCP 9559 silently (no per-poll log), so absent log lines doesn't mean it's broken.

### Working recipe
```bash
# From project root, with vLLM running:
uv run python robot/scripts/pepper_catch_fire.py             # auto-discover
uv run python robot/scripts/pepper_catch_fire.py 192.168.210.113   # hint a known IP

# After it prints the IP:
PEPPER_QI_URL=tcp://<IP>:9559 docker compose -f docker/docker-compose.yml \
    up -d --force-recreate safe-startup bridge
```

The catcher does a lot in parallel (probes known IPs, sweeps eth0 subnets and 169.254/16, watches `ip neigh` for Aldebaran-OUI MACs `00:13:95:*`), races NAOqi port 9559 once it gets an IP, then runs the safe_startup sequence directly via host-side qi (no docker overhead).

### Side notes from this session
- We discovered in one boot her eth0 had **775M `rx_resource_errors`** — sounded scary but turned out to be a transient burst that stopped on its own (rate dropped to 0/sec). Didn't break anything once the link settled.
- **Eth link "up" with 0 ingress packets** is a real state we observed multiple times — `ethtool eth0` showed link/1Gb negotiated, but `cat /sys/class/net/eth0/statistics/rx_packets` was frozen across an entire boot cycle. Hypothesis (NOT confirmed in this session, but worth trying first): Pepper's OS may have prioritized wifi over eth0, so the IP she announces with "what is my IP" might be wifi APIPA while eth0 sits idle. Possible mitigations to try: unplug+replug her ethernet so her network stack re-init; or ensure her wifi profile fails fast (we didn't go this route).

---

## Issue 2 — DJI mic missing from container

### What we thought (and was wrong)
- "USB device must not be connected." — `lsusb` showed it was.
- "ALSA in container needs restart." — only partially helped.
- "Need to bind-mount `/proc/asound`." — runc's proc-safety check refuses bind-mounts inside `/proc`. Won't even start the container.

### What was actually true (TWO compounding causes)
1. **Docker's default container has `/proc/asound` masked.** PortAudio/sounddevice can't enumerate ALSA cards without it, so the DJI's `hw:2,0` is invisible in the container even though `/dev/snd/pcmC2D0c` is bind-mounted. Fix: `privileged: true` (or `security_opt: [systempaths=unconfined]`).
2. **PipeWire on the host grabs the USB audio device exclusively.** Once PipeWire has it open, ALSA clients (incl. our container) get `Device or resource busy`. PipeWire is unnecessary on this headless RPi — masked it permanently.

### Working recipe (already applied to RPi & compose)
- `docker/docker-compose.yml` has `privileged: true` on `user-client`.
- PipeWire masked: `systemctl --user mask pipewire pipewire-pulse wireplumber` (this **persists across reboots** for user `lucas`).
- After any USB topology change: `docker compose -f docker/docker-compose.yml restart user-client`.

### How to diagnose if the mic loop returns
```bash
# On the host:
lsusb | grep DJI                                          # confirm mic plugged
cat /proc/asound/cards                                    # confirm card 2 = DJI MINI
fuser /dev/snd/pcmC2D0c 2>&1                              # who holds it?
systemctl --user is-active pipewire pipewire-pulse        # should be inactive

# Inside container:
docker compose -f docker/docker-compose.yml exec user-client \
    python3 -c "import sounddevice; [print(d) for d in sounddevice.query_devices() if d['max_input_channels']>0]"
# ← must list the DJI; if empty, container restart needed
```

---

## Issue 3 — Local voice agent tool calling

This was the deepest rabbit hole. Many wrong hypotheses before the actual root causes.

### What we thought (and was wrong)
- ❌ "Qwen 2.5 7B can't handle 3+ tools." — already disproven in `tools-issue.md` Attempt 10 (4 *generic* dummy tools = 100% pass). Re-confirmed here that count itself isn't the limit. What we did NOT prove this session is "any 3-tool combo with our pose tool works fine" — `pair_pose+search` got 57.5% and `trio (pose+search+time)` got 41.7%. So *individual* tool schemas (the `play_pose` schema, even renamed, has some baseline instability) can hurt at any count. The takeaway: count is not the constraint, but per-tool schema interactions stack — keep the tool-set minimal AND each tool's schema clean.
- ❌ "Tool description framing matters most ('returns body state' is misleading)." — Mild effect (~17pp) but not the root cause; even with cleaned descriptions, leaks persisted with `play_animation` name.
- ❌ "Returning `None` from `play_pose` to avoid double-response is the right move." — Solves greetings but **breaks chains AND causes silent hellos** (model picks tool-only at call 1, never produces spoken text).
- ❌ "Long system prompt is the issue." — Length alone doesn't matter; a 327-char single-line prompt has 0 leaks, same content with 5 newlines has 3/4 leaks.
- ❌ "We need to drop `play_animation` and drive animation from agent code." — Premature giving-up. The actual fix was much smaller.
- ❌ "Higher temperature (Qwen-recommended 0.7) helps." — In multi-turn, **temp=0.7 INTRODUCES leakage** that 0.01 doesn't have.

### What was actually true (THREE landmines that all need fixing)

**Landmine 1 — the tool name `play_animation` itself triggers hermes parser failures.**

Direct empirical evidence (`voice-agent/tests/tool_prompt_diff.py`, 4-step ladder where each step changes ONE thing relative to its predecessor):

```
A (search_kb + play_pose + get_time, prompt mentions all 3):  Hello ✅  Room ✅  Bye ❌
B (drop get_time → 2 tools):                                  Hello ✅  Room ✅  Bye ✅
C (rename search_kb → query_search):                          Hello ✅  Room ✅  Bye ✅
D (rename play_pose → play_animation):                        Hello ❌  Room ✅  Bye ❌
```

Reading: A→B says "adding get_time degrades Bye"; B→C says "renaming search_kb→query_search has no effect"; **C→D says "renaming play_pose→play_animation breaks Hello and Bye"**. That C→D delta is the load-bearing finding.

It's a tokenizer interaction — `play_animation` tokenizes in a way that makes Qwen 2.5 7B emit chat-template tokens (`<|im_start|>`) inside its `<tool_call>` blocks. The hermes parser then JSON-decodes those and crashes, falling back to "all is text content" — leaks visible to TTS.

**Fix:** in `voice-agent/src/tools.py`, register the local-mode variant as `@function_tool(name="play_pose")` with parameter `pose: str` (passes through to the same `_play_animation_impl`).

**Landmine 2 — newlines in long system prompts.**

`voice-agent/tests/prompt_format_test.py`:

| variant | chars | newlines | leaks |
|---|---|---|---|
| short single-line | 157 | 0 | 0/4 |
| **long single-line** | **327** | **0** | **0/4** |
| long, `\n` per sentence | 327 | 5 | 3/4 |
| long, `\n\n` per sentence | 332 | 10 | 4/4 |
| short with newlines | 157 | 3 | 0/4 |

Length alone is fine; newlines alone (in short prompts) are fine. **Length × newlines** kills it.

**Fix:** in `voice-agent/src/config.py` `LOCAL_SYSTEM_PROMPT`, use Python string concatenation across lines (which renders as one logical line). Don't put literal `\n` or use triple-quoted multi-line strings.

**Landmine 3 — sampling + tool-return shape interaction.**

In multi-turn:
- temp=0.7 + pose returns DATA: 3/6, 1 leak
- temp=0.01 + pose returns DATA: **6/6, 0 leaks** ← winner
- temp=0.01 + pose returns NONE: 5/6 (Goodbye misses animation; greeting goes silent)

Counter-intuitive: at low temp, the model is deterministic enough to pick **tool-only on call 1, then full text on call 2** when the tool returns data. So you get the SDK's natural re-call → spoken text, with no duplicate from call 1. At higher temp, the model emits text + tool together → re-call → duplicate.

**Fix:**
- `agent.py` local LLM: `temperature=0.01, top_p=0.8, extra_body={"repetition_penalty": 1.05}`.
- `tools.py` `_play_animation_impl` local branch: `return json.dumps({"ok": True, "pose": resolved})` (NOT `None`).

### The full working production config

| File | Change |
|---|---|
| `voice-agent/src/tools.py` | `@function_tool(name="play_pose")`, parameter `pose: str`, returns `{"ok": True, "pose": resolved}` |
| `voice-agent/src/agent.py` | `temperature=0.01, top_p=0.8, extra_body={"chat_template_kwargs": ..., "repetition_penalty": 1.05}` on local-mode `openai.LLM(...)` |
| `voice-agent/src/config.py` | `LOCAL_SYSTEM_PROMPT` = 4 imperative sentences, single logical line: "You are Pepper, a robot receptionist. Always call query_search to look up facts before answering. Always call play_pose before speaking. Be brief and polite." |

Verified end-to-end: 9-turn conversation (greeting, room, dean, cafeteria hours, location, "how are you", joke, "can you sing", goodbye) → **9/9 turns, 0 leaks, all factual queries hit `query_search`, all tool extraction clean**.

### Test scripts left in `voice-agent/tests/` for future debugging
- `tool_chat_repl.py` — interactive REPL with `/tools`, `/temp`, `/system`, `/pose-return`, etc.
- `tool_systematic_test.py` — full grid of (tool_combo × prompt × sampling)
- `tool_pose_design_test.py` — A/B over 5 play_pose schema variants (V0..V4)
- `tool_multiturn_test.py` — end-to-end conversation with sweeps
- `tool_prompt_diff.py` — the A→B→C→D ladder that isolated `play_animation` as the bad name
- `prompt_format_test.py` — the length × newlines test (Landmine 2 evidence)
- (Run from project root: `uv run python voice-agent/tests/<script>`)

---

## Recovery checklist (cold-start after a room move)

```bash
# 1. Power on Pepper, wait ~90s for the boot chime.

# 2. Discover her IP + run safe_startup before she halts:
cd /home/lucas/Projects/FEL/Pepper
uv run python robot/scripts/pepper_catch_fire.py
# → prints "PEPPER_QI_URL=tcp://X.X.X.X:9559" at the end

# 3. Point docker services at her:
PEPPER_QI_URL=tcp://X.X.X.X:9559 \
  docker compose -f docker/docker-compose.yml up -d --force-recreate safe-startup bridge

# 4. Verify:
docker compose -f docker/docker-compose.yml logs --tail 5 safe-startup
# → "Pepper already online at startup — entering idle monitoring"

# 5. Mic check (if it was unplugged/moved):
docker compose -f docker/docker-compose.yml restart user-client
docker compose -f docker/docker-compose.yml logs --tail 5 user-client
# → should see "mic=live, frames=N, healthy=True"

# 6. If gestures stop working (life state lingering disabled), one-shot:
PYTHONPATH=/home/lucas/Projects/FEL/QI_test/libqi-python/build/build/linux-armv8-gcc-release \
LD_LIBRARY_PATH=/home/lucas/.conan2/p/b/boost00dddf9f5dc9e/p/lib:/home/lucas/Projects/FEL/QI_test/local_qi/lib \
uv run python -c "
import qi
s = qi.Session(); s.connect('tcp://<IP>:9559', _async=True).value(10000)
life = s.service('ALAutonomousLife'); life.setState('solitary')
for n,v in [('AutonomousBlinking',True),('BackgroundMovement',True),('BasicAwareness',False),('ListeningMovement',False),('SpeakingMovement',True)]:
    life.setAutonomousAbilityEnabled(n, v); print(n, v)
"
```

---

## Things to avoid in the future

- **Don't reach for "drop the tool" or "rewrite agent" before doing held-everything-constant A/B.** Both major issues this session (`play_animation` name + newlines) were 1-line fixes hiding under a pile of confidence about other root causes.
- **Don't single-shot-test what's a multi-turn problem.** Single-shot will hide double-response, chain breakage, and history-pollution issues.
- **Don't use `--force-recreate` mid-incident if you can `restart`** — recreate slow path can blow your halt-time budget on Pepper.
- **Don't trust `tool_calls=0` in vLLM logs as proof tools weren't called** — hermes parser swallowing a malformed JSON tool call yields the same signal as "model produced text only", but with garbage `<tool_call>` markers buried in the text.
- **Don't put the catcher script in `/tmp/`** — it'll get wiped on reboot. Now lives in `robot/scripts/pepper_catch_fire.py`.

## Things we now know that aren't in the older docs

- The 2-tool limit was already disproven in `tools-issue.md` Attempt 10 (4 generic dummy tools = 100% pass). What was NOT in the older docs: that **specific tool names** independently destabilize the parser, and the leak rate stacks across tools. Concrete data from this session:
  - `solo_pose` (just `play_pose`, baseline schema): 66.7% OK / 83.3% OK+text-dirty (systematic test, single-shot, mixed scenarios)
  - `pair_search+time` (no pose at all): 96.9% — 2 tools work near-perfectly when both are clean
  - 2-tool combo `query_search + play_animation` vs `query_search + play_pose` with the same prompt: play_animation leaks consistently on greetings/goodbyes; play_pose doesn't (`voice-agent/tests/tool_prompt_diff.py` setup D vs C). We did NOT isolate `play_animation` solo (1-tool) in this session, so I can't claim a number for that case.
  - Multi-turn at temp=0.01 + V1 schema + short prompt + DATA return: 6/6 → 9/9 zero leaks. That's the load-bearing result for the production config.
- `tools-issue.md` Attempt 7 said "single-line system prompt with tool instructions" — true, but **the actual rule is "no newlines in long prompts"**. Long single-line is fine, short multi-line is fine.
- The "Qwen needs the tool to be framed as returning needed data" guidance from Attempt 7 was overgeneralized — the real driver is the **tool name's tokenization**, not its description framing.
- The `livekit/agents#4554` "double-response after non-None tool result" workaround (return None from pose) **trades a worse failure mode** in multi-turn (silent hellos, broken chains). At temp=0.01 with data return, the issue doesn't manifest.
