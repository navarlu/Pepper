"""Paper (Track B) on-robot source — RPi-only realtime receptionist MVP.

A minimal LiveKit Agents stack around an OpenAI realtime model
(speech-to-speech) with 2 tools, talking to Pepper in Czech. Design
doc: docs/paper/paper_code_plan.md. The offline benchmark (Track A)
lives in docs/paper/benchmark/ and is unrelated to this package.

Modules:
  - agent_realtime.py : the LiveKit Agents worker (RealtimeModel + tools)
  - prompt.py         : Czech SYSTEM_PROMPT
  - dispatcher.py     : keeps one agent dispatched into the fixed room
  - tools/            : the 2-tool surface (find_room, lookup_person)
"""
