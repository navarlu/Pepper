# Text Chat CLI

A terminal-based debug tool for talking to Pepper as if you were the
in-room user — without having to speak. Useful when iterating on agent
behavior, tools, or prompts and you want a fast loop without the audio
pipeline in the way.

Source: [tools/text_chat.py](../../tools/text_chat.py)

---

## How it works

The CLI joins the active LiveKit room **as the same identity that
`user-client` normally uses** (`"user"`). It does this by reading the
token the orchestrator already wrote to disk:
[services/src/session_manager/data/token-latest.json](../../services/src/session_manager/data/token-latest.json).

The voice-agent's session is bound to that one specific identity (see
[voice-agent/src/agent.py](../../voice-agent/src/agent.py) — the
`participant_identity=` argument to `session.start()`). Text from any
other identity is silently dropped. That's why the CLI must impersonate
`user` rather than join with a separate identity like `text-cli`.

Because LiveKit doesn't allow two simultaneous connections with the same
identity, you must stop `user-client` before starting the CLI (or use
`/mic off` from inside the CLI — see below).

```
┌──────────────┐    lk.chat (text)        ┌────────────────┐
│ text_chat.py │ ───────────────────────► │  voice-agent   │
│  identity:   │                          │ (same session  │
│   "user"     │ ◄─── transcription ────  │  as voice path)│
│              │ ◄─── pepper.debug ────   │                │
│              │ ─── pepper.control ────► │                │
└──────────────┘                          └────────────────┘
```

Three LiveKit topics are in play:

| Topic            | Direction       | Purpose                                       |
|------------------|-----------------|-----------------------------------------------|
| `lk.chat`        | CLI → agent     | Text input (replaces voice).                  |
| `pepper.debug`   | agent → CLI     | Tool calls (name, args, result, duration).    |
| `pepper.control` | CLI → agent     | Out-of-band commands (currently: `reset`).    |

Tool-call broadcasting is hooked at the central `_post_tool_event` in
[voice-agent/src/tools.py](../../voice-agent/src/tools.py) — the agent
registers a listener at startup that forwards every fired tool over
`pepper.debug`. So *every* tool call shows up in the CLI, regardless of
which mode (`openai` / `local`) is active.

---

## Starting the CLI

```bash
# 1. Free the "user" identity (kills the mic path):
docker compose -f docker/docker-compose.yml stop user-client

# 2. Run the CLI:
uv run python tools/text_chat.py

# 3. When done, restart the mic path (or use /mic on inside the CLI):
docker compose -f docker/docker-compose.yml start user-client
```

Connection details (room name, ws URL) are read automatically from the
orchestrator's token file — no flags needed.

---

## Slash commands

Type `/help` inside the CLI to see this list at any time.

| Command            | What it does                                                                   |
|--------------------|--------------------------------------------------------------------------------|
| `/help`            | Show the list of commands.                                                     |
| `/status`          | Snapshot of room id, current mode, your identity, all participants, user-client docker state, and how old the orchestrator's token is. |
| `/mode <openai\|local>` | Switch the agent mode by writing `services/src/orchestrator_config.json`. The orchestrator picks this up within ~3s, deletes the current room, creates a new one, and dispatches a warm agent of the new mode. **Note:** the room name changes — you'll need to `/quit` and re-launch the CLI to pick up the new tokens. |
| `/mic <on\|off>`   | Start or stop the `user-client` docker container. Use `/mic off` to free the identity for the CLI; `/mic on` to bring the voice path back. |
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
  participants agent-AJ_xxx, listener-python, user
  user-client exited
  session     generated 2m4s ago
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
