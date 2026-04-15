# Text Chat CLI

A terminal-based debug tool for talking to Pepper as if you were the
in-room user — without having to speak. Useful when iterating on agent
behavior, tools, or prompts and you want a fast loop without the audio
pipeline in the way.

Source: [services/src/text_chat.py](../../services/src/text_chat.py)

---

## How it works

The CLI joins the active LiveKit room as a **separate identity**
(`"debug-cli"`), subscribe-only. It coexists with `user-client` (which
stays connected as `"user"` for the audio path) — no need to stop the
voice path to chat.

The token for `debug-cli` is provisioned by the orchestrator alongside
the existing `user` / `listener` / `monitor` tokens, all written to
[services/data/token-latest.json](../../services/data/token-latest.json).

To keep the agent's view consistent ("text always looks like user
input"), text from `debug-cli` is sent on a custom topic `pepper.text`
rather than `lk.chat`. The agent has a handler that calls
`session.generate_reply(user_input=text)` directly — the LLM only sees
"user input", never knows the LiveKit identity of the sender. So
whether you speak via mic or type via CLI, the agent's chat context
looks identical.

```
┌──────────────┐                          ┌────────────────┐
│ user-client  │ ──── audio (mic) ──────► │                │
│ identity:    │                          │  voice-agent   │
│   "user"     │ ◄── pepper.control ────  │ (one session,  │
│              │     (mic mute/unmute)    │  bound to user)│
└──────────────┘                          │                │
                                          │                │
┌──────────────┐                          │                │
│ text_chat.py │ ─── pepper.text ───────► │                │
│ identity:    │ ─── pepper.control ────► │                │
│  "debug-cli" │     (reset)              │                │
│ subscribe-   │ ◄── lk.chat ───────────  │                │
│  only        │     (transcriptions)     │                │
│              │ ◄── pepper.debug ──────  │                │
│              │     (tool calls)         │                │
└──────────────┘                          └────────────────┘
```

LiveKit topics in play:

| Topic            | Direction                     | Purpose                                                     |
|------------------|-------------------------------|-------------------------------------------------------------|
| `pepper.text`    | debug-cli → agent             | Text input. Agent feeds it to the session as user input.    |
| `pepper.control` | debug-cli → agent / user-client | `{"cmd":"reset"}` → agent clears chat ctx. `{"cmd":"mic","muted":bool}` → user-client toggles its `mic_muted` flag. |
| `pepper.debug`   | agent → all                   | Tool calls (name, args, result, duration).                  |
| `lk.chat`        | agent → all                   | Agent transcriptions (also accepts text input from `user`). |

Tool-call broadcasting is hooked at the central `_post_tool_event` in
[voice-agent/src/tools.py](../../voice-agent/src/tools.py) — the agent
registers a listener at startup that forwards every fired tool over
`pepper.debug`. So *every* tool call shows up in the CLI, regardless of
which mode (`openai` / `local`) is active.

---

## Starting the CLI

```bash
uv run python services/src/text_chat.py
```

That's it. No need to stop user-client first. Room name, ws URL, and
the `debug-cli` token are all read from the orchestrator's session file.

If you don't want the mic to pick up ambient sound while you're
debugging via text, run `/mic off` after connecting — that sends silent
frames from user-client, no disconnect.

---

## Slash commands

Type `/help` inside the CLI to see this list at any time.

| Command            | What it does                                                                   |
|--------------------|--------------------------------------------------------------------------------|
| `/help`            | Show the list of commands.                                                     |
| `/status`          | Snapshot of room id, current mode, your identity, all participants, whether `user` is connected, current mic state, and how old the orchestrator's token is. |
| `/mode <openai\|local>` | Switch the agent mode by writing `services/src/orchestrator_config.json`. The orchestrator picks this up within ~3s, deletes the current room, creates a new one, and dispatches a warm agent of the new mode. **Note:** the room name changes — you'll need to `/quit` and re-launch the CLI to pick up the new tokens. |
| `/mic <on\|off>`   | Soft mute/unmute of `user-client`'s mic. `off` makes it send silent frames; `on` re-enables mic capture. user-client stays connected to the room either way — no docker restart. |
| `/reset`           | Tell the agent to clear its chat history. Same effect as the 60s idle timeout, but on demand. The agent stays warm — only the conversation context is wiped. |
| `/quit`            | Disconnect and exit. `Ctrl-D` works too.                                       |

Anything that doesn't start with `/` is sent to the agent as a chat
message.

---

## What appears in the CLI

```
You> hello pepper
  [agent-AJ_xxx]: Hello! How can I assist you today?
You> wave hello
  [tool ✓] play_animation({"animation": "greeting", "resolved": "Hey_1"}) -> {"body_state": "ready", "posture": "Hey_1"} [12ms]
  [agent-AJ_xxx]: Here's a wave for you!
You> /reset
Reset signal sent — agent will clear chat history shortly.
You> /status
─── status ───
  room        pepper-1776244635
  mode        openai
  identity    user
  participants agent-AJ_xxx, debug-cli, listener-python, user
  user-client  connected
  mic          live
  session      generated 2m4s ago
──────────────
```

Live event lines (printed asynchronously when they happen):

| Prefix          | Meaning                                                                           |
|-----------------|-----------------------------------------------------------------------------------|
| `[agent-XXX]:`  | An agent message (transcription or chat reply).                                   |
| `[tool ✓] ...`  | A tool fired successfully. Args/result truncated to ~200 chars per field.         |
| `[tool ✗] ...`  | A tool fired with an error (followed by `[tool err] ...`).                        |
| `[room] + id`   | A participant joined the room.                                                    |
| `[room] - id`   | A participant left the room.                                                      |
| `[mode] X -> Y` | Someone (you or another tool) changed the agent mode.                             |

---

## "Create a new session" — what does that mean?

There are two layers:

1. **Conversation session** — fresh chat history, same warm agent, same
   room: use `/reset`. The agent logs
   `[PERSIST] resetting chat history session_num=N`.
2. **LiveKit room session** — brand new room, fresh tokens, fresh agent
   dispatch: toggle the mode (`/mode local` then `/mode openai`), or
   restart the orchestrator. After this the room name changes, so
   `/quit` and re-launch the CLI.

For day-to-day debugging `/reset` is almost always what you want.

---

## Troubleshooting

**Symptom: "Token file missing user.token …"**
The orchestrator hasn't dispatched yet. Wait a few seconds after
`docker compose up`, or check
`docker compose -f docker/docker-compose.yml logs orchestrator`.

**Symptom: connect succeeds but the agent never replies**
- Two stale agents in the room (you'll see this in `/status` —
  multiple `agent-AJ_*` entries). Force a fresh dispatch with
  `/mode local` then `/mode openai`. Then `/quit` and re-launch.
- Agent in `local` mode but the woska-side tmux agent isn't running,
  or the woska agent has stale code (see
  [running.md](running.md) section 4 for the deploy steps).

**Symptom: tool fires (Pepper moves) but no `[tool ✓]` line in the CLI**
The voice-agent is running stale code. Restart it:
```bash
docker compose -f docker/docker-compose.yml restart voice-agent
```
And in `local` mode, also restart the woska tmux agent (it doesn't
auto-reload).

**Symptom: agent replies twice / two identities reply**
Stale agent dispatches still in the room. Fix as above with the
`/mode` toggle, or restart the orchestrator.
