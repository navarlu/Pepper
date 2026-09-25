"""Realtime (speech-to-speech) agent variant for the paper stack.

A minimal LiveKit Agents worker around an OpenAI realtime model with 2
tools, talking to Pepper in Czech (`realtime` compose profile).

Modules:
  - agent_realtime.py : the LiveKit Agents worker (RealtimeModel + tools)
  - prompt.py         : Czech SYSTEM_PROMPT
  - dispatcher.py     : keeps one agent dispatched into the fixed room
  - tools/            : the 2-tool surface (find_room, lookup_person)
"""
